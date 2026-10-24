"""显式序列规则不能经旧 stable 候选及编译入口消费普通材料。"""

import pytest

from services.api.app.agents.runtime.business_middlewares.base import TaskRuntimeSnapshot
from services.api.app.application.inspection_candidate_publish import (
    prepare_enforced_batch_commands_from_candidate_result,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.tests.test_app_inspection_work_item_publish import _FakeRule
from services.api.tests.test_task_repository import FakePool
from services.api.tests.test_web_sequence_judge import _definition_and_request


def _rule(role):
    definition, _ = _definition_and_request()
    contract = dict(definition.input_contract)
    if role is None:
        contract.pop("web_evidence")
    else:
        contract["web_evidence"] = dict(
            contract["web_evidence"], accepted_roles=[role], accept_unclassified=False
        )
    return vars(
        _FakeRule(
            rule_id="rule-1",
            rule_code="web-test",
            input_contract=contract,
            judge_config={"tool_refs": ["semantic_rule_judge_tool"]},
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", ["failure_sequence", "interaction_sequence", "diagnostic", "stable_page", None]
)
async def test_original_candidate_repository_filters_only_explicit_non_stable_contracts(role):
    repository = PostgresTaskRepository(pool=FakePool(None, rows=[_rule(role)]))
    candidates = await repository.list_candidate_rules(task_id="task-1", page_type="home")
    assert len(candidates) == int(role in ("stable_page", None))


@pytest.mark.parametrize("role", ["failure_sequence", "interaction_sequence", "diagnostic"])
def test_stable_work_compiler_rejects_bypassed_non_stable_candidate(role):
    with pytest.raises(ValueError, match="stable_page"):
        prepare_enforced_batch_commands_from_candidate_result(
            snapshot=TaskRuntimeSnapshot(
                task_id="task-1",
                status="running",
                ruleset_version=1,
                ruleset_id="ruleset-1",
                target_platform="web",
            ),
            task_id="task-1",
            agent_run_id="run-1",
            page_capture_id="capture-1",
            rules=[_rule(role)],
            batches=[
                {
                    "batch_id": "batch-1",
                    "rule_codes": ["web-test"],
                    "strategy": "llm",
                    "category": "interaction",
                    "session_dependency": "snapshot",
                }
            ],
        )
