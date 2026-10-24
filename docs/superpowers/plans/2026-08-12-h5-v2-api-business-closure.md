# H5 V2 API Business Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 H5 V2 Explorer 接入完整任务与仅探索 V2 API，并以证据交接门禁区分正式质检收口和仅留证收口。

**Architecture:** 完整质检和仅探索共享现有 H5 V2 Runtime，通过领域枚举 `TaskExecutionMode` 锁定执行模式。H5 Runtime 关闭 Session 后统一读取当前任务/Run 的已持久化 Capture，证据门禁通过后，完整质检发布正式 Inspection Work Item 并进入 Rule Judge/Completion Gate，仅探索只记录 `evidence_ready` 后结束。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL/asyncpg、Redis、Microsoft Agent Framework、Playwright、Vue 3、TypeScript、Vitest、pytest。

## Global Constraints

- 只接入 H5；Web 和仅探索 App 保持结构化未接入错误。
- 新 H5 V2 API 仅在 `ALIOTH_H5_V2_MODE=on` 时接受请求，禁止落入 V1 或 Shadow。
- `TaskExecutionMode` 只允许 `full_inspection` 和 `exploration_only`，未知值失败关闭；既有未声明任务兼容为 `full_inspection`。
- 仅探索不得发布 Inspection Work Item、调用 Rule Judge、写 `qc_rule_execution`、运行 Completion Gate 或生成报告。
- 完整质检只能在截图、DOM、页面判定和任务/Run 归属通过证据门禁后发布正式质检工作。
- 独立规则重测只写 `qc_rule_evaluation_run`，不得覆盖正式规则结果。
- 不新增自动登录；凭据、验证码、MFA 或扫码授权保持 `requires_auth`/人工复核。
- 所有新增注释使用中文，所有文件使用 UTF-8。
- 通用回归规则同步更新 `docs/专项能力与高频回归规则.md`。

---

### Task 1: 建立稳定执行模式领域合同

**Files:**
- Create: `services/api/app/domain/task/execution_mode.py`
- Modify: `services/api/app/domain/task/__init__.py`
- Test: `services/api/tests/test_task_execution_mode.py`

**Interfaces:**
- Produces: `TaskExecutionMode(StrEnum)`，成员 `FULL_INSPECTION`、`EXPLORATION_ONLY`。
- Produces: `task_execution_mode_from_config(config: Mapping[str, object] | None) -> TaskExecutionMode`。
- Produces: `lock_task_execution_mode(config: Mapping[str, object], mode: TaskExecutionMode) -> dict[str, object]`。

- [ ] **Step 1: 写执行模式失败测试**

```python
def test_missing_execution_mode_is_backward_compatible_full_inspection() -> None:
    assert task_execution_mode_from_config({}) is TaskExecutionMode.FULL_INSPECTION


def test_unknown_execution_mode_fails_closed() -> None:
    with pytest.raises(ValueError, match="execution_mode"):
        task_execution_mode_from_config({"execution_mode": "future"})


def test_lock_execution_mode_does_not_mutate_source() -> None:
    source = {"workflow_version": "v2"}
    locked = lock_task_execution_mode(source, TaskExecutionMode.EXPLORATION_ONLY)
    assert source == {"workflow_version": "v2"}
    assert locked["execution_mode"] == "exploration_only"
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `python -m pytest services/api/tests/test_task_execution_mode.py -q`

Expected: FAIL，原因是 `execution_mode` 模块或导出不存在。

- [ ] **Step 3: 实现最小领域合同**

```python
class TaskExecutionMode(StrEnum):
    FULL_INSPECTION = "full_inspection"
    EXPLORATION_ONLY = "exploration_only"


def task_execution_mode_from_config(
    config: Mapping[str, object] | None,
) -> TaskExecutionMode:
    value = (config or {}).get("execution_mode")
    if value is None:
        return TaskExecutionMode.FULL_INSPECTION
    try:
        return TaskExecutionMode(str(value))
    except ValueError as exc:
        raise ValueError(f"execution_mode 不受支持: {value}") from exc
