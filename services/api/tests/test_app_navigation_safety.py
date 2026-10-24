"""通用底栏的当前帧、身份与根页退出保护。"""

import pytest

from services.api.app.application.app_flow_navigation import visible_bottom_tabs
from services.api.app.application.app_tangguo_flow_driver import _back_intent
from services.api.app.application.app_tangguo_flow_state import TangguoFlowState
from services.api.app.application.app_tangguo_recognize import recognize_bottom_tab
from services.api.app.application.app_three_contract_driver import decide_app_three_contracts
from services.api.app.domain.exploration import ExplorationTerminal
from services.api.tests.test_app_three_contract_driver import candidate, work_for


@pytest.mark.parametrize("metadata", [
    {}, {"top_navigation_tab_seek": True},
    {"bottom_navigation_tab_seek": True, "source_capture_id": "old-frame"},
    {"bottom_navigation_tab_seek": True, "control_role": "home"},
    {"bottom_navigation_tab_seek": True, "normalized_bounds": {"left": -1}},
])
def test_invalid_navigation_cannot_be_discovered_or_clicked(metadata):
    work = work_for(candidates=(candidate("推荐", **metadata),))
    assert visible_bottom_tabs(work) == []
    assert recognize_bottom_tab(work, wanted_label="推荐", aliases=()).status == "not_found"


def test_unknown_but_explicit_bottom_label_is_supported():
    work = work_for(candidates=(candidate("新栏目", bottom_navigation_tab_seek=True),))
    assert recognize_bottom_tab(work, wanted_label="新栏目", aliases=()).status == "found"


def test_root_without_home_label_never_emits_back():
    work = work_for(page_type="content_list", page_state="stable", candidates=(
        candidate("精选", bottom_navigation_tab_seek=True),
        candidate("我的", bottom_navigation_tab_seek=True),
    ))
    state = TangguoFlowState(phase="final_home")
    assert _back_intent(work, state, purpose="返回") is None
    assert state.stop_reason == "app_root_back_forbidden"
    assert state.device_actions_used == 0


@pytest.mark.parametrize("page", ["launcher", "desktop", "system_home"])
def test_launcher_stops_before_clicking_old_app_targets(page):
    result = decide_app_three_contracts(work_for(
        page_type=page, candidates=(candidate("立即看"),),
    ))
    assert isinstance(result, ExplorationTerminal)
    assert result.reason == "app_left_foreground"
