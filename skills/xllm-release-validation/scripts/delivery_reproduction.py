"""Fresh-environment delivery YAML and its explicit workflow replay entry.

Native xTask does not execute the release_reproduction preparation extension.
release.py reproduce consumes it, prepares a fresh container/checkout/build,
then calls the unchanged xTask adapter with the resulting new artifacts.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import subprocess

import yaml

from frameworks import framework
from release_common import ROOT, digest, require

ROOT_TOKEN = "@@RUN_ROOT@@"
CONTAINER_TOKEN = "release-reproduction-container"


def replace_tree(value, substitutions):
    if isinstance(value, dict): return {replace_tree(k, substitutions): replace_tree(v, substitutions) for k, v in value.items()}
    if isinstance(value, list): return [replace_tree(v, substitutions) for v in value]
    if isinstance(value, str):
        for before, after in sorted(substitutions.items(), key=lambda pair: -len(pair[0])):
            if before: value = value.replace(before, after)
    return value


def relative_path(value, label):
    require(isinstance(value, str) and value and not Path(value).is_absolute() and ".." not in Path(value).parts,
            f"{label} must be a path relative to the fresh checkout")
    return value


def fresh_spec(row, release):
    profile = deepcopy(row.get("delivery", {}).get("xtask_reproduction", {}))
    require(profile.get("image"), "xTask delivery requires delivery.xtask_reproduction.image; the internal reused container is not a delivery prerequisite")
    require("REPLACE_" not in profile["image"], "Replace the example delivery image with the tested image")
    require(row.get("xtask", {}).get("commit") and re.fullmatch(r"[0-9a-f]{40}", row["xtask"]["commit"]) and row["xtask"]["commit"] != "0" * 40, "Pin the xTask commit for delivery")
    require(re.fullmatch(r"[0-9a-f]{40}", release.get("commit", "")) and release["commit"] != "0" * 40, "Pin the release commit for delivery")
    c = deepcopy({k: row[k] for k in ("model", "service", "workload", "accuracy", "code", "environment", "xtask", "acceptance", "timeout") if k in row})
    c.update(id="reproduction-case", framework=framework(row), mode="xtask", kind=row["kind"], max_attempts=1)
    c["xtask"].update(repo=ROOT_TOKEN + "/tools/xTask")
    environment = c["environment"]
    # Host identity/SSH and the workflow location are supplied on the target machine.
    for context in ("host", "service", "client"):
        environment.setdefault(context, {}).pop("ssh_host", None)
    environment["shared_filesystem"] = True
    environment["resource_pool_id"] = "release-reproduction-local-host"
    environment["lease_dir"] = ROOT_TOKEN + "/device-leases"
    previous_container = environment["service"].get("container")
    environment["service"]["container"] = CONTAINER_TOKEN
    environment["host"]["python"] = profile.get("host_python", "python3")
    environment["service"]["python"] = profile.get("python", "python3")
    if "sources" in profile: environment["service"]["sources"] = deepcopy(profile["sources"])
    client = environment["client"]
    if client.get("container"):
        require(client["container"] == previous_container, "Delivery needs an explicit fresh client recipe for a separate benchmark container")
        client["container"] = CONTAINER_TOKEN
    client["python"] = profile.get("python", "python3") if client.get("container") else profile.get("host_python", "python3")
    c["xtask"]["context"] = {k: v for k, v in c["xtask"].get("context", {}).items() if k != "ssh_host"}
    c["xtask"]["context"]["python"] = profile.get("host_python", "python3")
    c["service"].update(ownership="managed", restart_policy="per_case")
    c["service"].pop("external_pid_file", None)
    # Reproduction occurs locally, so the API/instance host must not point to the old host.
    from urllib.parse import urlparse
    for field in ("api_url", "ready_api_url"):
        if c["service"].get(field):
            old = urlparse(c["service"][field]); c["service"][field] = f"http://127.0.0.1:{old.port or c['service']['port']}{old.path}"
    c["xtask"]["instance_host"] = "127.0.0.1"
    sources = [{"url": profile.get("xtask_url", "https://github.com/Vectorwh/xTask.git"),
                "commit": c["xtask"]["commit"], "repo": c["xtask"]["repo"], "context": environment["host"]}]
    container = deepcopy(profile.get("container", {}))
    require(not any(k in container for k in ("reference", "name", "mode", "image")), "Delivery container cannot reference an existing container; image/name/mode are generated")
    container.update(name=CONTAINER_TOKEN, mode="create", image=profile["image"], pull=profile.get("pull", True))
    # New roots are the only build/workspace mounts added by the interpreter.
    container["binds"] = list(container.get("binds", []))
    preparation = {"containers": [container], "sources": sources, "setup": deepcopy(profile.get("setup", [])),
                   "builds": [], "mtp": [], "resources": deepcopy(profile.get("resources", []))}
    substitutions = {}
    if framework(row) == "xllm":
        require(profile.get("source_url"), "xTask xLLM delivery requires a source_url")
        build = profile.get("build", {})
        require(build.get("gate_args"), "xTask delivery requires explicit build gate_args from the tested checkout")
        repo = ROOT_TOKEN + "/source/xllm"
        binary = repo + "/" + relative_path(build.get("binary"), "build.binary")
        old_repo, old_binary = c["code"].get("repo"), c["code"].get("binary")
        substitutions.update({old_binary: binary, old_repo: repo, "@@REPO@@": repo})
        if old_binary: substitutions[str(Path(old_binary).parent)] = str(Path(binary).parent)
        c = replace_tree(c, {k: v for k, v in substitutions.items() if k})
        c["code"] = {"repo": repo, "commit": release["commit"], "binary": binary, "allow_dirty": build.get("allow_dirty", False),
                     "build_verdict": ROOT_TOKEN + "/build-record/build/verdict.json",
                     "binary_provenance": ROOT_TOKEN + "/build-record/build/binary-provenance.json"}
        sources.append({"url": profile["source_url"], "commit": release["commit"], "repo": repo, "context": c["environment"]["service"]})
        preparation["builds"].append({"repo": repo, "build_dir": repo + "/" + relative_path(build.get("build_dir", "build"), "build.build_dir"),
            "binary": binary, "run_root": ROOT_TOKEN + "/build-record", "gate_args": build["gate_args"], "context": c["environment"]["service"]})
        old_draft = row["model"].get("draft_model")
        if old_draft:
            require(profile.get("mtp", {}).get("model_type"), "MTP delivery requires the pinned export_mtp.py model_type")
            draft = ROOT_TOKEN + "/models/mtp-draft"
            c["model"]["draft_model"] = draft
            c["model"]["sha256"] = {p: sha for p, sha in c["model"].get("sha256", {}).items() if not p.startswith(old_draft.rstrip("/") + "/")}
            c = replace_tree(c, {old_draft: draft})
            preparation["mtp"].append({"repo": repo, "commit": release["commit"], "input_dir": c["model"]["path"],
                                      "output_dir": draft, "model_type": profile["mtp"]["model_type"], "context": c["environment"]["service"]})
        c["xtask"]["service_mode"] = "native"
    else:
        require(profile["image"] == c["code"]["image"], "vLLM delivery image must match the measured version's declared image")
        c["code"]["image"] = profile["image"]
        c["xtask"]["service_mode"] = "external"
    # Do not carry the original run's output data path into the fresh benchmark.
    w = c.get("workload", {})
    if w.get("dataset") in {"gsm8k", "sharegpt"}: w["prepared_path"] = ROOT_TOKEN + "/datasets/" + w["dataset"] + ".txt"
    preparation = replace_tree(preparation, {k: v for k, v in substitutions.items() if k})
    # JSONs are runtime dependencies, but their effective settings travel in this YAML.
    c["xtask"]["exact_config"] = True
    c["xtask"]["task_config"] = deepcopy(profile.get("task_config", {"ENV_CONFIG": {"CLEAN_RESULTS": False, "DEPENDENCE_PACKAGE_PATH": "", "XTask_LOG_LEVEL": "INFO"}, "DOCKER_CONFIG": {"volumes": {}, "enable_proxy": False}}))
    image_home, _, image_name = profile["image"].rpartition(":")
    require(image_home and image_name, "Delivery image needs an explicit tag or digest")
    engine_defaults = {"image_home": image_home, "default_image_name": image_name, "enable_proxy": False,
                       "benchmark_timeout": row.get("timeout", 7200)}
    c["xtask"]["xllm_config"] = deepcopy(profile.get("xllm_config", {"XLLM_CONFIG": engine_defaults}))
    c["xtask"].pop("vllm_config", None)
    if framework(row) == "vllm-ascend": c["xtask"]["vllm_config"] = deepcopy(profile.get("vllm_config", {}))
    c["delivery"] = {"xtask_reproduction": profile}
    return {"schema_version": 1, "release": {k: release[k] for k in ("version", "commit")}, "environment": c.pop("environment"),
            "preparation": preparation, "execution": {"default_mode": "xtask"}, "cases": [c]}


def generate(row, release):
    from xtask_adapter import task_config
    from release_common import expand
    spec = fresh_spec(row, release)
    expand(spec)  # Reject a derived case that cannot be materialized by plan.
    c = deepcopy(spec["cases"][0]); c["environment"] = spec["environment"]
    task = task_config(c)
    if task.get("deployment"):
        for instance in task["deployment"]["instances"]: instance["reuse_existing_container"] = False
    task["release_reproduction"] = {"schema_version": 1, "entrypoint": "release.py reproduce --yaml CASE.yaml --run-root NEW_SHARED_ROOT",
        "source_case_id": row["case_id"], "validation": "configuration-derived; independent-hardware-replay-pending", "spec": spec}
    task["release_reproduction"]["workflow"] = {
        "repository": subprocess.check_output(["git", "-C", str(ROOT), "remote", "get-url", "origin"], text=True).strip(),
        "commit": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()}
    task["release_reproduction"]["configuration_sha256"] = digest(task)
    return task


def replay_spec(task, root):
    """Pure validation/materialization; performs no Docker, Git or NPU operation."""
    root = Path(root).resolve()
    extension = task.get("release_reproduction", {})
    require(extension.get("schema_version") == 1, "Delivery YAML has no supported release_reproduction block")
    candidate = deepcopy(task); candidate["release_reproduction"].pop("configuration_sha256", None)
    require(extension.get("configuration_sha256") == digest(candidate), "Delivery YAML configuration changed; regenerate rather than diverging native and preparation settings")
    require(not root.exists() or not any(root.iterdir()), "Reproduction requires a new empty run root")
    container = "xllm-repro-" + digest(str(root))[:12]
    original_profiles = [deepcopy(c.get("delivery", {})) for c in extension["spec"]["cases"]]
    spec = replace_tree(extension["spec"], {ROOT_TOKEN: str(root), CONTAINER_TOKEN: container, "@@WORKFLOW@@": str(ROOT)})
    for case, profile in zip(spec["cases"], original_profiles): case["delivery"] = profile
    require(all(r.get("mode") == "create" and not r.get("reference") for r in spec["preparation"]["containers"]), "Delivery must create independent containers")
    for item in spec["preparation"]["containers"]:
        item["binds"] += [str(root) + ":" + str(root), str(ROOT) + ":" + str(ROOT)]
    spec["environment"]["shared_filesystem"] = True
    import socket
    spec["environment"]["resource_pool_id"] = "host:" + socket.gethostname()
    spec["environment"]["lease_dir"] = str(root.parent / ".release-device-leases")
    return spec


def reproduce(yaml_path, root, *, plan_only=False):
    from release import create_plan, execute, report
    from release_prepare import prepare_campaign
    task = yaml.safe_load(Path(yaml_path).read_text())
    spec = replay_spec(task, root)
    root = Path(root).resolve()
    source = root / "reproduction-spec.yaml"
    plan = create_plan(source, root, source_config=spec)
    source.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
    (root / "delivery-input.yaml").write_text(Path(yaml_path).read_text())
    if not plan_only:
        prepare_campaign(root, plan)
        execute(root)
        report(root)
    return {"run_root": str(root), "cases": len(plan["cases"]), "executed": not plan_only}
