"""底栏切栏留证必须消费同一截图绑定的视觉选中态。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from services.api.app.application.app_action_observation import (
    build_app_action_observation,
    confirmed_app_identity,
)
from services.api.app.domain.exploration.app_visual import AppVisualAnalysis
from services.api.app.domain.exploration.app_visual_page_identity import (
    build_app_visual_page_facts,
    validate_app_visual_local_state,
)
from services.api.tests.test_app_action_observation import _scenario
from services.api.tests.test_app_visual_page_identity import _anchor, _observation
from services.api.tests.test_llm_app_vision import _request


def _visual_frame(
    role: str,
    *,
    page_type: str,
    object_id: str | None = None,
    selected_tab: list[dict[str, Any]] | None = None,
) -> AppVisualAnalysis:
    """用正式身份和局部状态 builder 生成与截图绑定的视觉事实。"""

    request = replace(
        _request(),
        capture_id=f"capture-{role}",
        screenshot_artifact_id=f"artifact-{role}",
        image_signature="sha256:" + ("a" if role == "before" else "b") * 64,
        package_name="com.example.target",
        activity=".MainActivity",
    )
    facts = build_app_visual_page_facts(
        identity_observation=_observation(object_id=object_id),
        local_state_observation=(
            {"selected_tab": selected_tab} if selected_tab is not None else None
        ),
        capture_id=request.capture_id,
        screenshot_artifact_id=request.screenshot_artifact_id,
        image_signature=request.image_signature,
        package_name=request.package_name,
        activity=request.activity,
        page_state="stable",
        overlay_signature=None,
        surface_facts={"visual_page_type_confidence": 0.92},
    )
    return AppVisualAnalysis.build(
        capture_id=request.capture_id,
        screenshot_artifact_id=request.screenshot_artifact_id,
        image_signature=request.image_signature,
        width=request.width,
        height=request.height,
        orientation=request.orientation,
        package_name=request.package_name,
        activity=request.activity,
        visual_page_type=page_type,
        visual_summary=page_type,
        key_elements=(page_type,),
        candidates=(),
        provider_id="test-app-vision",
        model_id="selected-tab-test",
        page_state="stable",
        surface_facts={**facts, "visual_page_type_confidence": 0.92},
    )


def _bottom_anchor(text: str) -> dict[str, Any]:
    return _anchor(text, top=0.82)


def _top_anchor(text: str) -> dict[str, Any]:
    return _anchor(text, top=0.10)


def _tab_seek_scenario(*, before: AppVisualAnalysis, after: AppVisualAnalysis, label: str):
    scenario = _scenario(
        before=before,
        after=after,
        metadata={
            "bottom_navigation_tab_seek": True,
            "require_screenshot_evidence": True,
            "frontier_scope": "app",
            "expected_next_page_type": "video_list",
        },
    )
    return {
        **scenario,
        "candidate": replace(scenario["candidate"], semantic_target=f"底栏{label}"),
    }


def _assert_bound_visual_state(frame: AppVisualAnalysis) -> None:
    assert confirmed_app_identity(frame) is not None
    assert validate_app_visual_local_state(
        facts=frame.surface_facts,
        capture_id=frame.capture_id,
        screenshot_artifact_id=frame.screenshot_artifact_id,
        image_signature=frame.image_signature,
        package_name=frame.package_name,
        activity=frame.activity,
    )


@pytest.mark.parametrize(
    "label,after_tabs",
    [
        (
            "短剧",
            [_top_anchor("AI短剧"), _bottom_anchor("短剧")],
        ),
        (
            "新栏目",
            [_top_anchor("顶部入口"), _bottom_anchor("新栏目")],
        ),
    ],
)
def test_bound_bottom_selected_tab_proves_seek_when_page_type_is_nested_detail(
    label: str, after_tabs: list[dict[str, Any]]
) -> None:
    """底栏目标已选中时，页型可仍为嵌套 detail，但必须留下切栏证据。"""

    before = _visual_frame(
        "before",
        page_type="home",
        selected_tab=[_top_anchor("AI")],
    )
    after = _visual_frame(
        "after",
        page_type="video_detail",
        object_id="视频详情目标",
        selected_tab=after_tabs,
    )
    _assert_bound_visual_state(before)
    _assert_bound_visual_state(after)

    proof = build_app_action_observation(
        **_tab_seek_scenario(before=before, after=after, label=label)
    )

    assert proof.outcome == "tab_seek_screenshot_evidenced"
    assert proof.reason == "after_selected_tab_matches_target"
    assert proof.arrival_proven
    assert proof.navigation_path == (
        confirmed_app_identity(before), confirmed_app_identity(after)
    )


def test_same_name_top_anchor_does_not_prove_bottom_tab_seek() -> None:
    before = _visual_frame(
        "before",
        page_type="home",
        selected_tab=[_top_anchor("短剧")],
    )
    after = _visual_frame(
        "after",
        page_type="video_detail",
        object_id="视频详情目标",
        selected_tab=[_top_anchor("短剧")],
    )
    _assert_bound_visual_state(before)
    _assert_bound_visual_state(after)

    proof = build_app_action_observation(
        **_tab_seek_scenario(before=before, after=after, label="短剧")
    )

    assert proof.outcome != "tab_seek_screenshot_evidenced"
    assert proof.reason == "different_business_target_observed"
    assert not proof.arrival_proven


def test_already_selected_bottom_tab_does_not_prove_mismatched_page_type() -> None:
    selected = [_bottom_anchor("短剧")]
    before = _visual_frame("before", page_type="home", selected_tab=selected)
    after = _visual_frame(
        "after",
        page_type="video_detail",
        object_id="视频详情目标",
        selected_tab=selected,
    )
    _assert_bound_visual_state(before)
    _assert_bound_visual_state(after)

    proof = build_app_action_observation(
        **_tab_seek_scenario(before=before, after=after, label="短剧")
    )

    assert proof.outcome != "tab_seek_screenshot_evidenced"
    assert proof.reason == "different_business_target_observed"
    assert not proof.arrival_proven


def test_unbound_raw_local_state_does_not_prove_bottom_tab_seek() -> None:
    before = _visual_frame(
        "before",
        page_type="home",
        selected_tab=[_top_anchor("AI")],
    )
    after_base = _visual_frame(
        "after",
        page_type="video_detail",
        object_id=None,
    )
    # 合法的 unknown identity fixture：只有未经截图绑定的自报 local_state。
    after = AppVisualAnalysis.build(
        capture_id=after_base.capture_id,
        screenshot_artifact_id=after_base.screenshot_artifact_id,
        image_signature=after_base.image_signature,
        width=after_base.width,
        height=after_base.height,
        orientation=after_base.orientation,
        package_name=after_base.package_name,
        activity=after_base.activity,
        visual_page_type=after_base.visual_page_type,
        visual_summary=after_base.visual_summary,
        key_elements=after_base.key_elements,
        candidates=after_base.candidates,
        provider_id=after_base.provider_id,
        model_id=after_base.model_id,
        page_state=after_base.page_state,
        surface_facts={
            "visual_page_type_confidence": 0.92,
            "local_state": {"selected_tab": ["短剧"]},
        },
    )
    assert confirmed_app_identity(after) is None
    assert "app_local_state_observation" not in after.surface_facts

    proof = build_app_action_observation(
        **_tab_seek_scenario(before=before, after=after, label="短剧")
    )

    assert proof.outcome != "tab_seek_screenshot_evidenced"
    assert proof.reason == "after_business_unconfirmed"
