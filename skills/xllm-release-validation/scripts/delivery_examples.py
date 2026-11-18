"""Representative delivery content: xTask YAML only, direct archived scripts."""
from __future__ import annotations

import json
import re
from pathlib import Path

from frameworks import framework
from release_common import require
from delivery_native import generate, yaml_text, instructions, settings, replace_tree


def choose(summary, selected=None):
    rows = summary["cases"]
    if selected:
        require(len(set(selected)) == len(selected), "Duplicate representative case IDs")
        lookup = {r["case_id"]: r for r in rows}
        require(all(k in lookup for k in selected), "Representative case ID not found")
        chosen = [lookup[k] for k in selected]
        groups = lambda rows: [(framework(r), r["mode"], r["kind"]) for r in rows]
        require(set(groups(chosen)) == set(groups(rows)) and len(groups(chosen)) == len(set(groups(chosen))), "Select one representative for each framework/mode/kind group")
        return chosen
    groups = {}
    for row in rows: groups.setdefault((framework(row), row["mode"], row["kind"]), []).append(row)
    result = []
    for group in groups.values():
        # Stable configuration preference; never select the fastest retry/result.
        result.append(min(enumerate(group), key=lambda item: (
            str(item[1]["model"].get("quantization", "none")).lower() not in {"none", "bf16"},
            item[1].get("workload", {}).get("parallel", 1) != 1, item[0]))[1])
    return result


def read_archived(path, incomplete):
    if path.is_file(): return path.read_text()
    require(incomplete, "Representative execution artifact missing: " + str(path))
    return "待补充：尚无本轮实际执行材料"


def examples(summary, root, selected=None, *, incomplete=False):
    root = Path(root)
    records, variants = [], []
    representatives = choose(summary, selected)
    delivery_cases = {}
    for row in summary["cases"]:
        if row["mode"] != "xtask": continue
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", row["case_id"]), "Unsafe delivery case ID")
        try:
            task = generate(row, summary["release"])
        except ValueError:
            if not incomplete: raise
            delivery_cases[row["case_id"]] = "待补充：新建环境交付配置不完整，不能使用内部复用用例替代"
            continue
        text = yaml_text(row, summary["release"], task)
        destination = root / "delivery/cases" / (row["case_id"] + ".yaml")
        destination.parent.mkdir(parents=True, exist_ok=True)
        materials = {destination: text, destination.with_suffix(".md"): instructions(row, summary["release"], task)}
        if row.get("workload", {}).get("dataset") in {"gsm8k", "sharegpt"}:
            prompts = Path(row["workload"]["prepared_path"])
            if not prompts.is_file():
                require(incomplete, "Frozen prompts missing for native delivery: " + str(prompts))
                delivery_cases[row["case_id"]] = "待补充：原生用例所需冻结数据尚未归档"
                continue
            materials[destination.with_suffix(".prompts.txt")] = prompts.read_text()
        for path, content in materials.items():
            if path.exists(): require(path.read_text() == content, "Delivery changed; preserve the archive and use a new campaign/delivery root")
            else: path.write_text(content)
        delivery_cases[row["case_id"]] = text
    for row in representatives:
        attempt = root / row["result"].get("attempt", "__not_executed__")
        record = {"case_id": row["case_id"], "framework": framework(row), "mode": row["mode"], "kind": row["kind"], "blocks": []}
        if row["mode"] == "xtask":
            record["blocks"].append({"label": "原生 xTask YAML 测试用例", "language": "yaml", "text": delivery_cases[row["case_id"]]})
            preparation = "获取固定版本 xTask 和 xLLM，使用 xTask 原生构建工具生成 wheel" if framework(row) == "xllm" else "获取固定版本 xTask，按指定 vLLM-Ascend 镜像新建服务"
            record["instructions"] = f"原生准备与测试说明：delivery/cases/{row['case_id']}.md。{preparation}，再用 main.py --case_name 执行此用例；接收方无需 workflow、发版 skill 或 release.py。镜像与代码版本见 YAML 注释，JSON 镜像/挂载配置及完整命令见配套说明。"
        else:
            reference = attempt / "service-reference.json"
            service = Path(json.loads(reference.read_text())["directory"]) if reference.is_file() else attempt / "service"
            record["blocks"].append({"label": "服务启动脚本", "language": "bash", "text": read_archived(service / "start.sh", incomplete)})
            record["blocks"].append({"label": "EvalScope 精度测试脚本" if row["kind"] == "accuracy" else "EvalScope 性能测试脚本",
                "language": "bash", "text": read_archived(attempt / "eval.sh", incomplete)})
        records.append(record)
    def flatten(value, prefix=""):
        out = {}
        for k, v in value.items():
            path = prefix + k
            if k == "sha256" and isinstance(v, dict):
                from release_common import digest
                out[path] = {"files": len(v), "manifest_sha256": digest(v)}
                continue
            if isinstance(v, dict): out.update(flatten(v, path + "."))
            elif isinstance(v, list): out.update(flatten({str(i): item for i, item in enumerate(v)}, path + "."))
            else: out[path] = v
        return out
    for row in summary["cases"]:
        reference = next((r for r in representatives if (framework(r), r["mode"], r["kind"]) == (framework(row), row["mode"], row["kind"])), None)
        if reference is None or reference["case_id"] == row["case_id"]: continue
        fields = ("model", "service", "workload", "accuracy", "code", "environment")
        if row["mode"] == "xtask":
            try:
                a = replace_tree(generate(reference, summary["release"]), {settings(reference, summary["release"])["workspace"]: "本轮xTask工作目录"})
                b = replace_tree(generate(row, summary["release"]), {settings(row, summary["release"])["workspace"]: "本轮xTask工作目录"})
                reference = {"case_id": reference["case_id"], "native": a}
                fresh = {"native": b}
                reference["native"].pop("case_name", None)
                fresh["native"].pop("case_name", None)
                fields = ("native",)
            except ValueError:
                if not incomplete: raise
                variants.append({"case": row["case_id"], "reference": reference["case_id"], "changes": "待补齐新建环境配置后生成差异"})
                continue
        else: fresh = row
        old, new = flatten({k: reference.get(k, {}) for k in fields}), flatten({k: fresh.get(k, {}) for k in fields})
        changed = {k: {"representative": old.get(k), "case": new.get(k)} for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)}
        variants.append({"case": row["case_id"], "reference": reference["case_id"], "changes": json.dumps(changed, ensure_ascii=False, sort_keys=True) if changed else "配置相同；独立重复轮次"})
    return records, variants


def render(records, *, markdown=True):
    lines = []
    for record in records:
        title = f"{record['case_id']} | {record['framework']} | {record['mode']} | {record['kind']}"
        lines += [("### " if markdown else "") + title, ""]
        for block in record["blocks"]:
            lines.append(block["label"])
            if markdown: lines.append("```" + block["language"])
            lines.append(block["text"].rstrip())
            if markdown: lines.append("```")
            lines.append("")
        if record.get("instructions"): lines += [record["instructions"], ""]
    return "\n".join(lines)
