# App 质检 API 接口

本文基于对接附件与仓库当前实现，固化 Alioth 正式 App 质检、星语 Official Runtime 和 Mobile Demo 三组 HTTP 接口。契约以仓库生成的 OpenAPI 为准。

- 正式业务：`/api/v1`，供工作台、业务系统和人工复核使用。
- 星语 Runtime：`/v1`，仅供 Worker 调用设备能力，业务端不得直连。
- Mobile Demo：`/api/v1/mobile-demo`，仅用于受控本地烟测，不能替代正式任务入口。

本机常用平台 API 端口为 `8020`。正式 App 质检需要：

- `ALIOTH_APP_QC_ENABLED=true`
- `ALIOTH_WORKFLOW_VERSION=v2`
- `Authorization: Bearer <JWT>`

## 1. 正式业务闭环

推荐调用顺序：

```text
POST /api/v1/auth/login
  → POST /api/v1/app-builds
  → POST /api/v1/tasks
  → GET /api/v1/workstation/tasks/{task_id}/app-stage
  → GET page-timeline / rule-results / inspection-work-items
  → GET /api/v1/workstation/reports/{task_id}
```

### 1.1 认证与构建制品

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/login` | 登录并取得 JWT |
| GET | `/api/v1/auth/me` | 查询当前用户 |
| POST | `/api/v1/app-builds` | 上传并校验 Android APK；`multipart/form-data` 字段为 `file`；成功返回 **201** |

`POST /app-builds` 返回 `platform_build_ref`（格式 `app-build:<uuid>`）、`sha256`、`size_bytes`、`package_name`、`version_name` 和 `version_code`。APK 校验失败返回 **422**。

### 1.2 提交 App 任务

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/tasks` | 提交质检并创建 Agent Run；成功返回 **202** |
| GET | `/api/v1/tasks` | 任务列表；Query：`status`、`limit` |
| GET | `/api/v1/tasks/{task_id}` | 任务详情 |
| GET | `/api/v1/tasks/{task_id}/agent-run` | 最近一次 Agent Run |
| GET | `/api/v1/tasks/{task_id}/rule-executions` | 规则执行结果；可传 `agent_run_id` |
| GET | `/api/v1/tasks/{task_id}/candidate-rules` | 候选规则；可传 `page_type` |

请求示例：

```json
{
  "target_platform": "app",
  "target_url": null,
  "ruleset_code": "alioth_first_ruleset",
  "ruleset_version": 1,
  "channel": "workbench",
  "priority": 100,
  "task_config": {
    "task_name": "示例 App 质检",
    "workflow_version": "v2",
    "remark": null,
    "app": {
      "platform_build_ref": "app-build:11111111-2222-3333-4444-555555555555",
      "package_name": null,
      "device": {
        "backend": "cloud_phone",
        "profile_id": "default"
      }
    },
    "product": {
      "department_name": "鼎丰",
      "product_code": "app-qc",
      "product_name": "App质检"
    }
  },
  "page_sampling_policy": {},
  "loop_guard": {}
}
```

`device.backend` 当前支持 `cloud_phone`、`ldplayer`、`google_emulator` 和 `redroid`。成功响应包含 `task_id`、`run_id`、`ruleset_id`、`status`。

主要失败码：

- **404**：规则集不存在。
- **409**：App QC 未启用，或 workflow 版本不符合要求。
- **422**：请求参数校验失败。
- **503**：数据库、队列或其他基础设施暂时不可用。

## 2. 工作台任务洞察

