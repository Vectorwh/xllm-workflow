# 规划与配置

配置入口见 `assets/release.example.yaml` 和 `assets/release-comparison.example.yaml`，environment/preparation 文件相对该 YAML 解析。其他执行路径使用绝对路径。版本固定完整 commit；不使用浮动分支名代替产物身份。

先从发版规划/模型清单确定必测矩阵与基线，再列出环境准备缺口：机器和实际物理卡、已有/新建容器、驱动/CANN/ATB/PyTorch/自定义算子、源码来源与 commit、权重/Tokenizer/量化方式、EvalScope 和数据集版本。新容器须记录镜像 digest、devices、挂载、网络、IPC、共享内存及环境变量；参考容器仍被使用时，在新容器和独立 checkout 中准备，不覆盖原任务。

优先核对已有权重；缺失时从用户指定的模型源获取并固定 revision，记录 BF16/量化权重及 MTP draft 的目录和身份，不能仅凭目录名认定同一模型。源码按 release commit 检出，构建步骤交给 build-gate，保留直接编译/增量编译命令、源码 diff、外部兼容处理、工具链及可执行文件证据。计划先声明准备输入和产物路径，prepare 创建容器、取源码、编译、导出 MTP 并冻结真实数据；run 消费准备结果。主模型权重先检查本地，缺失时按用户指定源固定 revision 获取并记录。run 不修改源码。

配置层次：公共 defaults → code/model/service/workload/accuracy profile → case 覆盖 → matrix 展开。执行方式由 case.mode 覆盖 execution.default_mode。xLLM 启动参数为开放的 bool/number/string 字典，vLLM 接受原生短横线参数和 JSON 字典；身份参数 model、model_id、draft_model、nnodes、rank、端口由调度器控制，避免两处来源互相覆盖。

xLLM 新计划未指定 npu_kernel_backend 时显式补为 TORCH，即默认 PyTorch 启动；defaults/profile/case/matrix 中的显式值优先。该默认同时进入 direct 脚本与 xTask serving_extensiable_param，不作用于 vLLM-Ascend，也不追改历史计划和交付配置。backend=llm/vlm 是模型类别；本次默认选择的是 npu_kernel_backend。目标版本必须支持该参数与模型路径，否则阻塞并说明。

matrix 支持 `service.flags.*`、`service.env.*`、`workload.*`、`accuracy.*`。`workload.parallel` 可直接写列表。每种组合及 repeat 都有唯一 case id，number 是该档正式总请求数，不乘并发。打印展开后的数量和服务组，避免无意执行巨大的笛卡尔积。所有默认值、显式关闭和未设置的开关分别保留；未设置值导出为 default，不臆测真实开关状态。

在任意 case 中可添加以下覆盖；新参数无需加入固定白名单，但目标版本必须在其 CLI help 中声明支持。修改已有值就做单场景验证，添加 matrix 才产生多组测试：

```yaml
service:
  flags:
    new_batch_limit: 16
    new_backend_option: example
matrix:
  service.flags.enable_new_feature: [true, false]
  service.env.NEW_RUNTIME_OPTION: ["mode_a", "mode_b"]
```

`plan` 使用新空目录，拒绝覆盖旧计划。生成 preview/start.sh、eval.sh、xTask case YAML 和每 case 的 canonical experiment.yaml；`plan.json` 是锁定的批次来源。修改配置需要新 plan。准备能力检查在 prepare、运行身份检查在 run 执行，计划生成不触碰 NPU。

输入版本规划/特性清单时先用业务字段建立场景映射：模型、权重身份、dtype/quantization、TP、MTP、图模式、长度、并发、测试类别。精度分数与量化配置分开。功能验证需独立的行为证据；当前自动脚本执行性能和精度，功能项由对应专项测试提供证据后交付，不因 flags=true 就填 PASS。

一次 code 产物变动须重新执行 build-gate；复用报告需核对 binary_provenance 的 path/SHA256。记录外置兼容处理的内容与哈希；构建后生成内容引起的 dirty 工作区可在确认来源后显式 allow_dirty，并保留 diff。模型完整哈希可写 model.sha256；仅有元数据哈希的测量不升级为 formal 模型身份一致性结论。

跨框架测试由 case.framework、code_profile、comparison_group 指定；vLLM 版本不能被 release.commit 覆盖。配置一致性见 comparison.md，部署与准备见 preparation.md。
