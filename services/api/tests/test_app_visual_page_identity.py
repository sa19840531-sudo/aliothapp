"""正式视觉输出的业务身份映射、状态分离和截图归属验证。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from services.api.app.domain.exploration.app_visual import (
    AppVisualAnalysis,
    AppVisualValidationError,
)
from services.api.app.domain.exploration.app_visual_page_identity import (
    APP_VISUAL_PAGE_IDENTITY_VERSION,
    build_app_visual_page_facts,
    validated_app_visual_page_identity,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.app_vision.llm import build_analysis_from_llm_payload
from services.api.tests.test_llm_app_vision import _request


def _anchor(text: str, *, top: float = 0.1, kind: str = "label") -> dict[str, Any]:
    return {
        "text": text,
        "kind": kind,
        "confidence": 0.95,
        "normalized_bounds": {"left": 0.1, "top": top, "right": 0.6, "bottom": top + 0.08},
    }


def _observation(*, object_id: str | None = None) -> dict[str, Any]:
    return {
        "page_role": "detail" if object_id else "collection",
        "confidence": 0.93,
        "location": _anchor("作品详情" if object_id else "作品目录"),
        "function": _anchor("开始阅读" if object_id else "搜索作品", top=0.3),
        "return_semantics": "back",
        "return_control": _anchor("返回箭头", top=0.01, kind="icon"),
        "business_object": [_anchor(object_id, top=0.5, kind="identifier")] if object_id else [],
    }


def _analysis(
    observation: Any = None,
    *,
    local_state: Any = None,
    page_state: str = "stable",
    surface_facts: dict[str, Any] | None = None,
    request: Any = None,
) -> AppVisualAnalysis:
    return build_analysis_from_llm_payload(
        request=request or _request(),
        payload={
            "visual_page_type": "content_list",
            "visual_page_type_confidence": 0.94,
            "page_name": "作品列表",
            "page_name_source": "top_bar",
            "page_name_confidence": 0.99,
            "page_state": page_state,
            "visual_summary": "作品目录",
            "page_identity_observation": observation,
            "local_state_observation": local_state,
            "surface_facts": surface_facts or {},
        },
        page_types=get_default_page_type_registry(),
        model_id="identity-test",
    )


def test_production_mapper_binds_identity_to_actual_screenshot_and_round_trips() -> None:
    analysis = _analysis(_observation(object_id="作品编号 123"))
    identity = analysis.surface_facts["app_page_identity"]
    assert identity["protocol_version"] == APP_VISUAL_PAGE_IDENTITY_VERSION
    assert identity["capture_id"] == analysis.capture_id
    assert identity["screenshot_artifact_id"] == analysis.screenshot_artifact_id
    assert identity["image_signature"] == analysis.image_signature
    assert identity["package_name"] == "com.example.app"
    assert identity["components"]["business_object"] == ("作品编号 123",)
    assert AppVisualAnalysis.from_dict(analysis.to_dict()) == analysis


def test_new_capture_geometry_and_local_state_do_not_change_business_identity() -> None:
    first = _analysis(_observation(), local_state={"page_index": _anchor("第1页")})
    observation = _observation()
    observation["location"]["normalized_bounds"]["right"] = 0.7
    observation["confidence"] = 0.88
    second = _analysis(
        observation,
        local_state={"page_index": _anchor("第2页")},
        request=replace(_request(), capture_id="55555555-5555-5555-5555-555555555555"),
    )
    assert (
        first.surface_facts["app_page_identity"]["identity"]
        == second.surface_facts["app_page_identity"]["identity"]
    )
    assert first.surface_facts["local_state"]["page_index"] == ("第1页",)
    assert second.surface_facts["local_state"]["page_index"] == ("第2页",)
    assert (
        first.surface_facts["app_local_state_observation"]["capture_id"]
        != second.surface_facts["app_local_state_observation"]["capture_id"]
    )


def test_business_object_and_return_semantics_produce_distinct_identity() -> None:
    first = _analysis(_observation(object_id="作品编号 123"))
    second_observation = _observation(object_id="作品编号 124")
    second = _analysis(second_observation)
    second_observation["return_semantics"] = "close"
    third = _analysis(second_observation)
    assert (
        len(
            {item.surface_facts["app_page_identity"]["identity"] for item in (first, second, third)}
        )
        == 3
    )


def test_title_or_model_self_verified_identity_cannot_replace_observation() -> None:
    analysis = _analysis(
        surface_facts={
            "app_page_identity": {"verified": True, "identity": "fake"},
            "business_page_identity": "fake",
            "local_state": {"page_index": 2},
        }
    )
    assert "app_page_identity" not in analysis.surface_facts
    assert "business_page_identity" not in analysis.surface_facts
    assert "local_state" not in analysis.surface_facts
    assert (
        analysis.surface_facts["app_page_identity_assessment"]["reason"]
        == "identity_observation_missing"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_role", "unknown"),
        ("page_role", []),
        ("return_semantics", None),
        ("confidence", True),
        ("confidence", float("nan")),
        ("confidence", 10**1000),
        ("location", None),
        ("function", None),
        ("return_control", None),
        ("business_object", "fake-id"),
        ("business_object", False),
    ],
)
def test_invalid_identity_observation_stays_unknown(field: str, value: Any) -> None:
    observation = _observation()
    observation[field] = value
    assert "app_page_identity" not in _analysis(observation).surface_facts


@pytest.mark.parametrize(
    "patch",
    [
        {"kind": []},
        {"confidence": 0.4},
        {"normalized_bounds": {"left": 0, "top": 0, "right": 1, "bottom": 1}},
        {"normalized_bounds": {"left": False, "top": 0, "right": 0.2, "bottom": 0.3}},
        {"text": "x" * 257},
    ],
)
def test_invalid_anchor_stays_unknown(patch: dict[str, Any]) -> None:
    observation = _observation()
    observation["location"].update(patch)
    assert "app_page_identity" not in _analysis(observation).surface_facts


def test_detail_needs_object_identifier_or_two_independent_object_labels() -> None:
    observation = _observation(object_id="作品编号 123")
    observation["business_object"] = [_anchor("某作品", top=0.5)]
    assert "app_page_identity" not in _analysis(observation).surface_facts
    observation["business_object"].append(_anchor("作者：某作者", top=0.65))
    assert "app_page_identity" in _analysis(observation).surface_facts
    observation["function"] = deepcopy(observation["location"])
    assert "app_page_identity" not in _analysis(observation).surface_facts


@pytest.mark.parametrize(
    "state,facts",
    [
        ("loading", {}),
        ("unknown", {}),
        ("popup", {}),
        ("stable", {"popup_visible": True}),
        ("stable", {"dialog_visible": True}),
        ("stable", {"topmost_ad_popup": {"is_ad": True}}),
    ],
)
def test_nonvisible_business_page_cannot_get_confirmed_identity(state: str, facts: dict) -> None:
    analysis = _analysis(_observation(), page_state=state, surface_facts=facts)
    assert "app_page_identity" not in analysis.surface_facts
    assert (
        analysis.surface_facts["app_page_identity_assessment"]["reason"]
        == "business_surface_not_visible"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("capture_id", "foreign"),
        ("screenshot_artifact_id", "foreign"),
        ("image_signature", "sha256:" + "0" * 64),
        ("package_name", "foreign"),
        ("activity", ".Other"),
        ("identity", "fake"),
    ],
)
def test_contract_rejects_identity_from_another_observation(field: str, value: str) -> None:
    analysis = _analysis(_observation())
    payload = analysis.to_dict()
    payload["surface_facts"]["app_page_identity"][field] = value
    with pytest.raises(AppVisualValidationError, match="业务页面身份"):
        AppVisualAnalysis.from_dict(payload)


def test_consumer_rejects_legacy_self_assertion_and_tampered_components() -> None:
    request = _request()
    facts = build_app_visual_page_facts(
        identity_observation=_observation(),
        local_state_observation=None,
        capture_id=request.capture_id,
        screenshot_artifact_id=request.screenshot_artifact_id,
        image_signature=request.image_signature,
        package_name=request.package_name,
        activity=request.activity,
        page_state="stable",
        overlay_signature=None,
        surface_facts={},
    )
    identity = facts["app_page_identity"]
    kwargs = {
        "capture_id": request.capture_id,
        "package_name": request.package_name,
        "activity": request.activity,
    }
    assert validated_app_visual_page_identity(identity, **kwargs) is not None
    identity["components"]["page_role"] = "detail"
    assert validated_app_visual_page_identity(identity, **kwargs) is None
    assert (
        validated_app_visual_page_identity(
            {
                "protocol_version": "app-visual-page-identity.v1",
                "verified": True,
                "identity": "fake",
            },
            **kwargs,
        )
        is None
    )


def test_local_state_only_accepts_bounded_visible_text() -> None:
    analysis = _analysis(
        _observation(),
        local_state={
            "page_index": _anchor("第2页"),
            "selected_filter": [_anchor("免费"), _anchor("已完结", top=0.4)],
            "sort_key": _anchor("图标猜测", kind="icon"),
            "expanded_item_id": [_anchor("简介")] * 9,
            "account_secret": _anchor("不应采纳"),
        },
    )
    assert set(analysis.surface_facts["local_state"]) == {"page_index", "selected_filter"}
    assert "sort_key" not in analysis.surface_facts["app_local_state_observation"]["anchors"]


@pytest.mark.parametrize("tamper", ["state", "capture", "anchor", "missing"])
def test_local_state_contract_rejects_changed_values_or_foreign_anchors(tamper: str) -> None:
    analysis = _analysis(_observation(), local_state={"page_index": _anchor("第1页")})
    payload = analysis.to_dict()
    facts = payload["surface_facts"]
    if tamper == "state":
        facts["local_state"]["page_index"] = ["第9页"]
    elif tamper == "capture":
        facts["app_local_state_observation"]["capture_id"] = "foreign"
    elif tamper == "anchor":
        facts["app_local_state_observation"]["anchors"]["page_index"][0]["kind"] = "icon"
    else:
        del facts["app_local_state_observation"]
    with pytest.raises(AppVisualValidationError, match="局部页面状态"):
        AppVisualAnalysis.from_dict(payload)


@pytest.mark.parametrize(
    "facts", [{"popup_visible": "false"}, {"dialog_visible": None}, {"topmost_ad_popup": []}]
)
def test_malformed_layer_observation_does_not_imply_visible_business_page(facts: dict) -> None:
    analysis = _analysis(_observation(), surface_facts=facts)
    assert "app_page_identity" not in analysis.surface_facts
    assert (
        analysis.surface_facts["app_page_identity_assessment"]["reason"]
        == "business_surface_visibility_unknown"
    )


@pytest.mark.parametrize("surface_facts", [None, [], "invalid"])
def test_direct_builder_rejects_nonmapping_layer_facts(surface_facts: Any) -> None:
    request = _request()
    facts = build_app_visual_page_facts(
        identity_observation=_observation(),
        local_state_observation=None,
        capture_id=request.capture_id,
        screenshot_artifact_id=request.screenshot_artifact_id,
        image_signature=request.image_signature,
        package_name=request.package_name,
        activity=request.activity,
        page_state="stable",
        overlay_signature=None,
        surface_facts=surface_facts,
    )
    assert facts == {
        "app_page_identity_assessment": {
            "status": "unknown",
            "reason": "business_surface_visibility_unknown",
        }
    }
