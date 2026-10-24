# App Screenshot Exploration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Fake Runtime 下实现 `APP_READY → Screenshot Understanding → Exploration Work → candidate_id → ActionIntent → before/action/after App Capture` 的完整单轮闭环。

**Architecture:** 复用公共 `SurfaceObservation`、`FrontierSnapshot`、`ExplorationWork` 和工作状态机，新增截图理解、视觉事实及 App 候选解析层。将 Capture Coordinator 的页面专属引用迁移为公共 `CaptureRef`，让 H5 与 App 共用动作证据编排；正式 Worker 路由继续关闭。

**Tech Stack:** Python 3.11、FastAPI 应用层、PostgreSQL/asyncpg、Microsoft Agent Framework、pytest、Ruff。

## Global Constraints

- App 页面事实只有 PNG Screenshot；禁止构造或请求 DOM、UI Tree、Visible Text、CSS、XPath 和 Appium locator。
- 图片字节只在 Artifact Store 与受信任 Screenshot Understanding Provider 之间传递。
- Explorer 只能选择当前 Frontier 已存在的 `candidate_id`，不能生成坐标。
- 坐标由 Runtime 在 Session Command Gate 内确定性解析。
- `qc_capture` 是公共 Capture 身份，`qc_page_capture` 与 `qc_app_capture` 是平台子事实。
- 继续使用现有任务、Work Item、Action Execution 和规则终态，不建立 App 专属状态机。
- Fake Runtime 只允许非生产环境；本计划不实现正式星语 Adapter。
- 本计划不修改生产 `worker._process_one()` 的 App 路由。
- 所有新增注释使用中文，所有文件使用 UTF-8。
- 每个任务必须严格执行 RED → GREEN → REFACTOR，并单独提交。

---

### Task 1: 冻结 App 视觉理解合同

**Files:**
- Create: `services/api/app/domain/exploration/app_visual.py`
- Modify: `services/api/app/domain/exploration/__init__.py`
- Test: `services/api/tests/test_app_visual_contracts.py`

**Interfaces:**
- Consumes: `InteractionOperation`、`InteractionRiskLevel`、协议校验工具。
- Produces: `NormalizedBounds`、`AppVisualCandidate`、`AppVisualAnalysis`、`APP_VISUAL_ANALYSIS_VERSION="app-visual-analysis.v1"`。

- [x] **Step 1: Write the failing contract tests**

```python
def test_app_visual_analysis_round_trip_and_stable_candidate_identity():
    analysis = AppVisualAnalysis.build(
        capture_id="capture-1",
        screenshot_artifact_id="artifact-1",
        image_signature="sha256:" + "a" * 64,
        width=1080,
        height=1920,
        orientation="portrait",
        visual_page_type="login",
        visual_summary="登录页",
        key_elements=("登录按钮",),
        candidates=(
            AppVisualCandidate.build(
                source_capture_id="capture-1",
                source_image_signature="sha256:" + "a" * 64,
                source_width=1080,
                source_height=1920,
                source_orientation="portrait",
                semantic_name="登录按钮",
                action_type=InteractionOperation.CLICK,
                normalized_bounds=NormalizedBounds(0.2, 0.7, 0.8, 0.8),
                confidence=0.95,
                risk_level=InteractionRiskLevel.LOW,
                purpose="进入系统",
            ),
        ),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
    )
    restored = AppVisualAnalysis.from_dict(analysis.to_dict())
    assert restored == analysis
    assert restored.candidates[0].candidate_id.startswith("candidate:")
```

另写独立用例证明越界 bounds、逆序 bounds、NaN confidence、重复 candidate ID、候选源 Capture 不一致和不允许动作均被拒绝。

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest services/api/tests/test_app_visual_contracts.py -q`

Expected: FAIL because `app_visual` and its exported contracts do not exist.

- [x] **Step 3: Implement immutable versioned contracts**

```python
@dataclass(frozen=True, slots=True)
class NormalizedBounds:
    left: float
    top: float
    right: float
    bottom: float

    @property
    def center(self) -> tuple[float, float]:
        return ((self.left + self.right) / 2, (self.top + self.bottom) / 2)

