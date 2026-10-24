"""App 任务级 Frontier 的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.api.app.application.app_frontier_queue import (
    ClaimAppFrontierCommand,
    PublishAppFrontierCandidatesCommand,
    RenewAppFrontierLeaseCommand,
    TransitionAppFrontierCommand,
)
from services.api.app.domain.exploration import (
    AppFrontierCounts,
    AppFrontierItem,
    AppFrontierLease,
    AppFrontierStatus,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class AppFrontierPersistenceError(RuntimeError):
    """Frontier 归属、幂等身份或并发状态发生冲突。"""


class PostgresAppFrontierRepository:
    """以同一 App Run 内候选指纹去重并提供租约领取。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def publish_candidates(
        self,
        *,
        command: PublishAppFrontierCandidatesCommand,
    ) -> tuple[AppFrontierItem, ...]:
        candidates = tuple(command.candidates)
        if not candidates:
            return ()
        _validate_publish_command(command)
        pool = await self._get_pool()
        persisted: list[AppFrontierItem] = []
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"app-frontier:{command.agent_run_id}",
            )
            for item in candidates:
                payload = item.to_dict()
                row = await connection.fetchrow(
                    """
                    INSERT INTO qc_app_exploration_frontier AS existing (
                        frontier_item_id, task_id, agent_run_id, surface_session_id,
                        source_work_id, source_capture_id, source_page_identity,
                        candidate_fingerprint, candidate_id, semantic_target,
                        action_type, risk_level, navigation_path, priority,
                        protocol_version, payload_hash, candidate_payload,
                        discovered_at, max_attempts
                    )
                    SELECT
                        $1, task.id, run.id, session.session_id,
                        work.work_id, capture.id, $7, $8, $9, $10,
                        $11, $12, $13::jsonb, $14, $15,
                        encode(
                            digest(convert_to($16::jsonb::text, 'UTF8'), 'sha256'),
                            'hex'
                        ),
                        $16::jsonb, $17, $18
                    FROM qc_task AS task
                    INNER JOIN qc_agent_run AS run
                        ON run.id = $3::uuid
                       AND run.task_id = task.id
                       AND run.status = 'running'
                    INNER JOIN qc_surface_session AS session
                        ON session.task_id = task.id
                       AND session.session_id = $4
                       AND session.status = 'active'
                       AND session.surface_kind IN ('android', 'ios')
                    INNER JOIN qc_v2_work_item AS work
                        ON work.work_id = $5
                       AND work.task_id = task.id
                       AND work.session_id = session.session_id
                       AND work.work_type = 'exploration'
                    INNER JOIN qc_capture AS capture
                        ON capture.id = $6::uuid
                       AND capture.task_id = task.id
                       AND capture.agent_run_id = run.id
                       AND capture.surface_session_id = session.session_id
                       AND capture.capture_status = 'captured'
                       AND capture.surface_kind IN ('android', 'ios')
                    WHERE task.id = $2::uuid
                      AND task.status = 'running'
                      AND task.finalization_state = 'open'
                    ON CONFLICT (agent_run_id, candidate_fingerprint) DO UPDATE
                    SET frontier_item_id = existing.frontier_item_id
                    WHERE existing.task_id = EXCLUDED.task_id
                      AND existing.surface_session_id = EXCLUDED.surface_session_id
                    RETURNING existing.status, existing.candidate_payload
                    """,
                    item.frontier_item_id,
                    item.task_id,
                    item.agent_run_id,
                    item.session_id,
                    item.source_work_id,
                    item.source_capture_id,
                    item.source_page_identity,
                    item.candidate_fingerprint,
                    item.candidate.candidate_id,
                    item.candidate.semantic_target,
                    item.candidate.action_type.value,
                    item.candidate.risk_level.value,
                    _json_dumps(item.navigation_path),
                    item.priority,
                    item.protocol_version,
                    _json_dumps(payload),
                    item.discovered_at,
                    _frontier_max_attempts_for_item(item),
                )
                if row is None:
                    raise AppFrontierPersistenceError(
                        "App Frontier 候选归属无效或幂等事实发生漂移"
                    )
                if AppFrontierStatus(str(row["status"])) is AppFrontierStatus.PENDING:
                    persisted.append(_parse_item(row["candidate_payload"]))
        return tuple(persisted)

    async def claim(
        self,
        *,
        command: ClaimAppFrontierCommand,
        lease_token: str,
    ) -> AppFrontierLease | None:
        if not isinstance(command, ClaimAppFrontierCommand):
            raise TypeError("command 必须为 ClaimAppFrontierCommand")
        if (
            not isinstance(lease_token, str)
            or not lease_token.strip()
            or lease_token != lease_token.strip()
        ):
            raise ValueError("lease_token 必须为无首尾空白的非空字符串")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                WITH candidate AS (
                    SELECT frontier.frontier_item_id
                    FROM qc_app_exploration_frontier AS frontier
                    INNER JOIN qc_task AS task ON task.id = frontier.task_id
                    INNER JOIN qc_agent_run AS run ON run.id = frontier.agent_run_id
                    INNER JOIN qc_surface_session AS session
                        ON session.task_id = frontier.task_id
                       AND session.session_id = frontier.surface_session_id
                    WHERE frontier.task_id = $1::uuid
                      AND frontier.agent_run_id = $2::uuid
                      AND frontier.surface_session_id = $3
                      AND ($7::varchar IS NULL OR frontier.frontier_item_id = $7)
                      AND task.status = 'running'
                      AND task.finalization_state = 'open'
                      AND run.status = 'running'
                      AND session.status = 'active'
                      AND (
                        (frontier.status IN ('pending', 'retry_wait')
                         AND frontier.next_attempt_at <= now())
                        OR (frontier.status IN ('leased', 'running')
                            AND frontier.lease_expires_at <= now())
                      )
                      AND frontier.attempt_count < frontier.max_attempts
                    ORDER BY frontier.priority, frontier.discovered_at, frontier.frontier_item_id
                    FOR UPDATE OF frontier SKIP LOCKED
                    LIMIT 1
                )
                UPDATE qc_app_exploration_frontier AS frontier
                SET
                    status = 'leased',
                    lease_owner = $4,
                    lease_token = $5::uuid,
                    lease_expires_at = now() + make_interval(secs => $6),
                    attempt_count = frontier.attempt_count + 1,
                    row_version = frontier.row_version + 1,
                    started_at = COALESCE(frontier.started_at, now()),
                    last_error_code = NULL,
                    last_error_message = NULL,
                    updated_at = now()
                FROM candidate
                WHERE frontier.frontier_item_id = candidate.frontier_item_id
                RETURNING frontier.candidate_payload, frontier.status,
                    frontier.lease_token::text AS lease_token,
                    frontier.attempt_count, frontier.row_version
                """,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                command.lease_owner,
                lease_token,
                command.lease_seconds,
                command.frontier_item_id,
            )
        if row is None:
            return None
        return AppFrontierLease(
            item=_parse_item(row["candidate_payload"]),
            status=AppFrontierStatus(str(row["status"])),
            lease_token=str(row["lease_token"]),
            attempt_count=int(row["attempt_count"]),
            row_version=int(row["row_version"]),
        )

    async def transition(self, *, command: TransitionAppFrontierCommand) -> int:
        _validate_transition(command)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row_version = await connection.fetchval(
                """
                UPDATE qc_app_exploration_frontier
                SET
                    status = $2::varchar,
                    result_work_id = COALESCE($6::varchar, result_work_id),
                    result_capture_id = COALESCE($7::uuid, result_capture_id),
                    last_error_code = $8::varchar,
                    last_error_message = $9,
                    next_attempt_at = CASE
                        WHEN $2::varchar = 'retry_wait'
                        THEN now() + make_interval(secs => $10)
                        ELSE next_attempt_at
                    END,
                    lease_owner = CASE WHEN $2::varchar = 'running' THEN lease_owner ELSE NULL END,
                    lease_token = CASE WHEN $2::varchar = 'running' THEN lease_token ELSE NULL END,
                    lease_expires_at = CASE
                        WHEN $2::varchar = 'running' THEN lease_expires_at
                        ELSE NULL
                    END,
                    finished_at = CASE
                        WHEN $2::varchar = ANY($11::varchar[]) THEN now()
                        ELSE NULL
                    END,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE frontier_item_id = $1::varchar
                  AND status = $3::varchar
                  AND row_version = $4
                  AND lease_token = $5::uuid
                  AND lease_expires_at > now()
                RETURNING row_version
                """,
                command.frontier_item_id,
                command.target_status.value,
                command.expected_status.value,
                command.row_version,
                command.lease_token,
                command.result_work_id,
                command.result_capture_id,
                command.error_code,
                command.error_message,
                command.retry_delay_seconds,
                [status.value for status in AppFrontierStatus if status.is_terminal],
            )
        if row_version is None:
            raise AppFrontierPersistenceError("App Frontier 状态、版本或租约已变化")
        return int(row_version)

    async def renew(self, *, command: RenewAppFrontierLeaseCommand) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            item_id = await connection.fetchval(
                """
                UPDATE qc_app_exploration_frontier
                SET lease_expires_at = now() + make_interval(secs => $4), updated_at = now()
                WHERE frontier_item_id = $1
                  AND status = 'running'
                  AND lease_owner = $2
                  AND lease_token = $3::uuid
                  AND lease_expires_at > now()
                RETURNING frontier_item_id
                """,
                command.frontier_item_id,
                command.lease_owner,
                command.lease_token,
                command.lease_seconds,
            )
        if item_id is None:
            raise AppFrontierPersistenceError("App Frontier 租约已失效")

    async def counts(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
    ) -> AppFrontierCounts:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    count(*) FILTER (WHERE status = 'pending')::int AS pending,
                    count(*) FILTER (WHERE status = 'leased')::int AS leased,
                    count(*) FILTER (WHERE status = 'running')::int AS running,
                    count(*) FILTER (WHERE status = 'retry_wait')::int AS retry_wait,
                    count(*) FILTER (WHERE status = 'visited')::int AS visited,
                    count(*) FILTER (WHERE status = 'blocked')::int AS blocked,
                    count(*) FILTER (WHERE status = 'failed')::int AS failed,
                    count(*) FILTER (WHERE status = 'cancelled')::int AS cancelled,
                    count(*) FILTER (WHERE status = 'superseded')::int AS superseded
                FROM qc_app_exploration_frontier
                WHERE task_id = $1::uuid
                  AND agent_run_id = $2::uuid
                  AND surface_session_id = $3
                """,
                task_id,
                agent_run_id,
                session_id,
            )
        if row is None:
            raise AppFrontierPersistenceError("无法读取 App Frontier 计数")
        return AppFrontierCounts(
            **{key: int(row[key]) for key in AppFrontierCounts.__annotations__}
        )

    async def next_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
    ) -> AppFrontierItem | None:
        """只读选择下一条候选；真正状态仍由父 Work 事务绑定。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT candidate_payload
                FROM qc_app_exploration_frontier
                WHERE task_id = $1::uuid
                  AND agent_run_id = $2::uuid
                  AND surface_session_id = $3
                  AND (
                    (status IN ('pending', 'retry_wait') AND next_attempt_at <= now())
                    OR (status IN ('leased', 'running') AND lease_expires_at <= now())
                  )
                  AND attempt_count < max_attempts
                ORDER BY priority, discovered_at, frontier_item_id
                LIMIT 1
                """,
                task_id,
                agent_run_id,
                session_id,
            )
        return None if row is None else _parse_item(row["candidate_payload"])

    async def list_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        limit: int = 256,
    ) -> tuple[AppFrontierItem, ...]:
        """有界读取可领取候选，供应用层按最新覆盖缺口重新排序。"""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 256:
            raise ValueError("limit 必须为 1..256 的整数")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT candidate_payload
                FROM qc_app_exploration_frontier
                WHERE task_id = $1::uuid
                  AND agent_run_id = $2::uuid
                  AND surface_session_id = $3
                  AND (
                    (status IN ('pending', 'retry_wait') AND next_attempt_at <= now())
                    OR (status IN ('leased', 'running') AND lease_expires_at <= now())
                  )
                  AND attempt_count < max_attempts
                ORDER BY discovered_at, frontier_item_id
                LIMIT $4
                """,
                task_id,
                agent_run_id,
                session_id,
                limit,
            )
        return tuple(_parse_item(row["candidate_payload"]) for row in rows)

    async def get_item(self, *, frontier_item_id: str) -> AppFrontierItem | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT candidate_payload
                FROM qc_app_exploration_frontier
                WHERE frontier_item_id = $1
                """,
                frontier_item_id,
            )
        return None if row is None else _parse_item(row["candidate_payload"])

    async def cancel_nonterminal(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        reason: str,
    ) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            result = await connection.fetchval(
                """
                WITH canceled AS (
                    UPDATE qc_app_exploration_frontier
                    SET status = 'cancelled', lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, last_error_code = 'WORKFLOW_CANCELED',
                        last_error_message = $3, finished_at = now(),
                        row_version = row_version + 1, updated_at = now()
                    WHERE task_id = $1::uuid
                      AND ($2::uuid IS NULL OR agent_run_id = $2::uuid)
                      AND status IN ('pending', 'leased', 'running', 'retry_wait')
                    RETURNING 1
                ) SELECT count(*) FROM canceled
                """,
                task_id,
                agent_run_id,
                reason,
            )
        return int(result or 0)

    async def expire_unclaimable(
        self,
        *,
        task_id: str,
        agent_run_id: str | None = None,
    ) -> int:
        """把耗尽尝试的 pending/retry 与过期租约转成可审计失败。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            count = await connection.fetchval(
                """
                WITH exhausted AS (
                    SELECT frontier_item_id
                    FROM qc_app_exploration_frontier
                    WHERE task_id = $1::uuid
                      AND ($2::uuid IS NULL OR agent_run_id = $2::uuid)
                      AND status IN ('pending', 'leased', 'running', 'retry_wait')
                      AND attempt_count >= max_attempts
                      AND (
                          status IN ('pending', 'retry_wait')
                          OR lease_expires_at <= now()
                      )
                    FOR UPDATE SKIP LOCKED
                ), updated AS (
                    UPDATE qc_app_exploration_frontier AS frontier
                    SET status = 'failed', lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = 'APP_FRONTIER_ATTEMPTS_EXHAUSTED',
                        last_error_message = 'App Frontier 已耗尽最大尝试次数',
                        finished_at = now(), row_version = frontier.row_version + 1,
                        updated_at = now()
                    FROM exhausted
                    WHERE frontier.frontier_item_id = exhausted.frontier_item_id
                    RETURNING 1
                )
                SELECT count(*) FROM updated
                """,
                task_id,
                agent_run_id,
            )
        return int(count or 0)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _validate_publish_command(command: PublishAppFrontierCandidatesCommand) -> None:
    fingerprints: set[str] = set()
    for item in command.candidates:
        if (
            item.task_id != command.task_id
            or item.agent_run_id != command.agent_run_id
            or item.session_id != command.session_id
            or item.source_work_id != command.source_work_id
        ):
            raise ValueError("App Frontier 候选与发布命令归属不一致")
        if item.candidate_fingerprint in fingerprints:
            raise ValueError("同一发布命令包含重复候选指纹")
        fingerprints.add(item.candidate_fingerprint)


def _validate_transition(command: TransitionAppFrontierCommand) -> None:
    allowed = {
        AppFrontierStatus.LEASED: {AppFrontierStatus.RUNNING},
        AppFrontierStatus.RUNNING: {
            AppFrontierStatus.VISITED,
            AppFrontierStatus.BLOCKED,
            AppFrontierStatus.FAILED,
            AppFrontierStatus.RETRY_WAIT,
            AppFrontierStatus.CANCELLED,
            AppFrontierStatus.SUPERSEDED,
        },
    }
    if command.target_status not in allowed.get(command.expected_status, set()):
        raise ValueError("App Frontier 状态迁移不受支持")
    if (
        isinstance(command.retry_delay_seconds, bool)
        or not isinstance(command.retry_delay_seconds, int)
        or command.retry_delay_seconds < 0
    ):
        raise ValueError("retry_delay_seconds 必须为非负整数")
    if (
        command.target_status is AppFrontierStatus.RETRY_WAIT
        and command.retry_delay_seconds <= 0
    ):
        raise ValueError("App Frontier 重试必须设置正数延迟")


def _parse_item(payload: Any) -> AppFrontierItem:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, Mapping):
        raise AppFrontierPersistenceError("candidate_payload 必须为对象")
    return AppFrontierItem.from_dict(payload)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _frontier_max_attempts_for_item(item: AppFrontierItem) -> int:
    """底栏 sibling 多给几次领取预算，与恢复侧 BOTTOM_NAV_ATTEMPT_LIMIT 对齐。"""

    from services.api.app.application.app_frontier_restoration import (
        APP_FRONTIER_BOTTOM_NAV_ATTEMPT_LIMIT,
    )

    metadata = item.candidate.metadata
    if metadata.get("bottom_navigation_tab_seek") is True:
        return APP_FRONTIER_BOTTOM_NAV_ATTEMPT_LIMIT
    terms = str(item.candidate.semantic_target or "").casefold()
    if metadata.get("frontier_scope") == "app" and any(
        marker in terms for marker in ("底栏", "底部tab", "底部导航")
    ):
        return APP_FRONTIER_BOTTOM_NAV_ATTEMPT_LIMIT
    return 3
