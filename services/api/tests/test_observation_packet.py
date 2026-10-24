import json
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from services.api.app.application.observation_packet import (
    MAX_OBSERVATION_PACKET_BYTES,
    BuildObservationPacketRequest,
    ManifestQualityProjection,
    ObservationArtifact,
    ObservationJourneyStep,
    ObservationPacketService,
    ObservationPacketValidationError,
    ObservationSource,
    _app_observation_snapshot_sequence,
    _compact_journey,
    _compact_media_state,
    _compact_ui_element,
    _compact_ui_snapshot,
    _enforce_packet_budget,
    _project_app_ad_landing_screenshot_pair,
    _summarize_dom,
    build_app_observation_location,
)


def _static_visual_app_source(*, png: bytes | None = None) -> ObservationSource:
    if png is None:
        image = Image.new("RGB", (32, 64), "white")
        ImageDraw.Draw(image).rectangle((3, 5, 25, 15), fill="black")
        stream = BytesIO()
        image.save(stream, format="PNG")
        png = stream.getvalue()
    return replace(
        _source(page_state="stable"),
        surface_kind="android",
        page_url="app://com.example/.MainActivity",
        artifacts=(_artifact("visual-shot", "screenshot", png, "image/png"),),
        app_capture_facts={
            "package_name": "com.example",
            "activity": ".MainActivity",
            "width": 32,
            "height": 64,
        },
        journey_steps=(),
        action_log=(),
    )


def _static_visual_request(rule_code: str = "alioth_spec_24") -> BuildObservationPacketRequest:
    return replace(
        _request(),
        rule_code=rule_code,
        category="visual",
        executor_type="hybrid",
        input_contract={
            "required_artifacts": ["screenshot", "ui_snapshot", "dom"],
            "quality_gate": {"min_screenshot_count": 1, "allow_empty_visible_text": True},
        },
        judge_config={
            "capability_config": {
                "semantic_judge": "visual_rule_judge_tool",
                "requires_journey": False,
            },
        },
        requested_evidence_artifact_ids=(),
        agent_input={},
    )


@pytest.mark.parametrize("rule_code", ["alioth_spec_24", "alioth_spec_27", "alioth_spec_28"])
async def test_static_visual_app_rule_consumes_real_png_without_hierarchy(rule_code: str) -> None:
    source = _static_visual_app_source()
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source),
    ).build(_static_visual_request(rule_code))

    assert packet.quality_gate_status == "pass"
    assert packet.image_payloads == (("visual-shot", source.artifacts[0].payload, "image/png"),)
    assert packet.packet_payload["facts"]["ui_snapshot"]["elements"] == []
    assert packet.packet_payload["facts"]["has_ui_tree"] is False
    assert packet.packet_payload["facts"]["action_log"] == []
    assert packet.packet_payload["facts"]["app_identity"]["safe_area"]["width"] == 32


@pytest.mark.parametrize("png", [b"", b"not-png", b"\x89PNG\r\n\x1a\ntruncated"])
async def test_static_visual_app_rule_does_not_treat_png_header_as_decodable_image(
    png: bytes,
) -> None:
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(_static_visual_app_source(png=png)),
    ).build(_static_visual_request())

    assert packet.quality_gate_status == "fail"
    assert "ui_snapshot" in packet.packet_payload["quality_gate"]["missing_sources"]


async def test_static_visual_app_rule_keeps_trusted_display_dimensions() -> None:
    source = _static_visual_app_source()
    source = replace(source, app_capture_facts={**source.app_capture_facts, "width": 33})
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source),
    ).build(_static_visual_request())

    assert packet.quality_gate_status == "fail"
    assert "ui_snapshot" in packet.packet_payload["quality_gate"]["missing_sources"]


@pytest.mark.parametrize("rule_code", ["alioth_spec_24", "alioth_spec_27", "alioth_spec_28"])
async def test_static_visual_app_rule_restores_full_display_from_nonzero_insets(
    rule_code: str,
) -> None:
    source = replace(_static_visual_app_source(), app_capture_facts={
        "package_name": "com.example",
        "activity": ".MainActivity",
        "device_snapshot": {"safe_area": {
            "left": 3, "right": 5, "top": 7, "bottom": 9,
            "width": 24, "height": 48, "source": "window_insets",
        }},
    })
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source),
    ).build(_static_visual_request(rule_code))

    assert packet.quality_gate_status == "pass"
    assert packet.image_payloads == (("visual-shot", source.artifacts[0].payload, "image/png"),)
    assert packet.packet_payload["facts"]["app_identity"]["safe_area"]["width"] == 24
    assert packet.packet_payload["facts"]["app_identity"]["safe_area"]["top"] == 7


async def test_static_visual_app_rule_rejects_png_cropped_to_safe_area() -> None:
    stream = BytesIO()
    Image.new("RGB", (24, 48), "white").save(stream, format="PNG")
    source = replace(_static_visual_app_source(png=stream.getvalue()), app_capture_facts={
        "package_name": "com.example",
        "activity": ".MainActivity",
        "device_snapshot": {"safe_area": {
            "left": 3, "right": 5, "top": 7, "bottom": 9,
            "width": 24, "height": 48, "source": "window_insets",
        }},
    })
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source),
    ).build(_static_visual_request())

    assert packet.quality_gate_status == "fail"
    assert "ui_snapshot" in packet.packet_payload["quality_gate"]["missing_sources"]


@pytest.mark.parametrize("field, value", [
    ("left", -1), ("right", -1), ("top", -1), ("bottom", -1),
    ("left", None), ("left", False), ("left", 0.5),
    ("left", float("inf")), ("left", float("nan")), ("left", "invalid"),
])
async def test_static_visual_app_rule_rejects_invalid_display_insets(
    field: str, value: object,
) -> None:
    area = {
        "left": 3, "right": 5, "top": 7, "bottom": 9,
        "width": 24, "height": 48, "source": "window_insets", field: value,
    }
    source = replace(_static_visual_app_source(), app_capture_facts={
        "package_name": "com.example",
        "activity": ".MainActivity",
        "device_snapshot": {"safe_area": area},
    })
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source),
    ).build(_static_visual_request())

    assert packet.quality_gate_status == "fail"
    assert "ui_snapshot" in packet.packet_payload["quality_gate"]["missing_sources"]


