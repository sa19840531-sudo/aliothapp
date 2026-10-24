"""糖果短剧：清障进首页后先点影片入口，再底栏语意相对巡检。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from services.api.app.agents.explorer.maf_agent import _intent_for_home_content_entry
from services.api.app.application.app_ui_tree_features import (
    home_content_entry_enabled,
    home_content_entry_first_card_bounds,
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


def _work(*, candidates: tuple[CandidateAction, ...] = (), page_state: str = "stable"):
    now = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    observation = SurfaceObservation(
        capture_id="cap-home",
        surface_kind=SurfaceKind.ANDROID,
        location="app://com.alz7xm2.drama/.MainActivity",
        state_version="state-1",
        observed_at=now,
        key_elements=(),
        page_type="home",
        page_state=page_state,
        overlay_signature=None,
        metadata={"package_name": "com.alz7xm2.drama"},
    )
    frontier = FrontierSnapshot(
        snapshot_id="snap-home",
        observation=observation,
        candidates=candidates,
    )
    return build_exploration_work(
        task_id="task-tg",
        session_id="session-tg",
        branch_id="branch-tg",
        objective="糖果首页影片入口",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="cap-home",
        frontier_snapshot_id="snap-home",
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
            required_page_types=("home", "video_detail", "profile"),
            required_milestones=("surface:stable_business_page",),
            allowed_packages=("com.alz7xm2.drama",),
            minimum_business_pages=1,
        ),
    )


def test_tangguo_home_content_entry_features_loaded() -> None:
    assert home_content_entry_enabled(package_name="com.alz7xm2.drama") is True
    bounds = home_content_entry_first_card_bounds(package_name="com.alz7xm2.drama")
    assert bounds is not None
    assert bounds["left"] < bounds["right"]


def test_prefer_watch_now_over_synthetic_card() -> None:
    watch = CandidateAction(
        candidate_id="watch-1",
        semantic_target="立即看",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=50,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"expected_page_type": "video_detail"},
    )
    intent = _intent_for_home_content_entry(
        work=_work(candidates=(watch,)),
        eligible=(watch,),
    )
    assert intent is not None
    assert intent.semantic_target == "立即看"
    assert intent.parameters.get("home_content_entry_seek") is True


def test_soft_popup_mislabel_allows_home_content_entry() -> None:
    """首页底部飘条常被标 popup，但仍应点「立即看」取证（23:41 QC）。"""

    watch = CandidateAction(
        candidate_id="watch-1",
        semantic_target="立即看",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=50,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"expected_page_type": "video_detail"},
    )
    intent = _intent_for_home_content_entry(
        work=_work(candidates=(watch,), page_state="popup"),
        eligible=(watch,),
    )
    assert intent is not None
    assert intent.semantic_target == "立即看"


def test_hard_center_popup_blocks_home_content_entry() -> None:
    close = CandidateAction(
        candidate_id="close-center",
        semantic_target="关闭弹层",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=40,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "关闭弹层下方X",
            "normalized_bounds": {
                "left": 0.45,
                "top": 0.55,
                "right": 0.55,
                "bottom": 0.65,
            },
        },
    )
    watch = CandidateAction(
        candidate_id="watch-1",
        semantic_target="立即看",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=50,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"expected_page_type": "video_detail"},
    )
    intent = _intent_for_home_content_entry(
        work=_work(candidates=(close, watch), page_state="popup"),
        eligible=(close, watch),
    )
    assert intent is None


def test_synthetic_first_card_when_no_semantic_watch() -> None:
    intent = _intent_for_home_content_entry(
        work=_work(candidates=()),
        eligible=(),
    )
    assert intent is not None
    assert str(intent.parameters.get("candidate_id") or "").startswith(
        "forced-pixel-home-content-entry:"
    )
    assert intent.parameters.get("home_content_entry_seek") is True
    assert isinstance(intent.parameters.get("normalized_bounds"), Mapping)


def test_top_category_latest_never_selected_as_home_content_entry() -> None:
    """上方分类「最新」不得被 prefer_markers 误当成影片入口（QC 9ced42b8）。"""

    top_latest = CandidateAction(
        candidate_id="top-latest",
        semantic_target="上方分类「最新」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "normalized_bounds": {
                "left": 0.20,
                "top": 0.11,
                "right": 0.32,
                "bottom": 0.16,
            },
            "expected_page_type": "home",
        },
    )
    watch = CandidateAction(
        candidate_id="watch-1",
        semantic_target="立即看",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=50,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"expected_page_type": "video_detail"},
    )
    intent = _intent_for_home_content_entry(
        work=_work(candidates=(top_latest, watch)),
        eligible=(top_latest, watch),
    )
    assert intent is not None
    assert intent.semantic_target == "立即看"
    assert intent.parameters.get("candidate_id") == "watch-1"


def test_top_category_latest_alone_falls_back_to_first_card() -> None:
    """仅有顶栏「最新」时须合成相对第一卡，禁止点上方分类。"""

    top_latest = CandidateAction(
        candidate_id="top-latest",
        semantic_target="上方分类「最新」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "normalized_bounds": {
                "left": 0.20,
                "top": 0.11,
                "right": 0.32,
                "bottom": 0.16,
            },
        },
    )
    intent = _intent_for_home_content_entry(
        work=_work(candidates=(top_latest,)),
        eligible=(top_latest,),
    )
    assert intent is not None
    assert str(intent.parameters.get("candidate_id") or "").startswith(
        "forced-pixel-home-content-entry:"
    )
    assert "上方分类" not in str(intent.semantic_target or "")
