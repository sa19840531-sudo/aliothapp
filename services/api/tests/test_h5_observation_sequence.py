"""H5 补证角色的结构化观察、时序归属与完整弹窗普查回归。"""

import json
from dataclasses import replace

import pytest

from services.api.app.application.h5_observation_sequence import h5_modal_count
from services.api.app.application.observation_packet import (
    ManifestQualityProjection,
    ObservationPacketService,
    ObservationPacketValidationError,
    _compact_ui_snapshot,
)
from services.api.app.application.rule_capability_engine import _popup_sequence
from services.api.tests.test_observation_packet import _artifact, _request, _source


def _snapshot(*, dialogs=1):
    return {
        "url": "https://example.com/home",
        "viewport": {"width": 390, "height": 844},
        "h5_modal_census_selectors": [f"#layer-{i}" for i in range(dialogs)],
        "h5_modal_census_truncated": False,
        "elements": [
            {
                "selector": f"#layer-{i}",
                "tag": "dialog",
                "role": "dialog",
                "aria_modal": True,
                "h5_modal_ancestor_selectors": [],
                "class_tokens": [],
                "rect": {"width": 300, "height": 600},
            }
            for i in range(dialogs)
        ],
    }


def _capture(capture_id, *, before=False, platform="h5"):
    base = _source()
    return replace(
        base,
        page_capture_id=capture_id,
        target_platform=platform,
        capture_role="probe_before" if before else "probe_result_replay",
        parent_capture_id=None if before else "before",
        captured_at="2026-10-03T01:00:00Z" if before else "2026-10-03T01:00:01Z",
        manifest_quality=ManifestQualityProjection(f"manifest-{capture_id}", 1, "pass"),
        artifacts=(
            _artifact(
                f"ui-{capture_id}",
                "ui_snapshot",
                json.dumps(
                    _snapshot(dialogs=1 if before else 0),
                ).encode(),
                "application/json",
            ),
            *[
                replace(item, artifact_id=f"{item.artifact_id}-{capture_id}")
                for item in base.artifacts
                if item.artifact_type != "ui_snapshot"
            ],
        ),
    )


class _Repository:
    def __init__(self, *, primary=None, before=None):
        self.sources = {
            "after": primary or _capture("after"),
            "before": before or _capture("before", before=True),
        }

    async def load_source_for_manifest(self, *, task_id, page_capture_id, manifest_id):
        return self.sources[page_capture_id]

    async def load_source(self, *, task_id, page_capture_id):
        return self.sources[page_capture_id]

    async def save_packet(self, **kwargs):
        return "packet-h5-roles"


def _bound_request():
    return replace(
        _request(),
        page_capture_id="after",
        source_agent_run_id="producer-run-1",
        evidence_capture_roles={"primary": "after", "before": "before"},
        evidence_manifest_refs={
            role: {
                "capture_id": capture,
                "manifest_id": f"manifest-{capture}",
                "manifest_version": "1",
            }
            for role, capture in (("primary", "after"), ("before", "before"))
        },
    )


async def test_h5_bound_before_after_snapshots_are_delivered_to_actual_packet():
    packet = await ObservationPacketService(repository=_Repository()).build(_bound_request())
    facts = packet.packet_payload["facts"]
    sequence = facts["ui_snapshot"]
    assert sequence["sequence"] is True
    assert sequence["source"] == "h5_bound_action_capture_roles"
    assert [frame["capture_id"] for frame in sequence["frames"]] == ["before", "after"]
    assert sequence["interval_ms"] == 1000
    assert h5_modal_count(sequence["frames"][0]) == 1
    assert h5_modal_count(sequence["frames"][1]) == 0
    assert facts["capture_roles"]["before"]["ui_snapshot"]["elements"]
    assert packet.quality_gate_status == "pass"


async def test_h5_shadow_manifest_reads_keep_real_bound_action_sequence():
    repository = _Repository(
        primary=replace(_capture("after"), manifest_quality=None),
        before=replace(_capture("before", before=True), manifest_quality=None),
    )
    packet = await ObservationPacketService(repository=repository).build(
        replace(_bound_request(), evidence_manifest_refs=None)
    )
    sequence = packet.packet_payload["facts"]["ui_snapshot"]
    assert sequence.get("sequence") is True
    assert [frame["capture_id"] for frame in sequence["frames"]] == ["before", "after"]
    assert packet.quality_gate_status == "pass"
    assert _popup_sequence(packet.packet_payload, {}).conclusion == "evidence_insufficient"
    assert "popup_sequence_cleared_observation_missing" in _popup_sequence(
        packet.packet_payload, {},
    ).facts["gap_codes"]


