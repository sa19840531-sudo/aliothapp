"""RuleExecution 写回租约 fencing 回归。"""

from __future__ import annotations

from typing import Any

import pytest

from services.api.app.application.rule_execution import (
    RecordedRuleExecution,
    RecordRuleExecutionCommand,
    RuleExecutionDefinition,
    RuleExecutionLeaseConflictError,
    RuleExecutionService,
    RuleExecutionValidationError,
)
from services.api.app.infrastructure.db.task_repository import (
    _assert_inspection_write_lease,
)


class _LeaseConnection:
    def __init__(self, *, row: dict[str, Any] | None) -> None:
        self.row = row
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any):
        self.queries.append((" ".join(query.split()), args))
        return self.row


class _FakeRepo:
    async def get_rule_execution_definition(self, **kwargs):
        return RuleExecutionDefinition(
            executor_type="rule_engine",
            manual_review_policy={},
            severity="medium",
        )

    async def record_rule_execution(self, command):
        return RecordedRuleExecution(
            execution_id="execution-1",
            rule_id=command.rule_id or "rule-1",
            rule_code=command.rule_code or "alioth_1",
            execution_status=command.execution_status,
            page_capture_id=command.page_capture_id,
        )


@pytest.mark.asyncio
async def test_assert_inspection_write_lease_rejects_stale_token() -> None:
    connection = _LeaseConnection(row=None)

    with pytest.raises(RuleExecutionLeaseConflictError, match="租约"):
        await _assert_inspection_write_lease(
            connection,
            task_id="11111111-1111-1111-1111-111111111111",
            work_item_id="22222222-2222-2222-2222-222222222222",
            attempt_id="33333333-3333-3333-3333-333333333333",
            lease_token="44444444-4444-4444-4444-444444444444",
            agent_run_id="55555555-5555-5555-5555-555555555555",
        )

    assert any("FOR UPDATE" in query for query, _ in connection.queries)


@pytest.mark.asyncio
async def test_assert_inspection_write_lease_accepts_matching_token() -> None:
    connection = _LeaseConnection(
        row={"work_item_id": "22222222-2222-2222-2222-222222222222"}
    )

    await _assert_inspection_write_lease(
        connection,
        task_id="11111111-1111-1111-1111-111111111111",
        work_item_id="22222222-2222-2222-2222-222222222222",
        attempt_id="33333333-3333-3333-3333-333333333333",
        lease_token="44444444-4444-4444-4444-444444444444",
        agent_run_id=None,
    )

    query, _ = connection.queries[0]
    assert "attempt.status IN ('running', 'judged')" in query


@pytest.mark.asyncio
async def test_service_rejects_partial_fencing_before_repository() -> None:
    service = RuleExecutionService(repository=_FakeRepo())

    with pytest.raises(RuleExecutionValidationError, match="work_item_id"):
        await service.record_rule_execution(
            RecordRuleExecutionCommand(
                task_id="11111111-1111-1111-1111-111111111111",
                rule_id="22222222-2222-2222-2222-222222222222",
                execution_status="pass",
                page_capture_id="44444444-4444-4444-4444-444444444444",
                evidence_artifact_ids=["artifact-1"],
                work_item_id="work-1",
            )
        )
