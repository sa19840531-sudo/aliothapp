"""真实 Observation Packet 两条齐全性路径的序列语义回归。"""

from dataclasses import dataclass, replace
from datetime import UTC

import pytest

from services.api.app.application.observation_packet import (
    BuildObservationPacketRequest,
    ManifestQualityProjection,
    ObservationPacketService,
    ObservationSource,
)
from services.api.tests.test_observation_packet import (
    FakeObservationPacketRepository,
    _request,
    _source,
)
from services.api.tests.test_web_rule_evidence_contract import _contract, _sequence


@dataclass(frozen=True)
class SequenceRequest(BuildObservationPacketRequest):
    evidence_input_set_id: str | None = "evidence-input-set:" + "a" * 64
    expected_contract_hash: str | None = "locked-hash"


@dataclass(frozen=True)
class SequenceSource(ObservationSource):
    web_sequence: dict | None = None


class SequenceRepository(FakeObservationPacketRepository):
    async def load_source_for_input_set(self, **kwargs):
        return self.source


def _ready_source(manifest):
    source = replace(_source(page_state="loading"), agent_run_id="run-1", surface_kind="web")
    source = replace(
        source,
        web_sequence={
            "input_set_id": "evidence-input-set:" + "a" * 64,
            "rule_id": _request().rule_id,
            "rule_contract_hash": "locked-hash",
            "sequence": _sequence(),
        },
    )
    source = replace(
        source,
        web_sequence_sources=tuple(
            replace(source, page_capture_id=f["capture_id"], web_sequence=None)
            for f in source.web_sequence["sequence"]["frames"]
        ),
    )
    return (
        replace(source, manifest_quality=ManifestQualityProjection("manifest-1", 1, "pass"))
        if manifest
        else source
    )


def _sequence_request():
    request = replace(
        _request(),
        executor_type="llm",
        judge_config={},
        source_agent_run_id="run-1",
        input_contract={
            "target_clients": ["WEB"],
            "web_evidence": _contract(),
            "required_artifacts": ["screenshot", "dom"],
        },
    )
    return replace(
        request,
        evidence_input_set_id="evidence-input-set:" + "a" * 64,
        expected_contract_hash="locked-hash",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_interaction_packet_preserves_original_action_and_independent_outcomes(missing):
    request = _sequence_request()
    contract = dict(request.input_contract)
    contract["web_evidence"] = dict(
        contract["web_evidence"], accepted_roles=["interaction_sequence"], accept_unclassified=False
    )
    request = replace(request, input_contract=contract)
    source = _ready_source(True)
    sequence = source.web_sequence["sequence"]
    sequence["purpose"] = "interaction_sequence"
    if not missing:
        sequence["interaction_outcomes"] = [
            {
                "outcome_ordinal": 0,
                "intent_id": "dismiss-intent",
                "action_execution_id": "action-1",
                "intent_hash": "a" * 64,
                "requested_source_capture_id": "source-1",
                "before_capture_id": "before-1",
                "action_result_capture_id": "after-1",
                "after_capture_id": "after-2",
                "command_accepted": True,
                "effect_observed": True,
                "postcondition_satisfied": False,
                "transition_arrived": False,
                "failure_reason": "original_body_not_restored",
                "evidence_mode": "natural",
            }
        ]
    packet = await ObservationPacketService(repository=SequenceRepository(source)).build(request)
    facts = packet.packet_payload["web_evidence"]
    assert facts["status"] == ("evidence_insufficient" if missing else "admissible")
    if not missing:
        assert facts["interaction_outcomes"][0]["postcondition_satisfied"] is False
        assert facts["interaction_outcomes"][0]["action_result_capture_id"] == "after-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", [False, True])
async def test_loading_sequence_semantics_are_separate_from_manifest_quality(manifest):
    packet = await ObservationPacketService(
        repository=SequenceRepository(_ready_source(manifest))
    ).build(_sequence_request())
    assert packet.quality_gate_status == "pass"
    assert packet.packet_payload["web_evidence"]["status"] == "admissible"
    assert packet.packet_payload["web_evidence"]["sample_capture_ids"] == ["after-1", "after-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", [False, True])