```

`lock_task_execution_mode` 返回新字典并写入枚举值；在 `domain.task.__init__` 中统一导出。

- [ ] **Step 4: 运行领域测试和任务提交回归**

Run: `python -m pytest services/api/tests/test_task_execution_mode.py services/api/tests/test_task_submission.py -q`

Expected: PASS。

- [ ] **Step 5: 提交任务 1**

```powershell
git add services/api/app/domain/task/execution_mode.py services/api/app/domain/task/__init__.py services/api/tests/test_task_execution_mode.py
git commit -m "[AI编程] feat: 增加任务执行模式领域合同；模块：Task Domain；数据库迁移：否；前端菜单路由：否；测试风险：真实历史脏配置需部署前扫描"
```

### Task 2: 接入 H5 完整任务和仅探索 V2 API

**Files:**
- Modify: `services/api/app/application/v2_task_entries.py`
- Modify: `services/api/app/api/v2/tasks.py`
- Modify: `services/api/tests/test_v2_task_entrypoints.py`

**Interfaces:**
- Consumes: `TaskExecutionMode`、`lock_task_execution_mode`。
- Produces: `H5V2ModeNotEnabledError`。
- Produces: `ExplorationTaskV2EntryService`。
- Changes: `TaskSubmissionV2EntryService(..., h5_v2_mode: Literal["off", "shadow", "on"] = "off")`。

- [ ] **Step 1: 将原 H5 501 测试改为 H5 on 模式成功提交测试**

测试必须断言真实应用服务收到：

```python
assert call["target"].platform is TaskTargetPlatform.H5
assert call["target"].target_url == "https://m.example.test/home"
assert call["task_config"]["workflow_version"] == "v2"
assert call["task_config"]["execution_mode"] == "full_inspection"
```

同时新增 `off`、`shadow` 返回结构化 `H5V2ModeNotEnabledError` 且不调用下游的测试。

- [ ] **Step 2: 增加仅探索成功和平台边界失败测试**

成功用例使用 H5/on，断言：

```python
assert response.status_code == 202
assert response.json()["task_id"] == "task-h5"
assert call["task_config"]["execution_mode"] == "exploration_only"
```

App/Web 用例断言结构化 501 且下游零调用。

- [ ] **Step 3: 运行入口测试并确认旧占位行为导致失败**

Run: `python -m pytest services/api/tests/test_v2_task_entrypoints.py -q`

Expected: FAIL；H5 完整任务仍返回 501，仅探索仍由占位入口返回 501。

- [ ] **Step 4: 实现公共目标构造和两种提交服务**

`TaskSubmissionV2EntryService.submit()`：

```python
if command.platform is TaskTargetPlatform.H5:
    _require_h5_v2_on(self._h5_v2_mode)
    target = TaskTarget(platform=TaskTargetPlatform.H5, target_url=command.target_url)
elif command.platform is TaskTargetPlatform.APP:
    target = await self._app_target(command)
else:
    raise V2PlatformHandlerNotConfiguredError("web 平台尚未接入完整 V2 运行时")
