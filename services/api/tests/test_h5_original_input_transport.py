"""普通 H5 发布必须保留原输入身份，不能误入 Web 序列合同。"""

from dataclasses import replace

import pytest

from services.api.app.agents.runtime.business_middlewares.base import TaskRuntimeSnapshot
from services.api.app.application.inspection_candidate_publish import (
    prepare_enforced_batch_commands_from_candidate_result,
)
from services.api.app.application.inspection_work_items import build_inspection_work_item_input_hash


def _commands(platform="h5", input_set="evidence-input-set:original"):
    rule = {
        "rule_id": "rule-original",
        "rule_code": "PRICE",
        "category": "text",
        "executor_type": "rule_engine",
        "check_point": "原价格",
        "input_contract": {},
    }
    if input_set is not None:
        rule["evidence_input_set_id"] = input_set
    return prepare_enforced_batch_commands_from_candidate_result(
        snapshot=TaskRuntimeSnapshot(
            task_id="task-original",
            status="running",
            ruleset_id="rules-original",
            ruleset_version=1,
            ruleset_snapshot_hash="snapshot-original",
            target_platform=platform,
        ),
        task_id="task-original",
        agent_run_id="run-original",
        page_capture_id="capture-original",
        rules=[rule],
        batches=[
            {
                "batch_id": "snapshot-original",
                "rule_codes": ["PRICE"],
                "strategy": "rule_engine",
                "session_dependency": "snapshot",
            }
        ],
    )


def test_h5_candidate_preserves_original_input_without_changing_rule_contract():
    bound = _commands()[0]
    legacy = _commands(input_set=None)[0]
    assert bound.rules[0].evidence_input_set_id == "evidence-input-set:original"
    assert bound.rules[0].contract_hash == legacy.rules[0].contract_hash
    assert build_inspection_work_item_input_hash(bound) != build_inspection_work_item_input_hash(
        legacy
    )
    assert build_inspection_work_item_input_hash(bound) == build_inspection_work_item_input_hash(
        replace(bound),
    )


@pytest.mark.parametrize("platform", ["app", "web"])
def test_generic_candidate_does_not_authorize_other_platform_input(platform):
    with pytest.raises(ValueError, match="H5"):
        _commands(platform=platform)


def test_missing_original_is_not_generated_from_capture_or_rule_name():
    assert _commands(input_set=None)[0].rules[0].evidence_input_set_id is None


@pytest.mark.parametrize("platform", ["h5", "web"])
async def test_original_page_input_reaches_judge_without_web_scope(platform):
    from services.api.app.agents.rule_judge.maf_agent import MafRuleJudgeAgent
    from services.api.app.application.inspection_handoff import build_inspection_handoff
    from services.api.tests.test_inspection_handoff import _lease
    from services.api.tests.test_maf_rule_judge_agent import FakeRuleJudgeService, _result

    lease = _lease()
    lease = replace(
        lease,
        target_platform=platform,
        rules=(
            replace(
                lease.rules[0],
                evidence_input_set_id="original-page-input",
            ),
        ),
    )
    handoff = build_inspection_handoff(lease, attempt_id="attempt-original")
    service = FakeRuleJudgeService(
        [
            replace(
                _result(),
                rule_id=lease.rules[0].rule_id,
                rule_code=lease.rules[0].rule_code,
            )
        ]
    )
    await MafRuleJudgeAgent(service=service).judge(handoff)
    request = service.requests[0]
    assert request.original_page_input_set_id == (
        "original-page-input" if platform == "h5" else None
    )
    assert request.evidence_input_set_id == (None if platform == "h5" else "original-page-input")
    assert request.lease_token == lease.lease_token
    assert "original_page_input_sets" not in handoff.to_dict()


@pytest.mark.parametrize("prepare_only", [False, True])
async def test_actual_capture_publisher_inherits_only_verified_original_inputs(
    monkeypatch, prepare_only
):
    from services.api.app.application.app_inspection_publisher import AppCaptureInspectionPublisher
    from services.api.tests.test_app_inspection_work_item_publish import (
        _FakeAgentRunReader,
        _FakeAnnotator,
        _FakeRule,
        _FakeSnapshotProvider,
        _FakeTaskQuery,
        _FakeWorkItemService,
        _NoActionSceneReader,
    )

    class BoundService(_FakeWorkItemService):
        async def read_original_page_inputs(self, **identity):
            assert identity["source_agent_run_id"] == "run-1"
            assert identity["rule_contracts"]["rule-1"]
            return {"rule-1": "verified-original"}

    service = BoundService()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([_FakeRule("rule-1", "PRICE")]),
        snapshot_provider=_FakeSnapshotProvider(target_platform="h5"),
        work_item_service=service,
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        action_scene_reader=_NoActionSceneReader(),
    )

    async def no_auth(**identity):
        return None

    monkeypatch.setattr(publisher, "_resolve_auth_gate", no_auth)
    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        agent_run_id="run-1",
        prepare_only=prepare_only,
    )
    commands = result.prepared_commands if prepare_only else service.commands
    assert commands[0].rules[0].evidence_input_set_id == "verified-original"
