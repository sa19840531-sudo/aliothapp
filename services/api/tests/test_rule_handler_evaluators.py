"""规则 Handler 纯求值器测试。"""

import pytest

from services.api.app.application.rule_handler_evaluators import (
    HandlerInputError,
    evaluate_collection_constraint,
    evaluate_geometry_hit_area,
    evaluate_required_ui_capability,
    evaluate_screenshot_change,
    evaluate_temporal_sequence,
    evaluate_visible_text_presence,
    validate_collection_constraint_config,
    validate_geometry_hit_area_config,
    validate_required_ui_capability_config,
    validate_screenshot_change_config,
    validate_temporal_sequence_config,
    validate_visible_text_presence_config,
)
from services.api.app.application.rule_transition_evaluators import (
    evaluate_external_link_open_mode,
    evaluate_state_transition,
    validate_external_link_open_mode_config,
    validate_state_transition_config,
)


def test_visible_text_presence_passes_when_pattern_exists() -> None:
    result = evaluate_visible_text_presence(
        visible_text="评论 12 点赞 8",
        config={"pattern": r"(?:评论|点赞)\s*\d+"},
    )

    assert result.passed is True
    assert result.judge_output["match"] == "评论 12"


def test_visible_text_presence_config_rejects_invalid_absence_conclusion() -> None:
    assert (
        validate_visible_text_presence_config({"pattern": r"\d+", "absence_conclusion": "skipped"})
        == "visible_text_presence.absence_conclusion 必须为 fail/evidence_insufficient"
    )


def test_visible_text_presence_config_rejects_non_boolean_snapshot_fallback() -> None:
    assert (
        validate_visible_text_presence_config(
            {"pattern": r"\d+", "include_ui_snapshot_text": "yes"}
        )
        == "visible_text_presence.include_ui_snapshot_text 必须为布尔值"
    )


def test_screenshot_change_passes_when_payloads_differ() -> None:
    result = evaluate_screenshot_change(
        screenshot_by_role={"before": b"before", "primary": b"after"},
        config={},
    )

    assert result.passed is True
    assert result.judge_output["changed"] is True


def test_screenshot_change_config_requires_distinct_roles() -> None:
    assert (
        validate_screenshot_change_config({"before_role": "primary", "after_role": "primary"})
        == "screenshot_change_assertion.before_role 与 after_role 必须不同"
    )


def test_case_style_constraint_passes_consistent_labels() -> None:
    result = evaluate_collection_constraint(
        judge_input={"labels": ["News", "Sports Center", "Top Videos"]},
        config={
            "constraint": "case_style_consistency",
            "input_field": "labels",
        },
    )

    assert result.passed is True
    assert result.judge_output["styles"] == ["title"]


def test_case_style_constraint_fails_mixed_styles() -> None:
    result = evaluate_collection_constraint(
        judge_input={"items": ["NEWS", "sports"]},
        config={"constraint": "case_style_consistency"},
    )

    assert result.passed is False
    assert result.judge_output["styles"] == ["lower", "upper"]


def test_monotonic_constraint_supports_object_value_field() -> None:
    result = evaluate_collection_constraint(
        judge_input={"items": [{"heat": 100}, {"heat": 90}, {"heat": 90}]},
        config={
            "constraint": "monotonic",
            "direction": "descending",
            "value_field": "heat",
        },
    )

    assert result.passed is True
    assert result.judge_output["values"] == [100.0, 90.0, 90.0]


def test_monotonic_constraint_rejects_boolean_value() -> None:
    with pytest.raises(HandlerInputError, match="有限数值"):
        evaluate_collection_constraint(
            judge_input={"items": [10, True]},
            config={"constraint": "monotonic"},
        )


def test_required_ui_capability_reports_missing_group() -> None:
    result = evaluate_required_ui_capability(
        judge_input={"observed_capabilities": ["upload_limit"]},
        config={
            "required_all_of": ["upload_limit", "format_disclosure"],
            "required_any_groups": [["multi_select", "single_select_disclosed"]],
        },
        visible_text="",
        action_trace_text="[]",
    )

    assert result.passed is False
    assert result.judge_output["missing_required_all"] == ["format_disclosure"]
    assert result.judge_output["missing_required_groups"] == [
        ["multi_select", "single_select_disclosed"]
    ]


