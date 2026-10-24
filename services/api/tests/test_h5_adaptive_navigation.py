"""H5 自适应导航发现、复核与知识生命周期测试。"""

from __future__ import annotations

from services.api.app.agents.tools.page_detection_tools import (
    _verified_bottom_navigation_effect,
    _with_adaptive_navigation_candidates,
)
from services.api.app.application.h5_adaptive_navigation import (
    adaptive_navigation_selector,
    discover_h5_adaptive_navigation_patterns,
)
from services.api.app.application.h5_exploration_observation import (
    _interaction_pattern_exploration_value,
)
from services.api.app.domain.exploration.h5_logical_surface import stable_h5_surface_hash
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.domain.page.nav_alias import PageNavAliasMatch
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    _select_structural_navigation_item,
)
from services.api.app.infrastructure.db.h5_interaction_pattern_repository import (
    _next_status,
)


def _snapshot(
    *,
    selector_prefix: str = "x",
    group_selector: str = "#dock",
    labels: tuple[str, ...] = ("首页", "短剧", "暗网", "我的"),
) -> dict:
    elements = []
    for index, label in enumerate(labels, start=1):
        elements.append(
            {
                "selector": f"#{selector_prefix}-{index}",
                "h5_adaptive_navigation_group_selector": group_selector,
                "parent_selector": f"#{selector_prefix}-wrapper-{index}",
                "tag": "div",
                "role": "tab",
                "text": label,
                "aria_selected": "true" if index == 1 else "false",
                "class_tokens": [f"opaque-{selector_prefix}-{index}"],
                "rect": {
                    "x": 8 + (index - 1) * 96,
                    "y": 746,
                    "width": 80,
                    "height": 54,
                },
                "visible": True,
                "interactive": True,
                "disabled": False,
                "read_only": False,
                "content_editable": False,
                "external": False,
                "is_advertisement": False,
                "fixed_or_sticky_ancestor": True,
                "within_navigation": False,
                "style": {"position": "static"},
                "hit_test": {"center_hits_target": True},
            }
        )
    return {
        "viewport": {"width": 390, "height": 800},
        "elements": elements,
    }


def test_css_and_selector_changes_do_not_change_pattern_identity() -> None:
    first = discover_h5_adaptive_navigation_patterns(
        _snapshot(selector_prefix="random-a", group_selector="#root-a")
    )
    second = discover_h5_adaptive_navigation_patterns(
        _snapshot(selector_prefix="random-b", group_selector="#root-b")
    )

    assert len(first) == len(second) == 1
    assert first[0].signature == second[0].signature
    assert first[0].pattern_payload == second[0].pattern_payload
    assert first[0].items[1].selector != second[0].items[1].selector


def test_product_labels_and_custom_tags_do_not_define_pattern_identity() -> None:
    first_snapshot = _snapshot(labels=("首页", "短剧", "暗网", "我的"))
    second_snapshot = _snapshot(labels=("发现", "会员", "消息", "设置"))
    for element in second_snapshot["elements"]:
        element["tag"] = "x-tab-item"

    first = discover_h5_adaptive_navigation_patterns(first_snapshot)
    second = discover_h5_adaptive_navigation_patterns(second_snapshot)

    assert len(first) == len(second) == 1
    assert first[0].signature == second[0].signature
    assert "labels" not in first[0].pattern_payload

    for element in second_snapshot["elements"]:
        element["role"] = "navigationitem"
    assert len(discover_h5_adaptive_navigation_patterns(second_snapshot)) == 1


def test_fixed_two_button_action_bar_is_not_treated_as_navigation() -> None:
    snapshot = _snapshot(labels=("取消", "确认"))
    snapshot["elements"][1]["rect"]["x"] = 286

    assert discover_h5_adaptive_navigation_patterns(snapshot) == ()

    for element in snapshot["elements"]:
        element["within_navigation"] = True
    assert len(discover_h5_adaptive_navigation_patterns(snapshot)) == 1


