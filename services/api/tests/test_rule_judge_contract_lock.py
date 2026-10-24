"""Inspection Work Item 锁定规则契约的消费时校验。"""

import pytest

from services.api.app.application.rule_judge import (
    RuleJudgeContractDriftError,
    RuleJudgeDefinition,
    RuleJudgeRequest,
    RuleJudgeService,
)


class StaticRuleRepository:
    def __init__(self, definition: RuleJudgeDefinition) -> None:
        self.definition = definition

    async def get_rule_judge_definition(
        self,
        *,
        task_id: str,
        rule_code: str,
    ) -> RuleJudgeDefinition | None:
        return self.definition


async def test_rule_judge_rejects_contract_drift_before_evidence_access() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-1",
        rule_code="alioth_1",
        executor_type="llm",
        judge_config={"tool_refs": ["semantic_rule_judge_tool"]},
        category="visual",
        severity="medium",
        check_point="登录按钮可见",
        steps=[{"type": "observe"}],
    )
    service = RuleJudgeService(repository=StaticRuleRepository(definition))

    with pytest.raises(RuleJudgeContractDriftError, match="规则契约已漂移"):
        await service.judge(
            RuleJudgeRequest(
                task_id="task-1",
                rule_code="alioth_1",
                page_capture_id="capture-1",
                evidence_artifact_ids=("artifact-1",),
                judge_input={},
                expected_contract_hash="locked-contract-hash",
            )
        )
