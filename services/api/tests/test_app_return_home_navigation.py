"""返回首页后保留真实底栏，不套用产品模板或让全局调度接管终态。"""

import pytest

from services.api.app.application.app_exploration_turn import AppExplorationTurnRunner
from services.api.app.application.app_flow_phase_budget import recover_phase_limit
from services.api.app.application.app_global_frontier import AppGlobalFrontierService
from services.api.app.application.app_tangguo_flow_state import TangguoFlowState
from services.api.app.application.app_three_contract_driver import decide_app_three_contracts
from services.api.app.application.app_visual_bottom_nav_calibration import (
    calibrate_bottom_navigation_candidates,
    observed_bottom_nav_labels,
)
from services.api.app.domain.exploration import AppVisualCandidate, NormalizedBounds
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
from services.api.tests.test_app_three_contract_driver import candidate, work_for
from services.api.tests.test_app_visual_bottom_nav_calibration import (
    _analysis_for_png,
    _png_with_bottom_nav,
)


def test_five_visible_tabs_never_become_other_product_six_tabs():
    png = _png_with_bottom_nav(tabs=5)
    base = _analysis_for_png(png)
    labels = ("首页", "剧库", "投票", "专题", "我的")
    items = tuple(AppVisualCandidate.build(
        source_capture_id=base.capture_id, source_image_signature=base.image_signature,
        source_width=base.width, source_height=base.height, source_orientation="portrait",
        semantic_name=f"底栏「{label}」", action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(index / 5 + .02, .86, (index + 1) / 5 - .02, .94),
        confidence=.95, risk_level=InteractionRiskLevel.LOW, purpose="检查底栏",
        expected_state_hint=f"tab:{label}", frontier_scope="app", discovery_source="vision",
    ) for index, label in enumerate(labels))
    analysis = _analysis_for_png(png, candidates=tuple(reversed(items)))
    assert observed_bottom_nav_labels(analysis) == labels
    result = calibrate_bottom_navigation_candidates(
        analysis=analysis, screenshot_png=png, ordered_labels=observed_bottom_nav_labels(analysis),
    )
    assert {item.semantic_name for item in result.candidates} == {
        f"底栏「{label}」" for label in labels
    }
    assert calibrate_bottom_navigation_candidates(
        analysis=base, screenshot_png=png, ordered_labels=observed_bottom_nav_labels(base),
    ) is base


def test_current_navigation_survives_global_frontier_filter():
    state = TangguoFlowState(phase="bottom_tabs", comic_flow_status="skipped")
    tab = candidate("首页", bottom_navigation_tab_seek=True, coverage_page_seek_contract={})
    work = work_for(state=state, candidates=(tab,))
    retained = AppGlobalFrontierService(repository=object()).retain_registered_candidates(
        work=work, registered=(),
    )
    assert retained.frontier.candidates == (tab,)
    assert decide_app_three_contracts(retained).semantic_target == "首页"


def test_restore_category_limit_continues_to_bottom_tabs():
    state = TangguoFlowState(phase="top_tab_restore")
    assert recover_phase_limit(state, "phase_round_limit:top_tab_restore")
    assert state.phase == "bottom_tabs"
    assert state.stage_results["top_tab_restore"] == "blocked"


@pytest.mark.asyncio
async def test_state_machine_terminal_cannot_restore_global_candidate():
    state = TangguoFlowState(phase="done", stop_reason="back_home_exhausted")
    work = work_for(state=state)
    terminal = decide_app_three_contracts(work)
    runner = AppExplorationTurnRunner(
        queue=object(), turn_executor=object(), continuation=object(), lease_owner="test",
        global_frontier=object(),
    )
    decision, item = await runner._restore_pending_frontier(
        work=work, agent_run_id="test", proposed_terminal=terminal,
    )
    assert decision is terminal
    assert item is None