@pytest.mark.parametrize("facts, missing", [
    ({"width": 32, "height": 64}, "identity.package"),
    ({"package_name": "com.example"}, "safe_area"),
])
async def test_static_visual_app_rule_keeps_identity_gate(facts: dict, missing: str) -> None:
    source = replace(_static_visual_app_source(), app_capture_facts=facts)
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source),
    ).build(_static_visual_request())

    assert packet.quality_gate_status == "fail"
    assert missing in packet.packet_payload["quality_gate"]["missing_sources"]


@pytest.mark.parametrize("state", ["loading", "line_loading", "transitioning", "detection_error"])
async def test_static_visual_app_rule_keeps_unstable_page_gate(state: str) -> None:
    source = replace(_static_visual_app_source(), page_state=state)
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source),
    ).build(_static_visual_request())

    assert packet.quality_gate_status == "fail"
    assert f"page_state({state})" in packet.packet_payload["quality_gate"]["missing_sources"]


@pytest.mark.parametrize(
    "rule_code", ["alioth_3", "alioth_19", "alioth_spec_72", "alioth_44", "alioth_15"],
)
async def test_other_app_rules_do_not_gain_static_visual_structural_fallback(
    rule_code: str,
) -> None:
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(_static_visual_app_source()),
    ).build(_static_visual_request(rule_code))

    assert packet.quality_gate_status == "fail"
    assert "ui_snapshot" in packet.packet_payload["quality_gate"]["missing_sources"]


@pytest.mark.parametrize("capability", [
    {"semantic_judge": "visual_rule_judge_tool", "requires_journey": True},
    {"semantic_judge": "visual_rule_judge_tool"},
    {"semantic_judge": "text_rule_judge_tool", "requires_journey": False},
])
async def test_static_visual_fallback_requires_existing_visual_contract(capability: dict) -> None:
    request = replace(_static_visual_request(), judge_config={"capability_config": capability})
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(_static_visual_app_source()),
    ).build(request)

    assert packet.quality_gate_status == "fail"
    assert "ui_snapshot" in packet.packet_payload["quality_gate"]["missing_sources"]


async def test_static_visual_fallback_keeps_additional_capture_role_contract() -> None:
    request = _static_visual_request()
    request = replace(request, input_contract={
        **request.input_contract,
        "required_capture_roles": ["primary", "after"],
    })
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(_static_visual_app_source()),
    ).build(request)

    assert packet.quality_gate_status == "fail"
    assert "ui_snapshot" in packet.packet_payload["quality_gate"]["missing_sources"]


async def test_static_visual_app_rule_keeps_additional_action_evidence_requirement() -> None:
    request = _static_visual_request()
    request = replace(request, input_contract={
        **request.input_contract,
        "required_artifacts": ["screenshot", "ui_snapshot", "dom", "action_trace"],
    })
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(_static_visual_app_source()),
    ).build(request)

    assert packet.quality_gate_status == "fail"
    assert "action_trace" in packet.packet_payload["quality_gate"]["missing_sources"]


@pytest.mark.parametrize("status", ["fail", "pending", "pass"])
async def test_static_visual_app_rule_preserves_manifest_authority(status: str) -> None:
    source = replace(_static_visual_app_source(), manifest_quality=ManifestQualityProjection(
        manifest_id="manifest-visual", manifest_version=2,
        quality_status=status, quality_reason="清单原始质量结论",
    ))
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source),
    ).build(_static_visual_request())

    assert packet.quality_gate_status == status
    assert packet.quality_gate_reason == "清单原始质量结论"
    assert packet.packet_payload["quality_gate"]["source"] == "evidence_manifest"


async def test_static_visual_app_fallback_does_not_apply_to_web_source() -> None:
    source = replace(_static_visual_app_source(), surface_kind="web")
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source),
    ).build(_static_visual_request())

    assert packet.quality_gate_status == "fail"
    assert "ui_snapshot" in packet.packet_payload["quality_gate"]["missing_sources"]


@pytest.mark.parametrize("gate_status,missing", [("pass", []), ("fail", ["action_trace"])])
def test_h5_packet_budget_bounds_every_capture_role_without_inventing_missing_evidence(
    gate_status, missing,
) -> None:
    snapshot = {"sequence": True, "frames": [
        {"capture_id": f"frame-{frame}", "elements": [
            {"selector": f"#item-{item}", "text": "字" * 280}
            for item in range(80)
        ]} for frame in range(15)
    ]}
    payload = {
        "identity": {"target_platform": "h5"},
        "facts": {"ui_snapshot": {"elements": []}, "capture_roles": {
            role: {"page_capture_id": f"capture-{role}", "artifact_ids": [f"ui-{role}"],
                   "manifest_id": f"manifest-{role}", "ui_snapshot": snapshot}
            for role in ("primary", "before")
        }},
        "quality_gate": {"status": gate_status, "missing_sources": missing},
    }
    bounded, status, _, sources = _enforce_packet_budget(
        packet_payload=payload, gate_status=gate_status, gate_reason=None,
        missing_sources=missing,
    )
    assert len(json.dumps(bounded, ensure_ascii=False).encode()) <= MAX_OBSERVATION_PACKET_BYTES
    assert status == gate_status
    assert sources == missing
    for role, fact in bounded["facts"]["capture_roles"].items():
        assert fact["manifest_id"] == f"manifest-{role}"
        assert fact["artifact_ids"] == [f"ui-{role}"]
        assert fact["ui_snapshot"]["frames"][0]["capture_id"] == "frame-0"
        assert fact["ui_snapshot"]["frames"][-1]["capture_id"] == "frame-14"


