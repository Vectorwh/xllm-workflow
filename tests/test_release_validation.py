"""Offline behavioral tests; no SSH, containers, models or NPUs are started."""
from copy import deepcopy
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/xllm-release-validation/scripts"
sys.path.insert(0, str(SCRIPTS))
import release
import release_common as common
import direct_adapter
import export_workbooks
import xtask_adapter
import release_runtime


@pytest.fixture
def config(tmp_path):
    value = common.load_config(ROOT / "skills/xllm-release-validation/assets/release.example.yaml")
    value["cases"] = [value["cases"][0]]
    value["workload_profiles"]["random-2k2k"].update(parallel=[1, 2], number=2, warmup=1)
    value["service_profiles"]["tp2"]["inter_case_seconds"] = 0
    value["environment"]["lease_dir"] = str(tmp_path / "leases")
    return value


@pytest.fixture
def case(config):
    return common.expand(config)[0]


def measurement(folder, number=2, failed=0, old=False, output_tokens=2048):
    folder.mkdir(parents=True)
    summary = {"Total Requests": number, "Success Requests": number-failed, "Failed Requests": failed,
               "Req Throughput (req/s)": 0.2, "Output Throughput (tok/s)": 409.6,
               "Avg Latency (s)": 2.5, "Avg TTFT (ms)": 100, "Avg TPOT (ms)": 2,
               "Avg ITL (ms)": 8, "Avg Input Tokens": 2048, "Avg Output Tokens": output_tokens,
               "Unmodeled native metric": 77}
    if old:
        summary = {aliases[-1][0]: summary[aliases[0][0]] / aliases[-1][1]
                   for aliases in common.METRICS.values()}
    common.write_json(folder / "benchmark_summary.json", summary)
    common.write_json(folder / "benchmark_percentile.json", [{"Percentiles": "99%", "TPOT (ms)": 4}])
    with sqlite3.connect(folder / "benchmark_data.db") as db:
        db.execute("CREATE TABLE result(success INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER)")
        db.executemany("INSERT INTO result VALUES(?,?,?)", [(1, 2048, output_tokens)]*(number-failed) + [(0,2048,0)]*failed)
    return summary


def test_extensible_flags_matrix_preserves_false_and_values(config):
    config["cases"][0]["matrix"] = {"service.flags.enable_new_feature": [True, False],
                                    "service.flags.new_batch_limit": [7, 9]}
    cases = common.expand(config)
    assert len(cases) == 8
    assert len({c["fingerprint"] for c in cases}) == 8
    false_case = next(c for c in cases if c["service"]["flags"]["enable_new_feature"] is False)
    args = common.flag_args(false_case, 0)
    assert "--enable_new_feature=false" in args
    assert any(arg in args for arg in ["--new_batch_limit=7", "--new_batch_limit=9"])
    assert "enable_unspecified_feature" not in false_case["service"]["flags"]


def test_pytorch_default_reaches_direct_and_native_xtask(config, tmp_path):
    config["service_profiles"]["tp2"]["flags"].pop("npu_kernel_backend", None)
    case = common.expand(config)[0]
    directory = tmp_path / "preview"
    direct_adapter.render_service(case, directory)
    assert "--npu_kernel_backend=TORCH" in (directory / "service/start.sh").read_text()
    case["mode"] = "xtask"
    case["case_id"] = case["id"]
    case["xtask"]["commit"] = "b" * 40
    case["delivery"]["xtask_native"]["image"] = "fixture/xllm:release"
    from delivery_native import generate
    native = generate(case, {"version": "fixture", "commit": "a" * 40})
    assert native["deployment"]["instances"][0]["serving_extensiable_param"]["npu_kernel_backend"] == "TORCH"


def test_backend_default_preserves_explicit_matrix_and_vllm_config(config):
    config["cases"][0]["matrix"] = {"service.flags.npu_kernel_backend": ["ATB", "AUTO"]}
    cases = common.expand(config)
    assert {c["service"]["flags"]["npu_kernel_backend"] for c in cases} == {"ATB", "AUTO"}
    comparison = common.load_config(common.ASSETS / "release-comparison.example.yaml")
    vllm = [c for c in common.expand(comparison) if c["framework"] == "vllm-ascend"]
    assert vllm and all("npu_kernel_backend" not in c["service"]["flags"] for c in vllm)


