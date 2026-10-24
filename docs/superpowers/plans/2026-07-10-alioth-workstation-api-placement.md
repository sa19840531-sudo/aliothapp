# Alioth Workstation API Placement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build workstation-specific API contracts and placement boundaries with minimal backend disruption while keeping admin APIs untouched.

**Architecture:** Add a workstation presentation API namespace under `services/api/app/api/v1/workstation/`, mounted as `/api/v1/workstation`. Keep existing domain APIs such as `/tasks`, `/rules`, `/users`, and `/auth` stable. Workstation endpoints call application-layer query/use-case services and must not place aggregation logic directly in FastAPI route functions.

**Tech Stack:** Python FastAPI, Pydantic, PostgreSQL, Vue 3, TypeScript, Vite.

## Global Constraints

- 后端固定技术栈：Python FastAPI。
- 数据库固定技术栈：PGSQL。
- 前端固定技术栈：Vite + Vue + TypeScript。
- AI 框架固定技术栈：Microsoft Agent Framework。
- 所有文档约定放在 `docs` 目录。
- 接口命名保持一致，资源路径使用小写资源名。
- 前端工作台接口不得放入 `app/admin/src/api`。
- 管理后台现有接口 `/auth`、`/users`、`/rulesets`、`/rules` 不因工作台页面接入而改名或迁移。
- 现有基础任务接口 `/api/v1/tasks` 保持兼容，用于任务域基础能力、联调和外部入口。

---

## Placement Decision

### Recommended Namespace

Workstation page-facing APIs should live under:

```text
/api/v1/workstation
```

Examples:

```text
GET  /api/v1/workstation/tasks
POST /api/v1/workstation/tasks
GET  /api/v1/workstation/tasks/{task_id}
POST /api/v1/workstation/tasks/{task_id}/start
POST /api/v1/workstation/tasks/{task_id}/cancel
POST /api/v1/workstation/tasks/{task_id}/rerun
POST /api/v1/workstation/tasks/{task_id}/copy
GET  /api/v1/workstation/reports
GET  /api/v1/workstation/reports/{task_id}
GET  /api/v1/workstation/reviews
GET  /api/v1/workstation/dashboard/overview
```

### Why This Is Minimal

- Only add one top-level router include in `services/api/app/api/v1/router.py`.
- Do not move existing `tasks.py`, `rules.py`, `users.py`, `auth.py`, or `agents.py`.
- Do not change admin frontend paths or admin API modules.
- Workstation frontend only changes its own `app/workstation/src/services/*.api.ts` path constants.
- Workstation-specific list fields such as product snapshot, journey, progress, report status, and available actions are treated as presentation read models, not as replacements for the lower-level `/tasks` contract.

### Boundary Rule

```text
admin frontend -> app/admin/src/api/* -> /api/v1/auth, /api/v1/users, /api/v1/rulesets, /api/v1/rules
workstation frontend -> app/workstation/src/services/* -> /api/v1/workstation/*
shared backend domain use cases -> services/api/app/application/*
shared backend persistence -> services/api/app/infrastructure/db/*
```

## File Structure

### Backend API Layer

- Create: `services/api/app/api/v1/workstation/__init__.py`
  - Exports the workstation router package.
- Create: `services/api/app/api/v1/workstation/router.py`
  - Creates `APIRouter(prefix="/workstation")`.
  - Includes workstation task, report, review, and dashboard routers.
- Create: `services/api/app/api/v1/workstation/tasks.py`
  - Handles task center list, create, detail, and task operations.
- Create: `services/api/app/api/v1/workstation/reports.py`
  - Handles report center and report detail read APIs.
- Create: `services/api/app/api/v1/workstation/reviews.py`
  - Handles manual review queue and review actions.
- Create: `services/api/app/api/v1/workstation/dashboard.py`
  - Handles workstation overview aggregation.
- Modify: `services/api/app/api/v1/router.py`
  - Include `workstation_router` once.
- Modify: `services/api/app/api/v1/README.md`
  - Document the workstation namespace and clarify that admin APIs remain separate.

### Backend Application Layer

- Create: `services/api/app/application/workstation_tasks.py`
  - Defines page-facing task list/detail read models and task operation commands.
- Create: `services/api/app/application/workstation_reports.py`
  - Wraps existing report generation service for page-facing report APIs.
- Create: `services/api/app/application/workstation_reviews.py`
  - Defines review queue query and review action use cases.
- Create: `services/api/app/application/workstation_dashboard.py`
  - Defines dashboard overview aggregation.

### Backend Infrastructure Layer

