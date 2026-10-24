from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.h5_exploration_cancellation import (
    H5ExplorationCanceled,
)
from services.api.app.application.h5_exploration_recovery import (
    H5BrowserRecoveryAttemptFacts,
    H5BrowserRecoveryFacts,
    H5ExplorationRecovery,
    H5FailureStage,
    H5RecoveryContext,
    build_h5_recovery_session_id,
    h5_global_bottom_navigation_index,
)
from services.api.app.domain.exploration import CandidateAction
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)


def _context(
    *,
    stage: str,
    recovery_attempt_count: int = 0,
    max_recovery_attempts: int = 2,
    last_safe_location: str = "https://example.test/home",
    entry_origin: str = "https://example.test/start",
) -> H5RecoveryContext:
    return H5RecoveryContext(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="h5:run-1",
        stage=H5FailureStage(stage),
        last_safe_location=last_safe_location,
        action_execution_id=(None if stage == "before_capture" else "action-1"),
        frontier_snapshot=H5TaskFrontierSnapshot.create(max_pages=3),
        recovery_attempt_count=recovery_attempt_count,
        max_recovery_attempts=max_recovery_attempts,
        entry_origin=entry_origin,
    )


def test_global_bottom_navigation_index_accepts_contextual_contract() -> None:
    """恢复链只接受完整的受信上下文底栏合同。"""
    candidate = CandidateAction(
        candidate_id="candidate-contextual-navigation",
        semantic_target=(
            "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"
        ),
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "target_keywords": ["bottom_navigation", "二次元", "content_list"],
            "expected_next_page_type": "content_list",
            "navigation_knowledge_source": "contextual",
            "navigation_knowledge_confidence": 1.0,
            "action_parameters": {
                "target_mode": "bottom_navigation",
                "navigation_index": 2,
                "navigation_label": "二次元",
            },
        },
    )

    assert h5_global_bottom_navigation_index(candidate) == 2


class _Runtime:
    def __init__(
        self,
        *,
        assessment_status: str = "covered",
        equivalent: bool = True,
        cancel_at: str | None = None,
    ) -> None:
        self.events: list[str] = []
        self.assessment_status = assessment_status
        self.equivalent = equivalent
        self.cancel_at = cancel_at
        self.action_calls = 0

    async def checkpoint(self, *, context, stage):
        del context
        self.events.append(f"cancel:{stage}")
        if self.cancel_at == stage:
            raise H5ExplorationCanceled(stage=stage)

    async def reopen(self, *, context):
        del context
        self.events.append("reopen")

    async def navigate(self, *, context, destination_url):
        del context
        self.events.append(f"navigate:{destination_url}")

    async def capture(self, *, context):
        del context
        self.events.append("capture")
        return {"capture_id": "capture-rebaseline"}

    async def detect(self, *, context, capture):
        del context, capture
        self.events.append("detect")
        return {"state_version": "state-safe"}

    async def assess(self, *, context, capture, detection):
        del context, capture, detection
        self.events.append("assess")
        return {"status": self.assessment_status}

    async def state_equivalent(self, *, context, capture, detection, assessment):
        del context, capture, detection, assessment
        self.events.append("equivalent")
        return self.equivalent

    async def persist_recovery(self, *, context, **kwargs):
        del context, kwargs
        self.events.append("persist")
        return None


@pytest.mark.asyncio
async def test_before_capture_loss_reopens_rebaselines_and_retries_only_once() -> None:
    runtime = _Runtime()
    recovery = H5ExplorationRecovery(runtime=runtime)

    outcome = await recovery.recover(context=_context(stage="before_capture"))

    assert outcome.retry_original_action is True
    assert outcome.recovery_attempt_count == 1
    assert outcome.rebaseline_steps == ("capture", "detect", "assess")
    assert runtime.action_calls == 0
    assert runtime.events == [
        "cancel:before_recovery_decision",
        "cancel:before_reopen",
        "reopen",
        "cancel:before_navigate",
        "navigate:https://example.test/home",
        "cancel:before_capture",
        "capture",
        "cancel:before_detect",
        "detect",
        "cancel:before_assess",
        "assess",
        "equivalent",
        "persist",
        "cancel:before_retry_action",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["action", "after_capture"])
async def test_unknown_or_after_loss_never_replays_and_supersedes_failed_work(
    stage: str,
) -> None:
    runtime = _Runtime()
    recovery = H5ExplorationRecovery(runtime=runtime)

    outcome = await recovery.recover(context=_context(stage=stage))

    assert outcome.retry_original_action is False
    assert outcome.inventory_status == "insufficient"
    assert outcome.supersede_failed_work is True
    assert outcome.rebaseline_steps == ("capture", "detect", "assess")
    assert runtime.action_calls == 0
    assert runtime.events[-1] == "cancel:before_successor"