def test_h5_irreducible_packet_budget_is_not_a_recapture_evidence_gap() -> None:
    payload = {
        "identity": {"target_platform": "h5"},
        "rule": {"criteria": {"huge": "x" * 300_000}},
        "facts": {}, "quality_gate": {"status": "pass", "missing_sources": []},
    }
    bounded, status, _, sources = _enforce_packet_budget(
        packet_payload=payload, gate_status="pass", gate_reason=None, missing_sources=[],
    )
    assert len(json.dumps(bounded).encode()) <= MAX_OBSERVATION_PACKET_BYTES
    assert bounded["quality_gate"]["payload_budget_exceeded"] is True
    assert sources == []


class FakeObservationPacketRepository:
    def __init__(self, source: ObservationSource) -> None:
        self.source = source
        self.saved: list[dict] = []
        self.load_count = 0

    async def load_source(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> ObservationSource | None:
        self.load_count += 1
        assert task_id == self.source.task_id
        assert page_capture_id == self.source.page_capture_id
        return self.source

    async def save_packet(self, **kwargs) -> str:
        self.saved.append(kwargs)
        return "packet-1"


class FakeManifestObservationPacketRepository(FakeObservationPacketRepository):
    def __init__(self, source: ObservationSource) -> None:
        super().__init__(source)
        self.manifest_reads: list[tuple[str, str, str]] = []

    async def load_source_for_manifest(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        manifest_id: str,
    ) -> ObservationSource | None:
        self.manifest_reads.append((task_id, page_capture_id, manifest_id))
        assert page_capture_id == self.source.page_capture_id
        return self.source


def _artifact(
    artifact_id: str,
    artifact_type: str,
    payload: bytes,
    content_type: str,
) -> ObservationArtifact:
    return ObservationArtifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        content_type=content_type,
        payload=payload,
    )


def _source(
    *,
    visible_text: str = "首页内容",
    page_state: str = "ready",
    include_action_trace: bool = False,
) -> ObservationSource:
    ui_snapshot = {
        "schema_version": "ui-snapshot.v1",
        "url": "https://example.com/home",
        "element_count": 1,
        "elements": [
            {
                "selector": "#logout",
                "tag": "button",
                "text": "退出登录",
                "rect": {"x": 10, "y": 10, "width": 80, "height": 40},
            }
        ],
    }
    return ObservationSource(
        task_id="task-1",
        page_capture_id="capture-1",
        agent_run_id="producer-run-1",
        page_url="https://example.com/home",
        page_title="首页",
        page_type="home",
        page_state=page_state,
        action_log=({"action": "observe"},),
        artifacts=(
            _artifact(
                "dom-1",
                "dom",
                b"<main><button id='logout'>Logout</button></main>",
                "text/html; charset=utf-8",
            ),
            _artifact(
                "text-1",
                "visible_text",
                visible_text.encode(),
                "text/plain; charset=utf-8",
            ),
            _artifact(
                "shot-1",
                "screenshot",
                b"\x89PNG\r\n\x1a\npayload",
                "image/png",
            ),
            _artifact(
                "ui-1",
                "ui_snapshot",
                json.dumps(ui_snapshot).encode(),
                "application/json",
            ),
        )
        + (
            (
                _artifact(
                    "action-1",
                    "action_trace",
                    b'[{"action":"observe"}]',
                    "application/json",
                ),
            )
            if include_action_trace
            else ()
        ),
        journey_steps=(
            ObservationJourneyStep(
                page_capture_id="capture-0",
                page_seq=1,
                page_url="https://example.com/list",
                page_title="列表",
                page_type="content_list",
                page_state="ready",
                action_log=({"action": "click", "selector": "#first-card"},),
                artifacts=(
                    _artifact(
                        "journey-text-1",
                        "visible_text",
                        b"List item",
                        "text/plain; charset=utf-8",
                    ),
                    _artifact(
                        "journey-ui-1",
                        "ui_snapshot",
                        json.dumps(
                            {
                                "schema_version": "ui-snapshot.v1",
                                "elements": [
                                    {
                                        "selector": "#first-card",
                                        "tag": "a",
                                        "text": "First item",
                                    }
                                ],
                            }
                        ).encode(),
                        "application/json",
                    ),
                ),
            ),
        ),
    )


async def test_packet_reads_exact_supplement_but_keeps_original_result_target() -> None:
    source = replace(_source(), page_capture_id="capture-2", agent_run_id="producer-run-2")
    repository = FakeManifestObservationPacketRepository(source)
    service = ObservationPacketService(repository=repository)
    request = BuildObservationPacketRequest(
        task_id="task-1",
        page_capture_id="capture-1",
        evidence_source_page_capture_id="capture-2",
        evidence_manifest_id="manifest-2",
        rule_id="rule-1",
        rule_code="alioth_1",
        rule_name="规则一",
        category="visual",
        executor_type="llm",
        check_point="检查页面",
        input_contract={},
        judge_config={},
        manual_review_policy={},
        requested_evidence_artifact_ids=(),
        agent_input={},
        agent_run_id="consumer-run-1",
        source_agent_run_id="producer-run-2",
    )

    packet = await service.build(request)

    assert repository.manifest_reads == [("task-1", "capture-2", "manifest-2")]
    assert packet.page_capture_id == "capture-1"
    assert packet.packet_payload["page"]["page_capture_id"] == "capture-2"
    assert packet.packet_payload["identity"]["evidence_manifest_id"] == "manifest-2"
    assert repository.saved[0]["page_capture_id"] == "capture-1"


