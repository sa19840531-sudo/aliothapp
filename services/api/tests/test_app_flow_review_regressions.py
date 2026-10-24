"""探索审查回归：跨帧保存、真实底栏、支付恢复与证据验证。"""

from dataclasses import replace

import pytest

from services.api.app.application import app_tangguo_flow_driver as driver
from services.api.app.application.app_screenshot_exploration import _app_flow_observation_lineage
from services.api.app.application.app_tangguo_flow_state import TangguoFlowState
from services.api.app.domain.exploration import ExplorationTerminal
from services.api.app.domain.interaction import InteractionOperation, TimedObservationIntent
from services.api.tests.test_tangguo_flow_driver import _tap, _work


def tab(label, selected=False):
    return _tap(
        candidate_id=label,
        label=label,
        bounds={"left": 0.1, "top": 0.9, "right": 0.2, "bottom": 0.98},
        bottom_navigation_tab_seek=True,
        selected=selected,
    )


def test_verified_tab_survives_serialized_observation():
    state = TangguoFlowState(
        phase="bottom_tabs",
        comic_flow_status="skipped",
        discovered_bottom_tabs=["首页", "发现"],
        tab_results={"首页": {"clicked": True}},
    )
    work = _work(
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(tab("首页", True), tab("发现")),
    )
    decision = driver.try_agent_app_flow_driver(work)
    assert isinstance(decision, TimedObservationIntent)
    restored = TimedObservationIntent.from_dict(decision.to_dict())
    lineage = _app_flow_observation_lineage(work, restored)
    assert lineage["tangguo_flow_state"]["visited_bottom_tabs"] == ["首页"]
    next_work = _work(
        capture_id="after-wait", metadata=lineage, candidates=(tab("首页", True), tab("发现"))
    )
    action = driver.try_agent_app_flow_driver(next_work)
    assert action.semantic_target == "发现"


def test_finished_flow_is_explicit_terminal():
    work = _work(metadata={"tangguo_flow_state": TangguoFlowState(phase="done").to_dict()})
    result = driver.try_agent_app_flow_driver(work)
    assert isinstance(result, ExplorationTerminal)
    assert "tangguo_flow_report" in result.coverage_summary


def test_active_flow_does_not_yield_to_legacy_top_sampling():
    from services.api.app.application.app_coverage_page_seek import begin_home_top_category_sample

    state = TangguoFlowState(
        phase="bottom_tabs",
        comic_flow_status="skipped",
        discovered_bottom_tabs=["首页"],
        tab_results={"首页": {"clicked": True}},
    )
    metadata = begin_home_top_category_sample({}, label="原创")
    metadata["tangguo_flow_state"] = state.to_dict()
    work = _work(metadata=metadata, candidates=(tab("首页", True),))
    assert isinstance(driver.try_agent_app_flow_driver(work), TimedObservationIntent)


def test_actual_bottom_tabs_replace_product_fallback():
    work = _work(candidates=(tab("发现", True), tab("个人中心")))
    assert [label for label, _ in driver._ordered_tabs(work)] == ["发现", "个人中心"]
    state = TangguoFlowState(phase="bottom_tabs")
    driver._verify_bottom_tab_after_click(work, state, label="发现")
    assert state.tab_results["发现"]["status"] == "passed"
    work = _work(page_type="profile", candidates=(tab("个人中心", True),))
    driver._verify_bottom_tab_after_click(work, state, label="个人中心")
    assert state.phase == "wallet_search"


@pytest.mark.parametrize("page_state", ["loading", "refreshing", "unknown", "error"])
def test_unready_bottom_content_never_passes(page_state):
    state = TangguoFlowState(retries_used=3)
    driver._verify_bottom_tab_after_click(
        _work(page_type="library", page_state=page_state), state, label="剧库"
    )
    assert state.tab_results["剧库"]["status"] == "failed"


