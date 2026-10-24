# H5 Page Exploration Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复审计报告确认的四项仓内 H5 缺口，使首跳证据、入口 URL、仅探索运行依赖和切换检查与既有任务快照合同一致。

**Architecture:** 保留当前分层单体与 H5 V2 组合根，只在共享 Capture helper、H5 API Schema、Rule Engine 执行前门禁和 H5 运维脚本四个现有边界内定点修复。所有行为先通过失败测试固定合同，再以最小实现通过；App、Web 公开合同、前端和数据库结构不变。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、pytest/pytest-asyncio、Microsoft Agent Framework、PostgreSQL/Redis 接口契约、Ruff、Alembic。

## Global Constraints

- 只处理 H5 审计报告中的共享首跳父 Capture、H5 URL 凭据、`exploration_only` Rule Engine 误依赖和切换脚本口径四项问题。
- 不新增 App 功能，不修改 Web 公开请求合同，不修改工作站菜单或路由，不引入数据库迁移或新环境变量。
- 所有新增注释和文档必须使用中文，所有文件保持 UTF-8。
- 严格执行 RED-GREEN-REFACTOR；生产代码修改前必须先看到对应测试因目标行为缺失而失败。
- 真实 PostgreSQL、Redis、Playwright、MinIO、LLM、认证联合验收不在本次环境伪造；复审必须明确保留该发布门禁。
- 工作目录固定为 `D:\Project\SSC\alioth\.worktrees\h5-page-exploration-audit-remediation`，分支固定为 `codex/h5-page-exploration-audit-remediation`，合并目标为 `dev_1.5`。
- Python 测试使用 `D:\Project\SSC\alioth\.venv\Scripts\python.exe`。

## File Structure

- `services/api/app/application/evidence_exit_gate.py`：入口哨兵与可信首跳的唯一判定。
- `services/api/app/application/capture_coordinator.py`：继续消费可信首跳判定，不新增第二套入口规则。
- `services/api/app/api/v2/schemas.py`：仅负责公开 H5 请求在转换成应用命令前的 URL 安全校验。
- `services/api/app/application/rule_judge.py`：保留 Rule Engine 配置扫描，并增加进程内延迟门禁与成功缓存。
- `services/api/app/worker.py`：按任务 `execution_mode` 调用共享门禁，并把同一个门禁实例注入所有消费协程。
- `scripts/check_h5_v2_cutover_prerequisites.py`：按目标档位解释全局默认版本与任务快照路由。
- `docs/专项能力与高频回归规则.md`：记录可复用根因和后续约束。
- `docs/reviews/2026-08-14-h5-page-exploration-audit-remediation-review.md`：记录逐项复审证据与剩余发布门禁。

---

### Task 1: 恢复三类 H5/Web 首跳哨兵的 Capture 父级合同

**Files:**
- Modify: `services/api/tests/test_web_evidence_exit_gate.py:116-161`
- Modify: `services/api/tests/test_capture_coordinator.py:284-327`
- Modify: `services/api/app/application/evidence_exit_gate.py:204-233,316-325`

**Interfaces:**
- Consumes: `ActionIntent.source_capture_id`、`ActionIntent.parameters`、`SurfaceKind.WEB`、`InteractionOperation.NAVIGATE`。
- Produces: `is_trusted_initial_surface_action(intent: ActionIntent) -> bool`；`CaptureCoordinator` 继续以该返回值决定 before Capture 的 `parent_capture_id` 是否为 `None`。

- [ ] **Step 1: 参数化 helper 合同并扩展完整 Capture 链测试**

把现有 `test_agent_entry_navigate_is_trusted_initial_action` 改为三类入口参数化测试，并保留 H5 未标记负例：

```python
@pytest.mark.parametrize(
    ("source_capture_id", "parameters"),
    [
        ("h5-entry:session-1", {"initial_surface_action": True}),
        ("agent-entry:session-1", {}),
        ("legacy-agent-entry", {}),
    ],
)
def test_entry_navigation_is_trusted_only_by_its_declared_contract(
    source_capture_id: str,
    parameters: dict[str, Any],
) -> None:
    intent = _intent(
        operation=InteractionOperation.NAVIGATE,
        source_capture_id=source_capture_id,
        parameters=parameters,
    )

    assert is_trusted_initial_surface_action(intent) is True
    assert requires_evidence_exit_gate(intent) is False


def test_h5_entry_navigate_requires_explicit_marker() -> None:
    unmarked = _intent(
        operation=InteractionOperation.NAVIGATE,
        source_capture_id="h5-entry:session-1",
    )

    assert is_trusted_initial_surface_action(unmarked) is False
```