def _request() -> BuildObservationPacketRequest:
    return BuildObservationPacketRequest(
        task_id="task-1",
        page_capture_id="capture-1",
        rule_id="rule-1",
        rule_code="alioth_test",
        rule_name="测试规则",
        category="interaction",
        executor_type="rule_engine",
        check_point="应存在退出入口",
        input_contract={
            "required_artifacts": [
                "dom_snapshot",
                "visible_text",
                "screenshot",
                "ui_snapshot",
            ],
            "quality_gate": {
                "min_screenshot_count": 1,
                "allow_empty_visible_text": False,
            },
        },
        judge_config={"engine_config": {"handler": "logout_entry_presence_handler"}},
        manual_review_policy={"min_confidence": 0.8},
        requested_evidence_artifact_ids=("dom-1", "text-1"),
        agent_input={
            "visible_text": "模型伪造的文本",
            "screenshot_base64": "forbidden",
            "hint": "只作为候选输入",
        },
        agent_run_id="run-1",
        source_agent_run_id="producer-run-1",
    )


async def test_h5_original_input_keeps_all_sealed_frames_and_rechecks_authority() -> None:
    source = replace(_source(), target_platform="h5", original_page_input={
        "input_set_id": "original", "input_set_hash": "hash", "rule_contract_hash": "contract",
        "work_item_id": "work", "attempt_id": "attempt", "consumer_agent_run_id": "run-1",
    })
    source = replace(source, artifacts=source.artifacts + (replace(
        next(item for item in source.artifacts if item.artifact_type == "screenshot"),
        artifact_id="shot-2",
    ),))

    class ExactPageRepository(FakeObservationPacketRepository):
        async def load_source_for_page_input_set(self, **identity):
            assert identity["input_set_id"] == "original"
            assert identity["attempt_id"] == "attempt"
            self.load_count += 1
            return self.source

    repository = ExactPageRepository(source)
    service = ObservationPacketService(repository=repository)
    request = replace(
        _request(), original_page_input_set_id="original", expected_contract_hash="contract",
        work_item_id="work", attempt_id="attempt", lease_token="private")
    for _ in range(2):
        packet = await service.build(request)
        assert [item[0] for item in packet.image_payloads] == ["shot-1", "shot-2"]
    assert repository.load_count == 2


async def test_h5_observation_source_is_reloaded_when_presentation_changes() -> None:
    source = replace(
        _source(page_state="stable"),
        target_platform="h5",
        presentation={
            "protocol_version": "presentation-stack.v1",
            "topmost_layer_kind": "page",
            "base_page_visibility": "visible",
            "blocking_scope": "none",
            "mutation_mode": "natural",
            "hit_test_status": "verified",
            "evidence_sources": ["ui_snapshot"],
            "diagnostic_operations": [],
            "uncertainty": [],
        },
    )
    repository = FakeObservationPacketRepository(source)
    service = ObservationPacketService(repository=repository)

    first = await service.build(_request())
    repository.source = replace(
        source,
        page_state="overlay",
        presentation={
            **source.presentation,
            "topmost_layer_kind": "overlay",
            "base_page_visibility": "fully_occluded",
            "blocking_scope": "page",
        },
    )
    second = await service.build(replace(_request(), rule_id="rule-2"))

    assert repository.load_count == 2
    assert first.packet_payload["page"]["page_state"] == "stable"
    assert second.packet_payload["page"]["page_state"] == "overlay"
    assert second.packet_payload["facts"]["presentation"]["base_page_visibility"] == (
        "fully_occluded"
    )


def test_app_observation_location_requires_real_package_and_activity() -> None:
    assert (
        build_app_observation_location(
            package_name=" com.Example ",
            activity=" .MainActivity ",
        )
        == "app://com.example/.MainActivity"
    )
    assert (
        build_app_observation_location(
            package_name="com.example",
            activity="",
        )
        is None
    )
    assert (
        build_app_observation_location(
            package_name="com.example/other",
            activity=".MainActivity",
        )
        is None
    )


async def test_observation_packet_exposes_bounded_media_state_facts() -> None:
    source = _source(include_action_trace=True)
    media_state = {
        "schema_version": "media-state.v1",
        "captured_at": "2026-08-14T09:00:00Z",
        "items": [
            {
                "media_index": 0,
                "selector": "#video",
                "tag": "video",
                "current_time": 30.2,
                "duration": 120,
                "paused": False,
                "seeking": False,
                "playback_rate": 1,
            }
        ],
        "display_candidates": [{"selector": "#time", "text": "00:30 / 02:00", "bounds": {}}],
    }
    source = replace(
        source,
        artifacts=(
            *source.artifacts,
            _artifact(
                "media-1",
                "media_state",
                json.dumps(media_state).encode(),
                "application/json",
            ),
        ),
    )
    repository = FakeObservationPacketRepository(source)

    packet = await ObservationPacketService(repository=repository).build(_request())

    facts = packet.packet_payload["facts"]["media_state"]
    assert facts["items"][0]["current_time"] == 30.2
    assert facts["display_candidates"][0]["text"] == "00:30 / 02:00"
    assert "media-1" in packet.evidence_artifact_ids


def test_compact_media_state_drops_resource_urls_and_bounds_lists() -> None:
    compact = _compact_media_state(
        {
            "schema_version": "media-state.v1",
            "url": "https://example.com/video?token=secret",
            "items": [
                {
                    "media_index": 0,
                    "src": "https://cdn.example/video.mp4?token=secret",
                    "current_time": 10,
                    "duration": 20,
                }
            ],
            "display_candidates": [],
        }
    )

    assert "url" not in compact
    assert "src" not in compact["items"][0]


