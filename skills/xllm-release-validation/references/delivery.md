# 报告、表格与复现

report 使用规范化 results.json 生成 CSV、完整原始指标/失败说明、report.md 和 reproduction.md。每个值可回溯到 case、attempt 和原始文件；reports 不产生新的压测或自动挑选重试中的最好一轮。内部 plan、实际脚本和身份记录继续归档。xTask 交付用例独立写到 delivery/cases，使用新建环境的路径；它与内部复用环境的执行记录分开，协议见 [fresh-reproduction.md](fresh-reproduction.md)。

发版交付还应提供 Word 适配交付报告，使用本 skill 的 010 源报告通用模板，按 [word-report.md](word-report.md) 准备叙述配置后，通过 report --docx-content 生成。它同时支持 xTask/direct 的规范化结果；不自动重跑测试。正式交付前渲染 DOCX 并检查所有页面，不能只以导出脚本返回 0 判定版式合格。

Excel 用 workbook-mapping.example.yaml 建立业务字段映射。rows 布局按表头和模型/配置/workload/并发匹配；多个结果命中一行时拒绝，先通过 case_selector 明确版本/重复轮次。columns 布局按特性行标签和目标模型列选择场景。布局变化只改映射，不改评测器。

保留原 workbook 的样式、公式、sheet 和已有内容；开关写 true/false/default，不写复选框。失败场景默认数值写 N/A，可显式 include_failed_metrics 展示原始观测，但仍需状态和来源。每个填入单元格附 case/status/attempt 注释。输出文件默认拒绝覆盖；--in-place 才替换输入，同时先备份。保存后重新读取校验。

交付检查：计划覆盖多少独立场景/重复几轮、实际成功/失败/阻塞/未执行、性能全部指标及分位数、精度样本和分数、版本回归与问题、完整复现入口、配置/日志/数据库保留位置。未进行真实硬件验证的适配器明确标注，不能将离线测试当作 NPU 稳定性结果。

代表场景按 framework/mode/kind 选择，默认优先 BF16、并发 1（不按最快结果选）。direct 展示实际 service/start.sh 和 eval.sh；xTask 仅展示新建环境 YAML 用例（通过 workflow reproduce 入口执行，不依赖原容器/编译产物），正文不展示启动/测试/内部生成脚本及基础 JSON。representatives 可指定确切 case_id，其他并发、量化、数据集及调优差异自动列出；所有场景性能结果和档案保持完整。Word 与 Markdown/复现文档遵循同一规则。Excel 在双框架相同模型/并发下须通过 framework、数据集、量化/轮次选中唯一结果，不混填。