def test_live_selector_requires_unique_current_pattern_position_and_label() -> None:
    snapshot = _snapshot(selector_prefix="current")
    pattern = discover_h5_adaptive_navigation_patterns(snapshot)[0]

    assert adaptive_navigation_selector(
        snapshot,
        pattern_signature=pattern.signature,
        navigation_index=2,
        navigation_label="短剧",
    ) == "#current-2"
    assert (
        adaptive_navigation_selector(
            snapshot,
            pattern_signature=pattern.signature,
            navigation_index=2,
            navigation_label="错误标签",
        )
        is None
    )

    duplicate = _snapshot(selector_prefix="duplicate", group_selector="#other-dock")
    snapshot["elements"].extend(duplicate["elements"])
    assert (
        adaptive_navigation_selector(
            snapshot,
            pattern_signature=pattern.signature,
            navigation_index=2,
            navigation_label="短剧",
        )
        is None
    )


def test_content_row_without_navigation_placement_is_not_discovered() -> None:
    snapshot = _snapshot()
    for element in snapshot["elements"]:
        element.pop("h5_adaptive_navigation_group_selector")
        element["parent_selector"] = "#content-grid"
        element["fixed_or_sticky_ancestor"] = False
        element["within_navigation"] = False

    assert discover_h5_adaptive_navigation_patterns(snapshot) == ()


def test_external_navigation_member_does_not_hide_internal_branches() -> None:
    snapshot = _snapshot(labels=("首页", "栏目甲", "栏目乙", "栏目丙", "账号"))
    snapshot["viewport"]["height"] = 844
    for index, element in enumerate(snapshot["elements"]):
        element.update(tag="a", role="link", within_navigation=True)
        element["rect"] = {
            "x": 36.59 + index * 73.2,
            "y": 790,
            "width": 24,
            "height": 46,
        }
    external = snapshot["elements"][-1]
    external["external"] = True

    patterns = discover_h5_adaptive_navigation_patterns(snapshot)

    assert len(patterns) == 1
    pattern = patterns[0]
    assert [item.label for item in pattern.items] == ["首页", "栏目甲", "栏目乙", "栏目丙"]
    assert external["selector"] not in [item.selector for item in pattern.items]
    assert adaptive_navigation_selector(
        snapshot,
        pattern_signature=pattern.signature,
        navigation_index=5,
        navigation_label="账号",
    ) is None

    external["hit_test"]["center_hits_target"] = False
    assert discover_h5_adaptive_navigation_patterns(snapshot) == ()
    external["hit_test"]["center_hits_target"] = True
    external["is_advertisement"] = True
    assert discover_h5_adaptive_navigation_patterns(snapshot) == ()


def test_semantic_top_navigation_is_discovered_as_page_tabs() -> None:
    snapshot = _snapshot(
        group_selector="#category-tabs",
        labels=("推荐", "国产", "动漫", "漫画"),
    )
    for index, element in enumerate(snapshot["elements"]):
        element["rect"]["y"] = 92
        element["fixed_or_sticky_ancestor"] = False
        element["within_navigation"] = True
        element["role"] = "tab"
        element["aria_selected"] = "true" if index == 0 else "false"

    patterns = discover_h5_adaptive_navigation_patterns(snapshot)

    assert len(patterns) == 1
    assert patterns[0].pattern_kind == "page_tab_navigation"
    assert patterns[0].pattern_payload["pattern_kind"] == "page_tab_navigation"


