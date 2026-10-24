"""00:59 任务回归：真实视觉候选映射后仍能进入影片及底栏状态机。"""

from dataclasses import replace

import pytest

from services.api.app.application.app_global_frontier import AppGlobalFrontierService
from services.api.app.application.app_screenshot_exploration import _map_candidate
from services.api.app.application.app_tangguo_flow_state import TangguoFlowState
from services.api.app.application.app_three_contract_driver import decide_app_three_contracts
from services.api.app.domain.interaction import SurfaceKind
from services.api.tests.test_app_global_frontier import MemoryFrontierRepository
from services.api.tests.test_app_interaction_runtime import _candidate
from services.api.tests.test_app_three_contract_driver import work_for


def mapped(label, hint, scope="app"):
    visual = _candidate(semantic_name=label, expected_state_hint=hint, frontier_scope=scope)
    return _map_candidate(visual, navigation_depth=0, surface_kind=SurfaceKind.ANDROID)


def current_work(items, phase):
    work = work_for(state=TangguoFlowState(phase=phase, comic_flow_status="skipped"))
    items = tuple(replace(item, metadata={
        **item.metadata, "source_capture_id": work.source_capture_id,
    }) for item in items)
    return replace(work, frontier=replace(work.frontier, candidates=items))


def test_visual_bottom_tabs_survive_mapping_and_reach_real_driver():
    labels = ("首页", "剧库", "投票", "专题", "我的")
    items = tuple(mapped(f"底栏「{label}」", f"tab:{label}") for label in labels)
    work = current_work(items, "bottom_tabs")
    service = AppGlobalFrontierService(repository=MemoryFrontierRepository())
    retained = service.retain_registered_candidates(
        work=work, registered=(),
    )
    decision = decide_app_three_contracts(retained)
    assert decision.parameters["candidate_id"] == items[0].candidate_id
    assert list(decision.parameters["tangguo_flow_state"]["discovered_bottom_tabs"]) == list(labels)
    assert all(item.metadata.get("normalized_bounds") for item in items)


@pytest.mark.parametrize("scope,hint", [("page", "tab:推荐"), ("app", "video_detail")])
def test_non_tab_candidates_are_not_promoted(scope, hint):
    assert mapped("推荐", hint, scope).metadata.get("bottom_navigation_tab_seek") is not True


def test_immediate_watch_survives_global_filter_before_stateless_selection():
    watch = mapped("立即看", "video_detail", "page")
    resume = mapped("继续观看", "video_detail", "page")
    work = current_work((resume, watch), "video_entry")
    service = AppGlobalFrontierService(repository=MemoryFrontierRepository())
    retained = service.retain_registered_candidates(
        work=work, registered=(),
    )
    decision = decide_app_three_contracts(retained)
    assert decision.semantic_target == "立即看"
    assert decision.parameters["candidate_id"] == watch.candidate_id
