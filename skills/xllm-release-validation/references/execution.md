# 执行、恢复与资源

run/resume 消费锁定 plan。控制端与各执行上下文需要共享绝对路径的 checkout、run root、源码及权重元数据；通过 shared-path token 检查。非共享的远程环境先在目标主机部署 workflow 再运行，不通过修改 manifest 伪造 preflight。

每 case 使用 experiment.yaml 和 xllm_flow preflight/run create；每个完成的尝试登记 canonical attempts.jsonl，失败保留原因。根 CHECKPOINT.md 是批次索引，case 的 manifest/ledger 才是该 case 的生命周期来源。identity.kind=release_validation 不套用性能优化的候选 ranking 要求。

执行器支持单机 colocated TP 的 xLLM/vLLM-Ascend。声明的容器、源码、构建和 MTP 准备由 prepare 完成，构建委托既有 build-gate；完整记录与锁定计划关联。多台机器可在独立配置/控制实例运行，使用共享 lease_dir 和统一 resource_pool_id 避免同一物理资源被两个发版任务预约；外部非合作任务仍由宿主机实时检查发现。

restart_policy: per_case 每个场景独立服务；reuse_group 只将相邻、同服务/环境/模式/重复轮次的场景合组，避免重排用户指定顺序。xTask 使用 native session API，direct 使用同一组 rank。native xTask 保有服务管理权；显式 external xTask 路径由 workflow 管理服务，xTask 只负责测评，不交叉接管进程。

第一次执行 run；中断后 resume。PASS 尝试必须通过已归档文件哈希核对才跳过；输入或产物身份变化需要新计划。组内部分完成、失败或中断，显式 resume --retry-failed 且 max_attempts 足够时，整组新建 attempt。不能重用 graph 状态或覆盖旧日志。

资源检查/身份/能力缺失记 BLOCKED，运行故障记 FAILED，材料不足记 INCONCLUSIVE。进程超时、连接断开后先检查真实服务和设备占用，再恢复。设备清理失败停止后续组。collect/report 不启动服务；原始测量与调度/解析错误分别保留。

结束后由 lifecycle validate/finalize/retention 收口；发布正式比较前使用 benchmark 的公平性和 evidence gate。大日志是否压缩可单独审阅，baseline 和失败证据不能由普通临时 run 的保留数量策略删除。