async def test_observation_packet_builds_trusted_bounded_facts() -> None:
    repository = FakeObservationPacketRepository(_source())
    packet = await ObservationPacketService(repository=repository).build(_request())

    assert packet.observation_packet_id == "packet-1"
    assert packet.quality_gate_status == "pass"
    assert packet.evidence_artifact_ids == (
        "dom-1",
        "text-1",
        "shot-1",
        "ui-1",
    )
    assert packet.packet_payload["facts"]["visible_text"] == "首页内容"
    assert packet.packet_payload["identity"] == {
        "source_agent_run_id": "producer-run-1",
        "consumer_agent_run_id": "run-1",
    }
    assert packet.packet_payload["candidate_input"] == {"hint": "只作为候选输入"}
    assert packet.packet_payload["facts"]["ui_snapshot"]["elements"][0]["text"] == ("退出登录")
    assert packet.packet_payload["facts"]["journey"][0]["page_type"] == ("content_list")
    assert packet.packet_payload["facts"]["journey"][0]["evidence_artifact_ids"] == [
        "journey-text-1",
        "journey-ui-1",
    ]
    assert packet.image_payloads[0][0] == "shot-1"
    assert repository.saved[0]["quality_gate_status"] == "pass"


async def test_observation_packet_keeps_locked_input_contract_over_candidate() -> None:
    """共享判定收到真实输入要求，页面或候选声明不能覆盖合同或泄露凭证。"""
    request = replace(
        _request(),
        input_contract={
            "required_artifacts": ["screenshot", "ui_snapshot"],
            "required_capture_roles": ["primary"],
            "quality_gate": {"min_screenshot_count": 1},
            "access_token": "must-not-reach-model",
        },
        agent_input={"input_contract": {"required_artifacts": ["screenshot"]}},
    )
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(_source()),
    ).build(request)

    contract = packet.packet_payload["rule"]["input_contract"]
    assert contract["required_artifacts"] == ["screenshot", "ui_snapshot"]
    assert contract["required_capture_roles"] == ["primary"]
    assert contract["access_token"] == "[redacted]"
    assert contract["projection_incomplete"] is True
    assert request.input_contract["access_token"] == "must-not-reach-model"
    contract["required_artifacts"].append("har")
    assert request.input_contract["required_artifacts"] == ["screenshot", "ui_snapshot"]


@pytest.mark.parametrize("requirement", [
    {"measurement_obligation": "必须使用网络实测" * 200},
    {"dom": "必须核对原始节点"},
    {"authorization": "该字段也可能承载必要规则要求"},
])
async def test_lossy_rule_contract_projection_cannot_be_judged_from_screenshot(requirement):
    from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import LLMSemanticRuleJudge

    request = replace(
        _request(), input_contract={"required_artifacts": ["screenshot"], **requirement},
    )
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(_source()),
    ).build(request)

    class ForbiddenClient:
        async def get_response(self, *args, **kwargs):
            raise AssertionError("合同要求已被删减时不得调用模型")

    judge = LLMSemanticRuleJudge(client=ForbiddenClient())
    result = await judge.judge(packet=packet, tool_results=[])
    assert result.conclusion == "manual_review"
    assert result.conflicts == ["rule_input_contract_truncated"]
    assert result.missing_evidence == []


async def test_observation_packet_rejects_cross_run_capture() -> None:
    repository = FakeObservationPacketRepository(
        replace(_source(), agent_run_id="another-producer-run")
    )

    with pytest.raises(ObservationPacketValidationError, match="生产 Run"):
        await ObservationPacketService(repository=repository).build(_request())

    assert repository.saved == []


async def test_app_packet_projects_capture_identity_and_snapshot_from_ui_tree() -> None:
    source = replace(
        _source(),
        page_url="app://uk.pnixc.juywgn/post-list",
        page_type="post_list",
        surface_kind="android",
        artifacts=(
            _artifact(
                "app-shot-1",
                "screenshot",
                b"\x89PNG\r\n\x1a\napp",
                "image/png",
            ),
            _artifact(
                "app-tree-1",
                "ui_tree",
                (
                    '<hierarchy><node package="uk.pnixc.juywgn" '
                    'activity="com.didi.hlw.MainActivity" '
                    'class="android.widget.ImageView" resource-id="cover" '
                    'bounds="[0,0][400,300]"/>'
                    '<node package="uk.pnixc.juywgn" '
                    'class="android.widget.TextView" '
                    'text="示例作者•2026年08月23日" '
                    'bounds="[0,320][400,380]"/></hierarchy>'
                ).encode(),
                "application/xml",
            ),
        ),
        app_capture_facts={
            "package_name": "uk.pnixc.juywgn",
            "activity": "com.didi.hlw.MainActivity",
            "width": 1080,
            "height": 1920,
        },
    )
    repository = FakeObservationPacketRepository(source)

    packet = await ObservationPacketService(repository=repository).build(_request())

    assert packet.quality_gate_status == "pass"
    facts = packet.packet_payload["facts"]
    assert facts["app_identity"]["package_name"] == "uk.pnixc.juywgn"
    assert facts["app_identity"]["activity"] == "com.didi.hlw.MainActivity"
    assert facts["app_identity"]["safe_area"]["source"] == "display_bounds"
    assert facts["ui_snapshot"]["source"] == "android_ui_hierarchy"
    assert facts["ui_snapshot"]["package_name"] == "uk.pnixc.juywgn"
    assert facts["ui_snapshot"]["safe_area"]["width"] == 1080
    assert facts["media_state"]["items"][0]["kind"] == "image"
    assert facts["content_attribution"] == {
        "scope": "ugc",
        "basis": ["page_type:post_list", "accessibility_byline"],
        "bylines": [
            {
                "author_text": "示例作者",
                "published_at_text": "2026年08月23日",
                "source": "android_accessibility_byline",
            }
        ],
        "ownership": {
            "status": "unverified",
            "owner_account_id": None,
            "basis": [],
        },
    }
    assert facts["has_ui_snapshot"] is True


async def test_app_packet_falls_back_to_capture_identity_when_page_url_missing() -> None:
    source = replace(
        _source(),
        page_url="   ",
        surface_kind="android",
        app_capture_facts={
            "package_name": " COM.Example ",
            "activity": " .MainActivity ",
        },
    )

    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source)
    ).build(_request())

    assert packet.packet_payload["page"]["url"] == ("app://com.example/.MainActivity")


