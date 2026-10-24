# Alioth 工作台 API 契约

## 1. 放置边界

工作台页面专属接口统一放在 `/api/v1/workstation/*`。

- 管理后台继续使用 `/api/v1/auth`、`/api/v1/users`、`/api/v1/rulesets`、`/api/v1/rules`。
- 基础任务域接口 `/api/v1/tasks` 保持兼容，继续承载任务提交、规则执行结果和候选规则等底层能力。
- 工作台页面使用 `app/workstation/src/services/*`，不引用 `app/admin/src/api/*`。

## 2. 任务中心

### `GET /api/v1/workstation/tasks`

用于任务中心列表。

查询参数：

| 参数 | 说明 |
| --- | --- |
| `task_name` | 按任务名称模糊搜索 |
| `product_name` | 按产品名称模糊搜索 |
| `product_code` | 按产品编码模糊搜索 |
| `target_url` | 按目标链接模糊搜索 |
| `status` | `queued/running/review/done/failed/canceled` |
| `report_status` | `not_started/generating/ready/failed` |
| `created_from` | 创建时间起点 |
| `created_to` | 创建时间终点 |
| `page` | 页码 |
| `page_size` | 每页条数，最大 100 |

响应字段：

| 字段 | 说明 |
| --- | --- |
| `task_id` | 任务 ID |
| `task_name` | 任务名称 |
| `product` | 产品快照，含部门、产品编码、产品名称 |
| `target_url` | 目标链接 |
| `journey_steps` | 用户旅程 |
| `status` | 执行状态 |
| `progress` | 执行进度，0-100 |
| `report_status` | 报告状态 |
| `failure_reason` | 失败原因 |
| `available_actions` | 当前状态允许的操作 |
| `created_at` | 创建时间 |

### `POST /api/v1/workstation/tasks`

用于创建质检任务。工作台请求会映射到现有 `TaskSubmissionService`，页面字段写入 `qc_task.task_config`，首版不新增数据库字段。

请求字段：

| 字段 | 说明 |
| --- | --- |
| `task_name` | 任务名称 |
| `target_url` | 目标 URL |
| `ruleset_code` | 规则集编码 |
| `ruleset_version` | 规则集版本 |
| `channel` | `workbench/telegram/api` |
| `priority` | 优先级 |
| `product` | 产品快照 |
| `scan_mode` | `full/quick/custom` |
| `journey_steps` | 用户旅程 |
| `remark` | 备注 |

### `GET /api/v1/workstation/tasks/{task_id}`

用于任务详情摘要。返回任务中心字段，并补充 `started_at`、`finished_at`、`ruleset_code`、`ruleset_version`、`channel`、`priority`、`run_id`。

### 任务操作

| 接口 | 说明 |
| --- | --- |
| `POST /api/v1/workstation/tasks/{task_id}/start` | 待执行任务开始执行 |
| `POST /api/v1/workstation/tasks/{task_id}/cancel` | 取消待执行或执行中任务 |
| `POST /api/v1/workstation/tasks/{task_id}/rerun` | 重新执行任务 |
| `POST /api/v1/workstation/tasks/{task_id}/copy` | 复制任务 |

状态与操作关系：

| 状态 | 可用操作 |
| --- | --- |
| `queued` | `start/edit/cancel` |
| `running` | `detail/cancel` |
| `done` | `detail/report/rerun/copy` |
| `failed` | `failure/rerun/copy` |

## 3. 报告中心

### `GET /api/v1/workstation/reports`

用于报告中心列表和任务选择器，返回任务级报告状态。

### `GET /api/v1/workstation/reports/{task_id}`

用于报告详情。该接口复用 `ReportGenerationService.generate_task_report(task_id)`，返回：

- `task`
- `summary`
- `attention_results`
- `rule_results`
- `evidence_entries`

## 4. 人工审核

### `GET /api/v1/workstation/reviews`

查询审核队列，支持：

- `status`
- `trigger_reason`
- `page`
- `page_size`

### 审核动作

| 接口 | 说明 |
| --- | --- |
| `POST /api/v1/workstation/reviews/{review_id}/approve` | 审核通过 |
| `POST /api/v1/workstation/reviews/{review_id}/reject` | 审核驳回 |
| `POST /api/v1/workstation/reviews/{review_id}/recheck` | 标记需复核 |

审核动作会同步更新 `qc_review_item.status` 和 `qc_rule_execution.review_status`。

## 5. 工作台首页

### `GET /api/v1/workstation/dashboard/overview`

返回首页聚合数据：

- `summary_cards`
- `recent_tasks`
- `status_distribution`
- `risk_distribution`
- `latest_rule_executions`

## 6. 前端调用边界

| 前端文件 | 后端接口 |
| --- | --- |
| `app/workstation/src/services/tasks.api.ts` | `/api/v1/workstation/tasks` |
| `app/workstation/src/services/reports.api.ts` | `/api/v1/workstation/reports` |
| `app/workstation/src/services/reviews.api.ts` | `/api/v1/workstation/reviews` |
| `app/workstation/src/services/dashboard.api.ts` | `/api/v1/workstation/dashboard/overview` |
