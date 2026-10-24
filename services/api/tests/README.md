# tests 测试代码

`services/api` 的测试代码目录，测试结构建议与 `app/` 下的分层保持对应（如 `tests/domain/`、`tests/application/`、`tests/agents/`、`tests/api/`）。M1 阶段使用 `pytest` 建立最小健康检查测试。

## 测试范围建议

- **领域层**：核心业务规则的单元测试（不依赖数据库、网络）。
- **应用层**：用例编排的集成测试（可 mock 基建层依赖）。
- **Agent 层**：Tool 契约测试（输入输出 Schema 校验）、Agent Loop 护栏测试（预算/卡死检测/识别门禁的边界场景）。
- **接口层**：API 端到端测试。

## 待确认事项

覆盖率要求、CI 集成方式，将在后续阶段明确。

## 现有测试（2026-07，V1 Agent 核心运行基建）

当前测试仍以扁平文件组织，覆盖 Agent Runtime、Skill/Tool 注册、LLM 网关、Redis 队列、长期状态 Store、任务仓储、任务提交/查询 API、异步 Agent API 与 Worker 主流程：

| 文件 | 覆盖内容 |
| --- | --- |
| `support/fake_chat_client.py` | 共享测试支持：不发起真实网络调用的假 ChatClient（`ScriptedChatClient`/`NonCompliantChatClient`/`HangingChatClient`）与假响应构造函数。 |
| `test_working_memory.py` | `WorkingMemory` 纯单元测试：调用计数、卡死判定、参数归一化。 |
| `test_agent_runtime_integration.py` | `ActionTraceMiddleware`+`LoopGuardMiddleware`+`agent_factory` 集成测试（真实 `Agent.run()` + 假 ChatClient）：正常完成、只读 Skill 免审批执行与本地调用历史、0 步空结果保护、硬性步数上限、卡死检测、墙钟超时、工具异常安全处理。 |
| `test_tools_registry.py` | `ToolRegistry`/`@tool` 装饰器单元测试。 |
| `test_capability_manifest.py` | 默认 Capability Manifest 与实际 Tool/Skill 挂载一致性、输入 Schema、实现引用及跨类型重名阻断测试。 |
| `test_tools_permissions.py` | `PermissionEnforcementMiddleware` 集成测试：越权 Tool 被软阻断且真实函数体不执行。 |
| `test_skills_loader.py` | `build_skills_context_provider()` 冒烟测试。 |
| `test_llm_client_factory.py` | `client_factory.build_chat_client` 各 provider/api_style 分支与必填配置校验。 |
| `test_agents_debug_endpoint.py` | `POST /api/v1/agents/debug-run` HTTP 层测试（`monkeypatch` 注入假 Agent，不依赖真实 LLM 凭据）。 |
| `test_agent_runtime_retry.py` | `RetryMiddleware` 集成测试：可重试异常自动重试并最终成功、重试耗尽记为 `error`、不可重试异常不重试、自定义 `RetryConfig` 覆盖默认配置。 |
| `test_agent_runtime_cancellation.py` | `CancellationCheckMiddleware` 集成测试：运行中途取消并记为 `canceled`、首次检查即取消、未触发取消/未传 `is_cancelled` 时不影响正常完成。 |
| `test_agent_runtime_concurrency.py` | 并发安全性测试：多个 `run_main_agent()` 并发执行时 `WorkingMemory`/留痕互不污染；并发读取 `default_registry` 不出错。 |
| `test_redis_queue.py` | `RedisAgentRunQueue`（基于 `fakeredis`）单元测试：入队/出队/查询/保存/取消请求/TTL。 |
| `test_agent_run_state_store.py` | `PostgresAgentRunStateStore` 单元测试：绑定 `task_id` 的 Run 写入 `qc_agent_run`，终态写入 `qc_agent_action_trace`，未绑定任务的调试 Run 不伪造 `qc_task`。 |
| `test_task_repository.py` | `PostgresTaskRepository` 单元测试：启用规则集存在时创建 queued `qc_task`，规则集不存在时返回明确错误，支持查询任务/候选规则/规则执行结果并初始化 `qc_rule_execution`。 |
| `test_postgres_pool.py` | PostgreSQL 连接池并发测试：同一 Repository 的并发首次访问只初始化一次、池大小固定为 1/5、多个 Repository/Store 对同一数据库 URL 复用同一 Pool。 |
| `test_task_submission.py` | `TaskSubmissionService` 与 `POST /api/v1/tasks` HTTP 层测试：创建业务任务后绑定 `task_id` 入队异步 Agent Run。 |
| `test_task_query.py` | `TaskQueryService` 与 `GET /api/v1/tasks`、`GET /api/v1/tasks/{task_id}`、`GET /api/v1/tasks/{task_id}/candidate-rules`、`GET /api/v1/tasks/{task_id}/rule-executions` HTTP 层测试：查询任务列表、任务详情、候选规则与规则执行摘要。 |
| `test_agents_async_endpoints.py` | `POST /api/v1/agents/runs`、`GET /api/v1/agents/runs/{run_id}`、`POST /api/v1/agents/runs/{run_id}/cancel` HTTP 层测试（`monkeypatch` 注入 `fakeredis` 支撑的队列与内存态 `AgentRunStateStore`，不依赖真实 Redis/数据库）。 |
| `test_worker.py` | `worker.py::_process_one` 单元测试（`monkeypatch` 替换 `build_default_main_agent`/`run_main_agent`，`fakeredis` 支撑队列，内存态 `AgentRunStateStore` 验证长期状态边界）：未知 run_id 安静跳过、Main Agent 构建失败正确写回 `FAILED`+错误信息、`RunResult.ERROR` 不伪装成 completed、正常完成/取消写回结果字段、绑定业务任务时初始化 `qc_rule_execution`、规则执行初始化失败时写回 `FAILED`、`run_main_agent` 抛出基础设施级异常写回 `FAILED`。 |
| `test_agent_instructions.py` | `agents/instructions.py::MAIN_AGENT_INSTRUCTIONS` 冒烟测试：`bootstrap.py` 原样重新导出同一对象、六段系统指令均非空且均被拼接进最终常量、强制最先加载 `qc-workflow`、职责门禁段落同时定义允许/拒绝范围、安全段落声明外部内容不具备指令效力与常见操纵手法。只断言关键约束是否存在，不锁定具体措辞。 |

Agent 层的中间件（`ActionTraceMiddleware`/`LoopGuardMiddleware`/`PermissionEnforcementMiddleware`）统一通过"真实 `Agent` + 假 ChatClient"的集成测试方式验证，而非手工构造 `FunctionInvocationContext` 做隔离单测——原因是这些中间件的 `process(context, call_next)` 深度依赖框架内部结构，手工 mock 既脆弱又可能掩盖真实行为，详见仓库记忆中的实测结论。