return await self._submit(command, target, TaskExecutionMode.FULL_INSPECTION)
```

`ExplorationTaskV2EntryService.submit()` 只接受 H5/on，调用相同任务提交服务并将结果转换为 `ExplorationTaskSubmissionResult`。

- [ ] **Step 5: 替换 API 占位工厂并增加配置错误映射**

`_get_exploration_task_entry()` 与完整入口复用同一套数据库、Redis、状态存储装配；两个工厂都把 `settings.workflow.h5_v2_mode` 传给应用服务。H5 模式未开启映射为 HTTP 409 的结构化错误。

- [ ] **Step 6: 运行入口与路由回归**

Run: `python -m pytest services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_app_workflow_router.py -q`

Expected: PASS。

- [ ] **Step 7: 提交任务 2**

```powershell
git add services/api/app/application/v2_task_entries.py services/api/app/api/v2/tasks.py services/api/tests/test_v2_task_entrypoints.py
git commit -m "[AI编程] feat: 接入H5 V2任务入口；模块：V2 API/H5 Submission；数据库迁移：否；前端菜单路由：待后续同步；测试风险：真实Redis与PostgreSQL提交待冒烟"
```

### Task 3: 建立 H5 已持久化证据交接门禁

**Files:**
- Modify: `services/api/app/application/app_inspection_publisher.py`
- Create: `services/api/app/application/h5_evidence_handoff.py`
- Create: `services/api/app/infrastructure/db/h5_evidence_handoff_repository.py`
- Test: `services/api/tests/test_h5_evidence_handoff.py`

**Interfaces:**
- Produces: 通用别名 `CaptureInspectionPublisher`、`CaptureInspectionResult`、`build_default_capture_inspection_publisher()`；保留现有 `App*` 名称兼容调用方。
- Produces: `H5EvidenceCapture(capture_id, page_type, page_state, detection_status, capture_role, artifact_types)`。
- Produces: `H5EvidenceHandoffSummary(evidence_ready, judgeable_capture_count, page_types, published_work_item_count)`。
- Produces: `H5EvidenceHandoffService.prepare(task_id, agent_run_id, execution_mode) -> H5EvidenceHandoffSummary`。

- [ ] **Step 1: 写门禁失败测试**

覆盖以下独立行为：

```python
with pytest.raises(H5EvidenceNotReadyError, match="可判定"):
    await service.prepare(..., captures=[])
```

- Capture 属于其他 task/run 时仓储不得返回；
- 只有 `before_action` 时不得成为可判定库存；
- `detection_status != recognized` 或缺少 `page_type` 时失败；
- 缺少 `screenshot` 或 `dom` 任一核心制品时失败。

- [ ] **Step 2: 写两种模式副作用测试**

```python
summary = await service.prepare(
    task_id="task-1",
    agent_run_id="run-1",
    execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
)
assert summary.evidence_ready is True
assert publisher.calls == []
```

完整质检断言只对 `bootstrap`/`after_action`/`observation` 中可判定 Capture 调用 `publish_for_capture(..., snapshot_only=True)`，并汇总发布数量。

- [ ] **Step 3: 运行新测试并确认模块不存在而失败**

Run: `python -m pytest services/api/tests/test_h5_evidence_handoff.py -q`

Expected: FAIL。

- [ ] **Step 4: 泛化现有 Capture 质检发布器命名**

不改变现有行为，只增加通用名称并保留兼容别名：

```python
CaptureInspectionResult = AppCaptureInspectionResult
CaptureInspectionPublisher = AppCaptureInspectionPublisher


def build_default_capture_inspection_publisher() -> CaptureInspectionPublisher:
    return CaptureInspectionPublisher()
```

同时将错误文案和日志中的仅 App 表述改为 Capture 通用表述，现有 App 测试必须继续通过。

- [ ] **Step 5: 实现 PostgreSQL 证据库存读取器**

查询必须限定：

```sql
WHERE common_capture.task_id = $1::uuid
  AND common_capture.agent_run_id = $2::uuid
  AND common_capture.surface_kind = 'web'
  AND common_capture.capture_status = 'captured'
  AND common_capture.capture_role IN ('bootstrap', 'after_action', 'observation')
