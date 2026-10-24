"""Surface Session、数据库命令租约与 Surface Registry 的 PostgreSQL 实现。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.api.app.application.app_bootstrap_state import APP_READY_MESSAGE_TYPE
from services.api.app.application.surface_sessions import (
    AppExplorationPendingFacts,
    SessionCloseReadiness,
    SessionCommandPermit,
    SessionDrainPendingError,
    SessionStateConflictError,
)
from services.api.app.domain.interaction import (
    SURFACE_SESSION_PROTOCOL_VERSION,
    AppBootstrapStage,
    AppReadyHandoff,
    SurfaceKind,
    SurfaceSessionSnapshot,
    SurfaceSessionSpec,
    SurfaceSessionStatus,
)
from services.api.app.domain.interaction.surface_driver import (
    app_primary_surface_ref,
    web_primary_surface_ref,
)
from services.api.app.domain.protocol import build_protocol_envelope
from services.api.app.infrastructure.db.h5_snapshot_recovery_scope import (
    h5_snapshot_session_permission_sql,
)
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.pool import get_postgres_pool


class SurfaceSessionPersistenceError(RuntimeError):
    """会话归属、租约、版本或关闭顺序不满足约束。"""


class SurfaceSessionDrainPendingError(SessionDrainPendingError, SurfaceSessionPersistenceError):
    """保留仓储错误兼容性，同时向上游表达可等待的排空阻塞。"""


class SurfaceSessionStateConflictError(SessionStateConflictError, SurfaceSessionPersistenceError):
    """保留仓储错误兼容性，同时明确会话状态 CAS 竞争。"""


class PostgresSurfaceSessionRepository:
    """以 PostgreSQL 为会话、命令顺序和逻辑 Surface 的唯一事实源。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def record_browser_stop(self, *, session_id: str, proof: dict) -> None:
        from services.api.app.infrastructure.db.h5_browser_stop_receipt import record_browser_stop

        await record_browser_stop(await self._get_pool(), session_id=session_id, proof=proof)

    async def read_browser_stop(self, *, session_id: str) -> dict | None:
        from services.api.app.infrastructure.db.h5_browser_stop_receipt import read_browser_stop

        return await read_browser_stop(await self._get_pool(), session_id=session_id)

    async def register_active(
        self,
        *,
        spec: SurfaceSessionSpec,
        session_owner: str,
        primary_surface_ref: str,
    ) -> SurfaceSessionSnapshot:
        if not isinstance(spec, SurfaceSessionSpec):
            raise TypeError("spec 必须为 SurfaceSessionSpec")
        _require_text(session_owner, "session_owner")
        _require_text(primary_surface_ref, "primary_surface_ref")
        if spec.surface_kind is not SurfaceKind.WEB:
            if session_owner != "app-workflow" or spec.platform_build_ref is None:
                raise SurfaceSessionPersistenceError(
                    "App Active Session 必须复用 app-workflow 与 APK Build 身份"
                )
            if primary_surface_ref != app_primary_surface_ref(spec.session_id):
                raise SurfaceSessionPersistenceError("App Active Session 主 Surface 身份不稳定")
        elif primary_surface_ref != web_primary_surface_ref(spec.session_id):
            # 与 App 对等的身份校验：qc_surface_ref 上还有 UNIQUE(session_id, surface_key)，
            # 下面的 INSERT 只按主键幂等，身份漂移会绕过幂等分支直接撞唯一约束，
            # 抛出裸 duplicate key 让调用方无从判断。这里先拦下并说清原因。
            raise SurfaceSessionPersistenceError("Web Active Session 主 Surface 身份不稳定")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{spec.task_id}",
            )
            session_row = await connection.fetchrow(
                """
                INSERT INTO qc_surface_session AS existing (
                    session_id, task_id, surface_kind, session_owner, status,
                    account_environment_ref, platform_build_ref, device_ref,
                    device_lease_id, device_lease_fencing_token,
                    finalization_epoch, activated_at
                )
                SELECT
                    $1::text, task.id, $3::text, $4::text, 'active',
                    $5, $6, $7, $8::uuid, $9::bigint,
                    task.finalization_epoch, now()
                FROM qc_task AS task
                WHERE task.id = $2::uuid
                  AND (
                      (task.status IN ('queued', 'running') AND task.finalization_state = 'open'
                       AND $4::text NOT LIKE 'h5-snapshot-recapture:%'
                       AND $1::text NOT LIKE 'h5:%:recapture:%')
                      OR __H5_SNAPSHOT_SESSION_PERMISSION__
                  )
                ON CONFLICT (session_id) DO UPDATE
                SET session_id = existing.session_id
                WHERE existing.task_id = EXCLUDED.task_id
                  AND existing.surface_kind = EXCLUDED.surface_kind
                  AND existing.session_owner = EXCLUDED.session_owner
                  AND existing.account_environment_ref
                        IS NOT DISTINCT FROM EXCLUDED.account_environment_ref
                  AND existing.platform_build_ref
                        IS NOT DISTINCT FROM EXCLUDED.platform_build_ref
                  AND existing.device_ref IS NOT DISTINCT FROM EXCLUDED.device_ref
                  AND existing.device_lease_id IS NOT DISTINCT FROM EXCLUDED.device_lease_id
                  AND existing.device_lease_fencing_token IS NOT DISTINCT FROM
                      EXCLUDED.device_lease_fencing_token
                  AND existing.status IN ('active', 'draining')
                RETURNING existing.*
                """.replace("__H5_SNAPSHOT_SESSION_PERMISSION__",
                    h5_snapshot_session_permission_sql(
                        session_id="$1::text", owner="$4::text", kind="$3::text")),
                spec.session_id,
                spec.task_id,
                spec.surface_kind.value,
                session_owner,
                spec.account_environment_ref,
                spec.platform_build_ref,
                spec.device_ref,
                spec.device_lease_id,
                spec.device_lease_fencing_token,
            )
            if session_row is None:
                raise SurfaceSessionPersistenceError(
                    "Surface Session 归属无效、任务不可运行或幂等身份漂移"
                )
            surface_row = await connection.fetchrow(
                """
                INSERT INTO qc_surface_ref AS existing (
                    surface_ref_id, session_id, task_id, surface_kind,
                    surface_key, status
                )
                VALUES ($1, $2, $3::uuid, $4, $5, 'active')
                ON CONFLICT (surface_ref_id) DO UPDATE
                SET surface_ref_id = existing.surface_ref_id
                WHERE existing.session_id = EXCLUDED.session_id
                  AND existing.task_id = EXCLUDED.task_id
                  AND existing.surface_kind = EXCLUDED.surface_kind
                  AND existing.surface_key = EXCLUDED.surface_key
                  AND existing.status IN ('active', 'pinned', 'draining')
                RETURNING existing.surface_ref_id
                """,
                primary_surface_ref,
                spec.session_id,
                spec.task_id,
                spec.surface_kind.value,
                "primary",
            )
            if surface_row is None:
                raise SurfaceSessionPersistenceError("主 Surface 幂等身份发生漂移")
            session_row = await connection.fetchrow(
                """
                UPDATE qc_surface_session
                SET
                    current_surface_ref = COALESCE(current_surface_ref, $3),
                    updated_at = now()
                WHERE session_id = $1
                  AND task_id = $2::uuid
                  AND (current_surface_ref IS NULL OR current_surface_ref = $3)
                RETURNING *
                """,
                spec.session_id,
                spec.task_id,
                primary_surface_ref,
            )
        if session_row is None:
            raise SurfaceSessionPersistenceError("主 Surface 与已有会话身份不一致")
        return _parse_session(session_row)

    async def register_starting(
        self,
        *,
        spec: SurfaceSessionSpec,
        session_owner: str,
        primary_surface_ref: str,
    ) -> SurfaceSessionSnapshot:
        """为 Android Bootstrap 注册不可执行普通命令的 Starting Session。"""
        if not isinstance(spec, SurfaceSessionSpec):
            raise TypeError("spec 必须为 SurfaceSessionSpec")
        if spec.surface_kind is not SurfaceKind.ANDROID:
            raise SurfaceSessionPersistenceError("App V1 只允许 Android Starting Session")
        if spec.platform_build_ref is None:
            raise SurfaceSessionPersistenceError("Android Starting Session 必须绑定 APK Build")
        _require_text(session_owner, "session_owner")
        _require_text(primary_surface_ref, "primary_surface_ref")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{spec.task_id}",
            )
            session_row = await connection.fetchrow(
                """
                INSERT INTO qc_surface_session AS existing (
                    session_id, task_id, surface_kind, session_owner, status,
                    account_environment_ref, platform_build_ref, device_ref,
                    device_lease_id, device_lease_fencing_token,
                    finalization_epoch
                )
                SELECT
                    $1, task.id, $3, $4, 'starting', $5, $6, $7, $8::uuid, $9::bigint,
                    task.finalization_epoch
                FROM qc_task AS task
                WHERE task.id = $2::uuid
                  AND task.target_platform = 'app'
                  AND task.status IN ('queued', 'running')
                  AND task.finalization_state = 'open'
                ON CONFLICT (session_id) DO UPDATE
                SET session_id = existing.session_id
                WHERE existing.task_id = EXCLUDED.task_id
                  AND existing.surface_kind = EXCLUDED.surface_kind
                  AND existing.session_owner = EXCLUDED.session_owner
                  AND existing.account_environment_ref
                        IS NOT DISTINCT FROM EXCLUDED.account_environment_ref
                  AND existing.platform_build_ref = EXCLUDED.platform_build_ref
                  AND existing.device_ref IS NOT DISTINCT FROM EXCLUDED.device_ref
                  AND existing.device_lease_id IS NOT DISTINCT FROM EXCLUDED.device_lease_id
                  AND existing.device_lease_fencing_token IS NOT DISTINCT FROM
                      EXCLUDED.device_lease_fencing_token
                  AND existing.status IN ('starting', 'active')
                RETURNING existing.*
                """,
                spec.session_id,
                spec.task_id,
                spec.surface_kind.value,
                session_owner,
                spec.account_environment_ref,
                spec.platform_build_ref,
                spec.device_ref,
                spec.device_lease_id,
                spec.device_lease_fencing_token,
            )
            if session_row is None:
                raise SurfaceSessionPersistenceError(
                    "Starting Session 归属无效、任务不可运行或幂等身份漂移"
                )
            surface_row = await connection.fetchrow(
                """
                INSERT INTO qc_surface_ref AS existing (
                    surface_ref_id, session_id, task_id, surface_kind,
                    surface_key, status
                )
                VALUES ($1, $2, $3::uuid, $4, $5, 'active')
                ON CONFLICT (surface_ref_id) DO UPDATE
                SET surface_ref_id = existing.surface_ref_id
                WHERE existing.session_id = EXCLUDED.session_id
                  AND existing.task_id = EXCLUDED.task_id
                  AND existing.surface_kind = EXCLUDED.surface_kind
                  AND existing.surface_key = EXCLUDED.surface_key
                  AND existing.status IN ('active', 'pinned')
                RETURNING existing.surface_ref_id
                """,
                primary_surface_ref,
                spec.session_id,
                spec.task_id,
                spec.surface_kind.value,
                "primary",
            )
            if surface_row is None:
                raise SurfaceSessionPersistenceError("App 主 Surface 幂等身份发生漂移")
            session_row = await connection.fetchrow(
                """
                UPDATE qc_surface_session
                SET
                    current_surface_ref = COALESCE(current_surface_ref, $3),
                    updated_at = now()
                WHERE session_id = $1
                  AND task_id = $2::uuid
                  AND (current_surface_ref IS NULL OR current_surface_ref = $3)
                RETURNING *
                """,
                spec.session_id,
                spec.task_id,
                primary_surface_ref,
            )
        if session_row is None:
            raise SurfaceSessionPersistenceError("App 主 Surface 与已有会话身份不一致")
        return _parse_session(session_row)

    async def activate_from_bootstrap(
        self,
        *,
        handoff: AppReadyHandoff,
        expected_bootstrap_stage: AppBootstrapStage,
        expected_bootstrap_row_version: int,
        expected_session_row_version: int,
        workflow_lease_owner: str,
        workflow_lease_token: str,
    ) -> SurfaceSessionSnapshot:
        """原子提交 APP_READY、激活 Session 并追加引用型 Outbox。"""
        if not isinstance(handoff, AppReadyHandoff):
            raise TypeError("handoff 必须为 AppReadyHandoff")
        if expected_bootstrap_stage is not AppBootstrapStage.BOOTSTRAP_CAPTURING:
            raise ValueError("只有 BOOTSTRAP_CAPTURING 可以激活 App Session")
        for field_name, value in (
            ("expected_bootstrap_row_version", expected_bootstrap_row_version),
            ("expected_session_row_version", expected_session_row_version),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        if handoff.bootstrap_row_version != expected_bootstrap_row_version + 1:
            raise ValueError("handoff.bootstrap_row_version 必须是提交后的检查点版本")
        _require_text(workflow_lease_owner, "workflow_lease_owner")
        _require_text(workflow_lease_token, "workflow_lease_token")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            bootstrap_row = await connection.fetchrow(
                """
                UPDATE qc_app_bootstrap AS bootstrap
                SET
                    stage = 'app_ready',
                    bootstrap_capture_id = $3::uuid,
                    ready_at = now(),
                    workflow_lease_owner = NULL,
                    workflow_lease_token = NULL,
                    workflow_lease_expires_at = NULL,
                    row_version = bootstrap.row_version + 1,
                    updated_at = now()
                WHERE bootstrap.task_id = $1::uuid
                  AND bootstrap.session_id = $2
                  AND bootstrap.stage = $4
                  AND bootstrap.bootstrap_capture_id = $3::uuid
                  AND bootstrap.row_version = $5
                  AND bootstrap.workflow_lease_owner = $6
                  AND bootstrap.workflow_lease_token = $7::uuid
                  AND bootstrap.workflow_lease_expires_at > now()
                  AND EXISTS (
                    SELECT 1
                    FROM qc_capture AS capture
                    INNER JOIN qc_app_capture AS app_capture
                      ON app_capture.capture_id = capture.id
                    WHERE capture.id = $3::uuid
                      AND capture.task_id = bootstrap.task_id
                      AND capture.surface_session_id = bootstrap.session_id
                      AND capture.surface_kind = 'android'
                      AND capture.capture_role = 'bootstrap'
                      AND capture.capture_status = 'captured'
                  )
                RETURNING bootstrap.ready_at
                """,
                handoff.task_id,
                handoff.session_id,
                handoff.capture.capture_id,
                expected_bootstrap_stage.value,
                expected_bootstrap_row_version,
                workflow_lease_owner,
                workflow_lease_token,
            )
            if bootstrap_row is None:
                raise SurfaceSessionPersistenceError(
                    "App Bootstrap Capture、阶段、版本或 Workflow 租约已变化"
                )
            session_row = await connection.fetchrow(
                """
                UPDATE qc_surface_session AS session
                SET
                    status = 'active',
                    current_capture_id = $3::uuid,
                    activated_at = now(),
                    row_version = session.row_version + 1,
                    updated_at = now()
                WHERE session.task_id = $1::uuid
                  AND session.session_id = $2
                  AND session.surface_kind = 'android'
                  AND session.status = 'starting'
                  AND session.row_version = $4
                  AND session.lease_token IS NULL
                  AND EXISTS (
                    SELECT 1
                    FROM qc_app_bootstrap AS bootstrap
                    WHERE bootstrap.task_id = session.task_id
                      AND bootstrap.session_id = session.session_id
                      AND bootstrap.stage = 'app_ready'
                      AND bootstrap.bootstrap_capture_id = $3::uuid
                  )
                RETURNING session.*
                """,
                handoff.task_id,
                handoff.session_id,
                handoff.capture.capture_id,
                expected_session_row_version,
            )
            if session_row is None:
                raise SurfaceSessionPersistenceError(
                    "Starting Session 状态、版本或 Bootstrap 归属已变化"
                )
            surface_ref_id = await connection.fetchval(
                """
                UPDATE qc_surface_ref AS surface
                SET
                    current_capture_id = $3::uuid,
                    row_version = surface.row_version + 1,
                    updated_at = now()
                FROM qc_surface_session AS session
                WHERE surface.task_id = $1::uuid
                  AND surface.session_id = $2
                  AND surface.surface_ref_id = session.current_surface_ref
                  AND session.task_id = surface.task_id
                  AND session.session_id = surface.session_id
                RETURNING surface.surface_ref_id
                """,
                handoff.task_id,
                handoff.session_id,
                handoff.capture.capture_id,
            )
            if surface_ref_id is None:
                raise SurfaceSessionPersistenceError("App 主 Surface 不存在或归属已变化")
            envelope = build_protocol_envelope(
                message_type=APP_READY_MESSAGE_TYPE,
                payload_protocol_version=handoff.protocol_version,
                idempotency_key=(f"app-ready:{handoff.session_id}:{handoff.capture.capture_id}"),
                task_id=handoff.task_id,
                aggregate_type="app_bootstrap",
                aggregate_id=handoff.session_id,
                aggregate_version=handoff.bootstrap_row_version,
                occurred_at=bootstrap_row["ready_at"],
                payload=handoff.to_dict(),
                correlation_id=handoff.task_id,
            )
            await insert_outbox_event(
                connection,
                aggregate_type="app_bootstrap",
                aggregate_id=handoff.session_id,
                envelope=envelope,
            )
        return _parse_session(session_row)

    async def get(self, *, session_id: str) -> SurfaceSessionSnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM qc_surface_session WHERE session_id = $1",
                session_id,
            )
        return _parse_session(row) if row is not None else None

    async def acquire_command(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind,
        lease_owner: str,
        lease_token: str,
        lease_seconds: int,
        allow_draining: bool,
    ) -> SessionCommandPermit:
        if not isinstance(surface_kind, SurfaceKind):
            raise TypeError("surface_kind 必须为 SurfaceKind")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        _require_text(lease_owner, "lease_owner")
        _require_text(lease_token, "lease_token")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_surface_session AS session
                SET
                    lease_owner = $4,
                    lease_token = $5::uuid,
                    lease_expires_at = now() + make_interval(secs => $6),
                    command_sequence = session.command_sequence + 1,
                    row_version = session.row_version + 1,
                    updated_at = now()
                WHERE session.session_id = $2
                  AND session.task_id = $1::uuid
                  AND session.surface_kind = $3
                  AND (
                    session.status = 'active'
                    OR ($7::boolean AND session.status = 'draining')
                  )
                  AND (
                    session.lease_token IS NULL
                    OR session.lease_expires_at <= now()
                  )
                  AND (
                    session.surface_kind <> 'android'
                    OR (
                        session.device_lease_id IS NULL
                        OR EXISTS (
                            SELECT 1
                            FROM qc_device_lease AS device_lease
                            WHERE device_lease.lease_id = session.device_lease_id
                              AND device_lease.fencing_token =
                                  session.device_lease_fencing_token
                              AND device_lease.state IN ('claimed', 'active')
                              AND device_lease.expires_at > now()
                        )
                    )
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM qc_task AS task
                      WHERE task.id = session.task_id
                        AND session.finalization_epoch = task.finalization_epoch
                        AND (
                            (task.status = 'running'
                             AND session.session_owner NOT LIKE 'h5-snapshot-recapture:%'
                             AND session.session_id NOT LIKE 'h5:%:recapture:%' AND (
                                task.finalization_state = 'open'
                                OR ($7::boolean AND task.finalization_state IN (
                                    'exploration_draining', 'session_draining')
                                )
                            ))
                            OR ($7::boolean AND __H5_SNAPSHOT_SESSION_PERMISSION__)
                        )
                  )
                RETURNING
                    session.task_id::text AS task_id,
                    session.session_id,
                    session.surface_kind,
                    session.lease_owner,
                    session.lease_token::text AS lease_token,
                    session.command_sequence,
                    session.finalization_epoch,
                    session.lease_expires_at,
                    session.current_capture_id
                """.replace("__H5_SNAPSHOT_SESSION_PERMISSION__",
                    h5_snapshot_session_permission_sql()),
                task_id,
                session_id,
                surface_kind.value,
                lease_owner,
                lease_token,
                lease_seconds,
                allow_draining,
            )
        if row is None:
            raise SurfaceSessionPersistenceError(
                "Surface Session 不可执行、正在排空或已被其他命令租用"
            )
        return _parse_permit(row)

    async def renew_command(
        self,
        *,
        permit: SessionCommandPermit,
        lease_seconds: int,
    ) -> SessionCommandPermit:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_surface_session
                SET
                    lease_expires_at = now() + make_interval(secs => $6),
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND session_id = $2
                  AND surface_kind = $3
                  AND lease_owner = $4
                  AND lease_token = $5::uuid
                  AND command_sequence = $7
                  AND finalization_epoch = $8
                  AND lease_expires_at > now()
                  AND (
                    surface_kind <> 'android'
                    OR (
                        device_lease_id IS NULL
                        OR EXISTS (
                            SELECT 1
                            FROM qc_device_lease AS device_lease
                            WHERE device_lease.lease_id = qc_surface_session.device_lease_id
                              AND device_lease.fencing_token =
                                  qc_surface_session.device_lease_fencing_token
                              AND device_lease.state IN ('claimed', 'active')
                              AND device_lease.expires_at > now()
                        )
                    )
                  )
                RETURNING
                    task_id::text AS task_id,
                    session_id,
                    surface_kind,
                    lease_owner,
                    lease_token::text AS lease_token,
                    command_sequence,
                    finalization_epoch,
                    lease_expires_at
                """,
                permit.task_id,
                permit.session_id,
                permit.surface_kind.value,
                permit.lease_owner,
                permit.lease_token,
                lease_seconds,
                permit.command_sequence,
                permit.finalization_epoch,
            )
        if row is None:
            raise SurfaceSessionPersistenceError("Surface Session 命令租约已失效")
        return _parse_permit(row)

    async def release_command(
        self,
        *,
        permit: SessionCommandPermit,
        current_capture_id: str | None,
    ) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            released_id = await connection.fetchval(
                """
                UPDATE qc_surface_session AS session
                SET
                    current_capture_id = COALESCE(capture.id, session.current_capture_id),
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    row_version = session.row_version + 1,
                    updated_at = now()
                FROM (SELECT 1) AS singleton
                LEFT JOIN qc_capture AS capture
                    ON capture.id = $8::uuid
                   AND capture.task_id = $1::uuid
                   AND capture.surface_session_id = $2
                   AND capture.session_command_sequence = $6
                WHERE session.task_id = $1::uuid
                  AND session.session_id = $2
                  AND session.surface_kind = $3
                  AND session.lease_owner = $4
                  AND session.lease_token = $5::uuid
                  AND session.command_sequence = $6
                  AND session.finalization_epoch = $7
                  AND (
                    session.surface_kind <> 'android'
                    OR session.device_lease_id IS NULL
                    OR EXISTS (
                        SELECT 1
                        FROM qc_device_lease AS device_lease
                        WHERE device_lease.lease_id = session.device_lease_id
                          AND device_lease.fencing_token =
                              session.device_lease_fencing_token
                          AND device_lease.state IN ('claimed', 'active', 'releasing')
                    )
                  )
                  AND (
                    $8::uuid IS NULL
                    OR capture.id IS NOT NULL
                  )
                RETURNING session.session_id
                """,
                permit.task_id,
                permit.session_id,
                permit.surface_kind.value,
                permit.lease_owner,
                permit.lease_token,
                permit.command_sequence,
                permit.finalization_epoch,
                current_capture_id,
            )
        return released_id is not None

    async def begin_draining(
        self,
        *,
        task_id: str,
        session_id: str,
        expected_row_version: int,
    ) -> SurfaceSessionSnapshot:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_surface_session
                SET
                    status = 'draining',
                    draining_at = COALESCE(draining_at, now()),
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND session_id = $2
                  AND status IN ('active', 'draining')
                  AND row_version = $3
                  AND lease_token IS NULL
                  AND alioth_h5_journey_available(session_id)
                RETURNING *
                """,
                task_id,
                session_id,
                expected_row_version,
            )
            if row is not None:
                await connection.execute(
                    """
                    UPDATE qc_surface_ref
                    SET
                        status = 'draining',
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE task_id = $1::uuid
                      AND session_id = $2
                      AND status = 'active'
                    """,
                    task_id,
                    session_id,
                )
        if row is None:
            raise SurfaceSessionPersistenceError("会话状态、版本已变化或仍有动作租约")
        return _parse_session(row)

    async def close_readiness(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> SessionCloseReadiness:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _CLOSE_READINESS_SQL,
                task_id,
                session_id,
            )
        if row is None:
            raise SurfaceSessionPersistenceError("Surface Session 不存在或不属于当前任务")
        return _parse_close_readiness(row)

    async def exploration_pending_facts(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> AppExplorationPendingFacts:
        """读取活跃探索期的动作和证据阻断项。

        Manifest 在正式收口阶段才统一 finalize，因此这里只统计已开始但
        尚未形成 Capture 事实的动作、采集与活跃 Evidence Request。
        """
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _EXPLORATION_PENDING_FACTS_SQL,
                task_id,
                session_id,
            )
        if row is None:
            raise SurfaceSessionPersistenceError("Surface Session 不存在或不属于当前任务")
        return AppExplorationPendingFacts(
            task_id=task_id,
            session_id=str(row["session_id"]),
            pending_action_count=int(row["pending_action_count"]),
            pending_evidence_count=int(row["pending_evidence_count"]),
        )

    async def seal_for_close(
        self,
        *,
        task_id: str,
        session_id: str,
        expected_row_version: int,
    ) -> SurfaceSessionSnapshot:
        """在事务内重新检查排空状态并封闭旧 Session 命令入口。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            readiness_row = await connection.fetchrow(
                _CLOSE_READINESS_SQL + " FOR UPDATE OF session",
                task_id,
                session_id,
            )
            if readiness_row is None:
                raise SurfaceSessionPersistenceError("Surface Session 不存在或不属于当前任务")
            if readiness_row["row_version"] != expected_row_version:
                raise SurfaceSessionPersistenceError("会话状态或版本已变化")
            readiness = _parse_close_readiness(readiness_row)
            if not readiness.ready:
                raise SurfaceSessionDrainPendingError(task_id=task_id, readiness=readiness)
            row = await connection.fetchrow(
                """
                UPDATE qc_surface_session
                SET
                    status = 'closing',
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND session_id = $2
                  AND status = 'draining'
                  AND row_version = $3
                  AND lease_token IS NULL
                RETURNING *
                """,
                task_id,
                session_id,
                expected_row_version,
            )
        if row is None:
            raise SurfaceSessionPersistenceError("会话状态或版本已变化")
        return _parse_session(row)

    async def seal_exploration_for_close(
        self,
        *,
        task_id: str,
        session_id: str,
        expected_row_version: int,
    ) -> SurfaceSessionSnapshot:
        """纯探索关闭只等待运行时写入，不等待质检库存或规则工作。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            readiness_row = await connection.fetchrow(
                _EXPLORATION_CLOSE_READINESS_SQL + " FOR UPDATE OF session",
                task_id,
                session_id,
            )
            if readiness_row is None:
                raise SurfaceSessionPersistenceError("Surface Session 不存在或不属于当前任务")
            if readiness_row["row_version"] != expected_row_version:
                raise SurfaceSessionPersistenceError("会话状态或版本已变化")
            readiness = _parse_close_readiness(readiness_row)
            if not readiness.ready:
                raise SurfaceSessionDrainPendingError(task_id=task_id, readiness=readiness)
            row = await connection.fetchrow(
                """
                UPDATE qc_surface_session
                SET
                    status = 'closing',
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND session_id = $2
                  AND status = 'draining'
                  AND row_version = $3
                  AND lease_token IS NULL
                RETURNING *
                """,
                task_id,
                session_id,
                expected_row_version,
            )
        if row is None:
            raise SurfaceSessionPersistenceError("会话状态或版本已变化")
        return _parse_session(row)

    async def mark_closed(
        self,
        *,
        task_id: str,
        session_id: str,
        expected_row_version: int,
    ) -> SurfaceSessionSnapshot:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_surface_session
                SET
                    status = 'closed',
                    closed_at = now(),
                    current_surface_ref = NULL,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND session_id = $2
                  AND status = 'closing'
                  AND row_version = $3
                  AND lease_token IS NULL
                RETURNING *
                """,
                task_id,
                session_id,
                expected_row_version,
            )
            if row is not None:
                await connection.execute(
                    """
                    UPDATE qc_surface_ref
                    SET
                        status = 'released',
                        released_at = now(),
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE task_id = $1::uuid
                      AND session_id = $2
                      AND status = 'draining'
                    """,
                    task_id,
                    session_id,
                )
        if row is None:
            raise SurfaceSessionPersistenceError("会话状态或版本已变化")
        return _parse_session(row)

    async def mark_lost(
        self,
        *,
        task_id: str,
        session_id: str,
        expected_row_version: int,
        reason: str,
    ) -> SurfaceSessionSnapshot:
        _require_text(reason, "reason")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_surface_session
                SET
                    status = 'lost',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    current_surface_ref = NULL,
                    failure_reason = $4,
                    lost_at = now(),
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND session_id = $2
                  AND status IN ('starting', 'active', 'draining', 'closing')
                  AND row_version = $3
                RETURNING *
                """,
                task_id,
                session_id,
                expected_row_version,
                reason,
            )
            if row is not None:
                await connection.execute(
                    """
                    UPDATE qc_surface_ref
                    SET
                        status = 'lost',
                        pin_reason = NULL,
                        released_at = now(),
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE task_id = $1::uuid
                      AND session_id = $2
                      AND status NOT IN ('released', 'lost')
                    """,
                    task_id,
                    session_id,
                )
        if row is None:
            raise SurfaceSessionStateConflictError("会话状态或版本已变化")
        return _parse_session(row)

    async def mark_web_lost_if_command_lease_expired(
        self,
        *,
        task_id: str,
        session_id: str,
        reason: str,
    ) -> bool:
        """仅在 Web 命令租约已过期时原子失效 Session，供正式孤儿收口。"""

        _require_text(reason, "reason")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_surface_session
                SET
                    status = 'lost',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    current_surface_ref = NULL,
                    failure_reason = $3,
                    lost_at = now(),
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND session_id = $2
                  AND surface_kind = 'web'
                  AND status IN ('active', 'draining', 'closing')
                  AND lease_token IS NOT NULL
                  AND lease_expires_at <= now()
                RETURNING session_id
                """,
                task_id,
                session_id,
                reason,
            )
            if row is not None:
                await connection.execute(
                    """
                    UPDATE qc_surface_ref
                    SET
                        status = 'lost',
                        pin_reason = NULL,
                        released_at = now(),
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE task_id = $1::uuid
                      AND session_id = $2
                      AND status NOT IN ('released', 'lost')
                    """,
                    task_id,
                    session_id,
                )
        return row is not None

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


