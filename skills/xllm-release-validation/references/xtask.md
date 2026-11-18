# xTask 路径

xTask 是外部依赖。只读固定版本 checkout，提供独立 JSON/YAML 工作目录；skill 不包含任何 xTask 源码覆盖、补丁下载、应用或自动合并逻辑。

`xtask.repo` 与 commit 必填。生成器会检查 checkout clean、commit 一致，以及实际源码所声明的参数和 session API；再调用该 checkout 的原生脚本生成器进行 dry generation，核对最终 benchmark 的每个配置参数。关键源码哈希和命令核对结果保存在 capabilities.json / command-verification.json。这是接口检查，不能替代版本真实硬件验证。

默认 `service_mode: native`：由 xTask public session API 准备服务，随后调用 workflow ready/真实生成 smoke，再运行 trial，最后 close session。可按声明的组复用同一次服务；失败后该组整体重跑。xTask 保有服务管理权，release 不另起一套 serving。入口文件 run_xtask.sh、xtask-session.py 和原生生成的启动/评测脚本全部保留。

已有容器场景检查 container_name/reuse_existing_container、xllm_binary、TRITON_BINARY_PATH、startup_environment、宿主机执行及 session API。当前 main 若尚无相应能力，场景 BLOCKED；由使用者准备合适版本或等待上游合入。仅设置配置不能弥补能力缺失。native 模式的 CPU 绑定必须匹配所选版本原生模板，否则明确拒绝。

可选 `service_mode: external`：xTask 只运行客户端。service.ownership=managed 时由 workflow 的 direct 服务适配器启动/验证/停止服务（vLLM 使用该路径）；xLLM 已有外部服务也可 ownership=external，并提供身份文件。生成 external_service 配置，不让 xTask 再启动该服务。该模式的客户端容器/依赖准备遵循所选 xTask 原生行为，需在计划审阅时检查 task_config/xllm_config。不会用 external 模式暗中替代不支持的 native 模式。

内部测试的两个 JSON 从外部 checkout 的配置加本轮显式覆盖生成到 attempt/xtask/config；新建交付模式使用 YAML 中的有效配置并启用 exact_config，不继承 checkout 的个人默认值。原 checkout 不修改。CLEAN_RESULTS=false 和正确的 serving_container_name 用于保留原容器；native 的 worker 退出逻辑必须由所选 xTask 自身提供。自动报告上传关闭（XTASK_BENCH_REPORT=0）。身份路径映射放在配置前部，要求这些路径在控制宿主机和容器中确实共享。

依赖安装按固定 checkout 的 requirement.txt 和文档执行，并记录版本；评测在宿主机运行时，EvalScope/Tokenizer 依赖须在宿主机可用。若该 xTask 的启动方式依赖已安装的 xLLM Python 包，应按相同源码版本的构建/安装说明安装并核对 import 路径与包内可执行文件；已有 xllm_binary 能力时可显式使用指定编译产物，不再假定 pip 安装是必要步骤。两种产物发现方式必须在计划和复现记录中说明，不能让 PATH/import 优先级选到旧包。

配置固定 benchmark_type=evalscope；支持 random，以及冻结为 custom 文本的 GSM8K/ShareGPT，CEval 使用 evalscope eval。生成器必须原样表达 dataset-path、数据参数和其他评测配置，缺失即阻塞。接口为 chat/completions、ignore_eos=true 和固定并发；原生生成器无法表达 open-loop/其他 endpoint 时阻塞，不删除选项假装等价。direct 支持的接口较宽；新增 xTask 版本能力应改适配器与测试，不改 xTask。

内置 native 集成在服务宿主机运行 xTask，instance_host 使用 localhost/127.0.0.1；client 与 service 的 ssh_host 一致。client 可以在宿主机或同一个服务容器内；容器客户端不支持额外 sources/env/cpu_set，避免声明的环境与原生 docker exec 实际环境不一致。宿主机客户端的 sources/env 会传入控制器，并由本地 executor 继承。原生模板不能安全表达的含空格或 shell 元字符的模型名/路径与输出路径会被拒绝。

`assess_test_type=para_set` 适用于原生手工扫描，但本 skill 已展开矩阵，因此每个 case/trial 仅执行一组参数，避免重复展开和 num_prompts 被自动改写。

内部 tests/cases/release_case.yaml 保留实际执行记录，可以使用已有容器/产物。交付文档仅展示由 delivery.xtask_reproduction 派生的新建环境 YAML：固定镜像、拉取指定代码并重新编译，不依赖原容器或编译产物。有效 JSON 值内嵌到 YAML；不再独立展示基础 JSON 或启动/测试脚本。通过 workflow 的 release.py reproduce 解释 release_reproduction 准备扩展，再调用原生 xTask；原生 xTask 单独执行不能完成该准备。完整协议见 [fresh-reproduction.md](fresh-reproduction.md)。vLLM 的 external 用例仍由 workflow 管理服务。
