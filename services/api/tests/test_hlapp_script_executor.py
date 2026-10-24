"""HLAPP 只用 hlapp.py 的順序與落點，不採用通用候選框。"""

from services.api.app.application.app_hlapp_script import (
    HLAPP_REQUIRED_FLOW,
    HLAPP_SCRIPT_INDEX_KEY,
    HLAPP_SCRIPT_REASON_CODE,
    _norm_px,
    apply_hlapp_script_progress,
    hlapp_script_override,
    hlapp_script_step_ready,
    hlapp_script_steps,
)
from services.api.app.application.app_screenshot_exploration import (
    scripted_product_step_counts_as_progress,
)
from services.api.app.domain.exploration import AppExplorationGoal, CandidateAction
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.tests.test_maf_explorer_agent import _work


def _hlapp_work(**metadata: object):
    candidates = metadata.pop("candidates", None)
    return _work(
        candidates=candidates if isinstance(candidates, tuple) else (),
        surface_kind=SurfaceKind.ANDROID,
        page_type="home",
        page_state="stable",
        observation_metadata={
            "package_name": "top.qkjzg.zfiufm",
            **metadata,
        },
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("top.qkjzg.zfiufm",),
        ),
        allowed_operations=(
            InteractionOperation.CLICK,
            InteractionOperation.OBSERVE,
            InteractionOperation.SCROLL,
        ),
    )


def _box(top: float, left: float = 0.08) -> dict[str, float]:
    return {"left": left, "top": top, "right": left + 0.40, "bottom": top + 0.04}


def test_dynamic_rule_exploration_is_not_overridden_by_package_script() -> None:
    work = _hlapp_work(app_runtime_policy={"exploration_mode": "dynamic_frontier"})

    assert hlapp_script_override(work) is None


def test_explicit_curriculum_keeps_existing_script() -> None:
    work = _hlapp_work(app_runtime_policy={"exploration_mode": "playbook_curriculum"})

    assert hlapp_script_override(work).parameters["hlapp_script"] is True


def _candidate(candidate_id: str, label: str, *, top: float, left: float = 0.08) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=label,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=10,
        navigation_depth=1,
        metadata={"normalized_bounds": _box(top, left)},
    )


def test_hlapp_script_matches_recorded_points() -> None:
    steps = hlapp_script_steps()
    by_tag = {step.tag: step for step in steps}
    assert by_tag["01"].action is InteractionOperation.OBSERVE
    assert by_tag["01"].semantic == "开屏本体"
    assert by_tag["01"].settle_ms == 2_000
    assert "02" not in by_tag
    assert steps[1].tag == "skip"
    assert by_tag["skip"].x == 0.877 and by_tag["skip"].y == 0.082
    skip_decision = hlapp_script_override(
        _hlapp_work(**{HLAPP_SCRIPT_INDEX_KEY: 1})
    )
    assert skip_decision is not None
    assert skip_decision.parameters.get("splash_skip_after_only") is True
    assert [step.tag for step in steps if step.tag.startswith("x")] == [
        "x03",
        "x04",
        "x05",
        "x06",
        "x07",
        "x08",
        "x09",
        "x10",
    ]
    assert (by_tag["x03"].x, by_tag["x03"].y) == (0.499, 0.701)
    assert (by_tag["x04"].x, by_tag["x04"].y) == (0.497, 0.702)
    assert (by_tag["x08"].x, by_tag["x08"].y) == (0.499, 0.786)
    assert (by_tag["x09"].x, by_tag["x09"].y) == (0.499, 0.270)
    assert (by_tag["x10"].x, by_tag["x10"].y) == (0.501, 0.734)
    assert "13" not in by_tag and "16" not in by_tag and "19" not in by_tag
    assert "31" not in by_tag and "43" not in by_tag
    assert "ai-open" not in by_tag and "detail-back" not in by_tag
    assert (by_tag["today"].x, by_tag["today"].y) == (0.261, 0.128)
    assert (by_tag["big"].x, by_tag["big"].y) == (0.443, 0.129)
    assert (by_tag["hot"].x, by_tag["hot"].y) == (0.609, 0.132)
    assert by_tag["hot"].settle_ms == 12_000
    assert (by_tag["menu"].x, by_tag["menu"].y) == (0.962, 0.130)
    assert (by_tag["ai-menu"].x, by_tag["ai-menu"].y) == (0.790, 0.337)
    assert (by_tag["featured"].x, by_tag["featured"].y) == _norm_px(310, 2768)
    assert (by_tag["library"].x, by_tag["library"].y) == _norm_px(518, 2767)
    assert (by_tag["live"].x, by_tag["live"].y) == _norm_px(716, 2768)
    assert (by_tag["first-live"].x, by_tag["first-live"].y) == _norm_px(382, 1354)
    assert (by_tag["video-back"].x, by_tag["video-back"].y) == _norm_px(71, 211)
    assert (by_tag["live-back"].x, by_tag["live-back"].y) == _norm_px(99, 246)
    assert (by_tag["ai-tech"].x, by_tag["ai-tech"].y) == _norm_px(922, 2776)
    assert (by_tag["circle"].x, by_tag["circle"].y) == _norm_px(1133, 2771)
    assert (by_tag["mine"].x, by_tag["mine"].y) == _norm_px(1336, 2774)
    assert "保存" not in by_tag["mine"].purpose
    assert by_tag["12"].action is InteractionOperation.SCROLL
    order = [step.tag for step in steps]
    ai_at = order.index("ai-menu")
    assert order[ai_at : ai_at + 4] == ["ai-menu", "21", "22", "featured"]
    assert by_tag["21"].action is InteractionOperation.SCROLL
    assert by_tag["22"].action is InteractionOperation.OBSERVE
    assert steps[-1].tag == "mine"
    assert "save-credential" not in by_tag
    required_at = [order.index(tag) for tag in HLAPP_REQUIRED_FLOW]
    assert required_at == sorted(required_at)
    assert HLAPP_REQUIRED_FLOW == (
        "today",
        "big",
        "hot",
        "ai-menu",
        "featured",
        "library",
        "live",
        "ai-tech",
        "circle",
        "mine",
    )


