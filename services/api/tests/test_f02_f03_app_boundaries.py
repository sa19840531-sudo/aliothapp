"""F02／F03：禁止合成左上角 VISION 点击；Main 不得挂直接 ADB Tool。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.api.app.agents.bootstrap import main_agent_tool_names
from services.api.app.agents.explorer.maf_agent import (
    MafExplorerAgent,
    _authorized_short_drama_exit_back,
    _short_drama_top_left_back_candidate,
)
from services.api.app.agents.tools import app_device_tools  # noqa: F401 - 登记试点 Tool
from services.api.app.agents.tools.registry import default_registry
from services.api.app.application.app_interaction_runtime import (
    _resolve_short_drama_top_left_back_from_intent,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.exploration import (
    AppExplorationGoal,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    FrontierSnapshot,
    NormalizedBounds,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    TimedObservationIntent,
)


class ScriptedChatClient:
    def __init__(self) -> None:
        self.received_messages: list = []

    async def get_response(self, *args, **kwargs):
        raise AssertionError("F02 确定性路径不得调用模型")


def _work(*, candidates=(), page_type="payment", metadata=None):
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    observation = SurfaceObservation(
        capture_id="cap-1",
        surface_kind=SurfaceKind.ANDROID,
        location="app://demo/.Main",
        state_version="state-1",
        observed_at=now,
        key_elements=(),
        page_type=page_type,
        page_state="stable",
        overlay_signature=None,
        metadata={
            "package_name": "com.demo.app",
            "visual_summary": "会员中心套餐页",
            "ocr_text": "会员中心 立即支付",
            "app_runtime_policy": {"exploration_mode": "dynamic_frontier"},
            **(metadata or {}),
        },
    )
    return build_exploration_work(
        task_id="task-1",
        session_id="session-1",
        branch_id="branch-1",
        objective="F02/F03",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="cap-1",
        frontier_snapshot_id="snap-1",
        frontier=FrontierSnapshot(
            snapshot_id="snap-1",
            observation=observation,
            candidates=candidates,
        ),
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.demo.app",),
            allowed_operations=(
                InteractionOperation.CLICK,
                InteractionOperation.TAP,
                InteractionOperation.BACK,
            ),
            max_navigation_depth=4,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
            allowed_route_prefixes=("/",),
        ),
        budget=ExplorationBudget(
            max_actions=20,
            remaining_actions=20,
            max_depth=4,
            remaining_duration_ms=60_000,
        ),
        deadline=now + timedelta(minutes=10),
        app_goal=AppExplorationGoal(
            required_page_types=("home", "payment"),
            required_milestones=("surface:stable_business_page",),
            allowed_packages=("com.demo.app",),
            minimum_business_pages=1,
        ),
    )


def test_f02_no_visual_back_does_not_synthesize_top_left_click() -> None:
    work = _work(candidates=())
    assert _short_drama_top_left_back_candidate(work=work, eligible=()) is None
    authorized = _authorized_short_drama_exit_back(work=work, eligible=())
    assert authorized.action_type is InteractionOperation.BACK
    assert str(authorized.candidate_id).startswith("forced-system-back:")
    assert not str(authorized.candidate_id).startswith("forced-short-drama-top-left-back:")


async def test_f02_membership_without_back_uses_semantic_back_not_coords() -> None:
    work = _work(candidates=())
    # 授权语义 BACK 仍禁止合成左上角坐标点击（F02 硬边界）。
    authorized = _authorized_short_drama_exit_back(work=work, eligible=())
    assert authorized.action_type is InteractionOperation.BACK
    assert str(authorized.candidate_id).startswith("forced-system-back:")
    assert not str(authorized.candidate_id).startswith(
        "forced-short-drama-top-left-back:"
    )
    # 三合同独占后 decide_deterministic 不再走短剧 BACK 兜底，但不得调模型／合成左上角。
    client = ScriptedChatClient()
    decision = await MafExplorerAgent(
        client=client,
        now=lambda: datetime(2026, 10, 23, 12, tzinfo=UTC),
    ).decide_deterministic(work)
    assert isinstance(
        decision, (ExplorationTerminal, TimedObservationIntent, ActionIntent)
    )
    if isinstance(decision, ActionIntent):
        assert decision.parameters.get("short_drama_top_left_back") is not True
        assert not str(decision.parameters.get("candidate_id") or "").startswith(
            "forced-short-drama-top-left-back:"
        )


def test_f02_runtime_refuses_to_rebuild_forced_top_left_as_vision() -> None:
    from types import SimpleNamespace

    intent = ActionIntent(
        task_id="t",
        session_id="s",
        intent_id="i",
        idempotency_key="k",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="cap-1",
        action_type=InteractionOperation.CLICK,
        semantic_target="左上角返回",
        purpose="test",
        capture_profile=CaptureProfilePolicy.for_surface(
            SurfaceKind.ANDROID,
            InteractionOperation.CLICK,
        ),
        deadline=datetime(2026, 10, 23, 12, tzinfo=UTC) + timedelta(minutes=1),
        parameters={
            "short_drama_top_left_back": True,
            "normalized_bounds": {
                "left": 0.02,
                "top": 0.02,
                "right": 0.1,
                "bottom": 0.08,
            },
        },
    )
    analysis = SimpleNamespace(
        capture_id="cap-1",
        image_signature="sig",
        width=1080,
        height=1920,
        orientation="portrait",
    )
    assert (
        _resolve_short_drama_top_left_back_from_intent(
            intent=intent,
            analysis=analysis,  # type: ignore[arg-type]
            candidate_id="forced-short-drama-top-left-back:cap-1",
        )
        is None
    )


def test_f02_real_top_left_back_still_selected() -> None:
    bounds = NormalizedBounds(0.02, 0.02, 0.12, 0.10)
    back = CandidateAction(
        candidate_id="candidate-title-back",
        semantic_target="返回",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=50,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"normalized_bounds": bounds.to_dict(), "text": "返回"},
    )
    work = _work(candidates=(back,))
    chosen = _short_drama_top_left_back_candidate(work=work, eligible=(back,))
    assert chosen is not None
    assert chosen.candidate_id == "candidate-title-back"


def test_f03_main_agent_excludes_direct_adb_tools() -> None:
    registered = {item.name for item in default_registry.list_tool_metadata()}
    assert "app_tap_tool" in registered  # 仍登记，但不进 Main
    main_names = main_agent_tool_names()
    for name in (
        "app_screenshot_tool",
        "app_tap_tool",
        "app_cloud_back_tool",
        "app_cloud_back_twice_tool",
        "app_tap_from_profile_tool",
    ):
        assert name not in main_names
    assert "load_app_product_profile_tool" in main_names