把协调器首跳测试参数化，确保三类哨兵都不会写成伪父级：

```python
@pytest.mark.parametrize(
    ("source_capture_id", "parameters"),
    [
        (
            "h5-entry:run-1",
            {
                "destination_url": "https://m.example.test/home",
                "initial_surface_action": True,
            },
        ),
        ("agent-entry:session-1", {"destination_url": "https://m.example.test/home"}),
        ("legacy-agent-entry", {"destination_url": "https://m.example.test/home"}),
    ],
)
async def test_initial_surface_action_persists_before_capture_without_fake_parent(
    source_capture_id: str,
    parameters: dict[str, object],
) -> None:
    events = []
    writer = RecordingCaptureWriter(events)
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=RecordingInteractionExecutor(events),
        evidence_exit_gate=RecordingEvidenceExitGate(),
    )
    command = _command()
    initial_intent = replace(
        command.intent,
        source_capture_id=source_capture_id,
        action_type=InteractionOperation.NAVIGATE,
        semantic_target="H5 任务锁定入口",
        parameters=parameters,
    )

    result = await coordinator.coordinate(replace(command, intent=initial_intent))

    assert writer.requests[0].parent_capture_id is None
    assert result.before_capture.parent_capture_id is None
    assert result.after_capture.parent_capture_id == result.before_capture.capture_id
```

- [ ] **Step 2: 运行 RED 并确认失败原因**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests/test_web_evidence_exit_gate.py services/api/tests/test_capture_coordinator.py -q
```

Expected: `agent-entry:*` 和 `legacy-agent-entry` 的可信首跳/父级断言失败；H5 显式标记正例继续通过，证明失败来自共享 helper 语义而非测试装配错误。

- [ ] **Step 3: 实现按哨兵类型分流的唯一 helper**

把 `is_trusted_initial_surface_action()` 改为：

```python
def is_trusted_initial_surface_action(intent: ActionIntent) -> bool:
    """按入口哨兵合同识别没有真实父 Capture 的 Web 首跳。"""
    if not isinstance(intent, ActionIntent):
        raise TypeError("intent 必须为 ActionIntent")
    if (
        intent.surface_kind is not SurfaceKind.WEB
        or intent.action_type is not InteractionOperation.NAVIGATE
        or not is_entry_sentinel_capture(intent.source_capture_id)
    ):
        return False
    if intent.source_capture_id.startswith("h5-entry:"):
        return intent.parameters.get("initial_surface_action") is True
    return True
```

删除 `trusted_h5_browser_recovery_identity()` 在返回元组之后的不可达首跳判断代码；不要在 `CaptureCoordinator` 中复制新规则。

- [ ] **Step 4: 运行 GREEN 与相关共享回归**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests/test_web_evidence_exit_gate.py services/api/tests/test_capture_coordinator.py -q
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m ruff check services/api/app/application/evidence_exit_gate.py services/api/app/application/capture_coordinator.py services/api/tests/test_web_evidence_exit_gate.py services/api/tests/test_capture_coordinator.py
```

Expected: 两条命令退出码均为 0；三类首跳父级均为 `None`，真实 Capture 和未标记 H5 仍不可信。

- [ ] **Step 5: 提交首跳合同修复**

```powershell
git add -- services/api/app/application/evidence_exit_gate.py services/api/tests/test_web_evidence_exit_gate.py services/api/tests/test_capture_coordinator.py
git commit -m "[AI编程] fix: 修复H5共享首跳Capture父级" -m "涉及模块：H5/共享 Capture 与 Evidence Exit Gate。数据库迁移：无。前端菜单/路由：无需同步。未覆盖测试风险：真实 Playwright 首跳和历史任务回放待联合环境验收。"
```

---