@pytest.mark.asyncio
async def test_recovery_attempt_exhaustion_stops_before_reopen() -> None:
    runtime = _Runtime()
    outcome = await H5ExplorationRecovery(runtime=runtime).recover(
        context=_context(
            stage="before_capture",
            recovery_attempt_count=2,
            max_recovery_attempts=2,
        )
    )

    assert outcome.retry_original_action is False
    assert outcome.reason_code == "h5_browser_recovery_attempts_exhausted"
    assert runtime.events == ["cancel:before_recovery_decision"]


@pytest.mark.asyncio
async def test_cross_origin_restore_fails_closed_before_reopen() -> None:
    runtime = _Runtime()
    outcome = await H5ExplorationRecovery(runtime=runtime).recover(
        context=_context(
            stage="action",
            last_safe_location="https://other.test/home",
        )
    )

    assert outcome.retry_original_action is False
    assert outcome.supersede_failed_work is False
    assert outcome.reason_code == "h5_browser_recovery_cross_origin"
    assert runtime.events == ["cancel:before_recovery_decision"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_origin", "last_safe_location", "allowed"),
    [
        ("https://EXAMPLE.test", "https://example.test:443/home", True),
        ("http://example.test:80", "http://example.test/home", True),
        ("https://example.test:8443", "https://example.test:8443/home", True),
        ("https://example.test:8443", "https://example.test:9443/home", False),
        ("https://user@example.test", "https://example.test/home", False),
        ("https://example.test:invalid", "https://example.test/home", False),
    ],
)
async def test_recovery_origin_comparison_is_canonical_and_fail_closed(
    entry_origin: str,
    last_safe_location: str,
    allowed: bool,
) -> None:
    runtime = _Runtime()
    outcome = await H5ExplorationRecovery(runtime=runtime).recover(
        context=_context(
            stage="before_capture",
            entry_origin=entry_origin,
            last_safe_location=last_safe_location,
        )
    )

    assert outcome.retry_original_action is allowed
    assert ("reopen" in runtime.events) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime", "reason"),
    [
        (_Runtime(assessment_status="requires_auth"), "h5_browser_recovery_assessment_blocked"),
        (_Runtime(equivalent=False), "h5_browser_recovery_state_mismatch"),
    ],
)
async def test_rebaseline_failures_never_retry_original_action(
    runtime: _Runtime,
    reason: str,
) -> None:
    outcome = await H5ExplorationRecovery(runtime=runtime).recover(
        context=_context(stage="before_capture")
    )

    assert outcome.retry_original_action is False
    assert outcome.reason_code == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_at",
    [
        "before_reopen",
        "before_navigate",
        "before_capture",
        "before_detect",
        "before_assess",
        "before_retry_action",
    ],
)
async def test_cancellation_applies_at_every_recovery_boundary(cancel_at: str) -> None:
    runtime = _Runtime(cancel_at=cancel_at)

    with pytest.raises(H5ExplorationCanceled):
        await H5ExplorationRecovery(runtime=runtime).recover(
            context=_context(stage="before_capture")
        )

    assert runtime.action_calls == 0


def test_recovery_context_is_immutable() -> None:
    context = _context(stage="action")

    with pytest.raises(FrozenInstanceError):
        context.recovery_attempt_count = 3  # type: ignore[misc]


def test_browser_recovery_facts_round_trip_with_stable_content_identity() -> None:
    facts = H5BrowserRecoveryFacts.create(
        task_id="task-1",
        agent_run_id="run-1",
        source_session_id="h5:run-1",
        failed_work_id="work-1",
        target_session_id=build_h5_recovery_session_id(
            agent_run_id="run-1", work_id="work-1", stage=H5FailureStage.ACTION,
            recovery_attempt_count=1,
        ),
        failure_stage=H5FailureStage.ACTION,
        recovery_attempt_count=1,
        original_source_capture_id="capture-source",
        rebaseline_before_capture_id="capture-recovery-before",
        rebaseline_after_capture_id="capture-rebaseline",
        failed_action_execution_id="action-1",
        restoration_action_execution_id="recovery-action-1",
        inventory_status="insufficient",
        source_state_version="state-before",
        target_state_version="state-before",
        equivalent=True,
        reason_code=None,
    )

    replay = H5BrowserRecoveryFacts.from_dict(facts.to_dict())

    assert replay == facts
    assert replay.schema_version == "h5-browser-recovery/v1"
    assert replay.restoration_attempt_id.startswith("h5-restoration:")
    assert replay.state_equivalence_id.startswith("h5-state-equivalence:")
    assert len(replay.payload_hash) == 64


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("target_state_version", "tampered-state"),
        ("recovery_attempt_count", 2),
        ("payload_hash", "0" * 64),
    ],
)
def test_browser_recovery_facts_reject_tampering(
    field_name: str,
    replacement: object,
) -> None:
    payload = H5BrowserRecoveryFacts.create(
        task_id="task-1",
        agent_run_id="run-1",
        source_session_id="h5:run-1",
        failed_work_id="work-1",
        target_session_id=build_h5_recovery_session_id(
            agent_run_id="run-1", work_id="work-1", stage=H5FailureStage.AFTER_CAPTURE,
            recovery_attempt_count=1,
        ),
        failure_stage=H5FailureStage.AFTER_CAPTURE,
        recovery_attempt_count=1,
        original_source_capture_id="capture-source",
        rebaseline_before_capture_id="capture-recovery-before",
        rebaseline_after_capture_id="capture-after",
        failed_action_execution_id="action-1",
        restoration_action_execution_id="recovery-action-1",
        inventory_status="insufficient",
        source_state_version="state-before",
        target_state_version="state-before",
        equivalent=True,
        reason_code=None,
    ).to_dict()
    payload[field_name] = replacement

    with pytest.raises(ValueError, match="hash|identity|事实"):
        H5BrowserRecoveryFacts.from_dict(payload)


