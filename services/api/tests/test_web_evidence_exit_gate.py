"""E16/E17/E18：Web 离场门禁灰度、离场动作范围、预留生命周期与证据不足回归。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.capture_evidence_readiness import (
    CaptureEvidenceReadinessService,
)
from services.api.app.application.evidence_exit_gate import (
    EvidenceExitGateBlockedError,
    build_evidence_exit_blocked_outcome,
    is_entry_sentinel_capture,
    is_trusted_initial_surface_action,
    requires_evidence_exit_gate,
)
from services.api.app.application.evidence_manifest_writer import (
    ManifestAdjudicationOutcome,
    ManifestAdjudicationStatus,
)
from services.api.app.application.web_evidence_exit_gate import (
    EvidenceExitReservationSweeper,
    NoopEvidenceExitGate,
    ShadowEvidenceExitGate,
    build_web_evidence_exit_gate,
    evaluate_web_coverage_exit_gate,
)
from services.api.app.core.config import WebEvidenceExitGateSettings
from services.api.app.domain.evidence import EvidenceQualityStatus
from services.api.app.domain.exploration import EvidenceExitReservationStatus
from services.api.app.domain.exploration.evidence_exit_gate import (
    EvidenceExitGateSnapshot,
    build_evidence_exit_gate_decision,
)
from services.api.app.domain.exploration.evidence_exit_requirements import (
    build_evidence_exit_requirement_set,
)
from services.api.app.domain.exploration.web_coverage_snapshot import (
    WebCoverageSnapshot,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "surface-session-1"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"


def _intent(
    *,
    operation: InteractionOperation,
    source_capture_id: str = CAPTURE_ID,
    surface_kind: SurfaceKind = SurfaceKind.WEB,
    parameters: dict[str, Any] | None = None,
) -> ActionIntent:
    return ActionIntent(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        intent_id="intent-1",
        idempotency_key="intent-1",
        surface_kind=surface_kind,
        source_capture_id=source_capture_id,
        action_type=operation,
        semantic_target="目标",
        purpose="test",
        capture_profile=CaptureProfile(
            profile_id="p-v1",
            before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
            after=CaptureDirective(role="primary", artifact_types=("screenshot",)),
        ),
        deadline=NOW + timedelta(minutes=5),
        parameters=parameters or {},
        risk_level=InteractionRiskLevel.LOW,
    )


# ---------- E17：离场动作范围 ----------


@pytest.mark.parametrize(
    "operation",
    [
        InteractionOperation.NAVIGATE,
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.BACK,
        InteractionOperation.REFRESH,
    ],
)
def test_leave_capable_actions_require_gate(operation: InteractionOperation) -> None:
    assert requires_evidence_exit_gate(_intent(operation=operation)) is True


@pytest.mark.parametrize(
    "operation",
    [
        InteractionOperation.OBSERVE,
        InteractionOperation.TIMED_CAPTURE,
        InteractionOperation.SCROLL,
        InteractionOperation.HOVER,
        InteractionOperation.INPUT,
    ],
)
def test_observation_actions_are_never_gated(operation: InteractionOperation) -> None:
    """E17 验收：观察类动作不受门禁影响。"""
    assert requires_evidence_exit_gate(_intent(operation=operation)) is False


@pytest.mark.parametrize(
    "sentinel",
    ["h5-entry:session-1", "agent-entry:session-1", "legacy-agent-entry"],
)
def test_entry_sentinels_are_recognized(sentinel: str) -> None:
    assert is_entry_sentinel_capture(sentinel) is True


def test_real_capture_id_is_not_sentinel() -> None:
    assert is_entry_sentinel_capture(CAPTURE_ID) is False


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
    """E17：Agent Tool 入口哨兵不指向真实 Capture，门禁无事实可算，必须放行。"""
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


def test_leave_action_from_sentinel_parent_is_not_gated() -> None:
    """哨兵父级没有离场事实，任何离场动作都不应被强行评估。"""
    intent = _intent(
        operation=InteractionOperation.CLICK,
        source_capture_id="agent-entry:session-1",
    )

    assert requires_evidence_exit_gate(intent) is False


def test_leave_action_from_real_capture_is_gated() -> None:
    assert requires_evidence_exit_gate(
        _intent(operation=InteractionOperation.CLICK)
    ) is True


# ---------- E16：灰度装配 ----------


def _snapshot(*, allow: bool) -> EvidenceExitGateSnapshot:
    return EvidenceExitGateSnapshot(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        source_capture_id=CAPTURE_ID,
        requirement_set_id="requirement-set-1" if allow else None,
        baseline_pass_manifest_count=1 if allow else 0,
        baseline_manifest_total_count=1,
        nonterminal_active_session_requirement_count=0,
        nonterminal_inline_evidence_request_count=0,
        evidence_commit_in_progress_count=0,
        evaluated_at=NOW,
        surface_kind="web",
    )


def _decision(*, allow: bool):
    return build_evidence_exit_gate_decision(
        action_intent_id="intent-1",
        snapshot=_snapshot(allow=allow),
    )


def _blocked_outcome(decision):
    return build_evidence_exit_blocked_outcome(decision=decision)


class FakeInnerGate:
    def __init__(self, decision: Any) -> None:
        self.decision = decision
        self.finalized: list[Any] = []
        self.expired_calls: list[int] = []

    async def reserve(self, *, intent, permit):
        del intent, permit
        return self.decision

    async def reserve_action_recovery(self, **kwargs: Any):
        del kwargs
        return self.decision

    async def finalize_stale_action_reservations(self, **kwargs: Any) -> int:
        del kwargs
        return 0

    async def finalize(self, *, decision, status) -> None:
        self.finalized.append((decision, status))

    async def expire_stale(self, *, limit: int = 100) -> int:
        self.expired_calls.append(limit)
        return 3


def test_off_mode_builds_noop_gate() -> None:
    gate = build_web_evidence_exit_gate(
        settings=WebEvidenceExitGateSettings(mode="off")
    )

    assert isinstance(gate, NoopEvidenceExitGate)


def test_shadow_mode_wraps_persistent_gate() -> None:
    inner = FakeInnerGate(_decision(allow=False))
    gate = build_web_evidence_exit_gate(
        settings=WebEvidenceExitGateSettings(mode="shadow"),
        postgres_gate_factory=lambda _settings: inner,
    )

    assert isinstance(gate, ShadowEvidenceExitGate)


def test_on_mode_uses_persistent_gate_directly() -> None:
    inner = FakeInnerGate(_decision(allow=True))
    gate = build_web_evidence_exit_gate(
        settings=WebEvidenceExitGateSettings(mode="on"),
        postgres_gate_factory=lambda _settings: inner,
    )

    assert gate is inner


def test_reservation_seconds_flow_into_factory() -> None:
    captured: list[WebEvidenceExitGateSettings] = []
    build_web_evidence_exit_gate(
        settings=WebEvidenceExitGateSettings(mode="on", reservation_seconds=300),
        postgres_gate_factory=lambda settings: captured.append(settings) or object(),
    )

    assert captured[0].reservation_seconds == 300


# ---------- E16：影子与真实拒绝 ----------


async def test_shadow_mode_records_block_but_allows_leave() -> None:
    """影子期吞掉阻断异常，动作照常执行，拒绝事实仍被记录供对账。"""
    blocked = _decision(allow=False)

    class BlockingGate(FakeInnerGate):
        async def reserve(self, *, intent, permit):
            del intent, permit
            raise EvidenceExitGateBlockedError(blocked, _blocked_outcome(blocked))

    gate = ShadowEvidenceExitGate(BlockingGate(blocked))

    decision = await gate.reserve(intent=None, permit=None)

    assert decision is None
    assert len(gate.blocked_decisions) == 1
    assert gate.blocked_decisions[0].allow_leave is False
    assert gate.blocked_decisions[0].block_reasons


async def test_on_mode_propagates_block_error() -> None:
    """E16 验收：on 模式下证据不全的离场动作被真实拒绝（异常传到编排层）。"""
    blocked = _decision(allow=False)

    class BlockingGate(FakeInnerGate):
        async def reserve(self, *, intent, permit):
            del intent, permit
            raise EvidenceExitGateBlockedError(blocked, _blocked_outcome(blocked))

    gate = build_web_evidence_exit_gate(
        settings=WebEvidenceExitGateSettings(mode="on"),
        postgres_gate_factory=lambda _settings: BlockingGate(blocked),
    )

    with pytest.raises(EvidenceExitGateBlockedError):
        await gate.reserve(intent=None, permit=None)


async def test_shadow_mode_passes_through_allowed_decision() -> None:
    inner = FakeInnerGate(_decision(allow=True))
    gate = ShadowEvidenceExitGate(inner)

    decision = await gate.reserve(intent=None, permit=None)

    assert decision is not None
    assert decision.allow_leave is True
    assert gate.blocked_decisions == []


async def test_on_mode_keeps_block_decision_intact() -> None:
    """E16 验收：证据不全时离场动作被真实拒绝。"""
    inner = FakeInnerGate(_decision(allow=False))

    decision = await inner.reserve(intent=None, permit=None)

    assert decision.allow_leave is False
    assert decision.block_reasons


async def test_noop_gate_never_blocks() -> None:
    gate = NoopEvidenceExitGate()

    assert await gate.reserve(intent=None, permit=None) is None
    assert await gate.expire_stale(limit=10) == 0


# ---------- E17：预留生命周期 ----------


async def test_sweeper_invokes_expire_stale_with_limit() -> None:
    inner = FakeInnerGate(_decision(allow=True))
    sweeper = EvidenceExitReservationSweeper(gate=inner, interval_seconds=1, limit=25)

    expired = await sweeper.sweep_once()

    assert expired == 3
    assert inner.expired_calls == [25]


async def test_sweeper_swallows_failure() -> None:
    class FailingGate(FakeInnerGate):
        async def expire_stale(self, *, limit: int = 100) -> int:
            raise RuntimeError("database unavailable")

    sweeper = EvidenceExitReservationSweeper(
        gate=FailingGate(_decision(allow=True)), interval_seconds=1
    )

    assert await sweeper.sweep_once() == 0


async def test_sweeper_start_and_stop_are_idempotent() -> None:
    inner = FakeInnerGate(_decision(allow=True))
    sweeper = EvidenceExitReservationSweeper(gate=inner, interval_seconds=0.01)

    sweeper.start()
    sweeper.start()
    await asyncio.sleep(0.05)
    await sweeper.stop()
    await sweeper.stop()

    assert inner.expired_calls


def test_sweeper_rejects_invalid_config() -> None:
    inner = FakeInnerGate(_decision(allow=True))

    with pytest.raises(ValueError, match="interval_seconds"):
        EvidenceExitReservationSweeper(gate=inner, interval_seconds=0)
    with pytest.raises(ValueError, match="limit"):
        EvidenceExitReservationSweeper(gate=inner, interval_seconds=1, limit=0)


# ---------- E16：证据就绪收口（门禁放行前置） ----------


class FakePublisher:
    def __init__(self, revision: int = 1) -> None:
        self.revision = revision
        self.calls: list[dict[str, Any]] = []

    async def publish_for_capture(self, **kwargs: Any):
        self.calls.append(kwargs)
        requirement_set = build_evidence_exit_requirement_set(
            revision=self.revision,
            task_id=kwargs["task_id"],
            session_id=kwargs["session_id"],
            capture_id=kwargs["capture_id"],
            matcher_snapshot_hash="matcher-1",
            requirements=(),
            created_at=NOW,
        )

        class _Persisted:
            def __init__(self, value) -> None:
                self.requirement_set = value
                self.created = True

        return _Persisted(requirement_set)


class FakeAdjudicator:
    def __init__(self, status: str = "pass") -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    async def adjudicate_for_capture(self, **kwargs: Any):
        self.calls.append(kwargs)

        class _Manifest:
            quality_status = EvidenceQualityStatus(self.status)

        return ManifestAdjudicationOutcome(
            status=ManifestAdjudicationStatus.ADJUDICATED,
            manifest=_Manifest(),  # type: ignore[arg-type]
            created=True,
        )


async def test_readiness_publishes_requirements_then_adjudicates() -> None:
    publisher = FakePublisher()
    adjudicator = FakeAdjudicator()
    service = CaptureEvidenceReadinessService(
        requirement_publisher=publisher,
        manifest_adjudicator=adjudicator,
    )

    readiness = await service.finalize_for_capture(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        page_type="content_list",
    )

    assert publisher.calls[0]["page_type"] == "content_list"
    # 裁决必须消费刚发布的同一份需求集（E14 同源）
    assert adjudicator.calls[0]["requirement_set"].capture_id == CAPTURE_ID
    assert readiness.manifest_quality_status == "pass"
    assert readiness.gate_ready is True


async def test_readiness_reports_not_ready_when_manifest_still_pending() -> None:
    service = CaptureEvidenceReadinessService(
        requirement_publisher=FakePublisher(),
        manifest_adjudicator=FakeAdjudicator(status="pending"),
    )

    readiness = await service.finalize_for_capture(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        page_type="content_list",
    )

    assert readiness.gate_ready is False


async def test_readiness_failure_never_raises() -> None:
    """需求集发布失败必须返回稳定、不可重试且脱敏的控制面错误。"""

    class FailingPublisher:
        async def publish_for_capture(self, **kwargs: Any):
            del kwargs
            raise RuntimeError("database unavailable")

    service = CaptureEvidenceReadinessService(
        requirement_publisher=FailingPublisher(),
        manifest_adjudicator=FakeAdjudicator(),
    )

    readiness = await service.finalize_for_capture(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        page_type="content_list",
    )

    assert readiness.gate_ready is False
    assert readiness.failure_code == "EVIDENCE_REQUIREMENT_PUBLISH_FAILED"
    assert readiness.retryable is False
    assert readiness.failure_reason == "requirement_publish_failed"
    assert "database unavailable" not in (readiness.failure_reason or "")


async def test_readiness_adjudication_failure_keeps_requirement_set() -> None:
    class FailingAdjudicator:
        async def adjudicate_for_capture(self, **kwargs: Any):
            del kwargs
            raise RuntimeError("adjudication exploded")

    service = CaptureEvidenceReadinessService(
        requirement_publisher=FakePublisher(),
        manifest_adjudicator=FailingAdjudicator(),
    )

    readiness = await service.finalize_for_capture(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        page_type="content_list",
    )

    assert readiness.requirement_set_id is not None
    assert readiness.gate_ready is False
    assert "manifest_adjudication_failed" in (readiness.failure_reason or "")


# ---------- E18：证据不足处理不触发补采 ----------


def test_blocked_decision_carries_reasons_without_supplement_request() -> None:
    """E18 验收：证据不足时记录阻断原因，不生成补采请求。"""
    decision = _decision(allow=False)

    assert decision.allow_leave is False
    assert decision.block_reasons
    payload = decision.snapshot.to_dict()
    # 快照只描述事实计数，不含任何补采指令
    assert "supplement" not in str(payload)
    assert "probe" not in str(payload)


async def test_finalize_status_flows_through_shadow_wrapper() -> None:
    inner = FakeInnerGate(_decision(allow=True))
    gate = ShadowEvidenceExitGate(inner)
    decision = _decision(allow=True)

    await gate.finalize(
        decision=decision, status=EvidenceExitReservationStatus.CONSUMED
    )

    assert inner.finalized == [(decision, EvidenceExitReservationStatus.CONSUMED)]


def _coverage_snapshot(*, status: str | None = None) -> WebCoverageSnapshot:
    return WebCoverageSnapshot.create(
        task_id=TASK_ID,
        agent_run_id="22222222-2222-2222-2222-222222222222",
        finalization_epoch=4,
        required_page_types=("content_list", "content_detail"),
        observed_page_types=("content_list",),
        unrecognized_page_types=(),
        coverage_status=status,
        snapshot_id="33333333-3333-3333-3333-333333333333",
        created_at=NOW,
    )


def test_final_coverage_gap_allows_report_with_completed_with_gaps() -> None:
    decision = evaluate_web_coverage_exit_gate(
        exploration_terminal=True,
        pending_capture_commit_count=0,
        snapshot=_coverage_snapshot(),
    )

    assert decision.allow_report is True
    assert decision.execution_status == "completed_with_gaps"
    assert decision.reason_codes == ("WEB_COVERAGE_GAP",)


def test_foundational_failure_blocks_report_and_never_becomes_gap() -> None:
    decision = evaluate_web_coverage_exit_gate(
        exploration_terminal=True,
        pending_capture_commit_count=0,
        snapshot=_coverage_snapshot(status="failed"),
    )

    assert decision.allow_report is False
    assert decision.execution_status == "failed"
    assert decision.reason_codes == ("WEB_FOUNDATIONAL_EVIDENCE_FAILED",)


def test_temporary_missing_coverage_before_terminal_is_not_a_gap() -> None:
    decision = evaluate_web_coverage_exit_gate(
        exploration_terminal=False,
        pending_capture_commit_count=1,
        snapshot=None,
    )

    assert decision.allow_report is False
    assert decision.execution_status is None
    assert decision.reason_codes == (
        "WEB_EXPLORATION_NOT_TERMINAL",
        "WEB_CAPTURE_COMMIT_PENDING",
        "WEB_COVERAGE_SNAPSHOT_MISSING",
    )


def test_untrusted_formal_capture_blocks_report_even_when_coverage_is_complete() -> None:
    decision = evaluate_web_coverage_exit_gate(
        exploration_terminal=True,
        pending_capture_commit_count=0,
        untrusted_formal_capture_count=1,
        snapshot=_coverage_snapshot(),
    )

    assert decision.allow_report is False
    assert decision.execution_status is None
    assert decision.reason_codes == ("WEB_UNTRUSTED_FORMAL_EVIDENCE",)
