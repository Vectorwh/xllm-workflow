"""Word delivery behavior using synthetic evidence; no model or workload runs."""
import json
from copy import deepcopy
from pathlib import Path
import posixpath
import sys
from zipfile import ZipFile

from docx import Document
from lxml import etree
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/xllm-release-validation/scripts"))
import export_delivery_docx as exporter


@pytest.fixture
def inputs(tmp_path):
    from release_common import expand, load_config
    base = expand(load_config(ROOT / "skills/xllm-release-validation/assets/release.example.yaml"))[0]
    base["xtask"]["commit"] = "b" * 40
    base["delivery"]["xtask_native"]["image"] = "fixture/xllm:release"
    summary = {"release": {"version": "0.12.0", "commit": "a" * 40},
        "counts": {"PASS": 1, "FAILED": 1}, "claim_scope": "measurement-only", "cases": []}
    for index, status in enumerate(["PASS", "FAILED"]):
        row = deepcopy(base)
        row.update(case_id=f"fixture-{index}", kind="performance", mode="direct")
        row["model"]["name"] = "FixtureModel"
        row["workload"].update(parallel=index + 1, input_tokens=2048, output_tokens=40)
        row["result"] = {"status": status, "attempt": f"cases/fixture-{index}/attempt-001",
                "metrics": {"request_throughput": 2.5, "avg_latency_s": .384, "ttft_ms": 150,
                    "tpot_ms": 6, "itl_ms": 7, "output_tps": 100, "success": 4-index, "total": 4},
                "raw_summary": {"Unmodeled native metric": 0, "Avg TTFT (ms)": 150},
                "raw_percentiles": [{"Percentiles": "99%", "TPOT (ms)": 10}]}
        summary["cases"].append(row)
    content = {"schema_version": 1, "report": {key.lower(): "fixture narrative" for key in exporter.REQUIRED},
        "blocks": {"xllm_start_script": {"file": "start.sh"}},
        "tables": {"prs": [{"pr": "#fixture", "description": "fixture change", "status": "未合入"}]},
        "options": {"include_multimodal": False, "include_raw_metrics": True}}
    content["report"].update(title="测试数据交付验证", author="Fixture Author", date="2026-11-18", models="FixtureModel")
    (tmp_path / "start.sh").write_text("#!/bin/bash\nprintf '%s' 'literal $VALUE; $(not_executed)'\n")
    for row in summary["cases"]:
        attempt = tmp_path / row["result"]["attempt"]
        (attempt / "service").mkdir(parents=True)
        (attempt / "service/start.sh").write_text((tmp_path / "start.sh").read_text())
        (attempt / "eval.sh").write_text("evalscope perf --parallel " + str(row["workload"]["parallel"]))
    (tmp_path / "results.json").write_text(json.dumps(summary))
    (tmp_path / "content.yaml").write_text(yaml.safe_dump(content))
    return summary, content


def test_word_metrics_rows_scripts_and_optional_sections(tmp_path, inputs):
    output = tmp_path / "delivery.docx"
    result = exporter.export(tmp_path / "results.json", tmp_path / "content.yaml", output)
    document = Document(output)
    body = "\n".join(p.text for p in exporter.paragraphs(document))
    assert result["measurement_cases"] == 2 and result["render_required"]
    for expected in ["0.384", "150", "Unmodeled native metric", "99%", "FAILED", "literal $VALUE; $(not_executed)"]:
        assert expected in body
    assert "150000" not in body  # Already normalized milliseconds must not be scaled again.
    assert "多模态模型评测" not in body
    assert "{{" not in body
    assert document.paragraphs[0].text == "测试数据交付验证"
    assert document.sections[0].page_width.cm == pytest.approx(21, abs=.01)
    with pytest.raises(ValueError, match="new .docx"):
        exporter.export(tmp_path / "results.json", tmp_path / "content.yaml", output)


