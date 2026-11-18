# 原生 xTask 交付

内部测试可复用已有容器、服务和二进制；实际 JSON/YAML、脚本及日志按 case/attempt 留档。release.py 只用于内部批次编排。接收方拿到原生 YAML 和同名说明后，用固定版本 xTask 的工具准备源码、编译、安装和测试，无需 workflow 或发版 skill。

## 交付内容

使用 assets/delivery-native.example.yaml 配置内部生成元数据 delivery.xtask_native：公开镜像完整版本、源码 URL、xTask URL、接收方的新工作目录、镜像环境脚本及 MTP model_type。完整 xLLM commit 从 release.commit 读取，xTask commit 从 xtask.commit 读取。此映射只用于生成，不放进交付 YAML。

每个场景输出 delivery/cases 下的 .yaml 与 .md；真实性能数据另附 .prompts.txt 冻结样本。YAML 只有所选版本 xTask 原生字段，镜像和源码版本用注释记录。禁止私有 release_* 配置块、路径占位符、原容器名、原二进制/Triton build 路径。新测试容器由 xTask 自动创建和命名，不设置 container_name 或 reuse_existing_container；安装本轮新编译的 wheel，Triton 路径按已安装包发现。

镜像由原生 config/xllm_config.json 读取，不能只写 YAML 注释就宣称已生效。配套说明给出 JSON 的确切覆盖值，要求清空 serving_container_name、设置挂载/资源目录并核对最终使用的镜像。不会复制原机器的个人 JSON，也不伪造 xTask 不支持的镜像/源码 YAML 字段。模型/数据可在目标机已有路径准备或联网获取固定版本，无需复制原测试环境。

## 原生准备和执行

1. 在目标 NPU 宿主机新建 /export/home 下的 xTask checkout，固定完整 xTask commit，按 requirement.txt 安装依赖。内置原生脚本依赖该路径挂载约定；其他布局需按所选版本文档核对。
2. Git 获取指定 xLLM commit、递归初始化 submodules 并核对 clean。原生 prepare_xllm.sh 支持仓库/分支或新 checkout 的 tag，不能把任意 SHA 当分支。精确 commit 路径使用 Git detach 检出后调用原生 tools/build_xllm_in_container.sh，避免外层 prepare 的 pull 推进版本。构建镜像使用与服务一致的指定版本。
3. 构建 Docker 参数以所选 xTask 的 prepare_xllm.sh 为依据，挂载本轮工作区、驱动/设备和本轮缓存；WORK_ROOT 指向新 xTask 工作区。记录源码 SHA、构建日志、wheel SHA256 及 vendors/runtime_libs/third_whl。新 checkout 无旧 pkg/xllm，失败不使用其他任务的旧包。
4. 使用新源码的 tools/export_mtp.py 生成 draft。原始权重与输出目录按说明挂载；不引用原 draft。真实性能数据直接交付冻结的原始 prompt 文本，接收方无需运行 workflow 数据转换脚本。
5. 将 YAML 放入 tests/cases，以 python3 main.py --case_name 执行。xTask 新建测试容器、使用 download_xllm.sh 安装本轮 pkg/xllm，随后启动并运行 EvalScope；宿主机压测时 EvalScope/Tokenizer 依赖须在宿主机可用。
6. 核对图/MTP 生效、服务参数、生成的 EvalScope 命令、成功数、实际长度和全部原始指标。CEval/真实数据路径透传能力不足则阻塞，不能静默改变数据或测试工具。

原生脚本可能带版本相关的代理、下载源、依赖资源或设备路径要求。说明须列清这些前提及本轮已验证范围；不得将联网可拉取镜像等同于编译/评测依赖在任意网络均可获取。禁止为此自动修改 xTask 源码或捆绑补丁。

vLLM 的 xTask 用例使用原生 external_service；同名说明写明指定镜像下新建 vLLM 服务及启动参数，YAML 不带 workflow 的 release_service_configuration。不能称 xTask native 已负责 vLLM 服务启动；如果所选版本存在可验证的原生启动适配，另行接入。

## 报告与验证

报告每个 framework/mode/kind 展示一个原生 YAML（优先 BF16 并发1），附原生准备/执行说明位置和关键步骤；不展示内部启动/测试脚本。其他场景差异与全部 YAML/说明仍归档。原始内部结果与派生交付用例分开标注，未在新容器执行时不得称已通过独立硬件复测。

验证需检查 YAML 可被固定版本 xTask 加载、生成服务与 EvalScope 命令、原生新建路径未被 reuse 字段触发，以及说明的 shell 语法/commit 锁定。离线检查不启动 Docker/NPU，不替代真实复测。旧私有扩展交付物保留历史档案，重新导出用新目录，不能覆盖已交付文件。
