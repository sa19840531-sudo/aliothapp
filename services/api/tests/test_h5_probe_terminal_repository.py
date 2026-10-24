"""H5 Probe 失败终态收口仓储测试。"""

from __future__ import annotations

from typing import Any

import pytest

from services.api.app.infrastructure.db.h5_probe_terminal_repository import (
    PostgresH5ProbeTerminalSettlementRepository,
)


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args


class _Connection:
    def __init__(self, *, settled: int = 0) -> None:
        self.settled = settled
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.queries.append(("execute", (" ".join(query.split()), *args)))
        return "SELECT 1"

    async def fetchval(self, query: str, *args: Any) -> int:
        self.queries.append(("fetchval", (" ".join(query.split()), *args)))
        if "WITH expired_request" in query:
            return 0
        return self.settled


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


async def test_settlement_is_scoped_to_h5_web_probe_and_current_run() -> None:
    connection = _Connection(settled=2)
    repository = PostgresH5ProbeTerminalSettlementRepository(pool=_Pool(connection))

    settled = await repository.settle_ready(
        task_id="task-1",
        agent_run_id="run-1",
    )

    assert settled == 2
    rendered = "\n".join(query for _, (query, *_) in connection.queries)
    assert "pg_advisory_xact_lock" in rendered
    assert "task.target_platform = 'h5'" in rendered
    assert "target_capture.agent_run_id = $2::uuid" in rendered
    assert "probe.work_payload->>'surface_kind' = 'web'" in rendered
    assert "probe.finalization_epoch = task.finalization_epoch" in rendered
    assert "probe.lease_expires_at <= now()" in rendered
    assert "probe.attempt_count >= probe.max_attempts" in rendered
    assert "request.status IN ('routed', 'running')" in rendered
    assert "request.session_dependency IN ('active_session', 'snapshot')" in rendered
    assert "request.expires_at <= now()" in rendered
    assert "FROM expired_request" in rendered
    assert "request.target_capture_id = target_capture.id" in rendered
    assert "NOT EXISTS" in rendered
    assert "supplement.request_id = request.request_id" in rendered
    assert "recoverable_probe.status IN" in rendered
    assert "'retry_wait', 'succeeded'" in rendered
    # 真实案例是未执行、未生成任何 Capture 的 planned 动作；活租约和未知执行不清。
    for guard in (
        "H5_ACTION_BEFORE_CAPTURE_ABORTED", "action.status = 'planned'",
        "action.started_at IS NULL", "action.source_capture_id IS NULL",
        "action.result_capture_id IS NULL", "session.lease_expires_at <= now()",
        "probe.deadline <= now()", "probe.work_id",
        "frame.action_execution_id = action.action_execution_id",
    ):
        assert guard in rendered


async def test_settlement_rejects_invalid_identity_before_database_access() -> None:
    connection = _Connection()
    repository = PostgresH5ProbeTerminalSettlementRepository(pool=_Pool(connection))

    with pytest.raises(ValueError, match="agent_run_id"):
        await repository.settle_ready(task_id="task-1", agent_run_id=" run-1")

    assert connection.queries == []


async def test_closed_session_settlement_is_scoped_to_current_h5_run() -> None:
    connection = _Connection(settled=3)
    repository = PostgresH5ProbeTerminalSettlementRepository(pool=_Pool(connection))

    settled = await repository.settle_requests_after_session_close(
        task_id="task-1",
        agent_run_id="run-1",
    )

    assert settled == 3
    rendered = "\n".join(query for _, (query, *_) in connection.queries)
    assert "pg_advisory_xact_lock" in rendered
    assert "task.target_platform = 'h5'" in rendered
    assert "target_capture.agent_run_id = $2::uuid" in rendered
    assert "request.session_dependency IN ('active_session', 'snapshot')" in rendered
    assert "session.status IN ('closed', 'lost')" in rendered
    assert "H5_RECAPTURE_SESSION_LOST" in rendered
    assert "session.finalization_epoch = task.finalization_epoch" in rendered
    assert "session.session_id = target_capture.surface_session_id" in rendered
    # Judge 可把 active-session 缺证请求升级为新会话 snapshot 补采；
    # 是否需要新会话以 EvidenceRequest 合同为准，不能再反向要求来源 Work 改型。
    assert "work_item.session_dependency = 'snapshot'" not in rendered
    assert "work_item.control_mode = 'enforced'" in rendered
    assert "probe.work_payload->>'surface_kind' = 'web'" in rendered
    assert "probe.evidence_request_id = request.request_id" in rendered
    assert "request.status IN ('pending', 'routed', 'running')" in rendered
    assert "supplement.request_id = request.request_id" in rendered
    assert "H5_RECAPTURE_SESSION_CLOSED" in rendered