### Task 2: 在 H5 API Schema 前移 URL 凭据与端口校验

**Files:**
- Modify: `services/api/tests/test_v2_task_entrypoints.py:110-172,175-285`
- Modify: `services/api/app/api/v2/schemas.py:85-106,251-263`

**Interfaces:**
- Consumes: `H5TargetRequest.url: str`。
- Produces: `_validated_h5_url(value: str) -> str`；合法 H5 URL 归一化后进入 `V2TaskSubmissionCommand`，非法 URL 在 Entry Port 之前返回 422。

- [ ] **Step 1: 新增完整任务与仅探索入口的敏感 URL 负例**

在 `test_v2_task_entrypoints.py` 增加：

```python
@pytest.mark.parametrize(
    "target_url",
    [
        "https://alice@example.test/h5",
        "https://alice:secret@example.test/h5",
        "https://example.test:invalid/h5",
    ],
)
@pytest.mark.parametrize("endpoint", ["/api/v2/tasks", "/api/v2/exploration-tasks"])
def test_h5_unsafe_url_is_rejected_before_application_entry(
    target_url: str,
    endpoint: str,
) -> None:
    entry = (
        RecordingTaskEntry()
        if endpoint == "/api/v2/tasks"
        else RecordingExplorationTaskEntry()
    )
    app = create_app()
    dependency = (
        v2_tasks_module._get_full_task_entry
        if endpoint == "/api/v2/tasks"
        else v2_tasks_module._get_exploration_task_entry
    )
    app.dependency_overrides[dependency] = lambda: entry
    client = TestClient(app)
    payload = _h5_task_payload()
    payload["target"]["url"] = target_url

    response = client.post(endpoint, json=payload)

    assert response.status_code == 422
    assert entry.commands == []
```

- [ ] **Step 2: 运行 RED 并确认带凭据 URL 当前被错误提交**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests/test_v2_task_entrypoints.py::test_h5_unsafe_url_is_rejected_before_application_entry -q
```

Expected: username、password 或非法端口用例至少有一项返回 202 或在端口解析处产生非 422 行为，且记录入口出现调用；失败原因必须对应 Schema 未前移校验。

- [ ] **Step 3: 只为 H5 目标增加专用安全校验**

把 `H5TargetRequest.validate_url()` 改为调用 `_validated_h5_url()`，保留 `WebTargetRequest` 调用原 `_validated_http_url()`：

```python
def _validated_h5_url(value: str) -> str:
    normalized = _validated_http_url(value, field_name="url")
    parsed = urlsplit(normalized)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url 必须是无凭据的 HTTP(S) URL")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("url 端口无效") from exc
    return normalized
```

```python
class H5TargetRequest(BaseModel):
    platform: Literal["h5"]
    url: str = Field(min_length=1, max_length=4096)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validated_h5_url(value)
```

- [ ] **Step 4: 运行 GREEN、普通 H5 正例和 Schema 静态检查**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests/test_v2_task_entrypoints.py -q
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m ruff check services/api/app/api/v2/schemas.py services/api/tests/test_v2_task_entrypoints.py
```

Expected: V2 API 测试全部通过；非法 H5 URL 为 422 且 Entry Port 零调用；Web/App 既有测试无变化。

- [ ] **Step 5: 提交 H5 URL 安全边界修复**

```powershell
git add -- services/api/app/api/v2/schemas.py services/api/tests/test_v2_task_entrypoints.py
git commit -m "[AI编程] fix: 前移H5入口URL安全校验" -m "涉及模块：H5 V2 API Schema。数据库迁移：无。前端菜单/路由：无需同步。未覆盖测试风险：真实反向代理和浏览器重定向链待联合环境验收。"
```

---

### Task 3: 建立按 execution_mode 延迟执行的 Rule Engine 配置门禁

**Files:**
- Modify: `services/api/tests/test_rule_judge.py:1-45,2015-2050`
- Modify: `services/api/app/application/rule_judge.py:1-20,217-245,411-431`

**Interfaces:**
- Consumes: `RuleEngineConfigurationRepository`、`TaskExecutionMode`。
- Produces: `RuleEngineConfigurationGuard.ensure_ready_for(execution_mode: TaskExecutionMode) -> Awaitable[None]`；仅完整质检扫描规则，成功结果按进程缓存，失败允许重试。

