"""沙箱实现同步正式观察包参数，保留已校验的内存观察包身份。"""

from dataclasses import replace

import pytest

from services.api.app.application.observation_packet import ObservationPacket, ObservationSource
from services.api.app.application.rule_judge import RuleJudgeValidationError
from services.api.app.infrastructure.rule_sandbox_evidence_reader import (
    SandboxRuleJudgeEvidenceReader,
)


@pytest.mark.parametrize("mismatch", [None, "task_id", "page_capture_id", "rule_id"])
async def test_sandbox_reuses_own_formal_packet_and_rejects_foreign_identity(mismatch):
    reader = SandboxRuleJudgeEvidenceReader(
        ObservationSource(
            task_id="task-1",
            page_capture_id="capture-1",
            page_url="app://test",
            page_title=None,
            page_type="home",
            page_state="stable",
            action_log=(),
            artifacts=(),
        )
    )
    packet = ObservationPacket(
        observation_packet_id="sandbox-packet-1",
        task_id="task-1",
        page_capture_id="capture-1",
        rule_id="rule-1",
        rule_code="rule-1",
        evidence_artifact_ids=(),
        packet_payload={},
        quality_gate_status="pass",
        quality_gate_reason=None,
    )
    if mismatch:
        packet = replace(packet, **{mismatch: "foreign"})
    options = dict(
        task_id="task-1",
        agent_run_id=None,
        rule_id="rule-1",
        page_capture_id="capture-1",
        evidence_capture_roles=None,
        evidence_artifact_ids=(),
        required_artifact_types=(),
        frozen_observation_packet=packet,
    )
    if mismatch:
        with pytest.raises(RuleJudgeValidationError):
            await reader.prepare_observation_packet(**options)
    else:
        bundle = await reader.prepare_observation_packet(**options)
        assert bundle.observation_packet_id == packet.observation_packet_id
