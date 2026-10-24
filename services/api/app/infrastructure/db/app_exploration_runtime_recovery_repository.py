"""App 页面探索 Runtime Checkpoint 与 Work 时间线 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.app_exploration_runtime_recovery import (
    AppExplorationRuntimeCheckpoint,
    AppExplorationRuntimeRecoveryError,
    AppExplorationWorkRecovery,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.domain.exploration import (
    AppExplorationOutcome,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
)
from services.api.app.domain.protocol import thaw_json_mapping
from services.api.app.infrastructure.db.app_exploration_result_repository import (
    PostgresAppExplorationResultRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresAppExplorationRuntimeRecoveryRepository:
    """只读取同一 App Run 的权威时间线，禁止从 Redis 猜测预算。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool
        self._results = PostgresAppExplorationResultRepository(
            database_url=database_url,
            pool=pool,
        )

    async def get_checkpoint(
        self,
        *,
        agent_run_id: str,
    ) -> AppExplorationRuntimeCheckpoint | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(_CHECKPOINT_SELECT, agent_run_id)
        return _parse_checkpoint(row) if row is not None else None

    async def create_checkpoint(
        self,
        *,
        checkpoint: AppExplorationRuntimeCheckpoint,
    ) -> AppExplorationRuntimeCheckpoint:
        if not isinstance(checkpoint, AppExplorationRuntimeCheckpoint):
            raise TypeError("checkpoint 必须为 AppExplorationRuntimeCheckpoint")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                INSERT INTO qc_app_exploration_runtime_checkpoint AS existing (
                    agent_run_id, task_id, surface_session_id, platform_build_ref,
                    bootstrap_capture_id, installed_package,
                    protocol_version, runtime_policy, runtime_policy_sha256,
                    objective_hash, started_at, deadline, created_at, updated_at
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::varchar, $4::varchar,
                    $5::uuid, $6::varchar,
                    $7::varchar, $8::jsonb, $9::varchar,
                    $10::varchar, $11::timestamptz, $12::timestamptz,
                    $13::timestamptz, $13::timestamptz
                )
                ON CONFLICT (agent_run_id) DO UPDATE
                SET agent_run_id = existing.agent_run_id
                WHERE existing.task_id = EXCLUDED.task_id
                  AND existing.surface_session_id = EXCLUDED.surface_session_id
                  AND existing.platform_build_ref = EXCLUDED.platform_build_ref
                  AND existing.bootstrap_capture_id = EXCLUDED.bootstrap_capture_id
                  AND existing.installed_package = EXCLUDED.installed_package
                  AND existing.protocol_version = EXCLUDED.protocol_version
                  AND existing.runtime_policy = EXCLUDED.runtime_policy
                  AND existing.runtime_policy_sha256 = EXCLUDED.runtime_policy_sha256
                  AND existing.objective_hash = EXCLUDED.objective_hash
                  AND existing.started_at = EXCLUDED.started_at
                  AND existing.deadline = EXCLUDED.deadline
                RETURNING
                    existing.agent_run_id::text AS agent_run_id,
                    existing.task_id::text AS task_id,
                    existing.surface_session_id,
                    existing.platform_build_ref,
                    existing.bootstrap_capture_id::text AS bootstrap_capture_id,
                    existing.installed_package,
                    existing.protocol_version,
                    existing.runtime_policy,
                    existing.runtime_policy_sha256,
                    existing.objective_hash,
                    existing.started_at,
                    existing.deadline,
                    existing.created_at
                """,
                checkpoint.agent_run_id,
                checkpoint.task_id,
                checkpoint.surface_session_id,
                checkpoint.platform_build_ref,
                checkpoint.bootstrap_capture_id,
                checkpoint.installed_package,
                checkpoint.protocol_version,
                _json_dumps(checkpoint.runtime_policy),
                checkpoint.runtime_policy_sha256,
                checkpoint.objective_hash,
                checkpoint.started_at,
                checkpoint.deadline,
                checkpoint.created_at,
            )
        if row is None:
            raise AppExplorationRuntimeRecoveryError(
                "agent_run_id 已绑定不同的 App Runtime Checkpoint"
            )
        return _parse_checkpoint(row)

    async def get_work_recovery(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        surface_session_id: str,
    ) -> AppExplorationWorkRecovery:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    item.work_id,
                    item.work_payload,
                    item.status,
                    item.terminal_payload,
                    item.last_error_code,
                    item.next_attempt_at,
                    item.started_at,
                    item.finished_at,
                    item.created_at,
                    item.lease_expires_at,
                    item.row_version
                FROM qc_v2_work_item AS item
                INNER JOIN qc_task AS task
                    ON task.id = item.task_id
                INNER JOIN qc_capture AS source
                    ON source.id = item.source_capture_id
                   AND source.task_id = item.task_id
                   AND source.agent_run_id = $2::uuid
                   AND source.surface_session_id = item.session_id
                   AND source.surface_kind IN ('android', 'ios')
                WHERE item.task_id = $1::uuid
                  AND item.session_id = $3::varchar
                  AND item.work_type = 'exploration'
                  AND item.finalization_epoch = task.finalization_epoch
                  AND task.target_platform = 'app'
                ORDER BY item.created_at, item.work_id
                """,
                task_id,
                agent_run_id,
                surface_session_id,
            )
        return _parse_work_recovery(rows, task_id=task_id, session_id=surface_session_id)

    async def get_result(self, *, agent_run_id: str):
        return await self._results.get_by_run_id(run_id=agent_run_id)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


