"""糖果短剧有状态主流程＋无状态辨识／支付宝 inspect_only 单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.app_constrained_locate import (
    ConstrainedLocateError,
    ConstrainedLocateResult,
    validate_constrained_locate,
)
from services.api.app.application.app_tangguo_flow_driver import (
    alipay_jump_is_not_payment_success,
    apply_alipay_selection_verification,
    inspect_only_blocks_payment,
    read_tangguo_flow_state,
    try_tangguo_flow_driver,
)
from services.api.app.application.app_tangguo_flow_state import (
    ResumeCheckpoint,
    TangguoBudgets,
    TangguoFlowState,
    merge_flow_state_from_intent_parameters,
    package_uses_tangguo_flow,
)
from services.api.app.application.app_tangguo_recognize import (
    is_comic_detail_not_reader,
    parse_external_locate,
    recognize_page_state,
    recognize_top_tabs,
    recognize_video_entry,
    recognize_wallet_entry,
)
from services.api.app.domain.exploration import (
    AppExplorationGoal,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)


def _work(
    *,
    capture_id: str = "cap-1",
    page_type: str = "home",
    page_state: str = "stable",
    overlay_signature: str | None = None,
    metadata: dict | None = None,
    candidates: tuple[CandidateAction, ...] = (),
):
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    observation = SurfaceObservation(
        capture_id=capture_id,
        surface_kind=SurfaceKind.ANDROID,
        location="app://com.alz7xm2.drama/.MainActivity",
        state_version="state-1",
        observed_at=now,
        key_elements=(),
        page_type=page_type,
        page_state=page_state,
        overlay_signature=overlay_signature,
        metadata={
            "package_name": "com.alz7xm2.drama",
            **(metadata or {}),
        },
    )
    frontier = FrontierSnapshot(
        snapshot_id="snap-1",
        observation=observation,
        candidates=candidates,
    )
    return build_exploration_work(
        task_id="task-1",
        session_id="session-1",
        branch_id="branch-1",
        objective="糖果短剧有状态流程试点",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=capture_id,
        frontier_snapshot_id="snap-1",
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.alz7xm2.drama",),
            allowed_operations=(
                InteractionOperation.CLICK,
                InteractionOperation.TAP,
                InteractionOperation.OBSERVE,
                InteractionOperation.BACK,
                InteractionOperation.SCROLL,
            ),
            max_navigation_depth=4,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
            allowed_route_prefixes=("/",),
        ),
        budget=ExplorationBudget(
            max_actions=72,
            remaining_actions=72,
            max_depth=4,
            remaining_duration_ms=3_600_000,
        ),
        deadline=now + timedelta(hours=1),
        app_goal=AppExplorationGoal(
            required_page_types=("home", "video_detail", "profile"),
            required_milestones=("surface:stable_business_page",),
            allowed_packages=("com.alz7xm2.drama",),
            minimum_business_pages=1,
        ),
    )


def _tap(
    *,
    candidate_id: str,
    label: str,
    bounds: dict,
    **meta,
) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=label,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "text": label,
            "normalized_bounds": bounds,
            **meta,
        },
    )


def test_package_uses_tangguo_flow() -> None:
    assert package_uses_tangguo_flow(package_name="com.alz7xm2.drama") is True
    # 无专属编排 → 通用 Agent 三合同（糖果课纲）
    assert package_uses_tangguo_flow(package_name="com.brand.new.app") is True
    assert package_uses_tangguo_flow(package_name="com.example.target") is True
    # 2.0 所有产品均使用通用驱动
    assert package_uses_tangguo_flow(package_name="com.dzu4.app") is True
    assert package_uses_tangguo_flow(package_name="com.hj.ttthree") is True


def test_top_tabs_empty_skips_without_synthetic_hscroll() -> None:
    """无上方分类证据时不得合成 tangguo-flow-hscroll 抢 MAF 寻页。"""

    state = TangguoFlowState(
        phase="top_tabs",
        stage_results={"home_video": "passed", "back_home": "passed"},
    )
    work = _work(
        page_type="home",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(),
    )
    intent = try_tangguo_flow_driver(work)
    assert intent is None or not str(
        (intent.parameters or {}).get("candidate_id") or ""
    ).startswith("tangguo-flow-hscroll:")
    state2 = read_tangguo_flow_state(work)
    assert state2.stage_results.get("top_tabs") == "skipped"


def test_constrained_locate_rejects_bad_payload_and_oob() -> None:
    with pytest.raises(ConstrainedLocateError):
        validate_constrained_locate(
            {"status": "found", "target": "wallet", "coordinate": [1.5, 0.2]},
            allowed_targets=("wallet",),
        )
    with pytest.raises(ConstrainedLocateError):
        validate_constrained_locate(
            {"status": "not_found", "coordinate": [0.1, 0.2]},
            allowed_targets=("wallet",),
        )
    with pytest.raises(ConstrainedLocateError):
        validate_constrained_locate(
            {"status": "found", "target": "nope", "coordinate": [0.1, 0.2]},
            allowed_targets=("wallet",),
        )
    assert parse_external_locate(
        {"status": "found", "target": "wallet", "coordinate": [0.1, 0.2], "extra": 1},
        allowed_targets=("wallet",),
    ) is None


def test_schema_timeout_equivalent_no_click() -> None:
    """模型超时／格式错误：parse 返回 None，主流程不得点击。"""
    assert parse_external_locate("not-json", allowed_targets=("alipay",)) is None
    assert parse_external_locate(None, allowed_targets=("alipay",)) is None


def test_video_entry_then_back_home() -> None:
    video = _tap(
        candidate_id="v1",
        label="立即看",
        bounds={"left": 0.1, "top": 0.6, "right": 0.3, "bottom": 0.75},
        home_content_entry_seek=True,
    )
    work = _work(candidates=(video,))
    intent = try_tangguo_flow_driver(work)
    assert intent is not None
    assert intent.action_type is InteractionOperation.CLICK
    assert intent.parameters.get("home_content_entry_seek") is True
    state = read_tangguo_flow_state(work)
    assert state.phase == "video_verify"
    assert state.video_attempts == 1

    # 详情页验证 → back_home
    detail = _work(
        page_type="video_detail",
        metadata={"tangguo_flow_state": state.to_dict()},
    )
    intent2 = try_tangguo_flow_driver(detail)
    # 阶段推进到 back_home，可能立刻发出 BACK
    state2 = read_tangguo_flow_state(detail)
    assert state2.video_outcome == "entered_detail"
    assert state2.stage_results.get("home_video") == "passed"
    assert intent2 is not None
    assert intent2.action_type is InteractionOperation.BACK

    # 回到首页验证
    home = _work(
        page_type="home",
        metadata={"tangguo_flow_state": {**state2.to_dict(), "phase": "back_home"}},
    )
    try_tangguo_flow_driver(home)
    state3 = read_tangguo_flow_state(home)
    assert state3.stage_results.get("back_home") == "passed"
    # 影片后进入上方分类；本帧无顶栏候选时跳过，不合成横滑，继续底栏。
    assert state3.phase in {"top_tabs", "top_tab_restore", "bottom_tabs", "final_home", "done"}
    if state3.phase != "top_tabs":
        assert state3.stage_results.get("top_tabs") == "skipped"


def test_bottom_tabs_must_click_home_even_if_content_done() -> None:
    """首页内容已检查，仍须实际点击底部首页按钮，不重跑影片链路。"""
    state = TangguoFlowState(
        phase="bottom_tabs",
        stage_results={
            "home_video": "passed",
            "home": "passed",
            "back_home": "passed",
            "top_tabs": "skipped",
        },
        discovered_bottom_tabs=["首页", "剧库", "投票", "专题", "我的"],
    )
    home = _tap(
        candidate_id="tab-home",
        label="首页",
        bounds={"left": 0.04, "top": 0.88, "right": 0.16, "bottom": 0.945},
        bottom_navigation_tab_seek=True,
        selected=True,
    )
    work = _work(
        page_type="home",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(home,),
    )
    intent = try_tangguo_flow_driver(work)
    assert intent is not None
    assert intent.semantic_target == "首页"
    state2 = read_tangguo_flow_state(work)
    assert "首页" not in state2.visited_bottom_tabs  # 须等点击后验证
    assert state2.tab_results["首页"].get("clicked") is True


def test_bottom_tabs_all_five_must_be_clicked() -> None:
    """底部 5 分頁全部实际点击，不能只抽 3 个。"""
    labels = ["首页", "剧库", "投票", "专题", "我的"]
    state = TangguoFlowState(
        phase="bottom_tabs",
        discovered_bottom_tabs=list(labels),
        stage_results={"home_video": "passed", "top_tabs": "skipped"},
    )
    clicked: list[str] = []
    for label in labels:
        cands = tuple(
            _tap(
                candidate_id=f"tab-{lab}",
                label=lab,
                bounds={
                    "left": 0.04 + i * 0.2,
                    "top": 0.88,
                    "right": 0.16 + i * 0.2,
                    "bottom": 0.945,
                },
                bottom_navigation_tab_seek=True,
            )
            for i, lab in enumerate(labels)
        )
        work = _work(
            page_type="home" if label == "首页" else "library",
            metadata={"tangguo_flow_state": state.to_dict()},
            candidates=cands,
        )
        intent = try_tangguo_flow_driver(work)
        assert intent is not None, f"should click {label}"
        assert intent.semantic_target == label
        clicked.append(label)
        state = read_tangguo_flow_state(work)
        # 模拟点击后验证成功（page_type 已在 work 中按 label 设定）
        state.tab_results[label] = {
            "status": "passed",
            "clicked": True,
            "selected_or_destination": True,
        }
        if label not in state.visited_bottom_tabs:
            state.visited_bottom_tabs.append(label)
        if label == "我的":
            state.wallet_search_done = True  # 本用例只验点击覆盖
        state.sync_pending_bottom_tabs()
    assert clicked == labels
    state.finalize_bottom_tabs_stage()
    assert state.compute_bottom_coverage() == "complete"
    assert state.stage_results.get("bottom_tabs") == "passed"


def test_bottom_coverage_incomplete_not_passed() -> None:
    state = TangguoFlowState(
        discovered_bottom_tabs=["首页", "剧库", "投票", "专题", "我的"],
        visited_bottom_tabs=["首页", "剧库"],
        tab_results={
            "首页": {"status": "passed", "clicked": True},
            "剧库": {"status": "passed", "clicked": True},
        },
    )
    assert state.compute_bottom_coverage() == "incomplete"
    state.finalize_bottom_tabs_stage()
    assert state.stage_results.get("bottom_tabs") != "passed"


def test_wallet_found_triggers_payment_branch() -> None:
    wallet = _tap(
        candidate_id="w1",
        label="我的钱包",
        bounds={"left": 0.1, "top": 0.4, "right": 0.9, "bottom": 0.48},
    )
    state = TangguoFlowState(phase="wallet_search")
    work = _work(
        page_type="profile",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(wallet,),
    )
    intent = try_tangguo_flow_driver(work)
    assert intent is not None
    assert "钱包" in str(intent.semantic_target)
    state2 = read_tangguo_flow_state(work)
    assert state2.wallet_search_result == "found"
    assert state2.phase == "payment_alipay"
    assert state2.stage_results.get("mine_wallet") == "passed"


def test_wallet_not_found_ends_branch_normally() -> None:
    state = TangguoFlowState(
        phase="wallet_search",
        budgets=TangguoBudgets(profile_scroll_max=0),
        discovered_bottom_tabs=["首页", "剧库", "投票", "专题", "我的"],
        visited_bottom_tabs=["首页", "剧库", "投票", "专题", "我的"],
        tab_results={
            lab: {"status": "passed", "clicked": True}
            for lab in ["首页", "剧库", "投票", "专题", "我的"]
        },
    )
    state.push_payment_checkpoint(note="test")
    work = _work(
        page_type="profile",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(),
    )
    intent = try_tangguo_flow_driver(work)
    state2 = read_tangguo_flow_state(work)
    assert state2.wallet_search_result == "not_found_in_scope"
    assert state2.stage_results.get("mine_wallet") == "skipped"
    assert state2.wallet_search_done is True
    # 支付分支正常结束后续续底栏／回首页，不宣告 APP 无支付
    assert state2.phase == "payment_back_home"
    assert state2.stop_reason is None
    # 可能发出回首页 BACK，但不得是付款动作
    if intent is not None:
        assert intent.action_type is InteractionOperation.BACK


def test_alipay_select_and_confirm() -> None:
    alipay = _tap(
        candidate_id="pay1",
        label="支付宝",
        bounds={"left": 0.2, "top": 0.5, "right": 0.8, "bottom": 0.58},
    )
    tabs = ["首页", "剧库", "投票", "专题", "我的"]
    state = TangguoFlowState(
        phase="payment_alipay",
        payment_mode="inspect_only",
        discovered_bottom_tabs=list(tabs),
        visited_bottom_tabs=list(tabs),
        tab_results={lab: {"status": "passed", "clicked": True} for lab in tabs},
    )
    state.push_payment_checkpoint(note="test")
    work = _work(
        page_type="payment",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(alipay,),
    )
    intent = try_tangguo_flow_driver(work)
    assert intent is not None
    assert intent.parameters.get("payment_method") == "alipay"
    state2 = read_tangguo_flow_state(work)
    assert state2.payment_farthest_stage == "alipay_tap"
    assert state2.alipay_available is True

    # 二次验证选中
    selected = _tap(
        candidate_id="pay1s",
        label="支付宝已选",
        bounds={"left": 0.2, "top": 0.5, "right": 0.8, "bottom": 0.58},
        selected=True,
    )
    work2 = _work(
        page_type="home",  # 接续 final_home 时可直接确认
        metadata={"tangguo_flow_state": state2.to_dict()},
        candidates=(selected,),
    )
    intent2 = try_tangguo_flow_driver(work2)
    state3 = read_tangguo_flow_state(work2)
    assert state3.alipay_selected is True
    assert state3.stage_results.get("payment_alipay") == "passed"
    # inspect_only：无最终付款；接续回首页／结束
    assert state3.phase in {"final_home", "done"}
    if intent2 is not None:
        assert "confirm_pay" not in str(intent2.purpose)


def test_switch_from_default_wechat_to_alipay() -> None:
    wechat = _tap(
        candidate_id="wx",
        label="微信支付",
        bounds={"left": 0.2, "top": 0.4, "right": 0.8, "bottom": 0.48},
        selected=True,
    )
    alipay = _tap(
        candidate_id="ali",
        label="支付宝",
        bounds={"left": 0.2, "top": 0.5, "right": 0.8, "bottom": 0.58},
    )
    state = TangguoFlowState(phase="payment_alipay")
    work = _work(
        page_type="payment",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(wechat, alipay),
    )
    intent = try_tangguo_flow_driver(work)
    assert intent is not None
    assert "支付宝" in str(intent.semantic_target)


def test_wallet_confirm_pay_with_channels_is_not_order_confirm() -> None:
    """钱包页「确认支付」+ 微信｜支付宝并排 ≠ 独立订单确认收口。"""
    from services.api.app.application.app_tangguo_recognize import recognize_alipay

    wechat = _tap(
        candidate_id="wx",
        label="微信",
        bounds={"left": 0.1, "top": 0.72, "right": 0.45, "bottom": 0.80},
        selected=True,
    )
    state = TangguoFlowState(phase="payment_alipay")
    work = _work(
        page_type="payment",
        metadata={
            "tangguo_flow_state": state.to_dict(),
            "visual_summary": "我的钱包 快速充值 微信 支付宝 确认支付 应付 ¥100",
        },
        candidates=(wechat,),
    )
    classified = recognize_page_state(work)
    assert classified.page == "recharge_page"
    intent = try_tangguo_flow_driver(work)
    assert intent is not None
    assert "支付宝" in str(intent.semantic_target)
    assert "confirm_pay" not in str(intent.purpose).casefold()
    # 无支付宝候选坐标时，用语意图标相对槽落点
    located = recognize_alipay(work, require_selected=False)
    assert located.status == "found"
    assert located.reason == "semantic_icon_relative"
    assert located.coordinate is not None
    assert located.coordinate[0] > 0.5


def test_payment_channel_relative_slots_wechat_left_alipay_right() -> None:
    from services.api.app.application.app_ui_tree_features import (
        payment_channel_tap_bounds,
        payment_channels_ui_tree_features,
    )

    channels = payment_channels_ui_tree_features(package_name="com.alz7xm2.drama")
    assert channels.get("policy") == "semantic_icon_relative"
    wechat = payment_channel_tap_bounds("微信支付", package_name="com.alz7xm2.drama")
    alipay = payment_channel_tap_bounds("支付宝", package_name="com.alz7xm2.drama")
    assert wechat is not None and alipay is not None
    assert wechat["right"] <= alipay["left"] or wechat["right"] <= 0.5
    assert (alipay["left"] + alipay["right"]) / 2.0 >= 0.5
    assert 0.65 <= wechat["top"] < wechat["bottom"] <= 0.88


def test_alipay_under_more_options() -> None:
    more = _tap(
        candidate_id="more",
        label="更多支付方式",
        bounds={"left": 0.3, "top": 0.7, "right": 0.7, "bottom": 0.76},
    )
    state = TangguoFlowState(phase="payment_alipay")
    # 画面文案含支付宝但无坐标 → uncertain may_be_under_more；同时有更多按钮
    work = _work(
        page_type="payment",
        metadata={
            "tangguo_flow_state": state.to_dict(),
            "visual_summary": "请选择支付方式 支付宝 更多支付方式",
        },
        candidates=(more,),
    )
    # recognize_alipay：文案有支付宝但无候选 → uncertain；driver 找更多
    intent = try_tangguo_flow_driver(work)
    assert intent is not None
    assert intent.parameters.get("expand_more_pay") is True or "更多" in str(
        intent.semantic_target
    )


def test_alipay_unavailable_stops() -> None:
    state = TangguoFlowState(
        phase="payment_alipay",
        discovered_bottom_tabs=["首页", "剧库", "投票", "专题", "我的"],
        visited_bottom_tabs=["首页", "剧库", "投票", "专题", "我的"],
        tab_results={
            lab: {"status": "passed", "clicked": True}
            for lab in ["首页", "剧库", "投票", "专题", "我的"]
        },
    )
    state.push_payment_checkpoint(note="test")
    work = _work(
        page_type="payment",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(),
    )
    intent = try_tangguo_flow_driver(work)
    state2 = read_tangguo_flow_state(work)
    assert state2.payment_blocked_reason == "blocked_alipay_unavailable"
    assert state2.stage_results.get("payment_alipay") == "blocked"
    # 支付阻断后仍接续底栏／回首页，不卡死在 payment
    assert state2.phase == "payment_back_home"
    if intent is not None:
        assert intent.action_type is InteractionOperation.BACK


def test_payment_then_continue_remaining_bottom_tabs() -> None:
    """支付子流程结束后继续剩余底部分页。"""
    state = TangguoFlowState(
        phase="payment_alipay",
        payment_farthest_stage="alipay_selected_confirmed",
        alipay_selected=True,
        alipay_available=True,
        discovered_bottom_tabs=["首页", "剧库", "投票", "专题", "我的"],
        visited_bottom_tabs=["首页", "剧库", "我的"],
        pending_bottom_tabs=["投票", "专题"],
        tab_results={
            "首页": {"status": "passed", "clicked": True},
            "剧库": {"status": "passed", "clicked": True},
            "我的": {"status": "passed", "clicked": True},
        },
    )
    state.resume_checkpoint = ResumeCheckpoint(
        phase="bottom_tabs",
        remaining_bottom_tabs=["投票", "专题"],
        note="after_payment",
    )
    # 直接走 finish helper
    from services.api.app.application.app_tangguo_flow_driver import (
        _finish_after_alipay_selected,
    )

    _finish_after_alipay_selected(state)
    assert state.phase == "payment_back_home"
    from services.api.app.application.app_tangguo_flow_driver import _phase_payment_back_home
    _phase_payment_back_home(_work(page_type="home"), state)
    assert state.phase == "bottom_tabs"
    assert "投票" in state.pending_bottom_tabs
    assert "专题" in state.pending_bottom_tabs


def test_alipay_unconfirmed_stops() -> None:
    state = TangguoFlowState()
    code = apply_alipay_selection_verification(
        state,
        ConstrainedLocateResult(
            status="uncertain",
            target=None,
            label=None,
            coordinate=None,
            reason="alipay_seen_but_unconfirmed",
        ),
    )
    assert code == "blocked_alipay_unconfirmed"
    assert state.payment_blocked_reason == "blocked_alipay_unconfirmed"


def test_alipay_cashier_jump_not_payment_success() -> None:
    assert alipay_jump_is_not_payment_success("alipay_cashier") is True
    tabs = ["首页", "剧库", "投票", "专题", "我的"]
    state = TangguoFlowState(
        phase="payment_alipay",
        discovered_bottom_tabs=list(tabs),
        visited_bottom_tabs=list(tabs),
        tab_results={lab: {"status": "passed", "clicked": True} for lab in tabs},
    )
    state.push_payment_checkpoint(note="test")
    work = _work(
        page_type="unknown",
        metadata={
            "tangguo_flow_state": state.to_dict(),
            "visual_summary": "支付宝收银台",
        },
    )
    classified = recognize_page_state(work)
    assert classified.page == "alipay_cashier"
    intent = try_tangguo_flow_driver(work)
    state2 = read_tangguo_flow_state(work)
    assert state2.payment_farthest_stage == "alipay_cashier"
    assert state2.stage_results.get("payment_alipay") == "passed"
    report = state2.as_report()
    assert report.get("payment_farthest_stage") == "alipay_cashier"
    assert "paid" not in str(report).casefold()
    # 跳转成功≠付款成功；可接续回首页 BACK
    if intent is not None:
        assert intent.action_type is InteractionOperation.BACK


def test_inspect_only_blocks_pay_and_order() -> None:
    assert inspect_only_blocks_payment(
        payment_mode="inspect_only",
        allow_pending_order_creation=False,
        action_kind="confirm_pay",
    )
    assert inspect_only_blocks_payment(
        payment_mode="inspect_only",
        allow_pending_order_creation=False,
        action_kind="create_order",
    )
    assert not inspect_only_blocks_payment(
        payment_mode="inspect_only",
        allow_pending_order_creation=True,
        action_kind="create_order",
    )
    assert not inspect_only_blocks_payment(
        payment_mode="sandbox_pay",
        allow_pending_order_creation=False,
        action_kind="confirm_pay",
    )


def test_budget_exhausted_finite_end() -> None:
    state = TangguoFlowState(
        phase="video_entry",
        budgets=TangguoBudgets(device_actions_max=0, ai_calls_max=0),
        device_actions_used=0,
        ai_calls_used=0,
        stage_results={"home": "passed"},
    )
    # budget_exhausted: used >= max → with max 0 and used 0, used >= max is True
    assert state.budget_exhausted()
    work = _work(metadata={"tangguo_flow_state": state.to_dict()})
    intent = try_tangguo_flow_driver(work)
    assert intent is None
    state2 = read_tangguo_flow_state(work)
    assert state2.phase == "done"
    assert state2.stop_reason == "budget_exhausted"


def test_recovery_does_not_reset_budget() -> None:
    prior = {
        "tangguo_flow_state": TangguoFlowState(
            device_actions_used=5,
            ai_calls_used=3,
            phase="back_home",
        ).to_dict()
    }
    merged = merge_flow_state_from_intent_parameters(
        prior_metadata=prior,
        parameters={
            "tangguo_flow_state": TangguoFlowState(
                device_actions_used=6,
                ai_calls_used=3,
                phase="back_home",
                recoveries_used=1,
            ).to_dict()
        },
    )
    state = TangguoFlowState.from_dict(merged["tangguo_flow_state"])
    assert state.device_actions_used == 6
    assert state.ai_calls_used == 3
    assert state.recoveries_used == 1


def test_recognize_wallet_skips_withdraw() -> None:
    withdraw = _tap(
        candidate_id="wd",
        label="提现",
        bounds={"left": 0.1, "top": 0.3, "right": 0.9, "bottom": 0.38},
    )
    work = _work(page_type="profile", candidates=(withdraw,))
    result = recognize_wallet_entry(work)
    assert result.status == "not_found"


def test_recognize_video_rejects_top_category_latest() -> None:
    """上方分类「最新」即使同屏有今日新剧文案，也不得当影片入口。"""

    top = _tap(
        candidate_id="top-latest",
        label="上方分类「最新」",
        bounds={"left": 0.20, "top": 0.11, "right": 0.32, "bottom": 0.16},
        home_top_category_tab_seek=False,
    )
    # 故意污染 purpose，模拟同屏「今日新剧」泄漏进 blob。
    top = CandidateAction(
        candidate_id=top.candidate_id,
        semantic_target=top.semantic_target,
        action_type=top.action_type,
        risk_level=top.risk_level,
        exploration_value=top.exploration_value,
        navigation_depth=top.navigation_depth,
        expected_surface_kind=top.expected_surface_kind,
        metadata={
            **dict(top.metadata),
            "purpose": "今日新剧区域旁的上方分类最新",
            "home_content_entry_seek": True,
        },
    )
    card = _tap(
        candidate_id="card-1",
        label="立即看",
        bounds={"left": 0.08, "top": 0.58, "right": 0.30, "bottom": 0.76},
        home_content_entry_seek=True,
    )
    result = recognize_video_entry(_work(candidates=(top, card)))
    assert result.status == "found"
    assert result.label == "立即看"


def test_member_badge_poster_still_counts_as_video_entry() -> None:
    """今日新剧海报常带「会员」角标，不得因此整卡跳过导致首页有片不点。"""
    poster = _tap(
        candidate_id="poster-1",
        label="种马系统重生",
        bounds={"left": 0.04, "top": 0.58, "right": 0.30, "bottom": 0.76},
    )
    poster = CandidateAction(
        candidate_id=poster.candidate_id,
        semantic_target=poster.semantic_target,
        action_type=poster.action_type,
        risk_level=poster.risk_level,
        exploration_value=poster.exploration_value,
        navigation_depth=poster.navigation_depth,
        expected_surface_kind=poster.expected_surface_kind,
        metadata={
            **dict(poster.metadata),
            "text": "种马系统重生 会员 最新",
            "expected_page_type": "video_detail",
        },
    )
    result = recognize_video_entry(_work(candidates=(poster,)))
    assert result.status == "found"
    assert result.coordinate is not None
    assert "种马" in str(result.label or "")


@pytest.mark.parametrize("attempted,expected", [((), "watch"), (("立即看",), "poster")])
def test_video_entry_uses_visual_candidate_id_without_agent_bounds(attempted, expected) -> None:
    from services.api.app.application.app_screenshot_exploration import _map_candidate
    from services.api.app.domain.exploration.app_visual import AppVisualCandidate, NormalizedBounds

    candidates = tuple(
        _map_candidate(
            AppVisualCandidate.build(
                source_capture_id="cap-1",
                source_image_signature="sha256:" + "a" * 64,
                source_width=1440,
                source_height=3040,
                source_orientation="portrait",
                semantic_name=label,
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                confidence=confidence,
                purpose="播放短剧",
                expected_state_hint="video_detail",
                normalized_bounds=NormalizedBounds(0.04, 0.50, 0.22, 0.54),
            ),
            navigation_depth=0,
            surface_kind=SurfaceKind.ANDROID,
        )
        for label, confidence in (
            ("新剧卡片猎王重生", 0.99), ("立即看", 0.85),
        )
    )
    assert all("normalized_bounds" not in item.metadata for item in candidates)
    state = TangguoFlowState(phase="video_entry", attempted_video_labels=list(attempted))
    intent = try_tangguo_flow_driver(_work(
        metadata={"tangguo_flow_state": state.to_dict()}, candidates=candidates,
    ))
    assert intent is not None
    selected = candidates[1 if expected == "watch" else 0]
    assert intent.parameters["candidate_id"] == selected.candidate_id
    assert "normalized_bounds" not in intent.parameters
    assert intent.parameters["home_content_entry_seek"] is True
    assert intent.parameters["tangguo_flow_state"]["phase"] == "video_verify"


def test_video_entry_falls_back_to_first_card_when_no_candidates() -> None:
    state = TangguoFlowState(phase="video_entry")
    work = _work(
        page_type="home",
        metadata={
            "tangguo_flow_state": state.to_dict(),
            "visual_summary": "推荐 今日新剧 立即看 种马系统 猎王重生",
        },
        candidates=(),
    )
    intent = try_tangguo_flow_driver(work)
    assert intent is not None
    assert intent.action_type is InteractionOperation.CLICK
    assert "第一片" in str(intent.semantic_target) or "影片" in str(intent.purpose)
    assert intent.parameters.get("home_content_entry_seek") is True


def test_recognize_video_excludes_attempted() -> None:
    a = _tap(
        candidate_id="a",
        label="立即看A",
        bounds={"left": 0.1, "top": 0.6, "right": 0.3, "bottom": 0.75},
        home_content_entry_seek=True,
    )
    b = _tap(
        candidate_id="b",
        label="立即看B",
        bounds={"left": 0.35, "top": 0.6, "right": 0.55, "bottom": 0.75},
        home_content_entry_seek=True,
    )
    work = _work(candidates=(a, b))
    first = recognize_video_entry(work)
    assert first.status == "found"
    second = recognize_video_entry(
        work, attempted_labels=(first.label or "",)
    )
    # 排除后应换另一张或 not_found
    if second.status == "found":
        assert second.label != first.label


def test_alipay_selected_helper() -> None:
    state = TangguoFlowState()
    code = apply_alipay_selection_verification(
        state,
        ConstrainedLocateResult(
            status="found",
            target="alipay",
            label="支付宝",
            coordinate=(0.5, 0.5),
            reason="selected",
        ),
    )
    assert code == "selected_confirmed"
    assert state.alipay_selected is True


def test_top_tabs_run_before_bottom_tabs() -> None:
    """上方分类在底部分页之前执行。"""
    rec = _tap(
        candidate_id="t-rec",
        label="推荐",
        bounds={"left": 0.05, "top": 0.10, "right": 0.18, "bottom": 0.16},
        home_top_category_tab_seek=True,
        selected=True,
    )
    yuan = _tap(
        candidate_id="t-yuan",
        label="原创",
        bounds={"left": 0.22, "top": 0.10, "right": 0.35, "bottom": 0.16},
        home_top_category_tab_seek=True,
    )
    state = TangguoFlowState(
        phase="top_tabs",
        stage_results={"home_video": "passed", "back_home": "passed"},
    )
    work = _work(
        page_type="home",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(rec, yuan),
    )
    intent = try_tangguo_flow_driver(work)
    state2 = read_tangguo_flow_state(work)
    assert state2.phase == "top_tabs"
    assert state2.home_default_top_tab == "推荐"
    assert intent is not None
    assert intent.semantic_target == "原创"
    assert intent.parameters.get("home_top_category_tab_seek") is True


def test_top_tabs_none_skipped() -> None:
    state = TangguoFlowState(
        phase="top_tabs",
        budgets=TangguoBudgets(top_tab_scroll_max=0),
        stage_results={"home_video": "passed"},
    )
    work = _work(
        page_type="home",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(),
    )
    try_tangguo_flow_driver(work)
    state2 = read_tangguo_flow_state(work)
    assert state2.stage_results.get("top_tabs") == "skipped"
    assert state2.phase in {"top_tab_restore", "bottom_tabs"}


def test_top_tabs_dedupe_by_label() -> None:
    a = _tap(
        candidate_id="a1",
        label="短剧",
        bounds={"left": 0.1, "top": 0.10, "right": 0.2, "bottom": 0.16},
        home_top_category_tab_seek=True,
        resource_id="tab/short",
    )
    b = _tap(
        candidate_id="a2",
        label="短剧",
        bounds={"left": 0.4, "top": 0.10, "right": 0.5, "bottom": 0.16},
        home_top_category_tab_seek=True,
        resource_id="tab/short2",
    )
    work = _work(page_type="home", candidates=(a, b))
    tabs = recognize_top_tabs(work)
    assert len(tabs) == 1


def test_top_tab_restore_default() -> None:
    rec = _tap(
        candidate_id="t-rec",
        label="推荐",
        bounds={"left": 0.05, "top": 0.10, "right": 0.18, "bottom": 0.16},
        home_top_category_tab_seek=True,
    )
    state = TangguoFlowState(
        phase="top_tab_restore",
        home_default_top_tab="推荐",
        visited_top_tabs=["原创", "短剧"],
        stage_results={"top_tabs": "passed"},
    )
    work = _work(
        page_type="home",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(rec,),
    )
    intent = try_tangguo_flow_driver(work)
    assert intent is not None
    assert intent.semantic_target == "推荐"
    # 下一帧已选中则进入底栏
    selected = _tap(
        candidate_id="t-rec2",
        label="推荐",
        bounds={"left": 0.05, "top": 0.10, "right": 0.18, "bottom": 0.16},
        home_top_category_tab_seek=True,
        selected=True,
    )
    work2 = _work(
        page_type="home",
        metadata={"tangguo_flow_state": read_tangguo_flow_state(work).to_dict()},
        candidates=(selected,),
    )
    try_tangguo_flow_driver(work2)
    state2 = read_tangguo_flow_state(work2)
    assert state2.stage_results.get("top_tab_restore") == "passed"
    assert state2.phase == "bottom_tabs"


def test_comic_detail_not_counted_as_reader() -> None:
    work = _work(
        page_type="comic_detail",
        metadata={"visual_summary": "漫画 开始阅读 目录 章节"},
    )
    assert is_comic_detail_not_reader(work) is True
    classified = recognize_page_state(work)
    assert classified.page == "comic_detail"


def test_comic_scroll_and_resume() -> None:
    state = TangguoFlowState(
        phase="comic_scroll",
        comic_flow_status="in_progress",
        comic_title="测试漫",
        resume_checkpoint=ResumeCheckpoint(
            phase="top_tabs", remaining_top_tabs=["短剧"]
        ),
        budgets=TangguoBudgets(comic_read_swipes=3),
    )
    work = _work(
        page_type="comic_reader",
        metadata={
            "tangguo_flow_state": state.to_dict(),
            "visual_summary": "漫画 阅读中 第1话",
        },
    )
    intent = try_tangguo_flow_driver(work)
    state2 = read_tangguo_flow_state(work)
    assert intent is not None
    assert intent.action_type is InteractionOperation.SCROLL
    assert state2.comic_swipes_done == 1
    assert intent.parameters.get("comic_read_scroll") is True

    # 每一滑都经过新截图验证，包括最后一次；不得手动补写成功计数。
    for index in range(1, 4):
        work_next = _work(
            capture_id=f"comic-after-{index}",
            page_type="comic_reader",
            metadata={
                "tangguo_flow_state": intent.parameters["tangguo_flow_state"],
                "comic_scroll_content_changed": True,
            },
        )
        intent = try_tangguo_flow_driver(work_next)
    state3 = read_tangguo_flow_state(work_next)
    assert state3.comic_flow_status == "passed"
    assert state3.comic_swipes_effective == 3
    assert state3.phase == "comic_back_home"


def test_comic_once_per_task_no_retrigger() -> None:
    comic = _tap(
        candidate_id="c1",
        label="热门漫画",
        bounds={"left": 0.2, "top": 0.4, "right": 0.8, "bottom": 0.5},
        comic_entry_seek=True,
        text="漫画",
    )
    state = TangguoFlowState(
        phase="top_tabs",
        comic_flow_status="passed",
        comic_outcome="read_swipes_done",
        home_default_top_tab="推荐",
        visited_top_tabs=["原创"],
    )
    work = _work(
        page_type="home",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(comic,),
    )
    try_tangguo_flow_driver(work)
    state2 = read_tangguo_flow_state(work)
    # 不得再次进入 comic_entry／comic_detail
    assert state2.phase != "comic_detail"
    assert state2.comic_flow_status == "passed"


def test_comic_paywall_blocked() -> None:
    state = TangguoFlowState(
        phase="comic_detail",
        comic_flow_status="in_progress",
        comic_title="付费漫",
    )
    work = _work(
        page_type="payment",
        metadata={
            "tangguo_flow_state": state.to_dict(),
            "visual_summary": "开通会员 付费章节",
        },
    )
    try_tangguo_flow_driver(work)
    state2 = read_tangguo_flow_state(work)
    assert state2.comic_flow_status == "blocked"
    assert state2.comic_outcome == "blocked_paywall"
    assert state2.phase == "comic_back_home"


def test_comic_hit_end_early() -> None:
    state = TangguoFlowState(
        phase="comic_scroll",
        comic_flow_status="in_progress",
        comic_swipes_done=1,
    )
    work = _work(
        page_type="comic_reader",
        metadata={
            "tangguo_flow_state": state.to_dict(),
            "visual_summary": "漫画 阅读中",
            "comic_scroll_content_changed": False,
            "comic_chapter_ended": True,
        },
    )
    try_tangguo_flow_driver(work)
    state2 = read_tangguo_flow_state(work)
    assert state2.comic_hit_end is True
    assert state2.comic_flow_status == "passed"
    assert state2.phase == "comic_back_home"


def test_single_tab_fail_then_continue_others() -> None:
    """单页失败后有限恢复，可继续其余底栏。"""
    state = TangguoFlowState(
        phase="bottom_tabs",
        discovered_bottom_tabs=["首页", "剧库", "投票"],
        pending_bottom_tabs=["剧库", "投票"],
        visited_bottom_tabs=["首页"],
        tab_results={"首页": {"status": "passed", "clicked": True}},
        failed_bottom_tabs=["剧库"],
        stage_results={"home_video": "passed", "top_tabs": "skipped"},
    )
    state.tab_results["剧库"] = {"status": "failed", "clicked": False, "reason": "x"}
    state.sync_pending_bottom_tabs()
    assert "剧库" not in state.pending_bottom_tabs
    assert state.pending_bottom_tabs == ["投票"]
    vote = _tap(
        candidate_id="tab-vote",
        label="投票",
        bounds={"left": 0.4, "top": 0.88, "right": 0.55, "bottom": 0.945},
        bottom_navigation_tab_seek=True,
    )
    work = _work(
        page_type="home",
        metadata={"tangguo_flow_state": state.to_dict()},
        candidates=(vote,),
    )
    intent = try_tangguo_flow_driver(work)
    assert intent is not None
    assert intent.semantic_target == "投票"


def test_report_contains_required_status_fields() -> None:
    state = TangguoFlowState(
        phase="bottom_tabs",
        video_outcome="playback_confirmed",
        stage_results={"home_video": "passed"},
        wallet_search_result="found",
        payment_farthest_stage="alipay_selected",
        bottom_coverage_status="incomplete",
        discovered_bottom_tabs=["首页", "剧库"],
        pending_bottom_tabs=["剧库"],
    )
    report = state.as_report()
    assert report["current_phase"] == "bottom_tabs"
    assert report["video_flow_status"] == "passed"
    assert report["wallet_search_status"] == "found"
    assert report["payment_flow_status"] == "alipay_selected"
    assert report["coverage_status"] == "incomplete"
    assert "phase_budgets" in report
    assert "total_budget" in report
    assert report["payment_mode"] == "inspect_only"
    assert report["payment_method"] == "alipay"


def test_top_tabs_fewer_than_limit_checks_all() -> None:
    """可用分类不足 3 个时检查全部。"""
    tabs = (
        _tap(
            candidate_id="t1",
            label="推荐",
            bounds={"left": 0.05, "top": 0.11, "right": 0.2, "bottom": 0.16},
            home_top_category_tab_seek=True,
        ),
        _tap(
            candidate_id="t2",
            label="短剧",
            bounds={"left": 0.25, "top": 0.11, "right": 0.4, "bottom": 0.16},
            home_top_category_tab_seek=True,
        ),
    )
    work = _work(page_type="home", candidates=tabs)
    found = recognize_top_tabs(work)
    assert len(found) == 2
    limit = TangguoBudgets().top_tab_visit_limit
    assert len(found) <= limit


def test_agent_flow_alias_matches_tangguo_driver() -> None:
    from services.api.app.application.app_tangguo_flow_driver import (
        try_agent_app_flow_driver,
    )
    from services.api.app.application.app_tangguo_flow_state import (
        package_uses_agent_app_flow,
    )

    assert package_uses_agent_app_flow(package_name="com.alz7xm2.drama") is True
    assert package_uses_agent_app_flow(package_name="com.dzu4.app") is True
    work = _work(page_type="home", candidates=())
    # 无候选时可能观察或前进；别名不得抛错
    try_agent_app_flow_driver(work)
