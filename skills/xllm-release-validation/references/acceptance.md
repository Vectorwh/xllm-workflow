# 数据验收

每个 case 保存 execution_status、measurement_status、cleanup_status、最终 status。EvalScope rc=0 不能覆盖 failed_requests>0；xTask 后处理失败与真实压测失败分别展示。保留全部 summary、percentile、原始请求 DB、精度报告和服务日志。

性能规范字段遵循 perf-artifact-schema.md；字段缺失使用 N/A/INCONCLUSIVE，不用零填补。支持旧版秒字段及新版 Avg TTFT/TPOT/ITL(ms)；summary 的 ms 不再乘 1000。DB 内延迟秒字段重算时才换成 ms。原始 summary/percentiles 完整保留，不能只保留挑选后的好看指标。

正式总请求数为 workload.number，warmup 不计入正式数据。strict_lengths=true 时必须有 benchmark_data.db 且每条成功请求符合输入输出；数量与 summary 不一致则不足以通过。请求全部成功、材料完整后才评估 maximum 中指定的延迟上限。warmup=0 只能作为冷启动观测，不升为稳态 PASS。

精度由 datasets、dataset_revision、dataset_args、generation_config、limit 确定。report_glob 与 JSON pointer score_pointer 显式给出，避免不同 EvalScope 版本结构变化时猜错分数。minimum_score 缺失时记录分数，状态 INCONCLUSIVE。量化方案是模型身份，不等于精度分数。

历史比较先匹配 case，再检查 model/service/workload/accuracy/environment/mode；任一不同或测量失败时为 REFERENCE_ONLY。代码版本是跨版本比较的预期变量；仍需保留二进制和权重证据、固定设备及未变化配置。comparison.json 的数值差分不是 formal 性能声明，发布前消费 benchmark fairness verdict。

功能支持要有专项行为测试；开关 true/false/default 只代表配置。固定 interval 与随机 open-loop rate、稳态与 profiling、不同请求数/接口格式不直接混比。

真实数据记录来源、固定选样和准备文本哈希，按请求 DB 统计实际 input/output tokens 的 min/max/mean，不用 input_tokens 预算冒充固定输入。跨框架比较另执行 comparison.md：关键参数相同、真实物理卡/主权重/客户端与请求身份一致、图和 MTP 激活证据齐全后才是 COMPARABLE_MEASUREMENT，正式结论仍委托 benchmark。
