"""H5 V2 Worker 完整质检与仅探索收口分流测试。"""

from types import SimpleNamespace

import fakeredis
import pytest

from services.api.app import worker as worker_module
from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.models import (
    ErrorCategory,
    ExecutionOutcome,
    RunResult,
    TerminationReason,
)
from services.api.app.agents.runtime.usage_meter import record_current_llm_usage
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.application.evidence_request_admission import (
    EvidenceRequestAdmissionService,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkType
from services.api.app.application.h5_evidence_handoff import (
    H5EvidenceHandoffSummary,
    H5EvidenceNotReadyError,
)
from services.api.app.application.h5_live_inspection_publish import (
    H5LiveInspectionTaskGate,
)
from services.api.app.application.h5_run_failure import H5RunFailureFacts
from services.api.app.application.supplemental_evidence_policy import (
    build_current_supplemental_evidence_policy,
)
from services.api.app.core.config import Settings
from services.api.app.domain.task import (
    H5InspectionNotificationMode,
    TaskExecutionMode,
    TaskExecutionStatus,
    TaskFinalizationState,
)
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import (
    AgentRunCancellationConflict,
    InMemoryAgentRunStateStore,
)


def test_h5_shared_writer_admits_requests_before_waiting() -> None:
    policy = build_current_supplemental_evidence_policy(enabled=True)

    writer = worker_module._h5_request_admitting_writer(policy)

    assert isinstance(writer._request_admission, EvidenceRequestAdmissionService)
    assert writer._request_admission._policy is policy
    assert writer._route_after_admission is False


async def _queued_record(mode: TaskExecutionMode):
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
    )
    record = await queue.enqueue(
        "H5 V2 任务",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={
            "workflow_version": "v2",
            "execution_mode": mode.value,
            "_task_target": {
                "platform": "h5",
                "target_url": "https://m.example.test/home",
            },
        },
    )
    return queue, record


class _Dispatcher:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    async def dispatch_h5_v2(self, *, record, settings, progress_recorder=None):
        del settings, progress_recorder
        self.events.append(("runtime", record.run_id))
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(step_count=3),
            trace_sink=InMemoryActionTraceSink(),
            final_text="H5 探索完成",
        )


class _FailedDispatcher:
    async def dispatch_h5_v2(self, *, record, settings, progress_recorder=None):
        del record, settings, progress_recorder
        return RunResult(
            termination_reason=TerminationReason.ERROR,
            working_memory=WorkingMemory(step_count=1),
            trace_sink=InMemoryActionTraceSink(),
            error_message="浏览器会话崩溃",
            error_category=ErrorCategory.INFRASTRUCTURE,
        )


class _MeteredDispatcher(_Dispatcher):
    async def dispatch_h5_v2(self, *, record, settings, progress_recorder=None):
        record_current_llm_usage(
            {
                "input_token_count": 120,
                "output_token_count": 30,
                "total_token_count": 150,
            }
        )
        return await super().dispatch_h5_v2(
            record=record,
            settings=settings,
            progress_recorder=progress_recorder,
        )


class _NeedsReviewDispatcher:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    async def dispatch_h5_v2(self, *, record, settings, progress_recorder=None):
        del settings, progress_recorder
        self.events.append(("runtime", record.run_id))
        return RunResult(
            termination_reason=TerminationReason.INCOMPLETE,
            working_memory=WorkingMemory(step_count=3),
            trace_sink=InMemoryActionTraceSink(),
            final_text="H5 游客范围探索完成，认证未完成",
            error_message="h5_auth_bootstrap:unsupported_login_challenge",
            execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
        )


class _ExplorationNeedsReviewDispatcher:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    async def dispatch_h5_v2(self, *, record, settings, progress_recorder=None):
        del settings, progress_recorder
        self.events.append(("runtime", record.run_id))
        return RunResult(
            termination_reason=TerminationReason.INCOMPLETE,
            working_memory=WorkingMemory(step_count=3),
            trace_sink=InMemoryActionTraceSink(),
            final_text="H5 探索尚未完整收敛",
            error_message="no_progress",
            execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
        )


class _MaxStepsDispatcher:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    async def dispatch_h5_v2(self, *, record, settings, progress_recorder=None):
        del settings, progress_recorder
        self.events.append(("runtime", record.run_id))
        return RunResult(
            termination_reason=TerminationReason.MAX_STEPS_REACHED,
            working_memory=WorkingMemory(step_count=40),
            trace_sink=InMemoryActionTraceSink(),
            final_text="H5 探索达到步数上限",
        )