_CHECKPOINT_SELECT = """
    SELECT
        agent_run_id::text AS agent_run_id,
        task_id::text AS task_id,
        surface_session_id,
        platform_build_ref,
        bootstrap_capture_id::text AS bootstrap_capture_id,
        installed_package,
        protocol_version,
        runtime_policy,
        runtime_policy_sha256,
        objective_hash,
        started_at,
        deadline,
        created_at
    FROM qc_app_exploration_runtime_checkpoint
    WHERE agent_run_id = $1::uuid
"""


def _parse_checkpoint(row: Mapping[str, Any]) -> AppExplorationRuntimeCheckpoint:
    return AppExplorationRuntimeCheckpoint(
        task_id=str(row["task_id"]),
        agent_run_id=str(row["agent_run_id"]),
        surface_session_id=str(row["surface_session_id"]),
        platform_build_ref=str(row["platform_build_ref"]),
        bootstrap_capture_id=str(row["bootstrap_capture_id"]),
        installed_package=str(row["installed_package"]),
        protocol_version=str(row["protocol_version"]),
        runtime_policy=_json_mapping(row["runtime_policy"], "runtime_policy"),
        runtime_policy_sha256=str(row["runtime_policy_sha256"]),
        objective_hash=str(row["objective_hash"]),
        started_at=_datetime(row["started_at"], "started_at"),
        deadline=_datetime(row["deadline"], "deadline"),
        created_at=_datetime(row["created_at"], "created_at"),
    )


