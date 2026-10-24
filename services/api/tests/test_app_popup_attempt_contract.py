"""旧包夹具不能掩盖当前合同的无效关层预算旁路。"""

import pytest

from services.api.app.agents.explorer.maf_agent import (
    _deterministic_app_coverage_decision,
    _synthetic_hjll_airtest_popup_close_candidate,
    _synthetic_pixel_popup_close_candidate,
)
from services.api.app.application.app_ui_tree_features import load_app_ui_tree_features
from services.api.app.domain.interaction import ActionIntent, InteractionOperation, SurfaceKind
from services.api.tests.test_maf_explorer_agent import _work


def popup_work(*, attempts=0, noop=0, calibrated=True, visible=True,
               exploration_mode="playbook_curriculum"):
    package = load_app_ui_tree_features(playbook_id="hjll")["package_name"]
    facts = {"popup_visible": visible, "dialog_kind": "ad_popup" if visible else "none"}
    if calibrated:
        facts["pixel_cross_close_calibration"] = {
            "normalized_bounds": {"left": 0.46, "top": 0.69, "right": 0.54, "bottom": 0.73},
            "close_kind": "bottom_circular_x",
        }
    return _work(
        candidates=(), surface_kind=SurfaceKind.ANDROID, page_type="home",
        page_state="popup" if visible else "stable",
        observation_metadata={
            "package_name": package, "surface_facts": facts,
            "surface_phase_same_popup_attempt_count": attempts,
            "dismiss_noop_streak": noop,
            "app_runtime_policy": {"exploration_mode": exploration_mode},
        },
        allowed_operations=(InteractionOperation.CLICK, InteractionOperation.OBSERVE),
    )


@pytest.mark.parametrize("attempts,noop", [(8, 0), (9, 0), (0, 8), (0, 9)])
@pytest.mark.parametrize("calibrated", [True, False])
def test_current_product_close_cannot_bypass_attempt_limit(attempts, noop, calibrated):
    work = popup_work(attempts=attempts, noop=noop, calibrated=calibrated)
    assert _synthetic_pixel_popup_close_candidate(work=work) is None
    assert _synthetic_hjll_airtest_popup_close_candidate(work=work) is None
    assert _deterministic_app_coverage_decision(work=work, eligible=()) is None


@pytest.mark.parametrize("attempts", [0, 1, 7])
@pytest.mark.parametrize("calibrated", [True, False])
def test_current_product_can_close_before_limit(attempts, calibrated):
    decision = _deterministic_app_coverage_decision(
        work=popup_work(attempts=attempts, calibrated=calibrated), eligible=(),
    )
    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.CLICK


def test_old_package_and_name_similarity_do_not_enable_product_points():
    from services.api.app.application.app_coverage_page_seek import (
        home_top_category_tabs_incomplete,
    )
    from services.api.app.application.app_ui_tree_features import (
        ordered_home_top_category_tab_labels,
        resolve_product_orchestrator_id,
    )

    for package in ("dark.wwcskrpojs.ebjhyebjbl", "com.example.haijiao", "com.hj.ttthree.clone"):
        assert resolve_product_orchestrator_id(package_name=package) is None
        assert ordered_home_top_category_tab_labels(package_name=package) == ()
        assert home_top_category_tabs_incomplete(
            coverage_metadata={"app_curriculum_progress": {"playbook_id": "hjll"}},
            package_name=package,
        ) is False


def test_other_product_pixel_close_is_not_bound_to_hjll_attempt_limit():
    from dataclasses import replace

    work = popup_work(attempts=8)
    observation = work.frontier.observation
    observation = replace(
        observation, metadata={**observation.metadata, "package_name": "com.example.other"},
    )
    work = replace(work, frontier=replace(work.frontier, observation=observation))
    assert _synthetic_pixel_popup_close_candidate(work=work) is not None


def test_old_attempt_count_does_not_block_work_after_popup_disappears():
    from dataclasses import replace

    from services.api.app.application.app_ui_tree_features import (
        ordered_bottom_tab_labels,
        ordered_home_top_category_tab_labels,
    )
    from services.api.app.domain.exploration import CandidateAction
    from services.api.app.domain.interaction import InteractionRiskLevel

    candidate = CandidateAction(
        candidate_id="ad-evidence", semantic_target="广告落地一致性取证",
        action_type=InteractionOperation.CLICK, risk_level=InteractionRiskLevel.LOW,
        exploration_value=100, navigation_depth=0,
        metadata={"app_ad_landing_consistency": True},
    )
    work = popup_work(attempts=8, visible=False, calibrated=False)
    observation = work.frontier.observation
    package = observation.metadata["package_name"]
    # 先完成已有课纲；否则现行策略应优先栏目，而非本用例待执行的广告补证。
    observation = replace(observation, metadata={
        **observation.metadata,
        "required_page_types": ["home"], "covered_page_types": ["home"],
        "visited_bottom_navigation_labels": list(ordered_bottom_tab_labels(package_name=package)),
        "visited_home_top_category_tabs": list(
            ordered_home_top_category_tab_labels(package_name=package)),
    })
    work = replace(work, frontier=replace(work.frontier, observation=observation))
    decision = _deterministic_app_coverage_decision(work=work, eligible=(candidate,))
    assert isinstance(decision, ActionIntent)
    assert decision.parameters["candidate_id"] == "ad-evidence"


def test_conflicting_popup_facts_are_not_treated_as_cleared():
    from dataclasses import replace

    work = popup_work(attempts=8, visible=False, calibrated=False)
    observation = work.frontier.observation
    observation = replace(observation, metadata={
        **observation.metadata,
        "surface_facts": {"popup_visible": False, "dialog_kind": "ad_popup"},
    })
    work = replace(work, frontier=replace(work.frontier, observation=observation))
    decision = _deterministic_app_coverage_decision(work=work, eligible=())
    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.OBSERVE


@pytest.mark.parametrize("attempts", [0, 1, 7, 8])
def test_dynamic_mode_never_borrows_fixed_product_points(attempts):
    from services.api.app.agents.explorer.maf_agent import (
        _intent_for_home_top_category_sample,
        _intent_for_home_top_category_tabs,
        _is_hjll_airtest_work,
    )

    work = popup_work(attempts=attempts, exploration_mode="dynamic_frontier")
    assert _is_hjll_airtest_work(work) is False
    assert _synthetic_hjll_airtest_popup_close_candidate(work=work) is None
    assert _intent_for_home_top_category_sample(work=work, eligible=()) is None
    assert _intent_for_home_top_category_tabs(work=work, eligible=()) is None


def test_dynamic_fallback_keeps_real_frontier_priority_not_old_curriculum():
    from services.api.app.agents.explorer.maf_agent import _decide_app_fallback
    from services.api.app.domain.exploration import CandidateAction
    from services.api.app.domain.interaction import InteractionRiskLevel

    candidates = tuple(CandidateAction(
        candidate_id=name, semantic_target=name,
        action_type=InteractionOperation.CLICK, risk_level=InteractionRiskLevel.LOW,
        exploration_value=value, navigation_depth=0,
    ) for name, value in (("current-business", 100), ("old-curriculum", 40)))
    work = popup_work(visible=False, calibrated=False, exploration_mode="dynamic_frontier")
    decision = _decide_app_fallback(
        work=work, eligible=candidates, error=RuntimeError("model offline"),
    )
    assert isinstance(decision, ActionIntent)
    assert decision.parameters["candidate_id"] == "current-business"
