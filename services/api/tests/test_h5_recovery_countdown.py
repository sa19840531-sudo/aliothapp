"""来源恢复也须消费已有倒计时观察合同，并保留原预算。"""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_exploration_observation import build_h5_frontier
from services.api.app.application.h5_exploration_workflow import _with_task_frontier
from services.api.app.application.h5_runtime_composition import (
    H5CancellationCheckpoint,
    _CoordinatorRecoveryBrowser,
)
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
from services.api.app.domain.interaction import CaptureKind, CaptureRef, SurfaceKind
from services.api.tests.test_h5_exploration_turn import _work


def _detection(capture_id, *, close=False):
    return {
        "schema_version": "page-detection.v2", "page_capture_id": capture_id,
        "page_url": "https://m.example.test/", "detection_status": "recognized",
        "page_type": "startup_ad", "page_state": "popup", "confidence": 0.98,
        "reason": "全屏倒计时广告", "risk_flags": [],
        "evidence_artifact_ids": [f"artifact:{capture_id}"],
        "candidate_actions": [{
            "action": "click", "target": "关闭当前弹窗", "navigation_depth": 0,
            "risk_level": "low", "purpose": "关闭当前可命中的广告弹层",
            "target_keywords": ["popup_dismissal", "close_control", "rendered_modal_close"],
            "obstruction_capability": "rendered_modal_close",
            "obstruction_signature": "h5-obstruction:" + "7" * 64,
        }] if close else [],
    }


def test_startup_ad_preserves_verified_generic_modal_close_candidate():
    work = _work()
    frontier = build_h5_frontier(
        detection_result=_detection("capture-ad", close=True),
        observed_at=work.frontier.observation.observed_at, navigation_depth=0,
    )
    assert [c.action_type.value for c in frontier.candidates] == ["click"]
    assert frontier.candidates[0].metadata["action_parameters"]["target_mode"] == "popup_dismissal"


@pytest.mark.parametrize("boundary", [
    "opens", "never_opens", "unknown", "no_budget", "other_location", "other_capture",
    "last_action", "business_page",
    "time_exhausted", "split_pool_exhausted",
])
async def test_recovery_waits_in_same_session_and_debits_existing_budget(boundary):
    base = _work()
    if boundary == "no_budget":
        base = replace(base, budget=replace(
            base.budget, remaining_actions=0, remaining_business_actions=0,
        ))
    elif boundary == "last_action":
        base = replace(base, budget=replace(
            base.budget, remaining_actions=1, remaining_business_actions=1,
        ))
    elif boundary == "time_exhausted":
        base = replace(base, budget=replace(base.budget, remaining_duration_ms=5))
    elif boundary == "split_pool_exhausted":
        base = replace(base, budget=replace(
            base.budget, max_business_actions=base.budget.max_actions - 1,
            remaining_business_actions=base.budget.remaining_actions - 1,
            max_obstruction_actions=1, remaining_obstruction_actions=1,
        ))
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=base.source_capture_id,
        source_state_version=base.frontier.observation.state_version,
        source_location=base.frontier.observation.location, navigation_depth=0,
        candidates=base.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(frontier=base.frontier, snapshot=snapshot)
    work = replace(base, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)
    capture = CaptureRef(
        task_id=work.task_id, session_id=work.session_id, capture_id="capture-ad",
        surface_kind=SurfaceKind.WEB, capture_kind=CaptureKind.PAGE, role="after_action",
    )
    intents = []
    clock = [work.frontier.observation.observed_at]

    async def coordinate(command):
        intents.append(command.intent)
        assert command.intent.session_id == work.session_id
        assert command.intent.deadline == work.deadline
        return SimpleNamespace(after_capture=replace(capture, capture_id=f"capture-{len(intents)}"))

    async def detect(*, capture, task_id, agent_run_id):
        result = _detection(capture.capture_id, close=boundary in {
            "opens", "last_action", "time_exhausted", "split_pool_exhausted",
        })
        if boundary == "time_exhausted":
            clock[0] += timedelta(milliseconds=10)
        if boundary == "unknown":
            result["detection_status"] = "unknown"
        elif boundary == "other_location":
            result["page_url"] = "https://other.example.test/"
        elif boundary == "other_capture":
            result["page_capture_id"] = "capture-other"
        elif boundary == "business_page":
            result.update(page_type="home", page_state="stable")
        return result

    browser = _CoordinatorRecoveryBrowser(
        work=work, agent_run_id="run-h5",
        capture_coordinator=SimpleNamespace(coordinate=coordinate),
        detector=SimpleNamespace(detect=detect), assessment_repository=SimpleNamespace(),
        cancellation=H5CancellationCheckpoint(),
        now=lambda: clock[0],
        source_state_version=work.frontier.observation.state_version,
    )
    result = await browser.dismiss_startup_ad(
        capture=capture, detection=_detection(capture.capture_id),
    )
    if boundary == "opens":
        assert result.capture_id == "capture-2"
        assert [i.action_type.value for i in intents] == ["observe", "click"]
        assert intents[1].source_capture_id == "capture-1"
        assert browser.remaining_budget.remaining_actions == work.budget.remaining_actions - 1
    elif boundary == "business_page":
        assert result.capture_id == "capture-1"
        assert [i.action_type.value for i in intents] == ["observe"]
    else:
        assert result is None
        assert all(i.action_type.value == "observe" for i in intents)
        expected = (
            0 if boundary == "no_budget" else min(10, work.budget.remaining_actions)
            if boundary == "never_opens" else 1
        )
        assert len(intents) == expected
        assert browser.remaining_budget.remaining_actions == (
            work.budget.remaining_actions - expected
        )
    assert browser.remaining_budget.max_actions == work.budget.max_actions
    if boundary == "time_exhausted":
        assert browser.remaining_budget.remaining_duration_ms == 0
        assert browser.remaining_budget.business_duration_floor_applied == 0
    if boundary == "split_pool_exhausted":
        assert browser.remaining_budget.remaining_obstruction_actions == 0
        assert browser.remaining_budget.remaining_business_actions == (
            work.budget.remaining_business_actions
        )