@dataclass(frozen=True, kw_only=True)
class AppVisualCandidate:
    candidate_id: str
    source_capture_id: str
    source_image_signature: str
    source_width: int
    source_height: int
    source_orientation: str
    semantic_name: str
    action_type: InteractionOperation
    normalized_bounds: NormalizedBounds | None
    confidence: float
    risk_level: InteractionRiskLevel
    purpose: str
    expected_state_hint: str | None = None
```

`build()` 必须用 `stable_protocol_hash` 从源 Capture、动作、语义名称和 bounds 生成 candidate ID。只允许 `click/scroll/back/observe`；click 必须有 bounds。

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest services/api/tests/test_app_visual_contracts.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```powershell
git add services/api/app/domain/exploration/app_visual.py services/api/app/domain/exploration/__init__.py services/api/tests/test_app_visual_contracts.py
git commit -m "[AI编程] feat: 冻结 App 视觉理解合同" -m "涉及模块：App Screenshot Understanding；数据库迁移：无；前端菜单/路由：无需同步；测试风险：尚未接真实视觉模型。"
```

### Task 2: 实现截图可信读取与 Fake Understanding

**Files:**
- Create: `services/api/app/application/app_screenshot_understanding.py`
- Create: `services/api/app/infrastructure/app_vision/fake.py`
- Create: `services/api/app/infrastructure/app_vision/__init__.py`
- Test: `services/api/tests/test_app_screenshot_understanding.py`
- Test: `services/api/tests/test_fake_app_vision.py`

**Interfaces:**
- Consumes: `CaptureRef`、`PersistedAppCapture`、Artifact Reader、Task 1 合同。
- Produces: `AppScreenshotSourceRepository.load(capture)`、`ScreenshotArtifactReader.read(artifact)`、`ScreenshotUnderstandingProvider.understand(request)`、`AppScreenshotUnderstandingService.understand(capture)`。

- [x] **Step 1: Write failing service tests**

```python
@pytest.mark.asyncio
async def test_understanding_verifies_checksum_before_provider_call():
    service = AppScreenshotUnderstandingService(
        sources=FakeSourceRepository(checksum="sha256:" + "0" * 64),
        artifacts=FakeArtifactReader(PNG_BYTES),
        provider=FailIfCalledProvider(),
    )
    with pytest.raises(AppScreenshotUnderstandingError, match="checksum"):
        await service.understand(_app_capture_ref())
```

另写成功用例，断言服务返回标准化 `AppVisualAnalysis`，Provider 输入收到 PNG，返回结果和 Agent/Work 投影不含图片字节或 Base64。

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest services/api/tests/test_app_screenshot_understanding.py services/api/tests/test_fake_app_vision.py -q`

Expected: FAIL because the service and provider do not exist.

- [x] **Step 3: Implement ports, checksum gate and deterministic provider**

```python
class ScreenshotUnderstandingProvider(Protocol):
    async def understand(self, request: UnderstandAppScreenshotRequest) -> AppVisualAnalysis: ...

class AppScreenshotUnderstandingService:
    async def understand(self, capture: CaptureRef) -> AppVisualAnalysis:
        source = await self._sources.load(capture=capture)
        content = await self._artifacts.read(source.screenshot)
        if _sha256(content) != source.image_signature:
            raise AppScreenshotUnderstandingError("Screenshot checksum 校验失败", code="SCREENSHOT_CHECKSUM_MISMATCH")
        return _validate_provider_result(await self._provider.understand(...), source)
```

Fake Provider 根据 `image_signature` 从场景映射生成一个低风险 click 候选；未配置场景时返回空 Frontier，而不是猜测控件。

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest services/api/tests/test_app_screenshot_understanding.py services/api/tests/test_fake_app_vision.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```powershell
git add services/api/app/application/app_screenshot_understanding.py services/api/app/infrastructure/app_vision services/api/tests/test_app_screenshot_understanding.py services/api/tests/test_fake_app_vision.py
git commit -m "[AI编程] feat: 实现 App 截图理解边界" -m "涉及模块：App Screenshot Understanding；数据库迁移：无；前端菜单/路由：无需同步；测试风险：Fake Provider 仅验证确定性合同。"
```