@pytest.mark.parametrize("flags", [{"model": "wrong"}, {"bad-name": True}, {"new_flag": []}])
def test_invalid_or_identity_overriding_flags_rejected(case, flags):
    case["service"]["flags"].update(flags)
    with pytest.raises(ValueError):
        common.validate_case(case)


def test_matrix_duplicates_and_limits_are_rejected(config):
    config["cases"][0]["matrix"] = {"service.flags.enable_graph": [False, False]}
    with pytest.raises(ValueError, match="unique"):
        common.expand(config)
    config["cases"][0].pop("matrix")
    config["execution"]["max_cases"] = 1
    with pytest.raises(ValueError, match="max_cases"):
        common.expand(config)


def test_total_requests_are_not_multiplied_by_parallel(case, tmp_path):
    case["workload"]["parallel"] = 8
    command = common.client_command(case, tmp_path)
    assert command[command.index("--number")+1] == "2"
    assert command[command.index("--parallel")+1] == "8"
    assert command[command.index("--warmup-num")+1] == "1"


def test_direct_rate_mode_is_explicit_and_xtask_does_not_drop_it(case, tmp_path):
    case["workload"]["rate"] = 2
    with pytest.raises(ValueError, match="open_loop"):
        common.validate_case(case)
    case["workload"]["open_loop"] = True
    common.validate_case(case)
    assert "--open-loop" in common.client_command(case, tmp_path)
    with pytest.raises(ValueError, match="open-loop"):
        xtask_adapter.task_config(case)


def test_plan_is_offline_immutable_and_groups_adjacent_cases(config, tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("plan executed a command"))
    spec, root = tmp_path / "release.yaml", tmp_path / "run"
    spec.write_text(yaml.safe_dump(config))
    plan = release.create_plan(spec, root)
    assert len(plan["groups"]) == 1 and len(plan["cases"]) == 2
    assert common.read_json(root / "plan.json")["fingerprint"] == plan["fingerprint"]
    with pytest.raises(ValueError, match="empty"):
        release.create_plan(spec, root)
    data = common.read_json(root / "plan.json")
    data["cases"][0]["workload"]["number"] = 999
    common.write_json(root / "plan.json", data)
    with pytest.raises(ValueError, match="identity changed"):
        release.load_plan(root)


def test_generated_direct_scripts_quote_flags_and_parse(case, tmp_path):
    case["service"]["flags"]["new_string"] = "value with spaces; $(not_a_command)"
    service = direct_adapter.render_service(case, tmp_path)
    client = direct_adapter.render_workload(case, tmp_path)
    commands = common.read_json(service / "commands.json")
    assert "--new_string=value with spaces; $(not_a_command)" in commands[0]
    for path in [service / "start.sh", service / "stop.sh", client]:
        subprocess.run(["bash", "-n", str(path)], check=True)
    assert ">>" not in (service / "start.sh").read_text().split("node_0.log")[0]
    stop_text = (service / "stop.sh").read_text()
    assert "fields[19]==start" in stop_text and "killall" not in stop_text


def test_xtask_configuration_reuses_container_without_second_matrix(case):
    task = xtask_adapter.task_config(case)
    instance = task["deployment"]["instances"][0]
    assert instance["container_name"] == "xllm-release"
    assert instance["xllm_binary"] == case["code"]["binary"]
    assert instance["serving_extensiable_param"]["enable_prefix_cache"] is False
    assert task["benchmark"]["perf"]["num_prompts"] == 2
    assert "assess_test_param" not in task


def test_xtask_cannot_silently_change_cpu_affinity(case):
    case["service"]["cpu_sets"][0] = "10-19"
    with pytest.raises(ValueError, match="affinity"):
        xtask_adapter.task_config(case)


def test_xtask_container_client_keeps_controller_on_host(case):
    case["environment"]["client"] = {"container": case["environment"]["service"]["container"]}
    task = xtask_adapter.task_config(case)
    assert task["deployment"]["benchmark"]["run_on_host"] is False
    assert "container" not in xtask_adapter.controller_context(case)
    case["environment"]["client"]["cpu_set"] = "0-3"
    with pytest.raises(ValueError, match="cannot inherit"):
        xtask_adapter.task_config(case)


