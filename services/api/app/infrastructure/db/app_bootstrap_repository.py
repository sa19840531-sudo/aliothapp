"""App Workflow Bootstrap 检查点的 PostgreSQL 实现。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.api.app.application.app_bootstrap_state import (
    CancelAppBootstrapCommand,
    FailAppBootstrapCommand,
    RenewAppBootstrapLeaseCommand,
    TransitionAppBootstrapCommand,
)
from services.api.app.domain.interaction import (
    AppBootstrapSnapshot,
    AppBootstrapStage,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class AppBootstrapPersistenceError(RuntimeError):
    """Bootstrap 归属、阶段、版本或 Workflow 租约不满足写入条件。"""


class PostgresAppBootstrapRepository:
    """以 PostgreSQL 乐观锁和 Workflow 租约串行化 App 启动流程。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def get_or_create(
        self,
        *,
        task_id: str,
        session_id: str,
        platform_build_ref: str,
        lease_owner: str,
        lease_token: str,
        lease_seconds: int,
    ) -> AppBootstrapSnapshot:
        self._validate_lease_seconds(lease_seconds)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                -- asyncpg：同一参数写入 varchar 又与 text 字面量/未知类型比较会 AmbiguousParameter；显式转型。
                INSERT INTO qc_app_bootstrap AS existing (
                    session_id, task_id, platform_build_ref, stage,
                    workflow_lease_owner, workflow_lease_token,
                    workflow_lease_expires_at
                )
                SELECT
                    session.session_id, session.task_id, $3::varchar,
                    'validating', $4::varchar, $5::uuid,
                    now() + make_interval(secs => $6::integer)
                FROM qc_surface_session AS session
                INNER JOIN qc_task AS task ON task.id = session.task_id
                WHERE session.task_id = $1::uuid
                  AND session.session_id = $2::varchar
                  AND session.surface_kind = 'android'
                  AND session.platform_build_ref = $3::varchar
                  AND session.status IN ('starting', 'active')
                  AND task.target_platform = 'app'
                  AND task.status IN ('queued', 'running')
                  AND task.finalization_state = 'open'
                ON CONFLICT (session_id) DO UPDATE
                SET
                    workflow_lease_owner = EXCLUDED.workflow_lease_owner,
                    workflow_lease_token = EXCLUDED.workflow_lease_token,
                    workflow_lease_expires_at = EXCLUDED.workflow_lease_expires_at,
                    updated_at = now()
                WHERE existing.task_id = EXCLUDED.task_id
                  AND existing.platform_build_ref = EXCLUDED.platform_build_ref
                  AND existing.stage NOT IN ('app_ready', 'failed', 'canceled')
                  AND (
                    (
                      existing.workflow_lease_owner = $4::varchar
                      AND existing.workflow_lease_token = $5::uuid
                    )
                    OR existing.workflow_lease_expires_at IS NULL
                    OR existing.workflow_lease_expires_at <= now()
                  )
                RETURNING existing.*
                """,
                task_id,
                session_id,
                platform_build_ref,
                lease_owner,
                lease_token,
                lease_seconds,
            )
        if row is None:
            raise AppBootstrapPersistenceError(
                "App Bootstrap 归属无效、已终结或正在被其他 Workflow 租用"
            )
        return _parse_bootstrap(row)

    async def get(self, *, session_id: str) -> AppBootstrapSnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM qc_app_bootstrap WHERE session_id = $1",
                session_id,
            )
        return _parse_bootstrap(row) if row is not None else None

    async def renew(
        self,
        command: RenewAppBootstrapLeaseCommand,
    ) -> AppBootstrapSnapshot:
        if not isinstance(command, RenewAppBootstrapLeaseCommand):
            raise TypeError("command 必须为 RenewAppBootstrapLeaseCommand")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_app_bootstrap AS bootstrap
                SET
                    workflow_lease_expires_at = now() + make_interval(secs => $6::integer),
                    updated_at = now()
                WHERE bootstrap.task_id = $1::uuid
                  AND bootstrap.session_id = $2::varchar
                  AND bootstrap.platform_build_ref = $3::varchar
                  AND bootstrap.workflow_lease_owner = $4::varchar
                  AND bootstrap.workflow_lease_token = $5::uuid
                  AND bootstrap.workflow_lease_expires_at > now()
                  AND bootstrap.stage NOT IN ('app_ready', 'failed', 'canceled')
                RETURNING bootstrap.*
                """,
                command.task_id,
                command.session_id,
                command.platform_build_ref,
                command.lease_owner,
                command.lease_token,
                command.lease_seconds,
            )
        if row is None:
            raise AppBootstrapPersistenceError("App Bootstrap Workflow 租约已失效")
        return _parse_bootstrap(row)

    async def transition(
        self,
        command: TransitionAppBootstrapCommand,
    ) -> AppBootstrapSnapshot:
        if not isinstance(command, TransitionAppBootstrapCommand):
            raise TypeError("command 必须为 TransitionAppBootstrapCommand")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_app_bootstrap AS bootstrap
                SET
                    stage = $5::varchar,
                    install_request_id = COALESCE($9::varchar, bootstrap.install_request_id),
                    install_attempt = COALESCE($10::integer, bootstrap.install_attempt),
                    installed_package = COALESCE($11::varchar, bootstrap.installed_package),
                    open_request_id = COALESCE($12::varchar, bootstrap.open_request_id),
                    open_attempt = COALESCE($13::integer, bootstrap.open_attempt),
                    row_version = bootstrap.row_version + 1,
                    updated_at = now()
                WHERE bootstrap.task_id = $1::uuid
                  AND bootstrap.session_id = $2::varchar
                  AND bootstrap.platform_build_ref = $3::varchar
                  AND bootstrap.stage = $4::varchar
                  AND bootstrap.row_version = $6::bigint
                  AND bootstrap.workflow_lease_owner = $7::varchar
                  AND bootstrap.workflow_lease_token = $8::uuid
                  AND bootstrap.workflow_lease_expires_at > now()
                  AND (
                    $9::varchar IS NULL
                    OR bootstrap.install_request_id IS NULL
                    OR bootstrap.install_request_id = $9::varchar
                    OR (bootstrap.stage = 'installing' AND $5::varchar = 'installed')
                  )
                  AND ($10::integer IS NULL OR $10::integer >= bootstrap.install_attempt)
                  AND (
                    $11::varchar IS NULL
                    OR bootstrap.installed_package IS NULL
                    OR bootstrap.installed_package = $11::varchar
                  )
                  AND (
                    $12::varchar IS NULL
                    OR bootstrap.open_request_id IS NULL
                    OR bootstrap.open_request_id = $12::varchar
                    OR (bootstrap.stage = 'opening' AND $5::varchar = 'opened')
                  )
                  AND ($13::integer IS NULL OR $13::integer >= bootstrap.open_attempt)
                RETURNING bootstrap.*
                """,
                command.task_id,
                command.session_id,
                command.platform_build_ref,
                command.expected_stage.value,
                command.target_stage.value,
                command.expected_row_version,
                command.lease_owner,
                command.lease_token,
                command.install_request_id,
                command.install_attempt,
                command.installed_package,
                command.open_request_id,
                command.open_attempt,
            )
        if row is None:
            raise AppBootstrapPersistenceError(
                "App Bootstrap 阶段、版本或 Workflow 租约已变化"
            )
        return _parse_bootstrap(row)

    async def cancel(self, command: CancelAppBootstrapCommand) -> AppBootstrapSnapshot:
        if not isinstance(command, CancelAppBootstrapCommand):
            raise TypeError("command 必须为 CancelAppBootstrapCommand")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_app_bootstrap AS bootstrap
                SET
                    stage = 'canceled',
                    workflow_lease_owner = NULL,
                    workflow_lease_token = NULL,
                    workflow_lease_expires_at = NULL,
                    row_version = bootstrap.row_version + 1,
                    updated_at = now()
                WHERE bootstrap.task_id = $1::uuid
                  AND bootstrap.session_id = $2::varchar
                  AND bootstrap.platform_build_ref = $3::varchar
                  AND bootstrap.stage = $4::varchar
                  AND bootstrap.row_version = $5::bigint
                  AND bootstrap.workflow_lease_owner = $6::varchar
                  AND bootstrap.workflow_lease_token = $7::uuid
                  AND bootstrap.workflow_lease_expires_at > now()
                RETURNING bootstrap.*
                """,
                command.task_id,
                command.session_id,
                command.platform_build_ref,
                command.expected_stage.value,
                command.expected_row_version,
                command.lease_owner,
                command.lease_token,
            )
        if row is None:
            raise AppBootstrapPersistenceError(
                "App Bootstrap 阶段、版本或 Workflow 租约已变化"
            )
        return _parse_bootstrap(row)

    async def fail(self, command: FailAppBootstrapCommand) -> AppBootstrapSnapshot:
        if not isinstance(command, FailAppBootstrapCommand):
            raise TypeError("command 必须为 FailAppBootstrapCommand")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_app_bootstrap AS bootstrap
                SET
                    stage = 'failed',
                    workflow_lease_owner = NULL,
                    workflow_lease_token = NULL,
                    workflow_lease_expires_at = NULL,
                    error_code = $8::varchar,
                    error_message = $9::text,
                    failed_at = now(),
                    row_version = bootstrap.row_version + 1,
                    updated_at = now()
                WHERE bootstrap.task_id = $1::uuid
                  AND bootstrap.session_id = $2::varchar
                  AND bootstrap.platform_build_ref = $3::varchar
                  AND bootstrap.stage = $4::varchar
                  AND bootstrap.row_version = $5::bigint
                  AND bootstrap.workflow_lease_owner = $6::varchar
                  AND bootstrap.workflow_lease_token = $7::uuid
                  AND bootstrap.workflow_lease_expires_at > now()
                RETURNING bootstrap.*
                """,
                command.task_id,
                command.session_id,
                command.platform_build_ref,
                command.expected_stage.value,
                command.expected_row_version,
                command.lease_owner,
                command.lease_token,
                command.error_code,
                command.error_message,
            )
        if row is None:
            raise AppBootstrapPersistenceError(
                "App Bootstrap 阶段、版本或 Workflow 租约已变化"
            )
        return _parse_bootstrap(row)

    @staticmethod
    def _validate_lease_seconds(lease_seconds: int) -> None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds 必须大于 0")

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_bootstrap(row: Mapping[str, Any]) -> AppBootstrapSnapshot:
    return AppBootstrapSnapshot(
        task_id=str(row["task_id"]),
        session_id=str(row["session_id"]),
        platform_build_ref=str(row["platform_build_ref"]),
        stage=AppBootstrapStage(str(row["stage"])),
        install_request_id=row["install_request_id"],
        install_attempt=int(row["install_attempt"]),
        installed_package=row["installed_package"],
        open_request_id=row["open_request_id"],
        open_attempt=int(row["open_attempt"]),
        bootstrap_capture_id=(
            str(row["bootstrap_capture_id"])
            if row["bootstrap_capture_id"] is not None
            else None
        ),
        error_code=row["error_code"],
        error_message=row["error_message"],
        row_version=int(row["row_version"]),
        started_at=row["started_at"],
        ready_at=row["ready_at"],
        failed_at=row["failed_at"],
        updated_at=row["updated_at"],
    )