def _parse_work_recovery(
    rows: list[Mapping[str, Any]],
    *,
    task_id: str,
    session_id: str,
) -> AppExplorationWorkRecovery:
    if not rows:
        return AppExplorationWorkRecovery(
            initial_work=None,
            latest_work=None,
            terminal=None,
            terminal_status=None,
            terminal_error_code=None,
            consumed_turn_count=0,
            consumed_action_count=0,
            has_nonterminal_work=False,
        )
    timeline: list[tuple[Mapping[str, Any], ExplorationWork, ExplorerWorkStatus]] = []
    terminal_rows: list[tuple[ExplorerWorkStatus, ExplorationTerminal]] = []
    next_claim_at: datetime | None = None
    for row in rows:
        work = ExplorationWork.from_dict(_json_mapping(row["work_payload"], "work_payload"))
        status = ExplorerWorkStatus(str(row["status"]))
        if work.task_id != task_id or work.session_id != session_id:
            raise AppExplorationRuntimeRecoveryError("Explorer Work 与恢复请求归属不一致")
        timeline.append((row, work, status))
        raw_terminal = row.get("terminal_payload")
        if raw_terminal is not None:
            terminal = _parse_terminal(raw_terminal)
            if terminal.work_id != work.work_id or terminal.task_id != task_id:
                raise AppExplorationRuntimeRecoveryError("Terminal payload 与 Work 归属不一致")
            if _work_status_for_terminal(terminal.status) is not status:
                raise AppExplorationRuntimeRecoveryError(
                    "Terminal payload 与持久化 Work 状态不一致"
                )
            terminal_rows.append((status, terminal))
        if status is ExplorerWorkStatus.RETRY_WAIT:
            candidate = _datetime(row["next_attempt_at"], "next_attempt_at")
            next_claim_at = candidate if next_claim_at is None else min(next_claim_at, candidate)
        elif status in {ExplorerWorkStatus.LEASED, ExplorerWorkStatus.RUNNING}:
            candidate = _datetime(row["lease_expires_at"], "lease_expires_at")
            next_claim_at = candidate if next_claim_at is None else min(next_claim_at, candidate)
    if len(terminal_rows) > 1:
        raise AppExplorationRuntimeRecoveryError("同一 Run 存在多个 Explorer 业务终态")
    nonterminal_statuses = [
        status for _row, _work, status in timeline if not status.is_terminal
    ]
    if ExplorerWorkStatus.PENDING in nonterminal_statuses:
        next_claim_at = None
    terminal_status, terminal = terminal_rows[0] if terminal_rows else (None, None)
    latest_row, latest, latest_status = _latest_timeline_item(timeline)
    initial = _initial_timeline_item(timeline)
    has_nonterminal = any(not status.is_terminal for _row, _work, status in timeline)
    if terminal is not None:
        terminal_work = next(
            work
            for row, work, _status in timeline
            if row.get("terminal_payload") is not None
        )
        if terminal_work.work_id != latest.work_id or has_nonterminal:
            raise AppExplorationRuntimeRecoveryError("业务 Terminal 不是当前 Work 时间线的唯一末端")
    terminal_error_code = None
    if not has_nonterminal and terminal_status is None:
        if latest_status.is_terminal:
            terminal_status = latest_status
            raw_error = latest_row.get("last_error_code")
            terminal_error_code = (
                str(raw_error).strip()
                if isinstance(raw_error, str) and raw_error.strip()
                else None
            )
            terminal = _synthetic_terminal(
                work=latest,
                status=latest_status,
                error_code=terminal_error_code,
            )
    consumed_turn_count = sum(1 for _row, _work, status in timeline if status.is_terminal)
    consumed_action_count = max(0, latest.budget.max_actions - latest.budget.remaining_actions)
    if terminal is not None and "consumed_action_count" in terminal.coverage_summary:
        settled_count = terminal.coverage_summary["consumed_action_count"]
        if (
            isinstance(settled_count, bool)
            or not isinstance(settled_count, int)
            or not consumed_action_count <= settled_count <= min(
                latest.budget.max_actions, consumed_action_count + 1
            )
        ):
            raise AppExplorationRuntimeRecoveryError("终态动作结算与末端 Work 预算不一致")
        consumed_action_count = settled_count
    return AppExplorationWorkRecovery(
        initial_work=initial,
        latest_work=latest,
        terminal=terminal,
        terminal_status=terminal_status,
        terminal_error_code=terminal_error_code,
        consumed_turn_count=consumed_turn_count,
        consumed_action_count=consumed_action_count,
        has_nonterminal_work=has_nonterminal,
        next_claim_at=next_claim_at,
    )


def _initial_timeline_item(
    timeline: list[tuple[Mapping[str, Any], ExplorationWork, ExplorerWorkStatus]],
) -> ExplorationWork:
    work_ids = {work.work_id for _row, work, _status in timeline}
    candidates = [
        work
        for _row, work, _status in timeline
        if _parent_work_id(work) not in work_ids
    ]
    if len(candidates) != 1:
        raise AppExplorationRuntimeRecoveryError("Explorer Work 时间线缺少唯一初始 Work")
    return candidates[0]


