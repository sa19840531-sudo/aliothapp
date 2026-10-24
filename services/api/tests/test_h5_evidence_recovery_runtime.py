"""H5 定向补采浏览器适配器的执行域和真实观察测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.evidence_recovery_replay import (
    EvidenceRecoveryReplayError,
    RecoveryGraphStep,
    RecoveryObservation,
)
from services.api.app.application.h5_exploration_observation import build_h5_frontier
from services.api.app.domain.exploration import CandidateAction
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationStrategy,
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.browser.h5_evidence_recovery_runtime import (
    H5EvidenceRecoveryRuntime,
)

NOW = datetime(2026, 9, 12, 20, tzinfo=UTC)


def _capture(capture_id: str) -> CaptureRef:
    return CaptureRef(
        task_id="task-h5",
        session_id="h5:run-h5",
        capture_id=capture_id,
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
        artifact_ids=(f"artifact-{capture_id}",),
    )


def _plan():
    request = build_evidence_request(
        work_item_id="work-h5",
        attempt_id="attempt-h5",
        task_id="task-h5",
        rule_id="rule-h5",
        rule_code="h5_popup",
        target_capture_id="capture-target",
        reason_code="BASE_PAGE_OCCLUDED",
        evidence_goal="重新观察当前 H5 页面",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        required_equivalence=EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"environment": {"surface_kind": "web"}},
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=NOW + timedelta(minutes=10),
    )
    return build_restoration_plan(
        request=request,
        source_session_id="h5:run-h5",
        target_session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB,
        strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        deadline=NOW + timedelta(minutes=3),
    )


class _Sessions:
    async def get(self, *, session_id: str):
        assert session_id == "h5:run-h5"
        return SimpleNamespace(
            task_id="task-h5",
            finalization_epoch=4,
            surface_kind=SurfaceKind.WEB,
            current_capture_id="capture-target",
        )


class _Coordinator:
    def __init__(self) -> None:
        self.command = None

    async def coordinate(self, command):
        self.command = command
        return SimpleNamespace(
            interaction=SimpleNamespace(
                status=InteractionResultStatus.SUCCEEDED,
                error_code=None,
                error_message=None,
                action_execution_id="action-restored",
            ),
            after_capture=_capture("capture-restored"),
        )


class _Detector:
    def __init__(self, result=None) -> None:
        self.calls = []
        self.result = result

    async def detect(self, *, capture, task_id: str, agent_run_id: str):
        self.calls.append((capture.capture_id, task_id, agent_run_id))
        if self.result is None:
            return None
        return {
            **self.result,
            "page_capture_id": capture.capture_id,
        }


class _PageGraph:
    async def get_graph(self, *, task_id: str, agent_run_id: str, finalization_epoch: int):
        assert (task_id, agent_run_id, finalization_epoch) == ("task-h5", "run-h5", 4)
        return {
            "states": [
                {
                    "state_id": "capture-restored",
                    "logical_page_key": "home",
                    "state_fingerprint": "fingerprint-home",
                    "captured_at": NOW.isoformat(),
                }
            ]
        }


@pytest.mark.parametrize("barrier", [None, "overlay", "auth", "unconfirmed"])
async def test_shared_observation_is_batch_scoped_and_excludes_obstructed_pages(
    monkeypatch, barrier,
):
    from unittest.mock import AsyncMock

    import services.api.app.infrastructure.browser.h5_evidence_recovery_runtime as module

    session = SimpleNamespace(task_id="task-h5", finalization_epoch=4, surface_kind=SurfaceKind.WEB,
                              current_capture_id="capture-target", command_sequence=1,
                              status="active", session_owner="h5-workflow")
    coordinator = _Coordinator()
    original_coordinate = coordinator.coordinate
    calls = []

    async def coordinate(command):
        calls.append(command)
        result = await original_coordinate(command)
        session.current_capture_id = result.after_capture.capture_id
        session.command_sequence += 1
        return result

    coordinator.coordinate = coordinate
    monkeypatch.setattr(module, "build_h5_frontier", lambda **_: SimpleNamespace(
        observation=SimpleNamespace(
            page_state="stable", overlay_signature="mask" if barrier == "overlay" else None,
            metadata={} if barrier == "unconfirmed" else {
                "logical_surface_identity_fingerprint": "same-page",
                "requires_auth": barrier == "auth",
            },
    )))
    runtime = H5EvidenceRecoveryRuntime(
        surface_sessions=SimpleNamespace(get=AsyncMock(return_value=session)),
        capture_coordinator=coordinator, detector=_Detector({"page_type": "home"}),
        page_graph=_PageGraph(), now=lambda: NOW,
    )
    kwargs = dict(task_id="task-h5", session_id="h5:run-h5", plan=_plan(),
                  agent_run_id="run-h5", finalization_epoch=4)
    with runtime.share_observations():
        first = await runtime.observe(**kwargs)
        second = await runtime.observe(**kwargs)
        assert first == second
        assert len(calls) == (1 if barrier is None else 2)
        if barrier is None:
            session.command_sequence += 1
            with pytest.raises(EvidenceRecoveryReplayError) as error:
                await runtime.observe(**kwargs)
            assert error.value.reason_code == "H5_RECAPTURE_SHARED_SURFACE_CHANGED"
            assert len(calls) == 1
    assert runtime._shared_observation.get() is None
    await runtime.observe(**kwargs)
    assert len(calls) == (2 if barrier is None else 3)


async def test_h5_recovery_reobserves_current_surface_through_capture_coordinator() -> None:
    coordinator = _Coordinator()
    detector = _Detector()
    runtime = H5EvidenceRecoveryRuntime(
        surface_sessions=_Sessions(),
        capture_coordinator=coordinator,
        detector=detector,
        page_graph=_PageGraph(),
        now=lambda: NOW,
    )

    observation = await runtime.observe(
        task_id="task-h5",
        session_id="h5:run-h5",
        plan=_plan(),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )

    assert observation.capture_id == "capture-restored"
    assert observation.page_identity == "home"
    assert coordinator.command.allow_draining is True
    assert coordinator.command.intent.action_type is InteractionOperation.OBSERVE
    assert coordinator.command.intent.source_capture_id == "capture-target"
    assert coordinator.command.intent.parameters["supplemental_evidence_source"] == (
        "targeted_recapture"
    )
    assert detector.calls == [("capture-restored", "task-h5", "run-h5")]


async def test_h5_recovery_rejects_non_exact_h5_session() -> None:
    runtime = H5EvidenceRecoveryRuntime(
        surface_sessions=_Sessions(),
        capture_coordinator=_Coordinator(),
        detector=_Detector(),
        page_graph=_PageGraph(),
    )

    with pytest.raises(EvidenceRecoveryReplayError) as raised:
        await runtime.observe(
            task_id="task-h5",
            session_id="h5:run-h5:other",
            plan=_plan(),
            agent_run_id="run-h5",
            finalization_epoch=4,
        )

    assert raised.value.reason_code == "H5_RECAPTURE_SCOPE_MISMATCH"
    assert raised.value.retryable is False


async def test_h5_recovery_replays_unique_low_risk_page_graph_action() -> None:
    detection = {
        "schema_version": "page-detection.v2",
        "page_url": "https://m.example.test/home",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.96,
        "reason": "可信详情入口",
        "matched_catalog_signals": {"ocr_keywords": ["详情"]},
        "candidate_actions": [
            {
                "action": "click",
                "target": "详情入口",
                "purpose": "进入详情页补采",
                "risk_level": "low",
                "navigation_depth": 1,
                "selector": ".detail-entry:visible",
            }
        ],
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact-screenshot"],
    }
    coordinator = _Coordinator()
    runtime = H5EvidenceRecoveryRuntime(
        surface_sessions=_Sessions(),
        capture_coordinator=coordinator,
        detector=_Detector(detection),
        page_graph=_PageGraph(),
    )
    source = await runtime.observe(
        task_id="task-h5",
        session_id="h5:run-h5",
        plan=_plan(),
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    step = RecoveryGraphStep(
        transition_id="transition-1",
        source_state_id="capture-target",
        target_state_id="capture-detail",
        action_kind="click",
        semantic_target="详情入口",
        source_identity="home",
        source_state_fingerprint="fingerprint-home",
        target_identity="detail",
        target_state_fingerprint="fingerprint-detail",
    )

    receipt = await runtime.execute(
        task_id="task-h5",
        session_id="h5:run-h5",
        plan=_plan(),
        agent_run_id="run-h5",
        finalization_epoch=4,
        step=step,
        source=source,
    )

    assert receipt.transition_id == "transition-1"
    assert receipt.action_execution_id == "action-restored"
    assert coordinator.command.intent.semantic_target == "详情入口"
    assert coordinator.command.intent.parameters["supplemental_evidence_source"] == (
        "targeted_recapture"
    )


async def test_h5_recovery_rejects_route_without_fresh_unique_candidate() -> None:
    runtime = H5EvidenceRecoveryRuntime(
        surface_sessions=_Sessions(),
        capture_coordinator=_Coordinator(),
        detector=_Detector(),
        page_graph=_PageGraph(),
    )
    source = RecoveryObservation(
        capture_id="capture-target",
        page_identity="home",
        state_fingerprint="fingerprint-home",
        observed_at=NOW,
    )
    step = RecoveryGraphStep(
        transition_id="transition-1",
        source_state_id="capture-target",
        target_state_id="capture-detail",
        action_kind="click",
        semantic_target="详情入口",
        source_identity="home",
        source_state_fingerprint="fingerprint-home",
        target_identity="detail",
        target_state_fingerprint="fingerprint-detail",
    )

    with pytest.raises(EvidenceRecoveryReplayError) as raised:
        await runtime.execute(
            task_id="task-h5",
            session_id="h5:run-h5",
            plan=_plan(),
            agent_run_id="run-h5",
            finalization_epoch=4,
            step=step,
            source=source,
        )

    assert raised.value.reason_code == "H5_RECAPTURE_SOURCE_FRONTIER_UNAVAILABLE"


@pytest.mark.parametrize("stale", [False, True])
async def test_recovery_route_keeps_fresh_logical_target_contract(stale):
    coordinator = _Coordinator()
    runtime = H5EvidenceRecoveryRuntime(
        surface_sessions=_Sessions(), capture_coordinator=coordinator,
        detector=_Detector(), page_graph=_PageGraph(), now=lambda: NOW,
    )
    frontier = build_h5_frontier(detection_result={
        "page_capture_id": "capture-target", "page_url": "https://m.example.test/home",
        "page_type": "home", "page_state": "stable", "confidence": 0.9,
        "candidate_actions": [],
    }, observed_at=NOW, navigation_depth=0)
    identity, state = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    candidate = CandidateAction(
        candidate_id="route-candidate", action_type=InteractionOperation.CLICK,
        semantic_target="详情入口", risk_level=InteractionRiskLevel.LOW, navigation_depth=1,
        exploration_value=80,
        metadata={
            "logical_target_required": True, "source_page_type": "home",
            "expected_next_page_type": "content_detail",
            "source_identity_fingerprint": identity,
            "source_state_fingerprint": "sha256:" + "c" * 64 if stale else state,
        },
    )
    runtime._frontiers["capture-target"] = replace(
        frontier, candidates=(candidate,), observation=replace(frontier.observation, metadata={
            "logical_targets_effective": True,
            "logical_surface_identity_fingerprint": identity,
            "logical_surface_state_fingerprint": state,
        }),
    )
    arguments = dict(
        task_id="task-h5", session_id="h5:run-h5", plan=_plan(),
        agent_run_id="run-h5", finalization_epoch=4,
        source=RecoveryObservation(
            capture_id="capture-target", page_identity="home",
            state_fingerprint="fingerprint-home", observed_at=NOW,
        ),
        step=RecoveryGraphStep(
            transition_id="transition-1", source_state_id="capture-target",
            target_state_id="capture-detail", action_kind="click", semantic_target="详情入口",
            source_identity="home", source_state_fingerprint="fingerprint-home",
            target_identity="detail", target_state_fingerprint="fingerprint-detail",
        ),
    )
    if stale:
        with pytest.raises(EvidenceRecoveryReplayError) as raised:
            await runtime.execute(**arguments)
        assert raised.value.reason_code == "LOGICAL_SOURCE_STATE_STALE"
        assert coordinator.command is None
    else:
        await runtime.execute(**arguments)
        assert coordinator.command.intent.parameters["logical_target"][
            "source_state_fingerprint"
        ] == state
        assert coordinator.command.intent.parameters["logical_target"][
            "expects_logical_navigation"
        ] is True
