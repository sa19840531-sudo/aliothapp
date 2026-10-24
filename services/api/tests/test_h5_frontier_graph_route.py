"""同 URL 的栏目恢复必须消费可信历史路线并重新核对实际现场。"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_exploration_recovery import restore_h5_frontier_source
from services.api.app.application.h5_frontier_graph_route import H5FrontierGraphOutcome
from services.api.tests.test_h5_exploration_turn import _DriftingBrowser, _dynamic_restore_entry


async def test_frontier_restoration_replays_column_path_before_rejecting_source():
    entry, expected = _dynamic_restore_entry(source_page_type="content_list")
    browser = _DriftingBrowser({**expected, "page_type": "home"})
    capture = {"capture_id": "capture-restored-column"}
    browser.restore_source_path = AsyncMock(return_value=H5FrontierGraphOutcome(
        capture, {**expected, "page_capture_id": capture["capture_id"]}, True,
    ))
    result = await restore_h5_frontier_source(
        entry=entry, current_location=entry.source_location, browser=browser,
    )
    assert result.restored is True
    assert result.capture == capture
    assert browser.restore_source_path.await_count == 1


@pytest.mark.parametrize("fault", ["wrong_column", "unavailable", "failure"])
async def test_graph_restoration_does_not_turn_failure_into_source_equivalence(fault):
    entry, expected = _dynamic_restore_entry(source_page_type="content_list")
    browser = _DriftingBrowser({**expected, "page_type": "home"})
    browser.restore_source_path = AsyncMock(return_value=None)
    if fault == "wrong_column":
        browser.restore_source_path.return_value = H5FrontierGraphOutcome(
            {"capture_id": "still-home"},
            {**expected, "page_capture_id": "still-home", "page_type": "home"},
            True,
        )
    elif fault == "failure":
        browser.restore_source_path.side_effect = RuntimeError("恢复图暂不可读")
    result = await restore_h5_frontier_source(
        entry=entry, current_location=entry.source_location, browser=browser,
    )
    assert result.restored is False


def _graph_fixture():
    scope = {"task_id": "task", "agent_run_id": "run", "finalization_epoch": 0}
    states = []
    for sid, identity in [("current", "home-default"), ("historical", "home-default"),
                          ("column", "home-column")]:
        states.append({
            **scope, "state_id": sid, "logical_page_key": identity,
            "state_fingerprint": f"state:{sid}", "surface_session_id": "h5:run",
            "identity_status": "confirmed", "counts_as_business_page": True,
            "is_overlay": False, "is_external": False, "surface_kind": "web",
            "state_payload": {"url": "https://example.test/home",
                              "logical_surface": {"identity_fingerprint": identity}},
        })
    graph = {
        **scope, "status": "available", "target_platform": "h5", "states": states,
        "transitions": [{
            **scope, "transition_id": "edge", "source_state_id": "historical",
            "target_state_id": "column", "action_kind": "click",
            "semantic_target": "栏目入口", "status": "completed", "proof": "trusted",
            "replay_eligible": True, "risk_level": "low",
        }],
    }
    entry, _ = _dynamic_restore_entry(source_page_type="content_list")
    entry = replace(entry, source_capture_id="column", candidate=replace(
        entry.candidate, metadata={**entry.candidate.metadata,
                                   "source_identity_fingerprint": "home-column"},
    ))
    return graph, entry


def _plan(graph, entry):
    from services.api.app.application.h5_frontier_graph_route import plan_h5_frontier_graph_steps

    return plan_h5_frontier_graph_steps(
        graph=graph, entry=entry, task_id="task", agent_run_id="run", session_id="h5:run",
        finalization_epoch=0, current_capture_id="current",
    )


def test_graph_restore_reuses_trusted_source_rebind_and_semantic_route():
    graph, entry = _graph_fixture()
    steps = _plan(graph, entry)
    assert len(steps) == 1
    assert steps[0].source_identity == "home-default"
    assert steps[0].target_identity == "home-column"
    assert steps[0].semantic_target == "栏目入口"


def test_graph_restore_preserves_real_before_after_ids_between_multiple_actions():
    graph, entry = _graph_fixture()
    column = graph["states"][-1]
    before = {**column, "state_id": "before-column"}
    deep = deepcopy(column)
    deep.update({"state_id": "deep", "logical_page_key": "home-detail",
                 "state_fingerprint": "state:deep"})
    deep["state_payload"]["logical_surface"]["identity_fingerprint"] = "home-detail"
    graph["states"].extend([before, deep])
    graph["transitions"].append({
        **graph["transitions"][0], "transition_id": "edge-deep",
        "source_state_id": "before-column", "target_state_id": "deep",
        "semantic_target": "详情入口",
    })
    entry = replace(entry, source_capture_id="deep", candidate=replace(
        entry.candidate, metadata={**entry.candidate.metadata,
                                   "source_identity_fingerprint": "home-detail"},
    ))
    steps = _plan(graph, entry)
    assert len(steps) == 2
    assert steps[1].source_state_id == "before-column"
    assert steps[0].target_state_id == "column"


@pytest.mark.parametrize(
    "fault", ["task", "run", "epoch", "web", "session", "overlay", "untrusted",
              "risky", "missing_semantics", "wrong_identity", "unknown_current"],
)
def test_graph_restore_rejects_untrusted_or_out_of_scope_routes(fault):
    graph, entry = _graph_fixture()
    graph = deepcopy(graph)
    if fault in {"task", "run", "epoch"}:
        graph[{"task": "task_id", "run": "agent_run_id", "epoch": "finalization_epoch"}[fault]] = (
            1 if fault == "epoch" else "other"
        )
    elif fault == "web":
        graph["target_platform"] = "web"
    elif fault == "session":
        graph["states"][-1]["surface_session_id"] = "h5:other-run"
    elif fault == "overlay":
        graph["states"][-1]["is_overlay"] = True
    elif fault == "untrusted":
        graph["transitions"][0]["proof"] = "insufficient"
    elif fault == "risky":
        graph["transitions"][0]["risk_level"] = "high"
    elif fault == "missing_semantics":
        graph["transitions"][0]["semantic_target"] = None
    elif fault == "wrong_identity":
        graph["states"][-1]["state_payload"]["logical_surface"]["identity_fingerprint"] = "other"
    else:
        graph["states"][0]["identity_status"] = "unknown"
    assert _plan(graph, entry) == ()


def _runtime_fixture(monkeypatch):
    from services.api.app.application import h5_runtime_composition as composition
    from services.api.app.application.evidence_recovery_replay import RecoveryGraphStep
    from services.api.app.application.h5_exploration_cancellation import H5CancellationCheckpoint
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
    from services.api.app.domain.interaction import InteractionResultStatus
    from services.api.tests.test_h5_exploration_turn import _capture_for_turn, _work

    work = _work()
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location, navigation_depth=0,
        candidates=work.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    entry = next(item for item in snapshot.entries if item.status.value == "selected")
    stored_frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    work = replace(work, frontier=stored_frontier, frontier_snapshot_id=stored_frontier.snapshot_id)
    capture = _capture_for_turn(work, "current")
    after = _capture_for_turn(work, "after-column")
    candidate = replace(work.frontier.candidates[0], candidate_id="fresh-column")
    frontier = replace(work.frontier, candidates=(candidate,), observation=replace(
        work.frontier.observation, capture_id="current", page_state="stable",
        metadata={"logical_surface_identity_fingerprint": "home-default",
                  "detection_status": "recognized"},
    ))
    step = RecoveryGraphStep(
        transition_id="edge", source_state_id="historical", target_state_id="column",
        action_kind="click", semantic_target=candidate.semantic_target,
        source_identity="home-default", source_state_fingerprint="historical-state",
        target_identity="home-column", target_state_fingerprint="column-state",
    )
    monkeypatch.setattr(composition, "build_h5_frontier", lambda **_: frontier)
    monkeypatch.setattr(composition, "plan_h5_frontier_graph_steps", lambda **_: (step,))
    parameter_calls = []

    def parameters(**kwargs):
        parameter_calls.append(kwargs)
        return {"candidate_id": kwargs["candidate"].candidate_id}

    monkeypatch.setattr(composition, "_h5_candidate_parameters", parameters)
    now = work.frontier.observation.observed_at
    session = SimpleNamespace(
        task_id=work.task_id, surface_kind=work.surface_kind, finalization_epoch=0,
        status=SimpleNamespace(value="active"), session_owner="h5-workflow",
        current_capture_id="current",
    )
    clock = [now]
    receipt = SimpleNamespace(after_capture=after, interaction=SimpleNamespace(
        status=InteractionResultStatus.SUCCEEDED,
    ))

    async def coordinate(command):
        clock[0] = now + timedelta(milliseconds=250)
        return receipt

    coordinator = SimpleNamespace(coordinate=AsyncMock(side_effect=coordinate))
    browser = composition._CoordinatorRecoveryBrowser(
        work=work, agent_run_id="run-h5", capture_coordinator=coordinator,
        detector=object(), assessment_repository=object(), cancellation=H5CancellationCheckpoint(),
        now=lambda: clock[0], source_state_version=entry.source_state_version,
        surface_sessions=SimpleNamespace(get=AsyncMock(return_value=session)),
        page_graph=SimpleNamespace(get_graph=AsyncMock(return_value={})),
        h5_visual_coordinate_mode="enforced",
    )
    browser.detect = AsyncMock(return_value={
        "page_capture_id": after.capture_id,
        "logical_surface": {"identity_fingerprint": "home-column"},
    })
    browser.assess = AsyncMock(return_value=SimpleNamespace(
        execution_status="accepted", page_type_assessment={
            "status": "covered", "page_type": "home", "reason_code": "page_confirmed",
            "page_capture_ids": ["after-column"],
            "evidence_artifact_ids": ["artifact"], "page_states": ["stable"],
        },
    ))
    return browser, entry, capture, frontier, receipt, session, parameter_calls


async def test_runtime_graph_replay_binds_fresh_candidate_and_debits_original_budget(monkeypatch):
    browser, entry, capture, _, _, _, calls = _runtime_fixture(monkeypatch)
    result = await browser.restore_source_path(entry=entry, capture=capture, detection={})
    assert result.arrived is True
    assert result.capture.capture_id == "after-column"
    command = browser._capture_coordinator.coordinate.call_args.args[0]
    assert command.intent.source_capture_id == "current"
    assert command.intent.parameters["candidate_id"] == "fresh-column"
    assert command.intent.parameters["h5_frontier_graph_transition_id"] == "edge"
    assert "evidence_request_id" not in command.intent.parameters
    assert command.intent.deadline == browser._work.deadline
    assert len(command.intent.intent_id) < 128
    assert calls[0]["work"].source_capture_id == "current"
    assert calls[0]["h5_visual_coordinate_mode"] == "enforced"
    assert browser.remaining_budget.remaining_actions == 2
    assert browser.remaining_budget.remaining_duration_ms == 29_750


@pytest.mark.parametrize("fault", [
    "ambiguous", "blocked", "source_identity", "source_capture", "overlay", "auth",
    "unstable", "no_budget", "no_time", "deadline", "session_owner", "session_closed",
    "session_capture", "session_epoch",
])
async def test_runtime_graph_replay_refuses_unproven_or_unbounded_actions(monkeypatch, fault):
    from services.api.app.application import h5_runtime_composition as composition

    browser, entry, capture, frontier, _, session, _ = _runtime_fixture(monkeypatch)
    if fault == "ambiguous":
        frontier = replace(frontier, candidates=(frontier.candidates[0], replace(
            frontier.candidates[0], candidate_id="other",
        )))
    elif fault == "blocked":
        frontier = replace(frontier, blocked_candidate_ids=(frontier.candidates[0].candidate_id,))
    elif fault in {"source_identity", "source_capture", "overlay", "auth", "unstable"}:
        values = {
            "source_identity": {"metadata": {"logical_surface_identity_fingerprint": "other"}},
            "source_capture": {"capture_id": "other"}, "overlay": {"overlay_signature": "modal"},
            "auth": {"metadata": {**frontier.observation.metadata, "requires_auth": True}},
            "unstable": {"page_state": "loading"},
        }
        frontier = replace(frontier, observation=replace(frontier.observation, **values[fault]))
    elif fault == "no_budget":
        browser.remaining_budget = replace(browser.remaining_budget, remaining_actions=0,
                                           remaining_business_actions=0)
    elif fault == "no_time":
        browser.remaining_budget = replace(browser.remaining_budget, remaining_duration_ms=0)
    elif fault == "deadline":
        browser._now = lambda: browser._work.deadline
    elif fault == "session_owner":
        session.session_owner = "other"
    elif fault == "session_closed":
        session.status.value = "closed"
    elif fault == "session_capture":
        session.current_capture_id = "other"
    else:
        browser._surface_sessions.get.side_effect = [session, SimpleNamespace(
            **{**vars(session), "finalization_epoch": 1},
        )]
    monkeypatch.setattr(composition, "build_h5_frontier", lambda **_: frontier)
    before = browser.remaining_budget
    result = await browser.restore_source_path(entry=entry, capture=capture, detection={})
    assert result is None or result.arrived is False
    browser._capture_coordinator.coordinate.assert_not_awaited()
    assert browser.remaining_budget == before


@pytest.mark.parametrize("fault", ["action", "target", "detection", "assessment"])
async def test_runtime_failed_graph_step_preserves_latest_capture_and_consumes_budget(
    monkeypatch, fault,
):
    from services.api.app.domain.interaction import InteractionResultStatus

    browser, entry, capture, _, receipt, _, _ = _runtime_fixture(monkeypatch)
    if fault == "action":
        receipt.interaction.status = InteractionResultStatus.FAILED
    elif fault == "target":
        browser.detect.return_value["logical_surface"]["identity_fingerprint"] = "other"
    elif fault == "detection":
        browser.detect.side_effect = RuntimeError("检测暂不可用")
    else:
        browser.assess.return_value.execution_status = "blocked"
    result = await browser.restore_source_path(entry=entry, capture=capture, detection={})
    assert result.arrived is False
    assert result.capture.capture_id == "after-column"
    assert browser.remaining_budget.remaining_actions == 2
    assert browser.remaining_budget.remaining_duration_ms == 29_750


async def test_failed_replay_receipt_is_not_overridden_by_matching_final_page():
    entry, expected = _dynamic_restore_entry(source_page_type="content_list")
    browser = _DriftingBrowser({**expected, "page_type": "home"})
    capture = {"capture_id": "actual-after"}
    browser.restore_source_path = AsyncMock(return_value=H5FrontierGraphOutcome(
        capture, {**expected, "page_capture_id": "actual-after"}, False,
    ))
    result = await restore_h5_frontier_source(
        entry=entry, current_location=entry.source_location, browser=browser,
    )
    assert result.restored is False
    assert result.capture == capture


async def test_runtime_graph_replay_preserves_after_capture_when_coordinator_fails(monkeypatch):
    from services.api.app.application.capture_coordination import (
        CaptureCoordinationError,
        CaptureCoordinationStage,
    )

    browser, entry, capture, _, receipt, _, _ = _runtime_fixture(monkeypatch)
    browser._capture_coordinator.coordinate.side_effect = CaptureCoordinationError(
        stage=CaptureCoordinationStage.AFTER_CAPTURE, error_code="TEST_FAILURE", message="测试失败",
        before_capture=capture, after_capture=receipt.after_capture,
    )
    browser.detect.side_effect = RuntimeError("暂不可检测")
    result = await browser.restore_source_path(entry=entry, capture=capture, detection={})
    assert result.arrived is False and result.capture == receipt.after_capture
    assert result.detection is None
    assert browser.remaining_budget.remaining_actions == 2


async def test_runtime_graph_replay_rebinds_each_step_and_cannot_reset_budget(monkeypatch):
    from services.api.app.application import h5_runtime_composition as composition
    from services.api.app.application.evidence_recovery_replay import RecoveryGraphStep

    browser, entry, capture, frontier, receipt, session, calls = _runtime_fixture(monkeypatch)
    second = replace(receipt.after_capture, capture_id="last-column")
    steps = tuple(RecoveryGraphStep(
        transition_id=f"edge-{i}", source_state_id=f"state-{i}",
        target_state_id=f"state-{i+1}", action_kind="click", semantic_target="我的",
        source_identity="home-default" if i == 0 else "home-column",
        target_identity="home-column", source_state_fingerprint=f"fp-{i}",
        target_state_fingerprint=f"fp-{i+1}",
    ) for i in range(2))
    monkeypatch.setattr(composition, "plan_h5_frontier_graph_steps", lambda **_: steps)
    second_frontier = replace(frontier, snapshot_id="frontier-second", observation=replace(
        frontier.observation, capture_id="after-column", metadata={
            "logical_surface_identity_fingerprint": "home-column", "detection_status": "recognized",
        },
    ), candidates=(replace(frontier.candidates[0], candidate_id="second-fresh"),))
    monkeypatch.setattr(composition, "build_h5_frontier",
                        lambda **kwargs: frontier if kwargs["detection_result"].get(
                            "page_capture_id") == "current" else second_frontier)
    now = browser._now()

    async def coordinate(command):
        is_second = command.intent.source_capture_id == "after-column"
        browser._now = lambda: now + timedelta(milliseconds=250 if not is_second else 500)
        session.current_capture_id = "after-column" if not is_second else "last-column"
        return SimpleNamespace(
            after_capture=second if is_second else receipt.after_capture,
            interaction=receipt.interaction,
        )

    browser._capture_coordinator.coordinate.side_effect = coordinate
    browser.detect.side_effect = lambda **kw: {
        "page_capture_id": kw["capture"].capture_id,
        "logical_surface": {"identity_fingerprint": "home-column"},
    }
    result = await browser.restore_source_path(
        entry=entry, capture=capture, detection={"page_capture_id": "current"},
    )
    assert result.arrived is True and result.capture.capture_id == "last-column"
    assert [call["work"].source_capture_id for call in calls] == ["current", "after-column"]
    assert [call["candidate"].candidate_id for call in calls] == ["fresh-column", "second-fresh"]
    intents = [call.args[0].intent
               for call in browser._capture_coordinator.coordinate.call_args_list]
    assert len(set(item.intent_id for item in intents)) == 2
    assert browser.remaining_budget.remaining_actions == 1
    assert browser.remaining_budget.remaining_duration_ms == 29_500
