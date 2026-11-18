# direct 路径

目标环境可由 prepare 准备 xLLM 容器/构建/MTP；CANN/ATB、自定义算子、模型及 EvalScope 依赖须按固定版本就绪。vLLM 的专用启动与身份流程见 vllm-ascend.md。控制端可在目标宿主机，service/client 可在不同已有容器；SSH 使用配置中的 ssh_host 和 BatchMode。Python、sources、env 在环境配置中明确，脚本不猜测安装路径或升级依赖。

运行前：核对 build verdict/binary provenance、commit、工作区状态、模型哈希、CLI flags、ACL 实际设备映射及依赖版本；宿主机采集 NPU/进程/负载。显式卡不足或繁忙时停止，不换卡、不修改 TP。API URL 必须从客户端可达；ready_api_url 可单独指定服务环境看到的地址。

start.sh 使用指定二进制及开放 flags，逐 rank 启动，保存 PID + /proc starttime 和日志。通过已有 service_lifecycle.py 记录 launch/ready/smoke。每组一条 smoke，内容可用 smoke_request 配置；它也属于图状态的组成部分。

stop.sh 只向当前 PID/starttime 匹配的进程发 TERM，等待 60 秒；超时明确失败，不扩大清理范围。宿主机检查物理卡释放，再调用 lifecycle cleanup。外部所有者的服务不发送信号，外部保留状态不作为正式 cleanup 证据。

workload 脚本精确使用 number，不复用旧 perf runner 中 number×parallel 的默认约定。endpoint、tokenizer、seed、采样、EOS、stream、tokenize_prompt、warmup、rate/open_loop 均来自 case，命令用 argv/shlex.quote 渲染。修改输入请求格式前要重新建立基线。

同组复用服务时，必须按 plan 中的相邻顺序执行。异常后仍进入本轮清理；服务状态不可恢复时整个组创建新 attempt。生成的脚本可供独立复查，运行脚本前同样需要资源和身份检查。

GSM8K/ShareGPT 使用同一冻结 custom 文本输入，不能再套 random 的严格输入长度；CEval 使用 EvalScope 精度入口，详见 datasets.md。代表场景默认 BF16 并发 1，其他并发/量化只在交付中列差异；每轮实际脚本仍全部归档。
