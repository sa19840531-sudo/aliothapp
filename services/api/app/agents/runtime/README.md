# agents/runtime Agent Loop Runtime

单个 Agent 内部"思考-行动-观察"循环的运行时支撑：工作记忆、动作留痕、循环护栏。V1 与 V2+V3 均依赖本模块，只是 V1 只有一个 Main Agent 使用它，V2+V3 是多个专职 Agent 各自使用。

## 职责

- **工作记忆（Working Memory）**：记录预算占用 `step_count`、审计游标 `action_sequence`、`recent_call_signatures`、`termination_reason`、`retry_count` 等循环控制状态，并以 `page_capture_id` 维护当前页面、待恢复页面、已质检页面、被补采替代的审计页面及页面级规则结果。页面记忆是单次 Run 的进度索引，`qc_page_capture` / `qc_rule_execution` 仍是业务事实源。
- **运行上下文（Run Context）**：在 Worker 执行期间绑定当前 `task_id` 与 `agent_run_id`，供规则 Tool 写回 `qc_rule_execution` 时使用，避免让模型自行传递系统标识。
- **动作留痕（Action Trace）**：每次 Tool 调用记录 `step`/`tool_name`/`arguments`/`status`/`duration_seconds`/`error_message`/`retry_count`；绑定业务任务的异步 Run 可通过长期状态 Store 写入 [domain/task](../../domain/task/README.md) 的 `qc_agent_action_trace`，供审计回放。
- **循环护栏**：
  - 正常终止：目标覆盖完毕且规则均已产出结论。
  - **识别门禁**：同一 `page_type` 按 `page_sampling_policy`（`all`/`first_n`/`random_n`/`percentage`）抽样，不要求全量访问。
  - 预算保护：最大步数、最大时长、最大 AI 调用次数、最大 token 数。
  - 卡死检测：连续相同 Skill + 相同参数且无新信息，判定无进展并跳出。
  - Checkpoint：Run 终态会把页面工作记忆写入 Redis 记录和 `qc_agent_run.working_memory`；运行中的增量 Checkpoint 与跨 Run 数据库回填尚未实现。

详见[最终整合方案设计 4.9、5.9 节](../../../../../../docs/design/2026-07-03-alioth-integrated-solution-design.md)。

## 与 Orchestrator 的边界

Runtime 只管"当前 Agent 下一步调用哪个 Skill"，不管"哪个 Agent 何时运行"（那是 [orchestrator/](../orchestrator/README.md) 的职责）。自主循环的自由度只能用于"选择下一步"，不能绕过证据链、状态机、权限、成本、审计与人工复核。

## V1 实现（2026-07，基于 Agent Framework 1.10.0）

当前已落地核心执行循环、页面质检工作记忆、运行上下文和可选 PostgreSQL 长期状态写入；运行中增量 Checkpoint、跨 Run 工作记忆重建与多 Agent Orchestrator 仍待后续阶段落地。