### Task 3: 持久化不可变 App 视觉事实

**Files:**
- Create: `services/api/migrations/versions/20260804_0036_app_visual_observation.py`
- Create: `services/api/app/infrastructure/db/app_visual_observation_repository.py`
- Test: `services/api/tests/test_app_visual_observation_migration.py`
- Test: `services/api/tests/test_app_visual_observation_repository.py`

**Interfaces:**
- Consumes: `AppVisualAnalysis`。
- Produces: `PostgresAppVisualObservationRepository.get_by_capture_id()` 与 `save()`，返回 `SavedAppVisualObservation(analysis, created)`。

- [x] **Step 1: Write failing migration and repository tests**

```python
def test_migration_creates_capture_unique_visual_fact_without_task_status():
    sql = migration.upgrade_sql()
    assert "CREATE TABLE qc_app_visual_observation" in sql
    assert "UNIQUE (capture_id)" in sql
    assert "task_status" not in sql

@pytest.mark.asyncio
async def test_save_rejects_same_capture_with_different_result_hash():
    repository = PostgresAppVisualObservationRepository(pool=fake_pool)
    await repository.save(first_analysis)
    with pytest.raises(AppVisualObservationPersistenceError):
        await repository.save(conflicting_analysis)
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest services/api/tests/test_app_visual_observation_migration.py services/api/tests/test_app_visual_observation_repository.py -q`

Expected: FAIL because migration and repository do not exist.

- [x] **Step 3: Implement migration and immutable upsert**

`save()` 使用 `INSERT ... ON CONFLICT (capture_id) DO UPDATE SET capture_id=existing.capture_id WHERE existing.result_hash=EXCLUDED.result_hash RETURNING ...`。SQL 必须连接 `qc_capture`、`qc_app_capture` 和 screenshot artifact，校验 task/session/artifact 归属；冲突 hash 返回明确持久化错误。

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest services/api/tests/test_app_visual_observation_migration.py services/api/tests/test_app_visual_observation_repository.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```powershell
git add services/api/migrations/versions/20260804_0036_app_visual_observation.py services/api/app/infrastructure/db/app_visual_observation_repository.py services/api/tests/test_app_visual_observation_migration.py services/api/tests/test_app_visual_observation_repository.py
git commit -m "[AI编程] feat: 持久化 App 视觉观察事实" -m "涉及模块：App Screenshot Understanding、PostgreSQL；数据库迁移：新增 0036；前端菜单/路由：无需同步；测试风险：需在集成环境执行真实迁移。"
```

### Task 4: 从 APP_READY 幂等发布 App Exploration Work

**Files:**
- Create: `services/api/app/application/app_screenshot_exploration.py`
- Modify: `services/api/app/infrastructure/db/explorer_work_repository.py`
- Test: `services/api/tests/test_app_screenshot_exploration.py`
- Modify: `services/api/tests/test_v2_control_plane_repositories.py`

**Interfaces:**
- Consumes: `AppReadyHandoff`、Task 2 service、Task 3 repository、`StartExplorationCommand`、`ExplorationWorkPublisher`。
- Produces: `AppScreenshotExplorationWorkflow.run(command) -> AppScreenshotExplorationResult`。

- [x] **Step 1: Write failing workflow and common Capture publishing tests**

```python
@pytest.mark.asyncio
async def test_replayed_handoff_reuses_visual_fact_and_work():
    first = await workflow.run(_command())
    second = await workflow.run(_command())
    assert second.observation_id == first.observation_id
    assert second.work_id == first.work_id
    assert understanding.call_count == 1

def test_exploration_publish_joins_common_capture_fact():
    source = inspect.getsource(PostgresExplorerWorkRepository.publish)
    assert "INNER JOIN qc_capture AS capture" in source
    assert "INNER JOIN qc_page_capture AS capture" not in source
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest services/api/tests/test_app_screenshot_exploration.py services/api/tests/test_v2_control_plane_repositories.py -q`

Expected: FAIL because workflow is absent and repository still joins page Capture.