async def test_h5_loading_cannot_gain_sequence_authority_from_web_surface(manifest):
    source = _ready_source(manifest)
    source.web_sequence["sequence"]["target_platform"] = "h5"
    packet = await ObservationPacketService(repository=SequenceRepository(source)).build(
        _sequence_request()
    )
    # Manifest PASS 仍只表达齐全性，独立语义字段必须拒绝。
    assert packet.quality_gate_status == ("pass" if manifest else "fail")
    assert packet.packet_payload["web_evidence"]["status"] == "evidence_insufficient"


@pytest.mark.asyncio
async def test_sequence_source_authority_is_rechecked_on_repeated_packet_build():
    from services.api.app.application.observation_packet import ObservationPacketValidationError

    class Expiring(SequenceRepository):
        reads = 0

        async def load_source_for_input_set(self, **kwargs):
            self.reads += 1
            if self.reads > 1:
                raise ObservationPacketValidationError("Web 周期已漂移")
            return self.source

    repository = Expiring(_ready_source(True))
    service = ObservationPacketService(repository=repository)
    await service.build(_sequence_request())
    with pytest.raises(ObservationPacketValidationError, match="漂移"):
        await service.build(_sequence_request())


@pytest.mark.asyncio
async def test_same_anchor_different_rule_contract_never_reuses_first_packet_material():
    from copy import deepcopy

    request_a = _sequence_request()
    contract_b = deepcopy(request_a.input_contract)
    contract_b["web_evidence"]["minimum_duration_ms"] = 3000
    request_b = replace(
        request_a,
        rule_id="rule-2",
        rule_code="rule-2",
        evidence_input_set_id="evidence-input-set:" + "b" * 64,
        expected_contract_hash="hash-b",
        input_contract=contract_b,
    )
    source_a = _ready_source(True)
    source_b = replace(
        source_a,
        artifacts=tuple(replace(a, artifact_id=a.artifact_id + "-b") for a in source_a.artifacts),
        web_sequence=deepcopy(source_a.web_sequence),
    )
    source_b.web_sequence.update(
        input_set_id=request_b.evidence_input_set_id, rule_id="rule-2", rule_contract_hash="hash-b"
    )
    source_b = replace(
        source_b,
        web_sequence_sources=tuple(
            replace(
                frame,
                artifacts=tuple(
                    replace(a, artifact_id=a.artifact_id + "-b") for a in frame.artifacts
                ),
            )
            for frame in source_b.web_sequence_sources
        ),
    )

    class Separate(SequenceRepository):
        async def load_source_for_input_set(self, **kwargs):
            return (
                source_a if kwargs["input_set_id"] == request_a.evidence_input_set_id else source_b
            )

    service = ObservationPacketService(repository=Separate(source_a))
    packet_a = await service.build(request_a)
    packet_b = await service.build(request_b)
    assert packet_a.packet_payload["web_evidence"]["status"] == "admissible"
    assert (
        packet_b.packet_payload["web_evidence"]["reason_code"]
        == "WEB_SEQUENCE_DURATION_INSUFFICIENT"
    )
    assert set(packet_a.evidence_artifact_ids).isdisjoint(packet_b.evidence_artifact_ids)


@pytest.mark.asyncio
async def test_frame_text_alignment_survives_actual_llm_batch_messages():
    from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import (
        _build_batch_messages,
    )

    source = _ready_source(True)
    words = ("SOURCE_ONLY", "LOADING_1", "LOADING_1", "LOADING_2")
    sources = tuple(
        replace(
            frame,
            artifacts=tuple(
                replace(a, payload=word.encode()) if a.artifact_type == "visible_text" else a
                for a in frame.artifacts
            ),
        )
        for frame, word in zip(source.web_sequence_sources, words, strict=True)
    )
    source = replace(source, artifacts=sources[-1].artifacts, web_sequence_sources=sources)
    packet = await ObservationPacketService(repository=SequenceRepository(source)).build(
        _sequence_request()
    )
    assert packet.packet_payload["facts"]["visible_text"] == "LOADING_2"
    frames = packet.packet_payload["facts"]["web_sequence"]["frames"]
    assert [f["facts"]["visible_text"] for f in frames] == list(words)
    messages = _build_batch_messages(
        items=[(packet, []), (replace(packet, rule_code="different-rule"), [])]
    )
    texts = "\n".join(c.text or "" for m in messages for c in m.contents)
    for word in words:
        assert word in texts
    assert "逐条规则保留自己的序列事实" in texts


