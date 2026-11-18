"""Release preparation, real datasets and cross-framework contracts; no NPU jobs."""
from copy import deepcopy
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from test_release_validation import config, case, measurement
import release
import release_common as common
import direct_adapter
import xtask_adapter
import frameworks
import prepare_dataset
import release_prepare
import export_delivery_docx as word
from delivery_examples import examples, render


def paired(case):
    x = deepcopy(case)
    x.update(comparison_group="bf16", framework="xllm")
    x["service"]["graph_phase"] = "decode"
    x["service"]["activation_checks"] = {"graph": r"RUNTIME graph enabled", "mtp": r"RUNTIME mtp enabled"}
    v = deepcopy(x)
    v.update(id="vllm-case", framework="vllm-ascend")
    v["model"].pop("draft_model", None)
    v["code"] = {"version": "fixture-version", "vllm_version": "fixture-core", "image": "fixture@sha256:abc", "binary": "/bin/vllm"}
    v["service"]["flags"] = {"enable-prefix-caching": False, "enable-chunked-prefill": True,
        "speculative-config": {"method": "qwen3_5_mtp", "num_speculative_tokens": 4},
        "compilation-config": {"cudagraph_mode": "FULL_DECODE_ONLY", "max_cudagraph_capture_size": 20}}
    return x, v


@pytest.mark.parametrize("dataset", ["gsm8k", "sharegpt"])
def test_real_dataset_freezes_prompts_without_answers_and_reuses_across_concurrency(case, tmp_path, dataset):
    source = tmp_path / "source.json"
    rows = [{"question": f"question {i}\nsecond line", "answer": "SECRET_ANSWER"} for i in range(6)] if dataset == "gsm8k" else [
        {"conversations": [{"from": "human", "value": f"question {i}\nsecond line"}, {"from": "gpt", "value": "SECRET_ANSWER"}]} for i in range(6)]
    source.write_text(json.dumps(rows))
    w = {**case["workload"], "dataset": dataset, "dataset_path": str(source), "prepared_path": str(tmp_path / "prompts.txt"),
         "dataset_revision": "fixture", "length_policy": "native", "strict_lengths": False}
    result = prepare_dataset.prepare(w, "/fixture/tokenizer")
    text = Path(w["prepared_path"]).read_text()
    assert "SECRET_ANSWER" not in text and len(text.splitlines()) == w["number"] + w["warmup"]
    assert not text.startswith("{") and "second line" in text
    w["parallel"] = 8
    assert prepare_dataset.prepare(w, "/fixture/tokenizer") == result
    case["workload"] = w
    common.validate_case(case)
    command = common.client_command(case, tmp_path)
    assert command[command.index("--dataset") + 1] == "custom"
    assert command[command.index("--dataset-path") + 1] == w["prepared_path"]
    assert command[command.index("--min-prompt-length") + 1] == "0"
    source.write_text(json.dumps(rows + [rows[0]]))
    with pytest.raises(ValueError, match="identity changed"):
        prepare_dataset.prepare(w, "/fixture/tokenizer")


def test_real_data_repetition_and_length_policy_are_explicit(case, tmp_path):
    source = tmp_path / "one.jsonl"
    source.write_text(json.dumps({"question": "one two three four"}) + "\n")
    w = {**case["workload"], "dataset": "gsm8k", "dataset_path": str(source), "prepared_path": str(tmp_path / "prompts.txt"),
         "dataset_revision": "fixture", "length_policy": "truncate", "input_tokens": 2, "strict_lengths": False}
    tokenizer = SimpleNamespace(encode=lambda p, **k: p.split(), decode=lambda ids, **k: " ".join(ids))
    with pytest.raises(ValueError, match="Insufficient samples"):
        prepare_dataset.prepare(w, "fixture", tokenizer=tokenizer)
    w["allow_repeat"] = True
    result = prepare_dataset.prepare(w, "fixture", tokenizer=tokenizer)
    assert result["repeated"] and Path(w["prepared_path"]).read_text().splitlines() == ["one two"] * 3


def test_real_data_audit_reports_actual_lengths(case, tmp_path):
    case["workload"].update(dataset="gsm8k", strict_lengths=False, length_policy="native", input_tokens=4096, output_tokens=40)
    measurement(tmp_path / "raw", output_tokens=40)
    result = common.collect_measurement(case, tmp_path / "raw")
    assert result["status"] == "PASS" and not result["request_audit"]["strict_lengths"]
    assert result["request_audit"]["output_tokens"] == {"min": 40, "max": 40, "mean": 40}


