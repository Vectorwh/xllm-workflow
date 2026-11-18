#!/usr/bin/env python3
"""Fill the source-derived release delivery DOCX from evidence and narrative YAML.

This exporter reads existing data and scripts; it never executes a workload.
Requires python-docx and PyYAML. Rendering remains a separate delivery gate.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

import yaml

from release_common import ASSETS, read_json, require
from delivery_examples import examples, render

TEMPLATE = ASSETS / "templates/release-delivery-template.docx"
TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)(?:\.([a-z0-9_]+))?\}\}")
UNFILLED = re.compile(r"\{\{[^{}]+\}\}")
REQUIRED = ("TITLE", "AUTHOR", "DATE", "BACKGROUND", "RELEASE_SCOPE", "CONCLUSION", "BUILD_RECORD",
            "PERFORMANCE_METHOD", "REPRODUCTION")
AUTOMATIC_TABLES = {"ACCURACY", "CASES", "LATENCY", "THROUGHPUT", "RAW_METRICS", "VARIANTS"}


def text(value):
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def scalar_slots(content, base, allow_incomplete):
    slots = {name.upper(): text(value) for name, value in content.get("report", {}).items()}
    for name, entry in content.get("blocks", {}).items():
        require(isinstance(entry, dict) and ("file" in entry) != ("text" in entry), f"Block {name} requires exactly one of file/text")
        value = (base / entry["file"]).read_text() if "file" in entry else entry["text"]
        slots[name.upper()] = text(value)
    if not allow_incomplete:
        missing = [name for name in REQUIRED if not slots.get(name, "").strip() or slots[name] in {"N/A", "待填写", "待补充"} or "{{" in slots[name]]
        require(not missing, "Missing delivery narrative: " + ", ".join(missing))
        require(re.fullmatch(r"\d{4}-\d{2}-\d{2}", slots["DATE"]), "Report date must use YYYY-MM-DD")
    return slots


def metric_unit(name):
    if "(ms)" in name: return "ms"
    if "(s)" in name: return "s"
    if "(req/s)" in name: return "req/s"
    if "(tok/s)" in name: return "tok/s"
    return "原字段单位"


def evidence_tables(summary):
    tables = {key: [] for key in AUTOMATIC_TABLES}
    for case in summary.get("cases", []):
        result = case.get("result", {})
        status, metrics = result.get("status", "PENDING"), result.get("metrics", {})
        cid = case["case_id"]
        model, service, workload = case.get("model", {}), case.get("service", {}), case.get("workload", {})
        origin = result.get("attempt", "未执行")
        evidence = f"{status}; {origin}/result.json" if "attempt" in result else status
        # Failed measurements remain visible together with their status, not promoted to PASS.
        if case.get("kind") == "accuracy":
            accuracy = case.get("accuracy", {})
            for report, score in metrics.get("scores", {"未取得报告": None}).items():
                tables["ACCURACY"].append(dict(case=cid, model=model.get("name"),
                    dataset=" / ".join(accuracy.get("datasets", [])), revision=accuracy.get("dataset_revision"),
                    samples=accuracy.get("limit", "全量 详见原始报告"), score=score,
                    evidence=f"{evidence}; {report}"))
            continue
        from frameworks import framework, object_value
        mtp = service.get('flags', {}).get('num_speculative_tokens', 'default') if framework(case) == 'xllm' else object_value(service.get('flags', {}).get('speculative-config', {})).get('num_speculative_tokens', 0)
        tables["CASES"].append(dict(case=cid, model=model.get("name"), mode=framework(case)+" / "+str(case.get("mode")),
            config=f"TP{len(service.get('devices', []))} MTP{mtp}",
            parallel=workload.get("parallel"), requests=f"{metrics.get('success', 'N/A')}/{metrics.get('total', 'N/A')}",
            length=f"{workload.get('input_tokens', 'N/A')}+{workload.get('output_tokens', 'N/A')}" if workload.get('dataset', 'random') == 'random' else workload.get('dataset')+"; "+workload.get('length_policy', 'native')+"; "+text(result.get('request_audit', {}).get('input_tokens')), status=status))
        tables["LATENCY"].append(dict(case=cid, qps=metrics.get("request_throughput"),
            e2e=metrics.get("avg_latency_s"), ttft=metrics.get("ttft_ms"), tpot=metrics.get("tpot_ms"), itl=metrics.get("itl_ms")))
        raw = result.get("raw_summary", {})
        tables["THROUGHPUT"].append(dict(case=cid, output=metrics.get("output_tps"),
            total=next((raw[k] for k in ("Total Throughput (tok/s)", "Total token throughput (tok/s)") if k in raw), None),
            decode=next((raw[k] for k in ("Avg Decode Throughput (tok/s)", "Decode Throughput (tok/s)") if k in raw), None),
            spec=raw.get("Spec Accept", "N/A"), evidence=evidence))
        def leaves(value, path=""):
            if isinstance(value, dict):
                for key, child in value.items(): yield from leaves(child, f"{path}/{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value): yield from leaves(child, f"{path}/{index}")
            else:
                yield path.lstrip("/"), value
        for name, value in leaves(raw):
            tables["RAW_METRICS"].append(dict(case=cid, group="summary", metric=name, value=text(value), unit=metric_unit(name)))
        percentiles = result.get("raw_percentiles", [])
        # Keep the original hierarchy intact; do not infer units for unknown columns.
        if percentiles:
            for name, value in leaves(percentiles):
                tables["RAW_METRICS"].append(dict(case=cid, group="percentile", metric=name, value=text(value), unit=metric_unit(name)))
    return tables


def paragraphs(document):
    yield from document.paragraphs
    def in_table(table):
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs
                for nested in cell.tables:
                    yield from in_table(nested)
    for table in document.tables:
        yield from in_table(table)
    for section in document.sections:
        for furniture in (section.header, section.first_page_header, section.even_page_header,
                          section.footer, section.first_page_footer, section.even_page_footer):
            yield from furniture.paragraphs


def set_paragraph(paragraph, value):
    # Template slots are plain text. Keep paragraph properties and the first run style.
    if paragraph.runs:
        paragraph.runs[0].text = value
        for run in paragraph.runs[1:]: run.text = ""
    else:
        paragraph.add_run(value)


def optional_section(document, name, enabled):
    start, end = "{{" + name + "_BEGIN}}", "{{" + name + "_END}}"
    deleting = False
    found = []
    from docx.text.paragraph import Paragraph
    for element in list(document.element.body):
        value = Paragraph(element, document).text if element.tag.endswith("}p") else ""
        if value == start:
            deleting = True
            found.append("start")
            element.getparent().remove(element)
        elif value == end:
            require(deleting, f"Unbalanced section {name}")
            deleting = False
            found.append("end")
            element.getparent().remove(element)
        elif deleting and not enabled:
            element.getparent().remove(element)
    require(found == ["start", "end"] and not deleting, f"Missing optional section {name}")


def insert_execution(document, records):
    """Keep each label with its code, while allowing long scripts/YAML to flow."""
    literals = []
    for slot in list(document.paragraphs):
        if slot.text != "{{EXECUTION_EXAMPLES}}": continue
        def insert(value, style="Normal", heading=False):
            paragraph = document.add_paragraph(value, style)
            slot._p.addprevious(paragraph._p)
            paragraph.paragraph_format.keep_with_next = heading
            paragraph.paragraph_format.widow_control = True
            paragraph.paragraph_format.keep_together = False
            if heading:
                for run in paragraph.runs: run.bold = True
            return paragraph
        for record in records:
            insert(f"{record['case_id']} | {record['framework']} | {record['mode']} | {record['kind']}", heading=True)
            for block in record["blocks"]:
                insert(block["label"], heading=True)
                literals.append(insert(block["text"].rstrip(), "Release Code"))
        slot._p.getparent().remove(slot._p)
    return literals


def export(results_path, content_path, output, *, allow_incomplete=False):
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.table import _Row

    output, content_path = Path(output).resolve(), Path(content_path).resolve()
    require(output.suffix == ".docx" and not output.exists(), "Use a new .docx output path")
    summary, content = read_json(results_path), yaml.safe_load(content_path.read_text())
    require(isinstance(content, dict) and content.get("schema_version") == 1, "Content schema_version must be 1")
    require("RELEASE_VERSION" not in {k.upper() for k in content.get("report", {})}, "Release identity comes from results.json")
    slots = scalar_slots(content, content_path.parent, allow_incomplete)
    slots.update(RELEASE_VERSION=summary["release"]["version"], COMMIT=summary["release"]["commit"],
                 COVERAGE=text(summary.get("counts", {})), CLAIM_SCOPE=summary.get("claim_scope", "measurement-only"),
                 RESULTS_FILE=str(Path(results_path).resolve()))
    versions = sorted({(str(c.get("code", {}).get("version", "N/A")), str(c.get("code", {}).get("vllm_version", "N/A")),
                        str(c.get("result", {}).get("observed_identity", {}).get("image_id", c.get("code", {}).get("image", "N/A"))))
                       for c in summary.get("cases", []) if c.get("framework") == "vllm-ascend"})
    for ascend, core, image in versions:
        slots["COMMIT"] += f"\nvLLM-Ascend {ascend} / vLLM {core}\n镜像 {image}"
    tables = evidence_tables(summary)
    records, variants = examples(summary, summary.get("run_root", Path(results_path).resolve().parent),
                                 content.get("representatives"), incomplete=allow_incomplete)
    slots["EXECUTION_EXAMPLES"] = render(records, markdown=False)
    tables["VARIANTS"] = variants
    comparisons = summary.get("framework_comparisons", [])
    if comparisons:
        labels = {"request_throughput": "QPS", "output_tps": "输出tok/s", "avg_latency_s": "E2E", "ttft_ms": "TTFT", "tpot_ms": "TPOT", "itl_ms": "ITL"}
        tables["COMPARISONS"] = [{"case": " / ".join(p["cases"]), "xllm": "详见性能结果表", "baseline": "详见性能结果表",
            "change": "\n".join(f"{labels[k]}: {value:+.2f}%" for k, value in p.get("delta_pct", {}).items() if k in labels) or "N/A",
            "evidence": p["status"] + "; " + "; ".join(p["differences"]) + "\n调优参数见代表场景和差异表；完整记录见 framework-comparison.json"} for p in comparisons]
    for name, rows in content.get("tables", {}).items():
        require(name.upper() not in AUTOMATIC_TABLES, f"Measured table {name} must come from results.json")
        require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), f"Table {name} requires a list of mappings")
        if name.upper() == "COMPARISONS" and comparisons:
            require(not rows, "Framework comparison must come from results.json")
            continue
        tables[name.upper()] = rows
    document = Document(TEMPLATE)
    if summary.get("cases") and all(c["mode"] == "xtask" for c in summary["cases"]):
        # The execution section contains only the actual xTask YAML. No launcher,
        # EvalScope shell, container command, or generated internal script is shown.
        for paragraph in list(document.paragraphs):
            if "_SCRIPT}}" in paragraph.text or paragraph.text == "容器创建脚本":
                paragraph._p.getparent().remove(paragraph._p)
    for paragraph in document.paragraphs:
        if paragraph.style.name == "Title":
            set_paragraph(paragraph, slots.get("TITLE", "xLLM NPU 模型适配交付报告"))
            break
    options = content.get("options", {})
    optional_section(document, "MULTIMODAL", options.get("include_multimodal", False))
    optional_section(document, "RAW", options.get("include_raw_metrics", False))
    literal_paragraphs = insert_execution(document, records)
    literal_elements = {p._p for p in literal_paragraphs}
    literal_texts = {p.text for p in literal_paragraphs}
    used = set()
    for table in document.tables:
        for prototype in list(table.rows):
            matches = [m for c in prototype.cells for p in c.paragraphs for m in TOKEN.finditer(p.text) if m.group(2)]
            if not matches: continue
            groups = {m.group(1) for m in matches}
            require(len(groups) == 1, "Each prototype row must belong to one table group")
            group = groups.pop()
            used.add(group)
            rows = tables.get(group, [])
            if not rows: rows = [{"_empty": True}]
            for entry in rows:
                element = deepcopy(prototype._tr)
                prototype._tr.addprevious(element)
                row = _Row(element, table)
                if entry.get("_empty"):
                    cell = row.cells[0].merge(row.cells[-1])
                    cell.text = "未提供或不适用"
                    continue
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        value = TOKEN.sub(lambda m: text(entry.get(m.group(2))), paragraph.text)
                        require(allow_incomplete or not UNFILLED.search(value), f"Unfilled manual table: {group}")
                        set_paragraph(paragraph, UNFILLED.sub("待补充", value) if allow_incomplete else value)
            table._tbl.remove(prototype._tr)
    disabled = {"MULTIMODAL_ACCURACY", "MULTIMODAL_PERF"} if not options.get("include_multimodal", False) else set()
    for name, rows in content.get("tables", {}).items():
        require(name.upper() in used or (not rows and name.upper() in disabled), f"Manual table has no enabled template slot: {name}")
    unresolved = set()
    for paragraph in paragraphs(document):
        if paragraph._p in literal_elements: continue
        def replace(match):
            name = match.group(1)
            if name in slots and "{{" in slots[name]:
                require(allow_incomplete, f"Unfilled content placeholder: {name}")
                unresolved.add(name)
                return "待补充"
            if name not in slots or not slots[name].strip():
                unresolved.add(name)
                return "待补充" if allow_incomplete else "未提供或不适用"
            return slots[name]
        if TOKEN.search(paragraph.text): set_paragraph(paragraph, TOKEN.sub(replace, paragraph.text))
    document.core_properties.title = slots.get("TITLE", "xLLM NPU 适配交付报告")
    document.core_properties.author = slots.get("AUTHOR", "")
    document.core_properties.last_modified_by = ""
    settings = document.settings.element
    update = settings.find(qn("w:updateFields"))
    if update is None:
        update = OxmlElement("w:updateFields"); settings.append(update)
    update.set(qn("w:val"), "true")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    document.save(temporary)
    verified = Document(temporary)
    require(not any(UNFILLED.search(p.text) for p in paragraphs(verified) if p.text not in literal_texts), "Unresolved template tokens remain")
    temporary.replace(output)
    return {"output": str(output), "measurement_cases": len(summary.get("cases", [])),
            "optional_missing_fields": sorted(unresolved), "render_required": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--content", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-incomplete", action="store_true", help="Generate an explicitly incomplete draft")
    args = parser.parse_args()
    print(json.dumps(export(args.results, args.content, args.output, allow_incomplete=args.allow_incomplete), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
