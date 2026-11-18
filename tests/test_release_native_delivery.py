"""Native delivery contracts. No container, package install or NPU execution."""
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/xllm-release-validation/scripts"))
import delivery_native as delivery
import release_common as common
import release
from delivery_examples import examples


@pytest.fixture
def row():
    config = common.load_config(common.ASSETS / "release.example.yaml")
    c = common.expand(config)[3]
    c.update(case_id=c["id"], mode="xtask", result={"status": "PASS"})
    c["xtask"]["commit"] = "b" * 40
    c["delivery"]["xtask_native"]["image"] = "fixture/xllm:release"
    return c


def release_id(): return {"version": "0.12.0", "commit": "a" * 40}


def test_native_case_keeps_workload_and_flags_without_old_artifacts(row):
    original = deepcopy(row)
    task = delivery.generate(row, release_id())
    text = delivery.yaml_text(row, release_id(), task)
    assert yaml.safe_load(text) == task and row == original
    assert set(task) <= {"case_name", "engine_type", "model_path", "served_model_name", "metrics", "dataset_name", "dataset_path", "dataset_format", "host_addr", "port", "communication_backend", "benchmark", "deployment"}
    assert "@@" not in text and "release_reproduction" not in text
    for path in (row["code"]["repo"], row["code"]["binary"], row["model"]["draft_model"]): assert path not in text
    instance = task["deployment"]["instances"][0]
    assert not {"container_name", "reuse_existing_container", "xllm_binary", "triton_binary_path"} & instance.keys()
    assert instance["serving_extensiable_param"]["enable_prefix_cache"] is False
    assert instance["serving_extensiable_param"]["num_speculative_tokens"] == row["service"]["flags"]["num_speculative_tokens"]
    perf = task["benchmark"]["perf"]
    assert (perf["batch_size"], perf["num_prompts"], perf["min_prompt_length"], perf["output_len"]) == (1, 40, 2048, 2048)
    assert "# Image: fixture/xllm:release" in text and "a" * 40 in text


def test_instructions_use_native_tools_and_exact_commit_not_a_fake_branch(row, tmp_path):
    task = delivery.generate(row, release_id())
    text = delivery.instructions(row, release_id(), task)
    assert "checkout --detach " + "a" * 40 in text
    assert "tools/build_xllm_in_container.sh" in text and "python3 main.py --case_name" in text
    assert "release.py" not in text and "build_gate.py" not in text and "@@" not in text
    assert row["code"]["binary"] not in text
    assert "config/xllm_config.json" in text and '"default_image_name": "release"' in text
    assert "export_mtp.py" in text and "pkg/xllm" in text
    config = json.loads(re.findall(r"```json\n(.*?)```", text, re.S)[0])
    parent = str(Path(delivery.settings(row, release_id())["workspace"]).parent)
    assert config["DOCKER_CONFIG"]["volumes"][parent] == parent
    for i, block in enumerate(re.findall(r"```bash\n(.*?)```", text, re.S)):
        script = tmp_path / f"instruction-{i}.sh"
        script.write_text(block)
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_native_delivery_keeps_container_benchmark_location_without_old_name(row):
    row["environment"]["client"] = {"container": row["environment"]["service"]["container"]}
    task = delivery.generate(row, release_id())
    assert task["deployment"]["benchmark"]["run_on_host"] is False
    assert row["environment"]["service"]["container"] not in yaml.safe_dump(task)


@pytest.mark.parametrize("missing", ["image", "workspace", "source_url", "mtp"])
def test_incomplete_recipe_is_not_presented_as_native_delivery(row, missing):
    row["delivery"]["xtask_native"].pop(missing)
    with pytest.raises(ValueError): delivery.generate(row, release_id())


def test_unknown_build_path_env_is_rejected_instead_of_reused(row):
    row["service"]["env"]["LD_LIBRARY_PATH"] = row["code"]["repo"] + "/build/lib"
    with pytest.raises(ValueError, match="internal artifact"): delivery.generate(row, release_id())


def test_real_data_is_exported_as_frozen_native_custom_input(row, tmp_path):
    frozen = tmp_path / "internal-frozen.txt"
    frozen.write_text("first question\nsecond question\n")
    row["workload"].update(dataset="gsm8k", dataset_revision="fixture", prepared_path=str(frozen), strict_lengths=False)
    summary = {"release": release_id(), "cases": [row]}
    examples(summary, tmp_path)
    directory = tmp_path / "delivery/cases"
    task = yaml.safe_load((directory / (row["case_id"] + ".yaml")).read_text())
    assert task["dataset_name"] == "custom" and str(frozen) not in yaml.safe_dump(task)
    assert (directory / (row["case_id"] + ".prompts.txt")).read_text() == frozen.read_text()
    assert "prompts.txt" in (directory / (row["case_id"] + ".md")).read_text()


def test_no_private_vllm_metadata_in_native_external_case(row):
    row.update(framework="vllm-ascend", code={"image": "fixture/vllm:release", "version": "fixture", "vllm_version": "fixture", "binary": "/usr/local/bin/vllm"})
    row["delivery"]["xtask_native"]["image"] = row["code"]["image"]
    row["model"].pop("draft_model", None)
    row["service"].update(flags={"max-num-seqs": 4}, env={})
    task = delivery.generate(row, release_id())
    assert task["external_service"]["enabled"] is True
    assert not any(k.startswith("release_") for k in task)
    assert "vllm serve" in delivery.instructions(row, release_id(), task)


def test_release_cli_has_no_recipient_replay_command(capsys):
    with pytest.raises(SystemExit) as result: release.main(["reproduce", "--help"])
    assert result.value.code == 2


def test_export_preserves_existing_delivery_artifacts(row, tmp_path):
    summary = {"release": release_id(), "cases": [row]}
    examples(summary, tmp_path)
    path = tmp_path / "delivery/cases" / (row["case_id"] + ".yaml")
    original = path.read_text()
    row["workload"]["parallel"] = 2
    with pytest.raises(ValueError, match="Delivery changed"): examples(summary, tmp_path)
    assert path.read_text() == original