@pytest.mark.parametrize("change", ["mtp", "graph", "prefix", "devices", "number"])
def test_key_comparison_differences_block_planning(case, change):
    x, v = paired(case)
    frameworks.validate_pairs([x, v])
    if change == "mtp": v["service"]["flags"]["speculative-config"]["num_speculative_tokens"] = 3
    if change == "graph": v["service"]["flags"]["compilation-config"]["cudagraph_mode"] = "NONE"
    if change == "prefix": v["service"]["flags"]["enable-prefix-caching"] = True
    if change == "devices": v["service"]["physical_devices"] = [2, 3]
    if change == "number": v["workload"]["number"] += 1
    with pytest.raises(ValueError, match="key configuration differences"):
        frameworks.validate_pairs([x, v])


def test_vllm_launch_has_one_entry_and_preserves_json_and_false(case, tmp_path):
    _, v = paired(case)
    common.validate_case(v)
    directory = direct_adapter.render_service(v, tmp_path)
    commands = common.read_json(directory / "commands.json")
    assert len(commands) == 1 and "--no-enable-prefix-caching" in commands[0]
    assert json.loads(commands[0][commands[0].index("--speculative-config") + 1])["num_speculative_tokens"] == 4
    for name in ("start.sh", "stop.sh"):
        subprocess.run(["bash", "-n", str(directory / name)], check=True)
    assert frameworks.semantic_contract(v)["graph_phase"] == "decode"
    v["mode"] = "xtask"
    with pytest.raises(ValueError, match="service_mode=external"):
        xtask_adapter.task_config(v)
    v["xtask"]["service_mode"] = "external"
    task = xtask_adapter.task_config(v)
    assert task["engine_type"] == "vllm-ascend"
    assert task["external_service"]["enabled"] and task["benchmark"]["benchmark_type"] == "evalscope"
    assert task["release_service_configuration"]["code"]["version"] == "fixture-version"


def test_xtask_real_data_cannot_drop_dataset_path(case, tmp_path):
    case["workload"].update(dataset="gsm8k", prepared_path="/shared/frozen.txt", dataset_path="/shared/source.jsonl", dataset_revision="fixture", strict_lengths=False)
    task = xtask_adapter.task_config(case)
    assert task["dataset_name"] == "custom" and task["dataset_path"] == "/shared/frozen.txt"
    folder = tmp_path / "preview"; folder.mkdir()
    command = common.client_command(case, folder)
    index = command.index("--dataset-path"); del command[index:index+2]
    (folder / "run_evalscope_test.sh").write_text("if ! " + shlex.join(command) + "; then\nexit 1\nfi\n")
    with pytest.raises(ValueError, match="dataset-path"):
        xtask_adapter.verify_preview(case, tmp_path)


def test_reference_container_recipe_preserves_constraints_without_replaying_process():
    reference = {"Config": {"User": "root", "Env": ["OLD=yes"], "Cmd": ["old-job"], "Labels": None},
        "HostConfig": {"Privileged": True, "NetworkMode": "host", "IpcMode": "host", "ShmSize": 1024,
                       "Binds": ["/old:/shared"], "Devices": [{"PathOnHost": "/dev/davinci4"}], "AutoRemove": True}}
    original = deepcopy(reference)
    body = release_prepare.container_request({"name": "new", "binds": ["/new:/shared"], "env": {"NEW": "ok"}}, "sha256:fixture", reference)
    assert reference == original and body["Cmd"] == ["sleep", "infinity"]
    assert body["HostConfig"]["Privileged"] and not body["HostConfig"]["AutoRemove"]
    assert body["HostConfig"]["Devices"] == reference["HostConfig"]["Devices"]
    assert body["HostConfig"]["Binds"] == ["/new:/shared"] and "OLD=yes" in body["Env"]
    reference["HostConfig"]["PortBindings"] = {"8000/tcp": [{"HostPort": "8000"}]}
    with pytest.raises(ValueError, match="PortBindings"):
        release_prepare.container_request({"name": "new"}, "fixture", reference)


def test_build_preparation_delegates_gate_and_preserves_old_log(tmp_path, monkeypatch):
    config = {"repo": "/fixture/xllm", "build_dir": "/fixture/build", "binary": "/fixture/xllm/bin", "run_root": str(tmp_path / "build-record")}
    seen = []
    def execute(command, **kwargs):
        seen.extend(command)
        common.write_json(Path(config["run_root"]) / "build/verdict.json", {"status": "PASS", "binary_ready": True})
    monkeypatch.setattr(release_prepare.subprocess, "run", execute)
    release_prepare.prepare_build(config, tmp_path / "output")
    assert "--execute" in seen and "--require-npu" in seen and "--framework" in seen
    (Path(config["run_root"]) / "build/build.log").write_text("old")
    with pytest.raises(ValueError, match="preserve prior logs"):
        release_prepare.prepare_build(config, tmp_path / "output2")
    assert (Path(config["run_root"]) / "build/build.log").read_text() == "old"


