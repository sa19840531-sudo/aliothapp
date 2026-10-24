"""上方分类：第一片取证后才记 visited，并挡底栏。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.api.app.application.app_coverage_page_seek import (
    HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY,
    HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY,
    HOME_TOP_SAMPLE_PHASE_NEED_BACK,
    HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD,
    VISITED_HOME_TOP_CATEGORY_TABS_KEY,
    begin_home_top_category_sample,
    complete_home_top_category_sample,
    home_top_category_sample_in_progress,
    home_top_category_tabs_incomplete,
)
from services.api.app.application.app_screenshot_exploration import (
    _next_coverage_metadata,
)
from services.api.app.application.app_ui_tree_features import (
    home_top_category_sample_first_card_bounds,
    home_top_category_sample_first_entry_enabled,
)
from services.api.app.domain.exploration import AppExplorationGoal, CandidateAction
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)
from services.api.tests.support.app_home_top_contract import (
    legacy_home_top_features as legacy_home_top_features,
)

pytestmark = pytest.mark.usefixtures("legacy_home_top_features")

_HJLL_PACKAGE = "dark.wwcskrpojs.ebjhyebjbl"


def test_unknown_package_does_not_inherit_frozen_category_contract():
    assert not home_top_category_sample_first_entry_enabled(package_name="com.unknown.app")
    assert home_top_category_sample_first_card_bounds(package_name="com.unknown.app") is None


def test_hjll_declares_sample_first_entry() -> None:
    assert home_top_category_sample_first_entry_enabled(package_name=_HJLL_PACKAGE)
    assert home_top_category_sample_first_entry_enabled(playbook_id="hjll")
    bounds = home_top_category_sample_first_card_bounds(package_name=_HJLL_PACKAGE)
    assert bounds is not None
    assert bounds["left"] < bounds["right"]
    assert bounds["top"] < bounds["bottom"]


def test_manga_sample_prefers_3d_manga_match_texts() -> None:
    from services.api.app.application.app_ui_tree_features import (
        home_top_category_sample_match_texts,
        home_top_category_sample_target_bounds,
    )

    texts = home_top_category_sample_match_texts(
        "漫画",
        package_name=_HJLL_PACKAGE,
    )
    assert any("3D漫画" in item or "3D漫畫" in item for item in texts)
    # 繁简别名栏名也可解析。
    assert home_top_category_sample_match_texts(
        "漫畫",
        package_name=_HJLL_PACKAGE,
    )
    target = home_top_category_sample_target_bounds(
        "漫画",
        package_name=_HJLL_PACKAGE,
    )
    assert target is not None
    assert target["top"] >= 0.5
    # 非漫画栏无强制文案。
    assert home_top_category_sample_match_texts(
        "最新",
        package_name=_HJLL_PACKAGE,
    ) == ()


def test_begin_sample_does_not_record_visited() -> None:
    begun = begin_home_top_category_sample(
        {VISITED_HOME_TOP_CATEGORY_TABS_KEY: []},
        label="最新",
    )
    assert begun[HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY] == "最新"
    assert begun[HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY] == HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD
    assert begun.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY) == []
    assert home_top_category_sample_in_progress(begun)
    assert home_top_category_tabs_incomplete(
        coverage_metadata=begun,
        package_name=_HJLL_PACKAGE,
    )


def test_complete_sample_records_visited_and_clears_phase() -> None:
    mid = {
        VISITED_HOME_TOP_CATEGORY_TABS_KEY: [],
        HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY: "最新",
        HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY: HOME_TOP_SAMPLE_PHASE_NEED_BACK,
    }
    done = complete_home_top_category_sample(mid)
    assert done[VISITED_HOME_TOP_CATEGORY_TABS_KEY] == ["最新"]
    assert HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY not in done
    assert HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY not in done
    assert not home_top_category_sample_in_progress(done)


def test_sample_in_progress_blocks_bottom_even_if_all_tabs_listed_visited() -> None:
    """全部栏已在 visited 但取证未完时仍 incomplete（挡底栏）。"""

    meta = {
        VISITED_HOME_TOP_CATEGORY_TABS_KEY: [
            "最新",
            "推荐",
            "国产",
            "动漫",
            "漫画",
        ],
        HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY: "漫画",
        HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY: HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD,
        "package_name": _HJLL_PACKAGE,
    }
    assert home_top_category_tabs_incomplete(
        coverage_metadata=meta,
        package_name=_HJLL_PACKAGE,
    )


def test_skip_label_does_not_begin_sample() -> None:
    begun = begin_home_top_category_sample({}, label="乱伦")
    assert HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY not in begun
    assert not home_top_category_sample_in_progress(begun)


def _analysis(*, visual_page_type: str = "home") -> SimpleNamespace:
    return SimpleNamespace(
        capture_id="capture-sample",
        screenshot_artifact_id="artifact-sample",
        image_signature="sig-sample",
        width=1080,
        height=1920,
        orientation="portrait",
        package_name=_HJLL_PACKAGE,
        activity=".MainActivity",
        page_state="stable",
        visual_page_type=visual_page_type,
        surface_facts={},
        overlay_signature=None,
        candidates=(),
        visual_summary="home",
        key_elements=(),
        provider_id="test",
        model_id="test",
    )


def test_next_coverage_tab_click_begins_sample_not_visited() -> None:
    goal = AppExplorationGoal(
        required_page_types=("home", "video_list"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=1,
        allowed_packages=(_HJLL_PACKAGE,),
    )
    tab_action = CandidateAction(
        candidate_id="top-latest",
        semantic_target="上方分类「最新」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        metadata={
            "home_top_category_tab_seek": True,
            "expected_state_hint": "home_top_tab:最新",
            "require_screenshot_evidence": True,
        },
    )
    result = _next_coverage_metadata(
        goal=goal,
        analysis=_analysis(visual_page_type="home"),
        prior_metadata={
            VISITED_HOME_TOP_CATEGORY_TABS_KEY: [],
            "package_name": _HJLL_PACKAGE,
            "app_flow_contract": {
                "popup_satisfied": True,
                "startup_satisfied": True,
            },
            "covered_page_types": ["home"],
            "required_page_types": ["home", "video_list"],
        },
        attempted_action=tab_action,
        completed_action=tab_action,
        action_succeeded=True,
        arrival_proven=True,
        action_observation_outcome="tab_seek_screenshot_evidenced",
    )
    assert result.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY) in ([], None) or list(
        result.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY) or ()
    ) == []
    assert result.get(HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY) == "最新"
    assert result.get(HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY) == (
        HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD
    )


def test_next_coverage_back_home_completes_sample() -> None:
    goal = AppExplorationGoal(
        required_page_types=("home", "video_list"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=1,
        allowed_packages=(_HJLL_PACKAGE,),
    )
    back_action = CandidateAction(
        candidate_id="sample-back",
        semantic_target="系统 BACK",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        metadata={
            "home_top_category_sample_seek": True,
            "home_top_category_sample_phase": HOME_TOP_SAMPLE_PHASE_NEED_BACK,
            "forced_home_top_sample_back": True,
        },
    )
    result = _next_coverage_metadata(
        goal=goal,
        analysis=_analysis(visual_page_type="home"),
        prior_metadata={
            VISITED_HOME_TOP_CATEGORY_TABS_KEY: [],
            HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY: "最新",
            HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY: HOME_TOP_SAMPLE_PHASE_NEED_BACK,
            "package_name": _HJLL_PACKAGE,
            "app_flow_contract": {
                "popup_satisfied": True,
                "startup_satisfied": True,
            },
            "covered_page_types": ["home"],
            "required_page_types": ["home", "video_list"],
        },
        attempted_action=back_action,
        completed_action=back_action,
        action_succeeded=True,
        arrival_proven=True,
        action_observation_outcome="local_state_changed",
    )
    assert list(result.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY) or ()) == ["最新"]
    assert result.get(HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY) is None
    assert result.get(HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY) is None
