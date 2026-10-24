# H5 Page Exploration Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 H5 V2 页面探索从单链采集流程整改为具备完整导航合同、任务级 Frontier、任务预算、协作式取消、崩溃恢复、Assessment/证据库存持久化和工作台回读能力的可审计闭环，并逐条复核附件中的 H5 建议。

**Architecture:** 复用现有 V2 Work、Capture、Action Execution、Inspection 与 Completion Gate，只新增 H5 专属运行策略、任务级 Frontier、取消/恢复编排和两张持久化事实表。共享组件只增加默认关闭的扩展点，H5 组合根显式启用，App 现有行为保持不变；工作台直接投影 H5 V2 事实，不再依赖旧页面节点或空 WorkingMemory。

**Tech Stack:** Python 3 + FastAPI + asyncpg + Alembic + PostgreSQL + Redis + Playwright + pytest；Vite + Vue 3 + TypeScript + Vitest。

## Global Constraints

- 仅处理 H5 以及直接影响 H5 的共享控制面；不得改变 App 专属探索、封存或 exploration-only 语义。
- quick：最多 40 个动作、深度 2、15 分钟、最多 3 个业务页面。
- full：动作数采用任务锁定值并受全局硬上限约束、深度 4、60 分钟。
- custom：动作数采用任务锁定值并受全局硬上限约束、深度 3、60 分钟，并消费锁定的 `journey_steps`。
- 任务运行参数只能来自已锁定任务快照和 `resolve_task_runtime_settings(...)`，不得重新引入 H5 私有硬编码预算。
- 导航动作的唯一合同是 `action="navigate"` 加非空 `destination_url`；`goto` 只允许在历史输入兼容解析边界转换，不得成为新写入事实。
- ACTION 已开始后必须完成 after-capture 或记录证据不足；取消不得切断 before/action/after 原子证据边界。
- ACTION 阶段连接丢失属于结果未知，不得自动重放原动作。
- H5 Frontier 快照以版本化 payload 存入 H5 Work 的 `observation.metadata["h5_task_frontier"]`，不新增第二套队列表。
- 新增迁移 revision `20260812_0049`，down revision 固定为 `20260812_0048`；降级前必须阻止非空 Assessment/Evidence Inventory 数据被静默删除。
- 所有新增注释和用户可见文案使用中文 UTF-8；单文件保持单一职责。
- exploration-only 成功完成后 `report_status` 为 `not_applicable`，任务详情仍必须展示探索结果和证据完整性结论。
- 代码级测试不得表述为生产放量验收；真实 PostgreSQL、Redis、对象存储和 Playwright E2E 单独列为环境门禁。

---

## File Map

### New domain/application files

- `services/api/app/domain/exploration/h5_frontier.py`：H5 任务级 Frontier Entry/Snapshot、版本化序列化、hash、去重和调度纯函数。
- `services/api/app/domain/exploration/__init__.py`：导出 H5 Frontier 稳定公共类型。
- `services/api/app/application/h5_task_runtime_settings.py`：把锁定的 task config、loop guard、page sampling policy 映射为 H5 动作、深度、时长、页面数、页面类型范围和旅程目标。
- `services/api/app/application/h5_exploration_cancellation.py`：取消端口、取消异常和阶段检查器。
- `services/api/app/application/h5_exploration_recovery.py`：浏览器丢失阶段分类、有限恢复和 rebaseline 编排。
- `services/api/app/application/h5_evidence_inventory.py`：预期动作驱动的证据库存模型、逐项校验和批次结论。
- `services/api/app/infrastructure/db/h5_page_type_assessment_repository.py`：PageTypeAssessment 幂等持久化和查询。
- `services/api/app/infrastructure/db/h5_evidence_inventory_repository.py`：Evidence Inventory 的预期集合、逐项 upsert 和终态降级保护。
- `services/api/app/infrastructure/storage/h5_evidence_object_verifier.py`：从既有 evidence store 重读对象字节并校验大小、归属和 SHA-256。
- `services/api/migrations/versions/20260812_0049_h5_exploration_audit_remediation.py`：新增 Assessment 与 Evidence Inventory 表、约束、索引和安全降级。
- `services/api/tests/test_h5_task_runtime_settings.py`、`test_h5_task_frontier.py`、`test_h5_exploration_cancellation.py`、`test_h5_exploration_recovery.py`、`test_h5_evidence_inventory.py`、`test_h5_exploration_audit_remediation_migration.py`：新增专项测试。
- `docs/reviews/2026-08-12-h5-page-exploration-audit-remediation-review.md`：逐条对照原附件的最终复审结论。

### Existing backend files to modify

- `services/api/app/agents/tools/page_detection_tools.py`：模型候选写出 `navigate` 和 `destination_url`。
- `services/api/app/application/h5_exploration_observation.py`：完整保留导航目标并拒绝空目标。
- `services/api/app/agents/explorer/maf_agent.py`：保持导航资格检查与统一合同一致。
- `services/api/app/application/h5_runtime_composition.py`：消费锁定设置，注入取消、恢复、持久化和 H5-only outbox 策略。
- `services/api/app/application/h5_exploration_workflow.py`：合并局部候选到任务 Frontier、确定性恢复来源页面、持久化 Assessment。
- `services/api/app/application/h5_exploration_turn.py`：阶段取消检查、恢复结果和全局 Frontier 后继提交。
- `services/api/app/application/explorer_turn.py`：增加默认无操作的 `before_side_effect` 异步钩子，供 H5 在动作前复查取消。
- `services/api/app/application/exploration_work_queue.py`：扩展取消协议和 H5 唤醒开关合同。
- `services/api/app/infrastructure/db/explorer_work_repository.py`：H5 可取消 exploration/probe 非终态项；H5 web work 可禁止无人消费的 outbox 唤醒。
- `services/api/app/application/h5_evidence_handoff.py`：按库存逐项交接，不因一条不足抹掉合格证据。
- `services/api/app/infrastructure/db/h5_evidence_handoff_repository.py`：查询从预期 Action Execution 出发并使用左连接。
- `services/api/app/application/app_inspection_publisher.py`：写入嵌套与兼容顶层 inspection 状态。
- `services/api/app/infrastructure/db/task_insight_repository.py`：直接读取 H5 V2 Work/Action/Capture/Assessment/Inventory 和正式 `target_platform`。
- `services/api/app/application/task_insight.py`：构造 H5 时间线、覆盖缺口、证据库存和 exploration-only 展示语义。
- `services/api/app/application/workstation_tasks.py`：统一 exploration-only 报告状态。
- `services/api/app/worker.py`：H5 组合根注入新端口、运行终态映射和 Completion Gate 启动校验。

### Existing frontend/docs files to modify

- `app/workstation/src/types/task-insight.ts`：增加 H5 Frontier/Assessment/Evidence Inventory 投影类型。
- `app/workstation/src/types/task.ts`：为任务列表/详情补齐正式 `target_platform` 与 `execution_mode`。
- `app/workstation/src/modules/tasks/pages/TaskDetailView.vue`：exploration-only 不显示“查看报告”，展示 H5 探索与证据完整性结论。
- `app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts`：新增 H5 exploration-only 与 needs-review 交互测试。
- `docs/专项能力与高频回归规则.md`：沉淀 H5 导航、Frontier、取消、证据库存和读模型回归规则。

---

### Task 1: Unify the H5 Navigation Contract

**Files:**
- Modify: `services/api/app/agents/tools/page_detection_tools.py`
- Modify: `services/api/app/application/h5_exploration_observation.py`
- Modify: `services/api/app/agents/explorer/maf_agent.py`
- Test: `services/api/tests/test_page_detection_tools.py`
- Test: `services/api/tests/test_h5_exploration_observation.py`
- Test: `services/api/tests/test_h5_explorer_bootstrap.py`

**Interfaces:**
- Consumes: `InteractionAction.NAVIGATE.value == "navigate"` and `CandidateAction.metadata`.
- Produces: `CandidateAction(action="navigate", metadata={"destination_url": <absolute same-origin url>, ...})`.

- [ ] **Step 1: Write failing navigation contract tests**

```python
def test_page_detection_emits_navigate_with_destination_url():
    candidate = normalize_h5_detection_candidate({"action": "goto", "url": "/products"})
    assert candidate["action"] == "navigate"
    assert candidate["destination_url"] == "/products"


def test_h5_frontier_preserves_navigation_destination():
    frontier = build_h5_frontier(
        detection_result={
            "candidate_actions": [
                {"action": "navigate", "destination_url": "https://example.test/products"}
            ]
        },
        source_page_capture_id="capture-1",
    )
    assert frontier.candidates[0].metadata["destination_url"] == "https://example.test/products"


@pytest.mark.parametrize(
    ("destination_url", "reason_code"),
    [
        (None, "h5_navigation_missing_url"),
        ("javascript:alert(1)", "h5_navigation_invalid_scheme"),
        ("https://other.test/products", "h5_navigation_cross_origin"),
        ("https://example.test/admin", "h5_navigation_route_out_of_scope"),
    ],
)
def test_h5_navigation_rejection_has_stable_reason(destination_url, reason_code):
    decision = evaluate_h5_navigation_candidate(
        destination_url=destination_url,
        entry_url="https://example.test/shop",
        allowed_route_prefixes=("/shop",),
        navigation_depth=1,
        max_depth=2,
    )
    assert decision.eligible is False
    assert decision.reason_code == reason_code


def test_h5_navigation_depth_rejection_is_auditable():
    decision = evaluate_h5_navigation_candidate(
        destination_url="https://example.test/shop/products",
        entry_url="https://example.test/shop",
        allowed_route_prefixes=("/shop",),
        navigation_depth=3,
        max_depth=2,
    )
    assert decision.reason_code == "h5_navigation_depth_exceeded"
```

