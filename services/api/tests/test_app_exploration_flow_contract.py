"""App 探索硬流程契约：开屏／弹层／首页／底栏回归。"""

from __future__ import annotations

from services.api.app.application.app_exploration_flow_contract import (
    APP_FLOW_CONTRACT_VERSION,
    empty_app_flow_contract,
    flow_contract_complete,
    flow_contract_insufficient_reason,
    popup_phase_blocks_bottom_tabs,
    popup_phase_blocks_frontier_exhaustion,
    update_app_flow_contract,
)
from services.api.app.domain.exploration.app_goal import AppExplorationCoverageSnapshot
from services.api.app.domain.exploration.app_terminal import AppExplorationCoverage


def _observed_tabs(*labels: str) -> dict[str, object]:
    return {"trusted_accessibility_controls": {"bottom_navigation": [
        {"label": label, "action_allowed": True} for label in labels
    ]}}


def test_flow_records_startup_frames_and_requires_evidence() -> None:
    contract = update_app_flow_contract(
        None,
        page_state="startup_ad",
        capture_id="cap-1",
        splash_burst=2,
        playbook_tabs=("视频", "吃瓜"),
    )
    assert contract["protocol_version"] == APP_FLOW_CONTRACT_VERSION
    assert contract["startup_ad_seen"] is True
    assert contract["startup_satisfied"] is False
    assert "startup_ad_evidence_missing" in contract["missing_reasons"]

    contract = update_app_flow_contract(
        contract,
        page_state="startup_ad",
        capture_id="cap-2",
        splash_burst=2,
        playbook_tabs=("视频", "吃瓜"),
    )
    assert contract["startup_ad_frame_count"] == 2
    assert contract["startup_satisfied"] is True


def test_flow_popup_requires_close_evidence_or_clear_surface() -> None:
    contract = update_app_flow_contract(
        None,
        page_state="popup",
        overlay_signature="ad:1",
        surface_facts={"popup_visible": True},
        playbook_tabs=("视频",),
    )
    assert contract["popup_seen"] is True
    assert contract["popup_satisfied"] is False

    cleared = update_app_flow_contract(
        contract,
        page_state="stable",
        overlay_signature=None,
        stable_business_page=True,
        package_within_boundary=True,
        action_observation_outcome="obstruction_removed",
        playbook_tabs=("视频",),
        visited_bottom_labels=("视频",),
    )
    assert cleared["popup_close_evidenced"] is True
    assert cleared["popup_satisfied"] is True
    assert cleared["home_arrived"] is True


def test_flow_bottom_tabs_require_every_observed_tab_visited() -> None:
    tabs = ("视频", "吃瓜", "交友", "应用", "我的")
    contract = update_app_flow_contract(
        None,
        page_state="stable",
        stable_business_page=True,
        surface_facts=_observed_tabs(*tabs),
        visited_bottom_labels=("视频", "应用"),
    )
    assert contract["home_arrived"] is True
    assert contract["bottom_tabs_satisfied"] is False
    assert "bottom_tabs_incomplete" in contract["missing_reasons"]
    assert set(contract["missing_bottom_tabs"]) >= {"吃瓜", "交友", "我的"}

    done = update_app_flow_contract(
        contract,
        page_state="stable",
        stable_business_page=True,
        playbook_tabs=tabs,
        visited_bottom_labels=tabs,
    )
    assert done["bottom_tabs_satisfied"] is True
    assert done["complete"] is True
    assert flow_contract_complete(done) is True
    assert flow_contract_insufficient_reason(done) is None