- [ ] **Step 1: 给 Fake Repository 增加调用计数并写门禁失败测试**

在测试文件导入 `asyncio`、`TaskExecutionMode` 和新门禁类；把 Fake 改为：

```python
class FakeRuleEngineConfigurationRepository:
    def __init__(self, definitions: list[RuleJudgeDefinition]) -> None:
        self.definitions = definitions
        self.calls = 0

    async def list_enabled_rule_judge_definitions(self) -> list[RuleJudgeDefinition]:
        self.calls += 1
        return self.definitions
```

新增三项测试：

```python
async def test_rule_engine_guard_skips_exploration_only_without_repository_access() -> None:
    repository = FakeRuleEngineConfigurationRepository([])
    guard = RuleEngineConfigurationGuard(repository)

    await guard.ensure_ready_for(TaskExecutionMode.EXPLORATION_ONLY)

    assert repository.calls == 0


async def test_rule_engine_guard_caches_success_across_concurrent_full_tasks() -> None:
    repository = FakeRuleEngineConfigurationRepository([])
    guard = RuleEngineConfigurationGuard(repository)

    await asyncio.gather(
        guard.ensure_ready_for(TaskExecutionMode.FULL_INSPECTION),
        guard.ensure_ready_for(TaskExecutionMode.FULL_INSPECTION),
    )

    assert repository.calls == 1


async def test_rule_engine_guard_retries_after_configuration_failure() -> None:
    repository = FakeRuleEngineConfigurationRepository(
        [
            RuleJudgeDefinition(
                rule_id="rule-invalid",
                rule_code="alioth_invalid",
                executor_type="rule_engine",
                judge_config={},
            )
        ]
    )
    guard = RuleEngineConfigurationGuard(repository)

    with pytest.raises(RuleEngineConfigurationError):
        await guard.ensure_ready_for(TaskExecutionMode.FULL_INSPECTION)
    repository.definitions = []
    await guard.ensure_ready_for(TaskExecutionMode.FULL_INSPECTION)

    assert repository.calls == 2
```

- [ ] **Step 2: 运行 RED 并确认新接口尚不存在**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests/test_rule_judge.py -k "rule_engine_guard" -q
```

Expected: 测试收集因 `RuleEngineConfigurationGuard` 尚不存在而失败，或相应行为断言失败；不得先写生产类。

- [ ] **Step 3: 实现带并发锁和成功缓存的门禁**

在 `rule_judge.py` 导入 `asyncio` 与 `TaskExecutionMode`，并在现有扫描函数之后增加：

```python
class RuleEngineConfigurationGuard:
    """按任务模式延迟校验规则配置，并只缓存成功结果。"""

    def __init__(self, repository: RuleEngineConfigurationRepository) -> None:
        self._repository = repository
        self._ready = False
        self._lock = asyncio.Lock()

    async def ensure_ready_for(self, execution_mode: TaskExecutionMode) -> None:
        if not isinstance(execution_mode, TaskExecutionMode):
            raise TypeError("execution_mode 必须为 TaskExecutionMode")
        if execution_mode is TaskExecutionMode.EXPLORATION_ONLY or self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            await validate_enabled_rule_engine_configuration(self._repository)
            self._ready = True
```

同步把 `validate_enabled_rule_engine_configuration()` 的 docstring 从“阻止 Worker 启动”改为“阻止完整质检在规则配置不可执行时继续”。

- [ ] **Step 4: 运行 GREEN 与现有规则路由回归**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests/test_rule_judge.py -q
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m ruff check services/api/app/application/rule_judge.py services/api/tests/test_rule_judge.py
```

Expected: 所有 Rule Judge 测试通过；并发完整任务只扫描一次，失败后下一次仍重新扫描。

- [ ] **Step 5: 提交延迟门禁领域能力**

```powershell
git add -- services/api/app/application/rule_judge.py services/api/tests/test_rule_judge.py
git commit -m "[AI编程] refactor: 按任务模式延迟规则配置门禁" -m "涉及模块：Rule Judge/H5 execution mode。数据库迁移：无。前端菜单/路由：无需同步。未覆盖测试风险：真实 PostgreSQL 规则热更新与多进程缓存一致性待部署验收。"
```

