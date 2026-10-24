"""多种底栏布局回归：真实底栏完整点击、别名定位及个人页钱包接续。"""

import pytest

from services.api.app.application import app_tangguo_flow_driver as driver
from services.api.app.application.app_tangguo_flow_state import TangguoFlowState
from services.api.app.application.app_tangguo_recognize import recognize_bottom_tab
from services.api.tests.test_app_three_contract_driver import candidate, work_for


@pytest.mark.parametrize("labels", [
    ("首页", "剧场", "专题", "我"),
    ("首頁", "劇場", "專題", "我"),
    ("主页", "短剧", "暗网", "AI", "社区", "我的"),
    ("主頁", "短劇", "暗網", "AI", "社區", "我的"),
])
def test_all_visible_tabs_are_clicked_and_profile_continues_to_wallet(labels):
    state = TangguoFlowState(phase="bottom_tabs", comic_flow_status="skipped")
    for index, label in enumerate(labels):
        items = tuple(candidate(name, bottom_navigation_tab_seek=True,
                                selected=position == index)
                      for position, name in enumerate(labels))
        work = work_for(candidates=items, page_state="stable")
        action = driver._phase_bottom_tabs(work, state)
        assert action.semantic_target == label
        assert action.parameters["candidate_id"] == "candidate:" + label
        driver._phase_bottom_tabs(work, state)
        assert state.tab_results[label]["status"] == "passed"
    assert state.discovered_bottom_tabs == list(labels)
    assert state.visited_bottom_tabs == list(labels)
    assert state.pending_bottom_tabs == []
    assert state.phase == "wallet_search"


@pytest.mark.parametrize("label,target", [
    ("首页", "tab_home"), ("首頁", "tab_home"),
    ("剧场", "tab_library"), ("劇場", "tab_library"),
    ("专题", "tab_topic"), ("專題", "tab_topic"), ("我", "tab_mine"),
    ("主页", "tab_home"), ("主頁", "tab_home"),
    ("短剧", "tab_library"), ("短劇", "tab_library"),
    ("暗网", "tab_darkweb"), ("暗網", "tab_darkweb"),
    ("AI", "tab_ai"), ("ai", "tab_ai"),
    ("社区", "tab_community"), ("社區", "tab_community"),
    ("我的", "tab_mine"), ("自定义栏目", "tab_other"),
])
def test_bottom_target_uses_role_without_defaulting_to_home(label, target):
    item = candidate(f"底栏「{label}」", bottom_navigation_tab_seek=True)
    found = recognize_bottom_tab(work_for(candidates=(item,)), wanted_label=label, aliases=())
    assert found.target == target
    assert found.candidate_id == item.candidate_id


def test_short_profile_label_does_not_match_body_text():
    body = candidate("我的收藏")
    tab = candidate("我", bottom_navigation_tab_seek=True)
    found = recognize_bottom_tab(
        work_for(candidates=(body, tab)), wanted_label="我", aliases=("我的",),
    )
    assert found.candidate_id == tab.candidate_id
    absent = recognize_bottom_tab(
        work_for(candidates=(body,)), wanted_label="我", aliases=("我的",),
    )
    assert absent.status == "not_found"


@pytest.mark.parametrize("label", ["暗网", "AI", "社区"])
def test_new_tab_requires_destination_evidence(label):
    from services.api.app.application.app_flow_navigation import tab_destination_confirmed

    unselected = candidate(label, bottom_navigation_tab_seek=True, selected=False)
    work = work_for(candidates=(unselected,), page_state="stable")
    assert not tab_destination_confirmed(work, label)
    selected = candidate(label, bottom_navigation_tab_seek=True, selected=True)
    work = work_for(candidates=(selected,), page_state="stable")
    assert tab_destination_confirmed(work, label)
