# xTask 新建环境交付

内部执行允许复用已有容器、指定二进制和已有服务；这些实际用例、JSON、脚本、身份和日志继续存于 case/attempt。对外交付的 YAML 独立生成到 `delivery/cases/<case_id>.yaml`，不能把内部的旧容器名、二进制路径作为复现前提。报告每组只展示一个代表 YAML 和其他场景差异，归档保留全部交付 YAML。

## 配置输入

从 `assets/delivery-reproduction.example.yaml` 复制 `delivery.xtask_reproduction`，可在 release 根级声明，也可按 case 覆盖。它与内部 environment/code/preparation 分开：

- image：固定镜像 tag，正式交付优先 digest；container：完整设备、HostConfig、环境和挂载，不允许 reference/mode/name。工作流自动挂载新的 run root 与 workflow 自身。
- source_url：xLLM 仓库；release.commit 为完整源码 SHA；xtask_url 与 xtask.commit 固定 xTask 版本。原有 checkout 不会成为输入。
- build：相对新 checkout 的 binary/build_dir，以及来自该 commit 构建说明的 gate_args。`@@REPO@@` 指新 checkout。新目录无旧缓存，经过 build gate 得到新 verdict/provenance。
- sources/python/host_python：环境加载脚本与解释器；setup：显式 argv 安装步骤及 context，例如按固定 xTask commit 的 requirement.txt 安装。镜像内缺少的构建工具或服务依赖须在此补全；禁止依靠原容器的手工安装。
- mtp.model_type：配置 draft 时必填，使用新 checkout 的 tools/export_mtp.py 生成到新 run root，不能复用原 draft 目录。
- task_config/xllm_config/vllm_config：需要的有效基础配置，随 YAML 内嵌；复现以 exact_config 生成隔离 JSON，不继承外部 checkout 的个人默认值。运行时容器名、路径映射由新环境注入，不需要再手交两个 JSON。
- resources：原始权重和数据集可使用目标机已存在的固定路径，也可填写 path/hub/repo_id/revision/sha256 下载并验身份。hub 为 modelscope 或 huggingface，SHA256 是相对 path 的文件名映射。下载 SDK 须由镜像/setup 提供；私有仓库凭据从本地环境获取，不写 YAML。

新开关仍使用 service.flags/service.env；模型、长度、请求数、预热、并发、采样、精度参数原样保留。调整镜像、构建依赖或新增启动配置后必须核验版本能力。物理卡、容器可见卡、CPU、端口和设备挂载须在接收方机器上核对，不能把一个机器上的设备映射视为跨机器通用。

## 执行入口与边界

交付 YAML 上半部保留原生 xTask 用例字段，`release_reproduction.spec` 声明新建准备流程，同时记录 workflow 仓库及 commit。这是 **workflow 扩展**，原生 xTask 不会解释它。本 skill 不修改 xTask 源码。接收方先获取 YAML 指定的 workflow commit，在目标 NPU 宿主机运行：

```bash
python <workflow>/skills/xllm-release-validation/scripts/release.py reproduce \
  --yaml /path/delivery-case.yaml --run-root /shared/runs/new-reproduction --plan-only
# 审阅计划后，在另一个空目录去掉 --plan-only 执行准备与测试。
python <workflow>/skills/xllm-release-validation/scripts/release.py reproduce \
  --yaml /path/delivery-case.yaml --run-root /shared/runs/actual-reproduction
```

plan-only 不连接设备、不执行 Docker/Git/安装/编译。正式执行顺序：新容器 → 新 xTask/xLLM checkout → 依赖安装 → 原始资源检查/下载 → 编译 → MTP → 冻结真实数据 → xTask 测试 → 结果报告。xLLM 的原生 xTask 会接管**本轮刚创建的容器与刚编译的产物**；这不是依赖原测试容器。vLLM 的 xTask 场景则新建指定镜像、由 workflow 启动服务，再通过 xTask external_service 执行 EvalScope，不编译 xLLM。

YAML 校验摘要用于发现准备块与原生用例被分别修改；更改配置需从 release spec 重新生成。新 run root 必须为空，自动派生独立容器名；同一原有容器不会被停止或重建。准备失败的日志保留，可按本轮 plan 用 prepare/run 恢复，不重复使用 reproduce 覆盖已有目录。

## 交付验收

镜像/源码/构建命令必须补齐才能严格导出 Word；缺配置只能生成待补充稿，不回退展示内部复用 YAML。生成文件默认保留并拒绝覆盖变化的用例。修改代码后先提交 workflow，再生成最终交付，确保 YAML 的 workflow commit 包含本能力。

派生 YAML 的 validation 初始为 `configuration-derived; independent-hardware-replay-pending`。原环境测试结果仍属于原 case/attempt；只有在新容器重新执行并保留准备、构建、性能/精度与对比证据后，才可认定新建流程已通过真实复现。离线测试仅验证配置转换、执行顺序和报告内容。
