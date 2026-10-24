"""默认 APP 三合同入口：跨帧预算、无模型回退及无坐标语义链路。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.agents.explorer import MafExplorerAgent
from services.api.app.application.app_constrained_locate import (
    ConstrainedLocateError,
    validate_constrained_locate,
)
from services.api.app.application.app_tangguo_flow_driver import _tap_intent
from services.api.app.application.app_tangguo_flow_state import TangguoFlowState
from services.api.app.application.app_tangguo_recognize import (
    recognize_alipay,
    recognize_bottom_tab,
    recognize_comic_entry,
    recognize_start_read,
    recognize_wallet_entry,
)
from services.api.app.application.app_three_contract_driver import decide_app_three_contracts
from services.api.app.domain.exploration import CandidateAction, ExplorationTerminal
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    TimedObservationIntent,
)
from services.api.tests.test_tangguo_flow_driver import _work


def candidate(label, **metadata):
    return CandidateAction(
        candidate_id="candidate:" + label,
        semantic_target=label,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        metadata=metadata,
    )


def work_for(*, state=None, candidates=(), package="com.alz7xm2.drama", **kwargs):
    metadata = {"package_name": package}
    if state:
        metadata["tangguo_flow_state"] = state.to_dict()
    work = _work(metadata=metadata, candidates=candidates, **kwargs)
    return replace(work, deadline=datetime.now(UTC) + timedelta(hours=1))


class NoModel:
    async def run(self, *_args, **_kwargs):
        raise AssertionError("APP 三合同不应调用自由探索模型")


@pytest.mark.parametrize("method", ["decide_next", "decide_deterministic"])
@pytest.mark.parametrize("package", ["com.alz7xm2.drama", "com.brand.new.app"])
async def test_default_app_entry_claims_all_packages_without_model(method, package):
    watch = candidate("立即看", expected_state_hint="video_detail")
    work = work_for(candidates=(watch,), package=package)
    agent = MafExplorerAgent(agent=NoModel())
    decision = await getattr(agent, method)(work)
    assert isinstance(decision, ActionIntent)
    assert decision.parameters["candidate_id"] == watch.candidate_id
    assert decision.parameters["app_exploration_contract"] == "bounded-stateless-constrained.v1"
    assert "normalized_bounds" not in decision.parameters
    assert decision.parameters["tangguo_flow_state"]["phase"] == "video_verify"


@pytest.mark.parametrize("method", ["decide_next", "decide_deterministic"])
async def test_former_script_product_uses_generic_candidate(method):
    watch = candidate("立即看", expected_state_hint="video_detail")
    work = work_for(package="com.dzu4.app", candidates=(watch,))
    decision = await getattr(MafExplorerAgent(agent=NoModel()), method)(work)
    assert isinstance(decision, ActionIntent)
    assert decision.parameters["candidate_id"] == watch.candidate_id
    assert decision.parameters["app_exploration_contract"] == "bounded-stateless-constrained.v1"


def test_same_work_is_idempotent_and_next_frame_keeps_budget():
    work = work_for(candidates=(candidate("立即看"),))
    first = decide_app_three_contracts(work)
    assert decide_app_three_contracts(work) is first
    state = TangguoFlowState.from_dict(first.parameters["tangguo_flow_state"])
    assert state.contract_runtime["rounds"] == 1
    next_work = work_for(state=state, capture_id="cap-2", page_type="video_detail")
    second = decide_app_three_contracts(next_work)
    assert second.action_type is InteractionOperation.BACK
    assert second.parameters["tangguo_flow_state"]["contract_runtime"]["rounds"] == 2


@pytest.mark.parametrize("tool,label,kwargs", [
    (recognize_comic_entry, "漫画第一本", {}),
    (recognize_start_read, "开始阅读", {}),
    (recognize_wallet_entry, "钱包", {}),
    (recognize_alipay, "支付宝", {}),
    (recognize_bottom_tab, "我的", {"wanted_label": "我的", "aliases": ("我的",)}),
])
def test_stateless_tools_return_current_candidate_id_without_coordinates(tool, label, kwargs):
    item = candidate(label, **({"bottom_navigation_tab_seek": True}
                              if tool is recognize_bottom_tab else {}))
    work = work_for(candidates=(item,))
    before = work.to_dict()
    result = tool(work, **kwargs)
    assert result.status == "found"
    assert result.candidate_id == item.candidate_id
    assert result.coordinate is None
    assert tool(work, **kwargs) == result
    assert work.to_dict() == before
    checked = validate_constrained_locate(result.as_dict(), allowed_targets=(result.target,))
    assert checked == result


@pytest.mark.parametrize("phase,page,label", [
    ("comic_entry", "home", "漫画第一本"),
    ("comic_detail", "comic_detail", "开始阅读"),
    ("wallet_search", "profile", "钱包"),
    ("payment_alipay", "payment_method", "支付宝"),
])
def test_semantic_tools_are_wired_to_real_flow(phase, page, label):
    item = candidate(label)
    decision = decide_app_three_contracts(work_for(
        state=TangguoFlowState(phase=phase), page_type=page, candidates=(item,),
    ))
    assert isinstance(decision, ActionIntent)
    assert decision.parameters["candidate_id"] == item.candidate_id


def test_popup_without_coordinates_is_closed_before_video_and_counts_global_budget():
    close = candidate("关闭广告", control_role="close")
    decision = decide_app_three_contracts(work_for(
        candidates=(candidate("立即看"), close), page_state="popup",
    ))
    assert decision.parameters["candidate_id"] == close.candidate_id
    assert decision.parameters["tangguo_flow_state"]["device_actions_used"] == 1
    assert decision.parameters["tangguo_flow_state"]["phase"] == "home_confirm"


def test_popup_without_close_waits_then_stops_instead_of_clicking_behind_it():
    state = TangguoFlowState()
    for index in range(2):
        result = decide_app_three_contracts(work_for(
            state=state, capture_id=f"cap-{index}", page_state="popup",
            candidates=(candidate("立即看"),),
        ))
        assert isinstance(result, TimedObservationIntent)
        state = TangguoFlowState.from_dict(result.workflow_state)
    result = decide_app_three_contracts(work_for(state=state, page_state="popup"))
    assert isinstance(result, ExplorationTerminal)
    assert "phase_round_limit" in result.reason


def test_home_back_home_advances_despite_popup_marker_without_close():
    """影片已回首页：帧上仍标 popup／飘条时不得空转，须推进上方分类／底栏。"""
    state = TangguoFlowState(
        phase="back_home",
        stage_results={"home": "passed", "home_video": "passed"},
        video_outcome="entered_detail",
        back_home_attempts=2,
    )
    top = candidate("推荐", home_top_category_tab_seek=True)
    bottom = candidate("剧库", bottom_navigation_tab_seek=True)
    result = decide_app_three_contracts(work_for(
        state=state,
        page_type="home",
        page_state="popup",
        candidates=(top, bottom, candidate("立即看")),
    ))
    assert isinstance(result, ActionIntent)
    saved = TangguoFlowState.from_dict(result.parameters["tangguo_flow_state"])
    assert saved.phase in {"top_tabs", "bottom_tabs"}
    assert saved.stage_results.get("back_home") == "passed"


def test_video_detail_page_type_returns_home_without_ai_retry_loop():
    """观察帧已是详情页：同轮直接 BACK，禁止 uncertain 连截多张。"""
    state = TangguoFlowState(
        phase="video_verify",
        video_attempts=1,
        stage_results={"home": "passed"},
    )
    result = decide_app_three_contracts(work_for(
        state=state, page_type="video_detail", candidates=(),
    ))
    assert isinstance(result, ActionIntent)
    assert result.action_type is InteractionOperation.BACK
    saved = TangguoFlowState.from_dict(result.parameters["tangguo_flow_state"])
    assert saved.phase == "back_home"
    assert saved.stage_results.get("home_video") == "passed"
    assert saved.ai_calls_used == 0


def test_dismiss_phase_round_limit_yields_to_home_curriculum_instead_of_terminal():
    """清障相位轮次用尽：交还首页课纲，不得整单 phase_round_limit 收口。"""
    state = TangguoFlowState(
        phase="back_home",
        stage_results={"home": "passed", "home_video": "passed"},
        video_outcome="entered_detail",
    )
    state.contract_runtime = {
        "rounds": 3,
        "rounds_by_phase": {
            "dismiss:popup:center_modal_ad_popup_with_cta_and_bottom_x": 2,
        },
        "phase_elapsed_seconds": {
            "dismiss:popup:center_modal_ad_popup_with_cta_and_bottom_x": 10.0,
        },
        "active_phase_key": "dismiss:popup:center_modal_ad_popup_with_cta_and_bottom_x",
        "active_since": datetime.now(UTC).isoformat(),
    }
    top = candidate("推荐", home_top_category_tab_seek=True)
    result = decide_app_three_contracts(work_for(
        state=state,
        page_type="home",
        page_state="popup",
        overlay_signature="center_modal_ad_popup_with_cta_and_bottom_x",
        candidates=(top,),
    ))
    assert not isinstance(result, ExplorationTerminal)
    if isinstance(result, ActionIntent):
        saved = TangguoFlowState.from_dict(result.parameters["tangguo_flow_state"])
    else:
        saved = TangguoFlowState.from_dict(result.workflow_state)
    assert "phase_round_limit" not in (saved.stop_reason or "")
    assert saved.phase != "done"
    assert any("phase_round_limit" in str(item) for item in saved.recovery_events)


@pytest.mark.parametrize("kind", ["deadline", "rounds", "timeout", "action_budget"])
def test_hard_limits_return_explicit_terminal(kind):
    now = datetime.now(UTC)
    state = TangguoFlowState(phase="video_entry")
    if kind == "rounds":
        state.contract_runtime = {"rounds": 160}
    if kind == "phase":
        state.contract_runtime = {"rounds_by_phase": {"video_entry": 12}}
    if kind == "timeout":
        state.started_at_iso = (now - timedelta(hours=2)).isoformat()
    work = work_for(state=state, candidates=(candidate("立即看"),))
    if kind == "deadline":
        work = replace(work, deadline=now - timedelta(seconds=1))
    if kind == "action_budget":
        work = replace(work, budget=replace(
            work.budget, remaining_actions=0, remaining_business_actions=0,
            remaining_obstruction_actions=0,
        ))
    result = decide_app_three_contracts(work, now=now)
    assert isinstance(result, ExplorationTerminal)
    assert result.status.value != "completed"


@pytest.mark.parametrize("payload", [
    {"status": "found", "target": "wallet", "candidate_id": ""},
    {"status": "found", "target": "wallet", "candidate_id": "id", "action": "pay"},
    {"status": "not_found", "candidate_id": "id"},
    {"status": "found", "target": "other", "candidate_id": "id"},
])
def test_rejects_invalid_tool_output(payload):
    with pytest.raises(ConstrainedLocateError):
        validate_constrained_locate(payload, allowed_targets=("wallet",))


@pytest.mark.parametrize("identity,source", [("missing", "cap-1"), ("candidate:钱包", "old")])
def test_invalid_or_stale_candidate_cannot_dispatch(identity, source):
    state = TangguoFlowState()
    work = work_for(candidates=(candidate("钱包", source_capture_id=source),))
    result = _tap_intent(work, state, candidate_id=identity, coordinate=None,
                         semantic_target="钱包", purpose="查看钱包")
    assert result is None
    assert state.stop_reason == "invalid_current_frame_candidate"


def test_all_bottom_tabs_remain_pending_after_first_tab():
    labels = ("首页", "剧库", "投票", "专题", "我的", "发现")
    items = tuple(candidate(label, bottom_navigation_tab_seek=True) for label in labels)
    state = TangguoFlowState(phase="bottom_tabs", comic_flow_status="skipped")
    result = decide_app_three_contracts(work_for(state=state, candidates=items))
    saved = TangguoFlowState.from_dict(result.parameters["tangguo_flow_state"])
    assert set(saved.discovered_bottom_tabs) == set(labels)
    assert len(saved.pending_bottom_tabs) == len(labels)


def test_selected_alipay_finishes_without_payment_submission():
    state = TangguoFlowState(phase="payment_alipay", payment_mode="pay", payment_method="wechat")
    result = decide_app_three_contracts(work_for(
        state=state, page_type="payment_method",
        candidates=(candidate("支付宝", selected=True), candidate("立即付款")),
    ))
    assert isinstance(result, ActionIntent)
    assert result.action_type is InteractionOperation.BACK
    saved = result.parameters["tangguo_flow_state"]
    assert saved["payment_method"] == "alipay"
    assert saved["payment_mode"] == "inspect_only"
    assert saved["alipay_selected"] is True


def test_bottom_flow_clicks_every_tab_and_returns_home_across_frozen_frames():
    labels = ("首页", "剧库", "投票", "专题", "我的", "发现")
    pages = dict(zip(
        labels, ("home", "library", "vote", "topic", "profile", "discover"), strict=True,
    ))
    state = TangguoFlowState(phase="bottom_tabs")
    selected = "首页"
    clicked = []
    for index in range(50):
        items = tuple(candidate(
            label, bottom_navigation_tab_seek=True, selected=label == selected,
            expected_next_page_type=pages[label],
        ) for label in labels)
        result = decide_app_three_contracts(work_for(
            state=state, candidates=items, page_type=pages[selected], capture_id=f"cap-{index}",
        ))
        if isinstance(result, ExplorationTerminal):
            saved = result.coverage_summary["tangguo_flow_state"]
            assert saved["bottom_coverage_status"] == "complete"
            assert set(clicked) == set(labels)
            assert selected == "首页"
            assert saved["comic_flow_status"] == "skipped"
            assert saved["comic_outcome"] == "not_found_in_explored_scope"
            assert saved["comic_device_actions_used"] == 0
            break
        if isinstance(result, TimedObservationIntent):
            state = TangguoFlowState.from_dict(result.workflow_state)
            continue
        state = TangguoFlowState.from_dict(result.parameters["tangguo_flow_state"])
        if result.parameters.get("bottom_navigation_tab_seek"):
            selected = result.semantic_target
            clicked.append(selected)
        elif result.action_type is InteractionOperation.BACK:
            selected = "首页"
    else:
        pytest.fail("底栏全量流程未在有限步数内完成")


def test_missing_comic_on_first_page_does_not_block_later_discovery():
    from services.api.app.application.app_tangguo_flow_driver import _maybe_enter_comic

    state = TangguoFlowState(phase="bottom_tabs")
    absent = _maybe_enter_comic(
        work_for(candidates=(candidate("立即看", expected_state_hint="video_detail"),)),
        state, note="from_bottom_tabs",
    )
    assert absent is None
    assert state.comic_flow_status == "not_started"
    assert state.comic_device_actions_used == 0
    book = candidate("漫画第一本", expected_state_hint="comic_detail")
    found = _maybe_enter_comic(
        work_for(capture_id="cap-later", candidates=(book,)), state, note="from_bottom_tabs",
    )
    assert isinstance(found, ActionIntent)
    assert found.parameters["candidate_id"] == book.candidate_id
    assert state.comic_flow_status == "in_progress"
