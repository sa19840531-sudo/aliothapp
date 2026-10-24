"""App Probe 补采挂接恢复读取测试。"""

from __future__ import annotations

from typing import Any

import pytest

from services.api.app.infrastructure.db.app_probe_completion_repository import (
    PostgresAppProbeCompletionRecoveryReader,
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
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.query = ""
        self.args: tuple[Any, ...] = ()

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.query = " ".join(query.split())
        self.args = args
        return self._rows


async def test_reader_only_selects_succeeded_unattached_probe_with_action_result() -> None:
    connection = _Connection([])
    reader = PostgresAppProbeCompletionRecoveryReader(pool=_Pool(connection))

    result = await reader.find_next(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=3,
    )

    assert result is None
    assert connection.args == ("task-1", "run-1", 3)
    assert "work.status = 'succeeded'" in connection.query
    assert "work.work_payload->>'surface_kind' IN ('android', 'ios')" in connection.query
    assert "request.status IN ('routed', 'running')" in connection.query
    assert "action.status = 'completed'" in connection.query
    assert "action.result_capture_id IS NOT NULL" in connection.query
    assert "exploration_work_id" in connection.query
    assert "supplement.supplement_id IS NULL" in connection.query
    assert "LIMIT 1" in connection.query


async def test_reader_rejects_invalid_epoch_before_database_access() -> None:
    connection = _Connection([])
    reader = PostgresAppProbeCompletionRecoveryReader(pool=_Pool(connection))

    with pytest.raises(ValueError, match="finalization_epoch"):
        await reader.find_next(
            task_id="task-1",
            agent_run_id="run-1",
            finalization_epoch=-1,
        )

    assert connection.query == ""