_CLOSE_READINESS_SQL = """
    SELECT
        session.session_id,
        session.row_version,
        session.status AS session_status,
        (session.lease_token IS NOT NULL) AS has_command_lease,
        NOT journey.available AS has_journey_lease,
        (
            session.status = 'draining'
            AND session.lease_token IS NULL
            AND journey.available
            AND counts.pending_actions = 0
            AND counts.pending_capture_commits = 0
            AND counts.active_evidence_requests = 0
            AND counts.running_restorations = 0
            AND counts.nonterminal_active_work_items = 0
            AND counts.nonterminal_app_frontier_items = 0
            AND counts.pinned_surfaces = 0
            AND counts.pending_manifests = 0
            AND counts.building_evidence_indexes = 0
        ) AS ready,
        counts.*
    FROM qc_surface_session AS session
    CROSS JOIN LATERAL (
        SELECT alioth_h5_journey_available(session.session_id) AS available
    ) AS journey
    CROSS JOIN LATERAL (
        SELECT
            (
                SELECT count(*)
                FROM qc_action_execution AS action
                WHERE action.task_id = session.task_id
                  AND action.session_id = session.session_id
                  AND action.status IN ('planned', 'executing', 'evidence_pending')
            )::integer AS pending_actions,
            (
                (
                    SELECT count(*)
                    FROM qc_action_execution AS action
                    WHERE action.task_id = session.task_id
                      AND action.session_id = session.session_id
                      AND action.status = 'evidence_pending'
                )
                + (
                    SELECT count(*)
                    FROM qc_capture AS capture
                    WHERE capture.task_id = session.task_id
                      AND capture.surface_session_id = session.session_id
                      AND capture.capture_status = 'pending'
                )
            )::integer AS pending_capture_commits,
            (
                SELECT count(*)
                FROM qc_evidence_request AS request
                INNER JOIN qc_capture AS capture
                    ON capture.id = request.target_capture_id
                   AND capture.task_id = request.task_id
                WHERE request.task_id = session.task_id
                  AND capture.surface_session_id = session.session_id
                  AND request.session_dependency = 'active_session'
                  AND request.status IN ('pending', 'routed', 'running')
            )::integer AS active_evidence_requests,
            (
                SELECT count(*)
                FROM qc_restoration_attempt AS restoration
                WHERE restoration.task_id = session.task_id
                  AND restoration.target_session_id = session.session_id
                  AND restoration.status = 'running'
            )::integer AS running_restorations,
            (
                (
                    SELECT count(*)
                    FROM qc_v2_work_item AS work_item
                    WHERE work_item.task_id = session.task_id
                      AND work_item.session_id = session.session_id
                      AND work_item.status NOT IN (
                        'succeeded', 'bounded', 'budget_exhausted', 'blocked',
                        'no_progress', 'failed', 'cancelled', 'superseded'
                      )
                )
                +
                (
                    SELECT count(*)
                    FROM qc_inspection_work_item AS inspection_work
                    INNER JOIN qc_capture AS inspection_capture
                        ON inspection_capture.id = inspection_work.page_capture_id
                       AND inspection_capture.task_id = inspection_work.task_id
                       AND inspection_capture.surface_session_id = session.session_id
                    WHERE inspection_work.task_id = session.task_id
                      AND inspection_work.session_dependency = 'active_session'
                      AND inspection_work.control_mode = 'enforced'
                      AND inspection_work.finalization_epoch = session.finalization_epoch
                      AND inspection_work.status NOT IN (
                        'succeeded', 'partial', 'failed',
                        'cancelled', 'superseded'
                      )
                )
            )::integer AS nonterminal_active_work_items,
            (
                SELECT count(*)
                FROM qc_app_exploration_frontier AS frontier
                WHERE frontier.task_id = session.task_id
                  AND frontier.surface_session_id = session.session_id
                  AND frontier.status IN ('pending', 'leased', 'running', 'retry_wait')
            )::integer AS nonterminal_app_frontier_items,
            (
                SELECT count(*)
                FROM qc_surface_ref AS surface
                WHERE surface.task_id = session.task_id
                  AND surface.session_id = session.session_id
                  AND surface.status = 'pinned'
            )::integer AS pinned_surfaces,
            (
                SELECT count(*)
                FROM qc_capture AS capture
                LEFT JOIN LATERAL (
                    SELECT manifest.quality_status
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.capture_id = capture.id
                    ORDER BY manifest.manifest_version DESC
                    LIMIT 1
                ) AS latest ON TRUE
                WHERE capture.task_id = session.task_id
                  AND capture.surface_session_id = session.session_id
                  AND capture.capture_status = 'captured'
                  -- 只拦「尚未裁决完」的清单：fail 是可审计终态，缺口由交接库存
                  -- 与人工复核收口；把终态也当阻塞会让任何一页裁决失败就永远关不掉会话。
                  AND (latest.quality_status IS NULL OR latest.quality_status = 'pending')
                  -- 只有承载判定需求的帧才需要裁决：无需求集的伴生帧（动作前基线帧等）
                  -- 按 E14 口径永远停在 pending（不伪造结论），不构成排空阻塞。
                  AND EXISTS (
                      SELECT 1
                      FROM qc_evidence_exit_requirement_set AS requirement
                      WHERE requirement.task_id = session.task_id
                        AND requirement.capture_id = capture.id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_app_evidence_index AS sealed_index
                      CROSS JOIN LATERAL jsonb_array_elements(
                          sealed_index.evidence_gaps
                      ) AS evidence_gap
                      WHERE sealed_index.task_id = session.task_id
                        AND sealed_index.surface_session_id = session.session_id
                        AND sealed_index.status = 'sealed'
                        AND sealed_index.outcome = 'evidence_insufficient'
                        AND evidence_gap->>'capture_id' = capture.id::text
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_h5_evidence_inventory_item AS h5_inventory
                      WHERE h5_inventory.task_id = session.task_id
                        AND h5_inventory.session_id = session.session_id
                        AND h5_inventory.status IN ('pass', 'insufficient', 'failed')
                        AND capture.id IN (
                            h5_inventory.before_capture_id,
                            h5_inventory.after_capture_id
                        )
                  )
            )::integer AS pending_manifests,
            (
                SELECT count(*)
                FROM qc_app_evidence_index AS evidence_index
                WHERE evidence_index.task_id = session.task_id
                  AND evidence_index.surface_session_id = session.session_id
                  AND evidence_index.status = 'building'
            )::integer AS building_evidence_indexes
    ) AS counts
    WHERE session.task_id = $1::uuid
      AND session.session_id = $2
"""


