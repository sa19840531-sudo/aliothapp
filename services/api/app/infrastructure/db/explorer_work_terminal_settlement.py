"""H5 deadline 后终态结算的身份、预算和 PostgreSQL 原子写回。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, NoReturn
from uuid import UUID

from services.api.app.application.exploration_terminal_settlement import (
    with_terminal_settlement,
)
from services.api.app.application.exploration_work_queue import (
    ExplorerWorkPersistenceError,
    ExplorerWorkStatus,
    ExplorerWorkType,
    TransitionExplorerWorkCommand,
)
from services.api.app.domain.exploration import (
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql


def terminal_for_settlement(
    command: TransitionExplorerWorkCommand,
) -> ExplorationTerminal | None:
    """从本轮结果 Work 重建终态摘要，不信任调用方的计数。"""
    result_work = command.settlement_work
    if result_work is None:
        return command.terminal
    if command.expected_status is not ExplorerWorkStatus.RUNNING:
        raise ExplorerWorkPersistenceError("终态结算父 Work 必须从 RUNNING 开始")
    if command.target_status is not ExplorerWorkStatus.BUDGET_EXHAUSTED:
        raise ExplorerWorkPersistenceError("终态结算只允许写入 budget_exhausted")
    if not isinstance(result_work, ExplorationWork):
        raise ExplorerWorkPersistenceError("终态结算结果必须为 ExplorationWork")
    if result_work.surface_kind is not SurfaceKind.WEB:
        raise ExplorerWorkPersistenceError("终态结算结果必须为 Web Exploration Work")
    if command.terminal is None:
        raise ExplorerWorkPersistenceError("终态结算必须提供 ExplorationTerminal")
    if command.terminal.status is not ExplorationTerminalStatus.BUDGET_EXHAUSTED:
        raise ExplorerWorkPersistenceError("终态结算 terminal 必须为 budget_exhausted")
    try:
        return with_terminal_settlement(command.terminal, result_work=result_work)
    except ValueError as exc:
        raise ExplorerWorkPersistenceError("终态结算结果 Work 谱系不一致") from exc


async def settle_expired_web_work_in_transaction(
    connection: Any,
    *,
    command: TransitionExplorerWorkCommand,
    terminal_payload: Mapping[str, Any],
) -> int:
    """在父 Work 原身份上结算 H5 结果；调用方随后在同一事务保存覆盖快照。"""
    result_work = command.settlement_work
    if not isinstance(result_work, ExplorationWork):
        _reject("终态结算缺少 Exploration Work 结果")

    work_task_id = await connection.fetchval(
        """
        SELECT task_id::text
        FROM qc_v2_work_item
        WHERE work_id = $1::varchar
        """,
        command.work_id,
    )
    if work_task_id is None:
        _reject("终态结算父 Work 不存在")
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"task-finalization:{work_task_id}",
    )
    task = await connection.fetchrow(
        """
        SELECT id::text, status, finalization_state, finalization_epoch
        FROM qc_task
        WHERE id = $1::uuid
        FOR UPDATE
        """,
        work_task_id,
    )
    if task is None:
        _reject("终态结算任务不存在")

    parent = await connection.fetchrow(
        """
        SELECT
            work_item.work_id,
            work_item.task_id::text AS task_id,
            work_item.session_id,
            work_item.branch_id,
            work_item.source_capture_id::text AS source_capture_id,
            work_item.work_type,
            work_item.status,
            work_item.work_payload,
            work_item.deadline,
            work_item.lease_token::text AS lease_token,
            work_item.lease_expires_at,
            work_item.row_version,
            work_item.finalization_epoch,
            clock_timestamp() AS database_now
        FROM qc_v2_work_item AS work_item
        WHERE work_item.work_id = $1::varchar
        FOR UPDATE
        """,
        command.work_id,
    )
    if parent is None:
        _reject("终态结算父 Work 不存在")
    if str(parent["task_id"]) != str(task["id"]):
        _reject("终态结算父 Work 任务归属漂移")
    if str(parent["task_id"]) != result_work.task_id:
        _reject("终态结算结果 Work 任务归属不一致")
    if parent["work_type"] != ExplorerWorkType.EXPLORATION.value:
        _reject("只有 Exploration Work 可以终态结算")
    if parent["status"] != ExplorerWorkStatus.RUNNING.value:
        _reject("终态结算父 Work 已不在 RUNNING")
    if task["status"] != "running" or task["finalization_state"] != "open":
        _reject("终态结算任务已封存")
    if int(parent["finalization_epoch"]) != int(task["finalization_epoch"]):
        _reject("终态结算父 Work epoch 已漂移")
    if parent["row_version"] != command.row_version:
        _reject("终态结算父 Work row_version 已变化")
    if parent["lease_token"] != command.lease_token:
        _reject("终态结算父 Work lease_token 已变化")
    if (
        parent["lease_expires_at"] is None
        or parent["lease_expires_at"] <= parent["database_now"]
    ):
        _reject("终态结算父 Work 租约已失效")
    if parent["deadline"] > parent["database_now"]:
        _reject("终态结算只允许在 deadline 已到时执行")

    try:
        parent_work = ExplorationWork.from_dict(
            _as_mapping(parent["work_payload"], "parent work_payload")
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise ExplorerWorkPersistenceError("终态结算父 Work payload 无效") from exc
    if parent_work.work_id != command.work_id:
        _reject("终态结算父 Work payload 身份不一致")
    if parent_work.task_id != str(parent["task_id"]):
        _reject("终态结算父 Work payload 任务归属不一致")
    if parent_work.session_id != str(parent["session_id"]):
        _reject("终态结算父 Work payload Session 归属不一致")
    if result_work.work_id == command.work_id:
        _reject("终态结算结果不得复用父 Work 身份")
    if parent_work.session_id != result_work.session_id:
        _reject("终态结算结果 Work Session 归属不一致")
    if parent_work.surface_kind is not SurfaceKind.WEB:
        _reject("终态结算父 Work 不是 Web")
    if parent_work.source_capture_id != str(parent["source_capture_id"]):
        _reject("终态结算父 Work source Capture 已漂移")
    if (
        parent_work.deadline != parent["deadline"]
        or result_work.deadline != parent["deadline"]
    ):
        _reject("终态结算不得改变父 Work deadline")
    if parent_work.objective_hash != result_work.objective_hash:
        _reject("终态结算结果 Work objective 已漂移")
    _require_settlement_budget_not_increased(
        parent_work,
        result_work,
    )

    parent_metadata = parent_work.frontier.observation.metadata
    result_metadata = result_work.frontier.observation.metadata
    parent_agent_run_id = _require_settlement_agent_run_id(
        parent_metadata,
    )
    result_agent_run_id = _require_settlement_agent_run_id(
        result_metadata,
    )
    if parent_agent_run_id != result_agent_run_id:
        _reject("终态结算结果 Work Run 归属不一致")
    if result_metadata.get("parent_work_id") != command.work_id:
        _reject("终态结算结果 Work 谱系不一致")
    if result_metadata.get("parent_branch_id") != parent_work.branch_id:
        _reject("终态结算结果 Work parent branch 不一致")
    if result_metadata.get("parent_capture_id") != parent_work.source_capture_id:
        _reject("终态结算结果 Work parent Capture 不一致")
    _validate_scheduler_scope(
        result_metadata,
        task=task,
        session_id=str(parent["session_id"]),
        agent_run_id=result_agent_run_id,
    )

    session = await connection.fetchrow(
        """
        SELECT session_id, surface_kind, status, finalization_epoch
        FROM qc_surface_session
        WHERE task_id = $1::uuid AND session_id = $2
        FOR UPDATE
        """,
        parent["task_id"],
        parent["session_id"],
    )
    if session is None:
        _reject("终态结算 Surface Session 不存在")
    if session["surface_kind"] != SurfaceKind.WEB.value:
        _reject("终态结算 Surface Session 不是 Web")
    if session["status"] not in {"starting", "active", "draining"}:
        _reject("终态结算 Surface Session 已关闭")
    if int(session["finalization_epoch"]) != int(task["finalization_epoch"]):
        _reject("终态结算 Session epoch 已漂移")

    parent_capture = await _read_settlement_capture(
        connection,
        capture_id=str(parent["source_capture_id"]),
    )
    _validate_capture_scope(
        parent_capture,
        task=task,
        session_id=str(parent["session_id"]),
        agent_run_id=parent_agent_run_id,
        label="父 Work source Capture",
    )
    result_capture = await _read_settlement_capture(
        connection,
        capture_id=result_work.source_capture_id,
    )
    _validate_capture_scope(
        result_capture,
        task=task,
        session_id=str(parent["session_id"]),
        agent_run_id=result_agent_run_id,
        label="终态结算 source Capture",
    )

    row_version = await connection.fetchval(
        """
        UPDATE qc_v2_work_item
        SET
            status = 'budget_exhausted',
            row_version = row_version + 1,
            terminal_payload = $6::jsonb,
            last_error_code = $7::varchar,
            last_error_message = $8::text,
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            finished_at = now(),
            updated_at = now()
        WHERE work_id = $1::varchar
          AND task_id = $2::uuid
          AND status = 'running'
          AND row_version = $3::bigint
          AND lease_token = $4::uuid
          AND lease_expires_at > clock_timestamp()
          AND deadline = $5::timestamptz
          AND deadline <= clock_timestamp()
          AND finalization_epoch = $9::integer
        RETURNING row_version
        """,
        command.work_id,
        task["id"],
        command.row_version,
        command.lease_token,
        parent["deadline"],
        _json_dumps(dict(terminal_payload)),
        command.error_code,
        command.error_message,
        task["finalization_epoch"],
    )
    if row_version is None:
        _reject("终态结算父 Work 状态、版本、租约或 deadline 已变化")
    return int(row_version)


async def _read_settlement_capture(
    connection: Any,
    *,
    capture_id: str,
) -> Any:
    try:
        UUID(capture_id)
    except (ValueError, AttributeError) as exc:
        raise ExplorerWorkPersistenceError("终态结算 source Capture ID 无效") from exc
    capture = await connection.fetchrow(
        f"""
        SELECT
            capture.task_id::text AS task_id,
            capture.agent_run_id::text AS agent_run_id,
            capture.surface_kind,
            capture.surface_session_id,
            capture.capture_status,
            {capture_epoch_sql()} AS effective_finalization_epoch,
            source_session.finalization_epoch AS session_finalization_epoch
        FROM qc_capture AS capture
        JOIN qc_surface_session AS source_session
          ON source_session.task_id = capture.task_id
         AND source_session.session_id = capture.surface_session_id
        WHERE capture.id = $1::uuid
        """,
        capture_id,
    )
    if capture is None:
        _reject("终态结算 source Capture 不存在")
    return capture


def _validate_capture_scope(
    capture: Any,
    *,
    task: Any,
    session_id: str,
    agent_run_id: str,
    label: str,
) -> None:
    try:
        UUID(agent_run_id)
    except (ValueError, AttributeError) as exc:
        raise ExplorerWorkPersistenceError(f"{label} Run ID 无效") from exc
    if (
        str(capture["task_id"]) != str(task["id"])
        or capture["agent_run_id"] != agent_run_id
        or capture["surface_kind"] != SurfaceKind.WEB.value
        or capture["surface_session_id"] != session_id
        or capture["capture_status"] != "captured"
        or capture["effective_finalization_epoch"] is None
        or int(capture["effective_finalization_epoch"])
        != int(task["finalization_epoch"])
        or int(capture["session_finalization_epoch"])
        != int(task["finalization_epoch"])
    ):
        _reject(f"{label} Run/Session/Epoch 不一致")


def _validate_scheduler_scope(
    metadata: Mapping[str, Any],
    *,
    task: Any,
    session_id: str,
    agent_run_id: str,
) -> None:
    scope = metadata.get("exploration_scheduler_facts_scope")
    if scope is None:
        return
    if not isinstance(scope, Mapping):
        _reject("终态结算调度事实 scope 无效")
    try:
        scope_epoch = int(scope["finalization_epoch"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExplorerWorkPersistenceError("终态结算调度事实缺少 epoch") from exc
    if (
        str(scope.get("task_id")) != str(task["id"])
        or str(scope.get("agent_run_id")) != agent_run_id
        or str(scope.get("session_id")) != session_id
        or scope_epoch != int(task["finalization_epoch"])
    ):
        _reject("终态结算调度事实 scope 身份不一致")


def _require_settlement_agent_run_id(
    metadata: Mapping[str, Any],
) -> str:
    value = metadata.get("agent_run_id")
    if not isinstance(value, str) or not value.strip():
        _reject("终态结算 Work 缺少 agent_run_id")
    return value.strip()


def _require_settlement_budget_not_increased(
    parent_work: ExplorationWork,
    result_work: ExplorationWork,
) -> None:
    parent_budget = parent_work.budget
    result_budget = result_work.budget
    for field_name in (
        "max_actions",
        "max_depth",
        "max_business_actions",
        "max_obstruction_actions",
        "business_duration_floor_ms",
    ):
        if getattr(result_budget, field_name) != getattr(parent_budget, field_name):
            _reject(f"终态结算不得改变预算上限: {field_name}")
    for field_name in (
        "remaining_actions",
        "remaining_business_actions",
        "remaining_obstruction_actions",
        "remaining_duration_ms",
    ):
        if getattr(result_budget, field_name) > getattr(parent_budget, field_name):
            _reject(f"终态结算预算不能增加: {field_name}")
    if parent_budget.remaining_actions - result_budget.remaining_actions != 1:
        _reject("终态结算本轮动作预算必须恰好扣减 1")
    if (
        result_budget.business_duration_floor_applied
        < parent_budget.business_duration_floor_applied
    ):
        _reject("终态结算不得回退业务时长地板状态")


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须为对象")
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reject(message: str) -> NoReturn:
    raise ExplorerWorkPersistenceError(message)
