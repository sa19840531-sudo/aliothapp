"""同一现场多次采集的连续补证；保存031时序不是原子旧帧恢复许可。"""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_targeted_recapture import (
    H5StateEquivalenceGate,
    H5TargetedRecapturePlanService,
)
from services.api.app.domain.inspection import (
    RestorationStrategy,
    StateInvariantSnapshot,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.tests.test_h5_targeted_recapture import NOW, _request


@pytest.mark.parametrize("change,allowed", [
    ({}, True),
    ({"current_document_identity": "another-document"}, False),
    ({"target_document_identity": None}, False),
    ({"current_state_fingerprint": "b" * 64}, False),
    ({"current_state_fingerprint": None}, False),
    ({"current_identity": "logical:another"}, False),
    ({"current_url": "https://m.example.test/#other"}, False),
    ({"source_session_closed": True}, False),
])
async def test_strong_origin_continues_new_capture_only_with_document_and_state_proof(
    change, allowed,
):
    context = SimpleNamespace(**{
        "task_id": "task-h5", "agent_run_id": "run-h5", "finalization_epoch": 4,
        "session_id": "h5:run-h5", "current_capture_id": "capture-new-frame",
        "target_identity": "logical:home", "current_identity": "logical:home",
        "target_url": "https://m.example.test/", "current_url": "https://m.example.test/",
        "source_session_closed": False, "target_state_id": "capture-target",
        "target_document_identity": "original-document",
        "current_document_identity": "original-document",
        "target_state_fingerprint": "a" * 64, "current_state_fingerprint": "a" * 64,
        **change,
    })
    graph = SimpleNamespace(get_recovery_route=AsyncMock())
    result = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=context)), page_graph=graph,
        clock=lambda: NOW,
    ).resolve(request=_request(), agent_run_id="run-h5", finalization_epoch=4)
    assert result.executable is allowed
    if allowed:
        assert result.plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN
        assert result.plan.source_session_id == result.plan.target_session_id == context.session_id
        assert result.plan.deadline == NOW + timedelta(minutes=3)
        assert not result.plan.normalization_step_ids
    graph.get_recovery_route.assert_not_awaited()


@pytest.mark.parametrize("change,allowed", [
    ({}, True),
    ({"document_identity": "another-document"}, False),
    ({"document_identity": None}, False),
    ({"logical_state_fingerprint": "b" * 64}, False),
])
def test_strong_origin_rechecks_document_after_observe_even_when_request_omits_it(change, allowed):
    request = _request()
    plan = build_restoration_plan(
        request=request, source_session_id="h5:run-h5", target_session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB, strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
        deadline=NOW + timedelta(minutes=3),
    )
    original = StateInvariantSnapshot(
        task_id=request.task_id, capture_id=request.target_capture_id, session_id="h5:run-h5",
        surface_kind=SurfaceKind.WEB, captured_at=NOW,
        environment={"surface_kind": "web"}, business={"page_type": "home"},
        semantic={}, structure={"layer_target": "base_page", "document_identity": "doc",
                                "logical_state_fingerprint": "a" * 64},
    )
    restored = replace(original, capture_id="new", captured_at=NOW + timedelta(seconds=1),
                       structure={**original.structure, **change})
    assessment = H5StateEquivalenceGate().assess(
        request=request, plan=plan, original=original, restored=restored,
        restoration_attempt_id="attempt-continuation",
    )
    assert assessment.passed is allowed


def test_saved_identical_frames_are_not_permission_to_invent_price_or_atomic_old_evidence():
    fixture = json.loads((Path(__file__).parent / "fixtures" /
                          "h5_031_price_20261012_170654.json").read_text())
    assert fixture["verified_artifacts"]["same_hash_for_all_three"]
    assert len({fixture[key] for key in (
        "before_capture_id", "after_capture_id", "last_capture_id",
    )}) == 3
    assert fixture["verified_artifacts"]["dom_price_fragments"] == 0
    assert fixture["input_contract"]["recapture_policy"]["kind"] == "exact_state"