def test_h5_complete_census_survives_unrelated_element_budget_compaction():
    from services.api.app.application.observation_packet import _limit_packet_ui_snapshot

    snapshot = _snapshot()
    snapshot["budget_truncated"] = True
    snapshot["elements"] = [
        *[{"selector": f"#content-{i}", "tag": "img", "text": "广告"} for i in range(100)],
        *snapshot["elements"],
    ]
    _limit_packet_ui_snapshot(snapshot, maximum_frames=3, maximum_elements=8)
    assert len(snapshot["elements"]) <= 8
    assert h5_modal_count(snapshot) == 1


async def test_h5_primary_before_and_after_close_keep_actual_temporal_roles():
    request = replace(
        _bound_request(),
        evidence_source_page_capture_id="before",
        evidence_capture_roles={"primary": "before", "after_close": "after"},
        evidence_manifest_refs={
            role: {
                "capture_id": capture,
                "manifest_id": f"manifest-{capture}",
                "manifest_version": "1",
            }
            for role, capture in (("primary", "before"), ("after_close", "after"))
        },
    )
    packet = await ObservationPacketService(repository=_Repository()).build(request)
    sequence = packet.packet_payload["facts"]["ui_snapshot"]
    assert sequence["sequence"] is True
    assert [frame["capture_id"] for frame in sequence["frames"]] == ["before", "after"]
    assert packet.packet_payload["page"]["page_capture_id"] == "before"
    assert packet.page_capture_id == "after"


@pytest.mark.parametrize("mutation", ["web", "parent", "time", "naive_time", "no_pass"])
@pytest.mark.parametrize("shadow_read", [False, True])
async def test_role_material_does_not_fabricate_h5_sequence(mutation, shadow_read):
    primary = _capture("after")
    before = _capture("before", before=True)
    request = _bound_request()
    if shadow_read:
        primary = replace(primary, manifest_quality=None)
        before = replace(before, manifest_quality=None)
        request = replace(request, evidence_manifest_refs=None)
    if mutation == "web":
        primary = replace(primary, target_platform="web")
        before = replace(before, target_platform="web")
    elif mutation == "parent":
        primary = replace(primary, parent_capture_id="unrelated")
    elif mutation == "time":
        primary = replace(primary, captured_at="2026-10-03T00:59:59Z")
    elif mutation == "naive_time":
        primary = replace(primary, captured_at="2026-10-03T01:00:01")
    else:
        primary = replace(
            primary,
            manifest_quality=ManifestQualityProjection(
                "manifest-after",
                1,
                "pending",
            ),
        )
    packet = await ObservationPacketService(
        repository=_Repository(
            primary=primary,
            before=before,
        )
    ).build(request)
    assert packet.packet_payload["facts"]["ui_snapshot"].get("sequence") is not True


async def test_cross_run_role_material_is_rejected_before_sequence_projection():
    before = replace(_capture("before", before=True), agent_run_id="other-run")
    with pytest.raises(ObservationPacketValidationError, match="证据生产 Run"):
        await ObservationPacketService(repository=_Repository(before=before)).build(
            _bound_request()
        )


def test_h5_census_and_semantics_survive_packet_compaction():
    snapshot = _snapshot()
    snapshot["elements"] = [
        *[{"selector": f"#content-{i}", "tag": "button", "text": "内容"} for i in range(100)],
        *snapshot["elements"],
    ]
    compact = _compact_ui_snapshot(snapshot)
    assert compact["h5_modal_census_truncated"] is False
    assert h5_modal_count(compact) == 1


@pytest.mark.parametrize("incomplete", ["missing", "truncated", "budget"])
def test_h5_incomplete_modal_census_never_becomes_zero_or_passing_sequence(incomplete):
    snapshot = _snapshot()
    if incomplete == "missing":
        snapshot["elements"] = []
    elif incomplete == "truncated":
        snapshot["h5_modal_census_truncated"] = True
    else:
        snapshot["budget_truncated"] = True
        snapshot["elements"] = []
    assert h5_modal_count(snapshot) is None
    result = _popup_sequence(
        {
            "identity": {"target_platform": "h5"},
            "facts": {
                "ui_snapshot": {"sequence": True, "frames": [snapshot, _snapshot(dialogs=0)]}
            },
        },
        {},
    )
    assert result.status == "insufficient"