---

### Task 4: 把同一 Rule Engine 门禁实例接入 Worker 的执行前路由

**Files:**
- Modify: `services/api/tests/test_worker_h5_v2_route.py:19-118`
- Modify: `services/api/tests/test_worker.py:3451-3472`
- Modify: `services/api/app/worker.py:115-145,300-418,2524-2613,3160-3322`

**Interfaces:**
- Consumes: Task 3 的 `RuleEngineConfigurationGuard` 与 `task_execution_mode_from_config()`。
- Produces: `_process_one(..., rule_engine_configuration_guard: RuleEngineConfigurationGuard | None = None)`；生产 `main()` 必须注入真实门禁，所有 `_worker_loop` 共享同一个实例。

- [ ] **Step 1: 写 Worker 模式传递和执行前失败测试**

在 `test_worker_h5_v2_route.py` 增加记录型门禁：

```python
class RecordingRuleEngineConfigurationGuard:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[TaskExecutionMode] = []

    async def ensure_ready_for(self, execution_mode: TaskExecutionMode) -> None:
        self.calls.append(execution_mode)
        if self.fail:
            raise RuleEngineConfigurationError("规则配置不可执行")
```

让 `_h5_v2_record()` 接受模式并写入 `task_config`：

```python
def _h5_v2_record(
    mode: TaskExecutionMode = TaskExecutionMode.FULL_INSPECTION,
) -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-h5-v2",
        task_id="task-h5-v2",
        message="探索 H5 页面",
        submitted_at=datetime.now(UTC),
        task_config={
            "workflow_version": "v2",
            "execution_mode": mode.value,
            "_task_target": {
                "platform": "h5",
                "target_url": "https://m.example.test/home",
            },
        },
    )
```

把现有路由测试参数化为完整质检和仅探索两种模式，向 `_process_one()` 传入记录型门禁并断言收到精确枚举。另增加失败门禁测试，monkeypatch `_finish_standard_terminal_run` 为无副作用 async 函数，断言 `_process_h5_v2_one` 没有被调用且 `record.status is FAILED`。

- [ ] **Step 2: 运行 RED 并确认 Worker 尚未接受门禁依赖**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests/test_worker_h5_v2_route.py -q
```

Expected: `_process_one()` 因未知关键字 `rule_engine_configuration_guard` 或门禁调用断言失败。

- [ ] **Step 3: 在 Worker 处理链传递同一个门禁实例**

执行以下最小生产改动：

1. `worker.py` 从 `rule_judge` 导入 `RuleEngineConfigurationGuard`，删除对 `validate_enabled_rule_engine_configuration` 的直接导入。
2. 给 `_process_one()`、`_worker_loop()`、`_start_worker_loops()` 增加可选/必需门禁参数；`_worker_loop()` 调用 `_process_one()` 时原样透传。
3. 在 `_process_one()` 完成 `workflow_dispatcher.route()` 后、任何 `_start_dequeued_record()`、App/H5 Runtime 分派之前执行：

```python
if rule_engine_configuration_guard is not None:
    await rule_engine_configuration_guard.ensure_ready_for(
        task_execution_mode_from_config(record.task_config)
    )
```

4. 把该调用放进现有执行前异常收口 `try`，并把日志文案改为“Workflow 路由或执行前门禁失败”。
5. 在 `main()` 中删除 eager `await validate_enabled_rule_engine_configuration(...)`，改为无 I/O 构造：

```python
rule_engine_configuration_guard = RuleEngineConfigurationGuard(
    PostgresTaskRepository()
)
```

6. `_start_worker_loops()` 接收并把同一个 `rule_engine_configuration_guard` 注入每个协程，不得在列表推导中重复构造。

- [ ] **Step 4: 更新补充证据启动顺序测试**

把 `test_worker_main_rejects_enabled_but_unready_supplemental_runtime` 中对旧校验函数的 monkeypatch 改为对门禁构造器的检测：

```python
def _unexpected_rule_configuration_guard(*_args, **_kwargs):
    nonlocal downstream_called
    downstream_called = True
    raise AssertionError("补充证据自检失败后不得继续装配规则门禁")

