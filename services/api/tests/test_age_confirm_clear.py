"""年龄确认门禁识别与动态 Frontier 清障优先级。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.api.app.agents.explorer.maf_agent import (
    _decide_app_fallback,
    _dynamic_app_candidate_rank,
)
from services.api.app.application.app_screenshot_exploration import _map_candidate
from services.api.app.application.h5_age_verification import (
    h5_dom_has_supported_age_verification_gate,
    h5_snapshot_age_verification_confirm_selectors,
)
from services.api.app.domain.exploration import (
    AppExplorationGoal,
    AppVisualCandidate,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    FrontierSnapshot,
    NormalizedBounds,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.exploration.age_confirm import (
    AGE_CONFIRM_ENTER_SELECTOR,
    is_age_confirm_candidate,
    text_matches_age_confirm,
    text_matches_age_leave,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)

_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def test_age_confirm_markers_match_common_labels() -> None:
    assert text_matches_age_confirm("confirm_age_18_enter")
    assert text_matches_age_confirm("我年满18岁-进入")
    assert text_matches_age_confirm("我已满18岁")
    assert text_matches_age_confirm("确认已满18岁并进入应用")
    assert not text_matches_age_confirm("关闭广告")
    assert not text_matches_age_confirm("我未满18岁-离开")
    assert not text_matches_age_confirm("未满18岁离开本站")


def test_age_confirm_selector_covers_age_verify_naming() -> None:
    """选择器写死 age-gate/age-confirm 三种命名时，用 age-verify 的站点一个候选都签不出。

    实测 91吃瓜（TJ-043）的年龄门 DOM：
    <div class="age-verify-overlay" id="age-verify-overlay" role="dialog">
      <button class="age-verify-btn age-verify-btn-leave">我未满18岁 - 离开</button>
      <button class="age-verify-btn …">我年满18岁 - 进入</button>
    """
    assert ".age-verify-overlay" in AGE_CONFIRM_ENTER_SELECTOR
    assert ".age-verify-btn" in AGE_CONFIRM_ENTER_SELECTOR
    # 原有三种命名不得回退
    assert ".age-gate" in AGE_CONFIRM_ENTER_SELECTOR
    assert ".age-gate-enter" in AGE_CONFIRM_ENTER_SELECTOR
    assert ".age-confirm-enter" in AGE_CONFIRM_ENTER_SELECTOR


def test_age_confirm_separates_enter_from_leave_with_spaced_labels() -> None:
    """两个按钮共用 age-verify-btn 类，只能靠语义区分；实测文案带空格。"""
    assert text_matches_age_confirm("我年满18岁 - 进入")
    assert not text_matches_age_confirm("我未满18岁 - 离开")
    assert text_matches_age_leave("我未满18岁 - 离开")
    assert not text_matches_age_leave("我年满18岁 - 进入")


def test_auth_gate_prefers_enter_over_leave_button() -> None:
    """同屏两按钮时只点「我年满18岁-进入」，绝不点离开。"""

    assert text_matches_age_confirm("我年满18岁-进入")
    assert not text_matches_age_confirm("我未满18岁-离开")
    assert is_age_confirm_candidate(
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.MEDIUM,
        semantic_target="我年满18岁-进入",
        purpose="确认已满18岁并进入应用",
    )
    assert not is_age_confirm_candidate(
        action_type=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.MEDIUM,
        semantic_target="我未满18岁-离开",
        purpose="未成年用户离开应用",
    )


def test_map_candidate_coerces_age_confirm_medium_to_low() -> None:
    visual = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature="sha256:" + ("a" * 64),
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="confirm_age_18_enter",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.12, 0.72, 0.88, 0.8),
        confidence=0.95,
        risk_level=InteractionRiskLevel.MEDIUM,
        purpose="确认已满18岁并进入应用",
        expected_state_hint="home",
    )

    mapped = _map_candidate(
        visual,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
    )

    assert mapped.risk_level is InteractionRiskLevel.LOW
    assert mapped.metadata.get("age_confirm_clear") is True
    assert is_age_confirm_candidate(
        action_type=mapped.action_type,
        risk_level=mapped.risk_level,
        semantic_target=mapped.semantic_target,
        purpose=mapped.metadata.get("purpose"),
        expected_state_hint=mapped.metadata.get("expected_state_hint"),
    )


def test_dynamic_fallback_prefers_age_confirm_over_observe() -> None:
    age = CandidateAction(
        candidate_id="candidate-age",
        semantic_target="confirm_age_18_enter",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.MEDIUM,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "确认已满18岁并进入应用"},
    )
    observe = CandidateAction(
        candidate_id="candidate-observe",
        semantic_target="观察当前画面",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=10,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "零触控观察"},
    )
    work = _dynamic_work(candidates=(age, observe))

    assert _dynamic_app_candidate_rank(work, age) > _dynamic_app_candidate_rank(
        work, observe
    )
    decision = _decide_app_fallback(
        work=work,
        eligible=(age, observe),
        error=RuntimeError("llm unavailable"),
    )
    assert isinstance(decision, ActionIntent)
    assert decision.parameters["candidate_id"] == "candidate-age"


def _dynamic_work(*, candidates: tuple[CandidateAction, ...]):
    observation = SurfaceObservation(
        capture_id="capture-1",
        surface_kind=SurfaceKind.ANDROID,
        location="app://com.edxuf.euloop/MainActivity",
        state_version="state-1",
        observed_at=_NOW,
        page_type="home",
        page_state="auth_gate",
        metadata={
            "surface_facts": {
                "dialog_kind": "age_verification",
                "popup_visible": True,
                "requires_auth": False,
            },
            "app_runtime_policy": {"exploration_mode": "dynamic_frontier"},
        },
    )
    frontier = FrontierSnapshot(
        snapshot_id="frontier-1",
        observation=observation,
        candidates=candidates,
    )
    return build_exploration_work(
        task_id="task-1",
        session_id="session-1",
        branch_id="branch-1",
        objective="动态探索 App",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-1",
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.edxuf.euloop",),
            allowed_operations=(
                InteractionOperation.CLICK,
                InteractionOperation.OBSERVE,
            ),
            max_navigation_depth=5,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=10,
            remaining_actions=10,
            max_depth=5,
            remaining_duration_ms=60_000,
        ),
        deadline=_NOW + timedelta(minutes=5),
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=2,
            allowed_packages=("com.edxuf.euloop",),
        ),
    )


_TEXT_AGE_GATE_DOM = """
<div class="age-verify-overlay" id="age-verify-overlay" role="dialog" aria-modal="true">
  <div class="age-verify-modal">
    <div class="age-verify-warning"><img src="/images/age-verify-warning.svg" alt=""></div>
    <h2 class="age-verify-heading">请确定你已年满18岁</h2>
    <p class="age-verify-text">该网站包含有年龄限制的内容。</p>
    <div class="age-verify-actions">
      <button type="button" class="age-verify-btn age-verify-btn-leave"
        id="age-verify-leave">我未满18岁 - 离开</button>
      <button type="button" class="age-verify-btn age-verify-btn-enter"
        id="age-verify-enter">我年满18岁 - 进入</button>
    </div>
  </div>
