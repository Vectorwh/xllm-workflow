# 环境与产物准备

`release.py prepare --run-root ...` 消费锁定计划中的 preparation。示例见 `assets/preparation.example.yaml`。准备状态写入 preparation.json，每轮日志保存在 preparation/attempt-NNN。prepare 不执行性能/精度测试；声明准备项或真实数据输入时，run 必须先看到同一 plan 的准备 PASS。

按 containers → sources → setup → resources → builds → mtp → 数据集冻结执行。setup 是显式 argv 安装步骤，resources 可校验已有原始权重/数据或按固定 revision 从 ModelScope/Hugging Face 下载，下载 SDK 由镜像/setup 提供。每项可给 context（ssh_host/container/python/sources/env）；未给时使用 environment.host。远程主机需要先部署 workflow 并共享本轮绝对路径；新容器必须挂载 workflow、run root、源码及权重所需目录。依赖缺失时按固定 checkout/镜像的文档安装固定版本并留安装记录，再执行准备，不自动升级包。参考容器含可写 CANN/OPP 共享挂载时，先为本轮配置独立安装路径和环境；独立容器名不能证明算子安装已隔离，仍复用 build gate 的主机级 OPP 锁。

## 容器

containers.mode=create 使用指定 image 或 reference 容器。参考模式复制 User、Env、WorkingDir、Volumes 和 HostConfig 等运行约束，再应用显式 env/binds/host_config 覆盖；不复制原业务进程，默认启动 sleep infinity。必须审阅生成的 create-request.json，尤其是设备、CPU/内存、网络、IPC、共享内存和挂载。端口映射必须显式提供新 PortBindings，防止占用参考容器端口；保留参考容器及其任务。

Docker Engine 通过配置主机的 Unix socket 创建容器；创建前可显式 pull，最终固定实际 image ID，保存完整容器和镜像 inspect。正式 run 不启用 AutoRemove；不会删除/停止同名外部容器。仅同 recipe 标签的容器可在准备重试时复用，其他同名容器需换名字。mode=existing 只检查已有运行中容器及指定镜像，不接管其业务进程。网络、权限、设备需要符合该镜像所选版本官方说明；示例不是跨机器默认。内部可复用 existing/reference；交付 YAML 必须使用 image 新建且不含 reference，按 [fresh-reproduction.md](fresh-reproduction.md) 生成独立源码和构建路径。

## 源码和编译

sources 用 URL、独立 repo 目录、完整 commit 获取代码并初始化 recursive submodules。已有目录只能在 commit 一致、工作区干净时复用；不 reset、不清空旧 build。

builds 调用已有 build_gate.py --execute --require-npu。repo/build_dir/binary/run_root 和 gate_args 显式指定；configure/incremental/OPP 命令来自本次源码构建文档。门禁决定首次/重新配置/增量策略，保留源码差异、工具链、OPP 身份、命令、完整 build.log、verdict 和 provenance。把真实 verdict/provenance 路径同时配置到相应 code profile。已有 build.log 时拒绝覆盖；需要重编就使用新 build run_root 和新计划。已有合格产物可不声明 build，run 仍重新验证它。

## MTP

mtp 配置 repo、commit、input_dir、output_dir、model_type、python。调用该 checkout 的 tools/export_mtp.py --input-dir ... --output-dir ... --model-type ...。记录 exporter 的 SHA256、源码 commit、主权重哈希、命令、日志和 draft 文件哈希。

已有 draft 必须有相同来源的 release-mtp-manifest.json 且文件校验通过才复用；来源不明或转换失败留下的目录需检查并换新输出路径，不删除或覆盖。BF16 和 W8A8 从各自主权重转换，不能混用。目标模型或脚本 CLI 变化时先核对 --help；不把 qwen3_5 作为所有模型默认值。vLLM 使用所选版本实际支持的 MTP 加载方式，不强制使用 xLLM 导出目录。

准备成功后再次 prepare 返回已完成记录；run 仍做容器/包、源码/二进制、权重及数据哈希检查。修改准备输入/产物需要新 plan，不能改旧记录来继续。运行中断保留失败 attempt，未完成步骤按报错修正后显式恢复；构建输出根目录不能复用覆盖。