- [ ] **Step 2: Run the focused tests and verify the contract fails**

Run: `python -m pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_explorer_bootstrap.py -q`

Expected: at least one assertion reports `goto` or a missing `destination_url`.

- [ ] **Step 3: Implement canonicalization at the detection boundary**

```python
def _canonical_h5_action(raw: object) -> str:
    normalized = str(raw or "").strip().casefold()
    return InteractionAction.NAVIGATE.value if normalized == "goto" else normalized


def _candidate_metadata(raw: Mapping[str, object]) -> dict[str, object]:
    metadata = {
        "purpose": raw.get("purpose"),
        "target_keywords": raw.get("target_keywords", []),
        "expected_next_page_type": raw.get("expected_next_page_type"),
    }
    if _canonical_h5_action(raw.get("action")) == InteractionAction.NAVIGATE.value:
        metadata["destination_url"] = raw.get("destination_url") or raw.get("url")
    return metadata


def normalize_h5_detection_candidate(raw: Mapping[str, object]) -> dict[str, object]:
    action = _canonical_h5_action(raw.get("action"))
    return {**dict(raw), "action": action, **_candidate_metadata(raw)}
```

Make `maf_agent.py` reject NAVIGATE candidates whose trimmed `destination_url` is empty, and retain the existing origin/safety checks.

Expose `evaluate_h5_navigation_candidate(...) -> H5NavigationEligibility` from the H5 observation/eligibility boundary. Validate HTTP(S), exact origin, allowed route prefix and depth, and persist the stable rejection codes asserted above instead of silently dropping candidates. Existing click candidates keep their current behavior.

- [ ] **Step 4: Run navigation tests**

Run: `python -m pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_explorer_bootstrap.py -q`

Expected: all selected tests pass and no newly emitted candidate contains `action="goto"`.

- [ ] **Step 5: Commit the navigation contract**

```bash
git add services/api/app/agents/tools/page_detection_tools.py services/api/app/application/h5_exploration_observation.py services/api/app/agents/explorer/maf_agent.py services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_explorer_bootstrap.py
git commit -m "[AI编程] fix: 统一H5导航合同；模块：H5 Detection/Explorer；数据库迁移：无；前端菜单路由：无；测试风险：同源导航真实浏览器行为待E2E验证"
```

---

### Task 2: Consume Locked H5 Runtime Settings

**Files:**
- Create: `services/api/app/application/h5_task_runtime_settings.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/app/worker.py`
- Test: `services/api/tests/test_h5_task_runtime_settings.py`
- Test: `services/api/tests/test_h5_runtime_composition.py`
- Test: `services/api/tests/test_worker_h5_v2_route.py`

**Interfaces:**
- Consumes: `resolve_task_runtime_settings(defaults, loop_guard, page_sampling_policy)` and locked `record.task_config`, `record.loop_guard`, `record.page_sampling_policy`.
- Produces: `H5TaskRuntimeSettings` and `build_h5_task_runtime_settings(*, task_config: Mapping[str, object], loop_guard: Mapping[str, object], page_sampling_policy: Mapping[str, object], defaults: AgentRuntimeSettings) -> H5TaskRuntimeSettings`.

- [ ] **Step 1: Write failing mode-mapping and runtime-consumption tests**

```python
@pytest.mark.parametrize(
    ("mode", "loop_guard", "sampling", "expected_actions", "expected_depth", "expected_seconds", "expected_pages"),
    [
        ("quick", {"max_steps": 40, "max_depth": 2, "timeout_minutes": 15}, {"max_pages": 3}, 40, 2, 900.0, 3),
        ("full", {"max_steps": 900, "max_depth": 4, "timeout_minutes": 60}, {}, 600, 4, 3600.0, None),
        ("custom", {"max_steps": 75, "max_depth": 3, "timeout_minutes": 60}, {}, 75, 3, 3600.0, None),
    ],
)
def test_build_h5_task_runtime_settings(
    mode, loop_guard, sampling, expected_actions, expected_depth, expected_seconds, expected_pages
):
    result = build_h5_task_runtime_settings(
        task_config={"scan_mode": mode, "max_depth": 8, "journey_steps": ["商品详情"]},
        loop_guard=loop_guard,
        page_sampling_policy={
            **sampling,
            "depth_limit": 6,
            "include_page_types": ["home", "product_detail"],
            "exclude_page_types": ["payment"],
            "rule_target_page_types": ["product_detail"],
        },
        defaults=AgentRuntimeSettings(max_steps=200, max_duration_seconds=3600, max_steps_hard_cap=600),
    )
    assert result.max_actions == expected_actions
    assert result.max_depth == expected_depth
    assert result.max_duration_seconds == expected_seconds
    assert result.max_pages == expected_pages
    assert result.journey_steps == ("商品详情",)
    assert result.include_page_types == ("home", "product_detail")
    assert result.exclude_page_types == ("payment",)
    assert result.rule_target_page_types == ("product_detail",)
```

Also assert invalid or contradictory locked policies raise `InvalidTaskExecutionPolicyError` before the browser session factory is called, and `H5V2Runtime.execute(settings=...)` passes the resolved values to the exploration executor instead of discarding `settings`.

- [ ] **Step 2: Run runtime-setting tests and verify failure**

Run: `python -m pytest services/api/tests/test_h5_task_runtime_settings.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_h5_v2_route.py -q`

Expected: import failure for `h5_task_runtime_settings` or assertions still observe 28/8/600/900.

- [ ] **Step 3: Implement the typed mapping and remove H5 private budgets**

```python
@dataclass(frozen=True, slots=True)
class H5TaskRuntimeSettings:
    scan_mode: str
    max_actions: int
    max_depth: int
    max_duration_seconds: float
    max_pages: int | None
    journey_steps: tuple[str, ...]
    include_page_types: tuple[str, ...]
    exclude_page_types: tuple[str, ...]
    rule_target_page_types: tuple[str, ...]


def build_h5_task_runtime_settings(
    *, task_config, loop_guard, page_sampling_policy, defaults
):
    resolved = resolve_task_runtime_settings(defaults, loop_guard, page_sampling_policy)
    mode = str(task_config.get("scan_mode") or "full")
    depth_values = tuple(
        int(value)
        for value in (
            page_sampling_policy.get("depth_limit"),
            loop_guard.get("max_depth"),
            task_config.get("max_depth"),
        )
        if value is not None
    )
    max_depth = min(depth_values) if depth_values else 0
    return H5TaskRuntimeSettings(
        scan_mode=mode,
        max_actions=resolved.max_steps,
        max_depth=max_depth,
        max_duration_seconds=resolved.max_duration_seconds,
        max_pages=_optional_positive_int(page_sampling_policy, "max_pages"),
        journey_steps=_text_tuple(task_config.get("journey_steps")),
        include_page_types=_text_tuple(page_sampling_policy.get("include_page_types")),
        exclude_page_types=_text_tuple(page_sampling_policy.get("exclude_page_types")),
        rule_target_page_types=_text_tuple(page_sampling_policy.get("rule_target_page_types")),
    )
```

Validate mode and all scalar/list shapes before returning. Change the H5 composition root and runtime method signatures so the settings object is mandatory for execution; compute the deadline from Run start plus `max_duration_seconds`; delete `_DEFAULT_MAX_ACTIONS`, `_DEFAULT_MAX_DEPTH`, the 600-second loop budget and the 900-second hardcoded deadline.

Build the exploration objective from the locked base objective plus `journey_steps`; pass include/exclude/rule-target page types into `sampling_policy_summary`. Task 3 selection must rank candidates matching an unfinished journey step or rule target ahead of equal-risk generic candidates. A policy parse failure must occur before `capture_runtime_factory()` or session registration.

- [ ] **Step 4: Run runtime and worker tests**

Run: `python -m pytest services/api/tests/test_h5_task_runtime_settings.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_h5_v2_route.py -q`

Expected: all selected tests pass; quick/full/custom values are asserted at the executor boundary.

- [ ] **Step 5: Commit runtime policy consumption**

```bash
git add services/api/app/application/h5_task_runtime_settings.py services/api/app/application/h5_runtime_composition.py services/api/app/worker.py services/api/tests/test_h5_task_runtime_settings.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_h5_v2_route.py
git commit -m "[AI编程] fix: H5运行时消费锁定预算；模块：H5 Runtime/Worker；数据库迁移：无；前端菜单路由：无；测试风险：长时任务超时边界待隔离环境验证"
```

---

### Task 3: Define the Versioned H5 Task Frontier

**Files:**
- Create: `services/api/app/domain/exploration/h5_frontier.py`
- Modify: `services/api/app/domain/exploration/__init__.py`
- Test: `services/api/tests/test_h5_task_frontier.py`

