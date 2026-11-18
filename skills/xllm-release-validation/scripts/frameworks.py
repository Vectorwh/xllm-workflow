"""Framework-specific launch arguments and semantic comparison contracts."""
from __future__ import annotations

import json
import re

from release_common import require


def framework(case):
    return case.get("framework", "xllm")


def object_value(value):
    return json.loads(value) if isinstance(value, str) else value


def vllm_arguments(case):
    s, m = case["service"], case["model"]
    flags = {"served-model-name": m["name"], "host": "0.0.0.0", "port": s["port"],
             "tensor-parallel-size": len(s["devices"]), "dtype": m["dtype"],
             **s.get("flags", {})}
    args = ["serve", m["path"]]
    for key, value in flags.items():
        require(re.fullmatch(r"[a-z][a-z0-9-]*", key), f"Invalid vLLM flag: {key}")
        if isinstance(value, bool):
            # BooleanOptionalAction uses --no-X. enforce-eager is a store_true flag.
            if key == "enforce-eager":
                if value: args.append("--" + key)
            else:
                args.append("--" + ("" if value else "no-") + key)
        else:
            args += ["--" + key, json.dumps(value, separators=(",", ":")) if isinstance(value, (dict, list)) else str(value)]
    return args


def semantic_contract(case):
    """Derive common settings from the actual native arguments, not a second label."""
    s, flags = case["service"], case["service"].get("flags", {})
    deployment = s.get("deployment", {"kind": "colocated", "tp": len(s["devices"]), "dp": 1, "pp": 1, "ep": 1})
    require(deployment == {"kind": "colocated", "tp": len(s["devices"]), "dp": 1, "pp": 1, "ep": 1},
            "Built-in release adapters support single-host colocated TP; another deployment needs an explicit adapter")
    if framework(case) == "xllm":
        result = {"deployment": deployment, "mtp_enabled": bool(case["model"].get("draft_model")) and flags.get("num_speculative_tokens", 0) > 0,
                  "mtp_tokens": flags.get("num_speculative_tokens", 0), "graph_enabled": flags.get("enable_graph"),
                  "graph_phase": s.get("graph_phase"), "chunked_prefill": flags.get("enable_chunked_prefill"),
                  "prefix_cache": flags.get("enable_prefix_cache")}
    else:
        spec = object_value(flags.get("speculative-config", {}))
        if case.get("comparison_group") and spec.get("num_speculative_tokens", 0):
            require(spec.get("method") == "mtp" or str(spec.get("method", "")).endswith("_mtp"), "Comparison requires MTP, not another speculative algorithm")
            require(spec.get("model") in {None, case["model"]["path"]}, "An external vLLM draft model requires an additional identity adapter")
        compilation = object_value(flags.get("compilation-config", {}))
        mode = compilation.get("cudagraph_mode")
        enabled = False if flags.get("enforce-eager") is True or mode == "NONE" else True if mode else None
        phase = {"FULL_DECODE_ONLY": "decode", "PIECEWISE": "prefill+decode", "FULL": "prefill+decode", "FULL_AND_PIECEWISE": "prefill+decode"}.get(mode)
        require(flags.get("tensor-parallel-size", len(s["devices"])) == len(s["devices"]), "vLLM TP conflicts with selected devices")
        require(flags.get("data-parallel-size", 1) == 1 and flags.get("pipeline-parallel-size", 1) == 1
                and not flags.get("enable-expert-parallel", False), "This vLLM adapter supports TP-only deployment")
        result = {"deployment": deployment, "mtp_enabled": bool(spec.get("num_speculative_tokens", 0)),
                  "mtp_tokens": spec.get("num_speculative_tokens", 0), "graph_enabled": enabled,
                  "graph_phase": phase if enabled else "none", "chunked_prefill": flags.get("enable-chunked-prefill"),
                  "prefix_cache": flags.get("enable-prefix-caching")}
    if result["graph_enabled"] is False: result["graph_phase"] = "none"
    if not result["mtp_enabled"]: result["mtp_tokens"] = 0
    if case.get("comparison_group"):
        require(all(result[k] is not None for k in result), "Paired tests require explicit graph phase, graph/chunked-prefill/prefix-cache settings")
        require(result["graph_phase"] in {"none", "decode", "prefill+decode"}, "Explicit supported graph_phase is required")
        require(not result["mtp_enabled"] or result["mtp_tokens"] > 0, "Invalid MTP token count")
    return result