class _Handoff:
    def __init__(
        self,
        events: list[object],
        *,
        fail: bool = False,
        excluded_active_session_rule_count: int = 0,
        insufficient_count: int = 0,
        failed_count: int = 0,
        evidence_ready: bool | None = None,
        total_count: int = 2,
        pass_count: int | None = None,
    ) -> None:
        self.events = events
        self.fail = fail
        self.excluded_active_session_rule_count = excluded_active_session_rule_count
        self.insufficient_count = insufficient_count
        self.failed_count = failed_count
        self.evidence_ready = evidence_ready
        self.total_count = total_count
        self.publication_count = total_count
        self.pass_count = pass_count
        self.notification_modes: list[H5InspectionNotificationMode] = []

    async def prepare(
        self,
        *,
        task_id,
        agent_run_id,
        execution_mode,
        notification_mode=None,
    ):
        self.notification_modes.append(notification_mode)
        self.events.append(("handoff", task_id, agent_run_id, execution_mode))
        if self.fail:
            raise H5EvidenceNotReadyError("缺少 DOM")
        return H5EvidenceHandoffSummary(
            evidence_ready=(
                not (self.insufficient_count or self.failed_count)
                if self.evidence_ready is None
                else self.evidence_ready
            ),
            judgeable_capture_count=2,
            page_types=("home", "video_detail"),
            published_work_item_count=(
                0 if execution_mode is TaskExecutionMode.EXPLORATION_ONLY else 4
            ),
            excluded_active_session_rule_count=(self.excluded_active_session_rule_count),
            total_count=self.total_count,
            pass_count=(
                self.total_count - self.insufficient_count - self.failed_count
                if self.pass_count is None
                else self.pass_count
            ),
            insufficient_count=self.insufficient_count,
            failed_count=self.failed_count,
            publication_count=self.publication_count,
        )


class _GateReader:
    def __init__(self, *, notification_mode: str = "off") -> None:
        self.notification_mode = notification_mode
        self.calls: list[str] = []

    async def load_task_gate(self, *, task_id: str) -> H5LiveInspectionTaskGate:
        self.calls.append(task_id)
        return H5LiveInspectionTaskGate(
            target_platform="h5",
            task_config={
                "workflow_version": "v2",
                "execution_mode": "full_inspection",
                "h5_inspection_notification_mode": self.notification_mode,
            },
        )


class _PublicationRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def await_task_terminal(self, *, task_id: str, minimum_total_count: int = 0):
        self.calls.append((task_id, minimum_total_count))
        return SimpleNamespace(
            total_count=1,
            nonterminal_count=0,
            published_work_item_count=4,
            excluded_active_session_rule_count=0,
            needs_review_count=0,
            failed_count=0,
        )


def _patch_worker_closure(monkeypatch: pytest.MonkeyPatch, events: list[object]) -> None:
    monkeypatch.setattr(worker_module, "build_task_progress_recorder", lambda **kwargs: None)

    async def drain(record, *, settings, worker_instance_id):
        del record, settings, worker_instance_id
        events.append("drain")

    async def finalize_rules(record, writer):
        del record, writer
        events.append("finalize_rules")

    async def finalize_work(record, *, settings):
        del record, settings
        events.append("finalize_work")

    async def finish(**kwargs):
        events.append(
            (
                "finish",
                kwargs["task_finalization_service"],
                kwargs["inspection_work_items_enabled"],
                kwargs["legacy_report_enabled"],
            )
        )

    monkeypatch.setattr(worker_module, "_drain_snapshot_inspection_work", drain)
    monkeypatch.setattr(worker_module, "_finalize_rule_executions", finalize_rules)
    monkeypatch.setattr(worker_module, "_finalize_persisted_inspection_work", finalize_work)
    monkeypatch.setattr(worker_module, "_finish_terminal_run", finish)


@pytest.mark.asyncio
async def test_exploration_only_h5_finishes_without_formal_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅探索交接成功后必须绕过 Judge、正式结果和 Completion Gate。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.EXPLORATION_ONLY)
    finalization_service = object()

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_Dispatcher(events),
        h5_evidence_handoff=_Handoff(events),
        final_report_generator=SimpleNamespace(),
        task_finalization_service=finalization_service,
    )

    assert events[0][0] == "runtime"
    assert events[1][-1] is TaskExecutionMode.EXPLORATION_ONLY
    assert events[2] == ("finish", None, False, False)
    assert "drain" not in events
    assert "finalize_rules" not in events
    assert "finalize_work" not in events
    assert record.status is AgentRunQueueStatus.COMPLETED
    assert record.usage["h5_evidence_ready"] is True
    assert record.usage["h5_judgeable_capture_count"] == 2
    assert record.usage["h5_page_type_count"] == 2
    assert record.usage["h5_published_work_item_count"] == 0
    assert record.usage["h5_evidence_total_count"] == 2


@pytest.mark.asyncio
async def test_h5_v2_records_unified_llm_usage_on_main_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.EXPLORATION_ONLY)

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_MeteredDispatcher(events),
        h5_evidence_handoff=_Handoff(events),
    )

    assert record.usage["input_tokens"] == 120
    assert record.usage["output_tokens"] == 30
    assert record.usage["total_tokens"] == 150
    assert record.usage["ai_calls"] == 1