- Create: `services/api/app/infrastructure/db/workstation_query_repository.py`
  - Contains read-model SQL for workstation pages.
  - Reads from `qc_task`, `qc_rule_execution`, `qc_evidence_artifact`, `qc_review_item`, and `qc_agent_run`.
  - Extracts task display fields from `qc_task.task_config` first, avoiding a schema migration for the initial API pass.

### Frontend Workstation Layer

- Modify: `app/workstation/src/services/tasks.api.ts`
  - Change page-facing paths from `/tasks` to `/workstation/tasks`.
- Modify: `app/workstation/src/services/reports.api.ts`
  - Stop adapting from task APIs once `/workstation/reports` exists.
- Modify: `app/workstation/src/services/reviews.api.ts`
  - Stop adapting from rule execution APIs once `/workstation/reviews` exists.
- Create: `app/workstation/src/services/dashboard.api.ts`
  - Calls `/workstation/dashboard/overview`.
- Modify: `app/workstation/src/types/task.ts`
  - Add workstation task center fields.
- Modify: `app/workstation/src/types/review.ts`
  - Align review status values with backend `approved/rejected/needs_recheck`.
- Create: `app/workstation/src/types/report.ts`
  - Add report center/detail response types.
- Create: `app/workstation/src/types/dashboard.ts`
  - Add overview response types.

### Tests

- Create: `services/api/tests/test_workstation_tasks_api.py`
- Create: `services/api/tests/test_workstation_reports_api.py`
- Create: `services/api/tests/test_workstation_reviews_api.py`
- Create: `services/api/tests/test_workstation_dashboard_api.py`
- Add frontend type/build verification through existing workstation build command.

---

## API Contract Plan

### Task Center

`GET /api/v1/workstation/tasks`

Query parameters:

```text
task_name?: string
product_name?: string
product_code?: string
target_url?: string
status?: queued | running | review | done | failed | canceled
report_status?: not_started | generating | ready | failed
created_from?: ISO datetime
created_to?: ISO datetime
page?: number
page_size?: number
```

Response:

```json
{
  "items": [
    {
      "task_id": "task-id",
      "task_name": "首页改版回归测试",
      "product": {
        "department_name": "质量保障部",
        "product_code": "SSC-H5-001",
        "product_name": "移动首页"
      },
      "target_url": "https://example.com/mobile/home",
      "journey_steps": ["首页", "商品详情", "购物车", "订单确认"],
      "status": "running",
      "progress": 72,
      "report_status": "generating",
      "failure_reason": null,
      "available_actions": ["detail", "cancel"],
      "created_at": "2026-07-10T09:15:00+08:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

### Task Creation

`POST /api/v1/workstation/tasks`

Request:

```json
{
  "task_name": "双十一主会场页面质检",
  "target_url": "https://example.com/mobile/home",
  "ruleset_code": "alioth_first_ruleset",
  "ruleset_version": 1,
  "channel": "workbench",
  "priority": 100,
  "product": {
    "department_name": "营销中台",
    "product_code": "MKT-CAMP-618",
    "product_name": "活动主会场"
  },
  "scan_mode": "full",
  "journey_steps": ["活动首页", "优惠券页", "商品列表", "下单页"],
  "remark": "活动前全量质检"
}
```

Implementation rule:

- Route calls existing `TaskSubmissionService`.
- The route maps `task_name`, `product`, `scan_mode`, `journey_steps`, and `remark` into `task_config`.
- No database migration is needed for the first pass.

### Task Operations

```text
POST /api/v1/workstation/tasks/{task_id}/start
POST /api/v1/workstation/tasks/{task_id}/cancel
POST /api/v1/workstation/tasks/{task_id}/rerun
POST /api/v1/workstation/tasks/{task_id}/copy
POST /api/v1/workstation/tasks/batch-actions
```

Operation response:

```json
{
  "task_id": "task-id",
  "status": "running",
  "run_id": "run-id",
  "message": "任务已开始执行"
}
```

Allowed operations:

```text
queued  -> start, edit, cancel
running -> detail, cancel
done    -> detail, report, rerun, copy
failed  -> failure, rerun, copy
```

### Reports

`GET /api/v1/workstation/reports`

Purpose:

- Feeds the report center task selector and report list.
- Uses `qc_task` plus rule execution summary.

`GET /api/v1/workstation/reports/{task_id}`

Purpose:

- Returns report summary, key findings, full rule results, and evidence entry metadata.
- Should reuse `ReportGenerationService.generate_task_report(task_id)` for counts and rule result grouping.

### Reviews

`GET /api/v1/workstation/reviews`

Query parameters:

```text
status?: pending | approved | rejected | needs_recheck
trigger_reason?: low_confidence | high_risk | evidence_insufficient | loop_budget_exceeded | no_progress | manual_flag
page?: number
page_size?: number
```

Actions:

```text
POST /api/v1/workstation/reviews/{review_id}/approve
POST /api/v1/workstation/reviews/{review_id}/reject
POST /api/v1/workstation/reviews/{review_id}/recheck
```

### Dashboard

`GET /api/v1/workstation/dashboard/overview`

Response groups:

```text
summary_cards
recent_tasks
status_distribution
risk_distribution
latest_rule_executions
```

---

## Implementation Tasks

### Task 1: Add Workstation API Namespace Skeleton

**Files:**
- Create: `services/api/app/api/v1/workstation/__init__.py`
- Create: `services/api/app/api/v1/workstation/router.py`
- Create: `services/api/app/api/v1/workstation/tasks.py`
- Create: `services/api/app/api/v1/workstation/reports.py`
- Create: `services/api/app/api/v1/workstation/reviews.py`
- Create: `services/api/app/api/v1/workstation/dashboard.py`
- Modify: `services/api/app/api/v1/router.py`
- Test: `services/api/tests/test_workstation_router.py`

**Interfaces:**
- Consumes: existing FastAPI `api_router`.
- Produces: `/api/v1/workstation/health` or `/api/v1/workstation/dashboard/overview` routable namespace for test discovery.

- [ ] **Step 1: Add router package files**

Create `workstation/router.py` with a root router that includes module routers under `/workstation`.

- [ ] **Step 2: Include workstation router once**

Modify `services/api/app/api/v1/router.py` to import `workstation_router` and call `api_router.include_router(workstation_router)`.

- [ ] **Step 3: Add route registration test**

Use FastAPI test client style already used by API tests and assert that a workstation route appears under `/api/v1/workstation`.

- [ ] **Step 4: Run router test**

Run:

```bash
pytest services/api/tests/test_workstation_router.py -q
```

Expected: PASS.

### Task 2: Implement Workstation Task Read Models

**Files:**
- Create: `services/api/app/application/workstation_tasks.py`
- Create: `services/api/app/infrastructure/db/workstation_query_repository.py`
- Modify: `services/api/app/api/v1/workstation/tasks.py`
- Test: `services/api/tests/test_workstation_tasks_api.py`

**Interfaces:**
- Consumes: `qc_task.task_config`, `qc_rule_execution`, latest `qc_agent_run`.
- Produces: `WorkstationTaskListResponse`, `WorkstationTaskDetailResponse`.

- [ ] **Step 1: Write tests for list filters**

Assert that query parameters for task name, product code, target URL, status, report status, and created time range are passed into the application service.

- [ ] **Step 2: Define dataclasses and repository protocol**

Define page-facing dataclasses in `workstation_tasks.py` for product snapshot, task list row, page result, and task detail.

- [ ] **Step 3: Implement SQL read model**

Read `task_config->>'task_name'`, `task_config->'product'`, and `task_config->'journey_steps'`. Derive progress from rule execution counts and task status. Derive report status from task status and rule execution availability.

- [ ] **Step 4: Wire route to service**

Route functions only parse HTTP parameters and call the application service.

- [ ] **Step 5: Run tests**

Run:

```bash
pytest services/api/tests/test_workstation_tasks_api.py -q
```

Expected: PASS.

### Task 3: Implement Workstation Task Operations

**Files:**
- Modify: `services/api/app/application/workstation_tasks.py`
- Modify: `services/api/app/infrastructure/db/workstation_query_repository.py`
- Modify: `services/api/app/api/v1/workstation/tasks.py`
- Test: `services/api/tests/test_workstation_task_operations_api.py`

**Interfaces:**
- Consumes: existing `TaskSubmissionService` and task state in `qc_task`.
- Produces: operation endpoints for start, cancel, rerun, copy, and batch actions.

- [ ] **Step 1: Write state transition tests**

Assert allowed actions for queued, running, done, and failed tasks match the task center UI.

- [ ] **Step 2: Implement action validation**

Validate current status before changing state. Return 409 for invalid operations.

- [ ] **Step 3: Implement operation handlers**

Start creates or dispatches an Agent run. Cancel marks the task canceled and requests run cancellation when a run exists. Rerun creates a new run or copied task according to service design. Copy returns a new task seeded from the original `task_config`.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest services/api/tests/test_workstation_task_operations_api.py -q
```

Expected: PASS.

### Task 4: Implement Workstation Reports APIs

**Files:**
- Create: `services/api/app/application/workstation_reports.py`
- Modify: `services/api/app/api/v1/workstation/reports.py`
- Test: `services/api/tests/test_workstation_reports_api.py`

**Interfaces:**
- Consumes: `ReportGenerationService.generate_task_report(task_id)`.
- Produces: report center list and task report detail.

