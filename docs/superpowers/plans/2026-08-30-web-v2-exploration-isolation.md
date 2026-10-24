# Web V2 Exploration Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可独立开启、入队、消费和启动桌面浏览器的 `WEB_V2` 仅探索链路，确保 Web 不进入既有 H5、App 或通用 Main Agent 路径。

**Architecture:** 保留现有单体和公共浏览器采集内核，在任务目标、Workflow 路由、Redis 队列、Worker 角色和 Playwright Browser Context 五个边界增加 Web 专属契约。首期只开放 `exploration_only`；Web Runtime 缺失时 fail closed，不回退 H5/App/Main Agent。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、Redis asyncio、Playwright、pytest、Vite + Vue + TypeScript。

**Spec:** `docs/superpowers/specs/2026-08-29-web-v2-exploration-workflow-design.md`

## Global Constraints

- Web Workflow 固定为 `WEB_V2`，禁止映射到 `H5_V2`、`APP` 或通用 Main Agent。
- Web 功能门禁为 `ALIOTH_WEB_V2_MODE=off|exploration_only|on`，默认 `off`，不得依赖 `ALIOTH_H5_V2_MODE`。
- 首期 Browser Profile 仅允许 `desktop_fhd_1920x1080_v1`。
- Browser Context 固定 `viewport=1920×1080`、`screen=1920×1080`、`device_scale_factor=1`、`is_mobile=false`、`has_touch=false`。
- H5 继续使用 `390×844` 和原队列；App 继续使用原设备 Runtime 和原队列。
- Web 使用专属 Redis 等待队列和 `web_explorer` Worker 角色，默认并发为 1。
- 首期 Web 仅支持 `exploration_only`，不得发布 Inspection Work、执行 Judge 或生成正式报告。
- 不新增数据库列；冻结信息写入已有 JSONB 任务配置和 Run 快照。
- 所有新增注释使用中文，所有文件使用 UTF-8。

---

### Task 1: 冻结 Web 目标与 Browser Profile 契约

**Files:**
- Create: `services/api/app/application/web_browser_profile.py`
- Modify: `services/api/app/domain/task/targets.py`
- Modify: `services/api/app/domain/task/__init__.py`
- Modify: `services/api/app/api/v2/schemas.py`
- Modify: `services/api/app/application/v2_task_entries.py`
- Test: `services/api/tests/test_web_browser_profile.py`
- Test: `services/api/tests/test_task_target_contracts.py`
- Test: `services/api/tests/test_v2_task_entrypoints.py`

**Interfaces:**
- Produces: `WebTaskConfig(browser_profile_id: str)` and `WebBrowserProfileRegistry.resolve(profile_id: str) -> WebBrowserProfile`.
- Produces: `V2TaskSubmissionCommand.browser_profile_id: str | None`.
- Consumes: existing `_task_target` JSON snapshot and `TaskTarget.to_dict()` persistence contract.

- [x] **Step 1: Write failing domain and API tests**

```python
def test_web_target_freezes_registered_browser_profile() -> None:
    target = TaskTarget(
        platform=TaskTargetPlatform.WEB,
        target_url="https://example.test",
        web=WebTaskConfig(browser_profile_id="desktop_fhd_1920x1080_v1"),
    )
    assert target.to_dict()["web"] == {
        "browser_profile_id": "desktop_fhd_1920x1080_v1"
    }

def test_unknown_web_browser_profile_is_rejected() -> None:
    with pytest.raises(WebBrowserProfileUnsupportedError):
        WebBrowserProfileRegistry().resolve("unknown")
```

- [x] **Step 2: Run tests and verify RED**

Run: `D:\Project\SSC\alioth\.venv\Scripts\python.exe -m pytest -q services/api/tests/test_web_browser_profile.py services/api/tests/test_task_target_contracts.py`

Expected: FAIL because `WebTaskConfig` and `WebBrowserProfileRegistry` do not exist.

- [x] **Step 3: Implement the fixed profile and immutable target snapshot**

```python
@dataclass(frozen=True, slots=True)
class WebBrowserProfile:
    profile_id: str
    viewport_width: int
    viewport_height: int
    screen_width: int
    screen_height: int
    device_scale_factor: float
    is_mobile: bool
    has_touch: bool

class WebBrowserProfileRegistry:
    def resolve(self, profile_id: str) -> WebBrowserProfile:
        normalized = profile_id.strip()
        try:
            return self._profiles[normalized]
        except KeyError as exc:
            raise WebBrowserProfileUnsupportedError(normalized) from exc
```

- [x] **Step 4: Run focused tests and verify GREEN**

- [x] **Step 5: Commit**