async def test_app_packet_keeps_real_device_safe_area_and_fails_without_identity_facts() -> None:
    source = replace(
        _source(),
        surface_kind="android",
        artifacts=(
            _artifact(
                "app-shot-2",
                "screenshot",
                b"\x89PNG\r\n\x1a\napp",
                "image/png",
            ),
            _artifact(
                "app-snapshot-2",
                "ui_snapshot",
                json.dumps(
                    {
                        "source": "android_ui_hierarchy",
                        "package_name": "com.example.spoofed",
                        "safe_area": {
                            "top": 999,
                            "right": 0,
                            "bottom": 0,
                            "left": 0,
                            "width": 1,
                            "height": 1,
                            "source": "snapshot_payload",
                        },
                        "elements": [],
                    }
                ).encode(),
                "application/json",
            ),
            _artifact(
                "app-text-2",
                "visible_text",
                "首页".encode(),
                "text/plain; charset=utf-8",
            ),
        ),
        app_capture_facts={
            "package_name": "uk.pnixc.juywgn",
            "width": 1080,
            "height": 1920,
            "device_snapshot": {
                "safe_area": {
                    "top": 24,
                    "right": 0,
                    "bottom": 48,
                    "left": 0,
                    "width": 1080,
                    "height": 1920,
                    "source": "device_snapshot",
                }
            },
        },
    )
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source)
    ).build(_request())

    assert packet.quality_gate_status == "pass"
    facts = packet.packet_payload["facts"]
    assert facts["ui_snapshot"]["package_name"] == "uk.pnixc.juywgn"
    assert facts["ui_snapshot"]["safe_area"]["top"] == 24
    assert facts["ui_snapshot"]["safe_area"]["source"] == "device_snapshot"

    no_facts_source = replace(
        source,
        app_capture_facts=None,
        artifacts=(
            _artifact(
                "app-shot-3",
                "screenshot",
                b"\x89PNG\r\n\x1a\napp",
                "image/png",
            ),
            _artifact(
                "app-tree-3",
                "ui_tree",
                '<hierarchy><node text="首页"/></hierarchy>'.encode(),
                "application/xml",
            ),
        ),
    )
    no_facts_packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(no_facts_source)
    ).build(_request())

    assert no_facts_packet.quality_gate_status == "fail"
    assert "identity.package" in (no_facts_packet.quality_gate_reason or "")
    assert "safe_area" in (no_facts_packet.quality_gate_reason or "")


def test_compact_journey_preserves_entry_steps_and_recent_steps() -> None:
    steps = tuple(
        ObservationJourneyStep(
            page_capture_id=f"capture-{page_seq}",
            page_seq=page_seq,
            page_url=f"https://example.com/{page_seq}",
            page_title=None,
            page_type="startup_ad" if page_seq == 1 else "home",
            page_state="ready",
            action_log=(),
            artifacts=(),
        )
        for page_seq in range(1, 26)
    )

    compact = _compact_journey(steps)

    assert len(compact) == 20
    assert [item["page_seq"] for item in compact[:3]] == [1, 2, 3]
    assert [item["page_seq"] for item in compact[-3:]] == [23, 24, 25]


def test_app_observations_in_same_surface_phase_form_media_frame_sequence() -> None:
    def media_snapshot(width: int) -> dict:
        return {
            "source": "android_ui_hierarchy",
            "surface_kind": "android",
            "elements": [
                {
                    "selector": "resource-id:hero",
                    "role": "image",
                    "rect": {"width": width, "height": 180},
                    "image": {
                        "kind": "image",
                        "source": "android_accessibility_class",
                    },
                }
            ],
        }

    previous = ObservationJourneyStep(
        page_capture_id="capture-observe-1",
        page_seq=1,
        page_url="app://com.example/HomeActivity",
        page_title=None,
        page_type="home",
        page_state="ready",
        action_log=(),
        artifacts=(
            _artifact(
                "ui-tree-observe-1",
                "ui_tree",
                (
                    b'<hierarchy><node class="android.widget.ImageView" '
                    b'resource-id="hero" bounds="[0,0][320,180]" '
                    b'visible-to-user="true" /></hierarchy>'
                ),
                "application/xml",
            ),
        ),
        capture_role="observation",
        surface_phase_run_id="surface-phase-1",
        captured_at="2026-08-25 10:00:00+08",
    )
    unrelated = replace(
        previous,
        page_capture_id="capture-other-phase",
        page_seq=2,
        surface_phase_run_id="surface-phase-2",
    )
    source = replace(
        _source(),
        page_capture_id="capture-observe-2",
        surface_kind="android",
        capture_role="observation",
        surface_phase_run_id="surface-phase-1",
        captured_at="2026-08-25 10:00:01+08",
        journey_steps=(previous, unrelated),
    )

    sequence = _app_observation_snapshot_sequence(
        source=source,
        current_snapshot=media_snapshot(300),
    )

    assert sequence["sequence"] is True
    assert sequence["source"] == "app_surface_phase_observations"
    assert [frame["capture_id"] for frame in sequence["frames"]] == [
        "capture-observe-1",
        "capture-observe-2",
    ]
    assert sequence["frames"][0]["elements"][0]["rect"]["width"] == 320
    assert sequence["frames"][1]["elements"][0]["rect"]["width"] == 300
    assert [frame["captured_at"] for frame in sequence["frames"]] == [
        "2026-08-25 10:00:00+08",
        "2026-08-25 10:00:01+08",
    ]


