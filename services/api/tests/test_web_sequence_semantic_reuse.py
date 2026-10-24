"""同一封存材料允许复用业务判定，位置与 occurrence 变化不得共用。"""

from copy import deepcopy
from dataclasses import replace

import pytest

from services.api.app.application.observation_packet import ObservationPacketService
from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import LLMSemanticRuleJudge
from services.api.tests.test_semantic_rule_judge import FakeStructuredClient
from services.api.tests.test_web_sequence_observation import (
    SequenceRepository,
    _ready_source,
    _sequence_request,
)


async def _packet(policy="same_material_context"):
    request = _sequence_request()
    contract = deepcopy(request.input_contract)
    contract["web_evidence"]["local_structure"] = {
        "schema_version": "web-local-structure-rule.v1",
        "scope": "current_document_light_dom",
        "predicate": "native_video_element_present",
        "reuse_policy": policy,
    }
    source = _ready_source(True)
    source.web_sequence["sequence"]["capability_revision"] = {
        "schema_version": "web-local-structure.v1",
        "scope": "current_document_light_dom",
        "predicate": "native_video_element_present",
        "status": "present",
        "revision_id": "revision-1",
        "revision": 1,
        "material_hash": "a" * 64,
        "position": {"scroll_x": 0, "scroll_y": 0},
        "account_state": "guest",
        "visible_layer": "page",
        "occurrence_id": "occurrence-1",
        "capture_id": "after-2",
    }
    return await ObservationPacketService(repository=SequenceRepository(source)).build(
        replace(request, input_contract=contract)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_actual_packet_and_semantic_judge_reuse_same_frozen_material_across_packet_storage_ids(
    batch,
):
    packet = await _packet()
    assert packet.packet_payload["web_evidence"]["status"] == "admissible"
    output = {
        "conclusion": "pass",
        "confidence": 0.95,
        "reasoning_summary": "按原封存材料判定",
        "evidence_refs": ["shot-1"],
    }
    client = FakeStructuredClient(
        {
            "schema_version": "semantic-rule-judge-batch.v1",
            "items": [{"rule_code": packet.rule_code, "result": output}],
        }
        if batch
        else output
    )
    judge = LLMSemanticRuleJudge(client=client)
    for packet_id in ("stored-packet-a", "stored-packet-b"):
        current = replace(packet, observation_packet_id=packet_id)
        result = (
            (await judge.judge_batch(items=[(current, [])]))[packet.rule_code]
            if batch
            else await judge.judge(packet=current, tool_results=[])
        )
        assert result.conclusion == "pass"
        assert result.evidence_refs == ["shot-1"]
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        "position",
        "occurrence_id",
        "revision_id",
        "material_hash",
        "account_state",
        "visible_layer",
        "rule_contract_hash",
    ],
)
async def test_material_context_change_really_reaches_model_again(changed):
    packet = await _packet()
    client = FakeStructuredClient(
        {
            "conclusion": "pass",
            "confidence": 0.95,
            "reasoning_summary": "分别判定",
            "evidence_refs": ["shot-1"],
        }
    )
    judge = LLMSemanticRuleJudge(client=client)
    await judge.judge(packet=packet, tool_results=[])
    payload = deepcopy(packet.packet_payload)
    block = payload["web_evidence"]
    if changed == "rule_contract_hash":
        block[changed] = "changed"
    else:
        block["capability_revision"][changed] = (
            {"scroll_x": 0, "scroll_y": 1} if changed == "position" else "changed"
        )
    await judge.judge(packet=replace(packet, packet_payload=payload), tool_results=[])
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_per_occurrence_keeps_original_packet_identity_and_frequency():
    packet = await _packet("per_occurrence")
    client = FakeStructuredClient(
        {
            "conclusion": "pass",
            "confidence": 0.95,
            "reasoning_summary": "各次复验",
            "evidence_refs": ["shot-1"],
        }
    )
    judge = LLMSemanticRuleJudge(client=client)
    await judge.judge(packet=packet, tool_results=[])
    await judge.judge(
        packet=replace(packet, observation_packet_id="new-storage-id"), tool_results=[]
    )
    assert len(client.calls) == 2
