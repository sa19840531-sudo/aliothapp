# infrastructure/queue 任务队列

Agent Run 的异步入队/出队与运行状态边界。Redis 负责短期执行队列与运行中查询
（[redis_queue.py](redis_queue.py)），长期状态写入通过
[state_store.py](state_store.py) 抽象出来。默认使用 no-op；设置
`ALIOTH_AGENT_RUN_STATE_STORE_BACKEND=postgres` 后，绑定 `task_id` 的 Run 会写入
`qc_agent_run` / `qc_agent_action_trace`。

## 技术选型

未采用 arq / Celery / RQ 等成熟任务队列框架，而是直接基于 `redis.asyncio` 实现一个
职责单一的轻量队列（`RedisAgentRunQueue`）：当前只需要"入队 + 阻塞出队 + 状态查询 +
取消标记"四类操作，引入完整任务队列框架的调度/重试/定时等能力属于过度设计。后续如
出现更复杂的调度需求（延时任务、优先级队列、跨机房路由等），再评估是否替换实现。

## 职责

- Agent Run 的入队（`POST /api/v1/agents/runs`）与 [worker.py](../../worker.py) 消费。
- Run 状态（`AgentRunQueueStatus`：queued/running/completed/failed/canceled）与结果
  （终止原因、最终文本、留痕、token 用量等）的读写，均以 JSON 存入 Redis，带 TTL 自然
  过期，**不是长期审计存储**。
- 取消请求（`is_cancel_requested`）：供 `agents/runtime/cancellation.py` 的
  `CancellationCheckMiddleware` 在 Agent Loop 内部轮询检查。
- 长期状态接口（`AgentRunStateStore`）：记录 submitted/started/finished/cancel_requested
  生命周期事件。当前默认 `NoopAgentRunStateStore`，测试使用 `InMemoryAgentRunStateStore`；
  `PostgresAgentRunStateStore` 只持久化绑定 `qc_task` 的 Run，不为调试 Run 伪造任务数据，
  并按 Run 生命周期同步推进 `qc_task.status`。

## 与 domain/task 的边界

本模块的 `AgentRunQueueStatus` 是 Redis 队列自身的执行态（面向"这次 Worker 处理到哪一步
了"），与 [domain/task](../../domain/task/README.md) 未来定义的质检任务业务状态机
（`qc_task`，面向"这条质检任务在业务流程里处于什么阶段"）是两个不同层次的概念，不要混用：
`PostgresAgentRunStateStore` 负责把 Agent Run 执行态映射到 `qc_task.status`
（queued/running/completed/failed/canceled → queued/running/done/failed/canceled），
但不会让业务任务复用 Redis 队列枚举。
