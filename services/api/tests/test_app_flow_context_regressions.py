"""全链路上下文审查：预算传递、子流程接续、局部恢复和最终验证。"""

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application import app_tangguo_flow_driver as driver
from services.api.app.application.app_flow_phase_budget import charge_phase_time
from services.api.app.application.app_screenshot_exploration import _app_flow_action_lineage
from services.api.app.application.app_tangguo_flow_state import ResumeCheckpoint, TangguoFlowState
from services.api.app.application.app_three_contract_driver import decide_app_three_contracts
from services.api.app.domain.exploration import ExplorationTerminal
from services.api.app.domain.interaction import ActionIntent, TimedObservationIntent
from services.api.tests.test_app_three_contract_driver import candidate, work_for


@pytest.mark.parametrize("script", [False])
def test_first_action_carries_budget_to_successor_without_existing_state(script):
    work = work_for(
        package="com.dzu4.app" if script else "com.alz7xm2.drama",
        page_state="stable" if script else "popup",
        candidates=(candidate("关闭广告", control_role="close"),),
    )
    if script:
        from services.api.app.application.app_product_orchestration_policy import (
            try_product_script_override,
        )
        action = decide_app_three_contracts(work, scripted=try_product_script_override(work))
    else:
        action = decide_app_three_contracts(work)
    assert "tangguo_flow_state" not in work.frontier.observation.metadata
    assert isinstance(action, ActionIntent)
    lineage = _app_flow_action_lineage(work, action)
    saved = TangguoFlowState.from_dict(lineage["tangguo_flow_state"])
    assert saved.device_actions_used == 1
    assert saved.contract_runtime["rounds"] == 1
    assert saved.started_at_iso


def test_comic_return_reclicks_pending_tab_before_verification():
    state = TangguoFlowState(
        phase="comic_back_home", comic_flow_status="passed",
        discovered_bottom_tabs=["首页", "漫画"], visited_bottom_tabs=["首页"],
        tab_results={"首页": {"status": "passed", "clicked": True}, "漫画": {"clicked": True}},
        resume_checkpoint=ResumeCheckpoint(phase="bottom_tabs", remaining_bottom_tabs=["漫画"]),
    )
    work = work_for(state=state, candidates=(
        candidate("首页", bottom_navigation_tab_seek=True, selected=True),
        candidate("漫画", bottom_navigation_tab_seek=True, selected=False),
    ))
    action = decide_app_three_contracts(work)
    assert isinstance(action, ActionIntent)
    assert action.semantic_target == "漫画"
    saved = action.parameters["tangguo_flow_state"]
    assert not saved["failed_bottom_tabs"]
    assert saved["tab_results"]["漫画"]["clicked"] is True


def test_parent_clock_does_not_charge_child_duration_or_reset_parent_total():
    start = datetime.now(UTC)
    runtime = {}
    assert charge_phase_time(runtime, "top_tabs:0", start) == 0
    assert charge_phase_time(runtime, "comic_detail", start + timedelta(seconds=20)) == 0
    assert charge_phase_time(runtime, "top_tabs:0", start + timedelta(seconds=700)) == 20
    assert charge_phase_time(runtime, "top_tabs:0", start + timedelta(seconds=710)) == 30


def test_return_from_child_does_not_time_out_parent():
    now = datetime.now(UTC)
    state = TangguoFlowState(
        phase="top_tabs", comic_flow_status="passed", started_at_iso=now.isoformat(),
        contract_runtime={
            "active_phase_key": "comic_back_home",
            "active_since": (now - timedelta(seconds=700)).isoformat(),
            "phase_elapsed_seconds": {"top_tabs:0": 20},
        },
    )
    result = decide_app_three_contracts(work_for(state=state), now=now)
    assert isinstance(result, TimedObservationIntent)
    assert result.workflow_state["stop_reason"] is None


def test_local_tab_limit_marks_only_that_tab_and_continues_next():
    state = TangguoFlowState(
        phase="bottom_tabs", comic_flow_status="skipped",
        discovered_bottom_tabs=["剧库", "专题"], pending_bottom_tabs=["剧库", "专题"],
        contract_runtime={"rounds_by_phase": {"bottom_tabs:剧库": 12}},
    )
    action = decide_app_three_contracts(work_for(state=state, candidates=(
        candidate("剧库", bottom_navigation_tab_seek=True),
        candidate("专题", bottom_navigation_tab_seek=True),
    )))
    assert isinstance(action, ActionIntent)
    assert action.semantic_target == "专题"
    assert action.parameters["tangguo_flow_state"]["blocked_bottom_tabs"] == ("剧库",)


def test_retry_budgets_are_scoped_and_global_cap_still_applies():
    state = TangguoFlowState(phase="video_verify")
    for _ in range(3):
        assert state.consume_retry()
    assert not state.can_retry()
    state.advance("bottom_tabs")
    state.pending_bottom_tabs = ["剧库"]
    assert state.consume_retry()
    restored = TangguoFlowState.from_dict(state.to_dict())
    restored.pending_bottom_tabs = ["专题"]
    assert restored.consume_retry()
    restored.retries_used = 30
    assert not restored.can_retry()


@pytest.mark.parametrize("page_state", ["loading", "unknown", "error"])
def test_unstable_final_home_is_observed_not_passed(page_state):
    result = decide_app_three_contracts(work_for(
        state=TangguoFlowState(phase="final_home"), page_state=page_state,
    ))
    assert isinstance(result, TimedObservationIntent)
    assert "final_home" not in result.workflow_state["stage_results"]


def test_unknown_bottom_tabs_are_not_invented():
    state = TangguoFlowState(phase="bottom_tabs")
    work = work_for()
    assert driver._ordered_tabs(work) == []
    for _ in range(4):
        driver._phase_bottom_tabs(work, state)
    assert state.discovered_bottom_tabs == []
    assert state.stage_results["bottom_tabs"] == "blocked"
    assert state.phase == "final_home"


def test_failed_comic_return_cannot_resume_on_reader_page():
    state = TangguoFlowState(phase="comic_back_home", comic_flow_status="passed")
    state.comic_back_home_attempts = state.budgets.comic_back_home_max
    result = decide_app_three_contracts(work_for(state=state, page_type="comic_reader"))
    assert isinstance(result, ExplorationTerminal)
    assert result.reason == "comic_back_home_exhausted"