def test_xtask_preview_detects_dropped_warmup(case, tmp_path):
    folder = tmp_path / "preview"
    folder.mkdir()
    command = common.client_command(case, folder)
    (folder / "run_evalscope_test.sh").write_text("if ! " + shlex.join(command) + "; then\nexit 1\nfi\n")
    xtask_adapter.verify_preview(case, tmp_path)
    index = command.index("--warmup-num")
    del command[index:index+2]
    (folder / "run_evalscope_test.sh").write_text("if ! " + shlex.join(command) + "; then\nexit 1\nfi\n")
    with pytest.raises(ValueError, match="warmup"):
        xtask_adapter.verify_preview(case, tmp_path)


@pytest.mark.parametrize("old", [False, True])
def test_metric_units_and_all_raw_metrics_are_preserved(case, tmp_path, old):
    measurement(tmp_path / "raw", old=old)
    result = common.collect_measurement(case, tmp_path / "raw")
    assert result["status"] == "PASS"
    assert result["metrics"]["ttft_ms"] == 100
    assert result["metrics"]["tpot_ms"] == 2
    assert result["metrics"]["avg_latency_s"] == 2.5
    assert result["raw_percentiles"][0]["TPOT (ms)"] == 4
    if not old:
        assert result["raw_summary"]["Unmodeled native metric"] == 77


def test_zero_exit_cannot_hide_failed_requests(case, tmp_path):
    measurement(tmp_path / "raw", failed=1)
    result = common.collect_measurement(case, tmp_path / "raw")
    assert result["status"] == "FAILED"
    assert result["metrics"]["success"] == 1


def test_request_level_length_audit_catches_near_average(case, tmp_path):
    measurement(tmp_path / "raw", output_tokens=2047)
    result = common.collect_measurement(case, tmp_path / "raw")
    assert result["status"] != "PASS"
    assert not result["request_audit"]["strict_lengths"]


def test_missing_latency_is_not_zero(case, tmp_path):
    summary = measurement(tmp_path / "raw")
    summary.pop("Avg TTFT (ms)")
    common.write_json(tmp_path / "raw/benchmark_summary.json", summary)
    result = common.collect_measurement(case, tmp_path / "raw")
    assert result["status"] == "INCONCLUSIVE"
    assert "ttft_ms" not in result["metrics"]


def test_accuracy_uses_explicit_score_path_and_threshold(case, tmp_path):
    case["kind"] = "accuracy"
    case["accuracy"] = {"datasets": ["gsm8k"], "dataset_revision": "fixture", "report_glob": "*.json", "score_pointer": "/results/0/score"}
    common.write_json(tmp_path / "dataset.json", {"results": [{"score": .85}]})
    result = common.collect_measurement(case, tmp_path)
    assert result["status"] == "INCONCLUSIVE"
    case["acceptance"]["minimum_score"] = .8
    assert common.collect_measurement(case, tmp_path)["status"] == "PASS"


def test_lifecycle_accepts_release_kind_without_optimization_gate(case, tmp_path):
    flow = release.workflow()
    spec = common.experiment(case, tmp_path)
    assert flow.validate_spec(spec) == []
    assert spec["identity"]["kind"] == "release_validation"


def fake_runtime(monkeypatch):
    flow = release.workflow()
    events = []
    monkeypatch.setattr(release, "workflow", lambda: flow)
    def preflight(path, output):
        result = {"status": "PASS", "source": {"valid": True}, "versions": {}}
        if output:
            common.write_json(output / "preflight.json", result)
        return result
    monkeypatch.setattr(flow, "preflight", preflight)
    monkeypatch.setattr(release, "verify_shared", lambda *args: None)
    def identity(case, attempt):
        value = {"commit": case["code"]["commit"], "binary_sha256": "fixture"}
        common.write_json(attempt / "identity.json", value)
        common.write_json(attempt / "client-identity.json", {"evalscope": "fixture"})
        return value
    monkeypatch.setattr(release, "verify_target", identity)
    monkeypatch.setattr(release, "snapshot", lambda *a, **kw: {})
    def execute(context, body, logfile, **kwargs):
        logfile.write_text(body)
        if logfile.name == "flow-preflight.log":
            common.write_json(logfile.parent / "preflight/preflight.json", {"status": "PASS"})
        if logfile.name == "flow-create.log":
            flow.run_create(logfile.parent.parent / "experiment.yaml")
    monkeypatch.setattr(release, "run", execute)
    monkeypatch.setattr(direct_adapter, "start", lambda *args: events.append("start"))
    monkeypatch.setattr(direct_adapter, "stop", lambda *args: events.append("stop"))
    def evaluate(case, attempt):
        events.append(case["id"])
        measurement(attempt / "raw")
    monkeypatch.setattr(direct_adapter, "evaluate", evaluate)
    return events


