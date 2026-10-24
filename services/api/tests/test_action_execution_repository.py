"""Action Execution 终态证据附加的持久化门禁测试。"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import (
    ActionExecutionStatus,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
)
from services.api.app.infrastructure.db.action_execution_repository import (
    ActionExecutionPersistenceError,
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.h5_probe_action_execution_repository import (
    PostgresH5ProbeActionExecutionRepository,
)


class _Acquire:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Pool:
    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _Connection:
    def __init__(self) -> None:
        self.query = ""
        self.args = ()

    async def fetchrow(self, query, *args):
        self.query = " ".join(query.split())
        self.args = args
        now = datetime.now(UTC)
        return {
            "action_execution_id": "action-1",
            "task_id": "11111111-1111-1111-1111-111111111111",
            "session_id": "session-1",
            "intent_id": "intent-1",
            "intent_hash": "intent-hash",
            "command_sequence": 3,
            "finalization_epoch": 2,
            "requested_source_capture_id": "22222222-2222-2222-2222-222222222222",
            "source_capture_id": "33333333-3333-3333-3333-333333333333",
            "result_capture_id": "44444444-4444-4444-4444-444444444444",
            "status": "failed",
            "result_payload": args[3],
            "error_code": "driver_failed",
            "error_message": "App Driver 执行失败",
            "row_version": 4,
            "created_at": now - timedelta(seconds=3),
            "started_at": now - timedelta(seconds=2),
            "action_finished_at": now - timedelta(seconds=1),
            "evidence_finished_at": now,
        }


class _CountConnection:
    def __init__(self, count: int) -> None:
        self.count = count
        self.query = ""
        self.args = ()
        self.transaction_count = 0

    def transaction(self) -> _Acquire:
        self.transaction_count += 1
        return _Acquire(self)

    async def fetchval(self, query, *args):
        self.query = " ".join(query.split())
        self.args = args
        return self.count


@pytest.mark.asyncio
async def test_fail_nonterminal_h5_actions_does_not_require_expired_session_lease() -> None:
    """Worker 已失联时旧租约必然过期，动作账本仍须按任务边界有界收束。"""

    connection = _CountConnection(3)
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    failed = await repository.fail_nonterminal_for_task(
        task_id="11111111-1111-1111-1111-111111111111",
        error_code="H5_WORKER_ORPHANED",
        error_message="H5 Worker 失联，动作结果未知",
        h5_only=True,
    )

    assert failed == 3
    assert "status = CASE" in connection.query
    assert "WHEN action.status = 'evidence_pending' THEN 'evidence_failed'" in connection.query
    assert "ELSE 'failed'" in connection.query
    assert "status IN ('planned', 'executing', 'evidence_pending')" in connection.query
    assert "session.surface_kind = 'web'" in connection.query
    assert "lease_token" not in connection.query
    assert connection.args == (
        "11111111-1111-1111-1111-111111111111",
        "H5_WORKER_ORPHANED",
        "H5 Worker 失联，动作结果未知",
        True,
    )


@pytest.mark.asyncio
async def test_fail_nonterminal_actions_requires_matching_task_finalization() -> None:
    """Gate 前清理必须绑定权威 Run、epoch 和 session_draining 状态。"""

    connection = _CountConnection(1)
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    failed = await repository.fail_nonterminal_for_finalization(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        finalization_epoch=3,
        error_code="TASK_FINALIZATION_ABORTED_ACTION",
        error_message="任务已进入收口阶段，遗留动作已失去执行者",
    )

    assert failed == 1
    assert connection.transaction_count == 1
    assert "task.finalization_state = 'session_draining'" in connection.query
    assert "task.finalization_agent_run_id = $2::uuid" in connection.query
    assert "task.finalization_epoch = $3" in connection.query
    assert "status IN ('planned', 'executing', 'evidence_pending')" in connection.query
    assert "error_code = COALESCE(action.error_code, $4)" in connection.query
    assert "error_message = COALESCE(action.error_message, $5)" in connection.query
    assert connection.args == (
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        3,
        "TASK_FINALIZATION_ABORTED_ACTION",
        "任务已进入收口阶段，遗留动作已失去执行者",
    )


def _action_row(
    *,
    status: ActionExecutionStatus,
    command_sequence: int = 3,
    finalization_epoch: int = 2,
    row_version: int = 4,
    source_capture_id: str | None = "33333333-3333-3333-3333-333333333333",
    result_capture_id: str | None = None,
    before_capture_retry_count: int | None = None,
) -> dict[str, object]:
    """构造能通过 ActionExecutionSnapshot 合同的数据库行。"""
    now = datetime.now(UTC)
    if status is ActionExecutionStatus.PLANNED:
        source_capture_id = None
    before_capture_failed = (
        status is ActionExecutionStatus.FAILED and source_capture_id is None
    )
    started_at = (
        None
        if status is ActionExecutionStatus.PLANNED or before_capture_failed
        else now - timedelta(seconds=3)
    )
    action_finished_at = (
        None
        if status in {ActionExecutionStatus.PLANNED, ActionExecutionStatus.EXECUTING}
        else now - timedelta(seconds=1)
    )
    evidence_finished_at = now if result_capture_id is not None else None
    if status is ActionExecutionStatus.EVIDENCE_FAILED:
        evidence_finished_at = now
    error_code = None
    error_message = None
    if before_capture_failed:
        error_code = "before_capture_failed"
        error_message = "动作前 Capture 未完成"
    elif status in {
        ActionExecutionStatus.EVIDENCE_FAILED,
        ActionExecutionStatus.BLOCKED,
        ActionExecutionStatus.FAILED,
        ActionExecutionStatus.TIMED_OUT,
    }:
        error_code = f"{status.value}_code"
        error_message = f"{status.value} message"
    if status is ActionExecutionStatus.PLANNED and before_capture_retry_count is not None:
        result_payload = {"before_capture_retry_count": before_capture_retry_count}
    elif status is ActionExecutionStatus.EVIDENCE_PENDING:
        result_payload = InteractionResult(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="session-1",
            intent_id="intent-1",
            action_execution_id="action-1",
            source_capture_id="33333333-3333-3333-3333-333333333333",
            status=InteractionResultStatus.EVIDENCE_PENDING,
        ).to_dict()
    elif before_capture_failed:
        result_payload = (
            {}
            if before_capture_retry_count is None
            else {"before_capture_retry_count": before_capture_retry_count}
        )
    elif status in {
        ActionExecutionStatus.BLOCKED,
        ActionExecutionStatus.FAILED,
        ActionExecutionStatus.TIMED_OUT,
    }:
        result_status = {
            ActionExecutionStatus.BLOCKED: InteractionResultStatus.BLOCKED,
            ActionExecutionStatus.FAILED: InteractionResultStatus.FAILED,
            ActionExecutionStatus.TIMED_OUT: InteractionResultStatus.TIMED_OUT,
        }[status]
        result_payload = InteractionResult(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="session-1",
            intent_id="intent-1",
            action_execution_id="action-1",
            source_capture_id="33333333-3333-3333-3333-333333333333",
            status=result_status,
            result_capture_id=result_capture_id,
            error_code=error_code,
            error_message=error_message,
        ).to_dict()
    elif status is ActionExecutionStatus.COMPLETED:
        result_payload = InteractionResult(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="session-1",
            intent_id="intent-1",
            action_execution_id="action-1",
            source_capture_id="33333333-3333-3333-3333-333333333333",
            status=InteractionResultStatus.SUCCEEDED,
            result_capture_id=result_capture_id,
        ).to_dict()
    else:
        result_payload = {}
    return {
        "action_execution_id": "action-1",
        "task_id": "11111111-1111-1111-1111-111111111111",
        "session_id": "session-1",
        "intent_id": "intent-1",
        "intent_hash": "intent-hash",
        "command_sequence": command_sequence,
        "finalization_epoch": finalization_epoch,
        "requested_source_capture_id": "22222222-2222-2222-2222-222222222222",
        "source_capture_id": source_capture_id,
        "result_capture_id": result_capture_id,
        "status": status.value,
        "result_payload": result_payload,
        "error_code": error_code,
        "error_message": error_message,
        "row_version": row_version,
        "created_at": now - timedelta(seconds=5),
        "started_at": started_at,
        "action_finished_at": action_finished_at,
        "evidence_finished_at": evidence_finished_at,
    }


class _RecoveryConnection:
    """按 SQL 类型返回恢复分支行，验证查询参数与锁条件。"""

    def __init__(
        self,
        *,
        initial_row: dict[str, object],
        update_rows: tuple[dict[str, object], ...] = (),
        after_capture_id: str | None = None,
        cursor_result: str | None = "session-1",
    ) -> None:
        self.initial_row = initial_row
        self.update_rows = list(update_rows)
        self.after_capture_id = after_capture_id
        self.cursor_result = cursor_result
        self.calls: list[tuple[str, str, tuple[object, ...]]] = []

    def transaction(self) -> "_RecoveryConnection":
        return self

    async def __aenter__(self) -> "_RecoveryConnection":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def fetchrow(self, query: str, *args: object):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "SELECT action.*" in normalized:
            return self.initial_row
        if "UPDATE qc_action_execution AS action" in normalized:
            return self.update_rows.pop(0) if self.update_rows else None
        raise AssertionError(f"未处理的 Action SQL：{normalized}")

    async def fetchval(self, query: str, *args: object):
        normalized = " ".join(query.split())
        self.calls.append(("fetchval", normalized, args))
        if "SELECT capture.id::text" in normalized:
            return self.after_capture_id
        if "UPDATE qc_surface_session AS session" in normalized:
            return self.cursor_result
        raise AssertionError(f"未处理的 Action SQL：{normalized}")


def _recovery_permit(
    *,
    command_sequence: int = 3,
    finalization_epoch: int = 2,
    surface_kind: SurfaceKind = SurfaceKind.ANDROID,
) -> SessionCommandPermit:
    return SessionCommandPermit(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="session-1",
        surface_kind=surface_kind,
        lease_owner="worker-1",
        lease_token="55555555-5555-5555-5555-555555555555",
        command_sequence=command_sequence,
        finalization_epoch=finalization_epoch,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


async def test_h5_probe_mark_executing_requires_probe_role_and_lineage() -> None:
    repository = PostgresH5ProbeActionExecutionRepository()
    expected = object()
    repository._transition = AsyncMock(return_value=expected)
    permit = _recovery_permit(surface_kind=SurfaceKind.WEB)

    result = await repository.mark_executing(
        action_execution_id="action-1",
        permit=permit,
        source_capture_id="33333333-3333-3333-3333-333333333333",
        expected_row_version=4,
    )

    assert result is expected
    transition = repository._transition.await_args.kwargs
    query = " ".join(transition["query"].split())
    assert "capture.capture_role = 'probe_before'" in query
    assert "supplemental_evidence_source" in query
    assert "targeted_recapture" in query
    assert "evidence_request_id" in query
    assert "action.session_id LIKE 'h5:%'" in query
    assert transition["args"] == (
        "action-1",
        4,
        "33333333-3333-3333-3333-333333333333",
        permit.lease_token,
        permit.lease_owner,
    )


@pytest.mark.asyncio
async def test_h5_probe_complete_evidence_requires_probe_result_role_and_lineage() -> None:
    repository = PostgresH5ProbeActionExecutionRepository()
    expected = object()
    repository._transition = AsyncMock(return_value=expected)
    permit = _recovery_permit(surface_kind=SurfaceKind.WEB)
    interaction = InteractionResult(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="h5:run-1",
        intent_id="intent-1",
        action_execution_id="action-1",
        source_capture_id="33333333-3333-3333-3333-333333333333",
        result_capture_id="44444444-4444-4444-4444-444444444444",
        status=InteractionResultStatus.SUCCEEDED,
    )

    result = await repository.complete_evidence(
        interaction=interaction,
        permit=permit,
        expected_row_version=4,
    )

    assert result is expected
    transition = repository._transition.await_args.kwargs
    query = " ".join(transition["query"].split())
    assert "capture.capture_role = 'probe_result_replay'" in query
    assert "capture_profile,after,role" in query
    assert "supplemental_evidence_source" in query
    assert "targeted_recapture" in query
    assert "evidence_request_id" in query
    assert "action.session_id LIKE 'h5:%'" in query
    assert "capture.capture_role = 'after_action'" not in query


@pytest.mark.asyncio
async def test_h5_probe_terminal_evidence_requires_probe_result_role_and_lineage() -> None:
    repository = PostgresH5ProbeActionExecutionRepository()
    expected = object()
    repository._transition = AsyncMock(return_value=expected)
    permit = _recovery_permit(surface_kind=SurfaceKind.WEB)
    interaction = InteractionResult(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="h5:run-1",
        intent_id="intent-1",
        action_execution_id="action-1",
        source_capture_id="33333333-3333-3333-3333-333333333333",
        result_capture_id="44444444-4444-4444-4444-444444444444",
        status=InteractionResultStatus.BLOCKED,
        error_code="TARGET_BLOCKED",
        error_message="目标被阻挡",
    )

    result = await repository.attach_terminal_evidence(
        interaction=interaction,
        permit=permit,
        expected_row_version=4,
    )

    assert result is expected
    transition = repository._transition.await_args.kwargs
    query = " ".join(transition["query"].split())
    assert "capture.capture_role = 'probe_result_replay'" in query
    assert "capture_profile,after,role" in query
    assert "supplemental_evidence_source" in query
    assert "targeted_recapture" in query
    assert "evidence_request_id" in query
    assert "action.session_id LIKE 'h5:%'" in query
    assert "capture.capture_role = 'after_action'" not in query


async def test_attach_terminal_evidence_updates_capture_without_changing_failed_status() -> None:
    connection = _Connection()
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )
    permit = SessionCommandPermit(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="session-1",
        surface_kind=SurfaceKind.ANDROID,
        lease_owner="worker-1",
        lease_token="55555555-5555-5555-5555-555555555555",
        command_sequence=3,
        finalization_epoch=2,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    interaction = InteractionResult(
        task_id=permit.task_id,
        session_id=permit.session_id,
        intent_id="intent-1",
        action_execution_id="action-1",
        source_capture_id="33333333-3333-3333-3333-333333333333",
        result_capture_id="44444444-4444-4444-4444-444444444444",
        status=InteractionResultStatus.FAILED,
        error_code="driver_failed",
        error_message="App Driver 执行失败",
    )

    saved = await repository.attach_terminal_evidence(
        interaction=interaction,
        permit=permit,
        expected_row_version=3,
    )

    assert saved.status is ActionExecutionStatus.FAILED
    assert saved.result_capture_id == interaction.result_capture_id


async def test_h5_browser_loss_unknown_action_terminal_is_strict_and_idempotent() -> None:
    class Connection(_Connection):
        async def fetchrow(self, query, *args):
            row = await super().fetchrow(query, *args)
            row.update(
                task_id=args[0],
                session_id=args[2],
                action_execution_id=args[3],
                source_capture_id=args[4],
                requested_source_capture_id=args[4],
                status="failed",
                result_payload=args[6],
                error_code="h5_browser_loss_action_unknown",
            )
            return row

    connection = Connection()
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    saved = await repository.fail_h5_browser_loss_unknown(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="66666666-6666-6666-6666-666666666666",
        session_id="h5:66666666-6666-6666-6666-666666666666",
        action_execution_id="action-1",
        source_capture_id="33333333-3333-3333-3333-333333333333",
        failure_stage="action",
    )

    assert saved.status is ActionExecutionStatus.FAILED
    assert "action.status = 'executing'" in connection.query
    assert "source_session.status = 'lost'" in connection.query
    assert "source.agent_run_id = $2::uuid" in connection.query
    assert "action.result_payload @> $7::jsonb" in connection.query
    assert "action.status = 'completed'" not in connection.query
    assert connection.args[4] == "33333333-3333-3333-3333-333333333333"


async def test_after_capture_browser_loss_terminalizes_evidence_pending_idempotently() -> None:
    class Connection(_Connection):
        def __init__(self) -> None:
            super().__init__()
            self.queries: list[str] = []

        async def fetchrow(self, query, *args):
            row = await super().fetchrow(query, *args)
            self.queries.append(self.query)
            writes_evidence_finished_at = (
                "evidence_finished_at = CASE WHEN $6 = 'after_capture' THEN now() "
                "ELSE action.evidence_finished_at END" in self.query
            )
            row.update(
                task_id=args[0],
                session_id=args[2],
                action_execution_id=args[3],
                source_capture_id=args[4],
                requested_source_capture_id=args[4],
                status="evidence_failed",
                result_payload=args[6],
                error_code="h5_browser_loss_after_capture_unknown",
                evidence_finished_at=(
                    datetime.now(UTC) if writes_evidence_finished_at else None
                ),
            )
            return row

    connection = Connection()
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )
    kwargs = {
        "task_id": "11111111-1111-1111-1111-111111111111",
        "agent_run_id": "66666666-6666-6666-6666-666666666666",
        "session_id": "h5:66666666-6666-6666-6666-666666666666",
        "action_execution_id": "action-1",
        "source_capture_id": "33333333-3333-3333-3333-333333333333",
        "failure_stage": "after_capture",
    }

    first = await repository.fail_h5_browser_loss_unknown(**kwargs)
    replay = await repository.fail_h5_browser_loss_unknown(**kwargs)

    assert first.status is ActionExecutionStatus.EVIDENCE_FAILED
    assert replay.status is ActionExecutionStatus.EVIDENCE_FAILED
    assert all("action.status = 'evidence_pending'" in query for query in connection.queries)
    assert all(
        "'h5_browser_loss_after_capture_unknown'" in query
        for query in connection.queries
    )
    assert all("source_session.status = 'lost'" in query for query in connection.queries)
    assert all(
        "evidence_finished_at = CASE WHEN $6 = 'after_capture' THEN now() "
        "ELSE action.evidence_finished_at END" in query
        for query in connection.queries
    )


async def test_action_browser_loss_does_not_fabricate_evidence_completion() -> None:
    class Connection(_Connection):
        async def fetchrow(self, query, *args):
            row = await super().fetchrow(query, *args)
            row.update(
                task_id=args[0],
                session_id=args[2],
                action_execution_id=args[3],
                source_capture_id=args[4],
                requested_source_capture_id=args[4],
                status="failed",
                result_payload=args[6],
                error_code="h5_browser_loss_action_unknown",
                evidence_finished_at=None,
            )
            return row

    connection = Connection()
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    await repository.fail_h5_browser_loss_unknown(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="66666666-6666-6666-6666-666666666666",
        session_id="h5:66666666-6666-6666-6666-666666666666",
        action_execution_id="action-1",
        source_capture_id="33333333-3333-3333-3333-333333333333",
        failure_stage="action",
    )

    assert (
        "evidence_finished_at = CASE WHEN $6 = 'after_capture' THEN now() "
        "ELSE action.evidence_finished_at END" in connection.query
    )
    assert "status = 'completed'" not in connection.query
    assert "$6 = 'action' AND action.status = 'executing'" in connection.query
    assert connection.args[5] == "action"


async def test_recover_planned_rebinds_command_with_same_finalization_epoch() -> None:
    initial = _action_row(
        status=ActionExecutionStatus.PLANNED,
        command_sequence=2,
        finalization_epoch=5,
        row_version=7,
    )
    rebound = _action_row(
        status=ActionExecutionStatus.PLANNED,
        command_sequence=3,
        finalization_epoch=5,
        row_version=8,
    )
    connection = _RecoveryConnection(initial_row=initial, update_rows=(rebound,))
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    restored = await repository.recover(
        action_execution_id="action-1",
        permit=_recovery_permit(command_sequence=3, finalization_epoch=5),
        expected_row_version=7,
    )

    assert restored.status is ActionExecutionStatus.PLANNED
    assert restored.command_sequence == 3
    assert restored.finalization_epoch == 5
    select = next(call for call in connection.calls if "SELECT action.*" in call[1])
    assert "session.finalization_epoch = $8" in select[1]
    assert select[2][7] == 5
    update = next(call for call in connection.calls if "UPDATE qc_action_execution" in call[1])
    assert "SET command_sequence = $2" in update[1]
    assert "action.finalization_epoch = $3" in update[1]
    assert "session.command_sequence = $2" in update[1]
    assert "session.finalization_epoch = $3" in update[1]
    assert update[2][1:4] == (3, 5, 7)


async def test_recover_failed_before_capture_allows_one_safe_retry() -> None:
    initial = _action_row(
        status=ActionExecutionStatus.FAILED,
        command_sequence=2,
        finalization_epoch=5,
        row_version=7,
        source_capture_id=None,
    )
    retried = _action_row(
        status=ActionExecutionStatus.PLANNED,
        command_sequence=3,
        finalization_epoch=5,
        row_version=8,
        before_capture_retry_count=1,
    )
    connection = _RecoveryConnection(initial_row=initial, update_rows=(retried,))
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    restored = await repository.recover(
        action_execution_id="action-1",
        permit=_recovery_permit(command_sequence=3, finalization_epoch=5),
        expected_row_version=7,
    )

    assert restored.status is ActionExecutionStatus.PLANNED
    assert restored.command_sequence == 3
    assert restored.result_payload["before_capture_retry_count"] == 1
    update = next(call for call in connection.calls if "UPDATE qc_action_execution" in call[1])
    assert "action.source_capture_id IS NULL" in update[1]
    assert "action.started_at IS NULL" in update[1]
    assert "action.result_capture_id IS NULL" in update[1]
    assert "action.evidence_finished_at IS NULL" in update[1]
    assert "action.error_code = 'before_capture_failed'" in update[1]
    assert "< 1" in update[1]
    assert "error_code = NULL" in update[1]
    assert "action_finished_at = NULL" in update[1]
    assert "result_capture_id = NULL" in update[1]
    assert "evidence_finished_at = NULL" in update[1]


async def test_recover_failed_before_capture_does_not_retry_twice_or_lookup_after() -> None:
    initial = _action_row(
        status=ActionExecutionStatus.FAILED,
        row_version=8,
        source_capture_id=None,
        before_capture_retry_count=1,
    )
    connection = _RecoveryConnection(initial_row=initial)
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    restored = await repository.recover(
        action_execution_id="action-1",
        permit=_recovery_permit(),
        expected_row_version=8,
    )

    assert restored.status is ActionExecutionStatus.FAILED
    assert restored.error_code == "before_capture_failed"
    assert not any("UPDATE qc_action_execution" in call[1] for call in connection.calls)
    assert not any("SELECT capture.id::text" in call[1] for call in connection.calls)


async def test_recover_rejects_action_from_old_finalization_epoch() -> None:
    connection = _RecoveryConnection(
        initial_row=_action_row(
            status=ActionExecutionStatus.PLANNED,
            command_sequence=2,
            finalization_epoch=4,
            row_version=7,
        )
    )
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    with pytest.raises(ActionExecutionPersistenceError, match="Finalization Epoch 已过期"):
        await repository.recover(
            action_execution_id="action-1",
            permit=_recovery_permit(command_sequence=3, finalization_epoch=5),
            expected_row_version=7,
        )

    select = next(call for call in connection.calls if "SELECT action.*" in call[1])
    assert "session.finalization_epoch = $8" in select[1]
    assert select[2][7] == 5
    assert not any("UPDATE qc_action_execution" in call[1] for call in connection.calls)


async def test_recover_executing_marks_outcome_unknown_without_retrying_driver() -> None:
    blocked = _action_row(
        status=ActionExecutionStatus.FAILED,
        row_version=5,
        source_capture_id="33333333-3333-3333-3333-333333333333",
    )
    blocked["error_code"] = "APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH"
    blocked["error_message"] = "进程中断时动作可能已经送达设备，禁止重复执行"
    connection = _RecoveryConnection(
        initial_row=_action_row(status=ActionExecutionStatus.EXECUTING, row_version=4),
        update_rows=(blocked,),
    )
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    restored = await repository.recover(
        action_execution_id="action-1",
        permit=_recovery_permit(),
        expected_row_version=4,
    )

    assert restored.status is ActionExecutionStatus.FAILED
    assert restored.error_code == "APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH"
    update = next(call for call in connection.calls if "UPDATE qc_action_execution" in call[1])
    assert "action.status = 'executing'" in update[1]
    assert "APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH" in update[1]
    assert "session.command_sequence = $6" in update[1]
    assert "session.finalization_epoch = $7" in update[1]


async def test_recover_evidence_pending_with_valid_after_completes_and_advances_cursor() -> None:
    after_capture_id = "88888888-8888-8888-8888-888888888888"
    completed = _action_row(
        status=ActionExecutionStatus.COMPLETED,
        result_capture_id=after_capture_id,
        row_version=5,
    )
    connection = _RecoveryConnection(
        initial_row=_action_row(status=ActionExecutionStatus.EVIDENCE_PENDING, row_version=4),
        update_rows=(completed,),
        after_capture_id=after_capture_id,
    )
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    restored = await repository.recover(
        action_execution_id="action-1",
        permit=_recovery_permit(),
        expected_row_version=4,
    )

    assert restored.status is ActionExecutionStatus.COMPLETED
    assert restored.result_capture_id == after_capture_id
    after_lookup = next(call for call in connection.calls if "SELECT capture.id::text" in call[1])
    assert "capture.capture_role = 'after_action'" in after_lookup[1]
    assert "capture.capture_status = 'captured'" in after_lookup[1]
    assert "capture.parent_capture_id = $6::uuid" in after_lookup[1]
    assert after_lookup[2][0:4] == (
        "action-1",
        "11111111-1111-1111-1111-111111111111",
        "session-1",
        3,
    )
    cursor = next(
        call for call in connection.calls if "UPDATE qc_surface_session AS session" in call[1]
    )
    assert "session.lease_owner = $4" in cursor[1]
    assert "session.lease_token = $5::uuid" in cursor[1]
    assert "session.lease_expires_at > now()" in cursor[1]
    assert "session.command_sequence = $6" in cursor[1]
    assert "session.finalization_epoch = $7" in cursor[1]
    assert cursor[2][-2:] == ("action-1", after_capture_id)


async def test_recover_evidence_pending_without_after_marks_evidence_failed() -> None:
    failed = _action_row(
        status=ActionExecutionStatus.EVIDENCE_FAILED,
        row_version=5,
        source_capture_id="33333333-3333-3333-3333-333333333333",
    )
    failed["error_code"] = "APP_AFTER_CAPTURE_MISSING_AFTER_CRASH"
    failed["error_message"] = "动作已经完成，但进程中断前未提交 After Capture"
    connection = _RecoveryConnection(
        initial_row=_action_row(status=ActionExecutionStatus.EVIDENCE_PENDING, row_version=4),
        update_rows=(failed,),
        after_capture_id=None,
    )
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    restored = await repository.recover(
        action_execution_id="action-1",
        permit=_recovery_permit(),
        expected_row_version=4,
    )

    assert restored.status is ActionExecutionStatus.EVIDENCE_FAILED
    assert restored.error_code == "APP_AFTER_CAPTURE_MISSING_AFTER_CRASH"
    assert not any("UPDATE qc_surface_session AS session" in call[1] for call in connection.calls)
    update = next(call for call in connection.calls if "UPDATE qc_action_execution" in call[1])
    assert "status = 'evidence_failed'" in update[1]
    assert update[2][2] == "APP_AFTER_CAPTURE_MISSING_AFTER_CRASH"


async def test_recover_rejects_corrupted_pending_result_before_state_update() -> None:
    initial = _action_row(
        status=ActionExecutionStatus.EVIDENCE_PENDING,
        row_version=4,
    )
    corrupted = dict(initial["result_payload"])
    corrupted["task_id"] = "99999999-9999-9999-9999-999999999999"
    initial["result_payload"] = corrupted
    connection = _RecoveryConnection(
        initial_row=initial,
        after_capture_id="88888888-8888-8888-8888-888888888888",
    )
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    with pytest.raises(ActionExecutionPersistenceError, match="账本状态、结果"):
        await repository.recover(
            action_execution_id="action-1",
            permit=_recovery_permit(),
            expected_row_version=4,
        )

    assert not any("SELECT capture.id::text" in call[1] for call in connection.calls)
    assert not any("UPDATE qc_action_execution" in call[1] for call in connection.calls)
    assert not any("UPDATE qc_surface_session" in call[1] for call in connection.calls)


@pytest.mark.parametrize(
    ("terminal_status", "result_status"),
    [
        (ActionExecutionStatus.FAILED, InteractionResultStatus.FAILED),
        (ActionExecutionStatus.BLOCKED, InteractionResultStatus.BLOCKED),
        (ActionExecutionStatus.TIMED_OUT, InteractionResultStatus.TIMED_OUT),
    ],
)
async def test_recover_terminal_action_attaches_after_for_each_terminal_status(
    terminal_status: ActionExecutionStatus,
    result_status: InteractionResultStatus,
) -> None:
    after_capture_id = "88888888-8888-8888-8888-888888888888"
    attached = _action_row(
        status=terminal_status,
        result_capture_id=after_capture_id,
        row_version=5,
    )
    connection = _RecoveryConnection(
        initial_row=_action_row(status=terminal_status, row_version=4),
        update_rows=(attached,),
        after_capture_id=after_capture_id,
    )
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    restored = await repository.recover(
        action_execution_id="action-1",
        permit=_recovery_permit(),
        expected_row_version=4,
    )

    assert restored.status is terminal_status
    assert restored.result_capture_id == after_capture_id
    assert restored.result_payload["status"] == result_status.value
    update = next(call for call in connection.calls if "UPDATE qc_action_execution" in call[1])
    assert "action.result_capture_id IS NULL" in update[1]
    assert "action.status = $4" in update[1]
    assert update[2][3] == terminal_status.value
    assert any("UPDATE qc_surface_session AS session" in call[1] for call in connection.calls)


async def test_recover_terminal_cursor_requires_current_lease() -> None:
    result_capture_id = "88888888-8888-8888-8888-888888888888"
    connection = _RecoveryConnection(
        initial_row=_action_row(
            status=ActionExecutionStatus.COMPLETED,
            result_capture_id=result_capture_id,
        ),
        cursor_result=None,
    )
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    with pytest.raises(ActionExecutionPersistenceError, match="无法推进当前 Session 游标"):
        await repository.recover(
            action_execution_id="action-1",
            permit=_recovery_permit(),
            expected_row_version=4,
        )

    cursor = next(
        call for call in connection.calls if "UPDATE qc_surface_session AS session" in call[1]
    )
    assert "session.status IN ('active', 'draining')" in cursor[1]
    assert "session.lease_owner = $4" in cursor[1]
    assert "session.lease_token = $5::uuid" in cursor[1]
    assert "session.lease_expires_at > now()" in cursor[1]


async def test_recover_rejects_mismatched_terminal_result_before_advancing_cursor() -> None:
    result_capture_id = "88888888-8888-8888-8888-888888888888"
    row = _action_row(
        status=ActionExecutionStatus.COMPLETED,
        result_capture_id=result_capture_id,
    )
    payload = dict(row["result_payload"])
    payload["result_capture_id"] = "99999999-9999-9999-9999-999999999999"
    row["result_payload"] = payload
    connection = _RecoveryConnection(initial_row=row)
    repository = PostgresActionExecutionRepository(
        pool=_Pool(connection), page_graph_repository=None
    )

    with pytest.raises(ActionExecutionPersistenceError, match="账本终态、结果或 Capture"):
        await repository.recover(
            action_execution_id="action-1",
            permit=_recovery_permit(),
            expected_row_version=4,
        )

    assert not any(
        "UPDATE qc_surface_session AS session" in call[1]
        for call in connection.calls
    )
