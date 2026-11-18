# vLLM-Ascend 执行适配

用户指定 vLLM-Ascend 版本。code_profile 固定 version、vllm_version、image、binary；不默认 0.23 或 latest。prepare 可拉指定镜像并创建独立容器。run 在宿主机检查实际 image ID，在容器检查两个包的实际版本及 executable，再核对 CLI 能力。镜像依赖与主机驱动/CANN 按所选版本说明准备，依赖差异写入环境表。

framework=vllm-ascend 与 mode=direct 是完整的新增执行路径：生成 vllm serve 命令，单个入口进程管理 TP worker；记录 PID/starttime、模型 ready、真实生成 smoke、服务日志、停止和物理卡清理。生命周期使用 package 身份，不要求镜像带 xLLM Git 仓库、ELF 二进制或 xLLM build verdict。包 METADATA/RECORD 和 console entry、镜像、运行参数均保存；这些是测量身份，正式声明仍须完整证据门禁。

启动参数使用 vLLM 原生短横线名称：布尔开关 true 生成 --flag，false 生成 --no-flag；enforce-eager 为 store_true，false 不传，图关闭用 enforce-eager=true 或明确 NONE。字典参数如 speculative-config/compilation-config/additional-config 序列化为单个 JSON 参数。所选版本不支持时阻塞，不能静默丢选项。模型、served-model-name、dtype、host、port 由身份配置产生。

内置适配为同机 colocated TP，卡数为 devices 长度；DP/PP>1、EP、PD 或多机部署要单独实现和验证，不用标签假装已执行。两框架使用相同物理卡顺序，分组顺序执行，上一组清理未通过就停止。

MTP 使用本版本实际 method 和 token 数；图模式由实际 compilation-config.cudagraph_mode 识别，例如 FULL_DECODE_ONLY→decode。启动日志必须有按版本配置的 activation_checks.graph/mtp，验证启用；不能只靠 streaming Spec Accept 推断。内部 capture size、算子/通信融合等可分别调优并归档。示例参数不是所有 vLLM-Ascend 版本的默认值。

可选 mode=xtask 时使用 service_mode=external：workflow 按上述方式管理 vLLM 服务，xTask 仅通过 external_service 做 EvalScope。YAML 中 release_service_configuration 保留实际框架/服务配置作为复现元数据；所选 xTask 需支持 external service、session 和所需 EvalScope 参数。native vLLM 启动适配尚未验证，明确拒绝。此选项不能在 native 不可用时静默启用。用户已有 vLLM 服务的 ownership=external 身份尚未适配，须用 workflow-managed 服务。

参考官方 [Qwen3.5/3.6 部署说明](https://docs.vllm.ai/projects/ascend/en/v0.26.0rc/tutorials/models/Qwen3.5-27B-Qwen3.6-27B.html) 与 [MTP](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/speculative_decoding.html)。实施时切换到用户指定版本文档，并保存本轮 CLI/help 与激活日志；当前离线验证不代替对应版本的 NPU smoke。
