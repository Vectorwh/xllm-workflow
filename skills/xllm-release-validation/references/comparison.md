# xLLM 与 vLLM-Ascend 配置一致性

为成对性能 case 指定相同 comparison_group。同一 group、dataset、parallel、rate、repeat 必须恰好有 xLLM/vLLM 各一条；计划阶段核对共同语义和 workload，差异直接报错。不同长度/精度矩阵使用不同 group；不能靠 group 名字绕过核对。

共同约束：主模型/Tokenizer 路径与权重内容、dtype、量化方案、同主机 resource_pool_id、相同物理卡顺序与卡数、部署方式/TP、MTP 是否开启和 draft token 数、graph 是否开启及覆盖阶段、chunked prefill 与 prefix cache 开关，以及全部请求生成参数。xLLM graph_phase 明确声明 decode/prefill+decode；vLLM 从 compilation-config 的明确 mode 识别；有歧义就阻塞。两个框架的 MTP 原生权重格式可不同，但必须来自同一主模型、同一算法场景，不能更换模型质量换速度。

允许差异：框架/镜像及匹配依赖、原生 MTP 加载格式、端口、CPU 绑定、图 capture size、batch/token budget、通信/算子融合及其他明确记录的内部调优参数。关键开关仍要相同；预算、调优和栈差异在 framework-comparison.json 与报告中逐项记录，不将“允许调优”用于改变请求或部署场景。

run 检查实际 native CLI、权重/Tokenizer 哈希、ACL 物理卡映射和宿主机快照。activation_checks 使用所选版本精确的日志模式确认 graph/MTP 真正激活；缺失/不匹配则不压测该组。所有测量使用同版本客户端和相同冻结数据/seed；完整权重哈希缺失、激活证据缺失、配对缺失、执行失败或运行身份不同，比较标 REFERENCE_ONLY，不输出性能收益百分比。

COMPARABLE_MEASUREMENT 仅代表本适配器的配置和测量检查通过。正式发版对外百分比声明还须交给 xllm-npu-benchmark 验证公平性、Run Evidence Gate 和准确率；不自动从这一个状态升级 formal。归档全部 EvalScope 指标和分位数，平均 E2E 必须在核心比较表展示，不能只挑 QPS。