def test_direct_campaign_reuses_service_and_resumes_without_rerunning(config, tmp_path, monkeypatch):
    events = fake_runtime(monkeypatch)
    spec, root = tmp_path / "release.yaml", tmp_path / "run"
    spec.write_text(yaml.safe_dump(config))
    release.create_plan(spec, root)
    first = release.execute(root)
    assert first["counts"] == {"PASS": 2}
    assert events == ["start", "qwen-bf16-direct.v1.r0", "qwen-bf16-direct.v2.r0", "stop"]
    assert len(release.workflow().read_attempts(root / "cases/qwen-bf16-direct.v1.r0")) == 1
    release.execute(root, resume=True)
    assert len(events) == 4
    release.report(root)
    assert (root / "report.md").exists() and (root / "reproduction.md").exists()
    raw = next(root.rglob("benchmark_summary.json"))
    raw.write_text("{}")
    with pytest.raises(ValueError, match="modified"):
        release.execute(root, resume=True)


def test_failed_group_cleans_up_and_retry_gets_new_attempt(config, tmp_path, monkeypatch):
    events = fake_runtime(monkeypatch)
    def fail(case, attempt):
        raise RuntimeError("fixture client failure")
    monkeypatch.setattr(direct_adapter, "evaluate", fail)
    spec, root = tmp_path / "release.yaml", tmp_path / "run"
    spec.write_text(yaml.safe_dump(config))
    plan = release.create_plan(spec, root)
    result = release.execute(root)
    assert result["counts"] == {"FAILED": 2} and events[-1] == "stop"
    with pytest.raises(ValueError, match="retry-failed"):
        release.execute(root, resume=True)
    monkeypatch.setattr(direct_adapter, "evaluate", lambda case, attempt: measurement(attempt / "raw"))
    assert release.execute(root, resume=True, retry_failed=True)["counts"] == {"PASS": 2}
    for case in plan["cases"]:
        assert len(release.attempts(root, case)) == 2
        assert common.read_json(release.attempts(root, case)[0] / "result.json")["status"] == "FAILED"


def test_interruption_records_failure_cleans_up_and_stops_campaign(config, tmp_path, monkeypatch):
    events = fake_runtime(monkeypatch)
    def interrupt(case, attempt):
        raise KeyboardInterrupt("fixture interruption")
    monkeypatch.setattr(direct_adapter, "evaluate", interrupt)
    spec, root = tmp_path / "release.yaml", tmp_path / "run"
    spec.write_text(yaml.safe_dump(config))
    plan = release.create_plan(spec, root)
    with pytest.raises(KeyboardInterrupt):
        release.execute(root)
    assert events == ["start", "stop"]
    assert all(release.latest(root, c)["status"] == "FAILED" for c in plan["cases"])
    checkpoint = common.read_json(root / "release-checkpoint.json")
    assert set(checkpoint["cases"].values()) == {"FAILED"}


def test_retry_limit_is_checked_before_creating_any_attempt(config, tmp_path):
    spec, root = tmp_path / "release.yaml", tmp_path / "run"
    spec.write_text(yaml.safe_dump(config))
    plan = release.create_plan(spec, root)
    last = plan["cases"][-1]
    for index in range(1, last["max_attempts"] + 1):
        (root / "cases" / last["id"] / f"attempt-{index:03d}").mkdir()
    with pytest.raises(ValueError, match="max_attempts"):
        release.execute_group(root, plan["cases"], retry_failed=True)
    assert not release.attempts(root, plan["cases"][0])


@pytest.mark.parametrize("failed", [False, True])
def test_xtask_session_runs_smoke_before_trials_and_always_closes(case, tmp_path, monkeypatch, failed):
    import types
    import signal
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(signal, "alarm", lambda *args: None)
    events = []
    scheduler = types.ModuleType("xtask.core.scheduler")
    scheduler.start_scheduler_session = lambda *args: (events.append("start") or types.SimpleNamespace(prepared=True, failed=False))
    def trial(*args):
        events.append("trial")
        return {"benchmark_success": not failed}
    scheduler.run_scheduler_session_trial = trial
    scheduler.close_scheduler_session = lambda session: events.append("close")
    for name in ("xtask", "xtask.core"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "xtask.core.scheduler", scheduler)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: events.append(cmd[2]))
    monkeypatch.setattr(xtask_adapter, "run", lambda *a, **kw: None)
    case["xtask"]["repo"] = str(tmp_path / "external-xtask")
    attempt, workspace = tmp_path / "attempt-001", tmp_path / "workspace"
    attempt.mkdir()
    xtask_adapter.evaluate_group([(case, attempt, workspace)])
    source = (attempt / "xtask-session.py").read_text()
    if failed:
        with pytest.raises(RuntimeError, match="benchmark failed"):
            exec(compile(source, "xtask-session.py", "exec"), {})
    else:
        exec(compile(source, "xtask-session.py", "exec"), {})
    assert events == ["start", "ready", "smoke", "trial", "close"]