def comparison_key(case):
    w = case.get("workload", {})
    return (case.get("comparison_group"), case.get("kind"), case.get("repeat_index", 0),
            w.get("dataset"), w.get("parallel"), w.get("rate", -1))


def declared_differences(left, right):
    differences = []
    for key in ("dtype", "quantization", "path", "tokenizer"):
        if left["model"].get(key) != right["model"].get(key): differences.append("model." + key)
    a, b = left["service"], right["service"]
    if a["physical_devices"] != b["physical_devices"]: differences.append("physical_devices")
    for key in ("resource_pool_id",):
        if left["environment"].get(key) != right["environment"].get(key): differences.append(key)
    if semantic_contract(left) != semantic_contract(right): differences.append("semantic_contract")
    # Names/output directories may differ; request-generation settings may not.
    for key in sorted(set(left.get("workload", {})) | set(right.get("workload", {}))):
        if left.get("workload", {}).get(key) != right.get("workload", {}).get(key): differences.append("workload." + key)
    return differences


def validate_pairs(cases):
    groups = {}
    for case in cases:
        if case.get("comparison_group") and case["kind"] == "performance": groups.setdefault(comparison_key(case), []).append(case)
    for key, rows in groups.items():
        require(len(rows) == 2 and {framework(r) for r in rows} == {"xllm", "vllm-ascend"}, f"Comparison {key} must select exactly one case per framework")
        require(not declared_differences(*rows), f"Comparison {key} has key configuration differences: {declared_differences(*rows)}")


def compare_frameworks(summary):
    pairs = {}
    for row in summary["cases"]:
        if row.get("comparison_group") and row["kind"] == "performance": pairs.setdefault(comparison_key(row), []).append(row)
    comparisons = []
    for key, rows in pairs.items():
        entry = {"group": list(key), "status": "REFERENCE_ONLY", "cases": [r["case_id"] for r in rows], "differences": []}
        if len(rows) != 2 or {framework(r) for r in rows} != {"xllm", "vllm-ascend"}:
            entry["differences"].append("Incomplete framework pair")
        else:
            entry["differences"] += declared_differences(*rows)
            for row in rows:
                result = row["result"]
                identity = result.get("observed_identity", {})
                if result.get("status") != "PASS": entry["differences"].append(row["case_id"] + ": measurement not PASS")
                if not identity.get("model_weights_verified"): entry["differences"].append(row["case_id"] + ": full weight identity missing")
                if framework(row) == "xllm" and semantic_contract(row)["mtp_enabled"] and not identity.get("draft_weights_verified"):
                    entry["differences"].append(row["case_id"] + ": draft identity missing")
                if result.get("activation", {}).get("status") != "PASS": entry["differences"].append(row["case_id"] + ": activation evidence missing")
            for field in ("base_model_sha256", "model_weight_sha256", "device_mapping", "hardware_identity", "workload_identity"):
                identities = [r["result"].get("observed_identity", {}) for r in rows]
                if not identities[0].get(field) or identities[0].get(field) != identities[1].get(field): entry["differences"].append(field)
            if rows[0]["result"].get("client_identity") != rows[1]["result"].get("client_identity"): entry["differences"].append("EvalScope client identity")
            entry["tuning"] = {framework(r): r["service"].get("flags", {}) for r in rows}
            if not entry["differences"]:
                entry["status"] = "COMPARABLE_MEASUREMENT"
                x = next(r for r in rows if framework(r) == "xllm")["result"]["metrics"]
                v = next(r for r in rows if framework(r) == "vllm-ascend")["result"]["metrics"]
                entry["delta_pct"] = {k: (value - v[k]) / v[k] * 100 for k, value in x.items()
                                      if type(value) in {int, float} and type(v.get(k)) in {int, float} and v[k] != 0}
        comparisons.append(entry)
    return comparisons