@pytest.mark.parametrize(
    ("stage", "attempt", "action_id", "inventory", "equivalent", "reason"),
    [
        (H5FailureStage.BEFORE_CAPTURE, 2, None, None, True, None),
        (H5FailureStage.BEFORE_CAPTURE, 1, "action-1", None, True, None),
        (H5FailureStage.BEFORE_CAPTURE, 1, None, "insufficient", True, None),
        (H5FailureStage.ACTION, 3, "action-1", "insufficient", True, None),
        (H5FailureStage.ACTION, 1, None, "insufficient", True, None),
        (H5FailureStage.ACTION, 1, "action-1", None, True, None),
        (H5FailureStage.ACTION, 1, "action-1", "insufficient", False, None),
        (
            H5FailureStage.ACTION,
            1,
            "action-1",
            "insufficient",
            True,
            "unexpected_reason",
        ),
    ],
)
def test_browser_recovery_facts_close_stage_and_equivalence_invariants(
    stage: H5FailureStage,
    attempt: int,
    action_id: str | None,
    inventory: str | None,
    equivalent: bool,
    reason: str | None,
) -> None:
    with pytest.raises(ValueError):
        H5BrowserRecoveryFacts.create(
            task_id="task-1",
            agent_run_id="run-1",
            source_session_id="h5:run-1",
            failed_work_id="work-1",
            target_session_id=build_h5_recovery_session_id(
                agent_run_id="run-1",
                work_id="work-1",
                stage=stage,
                recovery_attempt_count=attempt,
            ),
            failure_stage=stage,
            recovery_attempt_count=attempt,
            original_source_capture_id="capture-source",
            rebaseline_before_capture_id="capture-recovery-before",
            rebaseline_after_capture_id="capture-rebaseline",
            failed_action_execution_id=action_id,
            restoration_action_execution_id="recovery-action-1",
            inventory_status=inventory,
            source_state_version="state-before",
            target_state_version="state-before",
            equivalent=equivalent,
            reason_code=reason,
        )


def test_recovery_session_id_is_canonical_non_nested_and_content_stable() -> None:
    first = build_h5_recovery_session_id(
        agent_run_id="run-1",
        work_id="work-1",
        stage=H5FailureStage.ACTION,
        recovery_attempt_count=1,
    )
    replay = build_h5_recovery_session_id(
        agent_run_id="run-1",
        work_id="work-1",
        stage=H5FailureStage.ACTION,
        recovery_attempt_count=1,
    )

    assert first == replay
    assert first.startswith("h5:run-1:recovery:")
    assert len(first.rsplit(":", 1)[1]) == 20
    assert first.count(":recovery:") == 1


def test_browser_recovery_attempt_round_trip_binds_work_budget_and_interaction() -> None:
    now = datetime.now(UTC)
    facts = H5BrowserRecoveryAttemptFacts.create(
        task_id="task-1",
        agent_run_id="run-1",
        source_session_id="h5:run-1",
        failed_work_id="work-1",
        source_state_version="state-1",
        frontier_snapshot_id="frontier-1",
        failure_stage=H5FailureStage.ACTION,
        recovery_attempt_count=1,
        status="started",
        error_code="page_capture_session_crashed",
        action_execution_id="action-1",
        before_capture_id="capture-before",
        after_capture_id=None,
        interaction_status="failed",
        interaction_result_capture_id=None,
        started_at=now.isoformat(),
        deadline=(now + timedelta(minutes=1)).isoformat(),
        remaining_duration_ms=30_000,
        action_cost=2,
        remaining_actions_after_reservation=1,
        last_safe_location="https://example.test/home",
        reason_code=None,
    )

    assert H5BrowserRecoveryAttemptFacts.from_dict(facts.to_dict()) == facts
    completed = facts.complete(reason_code="h5_browser_recovery_state_mismatch")
    assert completed.status == "completed"
    assert completed.payload_hash != facts.payload_hash

    for field, value in (
        ("task_id", "other-task"),
        ("recovery_attempt_count", True),
        ("remaining_actions_after_reservation", -1),
        ("started_at", now.replace(tzinfo=None).isoformat()),
    ):
        tampered = facts.to_dict()
        tampered[field] = value
        with pytest.raises((TypeError, ValueError)):
            H5BrowserRecoveryAttemptFacts.from_dict(tampered)
