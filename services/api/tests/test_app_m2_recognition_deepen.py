"""M2 加深：判别指南 v2、运行时能力影子、身份 v6 与图权威禁开。"""

from __future__ import annotations

import pytest

from services.api.app.application.app_bounded_exploration_milestones import (
    build_app_bounded_exploration_milestone_policy,
)
from services.api.app.application.app_runtime_capability_assessment import (
    APP_RUNTIME_CAPABILITY_SHADOW_VERSION,
    build_app_runtime_capability_shadow,
    capability_shadow_from_coverage_inputs,
)
from services.api.app.application.page_graph_projection import (
    attach_app_coverage_page_graph_shadow,
)
from services.api.app.domain.exploration.app_page_identity import (
    APP_BUSINESS_PAGE_IDENTITY_VERSION,
    build_app_business_page_group,
)
from services.api.app.domain.exploration.app_visual_page_identity import (
    build_app_visual_page_facts,
)
from services.api.app.domain.page.discrimination_guide import (
    APP_PAGE_DISCRIMINATION_GUIDE_VERSION,
    build_app_page_discrimination_guide,
    format_app_page_discrimination_guide_for_prompt,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.app_vision.llm import _system_prompt


def _anchor(text: str, *, top: float = 0.1, kind: str = "label") -> dict[str, object]:
    return {
        "text": text,
        "kind": kind,
        "confidence": 0.95,
        "normalized_bounds": {
            "left": 0.1,
            "top": top,
            "right": 0.6,
            "bottom": top + 0.08,
        },
    }


def _confirmed_visual_facts(*, object_id: str, capture_id: str) -> dict[str, object]:
    return build_app_visual_page_facts(
        identity_observation={
            "page_role": "detail",
            "confidence": 0.93,
            "location": _anchor("作品详情"),
            "function": _anchor("开始阅读", top=0.3),
            "return_semantics": "back",
            "return_control": _anchor("返回箭头", top=0.01, kind="icon"),
            "business_object": [_anchor(object_id, top=0.5, kind="identifier")],
        },
        local_state_observation=None,
        capture_id=capture_id,
        screenshot_artifact_id=f"artifact-{capture_id}",
        image_signature="sha256:" + "a" * 64,
        package_name="com.example.app",
        activity=".MainActivity",
        page_state="stable",
        overlay_signature=None,
        surface_facts={},
    )


def test_discrimination_guide_v2_adds_capability_and_gate_pairs() -> None:
    guide = build_app_page_discrimination_guide()
    assert guide["protocol_version"] == "app-page-discrimination-guide.v2"
    assert guide["protocol_version"] == APP_PAGE_DISCRIMINATION_GUIDE_VERSION
    assert any(
        pair["left"] == "auth_gate" and pair["right"] == "home"
        for pair in guide["confusable_pairs"]
    )
    assert any(
        pair["left"] == "permission_dialog" and pair["right"] == "home"
        for pair in guide["confusable_pairs"]
    )
    rules = guide["rules"]
    assert any("静态产品画像" in rule for rule in rules)
    text = format_app_page_discrimination_guide_for_prompt(guide)
    assert "auth_gate vs home" in text
    assert "不得静默缩小冻结必检分母" in text


def test_vision_prompt_embeds_discrimination_guide_v2() -> None:
    prompt = _system_prompt(
        page_codes=("home", "content_list"),
        allowed_actions=("click", "back"),
        max_candidates=3,
        page_types=get_default_page_type_registry(),
    )
    assert APP_PAGE_DISCRIMINATION_GUIDE_VERSION in prompt
    assert "静态产品画像不等于入口后真实界面能力" in prompt or "判别指南" in prompt


def test_runtime_capability_shadow_never_shrinks_frozen_denominator() -> None:
    shadow = build_app_runtime_capability_shadow(
        declared_required_page_types=("home", "content_list", "profile"),
        observed_page_type=None,
        entry_layer_kind="startup_ad",
        presentation_layer="overlay",
    )
    assert shadow["protocol_version"] == APP_RUNTIME_CAPABILITY_SHADOW_VERSION
    assert shadow["authority"] is False
    assert shadow["shrinks_frozen_denominator"] is False
    assert shadow["static_portrait_equals_runtime_capability"] is False
    assert shadow["missing_entry_means_not_applicable"] is False
    assert shadow["declared_required_page_types"] == [
        "home",
        "content_list",
        "profile",
    ]
    assert shadow["observed_capabilities"] == []
    assert shadow["unconfirmed_required_page_types"] == [
        "home",
        "content_list",
        "profile",
    ]
    assert "entry_layer_blocks_capability_confirmation" in shadow["reason_codes"]


def test_runtime_capability_shadow_observes_only_on_business_surface() -> None:
    blocked = capability_shadow_from_coverage_inputs(
        goal_required_page_types=("home", "profile"),
        analysis_page_type="home",
        entry_layer={
            "kind": "startup_ad",
            "presentation_layer": "overlay",
        },
    )
    assert blocked["observed_capabilities"] == []
    assert "home" in blocked["unconfirmed_required_page_types"]

    confirmed = capability_shadow_from_coverage_inputs(
        goal_required_page_types=("home", "profile"),
        analysis_page_type="home",
        entry_layer={
            "kind": "business_surface",
            "presentation_layer": "business_surface",
        },
    )
    assert confirmed["observed_capabilities"] == ["home"]
    assert confirmed["unconfirmed_required_page_types"] == ["profile"]
    assert len(confirmed["declared_required_page_types"]) == 2


def test_business_identity_v6_excludes_page_type_from_core_key() -> None:
    assert APP_BUSINESS_PAGE_IDENTITY_VERSION.endswith(".v6")
    facts = _confirmed_visual_facts(object_id="作品编号 123", capture_id="capture-a")
    first = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="content_detail",
        surface_facts=facts,
        navigation_depth=1,
        page_scope="detail-a",
        capture_id="capture-a",
        image_signature="sha256:" + "a" * 64,
        screenshot_artifact_id="artifact-capture-a",
    )
    revised = build_app_business_page_group(
        package_name="com.example.app",
        activity=".MainActivity",
        page_type="video_detail",
        surface_facts=facts,
        navigation_depth=1,
        page_scope="detail-a",
        capture_id="capture-a",
        image_signature="sha256:" + "a" * 64,
        screenshot_artifact_id="artifact-capture-a",
    )
    assert first["business_page_identity"] == revised["business_page_identity"]
    assert "content_detail" not in first["business_page_identity"]
    assert "video_detail" not in revised["business_page_identity"]
    assert "|vid:" in first["business_page_identity"]


