# CLAUDE.md

核心规则、Skill路由、目录说明请读取 [AGENTS.md](AGENTS.md)。

本文件仅记录 Claude Code 工具专有的补充约定（如有）。

发版容器/构建/MTP 准备、xTask/direct 数据矩阵、指定版本 vLLM-Ascend 比较及交付统一使用 `skills/xllm-release-validation/SKILL.md`，路由与约束以 AGENTS.md 为准。
内部测试可复用已有环境；交付 xTask YAML 使用 workflow reproduce 入口新建容器、拉取固定源码并编译，不依赖原容器/产物。