**Interfaces:**
- Consumes: `CandidateAction`, `InteractionAction`, source capture/state/location and locked max-page budget.
- Produces: `H5FrontierEntryStatus`, `H5TaskFrontierEntry`, `H5TaskFrontierSnapshot`, `merge_h5_candidates(...)`, `select_next_h5_entry(...)`, `H5_TASK_FRONTIER_METADATA_KEY`.

- [ ] **Step 1: Write failing serialization, merge and selection tests**

```python
def _candidate(candidate_id: str) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=candidate_id,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=10,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={"expected_next_page_type": candidate_id},
    )


def _merge_args(state_version: str, candidate_id: str) -> dict[str, object]:
    return {
        "source_capture_id": f"capture-{state_version}",
        "source_state_version": state_version,
        "source_location": "https://example.test/home",
        "navigation_depth": 0,
        "candidates": (_candidate(candidate_id),),
    }


def test_h5_task_frontier_round_trip_keeps_siblings_and_hash():
    snapshot = H5TaskFrontierSnapshot.create(max_pages=3).merge(
        source_capture_id="capture-1",
        source_state_version="state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_candidate("product"), _candidate("cart")),
    )
    restored = H5TaskFrontierSnapshot.from_dict(snapshot.to_dict())
    assert [item.candidate.candidate_id for item in restored.pending_entries] == ["product", "cart"]
    assert restored.payload_hash == restored.recompute_payload_hash()


def test_h5_task_frontier_deduplicates_action_fingerprint_across_states():
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(**_merge_args("state-1", "product"))
    second = first.merge(**_merge_args("state-2", "product"))
    assert len(second.entries) == 1
```

- [ ] **Step 2: Run the domain test and verify import failure**

Run: `python -m pytest services/api/tests/test_h5_task_frontier.py -q`

Expected: module or type import failure.

- [ ] **Step 3: Implement immutable Frontier values and deterministic hash**

```python
H5_TASK_FRONTIER_METADATA_KEY = "h5_task_frontier"
H5_TASK_FRONTIER_SCHEMA_VERSION = "h5-task-frontier.v1"


class H5FrontierEntryStatus(StrEnum):
    PENDING = "pending"
    SELECTED = "selected"
    EXECUTED = "executed"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class H5TaskFrontierEntry:
    entry_id: str
    candidate: CandidateAction
    source_capture_id: str
    source_state_version: str
    source_location: str
    navigation_depth: int
    parent_state_version: str | None
    lineage: tuple[str, ...]
    created_at: datetime
    status: H5FrontierEntryStatus = H5FrontierEntryStatus.PENDING
    restoration_attempt_count: int = 0
    last_failure_reason: str | None = None
    selected_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class H5TaskFrontierSnapshot:
    schema_version: str
    snapshot_id: str
    entries: tuple[H5TaskFrontierEntry, ...]
    active_state_version: str | None
    visited_state_ids: frozenset[str]
    executed_candidate_ids: frozenset[str]
    action_fingerprints: frozenset[str]
    covered_page_count: int
    covered_page_types: frozenset[str]
    max_pages: int | None
    pending_count: int
    blocked_count: int
    failed_count: int
    sampling_policy_summary: Mapping[str, object]
    payload_hash: str
```

Use stable JSON (`sort_keys=True`, compact separators) for `payload_hash`; exclude `payload_hash` itself from the hashed payload. Candidate fingerprints use `state_version + operation + semantic target + expected page type`. Selection first chooses a safe pending candidate on the active lineage for bounded depth-first progress; when that lineage is exhausted, it chooses the nearest recoverable sibling and then insertion order for breadth completion. Candidates outside depth, risk, include/exclude/rule-target or max-page policy stay in the snapshot as blocked with stable reason codes. Recompute pending/blocked/failed counts on every immutable transition.

- [ ] **Step 4: Run Frontier unit tests**

Run: `python -m pytest services/api/tests/test_h5_task_frontier.py -q`

Expected: serialization, hash, sibling retention, deduplication and max-pages cases pass.

- [ ] **Step 5: Commit the Frontier domain**

```bash
git add services/api/app/domain/exploration/h5_frontier.py services/api/app/domain/exploration/__init__.py services/api/tests/test_h5_task_frontier.py
git commit -m "[AI编程] feat: 建立H5任务级Frontier；模块：H5 Exploration Domain；数据库迁移：无、快照进入Work payload；前端菜单路由：无；测试风险：真实页面状态等价性待E2E验证"
```

---

### Task 4: Persist Siblings and Restore Source Branches

**Files:**
- Create: `services/api/app/application/h5_exploration_recovery.py`
- Modify: `services/api/app/application/h5_exploration_workflow.py`
- Modify: `services/api/app/application/h5_exploration_turn.py`
- Modify: `services/api/app/application/h5_exploration_observation.py`
- Modify: `services/api/app/infrastructure/db/explorer_work_repository.py`
- Test: `services/api/tests/test_h5_exploration_workflow.py`
- Test: `services/api/tests/test_h5_exploration_turn.py`
- Test: `services/api/tests/test_h5_acceptance_closure.py`

**Interfaces:**
- Consumes: Task 3 `H5TaskFrontierSnapshot.from_dict()/to_dict()/merge()/select_next()`.
- Produces: `restore_h5_frontier_source(*, entry, current_location, browser) -> H5RestorationResult` and successor Work containing the updated task snapshot.

- [ ] **Step 1: Write failing sibling and restoration tests**

```python
@pytest.mark.asyncio
async def test_after_action_keeps_unexecuted_parent_sibling_in_successor_work():
    prior_work = _work_with_frontier("product", "cart")
    outcome = await workflow.prepare_after_action(
        ContinueH5ExplorationCommand(
            prior_work=prior_work,
            turn=_successful_turn(prior_work, selected_candidate_id="product"),
        )
    )
    snapshot = H5TaskFrontierSnapshot.from_dict(
        outcome.successor.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    )
    assert [item.candidate.candidate_id for item in snapshot.pending_entries] == ["cart"]


@pytest.mark.asyncio
async def test_sibling_on_ancestor_uses_back_then_rebaseline_before_execution():
    result = await restore_h5_frontier_source(
        entry=_entry(source_location="https://example.test/home"),
        current_location="https://example.test/product/1",
        browser=browser,
    )
    assert browser.calls == ["back", "capture", "detect", "assess"]
    assert result.restored is True
```

- [ ] **Step 2: Run workflow/turn tests and verify failure**

Run: `python -m pytest services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_acceptance_closure.py -q`

Expected: the successor contains only the newly discovered local frontier, or restoration functions are absent.

- [ ] **Step 3: Merge local candidates into the task snapshot**

```python
snapshot = H5TaskFrontierSnapshot.from_dict(
    work.frontier.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
)
snapshot = snapshot.mark_executed(execution.candidate_id)
snapshot = snapshot.merge(
    source_capture_id=after.capture_id,
    source_state_version=after.state_version,
    source_location=after.location,
    navigation_depth=work.navigation_depth + 1,
    candidates=after.local_frontier.candidates,
)
next_entry, snapshot = snapshot.select_next()
successor = build_successor_work(next_entry, snapshot=snapshot)
```

Implement the deterministic source-restoration primitives in `h5_exploration_recovery.py`: for an ancestor/history source, try `browser.back()`; for another same-origin source, use `browser.navigate(source_location)`. After either route, always capture, detect and assess before executing the selected candidate. Limit restoration to two attempts, preserving the sibling as failed with a reason after exhaustion. Task 8 extends this same module for process/session loss instead of creating a second restoration path.

- [ ] **Step 4: Run H5 workflow regression tests**

Run: `python -m pytest services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_acceptance_closure.py -q`

Expected: parent sibling, child candidates, restore-success, restore-exhaustion and resumed-work cases pass.

- [ ] **Step 5: Commit global Frontier integration**

```bash
git add services/api/app/application/h5_exploration_recovery.py services/api/app/application/h5_exploration_workflow.py services/api/app/application/h5_exploration_turn.py services/api/app/application/h5_exploration_observation.py services/api/app/infrastructure/db/explorer_work_repository.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_acceptance_closure.py
git commit -m "[AI编程] feat: 接通H5全局Frontier与分支恢复；模块：H5 Workflow/Work Queue；数据库迁移：无；前端菜单路由：无；测试风险：浏览器历史栈差异待Playwright E2E验证"
```

---

### Task 5: Add Cooperative Cancellation and Terminal Race Guards

**Files:**
- Create: `services/api/app/application/h5_exploration_cancellation.py`
- Modify: `services/api/app/application/explorer_turn.py`
- Modify: `services/api/app/application/h5_exploration_turn.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/app/application/exploration_work_queue.py`
- Modify: `services/api/app/infrastructure/db/explorer_work_repository.py`
- Modify: `services/api/app/worker.py`
- Test: `services/api/tests/test_h5_exploration_cancellation.py`
- Test: `services/api/tests/test_h5_exploration_turn.py`
- Test: `services/api/tests/test_h5_runtime_composition.py`
- Test: `services/api/tests/test_worker_h5_v2_finalization.py`

**Interfaces:**
- Consumes: Redis cancel flag and PostgreSQL explorer work repository.
- Produces: `H5CancellationPort.is_cancel_requested(task_id: str, agent_run_id: str) -> Awaitable[bool]`, `H5CancellationCheckpoint.raise_if_requested(...)`, optional `ExplorerTurnExecutor(before_side_effect=...)`.

