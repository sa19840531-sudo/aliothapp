"""多种底栏布局回归：真实底栏完整点击、别名定位及个人页钱包接续。"""

import pytest

from services.api.app.application import app_tangguo_flow_driver as driver
from services.api.app.application.app_tangguo_flow_state import TangguoFlowState
from services.api.app.application.app_tangguo_recognize import recognize_bottom_tab
from services.api.tests.test_app_three_contract_driver import candidate, work_for


@pytest.mark.parametrize("labels", [
    ("短剧", "推荐", "视频", "AI社区", "我的"),
    ("短劇", "推薦", "視頻", "AI社區", "我的"),
    ("首页", "精选", "片库", "直播", "AI科技", "圈子", "我的"),
    ("首頁", "精選", "片庫", "直播", "AI科技", "圈子", "我的"),
    ("首页", "视频", "抖阴", "海角", "暗网", "吃瓜", "我的"),
    ("首頁", "視頻", "抖陰", "海角", "暗網", "吃瓜", "我的"),
    ("首页", "剧场", "专题", "我"),
    ("首頁", "劇場", "專題", "我"),
    ("主页", "短剧", "暗网", "AI", "社区", "我的"),
    ("主頁", "短劇", "暗網", "AI", "社區", "我的"),
    ("漫画", "小说", "视频", "我的"),
    ("漫畫", "小說", "視頻", "我的"),
    ("首页", "剧场", "视频", "社区", "福利", "我的"),
    ("首頁", "劇場", "視頻", "社區", "福利", "我的"),
    ("视频", "女优", "社区", "福利", "我的"),
    ("視頻", "女優", "社區", "福利", "我的"),
    ("二次元", "视频", "有声漫", "AI女友", "广场", "我的"),
    ("二次元", "視頻", "有聲漫", "AI女友", "廣場", "我的"),
    ("精选", "二次元", "禁区", "AI生成", "社区", "我的"),
    ("精選", "二次元", "禁區", "AI生成", "社區", "我的"),
    ("首页", "视频", "社区", "禁漫", "AI", "我的"),
    ("首頁", "視頻", "社區", "禁漫", "AI", "我的"),
    ("应用", "视频", "次元", "情色AI", "广场", "我的"),
    ("應用", "視頻", "次元", "情色AI", "廣場", "我的"),
    ("福利", "首页", "片库", "AI", "我的"),
    ("福利", "首頁", "片庫", "AI", "我的"),
    ("首页", "播放图标", "抖阴", "海角", "暗网", "吃瓜", "我的"),
    ("首頁", "播放圖示", "抖陰", "海角", "暗網", "吃瓜", "我的"),
    ("二次元", "视频", "萝莉岛", "情涩AI", "朋友圈", "我的"),
    ("二次元", "視頻", "蘿莉島", "情澀AI", "朋友圈", "我的"),
    ("首页", "雅间", "视频", "消息", "我的"),
    ("首頁", "雅間", "視頻", "消息", "我的"),
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
    ("片库", "tab_library"), ("片庫", "tab_library"),
    ("抖阴", "tab_douyin"), ("抖陰", "tab_douyin"),
    ("海角", "tab_haijiao"), ("吃瓜", "tab_gossip"),
    ("播放图标", "tab_play_icon"), ("播放圖示", "tab_play_icon"),
    ("暗网", "tab_darkweb"), ("暗網", "tab_darkweb"),
    ("AI", "tab_ai"), ("ai", "tab_ai"),
    ("社区", "tab_community"), ("社區", "tab_community"),
    ("我的", "tab_mine"), ("自定义栏目", "tab_other"),
    ("漫画", "tab_comic"), ("漫畫", "tab_comic"),
    ("禁漫", "tab_comic"),
    ("小说", "tab_novel"), ("小說", "tab_novel"),
    ("视频", "tab_video"), ("視頻", "tab_video"), ("影片", "tab_video"),
    ("福利", "tab_benefits"),
    ("女优", "tab_actress"), ("女優", "tab_actress"),
    ("二次元", "tab_acg"), ("有声漫", "tab_audio_comic"), ("有聲漫", "tab_audio_comic"),
    ("AI女友", "tab_ai_companion"), ("ai女友", "tab_ai_companion"),
    ("广场", "tab_square"), ("廣場", "tab_square"),
    ("精选", "tab_featured"), ("精選", "tab_featured"),
    ("禁区", "tab_restricted_zone"), ("禁區", "tab_restricted_zone"),
    ("AI生成", "tab_ai_generation"), ("ai生成", "tab_ai_generation"),
    ("应用", "tab_apps"), ("應用", "tab_apps"), ("次元", "tab_acg"),
    ("情色AI", "tab_adult_ai"), ("情色ai", "tab_adult_ai"),
    ("萝莉岛", "tab_island"), ("蘿莉島", "tab_island"),
    ("情涩AI", "tab_qingse_ai"), ("情澀AI", "tab_qingse_ai"),
    ("情涩ai", "tab_qingse_ai"), ("朋友圈", "tab_moments"),
    ("雅间", "tab_private_room"), ("雅間", "tab_private_room"),
    ("消息", "tab_messages"), ("訊息", "tab_messages"), ("信息", "tab_messages"),
    ("直播", "tab_live"), ("AI科技", "tab_ai_technology"),
    ("ai科技", "tab_ai_technology"), ("圈子", "tab_circles"),
    ("推荐", "tab_recommend"), ("推薦", "tab_recommend"),
    ("AI社区", "tab_ai_community"), ("AI社區", "tab_ai_community"),
    ("ai社区", "tab_ai_community"),
])
def test_bottom_target_uses_role_without_defaulting_to_home(label, target):
    item = candidate(f"底栏「{label}」", bottom_navigation_tab_seek=True)
    found = recognize_bottom_tab(work_for(candidates=(item,)), wanted_label=label, aliases=())
    assert found.target == target
    assert found.candidate_id == item.candidate_id


