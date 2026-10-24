"""动作后语义证明与调度进展必须一致，动画像素不能清掉无效返回。"""

from services.api.app.application.app_screenshot_exploration import _next_coverage_metadata
from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
)
from services.api.app.domain.exploration import (
    AppExplorationGoal,
    AppVisualAnalysis,
    CandidateAction,
)
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel


def _analysis(index: int, *, popup: bool = False) -> AppVisualAnalysis:
    return AppVisualAnalysis.build(
        capture_id=f"capture-{index}", screenshot_artifact_id=f"artifact-{index}",
        image_signature="sha256:" + str(index) * 64,
        width=1440, height=3040, orientation="portrait",
        package_name="com.example", activity=".MainActivity",
        visual_page_type="payment", visual_summary="会员页",
        key_elements=(), candidates=(), provider_id="fake", model_id="fake",
        page_state="popup" if popup else "stable",
        overlay_signature=f"layer-{index}" if popup else None,
        surface_facts={"popup_visible": True} if popup else {},
    )


def _next(prior, *, index, candidate, outcome, popup=False, succeeded=True):
    return _next_coverage_metadata(
        goal=AppExplorationGoal(
            required_page_types=("home",), required_milestones=("surface:stable",),
            minimum_business_pages=1, allowed_packages=("com.example",),
        ),
        analysis=_analysis(index, popup=popup),
        prior_metadata=prior,
        prior_state_version=_analysis(index - 1).image_signature,
        attempted_action=candidate,
        action_succeeded=succeeded,
        arrival_proven=False,
        action_observation_outcome=outcome,
    )


def _prior():
    return {BOUNDED_EXPLORATION_STATE_KEY: {"max_actions": 30, "no_progress_count": 0}}


def test_unproven_back_counts_no_progress_despite_new_screenshot() -> None:
    back = CandidateAction(
        candidate_id="back", semantic_target="返回",
        action_type=InteractionOperation.BACK, risk_level=InteractionRiskLevel.LOW,
        exploration_value=50, navigation_depth=0,
    )
    prior = _prior()
    for index in (1, 2):
        prior = _next(prior, index=index, candidate=back, outcome="unproven")
        assert prior[BOUNDED_EXPLORATION_STATE_KEY]["no_progress_count"] == index
        assert prior["covered_business_page_count"] == 0

    # 未触控的重验失败不能因新截图清掉前两次无效返回。
    after_revalidation = _next(
        prior, index=3, candidate=None, outcome="failed", succeeded=False,
    )
    assert after_revalidation[BOUNDED_EXPLORATION_STATE_KEY]["no_progress_count"] == 3

    # 已证明的局部交互进展可以清零，不需要新增页型。
    progressed = _next(prior, index=3, candidate=back, outcome="local_state_changed")
    assert progressed[BOUNDED_EXPLORATION_STATE_KEY]["no_progress_count"] == 0


def test_replaced_ad_layers_keep_progress_without_claiming_business_arrival() -> None:
    close = CandidateAction(
        candidate_id="close", semantic_target="关闭弹窗",
        action_type=InteractionOperation.CLICK, risk_level=InteractionRiskLevel.LOW,
        exploration_value=50, navigation_depth=0,
        metadata={"control_role": "dismiss", "expected_state_hint": "close_popup_x"},
    )
    prior = _prior()
    prior[BOUNDED_EXPLORATION_STATE_KEY]["last_popup_fingerprint"] = "layer-0"
    for index in (1, 2, 3):
        prior = _next(
            prior, index=index, candidate=close, outcome="intermediate", popup=True,
        )
        assert prior[BOUNDED_EXPLORATION_STATE_KEY]["no_progress_count"] == 0
        assert prior["covered_business_page_count"] == 0
