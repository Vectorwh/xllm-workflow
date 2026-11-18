"""Read-only xTask integration: isolated JSON/YAML, native script previews.

This file never copies, applies, cherry-picks, or edits an xTask patch.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import shlex
from urllib.parse import urlparse

import yaml

from release_common import ROOT, client_command, environment_lines, merge, read_json, require, write_json
from release_runtime import python_script, run
from frameworks import framework


def controller_context(case):
    client = dict(case["environment"].get("client", {}))
    # The native controller talks to Docker on the host even for a container client.
    client.pop("container", None)
    context = merge(client, case["xtask"].get("context", {}))
    require(not context.get("container"), "xTask controller must run outside the serving container")
    return context


def task_config(case):
    s, m = case["service"], case["model"]
    location = urlparse(s["api_url"])
    for name, value in (("model.name", m["name"]), ("model.path", m["path"]), ("API host", location.hostname or "")):
        require(re.fullmatch(r"[\w/.:@+-]+", value), f"Native xTask shell template cannot quote this {name}; use a supported path/adapter")
    require(location.path.rstrip("/") == "/v1", "xTask adapter requires an explicit /v1 API URL")
    require(framework(case) == "xllm" or case["xtask"].get("service_mode") == "external", "vLLM xTask execution uses service_mode=external with workflow-managed serving; native adapter not verified")
    task = {"case_name": "release_case", "engine_type": framework(case), "model_path": m["path"],
            "served_model_name": m["name"], "metrics": "perf" if case["kind"] == "performance" else "acc",
            "dataset_name": case.get("workload", {}).get("dataset", "random"),
            "dataset_path": "", "dataset_format": "json", "host_addr": location.hostname,
            "port": location.port, "communication_backend": s.get("flags", {}).get("communication_backend", "hccl"),
            "benchmark": {"benchmark_type": "evalscope"}}
    if case["kind"] == "performance":
        w = case["workload"]
        if w["dataset"] != "random":
            task["dataset_name"] = "custom"
            task["dataset_path"] = w["prepared_path"]
        require(w["endpoint"] == "/chat/completions", "This xTask adapter requires /chat/completions; choose a capable version/adapter for another endpoint")
        require(w.get("ignore_eos", True) is True and not w.get("open_loop", False), "Native xTask generator must support requested EOS/open-loop policy")
        task["benchmark"]["perf"] = {"case_type": "performance", "batch_size": w["parallel"],
            "num_prompts": w["number"], "request_rate": w.get("rate", -1),
            "min_prompt_length": w["input_tokens"] if w["dataset"] == "random" else 0,
            "max_prompt_length": w["input_tokens"] if w["dataset"] == "random" else w.get("max_prompt_tokens", 131072),
            "output_len": w["output_tokens"], "max_out_len": w["output_tokens"],
            "warmup_num": w["warmup"], "temperature": w.get("temperature", 0), "seed": w.get("seed", 42),
            "stream": w.get("stream", False), "tokenize_prompt": w.get("tokenize_prompt", False)}
        if w["dataset"] != "random": task["benchmark"]["perf"]["dataset_path"] = w["prepared_path"]
    else:
        a = case["accuracy"]
        require(len(a["datasets"]) == 1, "Use one xTask accuracy case per dataset")
        task["dataset_name"] = a["datasets"][0]
        task["benchmark"]["acc"] = {"batch_size": a.get("batch_size", 1), "endpoint": "/v1",
            "generation_config": a.get("generation_config", {}), "dataset_args": a.get("dataset_args", {}),
            "timeout": case.get("timeout", 7200), "acc_dataset_len": a.get("limit")}
    if case["xtask"].get("service_mode", "native") == "external":
        task["release_service_configuration"] = {"framework": framework(case), "code": case["code"], "model": m, "service": s}
        task["external_service"] = {"enabled": True, "endpoint": f"{location.scheme}://{location.netloc}", "health_path": "/health"}
        return task
    context = case["environment"].get("service", {})
    require(context.get("container"), "Native xTask integration requires an explicitly prepared existing container")
    client = case["environment"].get("client", {})
    control = controller_context(case)
    require(not control.get("container"), "xTask controller must run on the service host, outside the serving container")
    require(case["xtask"].get("instance_host", "127.0.0.1") in {"127.0.0.1", "localhost"}, "Built-in xTask integration uses a local instance on its controller host")
    require(client.get("ssh_host") == context.get("ssh_host") == control.get("ssh_host"), "xTask controller, native benchmark and service must use the same host context")
    if client.get("container"):
        require(client["container"] == context["container"], "Native xTask client must use the serving container or its host")
        require(not any(client.get(k) for k in ("sources", "env", "cpu_set")), "Native container benchmark cannot inherit custom client environment/CPU binding")
    # Current native multi-process template binds DEVICE*40. Never conceal a mismatch.
    expected = [f"{d*40}-{d*40+39}" for d in s["devices"]]
    require(list(map(str, s["cpu_sets"])) == expected, "xTask CPU affinity does not match its native startup template")
    flags = dict(s.get("flags", {}))
    flags["model_id"] = m["name"]
    if m.get("draft_model"):
        flags["draft_model"] = m["draft_model"]
    # Native xTask inserts these into shell text. Quote string values explicitly.
    flags = {k: shlex.quote(v) if isinstance(v, str) else v for k, v in flags.items()}
    task["deployment"] = {"master_host": case["xtask"].get("instance_host", "127.0.0.1"),
        "sync_workspace": False, "resource_sync": {"enabled": False},
        "benchmark": {"runner_instance": "master-0", "run_on_host": not case["environment"].get("client", {}).get("container"), "health_path": "/health"},
        "instances": [{"name": "master-0", "host": case["xtask"].get("instance_host", "127.0.0.1"),
            "role": "master", "rank_start": 0, "device_ids": s["devices"], "local_port": s["port"],
            "progress_conn_port": s["master_port"], "hccl_if_port": s["hccl_port"],
            "container_name": context["container"], "reuse_existing_container": True,
            "xllm_binary": case["code"]["binary"], "triton_binary_path": s.get("env", {}).get("TRITON_BINARY_PATH", ""),
            "serving_ready_timeout": s.get("ready_timeout", 900),
            "startup_environment": environment_lines(context, case).strip().splitlines(),
            "serving_extensiable_param": flags}]}
    return task


def prepare(case, attempt):
    attempt = Path(attempt)
    workspace = attempt / "xtask"
    workspace.mkdir()
    repo = Path(case["xtask"].get("repo", ""))
    require(repo.is_absolute(), "xtask.repo must be an absolute shared path")
    require(case["xtask"].get("commit"), "Pin xtask.commit")
    config = task_config(case)
    (workspace / "tests/cases").mkdir(parents=True)
    (workspace / "tests/cases/release_case.yaml").write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    (workspace / "config").mkdir()
    for filename, overrides in (("xTask_config.json", case["xtask"].get("task_config", {})),
                                ("xllm_config.json", case["xtask"].get("xllm_config", {}))):
        value = merge({}, overrides) if case["xtask"].get("exact_config", False) else merge(read_json(repo / "config" / filename), overrides)
        if filename == "xTask_config.json":
            value.setdefault("ENV_CONFIG", {})["CLEAN_RESULTS"] = False
            docker = value.setdefault("DOCKER_CONFIG", {})
            # Identity mappings are intentionally first: xTask appends cwd:/workspace.
            # All contexts were checked to share these paths before execution.
            paths = [str(attempt.parents[2]), case["model"]["path"], case["model"]["tokenizer"]]
            if case["model"].get("draft_model"):
                paths.append(case["model"]["draft_model"])
            docker["volumes"] = {**{p: p for p in paths}, **docker.get("volumes", {})}
        else:
            value.setdefault("XLLM_CONFIG", {})["serving_container_name"] = case["environment"].get("service", {}).get("container", "")
        write_json(workspace / "config" / filename, value)
    # Source links are read-only inputs. Configuration and generated outputs stay isolated.
    for name in ("tools", "xtask", "pkg"):
        (workspace / name).symlink_to(repo / name, target_is_directory=True)
    for source in (repo / "config").glob("*hosts*.json"):
        (workspace / "config" / source.name).write_bytes(source.read_bytes())
    if framework(case) == "vllm-ascend":
        name = "vllm_ascend_config.json"
        require((repo / "config" / name).is_file(), "Selected xTask has no vLLM-Ascend configuration")
        base = {} if case["xtask"].get("exact_config", False) else read_json(repo / "config" / name)
        write_json(workspace / "config" / name, merge(base, case["xtask"].get("vllm_config", {})))
    context = controller_context(case)
    source = '''import ast,hashlib,json,pathlib,subprocess,sys
repo=pathlib.Path(REPO); expected=EXPECTED
def check(ok,msg):
 if not ok: raise RuntimeError(msg)
head=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
check(head==expected,'xTask commit differs')
check(not subprocess.check_output(['git','-C',str(repo),'status','--porcelain'],text=True).strip(),'xTask checkout must be clean')
config=json.load(open(WORKSPACE+'/config/xTask_config.json'))
required={'xtask/benchmarks/evalscope/generate_evalscope_script.py':['warmup_num','temperature','tokenize_prompt']}
if NATIVE:
 required.update({'xtask/core/scheduler.py':['reuse_existing_container','run_on_host','xllm_binary'], 'xtask/core/service_group.py':['container_name','xllm_binary','startup_environment'], 'xtask/serv_fmk/xllm_serv/generate_xllm_serv.py':['XLLM_BINARY','TRITON_BINARY_PATH']})
identity={}
for name,keys in required.items():
 p=repo/name; text=p.read_text(); constants={n.value for n in ast.walk(ast.parse(text)) if isinstance(n,ast.Constant) and isinstance(n.value,str)}
 missing=set(keys)-constants;check(not missing,'xTask capability missing: '+name+' '+str(missing))
 identity[name]=hashlib.sha256(p.read_bytes()).hexdigest()
functions={n.name for n in ast.walk(ast.parse((repo/'xtask/core/scheduler.py').read_text())) if isinstance(n,ast.FunctionDef)}
check({'start_scheduler_session','run_scheduler_session_trial','close_scheduler_session'}<=functions,'Native session API missing')
sys.path.insert(0,str(repo))
import yaml
from xtask.benchmarks.evalscope.generate_evalscope_script import generate_evalscope_script
task=yaml.safe_load(pathlib.Path(WORKSPACE+'/tests/cases/release_case.yaml').read_text())
preview=pathlib.Path(WORKSPACE)/'preview';preview.mkdir()
generate_evalscope_script(task,str(preview))
if NATIVE:
 from xtask.core.service_group import build_service_plans
 from xtask.serv_fmk.xllm_serv.generate_xllm_serv import generate_xllm_serv_commands
 for plan in build_service_plans(task,str(preview)):
  generate_xllm_serv_commands(plan.instance_config,{},plan.output_dir,instance={'name':plan.instance_name,'role':plan.role})
pathlib.Path(WORKSPACE+'/capabilities.json').write_text(json.dumps({'commit':head,'source_sha256':identity,'patches_applied':False},indent=2))
'''.replace("WORKSPACE", repr(str(workspace))).replace("REPO", repr(str(repo))).replace("EXPECTED", repr(case["xtask"]["commit"])).replace("NATIVE", repr(case["xtask"].get("service_mode", "native") == "native"))
    # CWD is part of xTask's public configuration contract.
    require(re.fullmatch(r"[\w/.:@+-]+", str(workspace)), "Native xTask generated output path cannot contain shell metacharacters or whitespace")
    body = "cd " + shlex.quote(str(workspace)) + "\n" + python_script(context, source)
    run(context, body, attempt / "xtask-preview.log")
    verify_preview(case, workspace)
    return workspace


def verify_preview(case, workspace):
    script = (workspace / "preview/run_evalscope_test.sh").read_text()
    lines = [line[5:].removesuffix("; then") for line in script.splitlines()
             if line.startswith("if ! ") and ("evalscope perf " in line or "evalscope eval " in line)]
    require(len(lines) == 1, "Cannot identify the native xTask benchmark command")
    actual = shlex.split(lines[0])
    expected = client_command(case, workspace / "preview")
    # Compare all declared benchmark options; extra image defaults are irrelevant to random.
    for index, item in enumerate(expected):
        if not item.startswith("--"):
            continue
        require(item in actual, f"xTask generated command omits {item}")
        if index + 1 < len(expected) and not expected[index + 1].startswith("--"):
            wanted = expected[index+1]
            observed = actual[actual.index(item)+1]
            if item in {"--tokenizer-path", "--outputs-dir", "--work-dir"}:
                wanted, observed = str(PurePosixPath(wanted)), str(PurePosixPath(observed))
            require(actual.count(item) == 1 and observed == wanted, f"xTask generated command differs: {item}")
    write_json(workspace / "command-verification.json", {"status": "PASS", "expected": expected, "actual": actual})


def evaluate_group(entries):
    case, attempt, workspace = entries[0]
    context = controller_context(case)
    payload = [{"case": c, "attempt": str(a), "workspace": str(w)} for c, a, w in entries]
    source = '''import json,pathlib,signal,subprocess,sys,time
entries=json.loads(ENTRIES); first=entries[0]; case=first['case']
sys.path.insert(0,case['xtask']['repo'])
from xtask.core.scheduler import start_scheduler_session,run_scheduler_session_trial,close_scheduler_session
def interrupted(signum,frame):raise SystemExit(128+signum)
signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGHUP,interrupted)
signal.signal(signal.SIGALRM,interrupted)
signal.alarm(sum(entry['case'].get('timeout',7200) for entry in entries))
def lifecycle(operation,extra):
 directory=pathlib.Path(first['attempt'])/'service';directory.mkdir(exist_ok=True)
 cmd=[sys.executable,HELPER,operation,'--attempt-dir',str(directory),'--attempt-id',pathlib.Path(first['attempt']).name,*extra]
 with (directory/(operation+'.log')).open('xb') as log:subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True)
session=None
try:
 session=start_scheduler_session(first['workspace']+'/tests/cases/release_case.yaml',first['attempt']+'/raw')
 if not session.prepared or session.failed:raise RuntimeError('xTask service preparation failed: '+str(session.context.result))
 url=case['service']['api_url']
 lifecycle('ready',['--api-url',url,'--expected-model-id',case['model']['name'],'--timeout',str(case['service'].get('ready_timeout',900))])
 request=case['service'].get('smoke_request',{'model':case['model']['name'],'messages':[{'role':'user','content':'Reply with OK.'}],'max_tokens':16,'temperature':0,'stream':False})
 request_path=pathlib.Path(first['attempt'])/'service/smoke-request.json';request_path.write_text(json.dumps(request))
 lifecycle('smoke',['--api-url',url,'--model',case['model']['name'],'--request-json',str(request_path)])
 sys.path.insert(0,SKILL_SCRIPTS)
 from direct_adapter import check_activation
 for entry in entries:
  if (entry['case'].get('comparison_group') or entry['case']['service'].get('activation_checks')) and not pathlib.Path(entry['attempt']+'/activation.json').exists():
   check_activation(entry['case'],pathlib.Path(first['attempt'])/'raw',pathlib.Path(entry['attempt']))
 for entry in entries:
  result=run_scheduler_session_trial(session,entry['workspace']+'/tests/cases/release_case.yaml',entry['attempt']+'/raw')
  pathlib.Path(entry['attempt']+'/xtask-result.json').write_text(json.dumps(result,default=str,indent=2))
  if result.get('scheduler_error') or not result.get('benchmark_success'):raise RuntimeError('xTask benchmark failed; inspect xtask-result.json')
  time.sleep(case['service'].get('inter_case_seconds',10))
finally:
 signal.alarm(0)
 close_scheduler_session(session)
'''.replace("ENTRIES", repr(json.dumps(payload))).replace("HELPER", repr(str(ROOT / "skills/xllm-npu-server-manager/scripts/service_lifecycle.py"))).replace("SKILL_SCRIPTS", repr(str(Path(__file__).parent)))
    wrapper = Path(attempt) / "xtask-session.py"
    wrapper.write_text(source)
    command = [context.get("python", "python3"), str(wrapper)]
    if case["environment"].get("client", {}).get("cpu_set"):
        command = ["taskset", "-c", str(case["environment"]["client"]["cpu_set"]), *command]
    body = "export XTASK_BENCH_REPORT=0\ncd " + shlex.quote(str(workspace)) + "\n" + shlex.join(command)
    (Path(attempt) / "run_xtask.sh").write_text("#!/usr/bin/env bash\n" + environment_lines(context) + body + "\n")
    # Give the native session's finally block time to close after its own alarm.
    run(context, body, Path(attempt) / "xtask.log", timeout=sum(c.get("timeout", 7200) for c, _, _ in entries) + 180)