- [ ] **Step 1: Write failing cancellation checkpoint tests**

```python
@pytest.mark.asyncio
async def test_cancel_after_decision_prevents_action_and_cancels_nonterminal_work(
    runner, coordinator, queue
):
    outcome = await runner.run_one(task_id="task-1", agent_run_id="run-1")
    assert outcome.final_status is ExplorerWorkStatus.CANCELLED
    assert coordinator.calls == []
    assert queue.canceled_work_types == {"exploration", "probe"}


@pytest.mark.asyncio
async def test_cancel_during_action_waits_for_after_capture_then_stops_successor(
    runner, capture_roles, queue
):
    outcome = await runner.run_one(task_id="task-1", agent_run_id="run-1")
    assert capture_roles == ["before_action", "after_action"]
    assert outcome.final_status is ExplorerWorkStatus.CANCELLED
    assert queue.successors == []
```

Add a worker finalization test in which cancel wins before completion and assert the task remains canceled.

- [ ] **Step 2: Run cancellation tests and verify new action still occurs**

Run: `python -m pytest services/api/tests/test_h5_exploration_cancellation.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_h5_v2_finalization.py -q`

Expected: cancellation is only observed before/after the whole runtime or completion overwrites canceled.

- [ ] **Step 3: Implement cancellation checkpoints and guarded finalization**

```python
class H5ExplorationCanceled(RuntimeError):
    pass


class H5CancellationPort(Protocol):
    async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool: ...


class H5CancellationCheckpoint:
    async def raise_if_requested(self, *, task_id: str, agent_run_id: str, stage: str) -> None:
        if await self._port.is_cancel_requested(task_id=task_id, agent_run_id=agent_run_id):
            raise H5ExplorationCanceled(f"H5 探索已在 {stage} 阶段取消")
```

Check before session open, before claim, after decision, before action, after the before/action/after evidence boundary, before successor, before probe, before handoff and before Completion Gate. Extend `ExplorerTurnExecutor` with a default-`None` async `before_side_effect` callback so App construction is unchanged. Make the PostgreSQL cancel update include H5 `work_type IN ('exploration', 'probe')`, and add task/run status predicates to completion updates.

- [ ] **Step 4: Run cancellation and App non-regression tests**

Run: `python -m pytest services/api/tests/test_h5_exploration_cancellation.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_app_exploration_turn.py -q`

Expected: all tests pass; App executor works with no hook; canceled is never replaced by completed.

- [ ] **Step 5: Commit cancellation controls**

```bash
git add services/api/app/application/h5_exploration_cancellation.py services/api/app/application/explorer_turn.py services/api/app/application/h5_exploration_turn.py services/api/app/application/h5_runtime_composition.py services/api/app/application/exploration_work_queue.py services/api/app/infrastructure/db/explorer_work_repository.py services/api/app/worker.py services/api/tests/test_h5_exploration_cancellation.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_app_exploration_turn.py
git commit -m "[AI编程] fix: 增加H5协作式取消与终态保护；模块：H5 Runtime/Worker/Work Queue；数据库迁移：无；前端菜单路由：无；测试风险：Redis与PG竞争时序待集成环境验证"
```

---

### Task 6: Migrate and Persist H5 Assessments and Evidence Inventory

**Files:**
- Create: `services/api/migrations/versions/20260812_0049_h5_exploration_audit_remediation.py`
- Create: `services/api/app/infrastructure/db/h5_page_type_assessment_repository.py`
- Modify: `services/api/app/application/h5_exploration_workflow.py`
- Test: `services/api/tests/test_h5_exploration_audit_remediation_migration.py`
- Test: `services/api/tests/test_h5_page_type_assessment_adapter.py`
- Test: `services/api/tests/test_h5_exploration_workflow.py`

**Interfaces:**
- Consumes: assessment payload produced by `adapt_h5_page_detection_to_assessment(...)` plus task/run/session/capture context and the detection result.
- Produces: `H5PageTypeAssessmentRepository.upsert(*, task_id: str, agent_run_id: str, session_id: str, page_capture_id: str, detection_result: Mapping[str, object], assessment_payload: Mapping[str, object])`, `list_for_run(*, task_id: str, agent_run_id: str)`, and tables `qc_h5_page_type_assessment`, `qc_h5_evidence_inventory_item`.

- [ ] **Step 1: Write failing migration and repository tests**

```python
def migration_source(revision: str) -> str:
    path = next(
        Path("services/api/migrations/versions").glob(f"*{revision}*.py")
    )
    return path.read_text(encoding="utf-8")


def test_h5_audit_remediation_migration_declares_tables_and_safe_downgrade():
    source = migration_source("20260812_0049")
    assert "CREATE TABLE qc_h5_page_type_assessment" in source
    assert "CREATE TABLE qc_h5_evidence_inventory_item" in source
    assert "20260812_0048" in source
    assert "RAISE EXCEPTION" in source


@pytest.mark.asyncio
async def test_assessment_upsert_is_idempotent_and_never_downgrades_terminal_status(repository):
    context = {
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "session_id": "h5:run-1",
        "page_capture_id": "capture-1",
        "detection_result": {"detection_status": "recognized", "page_state": "stable"},
    }
    await repository.upsert(
        **context,
        assessment_payload={
            "schema_version": "h5-page-assessment.v2",
            "page_type": "product_detail",
            "status": "covered",
            "reason_code": "h5_page_detection_covered",
        },
    )
    await repository.upsert(
        **context,
        assessment_payload={
            "schema_version": "h5-page-assessment.v1",
            "page_type": "product_detail",
            "status": "evidence_insufficient",
            "reason_code": "h5_page_detection_low_confidence",
        },
    )
    stored = await repository.list_for_run(task_id="task-1", agent_run_id="run-1")
    assert stored[0].status == "covered"
    assert stored[0].schema_version == "h5-page-assessment.v2"
```

- [ ] **Step 2: Run migration/assessment tests and verify failure**

Run: `python -m pytest services/api/tests/test_h5_exploration_audit_remediation_migration.py services/api/tests/test_h5_page_type_assessment_adapter.py services/api/tests/test_h5_exploration_workflow.py -q`

Expected: migration and repository imports are absent.

- [ ] **Step 3: Implement schema and idempotent assessment persistence**

```sql
CREATE UNIQUE INDEX uq_qc_agent_run_task_id_id
    ON qc_agent_run(task_id, id);
CREATE UNIQUE INDEX uq_qc_page_capture_task_id_id
    ON qc_page_capture(task_id, id);
CREATE UNIQUE INDEX uq_qc_capture_task_id_id
    ON qc_capture(task_id, id);
CREATE UNIQUE INDEX uq_qc_action_execution_task_session_id
    ON qc_action_execution(task_id, session_id, action_execution_id);

CREATE TABLE qc_h5_page_type_assessment (
    assessment_record_id VARCHAR(160) PRIMARY KEY,
    assessment_id VARCHAR(160) NOT NULL,
    task_id UUID NOT NULL REFERENCES qc_task(id),
    agent_run_id UUID NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    page_capture_id UUID NOT NULL,
    detection_status VARCHAR(64) NOT NULL,
    page_type TEXT NOT NULL,
    page_state TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'covered', 'requires_auth', 'blocked_by_safety', 'not_found',
        'evidence_insufficient', 'not_applicable'
    )),
    reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    safety_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    assessment_payload JSONB NOT NULL,
    schema_version VARCHAR(64) NOT NULL,
    payload_hash VARCHAR(128) NOT NULL,
    row_version BIGINT NOT NULL DEFAULT 0 CHECK (row_version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (task_id, session_id)
        REFERENCES qc_surface_session(task_id, session_id),
    FOREIGN KEY (task_id, agent_run_id)
        REFERENCES qc_agent_run(task_id, id),
    FOREIGN KEY (task_id, page_capture_id)
        REFERENCES qc_page_capture(task_id, id),
    UNIQUE (page_capture_id, schema_version),
    CHECK (jsonb_typeof(reason_codes) = 'array'),
    CHECK (jsonb_typeof(safety_summary) = 'object'),
    CHECK (jsonb_typeof(assessment_payload) = 'object')
);

CREATE TABLE qc_h5_evidence_inventory_item (
    inventory_item_id VARCHAR(160) PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES qc_task(id),
    agent_run_id UUID NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    action_execution_id VARCHAR(96) NOT NULL,
    before_capture_id UUID NULL,
    after_capture_id UUID NULL,
    page_type TEXT,
    page_state TEXT,
    status TEXT NOT NULL CHECK (status IN ('pass', 'insufficient', 'failed')),
    reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    required_artifacts JSONB NOT NULL DEFAULT '[]'::jsonb,
    found_artifacts JSONB NOT NULL DEFAULT '[]'::jsonb,
    verification_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    published_inspection_count INTEGER NOT NULL DEFAULT 0 CHECK (published_inspection_count >= 0),
    schema_version VARCHAR(64) NOT NULL,
    payload_hash VARCHAR(128) NOT NULL,
    row_version BIGINT NOT NULL DEFAULT 0 CHECK (row_version >= 0),
    verified_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (task_id, session_id)
        REFERENCES qc_surface_session(task_id, session_id),
    FOREIGN KEY (task_id, agent_run_id)
        REFERENCES qc_agent_run(task_id, id),
    FOREIGN KEY (task_id, session_id, action_execution_id)
        REFERENCES qc_action_execution(task_id, session_id, action_execution_id),
    FOREIGN KEY (task_id, before_capture_id)
        REFERENCES qc_capture(task_id, id),
    FOREIGN KEY (task_id, after_capture_id)
        REFERENCES qc_capture(task_id, id),
    UNIQUE (action_execution_id),
    CHECK (jsonb_typeof(reason_codes) = 'array'),
    CHECK (jsonb_typeof(required_artifacts) = 'array'),
    CHECK (jsonb_typeof(found_artifacts) = 'array'),
    CHECK (jsonb_typeof(verification_summary) = 'object')
);
```