def test_no_stale_measurements_or_overriding_automatic_tables(tmp_path, inputs):
    _, content = inputs
    content["tables"]["latency"] = [{"case": "invented", "e2e": 0}]
    (tmp_path / "content.yaml").write_text(yaml.safe_dump(content))
    with pytest.raises(ValueError, match="must come from results"):
        exporter.export(tmp_path / "results.json", tmp_path / "content.yaml", tmp_path / "bad.docx")
    assert not (tmp_path / "bad.docx").exists()


def test_incomplete_narrative_requires_explicit_draft(tmp_path, inputs):
    _, content = inputs
    content["report"]["conclusion"] = "{{待填写}}"
    (tmp_path / "content.yaml").write_text(yaml.safe_dump(content))
    with pytest.raises(ValueError, match="Missing delivery narrative"):
        exporter.export(tmp_path / "results.json", tmp_path / "content.yaml", tmp_path / "bad.docx")
    exporter.export(tmp_path / "results.json", tmp_path / "content.yaml", tmp_path / "draft.docx", allow_incomplete=True)
    body = "\n".join(p.text for p in exporter.paragraphs(Document(tmp_path / "draft.docx")))
    assert "待补充" in body and "{{" not in body


def test_multimodal_sections_are_opt_in_and_keep_manual_evidence(tmp_path, inputs):
    _, content = inputs
    content["options"]["include_multimodal"] = True
    content["tables"]["multimodal_accuracy"] = [{"model": "FixtureVL", "dataset": "fixture revision", "samples": 10, "score": 0, "evidence": "fixture.json"}]
    (tmp_path / "content.yaml").write_text(yaml.safe_dump(content))
    exporter.export(tmp_path / "results.json", tmp_path / "content.yaml", tmp_path / "vl.docx")
    body = "\n".join(p.text for p in exporter.paragraphs(Document(tmp_path / "vl.docx")))
    assert "多模态模型评测" in body and "FixtureVL" in body and "fixture.json" in body


def test_template_has_no_historical_values_or_dangling_relationships():
    with ZipFile(exporter.TEMPLATE) as package:
        names = set(package.namelist())
        for name in names:
            data = package.read(name)
            for forbidden in [b"93.11", b"92.78", b"2026/06/16", b"Qwen3.6-27B"]:
                assert forbidden not in data, (name, forbidden)
            assert not name.startswith(("word/media/", "word/comments", "customXml/"))
            if name.endswith(".xml") or name.endswith(".rels"):
                etree.fromstring(data)
            if name.endswith(".rels"):
                directory = "" if name == "_rels/.rels" else name.rsplit("/_rels/", 1)[0]
                for rel in etree.fromstring(data):
                    if rel.get("TargetMode") == "External": continue
                    target = posixpath.normpath(posixpath.join(directory, rel.get("Target")))
                    assert target in names, (name, target)
        properties = etree.fromstring(package.read("docProps/core.xml"))
        assert not properties.xpath("//*[local-name()='creator' or local-name()='lastModifiedBy']/text()")