工作台前缀为 `/api/v1/workstation`，必须登录。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tasks` | 任务中心列表 |
| POST | `/tasks` | 工作台创建任务；成功返回 **202** |
| GET | `/tasks/{task_id}` | 工作台任务详情读模型 |
| POST | `/tasks/{task_id}/{operation}` | `start`、`cancel`、`rerun`、`copy` |
| GET | `/tasks/{task_id}/app-stage` | App 探索封存与正式质检九阶段投影 |
| GET | `/tasks/{task_id}/page-timeline` | 页面识别与探索时间线 |
| GET | `/tasks/{task_id}/rule-results` | 统一规则展示态；Query：`display_status`、`page_type` |
| GET | `/tasks/{task_id}/stats` | 命中统计和最近异常 |
| GET | `/tasks/{task_id}/inspection-work-items` | 持久化正式质检工作项 |
| GET | `/tasks/{task_id}/logs` | 执行日志；Query：`level`、`page`、`page_size` |
| GET | `/tasks/{task_id}/logs/export` | 导出纯文本日志 |
| GET | `/tasks/{task_id}/screenshots` | 截图证据列表 |
| GET | `/tasks/{task_id}/screenshots/archive` | 下载截图压缩包 |
| GET | `/tasks/{task_id}/platform-progress/export` | 导出平台能力进展 Excel |

### 2.1 `app-stage` 响应

新流程响应示例：

```json
{
  "applicable": true,
  "task_id": "11111111-2222-3333-4444-555555555555",
  "task_status": "done",
  "bootstrap": {
    "stage": "app_ready",
    "installed_package": "com.example.app",
    "error_code": null,
    "ready_at": "2026-08-11T12:00:00+00:00"
  },
  "exploration_work": {
    "succeeded": 6
  },
  "app_exploration": {
    "status": "exploration_completed",
    "surface_phase": "popup_chain",
    "current_page_identity": "page:home",
    "completed_work_count": 6,
    "pending_work_count": 0,
    "action_count": 4,
    "reobservation_count": 2,
    "coverage_summary": {
      "indexed_capture_count": 12,
      "unique_page_count": 5,
      "surface_phase_count": 3
    },
    "terminal_reason_code": "exploration_completed",
    "outcome": "exploration_completed",
    "evidence_index_id": "app-evidence-index:...",
    "evidence_index_hash": "sha256:...",
    "evidence_gap_count": 0,
    "surface_session_status": "closed",
    "started_at": "2026-08-11T12:00:00+00:00",
    "completed_at": "2026-08-11T12:05:00+00:00"
  },
  "gate": {
    "gate_passed": true,
    "block_reasons": []
  },
  "inspection_summary": {
    "work_item_count": 2,
    "enforced_count": 2,
    "nonterminal_count": 0,
    "failed_count": 0
  }
}
```

`app_exploration.status` 语义：

| 状态 | 含义 |
|---|---|
| `created` / `bootstrapping` / `surface_resolving` | 初始化或界面准备中 |
| `exploring` / `final_observing` | 页面探索或最终观察中 |
| `evidence_validating` / `evidence_sealing` | 证据校验或封存中 |
| `session_draining` | 已封存，正在关闭 Session |
| `exploration_completed` | 业务探索完成 |
| `evidence_insufficient` | 探索正常结束，但业务证据不足 |
| `technical_failed` | 封存前发生技术失败 |
| `close_failed` | Evidence Index 已封存，但 Session 关闭失败 |

旧任务没有 Evidence Index、Exploration Work、Surface Phase 或 Action 等新流程事实时，`app_exploration` 返回 `null`。不得根据历史 Session 伪造新流程状态。

工作台按以下九阶段展示，不再把 Probe 当作 App 正常阶段：

1. Build Validation
2. Bootstrap
3. Surface Readiness
4. Page Exploration
5. Evidence Validation
6. Evidence Sealing
7. Session Closure
8. Formal Inspection
9. Report / Review

## 3. 证据、报告与人工复核

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/evidence/{artifact_id}/file` | 回读证据二进制；不存在返回 **404** |
| GET | `/api/v1/workstation/reports` | 报告列表 |
| GET | `/api/v1/workstation/reports/{task_id}` | 报告详情 |
| GET | `/api/v1/workstation/reports/{task_id}/insight` | 摘要、人工确认和全量明细 |
| POST | `/api/v1/workstation/reports/{task_id}/confirmations/{execution_id}` | 人工确认规则终审 |
| GET | `/api/v1/workstation/reports/{task_id}/export` | 导出自包含 HTML 报告 |
| GET | `/api/v1/workstation/reviews` | 人工审核队列 |
| POST | `/api/v1/workstation/reviews/{review_id}/{action}` | 处理审核项 |
| GET | `/api/v1/app-qc/api-doc` | App 质检 Swagger UI |
| GET | `/api/v1/app-qc/openapi.json` | App 质检 OpenAPI 3 JSON |

规则结论必须引用 `artifact_id` 和持久化 Capture/Manifest；工作台日志只用于排障，不能充当可信证据。正式 Inspection 只消费 hash 校验通过的 sealed Evidence Index 快照，不能反向触发设备补采。