def test_collection_config_rejects_invalid_direction() -> None:
    error = validate_collection_constraint_config(
        {"constraint": "monotonic", "direction": "random"}
    )

    assert error == "collection_constraint.direction 必须为 ascending/descending"


def test_required_ui_config_rejects_undeclared_pattern_capability() -> None:
    error = validate_required_ui_capability_config(
        {
            "required_all_of": ["logout_entry"],
            "capability_patterns": {"other": "退出"},
        }
    )

    assert error == "capability_patterns 包含未声明能力：other"


def test_temporal_sequence_passes_real_time_countdown() -> None:
    result = evaluate_temporal_sequence(
        temporal_trace={
            "clock": "monotonic",
            "samples": [
                {"offset_ms": 0, "visible_text": "跳过 5 秒"},
                {"offset_ms": 1000, "visible_text": "跳过 4 秒"},
                {"offset_ms": 2000, "visible_text": "跳过 3 秒"},
            ],
        },
        config={
            "value_pattern": r"跳过\s*(?P<value>\d+)",
            "expected_rate_per_second": -1,
            "tolerance": 0.1,
        },
    )

    assert result.passed is True
    assert result.judge_output["violations"] == []


def test_temporal_sequence_flags_skipped_count() -> None:
    result = evaluate_temporal_sequence(
        temporal_trace={
            "clock": "monotonic",
            "samples": [
                {"offset_ms": 0, "visible_text": "跳过 5 秒"},
                {"offset_ms": 1000, "visible_text": "跳过 3 秒"},
                {"offset_ms": 2000, "visible_text": "跳过 2 秒"},
            ],
        },
        config={
            "value_pattern": r"跳过\s*(\d+)",
            "expected_rate_per_second": -1,
            "tolerance": 0.1,
        },
    )

    assert result.passed is False
    assert result.judge_output["violations"][0]["observed_delta"] == -2.0


def test_temporal_sequence_accepts_subsecond_duplicate_frames_and_close_marker() -> None:
    result = evaluate_temporal_sequence(
        temporal_trace={
            "clock": "monotonic",
            "samples": [
                {"offset_ms": 100, "visible_text": "2s后关闭"},
                {"offset_ms": 450, "visible_text": "2s后关闭"},
                {"offset_ms": 800, "visible_text": "2s后关闭"},
                {"offset_ms": 1120, "visible_text": "1s后关闭"},
                {"offset_ms": 1450, "visible_text": "1s后关闭"},
                {"offset_ms": 1780, "visible_text": "1s后关闭"},
                {"offset_ms": 2130, "visible_text": "关闭"},
            ],
        },
        config={
            "value_pattern": r"(?P<value>\d+(?:\.\d+)?)\s*(?:秒|s)",
            "expected_rate_per_second": -1,
            "tolerance": 0.45,
        },
    )

    assert result.passed is True
    assert [item["value"] for item in result.judge_output["change_samples"]] == [
        2.0,
        1.0,
        0.0,
    ]


def test_temporal_sequence_uses_countdown_events_when_visible_samples_lack_count() -> None:
    result = evaluate_temporal_sequence(
        temporal_trace={
            "clock": "monotonic",
            "samples": [
                {"offset_ms": 1000, "visible_text": "close\nhome content"},
                {"offset_ms": 2000, "visible_text": "close\nhome content"},
                {"offset_ms": 3000, "visible_text": "close\nhome content"},
            ],
            "countdown_events": [
                {"kind": "start", "offset_ms": 0, "text": "4s close", "value": 4},
                {"kind": "change", "offset_ms": 1000, "text": "3s close", "value": 3},
                {"kind": "change", "offset_ms": 2000, "text": "2s close", "value": 2},
            ],
        },
        config={
            "value_pattern": r"(?P<value>\d+(?:\.\d+)?)\s*s",
            "expected_rate_per_second": -1,
            "tolerance": 0.1,
        },
    )

    assert result.passed is True
    assert [item["value"] for item in result.judge_output["samples"]] == [
        4.0,
        3.0,
        2.0,
    ]
    assert result.judge_output["samples"][0]["source"] == "countdown_events"


