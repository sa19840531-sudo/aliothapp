"""App 补采排空 PostgreSQL 状态读取测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.infrastructure.db.app_supplemental_evidence_drain_repository import (
    PostgresAppSupplementalEvidenceDrainSettlementRepository,
    PostgresAppSupplementalEvidenceDrainStateReader,
)


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _Connection:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        del args


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)


class _Connection:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row
        self.query = ""
        self.args: tuple[Any, ...] = ()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.query = " ".join(query.split())
        self.args = args
        return self._row


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args


class _SettlementConnection:
    def __init__(
        self,
        *,
        candidate_rows: list[dict[str, Any]],
        fetchval_results: list[int],
        fetch_results: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self.candidate_rows = candidate_rows
        self.fetchval_results = list(fetchval_results)
        self.fetch_results = list(fetch_results or [candidate_rows])
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.queries.append(("execute", (" ".join(query.split()), *args)))
        return "SELECT 1"

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append(("fetch", (" ".join(query.split()), *args)))
        if self.fetch_results:
            return self.fetch_results.pop(0)
        return self.candidate_rows

    async def fetchval(self, query: str, *args: Any) -> int:
        self.queries.append(("fetchval", (" ".join(query.split()), *args)))
        return self.fetchval_results.pop(0)


class _SettlementPool:
    def __init__(self, connection: _SettlementConnection) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)  # type: ignore[arg-type]


async def test_reader_scopes_requests_probes_and_pending_completion_to_run_epoch() -> None:
    retry_at = datetime(2026, 9, 8, 21, tzinfo=UTC)
    connection = _Connection(
        {
            "nonterminal_request_count": 2,
            "nonterminal_probe_count": 1,
            "pending_completion_count": 1,
            "next_attempt_at": retry_at,
        }
    )
    reader = PostgresAppSupplementalEvidenceDrainStateReader(pool=_Pool(connection))

    state = await reader.load(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
    )

    assert state.nonterminal_request_count == 2
    assert state.nonterminal_probe_count == 1
    assert state.pending_completion_count == 1
    assert state.next_attempt_at == retry_at
    assert connection.args == ("task-1", "run-1", 3)
    assert "capture.agent_run_id = $2::uuid" in connection.query
    assert "request.finalization_epoch = $3" in connection.query
    assert "work.work_payload->>'surface_kind' IN ('android', 'ios')" in connection.query
    assert "count(DISTINCT probe.work_id)" in connection.query
    assert "supplement.supplement_id IS NULL" in connection.query
    assert "exploration_work_id" in connection.query


async def test_reader_rejects_invalid_execution_identity_before_database_access() -> None:
    connection = _Connection({})
    reader = PostgresAppSupplementalEvidenceDrainStateReader(pool=_Pool(connection))

    with pytest.raises(ValueError, match="agent_run_id"):
        await reader.load(
            task_id="task-1",
            agent_run_id=" run-1",
            finalization_epoch=3,
        )

    assert connection.query == ""


async def test_settlement_scopes_candidate_and_protects_in_flight_facts() -> None:
    connection = _SettlementConnection(
        candidate_rows=[{"request_id": "request-1"}],
        fetchval_results=[2, 1],
        fetch_results=[[{"request_id": "request-1"}], [], [{"request_id": "request-1"}]],
    )
    repository = PostgresAppSupplementalEvidenceDrainSettlementRepository(
        pool=_SettlementPool(connection)
    )

    result = await repository.settle_exhausted(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        reason_code="SUPPLEMENTAL_DRAIN_ACTION_BUDGET_EXHAUSTED",
    )

    assert result.settled_request_count == 1
    assert result.cancelled_probe_count == 2
    rendered = "\n".join(query for _, (query, *_) in connection.queries)
    assert "pg_advisory_xact_lock" in rendered
    assert "FOR UPDATE OF request" in rendered
    assert "request.finalization_epoch = $3" in rendered
    assert "capture.agent_run_id = $2::uuid" in rendered
    assert "probe.status IN ('leased', 'running', 'succeeded')" in rendered
    assert "action.intent_payload" in rendered
    assert "action.status = 'completed'" not in rendered
    assert "action.result_capture_id IS NOT NULL" not in rendered
    assert "status IN ('pending', 'retry_wait')" in rendered


async def test_settlement_is_idempotent_when_no_current_run_candidate_exists() -> None:
    connection = _SettlementConnection(candidate_rows=[], fetchval_results=[])
    repository = PostgresAppSupplementalEvidenceDrainSettlementRepository(
        pool=_SettlementPool(connection)
    )

    result = await repository.settle_exhausted(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
        reason_code="SUPPLEMENTAL_DRAIN_DEADLINE_EXHAUSTED",
    )

    assert result.settled_request_count == 0
    assert result.cancelled_probe_count == 0
    assert [kind for kind, _ in connection.queries] == ["execute", "fetch"]