def test_m2_forbids_opening_graph_authority_and_keeps_shadow_flags() -> None:
    policy = build_app_bounded_exploration_milestone_policy()
    meta = attach_app_coverage_page_graph_shadow(
        {
            "covered_business_state_ids": ["pkg|act|vid:home"],
            "business_page_identity": "pkg|act|vid:home",
        },
        current_frame={
            "capture_id": "cap-1",
            "page_state": "stable",
            "stable_business_page": True,
            "business_page_identity": "pkg|act|vid:home",
            "package_within_boundary": True,
        },
        milestone_policy=policy,
    )
    shadow = meta["app_coverage_page_graph_shadow"]
    assert shadow["schedules_from_graph"] is False
    assert shadow["gate_consumes_graph"] is False
    assert shadow["coverage_authority_changed"] is False
    assert meta["app_page_graph_snapshot"]["schedules_from_graph"] is False

    bad = build_app_bounded_exploration_milestone_policy(
        {"authority": {"M2": True}, "shadow": {"M2": True}}
    )
    with pytest.raises(ValueError, match="M2"):
        attach_app_coverage_page_graph_shadow({}, milestone_policy=bad)


def test_coverage_metadata_writes_runtime_capability_shadow() -> None:
    from services.api.app.application.app_screenshot_exploration import _next_coverage_metadata
    from services.api.tests.test_app_screenshot_exploration import (
        _coverage_analysis,
        _coverage_goal,
    )

    meta = _next_coverage_metadata(
        goal=_coverage_goal(required_page_types=("home", "profile")),
        analysis=_coverage_analysis(
            capture_id="capture-startup",
            artifact_id="artifact-startup",
            image_signature="sha256:" + "b" * 64,
            visual_page_type="startup_ad",
            page_state="startup_ad",
        ),
    )
    shadow = meta["app_runtime_capability_shadow"]
    assert shadow["protocol_version"] == APP_RUNTIME_CAPABILITY_SHADOW_VERSION
    assert shadow["authority"] is False
    assert shadow["shrinks_frozen_denominator"] is False
    assert shadow["declared_required_page_types"] == ["home", "profile"]
    assert shadow["observed_capabilities"] == []
    assert meta["app_entry_layer"]["app_ready_equals_business_home"] is False