- [ ] **Step 1: Write tests for report detail**

Assert `/workstation/reports/{task_id}` returns task summary, summary counts, attention results, rule results, and evidence entries.

- [ ] **Step 2: Wrap existing report service**

Do not duplicate count logic from `ReportGenerationService`; adapt its output to the page response shape.

- [ ] **Step 3: Run tests**

Run:

```bash
pytest services/api/tests/test_workstation_reports_api.py -q
```

Expected: PASS.

### Task 5: Implement Workstation Review APIs

**Files:**
- Create: `services/api/app/application/workstation_reviews.py`
- Modify: `services/api/app/api/v1/workstation/reviews.py`
- Test: `services/api/tests/test_workstation_reviews_api.py`

**Interfaces:**
- Consumes: `qc_review_item`, `qc_rule_execution`, `qc_task`.
- Produces: review queue list and approve/reject/recheck actions.

- [ ] **Step 1: Write queue query tests**

Assert status and trigger reason filters are honored.

- [ ] **Step 2: Implement review actions**

Update `qc_review_item.status` and mirror the final status to `qc_rule_execution.review_status`.

- [ ] **Step 3: Run tests**

Run:

```bash
pytest services/api/tests/test_workstation_reviews_api.py -q
```

Expected: PASS.

### Task 6: Implement Workstation Dashboard Overview

**Files:**
- Create: `services/api/app/application/workstation_dashboard.py`
- Modify: `services/api/app/api/v1/workstation/dashboard.py`
- Test: `services/api/tests/test_workstation_dashboard_api.py`

**Interfaces:**
- Consumes: task, rule execution, review, and report aggregates.
- Produces: one overview response for the workbench home page.

- [ ] **Step 1: Write overview response test**

Assert the response contains summary cards, recent tasks, status distribution, risk distribution, and latest rule executions.

- [ ] **Step 2: Implement dashboard aggregation**

Keep this as a read-only aggregation. Do not add write operations to dashboard routes.

- [ ] **Step 3: Run tests**

Run:

```bash
pytest services/api/tests/test_workstation_dashboard_api.py -q
```

Expected: PASS.

### Task 7: Migrate Workstation Frontend Services

**Files:**
- Modify: `app/workstation/src/services/tasks.api.ts`
- Modify: `app/workstation/src/services/reports.api.ts`
- Modify: `app/workstation/src/services/reviews.api.ts`
- Create: `app/workstation/src/services/dashboard.api.ts`
- Modify: `app/workstation/src/types/task.ts`
- Create: `app/workstation/src/types/report.ts`
- Create: `app/workstation/src/types/dashboard.ts`
- Modify: `app/workstation/src/types/review.ts`

**Interfaces:**
- Consumes: `/api/v1/workstation/*`.
- Produces: frontend service boundary isolated from `app/admin/src/api/*`.

- [ ] **Step 1: Update service paths**

Change workstation API calls from `/tasks` adapters to `/workstation/tasks`, `/workstation/reports`, `/workstation/reviews`, and `/workstation/dashboard/overview`.

- [ ] **Step 2: Update TypeScript response types**

Mirror the Pydantic response fields using snake_case at the HTTP boundary.

- [ ] **Step 3: Run workstation build**

Run:

```bash
npm --prefix app/workstation run build
```

Expected: PASS.

### Task 8: Document API Boundary

**Files:**
- Modify: `services/api/app/api/v1/README.md`
- Create: `docs/design/2026-07-10-alioth-workstation-api-contract.md`

**Interfaces:**
- Consumes: final endpoint paths and response shapes from Tasks 1-7.
- Produces: team-facing API contract and placement note.

- [ ] **Step 1: Update API README**

Add `workstation` as a presentation API namespace and explicitly state that admin APIs remain under their current domain routes.

- [ ] **Step 2: Add contract document**

Document each endpoint, request parameters, response fields, status enums, and frontend service ownership.

- [ ] **Step 3: Run documentation sanity check**

Run:

```bash
rg -n "/workstation|app/admin/src/api|app/workstation/src/services" services/api/app/api/v1/README.md docs/design/2026-07-10-alioth-workstation-api-contract.md
```

Expected: output includes both the workstation namespace and the admin/workstation boundary.

---

## Self-Review

- Spec coverage: The plan separates workstation APIs from admin APIs, keeps existing admin endpoints stable, and minimizes backend changes to one new namespace plus application/query services.
- Placeholder scan: No endpoint or file location is left unspecified.
- Type consistency: API paths use `/api/v1/workstation/*`; frontend service ownership remains under `app/workstation/src/services`; admin frontend ownership remains under `app/admin/src/api`.
