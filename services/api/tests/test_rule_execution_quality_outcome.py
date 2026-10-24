"""规则级 quality_outcome 五态映射与生成列迁移的同源契约。"""

from pathlib import Path

import pytest

from services.api.app.application.rule_execution import (
    RULE_QUALITY_OUTCOMES,
    VALID_RULE_EXECUTION_STATUSES,
    RecordRuleExecutionCommand,
    RuleExecutionValidationError,
    derive_quality_outcome,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository

_EXPECTED_MAPPING = {
    "pass": "passed",
    "fail": "failed",
    "warning": "failed",
    "manual_review": "inconclusive",
    "error": "inconclusive",
    "evidence_insufficient": "evidence_gap",
    "skipped": "skipped",
}

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260804_0037_rule_execution_quality_outcome.py"
)


def test_mapping_covers_every_execution_status_exactly() -> None:
    assert set(_EXPECTED_MAPPING) == VALID_RULE_EXECUTION_STATUSES
    for status, outcome in _EXPECTED_MAPPING.items():
        assert derive_quality_outcome(status) == outcome


def test_five_state_vocabulary_locked() -> None:
    assert RULE_QUALITY_OUTCOMES == {
        "passed",
        "failed",
        "skipped",
        "inconclusive",
        "evidence_gap",
    }
    assert set(_EXPECTED_MAPPING.values()) == RULE_QUALITY_OUTCOMES


def test_unknown_execution_status_rejected() -> None:
    with pytest.raises(RuleExecutionValidationError, match="非法规则执行状态"):
        derive_quality_outcome("unknown_status")


def test_generated_column_migration_shares_the_same_mapping() -> None:
    migration_source = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "GENERATED ALWAYS AS" in migration_source
    assert "STORED NOT NULL" in migration_source
    for status, outcome in _EXPECTED_MAPPING.items():
        assert f"WHEN '{status}' THEN '{outcome}'" in migration_source
    assert migration_source.count("WHEN '") == len(_EXPECTED_MAPPING)


class _FakeConnection:
    def __init__(self, row: dict[str, str | None]) -> None:
        self._row = row

    def transaction(self) -> "_FakeAcquire":
        """模拟 asyncpg 事务上下文。"""
        return _FakeAcquire(self)

    async def fetchrow(self, query: str, *args: object) -> dict[str, str | None]:
        return self._row


class _FakeAcquire:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self._connection

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakePool:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._connection)


async def test_repository_returns_quality_outcome_derived_from_persisted_status() -> None:
    row = {
        "execution_id": "exec-1",
        "rule_id": "rule-1",
        "rule_code": "alioth_1",
        "execution_status": "manual_review",
        "page_capture_id": "capture-1",
        "validation_error": None,
    }
    repository = PostgresTaskRepository(pool=_FakePool(_FakeConnection(row)))
    recorded = await repository.record_rule_execution(
        RecordRuleExecutionCommand(
            task_id="task-1",
            execution_status="manual_review",
            rule_code="alioth_1",
            page_capture_id="capture-1",
        )
    )
    assert recorded is not None
    assert recorded.execution_status == "manual_review"
    assert recorded.quality_outcome == "inconclusive"