```

联接 `qc_page_capture` 和 `qc_evidence_artifact`，返回页面判定事实与实际制品类型；排除 `superseded`、`abandoned`。

- [ ] **Step 6: 实现模式感知的交接服务**

共同门禁先验证全部可判定 Capture；只有 `FULL_INSPECTION` 才调用通用 Capture 发布器，`EXPLORATION_ONLY` 只返回证据摘要。未知执行模式在进入服务前已由领域解析失败关闭。

- [ ] **Step 7: 运行门禁、发布器和规则工作项回归**

Run: `python -m pytest services/api/tests/test_h5_evidence_handoff.py services/api/tests/test_app_inspection_work_item_publish.py services/api/tests/test_inspection_work_items.py -q`

Expected: PASS。

- [ ] **Step 8: 提交任务 3**

```powershell
git add services/api/app/application/app_inspection_publisher.py services/api/app/application/h5_evidence_handoff.py services/api/app/infrastructure/db/h5_evidence_handoff_repository.py services/api/tests/test_h5_evidence_handoff.py
git commit -m "[AI编程] feat: 增加H5证据交接门禁；模块：H5 Evidence/Inspection；数据库迁移：否；前端菜单路由：否；测试风险：真实制品存储完整性待集成验证"
```

### Task 4: 在 Worker 中闭合两种 H5 收口路径

**Files:**
- Modify: `services/api/app/worker.py`
- Modify: `services/api/tests/test_worker_h5_v2_route.py`
- Test: `services/api/tests/test_worker_h5_v2_finalization.py`

**Interfaces:**
- Consumes: `task_execution_mode_from_config`。
- Consumes: `H5EvidenceHandoffPort.prepare(...)`。
- Changes: `_process_one(..., h5_evidence_handoff: H5EvidenceHandoffPort | None = None)`。
- Changes: `_process_h5_v2_one(..., h5_evidence_handoff: H5EvidenceHandoffPort | None = None)`。

- [ ] **Step 1: 写仅探索 Worker 失败测试**

构造 `execution_mode=exploration_only` 的 H5 V2 Run，Fake Handler 返回完成。断言：

```python
assert handoff.calls == [(task_id, run_id, TaskExecutionMode.EXPLORATION_ONLY)]
assert rule_writer.calls == []
assert report_generator.calls == []
assert finalization_service.calls == []
assert finished_record.usage["h5_evidence_ready"] is True
```

- [ ] **Step 2: 写完整质检 Worker 失败测试**

断言顺序为：H5 Runtime → 证据交接/正式工作发布 → 工作项排空 → 正式规则结果收口 → Completion Gate/报告。证据交接失败时后续步骤零调用且 Run 为失败/人工复核。

- [ ] **Step 3: 运行 Worker 新测试并确认当前实现未分流而失败**

Run: `python -m pytest services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_worker_h5_v2_route.py -q`

Expected: FAIL；当前 Worker 无执行模式和交接门禁。

- [ ] **Step 4: 注入并调用 H5 证据交接服务**

在 `_apply_result()` 之后解析执行模式。只有 Runtime 产生可收口结果时调用：

```python
summary = await handoff.prepare(
    task_id=record.task_id,
    agent_run_id=record.run_id,
    execution_mode=execution_mode,
)
```

将 `evidence_ready`、Capture 数、页面类型数和正式工作项数写入 `record.usage`，不把原始 DOM、截图或敏感内容写入 Redis。

- [ ] **Step 5: 实现仅探索直接终态路径**

`EXPLORATION_ONLY` 跳过三个正式质检收口函数，并调用 `_finish_terminal_run` 时显式关闭 V2 Finalization/报告路径。Run/Task 长期状态仍由状态存储记录为终态。

- [ ] **Step 6: 保持完整质检现有收口并把门禁放在最前**

`FULL_INSPECTION` 在交接成功后继续 `_drain_snapshot_inspection_work`、`_finalize_rule_executions`、`_finalize_persisted_inspection_work` 和 Completion Gate。任一失败保持 fail closed。

- [ ] **Step 7: 在 Worker 组合根创建默认 H5 交接服务**

仅当 `h5_v2_mode=on` 时构建生产实现，并传入每次 `_process_one`；测试可注入 Fake。

- [ ] **Step 8: 运行 Worker、H5 Runtime 和 Completion Gate 回归**

Run: `python -m pytest services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker.py services/api/tests/test_task_finalization_persistence.py -q`

Expected: PASS。

- [ ] **Step 9: 提交任务 4**

```powershell
git add services/api/app/worker.py services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_worker_h5_v2_finalization.py
git commit -m "[AI编程] feat: 闭合H5双模式Worker收口；模块：H5 Worker/Completion Gate；数据库迁移：否；前端菜单路由：否；测试风险：真实PG/Redis/Playwright事务顺序待冒烟"
```

### Task 5: 开放工作站仅探索入口

**Files:**
- Modify: `app/workstation/src/types/task.ts`
- Modify: `app/workstation/src/services/tasks.api.ts`
- Modify: `app/workstation/src/services/tasks.api.test.ts`
- Modify: `app/workstation/src/modules/tasks/pages/TaskCreateView.vue`
- Modify: `app/workstation/src/modules/tasks/pages/TaskCreateView.test.ts`

**Interfaces:**
- Produces: `V2TaskExecutionMode = 'full_inspection' | 'exploration_only'`。
- Produces: `submitExplorationTask(payload: V2TaskCreatePayload)`，调用 `/api/v2/exploration-tasks`。

- [ ] **Step 1: 写前端 API 失败测试**

```typescript
await submitExplorationTask(payload)
expect(url).toBe('/api/v2/exploration-tasks')
expect(init?.method).toBe('POST')
```

- [ ] **Step 2: 写创建页两种任务类型失败测试**

覆盖：完整质检调用 `submitV2Task`；仅探索 H5 调用 `submitExplorationTask`；仅探索不可与 App/Web 同时提交；成功后进入现有详情页。

- [ ] **Step 3: 运行前端定向测试并确认按钮仍禁用而失败**

Run: `npm.cmd test -- tasks.api.test.ts TaskCreateView.test.ts`

Expected: FAIL。

- [ ] **Step 4: 实现 API 和类型**

`submitExplorationTask` 与完整提交使用相同 payload 和响应类型，仅切换 V2 路径。

- [ ] **Step 5: 实现任务类型交互**

将禁用单选改为受控 `execution_mode`。仅探索时平台选择只保留 H5，或在平台切换时恢复 `full_inspection` 并显示明确提示；提交函数按模式选择服务。

- [ ] **Step 6: 运行前端测试和生产构建**

Run: `npm.cmd test -- tasks.api.test.ts TaskCreateView.test.ts`

Run: `npm.cmd run build`

Expected: PASS。

- [ ] **Step 7: 提交任务 5**

```powershell
git add app/workstation/src/types/task.ts app/workstation/src/services/tasks.api.ts app/workstation/src/services/tasks.api.test.ts app/workstation/src/modules/tasks/pages/TaskCreateView.vue app/workstation/src/modules/tasks/pages/TaskCreateView.test.ts
git commit -m "[AI编程] feat: 开放H5仅探索任务入口；模块：Workstation Task Create；数据库迁移：否；前端菜单路由：无新增路由；测试风险：浏览器真实交互与后端联调待冒烟"
```

### Task 6: 补齐闭环回归与规则文档

**Files:**
- Create: `services/api/tests/test_h5_v2_api_business_closure.py`
- Modify: `services/api/tests/test_v2_task_entrypoints.py`
- Modify: `docs/专项能力与高频回归规则.md`
- Modify: `docs/workflows/h5-v2-explorer-workflow.txt`

**Interfaces:**
- Verifies: API 命令模式、Worker 模式、证据交接副作用和独立规则测试隔离。

- [ ] **Step 1: 写跨组件闭环测试**

使用真实应用层组件和内存/Fake 外部端口，分别验证：

```text
H5 full API -> queued run(full_inspection)
             -> H5 handler
             -> evidence handoff publishes
             -> formal finalization