```text
[AI编程] feat(web): 冻结桌面浏览器配置合同
模块: Web V2 目标与 API 契约；数据库迁移: 无；前端菜单/路由: 无；测试风险: 尚未接入 Worker。
```

### Task 2: 建立独立 WEB_V2 路由和功能门禁

**Files:**
- Modify: `services/api/app/application/app_workflow_router.py`
- Modify: `services/api/app/application/app_workflow_dispatch.py`
- Create: `services/api/app/application/web_workflow_dispatch.py`
- Modify: `services/api/app/core/config.py`
- Modify: `services/api/app/application/v2_task_entries.py`
- Modify: `services/api/app/api/v2/tasks.py`
- Test: `services/api/tests/test_app_workflow_router.py`
- Test: `services/api/tests/test_app_workflow_dispatch.py`
- Test: `services/api/tests/test_app_qc_settings.py`
- Test: `services/api/tests/test_v2_task_entrypoints.py`

**Interfaces:**
- Produces: `WorkflowRoute.WEB_V2`.
- Produces: `WebWorkflowHandler.run(record, settings, progress_recorder=None) -> RunResult`.
- Produces: `AppWorkflowDispatcher.dispatch_web_v2(record=record, settings=settings, progress_recorder=progress_recorder)` and `WebWorkflowNotConfiguredError`.
- Consumes: `Settings.workflow.web_v2_mode`.

- [x] **Step 1: Replace the legacy Web-to-H5 expectation with failing isolation tests**

```python
def test_web_v2_route_is_independent_from_h5_mode() -> None:
    router = AppWorkflowRouter(
        app_qc_enabled=False,
        h5_v2_mode="off",
        web_v2_mode="exploration_only",
    )
    assert router.route(target=_web_target(), workflow_version="v2") is WorkflowRoute.WEB_V2
```

- [x] **Step 2: Verify RED because `WEB_V2` and `web_v2_mode` are absent**

- [x] **Step 3: Implement fail-closed routing and Handler selection**

```python
if target.platform is TaskTargetPlatform.WEB:
    if workflow_version != "v2":
        raise WorkflowRoutingError("Web 任务仅支持 workflow_version=v2")
    if self._web_v2_mode == "off":
        raise WorkflowRoutingError("Web V2 Workflow 未开启")
    return WorkflowRoute.WEB_V2
```

- [x] **Step 4: Add entry-service tests for `off`, `exploration_only`, and `on`**

- [x] **Step 5: Verify H5/App routing tests remain GREEN**

- [x] **Step 6: Commit**

```text
[AI编程] feat(web): 增加独立WEB_V2路由门禁
模块: Workflow 路由与 V2 入口；数据库迁移: 无；前端菜单/路由: 无；测试风险: Web Runtime 尚未组合。
```

### Task 3: 隔离 Redis 等待队列与提交恢复路径

**Files:**
- Modify: `services/api/app/infrastructure/queue/redis_queue.py`
- Modify: `services/api/app/core/config.py`
- Modify: `services/api/app/application/task_submission.py`
- Modify: `services/api/app/application/task_submission_delivery.py`
- Modify: `services/api/app/api/v2/tasks.py`
- Test: `services/api/tests/test_redis_queue.py`
- Test: `services/api/tests/test_task_submission.py`
- Test: `services/api/tests/test_task_submission_reliability.py`
- Create: `services/api/tests/test_web_queue_isolation.py`

**Interfaces:**
- Produces: `RedisAgentRunQueue(client, ttl_seconds=ttl_seconds, queue_key=DEFAULT_AGENT_RUN_QUEUE_KEY)`.
- Produces: `QueueSettings.agent_run_queue_key` and `QueueSettings.web_agent_run_queue_key`.
- Consumes: `_task_target.platform` from each immutable run record.

- [x] **Step 1: Write failing queue-key behavior test**

```python
async def test_two_queue_instances_push_and_pop_different_waiting_lists() -> None:
    default_queue = RedisAgentRunQueue(client, ttl_seconds=60, queue_key="runs:default")
    web_queue = RedisAgentRunQueue(client, ttl_seconds=60, queue_key="runs:web")
    await default_queue.enqueue("h5")
    await web_queue.enqueue("web")
    assert await client.llen("runs:default") == 1
    assert await client.llen("runs:web") == 1
```

- [x] **Step 2: Verify RED because queue key is hard-coded**

- [x] **Step 3: Replace every waiting-list operation with `self._queue_key` while preserving shared record/cancel keys**

- [x] **Step 4: Route Web enqueue, restore, ensure-enqueued and queued cancellation through the Web queue**

- [x] **Step 5: Verify non-Web submissions still use the legacy queue key**

- [x] **Step 6: Commit**

```text
[AI编程] feat(web): 隔离Web探索Redis队列
模块: 任务提交与Redis投影；数据库迁移: 无；前端菜单/路由: 无；测试风险: 真实Redis恢复流程需集成环境复验。
```

