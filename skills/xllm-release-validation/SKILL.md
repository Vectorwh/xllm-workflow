---
name: xllm-release-validation
description: 编排 xLLM 版本发布的场景矩阵、性能与精度回归、结果验收、Excel 回填及复现交付，支持外部 xTask 和 direct 两种执行方式及可扩展启动参数。用于明确的发版数据测试任务；普通单次评测、开放式优化、单独服务维护使用对应专项 skill。
---

# xLLM 发版数据测试

以版本、场景和验收目标组织测试，支持 xLLM 与用户指定版本的 vLLM-Ascend 性能比较。参数来自本轮配置，不把 011 的兼容处理当后续版本默认值。

后续 xLLM 发版测试默认使用 Python/PyTorch 模型实现：`service.flags` 显式设置 `model_impl: python`、`npu_kernel_backend: TORCH`、`enable_shared_expert_overlap: true`、`enable_static_kernel: true`，适用于 direct、xTask、性能和精度。显式覆盖及 matrix 值优先；目标版本须支持所配参数、模型和运行依赖，静态 kernel 要求 NPU 图模式，详见 [planning.md](references/planning.md)。不静默降级，也不追改历史已锁定计划及测量记录。

## 流程

1. 读 [planning.md](references/planning.md)，从版本规划/模型清单生成矩阵、资源和验收方案。框架 `xllm/vllm-ascend` 与执行方式 `direct/xtask` 分开配置；沿用用户已给出的执行授权。
2. 缺容器、源码、编译产物或 MTP draft 时读 [preparation.md](references/preparation.md)，执行 prepare。构建复用 `xllm-npu-build-gate`；draft 使用本次 checkout 的 `tools/export_mtp.py`。记录直接/增量编译、dirty diff、外置兼容处理及产物身份，不擅自改目标源码。
3. 数据准备和 EvalScope 配置读 [datasets.md](references/datasets.md)。CEval 用于精度，random/GSM8K/ShareGPT 用于性能；真实数据先冻结选样，报告实际长度。
4. direct 读 [direct.md](references/direct.md)，xTask 读 [xtask.md](references/xtask.md)，vLLM-Ascend 读 [vllm-ascend.md](references/vllm-ascend.md)。两种执行方式均使用 EvalScope。**不携带、不应用、不 cherry-pick、不修改 xTask 补丁**；所选版本无法表达必需配置时阻塞，不切换成 AISBench 或静默切换执行方式。
5. 比较前读 [comparison.md](references/comparison.md)。关键配置保持一致，允许记录后的框架独立调优；运行日志验证图模式/MTP 生效。正式收益声明仍交给 `xllm-npu-benchmark` 的公平性和证据验收。
6. 执行/恢复读 [execution.md](references/execution.md)，测量验收读 [acceptance.md](references/acceptance.md)，交付读 [delivery.md](references/delivery.md)、[native-delivery.md](references/native-delivery.md) 与 [word-report.md](references/word-report.md)。内部可复用已有环境；交付原生 xTask YAML 及原生准备/测试说明；接收方无需 workflow 或原容器/产物。相似故障再读 [lessons-011.md](references/lessons-011.md)。

## 命令

使用实际 `<repo_root>`，Python 需要 PyYAML；Excel 需要 openpyxl，Word 需要 python-docx。复制 assets 中示例为本轮本地配置，固定真实版本与路径。`release.example.yaml` 保留基础测试示例，`release-comparison.example.yaml` 提供 CEval、真实数据和双框架配置。

```bash
python <repo_root>/skills/xllm-release-validation/scripts/release.py plan --spec /path/release.local.yaml --run-root /shared/runs/release-012
python <repo_root>/skills/xllm-release-validation/scripts/release.py prepare --run-root /shared/runs/release-012
python <repo_root>/skills/xllm-release-validation/scripts/release.py run --run-root /shared/runs/release-012
python <repo_root>/skills/xllm-release-validation/scripts/release.py resume --run-root /shared/runs/release-012
python <repo_root>/skills/xllm-release-validation/scripts/release.py collect --run-root /shared/runs/release-012
python <repo_root>/skills/xllm-release-validation/scripts/release.py report --run-root /shared/runs/release-012 --docx-content /path/delivery-report.local.yaml
python <repo_root>/skills/xllm-release-validation/scripts/export_workbooks.py --results /shared/runs/release-012/results.json --mapping /path/workbook-mapping.local.yaml
```

`release.py` 仅作内部批次编排。`plan` 不连接机器、不创建容器、不安装依赖、不编译；`prepare` 只执行声明的准备项，`run` 才运行测评。没有准备项且使用 random/已有环境时可直接 run。计划锁定后不改身份；调整配置创建新计划。所有 attempt/日志保留，失败不选最好一轮覆盖。

## 必须核对

- 控制端、远程主机和容器共享同一绝对路径的 workflow、run root、源码和所需权重；非共享环境先在目标宿主机部署 workflow。核对真实物理卡映射、CPU、端口、设备空闲及 PID 所有权。
- xLLM 消费 PASS build verdict；vLLM 镜像核对实际 image ID、vLLM/vLLM-Ascend 包版本和 CLI。新开关放在框架原生 `service.flags`、环境变量放 `service.env`，目标版本须支持；未配置为 default，显式 false 不丢失。
- 不因工具返回 0 判通过：核对正式请求总数、失败数、请求级长度、单位、服务激活和清理。图/MTP 等关键项不能靠同名参数或配置文本认定生效。精度数据集、提示词、样本、生成参数和阈值显式配置。
- 内置服务适配范围是单机 colocated TP。多机/PD/DP/EP/PP 需对应真实适配器，不能仅加标签就称已支持。固定并发和原生 open-loop rate 已有入口；固定间隔/SLA 摸高委托 benchmark 设计客户端，不把 rate 称为固定间隔。

## 输出

输出锁定计划、准备记录、每次脚本/环境/日志、原始性能 summary/percentile/DB 或精度报告、规范化结果、跨框架比较、CSV/Markdown/Word、Excel 和复现入口。

交付执行内容：**direct 每个框架/测试类别展示一个代表场景的实际脚本，优先 BF16 并发1；xTask 展示原生 YAML 和原生准备/测试说明，不展示内部启动/测试脚本。** 全部用例及同名 .md 说明保存到 delivery/cases；镜像、完整源码 commit 写明，接收方使用 xTask 自身构建和 main.py 入口，无需本 skill、workflow 或 release.py。YAML 不带私有扩展、路径占位符、原容器名或原编译路径；镜像等原生 JSON 配置在说明中明确。内部实际记录继续归档，派生用例未独立实测时须标注。其他场景用差异表说明。Word 使用 [模板](assets/templates/release-delivery-template.docx)，交付前渲染并逐页检查。Excel 使用业务映射和 true/false/default。

本适配器的本地测试不等于硬件发版验收；记录所选版本的真实 smoke/性能/精度结果后才能声明该环境可用。完成后由 lifecycle validate/finalize/retention 收口；正式证据不完整时保留 measurement-only。通用仓库不保存实际账号、内部地址和私有日志。