- [x] **Step 3: Implement deterministic mapping and publish**

构造 `SurfaceObservation` 时：`location="app://<package>/<activity-or-unknown>"`，`state_version=image_signature`，metadata 只含 screenshot artifact ID、尺寸和方向。候选映射到公共 `CandidateAction`，metadata 只含 `candidate_id` 与受限状态提示，不放 bounds；Resolver 后续从视觉事实表加载 bounds。

Work 的 branch ID 使用 `app:<capture_id>`，objective 使用命令锁定值，deadline、budget 和 boundaries 由 Orchestrator 命令提供。仓储发布改连 `qc_capture`。

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest services/api/tests/test_app_screenshot_exploration.py services/api/tests/test_v2_control_plane_repositories.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```powershell
git add services/api/app/application/app_screenshot_exploration.py services/api/app/infrastructure/db/explorer_work_repository.py services/api/tests/test_app_screenshot_exploration.py services/api/tests/test_v2_control_plane_repositories.py
git commit -m "[AI编程] feat: 发布 App 截图探索工作" -m "涉及模块：App Workflow、Explorer Work；数据库迁移：使用 0036；前端菜单/路由：无需同步；测试风险：尚未连接正式 Worker 唤醒。"
```

### Task 5: 让 Explorer 按 Surface 生成安全 Capture Profile

**Files:**
- Modify: `services/api/app/agents/explorer/maf_agent.py`
- Modify: `services/api/app/application/capture_profiles.py`
- Test: `services/api/tests/test_maf_explorer_agent.py`
- Test: `services/api/tests/test_capture_profile_policy.py`

**Interfaces:**
- Consumes: `CaptureProfilePolicy.for_surface(surface_kind, operation)`。
- Produces: Android ActionIntent 只请求 screenshot/action_trace，Web 行为保持不变。

- [x] **Step 1: Write failing behavior tests**

```python
@pytest.mark.asyncio
async def test_android_explorer_intent_never_requests_dom_artifacts():
    decision = await explorer.decide_next(_android_work_with_click_candidate())
    assert decision.capture_profile.before.artifact_types == ("screenshot", "action_trace")
    assert decision.capture_profile.after.artifact_types == ("screenshot", "action_trace")
    assert decision.parameters == {"candidate_id": "candidate:known"}
```

另保留 Web 用例断言现有 DOM 采集不变，并新增 App 非 allowlist 候选被过滤的用例。

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest services/api/tests/test_maf_explorer_agent.py services/api/tests/test_capture_profile_policy.py -q`

Expected: FAIL because `_BASE_ARTIFACTS` still applies to Android.

- [x] **Step 3: Inject and use CaptureProfilePolicy**

删除 Agent 内全局 `_BASE_ARTIFACTS` 决策；`_build_action_intent()` 调用策略并只把 candidate ID 放入 parameters。所有 bounds、图像签名和 locator 都不得进入 Agent 生成的 ActionIntent。

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest services/api/tests/test_maf_explorer_agent.py services/api/tests/test_capture_profile_policy.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```powershell
git add services/api/app/agents/explorer/maf_agent.py services/api/app/application/capture_profiles.py services/api/tests/test_maf_explorer_agent.py services/api/tests/test_capture_profile_policy.py
git commit -m "[AI编程] feat: 隔离 App Explorer 采集能力" -m "涉及模块：Explorer Agent、Capture Profile；数据库迁移：无；前端菜单/路由：无需同步；测试风险：真实模型候选选择尚未联调。"
```

### Task 6: 将 Capture Coordinator 迁移到公共 CaptureRef

**Files:**
- Modify: `services/api/app/application/capture_coordination.py`
- Modify: `services/api/app/application/capture_coordinator.py`
- Modify: `services/api/app/infrastructure/browser/capture_runtime.py`
- Modify: `services/api/app/domain/interaction/captures.py`
- Modify: `services/api/tests/test_capture_coordinator.py`
- Modify: `services/api/tests/test_web_capture_runtime.py`

**Interfaces:**
- Consumes: `CaptureRef.capture_id`。
- Produces: `CaptureWriter.capture() -> CaptureRef` 与公共 `CaptureCoordinationResult`；`CaptureReference` 保留反序列化兼容但不再作为 Coordinator 类型。

- [x] **Step 1: Write failing common-reference tests**

```python
@pytest.mark.asyncio
async def test_coordinator_accepts_android_capture_refs():
    result = await coordinator.coordinate(_android_command())
    assert result.before_capture.capture_kind is CaptureKind.APP
    assert result.after_capture.parent_capture_id == result.before_capture.capture_id