### Task 4: 固定非移动 1920×1080 Browser Context

**Files:**
- Modify: `services/api/app/application/capture_coordination.py`
- Modify: `services/api/app/application/capture_coordinator.py`
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/app/infrastructure/browser/surface_driver.py`
- Create: `services/api/app/application/web_exploration_workflow.py`
- Test: `services/api/tests/test_page_capture_tool.py`
- Create: `services/api/tests/test_web_exploration_workflow.py`

**Interfaces:**
- Produces: `PageCaptureRequest.screen_width`, `screen_height`, `device_scale_factor`, `is_mobile`, `has_touch`.
- Produces: `WebSurfaceBootstrapper.start(command) -> CaptureCoordinationResult`.
- Consumes: `WebBrowserProfile.to_action_parameters()`.

- [x] **Step 1: Write a failing browser-context options test**

```python
async def test_web_profile_creates_desktop_context_at_1920x1080() -> None:
    request = PageCaptureRequest(
        mode=PageCaptureMode.GOTO,
        url="https://example.test",
        session_id="web-run",
        viewport_width=1920,
        viewport_height=1080,
        screen_width=1920,
        screen_height=1080,
        device_scale_factor=1,
        is_mobile=False,
        has_touch=False,
    )
    await browser.capture(request)
    assert fake_browser.context_options == {
        "ignore_https_errors": True,
        "viewport": {"width": 1920, "height": 1080},
        "screen": {"width": 1920, "height": 1080},
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,
    }
```

- [x] **Step 2: Verify RED because device fields are absent and `has_touch=True` is hard-coded**

- [x] **Step 3: Thread the immutable profile through CaptureCoordinator into PageCaptureRequest**

- [x] **Step 4: Implement `WebSurfaceBootstrapper` with a Web-specific profile and intent names**

- [x] **Step 5: Verify existing H5 tests still receive `390×844` and touch semantics**

- [x] **Step 6: Commit**

```text
[AI编程] feat(web): 增加1920x1080桌面浏览器会话
模块: Web Browser Context 与首跳双采；数据库迁移: 无；前端菜单/路由: 无；测试风险: Playwright浏览器需环境冒烟。
```

### Task 5: 接入独立 Web Worker 角色并禁止通用 Agent 回退

**Files:**
- Create: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/worker.py`
- Modify: `.env.example`
- Modify: `infra/env/.env.production.example`
- Test: `services/api/tests/test_worker.py`
- Create: `services/api/tests/test_worker_web_v2_route.py`

**Interfaces:**
- Produces: `build_web_workflow_handler(settings, worker_instance_id, queue=web_queue, state_store=state_store) -> WebWorkflowHandler`.
- Produces: `_process_web_v2_one(queue, record, settings=settings, state_store=state_store, dispatcher=dispatcher, worker_instance_id=worker_instance_id)` that only accepts `WorkflowRoute.WEB_V2`.
- Consumes: Web queue and `web_explorer` role; never receives the default queue in Web-only deployments.

- [x] **Step 1: Write failing tests proving Web goes only to `dispatch_web_v2`**

- [x] **Step 2: Write failing tests proving missing Web Handler produces `WEB_RUNTIME_NOT_CONFIGURED`**

- [x] **Step 3: Add a Web-only worker loop with `web_concurrency=1`**

- [x] **Step 4: Keep legacy explorer loops attached only to the legacy queue**

- [x] **Step 5: Close the Web browser session and save exploration-only terminal state without Judge/report publication**

- [x] **Step 6: Run worker, H5 V2 and App route regression tests**

- [x] **Step 7: Commit**

```text
[AI编程] feat(web): 接入独立Web探索Worker
模块: Worker组合根与运行终态；数据库迁移: 无；前端菜单/路由: 无；测试风险: PostgreSQL/Redis/Playwright联合链路需集成环境复验。
```

### Task 5A: 增加桌面首页探索、逐页清障、Navbar 导航与截图体积压缩