def test_terminal_blocks_completion_when_bottom_tabs_incomplete() -> None:
    snapshot = AppExplorationCoverageSnapshot(
        required_page_types=("video_list",),
        covered_page_types=("video_list",),
        missing_page_types=(),
        blocked_page_types=(),
        required_milestones=(),
        covered_milestones=(),
        missing_milestones=(),
        blocked_milestones=(),
        minimum_business_pages=1,
        covered_business_page_count=1,
        eligible_candidate_count=0,
        pending_action_count=0,
        pending_evidence_count=0,
        frontier_exhausted=True,
        stable_page_reached=True,
        baseline_evidence_complete=True,
    )
    incomplete = AppExplorationCoverage(
        stable_page_reached=True,
        baseline_evidence_complete=True,
        unique_state_count=3,
        minimum_unique_state_count=2,
        eligible_candidate_count=0,
        repeated_state_count=0,
        snapshot=snapshot,
        pending_facts_available=True,
        flow_contract=update_app_flow_contract(
            None,
            page_state="stable",
            stable_business_page=True,
            surface_facts=_observed_tabs("视频", "吃瓜"),
            visited_bottom_labels=("视频",),
        ),
    )
    assert incomplete.flow_contract_complete is False
    assert incomplete.minimum_coverage_reached is False

    complete = AppExplorationCoverage(
        stable_page_reached=True,
        baseline_evidence_complete=True,
        unique_state_count=3,
        minimum_unique_state_count=2,
        eligible_candidate_count=0,
        repeated_state_count=0,
        snapshot=snapshot,
        pending_facts_available=True,
        flow_contract=update_app_flow_contract(
            None,
            page_state="stable",
            stable_business_page=True,
            surface_facts=_observed_tabs("视频", "吃瓜"),
            visited_bottom_labels=("视频", "吃瓜"),
        ),
    )
    assert complete.flow_contract_complete is True
    assert complete.minimum_coverage_reached is True


def test_empty_flow_metadata_keeps_legacy_compatible() -> None:
    assert flow_contract_complete(None) is True
    assert flow_contract_complete({}) is True
    assert empty_app_flow_contract(playbook_tabs=("首页",))["startup_satisfied"] is True


def test_flow_distinct_visible_labels_do_not_share_visit_evidence() -> None:
    contract = update_app_flow_contract(
        None,
        page_state="stable",
        page_type="home",
        stable_business_page=True,
        surface_facts=_observed_tabs("首页", "短剧", "我的"),
        visited_bottom_labels=("主页", "视频", "我的"),
    )
    assert contract["bottom_tabs_satisfied"] is True
    assert contract["missing_bottom_tabs"] == []


def test_popup_phase_blocks_frontier_exhaustion_while_unsatisfied() -> None:
    """弹窗已见未清：禁止底栏，也禁止 Frontier 排空收口。"""

    meta = {
        "app_flow_contract": update_app_flow_contract(
            None,
            page_state="popup",
            overlay_signature="ad:grid",
            surface_facts={"popup_visible": True},
            playbook_tabs=("主页", "短剧"),
        )
    }
    assert popup_phase_blocks_bottom_tabs(meta) is True
    assert popup_phase_blocks_frontier_exhaustion(meta) is True

    cleared = {
        "app_flow_contract": update_app_flow_contract(
            meta["app_flow_contract"],
            page_state="stable",
            overlay_signature=None,
            stable_business_page=True,
            package_within_boundary=True,
            action_observation_outcome="obstruction_removed",
            playbook_tabs=("主页", "短剧"),
        )
    }
    assert popup_phase_blocks_bottom_tabs(cleared) is False
    assert popup_phase_blocks_frontier_exhaustion(cleared) is False


