# EvalScope 精度和性能数据集

精度、性能均使用固定版本 EvalScope。xTask 配置 benchmark_type=evalscope，dry generation 后核对真实命令；不能将 AISBench 的数据预处理能力当成 EvalScope 已支持。

## CEval 精度

accuracy_profiles 使用 datasets: [ceval]、dataset_revision、dataset_args、generation_config、batch_size、report_glob 和 score_pointer。dataset_args.ceval 明确 dataset_id（固定本地数据或源）、few_shot_num、few_shot_random 和需要的 subset_list；thinking 开关及答案过滤/提取按模型与 EvalScope 版本配置并归档。全量测试不设置 limit；smoke/子集显式 limit，不能当全量结论。

配置实际最低分数 acceptance.minimum_score；未配置仅记录 INCONCLUSIVE 观察值。score_pointer 必须在所选版本真实生成的 CEval 报告上验证，保留 aggregate、分类分数、预测与失败样本，计分单位不擅自转换。数据版本、固定数据内容、请求顺序、模板和 answer extractor 同精度 evidence 一起保留。

## GSM8K 和 ShareGPT 性能

配置 dataset=gsm8k/sharegpt、dataset_revision、dataset_path、prepared_path。GSM8K 来源支持 JSON 数组或 JSONL 的 question 字段；ShareGPT 支持 conversations(from/value) 或 messages(role/content)，sharegpt_turn 明确 first_user 或 last_user。这是单轮性能请求，不进行 GSM8K 答案评分，也不是 ShareGPT 多轮会话吞吐。

prepare_dataset.py 先固定源文件 SHA256，用 seed 洗牌并选择 number+warmup 条，保存原样本索引及转换清单。文件可预置本地；缺失时仅在给出 source_url 与 source_sha256 后下载并验证，源 URL 应固定版本。样本不足默认报错，allow_repeat=true 才循环补足并注明重复。两框架复用同一 prepared_path。

EvalScope custom 插件消费“一行一个原始 prompt”，不是带 question/messages 的 JSONL。转换显式折叠 prompt 内部空白，写为 UTF-8 文本并保存 .manifest.json；命令为 perf --dataset custom --dataset-path <prepared_path>。运行前检查所选 EvalScope custom 插件支持该格式及实际生成的请求。xTask 若不透传 dataset-path，必须 BLOCKED，禁止退回 AISBench或 random。

length_policy：native 保留自然长度；filter 按 min_prompt_tokens/max_prompt_tokens 筛选；truncate 对单条 prompt 按 input_tokens 截断、不自动填充/拼接。后两种使用固定本地 Tokenizer，默认不执行远程模型代码。prepared 文本 token 长度与请求 chat template 后的线长可能不同，strict_lengths 必须 false，输入长度声明为预算/策略而非每条精确值。输出 max/min tokens、ignore_eos 和实际生成长度都记录；ignore_eos=true 时默认逐条核对输出 tokens；strict_output_lengths=false 才显式允许自然输出，并在报告中说明。

## random 和请求验收

保留 random 的固定输入/输出、固定并发及 EvalScope 原生 open-loop rate。number 是正式请求总数，不乘并发，warmup 不计入正式数。warmup、seed、接口、stream、tokenize_prompt、temperature、EOS 和长度处理在框架间相同。

真实数据必须有请求 DB，保存实际 input/output tokens 的 min/max/mean 和失败数。random strict_lengths=true 按每条请求校验，不能只看平均值。完整 summary、percentile、DB 与冻结数据清单全部保留。

接口依据：[EvalScope custom 插件](https://github.com/modelscope/evalscope/blob/main/evalscope/perf/plugin/datasets/custom.py)、[性能参数](https://github.com/modelscope/evalscope/blob/main/evalscope/perf/arguments.py)、[精度参数](https://github.com/modelscope/evalscope/blob/main/skills/evalscope/eval-reference.md)。这些 main 链接仅用于查找；实际执行以记录的安装版本和 help/原始请求为准。
