"""测试候选规则驱动的最小页面采集计划。"""

from services.api.app.application.capture_plan import (
    build_capture_plan,
    derive_session_dependency,
)


def test_capture_plan_uses_input_contract_instead_of_collecting_everything() -> None:
    plan = build_capture_plan(
        page_type="home",
        rules=[
            {
                "input_contract": {"required_artifacts": ["visible_text"]},
                "judge_config": {"tool_refs": ["deterministic_assertion_tool"]},
            }
        ],
    )

    assert plan.visible_text is True
    assert plan.screenshot is False
    assert plan.dom is False
    assert plan.temporal is False
    assert plan.session_dependency == "snapshot"


def test_capture_plan_marks_interaction_and_list_coverage_as_active_session() -> None:
    plan = build_capture_plan(
        page_type="video_list",
        rules=[
            {
                "input_contract": {
                    "required_artifacts": ["action_trace", "visible_text"],
                    "required_fields": ["video_items"],
                    "required_capture_roles": ["before", "primary"],
                },
                "judge_config": {},
            }
        ],
    )

    assert plan.interaction is True
    assert plan.required_capture_roles == ("before", "primary")
    assert plan.scroll is True
    assert plan.page_coverage is True
    assert plan.session_dependency == "active_session"
    assert (
        derive_session_dependency(
            input_contract={"required_artifacts": ["temporal_trace"]},
            judge_config={},
        )
        == "active_session"
    )


def test_player_media_contract_requires_active_session() -> None:
    assert (
        derive_session_dependency(
            input_contract={"required_artifacts": ["media_state"]},
            judge_config={},
        )
        == "active_session"
    )


def test_startup_ad_always_requires_temporal_evidence() -> None:
    plan = build_capture_plan(
        page_type="startup_ad",
        rules=[{"input_contract": {"required_artifacts": ["screenshot"]}}],
    )

    assert plan.temporal is True
    assert plan.session_dependency == "active_session"


def test_capture_plan_exposes_hybrid_journey_requirement() -> None:
    plan = build_capture_plan(
        page_type="home",
        rules=[
            {
                "input_contract": {"required_artifacts": ["screenshot"]},
                "judge_config": {"capability_config": {"requires_journey": True}},
            }
        ],
    )

    assert plan.requires_journey is True
    assert plan.interaction is True


def test_list_page_does_not_scroll_without_explicit_coverage_contract() -> None:
    plan = build_capture_plan(
        page_type="content_list",
        rules=[{"input_contract": {"required_artifacts": ["visible_text"]}}],
    )

    assert plan.scroll is False
    assert plan.page_coverage is False


def test_list_page_scrolls_when_collection_coverage_is_explicitly_required() -> None:
    plan = build_capture_plan(
        page_type="content_list",
        rules=[
            {
                "input_contract": {
                    "required_artifacts": ["visible_text"],
                    "required_fields": ["collection_items"],
                }
            }
        ],
    )

    assert plan.scroll is True
    assert plan.page_coverage is True


def test_multi_frame_tool_reference_alone_does_not_force_timed_capture() -> None:
    plan = build_capture_plan(
        page_type="home",
        rules=[
            {
                "input_contract": {"required_artifacts": ["screenshot"]},
                "judge_config": {"tool_refs": ["multi_frame_visual_tool"]},
            }
        ],
    )

    assert plan.temporal is False


def test_popup_on_list_page_does_not_scroll_obstructed_business_content() -> None:
    plan = build_capture_plan(
        page_type="video_list",
        page_state="popup",
        rules=[{"input_contract": {"required_artifacts": ["screenshot"]}}],
    )

    assert plan.scroll is False
    assert plan.page_coverage is False
