"""从任务与 Surface Session 快照生成受信命令范围。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from services.api.app.application.interaction_command_validation import (
    TrustedCommandScope,
)
from services.api.app.domain.interaction import (
    DEFAULT_ALLOWED_OPERATIONS,
    SERVER_MINIMUM_RISK_BY_OPERATION,
    CommandPolicy,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.db.h5_snapshot_recovery_scope import (
    h5_snapshot_session_permission_sql,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresTaskCommandScopeReader:
    """以 ``qc_task`` + ``qc_surface_session`` 为唯一命令范围事实源。

    Agent 只提交 ActionIntent；任务平台、会话生命周期、Finalization Epoch、
    Web 主机和 App 构建包名均由本类从 PostgreSQL 读取。任务或会话缺失、已
    关闭、Epoch 过期以及平台身份不一致时返回 ``None``，由应用层失败关闭。
    """

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def read_command_scope(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind,
    ) -> TrustedCommandScope | None:
        if not _is_uuid(task_id) or not _valid_text(session_id):
            return None
        if not isinstance(surface_kind, SurfaceKind):
            return None

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    task.id::text AS task_id,
                    task.target_platform,
                    task.target_url,
                    task.finalization_epoch AS task_finalization_epoch,
                    session.session_id,
                    session.surface_kind,
                    session.status AS session_status,
                    session.finalization_epoch AS session_finalization_epoch,
                    session.platform_build_ref,
                    build.package_name AS build_package_name
                FROM qc_task AS task
                INNER JOIN qc_surface_session AS session
                    ON session.task_id = task.id
                LEFT JOIN qc_platform_build AS build
                    ON task.target_platform = 'app'
                   AND build.id::text = regexp_replace(
                       COALESCE(session.platform_build_ref, ''),
                       '^app-build:',
                       ''
                   )
                   AND build.platform = 'android'
                   AND build.status = 'verified'
                WHERE task.id = $1::uuid
                  AND session.session_id = $2
                  AND session.surface_kind = $3
                  AND ((task.status IN ('queued', 'running') AND task.finalization_state = 'open'
                        AND session.session_owner NOT LIKE 'h5-snapshot-recapture:%'
                        AND session.session_id NOT LIKE 'h5:%:recapture:%')
                       OR __H5_SNAPSHOT_SESSION_PERMISSION__)
                  AND session.status IN ('active', 'draining')
                  AND session.finalization_epoch = task.finalization_epoch
                """.replace("__H5_SNAPSHOT_SESSION_PERMISSION__",
                    h5_snapshot_session_permission_sql()),
                task_id,
                session_id,
                surface_kind.value,
            )
        return _scope_from_row(
            row,
            requested_task_id=task_id,
            requested_session_id=session_id,
            requested_surface_kind=surface_kind,
        )

    async def verify_execution_context(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind,
        lease_owner: str,
        lease_token: str,
        command_sequence: int,
        finalization_epoch: int,
    ) -> TrustedCommandScope | None:
        """在不申请新租约的前提下校验 Coordinator 已持有的命令上下文。

        Xingyu 位于 App Coordinator 下游，不能在这里再次调用
        ``acquire_command``。查询同时绑定 task/session/surface、Lease、命令序号
        与 Finalization Epoch，并要求 Lease 尚未过期；任一事实发生变化都返回
        ``None``，由调用方拒绝设备动作。
        """
        if (
            not _is_uuid(task_id)
            or not _valid_text(session_id)
            or not isinstance(surface_kind, SurfaceKind)
            or not _valid_text(lease_owner)
            or not _valid_uuid_text(lease_token)
            or not _positive_int(command_sequence)
            or not _nonnegative_int(finalization_epoch) == finalization_epoch
        ):
            return None

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    task.id::text AS task_id,
                    task.target_platform,
                    task.target_url,
                    task.finalization_epoch AS task_finalization_epoch,
                    session.session_id,
                    session.surface_kind,
                    session.status AS session_status,
                    session.finalization_epoch AS session_finalization_epoch,
                    session.platform_build_ref,
                    build.package_name AS build_package_name
                FROM qc_task AS task
                INNER JOIN qc_surface_session AS session
                    ON session.task_id = task.id
                LEFT JOIN qc_platform_build AS build
                    ON task.target_platform = 'app'
                   AND build.id::text = regexp_replace(
                       COALESCE(session.platform_build_ref, ''),
                       '^app-build:',
                       ''
                   )
                   AND build.platform = 'android'
                   AND build.status = 'verified'
                WHERE task.id = $1::uuid
                  AND session.session_id = $2
                  AND session.surface_kind = $3
                  AND ((task.status IN ('queued', 'running') AND task.finalization_state = 'open'
                        AND session.session_owner NOT LIKE 'h5-snapshot-recapture:%'
                        AND session.session_id NOT LIKE 'h5:%:recapture:%')
                       OR __H5_SNAPSHOT_SESSION_PERMISSION__)
                  AND session.status IN ('active', 'draining')
                  AND session.finalization_epoch = task.finalization_epoch
                  AND session.lease_owner = $4
                  AND session.lease_token = $5::uuid
                  AND session.command_sequence = $6
                  AND session.finalization_epoch = $7
                  AND session.lease_expires_at > now()
                """.replace("__H5_SNAPSHOT_SESSION_PERMISSION__",
                    h5_snapshot_session_permission_sql()),
                task_id,
                session_id,
                surface_kind.value,
                lease_owner,
                lease_token,
                command_sequence,
                finalization_epoch,
            )
        return _scope_from_row(
            row,
            requested_task_id=task_id,
            requested_session_id=session_id,
            requested_surface_kind=surface_kind,
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _scope_from_row(
    row: Mapping[str, Any] | None,
    *,
    requested_task_id: str,
    requested_session_id: str,
    requested_surface_kind: SurfaceKind,
) -> TrustedCommandScope | None:
    if row is None:
        return None
    canonical_task_id = _canonical_uuid(requested_task_id)
    if canonical_task_id is None:
        return None
    task_id = _text(row.get("task_id"))
    session_id = _text(row.get("session_id"))
    target_platform = _text(row.get("target_platform"))
    row_surface = _surface_kind(row.get("surface_kind"))
    if (
        task_id is None
        or session_id is None
        or row_surface is None
        or task_id != canonical_task_id
        or session_id != requested_session_id
        or row_surface is not requested_surface_kind
    ):
        return None

    expected_surface = {
        "h5": SurfaceKind.WEB,
        "web": SurfaceKind.WEB,
        "app": SurfaceKind.ANDROID,
    }.get(target_platform.casefold() if target_platform else "")
    if expected_surface is None or expected_surface is not requested_surface_kind:
        return None

    task_epoch = _nonnegative_int(row.get("task_finalization_epoch"))
    session_epoch = _nonnegative_int(row.get("session_finalization_epoch"))
    if task_epoch is None or session_epoch is None or task_epoch != session_epoch:
        return None

    target_host: str | None = None
    package_name: str | None = None
    if requested_surface_kind is SurfaceKind.WEB:
        target_host = _http_hostname(row.get("target_url"))
        if target_host is None:
            return None
    else:
        # 仅使用已经 verified 的 APK 构建包名。任务输入里的 package_name
        # 可能陈旧，不能用它覆盖构建制品事实；没有已核验包名时不能把空
        # allowed_packages 当成“任意 App”，必须失败关闭。
        package_name = _optional_text(row.get("build_package_name"))
        if package_name is None:
            return None

    allowed_operations = {
        requested_surface_kind: DEFAULT_ALLOWED_OPERATIONS[requested_surface_kind]
    }
    policy = CommandPolicy(
        allowed_operations=allowed_operations,
        allowed_risk_levels=(
            InteractionRiskLevel.LOW,
            InteractionRiskLevel.MEDIUM,
        ),
        allowed_hosts=(target_host,) if target_host is not None else (),
        allowed_packages=(package_name,) if package_name is not None else (),
        minimum_risk_by_operation=SERVER_MINIMUM_RISK_BY_OPERATION,
    )
    return TrustedCommandScope(
        task_id=task_id,
        session_id=session_id,
        surface_kind=requested_surface_kind,
        policy=policy,
        finalization_epoch=task_epoch,
    )


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _valid_uuid_text(value: object) -> bool:
    """验证租约 token 的 UUID 外形，避免把非法值交给 PostgreSQL 类型转换。"""
    return _is_uuid(value)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _canonical_uuid(value: object) -> str | None:
    if not _is_uuid(value):
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _text(value: object) -> str | None:
    return str(value) if _valid_text(value) else None


def _optional_text(value: object) -> str | None:
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _surface_kind(value: object) -> SurfaceKind | None:
    try:
        return SurfaceKind(str(value))
    except (TypeError, ValueError):
        return None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _http_hostname(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value):
        return None
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        # 访问 parsed.port 触发非法端口校验；凭据和片段不能成为任务边界。
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return hostname.casefold().rstrip(".") or None


__all__ = ["PostgresTaskCommandScopeReader"]