def test_payment_return_precedes_remaining_tabs():
    state = TangguoFlowState(
        phase="payment_alipay", comic_flow_status="skipped", discovered_bottom_tabs=["专题"]
    )
    driver._finish_after_alipay_selected(state)
    work = _work(page_type="payment", metadata={"tangguo_flow_state": state.to_dict()})
    action = driver.try_agent_app_flow_driver(work)
    assert action.action_type == InteractionOperation.BACK
    assert not driver.read_tangguo_flow_state(work).blocked_bottom_tabs
    work_home = _work(
        capture_id="returned",
        candidates=(tab("专题"),),
        metadata={"tangguo_flow_state": action.parameters["tangguo_flow_state"]},
    )
    action = driver.try_agent_app_flow_driver(work_home)
    assert action.semantic_target == "专题"


def test_wallet_navigates_to_recharge_before_looking_for_alipay():
    state = TangguoFlowState(phase="payment_alipay")
    recharge = _tap(
        candidate_id="recharge",
        label="充值",
        bounds={"left": 0.1, "top": 0.2, "right": 0.5, "bottom": 0.3},
    )
    work = _work(page_type="wallet", candidates=(recharge,))
    action = driver._phase_payment_alipay(work, state)
    assert action.semantic_target == "充值"
    assert state.payment_blocked_reason is None


def test_configured_plan_selected_without_submitting_order():
    state = TangguoFlowState(phase="payment_alipay")
    plan = _tap(
        candidate_id="plan",
        label="测试套餐",
        bounds={"left": 0.1, "top": 0.2, "right": 0.5, "bottom": 0.3},
        payment_plan=True,
    )
    work = _work(
        page_type="recharge_page",
        candidates=(plan,),
        metadata={"tangguo_flow": {"test_payment_plan": "测试套餐"}},
    )
    action = driver._phase_payment_alipay(work, state)
    assert action.semantic_target == "测试套餐"


def test_last_comic_swipe_requires_fresh_evidence():
    state = TangguoFlowState(
        phase="comic_scroll",
        comic_flow_status="in_progress",
        comic_swipes_done=3,
        comic_swipes_verified=2,
        comic_swipes_effective=2,
        comic_scroll_source_capture="before",
    )
    driver._phase_comic_scroll(_work(page_type="comic_reader"), state)
    assert state.comic_flow_status != "passed"
    driver._phase_comic_scroll(
        _work(
            capture_id="after",
            page_type="comic_reader",
            metadata={"comic_scroll_content_changed": True},
        ),
        state,
    )
    assert state.comic_swipes_effective == 3
    assert state.comic_flow_status == "passed"


def test_unchanged_comic_is_not_chapter_end():
    state = TangguoFlowState(
        phase="comic_scroll",
        comic_flow_status="in_progress",
        comic_swipes_done=1,
        comic_scroll_source_capture="before",
        retries_used=3,
    )
    driver._phase_comic_scroll(
        _work(
            capture_id="after",
            page_type="comic_reader",
            metadata={"comic_scroll_content_changed": False},
        ),
        state,
    )
    assert state.comic_flow_status == "blocked"
    assert state.comic_hit_end is not True


def test_failed_top_categories_do_not_aggregate_to_passed():
    state = TangguoFlowState(
        phase="top_tabs",
        home_default_top_tab="推荐",
        visited_top_tabs=["原创", "短剧", "热门"],
        top_tab_results={name: {"status": "failed"} for name in ["原创", "短剧", "热门"]},
    )
    current = _tap(
        candidate_id="top",
        label="推荐",
        bounds={"left": 0.1, "top": 0.1, "right": 0.2, "bottom": 0.2},
        selected=True,
    )
    driver._phase_top_tabs(_work(candidates=(current,)), state)
    assert state.stage_results["top_tabs"] == "failed"


