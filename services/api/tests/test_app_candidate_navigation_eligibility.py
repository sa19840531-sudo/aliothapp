"""导航真实性过滤独立于探索模式及课纲进度。"""

import pytest

from services.api.app.application.app_candidate_eligibility import (
    eligible_app_candidates,
    is_app_candidate_eligible,
)
from services.api.app.domain.exploration import CandidateAction
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.tests.test_maf_explorer_agent import _work


def _candidate(metadata):
    return CandidateAction(
        candidate_id="current-navigation",
        semantic_target="当前栏目",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
        metadata=metadata,
    )


def _navigation_work(candidate, mode):
    return _work(
        candidates=(candidate,),
        surface_kind=SurfaceKind.ANDROID,
        allowed_operations=(InteractionOperation.CLICK,),
        observation_metadata={"app_runtime_policy": {"exploration_mode": mode}},
    )


@pytest.mark.parametrize("mode", ["dynamic_frontier", "playbook_curriculum"])
@pytest.mark.parametrize(
    "metadata",
    [
        {"coverage_page_seek_contract": {"protocol_version": "app-coverage-page-seek.v1"}},
        {
            "bottom_navigation_tab_seek": True,
            "normalized_bounds": {
                "left": 0.1,
                "top": 0.1,
                "right": 0.3,
                "bottom": 0.2,
            },
        },
        {"bottom_navigation_tab_seek": True},
        {
            "bottom_navigation_tab_seek": True,
            "normalized_bounds": {
                "left": 0.1,
                "top": 0.9,
                "right": 0.3,
                "bottom": 1.2,
            },
        },
    ],
)
def test_shared_eligibility_rejects_unproven_or_mislabelled_bottom_navigation(mode, metadata):
    candidate = _candidate(metadata)
    work = _navigation_work(candidate, mode)

    eligible, rejected = eligible_app_candidates(work)

    assert eligible == []
    assert candidate.candidate_id in rejected
    assert is_app_candidate_eligible(work, candidate) is False


@pytest.mark.parametrize("mode", ["dynamic_frontier", "playbook_curriculum"])
@pytest.mark.parametrize(
    "metadata",
    [
        {
            "bottom_navigation_tab_seek": True,
            "normalized_bounds": {
                "left": 0.1,
                "top": 0.8,
                "right": 0.3,
                "bottom": 0.9,
            },
        },
        {
            "home_top_category_tab_seek": True,
            "normalized_bounds": {
                "left": 0.1,
                "top": 0.1,
                "right": 0.3,
                "bottom": 0.2,
            },
        },
    ],
)
def test_shared_eligibility_preserves_real_bottom_and_correctly_typed_top_navigation(
    mode, metadata
):
    candidate = _candidate(metadata)
    work = _navigation_work(candidate, mode)

    assert eligible_app_candidates(work)[0] == [candidate]
    assert is_app_candidate_eligible(work, candidate) is True