**Files:**
- Create: `services/api/app/application/web_desktop_navigation.py`
- Create: `services/api/app/infrastructure/browser/web_desktop_capture_policy.py`
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Create: `services/api/app/application/web_desktop_exploration.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Create: `services/api/tests/test_web_desktop_capture_policy.py`
- Create: `services/api/tests/test_web_desktop_navigation.py`
- Create: `services/api/tests/test_web_desktop_exploration.py`
- Test: `services/api/tests/test_web_exploration_workflow.py`
- Test: `services/api/tests/test_web_runtime_composition.py`

**Interfaces:**
- Produces: Web-only capture policy with `startup_policy=direct_home`, `full_page=false`,
  safe page-entry obstruction clearing, and lossless PNG size compression.
- Produces: case-insensitive `nav` substring matches in HTML class attributes, gated by
  `ul/li/a[href]`, as same-origin navigation candidates with stable page-type keys and
  deduplication.
- Consumes: fixed `desktop_fhd_1920x1080_v1`; never changes H5 capture defaults.

- [x] **Step 1: Write failing direct-home and page-entry obstruction tests**

- [x] **Step 2: Write failing fuzzy nav-class plus ul/li/a extraction tests**

- [x] **Step 3: Write failing PNG compression tests proving width/height stay 1920×1080**

- [x] **Step 4: Implement the Web-only capture decorator and navigation parser**

- [x] **Step 5: Feed Web navigation candidates into the bounded exploration timeline**

- [x] **Step 6: Run Web capture plus H5/App browser regressions**

- [x] **Step 7: Commit**

```text
[AI编程] feat(web): 增加桌面页面探索策略
模块: Web页面清障、导航发现与截图策略；数据库迁移: 无；前端菜单/路由: 无；测试风险: 真实站点复杂弹窗和PNG压缩率需灰度验证。
```

### Task 6: 开放受能力门禁控制的工作台入口

**Files:**
- Create: `services/api/app/api/v2/capabilities.py`
- Modify: `services/api/app/api/v2/router.py`
- Modify: `services/api/app/application/workstation_tasks.py`
- Modify: `services/api/app/infrastructure/db/workstation_query_repository.py`
- Create: `app/workstation/src/services/capabilities.api.ts`
- Modify: `app/workstation/src/types/task.ts`
- Modify: `app/workstation/src/services/tasks.api.ts`
- Modify: `app/workstation/src/modules/tasks/pages/TaskCreateView.vue`
- Modify: `app/workstation/src/modules/tasks/pages/TaskDetailView.vue`
- Test: `services/api/tests/test_workstation_task_service.py`
- Test: `services/api/tests/test_workstation_tasks_api.py`
- Create: `app/workstation/src/services/capabilities.api.test.ts`
- Test: `app/workstation/src/services/tasks.api.test.ts`
- Test: `app/workstation/src/modules/tasks/pages/TaskCreateView.test.ts`
- Test: `app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts`

**Interfaces:**
- Produces: `GET /api/v2/capabilities` with `web.enabled`, `web.execution_modes`, and registered profiles.
- Consumes: `ALIOTH_WEB_V2_MODE`; the UI does not infer deployment state.

- [x] **Step 1: Write failing API and component tests for disabled/enabled Web capability**

- [x] **Step 2: Implement the read-only capability endpoint**

- [x] **Step 3: Enable Web selection only when capability reports it available**

- [x] **Step 4: Submit a Web-specific URL and fixed profile ID without reading product `h5_url`**

- [x] **Step 5: Display declared `1920×1080` and profile ID on task detail**

- [x] **Step 6: Run backend and workstation focused tests**

- [x] **Step 7: Commit**

```text
[AI编程] feat(web): 开放受门禁控制的Web探索入口
模块: V2能力接口与工作台任务页面；数据库迁移: 无；前端菜单/路由: 无新增；测试风险: 需浏览器端联调部署能力接口。
```

### Task 7: 隔离回归与交付验证

**Files:**
- Modify: `docs/superpowers/specs/2026-08-29-web-v2-exploration-workflow-design.md` only if implementation reveals a contract correction.
- Test: all files added or modified above.

**Interfaces:**
- Consumes: all previous task outputs.
- Produces: verified M0/M1/M2 exploration-only branch.

- [x] **Step 1: Run backend route, entry, queue, browser and worker suites**

```powershell
D:\Project\SSC\alioth\.venv\Scripts\python.exe -m pytest -q `
  services/api/tests/test_app_workflow_router.py `
  services/api/tests/test_app_workflow_dispatch.py `
  services/api/tests/test_v2_task_entrypoints.py `
  services/api/tests/test_redis_queue.py `
  services/api/tests/test_web_queue_isolation.py `
  services/api/tests/test_web_browser_profile.py `
  services/api/tests/test_web_exploration_workflow.py `
  services/api/tests/test_worker_web_v2_route.py
```

- [x] **Step 2: Run H5/App regression suites**

- [x] **Step 3: Run workstation tests and production build**

- [x] **Step 4: Run formatting and static checks for changed files**

- [x] **Step 5: Inspect `git diff --check` and worktree status**

- [x] **Step 6: Commit final verification/documentation adjustments**

```text
[AI编程] test(web): 完成Web探索隔离回归
模块: Web/H5/App隔离回归；数据库迁移: 无；前端菜单/路由: 无新增；测试风险: 外部真实站点与生产Redis需灰度验证。
```