_EXPLORATION_CLOSE_READINESS_SQL = """
    SELECT
        session.session_id,
        session.row_version,
        session.status AS session_status,
        (session.lease_token IS NOT NULL) AS has_command_lease,
        NOT journey.available AS has_journey_lease,
        (
            session.status = 'draining'
            AND session.lease_token IS NULL
            AND journey.available
            AND counts.pending_actions = 0
            AND counts.pending_capture_commits = 0
            AND counts.running_restorations = 0
            AND counts.nonterminal_active_work_items = 0
            AND counts.nonterminal_app_frontier_items = 0
            AND counts.pinned_surfaces = 0
        ) AS ready,
        counts.pending_actions,
        counts.pending_capture_commits,
        0::integer AS active_evidence_requests,
        counts.running_restorations,
        counts.nonterminal_active_work_items,
        counts.nonterminal_app_frontier_items,
        counts.pinned_surfaces,
        0::integer AS pending_manifests,
        0::integer AS building_evidence_indexes
    FROM qc_surface_session AS session
    CROSS JOIN LATERAL (
        SELECT alioth_h5_journey_available(session.session_id) AS available
    ) AS journey
    CROSS JOIN LATERAL (
        SELECT
            (
                SELECT count(*)
                FROM qc_action_execution AS action
                WHERE action.task_id = session.task_id
                  AND action.session_id = session.session_id
                  AND action.status IN ('planned', 'executing', 'evidence_pending')
            )::integer AS pending_actions,
            (
                (
                    SELECT count(*)
                    FROM qc_action_execution AS action
                    WHERE action.task_id = session.task_id
                      AND action.session_id = session.session_id
                      AND action.status = 'evidence_pending'
                )
                + (
                    SELECT count(*)
                    FROM qc_capture AS capture
                    WHERE capture.task_id = session.task_id
                      AND capture.surface_session_id = session.session_id
                      AND capture.capture_status = 'pending'
                )
            )::integer AS pending_capture_commits,
            (
                SELECT count(*)
                FROM qc_restoration_attempt AS restoration
                WHERE restoration.task_id = session.task_id
                  AND restoration.target_session_id = session.session_id
                  AND restoration.status = 'running'
            )::integer AS running_restorations,
            (
                SELECT count(*)
                FROM qc_v2_work_item AS work_item
                WHERE work_item.task_id = session.task_id
                  AND work_item.session_id = session.session_id
                  AND work_item.status NOT IN (
                    'succeeded', 'bounded', 'budget_exhausted', 'blocked',
                    'no_progress', 'failed', 'cancelled', 'superseded'
                  )
            )::integer AS nonterminal_active_work_items,
            (
                SELECT count(*)
                FROM qc_app_exploration_frontier AS frontier
                WHERE frontier.task_id = session.task_id
                  AND frontier.surface_session_id = session.session_id
                  AND frontier.status IN ('pending', 'leased', 'running', 'retry_wait')
            )::integer AS nonterminal_app_frontier_items,
            (
                SELECT count(*)
                FROM qc_surface_ref AS surface
                WHERE surface.task_id = session.task_id
                  AND surface.session_id = session.session_id
                  AND surface.status = 'pinned'
            )::integer AS pinned_surfaces
    ) AS counts
    WHERE session.task_id = $1::uuid
      AND session.session_id = $2
"""


