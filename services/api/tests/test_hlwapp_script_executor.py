"""黑料网只用 hlwapp.py 的顺序与落点，不采用通用候选框。"""

from services.api.app.application.app_hlwapp_script import (
    HLWAPP_SCRIPT_INDEX_KEY,
    apply_hlwapp_script_progress,
    hlwapp_script_override,
    hlwapp_script_steps,
)
from services.api.app.domain.exploration import AppExplorationGoal, CandidateAction
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.tests.test_maf_explorer_agent import _work


def _hlwapp_work(**metadata: object):
    candidates = metadata.pop("candidates", None)
    return _work(
        candidates=candidates if isinstance(candidates, tuple) else (),
        surface_kind=SurfaceKind.ANDROID,
        page_type="home",
        page_state="stable",
        observation_metadata={
            "package_name": "me.fifeg.dtphkj",
            **metadata,
        },
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("me.fifeg.dtphkj",),
        ),
        allowed_operations=(
            InteractionOperation.CLICK,
            InteractionOperation.OBSERVE,
            InteractionOperation.SCROLL,
            InteractionOperation.BACK,
        ),
    )


def test_dynamic_rule_exploration_is_not_overridden_by_hlwapp_script() -> None:
    work = _hlwapp_work(app_runtime_policy={"exploration_mode": "dynamic_frontier"})

    assert hlwapp_script_override(work) is None


def test_other_package_stays_with_the_generic_agent() -> None:
    work = _work(
        candidates=(),
        surface_kind=SurfaceKind.ANDROID,
        page_type="home",
        page_state="stable",
        observation_metadata={"package_name": "top.qkjzg.zfiufm"},
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("top.qkjzg.zfiufm",),
        ),
    )

    assert hlwapp_script_override(work) is None


def test_hlwapp_script_uses_measured_points_not_candidate_boxes() -> None:
    steps = hlwapp_script_steps()
    by_tag = {step.tag: step for step in steps}
    decoy = CandidateAction(
        candidate_id="model-ad",
        semantic_target="开屏广告",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=10,
        navigation_depth=1,
        metadata={"normalized_bounds": {"left": 0.4, "top": 0.2, "right": 0.6, "bottom": 0.4}},
    )
    play_index = next(index for index, step in enumerate(steps) if step.tag == "24-video-play")
    work = _hlwapp_work(
        **{HLWAPP_SCRIPT_INDEX_KEY: play_index},
        candidates=(decoy,),
    )

    decision = hlwapp_script_override(work)

    assert by_tag["10-latest"].point == (348, 397)
    assert by_tag["13-back"].point == (85, 230)
    assert by_tag["24-video-play"].point == (375, 1525)
    assert decision.parameters["hlwapp_script"] is True
    assert decision.parameters["candidate_id"] == f"hlwapp-script:{play_index}:24-video-play"
    assert decision.parameters["normalized_bounds"]["left"] < 375 / 1440 < decision.parameters[
        "normalized_bounds"
    ]["right"]


def test_browser_evidence_waits_before_cloud_phone_back() -> None:
    steps = hlwapp_script_steps()
    by_tag = {step.tag: step for step in steps}

    assert by_tag["33-browser"].browser_hold is True
    assert by_tag["33-browser"].settle_ms == 20_000
    assert by_tag["34-back"].action is InteractionOperation.BACK
    assert steps.index(by_tag["34-back"]) == steps.index(by_tag["33-browser"]) + 1


def test_script_progress_advances_one_recorded_step() -> None:
    metadata: dict[str, object] = {}
    action = CandidateAction(
        candidate_id="hlwapp-script:0:01-splash",
        semantic_target="黑料网 01-splash",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        metadata={"hlwapp_script": True},
    )

    apply_hlwapp_script_progress(
        metadata,
        prior={},
        completed_action=action,
        action_succeeded=True,
    )

    assert metadata[HLWAPP_SCRIPT_INDEX_KEY] == 1
    assert "hlwapp_script_completed" not in metadata