## 4. 星语 Official Runtime

Runtime 前缀为 `/v1`，由 `ALIOTH_APP_RUNTIME_PROVIDER=official` 启用。它是 Worker 内部设备适配协议，不是业务任务入口。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/sessions` | 创建 Session |
| POST | `/v1/sessions/reattach` | 重连 Session |
| POST | `/v1/sessions/close` | 关闭 Session |
| POST | `/v1/apps/install` | 安装目标包 |
| POST | `/v1/apps/open` | 打开 App |
| POST | `/v1/apps/capture` | 截图 |
| POST | `/v1/apps/capture-ui-tree` | 获取 UI Tree |
| POST | `/v1/apps/capture-aux` | 采集 `device_log`、`network_trace` 或 `fault_artifact` |
| POST | `/v1/apps/execute` | 执行点击、滑动、输入和恢复原语 |
| GET | `/v1/api-doc` | Swagger UI |
| GET | `/v1/openapi.json` | OpenAPI JSON |

## 5. Mobile Demo

仅当 `ALIOTH_MOBILE_DEMO_ENABLED=true` 时挂载，并受 loopback 与 Host Token 安全边界约束。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/mobile-demo/version` | Demo 版本 |
| GET/POST | `/api/v1/mobile-demo/device-backend` | 查询或切换设备后端 |
| GET | `/api/v1/mobile-demo/devices` | 设备列表 |
| POST | `/api/v1/mobile-demo/apps/upload` | 本地上传 APK 并启动烟测 |
| POST | `/api/v1/mobile-demo/apps/from-url` | 已停用 |
| GET | `/api/v1/mobile-demo/runs/{run_id}` | Run 状态 |
| GET | `/api/v1/mobile-demo/runs/{run_id}/log` | Run 日志 |
| GET | `/api/v1/mobile-demo/screenshots` | 截图列表 |
| GET | `/api/v1/mobile-demo/screens/{filename}` | 读取 Demo 截图 |
| GET | `/api/v1/mobile-demo/runs/{run_id}/screens/{filename}` | 按 Run 读取截图 |
| GET | `/api/v1/mobile-demo/artifacts/{artifact_id}` | Artifact 元数据 |
| GET | `/api/v1/mobile-demo/artifacts/{artifact_id}/file` | Artifact 文件 |
| POST | `/api/v1/mobile-demo/stop` | 停止烟测 |
| GET | `/api/v1/mobile-demo/demo` | 操作页 |
| GET | `/api/v1/mobile-demo/api-doc` | Swagger UI |
| GET | `/api/v1/mobile-demo/openapi.json` | OpenAPI JSON |

## 6. 共用管理接口

App、Web 与 H5 共用以下规则和工作台配置接口。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/api/v1/rulesets` | 规则集列表／建立 |
| GET | `/api/v1/rulesets/{ruleset_id}` | 规则集详情 |
| POST | `/api/v1/rulesets/{ruleset_id}/publish` | 发布规则集 |
| GET / POST | `/api/v1/rules` | 规则列表／建立 |
| GET | `/api/v1/rules/{rule_id}` | 规则详情 |
| GET / POST | `/api/v1/products` | 产品分页／建立 |
| GET | `/api/v1/workstation/products` | 工作台产品下拉 |
| GET | `/api/v1/workstation/dashboard` | 工作台仪表板 |
| GET | `/api/v1/workstation/system/status` | 系统状态 |
| GET | `/api/v1/workstation/inspection-scope` | 执行范围预览 |
| GET | `/api/v1/workstation/page-config` | 页面配置总览 |

## 7. 对接约束

1. 对外系统只使用 `/api/v1` 正式业务接口；不要直接调用 `/v1/apps/*` 或 Mobile Demo。
2. App 任务的 `target_url` 为 `null`，`target_platform=app`，`workflow_version=v2`。
3. 先上传 APK，再把返回的 `platform_build_ref` 传给任务。
4. `exploration_completed` 与 `evidence_insufficient` 是业务结果；`technical_failed` 与 `close_failed` 是技术/关闭异常，调用方不得合并展示。
5. Evidence Index 封存后不可变；Session 关闭失败不撤销已封存证据。
6. 接口契约入口：`GET /api/v1/app-qc/api-doc`；机器可读契约：`GET /api/v1/app-qc/openapi.json`。
