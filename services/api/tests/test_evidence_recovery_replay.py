from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.evidence_recovery_replay import (
    EvidenceRecoveryReplayError,
    EvidenceRecoveryReplayExecutor,
    RecoveryObservation,
    RecoveryStepReceipt,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationPlan,
    RestorationStrategy,
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _plan(
    *,
    transition_ids: tuple[str, ...] = ("edge-1", "edge-2"),
    deadline: datetime | None = None,
    strategy: RestorationStrategy = RestorationStrategy.SAME_APP_SESSION,
    surface_kind: SurfaceKind = SurfaceKind.ANDROID,
) -> RestorationPlan:
    request = build_evidence_request(
        work_item_id="work-item-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="RULE-1",
        target_capture_id="state-detail",
        reason_code="missing_detail_evidence",
        evidence_goal="补采详情页证据",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"semantic": {"page_identity": "detail"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=NOW + timedelta(minutes=10),
    )
    return build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=surface_kind,
        strategy=strategy,
        replay_execution_plan_id=(
            "page-graph-route:route-1"
            if strategy is not RestorationStrategy.ORIGINAL_SURFACE_PIN
            else None
        ),
        normalization_step_ids=(
            transition_ids
            if strategy is not RestorationStrategy.ORIGINAL_SURFACE_PIN
            else ()
        ),
        deadline=deadline or NOW + timedelta(minutes=5),
    )


def _state(state_id: str, identity: str, fingerprint: str) -> dict[str, object]:
    return {
        "state_id": state_id,
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "surface_session_id": "session-1",
        "surface_kind": "android",
        "finalization_epoch": 3,
        "logical_page_key": identity,
        "identity_status": "confirmed",
        "state_fingerprint": fingerprint,
        "counts_as_business_page": True,
        "is_overlay": False,
        "is_external": False,
    }


def _edge(
    transition_id: str,
    source: str,
    target: str,
    *,
    semantic_target: str,
) -> dict[str, object]:
    return {
        "transition_id": transition_id,
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "surface_session_id": "session-1",
        "surface_kind": "android",
        "finalization_epoch": 3,
        "source_state_id": source,
        "target_state_id": target,
        "action_kind": "click",
        "semantic_target": semantic_target,
        "status": "completed",
        "proof": "trusted",
        "risk_level": "low",
        "replay_eligible": True,
    }


def _graph() -> dict[str, object]:
    return {
        "protocol_version": "page_graph.v1",
        "status": "available",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "finalization_epoch": 3,
        "states": [
            _state("state-home", "home", "fp-home"),
            _state("state-list", "list", "fp-list"),
            _state("state-detail", "detail", "fp-detail"),
        ],
        "transitions": [
            _edge("edge-1", "state-home", "state-list", semantic_target="内容列表"),
            _edge("edge-2", "state-list", "state-detail", semantic_target="内容详情"),
        ],
    }


class GraphRepository:
    def __init__(self, graph: dict[str, object]) -> None:
        self.graph = graph
        self.calls: list[dict[str, object]] = []

    async def get_graph(self, **kwargs):
        self.calls.append(kwargs)
        return self.graph


class Runtime:
    def __init__(self, observations: list[RecoveryObservation]) -> None:
        self.observations = list(observations)
        self.steps = []

    async def observe(self, **kwargs) -> RecoveryObservation:
        del kwargs
        return self.observations.pop(0)

    async def execute(
        self,
        *,
        step,
        source,
        task_id,
        session_id,
        plan,
        agent_run_id,
        finalization_epoch,
    ) -> RecoveryStepReceipt:
        assert task_id == "task-1"
        assert session_id == "session-1"
        assert plan.task_id == task_id
        assert agent_run_id == "run-1"
        assert finalization_epoch == 3
        self.steps.append((step, source))
        after = self.observations.pop(0)
        return RecoveryStepReceipt(
            transition_id=step.transition_id,
            action_execution_id=f"action-{step.transition_id}",
            after=after,
        )


def _observation(capture_id: str, identity: str, fingerprint: str) -> RecoveryObservation:
    return RecoveryObservation(
        capture_id=capture_id,
        page_identity=identity,
        state_fingerprint=fingerprint,
        observed_at=NOW,
    )


@pytest.mark.asyncio
async def test_replay_executes_graph_edges_in_order_and_reverifies_each_arrival() -> None:
    repository = GraphRepository(_graph())
    runtime = Runtime(
        [
            _observation("capture-live-home", "home", "fp-home"),
            _observation("capture-live-list", "list", "fp-list"),
            _observation("capture-live-detail", "detail", "fp-detail"),
        ]
    )

    outcome = await EvidenceRecoveryReplayExecutor(
        repository=repository,
        runtime=runtime,
        now=lambda: NOW,
    ).execute(
        plan=_plan(),
        agent_run_id="run-1",
        finalization_epoch=3,
    )

    assert outcome.arrived is True
    assert outcome.result_capture_id == "capture-live-detail"
    assert outcome.action_execution_ids == ("action-edge-1", "action-edge-2")
    assert [item[0].transition_id for item in runtime.steps] == ["edge-1", "edge-2"]
    assert runtime.steps[1][1].capture_id == "capture-live-list"
    assert repository.calls == [
        {
            "task_id": "task-1",
            "agent_run_id": "run-1",
            "finalization_epoch": 3,
        }
    ]


@pytest.mark.asyncio
async def test_replay_stops_before_first_action_when_live_source_has_drifted() -> None:
    runtime = Runtime([_observation("capture-other", "popup", "fp-popup")])

    with pytest.raises(EvidenceRecoveryReplayError) as error:
        await EvidenceRecoveryReplayExecutor(
            repository=GraphRepository(_graph()),
            runtime=runtime,
            now=lambda: NOW,
        ).execute(plan=_plan(), agent_run_id="run-1", finalization_epoch=3)

    assert error.value.reason_code == "RECOVERY_SOURCE_STATE_DRIFTED"
    assert runtime.steps == []


@pytest.mark.asyncio
async def test_h5_replay_rebinds_dynamic_state_by_identity_and_reverifies_each_step() -> None:
    graph = _graph()
    graph["target_platform"] = "h5"
    for state in graph["states"]:  # type: ignore[index, union-attr]
        state["surface_kind"] = "web"
    for edge in graph["transitions"]:  # type: ignore[index, union-attr]
        edge["surface_kind"] = "web"
    runtime = Runtime(
        [
            _observation("capture-live-home", "home", "fp-home-live-timer"),
            _observation("capture-live-list", "list", "fp-list-live-timer"),
            _observation("capture-live-detail", "detail", "fp-detail-live-timer"),
        ]
    )

    outcome = await EvidenceRecoveryReplayExecutor(
        repository=GraphRepository(graph),
        runtime=runtime,
        now=lambda: NOW,
    ).execute(
        plan=_plan(
            surface_kind=SurfaceKind.WEB,
            strategy=RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
        ),
        agent_run_id="run-1",
        finalization_epoch=3,
    )

    assert outcome.arrived is True
    assert outcome.transition_ids == ("edge-1", "edge-2")
    assert [item[0].transition_id for item in runtime.steps] == ["edge-1", "edge-2"]


@pytest.mark.asyncio
async def test_h5_replay_still_rejects_different_logical_page_identity() -> None:
    graph = _graph()
    graph["target_platform"] = "h5"
    for state in graph["states"]:  # type: ignore[index, union-attr]
        state["surface_kind"] = "web"
    for edge in graph["transitions"]:  # type: ignore[index, union-attr]
        edge["surface_kind"] = "web"
    runtime = Runtime([_observation("capture-popup", "popup", "fp-live")])

    with pytest.raises(EvidenceRecoveryReplayError) as error:
        await EvidenceRecoveryReplayExecutor(
            repository=GraphRepository(graph),
            runtime=runtime,
            now=lambda: NOW,
        ).execute(
            plan=_plan(
                surface_kind=SurfaceKind.WEB,
                strategy=RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
            ),
            agent_run_id="run-1",
            finalization_epoch=3,
        )

    assert error.value.reason_code == "RECOVERY_SOURCE_STATE_DRIFTED"
    assert runtime.steps == []


@pytest.mark.asyncio
async def test_replay_stops_at_first_divergent_arrival_without_skipping_next_edge() -> None:
    runtime = Runtime(
        [
            _observation("capture-live-home", "home", "fp-home"),
            _observation("capture-wrong", "search", "fp-search"),
            _observation("capture-unused", "detail", "fp-detail"),
        ]
    )

    with pytest.raises(EvidenceRecoveryReplayError) as error:
        await EvidenceRecoveryReplayExecutor(
            repository=GraphRepository(_graph()),
            runtime=runtime,
            now=lambda: NOW,
        ).execute(plan=_plan(), agent_run_id="run-1", finalization_epoch=3)

    assert error.value.reason_code == "RECOVERY_STEP_ARRIVAL_DIVERGED"
    assert [item[0].transition_id for item in runtime.steps] == ["edge-1"]


@pytest.mark.asyncio
async def test_replay_rejects_untrusted_or_cross_scope_graph_edge() -> None:
    for changed in (
        {"proof": "insufficient"},
        {"risk_level": "high"},
        {"replay_eligible": False},
        {"agent_run_id": "run-other"},
        {"finalization_epoch": 4},
    ):
        graph = _graph()
        graph["transitions"][0].update(changed)  # type: ignore[index, union-attr]
        runtime = Runtime([_observation("capture-live-home", "home", "fp-home")])

        with pytest.raises(EvidenceRecoveryReplayError) as error:
            await EvidenceRecoveryReplayExecutor(
                repository=GraphRepository(graph),
                runtime=runtime,
                now=lambda: NOW,
            ).execute(plan=_plan(), agent_run_id="run-1", finalization_epoch=3)

        assert error.value.reason_code == "RECOVERY_ROUTE_INVALID"
        assert runtime.steps == []


@pytest.mark.asyncio
async def test_replay_rejects_expired_plan_before_reading_graph() -> None:
    repository = GraphRepository(_graph())
    runtime = Runtime([])
    plan = _plan(deadline=NOW)

    with pytest.raises(EvidenceRecoveryReplayError) as error:
        await EvidenceRecoveryReplayExecutor(
            repository=repository,
            runtime=runtime,
            now=lambda: NOW,
        ).execute(plan=plan, agent_run_id="run-1", finalization_epoch=3)

    assert error.value.reason_code == "RECOVERY_DEADLINE_EXHAUSTED"
    assert repository.calls == []


@pytest.mark.asyncio
async def test_original_surface_pin_observes_in_place_without_graph_replay() -> None:
    """当前目标就地 pin：只观察补帧，不读页面图、不执行边。"""

    request = build_evidence_request(
        work_item_id="work-item-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="RULE-1",
        target_capture_id="state-detail",
        reason_code="missing_detail_evidence",
        evidence_goal="补采详情页证据",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"semantic": {"page_identity": "detail"}},
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=NOW + timedelta(minutes=10),
    )
    plan = build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        deadline=NOW + timedelta(minutes=5),
    )
    repository = GraphRepository(_graph())
    runtime = Runtime([_observation("capture-pin-fresh", "detail", "fp-detail")])

    outcome = await EvidenceRecoveryReplayExecutor(
        repository=repository,
        runtime=runtime,
        now=lambda: NOW,
    ).execute(plan=plan, agent_run_id="run-1", finalization_epoch=3)

    assert outcome.arrived is True
    assert outcome.result_capture_id == "capture-pin-fresh"
    assert outcome.action_execution_ids == ()
    assert outcome.transition_ids == ()
    assert runtime.steps == []
    assert repository.calls == []
