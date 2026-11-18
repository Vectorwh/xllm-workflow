"""Render extensible xLLM commands; reuse the repository lifecycle recorder."""
from __future__ import annotations

import json
from pathlib import Path
import shlex

from release_common import ASSETS, ROOT, client_command, environment_lines, flag_args, write_json
from release_runtime import run, snapshot
from frameworks import framework, semantic_contract, vllm_arguments


def render_template(name, replacements):
    text = (ASSETS / "templates" / name).read_text()
    for key, value in replacements.items():
        text = text.replace("@@" + key + "@@", str(value))
    return text


def render_service(case, attempt):
    attempt = Path(attempt)
    directory = attempt / "service"
    directory.mkdir(parents=True)
    context = case["environment"].get("service", {})
    python = context.get("python", "python3")
    commands = []
    body = [f"test ! -e {shlex.quote(str(directory / 'pids.txt'))}"]
    ranks = list(enumerate(case["service"]["cpu_sets"])) if framework(case) == "xllm" else [(0, ",".join(map(str, case["service"]["cpu_sets"])))]
    for rank, cpus in ranks:
        args = flag_args(case, rank) if framework(case) == "xllm" else vllm_arguments(case)
        command = ["numactl", "-C", str(cpus), case["code"]["binary"], *args]
        commands.append(command)
        body += [f"nohup setsid {shlex.join(command)} > {shlex.quote(str(directory / f'node_{rank}.log'))} 2>&1 < /dev/null &",
                 "pid=$!", f"{shlex.quote(python)} -c " + shlex.quote(
                     "import pathlib,sys; p=sys.argv[1]; "
                     "start=pathlib.Path('/proc/'+p+'/stat').read_text().split(') ',1)[1].split()[19]; "
                     "print(p,start)") + f" \"$pid\" >> {shlex.quote(str(directory / 'pids.txt'))}"]
    start = render_template("start_xllm.sh.tmpl", {"ENVIRONMENT": environment_lines(context, case), "LAUNCH": "\n".join(body)})
    (directory / "start.sh").write_text(start)
    stop_source = '''import json,os,pathlib,signal,time
directory=pathlib.Path(DIRECTORY)
manifest=directory/'pids.txt'
entries=[line.split() for line in manifest.read_text().splitlines()] if manifest.exists() else []
def running(pid,start):
 try:
  fields=pathlib.Path('/proc/'+pid+'/stat').read_text().split(') ',1)[1].split()
  return fields[19]==start and fields[0]!='Z'
 except FileNotFoundError:return False
for pid,start in entries:
 if running(pid,start):os.kill(int(pid),signal.SIGTERM)
deadline=time.monotonic()+60
while any(running(*entry) for entry in entries) and time.monotonic()<deadline:time.sleep(.2)
remaining=[entry for entry in entries if running(*entry)]
(directory/'process-cleanup.json').write_text(json.dumps({'remaining':remaining,'pids':entries}))
if remaining:raise RuntimeError('Owned processes did not exit; inspect before releasing the device lease')
'''.replace("DIRECTORY", repr(str(directory)))
    stop = render_template("stop_xllm.sh.tmpl", {"ENVIRONMENT": environment_lines(context, case),
                            "STOP": shlex.join([python, "-c", stop_source])})
    (directory / "stop.sh").write_text(stop)
    write_json(directory / "commands.json", commands)
    return directory


def render_workload(case, attempt):
    attempt = Path(attempt)
    raw = attempt / "raw"
    command = client_command(case, raw)
    name = "eval_perf.sh.tmpl" if case["kind"] == "performance" else "eval_acc.sh.tmpl"
    text = render_template(name, {"ENVIRONMENT": environment_lines(case["environment"].get("client", {})),
                                 "COMMAND": shlex.join(command)})
    path = attempt / "eval.sh"
    path.write_text(text)
    write_json(attempt / "client-command.json", command)
    return path


