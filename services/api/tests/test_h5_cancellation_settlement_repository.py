"""H5 取消后的 Evidence Request 与 Explorer Work 收口测试。"""

from __future__ import annotations

from typing import Any

import pytest

from services.api.app.infrastructure.db.h5_cancellation_settlement_repository import (
    H5CancellationSettlementSummary,
    PostgresH5CancellationSettlementRepository,
)


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args


class _Connection:
    def __init__(self, *, row: dict[str, int] | None = None) -> None:
        self.row = row
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.queries.append(("execute", (" ".join(query.split()), *args)))
        return "SELECT 1"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, int] | None:
        self.queries.append(("fetchrow", (" ".join(query.split()), *args)))
        return self.row


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


async def test_settle_canceled_closes_current_h5_epoch_in_one_transaction() -> None:
    connection = _Connection(
        row={
            "request_count": 2,
            "explorer_work_count": 3,
            "inspection_work_count": 4,
            "inspection_attempt_count": 5,
            "inspection_rule_count": 6,
            "aggregate_work_count": 7,
            "aggregate_attempt_count": 8,
            "publication_count": 9,
        }
    )
    repository = PostgresH5CancellationSettlementRepository(pool=_Pool(connection))

    summary = await repository.settle_canceled(
        task_id="task-1",
        reason="H5_TASK_CANCELED",
    )

    assert summary == H5CancellationSettlementSummary(
        request_count=2,
        explorer_work_count=3,
        inspection_work_count=4,
        inspection_attempt_count=5,
        inspection_rule_count=6,
        aggregate_work_count=7,
        aggregate_attempt_count=8,
        publication_count=9,
    )
    assert summary.total_count == 44
    rendered = "\n".join(query for _, (query, *_) in connection.queries)
    assert "pg_advisory_xact_lock" in rendered
    assert "task-finalization:" in str(connection.queries[0][1][1])
    assert "task.target_platform = 'h5'" in rendered
    assert "task.status IN ('running', 'canceled')" in rendered
    assert "request.finalization_epoch = task.finalization_epoch" in rendered
    assert "request.status IN ('pending', 'routed', 'running')" in rendered
    assert "work_item.finalization_epoch = task.finalization_epoch" in rendered
    assert "work_item.work_type IN ('exploration', 'probe')" in rendered
    assert "work_item.work_payload->>'surface_kind' = 'web'" in rendered
    assert "work_item.status IN (" in rendered
    assert "'pending', 'leased', 'running', 'retry_wait'" in rendered
    assert "UPDATE qc_inspection_work_item AS work_item" in rendered
    assert "UPDATE qc_inspection_attempt AS attempt" in rendered
    assert "UPDATE qc_inspection_work_item_rule AS member" in rendered
    assert "UPDATE qc_aggregate_inspection_work_item AS work_item" in rendered
    assert "UPDATE qc_aggregate_inspection_attempt AS attempt" in rendered
    assert "UPDATE qc_h5_inspection_publication AS publication" in rendered
    assert "'waiting_evidence'" in rendered
    assert "status = 'abandoned'" in rendered
    assert "status = 'error'" in rendered
    assert "status = 'cancelled'" in rendered
    assert "lease_owner = NULL" in rendered
    assert "lease_token = NULL" in rendered
    assert "lease_expires_at = NULL" in rendered


async def test_settle_canceled_is_idempotent_when_nothing_remains() -> None:
    repository = PostgresH5CancellationSettlementRepository(
        pool=_Pool(
            _Connection(
                row={
                    "request_count": 0,
                    "explorer_work_count": 0,
                    "inspection_work_count": 0,
                    "inspection_attempt_count": 0,
                    "inspection_rule_count": 0,
                    "aggregate_work_count": 0,
                    "aggregate_attempt_count": 0,
                    "publication_count": 0,
                }
            )
        )
    )

    summary = await repository.settle_canceled(
        task_id="task-1",
        reason="H5_TASK_CANCELED",
    )

    assert summary.total_count == 0


async def test_settle_canceled_rejects_invalid_identity_before_database_access() -> None:
    connection = _Connection()
    repository = PostgresH5CancellationSettlementRepository(pool=_Pool(connection))

    with pytest.raises(ValueError, match="task_id"):
        await repository.settle_canceled(
            task_id=" task-1",
            reason="H5_TASK_CANCELED",
        )

    assert connection.queries == []