def test_temporal_sequence_accepts_compensating_timer_callback_jitter() -> None:
    result = evaluate_temporal_sequence(
        temporal_trace={
            "clock": "monotonic",
            "samples": [
                {"offset_ms": 121.031, "visible_text": "8s后关闭"},
                {"offset_ms": 928.046, "visible_text": "7s后关闭"},
                {"offset_ms": 1671.340, "visible_text": "6s后关闭"},
                {"offset_ms": 3223.360, "visible_text": "5s后关闭"},
                {"offset_ms": 3735.601, "visible_text": "4s后关闭"},
                {"offset_ms": 4732.969, "visible_text": "3s后关闭"},
            ],
        },
        config={
            "value_pattern": r"(?P<value>\d+(?:\.\d+)?)\s*(?:秒|s)",
            "expected_rate_per_second": -1,
            "tolerance": 0.45,
        },
    )

    assert result.passed is True
    assert any(item["within_tolerance"] is False for item in result.judge_output["intervals"])
    assert result.judge_output["violations"] == []


def test_temporal_sequence_rejects_non_monotonic_clock() -> None:
    with pytest.raises(HandlerInputError, match="严格递增"):
        evaluate_temporal_sequence(
            temporal_trace={
                "clock": "monotonic",
                "samples": [
                    {"offset_ms": 0, "visible_text": "3"},
                    {"offset_ms": 0, "visible_text": "2"},
                    {"offset_ms": 1000, "visible_text": "1"},
                ],
            },
            config={"value_pattern": r"(\d+)"},
        )


def test_temporal_sequence_rejects_non_monotonic_clock_source() -> None:
    with pytest.raises(HandlerInputError, match="clock 必须为 monotonic"):
        evaluate_temporal_sequence(
            temporal_trace={
                "clock": "wall_clock",
                "samples": [
                    {"offset_ms": 0, "visible_text": "3"},
                    {"offset_ms": 1000, "visible_text": "2"},
                    {"offset_ms": 2000, "visible_text": "1"},
                ],
            },
            config={"value_pattern": r"(\d+)"},
        )


def test_geometry_hit_area_passes_contained_target() -> None:
    result = evaluate_geometry_hit_area(
        judge_input={"element_index": 3},
        ui_snapshot={
            "elements": [
                {
                    "element_index": 3,
                    "text": "跳过",
                    "disabled": False,
                    "hit_bounds": {"x": 10, "y": 20, "width": 48, "height": 48},
                    "visual_bounds": {"x": 14, "y": 24, "width": 32, "height": 32},
                }
            ]
        },
        config={"min_width": 44, "min_height": 44},
    )

    assert result.passed is True
    assert result.judge_output["contains_visual"] is True


def test_geometry_hit_area_fails_small_or_uncontained_target() -> None:
    result = evaluate_geometry_hit_area(
        judge_input={"element_index": 0},
        ui_snapshot={
            "elements": [
                {
                    "element_index": 0,
                    "disabled": False,
                    "hit_bounds": {"x": 10, "y": 20, "width": 30, "height": 30},
                    "visual_bounds": {"x": 5, "y": 15, "width": 40, "height": 40},
                }
            ]
        },
        config={"min_width": 44, "min_height": 44},
    )

    assert result.passed is False
    assert result.judge_output["contains_visual"] is False
    assert result.judge_output["size_ok"] is False


def test_geometry_hit_area_rejects_malformed_snapshot_types() -> None:
    with pytest.raises(HandlerInputError, match="element_index 必须为整数"):
        evaluate_geometry_hit_area(
            judge_input={"element_index": 1},
            ui_snapshot={
                "elements": [
                    {
                        "element_index": True,
                        "disabled": False,
                        "hit_bounds": {"x": 0, "y": 0, "width": 48, "height": 48},
                        "visual_bounds": {"x": 0, "y": 0, "width": 48, "height": 48},
                    }
                ]
            },
            config={},
        )


def test_temporal_and_geometry_config_validation() -> None:
    assert (
        validate_temporal_sequence_config({"value_pattern": r"\d+"})
        == "temporal_sequence_assertion.value_pattern 必须包含数值捕获组"
    )
    assert (
        validate_geometry_hit_area_config({"min_width": 0})
        == "geometry_hit_area_assertion.min_width 必须在 1 到 500 之间"
    )


