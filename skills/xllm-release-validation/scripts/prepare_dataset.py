#!/usr/bin/env python3
"""Freeze GSM8K/ShareGPT single-turn prompts for EvalScope's custom plugin.

The plugin consumes one raw prompt per line, NOT a JSON object per line.
Both frameworks consume this same immutable file and sampling order.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import re
import urllib.request

from release_common import digest, file_hash, read_json, require, write_json


def prompts(source, dataset, turn="first_user"):
    data = json.loads(source.read_text()) if source.suffix.lower() == ".json" else [json.loads(s) for s in source.read_text().splitlines() if s.strip()]
    require(isinstance(data, list), "Dataset source must be a JSON array or JSONL mappings")
    require(turn in {"first_user", "last_user"}, "ShareGPT turn must be first_user or last_user")
    for index, row in enumerate(data):
        if dataset == "gsm8k":
            value = row.get("question")
        else:
            messages = row.get("conversations", row.get("messages", []))
            users = [m.get("value", m.get("content")) for m in messages if m.get("from", m.get("role")) in {"human", "user"}]
            value = users[0 if turn == "first_user" else -1] if users else None
        if isinstance(value, str) and value.strip():
            # custom is line-oriented: normalize embedded whitespace explicitly.
            yield index, re.sub(r"\s+", " ", value).strip()


def prepare(workload, tokenizer_path, *, tokenizer=None):
    source, target = Path(workload["dataset_path"]), Path(workload["prepared_path"])
    manifest_path = target.with_name(target.name + ".manifest.json")
    config = {"workload": {k: v for k, v in workload.items() if k not in {"parallel", "rate", "open_loop", "endpoint", "temperature", "stream", "ignore_eos", "output_tokens", "strict_lengths", "strict_output_lengths", "tokenize_prompt"}}, "tokenizer": tokenizer_path}
    if not source.exists():
        require(workload.get("source_url") and workload.get("source_sha256"), "Missing source: provide source_url and source_sha256, or prepare the local pinned dataset")
        require(not source.parent.exists() or source.parent.is_dir(), "Invalid source directory")
        source.parent.mkdir(parents=True, exist_ok=True)
        temporary = source.with_name(source.name + ".download")
        require(not temporary.exists(), "Previous partial download exists; inspect it before retrying")
        with urllib.request.urlopen(workload["source_url"], timeout=120) as response, temporary.open("xb") as output:
            while block := response.read(8 * 1024 * 1024): output.write(block)
        require(file_hash(temporary) == workload["source_sha256"], "Downloaded dataset hash differs")
        temporary.replace(source)
    source_sha = file_hash(source)
    if workload.get("source_sha256"): require(source_sha == workload["source_sha256"], "Dataset source hash differs")
    identity = digest({**config, "source_sha256": source_sha})
    if target.exists() or manifest_path.exists():
        require(target.is_file() and manifest_path.is_file(), "Incomplete prepared dataset exists; inspect instead of overwriting")
        old = read_json(manifest_path)
        require(old["identity"] == identity and old["prepared_sha256"] == file_hash(target), "Prepared dataset identity changed; use a new path")
        return old
    items = list(prompts(source, workload["dataset"], workload.get("sharegpt_turn", "first_user")))
    rng = random.Random(workload.get("seed", 42))
    if workload.get("shuffle", True): rng.shuffle(items)
    policy = workload.get("length_policy", "native")
    if policy != "native":
        if tokenizer is None:
            from transformers import AutoTokenizer
            tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, trust_remote_code=False, local_files_only=True)
        transformed = []
        for index, prompt in items:
            tokens = tokenizer.encode(prompt, add_special_tokens=False)
            if policy == "filter" and not workload.get("min_prompt_tokens", 0) <= len(tokens) <= workload.get("max_prompt_tokens", 131072): continue
            if policy == "truncate": prompt = tokenizer.decode(tokens[:workload["input_tokens"]], skip_special_tokens=False)
            transformed.append((index, re.sub(r"\s+", " ", prompt).strip()))
        items = transformed
    count = workload["number"] + workload.get("warmup", 0)
    require(items, "No valid dataset prompts remain")
    require(len(items) >= count or workload.get("allow_repeat", False), "Insufficient samples; reduce number or explicitly allow_repeat")
    selected = [items[i % len(items)] for i in range(count)]
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x") as output:
        for _, prompt in selected: output.write(prompt + "\n")
    manifest = {"identity": identity, "dataset": workload["dataset"], "revision": workload["dataset_revision"],
                "source_sha256": source_sha, "prepared_sha256": file_hash(target), "format": "evalscope-custom-raw-lines",
                "sample_indices": [i for i, _ in selected], "length_policy": policy,
                "sharegpt_turn": workload.get("sharegpt_turn", "first_user"), "whitespace": "collapse",
                "count": count, "repeated": len(items) < count, "tokenizer": tokenizer_path}
    write_json(manifest_path, manifest)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, type=Path)
    args = parser.parse_args()
    case = read_json(args.case)
    print(json.dumps(prepare(case["workload"], case["model"]["tokenizer"]), ensure_ascii=False))