def test_xtask_delivery_contains_only_fresh_yaml_execution_config(tmp_path, inputs, monkeypatch):
    from delivery_examples import examples, render
    import release
    summary, content = inputs
    summary.update(run_root=str(tmp_path), framework_comparisons=[])
    for row in summary["cases"]:
        row["mode"] = "xtask"
        path = tmp_path / row["result"]["attempt"] / "xtask/tests/cases"
        path.mkdir(parents=True)
        (path / "release_case.yaml").write_text("case_name: fixture_yaml_only\nbenchmark:\n  benchmark_type: evalscope\n")
        # These real archived implementation scripts must not appear in delivery.
        (tmp_path / row["result"]["attempt"] / "eval.sh").write_text("DO_NOT_SHOW_INTERNAL_TEST_SCRIPT")
    content["blocks"]["container_script"] = {"text": "DO_NOT_SHOW_CONTAINER_SCRIPT"}
    (tmp_path / "results.json").write_text(json.dumps(summary))
    (tmp_path / "content.yaml").write_text(yaml.safe_dump(content))
    output = tmp_path / "xtask.docx"
    exporter.export(tmp_path / "results.json", tmp_path / "content.yaml", output)
    text = "\n".join(p.text for p in exporter.paragraphs(Document(output)))
    assert text.count("case_name: fixture-0") == 1
    assert "release_reproduction:" not in text
    assert "container_name:" not in text and "xllm_binary:" not in text
    assert "@@" not in text and "fixture-0.md" in text
    assert "fixture_yaml_only" not in text and "/shared/code/xllm" not in text
    for forbidden in ("DO_NOT_SHOW", "literal $VALUE", "#!/bin/bash", "服务启动脚本", "容器创建脚本"):
        assert forbidden not in text
    assert "0.384" in text and "fixture-1" in text  # All measurements/variants remain.
    assert len(list((tmp_path / "delivery/cases").glob("*.yaml"))) == 2
    monkeypatch.setattr(release, "collect", lambda root: summary)
    release.report(tmp_path)
    for filename in ("report.md", "reproduction.md"):
        text = (tmp_path / filename).read_text()
        assert text.count("case_name: fixture-0") == 1
        assert "release_reproduction:" not in text
        assert "```bash" not in text and "DO_NOT_SHOW" not in text


def test_mixed_delivery_uses_yaml_and_one_direct_representative(tmp_path, inputs):
    from delivery_examples import examples, render
    summary, _ = inputs
    xtask = json.loads(json.dumps(summary["cases"][0]))
    xtask.update(case_id="xtask-fixture", mode="xtask", framework="vllm-ascend", code={"version": "fixture-ascend", "vllm_version": "fixture-core", "image": "fixture-image:tag", "binary": "/usr/local/bin/vllm"})
    xtask["delivery"]["xtask_native"]["image"] = "fixture-image:tag"
    xtask["service"]["flags"] = {"max-num-seqs": 4}
    xtask["service"]["env"] = {}
    xtask["model"].pop("draft_model", None)
    xtask["result"]["attempt"] = "cases/xtask-fixture/attempt-001"
    folder = tmp_path / xtask["result"]["attempt"] / "xtask/tests/cases"
    folder.mkdir(parents=True)
    (folder / "release_case.yaml").write_text("case_name: actual_yaml\n")
    summary["cases"].append(xtask)
    records, variants = examples(summary, tmp_path)
    text = render(records)
    assert len(records) == 2 and len(variants) == 1
    assert text.count("literal $VALUE") == 1 and text.count("case_name: xtask-fixture") == 1
    assert "case_name: actual_yaml" not in text
    assert "代表场景" not in variants[0]["changes"]  # Concrete changed values, not a vague note.
    (tmp_path / "mixed-results.json").write_text(json.dumps(summary))
    exporter.export(tmp_path / "mixed-results.json", tmp_path / "content.yaml", tmp_path / "mixed.docx")
    text = "\n".join(p.text for p in exporter.paragraphs(Document(tmp_path / "mixed.docx")))
    assert "fixture-ascend" in text and "fixture-core" in text and "fixture-image" in text


def test_archived_script_template_like_text_is_not_rewritten(tmp_path, inputs):
    summary, _ = inputs
    path = tmp_path / summary['cases'][0]['result']['attempt'] / 'service/start.sh'
    path.write_text("#!/bin/bash\nprintf '%s' '{{LITERAL_NOT_A_REPORT_SLOT}}'\n")
    exporter.export(tmp_path / 'results.json', tmp_path / 'content.yaml', tmp_path / 'literal.docx')
    text = '\n'.join(p.text for p in exporter.paragraphs(Document(tmp_path / 'literal.docx')))
    assert "'{{LITERAL_NOT_A_REPORT_SLOT}}'" in text