def _latest_timeline_item(
    timeline: list[tuple[Mapping[str, Any], ExplorationWork, ExplorerWorkStatus]],
) -> tuple[Mapping[str, Any], ExplorationWork, ExplorerWorkStatus]:
    parent_ids = {
        parent_id
        for _row, work, _status in timeline
        if (parent_id := _parent_work_id(work)) is not None
    }
    candidates = [item for item in timeline if item[1].work_id not in parent_ids]
    if len(candidates) != 1:
        raise AppExplorationRuntimeRecoveryError("Explorer Work 时间线缺少唯一末端 Work")
    return candidates[0]


def _parent_work_id(work: ExplorationWork) -> str | None:
    raw = work.frontier.observation.metadata.get("parent_work_id")
    return raw if isinstance(raw, str) and raw.strip() else None


def _json_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, Mapping):
        raise AppExplorationRuntimeRecoveryError(f"{field_name} 必须为 JSON object")
    return parsed


def _parse_terminal(value: Any) -> ExplorationTerminal:
    payload = _json_mapping(value, "terminal_payload")
    try:
        return ExplorationTerminal(
            protocol_version=str(payload["protocol_version"]),
            work_id=str(payload["work_id"]),
            task_id=str(payload["task_id"]),
            status=ExplorationTerminalStatus(str(payload["status"])),
            reason=str(payload["reason"]),
            outcome=(str(payload["outcome"]) if payload.get("outcome") is not None else None),
            reason_code=(
                str(payload["reason_code"])
                if payload.get("reason_code") is not None
                else None
            ),
            coverage_summary=_json_mapping(
                payload.get("coverage_summary", {}),
                "terminal_payload.coverage_summary",
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AppExplorationRuntimeRecoveryError("Terminal payload 无法恢复") from exc


def _work_status_for_terminal(status: ExplorationTerminalStatus) -> ExplorerWorkStatus:
    return {
        ExplorationTerminalStatus.COMPLETED: ExplorerWorkStatus.SUCCEEDED,
        ExplorationTerminalStatus.BOUNDED: ExplorerWorkStatus.BOUNDED,
        ExplorationTerminalStatus.BUDGET_EXHAUSTED: (
            ExplorerWorkStatus.BUDGET_EXHAUSTED
        ),
        ExplorationTerminalStatus.BLOCKED: ExplorerWorkStatus.BLOCKED,
        ExplorationTerminalStatus.NO_PROGRESS: ExplorerWorkStatus.NO_PROGRESS,
    }[status]


def _synthetic_terminal(
    *,
    work: ExplorationWork,
    status: ExplorerWorkStatus,
    error_code: str | None,
) -> ExplorationTerminal:
    """终态行缺 payload 时保守收口，绝不把未知终态制造为成功。"""

    bounded_failure = error_code in {
        "EXPLORER_WORK_DEADLINE_EXCEEDED",
        "EXPLORER_WORK_ATTEMPTS_EXHAUSTED",
    }
    technical = (
        status in {ExplorerWorkStatus.FAILED, ExplorerWorkStatus.SUCCEEDED}
        and not bounded_failure
    )
    terminal_status = {
        ExplorerWorkStatus.BUDGET_EXHAUSTED: ExplorationTerminalStatus.BUDGET_EXHAUSTED,
        ExplorerWorkStatus.BOUNDED: ExplorationTerminalStatus.BOUNDED,
        ExplorerWorkStatus.NO_PROGRESS: ExplorationTerminalStatus.NO_PROGRESS,
    }.get(status, ExplorationTerminalStatus.BLOCKED)
    if error_code == "EXPLORER_WORK_DEADLINE_EXCEEDED":
        terminal_status = ExplorationTerminalStatus.BUDGET_EXHAUSTED
    return ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=terminal_status,
        reason="持久化 Explorer Work 已终止，但缺少完整 Terminal payload。",
        outcome=(
            AppExplorationOutcome.TECHNICAL_FAILED.value
            if technical
            else AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value
        ),
        reason_code=error_code or f"persisted_work_{status.value}_without_terminal_payload",
        coverage_summary={
            "persisted_work_status": status.value,
            "terminal_payload_missing": True,
        },
    )


def _json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(
        thaw_json_mapping(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise AppExplorationRuntimeRecoveryError(f"{field_name} 必须为 datetime")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
