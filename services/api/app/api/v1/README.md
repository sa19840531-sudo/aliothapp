# app/api/v1 接口层 v1 版本

当前生效的 API 版本。路由按业务域分组组织，具体路由文件在实现阶段按需新增，规划的分组如下：

| 分组（规划） | 对应领域 |
| --- | --- |
| rules | [domain/rule](../../domain/rule/README.md) —— 规则集、规则、敏感词、品牌词、知识库 CRUD 与查询 |
| tasks | [domain/task](../../domain/task/README.md) —— 质检任务提交、状态查询、Agent 执行留痕查看 |
| evidence | [domain/evidence](../../domain/evidence/README.md) —— 证据与规则判定结果查询 |
| review | [domain/review](../../domain/review/README.md) —— 人工审核队列与处理 |
| iam | [domain/iam](../../domain/iam/README.md) —— 账号、角色、权限、审计日志 |
| integration | [domain/integration](../../domain/integration/README.md) —— Agent 配置、MCP 注册、凭证库、禅道/Telegram 集成 |

## 约定

- 每个分组对应一个路由模块，模块内路由前缀与分组名一致（如 `/api/v1/rules`）。
- 涉及权限的接口必须显式声明所需权限点，配合 `domain/iam` 的权限模型校验。
- 新增接口需在提交说明中注明，便于前端同步路由与菜单。

## 已实现路由（2026-07）

- `health.py`：`GET /api/v1/health` 健康检查。
- `tasks.py`：业务侧质检任务入口。
  - `POST /api/v1/tasks`：创建 `qc_task`，绑定规则集版本，并派发一个带 `task_id` 的异步 Agent Run 到 Redis 队列。
  - `GET /api/v1/tasks`：查询质检任务列表，支持按任务状态过滤。
  - `GET /api/v1/tasks/{task_id}`：查询质检任务详情，包含规则集版本与关联 Agent Run。
  - `GET /api/v1/tasks/{task_id}/rule-executions`：查询质检任务下的规则执行结果摘要；可通过 `agent_run_id` 限定指定 Run。
  - `GET /api/v1/tasks/{task_id}/candidate-rules`：查询任务锁定规则集下的候选规则，支持按 `page_type` 过滤。
- `workstation/`：工作台页面专属聚合接口，挂载在 `/api/v1/workstation`，用于区别管理后台接口与基础领域接口。
  - `GET /api/v1/workstation/tasks`：任务中心列表，支持任务名称、产品、目标链接、创建时间、执行状态、报告状态筛选。
  - `POST /api/v1/workstation/tasks`：按工作台创建任务页面 payload 创建质检任务，页面字段写入 `task_config`。
  - `GET /api/v1/workstation/tasks/{task_id}`：查询工作台任务详情摘要。
  - `POST /api/v1/workstation/tasks/{task_id}/{start|cancel|rerun|copy}`：执行任务操作。
  - `GET /api/v1/workstation/reports`、`GET /api/v1/workstation/reports/{task_id}`：报告中心列表与报告详情；详情可通过 `agent_run_id` 查询参数读取指定 Run 的报告，未传时读取最新 Run。
  - `GET /api/v1/workstation/reviews`、`POST /api/v1/workstation/reviews/{review_id}/{approve|reject|recheck}`：人工审核队列与处理。
  - `GET /api/v1/workstation/dashboard/overview`：工作台首页聚合数据。
- `agents.py`：Agent 核心运行基建的调试/联调入口，不对应上表任何业务分组，业务侧的任务提交接口仍应落在 `tasks` 分组。
  - `POST /api/v1/agents/debug-run`：手动触发一次 [../../agents/runtime/README.md](../../agents/runtime/README.md) 的 Main Agent 运行，同步等待结果，仅用于本地/联调调试，固定只放行 `safe` 权限级别 Tool。
  - `POST /api/v1/agents/runs`：提交一次异步 Main Agent 运行，入队后立即返回 `run_id`（202），真正执行由独立的 [../../worker.py](../../worker.py) 进程消费 Redis 队列完成。
  - `GET /api/v1/agents/runs/{run_id}`：查询一次异步运行的状态/结果（找不到返回 404）。
  - `POST /api/v1/agents/runs/{run_id}/cancel`：请求取消一次尚未结束的异步运行（找不到或已处于终态返回 404）。