@pytest.mark.asyncio
async def test_browser_writer_returns_page_capture_ref():
    capture = await writer.capture(_web_request())
    assert capture.capture_kind is CaptureKind.PAGE
    assert capture.capture_id == persisted.page_capture_id
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest services/api/tests/test_capture_coordinator.py services/api/tests/test_web_capture_runtime.py -q`

Expected: FAIL because Coordinator requires `CaptureReference.page_capture_id`.

- [x] **Step 3: Migrate the shared boundary**

Rename `CaptureWriteRequest.parent_page_capture_id` to `parent_capture_id`; update validation, error payloads and Coordinator to use `CaptureRef.capture_id`。Browser writer maps persisted page capture into:

```python
CaptureRef(
    task_id=page.task_id,
    session_id=page.session_id,
    capture_id=page.page_capture_id,
    surface_kind=SurfaceKind.WEB,
    capture_kind=CaptureKind.PAGE,
    role=request.directive.role,
    artifact_ids=page.artifact_ids,
    parent_capture_id=page.parent_page_capture_id,
    action_execution_id=page.action_execution_id,
)
```

- [x] **Step 4: Run focused and H5 regression tests**

Run: `python -m pytest services/api/tests/test_capture_coordinator.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_explorer_turn.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```powershell
git add services/api/app/application/capture_coordination.py services/api/app/application/capture_coordinator.py services/api/app/infrastructure/browser/capture_runtime.py services/api/app/domain/interaction/captures.py services/api/tests/test_capture_coordinator.py services/api/tests/test_web_capture_runtime.py
git commit -m "[AI编程] refactor: 统一 Web App Capture 编排引用" -m "涉及模块：Interaction Runtime、Web Capture；数据库迁移：无；前端菜单/路由：无需同步；测试风险：需全量回归历史 Capture 调用方。"
```

### Task 7: 实现 App before/action/after Runtime Adapter

**Files:**
- Create: `services/api/app/application/app_interaction_runtime.py`
- Modify: `services/api/app/application/app_capture_persistence.py`
- Modify: `services/api/app/infrastructure/db/app_capture_repository.py`
- Test: `services/api/tests/test_app_interaction_runtime.py`
- Modify: `services/api/tests/test_app_capture_persistence.py`
- Modify: `services/api/tests/test_app_capture_repository.py`

**Interfaces:**
- Consumes: `SurfaceDriver`、`SurfaceSessionRepository`、Task 3 visual repository、Task 6 `CaptureWriter/InteractionExecutor`。
- Produces: `AppCaptureWriter`、`AppCandidateResolver.resolve()`、`AppInteractionExecutor.execute()`。

- [x] **Step 1: Write failing stale-candidate and success tests**

```python
@pytest.mark.asyncio
async def test_stale_candidate_does_not_call_driver():
    executor = _executor(before_signature="sha256:" + "b" * 64)
    with pytest.raises(AppInteractionRuntimeError) as exc:
        await executor.execute(_request(candidate_signature="sha256:" + "a" * 64))
    assert exc.value.error_code == "STALE_VISUAL_CANDIDATE"
    assert driver.execute_count == 0

@pytest.mark.asyncio
async def test_click_resolves_normalized_center_inside_runtime():
    result = await executor.execute(_request(bounds=(0.2, 0.7, 0.8, 0.9)))
    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.last_intent.parameters["resolved_target"] == {"x": 540, "y": 1536}
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest services/api/tests/test_app_interaction_runtime.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_capture_repository.py -q`

Expected: FAIL because App action adapters and non-bootstrap Capture persistence do not exist.

- [x] **Step 3: Implement deterministic resolver and action Capture persistence**

