"""判定包里的展示层事实：持久化评估投影、原始快照兜底推导与 App 视觉推导。"""

from __future__ import annotations

import json
from dataclasses import replace

from services.api.app.application.observation_packet import (
    ObservationPacketService,
    ObservationSource,
)
from services.api.app.domain.evidence.presentation import PRESENTATION_PROTOCOL_VERSION
from services.api.tests.test_observation_packet import (
    FakeObservationPacketRepository,
    _artifact,
    _request,
    _source,
)


def _natural_presentation() -> dict:
    return {
        "protocol_version": PRESENTATION_PROTOCOL_VERSION,
        "topmost_layer_kind": "page",
        "base_page_visibility": "visible",
        "blocking_scope": "none",
        "mutation_mode": "natural",
        "hit_test_status": "available",
        "evidence_sources": ["h5_ui_snapshot"],
        "diagnostic_operations": [],
        "uncertainty": [],
    }


def _snapshot_with_dialog() -> dict:
    return {
        "schema_version": "ui-snapshot.v1",
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": "#content",
                "class_tokens": ["content"],
                "rect": {"x": 0, "y": 0, "width": 390, "height": 300},
                "style": {"display": "block"},
                "hit_test": {"center_hits_target": True},
            },
            {
                "selector": "div.van-popup",
                "role": "dialog",
                "class_tokens": ["van-popup", "van-overlay"],
                "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                "style": {"display": "block"},
                "hit_test": {"center_hits_target": True},
            },
        ],
    }


def _with_snapshot(source: ObservationSource, snapshot: dict) -> ObservationSource:
    kept = tuple(item for item in source.artifacts if item.artifact_type != "ui_snapshot")
    return replace(
        source,
        artifacts=(
            *kept,
            _artifact(
                "ui-dialog", "ui_snapshot", json.dumps(snapshot).encode(), "application/json"
            ),
        ),
    )


async def _facts(source: ObservationSource) -> dict:
    service = ObservationPacketService(repository=FakeObservationPacketRepository(source))
    packet = await service.build(_request())
    return packet.packet_payload["facts"]


async def test_stored_dom_presentation_outranks_popup_page_state_but_records_dispute() -> None:
    # 采集时 DOM 结构确认无阻断（hit_test available），识别却给出 popup：以结构为准
    source = replace(_source(page_state="popup"), presentation=_natural_presentation())

    facts = await _facts(source)

    presentation = facts["presentation"]
    assert presentation["protocol_version"] == PRESENTATION_PROTOCOL_VERSION
    assert presentation["topmost_layer_kind"] == "page"
    assert presentation["base_page_visibility"] == "visible"
    assert "page_state" in presentation["evidence_sources"]
    assert "page_state_reports_overlay" in presentation["uncertainty"]


async def test_stored_degraded_presentation_defers_to_popup_page_state() -> None:
    # 采集时结构不可信（Flutter 画布）：识别说 popup 就保守认定最上层有遮挡物
    stored = {**_natural_presentation(), "hit_test_status": "degraded"}
    source = replace(_source(page_state="popup"), presentation=stored)

    facts = await _facts(source)

    presentation = facts["presentation"]
    assert presentation["topmost_layer_kind"] == "overlay"
    assert presentation["base_page_visibility"] == "unknown"
    assert "structure_snapshot_disagrees_with_page_state" in presentation["uncertainty"]


async def test_stored_presentation_is_kept_verbatim_when_page_state_is_stable() -> None:
    source = replace(_source(page_state="stable"), presentation=_natural_presentation())

    facts = await _facts(source)

    assert facts["presentation"] == _natural_presentation()


async def test_missing_presentation_is_derived_from_raw_ui_snapshot() -> None:
    clean = await _facts(_source(page_state="ready"))
    assert clean["presentation"]["topmost_layer_kind"] == "page"
    assert clean["presentation"]["base_page_visibility"] == "visible"

    occluded = await _facts(_with_snapshot(_source(page_state="ready"), _snapshot_with_dialog()))
    assert occluded["presentation"]["topmost_layer_kind"] == "overlay"
    assert occluded["presentation"]["base_page_visibility"] == "fully_occluded"
    assert occluded["presentation"]["overlay_coverage"] == 1.0


async def test_app_presentation_comes_from_visual_observation() -> None:
    source = replace(
        _source(page_state="stable"),
        surface_kind="android",
        visual_observation={
            "visual_summary": "首页上方有充值弹窗",
            "key_elements": [],
            "candidate_targets": [],
            "result_hash": "hash-1",
            "page_state": "popup",
            "overlay_signature": "recharge-offer",
            "surface_facts": {"popup_visible": True},
            "source": "app_visual",
        },
    )

    facts = await _facts(source)

    presentation = facts["presentation"]
    assert presentation["topmost_layer_kind"] == "overlay"
    assert presentation["base_page_visibility"] == "fully_occluded"
    assert presentation["overlay_signature"] == "recharge-offer"
    assert presentation["evidence_sources"] == ["app_visual_observation"]


async def test_app_without_visual_observation_uses_capture_page_state_only() -> None:
    source = replace(_source(page_state="stable"), surface_kind="android", visual_observation=None)

    facts = await _facts(source)

    assert facts["presentation"]["topmost_layer_kind"] == "page"
    assert facts["presentation"]["base_page_visibility"] == "visible"


async def test_declared_capture_fields_are_read_directly() -> None:
    """采集侧在证据清单里声明了遮挡 / 诊断 / 登录态：判定包直接按声明取值。"""
    # Flutter 画布上识别报 popup（底页可见程度未知）；采集侧关完弹窗并验证过，声明没遮挡
    stored = {**_natural_presentation(), "hit_test_status": "degraded"}
    source = replace(
        _source(page_state="popup"),
        presentation=stored,
        declared_flags={"is_obstructed": False, "session_auth": "guest"},
    )
    service = ObservationPacketService(repository=FakeObservationPacketRepository(source))
    packet = await service.build(_request())

    presentation = packet.packet_payload["facts"]["presentation"]
    assert presentation["topmost_layer_kind"] == "page"
    assert presentation["base_page_visibility"] == "visible"
    assert "capture_declared" in presentation["evidence_sources"]
    assert "declared_obstruction_overrides_derived" in presentation["uncertainty"]
    assert packet.packet_payload["page"]["session_auth"] == "guest"

    diagnostic = await _facts(
        replace(
            _source(page_state="stable"),
            presentation=_natural_presentation(),
            declared_flags={"diagnostic": ["style_hide_obstruction"]},
        )
    )
    assert diagnostic["presentation"]["mutation_mode"] == "instrumented_diagnostic"
    assert diagnostic["presentation"]["diagnostic_operations"] == [
        {"kind": "style_hide_obstruction", "source": "capture_declared"}
    ]


async def test_packets_without_declared_fields_are_unchanged() -> None:
    source = replace(_source(page_state="stable"), presentation=_natural_presentation())
    service = ObservationPacketService(repository=FakeObservationPacketRepository(source))
    packet = await service.build(_request())

    assert packet.packet_payload["facts"]["presentation"] == _natural_presentation()
    assert "session_auth" not in packet.packet_payload["page"]