Add task/agent-run/session/capture/action/status indexes. The four supporting unique indexes and composite foreign keys above make task ownership a database invariant instead of an application-only check. Upsert only when the same natural key and payload hash are identical or the row version advances; an older schema version cannot overwrite a newer assessment. In `downgrade()`, raise when either new table contains rows, then drop the new tables, their query indexes and the four supporting ownership indexes. Persist every assessment immediately after detection/assessment and before generating the successor Work; a persistence failure aborts publication of the initial/successor Work.

- [ ] **Step 4: Run migration and persistence tests**

Run: `python -m pytest services/api/tests/test_h5_exploration_audit_remediation_migration.py services/api/tests/test_h5_page_type_assessment_adapter.py services/api/tests/test_h5_exploration_workflow.py -q`

Expected: schema assertions, idempotent upsert and workflow persistence ordering pass.

- [ ] **Step 5: Commit the migration and assessment repository**

```bash
git add services/api/migrations/versions/20260812_0049_h5_exploration_audit_remediation.py services/api/app/infrastructure/db/h5_page_type_assessment_repository.py services/api/app/application/h5_exploration_workflow.py services/api/tests/test_h5_exploration_audit_remediation_migration.py services/api/tests/test_h5_page_type_assessment_adapter.py services/api/tests/test_h5_exploration_workflow.py
git commit -m "[AI编程] feat: 持久化H5页面判定与证据库存结构；模块：H5 Assessment/Evidence；数据库迁移：新增0049两张表；前端菜单路由：无；测试风险：真实PG升级降级待隔离库验证"
```

---

### Task 7: Build Expected-Action Evidence Inventory and Re-read Objects

**Files:**
- Create: `services/api/app/application/h5_evidence_inventory.py`
- Create: `services/api/app/infrastructure/db/h5_evidence_inventory_repository.py`
- Create: `services/api/app/infrastructure/storage/h5_evidence_object_verifier.py`
- Modify: `services/api/app/application/h5_evidence_handoff.py`
- Modify: `services/api/app/infrastructure/db/h5_evidence_handoff_repository.py`
- Modify: `services/api/app/worker.py`
- Test: `services/api/tests/test_h5_evidence_inventory.py`
- Test: `services/api/tests/test_h5_evidence_handoff.py`
- Test: `services/api/tests/test_worker_h5_v2_finalization.py`

**Interfaces:**
- Consumes: expected `qc_action_execution` rows, optional before/after captures, artifact metadata, `EvidenceArtifactReadPort.read(storage_path)` implemented by the existing local/MinIO evidence stores.
- Produces: `H5EvidenceArtifactRef`, `H5EvidenceInventoryItem`, `H5EvidenceInventorySummary`, `H5EvidenceObjectVerifier.verify(...)`, and partial handoff statuses `pass|insufficient|failed`.

- [ ] **Step 1: Write failing missing-side, checksum and partial-success tests**

```python
def _artifact(*, checksum: str, size_bytes: int) -> H5EvidenceArtifactRef:
    return H5EvidenceArtifactRef(
        artifact_id="artifact-1",
        task_id="task-1",
        agent_run_id="run-1",
        storage_path="h5/task-1/artifact-1.png",
        artifact_type="screenshot",
        checksum=checksum,
        size_bytes=size_bytes,
    )


@pytest.mark.asyncio
async def test_inventory_retains_action_when_after_capture_is_missing():
    items = await repository.list_expected_items(task_id="task-1", agent_run_id="run-1")
    assert len(items) == 1
    assert items[0].action_execution_id == "action-1"
    assert items[0].after_capture_id is None


@pytest.mark.asyncio
async def test_object_verifier_rejects_checksum_mismatch(verifier):
    result = await verifier.verify(
        artifact=_artifact(checksum="sha256:" + "0" * 64, size_bytes=3),
        expected_task_id="task-1",
        expected_agent_run_id="run-1",
    )
    assert result.status == "failed"
    assert result.reason_code == "artifact_checksum_mismatch"


@pytest.mark.asyncio
async def test_handoff_publishes_valid_item_and_keeps_insufficient_item(service, publisher):
    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
    )
    assert summary.pass_count == 1
    assert summary.insufficient_count == 1
    assert publisher.capture_ids == ["after-valid"]


@pytest.mark.asyncio
async def test_exploration_only_keeps_inventory_without_publishing_rule_work(
    service, publisher
):
    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
    )
    assert summary.pass_count == 1
    assert publisher.capture_ids == []
```

Extend the existing worker test `test_exploration_only_h5_finishes_without_formal_inspection` to assert the summary counts are copied to `record.usage`, `task_finalization_service` is passed as `None`, and drain/rule/final-report hooks are not called.

- [ ] **Step 2: Run evidence tests and verify disappearing/whole-batch failure**

Run: `python -m pytest services/api/tests/test_h5_evidence_inventory.py services/api/tests/test_h5_evidence_handoff.py services/api/tests/test_worker_h5_v2_finalization.py -q`

Expected: the missing-side action is absent or one insufficient item raises `H5EvidenceNotReadyError` for the whole batch.

- [ ] **Step 3: Implement inventory classification and object verification**

```python
class H5EvidenceInventoryStatus(StrEnum):
    PASS = "pass"
    INSUFFICIENT = "insufficient"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class H5EvidenceArtifactRef:
    artifact_id: str
    task_id: str
    agent_run_id: str
    storage_path: str
    artifact_type: str
    checksum: str
    size_bytes: int


class EvidenceArtifactReadPort(Protocol):
    async def read(self, storage_path: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class H5EvidenceInventoryItem:
    action_execution_id: str
    before_capture_id: str | None
    after_capture_id: str | None
    status: H5EvidenceInventoryStatus
    reason_codes: tuple[str, ...]
    verification_summary: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class H5EvidenceInventorySummary:
    total: int
    pass_count: int
    insufficient_count: int
    failed_count: int
    items: tuple[H5EvidenceInventoryItem, ...]


async def verify_artifact_bytes(
    *, store: EvidenceArtifactReadPort, artifact, expected_task_id, expected_agent_run_id
):
    payload = await store.read(artifact.storage_path)
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if (
        artifact.task_id != expected_task_id
        or artifact.agent_run_id != expected_agent_run_id
    ):
        return _failed("artifact_ownership_mismatch")
    if len(payload) != artifact.size_bytes:
        return _failed("artifact_size_mismatch")
    if digest != artifact.checksum:
        return _failed("artifact_checksum_mismatch")
    return _pass(size_bytes=len(payload), checksum=digest)
```

Change the SQL driving query to `qc_action_execution LEFT JOIN before_capture LEFT JOIN after_capture`; include the initial NAVIGATE action. Validate before/after action identity, URL, recognized page type, Screenshot and DOM in addition to object bytes. Upsert every item independently. Publish only `pass` items, retain insufficient/failed facts, and map `insufficient` to `incomplete/needs_review`; map corrupt bytes, checksum/ownership/identity conflicts to the stable technical terminal `failed/evidence_integrity_failed`. A handoff infrastructure outage remains a system failure. Repeated handoff with the same payload hash must be idempotent and must not decrease `published_inspection_count`. In exploration-only mode, persist and expose the inventory but do not publish Rule Judge work and do not invoke Completion Gate; in full-inspection mode, publish passed snapshots and require Inspection/Rule/Completion closure.

- [ ] **Step 4: Run evidence, worker and object-store regressions**

Run: `python -m pytest services/api/tests/test_h5_evidence_inventory.py services/api/tests/test_h5_evidence_handoff.py services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_h5_acceptance_closure.py -q`

Expected: missing before/after, wrong owner, missing object, wrong size, checksum mismatch, partial success and all-pass cases pass.

- [ ] **Step 5: Commit evidence inventory closure**

```bash
git add services/api/app/application/h5_evidence_inventory.py services/api/app/infrastructure/db/h5_evidence_inventory_repository.py services/api/app/infrastructure/storage/h5_evidence_object_verifier.py services/api/app/application/h5_evidence_handoff.py services/api/app/infrastructure/db/h5_evidence_handoff_repository.py services/api/app/worker.py services/api/tests/test_h5_evidence_inventory.py services/api/tests/test_h5_evidence_handoff.py services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_h5_acceptance_closure.py
git commit -m "[AI编程] fix: 建立H5预期动作证据库存；模块：H5 Evidence/Worker；数据库迁移：使用0049库存表；前端菜单路由：无；测试风险：MinIO对象重读待真实环境验证"
```

---

### Task 8: Recover from Browser Loss Without Replaying Unknown Actions