`AppCaptureWriter.capture()` 调用同一 Runtime Driver 的 `capture()`，校验 PNG 后使用扩展后的 persistence/repository 保存 before 或 after Capture。`AppInteractionExecutor` 加载 Session Ref、视觉候选和 before Capture，校验身份与签名后复制 ActionIntent，仅向 Driver 增加 `resolved_target`。

数据库写入必须校验 permit sequence、finalization epoch、父 Capture 和 Action Execution，Artifact 对象写入失败继续使用现有补偿删除语义。

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest services/api/tests/test_app_interaction_runtime.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_capture_repository.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```powershell
git add services/api/app/application/app_interaction_runtime.py services/api/app/application/app_capture_persistence.py services/api/app/infrastructure/db/app_capture_repository.py services/api/tests/test_app_interaction_runtime.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_capture_repository.py
git commit -m "[AI编程] feat: 实现 App 动作证据 Runtime" -m "涉及模块：App Interaction Runtime、App Capture；数据库迁移：复用 0034/0035；前端菜单/路由：无需同步；测试风险：正式星语坐标 DTO 尚未验证。"
```

### Task 8: 完成 Fake App 探索单轮 Runner

**Files:**
- Create: `services/api/app/application/app_exploration_turn.py`
- Modify: `services/api/app/infrastructure/app_runtime/fake.py`
- Modify: `services/api/app/infrastructure/app_runtime/factory.py`
- Test: `services/api/tests/test_app_exploration_turn.py`
- Test: `services/api/tests/test_app_screenshot_exploration_cycle.py`

**Interfaces:**
- Consumes: `ExplorerWorkQueue`、`ExplorerTurnExecutor`、共享 `AppRuntimeBundle`。
- Produces: `AppExplorationTurnRunner.run_one(task_id, agent_run_id) -> AppExplorationTurnOutcome`。

- [x] **Step 1: Write failing runner and end-to-end fake tests**

```python
@pytest.mark.asyncio
async def test_fake_app_completes_one_screenshot_exploration_turn():
    ready = await runtime.bootstrap(_bootstrap_command())
    published = await screenshot_workflow.run(_exploration_command(ready))
    outcome = await runner.run_one(task_id=ready.task_id, agent_run_id=ready.agent_run_id)
    assert outcome.work_id == published.work_id
    assert outcome.status is ExplorerWorkStatus.SUCCEEDED
    assert outcome.action_type is InteractionOperation.CLICK
    assert outcome.before_capture_id != ready.capture.capture_id
    assert outcome.after_capture_id != outcome.before_capture_id
    assert fake_runtime.action_count == 1
```

另写重复 action execution、after screenshot 失败、非 Android Work 和过期 lease 用例。

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest services/api/tests/test_app_exploration_turn.py services/api/tests/test_app_screenshot_exploration_cycle.py -q`

Expected: FAIL because the runner and screenshot sequence are absent.

- [x] **Step 3: Implement runner and screenshot sequence**

Runner 只领取指定 task 的 exploration work，校验 Android 后执行 `leased → running → succeeded`。终态或异常使用现有 `TransitionExplorerWorkCommand`；after evidence 失败进入 retry/failed 路径，不能标记 succeeded。

`FakeAppScenario` 新增不可变 `screenshot_sequence`，每次 capture 按顺序返回，耗尽后复用最后一张。`AppRuntimeFactory` 的 bundle 由组合根持有并同时提供 Bootstrap 与探索 Driver。

- [x] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest services/api/tests/test_app_exploration_turn.py services/api/tests/test_app_screenshot_exploration_cycle.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```powershell
git add services/api/app/application/app_exploration_turn.py services/api/app/infrastructure/app_runtime/fake.py services/api/app/infrastructure/app_runtime/factory.py services/api/tests/test_app_exploration_turn.py services/api/tests/test_app_screenshot_exploration_cycle.py
git commit -m "[AI编程] feat: 跑通 Fake App 探索单轮" -m "涉及模块：App Workflow、Explorer Worker、Fake Runtime；数据库迁移：使用 0036；前端菜单/路由：无需同步；测试风险：生产 Worker 路由仍保持关闭。"
```

### Task 9: 组合根、架构门禁和完整回归

**Files:**
- Create: `services/api/app/application/app_v2_runtime.py`
- Modify: `services/api/tests/test_app_architecture_boundaries.py`
- Create: `services/api/tests/test_app_v2_runtime.py`
- Modify: `docs/专项能力与高频回归规则.md`
- Modify: `docs/superpowers/plans/2026-08-04-app-screenshot-exploration.md`

**Interfaces:**
- Consumes: Tasks 2-8 的端口和组件。
- Produces: `build_app_v2_runtime(...) -> AppV2Runtime`，暴露 `bootstrap()`、`publish_exploration()`、`run_one_turn()`，不接生产 Worker。

- [x] **Step 1: Write failing composition and boundary tests**

```python
def test_app_v2_runtime_reuses_one_runtime_bundle():
    runtime = build_app_v2_runtime(provider="fake", environment="test")
    assert runtime.bootstrap_driver is runtime.interaction_driver

