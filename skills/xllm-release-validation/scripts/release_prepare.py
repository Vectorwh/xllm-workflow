"""Prepare declared containers, checkouts, builds and MTP weights, with a ledger."""
from __future__ import annotations

from copy import deepcopy
import fcntl
import json
from pathlib import Path
import re
import shlex
import subprocess

from release_common import ROOT, digest, file_hash, merge, read_json, require, write_json
from release_runtime import python_script, run


def container_request(config, image, reference=None):
    """Docker Engine create payload; preserve a reference's runtime constraints."""
    body = deepcopy((reference or {}).get("Config", {}))
    body = {k: v for k, v in body.items() if k in {"User", "Env", "WorkingDir", "Volumes", "ExposedPorts", "Labels", "StopSignal", "Healthcheck"}}
    body.update(Image=image, Hostname="", Entrypoint=[], Cmd=config.get("command", ["sleep", "infinity"]))
    body["HostConfig"] = deepcopy((reference or {}).get("HostConfig", {}))
    host = body["HostConfig"]
    host["AutoRemove"] = False
    host["ContainerIDFile"] = ""
    # Port bindings cannot be inherited silently: the reference may still own them.
    require(not host.get("PortBindings") or "PortBindings" in config.get("host_config", {}), "Reference publishes ports; explicitly provide new PortBindings")
    host.update(deepcopy(config.get("host_config", {})))
    binds = {s.split(":")[1]: s for s in host.get("Binds") or []}
    for bind in config.get("binds", []):
        require(len(bind.split(":")) in {2, 3}, "Bind must be host:container[:mode]")
        binds[bind.split(":")[1]] = bind
    host["Binds"] = list(binds.values())
    env = {s.split("=", 1)[0]: s.split("=", 1)[1] for s in body.get("Env") or []}
    env.update({k: str(v) for k, v in config.get("env", {}).items()})
    body["Env"] = [k + "=" + v for k, v in env.items()]
    body["Labels"] = body.get("Labels") or {}
    body["Labels"]["xllm-release.recipe"] = digest(config)
    return body


def prepare_container(config, output):
    import http.client
    import os
    import socket
    import urllib.parse
    output = Path(output)
    def capture(argv): return subprocess.check_output(argv, text=True).strip()
    def inspect(name):
        result = subprocess.run(["docker", "inspect", name], text=True, capture_output=True)
        return json.loads(result.stdout)[0] if result.returncode == 0 else None
    existing = inspect(config["name"])
    if config.get("mode", "create") == "existing":
        require(existing and existing["State"]["Running"], "Existing container is missing or stopped")
    else:
        require(config.get("mode", "create") == "create", "Container mode must be create or existing")
        if existing:
            require(existing["Config"].get("Labels", {}).get("xllm-release.recipe") == digest(config), "Container name belongs to a different recipe; choose another name")
        else:
            reference = inspect(config["reference"]) if config.get("reference") else None
            require(not config.get("reference") or reference, "Reference container missing")
            image = config.get("image") or (reference or {}).get("Image")
            require(image, "Container image or reference is required")
            if config.get("pull", False): subprocess.run(["docker", "pull", image], check=True)
            image_info = json.loads(capture(["docker", "image", "inspect", image]))[0]
            # Image defaults supply user/env, while the recipe controls the idle command.
            request = container_request(config, image_info["Id"], reference or image_info)
            write_json(output / "create-request.json", request)
            docker_host = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
            require(docker_host.startswith("unix://"), "Container preparation requires a local Docker Unix socket on the configured host")
            class UnixConnection(http.client.HTTPConnection):
                def connect(self):
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(docker_host[7:])
            connection = UnixConnection("localhost", timeout=120)
            connection.request("POST", "/containers/create?name=" + urllib.parse.quote(config["name"], safe=""),
                               json.dumps(request), {"Content-Type": "application/json"})
            response = connection.getresponse(); result = response.read().decode(); connection.close()
            require(response.status == 201, "Docker create failed: " + result)
        subprocess.run(["docker", "start", config["name"]], check=True)
        existing = inspect(config["name"])
    if config.get("image"):
        requested = json.loads(capture(["docker", "image", "inspect", config["image"]]))[0]
        require(existing["Image"] == requested["Id"], "Container image differs from declared image")
    write_json(output / "container-inspect.json", existing)
    image_info = json.loads(capture(["docker", "image", "inspect", existing["Image"]]))[0]
    write_json(output / "image-inspect.json", image_info)
    return {"name": config["name"], "image_id": existing["Image"], "repo_digests": image_info.get("RepoDigests", []), "reference": config.get("reference")}