@pytest.mark.parametrize("method", ["decide_next", "decide_deterministic"])
async def test_maf_preserves_flow_wait_without_model_fallback(method):
    from services.api.app.agents.explorer import MafExplorerAgent

    class NoModel:
        async def run(self, *_args, **_kwargs):
            raise AssertionError("等待期间禁止回落模型")

    state = TangguoFlowState(
        phase="bottom_tabs",
        comic_flow_status="skipped",
        discovered_bottom_tabs=["首页"],
        tab_results={"首页": {"clicked": True}},
    )
    work = _work(metadata={"tangguo_flow_state": state.to_dict()}, candidates=(tab("首页", True),))
    decision = await getattr(MafExplorerAgent(
        agent=NoModel(), now=lambda: work.frontier.observation.observed_at,
    ), method)(work)
    assert isinstance(decision, TimedObservationIntent)
    assert decision.workflow_state["visited_bottom_tabs"] == ("首页",)


async def test_observation_workflow_persists_state_into_successor():
    from services.api.app.application.app_flow_decisions import flow_passive_decision
    from services.api.app.application.app_screenshot_exploration import (
        AppScreenshotExplorationWorkflow,
        ContinueAppScreenshotObservationCommand,
    )
    from services.api.tests import test_app_screenshot_exploration as fixtures

    observations = fixtures.MemoryVisualObservations()
    orchestrator = fixtures.RecordingOrchestrator()
    initial = await AppScreenshotExplorationWorkflow(
        understanding=fixtures.RecordingUnderstanding(fixtures._analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).run(fixtures._command())
    state = TangguoFlowState(phase="bottom_tabs", visited_bottom_tabs=["首页"])
    decision = flow_passive_decision(initial.work, state)
    decision = TimedObservationIntent.from_dict(decision.to_dict())
    turn = replace(fixtures._observation_turn(initial.work), decision=decision)
    result = await AppScreenshotExplorationWorkflow(
        understanding=fixtures.RecordingUnderstanding(fixtures._after_capture_analysis()),
        observations=observations,
        orchestrator=orchestrator,
    ).prepare_after_observation(
        ContinueAppScreenshotObservationCommand(prior_work=initial.work, turn=turn)
    )
    saved = result.work.frontier.observation.metadata["tangguo_flow_state"]
    assert saved["visited_bottom_tabs"] == ("首页",)
    assert result.work.budget.remaining_actions == initial.work.budget.remaining_actions


def test_promoted_tab_label_and_pixel_selection_are_used():
    work = _work(
        page_type="content_list",
        candidates=(tab("底栏「发现」"),),
        metadata={"surface_facts": {"pixel_selected_labels": ["发现"]}},
    )
    assert driver._ordered_tabs(work) == [("发现", ("发现",))]
    state = TangguoFlowState()
    driver._verify_bottom_tab_after_click(work, state, label="发现")
    assert state.tab_results["发现"]["status"] == "passed"


def test_other_observations_do_not_erase_flow_progress():
    from services.api.app.application.app_flow_decisions import flow_passive_decision

    state = TangguoFlowState(phase="top_tabs", visited_top_tabs=["原创"])
    work = _work(metadata={"tangguo_flow_state": state.to_dict()})
    decision = replace(
        flow_passive_decision(work, state), phase_run_id="popup:task-1", workflow_state=None
    )
    saved = _app_flow_observation_lineage(work, decision)
    assert saved["tangguo_flow_state"]["visited_top_tabs"] == ["原创"]


def test_dismiss_action_preserves_and_counts_flow_budget():
    from services.api.app.application.app_tangguo_flow_state import (
        merge_flow_state_from_intent_parameters,
    )

    state = TangguoFlowState(phase="top_tabs", device_actions_used=4)
    saved = merge_flow_state_from_intent_parameters(
        prior_metadata={"tangguo_flow_state": state.to_dict()},
        parameters={"bounded_dismiss_tool": True},
    )["tangguo_flow_state"]
    assert saved["phase"] == "top_tabs"
    assert saved["device_actions_used"] == 5
