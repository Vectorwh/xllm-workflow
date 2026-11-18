"""Read-only package identity for the vLLM-Ascend release adapter."""
import ctypes
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess

from release_common import digest, file_hash, read_json, require
from frameworks import vllm_arguments


def workload_identity(case):
    w = case.get("workload", {})
    if case["kind"] != "performance": return {"dataset_revision": case["accuracy"]["dataset_revision"]}
    if w["dataset"] == "random": return {"settings": digest(w)}
    target = Path(w["prepared_path"])
    manifest = read_json(target.with_name(target.name + ".manifest.json"))
    require(manifest["prepared_sha256"] == file_hash(target), "Prepared dataset changed")
    return {"prepared_sha256": manifest["prepared_sha256"], "source_sha256": manifest["source_sha256"], "sample_indices": manifest["sample_indices"]}


def common_identity(case):
    model = case["model"]
    weights, metadata = {}, {}
    for p in Path(model["path"]).glob("*.safetensors"):
        expected = model.get("sha256", {}).get(str(p))
        if expected:
            require(file_hash(p) == expected, "Model weight differs: " + str(p)); weights[p.name] = expected
    for p in Path(model["path"]).glob("*.bin"):
        expected = model.get("sha256", {}).get(str(p))
        if expected:
            require(file_hash(p) == expected, "Model weight differs: " + str(p)); weights[p.name] = expected
    for label, folder in (("model", model["path"]), ("tokenizer", model["tokenizer"])):
        require(Path(folder).is_dir(), "Missing " + label + " directory")
        for name in ("config.json", "tokenizer.json", "tokenizer_config.json", "quant_model_description.json"):
            path = Path(folder) / name
            if path.is_file(): metadata[label + "/" + name] = file_hash(path)
    count = len(list(Path(model["path"]).glob("*.safetensors"))) + len(list(Path(model["path"]).glob("*.bin")))
    draft_verified = False
    if model.get("draft_model"):
        draft = Path(model["draft_model"])
        files = list(draft.glob("*.safetensors")) + list(draft.glob("*.bin"))
        draft_verified = bool(files) and all(model.get("sha256", {}).get(str(p)) and file_hash(p) == model["sha256"][str(p)] for p in files)
        marker = draft / "release-mtp-manifest.json"
        if not draft_verified and marker.is_file():
            saved = read_json(marker)
            source_weights = saved.get("source", {}).get("source_weights", {})
            draft_verified = (bool(files) and saved.get("source", {}).get("config", {}).get("commit") == case["code"].get("commit")
                and source_weights == {str(Path(model["path"]) / name): sha for name, sha in weights.items()}
                and all(saved.get("files", {}).get(p.name) == file_hash(p) for p in files)
                and saved.get("files", {}).get("config.json") == file_hash(draft / "config.json"))
    return {"base_model_sha256": metadata, "model_weight_sha256": weights,
            "model_weights_verified": bool(count) and len(weights) == count,
            "draft_weights_verified": draft_verified,
            "workload_identity": workload_identity(case)}


def observe_vllm(case):
    code, service = case["code"], case["service"]
    packages = {}
    for name in ("vllm", "vllm-ascend", "torch", "torch-npu", "transformers", "tokenizers"):
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = None
    require(packages["vllm"] == code["vllm_version"] and packages["vllm-ascend"] == code["version"], "Installed vLLM/vLLM-Ascend versions differ")
    for key, expected in case["environment"].get("expected_packages", {}).items(): require(packages.get(key) == expected, "Package differs: " + key)
    binary = Path(code["binary"])
    require(binary.is_file() and os.access(binary, os.X_OK), "vLLM executable missing")
    version_result = subprocess.run([str(binary), "--version"], text=True, capture_output=True, timeout=120)
    cli_version = version_result.stdout + version_result.stderr
    require(version_result.returncode == 0 and re.search(r"(?<![\w.+-])v?" + re.escape(code["vllm_version"]) + r"(?![\w.+-])", cli_version), "vLLM executable version differs from the declared Python package")
    command = [str(binary), "serve", "--help=all"]
    help_result = subprocess.run(command, text=True, capture_output=True, timeout=120)
    if help_result.returncode:
        help_result = subprocess.run([str(binary), "serve", "--help"], text=True, capture_output=True, timeout=120)
    help_text = help_result.stdout + help_result.stderr
    require(help_result.returncode == 0, "vLLM CLI help failed")
    for flag in vllm_arguments(case):
        if flag.startswith("--"):
            canonical = flag.replace("--no-", "--", 1)
            require(canonical in help_text, "Unsupported vLLM flag: " + flag)
    if service.get("ownership") == "external":
        # External services require independent runtime evidence; do not claim a supplied endpoint is this package.
        require(False, "vLLM external ownership is not verified; use workflow-managed serving with xTask external benchmarking")
    mapping = []
    if service.get("verify_acl_mapping", True):
        ascend = os.environ.get("ASCEND_HOME_PATH") or os.environ.get("ASCEND_TOOLKIT_HOME")
        require(ascend, "CANN environment not loaded")
        acl = ctypes.CDLL(str(Path(ascend) / "lib64/libascendcl.so"))
        require(acl.aclInit(None) == 0, "aclInit failed")
        try:
            for user, expected in enumerate(service["physical_devices"]):
                logical, physical = ctypes.c_int32(-1), ctypes.c_int32(-1)
                require(acl.aclrtGetLogicDevIdByUserDevId(user, ctypes.byref(logical)) == 0, "ACL user mapping failed")
                require(acl.aclrtGetPhyDevIdByLogicDevId(logical.value, ctypes.byref(physical)) == 0, "ACL physical mapping failed")
                require(physical.value == expected, "Physical device mapping differs")
                mapping.append({"user": user, "logic": logical.value, "physical": physical.value})
        finally: acl.aclFinalize()
    common = common_identity(case)
    return {"framework": "vllm-ascend", "commit": code["version"], "packages": packages,
            "binary": str(binary), "binary_sha256": file_hash(binary), "cli_version": cli_version, "flags_help": help_text,
            "device_mapping": mapping, "model_metadata_sha256": common["base_model_sha256"], **common}