monkeypatch.setattr(
    worker_module,
    "RuleEngineConfigurationGuard",
    _unexpected_rule_configuration_guard,
)
```

继续断言 `downstream_called is False`，证明 supplemental runtime 自检仍在数据库、Redis 和新门禁装配之前失败。

- [ ] **Step 5: 运行 GREEN 与 Worker 相关回归**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_worker.py -q
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m ruff check services/api/app/worker.py services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_worker.py
```

Expected: Worker 测试通过；仅探索模式触发门禁方法但门禁零访问规则仓储，完整质检在 Runtime 前完成校验，启动期不再执行数据库规则扫描。

- [ ] **Step 6: 提交 Worker 接线**

```powershell
git add -- services/api/app/worker.py services/api/tests/test_worker.py services/api/tests/test_worker_h5_v2_route.py
git commit -m "[AI编程] fix: 隔离H5仅探索规则配置依赖" -m "涉及模块：H5 Worker/Rule Engine 执行前门禁。数据库迁移：无。前端菜单/路由：无需同步。未覆盖测试风险：真实多 Worker 进程缓存与 PostgreSQL 短断恢复待部署验收。"
```

---

### Task 5: 让 H5 切换检查按 shadow/on 目标解释全局默认版本

**Files:**
- Modify: `services/api/tests/test_h5_v2_cutover_prerequisites.py:17-33,80-110`
- Modify: `scripts/check_h5_v2_cutover_prerequisites.py:2-12,56-65,145-168`

**Interfaces:**
- Consumes: `settings.workflow.version`、目标档位 `Literal["shadow", "on"]`。
- Produces: `_workflow_routing_check(settings, *, target: str) -> Check`；`shadow+global v1` 阻塞，`on+global v1` 仅警告。

- [ ] **Step 1: 把旧全局硬门槛测试改成目标档位矩阵**

用以下测试替换原“v1 一律阻塞”合同：

```python
def test_shadow_requires_legacy_entry_to_lock_v2_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_settings(monkeypatch, workflow_version="v1", h5_mode="shadow")

    report = build_report(target="shadow")

    assert report["ready"] is False
    assert "legacy_entry_default_locks_v2" in report["blocking_failures"]


def test_on_accepts_v2_api_snapshot_when_legacy_default_remains_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_settings(monkeypatch, workflow_version="v1", h5_mode="on")

    report = build_report(target="on")
    checks = _by_name(collect_checks(target="on"))

    assert report["ready"] is True
    assert report["blocking_failures"] == []
    assert checks["legacy_entry_default_locks_v2"].passed is False
    assert checks["legacy_entry_default_locks_v2"].blocking is False
    assert "V2 API" in checks["legacy_entry_default_locks_v2"].detail
    assert "任务快照" in checks["legacy_entry_default_locks_v2"].detail
```

同步更新 CLI 退出码测试：`shadow+v1` 返回 2，`on+v1` 返回 0。

- [ ] **Step 2: 运行 RED 并确认 on 仍被旧硬门槛阻断**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests/test_h5_v2_cutover_prerequisites.py -q
```

Expected: `on+v1` 的 readiness 与检查名断言失败，证明脚本仍使用全局硬门槛。

- [ ] **Step 3: 实现按目标档位变化的路由检查**

用下列函数替换 `_workflow_version_check()`：

```python
def _workflow_routing_check(settings, *, target: str) -> Check:
    version = settings.workflow.version
    shadow_target = target == "shadow"
    return Check(
        name="legacy_entry_default_locks_v2",
        passed=version == "v2",
        detail=(
            f"ALIOTH_WORKFLOW_VERSION={version}；"
            + (
                "shadow 依赖旧入口生成 workflow_version=v2 任务快照"
                if shadow_target
                else "该值只控制旧入口默认快照；V2 API 会显式锁定 v2，运行时以任务快照路由"
            )
        ),
        blocking=shadow_target,
    )