def lifecycle(case, directory, operation, extra):
    context = case["environment"].get("service", {})
    cmd = [context.get("python", "python3"), str(ROOT / "skills/xllm-npu-server-manager/scripts/service_lifecycle.py"),
           operation, "--attempt-dir", str(directory), "--attempt-id", directory.parent.name, *map(str, extra)]
    run(context, shlex.join(cmd), directory / (operation + ".log"), case=case)


def start(case, directory):
    service, context = case["service"], case["environment"].get("service", {})
    url = service.get("ready_api_url", service["api_url"])
    if service.get("ownership", "managed") == "managed":
        run(context, "bash " + shlex.quote(str(directory / "start.sh")), directory / "start.log", case=case)
        extra = ["--api-url", url, "--model", case["model"]["path"],
                 "--command-file", directory / "start.sh", "--pid-file", directory / "pids.txt"]
        for rank, device in enumerate(service["devices"]):
            extra += ["--visible-device", device]
            if framework(case) == "xllm" or rank == 0: extra += ["--log", directory / f"node_{rank}.log", "--port", service["port"] + rank]
        lifecycle(case, directory, "launch", extra)
    else:
        write_json(directory / "external-service.json", {"ownership": "external", "api_url": url})
    lifecycle(case, directory, "ready", ["--api-url", url, "--expected-model-id", case["model"]["name"],
                                         "--timeout", service.get("ready_timeout", 900)])
    smoke = service.get("smoke_request", {"model": case["model"]["name"],
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 16, "temperature": 0, "stream": False})
    write_json(directory / "smoke-request.json", smoke)
    lifecycle(case, directory, "smoke", ["--api-url", url, "--model", case["model"]["name"],
                                         "--request-json", directory / "smoke-request.json"])


def stop(case, directory, attempt):
    if case["service"].get("ownership", "managed") == "external":
        write_json(directory / "cleanup.json", {"status": "EXTERNAL", "disposition": "External service retained; caller owns cleanup"})
        return
    context = case["environment"].get("service", {})
    if (directory / "pids.txt").exists():
        run(context, "bash " + shlex.quote(str(directory / "stop.sh")), directory / "stop.log", case=case)
    after = Path(attempt) / "env" / "after_stop"
    snapshot(case, after, after=True)
    extra = ["--pid-file", directory / "pids.txt", "--npu-snapshot", after / "snapshot.json"]
    for rank in range(len(case["service"]["devices"]) if framework(case) == "xllm" else 1):
        extra += ["--port", case["service"]["port"] + rank]
    for device in case["service"]["physical_devices"]:
        extra += ["--expected-physical-device", device]
    lifecycle(case, directory, "cleanup", extra)


def evaluate(case, attempt):
    path = render_workload(case, attempt)
    run(case["environment"].get("client", {}), "bash " + shlex.quote(str(path)),
        Path(attempt) / "client.log", timeout=case.get("timeout", 7200))


def check_activation(case, directory, attempt):
    """Match version-specific startup evidence for enabled comparison features."""
    if not case.get("comparison_group") and not case["service"].get("activation_checks"): return {"status": "NOT_REQUIRED"}
    import re
    from release_common import require
    contract = semantic_contract(case)
    checks = case["service"].get("activation_checks", {})
    required = [name for name, enabled in (("graph", contract["graph_enabled"]), ("mtp", contract["mtp_enabled"])) if enabled]
    require(all(checks.get(name) for name in required), "Paired tests require version-specific activation_checks for enabled graph/MTP")
    required = list(dict.fromkeys([*required, *checks]))
    # Logs can be read from another context as long as the declared shared path holds.
    paths = list(Path(directory).glob("node_*.log"))
    if not paths: paths = list(Path(attempt).rglob("*.log"))
    matches = {}
    for name in required:
        matches[name] = [str(p) for p in paths if re.search(checks[name], p.read_text(errors="replace"), re.I | re.M)]
        require(matches[name], f"No runtime activation evidence for {name}; inspect the version's service logs")
    result = {"status": "PASS", "contract": contract, "patterns": checks, "matches": matches}
    write_json(Path(attempt) / "activation.json", result)
    return result