@pytest.mark.asyncio
async def test_sequence_frame_budget_rejects_instead_of_omitting_required_frames():
    from services.api.app.application.web_sequence_observation import project_web_sequence_semantics

    source = _ready_source(True)
    source = replace(
        source,
        web_sequence_sources=tuple(
            replace(
                frame,
                artifacts=tuple(
                    replace(a, payload=("很长但真实的有界材料" * 2000).encode())
                    if a.artifact_type == "visible_text"
                    else a
                    for a in frame.artifacts
                ),
            )
            for frame in source.web_sequence_sources
        ),
    )
    projection = project_web_sequence_semantics(request=_sequence_request(), source=source)
    assert projection["status"] == "evidence_insufficient"
    assert projection["reason_code"] == "WEB_SEQUENCE_FACT_BUDGET_EXCEEDED"
    assert projection["input_set_id"] == _sequence_request().evidence_input_set_id
    assert projection["rule_contract_hash"] == _sequence_request().expected_contract_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("same_image", [False, True])
async def test_all_required_sequence_screenshots_reach_actual_model_messages(same_image):
    from datetime import datetime, timedelta

    from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import (
        _build_batch_messages,
    )

    source = _ready_source(True)
    sequence = source.web_sequence["sequence"]
    prototype = sequence["frames"][-1]
    frames = []
    for index in range(8):
        before = index % 2 == 0
        frames.append(
            dict(
                prototype,
                capture_id=f"capture-{index}",
                capture_role="before_action" if before else "after_action",
                parent_capture_id=f"capture-{index - 1}" if index else "source-1",
                action_execution_id=f"action-{index // 2}",
                captured_at=(
                    datetime(2026, 9, 10, tzinfo=UTC) + timedelta(seconds=index)
                ).isoformat(),
                artifact_ids=[f"{a.artifact_id}-{index}" for a in source.artifacts],
            )
        )
    sequence["frames"] = frames
    members = tuple(
        replace(
            source,
            page_capture_id=frame["capture_id"],
            web_sequence=None,
            artifacts=tuple(
                replace(
                    a,
                    artifact_id=f"{a.artifact_id}-{index}",
                    payload=a.payload + (b"" if same_image else bytes([index]))
                    if a.artifact_type == "screenshot"
                    else a.payload,
                )
                for a in source.artifacts
            ),
        )
        for index, frame in enumerate(frames)
    )
    source = replace(source, artifacts=members[-1].artifacts, web_sequence_sources=members)
    packet = await ObservationPacketService(repository=SequenceRepository(source)).build(
        _sequence_request()
    )
    assert packet.packet_payload["web_evidence"]["status"] == "admissible"
    assert len(packet.image_payloads) == 8
    messages = _build_batch_messages(items=[(packet, [])])
    contents = [content for message in messages for content in message.contents]
    assert sum(content.type == "data" for content in contents) == (1 if same_image else 8)
    texts = "\n".join(content.text or "" for content in contents)
    for index in range(8):
        assert f"shot-1-{index}" in texts


def test_ordinary_capture_screenshot_limit_stays_six():
    from services.api.app.application.observation_packet import _decode_screenshot_payloads

    shot = next(a for a in _source().artifacts if a.artifact_type == "screenshot")
    assert (
        len(_decode_screenshot_payloads([replace(shot, artifact_id=f"shot-{i}") for i in range(8)]))
        == 6
    )