def test_hlapp_script_ignores_generic_candidates_and_other_packages() -> None:
    decision = hlapp_script_override(
        _hlapp_work(
            candidates=(
                _candidate("generic", "乱伦", top=0.40),
            )
        )
    )
    assert decision is not None
    assert decision.parameters["hlapp_script"] is True
    assert decision.parameters["candidate_id"] == "hlapp-script:0:01"
    assert decision.action_type is InteractionOperation.OBSERVE
    assert decision.semantic_target == "开屏本体"
    assert decision.parameters.get("splash_body_evidence") is True

    other = _work(
        candidates=(),
        surface_kind=SurfaceKind.ANDROID,
        observation_metadata={"package_name": "com.hj.ttthree"},
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("com.hj.ttthree",),
        ),
    )
    assert hlapp_script_override(other) is None


def test_hlapp_script_age_gate_uses_recorded_point() -> None:
    age = hlapp_script_override(_hlapp_work(visual_summary="我年满18岁-进入"))
    assert age is not None
    assert age.parameters["candidate_id"] == "hlapp-script:age:age_gate"
    bounds = age.parameters["normalized_bounds"]
    assert (bounds["left"] + bounds["right"]) / 2 == 0.478
    assert (bounds["top"] + bounds["bottom"]) / 2 == 0.706


def test_hlapp_script_taps_load_fail_retry_without_advancing() -> None:
    decision = hlapp_script_override(
        _hlapp_work(
            visual_summary="加载失败，点击重试",
            **{HLAPP_SCRIPT_INDEX_KEY: 3},
        )
    )
    assert decision is not None
    assert decision.parameters["candidate_id"] == "hlapp-script:load_retry:load_retry"
    assert decision.semantic_target == "点击重试"
    bounds = decision.parameters["normalized_bounds"]
    assert round((bounds["left"] + bounds["right"]) / 2, 4) == 0.50
    assert round((bounds["top"] + bounds["bottom"]) / 2, 4) == 0.58
    result: dict[str, object] = {}
    apply_hlapp_script_progress(
        result,
        prior={HLAPP_SCRIPT_INDEX_KEY: 3},
        completed_action=type(
            "Action", (), {"candidate_id": "hlapp-script:load_retry:load_retry"}
        )(),
        action_succeeded=True,
    )
    assert result[HLAPP_SCRIPT_INDEX_KEY] == 3


