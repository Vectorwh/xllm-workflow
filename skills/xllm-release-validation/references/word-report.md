# Word 适配交付报告模板

模板由 010 发版的模型适配交付报告整理而来。保留 A4 页面、居中标题、分级编号、蓝色表头，以及业务背景、PR、特性、部署、代表场景执行配置、模型评测、多模态评测的主线；移除原报告的个人水印、作者账号、旧 PR、旧模型数值和历史结论。另补充产物身份、场景覆盖、平均 E2E、已知限制、证据索引及复现入口。后续 012/013 等发版均复用本模板，版本从结果数据读取。

## 文件与生成方式

- `assets/templates/release-delivery-template.docx`：可直接打开填写的 Word 模板，也是自动生成器的版式来源。
- `assets/delivery-report.example.yaml`：业务叙述、PR、特性、部署和代表场景选择的填写示例；复制成自己的本地文件，填完后再导出。
- `scripts/export_delivery_docx.py`：读取本次 results.json 和叙述 YAML，填入模板并扩展数据行；只读证据，不执行脚本。

Python 环境需要 PyYAML 与 python-docx。使用工作区/环境提供的固定 Python 与依赖，记录实际版本。可独立执行：

```bash
python <repo_root>/skills/xllm-release-validation/scripts/export_delivery_docx.py \
  --results /shared/runs/release-012/results.json \
  --content /shared/runs/release-012/delivery-report.local.yaml \
  --output /shared/runs/release-012/xLLM-NPU-适配交付报告.docx
```

也可调用 `release.py report --run-root ... --docx-content ... --docx-output ...`，先汇总本批次，再生成 Word。输出路径必须为新文件，避免覆盖已交付版本。没有 `--docx-content` 时，原 Markdown/CSV 生成行为仍可单独使用；完成发版交付时需补齐 Word。

## 内容与证据如何映射

| 章节 | 数据来源及填写要求 |
| --- | --- |
| 标题和版本 | report.title/models/author/date；版本、commit、vLLM/vLLM-Ascend 版本与镜像身份、场景状态只取 results.json |
| 业务背景与结论 | report.background/release_scope/conclusion；明确覆盖范围、通过、失败、阻塞和未执行 |
| PR 与特性 | tables.prs/features 和 report.feature_details；特性配置使用 true/false/default，验证结论带证据 |
| 环境与部署 | tables.environment/deployment；记录机器、物理卡与容器卡映射、CPU、镜像 digest、容器及挂载、软件栈、权重版本 |
| 构建与准备 | blocks.build_record 和 report.mtp_preparation；写清直接或增量编译、dirty diff、兼容修改、二进制 SHA256、导出工具版本与 draft 身份。纯 xTask 报告不展示容器/转换命令 |
| 代表场景 | direct 从实际 attempt 读取启动与 EvalScope 脚本；xTask 从锁定配置派生原生 YAML，附同名原生准备/测试说明；不展示内部脚本或基础 JSON |
| 配置差异 | 自动对比其他场景与本框架/方式/类别的代表场景，覆盖并发、量化、数据集、启动和调优配置 |
| 精度评测 | 方法取叙述文件，实际脚本/YAML 取代表场景；分数取 accuracy cases 的 metrics.scores；保留原计分单位，不默认将 0–1 转成百分数 |
| 性能评测 | 场景、请求数、均值 E2E/TTFT/TPOT/ITL、QPS、吞吐从 performance cases 读取，状态和 attempt 来源一同展示 |
| 框架或历史比较 | 跨框架表来自 framework_comparisons，不能手填覆盖；其他历史比较可填 tables.comparisons。保留公平性状态及调优差异 |
| 多模态 | 可选单独验证的结果、方法与脚本；模板支持整理结果，不表示当前 release runner 已支持 random_vl |
| 问题与交付 | tables.risks/artifacts、report.limitations/reproduction；归档完整原始指标、DB、配置和复现文档 |

正文中的 `{{NAME}}` 是文本槽位；表格中的 `{{GROUP.field}}` 是可重复数据行。不要删除模板中的结构标记或将字段拆成不同语义。通常修改 YAML 即可；新增章节或修改表格列时，同步更新模板、生成器和回归测试。

## 脚本和可选章节

representatives 可填写确切 case_id 列表；未给时按 framework/mode/kind 分组选一个代表，优先 BF16 并发 1，保留计划顺序，不按结果快慢筛选。direct 读取代表 attempt 的 service/start.sh（或 service-reference 指向的同组服务）和 eval.sh；xTask 生成 delivery/cases 中的原生 YAML 和同名 .md 说明，不展示内部复用容器的 attempt/xtask/tests/cases/release_case.yaml。缺 direct 实际档案或 xTask 原生交付元数据时默认拒绝正式导出；仅 allow-incomplete 可生成待补充稿。接收方用原生 xTask 构建及测试入口，不需要 workflow/skill/release.py；生成不代表已通过独立硬件复测。

xTask 场景展示原生 YAML 及原生准备/测试说明位置和要点，不展示内部启动脚本、测试脚本、生成脚本或基础 JSON；混合执行方式时各组按对应规则展示。纯 xTask 文档也移除容器创建脚本槽位。全部内部材料仍归档，报告结果表仍包含全部场景。构建/转换写身份和准备结论，不在纯 xTask 文档里再展开命令。

blocks 只用于构建记录和可选准备/外部多模态材料，file/text 二选一；文件只读。启动和测试脚本不再由叙述 YAML 手工粘贴替代，direct 必须来自实际归档；xTask 使用原生 YAML 及配套原生工具说明，详见 [native-delivery.md](native-delivery.md)。

`include_multimodal: false` 会整段删除多模态章节并保持编号连续；启用后可提供 multimodal_accuracy/perf 表。`include_raw_metrics: true` 会将所有原始 summary 和 percentile 叶子字段逐项附在 Word 尾部，保留字段名及层次路径；默认正文只展示核心指标，完整原始数据始终随 results.json 和原始目录归档，不能丢弃。

空值是 N/A，不补成 0。失败场景的原始观测仍显示，但保留失败状态，不能用于通过结论。默认拒绝必填叙述缺失、未填写的示例占位符，以及从叙述文件覆盖自动测量表；仅临时检查布局时可用 `--allow-incomplete` 生成待补充稿，该稿不能作为正式交付。

## Word 交付检查

用 Documents 技能的 render_docx.py 将最终文件渲染成逐页 PNG 并全部检查：中文字体完整、表头跨页重复、无截断/越界/重叠、标题与表格不孤立、脚本保持完整、页码连续。使用其运行时自带的 LibreOffice；字体不足时在任务临时目录配置字体搜索路径，不修改用户的全局字体或插件缓存。不得用 LibreOffice 另存为覆盖模板来刷新字段。自动生成文件设置 updateFields，Word 打开时可更新页码。

最后核对表格中的计量单位、请求数、失败数、平均 E2E、源脚本与 case/attempt；不要复制 010 报告的得分或默认参数。报告正文中的真实机器/内部路径只留在本地交付物和 run root，通用模板与示例不保存这些实际值。