_EXPLORATION_PENDING_FACTS_SQL = """
    SELECT
        session.session_id,
        (
            SELECT count(*)
            FROM qc_action_execution AS action
            WHERE action.task_id = session.task_id
              AND action.session_id = session.session_id
              AND action.status IN ('planned', 'executing', 'evidence_pending')
        )::integer AS pending_action_count,
        (
            (
                SELECT count(*)
                FROM qc_action_execution AS action
                WHERE action.task_id = session.task_id
                  AND action.session_id = session.session_id
                  AND action.status = 'evidence_pending'
            )
            + (
                SELECT count(*)
                FROM qc_capture AS capture
                WHERE capture.task_id = session.task_id
                  AND capture.surface_session_id = session.session_id
                  AND capture.capture_status = 'pending'
            )
            + (
                SELECT count(*)
                FROM qc_evidence_request AS request
                INNER JOIN qc_capture AS capture
                    ON capture.id = request.target_capture_id
                   AND capture.task_id = request.task_id
                WHERE request.task_id = session.task_id
                  AND capture.surface_session_id = session.session_id
                  AND request.status IN ('pending', 'routed', 'running')
            )
        )::integer AS pending_evidence_count
    FROM qc_surface_session AS session
    WHERE session.task_id = $1::uuid
      AND session.session_id = $2
      AND session.status IN ('starting', 'active', 'draining')
"""