def test_app_ad_landing_projects_real_source_and_browser_png_pair() -> None:
    action = {
        "action": "click",
        "result": "success",
        "action_execution_id": "action-ad-1",
        "ad_navigation_evidence": {
            "schema_version": "ad-navigation-evidence.v1",
            "status": "observed",
            "navigation_mode": "external_browser",
            "pair_complete": True,
            "source": {
                "capture_id": "capture-ad-source",
                "artifact_id": "artifact-ad-source",
                "screenshot_ref": "ad_landing_consistency#frame-0",
            },
            "landing": {
                "capture_id": "capture-ad-landing",
                "artifact_id": "artifact-ad-landing",
                "package_name": "com.android.chrome",
                "screenshot_ref": "ad_landing_consistency#frame-1",
            },
        },
    }
    source_step = ObservationJourneyStep(
        page_capture_id="capture-ad-source",
        page_seq=1,
        page_url="app://com.example.target/.MainActivity",
        page_title="首个弹窗广告",
        page_type="startup_ad",
        page_state="popup",
        action_log=(),
        artifacts=(
            _artifact(
                "artifact-ad-source",
                "screenshot",
                b"\x89PNG\r\n\x1a\nsource",
                "image/png",
            ),
        ),
        capture_role="before_action",
    )
    source = replace(
        _source(),
        page_capture_id="capture-ad-landing",
        page_url="app://com.android.chrome/com.google.android.apps.chrome.Main",
        page_title="广告落地页",
        page_type="browser_page",
        surface_kind="android",
        capture_role="after_action",
        parent_capture_id="capture-ad-source",
        action_log=(action,),
        artifacts=(
            _artifact(
                "artifact-ad-landing",
                "screenshot",
                b"\x89PNG\r\n\x1a\nlanding",
                "image/png",
            ),
        ),
        journey_steps=(source_step,),
    )

    projection = _project_app_ad_landing_screenshot_pair(source)

    assert [item.artifact_id for item in projection["artifacts"]] == [
        "artifact-ad-source",
        "artifact-ad-landing",
    ]
    evidence = projection["screenshot_evidence"][0]
    assert evidence["purpose"] == "ad_landing_consistency"
    assert evidence["frame_count"] == 2
    assert [item["role"] for item in evidence["frames"]] == ["source", "landing"]
    assert [item["artifact_ref"] for item in evidence["frames"]] == [
        "artifact-ad-source",
        "artifact-ad-landing",
    ]


def test_compact_journey_keeps_bounded_media_state_for_before_after_rules() -> None:
    step = ObservationJourneyStep(
        page_capture_id="capture-before",
        page_seq=1,
        page_url="https://example.com/video",
        page_title="播放页",
        page_type="video_detail",
        page_state="ready",
        action_log=(),
        artifacts=(
            _artifact(
                "media-before",
                "media_state",
                json.dumps(
                    {
                        "schema_version": "media-state.v1",
                        "items": [{"current_time": 10, "duration": 120}],
                    }
                ).encode(),
                "application/json",
            ),
        ),
    )

    compact = _compact_journey((step,))

    assert compact[0]["media_state"]["items"][0] == {
        "current_time": 10,
        "duration": 120,
    }


async def test_observation_packet_budget_preserves_journey_entry_and_tail() -> None:
    journey_steps = tuple(
        ObservationJourneyStep(
            page_capture_id=f"capture-{page_seq}",
            page_seq=page_seq,
            page_url=f"https://example.com/{page_seq}",
            page_title=None,
            page_type="startup_ad" if page_seq == 1 else "home",
            page_state="ready",
            action_log=(),
            artifacts=(),
        )
        for page_seq in range(1, 26)
    )
    repository = FakeObservationPacketRepository(replace(_source(), journey_steps=journey_steps))
    request = replace(
        _request(),
        judge_config={"engine_config": {"huge": "x" * 300_000}},
    )

    packet = await ObservationPacketService(repository=repository).build(request)
    journey = packet.packet_payload["facts"]["journey"]

    assert journey[0]["page_seq"] == 1
    assert journey[-1]["page_seq"] == 25


async def test_observation_packet_persists_failed_quality_gate() -> None:
    repository = FakeObservationPacketRepository(_source(visible_text=""))
    packet = await ObservationPacketService(repository=repository).build(_request())

    assert packet.quality_gate_status == "fail"
    assert "visible_text(non_empty)" in (packet.quality_gate_reason or "")
    assert repository.saved[0]["quality_gate_status"] == "fail"


async def test_observation_packet_keeps_action_trace_as_trusted_evidence() -> None:
    repository = FakeObservationPacketRepository(_source(include_action_trace=True))
    base_request = _request()
    request = replace(
        base_request,
        input_contract={
            **base_request.input_contract,
            "required_artifacts": [
                *base_request.input_contract["required_artifacts"],
                "action_trace",
            ],
        },
    )

    packet = await ObservationPacketService(repository=repository).build(request)

    assert packet.quality_gate_status == "pass"
    assert "action-1" in packet.evidence_artifact_ids
    assert any(
        item["artifact_type"] == "action_trace" for item in packet.packet_payload["evidence"]
    )


async def test_observation_packet_keeps_bounded_transient_message_style_facts() -> None:
    source = replace(
        _source(include_action_trace=True),
        action_log=(
            {
                "action": "click",
                "selector": "#save",
                "transient_message_observation": {
                    "schema_version": "transient-message-observation.v1",
                    "status": "observed",
                    "message_count": 1,
                },
                "transient_messages": [
                    {
                        "event_id": "transient-1",
                        "kind": "toast",
                        "text": "保存成功",
                        "selector": ".product-toast",
                        "position_zone": "top_center",
                        "style": {
                            "font_size": "16px",
                            "text_color": "rgb(255, 255, 255)",
                            "background_color": "rgba(0, 0, 0, 0.72)",
                            "background_opacity": 0.72,
                        },
                        "src": "https://example.com/secret",
                    }
                ],
            },
        ),
    )
    packet = await ObservationPacketService(
        repository=FakeObservationPacketRepository(source)
    ).build(_request())

    action = packet.packet_payload["facts"]["action_log"][0]
    message = action["transient_messages"][0]
    assert action["transient_message_observation"]["status"] == "observed"
    assert message["text"] == "保存成功"
    assert message["style"]["background_opacity"] == 0.72
    assert "src" not in message