def test_missing_xtask_capability_blocks_without_modifying_source(case, tmp_path):
    repo = tmp_path / "external"
    (repo / "config").mkdir(parents=True)
    for name in ("xTask_config.json", "xllm_config.json"):
        common.write_json(repo / "config" / name, {})
    generator = repo / "xtask/benchmarks/evalscope/generate_evalscope_script.py"
    generator.parent.mkdir(parents=True)
    generator.write_text("keys=['warmup_num','temperature','tokenize_prompt']\n")
    core = repo / "xtask/core"
    core.mkdir()
    (core / "scheduler.py").write_text("old_api=True\n")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-m", "fixture"], check=True, capture_output=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    case["xtask"].update(repo=str(repo), commit=commit, context={"python": sys.executable})
    attempt = tmp_path / "attempt-001"
    attempt.mkdir()
    with pytest.raises(ValueError, match="Execution failed"):
        xtask_adapter.prepare(case, attempt)
    assert "capability missing" in (attempt / "xtask-preview.log").read_text()
    assert not subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip()
    assert (core / "scheduler.py").read_text() == "old_api=True\n"


@pytest.mark.parametrize("mismatch", [None, "commit", "binary_sha256", "working_tree"])
def test_target_identity_checks_real_binary_commit_and_build_source(case, tmp_path, monkeypatch, mismatch):
    import hashlib
    repo = tmp_path / "code"
    repo.mkdir()
    (repo / "source.txt").write_text("original\n")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-m", "fixture"], check=True, capture_output=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    binary = tmp_path / "fake-xllm"
    flags = list(case["service"]["flags"]) + ["model", "model_id", "nnodes", "node_rank", "port", "master_node_addr", "draft_model"]
    binary.write_text("#!/bin/sh\necho '" + " ".join("--" + f for f in flags) + "'\n")
    binary.chmod(0o755)
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    case["model"].update(path=str(model), tokenizer=str(model), draft_model=str(model))
    case["service"]["verify_acl_mapping"] = False
    case["environment"]["service"] = {"python": sys.executable}
    case["environment"]["client"] = {"python": sys.executable}
    case["code"].update(repo=str(repo), commit=commit, binary=str(binary), allow_dirty=True,
                          build_verdict=str(tmp_path / "verdict.json"), binary_provenance=str(tmp_path / "provenance.json"))
    common.write_json(case["code"]["build_verdict"], {"status": "PASS", "binary_ready": True})
    provenance = {"commit": commit, "repo": str(repo), "dirty_diff_sha256": hashlib.sha256(b"{}").hexdigest(),
                  "binary": {"path": str(binary), "sha256": common.file_hash(binary)}}
    if mismatch == "commit": provenance["commit"] = "f" * 40
    if mismatch == "binary_sha256": provenance["binary"]["sha256"] = "wrong"
    if mismatch == "working_tree": (repo / "source.txt").write_text("changed after build\n")
    common.write_json(case["code"]["binary_provenance"], provenance)
    original_run = release_runtime.run
    def run(context, body, log, **kwargs):
        if log.name == "client-identity.log":
            common.write_json(log.parent / "client-identity.json", {"evalscope": "fixture"})
        else:
            original_run(context, body, log, **kwargs)
    monkeypatch.setattr(release_runtime, "run", run)
    attempt = tmp_path / "attempt-001"
    attempt.mkdir()
    if mismatch:
        with pytest.raises(ValueError, match="Execution failed"):
            release_runtime.verify_target(case, attempt)
    else:
        observed = release_runtime.verify_target(case, attempt)
        assert observed["commit"] == commit
        assert observed["binary_sha256"] == common.file_hash(binary)
        assert (attempt / "code-working-tree.diff").read_text() == ""