**Files:**
- Modify: `services/api/app/application/h5_exploration_recovery.py`
- Modify: `services/api/app/application/h5_exploration_turn.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Modify: `services/api/app/application/h5_exploration_workflow.py`
- Test: `services/api/tests/test_h5_exploration_recovery.py`
- Test: `services/api/tests/test_h5_exploration_turn.py`
- Test: `services/api/tests/test_h5_runtime_composition.py`

**Interfaces:**
- Consumes: capture coordination stage, active Frontier entry, session factory, existing Restoration/State Equivalence facts.
- Produces: `H5FailureStage`, `H5RecoveryContext`, `H5RecoveryOutcome`, `H5ExplorationRecovery.recover(...)`.

- [ ] **Step 1: Write failing stage-semantics tests**

```python
def _context(stage: str) -> H5RecoveryContext:
    return H5RecoveryContext(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="h5:run-1",
        stage=H5FailureStage(stage),
        last_safe_location="https://example.test/home",
        action_execution_id="action-1",
        frontier_snapshot=H5TaskFrontierSnapshot.create(max_pages=3),
        recovery_attempt_count=0,
        max_recovery_attempts=2,
    )


@pytest.mark.asyncio
async def test_before_capture_loss_retries_safely_once(recovery, session_factory):
    outcome = await recovery.recover(context=_context(stage="before_capture"))
    assert outcome.retry_original_action is True
    assert session_factory.open_count == 1


@pytest.mark.asyncio
async def test_action_loss_never_replays_unknown_action(recovery, driver):
    outcome = await recovery.recover(context=_context(stage="action"))
    assert outcome.retry_original_action is False
    assert outcome.inventory_status == "insufficient"
    assert driver.action_calls == 0


@pytest.mark.asyncio
async def test_after_capture_loss_preserves_action_and_rebaselines(recovery):
    outcome = await recovery.recover(context=_context(stage="after_capture"))
    assert outcome.supersede_failed_work is True
    assert outcome.rebaseline_steps == ("capture", "detect", "assess")
```

- [ ] **Step 2: Run recovery tests and verify only BEFORE_CAPTURE is retryable**

Run: `python -m pytest services/api/tests/test_h5_exploration_recovery.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py -q`

Expected: recovery module is absent and ACTION/AFTER_CAPTURE terminates without a recovery Work.

- [ ] **Step 3: Implement bounded reopen, safe-location restore and rebaseline**

```python
class H5FailureStage(StrEnum):
    BEFORE_CAPTURE = "before_capture"
    ACTION = "action"
    AFTER_CAPTURE = "after_capture"


@dataclass(frozen=True, slots=True)
class H5RecoveryContext:
    task_id: str
    agent_run_id: str
    session_id: str
    stage: H5FailureStage
    last_safe_location: str
    action_execution_id: str | None
    frontier_snapshot: H5TaskFrontierSnapshot
    recovery_attempt_count: int
    max_recovery_attempts: int


@dataclass(frozen=True, slots=True)
class H5RecoveryOutcome:
    retry_original_action: bool
    inventory_status: str | None
    supersede_failed_work: bool
    rebaseline_steps: tuple[str, ...]
    recovery_work: ExplorerWork | None
```

Before-capture loss may reopen and retry once. ACTION loss records an unknown result and never invokes the original action again. AFTER_CAPTURE loss preserves the execution fact and creates an insufficient inventory item. Both unknown/after cases reopen, navigate to the last safe same-origin location, capture/detect/assess, write Restoration and State Equivalence facts, supersede the failed work and continue from the persisted task Frontier. Count recovery attempts against the original action/time budget.

- [ ] **Step 4: Run recovery, Frontier and cancellation regressions together**

Run: `python -m pytest services/api/tests/test_h5_exploration_recovery.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_cancellation.py -q`

Expected: no action replay after unknown result; recovery stops at its bound; cancellation still prevents the next recovered action.

- [ ] **Step 5: Commit browser recovery**

```bash
git add services/api/app/application/h5_exploration_recovery.py services/api/app/application/h5_exploration_turn.py services/api/app/application/h5_runtime_composition.py services/api/app/application/h5_exploration_workflow.py services/api/tests/test_h5_exploration_recovery.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py
git commit -m "[AI编程] feat: 增加H5浏览器崩溃有界恢复；模块：H5 Runtime/Workflow；数据库迁移：无；前端菜单路由：无；测试风险：浏览器进程级崩溃待Playwright故障注入验证"
```

---

### Task 9: Project H5 V2 Facts into the Workstation Read Model

**Files:**
- Modify: `services/api/app/infrastructure/db/task_insight_repository.py`
- Modify: `services/api/app/application/task_insight.py`
- Modify: `services/api/app/application/app_inspection_publisher.py`
- Modify: `services/api/app/application/workstation_tasks.py`
- Test: `services/api/tests/test_task_insight_repository.py`
- Test: `services/api/tests/test_task_insight_service.py`
- Test: `services/api/tests/test_task_insight_api.py`
- Test: `services/api/tests/test_workstation_tasks_api.py`

**Interfaces:**
- Consumes: `qc_task.target_platform`, `qc_v2_work_item`, `qc_action_execution`, `qc_capture/qc_page_capture`, `qc_h5_page_type_assessment`, `qc_h5_evidence_inventory_item`.
- Produces: H5 timeline fields `frontier`, `assessments`, `evidence_inventory`, formal platform stage, `build_capture_metadata_patch(...)`, and execution-mode-aware `report_status_from_task_status(...)` / `available_actions_for_status(...)`.

- [ ] **Step 1: Write failing H5 V2 projection tests**

```python
@pytest.mark.asyncio
async def test_h5_timeline_reads_v2_facts_without_legacy_nodes_or_working_memory(service):
    timeline = await service.page_timeline("task-h5")
    assert timeline["nodes"][0]["page_capture_id"] == "capture-v2"
    assert timeline["exploration_actions"][0]["action"] == "navigate"
    assert timeline["h5_evidence_inventory"]["insufficient_count"] == 1
    assert timeline["h5_assessments"][0]["status"] == "covered"


@pytest.mark.asyncio
async def test_h5_terminal_projection_requires_global_frontier_and_inventory_closure(service):
    timeline = await service.page_timeline("task-h5-needs-review")
    assert timeline["exploration_summary"]["complete"] is False
    assert timeline["exploration_summary"]["quality_status"] == "needs_review"
    assert timeline["terminal_reason"] == "evidence_insufficient"


def test_inspection_publisher_writes_nested_and_flat_status():
    patch = build_capture_metadata_patch(inspection_status="published")
    assert patch["inspection"]["status"] == "published"
    assert patch["inspection_status"] == "published"


def test_exploration_only_completed_report_is_not_applicable():
    report_status = report_status_from_task_status(
        "completed",
        total_rules=0,
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
    )
    actions = available_actions_for_status(
        "completed",
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
    )
    assert report_status == "not_applicable"
    assert "report" not in actions
```

- [ ] **Step 2: Run task-insight tests and verify they still use legacy facts**

Run: `python -m pytest services/api/tests/test_task_insight_repository.py services/api/tests/test_task_insight_service.py services/api/tests/test_task_insight_api.py services/api/tests/test_workstation_tasks_api.py -q`

Expected: H5 V2 nodes/actions are empty, platform is inferred heuristically, or report status conflicts.

- [ ] **Step 3: Implement direct V2 projection and schema-compatible inspection reads**

```python
class TaskInsightRepository(Protocol):
    async def list_h5_v2_work_items(self, *, task_id: str, agent_run_id: str) -> list[dict[str, object]]: ...
    async def list_h5_action_executions(self, *, task_id: str, agent_run_id: str) -> list[dict[str, object]]: ...
    async def list_h5_page_captures(self, *, task_id: str, agent_run_id: str) -> list[dict[str, object]]: ...
    async def list_h5_page_type_assessments(self, *, task_id: str, agent_run_id: str) -> list[dict[str, object]]: ...
    async def list_h5_evidence_inventory(self, *, task_id: str, agent_run_id: str) -> list[dict[str, object]]: ...
