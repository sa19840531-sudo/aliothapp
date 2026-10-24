"""确定性合同故障和未实现采样不应反复请求模型。"""

import pytest

from services.api.app.agents.runtime.inspection_work_item_consumer import InspectionWorkItemConsumer
from services.api.app.application.rule_judge import RuleJudgeValidationError
from services.api.tests.test_inspection_work_item_consumer import (
    FakeJudge,
    FakeResultWriter,
    FakeService,
)
from services.api.tests.test_rule_judge_evidence_cooperation import (
    PagePacketService,
    _hybrid,
    _judge,
)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("platform", ["h5", "web"])
async def test_pending_multi_client_capability_is_gap_not_pass_or_not_applicable(batch, platform):
    definition = _hybrid(
        rule_code="alioth_spec_new_25",
        batch_group="multi-client-pending",
        input_contract={
            "required_artifacts": ["screenshot"],
            "reuse_policy": {"scope": "per_task"},
            "recapture_policy": {"kind": "manual_only"},
        },
    )

    class PacketService(PagePacketService):
        async def build(self, request):
            packet = await super().build(request)
            packet.packet_payload["identity"] = {"target_platform": platform}
            return packet

    result, judge = await _judge(
        definition, batch=batch, packet_service=PacketService(page={"surface_kind": "web"})
    )
    if platform == "h5":
        assert result.execution_status == "evidence_insufficient"
        assert judge.call_count == 0
        assert result.judge_output["reason_code"] == "H5_SAMPLING_CAPABILITY_UNAVAILABLE"
        assert result.judge_output["evidence_gap"]["requested_request"] is None
    else:
        assert result.execution_status == "pass"
        assert judge.call_count == 1


@pytest.mark.parametrize("platform", ["h5", "web"])
@pytest.mark.parametrize("error", ["contract", "network"])
async def test_consumer_does_not_retry_same_h5_contract_error(platform, error):
    events = []
    service = FakeService(events, target_platform=platform)

    class BrokenJudge(FakeJudge):
        async def judge(self, handoff):
            if error == "contract":
                raise RuleJudgeValidationError("采集角色 primary 存在重复 ui_snapshot 证据")
            raise ConnectionError("临时读取失败")

    consumer = InspectionWorkItemConsumer(
        service=service,
        lease_owner="test",
        lease_seconds=180,
        retry_backoff_seconds=2,
        judge=BrokenJudge(events),
        result_writer=FakeResultWriter(events),
    )
    await consumer.drain_snapshot(task_id="task-1", consumer_agent_run_id="run-1")
    assert service.transitions[-1].target_status.value == (
        "waiting_evidence" if platform == "h5" and error == "contract" else "retry_wait"
    )
    assert service.attempt_transitions[-1].target_status.value == (
        "terminal_error" if platform == "h5" and error == "contract" else "retryable_error"
    )