@pytest.mark.asyncio
async def test_full_inspection_h5_runs_formal_closure_after_evidence_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整质检必须先过证据门禁，再执行正式质检与报告收口。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    finalization_service = object()

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_Dispatcher(events),
        h5_evidence_handoff=_Handoff(events),
        task_finalization_service=finalization_service,
    )

    assert events == [
        ("runtime", record.run_id),
        (
            "handoff",
            record.task_id,
            record.run_id,
            TaskExecutionMode.FULL_INSPECTION,
        ),
        "drain",
        "finalize_rules",
        "finalize_work",
        ("finish", finalization_service, True, True),
    ]
    assert record.usage["h5_published_work_item_count"] == 4


@pytest.mark.asyncio
async def test_h5_max_steps_reaches_handoff_and_review_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """步数上限是复核型终止，不能跳过 H5 证据交接和 Task Finalization。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    finalization_service = object()

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_MaxStepsDispatcher(events),
        h5_evidence_handoff=_Handoff(events),
        task_finalization_service=finalization_service,
    )

    assert events == [
        ("runtime", record.run_id),
        (
            "handoff",
            record.task_id,
            record.run_id,
            TaskExecutionMode.FULL_INSPECTION,
        ),
        "drain",
        "finalize_rules",
        "finalize_work",
        ("finish", finalization_service, True, True),
    ]
    assert record.status is AgentRunQueueStatus.FAILED
    assert record.termination_reason == TerminationReason.MAX_STEPS_REACHED.value
    assert record.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value


@pytest.mark.asyncio
async def test_full_inspection_keeps_run_completed_when_active_session_rules_are_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session 关闭后的规则缺口应进入业务复核，不能误报技术运行失败。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    finalization_service = object()

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_Dispatcher(events),
        h5_evidence_handoff=_Handoff(
            events,
            excluded_active_session_rule_count=2,
        ),
        task_finalization_service=finalization_service,
    )

    assert record.status is AgentRunQueueStatus.COMPLETED
    assert record.termination_reason == TerminationReason.COMPLETED.value
    assert record.execution_outcome == "needs_review"
    assert record.usage["h5_excluded_active_session_rule_count"] == 2
    assert "2 条活动会话规则" in str(record.final_text)
    assert events[-1] == ("finish", finalization_service, True, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("insufficient_count", "failed_count", "outcome"),
    [(1, 0, "needs_review"), (0, 1, "failed")],
)
async def test_nonpass_inventory_blocks_formal_report_and_gate(
    monkeypatch: pytest.MonkeyPatch,
    insufficient_count: int,
    failed_count: int,
    outcome: str,
) -> None:
    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    finalization_service = object()
    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_Dispatcher(events),
        h5_evidence_handoff=_Handoff(
            events,
            insufficient_count=insufficient_count,
            failed_count=failed_count,
        ),
        task_finalization_service=finalization_service,
    )
    assert record.execution_outcome == outcome
    assert events[-1] == ("finish", finalization_service, True, True)


@pytest.mark.asyncio
async def test_empty_not_ready_inventory_blocks_formal_report_and_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空库存也必须按 evidence_ready 阻断正式封存与 Completion Gate。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    finalization_service = object()

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_Dispatcher(events),
        h5_evidence_handoff=_Handoff(
            events,
            evidence_ready=False,
            total_count=0,
            pass_count=0,
        ),
        task_finalization_service=finalization_service,
    )

    assert record.execution_outcome == "needs_review"
    assert events[-1] == ("finish", finalization_service, True, True)


@pytest.mark.asyncio
async def test_h5_evidence_gate_failure_prevents_formal_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证据门禁失败时不得继续发布、判定或生成报告。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_Dispatcher(events),
        h5_evidence_handoff=_Handoff(events, fail=True),
        task_finalization_service=object(),
    )

    assert events[0][0] == "runtime"
    assert events[1][0] == "handoff"
    assert events[2] == ("finish", None, False, False)
    assert "drain" not in events
    assert "finalize_rules" not in events
    assert "finalize_work" not in events
    assert record.status is AgentRunQueueStatus.FAILED
    assert record.error_message == "缺少 DOM"


class _InflightCountingService:
    def __init__(
        self,
        counts: list[int],
        *,
        consumer_run_id: str | None = None,
        backlog_counts: list[int] | None = None,
        waitable_counts: list[int] | None = None,
    ) -> None:
        self._counts = counts
        self._consumer_run_id = consumer_run_id
        self._backlog_counts = backlog_counts or []
        self._waitable_counts = waitable_counts
        self.calls = 0
        self.backlog_calls = 0
        self.backlog_consumer_run_ids: list[str] = []

    async def count_inflight_leased_work(self, *, task_id: str) -> int:
        del task_id
        self.calls += 1
        if self._counts:
            return self._counts.pop(0)
        return 0

    async def find_active_consumer_run(self, *, task_id: str) -> str | None:
        del task_id
        return self._consumer_run_id

    async def count_claimable_pending_work(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> int:
        del task_id
        self.backlog_calls += 1
        self.backlog_consumer_run_ids.append(consumer_agent_run_id)
        if self._backlog_counts:
            return self._backlog_counts.pop(0)
        return 0

    async def count_waitable_nonterminal_work(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> int:
        if self._waitable_counts is None:
            return await self.count_claimable_pending_work(
                task_id=task_id,
                consumer_agent_run_id=consumer_agent_run_id,
            )
        del task_id, consumer_agent_run_id
        if self._waitable_counts:
            return self._waitable_counts.pop(0)
        return 0


@pytest.mark.asyncio
async def test_finalize_grace_returns_immediately_without_inflight_work() -> None:
    service = _InflightCountingService([0])

    waited = await worker_module._wait_for_inflight_inspection_work(
        service,
        task_id="task-1",
        grace_seconds=1.0,
        poll_seconds=0.01,
    )

    assert waited == 0.0
    assert service.calls == 1


def test_finalize_grace_covers_semantic_batch_timeout() -> None:
    """合法语义批次尚在超时预算内时，收口不得先把工作项判死。"""
    from services.api.app.core.config import QualityOptimizationSettings

    semantic_timeout = QualityOptimizationSettings().semantic_batch_timeout_seconds

    assert worker_module.INSPECTION_FINALIZE_GRACE_SECONDS > semantic_timeout


@pytest.mark.asyncio
async def test_finalize_grace_waits_for_inflight_judgment_to_land() -> None:
    """收口门禁不得立刻打断刚被领取的在途判定（收口边界竞态）。"""
    service = _InflightCountingService([2, 1, 0])

    waited = await worker_module._wait_for_inflight_inspection_work(
        service,
        task_id="task-1",
        grace_seconds=1.0,
        poll_seconds=0.01,
    )

    assert waited > 0.0
    assert service.calls == 3


@pytest.mark.asyncio
async def test_finalize_grace_is_bounded_when_judgment_hangs() -> None:
    service = _InflightCountingService([1] * 1000)

    waited = await worker_module._wait_for_inflight_inspection_work(
        service,
        task_id="task-1",
        grace_seconds=0.05,
        poll_seconds=0.01,
    )

    assert waited >= 0.05
    assert service.calls <= 6


@pytest.mark.asyncio
async def test_finalize_grace_waits_for_claimable_backlog_being_drained() -> None:
    """有活跃消费 Run 时，可领取积压持续下降就不得抢跑强制判死。"""
    service = _InflightCountingService(
        [0] * 6,
        consumer_run_id="consumer-1",
        backlog_counts=[72, 48, 24, 12, 3, 0],
    )

    waited = await worker_module._wait_for_inflight_inspection_work(
        service,
        task_id="task-1",
        grace_seconds=0.05,
        poll_seconds=0.01,
    )

    assert waited > 0.0
    assert service.backlog_calls == 6
    assert service.backlog_consumer_run_ids == ["consumer-1"] * 6


@pytest.mark.asyncio
async def test_finalize_grace_waits_for_retry_wait_not_yet_claimable() -> None:
    """未来重试窗口仍可推进，不能因当前 claimable=0 抢跑最终化。"""
    service = _InflightCountingService(
        [0, 0],
        consumer_run_id="consumer-1",
        backlog_counts=[0, 0],
        waitable_counts=[1, 0],
    )

    waited = await worker_module._wait_for_inflight_inspection_work(
        service,
        task_id="task-1",
        grace_seconds=1.0,
        poll_seconds=0.01,
    )

    assert waited > 0.0


@pytest.mark.asyncio
async def test_finalize_grace_ignores_backlog_without_active_consumer() -> None:
    """没有消费 Run 时积压无人能领取，不应为其空等。"""
    service = _InflightCountingService(
        [0],
        consumer_run_id=None,
        backlog_counts=[72],
    )

    waited = await worker_module._wait_for_inflight_inspection_work(
        service,
        task_id="task-1",
        grace_seconds=0.05,
        poll_seconds=0.01,
    )

    assert waited == 0.0
    assert service.backlog_calls == 0


@pytest.mark.asyncio
async def test_finalize_grace_is_bounded_when_backlog_stalls() -> None:
    """积压数量停滞超过宽限窗时按原兜底放行强制判死。"""
    service = _InflightCountingService(
        [0] * 1000,
        consumer_run_id="consumer-1",
        backlog_counts=[72] * 1000,
    )

    waited = await worker_module._wait_for_inflight_inspection_work(
        service,
        task_id="task-1",
        grace_seconds=0.05,
        poll_seconds=0.01,
    )

    assert waited >= 0.05
    assert service.backlog_calls <= 6


@pytest.mark.asyncio
async def test_finalize_grace_has_absolute_ceiling() -> None:
    """即使积压持续变化，总等待也不得超过绝对上限。"""
    service = _InflightCountingService(
        [0] * 1000,
        consumer_run_id="consumer-1",
        backlog_counts=list(range(1000, 0, -1)),
    )

    waited = await worker_module._wait_for_inflight_inspection_work(
        service,
        task_id="task-1",
        grace_seconds=0.05,
        poll_seconds=0.01,
        max_wait_seconds=0.03,
    )

    assert waited <= 0.05
    assert service.backlog_calls <= 5


@pytest.mark.asyncio
async def test_h5_finalize_grace_can_wait_past_common_ceiling_while_progress_continues() -> None:
    """H5 批量规则持续落库时，可使用更长但有限的等待窗口。"""
    service = _InflightCountingService(
        [0] * 8,
        consumer_run_id="consumer-1",
        backlog_counts=[7, 6, 5, 4, 3, 2, 1, 0],
    )

    waited = await worker_module._wait_for_inflight_inspection_work(
        service,
        task_id="task-1",
        grace_seconds=0.02,
        poll_seconds=0.01,
        max_wait_seconds=0.1,
    )

    assert waited >= 0.07
    assert service.backlog_calls == 8


def test_h5_finalize_grace_has_finite_absolute_ceiling() -> None:
    assert worker_module.H5_INSPECTION_FINALIZE_MAX_WAIT_SECONDS > (
        worker_module.INSPECTION_FINALIZE_MAX_WAIT_SECONDS
    )
    assert worker_module.H5_INSPECTION_FINALIZE_MAX_WAIT_SECONDS <= 5400


@pytest.mark.asyncio
async def test_h5_runtime_error_preserves_root_failure_when_salvage_handoff_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime 已失败时兜底交接自身出错不得覆盖原始根因。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_FailedDispatcher(),
        h5_evidence_handoff=_Handoff(events, fail=True),
        task_finalization_service=object(),
    )

    assert events[0][0] == "handoff"
    assert events[1] == ("finish", None, False, False)
    assert "drain" not in events
    assert "finalize_rules" not in events
    assert "finalize_work" not in events
    assert record.status is AgentRunQueueStatus.FAILED
    assert record.error_message == "浏览器会话崩溃"
    assert record.error_category == ErrorCategory.INFRASTRUCTURE.value


@pytest.mark.asyncio
async def test_h5_runtime_error_still_hands_off_evidence_and_judges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探索单点失败不得连坐已留存证据：交接与判定收口照常执行。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)

    handoff = _Handoff(events)
    gate_reader = _GateReader(notification_mode="enforced")
    publication_repository = _PublicationRepository()
    finalization_service = object()

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_FailedDispatcher(),
        h5_evidence_handoff=handoff,
        task_finalization_service=finalization_service,
        h5_task_gate_reader=gate_reader,
        h5_publication_repository=publication_repository,  # type: ignore[arg-type]
    )

    assert events[0][0] == "handoff"
    assert events[0][-1] is TaskExecutionMode.FULL_INSPECTION
    assert "drain" in events
    assert "finalize_rules" in events
    assert "finalize_work" in events
    # 打捞完整的失败 Run 按正式门禁收口（活会话规则发布语义统一后的新合同）。
    assert events[-1] == ("finish", finalization_service, True, True)
    assert record.status is AgentRunQueueStatus.FAILED
    assert record.termination_reason == TerminationReason.ERROR.value
    assert record.error_message == "浏览器会话崩溃"
    assert record.usage["h5_published_work_item_count"] == 4
    assert record.usage["h5_evidence_ready"] is True
    assert handoff.notification_modes == [H5InspectionNotificationMode.ENFORCED]
    assert gate_reader.calls == [record.task_id]
    assert publication_repository.calls == [(record.task_id, handoff.publication_count)]


@pytest.mark.asyncio
async def test_h5_locked_notification_mode_mismatch_fails_closed() -> None:
    _queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    record.task_config["h5_inspection_notification_mode"] = "shadow"

    with pytest.raises(RuntimeError, match="通知模式.*不一致"):
        await worker_module._resolve_locked_h5_task_modes(
            record,
            gate_reader=_GateReader(notification_mode="enforced"),
        )


@pytest.mark.asyncio
async def test_h5_runtime_error_salvage_respects_exploration_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅留证模式的失败 Run 兜底只做库存交接，不得触发判定收口。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.EXPLORATION_ONLY)

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_FailedDispatcher(),
        h5_evidence_handoff=_Handoff(events),
        task_finalization_service=object(),
    )

    assert events[0][0] == "handoff"
    assert events[0][-1] is TaskExecutionMode.EXPLORATION_ONLY
    assert "drain" not in events
    assert "finalize_rules" not in events
    assert "finalize_work" not in events
    # 仅留证模式没有质检工作项，失败兜底不开正式门禁与工作项收口。
    assert events[-1] == ("finish", None, False, False)
    assert record.status is AgentRunQueueStatus.FAILED
    assert record.error_message == "浏览器会话崩溃"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_phase", ["before_runtime", "runtime_completed"])
async def test_h5_cancel_wins_over_completed_runtime_before_handoff(
    monkeypatch: pytest.MonkeyPatch,
    cancel_phase: str,
) -> None:
    """已取消不启动 Runtime；完成时赢得的取消仍不得进入证据交接。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)

    async def cancel_work(*, task_id: str) -> int:
        assert task_id == "11111111-1111-1111-1111-111111111111"
        return 2

    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", cancel_work)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    if cancel_phase == "before_runtime":
        assert await queue.request_cancel(record.run_id) is True

    class Dispatcher(_Dispatcher):
        async def dispatch_h5_v2(self, **kwargs):
            result = await super().dispatch_h5_v2(**kwargs)
            assert await queue.request_cancel(record.run_id) is True
            return result

    async def skip_trace(_record):
        return None

    monkeypatch.setattr(worker_module, "_attach_h5_v2_exploration_action_trace", skip_trace)

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=Dispatcher(events),
        h5_evidence_handoff=_Handoff(events),
        task_finalization_service=object(),
    )

    assert record.status is AgentRunQueueStatus.CANCELED
    assert record.termination_reason == TerminationReason.CANCELED.value
    assert record.execution_outcome == "needs_review"
    expected = [] if cancel_phase == "before_runtime" else [("runtime", record.run_id)]
    assert events == [*expected, ("finish", None, False, False)]


