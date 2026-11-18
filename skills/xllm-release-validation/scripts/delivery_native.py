"""Export native xTask cases plus standalone preparation instructions.

The recipient needs xTask and its dependencies, never this workflow or skill.
Only internal orchestration calls this module; it executes no preparation.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import shlex

import yaml

from frameworks import framework, vllm_arguments
from release_common import require


def replace_tree(value, replacements):
    if isinstance(value, dict): return {replace_tree(k, replacements): replace_tree(v, replacements) for k, v in value.items()}
    if isinstance(value, list): return [replace_tree(v, replacements) for v in value]
    if isinstance(value, str):
        for before, after in sorted(replacements.items(), key=lambda pair: -len(pair[0])):
            if before: value = value.replace(before, after)
    return value


def settings(row, release):
    p = deepcopy(row.get("delivery", {}).get("xtask_native", {}))
    for name, value in (("release", release.get("commit")), ("xTask", row.get("xtask", {}).get("commit"))):
        require(re.fullmatch(r"[0-9a-f]{40}", str(value or "")) and value != "0" * 40, f"Pin the {name} commit for delivery")
    image = p.get("image", "")
    require(image and "REPLACE_" not in image and (":" in image.rsplit("/", 1)[-1]), "Native delivery requires a versioned public image")
    require(p.get("workspace", "").startswith("/export/home/"), "Declare a concrete new /export/home workspace for native xTask tools")
    require(re.fullmatch(r"[A-Za-z0-9/_.-]+", p["workspace"]) and ".." not in Path(p["workspace"]).parts, "Delivery workspace must be concrete and supported by native shell templates")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", row["case_id"]), "Unsafe delivery case ID")
    # One fresh xTask tree per case prevents old wheels from another run being used.
    p["workspace"] = p["workspace"].rstrip("/") + "/" + row["case_id"]
    p.setdefault("xtask_url", "https://github.com/Vectorwh/xTask.git")
    if framework(row) == "xllm":
        require(p.get("source_url"), "Native xLLM delivery requires source_url")
        if row["model"].get("draft_model"): require(p.get("mtp", {}).get("model_type"), "MTP delivery requires model_type")
    else:
        require(image == row["code"]["image"], "vLLM image must match the declared measured version")
    return p


def delivery_case(row, release):
    from xtask_adapter import task_config
    p = settings(row, release)
    c = deepcopy(row)
    c["service"]["ownership"] = "managed"
    c["service"]["api_url"] = f"http://127.0.0.1:{c['service']['port']}/v1"
    c["xtask"]["instance_host"] = "127.0.0.1"
    c["xtask"]["context"] = {}
    c["environment"]["host"] = {"python": "python3"}
    c["environment"]["client"] = {"python": "python3"}
    client_container = row["environment"].get("client", {}).get("container")
    if client_container:
        require(client_container == row["environment"]["service"].get("container"), "Separate client containers need an explicit native preparation adapter")
        c["environment"]["client"] = {"container": "delivery-generation-only"}
    c["environment"]["service"] = {"container": "delivery-generation-only", "sources": p.get("sources", [])}
    c["service"].get("env", {}).pop("TRITON_BINARY_PATH", None)  # Native template discovers the installed package.
    c["service"].setdefault("env", {}).update(p.get("env", {}))
    if c["model"].get("draft_model"):
        draft = p["workspace"] + "/models/mtp-draft"
        c = replace_tree(c, {c["model"]["draft_model"]: draft})
    if c.get("workload", {}).get("dataset") in {"gsm8k", "sharegpt"}:
        c["workload"]["prepared_path"] = p["workspace"] + "/datasets/prompts.txt"
    if framework(row) != "xllm": c["xtask"]["service_mode"] = "external"
    else: c["xtask"]["service_mode"] = "native"
    task = task_config(c)
    task["case_name"] = row["case_id"]
    task.pop("release_service_configuration", None)
    for instance in task.get("deployment", {}).get("instances", []):
        # A container_name alone can trigger reuse in xTask. Omit all reuse and
        # binary overrides, rather than setting reuse_existing_container=false.
        for field in ("container_name", "reuse_existing_container", "xllm_binary", "triton_binary_path"):
            instance.pop(field, None)
    text = yaml.safe_dump(task)
    for value in (row["code"].get("repo"), row["code"].get("binary") if framework(row) == "xllm" else None,
                  row["model"].get("draft_model")):
        require(not value or value not in text, "Native delivery still references an internal artifact; provide portable delivery.xtask_native.env/paths")
    require("@@" not in text and "REPLACE_" not in text and "{{" not in text, "Delivery YAML contains unresolved placeholders")
    return task


def generate(row, release):
    return delivery_case(row, release)


def yaml_text(row, release, task):
    p = settings(row, release)
    header = [f"# Image: {p['image']}", f"# xTask commit: {row['xtask']['commit']}",
              f"# xLLM commit: {release['commit']}" if framework(row) == "xllm" else f"# vLLM-Ascend version: {row['code']['version']}",
              f"# Native preparation and test instructions: {row['case_id']}.md"]
    return "\n".join(header) + "\n" + yaml.safe_dump(task, sort_keys=False, allow_unicode=True)


def instructions(row, release, task):
    """Concrete commands for recipients; no script from this workflow is needed."""
    p = settings(row, release)
    q = shlex.quote
    work, cid = p["workspace"], row["case_id"]
    image_home, _, image_tag = p["image"].rpartition(":")
    # xTask appends cwd:/workspace and overwrites an identical host key. Mount
    # its fresh parent first so absolute draft/data paths remain visible too.
    paths = [str(Path(work).parent), row["model"]["path"], row["model"]["tokenizer"]]
    config = {"ENV_CONFIG": {"CLEAN_RESULTS": False, "DEPENDENCE_PACKAGE_PATH": p.get("dependence_package_path", ""), "XTask_LOG_LEVEL": "INFO"},
              "DOCKER_CONFIG": {"enable_proxy": False, "volumes": {path: path for path in paths}}}
    engine = {"XLLM_CONFIG": {"image_home": image_home, "default_image_name": image_tag,
              "serving_container_name": "", "enable_proxy": False, "benchmark_timeout": row.get("timeout", 7200)}}
    lines = [f"# {cid} 原生 xTask 执行说明", "", "本文件与同名 YAML 一起交付。接收方只需 xTask、Git、Docker、NPU 驱动及所选版本依赖，无需安装发版 skill 或 workflow。",
        "固定镜像、源码和启动/评测参数；无需复制原机器、容器或编译产物。物理卡、CPU、端口、权重与数据路径按本机核对，修改资源路径时同步 YAML 与挂载。",
        "以下命令在目标 NPU 宿主机的新目录执行；任一步失败即停止，不回退使用旧 wheel。示例按 Bash 执行，保留全部日志。",
        "", "## 1 获取固定版本 xTask", "", "```bash", "set -euo pipefail",
        f"XTASK_DIR={q(work)}", 'test ! -e "$XTASK_DIR"',
        f"git clone {q(p['xtask_url'])} \"$XTASK_DIR\"",
        f"git -C \"$XTASK_DIR\" checkout --detach {row['xtask']['commit']}",
        f"test \"$(git -C \"$XTASK_DIR\" rev-parse HEAD)\" = {row['xtask']['commit']}",
        'cd "$XTASK_DIR"', "python3 -m pip install -r requirement.txt", "mkdir -p outputs", "```", "",
        "## 2 配置原生 JSON", "", "镜像由 config/xllm_config.json 读取，不放入虚构的 YAML 字段。下面是所需覆盖值：合并进新 checkout 的对应 JSON，并清空旧 serving_container_name。", "",
        "config/xTask_config.json：", "", "```json", json.dumps(config, indent=2, ensure_ascii=False), "```", "",
        "config/xllm_config.json：", "", "```json", json.dumps(engine, indent=2, ensure_ascii=False), "```", "",
        "保留该版本必需的其他默认键；代理、资源目录和额外设备/权重挂载按目标机配置。CLEAN_RESULTS=false 保留本次记录；不引用原服务容器。所有这些配置均是 xTask 原生 JSON。",
        "按所选 download_xllm.sh 核对原始运行依赖（例如 xllm_pack、numactl RPM 和脚本仍要求的 torch_npu wheel），准备到 DEPENDENCE_PACKAGE_PATH 并配置挂载；这些是可重新获取的依赖，不是原测试容器/产物。目录未填写不表示这些依赖可以忽略。", ""]
    if framework(row) == "xllm":
        lines += ["## 3 拉取指定 xLLM commit 并编译", "",
            "原生 tools/prepare_xllm.sh 支持仓库与分支选择，可在固定 ref 对应所需 commit 时使用；不要将任意 SHA 冒充分支传入。下面对完整 commit 使用 Git 检出，再直接调用同一个原生容器内构建工具，避免 prepare 的 git pull 将版本推进。", "",
            "```bash", f"git clone --no-checkout {q(p['source_url'])} xllm",
            f"git -C xllm checkout --detach {release['commit']}",
            "git -C xllm submodule update --init --recursive",
            f"test \"$(git -C xllm rev-parse HEAD)\" = {release['commit']}",
            "git -C xllm status --porcelain > outputs/source-status.txt",
            "test ! -s outputs/source-status.txt", f"docker pull {q(p['image'])}", "```", "",
            "以 tools/prepare_xllm.sh 中的 Docker 参数为依据，挂载本轮 XTASK_DIR（同一绝对路径）、驱动、所需设备、权重和本轮 vcpkg 缓存，设置 WORK_ROOT、HOST_UID/HOST_GID，使用上述镜像运行：", "",
            "```bash", f"docker run --rm --privileged --network host --ipc host --device /dev/davinci{row['service']['physical_devices'][0]} --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \\",
            '  -v "$XTASK_DIR:$XTASK_DIR" -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \\',
            '  -e WORK_ROOT="$XTASK_DIR" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \\',
            f"  {q(p['image'])} bash \"$XTASK_DIR/tools/build_xllm_in_container.sh\" > outputs/build.log 2>&1",
            "test -d pkg/xllm/vendors", "find pkg/xllm -maxdepth 1 -name 'xllm_npu*.whl' -print",
            f"test \"$(git -C xllm rev-parse HEAD)\" = {release['commit']}", "```", "",
            "检查本轮 wheel 唯一、vendors/runtime_libs/third_whl 等归档完整，记录源码 SHA 和 wheel SHA256。测试时 xTask 的 download_xllm.sh 从本轮 pkg/xllm 安装到新测试容器；不依赖原二进制路径。所选版本若有硬编码代理/下载源或额外挂载要求，按该版本原生文档配置可用网络和资源；不能声称任意公开网络均已验证。", ""]
        if row["model"].get("draft_model"):
            draft = task["deployment"]["instances"][0]["serving_extensiable_param"]["draft_model"]
            lines += ["### MTP 权重", "", "用上述相同镜像和源码 checkout 的 tools/export_mtp.py 生成新的 draft，挂载原始模型与本轮目录。使用同样的 Docker 资源参数，将构建命令替换为以下容器内命令：", "",
                "```bash", shlex.join(["python3", work + "/xllm/tools/export_mtp.py", "--input-dir", row["model"]["path"], "--output-dir", shlex.split(draft)[0], "--model-type", p["mtp"]["model_type"]]), "```", ""]
    else:
        lines += ["## 3 准备 vLLM 服务", "", "此 YAML 使用原生 external_service。先按指定 vLLM-Ascend 版本的官方说明用同一镜像新建服务容器，配置模型/设备挂载与服务环境；不要复用原容器。容器内启动参数为：", "",
                  "```bash", shlex.join([row["code"]["binary"], *vllm_arguments(row)]), "```", ""]
    if row.get("workload", {}).get("dataset") in {"gsm8k", "sharegpt"}:
        lines += ["## 数据准备", "", f"将随交付附带的 {cid}.prompts.txt 放到 {work}/datasets/prompts.txt。它是本次冻结的 UTF-8 prompt，每行一条；不要重新随机抽样或调用 workflow 转换。", ""]
    lines += ["## 4 运行原生 YAML", "", f"将 {cid}.yaml 放入新 checkout 的 tests/cases/，保持文件名与 case_name 一致。核对模型/Tokenizer、数据版本、EvalScope 版本和依赖。宿主机压测需在宿主机安装 EvalScope。", "",
        "```bash", f"python3 main.py --case_name {q(cid)} > outputs/test.log 2>&1", "```", "",
        "新测试容器由 xTask 自动命名创建。核对最终生成的服务与 EvalScope 命令、实际安装的本轮 wheel、请求成功数、长度及图/MTP 生效日志；保留 outputs、服务日志和全部 EvalScope 原始指标。",
        "原用例测量记录属于内部原环境；本原生交付用例为配置派生，独立硬件执行待验证。比较结论需在同等硬件和一致负载下核对，数值允许正常波动。", ""]
    return "\n".join(lines)