def test_explorer_and_judge_cannot_import_app_runtime_or_artifact_reader():
    violations = scan_forbidden_imports()
    assert violations == []
```

同时断言 `worker._process_one` 不构造 `AppV2Runtime`、`FakeAppRuntime` 或 App Driver。

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest services/api/tests/test_app_v2_runtime.py services/api/tests/test_app_architecture_boundaries.py -q`

Expected: FAIL because composition root is absent.

- [x] **Step 3: Implement composition root and update regression rules**

组合根只负责依赖装配，不包含业务状态判断。专项规则补充：视觉候选绑定 Capture、坐标只能由 Runtime 解析、相同 Handoff 幂等、production Worker App 路由保持关闭。

- [x] **Step 4: Run targeted tests and Ruff**

Run: `python -m pytest services/api/tests/test_app_visual_contracts.py services/api/tests/test_app_screenshot_understanding.py services/api/tests/test_fake_app_vision.py services/api/tests/test_app_visual_observation_migration.py services/api/tests/test_app_visual_observation_repository.py services/api/tests/test_app_screenshot_exploration.py services/api/tests/test_maf_explorer_agent.py services/api/tests/test_capture_profile_policy.py services/api/tests/test_capture_coordinator.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_app_interaction_runtime.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_capture_repository.py services/api/tests/test_app_exploration_turn.py services/api/tests/test_app_screenshot_exploration_cycle.py services/api/tests/test_app_v2_runtime.py services/api/tests/test_app_architecture_boundaries.py -q`

Run: `python -m ruff check <all changed Python files>`

Expected: all PASS.

- [x] **Step 5: Run complete API regression**

Run: `python -m pytest services/api/tests -q`

Expected: all existing and new tests PASS.

- [x] **Step 6: Mark this plan complete and commit**

```powershell
git add services/api/app/application/app_v2_runtime.py services/api/tests/test_app_v2_runtime.py services/api/tests/test_app_architecture_boundaries.py docs/专项能力与高频回归规则.md docs/superpowers/plans/2026-08-04-app-screenshot-exploration.md
git commit -m "[AI编程] test: 固化 App 截图探索回归门禁" -m "涉及模块：App Screenshot Exploration、架构回归；数据库迁移：验证 0036；前端菜单/路由：无需同步；测试风险：正式星语 API、真实视觉模型和真机链路仍待后续验证。"
```

## Plan Self-Review

- Spec coverage：合同、图片可信边界、视觉事实、Work 发布、Surface Capture Profile、公共 Coordinator、候选解析、Fake 单轮、组合根、架构门禁和全量回归均有对应任务。
- Placeholder scan：计划不包含 TBD、TODO、未定义实现或“类似 Task N”的省略步骤。
- Type consistency：视觉层统一输出 `AppVisualAnalysis`；探索层统一使用 `SurfaceObservation/FrontierSnapshot/ExplorationWork`；动作证据层统一使用 `CaptureRef`；运行入口统一使用 `AppV2Runtime`。
- Scope check：不实现 Official Adapter、Inspection、工作台或生产 Worker 路由，保持为单个可测试交付。
