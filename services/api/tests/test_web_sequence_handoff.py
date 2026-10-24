"""逐规则序列绑定必须进入工作幂等身份与真实 Judge 交接。"""

from dataclasses import replace

import pytest

from services.api.app.application.inspection_handoff import build_inspection_handoff
from services.api.app.application.inspection_work_items import build_inspection_work_item_input_hash
from services.api.tests.test_inspection_handoff import _lease
from services.api.tests.test_inspection_work_items import _publish_command


def test_rule_sequence_binding_changes_work_hash_without_changing_legacy_hash():
    command = _publish_command()
    assert (
        build_inspection_work_item_input_hash(command)
        == "3592e6c29a3dfffbba93975b3cc36dc79a9ab64c7bcff240fdc7d25489378293"
    )
    first = replace(
        command,
        rules=(replace(command.rules[0], evidence_input_set_id="evidence-input-set:" + "a" * 64),),
    )
    second = replace(
        command,
        rules=(replace(command.rules[0], evidence_input_set_id="evidence-input-set:" + "b" * 64),),
    )
    assert (
        len({build_inspection_work_item_input_hash(item) for item in (command, first, second)}) == 3
    )


def test_two_rules_keep_their_own_input_set_in_handoff():
    lease = _lease()
    lease = replace(
        lease,
        target_platform="web",
        rules=tuple(
            replace(rule, evidence_input_set_id="evidence-input-set:" + rule.rule_code[-1] * 64)
            for rule in lease.rules
        ),
    )
    handoff = build_inspection_handoff(lease, attempt_id="attempt-1")
    assert handoff.lease_token == lease.lease_token
    assert "lease_token" not in handoff.to_dict()
    assert lease.lease_token not in repr(handoff)
    assert [rule.evidence_input_set_id for rule in handoff.rules] == [
        "evidence-input-set:" + "1" * 64,
        "evidence-input-set:" + "2" * 64,
    ]
    assert [item["evidence_input_set_id"] for item in handoff.to_dict()["rules"]] == [
        "evidence-input-set:" + "1" * 64,
        "evidence-input-set:" + "2" * 64,
    ]


def test_legacy_handoff_has_no_sequence_field():
    handoff = build_inspection_handoff(_lease(), attempt_id="attempt-1")
    assert handoff.lease_token is None
    assert all("evidence_input_set_id" not in item for item in handoff.to_dict()["rules"])


@pytest.mark.asyncio
async def test_maf_agent_propagates_each_rule_input_set_to_real_request():
    from dataclasses import replace

    from services.api.app.agents.rule_judge.maf_agent import MafRuleJudgeAgent
    from services.api.app.application.inspection_handoff import build_inspection_handoff
    from services.api.tests.test_inspection_handoff import _lease

    class StopAfterRequest(Exception):
        pass

    class Recorder:
        async def judge_many(self, requests):
            assert all(
                r.work_item_id == lease.work_item_id
                and r.attempt_id == "attempt-1"
                and r.lease_token == lease.lease_token
                for r in requests
            )
            assert [getattr(r, "evidence_input_set_id", None) for r in requests] == [
                "set-1",
                "set-2",
            ]
            raise StopAfterRequest

    lease = _lease()
    lease = replace(
        lease,
        rules=tuple(
            replace(r, evidence_input_set_id="set-" + r.rule_code[-1]) for r in lease.rules
        ),
    )
    with pytest.raises(StopAfterRequest):
        await MafRuleJudgeAgent(service=Recorder()).judge(
            build_inspection_handoff(lease, attempt_id="attempt-1")
        )


@pytest.mark.asyncio
async def test_publish_sql_persists_rule_binding_with_initial_member():
    from services.api.app.infrastructure.db.inspection_work_item_repository import (
        publish_inspection_work_item_on_connection,
    )
    from services.api.tests.test_inspection_work_item_repository import (
        FakeConnection,
        _publish_command,
    )

    command = _publish_command()
    command = replace(
        command, rules=tuple(replace(r, evidence_input_set_id="set-1") for r in command.rules)
    )
    connection = FakeConnection(
        fetchrow_result={"id": "work-1", "status": "pending", "created": True},
        fetchval_result="member-1",
    )
    await publish_inspection_work_item_on_connection(connection, command=command, input_hash="hash")
    inserts = [
        call for call in connection.calls if "INSERT INTO qc_inspection_work_item_rule" in call[1]
    ]
    assert len(inserts) == 1
    assert "evidence_input_set_id" in inserts[0][1]
    assert "set-1" in inserts[0][2]