@pytest.mark.asyncio
async def test_h5_cancel_cleanup_settles_requests_work_and_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class CancellationSettlementRepository:
        async def settle_canceled(self, *, task_id: str, reason: str):
            calls.append(("settlement", (task_id, reason)))
            return SimpleNamespace(total_count=3)

    class ActionRepository:
        async def fail_nonterminal_for_task(self, **kwargs):
            calls.append(("action", kwargs))
            return 2

    class ExplorerWorkRepository:
        async def cancel_nonterminal(self, **kwargs):
            calls.append(("legacy_work", kwargs))
            return 0

    class InspectionWorkRepository:
        async def cancel_nonterminal(self, **kwargs):
            calls.append(("legacy_inspection", kwargs))
            return 0

    monkeypatch.setattr(
        worker_module,
        "PostgresH5CancellationSettlementRepository",
        CancellationSettlementRepository,
    )
    monkeypatch.setattr(
        worker_module,
        "PostgresActionExecutionRepository",
        ActionRepository,
    )
    monkeypatch.setattr(
        worker_module,
        "PostgresExplorerWorkRepository",
        ExplorerWorkRepository,
    )
    monkeypatch.setattr(
        worker_module,
        "PostgresInspectionWorkItemRepository",
        InspectionWorkRepository,
    )

    count = await worker_module._cancel_h5_nonterminal_work(task_id="task-1")

    assert count == 5
    assert calls == [
        ("settlement", ("task-1", "H5_TASK_CANCELED")),
        (
            "legacy_work",
            {
                "task_id": "task-1",
                "reason": "H5 Worker 在收口前确认统一 Run 取消",
                "work_types": (
                    ExplorerWorkType.EXPLORATION,
                    ExplorerWorkType.PROBE,
                ),
                "h5_only": True,
            },
        ),
        (
            "action",
            {
                "task_id": "task-1",
                "error_code": "WORKFLOW_CANCELED",
                "error_message": "H5 Worker 在收口前确认统一 Run 取消",
                "h5_only": True,
            },
        ),
        (
            "legacy_inspection",
            {
                "task_id": "task-1",
                "reason": "H5 Worker 在收口前确认统一 Run 取消",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_auth_review_result_skips_handoff_judge_and_sealed_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_NeedsReviewDispatcher(events),
        h5_evidence_handoff=_Handoff(events),
        task_finalization_service=object(),
    )

    assert events == [
        ("runtime", record.run_id),
        ("finish", None, False, False),
    ]
    assert record.termination_reason == TerminationReason.INCOMPLETE.value
    assert record.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value
    assert "handoff" not in {event[0] for event in events if isinstance(event, tuple)}


@pytest.mark.asyncio
async def test_h5_durable_cancel_wins_when_redis_signal_is_missing_before_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis 信号丢失时，已持久化取消仍必须阻止 evidence handoff。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    state_store = InMemoryAgentRunStateStore()

    async def cancel_work(*, task_id: str) -> int:
        assert task_id == "11111111-1111-1111-1111-111111111111"
        return 2

    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", cancel_work)

    class CancelAfterRuntimeDispatcher(_Dispatcher):
        async def dispatch_h5_v2(self, **kwargs):
            result = await super().dispatch_h5_v2(**kwargs)
            await state_store.record_cancel_requested(record.run_id)
            return result

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=CancelAfterRuntimeDispatcher(events),
        h5_evidence_handoff=_Handoff(events),
        task_finalization_service=object(),
    )

    assert await queue.is_cancel_requested(record.run_id) is False
    assert record.status is AgentRunQueueStatus.CANCELED
    assert events == [("runtime", record.run_id), ("finish", None, False, False)]


@pytest.mark.asyncio
async def test_non_auth_review_result_still_runs_evidence_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通探索复核不得被认证专属的提前收口逻辑误伤。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_ExplorationNeedsReviewDispatcher(events),
        h5_evidence_handoff=_Handoff(events),
        task_finalization_service=object(),
    )

    assert events[0][0] == "runtime"
    assert events[1][0] == "handoff"
    assert "drain" in events
    assert "finalize_rules" in events
    assert "finalize_work" in events


