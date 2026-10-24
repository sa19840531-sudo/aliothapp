"""糖果短剧限步无状态清障工具（①②③）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.api.app.application.app_bounded_dismiss_tool import (
    POPUP_MAX_STEPS,
    SPLASH_MAX_STEPS,
    judge_dismiss_frame,
    package_uses_bounded_dismiss,
    try_bounded_dismiss_tool,
)
from services.api.app.application.app_screenshot_exploration_phase import (
    next_bounded_dismiss_counters,
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
    page_type: str = "home",
    page_state: str = "stable",
    metadata: dict | None = None,
    candidates: tuple[CandidateAction, ...] = (),
):
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    observation = SurfaceObservation(
        capture_id="cap-1",
        surface_kind=SurfaceKind.ANDROID,
        location="app://com.alz7xm2.drama/.MainActivity",
        state_version="state-1",
        observed_at=now,
        key_elements=(),
        page_type=page_type,
        page_state=page_state,
        overlay_signature=None,
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
        objective="糖果短剧限步清障试点",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="cap-1",
        frontier_snapshot_id="snap-1",
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.alz7xm2.drama",),
            allowed_operations=(
                InteractionOperation.CLICK,
                InteractionOperation.TAP,
                InteractionOperation.OBSERVE,
                InteractionOperation.BACK,
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
            required_page_types=("home", "video_detail"),
            required_milestones=("surface:stable_business_page",),
            allowed_packages=("com.alz7xm2.drama",),
            minimum_business_pages=1,
        ),
    )


def _close_candidate() -> CandidateAction:
    from services.api.app.application.app_synthetic_touch_contracts import (
        popup_close_corner_contract,
    )
    from services.api.app.domain.exploration import NormalizedBounds

    purpose = "点弹层像素检出关闭控件，立即关闭并截图；禁止只观察／点广告本体"
    bounds = NormalizedBounds(0.45, 0.68, 0.55, 0.74)
    return CandidateAction(
        candidate_id="close-1",
        semantic_target="关闭弹层下方X",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "dismiss",
            "purpose": purpose,
            "expected_state_hint": "close_popup_x",
            "normalized_bounds": bounds.to_dict(),
            "popup_close_corner_fallback": True,
            "popup_close_pixel_visual": True,
            "popup_close_corner_contract": popup_close_corner_contract(
                bounds=bounds,
                purpose=purpose,
                confidence=0.95,
            ),
        },
    )


def _skip_candidate() -> CandidateAction:
    return CandidateAction(
        candidate_id="skip-1",
        semantic_target="跳过",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "skip",
            "expected_state_hint": "tap_skip",
            "normalized_bounds": {
                "left": 0.72,
                "top": 0.04,
                "right": 0.94,
                "bottom": 0.10,
            },
        },
    )


def _cloud_back_candidate() -> CandidateAction:
    return CandidateAction(
        candidate_id="cloud-back-1",
        semantic_target="底部导航栏「<」关闭弹窗",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "cloud_back_tap_twice 关窗",
            "cloud_back_popup_close": True,
            "expected_state_hint": "home",
        },
    )


def test_all_packages_use_bounded_dismiss() -> None:
    # 所有产品统一采用通用限步清障，历史产品也不例外。
    assert package_uses_bounded_dismiss(package_name="com.alz7xm2.drama") is True
    assert package_uses_bounded_dismiss(package_name="com.other.app") is True
    assert package_uses_bounded_dismiss(package_name="com.dzu4.app") is True
    assert package_uses_bounded_dismiss(package_name="dark.nvycy.ambpu") is True


def test_splash_picks_skip_within_three_steps() -> None:
    work = _work(
        page_type="startup_ad",
        page_state="startup_ad",
        candidates=(_skip_candidate(),),
    )
    intent = try_bounded_dismiss_tool(work)
    assert intent is not None
    assert intent.action_type is InteractionOperation.CLICK
    assert intent.parameters.get("bounded_dismiss_tool") is True
    assert intent.parameters.get("bounded_dismiss_phase") == "splash"
    assert intent.parameters.get("bounded_dismiss_constrained") is True
    from collections.abc import Mapping

    verdict = intent.parameters.get("bounded_dismiss_verdict")
    assert isinstance(verdict, Mapping)
    assert verdict.get("found") is True
    assert list(verdict.get("coordinate") or ()) == [0.83, 0.07]
    assert f"1/{SPLASH_MAX_STEPS}" in intent.purpose


def test_popup_intent_carries_close_corner_contract() -> None:
    """限步清障点下方 X 必须带合成合同，避免 VISUAL_CANDIDATE_NOT_FOUND。"""

    work = _work(
        page_type="home",
        page_state="popup",
        metadata={"surface_facts": {"dialog_kind": "ad_popup", "popup_visible": True}},
        candidates=(_close_candidate(),),
    )
    intent = try_bounded_dismiss_tool(work)
    assert intent is not None
    assert intent.action_type is InteractionOperation.CLICK
    assert intent.semantic_target == "关闭弹层下方X"
    assert "关闭弹层下方X" in intent.purpose
    from collections.abc import Mapping

    contract = intent.parameters.get("app_popup_close_corner")
    assert isinstance(contract, Mapping)
    assert contract.get("expected_state_hint") == "close_popup_x"
    assert isinstance(contract.get("normalized_bounds"), Mapping)
    verdict = intent.parameters.get("bounded_dismiss_verdict")
    assert isinstance(verdict, Mapping) and verdict.get("found") is True


def test_judge_constrained_output_found_false_without_coords() -> None:
    """③：无关闭坐标 → found:false，交还业务（不发 BACK）。"""

    work = _work(
        page_type="home",
        page_state="popup",
        metadata={"surface_facts": {"dialog_kind": "ad_popup", "popup_visible": True}},
        candidates=(_cloud_back_candidate(),),
    )
    verdict = judge_dismiss_frame(work)
    assert verdict is not None
    assert verdict.found is False
    assert try_bounded_dismiss_tool(work) is None


def test_popup_budget_exhausted_yields_without_back() -> None:
    """① Fail-safe：步数用尽交还业务；③ 禁止 BACK。"""

    for page_type in ("home", "video_detail"):
        work = _work(
            page_type=page_type,
            page_state="popup",
            metadata={
                "bounded_dismiss_popup_steps": POPUP_MAX_STEPS,
                "surface_facts": {"dialog_kind": "ad_popup", "popup_visible": True},
            },
            candidates=(_close_candidate(), _cloud_back_candidate()),
        )
        assert try_bounded_dismiss_tool(work) is None


def test_never_selects_cloud_back_even_when_ranked_higher() -> None:
    """③：即使用户帧里有 cloud_back，清障工具也不得签发 BACK。"""

    work = _work(
        page_type="video_detail",
        page_state="popup",
        metadata={
            "bounded_dismiss_popup_steps": 1,
            "surface_facts": {"dialog_kind": "ad_popup", "popup_visible": True},
        },
        candidates=(_close_candidate(), _cloud_back_candidate()),
    )
    intent = try_bounded_dismiss_tool(work)
    assert intent is not None
    assert intent.action_type is InteractionOperation.CLICK
    assert intent.action_type is not InteractionOperation.BACK


def test_non_obstruction_frame_returns_none() -> None:
    work = _work(
        page_type="home",
        page_state="stable",
        candidates=(_close_candidate(),),
    )
    assert try_bounded_dismiss_tool(work) is None
    assert judge_dismiss_frame(work) is None


def test_bounded_dismiss_counters_persist_max_step() -> None:
    updated = next_bounded_dismiss_counters(
        prior_metadata={"bounded_dismiss_popup_steps": 1},
        phase="popup",
        step=2,
    )
    assert updated["bounded_dismiss_popup_steps"] == 2
    assert updated["bounded_dismiss_splash_steps"] == 0
