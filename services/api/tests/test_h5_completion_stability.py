"""页面尚未稳定时，空候选和旧完成标记均不能结束 H5 探索。"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.application.h5_exploration_observation import build_h5_frontier
from services.api.app.application.h5_exploration_workflow import (
    _next_insufficient_reobserve_count,
    _with_task_frontier,
)
from services.api.app.application.h5_runtime_composition import H5CompletionAwareExplorer
from services.api.app.domain.exploration import ExplorationTerminal, ExplorationTerminalStatus
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
from services.api.app.domain.interaction import ActionIntent, InteractionOperation, SurfaceKind
from services.api.tests.test_h5_exploration_observation import (
    _detection,
    _detection_with_logical_surface,
)
from services.api.tests.test_h5_exploration_turn import _work


@pytest.mark.parametrize("page_state,metadata", [
    ("loading", {}), ("transitioning", {}), ("partially_loaded", {}),
    ("stable", {"obstruction_blocking": True}),
    ("stable", {"detection_status": "insufficient"}),
])
async def test_empty_frontier_cannot_override_unavailable_surface(page_state, metadata):
    work = _work()
    frontier = replace(work.frontier, candidates=(), observation=replace(
        work.frontier.observation, page_state=page_state,
        metadata={"objective_satisfied": True, **metadata},
    ))
    snapshot = H5TaskFrontierSnapshot.create(max_pages=10)
    frontier = _with_task_frontier(frontier=frontier, snapshot=snapshot)
    work = replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)

    class Delegate:
        async def decide_next(self, work):
            return ExplorationTerminal(
                work_id=work.work_id, task_id=work.task_id,
                status=ExplorationTerminalStatus.COMPLETED, reason="模型提前结束",
            )

    result = await H5CompletionAwareExplorer(delegate=Delegate()).decide_next(work)
    assert frontier.observation.metadata["objective_satisfied"] is False
    assert result.status is not ExplorationTerminalStatus.COMPLETED


@pytest.mark.parametrize("count", [0, 1, 2, 3])
@pytest.mark.parametrize("page_state", ["loading", "transitioning", "transient_loading"])
def test_transient_recognized_page_has_bounded_observe_without_logical_projection(
    count, page_state,
):
    detection = _detection(capture_id="capture-loading", evidence_id="artifact-loading")
    detection.update(page_state=page_state, candidate_actions=[], rule_matching_allowed=False)
    frontier = build_h5_frontier(
        detection_result=detection, observed_at=datetime.now(UTC), navigation_depth=0,
        lineage_metadata={"insufficient_reobserve_count": count},
    )
    assert frontier.observation.metadata["objective_satisfied"] is False
    if count < 3:
        assert len(frontier.candidates) == 1
        assert frontier.candidates[0].action_type is InteractionOperation.OBSERVE
        assert frontier.candidates[0].metadata["insufficient_reobserve_attempt"] == count + 1
    else:
        assert frontier.candidates == ()
        assert frontier.observation.metadata["insufficient_reobserve_exhausted"] is True


@pytest.mark.parametrize("count", [0, 1, 2])
def test_detected_loading_is_not_overridden_by_logical_loading_false(count):
    detection = _detection_with_logical_surface(
        capture_id="capture-loading", page_type="home",
        identity_fingerprint="sha256:" + "a" * 64,
        state_fingerprint="sha256:" + "b" * 64,
    )
    detection.update(page_state="loading", candidate_actions=[], rule_matching_allowed=False)
    detection["logical_surface"]["state_signals"] = {"loading": False}
    frontier = build_h5_frontier(
        detection_result=detection, observed_at=datetime.now(UTC), navigation_depth=0,
        logical_surface_effective=True, logical_targets_effective=True,
        lineage_metadata={"logical_reobserve_count": count},
    )
    assert frontier.observation.metadata["objective_satisfied"] is False
    if count < 2:
        assert len(frontier.candidates) == 1
        assert frontier.candidates[0].action_type is InteractionOperation.OBSERVE
    else:
        assert frontier.candidates == ()
        assert frontier.observation.metadata["logical_reobserve_exhausted"] is True


@pytest.mark.parametrize("mode,operation,expected", [
    ("insufficient_reobserve", InteractionOperation.OBSERVE, 3),
    ("logical_reobserve", InteractionOperation.OBSERVE, 2),
    (None, InteractionOperation.CLICK, 0),
])
def test_reobserve_counter_only_counts_its_own_candidate_and_resets_after_action(
    mode, operation, expected,
):
    work = _work()
    candidate = replace(work.frontier.candidates[0], action_type=operation, metadata={
        "action_parameters": {"target_mode": mode},
    })
    frontier = replace(work.frontier, candidates=(candidate,), observation=replace(
        work.frontier.observation, metadata={"insufficient_reobserve_count": 2},
    ))
    work = replace(work, frontier=frontier)
    decision = ActionIntent(
        task_id=work.task_id, session_id=work.session_id,
        intent_id="reobserve-counter", idempotency_key="reobserve-counter",
        surface_kind=SurfaceKind.WEB, source_capture_id=work.source_capture_id,
        action_type=operation, semantic_target=candidate.semantic_target,
        purpose="验证重观察次数边界", deadline=work.deadline,
        capture_profile=CaptureProfilePolicy.for_surface(SurfaceKind.WEB, operation),
        parameters={"candidate_id": candidate.candidate_id},
    )
    turn = ExplorerTurnResult(
        work_id=work.work_id, task_id=work.task_id, decision=decision, coordination=None,
    )
    assert _next_insufficient_reobserve_count(prior=work, turn=turn) == expected