def prepare_source(config, output):
    repo = Path(config["repo"])
    require(re.fullmatch(r"[0-9a-f]{40}", config["commit"]), "Source commit must be a full SHA")
    if not repo.exists():
        subprocess.run(["git", "clone", "--no-checkout", config["url"], str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach", config["commit"]], check=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    require(commit == config["commit"], "Existing checkout differs; use an independent directory instead of resetting it")
    require(not subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip(), "Source checkout is dirty")
    subprocess.run(["git", "-C", str(repo), "submodule", "update", "--init", "--recursive"], check=True)
    return {"repo": str(repo), "commit": commit}


def prepare_build(config, output):
    root = Path(config["run_root"])
    require(not (root / "build/build.log").exists(), "Build output exists; use a fresh build run_root to preserve prior logs")
    command = [config.get("python", "python3"), str(ROOT / "skills/xllm-npu-build-gate/scripts/build_gate.py"),
               "--framework", "xllm", "--repo", config["repo"], "--run-root", str(root),
               "--build-dir", config["build_dir"], "--binary", config["binary"], "--execute", "--require-npu"]
    command += config.get("gate_args", [])
    write_json(Path(output) / "build-command.json", command)
    subprocess.run(command, check=True)
    verdict = read_json(root / "build/verdict.json")
    require(verdict.get("status") == "PASS" and verdict.get("binary_ready"), "Build gate did not produce a ready binary")
    return {"verdict": str(root / "build/verdict.json"), "provenance": str(root / "build/binary-provenance.json")}


def prepare_mtp(config, output):
    source, target, repo = Path(config["input_dir"]), Path(config["output_dir"]), Path(config["repo"])
    tool = repo / "tools/export_mtp.py"
    require(tool.is_file(), "Pinned xLLM checkout has no tools/export_mtp.py")
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    require(commit == config["commit"], "MTP exporter checkout differs from the release commit")
    identity = {"config": config, "tool_sha256": file_hash(tool), "source_config_sha256": file_hash(source / "config.json"),
                "source_weights": {str(p): file_hash(p) for p in sorted(source.glob("*.safetensors"))}}
    require(identity["source_weights"], "No source safetensors for MTP export")
    marker = target / "release-mtp-manifest.json"
    if target.exists():
        require(marker.exists(), "Existing draft has no provenance; use a new output path")
        old = read_json(marker)
        require(old["identity"] == digest(identity), "Draft provenance differs; do not overwrite")
        require(all((target / name).is_file() and file_hash(target / name) == sha for name, sha in old["files"].items()), "Draft files changed")
        return old
    command = [config.get("python", "python3"), str(tool), "--input-dir", str(source), "--output-dir", str(target), "--model-type", config["model_type"]]
    write_json(Path(output) / "mtp-command.json", command)
    subprocess.run(command, check=True)
    require((target / "config.json").is_file() and list(target.glob("*.safetensors")), "MTP export artifacts incomplete")
    manifest = {"identity": digest(identity), "source": identity, "command": command,
                "files": {p.name: file_hash(p) for p in sorted(target.iterdir()) if p.is_file()}}
    write_json(marker, manifest)
    return manifest


def prepare_setup(config, output):
    command = config.get("command")
    require(isinstance(command, list) and command and all(isinstance(v, str) for v in command), "setup.command must be an explicit argv list")
    write_json(Path(output) / "setup-command.json", command)
    subprocess.run(command, cwd=config.get("cwd"), check=True)
    return {"command": command, "cwd": config.get("cwd")}


def prepare_resource(config, output):
    target = Path(config["path"])
    if not target.exists():
        hub = config.get("hub")
        require(config.get("repo_id") and config.get("revision"), "Missing resource requires a pinned repo_id and revision")
        if hub == "modelscope":
            from modelscope import snapshot_download
            snapshot_download(config["repo_id"], revision=config["revision"], local_dir=str(target))
        elif hub == "huggingface":
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=config["repo_id"], revision=config["revision"], local_dir=str(target), repo_type=config.get("repo_type", "model"))
        else:
            raise ValueError("Resource missing; provide modelscope/huggingface source or prepare the declared raw model/dataset locally")
    for name, expected in config.get("sha256", {}).items():
        path = target / name
        require(path.is_file() and file_hash(path) == expected, "Resource identity differs: " + str(path))
    return {"path": str(target), "hub": config.get("hub"), "repo_id": config.get("repo_id"), "revision": config.get("revision")}


def execute_step(kind, config, output):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    result = {"container": prepare_container, "source": prepare_source, "setup": prepare_setup, "resource": prepare_resource, "build": prepare_build, "mtp": prepare_mtp}[kind](config, output)
    write_json(output / "result.json", {"status": "PASS", "result": result})


def prepare_campaign(root, plan):
    root = Path(root)
    with (root / ".release.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = sorted((root / "preparation").glob("attempt-*/result.json"))
        if previous and read_json(previous[-1]).get("status") == "PASS":
            require(read_json(previous[-1]).get("plan_fingerprint") == plan["fingerprint"], "Preparation belongs to another plan")
            return read_json(previous[-1])
        directory = root / "preparation" / f"attempt-{len(list((root / 'preparation').glob('attempt-*')))+1:03d}"
        directory.mkdir(parents=True)
        result = {"plan_fingerprint": plan["fingerprint"], "status": "FAILED", "steps": []}
        try:
            preparation = plan["config"].get("preparation", {})
            for kind, plural in (("container", "containers"), ("source", "sources"), ("setup", "setup"), ("resource", "resources"), ("build", "builds"), ("mtp", "mtp")):
                for index, entry in enumerate(preparation.get(plural, [])):
                    config = {k: v for k, v in entry.items() if k != "context"}
                    context = merge(plan["config"]["environment"].get("host", {}), entry.get("context", {}))
                    require(kind != "container" or not context.get("container"), "Create containers on the configured host, not inside another container")
                    output = directory / f"{kind}-{index+1:03d}"
                    source = "import sys;sys.path.insert(0," + repr(str(Path(__file__).parent)) + ");from release_prepare import execute_step;execute_step(" + repr(kind) + "," + repr(config) + "," + repr(str(output)) + ")"
                    run(context, python_script(context, source), output / "execute.log", timeout=entry.get("timeout", 14400))
                    result["steps"].append({"kind": kind, "config": config, "output": str(output), "result": read_json(output / "result.json")})
            seen = {}
            for case in plan["cases"]:
                w = case.get("workload", {})
                if case["kind"] == "performance" and w["dataset"] != "random":
                    key = w["prepared_path"]
                    inputs = digest({"tokenizer": case["model"]["tokenizer"], "settings": {k: v for k, v in w.items() if k not in {"parallel", "rate", "open_loop", "endpoint", "temperature", "stream", "ignore_eos", "output_tokens", "strict_lengths", "strict_output_lengths", "tokenize_prompt"}}})
                    if key in seen:
                        require(seen[key] == inputs, "Different dataset preparations share one output path; choose independent prepared_path values")
                        continue
                    seen[key] = inputs
                    command = [case["environment"].get("client", {}).get("python", "python3"), str(Path(__file__).with_name("prepare_dataset.py")), "--case", str(root / "cases" / case["id"] / "resolved.json")]
                    run(case["environment"].get("client", {}), shlex.join(command), directory / f"dataset-{len(seen):03d}.log")
            result["status"] = "PASS"
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            result["error"] = str(exc)
            raise
        finally:
            write_json(directory / "result.json", result)
            write_json(root / "preparation.json", result)
        return result