def test_adaptive_page_tabs_generate_distinct_same_surface_candidates() -> None:
    snapshot = _snapshot(
        group_selector="#category-tabs",
        labels=("推荐", "国产", "动漫", "漫画"),
    )
    for index, element in enumerate(snapshot["elements"]):
        element["rect"]["y"] = 92
        element["fixed_or_sticky_ancestor"] = False
        element["within_navigation"] = True
        element["role"] = "tab"
        element["aria_selected"] = "true" if index == 0 else "false"
    pattern = discover_h5_adaptive_navigation_patterns(snapshot)[0]

    actions = _with_adaptive_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        patterns=(pattern,),
        pattern_statuses={pattern.signature: "candidate"},
        nav_alias_matches={},
        dom_obstruction_blocks_navigation=False,
    )

    assert [action.target for action in actions] == [
        "internal:page_tab:nth-of-type(2)",
        "internal:page_tab:nth-of-type(3)",
        "internal:page_tab:nth-of-type(4)",
    ]
    assert all(action.expected_next_page_type == "home" for action in actions)
    assert all("adaptive_page_tab" in action.target_keywords for action in actions)
    assert all(action.interaction_pattern_signature == pattern.signature for action in actions)


def test_adaptive_page_tab_is_relocated_from_the_current_snapshot() -> None:
    snapshot = _snapshot(
        selector_prefix="current",
        group_selector="#category-tabs",
        labels=("推荐", "国产", "动漫", "漫画"),
    )
    for index, element in enumerate(snapshot["elements"]):
        element["rect"]["y"] = 92
        element["fixed_or_sticky_ancestor"] = False
        element["within_navigation"] = True
        element["role"] = "tab"
        element["aria_selected"] = "true" if index == 0 else "false"
    pattern = discover_h5_adaptive_navigation_patterns(snapshot)[0]

    selector = _select_structural_navigation_item(
        snapshot,
        target_mode="page_tab_navigation",
        navigation_index=2,
        navigation_label="国产",
        interaction_pattern_signature=pattern.signature,
        operation=InteractionOperation.CLICK,
    )

    assert selector == "#current-2"


def test_page_tab_relocates_after_navigation_group_reflows() -> None:
    snapshot = _snapshot(
        group_selector="#category-tabs", labels=("推荐", "国产", "动漫", "漫画")
    )
    for element in snapshot["elements"]:
        element["rect"]["y"] = 92
        element["fixed_or_sticky_ancestor"] = False
        element["within_navigation"] = True
    signature = discover_h5_adaptive_navigation_patterns(snapshot)[0].signature

    reflowed = _snapshot(
        selector_prefix="reflowed",
        group_selector="#category-tabs",
        labels=("推荐", "国产", "动漫", "漫画"),
    )
    for element in reflowed["elements"]:
        element["rect"]["y"] = 126
        element["rect"]["x"] += 4
        element["fixed_or_sticky_ancestor"] = False
        element["within_navigation"] = True
    assert discover_h5_adaptive_navigation_patterns(reflowed)[0].signature != signature

    assert adaptive_navigation_selector(
        reflowed,
        pattern_signature=signature,
        navigation_index=2,
        navigation_label="国产",
        allow_page_tab_reflow=True,
    ) == "#reflowed-2"


def test_top_navigation_without_tab_or_active_semantics_is_rejected() -> None:
    snapshot = _snapshot(
        group_selector="#account-navigation",
        labels=("首页", "登录", "充值", "帮助"),
    )
    for element in snapshot["elements"]:
        element["rect"]["y"] = 92
        element["fixed_or_sticky_ancestor"] = False
        element["within_navigation"] = True
        element["tag"] = "a"
        element["role"] = "link"
        element["aria_selected"] = None

    assert discover_h5_adaptive_navigation_patterns(snapshot) == ()


def test_fixed_action_toolbar_without_navigation_facts_is_not_discovered() -> None:
    snapshot = _snapshot(labels=("收藏", "分享", "购买", "发送"))
    for element in snapshot["elements"]:
        element["role"] = "button"
        element["aria_selected"] = None
        element["class_tokens"] = ["opaque-action"]

    assert discover_h5_adaptive_navigation_patterns(snapshot) == ()