H5 exploration API -> queued run(exploration_only)
                    -> H5 handler
                    -> evidence_ready
                    -> no judge/report
                    -> independent rule evaluation remains separate
```

- [ ] **Step 2: 运行闭环测试并确认任何遗漏会失败**

Run: `python -m pytest services/api/tests/test_h5_v2_api_business_closure.py -q`

Expected: 首次运行若暴露接口或收口遗漏则 FAIL；修正实现后必须 PASS。

- [ ] **Step 3: 更新专项回归规则**

在 `docs/专项能力与高频回归规则.md` 记录：新控制面入口必须真实接到平台 Runtime；仅探索必须在发布正式质检库存前分流；证据门禁必须校验 task/run/role/page_type/screenshot/DOM；H5 V2 新入口必须在 `on` 模式 fail closed。

- [ ] **Step 4: 更新 H5 Workflow 文本**

在入口和收口段增加 `full_inspection`/`exploration_only` 分支、V2 API 路径、证据门禁与独立规则重测关系。

- [ ] **Step 5: 运行文档编码和闭环回归**

Run: `python -m pytest services/api/tests/test_h5_v2_api_business_closure.py services/api/tests/test_v2_task_entrypoints.py -q`

Run: `git diff --check`

Expected: PASS，且无空白错误。

- [ ] **Step 6: 提交任务 6**

```powershell
git add services/api/tests/test_h5_v2_api_business_closure.py services/api/tests/test_v2_task_entrypoints.py docs/专项能力与高频回归规则.md docs/workflows/h5-v2-explorer-workflow.txt
git commit -m "[AI编程] test: 固化H5 V2 API业务闭环回归；模块：H5/V2 API/Workflow；数据库迁移：否；前端菜单路由：否；测试风险：生产依赖仍需部署冒烟"
```

### Task 7: 全量验证、深度审核与整改

**Files:**
- Modify: 仅限审核发现的相关文件。

**Interfaces:**
- Verifies: 设计规格全部验收条件。

- [ ] **Step 1: 运行后端相关测试**

Run: `python -m pytest services/api/tests/test_task_execution_mode.py services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_h5_evidence_handoff.py services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_rule_evaluation_runs.py services/api/tests/test_h5_v2_api_business_closure.py -q`

Expected: PASS。

- [ ] **Step 2: 运行后端全量测试**

Run: `python -m pytest services/api/tests -q`

Expected: PASS；如受外部环境影响，必须区分真实代码失败和环境阻断并记录证据。

- [ ] **Step 3: 运行前端全量测试和构建**

Run: `npm.cmd test -- --run`

Run: `npm.cmd run build`

Expected: PASS。

- [ ] **Step 4: 执行静态检查与变更审计**

Run: `git diff --check dev_1.5...HEAD`

Run: `git status --short`

逐项审查：异常映射、平台边界、配置门禁、执行模式解析、数据库查询归属、核心制品要求、仅探索零副作用、正式结果隔离、Session 关闭顺序、日志敏感信息、前端错误反馈。

- [ ] **Step 5: 使用 requesting-code-review 做独立严格审核**

以 `dev_1.5` 为 BASE，当前 HEAD 为目标，要求 reviewer 对照设计规格报告 Critical/Important/Minor 问题。Critical 和 Important 必须修复并重新运行相关测试。

- [ ] **Step 6: 条件允许时执行真实依赖冒烟**

验证本机/部署环境是否提供 PostgreSQL、Redis、Playwright 和证据存储配置。若提供，则使用安全测试 H5 URL 完成一次仅探索和一次完整质检；若未提供，输出精确的未验收依赖清单，不声称真实环境闭环已验证。

- [ ] **Step 7: 提交审核整改**

若审核产生整改，按本计划限定的相关文件显式暂存；不存在整改时跳过此提交：

```powershell
git add services/api/app/domain/task/execution_mode.py services/api/app/domain/task/__init__.py services/api/app/application/v2_task_entries.py services/api/app/api/v2/tasks.py services/api/app/application/app_inspection_publisher.py services/api/app/application/h5_evidence_handoff.py services/api/app/infrastructure/db/h5_evidence_handoff_repository.py services/api/app/worker.py services/api/tests app/workstation/src/types/task.ts app/workstation/src/services/tasks.api.ts app/workstation/src/services/tasks.api.test.ts app/workstation/src/modules/tasks/pages/TaskCreateView.vue app/workstation/src/modules/tasks/pages/TaskCreateView.test.ts docs/专项能力与高频回归规则.md docs/workflows/h5-v2-explorer-workflow.txt
git commit -m "[AI编程] fix: 整改H5 V2 API闭环审核问题；模块：H5/V2 API/Worker；数据库迁移：否；前端菜单路由：无新增路由；测试风险：真实生产依赖仍需灰度验收"
```

- [ ] **Step 8: 运行最终新鲜验证并输出业务闭环审核结论**

最终结论必须区分：代码级闭环、自动化测试闭环、真实依赖冒烟、生产灰度配置四个层级；任何未验证层级都明确标记，禁止用“应该可用”替代证据。