def test_analysis_blocks_home_top_when_hard_popup_even_if_page_stable() -> None:
    """页型已标 home+stable，但 dialog_kind 真广告模态：上方栏目未露出。"""

    from types import SimpleNamespace

    from services.api.app.application.app_exploration_flow_contract import (
        analysis_blocks_home_top_category_tabs,
        popup_phase_blocks_home_top_category_tabs,
    )

    soft = SimpleNamespace(
        page_state="stable",
        visual_page_type="home",
        surface_facts={"popup_visible": True},
        overlay_signature="float:side-ad",
    )
    # 软浮层不得禁上方分类（右侧角标／底飘时栏已露出）。
    assert analysis_blocks_home_top_category_tabs(soft) is False

    # Vision 误把国庆浮动贴标成 page_state=popup，但主页栏／底栏已露出：仍须放行「最新」。
    soft_mislabeled_popup = SimpleNamespace(
        page_state="popup",
        visual_page_type="home",
        surface_facts={
            "popup_visible": True,
            "home_top_selected_label": "推荐",
            "pixel_selected_labels": ["主页"],
        },
        overlay_signature="float:national-day",
        candidates=(),
    )
    assert analysis_blocks_home_top_category_tabs(soft_mislabeled_popup) is False

    hard = SimpleNamespace(
        page_state="stable",
        visual_page_type="home",
        surface_facts={"popup_visible": True, "dialog_kind": "ad_popup"},
        overlay_signature="ad:grid",
    )
    assert analysis_blocks_home_top_category_tabs(hard) is True

    hard_state_only = SimpleNamespace(
        page_state="popup",
        visual_page_type="unknown",
        surface_facts={"popup_visible": True},
        overlay_signature="ad:center",
        candidates=(),
    )
    assert analysis_blocks_home_top_category_tabs(hard_state_only) is True

    promo = SimpleNamespace(
        page_state="stable",
        visual_page_type="home",
        visual_summary="至尊终身卡 国庆狂欢全站永久免费 立即抢购",
        key_elements=("至尊终身卡", "立即抢购"),
        surface_facts={"popup_visible": True},
        candidates=(
            SimpleNamespace(
                control_role="dismiss",
                semantic_name="关闭底部横幅",
                purpose="ui_tree:bottom_banner_x",
                normalized_bounds={"top": 0.90, "bottom": 0.96},
            ),
        ),
        overlay_signature="promo:lifetime",
    )
    assert analysis_blocks_home_top_category_tabs(promo) is True
    from services.api.app.application.app_visual_close_calibration import (
        is_no_close_auto_dismiss_promo,
    )

    assert is_no_close_auto_dismiss_promo(promo) is True

    meta = {
        "app_flow_contract": update_app_flow_contract(
            None,
            page_state="popup",
            overlay_signature="ad:grid",
            surface_facts={"popup_visible": True},
            playbook_tabs=("主页", "短剧"),
        )
    }
    assert popup_phase_blocks_home_top_category_tabs(meta) is True
    # 当前帧主页栏已露出：滞后 popup_satisfied 不得禁「最新」。
    assert (
        popup_phase_blocks_home_top_category_tabs(meta, analysis=soft_mislabeled_popup)
        is False
    )
    chrome_meta = {
        **meta,
        "surface_facts": {
            "home_top_selected_label": "推荐",
            "pixel_selected_labels": ["主页"],
        },
        "page_type": "home",
    }
    assert popup_phase_blocks_home_top_category_tabs(chrome_meta) is False


def test_unseen_template_tabs_never_create_required_entries() -> None:
    contract = update_app_flow_contract(
        None, page_state="stable", stable_business_page=True,
        playbook_id="old-product", playbook_tabs=("预设栏目",),
        surface_facts=_observed_tabs("新发现栏目"),
        visited_bottom_labels=("新发现栏目",),
    )
    assert contract["required_bottom_tabs"] == ["新发现栏目"]
    assert contract["playbook_id"] is None
    assert contract["denominator_source"] == "current_observation"
    assert contract["complete"] is True
    assert empty_app_flow_contract(playbook_tabs=("预设栏目",))["required_bottom_tabs"] == []


def test_newly_observed_tabs_expand_denominator_without_template_filtering() -> None:
    first = update_app_flow_contract(
        None, page_state="stable", stable_business_page=True,
        surface_facts=_observed_tabs("先发现"), visited_bottom_labels=("先发现",),
    )
    assert first["complete"] is True
    later = update_app_flow_contract(
        first, page_state="stable", stable_business_page=True,
        surface_facts=_observed_tabs("先发现", "后发现"),
    )
    assert later["required_bottom_tabs"] == ["先发现", "后发现"]
    assert later["missing_bottom_tabs"] == ["后发现"]
    assert later["complete"] is False


def test_legacy_flow_read_remains_strict_but_update_removes_template_denominator() -> None:
    old = {
        "protocol_version": "app-exploration-flow-contract.v1",
        "required": True, "complete": False,
        "required_bottom_tabs": ["从未出现"],
        "observed_bottom_tabs": ["实际入口"],
        "visited_bottom_tabs": ["实际入口"],
    }
    assert flow_contract_complete(old) is False
    updated = update_app_flow_contract(
        old, page_state="stable", stable_business_page=True,
    )
    assert updated["required_bottom_tabs"] == ["实际入口"]
    assert updated["complete"] is True