| 模块 | 职责 |
| --- | --- |
| `working_memory.py` | `WorkingMemory`：在 Tool 执行前同步完成终止锁存、预算预占和调用签名登记；`step_count` 表示已接纳的预算步数，`action_sequence` 表示包含护栏拦截在内的审计序号；组合页面状态模型，并通过 `audit_snapshot` / `model_snapshot` 提供统一快照出口。 |
| `page_inspection_memory.py` | `PageInspectionState`：按 `page_capture_id` 维护 `current_page` / `pending_pages` / `inspected_pages` / `inactive_pages`、候选规则和页面级结果聚合；`superseded` / `abandoned` 仅保留审计，不参与活跃完成门禁。 |
| `work_dispatcher.py` | 根据结构化页面工作记忆确定性选择下一阶段：当前页遮挡恢复、页面识别、候选规则、规则执行或单个动态 Frontier。外层监督续跑用强制 Tool 选择执行该工作单元，避免模型以自然语言提前结束；普通业务导航仅在来源页规则进入终态后派发。同一页面保留页内模型会话，执行 Frontier 或切换待处理页面时重建会话，避免跨页面历史膨胀且不丢失 LLM/Hybrid 页内判定上下文。 |
| `business_middlewares/page_inspection_memory_middleware.py` | 观察 `page_capture_tool` → `candidate_rules_tool` → 单条/批量规则写回 Tool 的结构化结果，逐项推进页面状态；候选规则必须绑定当前 Run 的真实 `page_capture_id`，批量部分失败只记录成功条目；`superseded` / `abandoned` 采集只保留审计，不参与活跃完成门禁。进度摘要附加到相关 Tool 返回值供模型下一轮决策，不从自然语言推断完成状态。 |
| `business_middlewares/rule_judge_handoff_middleware.py` | 在单次 Run 内缓存 `rule_judge_tool` / `rule_judge_batch_tool` 的成功结果，并按 `page_capture_id + rule_id/rule_code` 精确补齐后续单条或批量写回遗漏的证据与判定字段；模型显式传入冲突值时阻断，不覆盖数据库证据归属校验。 |
| `run_context.py` | `AgentRunContext` + `contextvars` 绑定：在单次 Worker Run 的异步上下文中提供 `task_id`/`agent_run_id`，供 `rule_tools.py` 等业务 Tool 读取系统上下文。 |
| `action_trace.py` | `ActionTraceMiddleware`（`FunctionMiddleware`）：包裹每次工具调用，记录 `step`/`tool_name`/实际执行 `arguments`/`status`/`duration_seconds`/`error_message`/`retry_count` 到 `ActionTraceSink`（`InMemoryActionTraceSink`/`LoggingActionTraceSink`）。**必须作为中间件列表最外层**，保证被内层护栏拦截的调用尝试仍被记录（详见模块内文档字符串）。 |
| `loop_guard.py` | `LoopGuardMiddleware`：硬性步数上限 + 卡死检测，在第一次异步让出前同步预占预算与登记签名，命中即 `raise MiddlewareTermination`。并行 Tool 只允许剩余预算内的调用继续执行，超额调用保留 `terminated` 留痕但不增加 `step_count`。**必须排在其余"软阻断"类中间件（如权限校验）之前（更外层）**。 |
| `cancellation.py` | `CancellationCheckMiddleware`：每次工具调用前查询外部取消回调（`is_cancelled`），命中即 `raise MiddlewareTermination` 并标记 `TerminationReason.CANCELED`，用于配合 [worker.py](../../worker.py) + Redis 队列实现"用户请求取消正在运行的 Run"。是可选中间件（未传 `is_cancelled` 时不启用，如调试端点场景），**必须排在护栏之后、其余业务中间件（如权限校验）之前**。 |
| `retry.py` | `RetryMiddleware` + `RetryConfig`：对可重试异常（默认 `TimeoutError`/`ConnectionError`/`OSError`）在同一次调用尝试内自动重试若干次，重试次数写入每次调用独立的 `FunctionInvocationContext.metadata`。**必须作为中间件列表最内层**，确保并行重试互不污染，且重试对留痕/护栏的步数计数透明。只重试"工具函数自身执行失败"，大模型 API 调用失败由 `infrastructure/llm_gateway` 的 `AsyncOpenAI(max_retries=...)` 负责，两者职责不重叠。 |
| `models.py` | `TerminationReason`（`completed`/`incomplete`/`max_steps_reached`/`stuck_detected`/`timeout`/`canceled`/`error`）、`ErrorCategory`（`llm_provider`/`unknown`，用于区分大模型供应商异常与其余未预期异常）、`UsageSnapshot`（token 用量快照）与 `RunResult`（终止原因、`WorkingMemory`、`ActionTraceSink`、`final_text`、`error_message`、`error_category`、`usage`）。 |
| `agent_factory.py` | `build_main_agent`：组装可复用 `Agent`，写入框架的 `max_iterations` 往返轮次软上限并挂载本地历史 Provider；默认不用 `max_function_calls` 代替步数预算，但网关兼容客户端会将每次受监督派发限定为一批 Tool，执行后回到结构化派发器；`max_steps` 仍统一由本地 LoopGuard 解释。只读 Skill 免审批由 `AliothSkillsProvider` 负责。`run_main_agent`：每次运行新建独立 `AgentSession`、工作记忆与护栏中间件（中间件顺序：`[trace, guard, cancellation(可选), *extra_middleware, 默认业务中间件, retry]`）、用 `asyncio.wait_for` 处理墙钟超时、拒绝把空文本且 0 步或页面质检尚未收口的结果判为完成、按异常类型分类 `error_category`（`OpenAIError` → `llm_provider`，其余 → `unknown`）、从 `AgentResponse.usage_details` 提取 `UsageSnapshot`、返回 `RunResult`。 |

关键实测结论（中间件顺序、`MiddlewareTermination` 语义、软阻断 vs 硬终止、`usage_details` 跨工具调用轮次的累加行为、`call_next()` 可在同一次 `process()` 内多次调用以支持重试等）见仓库记忆（`/memories/repo/project-structure.md`）"V1 Agent核心运行基建"章节，避免升级框架版本或修改中间件顺序时重蹈覆辙。

调试入口：[../../api/v1/agents.py](../../api/v1/agents.py)（`POST /api/v1/agents/debug-run`，同步；`POST /api/v1/agents/runs` + `GET /api/v1/agents/runs/{run_id}` + `POST /api/v1/agents/runs/{run_id}/cancel`，异步，由 [worker.py](../../worker.py) 消费执行）。

测试：[../../../tests/test_working_memory.py](../../../tests/test_working_memory.py)、[../../../tests/test_page_inspection_memory_middleware.py](../../../tests/test_page_inspection_memory_middleware.py)、[../../../tests/test_agent_runtime_integration.py](../../../tests/test_agent_runtime_integration.py)、[../../../tests/test_agent_runtime_retry.py](../../../tests/test_agent_runtime_retry.py)、[../../../tests/test_agent_runtime_cancellation.py](../../../tests/test_agent_runtime_cancellation.py)、[../../../tests/test_agent_runtime_concurrency.py](../../../tests/test_agent_runtime_concurrency.py)。