def _parse_session(row: Mapping[str, Any]) -> SurfaceSessionSnapshot:
    return SurfaceSessionSnapshot(
        session_id=str(row["session_id"]),
        task_id=str(row["task_id"]),
        surface_kind=SurfaceKind(str(row["surface_kind"])),
        session_owner=str(row["session_owner"]),
        status=SurfaceSessionStatus(str(row["status"])),
        command_sequence=int(row["command_sequence"]),
        finalization_epoch=int(row["finalization_epoch"]),
        row_version=int(row["row_version"]),
        account_environment_ref=row["account_environment_ref"],
        platform_build_ref=row["platform_build_ref"],
        device_ref=row["device_ref"],
        device_lease_id=(
            str(row["device_lease_id"]) if row.get("device_lease_id") is not None else None
        ),
        device_lease_fencing_token=(
            int(row["device_lease_fencing_token"])
            if row.get("device_lease_fencing_token") is not None
            else None
        ),
        current_surface_ref=row["current_surface_ref"],
        current_capture_id=(
            str(row["current_capture_id"]) if row["current_capture_id"] is not None else None
        ),
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        protocol_version=SURFACE_SESSION_PROTOCOL_VERSION,
    )


def _parse_permit(row: Mapping[str, Any]) -> SessionCommandPermit:
    return SessionCommandPermit(
        task_id=str(row["task_id"]),
        session_id=str(row["session_id"]),
        surface_kind=SurfaceKind(str(row["surface_kind"])),
        lease_owner=str(row["lease_owner"]),
        lease_token=str(row["lease_token"]),
        command_sequence=int(row["command_sequence"]),
        finalization_epoch=int(row["finalization_epoch"]),
        lease_expires_at=row["lease_expires_at"],
        current_capture_id=(
            str(row["current_capture_id"])
            if row.get("current_capture_id") is not None
            else None
        ),
    )


def _parse_close_readiness(row: Mapping[str, Any]) -> SessionCloseReadiness:
    return SessionCloseReadiness(
        session_id=str(row["session_id"]),
        ready=bool(row["ready"]),
        pending_actions=int(row["pending_actions"]),
        pending_capture_commits=int(row["pending_capture_commits"]),
        active_evidence_requests=int(row["active_evidence_requests"]),
        running_restorations=int(row["running_restorations"]),
        nonterminal_active_work_items=int(row["nonterminal_active_work_items"]),
        nonterminal_app_frontier_items=int(row.get("nonterminal_app_frontier_items", 0)),
        pinned_surfaces=int(row["pinned_surfaces"]),
        pending_manifests=int(row["pending_manifests"]),
        building_evidence_indexes=int(row["building_evidence_indexes"]),
        session_status=SurfaceSessionStatus(row["session_status"]),
        has_command_lease=bool(row["has_command_lease"]),
        has_journey_lease=bool(row.get("has_journey_lease", False)),
    )


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
