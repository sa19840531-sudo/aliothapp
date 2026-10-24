"""现有统一 Judge 到 V2 Rule Judge Agent 端口的迁移适配。"""

from __future__ import annotations

from services.api.app.agents.rule_judge.normalization import (
    normalize_rule_judge_outcome,
)
from services.api.app.agents.tools.rule_judge_tools import (
    judge_rule_for_current_run,
    judge_rules_for_current_run,
)
from services.api.app.domain.inspection import (
    InspectionHandoff,
    RuleJudgeOutcome,
)


class CurrentRuleJudgeAgentAdapter:
    """迁移期复用当前判定实现；本适配器禁止调用任何结果写回入口。"""

    async def judge(self, handoff: InspectionHandoff) -> RuleJudgeOutcome:
        page_capture_id = handoff.evidence.page_capture_id
        artifact_ids = list(handoff.evidence.artifact_ids)
        rules = [
            {
                "rule_code": rule.rule_code,
                "expected_contract_hash": rule.contract_hash,
            }
            for rule in handoff.rules
        ]
        if len(rules) == 1:
            raw = await judge_rule_for_current_run(
                rule_code=rules[0]["rule_code"],
                expected_contract_hash=rules[0]["expected_contract_hash"],
                page_capture_id=page_capture_id,
                evidence_artifact_ids=artifact_ids,
                judge_input={},
            )
        else:
            raw = await judge_rules_for_current_run(
                page_capture_id=page_capture_id,
                evidence_artifact_ids=artifact_ids,
                rules=rules,
            )
        return normalize_rule_judge_outcome(handoff=handoff, raw=raw)