def test_h5_two_concurrent_dialogs_fail_even_without_css_names():
    result = _popup_sequence(
        {
            "identity": {"target_platform": "h5"},
            "facts": {
                "ui_snapshot": {
                    "sequence": True,
                    "frames": [
                        _snapshot(dialogs=2),
                        _snapshot(dialogs=0),
                    ],
                }
            },
        },
        {},
    )
    assert result.status == "ok"
    assert result.conclusion == "fail"
    assert result.facts["modal_counts"] == [2, 0]


@pytest.mark.parametrize("dialogs,incomplete", [(1, False), (0, True)])
def test_h5_single_frame_modal_or_unknown_census_is_not_skipped(dialogs, incomplete):
    snapshot = _snapshot(dialogs=dialogs)
    snapshot["h5_modal_census_truncated"] = incomplete
    result = _popup_sequence(
        {"identity": {"target_platform": "h5"}, "facts": {"ui_snapshot": snapshot}},
        {},
    )
    assert result.status == "insufficient"


@pytest.mark.parametrize("malformed", ["geometry", "parents"])
def test_h5_malformed_modal_geometry_or_parent_chain_is_unknown(malformed):
    snapshot = _snapshot()
    if malformed == "geometry":
        snapshot["elements"][0]["rect"]["width"] = float("nan")
    else:
        snapshot["elements"][0]["h5_modal_ancestor_selectors"] = [None]
    assert h5_modal_count(snapshot) is None


@pytest.mark.parametrize("census", [None, "#layer", [None], [{}]])
def test_compacting_invalid_h5_census_does_not_create_complete_empty_census(census):
    snapshot = _snapshot(dialogs=0)
    snapshot["h5_modal_census_selectors"] = census
    compact = _compact_ui_snapshot(snapshot)
    assert compact["h5_modal_census_truncated"] is True
    assert h5_modal_count(compact) is None


async def test_compacting_h5_role_sequence_preserves_actual_capture_ids():
    packet = await ObservationPacketService(repository=_Repository()).build(_bound_request())
    compact = _compact_ui_snapshot(packet.packet_payload["facts"]["ui_snapshot"])
    assert compact["source"] == "h5_bound_action_capture_roles"
    assert [frame["capture_id"] for frame in compact["frames"]] == ["before", "after"]


@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("watch", ["missing", "clear", "late"])
async def test_real_h5_dialog_close_survives_role_packet_and_rule_projection(semantic, watch):
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot

    attributes = 'role="dialog" aria-modal="true"' if semantic else ""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(f"""
                <style>body{{margin:0}} #shell{{position:fixed;inset:0;z-index:500}}
                  #panel{{margin:200px 20px;background:white;height:300px}}</style>
                <main><h1>首页</h1></main>
                <div id="shell" {attributes}><section id="panel"><h2>公告</h2>
                  <button id="exit" data-dismiss="modal"
                    onclick="document.querySelector('#shell').remove()">关闭</button>
                </section></div>
            """)
            before = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            await page.locator("#exit").click()
            after = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            after_material = after
            if watch != "missing":
                if watch == "late":
                    await page.evaluate("""() => {
                      const layer=document.createElement('dialog');layer.open=true;
                      layer.textContent='迟到弹窗';document.body.append(layer);
                    }""")
                observed = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
                # 三帧来自真实DOM采集，不复制清空帧冒充后续观察。
                after_material = {"sequence": True, "frames": [before, after, observed]}
            repository = _Repository()
            for capture_id, snapshot in (("before", before), ("after", after_material)):
                source = repository.sources[capture_id]
                repository.sources[capture_id] = replace(
                    source,
                    artifacts=tuple(
                        replace(artifact, payload=json.dumps(snapshot).encode())
                        if artifact.artifact_type == "ui_snapshot"
                        else artifact
                        for artifact in source.artifacts
                    ),
                )
            packet = await ObservationPacketService(repository=repository).build(_bound_request())
            sequence = packet.packet_payload["facts"]["ui_snapshot"]
            counts = [1, 0] if watch == "missing" else [1, 0, 1 if watch == "late" else 0]
            assert [h5_modal_count(frame) for frame in sequence["frames"]] == counts
            result = _popup_sequence(packet.packet_payload, {})
            assert result.status == ("insufficient" if watch == "missing" else "ok")
            assert result.conclusion == {
                "missing": "evidence_insufficient", "clear": "pass", "late": "fail",
            }[watch]
            assert result.facts["modal_counts"] == counts
        finally:
            await browser.close()