```

Keep these as separate bounded queries so a session's multiple Work and Action rows do not form a Cartesian product. Scope every query to the current `agent_run_id`; for Work/Action facts use the H5 session ID and Work payload's `agent_run_id`, and for Capture/Assessment/Inventory use their formal run reference. Query actions by `qc_action_execution.task_id`; correlate before/after using `requested_source_capture_id`, `source_capture_id`, `result_capture_id` and `qc_capture.action_execution_id`. Correlate Work history through the H5 payload's `parent_work_id`, branch lineage and task Frontier metadata. Build page sequence from capture/action timestamps and lineage; never consult WorkingMemory as a formal source. Preserve old queries only for non-H5 compatibility.

Update `qc_page_capture.inspection_status` and the compatible nested/flat common-capture metadata in one transaction. Readers prefer the formal column, then nested `metadata.inspection.status`, then flat compatibility. Derive platform exclusively from `target_platform`; only a separately named legacy fallback may inspect product/URL when `target_platform` is null. Compute terminal status from global pending work/Frontier, auth/blocked/recovery reasons, inventory status, and—only for full inspection—Inspection/Rule/Completion Gate facts.

- [ ] **Step 4: Run backend read-model tests**

Run: `python -m pytest services/api/tests/test_task_insight_repository.py services/api/tests/test_task_insight_service.py services/api/tests/test_task_insight_api.py services/api/tests/test_workstation_tasks_api.py services/api/tests/test_h5_v2_api_business_closure.py -q`

Expected: V2-only fixtures produce a populated timeline; exploration-only is not applicable; inspection metadata is readable in both forms.

- [ ] **Step 5: Commit the H5 V2 read model**

```bash
git add services/api/app/infrastructure/db/task_insight_repository.py services/api/app/application/task_insight.py services/api/app/application/app_inspection_publisher.py services/api/app/application/workstation_tasks.py services/api/tests/test_task_insight_repository.py services/api/tests/test_task_insight_service.py services/api/tests/test_task_insight_api.py services/api/tests/test_workstation_tasks_api.py services/api/tests/test_h5_v2_api_business_closure.py
git commit -m "[AI编程] fix: 工作台直读H5 V2探索事实；模块：Task Insight/Workstation API；数据库迁移：读取0049事实表；前端菜单路由：无新增；测试风险：大任务聚合查询性能待真实PG验证"
```

---

### Task 10: Render H5 Exploration-only and Evidence Results in the Workstation

**Files:**
- Modify: `app/workstation/src/types/task-insight.ts`
- Modify: `app/workstation/src/types/task.ts`
- Modify: `app/workstation/src/modules/tasks/pages/TaskDetailView.vue`
- Create: `app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts`
- Test: `app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts`

**Interfaces:**
- Consumes: Task 9 H5 timeline projection and existing `execution_mode`/`report_status` task fields.
- Produces: typed `H5FrontierProjection`, `H5AssessmentProjection`, `H5EvidenceInventoryProjection`; exploration-only result banner and evidence attention state.

- [ ] **Step 1: Write failing component tests**

```typescript
it('H5 仅探索完成时隐藏报告入口并展示探索结果', async () => {
  const wrapper = mountTaskDetail({
    task: { target_platform: 'h5', execution_mode: 'exploration_only', report_status: 'not_applicable' },
    timeline: h5Timeline({ pass_count: 2, insufficient_count: 0 }),
  })
  expect(wrapper.text()).not.toContain('查看报告')
  expect(wrapper.text()).toContain('页面探索已完成')
  expect(wrapper.text()).toContain('证据完整 2 项')
})

it('H5 证据不足时展示待复核而不是任务失败', async () => {
  const wrapper = mountTaskDetail({ timeline: h5Timeline({ pass_count: 1, insufficient_count: 1 }) })
  expect(wrapper.text()).toContain('证据不足 1 项')
  expect(wrapper.text()).toContain('需要复核')
})
```

- [ ] **Step 2: Run the component test and verify the old report action appears**

Run: `pnpm --dir app/workstation test -- src/modules/tasks/pages/TaskDetailView.test.ts`

Expected: report button or H5 evidence summary assertions fail.

- [ ] **Step 3: Add explicit projection types and view-state computations**

```typescript
export interface H5EvidenceInventoryProjection {
  total: number
  pass_count: number
  insufficient_count: number
  failed_count: number
  status_counts: Record<string, number>
}

export interface H5AssessmentProjection {
  page_capture_id: string
  page_type: string
  status: PageTypeAssessmentStatus
  reason_code: string
}

const isH5ExplorationOnly = computed(() =>
  task.value?.target_platform === 'h5' && task.value?.execution_mode === 'exploration_only',
)
const canViewReport = computed(() =>
  !isH5ExplorationOnly.value &&
    (task.value?.available_actions.includes('report') ?? false) &&
    task.value?.report_status === 'generated',
)
```

Render one compact H5 result banner above the timeline. Show complete, needs-review or technical-failed tone from task terminal state plus inventory counts; retain the existing Page Exploration panel for detail.

- [ ] **Step 4: Run frontend unit, type and build checks**

Run: `pnpm --dir app/workstation test -- src/modules/tasks/pages/TaskDetailView.test.ts`

Run: `pnpm --dir app/workstation exec vue-tsc --noEmit -p tsconfig.app.json`

Run: `pnpm --dir app/workstation build`

Expected: component tests pass, TypeScript reports no errors and Vite build succeeds.

- [ ] **Step 5: Commit the Workstation presentation**

```bash
git add app/workstation/src/types/task-insight.ts app/workstation/src/types/task.ts app/workstation/src/modules/tasks/pages/TaskDetailView.vue app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts
git commit -m "[AI编程] fix: 展示H5探索与证据终态；模块：Workstation Task Detail；数据库迁移：无；前端菜单路由：无新增或变更；测试风险：不同分辨率视觉回归待浏览器检查"
```

---

### Task 11: Fix H5-only Completion Configuration and Unused Wakeups

**Files:**
- Modify: `services/api/app/worker.py`
- Modify: `services/api/app/application/exploration_work_queue.py`
- Modify: `services/api/app/infrastructure/db/explorer_work_repository.py`
- Modify: `services/api/app/application/h5_runtime_composition.py`
- Test: `services/api/tests/test_task_completion_gate.py`
- Test: `services/api/tests/test_outbox_writer.py`
- Test: `services/api/tests/test_h5_runtime_composition.py`
- Test: `services/api/tests/test_app_runtime_composition.py`

**Interfaces:**
- Consumes: `settings.app_qc_enabled`, H5 V2 mode, explorer work publisher.
- Produces: startup predicate `app_qc_enabled or h5_v2_mode == "on"`; H5-only `emit_wakeup=False` while App stays `True`.

- [ ] **Step 1: Write failing startup and outbox-suppression tests**

```python
def _settings(*, app_qc_enabled: bool, h5_v2_mode: str):
    return SimpleNamespace(
        app_qc_enabled=app_qc_enabled,
        workflow=SimpleNamespace(h5_v2_mode=h5_v2_mode),
    )


def test_completion_gate_is_validated_when_only_h5_v2_is_on():
    with pytest.raises(CompletionGateConfigurationError):
        validate_completion_gate_configuration(
            settings=_settings(app_qc_enabled=False, h5_v2_mode="on"),
            gate=None,
        )


@pytest.mark.asyncio
async def test_h5_web_work_does_not_emit_unused_explorer_or_probe_wakeup(
    repository, h5_work
):
    await repository.enqueue(h5_work, emit_wakeup=False)
    assert repository.outbox_events == []


@pytest.mark.asyncio
async def test_app_work_still_emits_wakeup(repository, app_work):
    await repository.enqueue(app_work, emit_wakeup=True)
    assert len(repository.outbox_events) == 1
```

Also cover missing PostgreSQL state store, database URL and Finalization Service under the same startup predicate; each must fail before the worker polling loop starts.

- [ ] **Step 2: Run gate/outbox tests and verify H5-only bypass and wakeup emission**

Run: `python -m pytest services/api/tests/test_task_completion_gate.py services/api/tests/test_outbox_writer.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_app_runtime_composition.py -q`

Expected: H5-only accepts a missing gate and H5 enqueue writes an unused outbox event.

- [ ] **Step 3: Implement the startup predicate and explicit wakeup option**

```python
completion_required = settings.app_qc_enabled or settings.workflow.h5_v2_mode == "on"
if not completion_required:
    return
if completion_gate is None:
    raise CompletionGateConfigurationError("启用 App QC 或 H5 V2 时必须配置 Completion Gate")
```

Add keyword-only `emit_wakeup: bool = True` to the PostgreSQL enqueue/successor methods. Skip only the outbox insert when false; always persist the Work transactionally. Pass false exclusively from the H5 web runtime composition. App callers omit the parameter and preserve current behavior.

- [ ] **Step 4: Run shared control-plane regressions**

Run: `python -m pytest services/api/tests/test_task_completion_gate.py services/api/tests/test_outbox_writer.py services/api/tests/test_outbox_relay.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_app_runtime_composition.py -q`

Expected: H5-only startup gate is enforced, H5 wakeup is absent and App outbox behavior is unchanged.

- [ ] **Step 5: Commit H5 control-plane fixes**

```bash
git add services/api/app/worker.py services/api/app/application/exploration_work_queue.py services/api/app/infrastructure/db/explorer_work_repository.py services/api/app/application/h5_runtime_composition.py services/api/tests/test_task_completion_gate.py services/api/tests/test_outbox_writer.py services/api/tests/test_outbox_relay.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_app_runtime_composition.py
git commit -m "[AI编程] fix: 补齐H5启动门禁并抑制无消费者唤醒；模块：Worker/Completion/Outbox；数据库迁移：无；前端菜单路由：无；测试风险：多Worker混合部署待集成验证"
```

---

### Task 12: Run the Final H5 Audit, Document Regressions, and Verify the Release Gate

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`
- Create: `docs/reviews/2026-08-12-h5-page-exploration-audit-remediation-review.md`
- Review: `C:/Users/40389/Downloads/Telegram Desktop/2026-08-12-h5-app-page-exploration-audit-report.html`
- Review: all files changed in Tasks 1–11

**Interfaces:**
- Consumes: approved design, original HTML audit, all code/test evidence from Tasks 1–11.
- Produces: one evidence-linked P1/P2/shared-H5 audit matrix and a documented production-environment acceptance list.

- [ ] **Step 1: Add durable H5 regression rules**

Insert the following checklist under a dedicated H5 section in `docs/专项能力与高频回归规则.md`:

```markdown
### H5 页面探索闭环

1. 新写入导航动作只能使用 `navigate + destination_url`，测试必须覆盖空目标与历史 `goto` 兼容边界。
2. 每次提交后继 Work 必须断言父页面未执行 sibling 仍存在于版本化任务 Frontier。
3. quick/full/custom 必须在 Runtime 边界断言动作、深度、时长、页面数和旅程目标。
4. 取消测试至少覆盖 decision 后、action 中、successor 前和 completion 竞争。
5. ACTION 结果未知时禁止自动重放；恢复必须 reopen、回到安全位置并 rebaseline。
6. 证据库存必须从预期 Action Execution 左连接 before/after，缺失证据不得从库存消失。
7. 交接前必须重读对象并校验 task/run 归属、size 和 SHA-256。
8. H5 工作台必须直接读取 V2 Work/Action/Capture/Assessment/Inventory，不得以 WorkingMemory 作为正式事实源。
9. H5-only 开启时 Completion Gate 配置必须通过启动校验；H5 web work 不写无人消费的 Explorer/Probe 唤醒。
10. exploration-only 报告状态固定为 `not_applicable`，探索结果仍可查看。
```

- [ ] **Step 2: Run focused backend regression suites**

Run: `python -m pytest services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_task_runtime_settings.py services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_exploration_cancellation.py services/api/tests/test_h5_exploration_recovery.py services/api/tests/test_h5_exploration_audit_remediation_migration.py services/api/tests/test_h5_evidence_inventory.py services/api/tests/test_h5_evidence_handoff.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_task_insight_repository.py services/api/tests/test_task_insight_service.py services/api/tests/test_task_insight_api.py services/api/tests/test_workstation_tasks_api.py services/api/tests/test_task_completion_gate.py services/api/tests/test_outbox_writer.py services/api/tests/test_app_runtime_composition.py -q`

Expected: all selected tests pass with zero failures.

- [ ] **Step 3: Run backend broad verification and migration-chain checks**

Run: `python -m pytest services/api/tests -q`

Run: `python -m compileall -q services/api/app`

Run: `python -m alembic -c services/api/migrations/alembic.ini heads`

Expected: full API tests pass, compileall exits zero, and the only Alembic head is `20260812_0049`.

- [ ] **Step 4: Run frontend verification**

Run: `pnpm --dir app/workstation test`

Run: `pnpm --dir app/workstation exec vue-tsc --noEmit -p tsconfig.app.json`

Run: `pnpm --dir app/workstation build`

Expected: Vitest, TypeScript and Vite build all exit zero.

- [ ] **Step 5: Run static contract searches and diff hygiene checks**

Run: `rg -n '"goto"|action\s*=\s*"goto"' services/api/app/agents/tools/page_detection_tools.py services/api/app/application/h5_exploration_observation.py services/api/app/application/h5_runtime_composition.py`

Expected: no new-write H5 path emits `goto`; compatibility parsing may appear only in its named normalization helper and test fixture.

Run: `rg -n '_DEFAULT_MAX_ACTIONS|_DEFAULT_MAX_DEPTH|600_000|900' services/api/app/application/h5_runtime_composition.py`

Expected: no hardcoded old H5 runtime budget remains.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 6: Write the final attachment-to-code audit matrix**

Create `docs/reviews/2026-08-12-h5-page-exploration-audit-remediation-review.md` with this exact table shape and one row for every P1, P2 and shared-H5 issue from the original report:

```markdown
| 审计项 | 原问题 | 代码证据 | 测试证据 | 结论 | 环境门禁 |
|---|---|---|---|---|---|
| H5-P1-01 | goto/navigate/destination_url 合同断裂 | `page_detection_tools.py`、`h5_exploration_observation.py` | `test_page_detection_tools.py`、`test_h5_exploration_observation.py` | 已闭环 | Playwright 同源导航待环境执行 |
| H5-P1-02 | 单后继导致 sibling 分支丢失 | `h5_frontier.py`、`h5_exploration_workflow.py` | `test_h5_task_frontier.py`、`test_h5_exploration_workflow.py` | 已闭环 | 多分支真实站点待环境执行 |
| H5-P1-03 | 提交预算与抽样设置未被 Runtime 消费 | `h5_task_runtime_settings.py`、`h5_runtime_composition.py` | `test_h5_task_runtime_settings.py`、`test_h5_runtime_composition.py` | 已闭环 | 长时任务边界待环境执行 |
| H5-P1-04 | 运行中取消不生效且完成态可覆盖取消 | `h5_exploration_cancellation.py`、`h5_exploration_turn.py` | `test_h5_exploration_cancellation.py`、`test_worker_h5_v2_finalization.py` | 已闭环 | Redis/PG 竞争注入待环境执行 |
| H5-P1-05 | H5 V2 事实无法由工作台回读 | `task_insight_repository.py`、`task_insight.py` | `test_task_insight_repository.py`、`test_task_insight_service.py` | 已闭环 | 大任务查询性能待真实 PG 执行 |
| H5-P1-06 | 证据查询内连接丢项且单项不足拖垮整批 | `h5_evidence_inventory_repository.py`、`h5_evidence_handoff.py` | `test_h5_evidence_inventory.py`、`test_h5_evidence_handoff.py` | 已闭环 | 真实对象库存待环境执行 |
| H5-P1-07 | H5-only 未执行 Completion Gate 启动校验 | `worker.py` | `test_task_completion_gate.py` | 已闭环 | H5-only 容器启动待环境执行 |
| H5-P2-01 | PageTypeAssessment 仅存在于内存 | `20260812_0049_h5_exploration_audit_remediation.py`、`h5_page_type_assessment_repository.py` | `test_h5_exploration_audit_remediation_migration.py`、`test_h5_exploration_workflow.py` | 已闭环 | 真实 PG upgrade/downgrade 待环境执行 |
| H5-P2-02 | ACTION/AFTER_CAPTURE 浏览器崩溃无恢复 | `h5_exploration_recovery.py` | `test_h5_exploration_recovery.py` | 已闭环 | Playwright 进程崩溃注入待环境执行 |
| H5-P2-03 | 交接未重读对象字节/checksum | `h5_evidence_object_verifier.py` | `test_h5_evidence_inventory.py` | 已闭环 | MinIO 重读与篡改注入待环境执行 |
| H5-SHARED-01 | H5 Explorer/Probe 写无人消费的唤醒事件 | `explorer_work_repository.py`、`h5_runtime_composition.py` | `test_outbox_writer.py`、`test_app_runtime_composition.py` | 已闭环 | 混合 Worker 部署待环境执行 |
| H5-SHARED-02 | exploration-only 报告状态与入口矛盾 | `workstation_tasks.py`、`TaskDetailView.vue` | `test_workstation_tasks_api.py`、`TaskDetailView.test.ts` | 已闭环 | 浏览器视觉检查待环境执行 |
| H5-SHARED-03 | 平台识别未使用正式 target_platform | `task_insight_repository.py` | `test_task_insight_repository.py` | 已闭环 | 历史空平台数据兼容待环境抽查 |
| H5-SHARED-04 | Inspection metadata 写读 schema 不一致 | `app_inspection_publisher.py`、`task_insight_repository.py` | `test_task_insight_repository.py`、`test_task_insight_service.py` | 已闭环 | 历史嵌套 metadata 待环境抽查 |
```

Replace each basename above with the repository-relative path plus the actual line reference after implementation. Use only `已闭环`, `部分闭环`, `未闭环` in the conclusion column; if its focused test or code inspection does not support `已闭环`, downgrade that row instead of preserving the planned value. Record command outputs and commit IDs. The environment section must separately list real PostgreSQL migration upgrade/downgrade, Redis cancel race, MinIO byte reread/checksum, Playwright branch restoration/browser-crash injection, and H5-only Completion Gate startup.

- [ ] **Step 7: Review scope and App non-regression**

Run: `git diff --name-only 5950b70..HEAD`

Expected: every changed production file maps to an H5 requirement or a default-preserving shared extension point; no App-only workflow, APK, device or visual-observation implementation is changed.

- [ ] **Step 8: Commit documentation and final audit**

```bash
git add docs/专项能力与高频回归规则.md docs/reviews/2026-08-12-h5-page-exploration-audit-remediation-review.md
git commit -m "[AI编程] doc: 完成H5页面探索整改复审；模块：H5全链路/回归规则；数据库迁移：复核0049；前端菜单路由：无新增、已核验详情页；测试风险：真实PG/Redis/MinIO/Playwright门禁按报告列示"
```

---

## Execution Notes

- 每个任务开始前只读取该任务列出的文件和它直接依赖的类型；发现既有测试命名不同，沿用同一测试文件并保持本计划的行为断言。
- 每个任务严格执行 red → green → focused regression → commit，不把多个任务积累到一个提交。
- 测试失败时先使用 `superpowers:systematic-debugging` 定位根因，不通过放宽断言、删除用例或增加无界重试获得绿色结果。
- 最终宣称完成前必须使用 `superpowers:verification-before-completion`，以当前工作树重新运行 Task 12 的命令并记录实际输出。
- 任一真实依赖不可用时，代码级结论仍可给出，但最终复审对应行必须标为“代码闭环、环境门禁未执行”，不得写成生产验收通过。
