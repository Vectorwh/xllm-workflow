"""Execution contexts and read-only provenance checks for release cases."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess

from release_common import ROOT, environment_lines, require, read_json, write_json


def run(context, body, log, *, case=None, timeout=1800):
    """Stream a script over stdin; paths must be shared, no remote code install."""
    command = ["bash", "-s"]
    if context.get("container"):
        command = ["docker", "exec", "-i", context["container"], *command]
    if context.get("ssh_host"):
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                   context["ssh_host"], shlex.join(command)]
    body = environment_lines(context, case) + body
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("xb") as output:
        result = subprocess.run(command, input=body.encode(), stdout=output,
                                stderr=subprocess.STDOUT, timeout=timeout)
    require(result.returncode == 0, f"Execution failed ({result.returncode}); see {log}")


def python_script(context, source):
    return shlex.join([context.get("python", "python3"), "-c", source]) + "\n"


def flow(case, case_root, *args):
    context = case["environment"].get("service", {})
    return shlex.join([context.get("python", "python3"), str(ROOT / "scripts/xllm_flow.py"), *map(str, args)])


def snapshot(case, directory, *, after=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    host = case["environment"].get("host", {})
    cmd = [host.get("python", "python3"), str(ROOT / "skills/xllm-npu-benchmark/scripts/capture_fairness_snapshot.py"),
           "--output", str(directory / "snapshot.json"), "--raw-dir", str(directory / "raw")]
    for device in case["service"]["physical_devices"]:
        cmd += ["--physical-device", str(device)]
    body = "unset ASCEND_RT_VISIBLE_DEVICES ASCEND_VISIBLE_DEVICES NPU_VISIBLE_DEVICES\n" + shlex.join(cmd) + "\n"
    run(host, body, directory / "capture.log")
    data = read_json(directory / "snapshot.json")
    require(not data.get("collection_errors"), "NPU snapshot incomplete")
    require(len(data["devices"]) == len(case["service"]["physical_devices"]), "Physical device snapshot incomplete")
    if not after:
        limits = case["environment"].get("idle_limits", {"hbm_usage_pct": 10, "aicore_usage_pct": 1})
        for device in data["devices"]:
            require(not device["processes"], f"Device is occupied: {device['physical_id']}")
            require(str(device["health"]).upper() in {"OK", "HEALTHY", "NORMAL"}, "Device health is not OK")
            for name, limit in limits.items():
                require(device.get(name) is not None and device[name] <= limit, f"Device {name} exceeds idle policy")
    return data


def verify_target(case, attempt):
    """Run inside the service environment; never substitute a different binary."""
    attempt = Path(attempt)
    output = attempt / "identity.json"
    config = json.dumps({"case": case, "output": str(output)})
    source = '''import ctypes,hashlib,importlib.metadata,json,os,pathlib,re,socket,subprocess
c=json.loads(CONFIG); case=c['case']; code=case['code']; service=case['service']; model=case['model']
def check(ok,msg):
 if not ok: raise RuntimeError(msg)
def sha(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  for b in iter(lambda:f.read(8388608),b''): h.update(b)
 return h.hexdigest()
def capture(argv):return subprocess.check_output(argv,text=True).strip()
commit=capture(['git','-C',code['repo'],'rev-parse','HEAD'])
check(commit==code['commit'],'xLLM commit differs')
dirty=capture(['git','-C',code['repo'],'status','--porcelain'])
check(not dirty or code.get('allow_dirty',False),'xLLM working tree dirty; record/approve build-generated differences explicitly')
verdict=json.load(open(code['build_verdict'])); provenance=json.load(open(code['binary_provenance']))
check(verdict.get('status')=='PASS' and verdict.get('binary_ready'),'Build gate is not PASS')
check(provenance.get('commit')==commit,'Build provenance commit differs')
check(pathlib.Path(provenance.get('repo','')).resolve()==pathlib.Path(code['repo']).resolve(),'Build provenance checkout differs')
diff=subprocess.check_output(['git','-C',code['repo'],'diff','--binary','HEAD'],text=True).encode()
untracked={}
for name in capture(['git','-C',code['repo'],'ls-files','--others','--exclude-standard','-z']).split('\\0'):
 if name and pathlib.Path(code['repo'],name).is_file():untracked[name]=sha(pathlib.Path(code['repo'],name))
dirty_sha=hashlib.sha256(diff+json.dumps(untracked,sort_keys=True).encode()).hexdigest()
check(provenance.get('dirty_diff_sha256')==dirty_sha,'Working tree differs from build provenance; rerun build gate')
pathlib.Path(c['output']).with_name('code-working-tree.diff').write_bytes(diff)
pathlib.Path(c['output']).with_name('code-untracked-sha256.json').write_text(json.dumps(untracked,indent=2))
binary=pathlib.Path(code['binary']).resolve(); binary_sha=sha(binary)
identity=provenance.get('binary',{})
check(str(binary)==str(pathlib.Path(identity.get('path','')).resolve()),'Build provenance binary path differs')
check(binary_sha==identity.get('sha256'),'Build provenance binary hash differs')
if service.get('ownership')=='external':
 entries=[line.split() for line in pathlib.Path(service['external_pid_file']).read_text().splitlines()]
 check(len(entries)==len(service['devices']),'External service rank count differs')
 for rank,(pid,start) in enumerate(entries):
  proc=pathlib.Path('/proc')/pid
  check(proc.joinpath('stat').read_text().split(') ',1)[1].split()[19]==start,'External service PID identity changed')
  check(proc.joinpath('exe').resolve()==binary,'External service binary differs')
  argv=proc.joinpath('cmdline').read_bytes().decode().strip('\\0').split('\\0');actual={};i=0
  while i<len(argv):
   arg=argv[i]
   if arg.startswith('--'):
    key,sep,value=arg[2:].partition('=')
    if not sep and i+1<len(argv) and not argv[i+1].startswith('--'):i+=1;value=argv[i]
    actual[key]=value
   i+=1
  wanted={'model':model['path'],'model_id':model['name'],'node_rank':rank,**service.get('flags',{})}
  if model.get('draft_model'):wanted['draft_model']=model['draft_model']
  for key,value in wanted.items():check(str(actual.get(key)).lower()==str(value).lower(),'External service flag differs: '+key)
model_hashes={}
for folder in [model['path'],model['tokenizer'],model.get('draft_model')]:
 if folder: check(pathlib.Path(folder).is_dir(),'Model/tokenizer/draft directory missing: '+folder)
for p,expected in model.get('sha256',{}).items():
 actual=sha(p); check(actual==expected,'Model identity differs: '+p); model_hashes[p]=actual
for folder in {model['path'],model['tokenizer'],model.get('draft_model')} - {None,''}:
 for name in ['config.json','tokenizer.json','tokenizer_config.json','quant_model_description.json']:
  p=pathlib.Path(folder)/name
  if p.is_file():model_hashes[str(p)]=sha(p)
help_result=subprocess.run([str(binary),'--helpfull'],capture_output=True,text=True,timeout=60)
help_text=help_result.stdout+help_result.stderr
if not help_text.strip():
 r=subprocess.run([str(binary),'--help'],capture_output=True,text=True,timeout=60); help_text=r.stdout+r.stderr
available=set(re.findall(r'-{1,2}([A-Za-z][A-Za-z0-9_]*)',help_text))
required=set(service.get('flags',{}))|{'model','model_id','nnodes','node_rank','port','master_node_addr'}
if model.get('draft_model'):required.add('draft_model')
check(required<=available,'Unsupported xLLM flags: '+str(sorted(required-available)))
mapping=[]
if service.get('verify_acl_mapping',True):
 home=os.environ.get('ASCEND_HOME_PATH') or os.environ.get('ASCEND_TOOLKIT_HOME')
 check(bool(home),'CANN environment not loaded')
 acl=ctypes.CDLL(str(pathlib.Path(home)/'lib64/libascendcl.so'))
 check(acl.aclInit(None)==0,'aclInit failed')
 try:
  for user,expected in enumerate(service['physical_devices']):
   logic=ctypes.c_int32(-1); physical=ctypes.c_int32(-1)
   check(acl.aclrtGetLogicDevIdByUserDevId(user,ctypes.byref(logic))==0,'ACL logical mapping failed')
   check(acl.aclrtGetPhyDevIdByLogicDevId(logic.value,ctypes.byref(physical))==0,'ACL physical mapping failed')
   check(physical.value==expected,'Actual physical mapping differs')
   mapping.append({'user':user,'logic':logic.value,'physical':physical.value})
 finally:acl.aclFinalize()
versions={}
for package in ['torch','torch-npu','evalscope','transformers','tokenizers','triton','tilelang']:
 try:versions[package]=importlib.metadata.version(package)
 except importlib.metadata.PackageNotFoundError:versions[package]=None
for key,expected in case['environment'].get('expected_packages',{}).items():
 check(versions.get(key)==expected,'Package version differs: '+key)
result={'commit':commit,'git_status':dirty,'dirty_diff_sha256':dirty_sha,'binary':str(binary),'binary_sha256':binary_sha,'model_metadata_sha256':model_hashes,'device_mapping':mapping,'packages':versions,'flags_help':help_text,'build_provenance':provenance,'build_verdict':verdict,'environment':{k:os.environ.get(k) for k in set(service.get('env',{}))|{'PATH','LD_LIBRARY_PATH','ASCEND_RT_VISIBLE_DEVICES','HCCL_IF_BASE_PORT'}}}
weight_files={str(p) for folder in [model['path'],model.get('draft_model')] if folder for pattern in ['*.safetensors','*.bin'] for p in pathlib.Path(folder).glob(pattern)}
result['model_weights_verified']=bool(weight_files) and weight_files<=set(model.get('sha256',{}))
pathlib.Path(c['output']).write_text(json.dumps(result,indent=2))
'''.replace("CONFIG", repr(config), 1)
    if case.get("framework") == "vllm-ascend":
        source = "import sys,json,pathlib;sys.path.insert(0," + repr(str(Path(__file__).parent)) + ");from target_identity import observe_vllm;pathlib.Path(" + repr(str(output)) + ").write_text(json.dumps(observe_vllm(" + repr(case) + ")))"
    else:
        source += "\nimport sys;sys.path.insert(0," + repr(str(Path(__file__).parent)) + ")\nfrom target_identity import common_identity\nresult.update(common_identity(case))\npathlib.Path(c['output']).write_text(json.dumps(result,indent=2))\n"
    context = case["environment"].get("service", {})
    run(context, python_script(context, source), attempt / "identity.log", case=case)
    client = case["environment"].get("client", {})
    client_source = ("import importlib.metadata,json,pathlib,sys; "
                     f"pathlib.Path({str(attempt / 'client-identity.json')!r}).write_text(json.dumps("
                     "{'python':sys.version,'evalscope':importlib.metadata.version('evalscope')}))")
    run(client, python_script(client, client_source), attempt / "client-identity.log")
    if case.get("framework") == "vllm-ascend":
        host = case["environment"].get("host", {})
        require(case["environment"].get("service", {}).get("container"), "vLLM image identity requires a declared serving container")
        inspected = attempt / "container-image.json"
        command = ["docker", "inspect", case["environment"]["service"]["container"]]
        def save_json_command(command, output, log):
            source = "import pathlib,subprocess;pathlib.Path(" + repr(str(output)) + ").write_bytes(subprocess.check_output(" + repr(command) + "))"
            run(host, python_script(host, source), log)
        save_json_command(command, inspected, attempt / "container-inspect.log")
        container = read_json(inspected)[0]
        expected = attempt / "expected-image.json"
        save_json_command(["docker", "image", "inspect", case["code"]["image"]], expected, attempt / "image-inspect.log")
        require(container["Image"] == read_json(expected)[0]["Id"], "Serving container image differs")
        identity = read_json(output); identity["image_id"] = container["Image"]; write_json(output, identity)
    return read_json(output)


def verify_shared(case, attempt):
    """Refuse execution if a context sees a different/missing run directory."""
    attempt = Path(attempt)
    token = attempt / "shared-path-token"
    token.write_text(str(attempt))
    for label in ("host", "service", "client"):
        context = case["environment"].get(label, {})
        source = ("from pathlib import Path; "
                  f"assert Path({str(token)!r}).read_text()=={str(attempt)!r}; "
                  f"assert Path({str(ROOT / 'scripts/xllm_flow.py')!r}).is_file()")
        run(context, python_script(context, source), attempt / f"shared-{label}.log")