```

在 `collect_checks()` 中传入 `target`。同时改写模块说明：全局版本只决定未显式指定版本的旧入口默认值；运行时以持久化任务快照为权威，`on` 不得宣称全局 v1 会让所有 V2 API 任务失效。

- [ ] **Step 4: 运行 GREEN、路由权威测试与 CLI 矩阵**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests/test_h5_v2_cutover_prerequisites.py services/api/tests/test_app_workflow_router.py services/api/tests/test_v2_task_entrypoints.py -q
$env:ALIOTH_WORKFLOW_VERSION='v1'
$env:ALIOTH_H5_V2_MODE='on'
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m scripts.check_h5_v2_cutover_prerequisites --target on --json
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m ruff check scripts/check_h5_v2_cutover_prerequisites.py services/api/tests/test_h5_v2_cutover_prerequisites.py
```

Expected: pytest 与 Ruff 退出码为 0；CLI JSON 中 `ready=true`，全局 v1 检查为非阻塞警告，并明确 V2 API/任务快照语义。

- [ ] **Step 5: 提交切换口径修复**

```powershell
git add -- scripts/check_h5_v2_cutover_prerequisites.py services/api/tests/test_h5_v2_cutover_prerequisites.py
git commit -m "[AI编程] fix: 对齐H5切换脚本与任务快照路由" -m "涉及模块：Ops/H5 Cutover。数据库迁移：无。前端菜单/路由：无需同步。未覆盖测试风险：真实 shadow 任务与 on 灰度切换仍需外部环境人工确认。"
```

---

### Task 6: 更新 H5 高频回归规则并完成仓内复审

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`
- Create: `docs/reviews/2026-08-14-h5-page-exploration-audit-remediation-review.md`

**Interfaces:**
- Consumes: Tasks 1-5 的 Git 差异、RED/GREEN 输出和最终验证输出。
- Produces: 可复用的 H5 回归约束与逐项复审结论；不得把本地测试描述为真实联合验收。

- [ ] **Step 1: 先执行四项定点回归集合**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests/test_web_evidence_exit_gate.py services/api/tests/test_capture_coordinator.py services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_rule_judge.py services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_worker.py services/api/tests/test_h5_v2_cutover_prerequisites.py services/api/tests/test_app_workflow_router.py -q
```

Expected: 退出码 0；允许既有显式 skip，但不得有 failed/error。

- [ ] **Step 2: 执行 H5/共享扩展回归、静态检查、编译和迁移图检查**

Run:

```powershell
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m pytest services/api/tests -k "h5 or web_evidence_exit_gate or capture_coordinator or v2_task_entrypoints or rule_engine_guard" -q
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m ruff check services/api/app/application/evidence_exit_gate.py services/api/app/application/capture_coordinator.py services/api/app/api/v2/schemas.py services/api/app/application/rule_judge.py services/api/app/worker.py scripts/check_h5_v2_cutover_prerequisites.py services/api/tests/test_web_evidence_exit_gate.py services/api/tests/test_capture_coordinator.py services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_rule_judge.py services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_h5_v2_cutover_prerequisites.py
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m compileall -q services/api/app/application/evidence_exit_gate.py services/api/app/application/capture_coordinator.py services/api/app/api/v2/schemas.py services/api/app/application/rule_judge.py services/api/app/worker.py scripts/check_h5_v2_cutover_prerequisites.py
& 'D:\Project\SSC\alioth\.venv\Scripts\python.exe' -m alembic -c services/api/migrations/alembic.ini heads
```

Expected: pytest、Ruff、compileall 退出码均为 0；Alembic 输出唯一 head `20260814_0057 (head)`。若测试因真实依赖缺失 skip，复审必须逐项记录，不能改写成通过。

- [ ] **Step 3: 更新专项回归规则**

在 `docs/专项能力与高频回归规则.md` 追加 `2026-08-14：H5 入口合同必须在持久化和执行副作用之前闭合`，明确写入：

- 三类入口哨兵的差异化信任规则及 before/after Capture 父级；
- H5 URL 必须在 API Schema 拒绝 userinfo 与非法端口，提交端口必须零调用；
- Rule Engine 配置只门禁 `full_inspection`，`exploration_only` 不访问规则仓储；
- H5 运行时只读取任务快照，切换脚本必须区分 shadow 旧入口和 on 的 V2 API；
- 任何本地 Fake/Mock/只读检查都不能替代真实联合验收。