async def test_recovery_wait_has_no_business_duration_floor_or_fresh_deadline():
    work = _work()
    browser = _CoordinatorRecoveryBrowser(
        work=replace(work, deadline=work.frontier.observation.observed_at),
        agent_run_id="run-h5", capture_coordinator=SimpleNamespace(coordinate=AsyncMock()),
        detector=SimpleNamespace(), assessment_repository=SimpleNamespace(),
        cancellation=H5CancellationCheckpoint(), now=lambda: work.frontier.observation.observed_at,
        source_state_version=work.frontier.observation.state_version,
    )
    capture = CaptureRef(
        task_id=work.task_id, session_id=work.session_id, capture_id="capture-ad",
        surface_kind=SurfaceKind.WEB, capture_kind=CaptureKind.PAGE, role="after_action",
    )
    assert await browser.dismiss_startup_ad(
        capture=capture, detection=_detection(capture.capture_id),
    ) is None
    browser._capture_coordinator.coordinate.assert_not_called()


async def test_failed_recovery_returns_debited_observation_budget(monkeypatch):
    """恢复失败重新排队时，不能丢失已经消费的观察动作预算。"""
    from services.api.app.application import h5_runtime_composition as module
    from services.api.app.application.h5_exploration_recovery import H5RestorationResult
    from services.api.app.domain.exploration import consume_exploration_action_budget

    base = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=base.source_capture_id,
        source_state_version=base.frontier.observation.state_version,
        source_location=base.frontier.observation.location, navigation_depth=0,
        candidates=base.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    frontier = _with_task_frontier(frontier=base.frontier, snapshot=snapshot)
    work = replace(base, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)

    async def restore(*, entry, current_location, browser, deadline, now):
        assert deadline == work.deadline
        assert now() == work.frontier.observation.observed_at
        browser.remaining_budget = consume_exploration_action_budget(
            work.budget, counts_toward_business=False, elapsed_duration_ms=100,
        )
        return H5RestorationResult(
            entry_id=entry.entry_id, restored=False, restoration_attempt_count=1,
            reason_code="h5_frontier_restore_startup_ad_blocked",
        )

    monkeypatch.setattr(module, "restore_h5_frontier_source", restore)
    result = await module.H5CoordinatorFrontierRecovery(
        capture_coordinator=SimpleNamespace(), detector=SimpleNamespace(),
        assessment_repository=SimpleNamespace(), now=lambda: work.frontier.observation.observed_at,
    ).restore(work=work, agent_run_id="run-h5", entry=snapshot.entries[0])
    assert not result.restored
    assert result.work.budget.remaining_actions == work.budget.remaining_actions - 1
    assert result.work.budget.remaining_duration_ms == work.budget.remaining_duration_ms - 100