@pytest.mark.asyncio
async def test_h5_projects_durable_cancel_conflict_without_sealing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最后 Redis 复查后持久化取消赢得竞争时，H5 不得继续 Completion Gate。"""

    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    finalization_service = object()
    finish_calls: list[object] = []
    canceled_work: list[str] = []

    async def finish(**kwargs):
        finish_calls.append(kwargs["task_finalization_service"])
        if len(finish_calls) == 1:
            raise AgentRunCancellationConflict("cancel wins")

    async def cancel_work(*, task_id: str) -> int:
        canceled_work.append(task_id)
        return 2

    monkeypatch.setattr(worker_module, "_finish_terminal_run", finish)
    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", cancel_work)

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=_Dispatcher(events),
        h5_evidence_handoff=_Handoff(events),
        task_finalization_service=finalization_service,
    )

    assert record.status is AgentRunQueueStatus.CANCELED
    assert record.termination_reason == TerminationReason.CANCELED.value
    assert record.execution_outcome == "needs_review"
    assert canceled_work == [record.task_id]
    assert finish_calls == [finalization_service, None]


@pytest.mark.asyncio
async def test_h5_cancel_during_handoff_blocks_formal_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    state_store = InMemoryAgentRunStateStore()

    async def cancel_work(*, task_id: str) -> int:
        assert task_id == record.task_id
        events.append("cancel_work")
        return 2

    class CancelingHandoff(_Handoff):
        async def prepare(self, **kwargs):
            summary = await super().prepare(**kwargs)
            await state_store.record_cancel_requested(record.run_id)
            return summary

    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", cancel_work)
    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=_Dispatcher(events),
        h5_evidence_handoff=CancelingHandoff(events),
        task_finalization_service=object(),
    )

    assert record.status is AgentRunQueueStatus.CANCELED
    assert events == [
        ("runtime", record.run_id),
        (
            "handoff",
            record.task_id,
            record.run_id,
            TaskExecutionMode.FULL_INSPECTION,
        ),
        "cancel_work",
        ("finish", None, False, False),
    ]


@pytest.mark.parametrize(
    ("canceling_event", "target_name", "expected_prefix"),
    [
        ("drain", "_drain_snapshot_inspection_work", ("drain",)),
        (
            "finalize_rules",
            "_finalize_rule_executions",
            ("drain", "finalize_rules"),
        ),
        (
            "finalize_work",
            "_finalize_persisted_inspection_work",
            ("drain", "finalize_rules", "finalize_work"),
        ),
    ],
)
@pytest.mark.asyncio
async def test_h5_cancel_between_formal_closure_steps_blocks_later_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    canceling_event: str,
    target_name: str,
    expected_prefix: tuple[str, ...],
) -> None:
    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    state_store = InMemoryAgentRunStateStore()

    async def cancel_work(*, task_id: str) -> int:
        assert task_id == record.task_id
        events.append("cancel_work")
        return 2

    async def canceling_step(*args, **kwargs) -> None:
        del args, kwargs
        events.append(canceling_event)
        await state_store.record_cancel_requested(record.run_id)

    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", cancel_work)
    monkeypatch.setattr(worker_module, target_name, canceling_step)
    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=_Dispatcher(events),
        h5_evidence_handoff=_Handoff(events),
        task_finalization_service=object(),
    )

    assert record.status is AgentRunQueueStatus.CANCELED
    assert events == [
        ("runtime", record.run_id),
        (
            "handoff",
            record.task_id,
            record.run_id,
            TaskExecutionMode.FULL_INSPECTION,
        ),
        *expected_prefix,
        "cancel_work",
        ("finish", None, False, False),
    ]


def test_replace_billable_usage_payload_preserves_non_billable_fields() -> None:
    payload = worker_module._replace_billable_usage_payload(
        {
            "ai_calls": 99,
            "input_tokens": 900,
            "output_tokens": 99,
            "total_tokens": 999,
            "total_cost": 9.9,
            "usage_event_count": 99,
            "h5_evidence_ready": True,
            "h5_page_type_count": 3,
            "worker_rss_after_bytes": 123456,
        },
        {
            "ai_calls": 2,
            "input_tokens": 400,
            "output_tokens": 60,
            "total_tokens": 460,
            "total_cost": 0.000458,
            "usage_event_count": 2,
        },
    )

    assert payload["ai_calls"] == 2
    assert payload["input_tokens"] == 400
    assert payload["output_tokens"] == 60
    assert payload["total_tokens"] == 460
    assert payload["total_cost"] == 0.000458
    assert payload["usage_event_count"] == 2
    assert payload["h5_evidence_ready"] is True
    assert payload["h5_page_type_count"] == 3
    assert payload["worker_rss_after_bytes"] == 123456


class _FailedTaskFinalizationService:
    def __init__(self, run_id: str, events: list[object]) -> None:
        self.events = events
        self.command = None
        self.snapshot = SimpleNamespace(
            agent_run_id=run_id, state=TaskFinalizationState.OPEN,
            row_version=0, finalization_epoch=0, execution_status=None,
        )

    async def get(self, *, task_id):
        del task_id
        return self.snapshot

    async def begin_exploration_draining(self, command):
        self.events.append("exploration_draining")
        assert command.agent_run_id == self.snapshot.agent_run_id
        self.snapshot.state = TaskFinalizationState.EXPLORATION_DRAINING
        self.snapshot.row_version += 1
        return self.snapshot

    async def begin_session_draining(self, command):
        self.events.append("session_draining")
        assert command.expected_row_version == self.snapshot.row_version
        self.snapshot.state = TaskFinalizationState.SESSION_DRAINING
        self.snapshot.row_version += 1
        return self.snapshot

    async def terminate(self, command):
        self.events.append("terminate_failed")
        self.command = command
        self.snapshot.state = TaskFinalizationState.SEALED
        self.snapshot.execution_status = command.execution_status
        return self.snapshot


def _patch_failure_facts(monkeypatch, facts):
    class Reader:
        async def fetch(self, *, task_id, agent_run_id):
            assert task_id and agent_run_id
            return facts

        async def assert_drained(self, *, task_id, agent_run_id):
            assert task_id and agent_run_id

    monkeypatch.setattr(worker_module, "PostgresH5RunFailureFactsRepository", Reader)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_failed", [False, True])
async def test_h5_zero_results_technical_failure_terminates_after_drain_without_report(
    monkeypatch: pytest.MonkeyPatch, runtime_failed: bool,
) -> None:
    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    service = _FailedTaskFinalizationService(record.run_id, events)
    _patch_failure_facts(monkeypatch, H5RunFailureFacts(0, 0, "COMMAND_HOST_BOUNDARY_VIOLATION"))

    await worker_module._process_h5_v2_one(
        queue, record, settings=Settings(), state_store=InMemoryAgentRunStateStore(),
        dispatcher=_FailedDispatcher() if runtime_failed else _Dispatcher(events),
        h5_evidence_handoff=_Handoff(events, insufficient_count=1),
        task_finalization_service=service,
    )

    assert events.index("drain") < events.index("terminate_failed")
    assert not any(isinstance(event, tuple) and event[0] == "finish" for event in events)
    assert service.command.execution_status is TaskExecutionStatus.FAILED
    assert service.command.reason_code == "COMMAND_HOST_BOUNDARY_VIOLATION"
    assert record.status is AgentRunQueueStatus.FAILED
    assert record.execution_outcome == "failed"
    assert record.termination_reason == TerminationReason.ERROR.value
    assert record.final_report is None
    assert record.error_message == (
        "浏览器会话崩溃" if runtime_failed else "COMMAND_HOST_BOUNDARY_VIOLATION"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_pages,business_results", [(1, 0), (0, 1)])
async def test_h5_partial_results_keep_existing_review_path(
    monkeypatch: pytest.MonkeyPatch, valid_pages: int, business_results: int,
) -> None:
    events: list[object] = []
    _patch_worker_closure(monkeypatch, events)
    queue, record = await _queued_record(TaskExecutionMode.FULL_INSPECTION)
    service = _FailedTaskFinalizationService(record.run_id, events)
    _patch_failure_facts(monkeypatch, H5RunFailureFacts(
        valid_pages, business_results, "COMMAND_HOST_BOUNDARY_VIOLATION",
    ))

    await worker_module._process_h5_v2_one(
        queue, record, settings=Settings(), state_store=InMemoryAgentRunStateStore(),
        dispatcher=_FailedDispatcher(), h5_evidence_handoff=_Handoff(events),
        task_finalization_service=service,
    )

    assert service.command is None
    assert events[-1] == ("finish", service, True, True)
    assert record.final_report is None