def test_quarantined_history_falls_back_to_current_snapshot_discovery() -> None:
    pattern = discover_h5_adaptive_navigation_patterns(_snapshot())[0]

    actions = _with_adaptive_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        patterns=(pattern,),
        pattern_statuses={pattern.signature: "quarantined"},
        nav_alias_matches={},
        dom_obstruction_blocks_navigation=False,
    )

    assert len(actions) == 3
    assert {action.interaction_pattern_status for action in actions} == {"quarantined"}
    assert all("adaptive_bottom_navigation" in action.target_keywords for action in actions)
    payload = actions[0].model_dump()
    assert payload["interaction_pattern_signature"] == pattern.signature
    assert payload["interaction_pattern_status"] == "quarantined"


def test_adaptive_navigation_keeps_distinct_branch_with_current_page_type() -> None:
    pattern = discover_h5_adaptive_navigation_patterns(
        _snapshot(labels=("首页", "推荐", "短剧", "我的"))
    )[0]

    actions = _with_adaptive_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        patterns=(pattern,),
        pattern_statuses={pattern.signature: "trusted"},
        nav_alias_matches={
            "推荐": PageNavAliasMatch(
                canonical_code="home",
                page_subtype_code=None,
                matched_term="推荐",
                source_category="首页",
                confidence=0.95,
                match_type="static",
            )
        },
        dom_obstruction_blocks_navigation=False,
    )

    assert [action.target_keywords[1] for action in actions] == ["推荐", "短剧", "我的"]
    recommendation = actions[0]
    assert recommendation.expected_next_page_type == "home"
    assert recommendation.interaction_pattern_status == "trusted"


def test_pattern_lifecycle_requires_cross_task_success_and_quarantines_failures() -> None:
    assert _next_status(success_count=0, failure_count=0, distinct_task_count=0) == "candidate"
    assert _next_status(success_count=1, failure_count=0, distinct_task_count=1) == "shadow"
    assert _next_status(success_count=3, failure_count=0, distinct_task_count=1) == "shadow"
    assert _next_status(success_count=3, failure_count=1, distinct_task_count=2) == "trusted"
    assert _next_status(success_count=1, failure_count=2, distinct_task_count=1) == "quarantined"


def test_pattern_status_only_changes_frontier_priority() -> None:
    assert _interaction_pattern_exploration_value(base_value=82, status="trusted") == 100
    assert _interaction_pattern_exploration_value(base_value=82, status="shadow") == 95
    assert _interaction_pattern_exploration_value(base_value=82, status="candidate") == 82
    assert _interaction_pattern_exploration_value(base_value=82, status="quarantined") == 25
    assert _interaction_pattern_exploration_value(base_value=82, status="deprecated") == 10


def test_adaptive_navigation_effect_accepts_only_signature_bound_runtime_trace() -> None:
    signature = "a" * 64
    before_url = "https://example.test/"
    after_url = "https://example.test/short-video"
    before_identity = "sha256:" + "1" * 64
    after_identity = "sha256:" + "2" * 64
    proof = {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": "trusted",
        "effect": "same_origin_url_change",
        "before_location_hash": stable_h5_surface_hash(before_url),
        "after_location_hash": stable_h5_surface_hash(after_url),
        "origin_hash": stable_h5_surface_hash("https://example.test:443"),
        "navigation_family": "adaptive_snapshot",
        "navigation_index": 2,
        "interaction_pattern_signature": signature,
        "before_identity_fingerprint": before_identity,
        "after_identity_fingerprint": after_identity,
    }
    transition = {
        "protocol_version": "logical_surface_transition.v1",
        "transition_kind": "logical_page_changed",
        "logical_transition_proof": "trusted",
        "reason_code": "trusted_bottom_navigation_url_change",
        "before_identity_fingerprint": before_identity,
        "after_identity_fingerprint": after_identity,
        "bottom_navigation_proof": proof,
    }
    action_log = [
        {
            "action": "click",
            "selector": "#runtime-current-selector",
            "navigation_family": "adaptive_snapshot",
            "navigation_index": 2,
            "interaction_pattern_signature": signature,
            "navigation_effect_verified": True,
            "navigation_effect_reason": "same_origin_url_change",
            "before_url": before_url,
            "after_url": after_url,
            "state_diff": {"logical_surface_transition": transition},
        }
    ]
    intent_payload = {
        "semantic_target": "internal:adaptive_bottom_navigation:nth-of-type(2)",
        "parameters": {
            "target_mode": "bottom_navigation",
            "navigation_family": "adaptive_snapshot",
            "navigation_index": 2,
            "navigation_label": "短剧",
            "interaction_pattern_signature": signature,
        },
    }
    result_payload = {
        "state_diff": {
            "before_location": before_url,
            "after_location": after_url,
            "location_changed": True,
            "logical_surface_transition": transition,
        }
    }

    assert _verified_bottom_navigation_effect(
        action_log=action_log,
        intent_payload=intent_payload,
        result_payload=result_payload,
    )
    action_log[0]["interaction_pattern_signature"] = "b" * 64
    assert not _verified_bottom_navigation_effect(
        action_log=action_log,
        intent_payload=intent_payload,
        result_payload=result_payload,
    )