def test_external_link_open_mode_passes_cross_origin_new_window() -> None:
    result = evaluate_external_link_open_mode(
        evidence_by_role={
            "primary": {
                "action_trace": [
                    {
                        "action": "click",
                        "selector": "a.ad-link",
                        "navigation": {
                            "before_url": "https://product.example/home",
                            "after_url": "https://product.example/home",
                            "new_page_count": 1,
                            "new_page_urls": ["https://advertiser.example/landing"],
                        },
                    }
                ]
            }
        },
        config={
            "expected_mode": "new_window",
            "action_selector_pattern": "ad-link",
            "target_url_pattern": "advertiser",
        },
    )

    assert result.passed is True
    assert result.judge_output["current_url_unchanged"] is True
    assert result.judge_output["cross_origin_ok"] is True


def test_external_link_open_mode_fails_current_window_navigation() -> None:
    result = evaluate_external_link_open_mode(
        evidence_by_role={
            "primary": {
                "action_trace": [
                    {
                        "action": "click",
                        "selector": "a.ad-link",
                        "navigation": {
                            "before_url": "https://product.example/home",
                            "after_url": "https://advertiser.example/landing",
                            "new_page_count": 0,
                            "new_page_urls": [],
                        },
                    }
                ]
            }
        },
        config={"expected_mode": "new_window"},
    )

    assert result.passed is False
    assert "窗口方式" in (result.problem_desc or "")


def test_state_transition_passes_immediate_numeric_and_refreshed_state() -> None:
    result = evaluate_state_transition(
        evidence_by_role={
            "primary": {
                "visible_text": "关注 评论 5",
                "action_trace": [],
                "ui_snapshot": _ui_state(coverage=0.2),
            },
            "after": {
                "visible_text": "已关注 评论 6",
                "action_trace": [{"action": "click", "selector": "button.follow"}],
                "ui_snapshot": _ui_state(coverage=0.2),
            },
            "refreshed": {
                "visible_text": "已关注 评论 6",
                "action_trace": [],
                "ui_snapshot": _ui_state(coverage=0.2),
            },
        },
        config={
            "before_text_pattern": r"(?:^|\s)关注(?:\s|$)",
            "after_text_pattern": "已关注",
            "refreshed_text_pattern": "已关注",
            "action_pattern": "button\\.follow",
            "require_text_change": True,
            "numeric_change": {
                "pattern": r"评论\s*(\d+)",
                "min_delta": 1,
                "max_delta": 1,
            },
        },
    )

    assert result.passed is True
    assert result.judge_output["numeric_change"]["delta"] == 1.0


def test_state_transition_flags_refreshed_state_rollback() -> None:
    result = evaluate_state_transition(
        evidence_by_role={
            "primary": {"visible_text": "关注"},
            "after": {
                "visible_text": "已关注",
                "action_trace": [{"action": "click"}],
            },
            "refreshed": {"visible_text": "关注"},
        },
        config={
            "after_text_pattern": "已关注",
            "refreshed_text_pattern": "已关注",
        },
    )

    assert result.passed is False
    assert result.judge_output["checks"][-1] == {
        "name": "refreshed_text_pattern",
        "passed": False,
    }


def test_state_transition_passes_fullscreen_entry() -> None:
    result = evaluate_state_transition(
        evidence_by_role={
            "primary": {
                "visible_text": "帖子图片",
                "ui_snapshot": _ui_state(coverage=0.25),
            },
            "after": {
                "visible_text": "帖子图片",
                "ui_snapshot": _ui_state(coverage=0.96),
            },
        },
        config={"after_fullscreen_required": True},
    )

    assert result.passed is True
    assert result.judge_output["fullscreen"]["after"]["active"] is True


def test_external_and_state_transition_config_validation() -> None:
    assert (
        validate_external_link_open_mode_config({"expected_mode": "same_tab"})
        == "external_link_open_mode.expected_mode 必须为 new_window/current_window"
    )
    assert validate_state_transition_config({}) == "state_transition_assertion 至少配置一项状态断言"
    assert (
        validate_state_transition_config({"action_pattern": "click"})
        == "state_transition_assertion 至少配置一项状态断言"
    )
    assert (
        validate_state_transition_config({"after_role": "primary", "after_text_pattern": "已关注"})
        == "state_transition_assertion.before_role 与 after_role 必须不同"
    )
    assert "包含数值捕获组" in str(
        validate_state_transition_config({"numeric_change": {"pattern": r"评论\s*\d+"}})
    )


def _ui_state(*, coverage: float) -> dict:
    return {
        "document_state": {"fullscreen_active": False},
        "media": [{"viewport_coverage": coverage}],
    }