def test_mtp_export_uses_pinned_checkout_and_never_overwrites(tmp_path):
    repo = tmp_path / "code"; (repo / "tools").mkdir(parents=True)
    tool = repo / "tools/export_mtp.py"
    tool.write_text("import argparse,pathlib\np=argparse.ArgumentParser();p.add_argument('--input-dir');p.add_argument('--output-dir');p.add_argument('--model-type');a=p.parse_args()\no=pathlib.Path(a.output_dir);o.mkdir();(o/'config.json').write_text('{}');(o/'mtp_layer_parameters.safetensors').write_bytes(b'fixture')\n")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-m", "fixture"], check=True, capture_output=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    model = tmp_path / "model"; model.mkdir(); (model / "config.json").write_text("{}"); (model / "a.safetensors").write_bytes(b"main")
    config = {"repo": str(repo), "commit": commit, "input_dir": str(model), "output_dir": str(tmp_path / "draft"), "model_type": "fixture", "python": sys.executable}
    first = release_prepare.prepare_mtp(config, tmp_path / "log")
    assert release_prepare.prepare_mtp(config, tmp_path / "log2") == first
    (Path(config["output_dir"]) / "mtp_layer_parameters.safetensors").write_bytes(b"changed")
    with pytest.raises(ValueError, match="files changed"):
        release_prepare.prepare_mtp(config, tmp_path / "log3")


def test_runtime_activation_is_required_before_comparison(case, tmp_path):
    x, _ = paired(case)
    with pytest.raises(ValueError, match="activation evidence"):
        direct_adapter.check_activation(x, tmp_path, tmp_path)
    (tmp_path / "node_0.log").write_text("RUNTIME graph enabled\nRUNTIME mtp enabled\n")
    result = direct_adapter.check_activation(x, tmp_path, tmp_path)
    assert result["status"] == "PASS" and common.read_json(tmp_path / "activation.json")["matches"]["mtp"]


def test_framework_comparison_needs_runtime_identity_not_only_flags(case):
    x, v = paired(case)
    rows = []
    identity = {"model_weights_verified": True, "draft_weights_verified": True, **{k: {"fixture": "same"} for k in ("base_model_sha256", "model_weight_sha256", "device_mapping", "hardware_identity", "workload_identity")}}
    for c in (x, v):
        rows.append({**c, "case_id": c["id"], "result": {"status": "PASS", "metrics": {"request_throughput": 3 if c is x else 2}, "activation": {"status": "PASS"}, "observed_identity": deepcopy(identity), "client_identity": {"evalscope": "same"}}})
    result = frameworks.compare_frameworks({"cases": rows})[0]
    assert result["status"] == "COMPARABLE_MEASUREMENT" and result["delta_pct"]["request_throughput"] == 50
    rows[0]["result"]["observed_identity"]["model_weights_verified"] = False
    result = frameworks.compare_frameworks({"cases": rows})[0]
    assert result["status"] == "REFERENCE_ONLY" and "delta_pct" not in result


def test_package_lifecycle_identity_does_not_require_git_or_ldd(case, tmp_path, monkeypatch):
    _, v = paired(case)
    flow = release.workflow()
    executable = tmp_path / "vllm"; executable.write_text("#!/bin/sh\nexit 0\n"); executable.chmod(0o755)
    model = tmp_path / "model"; model.mkdir()
    v["code"]["binary"] = str(executable); v["model"].update(path=str(model), tokenizer=str(model))
    v["service"]["ownership"] = "external"
    spec = common.experiment(v, tmp_path / "run")
    spec["environment"].update(require_npu=False, require_clean_submodules=False)
    spec["profiling"]["enabled"] = False
    path = tmp_path / "experiment.yaml"; path.write_text(yaml.safe_dump(spec))
    monkeypatch.setattr(flow.importlib.metadata, "distribution", lambda n: SimpleNamespace(version=v["code"]["vllm_version"] if n == "vllm" else v["code"]["version"], files=[]))
    result = flow.preflight(path, tmp_path / "preflight")
    assert result["status"] == "PASS" and result["source"]["identity_type"] == "package"
    assert "source_git" not in [c["name"] for c in result["checks"]]