def test_hlapp_ai_page_swipes_once_then_screenshots() -> None:
    steps = hlapp_script_steps()
    index = next(i for i, step in enumerate(steps) if step.tag == "21")
    swipe = hlapp_script_override(_hlapp_work(**{HLAPP_SCRIPT_INDEX_KEY: index}))
    assert swipe is not None
    assert swipe.action_type is InteractionOperation.SCROLL
    assert swipe.parameters["from"] == {"x": 0.50, "y": 0.84, "space": "normalized_01"}
    assert swipe.parameters["to"] == {"x": 0.50, "y": 0.16, "space": "normalized_01"}
    assert swipe.parameters["duration_ms"] == 600
    shot = hlapp_script_override(_hlapp_work(**{HLAPP_SCRIPT_INDEX_KEY: index + 1}))
    assert shot is not None
    assert shot.action_type is InteractionOperation.OBSERVE
    assert shot.parameters["hlapp_script_tag"] == "22"
    featured = hlapp_script_override(_hlapp_work(**{HLAPP_SCRIPT_INDEX_KEY: index + 2}))
    assert featured is not None
    assert featured.parameters["hlapp_script_tag"] == "featured"


def test_hlapp_popup_taps_follow_the_script_order() -> None:
    steps = hlapp_script_steps()
    index = next(i for i, step in enumerate(steps) if step.tag == "x03")
    first = hlapp_script_override(_hlapp_work(**{HLAPP_SCRIPT_INDEX_KEY: index}))
    assert first is not None
    assert first.parameters["candidate_id"] == f"hlapp-script:{index}:x03"
    assert first.parameters["normalized_bounds"]["top"] < 0.75
    later = hlapp_script_override(_hlapp_work(**{HLAPP_SCRIPT_INDEX_KEY: index + 6}))
    assert later is not None
    assert later.parameters["hlapp_script_tag"] == "x09"


def test_hlapp_popup_verification_does_not_release_the_next_step() -> None:
    steps = hlapp_script_steps()
    today = next(i for i, step in enumerate(steps) if step.tag == "today")
    x03 = next(i for i, step in enumerate(steps) if step.tag == "x03")
    assert hlapp_script_step_ready(
        candidate_id=f"hlapp-script:{x03}:x03",
        action_succeeded=True,
        observation_completed=False,
        observation_reason="obstruction_not_removed",
    ) is True
    assert hlapp_script_step_ready(
        candidate_id="hlapp-script:9:x09",
        action_succeeded=True,
        observation_completed=False,
        observation_reason="after_package_outside_scope",
    ) is False
    assert hlapp_script_step_ready(
        candidate_id=f"hlapp-script:{today}:today",
        action_succeeded=True,
        observation_completed=False,
        observation_reason="intended_effect_unconfirmed",
    ) is True


def test_hlapp_script_advances_only_after_success_and_completes_on_mine() -> None:
    result: dict[str, object] = {}
    apply_hlapp_script_progress(
        result,
        prior={},
        completed_action=type("Action", (), {"candidate_id": "hlapp-script:0:01"})(),
        action_succeeded=True,
    )
    assert result[HLAPP_SCRIPT_INDEX_KEY] == 1
    stalled: dict[str, object] = {}
    apply_hlapp_script_progress(
        stalled,
        prior={HLAPP_SCRIPT_INDEX_KEY: 1},
        completed_action=type("Action", (), {"candidate_id": "hlapp-script:1:skip"})(),
        action_succeeded=False,
    )
    assert stalled[HLAPP_SCRIPT_INDEX_KEY] == 1

    done = hlapp_script_override(
        _hlapp_work(**{HLAPP_SCRIPT_INDEX_KEY: len(hlapp_script_steps())})
    )
    assert done is not None
    assert done.reason_code == HLAPP_SCRIPT_REASON_CODE
    assert done.coverage_summary["hlapp_script_completed"] is True
    assert done.coverage_summary["coverage_complete"] is True
    assert done.coverage_summary["visual_quality_satisfied"] is True


def test_hlapp_screenshot_step_counts_as_progress_so_skip_is_not_cut_off() -> None:
    shot = CandidateAction(
        candidate_id="hlapp-script:0:01",
        semantic_target="开屏本体",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        metadata={"hlapp_script": True},
    )
    assert scripted_product_step_counts_as_progress(shot, action_succeeded=True) is True
    assert scripted_product_step_counts_as_progress(shot, action_succeeded=False) is False
    assert scripted_product_step_counts_as_progress(None, action_succeeded=True) is False