@pytest.mark.parametrize("home,home_icon,haijiao_icon", [
    ("首页", "首页图标", "海角图标"),
    ("首頁", "首頁圖示", "海角圖示"),
    ("首頁", "首頁圖標", "海角圖標"),
])
def test_selected_icons_keep_identity_across_seven_tab_flow(home, home_icon, haijiao_icon):
    labels = (home, "视频", "抖阴", "海角", "暗网", "吃瓜", "我的")
    state = TangguoFlowState(phase="bottom_tabs", comic_flow_status="skipped")
    icons = {home: home_icon, "海角": haijiao_icon}
    for label in labels:
        before = work_for(candidates=tuple(
            candidate(name, bottom_navigation_tab_seek=True) for name in labels
        ), page_state="stable")
        action = driver._phase_bottom_tabs(before, state)
        assert action.semantic_target == label
        after = work_for(candidates=tuple(
            candidate(f"底栏「{icons.get(name, name)}」" if name == label else name,
                      bottom_navigation_tab_seek=True, selected=name == label)
            for name in labels
        ), page_state="stable")
        found = recognize_bottom_tab(after, wanted_label=label, aliases=())
        assert found.candidate_id == f"candidate:底栏「{icons.get(label, label)}」"
        driver._phase_bottom_tabs(after, state)
        assert state.tab_results[label]["status"] == "passed"
    assert state.discovered_bottom_tabs == list(labels)
    assert state.visited_bottom_tabs == list(labels)
    assert state.pending_bottom_tabs == []
    assert state.phase == "wallet_search"


def test_unidentified_icon_is_not_assumed_to_be_haijiao():
    from services.api.app.application.app_flow_navigation import tab_label

    assert tab_label("绿色圆形图标") == "绿色圆形图标"


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


@pytest.mark.parametrize("visited", [[], ["首页", "精选"]])
def test_seven_tabs_resume_after_popup_without_losing_progress(visited):
    from services.api.app.application.app_three_contract_driver import decide_app_three_contracts

    labels = ["首页", "精选", "片库", "直播", "AI科技", "圈子", "我的"]
    state = TangguoFlowState(
        phase="bottom_tabs", comic_flow_status="skipped",
        discovered_bottom_tabs=labels, visited_bottom_tabs=list(visited),
        tab_results={name: {"status": "passed", "clicked": True} for name in visited},
    )
    tabs = tuple(candidate(name, bottom_navigation_tab_seek=True) for name in labels)
    close = candidate("关闭广告", control_role="close")
    dismiss = decide_app_three_contracts(work_for(
        state=state, candidates=(*tabs, candidate("立即参与"), close), page_state="popup",
    ))
    assert dismiss.parameters["candidate_id"] == close.candidate_id
    saved = TangguoFlowState.from_dict(dismiss.parameters["tangguo_flow_state"])
    assert saved.phase == "bottom_tabs"
    assert saved.visited_bottom_tabs == visited
    resumed = decide_app_three_contracts(work_for(
        state=saved, candidates=tabs, page_state="stable", capture_id="after-popup",
    ))
    assert resumed.parameters["candidate_id"] == "candidate:" + labels[len(visited)]
    assert list(resumed.parameters["tangguo_flow_state"]["visited_bottom_tabs"]) == visited


@pytest.mark.parametrize("label", ["推荐", "推薦"])
def test_recommend_bottom_tab_takes_priority_over_top_category(label):
    top = candidate(label, top_navigation_tab_seek=True)
    bottom = candidate(f"底栏「{label}」", bottom_navigation_tab_seek=True)
    work = work_for(candidates=(top, bottom), page_state="stable")
    found = recognize_bottom_tab(work, wanted_label=label, aliases=())
    assert found.candidate_id == bottom.candidate_id
    assert found.target == "tab_recommend"


@pytest.mark.parametrize("label", [
    "暗网", "AI", "社区", "漫画", "小说", "视频", "福利", "女优",
    "二次元", "有声漫", "AI女友", "广场",
    "精选", "禁区", "AI生成",
    "禁漫",
    "片库",
    "抖阴", "海角", "吃瓜", "播放图标",
    "萝莉岛", "情涩AI", "朋友圈",
    "雅间", "消息",
    "直播", "AI科技", "圈子",
    "推荐", "推薦", "AI社区", "AI社區",
    "应用", "次元", "情色AI",
])
def test_new_tab_requires_destination_evidence(label):
    from services.api.app.application.app_flow_navigation import tab_destination_confirmed

    unselected = candidate(label, bottom_navigation_tab_seek=True, selected=False)
    work = work_for(candidates=(unselected,), page_state="stable")
    assert not tab_destination_confirmed(work, label)
    selected = candidate(label, bottom_navigation_tab_seek=True, selected=True)
    work = work_for(candidates=(selected,), page_state="stable")
    assert tab_destination_confirmed(work, label)
