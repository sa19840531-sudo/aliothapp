"""App 探索结果 PostgreSQL 幂等写入测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from services.api.app.domain.evidence import (
    AppExplorationResult,
    AppExplorationResultOutcome,
)
from services.api.app.infrastructure.db.app_exploration_result_repository import (
    PostgresAppExplorationResultRepository,
)

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class RecordingConnection:
    def __init__(self, result: AppExplorationResult) -> None:
        self.result = result
        self.queries: list[str] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "INSERT INTO qc_app_exploration_result AS existing" in normalized:
            return {
                "run_id": self.result.run_id,
                "result_sha256": self.result.result_sha256,
                "created": True,
            }
        if "FROM qc_app_exploration_result" in normalized:
            payload = self.result.to_dict()
            return {
                "run_id": self.result.run_id,
                "task_id": self.result.task_id,
                "surface_session_id": self.result.surface_session_id,
                "evidence_index_id": self.result.evidence_index_id,
                "protocol_version": self.result.protocol_version,
                "outcome": self.result.outcome.value,
                "terminal_reason_code": self.result.terminal_reason_code,
                "evidence_index_hash": self.result.evidence_index_hash,
                "coverage_summary": payload["coverage_summary"],
                "evidence_gaps": payload["evidence_gaps"],
                "started_at": self.result.started_at,
                "completed_at": self.result.completed_at,
                "result_sha256": self.result.result_sha256,
            }
        raise AssertionError(f"未处理 SQL：{normalized}")


def _result() -> AppExplorationResult:
    return AppExplorationResult(
        task_id="11111111-1111-1111-1111-111111111111",
        run_id="22222222-2222-2222-2222-222222222222",
        surface_session_id="app:11111111-1111-1111-1111-111111111111",
        outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="exploration_completed",
        evidence_index_id="app-evidence-index:abc",
        evidence_index_hash="sha256:" + "a" * 64,
        coverage_summary={"covered_page_types": ["home"]},
        evidence_gaps=(),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=5),
    )


async def test_create_is_idempotent_by_run_and_result_hash() -> None:
    result = _result()
    connection = RecordingConnection(result)
    repository = PostgresAppExplorationResultRepository(pool=FakePool(connection))

    persisted = await repository.create(result=result)

    assert persisted.created is True
    sql = " ".join(connection.queries)
    assert "ON CONFLICT (agent_run_id)" in sql
    assert "existing.result_sha256 = EXCLUDED.result_sha256" in sql


async def test_get_revalidates_persisted_result_hash() -> None:
    result = _result()
    repository = PostgresAppExplorationResultRepository(
        pool=FakePool(RecordingConnection(result))
    )

    loaded = await repository.get_by_run_id(run_id=result.run_id)

    assert loaded == result
    assert loaded is not None
    assert loaded.result_sha256 == loaded.recompute_result_sha256()
