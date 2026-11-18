#!/usr/bin/env python3
"""Plan, execute, resume, collect and report release data with two adapters."""
from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import csv
import fcntl
import importlib.util
import json
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import sys
import time

import yaml

import direct_adapter
import xtask_adapter
from release_common import (ASSETS, ROOT, collect_measurement, digest, expand, experiment,
                            file_hash, load_config, read_json, require, write_json)
from release_runtime import flow, run, snapshot, verify_shared, verify_target
from frameworks import compare_frameworks, framework, validate_pairs


def workflow():
    spec = importlib.util.spec_from_file_location("release_xllm_flow", ROOT / "scripts/xllm_flow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def service_fingerprint(case):
    return digest({**{key: case.get(key) for key in ("framework", "mode", "kind", "code", "model", "service", "environment", "xtask", "repeat_index")},
                   "accuracy_datasets": case.get("accuracy", {}).get("datasets")})


def create_plan(spec_path, root, *, source_config=None):
    root = Path(root).resolve()
    require(not root.exists() or not any(root.iterdir()), "Use a new empty run root; plan never overwrites an existing campaign")
    from release_common import merge
    config = load_config(spec_path) if source_config is None else merge({}, source_config)
    cases = expand(config)
    validate_pairs(cases)
    root.mkdir(parents=True, exist_ok=True)
    groups = []
    for case in cases:
        key = service_fingerprint(case)
        if (case["service"].get("restart_policy", "per_case") == "reuse_group"
                and groups and groups[-1]["service_fingerprint"] == key):
            groups[-1]["case_ids"].append(case["id"])
        else:
            groups.append({"id": f"group-{len(groups)+1:03d}", "service_fingerprint": key, "case_ids": [case["id"]]})
        directory = root / "cases" / case["id"]
        directory.mkdir(parents=True)
        write_json(directory / "resolved.json", case)
        canonical = experiment(case, directory)
        (directory / "experiment.yaml").write_text(yaml.safe_dump(canonical, sort_keys=False))
        preview = directory / "preview"
        preview.mkdir()
        direct_adapter.render_service(case, preview)
        direct_adapter.render_workload(case, preview)
        if case["mode"] == "xtask":
            task = xtask_adapter.task_config(case)
            (preview / "xtask-case.yaml").write_text(yaml.safe_dump(task, sort_keys=False))
    plan = {"schema_version": 1, "release": config["release"], "config": config,
            "source_spec": str(Path(spec_path).resolve()), "run_root": str(root),
            "cases": cases, "groups": groups, "source_fingerprint": digest(config)}
    plan["fingerprint"] = digest(plan)
    write_json(root / "plan.json", plan)
    (root / "resolved-release.yaml").write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    checkpoint(root, plan)
    return plan


def load_plan(root):
    root = Path(root).resolve()
    plan = read_json(root / "plan.json")
    require(plan["run_root"] == str(root), "Run root moved; create a new plan instead of changing path identity")
    require(plan["fingerprint"] == digest({k: v for k, v in plan.items() if k != "fingerprint"}), "Plan identity changed")
    for case in plan["cases"]:
        require(read_json(root / "cases" / case["id"] / "resolved.json") == case, "Resolved case changed")
    return plan


def attempts(root, case):
    return sorted((Path(root) / "cases" / case["id"]).glob("attempt-[0-9][0-9][0-9]"))


def latest(root, case):
    entries = attempts(root, case)
    return read_json(entries[-1] / "result.json") if entries and (entries[-1] / "result.json").exists() else {"status": "PENDING"}


def verify_completed(root, case):
    result = latest(root, case)
    if result.get("status") != "PASS":
        return False
    require(result.get("case_fingerprint") == case["fingerprint"], "Completed case identity differs")
    for relative, expected in result.get("sha256", {}).items():
        path = Path(root) / relative
        require(path.is_file() and file_hash(path) == expected, f"Completed artifact modified: {relative}")
    require(bool(result.get("sha256")), "Completed attempt has no artifact hashes")
    return True


def verify_resume_identity(root, case):
    """A cached PASS describes one binary/model/environment, not just a YAML."""
    import tempfile
    previous = attempts(root, case)[-1]
    directory = Path(tempfile.mkdtemp(prefix="identity-", dir=previous.parent))
    observed = verify_target(case, directory)
    original = read_json(previous / "identity.json")
    for key in ("commit", "git_status", "dirty_diff_sha256", "binary_sha256", "model_metadata_sha256", "device_mapping", "packages", "image_id", "workload_identity"):
        require(observed.get(key) == original.get(key), f"Resume identity changed: {key}; create a new plan")
    require(read_json(directory / "client-identity.json") == read_json(previous / "client-identity.json"), "Client identity changed")


def checkpoint(root, plan):
    statuses = {case["id"]: latest(root, case).get("status", "PENDING") for case in plan["cases"]}
    write_json(Path(root) / "release-checkpoint.json", {"plan_fingerprint": plan["fingerprint"], "cases": statuses})
    lines = ["# Release checkpoint", "", f"- plan: `{plan['fingerprint']}`",
             "- Case identity and attempt ledger: cases/<id>/manifest.json and attempts.jsonl",
             "- Resume complete reuse groups; never resume a lost graph state at the last workload.", "",
             "| Case | Status |", "| --- | --- |"]
    lines += [f"| {key} | {status} |" for key, status in statuses.items()]
    (Path(root) / "CHECKPOINT.md").write_text("\n".join(lines) + "\n")


@contextlib.contextmanager
def device_lease(case, root):
    pool = case["environment"].get("resource_pool_id")
    require(pool, "environment.resource_pool_id must identify the physical host consistently")
    directory = Path(case["environment"].get("lease_dir", str(Path(root).parent / ".release-device-leases")))
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        for device in sorted(case["service"]["physical_devices"]):
            handle = stack.enter_context((directory / f"{digest(pool)[:16]}-{device}.lock").open("a+"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError(f"Device reserved by another release campaign: {device}") from exc
        yield


def write_result(root, case, attempt, outcome):
    result = {"case_id": case["id"], "case_fingerprint": case["fingerprint"],
              "framework": framework(case), "mode": case["mode"], "kind": case["kind"], "claim_scope": "measurement-only",
              "attempt": str(Path(attempt).relative_to(root)), **outcome}
    if (Path(attempt) / "identity.json").exists():
        identity = read_json(Path(attempt) / "identity.json")
        result["observed_identity"] = {k: identity.get(k) for k in ("commit", "binary_sha256", "model_metadata_sha256", "base_model_sha256", "model_weight_sha256", "model_weights_verified", "draft_weights_verified", "device_mapping", "packages", "image_id", "hardware_identity", "workload_identity")}
    if (Path(attempt) / "activation.json").exists(): result["activation"] = read_json(Path(attempt) / "activation.json")
    if (Path(attempt) / "client-identity.json").exists():
        result["client_identity"] = read_json(Path(attempt) / "client-identity.json")
    tracked = [p for p in Path(attempt).rglob("*") if p.is_file() and not p.is_symlink() and p.name != "result.json"]
    # Avoid following xTask input source symlinks during collection.
    tracked = [p for p in tracked if not any(parent.is_symlink() for parent in p.parents if parent != Path(attempt))]
    result["sha256"] = {str(p.relative_to(root)): file_hash(p) for p in tracked}
    write_json(Path(attempt) / "result.json", result)
    directory = Path(root) / "cases" / case["id"]
    if (directory / "manifest.json").exists():
        module = workflow()
        manifest = read_json(directory / "manifest.json")
        status = "pass" if result["status"] == "PASS" else "fail"
        module.append_attempt(directory, {
            "entry_id": Path(attempt).name, "run_id": case["id"], "attempt_id": Path(attempt).name,
            "spec_fingerprint": manifest["fingerprint"], "fingerprint": digest([case["fingerprint"], Path(attempt).name]),
            "phase": "benchmark", "status": status,
            "metrics": result.get("metrics", {"status": result["status"]}),
            "artifacts": [str(Path(attempt).relative_to(directory) / "result.json")],
            "reason": "; ".join(result.get("errors", [])) or ("" if status == "pass" else result["status"]),
        })
        write_json(directory / "result.json", result)
    return result


def execute_group(root, cases, retry_failed=False):
    group, contexts = [], []
    positive_existing = [verify_completed(root, case) for case in cases]
    if all(positive_existing):
        for case in cases:
            verify_resume_identity(root, case)
        return
    if any(positive_existing) and cases[0]["service"].get("restart_policy") == "reuse_group":
        require(retry_failed, "Partial reuse group requires --retry-failed and new attempts for the entire group")
    pending = []
    for case in cases:
        existing = attempts(root, case)
        if existing:
            require(retry_failed, "Existing incomplete/failed attempt requires --retry-failed")
        require(len(existing) < case.get("max_attempts", 1), f"max_attempts reached: {case['id']}")
        attempt = Path(root) / "cases" / case["id"] / f"attempt-{len(existing)+1:03d}"
        pending.append((case, attempt))
    # Validate the whole reuse group before creating any new attempt directories.
    for case, attempt in pending:
        attempt.mkdir()
        group.append((case, attempt))
    first, first_attempt = group[0]
    service_dir, execution_error, cleanup_error, stage = None, None, None, "preflight"
    interrupted = None
    try:
        for case, attempt in group:
            verify_shared(case, attempt)
            verify_target(case, attempt)
            directory = attempt.parent
            preflight = flow(case, directory, "preflight", "--spec", directory / "experiment.yaml", "--output", attempt / "preflight")
            # Existing service ownership deliberately has no free-port request in experiment.yaml.
            run(case["environment"].get("service", {}), preflight, attempt / "flow-preflight.log", case=case)
            require(read_json(attempt / "preflight/preflight.json")["status"] == "PASS", "Lifecycle preflight is not PASS")
            if not (directory / "manifest.json").exists():
                run(case["environment"].get("service", {}),
                    flow(case, directory, "run", "create", "--spec", directory / "experiment.yaml"),
                    attempt / "flow-create.log", case=case)
        if first["service"].get("ownership", "managed") == "managed":
            hardware = snapshot(first, first_attempt / "env/before")
            for case, attempt in group:
                identity = read_json(attempt / "identity.json")
                identity["hardware_identity"] = [{k: d.get(k) for k in ("physical_id", "name", "model", "memory_total_mb")} for d in hardware.get("devices", [])]
                write_json(attempt / "identity.json", identity)
        workflow_service = first["mode"] == "direct" or first["xtask"].get("service_mode") == "external"
        if workflow_service:
            service_dir = direct_adapter.render_service(first, first_attempt)
            stage = "service"
            direct_adapter.start(first, service_dir)
            for case, attempt in group:
                direct_adapter.check_activation(case, service_dir, attempt)
        if first["mode"] == "direct":
            for case, attempt in group:
                stage = "evaluation"
                direct_adapter.evaluate(case, attempt)
                write_json(attempt / "service-reference.json", {"directory": str(service_dir)})
                time.sleep(first["service"].get("inter_case_seconds", 10))
        else:
            for case, attempt in group:
                contexts.append((case, attempt, xtask_adapter.prepare(case, attempt)))
            stage = "evaluation"
            xtask_adapter.evaluate_group(contexts)
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        execution_error = str(exc)
    except (KeyboardInterrupt, SystemExit) as exc:
        interrupted = exc
        execution_error = "Campaign interrupted: " + str(exc)
    finally:
        try:
            if service_dir is not None:
                direct_adapter.stop(first, service_dir, first_attempt)
            elif stage != "preflight" and first["mode"] == "xtask":
                data = snapshot(first, first_attempt / "env/after_stop", after=True)
                if first["service"].get("ownership", "managed") == "managed":
                    require(all(not d["processes"] for d in data["devices"]), "xTask cleanup did not release physical devices")
        except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            cleanup_error = str(exc)
        for case, attempt in group:
            try:
                outcome = collect_measurement(case, attempt / "raw")
            except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
                outcome = {"status": "INCONCLUSIVE", "metrics": {}, "errors": [str(exc)]}
            outcome["measurement_status"] = outcome["status"]
            outcome["execution_status"] = "FAILED" if execution_error else "PASS"
            outcome["cleanup_status"] = "FAILED" if cleanup_error else "PASS" if stage != "preflight" else "NOT_STARTED"
            if execution_error:
                outcome["status"] = "BLOCKED" if stage == "preflight" else "FAILED"
                outcome["errors"].append(execution_error)
            if cleanup_error:
                outcome["status"] = "FAILED"
                outcome["errors"].append("Cleanup: " + cleanup_error)
            write_result(root, case, attempt, outcome)
    if interrupted is not None:
        raise interrupted
    require(not cleanup_error, cleanup_error or "")


def execute(root, resume=False, retry_failed=False):
    root, plan = Path(root).resolve(), load_plan(root)
    needs_prepare = bool(plan["config"].get("preparation")) or any(c.get("workload", {}).get("dataset") in {"gsm8k", "sharegpt"} for c in plan["cases"])
    if needs_prepare:
        prepared = read_json(root / "preparation.json")
        require(prepared.get("status") == "PASS" and prepared.get("plan_fingerprint") == plan["fingerprint"], "Run prepare successfully for this plan before executing")
    lookup = {case["id"]: case for case in plan["cases"]}
    with (root / ".release.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not resume:
            require(not any(attempts(root, c) for c in plan["cases"]), "Use resume for a campaign with existing attempts")
        for group in plan["groups"]:
            cases = [lookup[key] for key in group["case_ids"]]
            try:
                with device_lease(cases[0], root):
                    execute_group(root, cases, retry_failed)
            finally:
                checkpoint(root, plan)
    return collect(root)


def collect(root):
    root, plan = Path(root).resolve(), load_plan(root)
    rows = []
    for case in plan["cases"]:
        result = latest(root, case)
        if result.get("status") == "PASS":
            verify_completed(root, case)
        rows.append({"case_id": case["id"], "source_case_id": case["source_case_id"],
                     "fingerprint": case["fingerprint"], "service_fingerprint": service_fingerprint(case),
                     "kind": case["kind"], "framework": framework(case), "comparison_group": case.get("comparison_group"), "repeat_index": case["repeat_index"], "mode": case["mode"], "model": case["model"],
                     "service": case["service"], "workload": case.get("workload", {}),
                     "accuracy": case.get("accuracy", {}), "code": case["code"], "xtask": case["xtask"],
                     "acceptance": case["acceptance"], "delivery": case.get("delivery", {}), "timeout": case.get("timeout", 7200),
                     "environment": case["environment"], "result": result})
    statuses = Counter(row["result"].get("status", "PENDING") for row in rows)
    summary = {"release": plan["release"], "plan_fingerprint": plan["fingerprint"], "run_root": str(root),
               "claim_scope": "measurement-only", "counts": dict(statuses), "cases": rows}
    summary["framework_comparisons"] = compare_frameworks(summary)
    write_json(root / "framework-comparison.json", summary["framework_comparisons"])
    write_json(root / "results.json", summary)
    checkpoint(root, plan)
    return summary


def compare_baseline(summary, path):
    baseline = read_json(path)
    previous = {row["case_id"]: row for row in baseline["cases"]}
    comparisons = []
    for row in summary["cases"]:
        old = previous.get(row["case_id"])
        if not old:
            continue
        fields = ["model", "service", "workload", "accuracy", "environment", "mode"]
        differences = [key for key in fields if old.get(key) != row.get(key)]
        for key in ("model_metadata_sha256", "device_mapping", "packages"):
            if old["result"].get("observed_identity", {}).get(key) != row["result"].get("observed_identity", {}).get(key):
                differences.append("observed_identity." + key)
        if old["result"].get("client_identity") != row["result"].get("client_identity"):
            differences.append("client_identity")
        if not (old["result"].get("observed_identity", {}).get("model_weights_verified") and row["result"].get("observed_identity", {}).get("model_weights_verified")):
            differences.append("full model weight identity not verified")
        valid = not differences and old["result"]["status"] == row["result"]["status"] == "PASS"
        entry = {"case_id": row["case_id"], "status": "COMPARABLE" if valid else "REFERENCE_ONLY", "differences": differences}
        if valid:
            old_metrics, metrics = old["result"]["metrics"], row["result"]["metrics"]
            entry["delta_pct"] = {key: 100 * (value - old_metrics[key]) / old_metrics[key]
                                  for key, value in metrics.items() if type(value) in {int, float}
                                  and type(old_metrics.get(key)) in {int, float} and old_metrics[key] != 0}
        comparisons.append(entry)
    return comparisons


def report(root):
    root = Path(root).resolve()
    summary = collect(root)
    from delivery_examples import examples, render
    representative, variants = examples(summary, root, incomplete=True)
    lines = ["# xLLM 发版测试数据", "", f"- 版本：{summary['release']['version']}",
             f"- Commit：`{summary['release']['commit']}`", f"- 场景状态：{summary['counts']}",
             "- 结果范围：measurement-only。正式跨框架/发版性能声明仍需 benchmark 的公平性与证据检查。", "",
             "| 场景 | 框架 | 方式 | 状态 | 成功/总数 | QPS | 输出 tok/s | 平均 E2E s | TTFT ms | TPOT ms | ITL ms |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in summary["cases"]:
        result, metrics = row["result"], row["result"].get("metrics", {})
        values = [row["case_id"], framework(row), row["mode"], result["status"], f"{metrics.get('success','N/A')}/{metrics.get('total','N/A')}"]
        values += [metrics.get(key, "N/A") for key in ("request_throughput", "output_tps", "avg_latency_s", "ttft_ms", "tpot_ms", "itl_ms")]
        lines.append("| " + " | ".join(map(str, values)) + " |")
    lines += ["", "## 逐场景结果与原始指标", ""]
    for row in summary["cases"]:
        lines += [f"### {row['case_id']}", "", f"- 结果：{row['result'].get('attempt','待执行')}",
                  f"- 说明：{'; '.join(row['result'].get('errors',[])) or '无'}", "", "```json",
                  json.dumps(row["result"], indent=2, ensure_ascii=False), "```", ""]
    if summary["release"].get("baseline_results"):
        comparisons = compare_baseline(summary, summary["release"]["baseline_results"])
        write_json(root / "comparison.json", comparisons)
        lines += ["## 历史比较", "", "```json", json.dumps(comparisons, indent=2, ensure_ascii=False), "```", ""]
    lines += ["## 跨框架比较与调优差异", "", "```json", json.dumps(summary["framework_comparisons"], ensure_ascii=False, indent=2), "```", "",
              "## 代表场景执行配置", "", render(representative), "## 其他场景配置差异", "",
              "```json", json.dumps(variants, ensure_ascii=False, indent=2), "```", ""]
    (root / "report.md").write_text((ASSETS / "templates/release-report.md").read_text().replace("@@CONTENT@@", "\n".join(lines)))
    reproduction = ["# 发版测试复现", "", f"- 版本：{summary['release']['version']}",
                    f"- 计划：{root / 'plan.json'}", f"- Workflow：{ROOT}",
                    "- direct 按实际环境与脚本复现；xTask 原生交付用例与配套说明不依赖原容器/产物。",
                    "- 新 run root 必须不存在或为空；不要直接重跑旧 attempt 中写死输出目录的脚本。", "",
                    "内部实际配置和日志按 case/attempt 归档。全部 xTask 交付用例在 delivery/cases；下文只展示代表场景，其他场景另列差异。",
                    "xTask 接收方按同名 .md 使用 xTask 原生工具获取源码、编译，再以 main.py --case_name 执行；无需 workflow、发版 skill 或 release.py。",
                    "交付 YAML 是从内部配置派生的原生用例；独立硬件执行仍待验证，不将原环境结果视为该流程已验证。", "",
                    render(representative), "## 其他场景配置差异", "", "```json", json.dumps(variants, ensure_ascii=False, indent=2), "```", ""]
    (root / "reproduction.md").write_text((ASSETS / "templates/reproduction.md").read_text().replace("@@CONTENT@@", "\n".join(reproduction)))
    with (root / "metrics.csv").open("w") as f:
        keys = ["case_id", "framework", "mode", "status", "request_throughput", "output_tps", "avg_latency_s", "ttft_ms", "tpot_ms", "itl_ms"]
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for row in summary["cases"]:
            metrics = row["result"].get("metrics", {})
            writer.writerow({"case_id": row["case_id"], "framework": framework(row), "mode": row["mode"], "status": row["result"]["status"],
                             **{key: metrics.get(key) for key in keys[4:]}})
    return root / "report.md"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--spec", required=True, type=Path)
    plan.add_argument("--run-root", required=True, type=Path)
    for name in ("prepare", "run", "resume", "collect", "report"):
        command = commands.add_parser(name)
        command.add_argument("--run-root", required=True, type=Path)
        if name == "resume":
            command.add_argument("--retry-failed", action="store_true")
        if name == "report":
            command.add_argument("--docx-content", type=Path, help="Delivery narrative YAML for the source-derived Word template")
            command.add_argument("--docx-output", type=Path, help="New Word output path; defaults to run-root/delivery-report.docx")
    args = parser.parse_args(argv)
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        if args.command == "plan":
            value = create_plan(args.spec, args.run_root)
            print(json.dumps({"plan": str(args.run_root / "plan.json"), "cases": len(value["cases"]), "groups": len(value["groups"])}, indent=2))
        elif args.command == "prepare":
            from release_prepare import prepare_campaign
            print(json.dumps(prepare_campaign(args.run_root.resolve(), load_plan(args.run_root)), ensure_ascii=False, indent=2))
        elif args.command in {"run", "resume"}:
            value = execute(args.run_root, resume=args.command == "resume", retry_failed=getattr(args, "retry_failed", False))
            print(json.dumps(value["counts"]))
            return 0 if set(value["counts"]) <= {"PASS"} else 1
        elif args.command == "collect":
            print(json.dumps(collect(args.run_root), ensure_ascii=False, indent=2))
        else:
            print(report(args.run_root))
            if args.docx_content:
                from export_delivery_docx import export
                result = export(args.run_root / "results.json", args.docx_content,
                                args.docx_output or args.run_root / "delivery-report.docx")
                print(result["output"])
    except (ValueError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Campaign interrupted; inspect checkpoint and cleanup evidence before resuming.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