def test_adaptive_same_url_effect_requires_activation_hashes_and_transition() -> None:
    signature = "a" * 64
    page_url = "https://example.test/"
    before_identity = "sha256:" + "1" * 64
    after_identity = "sha256:" + "2" * 64
    before_activation = "sha256:" + "3" * 64
    after_activation = "sha256:" + "4" * 64
    proof = {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": "trusted",
        "effect": "activation_state_change",
        "before_location_hash": stable_h5_surface_hash(page_url),
        "after_location_hash": stable_h5_surface_hash(page_url),
        "origin_hash": stable_h5_surface_hash("https://example.test:443"),
        "navigation_family": "adaptive_snapshot",
        "navigation_index": 2,
        "interaction_pattern_signature": signature,
        "activation_before_sha256": before_activation,
        "activation_after_sha256": after_activation,
        "before_identity_fingerprint": before_identity,
        "after_identity_fingerprint": after_identity,
    }
    transition = {
        "protocol_version": "logical_surface_transition.v1",
        "transition_kind": "logical_page_changed",
        "logical_transition_proof": "trusted",
        "reason_code": "trusted_bottom_navigation_activation_change",
        "before_identity_fingerprint": before_identity,
        "after_identity_fingerprint": after_identity,
        "bottom_navigation_proof": proof,
    }
    action_log = [
        {
            "action": "click",
            "selector": "#runtime-current-selector",
            "navigation_family": "adaptive_snapshot",
            "navigation_index": 2,
            "interaction_pattern_signature": signature,
            "navigation_effect_verified": True,
            "navigation_effect_reason": "activation_state_change",
            "activation_before_sha256": before_activation,
            "activation_after_sha256": after_activation,
            "before_url": page_url,
            "after_url": page_url,
            "state_diff": {"logical_surface_transition": transition},
        }
    ]
    intent_payload = {
        "semantic_target": "internal:adaptive_bottom_navigation:nth-of-type(2)",
        "parameters": {
            "target_mode": "bottom_navigation",
            "navigation_family": "adaptive_snapshot",
            "navigation_index": 2,
            "navigation_label": "短剧",
            "interaction_pattern_signature": signature,
        },
    }
    result_payload = {
        "state_diff": {
            "before_location": page_url,
            "after_location": page_url,
            "location_changed": False,
            "logical_surface_transition": transition,
        }
    }

    assert _verified_bottom_navigation_effect(
        action_log=action_log,
        intent_payload=intent_payload,
        result_payload=result_payload,
    )
    action_log[0]["activation_after_sha256"] = before_activation
    assert not _verified_bottom_navigation_effect(
        action_log=action_log,
        intent_payload=intent_payload,
        result_payload=result_payload,
    )