- [ ] **Step 4: 创建逐项复审报告**

创建 `docs/reviews/2026-08-14-h5-page-exploration-audit-remediation-review.md`，按以下固定结构记录 Step 1-2 的真实结果：

```markdown
# H5 页面探索完善度审计整改复审

## 范围

仅复审 R-01、R-02、H5 exploration_only Rule Engine 隔离和 H5 Cutover 路由口径；App 问题不在本次范围。

## 逐项结论

分别列出每项的根因、修改文件、RED 证据、GREEN 证据、最终状态和未覆盖环境风险。

## 验证证据

逐条记录实际命令、退出码、passed/skipped/failed 数量、Ruff、compileall 和 Alembic head；不得预填或推测数量。

## 发布门禁

明确真实 PostgreSQL、Redis、Playwright、MinIO、LLM、认证、浏览器崩溃和 Worker 重启联合验收仍未执行，不得据此批准生产放量。

## 变更影响

数据库迁移：无。前端菜单/路由：无。App 产品合同：无变化。
```

- [ ] **Step 5: 审查最终差异不扩展范围**

Run:

```powershell
git diff --check dev_1.5...HEAD
git diff --stat dev_1.5...HEAD
git diff --name-only dev_1.5...HEAD
git status --short
```

Expected: 只出现计划列出的 H5/共享文件、设计/计划/规则/复审文档；没有 App 功能、工作站代码、数据库 migration 或未知文件。

- [ ] **Step 6: 提交规则和复审文档**

```powershell
git add -- docs/专项能力与高频回归规则.md docs/reviews/2026-08-14-h5-page-exploration-audit-remediation-review.md
git commit -m "[AI编程] doc: 完成H5审计整改复审" -m "涉及模块：H5 Capture/API/Worker/Ops 与专项回归规则。数据库迁移：无。前端菜单/路由：无需同步。未覆盖测试风险：真实 PostgreSQL、Redis、Playwright、MinIO、LLM、认证及故障注入仍为发布门禁。"
```

---

## Final Verification and Integration

- [ ] **Step 1: 在修复分支上重新执行 Task 6 的全部验证命令**

任何命令非零都停止集成；不得用之前的输出替代新鲜证据。

- [ ] **Step 2: 确认提交历史和 worktree 干净**

```powershell
git status --short --branch
git log --oneline --decorate dev_1.5..HEAD
```

- [ ] **Step 3: 在合并前执行代码复审**

以 `dev_1.5` 为 base、当前 `HEAD` 为 head，逐条核对设计、实施计划、审计报告四项 H5 要求和全部差异；Critical/Important 问题必须修复并重新验证后才能继续。

- [ ] **Step 4: 从主仓库快进或合并到 dev_1.5**

```powershell
git -C 'D:\Project\SSC\alioth' checkout dev_1.5
git -C 'D:\Project\SSC\alioth' pull --ff-only origin dev_1.5
git -C 'D:\Project\SSC\alioth' merge --no-ff codex/h5-page-exploration-audit-remediation -m "[AI编程] merge: 合并H5页面探索审计整改" -m "涉及模块：H5 Capture/API/Worker/Ops。数据库迁移：无。前端菜单/路由：无需同步。未覆盖测试风险：真实联合验收和故障注入仍为发布门禁。"
```

若远程 `dev_1.5` 已前进，先停止并在修复 worktree 合并最新 `origin/dev_1.5`，解决冲突、重新执行全部验证，再回到本步骤。

- [ ] **Step 5: 在合并后的 dev_1.5 上重新执行 Task 6 的全部验证命令**

测试必须从 `D:\Project\SSC\alioth` 运行，证明合并结果而非仅证明功能分支。

- [ ] **Step 6: 推送 dev_1.5 并确认远程同步**

```powershell
git -C 'D:\Project\SSC\alioth' push origin dev_1.5
git -C 'D:\Project\SSC\alioth' fetch origin dev_1.5
git -C 'D:\Project\SSC\alioth' rev-parse dev_1.5
git -C 'D:\Project\SSC\alioth' rev-parse origin/dev_1.5
```

Expected: 两个 SHA 完全一致；禁止 force push。
