"""E02 条款 1.4：清单成为齐全性权威后，判定侧退出自算证据齐不齐。"""

from __future__ import annotations

from typing import Any

import pytest

from services.api.app.application.observation_packet import (
    MANIFEST_QUALITY_GATE_SOURCE,
    ObservationPacket,
)
from services.api.app.application.rule_judge import (
    RuleJudgeNotConfiguredError,
    RuleJudgeRequest,
    RuleJudgeService,
)

TASK_ID = "11111111-1111-1111-1111-111111111111"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"
RULE_ID = "33333333-3333-3333-3333-333333333333"


def _packet(
    *,
    quality_status: str,
    manifest_backed: bool,
    reason: str | None = None,
) -> ObservationPacket:
    quality_gate: dict[str, Any] = {
        "status": quality_status,
        "reason": reason,
        "missing_sources": [],
    }
    if manifest_backed:
        quality_gate["source"] = MANIFEST_QUALITY_GATE_SOURCE
        quality_gate["manifest_id"] = "evidence-manifest:abc"
        quality_gate["manifest_version"] = 3
    return ObservationPacket(
        observation_packet_id="packet-1",
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        rule_id=RULE_ID,
        rule_code="alioth_1",
        evidence_artifact_ids=("artifact-1",),
        packet_payload={
            "rule": {"rule_code": "alioth_1"},
            "page": {"page_capture_id": CAPTURE_ID, "url": "https://example.com"},
            "facts": {"visible_text": "页面文案"},
            "quality_gate": quality_gate,
        },
        quality_gate_status=quality_status,
        quality_gate_reason=reason,
    )


class FakePacketService:
    def __init__(self, packet: ObservationPacket) -> None:
        self.packet = packet
        self.calls = 0

    async def build(self, request) -> ObservationPacket:
        del request
        self.calls += 1
        return self.packet


class FakeRepository:
    def __init__(self, definition) -> None:
        self.definition = definition

    async def get_rule_judge_definition(self, *, task_id: str, rule_code: str):
        del task_id, rule_code
        return self.definition


def _definition():
    from services.api.app.application.rule_judge import RuleJudgeDefinition

    return RuleJudgeDefinition(
        rule_id=RULE_ID,
        rule_code="alioth_1",
        rule_name="首页红线",
        category="content",
        executor_type="llm",
        check_point="首页不得出现红线内容",
        input_contract={
            "required_artifacts": ["visible_text"],
            "required_capture_roles": ["primary", "before"],
        },
        judge_config={"llm_checks": ["检查红线内容"]},
        manual_review_policy={},
    )


def _request() -> RuleJudgeRequest:
    return RuleJudgeRequest(
        task_id=TASK_ID,
        rule_code="alioth_1",
        page_capture_id=CAPTURE_ID,
        evidence_artifact_ids=("artifact-1",),
        judge_input={},
        # 刻意只给 primary：旧口径会因缺 before 角色直接判证据不足
        evidence_capture_roles={"primary": CAPTURE_ID},
    )


async def _judge(*, packet: ObservationPacket, authoritative: bool):
    definition = _definition()
    service = RuleJudgeService(
        repository=FakeRepository(definition),
        observation_packet_service=FakePacketService(packet),
        manifest_quality_authoritative=authoritative,
    )
    return await service.judge(_request())


async def test_legacy_mode_still_blocks_on_missing_capture_role() -> None:
    """off 模式行为不变：判定侧仍自算采集角色，缺角色即证据不足。"""
    result = await _judge(
        packet=_packet(quality_status="pass", manifest_backed=False),
        authoritative=False,
    )

    assert result.execution_status == "evidence_insufficient"
    assert "采集角色" in (result.problem_desc or "")


async def test_manifest_authority_skips_capture_role_precheck() -> None:
    """清单权威时角色齐全性已由需求集裁决，判定侧不再自算。

    缺 before 角色不再被拦下，流程一路走到语义判定；本用例未配置语义 Judge，
    因此以「未配置」异常收场——这正是预检已被跳过的证据。
    """
    with pytest.raises(RuleJudgeNotConfiguredError):
        await _judge(
            packet=_packet(quality_status="pass", manifest_backed=True),
            authoritative=True,
        )


async def test_failed_manifest_maps_to_evidence_insufficient() -> None:
    """E02 条款 2.1：唯有清单裁决 fail 才报证据不足。"""
    result = await _judge(
        packet=_packet(
            quality_status="fail",
            manifest_backed=True,
            reason="证据齐全性裁决未通过：缺少证据类型 screenshot",
        ),
        authoritative=True,
    )

    assert result.execution_status == "evidence_insufficient"
    assert result.judge_output["reason_code"] == "manifest_quality_failed"
    assert result.judge_output["evidence_trusted"] is False
    assert result.judge_input["manifest_read"]["business_outcome"] == "evidence_insufficient"
    assert result.judge_input["manifest_read"]["manifest_version"] == 3


async def test_pending_manifest_is_not_reported_as_evidence_insufficient() -> None:
    result = await _judge(
        packet=_packet(quality_status="pending", manifest_backed=True),
        authoritative=True,
    )

    assert result.execution_status == "error"
    assert result.execution_status != "evidence_insufficient"
    assert result.judge_output["reason_code"] == "manifest_pending"
    assert result.judge_input["manifest_read"]["business_outcome"] == "evidence_pending"


async def test_non_manifest_gate_keeps_legacy_evidence_insufficient() -> None:
    """off 模式非 pass 仍按原语义报证据不足，不引入新状态。"""
    result = await _judge(
        packet=_packet(
            quality_status="fail",
            manifest_backed=False,
            reason="必需证据缺失",
        ),
        authoritative=False,
    )

    assert result.execution_status == "evidence_insufficient"
    assert "reason_code" not in result.judge_output
