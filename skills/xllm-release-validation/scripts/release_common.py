"""Shared release configuration, command rendering and measurement validation.

No serving framework or xTask source is modified by this module.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import re
import shlex
import sqlite3
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
ASSETS = Path(__file__).resolve().parents[1] / "assets"
RESERVED_FLAGS = {"model", "model_id", "draft_model", "nnodes", "node_rank", "port", "master_node_addr"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def put(value, dotted, replacement):
    parts = dotted.split(".")
    require(parts[0] in {"service", "workload", "accuracy"}, f"Unsupported matrix axis: {dotted}")
    cursor = value
    for key in parts[:-1]:
        cursor = cursor.setdefault(key, {})
        require(isinstance(cursor, dict), f"Matrix axis traverses a scalar: {dotted}")
    cursor[parts[-1]] = replacement


def positive(value, label, zero=False):
    require(type(value) is int and value >= (0 if zero else 1), f"{label} must be an integer >= {0 if zero else 1}")


def load_config(path):
    path = Path(path).resolve()
    config = yaml.safe_load(path.read_text())
    require(isinstance(config, dict), "Release specification must be a mapping")
    if isinstance(config.get("environment"), str):
        config["environment"] = yaml.safe_load((path.parent / config["environment"]).read_text())
    if isinstance(config.get("preparation"), str):
        config["preparation"] = yaml.safe_load((path.parent / config["preparation"]).read_text())
    if isinstance(config.get("delivery"), str):
        config["delivery"] = yaml.safe_load((path.parent / config["delivery"]).read_text())
    require(config.get("schema_version") == 1, "schema_version must be 1")
    require(isinstance(config.get("cases"), list) and config["cases"], "cases must be non-empty")
    release = config.get("release", {})
    require(release.get("version"), "release.version is required")
    require(re.fullmatch(r"[0-9a-f]{40}", str(release.get("commit", ""))), "release.commit must be a full commit SHA")
    return config


def expand(config):
    cases, ids = [], set()
    for definition in config["cases"]:
        identifier = definition.get("id", "")
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", identifier), "case id must be a safe file name")
        require(identifier not in ids, f"Duplicate case id: {identifier}")
        ids.add(identifier)
        base = merge(config.get("defaults", {}), definition)
        for section in ("model", "service", "workload", "accuracy", "code"):
            profile = base.pop(section + "_profile", None)
            if profile:
                profiles = config.get(section + "_profiles", {})
                require(profile in profiles, f"Unknown {section}_profile: {profile}")
                base[section] = merge(profiles[profile], base.get(section, {}))
        axes = base.pop("matrix", {})
        require(isinstance(axes, dict), "matrix must be a mapping")
        if isinstance(base.get("workload", {}).get("parallel"), list):
            require("workload.parallel" not in axes, "parallel list and matrix axis conflict")
            axes["workload.parallel"] = base["workload"].pop("parallel")
        for key, values in axes.items():
            require(isinstance(values, list) and values and len({digest(v) for v in values}) == len(values), f"Matrix values must be non-empty and unique: {key}")
        repeats = base.pop("repetitions", 1)
        positive(repeats, "repetitions")
        require(len(cases) + math.prod(len(v) for v in axes.values()) * repeats <= config.get("execution", {}).get("max_cases", 1000), "Expanded matrix exceeds max_cases")
        combinations = list(itertools.product(*axes.values())) if axes else [()]
        for repeat in range(repeats):
            for index, values in enumerate(combinations, 1):
                case = copy.deepcopy(base)
                for key, value in zip(axes, values):
                    put(case, key, value)
                case["source_case_id"] = identifier
                case["id"] = f"{identifier}.v{index}.r{repeat}"
                case["repeat_index"] = repeat
                case["mode"] = case.get("mode", config.get("execution", {}).get("default_mode", "direct"))
                case["kind"] = case.get("kind", "performance")
                case["framework"] = case.get("framework", "xllm")
                if case["framework"] == "xllm":
                    case.setdefault("service", {}).setdefault("flags", {}).setdefault("npu_kernel_backend", "TORCH")
                case["code"] = merge(config.get("code", {}) if case["framework"] == "xllm" else {}, case.get("code", {}))
                if case["framework"] == "xllm": case["code"]["commit"] = config["release"]["commit"]
                case["environment"] = merge(config.get("environment", {}), case.get("environment", {}))
                case["xtask"] = merge(config.get("xtask", {}), case.get("xtask", {}))
                case["delivery"] = merge(config.get("delivery", {}), case.get("delivery", {}))
                case["acceptance"] = merge(config.get("acceptance", {}), case.get("acceptance", {}))
                validate_case(case)
                case["fingerprint"] = digest(case)
                cases.append(case)
    require(len(cases) <= config.get("execution", {}).get("max_cases", 1000), "Expanded matrix exceeds max_cases")
    return cases


def validate_case(case):
    positive(case.get("max_attempts", 1), "max_attempts")
    require(case["mode"] in {"direct", "xtask"}, "mode must be direct or xtask")
    require(case["kind"] in {"performance", "accuracy"}, "kind must be performance or accuracy")
    require(case.get("framework", "xllm") in {"xllm", "vllm-ascend"}, "Unsupported serving framework")
    model, service, env = case.get("model", {}), case.get("service", {}), case["environment"]
    for key in ("name", "path", "tokenizer", "dtype", "quantization"):
        require(model.get(key), f"model.{key} is required")
    code_fields = ("repo", "binary", "build_verdict", "binary_provenance") if case.get("framework", "xllm") == "xllm" else ("binary", "version", "vllm_version", "image")
    for key in code_fields:
        require(case["code"].get(key), f"code.{key} is required")
    require(service.get("api_url"), "service.api_url is required and must be reachable from the client")
    require(service.get("ownership", "managed") in {"managed", "external"}, "Invalid service ownership")
    if service.get("ownership") == "external":
        require(service.get("external_pid_file"), "External services require a PID/starttime manifest for read-only identity verification")
    require(service.get("restart_policy", "per_case") in {"per_case", "reuse_group"}, "Invalid restart_policy")
    devices, physical = service.get("devices"), service.get("physical_devices")
    require(isinstance(devices, list) and devices and len(set(devices)) == len(devices), "Explicit unique service.devices are required")
    require(isinstance(physical, list) and len(physical) == len(devices) and len(set(physical)) == len(physical), "physical_devices must match devices")
    require(all(type(d) is int and d >= 0 for d in devices + physical), "Device IDs must be non-negative integers")
    require(len(service.get("cpu_sets", [])) == len(devices), "One CPU set per rank is required")
    for value in service["cpu_sets"]:
        require(re.fullmatch(r"[0-9,-]+", str(value)), "Invalid CPU affinity")
    for field in ("port", "master_port", "hccl_port"):
        positive(service.get(field), f"service.{field}")
        require(service[field] < 65536, f"Invalid {field}")
    flags = service.get("flags", {})
    require(isinstance(flags, dict), "service.flags must be a mapping")
    for key, value in flags.items():
        if case.get("framework", "xllm") == "xllm":
            require(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key) and key not in RESERVED_FLAGS, f"Invalid or identity-reserved flag: {key}")
            require(type(value) in {str, int, float, bool} and not (isinstance(value, float) and not math.isfinite(value)), f"Invalid flag value: {key}")
        else:
            require(re.fullmatch(r"[a-z][a-z0-9-]*", key) and key not in {"model", "served-model-name", "port", "host", "dtype"}, f"Invalid or identity-reserved vLLM flag: {key}")
            json.dumps(value, allow_nan=False)
    for key, value in service.get("env", {}).items():
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and type(value) in {str, int, float, bool}, f"Invalid environment variable: {key}")
    require(env.get("shared_filesystem") is True, "Control host, execution contexts and workflow checkout must share absolute artifact paths")
    if case["kind"] == "performance":
        workload = case.get("workload", {})
        for field in ("input_tokens", "output_tokens", "parallel", "number"):
            positive(workload.get(field), f"workload.{field}")
        positive(workload.get("warmup"), "workload.warmup", zero=True)
        require(workload.get("dataset") in {"random", "gsm8k", "sharegpt"}, "Supported performance datasets: random, gsm8k, sharegpt")
        if workload["dataset"] != "random":
            require(workload.get("dataset_revision") and workload.get("dataset_path") and workload.get("prepared_path"), "Real datasets require revision, dataset_path and prepared_path")
            require(workload.get("length_policy", "native") in {"native", "filter", "truncate"}, "Unsupported real-data length_policy")
            require(workload.get("strict_lengths") is False, "Real datasets require strict_lengths=false; record actual lengths instead of claiming random-style exact inputs")
        require(workload.get("endpoint") in {"/completions", "/chat/completions"}, "Explicit performance endpoint is required")
        require(workload.get("rate", -1) == -1 or workload.get("rate", -1) > 0, "rate must be -1 or positive")
        require(not (workload.get("rate", -1) > 0 and not workload.get("open_loop", False)), "A positive rate requires explicit open_loop=true")
    else:
        accuracy = case.get("accuracy", {})
        require(accuracy.get("datasets") and accuracy.get("report_glob") and accuracy.get("score_pointer"), "accuracy requires datasets, report_glob and score_pointer")
        require(accuracy.get("dataset_revision"), "accuracy.dataset_revision is required")
    if case.get("comparison_group"):
        from frameworks import semantic_contract
        semantic_contract(case)


def flag_args(case, rank):
    service, model = case["service"], case["model"]
    values = {"model": model["path"], "model_id": model["name"],
              "master_node_addr": f"127.0.0.1:{service['master_port']}",
              "nnodes": len(service["devices"]), "node_rank": rank,
              "port": service["port"] + rank}
    if model.get("draft_model"):
        values["draft_model"] = model["draft_model"]
    values.update(service.get("flags", {}))
    return [f"--{key}={str(value).lower() if type(value) is bool else value}" for key, value in values.items()]


def client_command(case, out):
    client = case["environment"].get("client", {})
    cmd = list(client.get("evalscope_command", ["evalscope"]))
    if client.get("cpu_set"):
        require(re.fullmatch(r"[0-9,-]+", str(client["cpu_set"])), "Invalid client CPU affinity")
        cmd = ["taskset", "-c", str(client["cpu_set"]), *cmd]
    model, url = case["model"], case["service"]["api_url"].rstrip("/")
    if case["kind"] == "accuracy":
        acc = case["accuracy"]
        cmd += ["eval", "--model", model["name"], "--api-url", url, "--api-key", "EMPTY",
                "--eval-type", "openai_api", "--datasets", *acc["datasets"],
                "--work-dir", str(out), "--eval-batch-size", str(acc.get("batch_size", 1)),
                "--generation-config", json.dumps(acc.get("generation_config", {})),
                "--dataset-args", json.dumps(acc.get("dataset_args", {}))]
        if acc.get("limit") is not None:
            cmd += ["--limit", str(acc["limit"])]
        return cmd
    w = case["workload"]
    cmd += ["perf", "--model", model["name"], "--url", url + w["endpoint"], "--api", "openai",
            "--tokenizer-path", model["tokenizer"], "--dataset", "random" if w["dataset"] == "random" else "custom",
            "--parallel", str(w["parallel"]), "--number", str(w["number"]),
            "--warmup-num", str(w["warmup"]), "--rate", str(w.get("rate", -1)),
            "--min-prompt-length", str(w["input_tokens"] if w["dataset"] == "random" else 0),
            "--max-prompt-length", str(w["input_tokens"] if w["dataset"] == "random" else w.get("max_prompt_tokens", 131072)),
            "--min-tokens", str(w["output_tokens"]), "--max-tokens", str(w["output_tokens"]),
            "--outputs-dir", str(out), "--seed", str(w.get("seed", 42)),
            "--temperature", str(w.get("temperature", 0)),
            "--extra-args", json.dumps({"ignore_eos": w.get("ignore_eos", True)})]
    if w["dataset"] != "random": cmd += ["--dataset-path", w["prepared_path"]]
    for setting, option in (("stream", "--stream"), ("tokenize_prompt", "--tokenize-prompt"), ("open_loop", "--open-loop")):
        if w.get(setting, False):
            cmd.append(option)
    return cmd


def environment_lines(context, case=None):
    lines = ["set -eo pipefail", "export PYTHONDONTWRITEBYTECODE=1", "unset PYTHONOPTIMIZE EVALSCOPE_FIXED_INTERVAL http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY"]
    for source in context.get("sources", []):
        lines.append("source " + shlex.quote(source))
    values = dict(context.get("env", {}))
    if case:
        values.update(case["service"].get("env", {}))
        values["ASCEND_RT_VISIBLE_DEVICES"] = ",".join(map(str, case["service"]["devices"]))
        values["HCCL_IF_BASE_PORT"] = str(case["service"]["hccl_port"])
    for key, value in values.items():
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key), f"Invalid environment key: {key}")
        lines.append("export " + key + "=" + shlex.quote(str(value)))
    return "\n".join(lines) + "\n"


def experiment(case, run_root):
    w = case.get("workload", {})
    code = dict(case["code"])
    if case.get("framework") == "vllm-ascend":
        code.update(identity_type="package", repo=str(ROOT), commit=code["version"],
                    packages={"vllm": code["vllm_version"], "vllm-ascend": code["version"]})
    return {"version": 1, "identity": {"task_id": case["id"], "purpose": "release data validation",
            "kind": "release_validation", "level": "smoke" if w.get("warmup") == 0 else "quick", "run_root": str(run_root)},
            "code": {"framework": case.get("framework", "xllm"), **code}, "model": case["model"],
            "service": {"tensor_parallel": len(case["service"]["devices"]),
                        "devices": case["service"]["devices"], "flags": case["service"].get("flags", {}),
                        **({"port": case["service"]["port"], "hccl_port": case["service"]["hccl_port"]}
                           if case["service"].get("ownership", "managed") == "managed" else {})},
            "workload": {"dataset": w.get("dataset", "accuracy"), "parallel": w.get("parallel", 1),
                         "number": w.get("number", 1), "warmup": w.get("warmup", 1),
                         "input_tokens": w.get("input_tokens", 1), "output_tokens": w.get("output_tokens", 1),
                         "dataset_path": w.get("prepared_path"),
                         "sampling": {k: w[k] for k in ("seed", "temperature", "ignore_eos") if k in w}},
            "evaluation": {"accuracy": case.get("accuracy", {}), "performance": {"repetitions": 1}},
            "profiling": {"enabled": False},
            "environment": {"require_npu": True, "require_clean_submodules": True, "required_tools": []},
            "artifacts": {"required": ["manifest.json", "attempts.jsonl", "result.json"]},
            "retention": {"keep_failed_attempts": True}}


METRICS = {
    "request_throughput": [("Req Throughput (req/s)", 1), ("Request throughput (req/s)", 1)],
    "output_tps": [("Output Throughput (tok/s)", 1), ("Output token throughput (tok/s)", 1)],
    "avg_latency_s": [("Avg Latency (s)", 1), ("Average latency (s)", 1)],
    "ttft_ms": [("Avg TTFT (ms)", 1), ("TTFT (ms)", 1), ("Average time to first token (s)", 1000)],
    "tpot_ms": [("Avg TPOT (ms)", 1), ("TPOT (ms)", 1), ("Average time per output token (s)", 1000)],
    "itl_ms": [("Avg ITL (ms)", 1), ("ITL (ms)", 1), ("Average inter-token latency (s)", 1000)],
    "avg_input_tokens": [("Avg Input Tokens", 1), ("Average input tokens per request", 1)],
    "avg_output_tokens": [("Avg Output Tokens", 1), ("Average output tokens per request", 1)],
    "total": [("Total Requests", 1), ("Total requests", 1)],
    "success": [("Success Requests", 1), ("Succeed requests", 1)],
    "failed": [("Failed Requests", 1), ("Failed requests", 1)],
}


def collect_measurement(case, raw):
    raw = Path(raw)
    result = {"status": "INCONCLUSIVE", "metrics": {}, "errors": [], "artifacts": []}
    if case["kind"] == "accuracy":
        reports = sorted(raw.glob(case["accuracy"]["report_glob"]))
        if not reports:
            result["errors"].append("Accuracy report missing")
            return result
        scores = {}
        for path in reports:
            node = read_json(path)
            for part in case["accuracy"]["score_pointer"].strip("/").split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                node = node[int(part)] if isinstance(node, list) else node[part]
            require(type(node) in {float, int} and math.isfinite(node), "Invalid accuracy score")
            scores[str(path.relative_to(raw))] = node
        result.update(metrics={"scores": scores}, artifacts=[str(p) for p in reports])
        minimum = case["acceptance"].get("minimum_score")
        result["status"] = "INCONCLUSIVE" if minimum is None else "PASS" if all(v >= minimum for v in scores.values()) else "FAILED"
        if minimum is None:
            result["errors"].append("minimum_score not configured; scores are observations only")
        return result
    summaries = list(raw.rglob("benchmark_summary.json"))
    if len(summaries) != 1:
        result["errors"].append(f"Expected one benchmark summary; found {len(summaries)}")
        return result
    summary = read_json(summaries[0])
    result["raw_summary"] = summary
    result["artifacts"].append(str(summaries[0]))
    for key, aliases in METRICS.items():
        values = [(summary[name], scale) for name, scale in aliases if name in summary]
        if values:
            value, scale = values[0]
            require(type(value) in {int, float} and math.isfinite(value), f"Invalid metric: {key}")
            result["metrics"][key] = value * scale
        else:
            result["errors"].append(f"Missing metric: {key}")
    percentiles = summaries[0].with_name("benchmark_percentile.json")
    if percentiles.exists():
        result["raw_percentiles"] = read_json(percentiles)
        result["artifacts"].append(str(percentiles))
    else:
        result["errors"].append("Percentile file missing")
    databases = list(raw.rglob("benchmark_data.db"))
    w, metrics = case["workload"], result["metrics"]
    if len(databases) == 1:
        with sqlite3.connect(databases[0].resolve().as_uri() + "?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT success,prompt_tokens,completion_tokens FROM result").fetchall()
        good = [(p, o) for success, p, o in rows if success]
        result["request_audit"] = {"formal_count": len(rows), "success": len(good),
                                   "strict_lengths": bool(good) and all(p == w["input_tokens"] and o == w["output_tokens"] for p, o in good)}
        result["request_audit"]["length_policy"] = w.get("length_policy", "fixed-random")
        result["request_audit"]["fixed_output_lengths"] = bool(good) and all(o == w["output_tokens"] for _, o in good)
        for label, values in (("input", [p for p, o in good]), ("output", [o for p, o in good])):
            result["request_audit"][label + "_tokens"] = {"min": min(values), "max": max(values), "mean": sum(values)/len(values)} if values else None
        if len(rows) != w["number"] or len(good) != metrics.get("success"):
            result["errors"].append("Request database and declared counts disagree")
        if w.get("strict_lengths", True) and not result["request_audit"]["strict_lengths"]:
            result["errors"].append("Request lengths do not match the declared workload")
        if w.get("strict_output_lengths", w.get("ignore_eos", True)) and not result["request_audit"]["fixed_output_lengths"]:
            result["errors"].append("Request output lengths do not match the declared output tokens")
        result["artifacts"].append(str(databases[0]))
    elif w.get("strict_lengths", True) or w.get("dataset") != "random":
        result["errors"].append("Request database required for strict length validation")
    failed = metrics.get("failed", 0) != 0 or metrics.get("success") != w["number"] or metrics.get("total") != w["number"]
    for key, limit in case["acceptance"].get("maximum", {}).items():
        if metrics.get(key) is None:
            result["errors"].append(f"Acceptance metric missing: {key}")
        elif metrics[key] > limit:
            failed = True
            result["errors"].append(f"{key} exceeds {limit}")
    result["status"] = "FAILED" if failed else "INCONCLUSIVE" if result["errors"] or w["warmup"] < 1 else "PASS"
    return result