def test_prepare_uses_host_ssh_for_container_steps_and_freezes_plan(tmp_path, monkeypatch):
    plan = {"fingerprint": "locked", "config": {"environment": {"host": {"ssh_host": "fixture-host", "python": "python3"}},
        "preparation": {"containers": [{"name": "fixture", "mode": "existing", "context": {"python": "python3.11"}}]}}, "cases": []}
    contexts = []
    def fake(context, body, log, **kwargs):
        contexts.append(context)
        log.parent.mkdir(parents=True, exist_ok=True); log.write_text(body)
        common.write_json(log.parent / "result.json", {"status": "PASS"})
    monkeypatch.setattr(release_prepare, "run", fake)
    result = release_prepare.prepare_campaign(tmp_path, plan)
    assert result["status"] == "PASS" and contexts[0]["ssh_host"] == "fixture-host"
    assert contexts[0]["python"] == "python3.11"
    before = (tmp_path / "preparation/attempt-001/container-001/execute.log").read_bytes()
    assert release_prepare.prepare_campaign(tmp_path, plan) == result
    assert len(contexts) == 1 and (tmp_path / "preparation/attempt-001/container-001/execute.log").read_bytes() == before


def test_prepare_failure_preserves_attempt_and_can_retry_without_overwriting(tmp_path, monkeypatch):
    plan = {"fingerprint": "locked", "config": {"environment": {"host": {}}, "preparation": {"containers": [{"name": "fixture"}]}}, "cases": []}
    def fail(context, body, log, **kwargs):
        log.parent.mkdir(parents=True, exist_ok=True); log.write_text("first failure"); raise ValueError("fixture unavailable")
    monkeypatch.setattr(release_prepare, "run", fail)
    with pytest.raises(ValueError, match="fixture unavailable"): release_prepare.prepare_campaign(tmp_path, plan)
    assert common.read_json(tmp_path / "preparation.json")["status"] == "FAILED"
    def succeed(context, body, log, **kwargs):
        log.parent.mkdir(parents=True, exist_ok=True); log.write_text("retry success"); common.write_json(log.parent / "result.json", {"status": "PASS"})
    monkeypatch.setattr(release_prepare, "run", succeed)
    assert release_prepare.prepare_campaign(tmp_path, plan)["status"] == "PASS"
    assert (tmp_path / "preparation/attempt-001/container-001/execute.log").read_text() == "first failure"
    assert (tmp_path / "preparation/attempt-002/container-001/execute.log").read_text() == "retry success"


def test_ceval_uses_evalscope_in_both_execution_modes(case, tmp_path):
    case["kind"] = "accuracy"
    case["accuracy"] = {"datasets": ["ceval"], "dataset_revision": "fixture-revision", "dataset_args": {"ceval": {"few_shot_num": 5}},
        "generation_config": {"temperature": 0}, "batch_size": 1, "report_glob": "*.json", "score_pointer": "/score", "limit": 5}
    command = common.client_command(case, tmp_path)
    assert "eval" in command and command[command.index("--datasets")+1] == "ceval"
    task = xtask_adapter.task_config(case)
    assert task["dataset_name"] == "ceval" and task["benchmark"]["benchmark_type"] == "evalscope"
    assert task["benchmark"]["acc"]["acc_dataset_len"] == 5


def test_native_input_does_not_allow_short_fixed_outputs_to_pass(case, tmp_path):
    case["workload"].update(dataset="sharegpt", strict_lengths=False, length_policy="native")
    measurement(tmp_path / "raw", output_tokens=40)
    result = common.collect_measurement(case, tmp_path / "raw")
    assert result["status"] != "PASS"
    assert "output lengths" in " ".join(result["errors"])


@pytest.mark.parametrize('reported', ['fixture-core', 'wrong-version'])
def test_vllm_executable_version_must_match_package_and_user_request(case, tmp_path, monkeypatch, reported):
    import target_identity
    _, v = paired(case)
    binary = tmp_path / 'vllm'; binary.write_text('#!/bin/sh\n'); binary.chmod(0o755)
    model = tmp_path / 'model'; model.mkdir(); (model / 'config.json').write_text('{}')
    v['code']['binary'] = str(binary); v['model'].update(path=str(model), tokenizer=str(model))
    v['service']['verify_acl_mapping'] = False
    monkeypatch.setattr(target_identity.importlib.metadata, 'version', lambda n: 'fixture-core' if n == 'vllm' else 'fixture-version')
    def run(command, **kwargs):
        if command[-1] == '--version': return SimpleNamespace(returncode=0, stdout=reported, stderr='')
        flags = ' '.join(a.replace('--no-', '--', 1) for a in frameworks.vllm_arguments(v) if a.startswith('--'))
        return SimpleNamespace(returncode=0, stdout=flags, stderr='')
    monkeypatch.setattr(target_identity.subprocess, 'run', run)
    if reported == 'wrong-version':
        with pytest.raises(ValueError, match='executable version differs'): target_identity.observe_vllm(v)
    else:
        identity = target_identity.observe_vllm(v)
        assert identity['cli_version'] == reported and identity['packages']['vllm'] == reported