def test_cross_version_comparison_requires_weight_and_environment_identity(case, tmp_path):
    row = {key: case.get(key) for key in ("model", "service", "workload", "accuracy", "environment", "mode")}
    row.update(case_id=case["id"], result={"status": "PASS", "metrics": {"output_tps": 10}})
    baseline = tmp_path / "baseline.json"
    common.write_json(baseline, {"cases": [row]})
    current = deepcopy(row)
    current["result"]["metrics"]["output_tps"] = 12
    assert release.compare_baseline({"cases": [current]}, baseline)[0]["status"] == "REFERENCE_ONLY"
    row["result"]["observed_identity"] = {"model_weights_verified": True}
    current["result"]["observed_identity"] = {"model_weights_verified": True}
    common.write_json(baseline, {"cases": [row]})
    comparison = release.compare_baseline({"cases": [current]}, baseline)[0]
    assert comparison["status"] == "COMPARABLE" and comparison["delta_pct"]["output_tps"] == 20
    current["mode"] = "xtask"
    assert release.compare_baseline({"cases": [current]}, baseline)[0]["status"] == "REFERENCE_ONLY"


def test_excel_semantic_matching_preserves_style_formulas_and_false(tmp_path):
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font
    book = Workbook()
    sheet = book.active
    sheet.title = "Planning"
    sheet.append(["QPS", "Concurrency", "Model", "Graph", "Formula"])
    sheet.append([None, 2, "example", None, "=1+1"])
    sheet["A2"].font = Font(bold=True)
    book.save(tmp_path / "input.xlsx")
    row = {"case_id": "example", "model": {"name": "example"}, "workload": {"parallel": 2},
           "service": {"flags": {"enable_graph": False}}, "result": {"status": "PASS", "metrics": {"request_throughput": .2}}}
    common.write_json(tmp_path / "results.json", {"cases": [row]})
    mapping = {"workbooks": [{"input": "input.xlsx", "output": "output.xlsx", "sheets": [{
        "name": "Planning", "match": {"Model": "model.name", "Concurrency": "workload.parallel"},
        "fill": {"QPS": "result.metrics.request_throughput", "Graph": "service.flags.enable_graph"}}]}]}
    (tmp_path / "mapping.yaml").write_text(yaml.safe_dump(mapping))
    export_workbooks.export(tmp_path / "results.json", tmp_path / "mapping.yaml")
    output = load_workbook(tmp_path / "output.xlsx")
    assert output["Planning"]["A2"].value == .2
    assert output["Planning"]["D2"].value == "false"
    assert output["Planning"]["E2"].value == "=1+1"
    assert output["Planning"]["A2"].font.bold
    assert "case_id=example" in output["Planning"]["A2"].comment.text
    assert load_workbook(tmp_path / "input.xlsx")["Planning"]["A2"].value is None


@pytest.mark.parametrize("in_place", [False, True])
def test_excel_feature_columns_default_values_and_backup(tmp_path, in_place):
    from openpyxl import Workbook, load_workbook
    book = Workbook()
    sheet = book.active
    sheet.title = "Features"
    sheet.append(["Graph", "example", None])
    sheet.append(["New flag", "", None])
    book.save(tmp_path / "input.xlsx")
    row = {"case_id": "one", "service": {"flags": {"enable_graph": True}}, "result": {"status": "PASS"}}
    common.write_json(tmp_path / "results.json", {"cases": [row]})
    item = {"input": "input.xlsx", "output": "input.xlsx" if in_place else "output.xlsx", "sheets": [{
        "name": "Features", "layout": "columns", "key_column": "A", "case_columns": {"C": {"case_id": "one"}},
        "fields": {"Graph": "service.flags.enable_graph", "New flag": "service.flags.unset"}}]}
    (tmp_path / "mapping.yaml").write_text(yaml.safe_dump({"workbooks": [item]}))
    if in_place:
        with pytest.raises(ValueError, match="in-place"):
            export_workbooks.export(tmp_path / "results.json", tmp_path / "mapping.yaml")
    export_workbooks.export(tmp_path / "results.json", tmp_path / "mapping.yaml", in_place=in_place)
    output = load_workbook(tmp_path / item["output"])
    assert output["Features"]["C1"].value == "true"
    assert output["Features"]["C2"].value == "default"
    if in_place:
        backups = list(tmp_path.glob("input.backup-*.xlsx"))
        assert len(backups) == 1
        assert load_workbook(backups[0])["Features"]["C1"].value is None