</div>
"""

_IMAGE_AGE_GATE_DOM = """
<div class="age-verify-popup">
  <div class="age-verify-content">
    <img class="age-verify-bg" src="/static/eighteenYearsBg.png" alt="">
    <div class="age-verify-buttons">
      <div class="btn-exit">离开</div>
      <div class="btn-confirm">进入</div>
    </div>
  </div>
</div>
"""


def test_text_age_gate_is_supported() -> None:
    """换皮成文字型门禁后仍要签发，否则整站停在首页一帧。

    实测 91吃瓜（TJ-043）换域名 air.mykxxsyu.cc 后不再用整张背景图，
    图片型判据（btn-confirm/btn-exit/eighteenYearsBg）一条都不中。
    """
    assert h5_dom_has_supported_age_verification_gate(_TEXT_AGE_GATE_DOM) is True


def test_image_age_gate_still_supported() -> None:
    """原图片型门禁不得因新增文字型而回退。"""
    assert h5_dom_has_supported_age_verification_gate(_IMAGE_AGE_GATE_DOM) is True


def test_text_age_gate_requires_both_enter_and_leave() -> None:
    """只有「进入」没有「离开」时不签发，避免把营销弹窗当年龄门。"""
    only_enter = _TEXT_AGE_GATE_DOM.replace(
        '<button type="button" class="age-verify-btn age-verify-btn-leave"\n'
        '        id="age-verify-leave">我未满18岁 - 离开</button>',
        "",
    )
    assert "age-verify-btn-leave" not in only_enter
    assert h5_dom_has_supported_age_verification_gate(only_enter) is False


def test_text_age_gate_ignores_buttons_outside_the_modal() -> None:
    """同名按钮不在门禁三层容器内时不算数。"""
    stray = """
    <div class="promo"><button class="age-verify-btn age-verify-btn-enter">进入</button>
    <button class="age-verify-btn age-verify-btn-leave">离开</button></div>
    """
    assert h5_dom_has_supported_age_verification_gate(stray) is False


def test_text_age_gate_snapshot_returns_only_enter_selector() -> None:
    """快照侧只签发「进入」，绝不把「离开」当确认。"""
    snapshot = {
        "elements": [
            {
                "selector": "#age-verify-leave",
                "tag": "button",
                "class_tokens": ["age-verify-btn", "age-verify-btn-leave"],
                "ancestor_class_tokens": [
                    "age-verify-actions",
                    "age-verify-modal",
                    "age-verify-overlay",
                ],
                "interactive": True,
                "hit_test": {"center_hits_target": True},
            },
            {
                "selector": "#age-verify-enter",
                "tag": "button",
                "class_tokens": ["age-verify-btn", "age-verify-btn-enter"],
                "ancestor_class_tokens": [
                    "age-verify-actions",
                    "age-verify-modal",
                    "age-verify-overlay",
                ],
                "interactive": True,
                "hit_test": {"center_hits_target": True},
            },
        ]
    }

    assert h5_snapshot_age_verification_confirm_selectors(snapshot) == (
        "#age-verify-enter",
    )