async def test_observation_packet_rejects_transient_loading_page() -> None:
    repository = FakeObservationPacketRepository(_source(page_state="loading"))
    packet = await ObservationPacketService(repository=repository).build(_request())

    assert packet.quality_gate_status == "fail"
    assert "page_state(loading)" in packet.packet_payload["quality_gate"]["missing_sources"]


async def test_observation_packet_hard_bounds_oversized_rule_configuration() -> None:
    repository = FakeObservationPacketRepository(_source())
    request = replace(
        _request(),
        judge_config={"engine_config": {"huge": "x" * 300_000}},
    )

    packet = await ObservationPacketService(repository=repository).build(request)
    encoded = json.dumps(packet.packet_payload, ensure_ascii=False).encode("utf-8")

    assert len(encoded) <= MAX_OBSERVATION_PACKET_BYTES
    assert packet.quality_gate_status == "fail"
    assert packet.packet_payload["facts"]["packet_budget"]["hard_truncated"] is True
    assert packet.packet_payload["rule"]["criteria"]["budget_truncated"] is True


async def test_observation_packet_compresses_ui_sequence_without_false_insufficient() -> None:
    source = _source()
    sequence = {
        "schema_version": "ui-snapshot.v2",
        "sequence": True,
        "interval_ms": 500,
        "frames": [
            {
                "index": frame_index,
                "elements": [
                    {
                        "selector": f"#countdown-{item}-" + "x" * 280,
                        "tag": "div",
                        "text": f"{15 - frame_index}s 后关闭 " + "文" * 280,
                    }
                    for item in range(80)
                ],
            }
            for frame_index in range(15)
        ],
    }
    source = replace(
        source,
        artifacts=tuple(
            artifact for artifact in source.artifacts if artifact.artifact_type != "ui_snapshot"
        )
        + (
            _artifact(
                "ui-sequence-1",
                "ui_snapshot",
                json.dumps(sequence, ensure_ascii=False).encode(),
                "application/json",
            ),
        ),
    )
    repository = FakeObservationPacketRepository(source)

    packet = await ObservationPacketService(repository=repository).build(_request())
    encoded = json.dumps(packet.packet_payload, ensure_ascii=False).encode("utf-8")
    snapshot = packet.packet_payload["facts"]["ui_snapshot"]

    assert len(encoded) <= MAX_OBSERVATION_PACKET_BYTES
    assert packet.quality_gate_status == "pass"
    assert packet.packet_payload["facts"]["packet_budget"]["truncated"] is True
    assert len(snapshot["frames"]) <= 8
    assert all(len(frame["elements"]) <= 8 for frame in snapshot["frames"])


def test_semantic_views_remove_scripts_styles_and_inline_data() -> None:
    encoded = "YQ==" * 20_000
    dom_summary = _summarize_dom(
        '<button class="primary">打开</button>'
        '<script>prompt("不可进入语义视图")</script>'
        f'<img src="data:image/png;base64,{encoded}">'
    )
    snapshot = _compact_ui_snapshot(
        {
            "elements": [
                {
                    "selector": "img.hero",
                    "tag": "img",
                    "src": f"data:image/png;base64,{encoded}",
                }
            ]
        }
    )

    assert "不可进入语义视图" not in json.dumps(dom_summary, ensure_ascii=False)
    assert "base64," not in json.dumps(dom_summary, ensure_ascii=False)
    assert "base64," not in json.dumps(snapshot, ensure_ascii=False)
    assert ";bytes=" in snapshot["elements"][0]["src"]


def test_ui_snapshot_compaction_keeps_late_advertising_element() -> None:
    elements = [
        {"selector": f"#ordinary-{index}", "tag": "span", "text": str(index)}
        for index in range(100)
    ]
    elements.append(
        {
            "selector": "#popup-ad-banner",
            "tag": "img",
            "alt": "推广",
            "rect": {"width": 600, "height": 240},
        }
    )

    snapshot = _compact_ui_snapshot({"element_count": len(elements), "elements": elements})

    assert len(snapshot["elements"]) == 80
    assert any(item["selector"] == "#popup-ad-banner" for item in snapshot["elements"])


def test_ui_element_compaction_keeps_bounded_truncation_fields() -> None:
    element = _compact_ui_element(
        {
            "selector": "#card-title",
            "tag": "div",
            "text": "标题",
            "style": {
                "overflow": "hidden",
                "overflow_x": "hidden",
                "overflow_y": "clip",
                "text_overflow": "ellipsis",
                "white_space": "nowrap",
                "webkit_line_clamp": "2",
                "line_clamp": "2",
                "cursor": "pointer",
            },
            "text_metrics": {
                "scroll_width": 400,
                "client_width": 200,
                "offset_width": 200,
                "scroll_height": 40,
                "client_height": 40,
                "offset_height": 40,
                "horizontal_overflow": True,
                "vertical_overflow": False,
                "text_overflow_ellipsis": True,
                "line_clamp": "2",
                "junk_metric": "drop-me",
            },
        }
    )

    assert element["text_metrics"] == {
        "scroll_width": 400,
        "client_width": 200,
        "offset_width": 200,
        "scroll_height": 40,
        "client_height": 40,
        "offset_height": 40,
        "horizontal_overflow": True,
        "vertical_overflow": False,
        "text_overflow_ellipsis": True,
        "line_clamp": "2",
    }
    assert element["style"]["text_overflow"] == "ellipsis"
    assert element["style"]["white_space"] == "nowrap"
    assert element["style"]["webkit_line_clamp"] == "2"
    assert element["style"]["line_clamp"] == "2"
    assert element["style"]["overflow_x"] == "hidden"
    assert element["style"]["overflow_y"] == "clip"
    assert "cursor" not in element["style"]


def test_ui_element_compaction_drops_malformed_text_metrics() -> None:
    element = _compact_ui_element(
        {
            "selector": "#plain",
            "tag": "span",
            "text_metrics": "not-a-mapping",
        }
    )

    assert "text_metrics" not in element
