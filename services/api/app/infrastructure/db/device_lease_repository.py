"""PostgreSQL App 设备资源与 fencing 租约仓储。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from services.api.app.application.device_pool import (
    DeviceInventoryItem,
    DeviceLease,
    DevicePoolError,
    DeviceSlotSeed,
    parse_device_pool_slot_seeds,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

logger = logging.getLogger(__name__)


class DeviceLeasePersistenceError(DevicePoolError):
    """设备租约数据库身份、状态或 fencing 校验失败。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "DEVICE_LEASE_PERSISTENCE_FAILED",
        retryable: bool = True,
    ) -> None:
        super().__init__(message, error_code=error_code, retryable=retryable)


class PostgresDeviceLeaseRepository:
    """以 PostgreSQL 资源行和 fencing token 作为设备唯一事实源。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def claim(
        self,
        *,
        task_id: str,
        session_id: str,
        preferred_device_ref: str | None = None,
        lease_seconds: int = 3600,
        lease_owner: str | None = None,
    ) -> DeviceLease:
        _validate_text(task_id, "task_id")
        _validate_text(session_id, "session_id")
        _validate_positive_seconds(lease_seconds)
        owner = _validate_text(
            lease_owner or f"device-claim:{session_id}",
            "lease_owner",
        )
        preferred = _optional_text(preferred_device_ref)
        lease_id = str(uuid4())
        lease_token = str(uuid4())
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            existing = await connection.fetchrow(
                """
                SELECT lease.*, resource.backend, resource.profile_id, resource.capabilities
                FROM qc_device_lease AS lease
                INNER JOIN qc_device_resource AS resource
                    ON resource.device_ref = lease.device_ref
                WHERE lease.task_id = $1::uuid
                  AND lease.session_id = $2
                  AND lease.state IN ('claimed', 'active', 'releasing', 'recovering')
                ORDER BY lease.updated_at DESC
                FOR UPDATE OF lease
                LIMIT 1
                """,
                task_id,
                session_id,
            )
            if existing is not None:
                if (
                    str(existing["lease_owner"]) == owner
                    and str(existing["state"]) in {"claimed", "active"}
                    and _aware(existing["expires_at"], "expires_at") > datetime.now(UTC)
                ):
                    return _parse_lease(existing)
                raise DeviceLeasePersistenceError(
                    "当前 App Session 已绑定其他设备租约或进入恢复状态",
                    error_code="DEVICE_LEASE_ALREADY_OWNED",
                )
            resource = await _lock_claimable_resource(connection, preferred)
            if resource is None and preferred is not None:
                miss = await _claim_miss_error(connection, preferred=preferred)
                if miss.error_code != "DEVICE_BUSY":
                    raise miss
                # 指定设备占用或不可领取时，改领其他空闲设备。
                resource = await _lock_claimable_resource(connection, None)
                if resource is None:
                    raise miss
                logger.info(
                    "preferred_device_busy_fallback preferred=%s selected=%s task_id=%s",
                    preferred,
                    resource["device_ref"],
                    task_id,
                )
            elif resource is None:
                raise await _claim_miss_error(connection, preferred=preferred)
            bumped = await connection.fetchrow(
                """
                UPDATE qc_device_resource
                SET
                    health_state = 'claimed',
                    fencing_epoch = fencing_epoch + 1,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE device_ref = $1
                  AND enabled
                  AND health_state = 'available'
                RETURNING device_ref, backend, profile_id, fencing_epoch, capabilities
                """,
                resource["device_ref"],
            )
            if bumped is None:
                raise DeviceLeasePersistenceError(
                    "设备资源在领取期间发生 fencing 冲突",
                    error_code="DEVICE_LEASE_CLAIM_RACE",
                )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_device_lease (
                    lease_id, device_ref, task_id, session_id,
                    lease_owner, lease_token, fencing_token, state,
                    claimed_at, expires_at, row_version, attempt_count
                )
                VALUES (
                    $1::uuid, $2, $3::uuid, $4,
                    $5, $6::uuid, $7::bigint, 'claimed',
                    now(), now() + make_interval(secs => $8::integer), 0, 0
                )
                RETURNING *
                """,
                lease_id,
                bumped["device_ref"],
                task_id,
                session_id,
                owner,
                lease_token,
                bumped["fencing_epoch"],
                lease_seconds,
            )
            if row is None:
                raise DeviceLeasePersistenceError("设备租约写入失败")
        return _parse_lease(
            row,
            backend=str(bumped["backend"]),
            profile_id=str(bumped["profile_id"]),
            morelogin_env_id=_morelogin_env_id_from_capabilities(
                bumped.get("capabilities"),
                backend=str(bumped["backend"]),
            ),
        )

    async def renew(self, *, lease: DeviceLease, lease_seconds: int) -> DeviceLease:
        _validate_lease(lease)
        _validate_positive_seconds(lease_seconds)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_device_lease AS lease
                SET
                    state = 'active',
                    expires_at = now() + make_interval(secs => $8::integer),
                    last_renewed_at = now(),
                    recovery_started_at = NULL,
                    quarantined_at = NULL,
                    quarantine_reason = NULL,
                    row_version = lease.row_version + 1,
                    updated_at = now()
                WHERE lease.lease_id = $1::uuid
                  AND lease.device_ref = $2
                  AND lease.task_id = $3::uuid
                  AND lease.session_id = $4
                  AND lease.lease_owner = $5
                  AND lease.lease_token = $6::uuid
                  AND lease.fencing_token = $7::bigint
                  AND (
                      (
                          lease.state IN ('claimed', 'active')
                          AND lease.expires_at > now()
                      )
                      OR lease.state = 'recovering'
                      OR (
                          lease.state = 'quarantined'
                          AND lease.quarantine_reason = 'expired_lease_session_still_open'
                          AND EXISTS (
                              SELECT 1
                              FROM qc_task AS task
                              WHERE task.id = lease.task_id
                                AND task.deleted_at IS NULL
                                AND task.status IN ('running', 'queued')
                          )
                      )
                  )
                RETURNING lease.*
                """,
                lease.lease_id,
                lease.device_ref,
                lease.task_id,
                lease.session_id,
                _lease_owner(lease),
                lease.lease_token,
                lease.fencing_token,
                lease_seconds,
            )
            if row is None:
                raise DeviceLeasePersistenceError(
                    "设备租约已过期、被接管或 fencing 不匹配",
                    error_code="APP_DEVICE_LEASE_LOST",
                )
            await connection.execute(
                """
                UPDATE qc_device_resource
                SET health_state = 'claimed',
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE device_ref = $1
                  AND health_state IN ('recovering', 'quarantined')
                """,
                lease.device_ref,
            )
            resource = await connection.fetchrow(
                """
                SELECT backend, profile_id, capabilities
                FROM qc_device_resource
                WHERE device_ref = $1
                """,
                lease.device_ref,
            )
        if resource is None:
            raise DeviceLeasePersistenceError("设备资源目录缺失")
        return _parse_lease(
            row,
            backend=str(resource["backend"]),
            profile_id=str(resource["profile_id"]),
            morelogin_env_id=_morelogin_env_id_from_capabilities(
                resource.get("capabilities"),
                backend=str(resource["backend"]),
            )
            or lease.morelogin_env_id,
        )

    async def release(self, *, lease: DeviceLease) -> bool:
        _validate_lease(lease)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_device_lease AS lease
                SET
                    state = 'released',
                    released_at = now(),
                    row_version = lease.row_version + 1,
                    updated_at = now()
                WHERE lease.lease_id = $1::uuid
                  AND lease.device_ref = $2
                  AND lease.task_id = $3::uuid
                  AND lease.session_id = $4
                  AND lease.lease_owner = $5
                  AND lease.lease_token = $6::uuid
                  AND lease.fencing_token = $7::bigint
                  AND lease.state IN ('claimed', 'active', 'releasing')
                  AND lease.expires_at > now()
                RETURNING lease.device_ref
                """,
                lease.lease_id,
                lease.device_ref,
                lease.task_id,
                lease.session_id,
                _lease_owner(lease),
                lease.lease_token,
                lease.fencing_token,
            )
            if row is None:
                return False
            await connection.execute(
                """
                UPDATE qc_device_resource
                SET health_state = 'available', row_version = row_version + 1,
                    updated_at = now()
                WHERE device_ref = $1
                  AND enabled
                  AND NOT EXISTS (
                      SELECT 1 FROM qc_device_lease AS active_lease
                      WHERE active_lease.device_ref = qc_device_resource.device_ref
                        AND active_lease.state IN (
                            'claimed', 'active', 'releasing', 'recovering'
                        )
                  )
                """,
                row["device_ref"],
            )
        return True

    async def handoff_to_successor_session(
        self,
        *,
        predecessor: DeviceLease,
        successor_session_id: str,
        predecessor_channel_silenced: bool,
    ) -> DeviceLease:
        """跨 Session 租约承接：领域校验后同事务轮转 fencing 并更新 session_id（T27）。"""

        _validate_lease(predecessor)
        _validate_text(successor_session_id, "successor_session_id")
        from services.api.app.application.flow_foundation_p1 import FlowFoundationP1Service

        successor_fencing = int(predecessor.fencing_token) + 1
        FlowFoundationP1Service().build_lease_handoff(
            predecessor=predecessor,
            successor_session_id=successor_session_id,
            successor_fencing_token=successor_fencing,
            predecessor_channel_silenced=predecessor_channel_silenced,
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_device_lease AS lease
                SET
                    session_id = $8,
                    fencing_token = $9::bigint,
                    row_version = lease.row_version + 1,
                    updated_at = now()
                WHERE lease.lease_id = $1::uuid
                  AND lease.device_ref = $2
                  AND lease.task_id = $3::uuid
                  AND lease.session_id = $4
                  AND lease.lease_owner = $5
                  AND lease.lease_token = $6::uuid
                  AND lease.fencing_token = $7::bigint
                  AND lease.state IN ('claimed', 'active', 'recovering')
                RETURNING lease.*
                """,
                predecessor.lease_id,
                predecessor.device_ref,
                predecessor.task_id,
                predecessor.session_id,
                _lease_owner(predecessor),
                predecessor.lease_token,
                predecessor.fencing_token,
                successor_session_id,
                successor_fencing,
            )
            if row is None:
                raise DeviceLeasePersistenceError(
                    "设备租约无法承接至后继 Session",
                    error_code="DEVICE_LEASE_HANDOFF_REJECTED",
                )
            await connection.execute(
                """
                UPDATE qc_device_resource
                SET
                    fencing_epoch = GREATEST(fencing_epoch, $2::bigint),
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE device_ref = $1
                """,
                predecessor.device_ref,
                successor_fencing,
            )
            resource = await connection.fetchrow(
                """
                SELECT backend, profile_id, capabilities
                FROM qc_device_resource
                WHERE device_ref = $1
                """,
                predecessor.device_ref,
            )
        if resource is None:
            raise DeviceLeasePersistenceError("设备资源目录缺失")
        return _parse_lease(
            row,
            backend=str(resource["backend"]),
            profile_id=str(resource["profile_id"]),
            morelogin_env_id=_morelogin_env_id_from_capabilities(
                resource.get("capabilities"),
                backend=str(resource["backend"]),
            )
            or predecessor.morelogin_env_id,
        )

    async def recover_then_handoff(
        self,
        *,
        lease: DeviceLease,
        successor_session_id: str,
        predecessor_channel_silenced: bool = True,
    ) -> DeviceLease:
        """先 recover 再 handoff 至后继 Session（无真机可调；DB 路径）。"""

        recovered = await self.recover(lease=lease)
        return await self.handoff_to_successor_session(
            predecessor=recovered,
            successor_session_id=successor_session_id,
            predecessor_channel_silenced=predecessor_channel_silenced,
        )

    async def recover(self, *, lease: DeviceLease) -> DeviceLease:
        _validate_lease(lease)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_device_lease AS lease
                SET
                    state = 'recovering',
                    recovery_started_at = COALESCE(lease.recovery_started_at, now()),
                    row_version = lease.row_version + 1,
                    updated_at = now()
                WHERE lease.lease_id = $1::uuid
                  AND lease.device_ref = $2
                  AND lease.task_id = $3::uuid
                  AND lease.session_id = $4
                  AND lease.lease_owner = $5
                  AND lease.lease_token = $6::uuid
                  AND lease.fencing_token = $7::bigint
                  AND lease.state IN ('claimed', 'active', 'releasing')
                RETURNING lease.*
                """,
                lease.lease_id,
                lease.device_ref,
                lease.task_id,
                lease.session_id,
                _lease_owner(lease),
                lease.lease_token,
                lease.fencing_token,
            )
            if row is None:
                raise DeviceLeasePersistenceError(
                    "设备租约无法进入恢复状态",
                    error_code="DEVICE_LEASE_RECOVERY_REJECTED",
                )
            await connection.execute(
                """
                UPDATE qc_device_resource
                SET health_state = 'recovering', row_version = row_version + 1,
                    updated_at = now()
                WHERE device_ref = $1 AND health_state = 'claimed'
                """,
                lease.device_ref,
            )
            resource = await connection.fetchrow(
                """
                SELECT backend, profile_id, capabilities
                FROM qc_device_resource
                WHERE device_ref = $1
                """,
                lease.device_ref,
            )
        if resource is None:
            raise DeviceLeasePersistenceError("设备资源目录缺失")
        return _parse_lease(
            row,
            backend=str(resource["backend"]),
            profile_id=str(resource["profile_id"]),
            morelogin_env_id=_morelogin_env_id_from_capabilities(
                resource.get("capabilities"),
                backend=str(resource["backend"]),
            )
            or lease.morelogin_env_id,
        )

    async def quarantine(self, *, lease: DeviceLease, reason: str) -> bool:
        _validate_lease(lease)
        _validate_text(reason, "reason")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            changed = await connection.fetchval(
                """
                UPDATE qc_device_lease AS lease
                SET
                    state = 'quarantined',
                    quarantined_at = COALESCE(lease.quarantined_at, now()),
                    quarantine_reason = COALESCE(lease.quarantine_reason, $8),
                    row_version = CASE
                        WHEN lease.state = 'quarantined' THEN lease.row_version
                        ELSE lease.row_version + 1
                    END,
                    updated_at = now()
                WHERE lease.lease_id = $1::uuid
                  AND lease.device_ref = $2
                  AND lease.task_id = $3::uuid
                  AND lease.session_id = $4
                  AND lease.lease_owner = $5
                  AND lease.lease_token = $6::uuid
                  AND lease.fencing_token = $7::bigint
                  AND lease.state IN (
                      'claimed', 'active', 'releasing', 'recovering', 'quarantined'
                  )
                RETURNING lease.lease_id
                """,
                lease.lease_id,
                lease.device_ref,
                lease.task_id,
                lease.session_id,
                _lease_owner(lease),
                lease.lease_token,
                lease.fencing_token,
                reason,
            )
            if changed is not None:
                await connection.execute(
                    """
                    UPDATE qc_device_resource
                    SET health_state = 'quarantined', row_version = row_version + 1,
                        updated_at = now()
                    WHERE device_ref = $1 AND health_state <> 'disabled'
                    """,
                    lease.device_ref,
                )
        return changed is not None

    async def get(
        self,
        *,
        lease_id: str | None = None,
        session_id: str | None = None,
    ) -> DeviceLease | None:
        if lease_id is None and session_id is None:
            raise ValueError("lease_id 或 session_id 至少提供一个")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    lease.*,
                    resource.backend,
                    resource.profile_id,
                    resource.capabilities
                FROM qc_device_lease AS lease
                INNER JOIN qc_device_resource AS resource
                    ON resource.device_ref = lease.device_ref
                WHERE ($1::uuid IS NULL OR lease.lease_id = $1::uuid)
                  AND ($2::varchar IS NULL OR lease.session_id = $2::varchar)
                  AND lease.state IN ('claimed', 'active', 'releasing', 'recovering')
                ORDER BY lease.updated_at DESC
                LIMIT 1
                """,
                lease_id,
                session_id,
            )
        return _parse_lease(row) if row is not None else None

    async def get_active(
        self,
        *,
        lease_id: str | None = None,
        session_id: str | None = None,
    ) -> DeviceLease | None:
        return await self.get(lease_id=lease_id, session_id=session_id)

    async def recover_expired(self, *, limit: int = 100) -> int:
        """把过期 claimed/active 移入 recovering，设备不会被静默复用。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit 必须为正整数")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            result = await connection.execute(
                """
                WITH stale AS (
                    SELECT lease_id
                    FROM qc_device_lease
                    WHERE state IN ('claimed', 'active', 'releasing')
                      AND expires_at <= now()
                    ORDER BY expires_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1::integer
                )
                UPDATE qc_device_lease AS lease
                SET state = 'recovering', recovery_started_at = COALESCE(
                        lease.recovery_started_at, now()
                    ), row_version = lease.row_version + 1, updated_at = now()
                FROM stale
                WHERE lease.lease_id = stale.lease_id
                """,
                limit,
            )
            await connection.execute(
                """
                UPDATE qc_device_resource AS resource
                SET health_state = 'recovering', row_version = resource.row_version + 1,
                    updated_at = now()
                WHERE resource.health_state = 'claimed'
                  AND EXISTS (
                      SELECT 1
                      FROM qc_device_lease AS lease
                      WHERE lease.device_ref = resource.device_ref
                        AND lease.state = 'recovering'
                  )
                """
            )
        return _rowcount(result)

    async def release_finished_task_devices(self, *, limit: int = 100) -> int:
        """待复核或已取消的任务不再占设备，隔离租约也放回空闲。"""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit 必须为正整数")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            released = await connection.fetch(
                """
                WITH finished AS (
                    SELECT lease.lease_id
                    FROM qc_device_lease AS lease
                    INNER JOIN qc_task AS task ON task.id = lease.task_id
                    WHERE lease.state IN (
                        'claimed', 'active', 'releasing', 'recovering', 'quarantined'
                    )
                      AND task.deleted_at IS NULL
                      AND task.status IN ('review', 'canceled')
                    ORDER BY lease.updated_at
                    FOR UPDATE OF lease SKIP LOCKED
                    LIMIT $1::integer
                )
                UPDATE qc_device_lease AS lease
                SET
                    state = 'released',
                    released_at = COALESCE(lease.released_at, now()),
                    quarantined_at = NULL,
                    quarantine_reason = NULL,
                    row_version = lease.row_version + 1,
                    updated_at = now()
                FROM finished
                WHERE lease.lease_id = finished.lease_id
                RETURNING lease.device_ref
                """,
                limit,
            )
            device_refs = list(dict.fromkeys(str(row["device_ref"]) for row in released))
            if device_refs:
                await connection.execute(
                    """
                    UPDATE qc_device_resource AS resource
                    SET health_state = 'available',
                        row_version = resource.row_version + 1,
                        updated_at = now()
                    WHERE resource.device_ref = ANY($1::varchar[])
                      AND resource.enabled
                      AND resource.health_state <> 'disabled'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_device_lease AS active_lease
                          WHERE active_lease.device_ref = resource.device_ref
                            AND active_lease.state IN (
                                'claimed', 'active', 'releasing', 'recovering', 'quarantined'
                            )
                      )
                    """,
                    device_refs,
                )
        return len(released)

    async def reclaim_recovering(self, *, limit: int = 100) -> int:
        """Session 已终态或从未登记时，把 recovering 租约释放回 available。

        过期回收不得把仍在 running 的 Session 对应设备直接标成可用；那条路径
        只能进 quarantine，等待健康检查或人工解除。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit 必须为正整数")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            released = await connection.fetch(
                """
                WITH stale AS (
                    SELECT lease.lease_id, lease.device_ref
                    FROM qc_device_lease AS lease
                    LEFT JOIN qc_surface_session AS session
                        ON session.session_id = lease.session_id
                    WHERE lease.state = 'recovering'
                      AND (
                          session.session_id IS NULL
                          OR session.status IN ('closed', 'lost', 'failed')
                      )
                    ORDER BY lease.recovery_started_at NULLS FIRST, lease.updated_at
                    FOR UPDATE OF lease SKIP LOCKED
                    LIMIT $1::integer
                ),
                released AS (
                    UPDATE qc_device_lease AS lease
                    SET
                        state = 'released',
                        released_at = COALESCE(lease.released_at, now()),
                        row_version = lease.row_version + 1,
                        updated_at = now()
                    FROM stale
                    WHERE lease.lease_id = stale.lease_id
                    RETURNING lease.device_ref
                )
                SELECT device_ref FROM released
                """,
                limit,
            )
            device_refs = [str(row["device_ref"]) for row in released]
            if device_refs:
                await connection.execute(
                    """
                    UPDATE qc_device_resource AS resource
                    SET health_state = 'available',
                        row_version = resource.row_version + 1,
                        updated_at = now()
                    WHERE resource.device_ref = ANY($1::varchar[])
                      AND resource.enabled
                      AND resource.health_state IN ('claimed', 'recovering')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_device_lease AS active_lease
                          WHERE active_lease.device_ref = resource.device_ref
                            AND active_lease.state IN (
                                'claimed', 'active', 'releasing', 'recovering'
                            )
                      )
                    """,
                    device_refs,
                )
        return len(device_refs)

    async def release_quarantine(self, *, device_ref: str) -> bool:
        """人工解除设备隔离：结清隔离租约并把资源放回 available。

        仅在设备上没有任何在用租约时才放行；调用方须自行确认设备已恢复健康
        （隔离原因保留在租约历史里，不做静默清除）。
        """
        ref = str(device_ref or "").strip()
        if not ref:
            raise ValueError("device_ref 不能为空")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE qc_device_lease
                SET state = 'released',
                    released_at = COALESCE(released_at, now()),
                    quarantined_at = NULL,
                    quarantine_reason = NULL,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE device_ref = $1
                  AND state = 'quarantined'
                """,
                ref,
            )
            restored = await connection.fetchval(
                """
                UPDATE qc_device_resource AS resource
                SET health_state = 'available',
                    row_version = resource.row_version + 1,
                    updated_at = now()
                WHERE resource.device_ref = $1
                  AND resource.enabled
                  AND resource.health_state = 'quarantined'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_device_lease AS active_lease
                      WHERE active_lease.device_ref = resource.device_ref
                        AND active_lease.state IN (
                            'claimed', 'active', 'releasing', 'recovering'
                        )
                  )
                RETURNING resource.device_ref
                """,
                ref,
            )
        return restored is not None

    async def quarantine_stale_recovering(
        self,
        *,
        grace_seconds: int = 60,
        limit: int = 100,
    ) -> int:
        """Session 仍非终态且超过宽限期的 recovering 租约进入隔离，禁止静默复用。

        任务仍在执行或排队时不隔离。同一条租约由续租接回 active，避免流程被掐断。
        """
        if (
            isinstance(grace_seconds, bool)
            or not isinstance(grace_seconds, int)
            or grace_seconds < 1
        ):
            raise ValueError("grace_seconds 必须为正整数")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit 必须为正整数")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            quarantined = await connection.fetch(
                """
                WITH stale AS (
                    SELECT lease.lease_id, lease.device_ref
                    FROM qc_device_lease AS lease
                    INNER JOIN qc_surface_session AS session
                        ON session.session_id = lease.session_id
                    WHERE lease.state = 'recovering'
                      AND COALESCE(lease.recovery_started_at, lease.expires_at)
                          <= now() - make_interval(secs => $1::integer)
                      AND session.status IN ('starting', 'active', 'draining', 'closing')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_task AS task
                          WHERE task.id = lease.task_id
                            AND task.deleted_at IS NULL
                            AND task.status IN ('running', 'queued')
                      )
                    ORDER BY lease.recovery_started_at NULLS FIRST, lease.updated_at
                    FOR UPDATE OF lease SKIP LOCKED
                    LIMIT $2::integer
                ),
                isolated AS (
                    UPDATE qc_device_lease AS lease
                    SET
                        state = 'quarantined',
                        quarantined_at = COALESCE(lease.quarantined_at, now()),
                        quarantine_reason = COALESCE(
                            lease.quarantine_reason,
                            'expired_lease_session_still_open'
                        ),
                        row_version = lease.row_version + 1,
                        updated_at = now()
                    FROM stale
                    WHERE lease.lease_id = stale.lease_id
                    RETURNING lease.device_ref
                )
                SELECT device_ref FROM isolated
                """,
                grace_seconds,
                limit,
            )
            device_refs = [str(row["device_ref"]) for row in quarantined]
            if device_refs:
                await connection.execute(
                    """
                    UPDATE qc_device_resource AS resource
                    SET health_state = 'quarantined',
                        row_version = resource.row_version + 1,
                        updated_at = now()
                    WHERE resource.device_ref = ANY($1::varchar[])
                      AND resource.health_state <> 'disabled'
                    """,
                    device_refs,
                )
        return len(device_refs)

    async def list_inventory(self) -> list[DeviceInventoryItem]:
        """列出设备池库存，并附带占用与偏好排队观测计数。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH active_lease AS (
                    SELECT DISTINCT ON (lease.device_ref)
                        lease.device_ref,
                        lease.task_id,
                        lease.session_id,
                        lease.state,
                        lease.expires_at
                    FROM qc_device_lease AS lease
                    WHERE lease.state IN ('claimed', 'active', 'releasing', 'recovering')
                    ORDER BY lease.device_ref, lease.updated_at DESC
                ),
                preferred_waiting AS (
                    SELECT
                        (
                            COALESCE(device.payload->>'backend', '')
                            || ':'
                            || COALESCE(device.payload->>'profile_id', '')
                        ) AS device_ref,
                        COUNT(DISTINCT task.id)::int AS waiting_count
                    FROM qc_task AS task
                    INNER JOIN qc_agent_run AS agent_run
                        ON agent_run.task_id = task.id
                       AND agent_run.agent_type = 'main'
                    CROSS JOIN LATERAL (
                        SELECT COALESCE(
                            agent_run.input_context
                                -> 'task_config'
                                -> '_task_target'
                                -> 'app'
                                -> 'device',
                            task.task_config -> '_task_target' -> 'app' -> 'device',
                            task.task_config -> 'device'
                        ) AS payload
                    ) AS device
                    WHERE task.target_platform = 'app'
                      AND task.status IN ('queued', 'running')
                      AND COALESCE(device.payload->>'backend', '') <> ''
                      AND COALESCE(device.payload->>'profile_id', '') <> ''
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_device_lease AS lease
                          WHERE lease.task_id = task.id
                            AND lease.device_ref = (
                                COALESCE(device.payload->>'backend', '')
                                || ':'
                                || COALESCE(device.payload->>'profile_id', '')
                            )
                            AND lease.state IN (
                                'claimed', 'active', 'releasing', 'recovering'
                            )
                      )
                    GROUP BY 1
                )
                SELECT
                    resource.device_ref,
                    resource.backend,
                    resource.profile_id,
                    resource.enabled,
                    resource.health_state,
                    resource.capabilities,
                    active_lease.task_id AS lease_task_id,
                    active_lease.session_id AS lease_session_id,
                    active_lease.state AS lease_state,
                    active_lease.expires_at AS lease_expires_at,
                    COALESCE(preferred_waiting.waiting_count, 0) AS queued_preferred_count
                FROM qc_device_resource AS resource
                LEFT JOIN active_lease
                    ON active_lease.device_ref = resource.device_ref
                LEFT JOIN preferred_waiting
                    ON preferred_waiting.device_ref = resource.device_ref
                ORDER BY resource.backend ASC, resource.profile_id ASC
                """
            )
        return [_parse_inventory_item(row) for row in rows]

    async def count_pending_app_device_demand(
        self,
        *,
        exclude_task_id: str | None = None,
    ) -> int:
        """统计仍需领取云手机的 App 任务数（排队中／已跑但尚无活跃租约）。

        用于租约结算后决定是否 powerOff：有后续任务则保持开机连跑，避免频繁开关机。
        """
        excluded = str(exclude_task_id or "").strip() or None
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT COUNT(DISTINCT task.id)::int
                FROM qc_task AS task
                INNER JOIN qc_agent_run AS agent_run
                    ON agent_run.task_id = task.id
                   AND agent_run.agent_type = 'main'
                WHERE task.target_platform = 'app'
                  AND task.status IN ('queued', 'running')
                  AND agent_run.status IN ('pending', 'running')
                  AND ($1::uuid IS NULL OR task.id <> $1::uuid)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_device_lease AS lease
                      WHERE lease.task_id = task.id
                        AND lease.state IN (
                            'claimed', 'active', 'releasing', 'recovering'
                        )
                  )
                """,
                excluded,
            )
        return max(0, int(value or 0))

    async def configure_resources(
        self,
        resources: Sequence[tuple[str, str, str]] | None = None,
    ) -> int:
        """显式登记设备资源；不会自动解除 quarantined。"""
        seeds = _resource_seeds(resources)
        pool = await self._get_pool()
        count = 0
        async with pool.acquire() as connection, connection.transaction():
            for seed in seeds:
                _validate_text(seed.device_ref, "device_ref")
                _validate_text(seed.backend, "backend")
                _validate_text(seed.profile_id, "profile_id")
                capabilities = json.dumps(seed.capabilities(), ensure_ascii=False)
                await connection.execute(
                    """
                    INSERT INTO qc_device_resource (
                        device_ref, backend, profile_id, enabled, health_state, capabilities
                    )
                    VALUES ($1, $2, $3, true, 'available', $4::jsonb)
                    ON CONFLICT (device_ref) DO UPDATE
                    SET backend = EXCLUDED.backend,
                        profile_id = EXCLUDED.profile_id,
                        enabled = true,
                        capabilities = CASE
                            WHEN EXCLUDED.capabilities <> '{}'::jsonb
                            THEN COALESCE(qc_device_resource.capabilities, '{}'::jsonb)
                                 || EXCLUDED.capabilities
                            ELSE qc_device_resource.capabilities
                        END,
                        health_state = CASE
                            WHEN qc_device_resource.health_state = 'disabled'
                            THEN 'available'
                            ELSE qc_device_resource.health_state
                        END,
                        row_version = qc_device_resource.row_version + 1,
                        updated_at = now()
                    """,
                    seed.device_ref,
                    seed.backend,
                    seed.profile_id,
                    capabilities,
                )
                count += 1
        return count

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_inventory_item(row: Mapping[str, Any]) -> DeviceInventoryItem:
    backend = str(row["backend"])
    health_state = str(row["health_state"])
    enabled = bool(row["enabled"])
    occupied = row.get("lease_task_id") is not None
    if not enabled or health_state == "disabled":
        occupancy_status = "disabled"
    elif health_state == "quarantined":
        occupancy_status = "quarantined"
    elif occupied:
        occupancy_status = "occupied"
    elif health_state in {"recovering", "claimed"}:
        occupancy_status = "recovering"
    else:
        occupancy_status = "available"
    lease_expires_at = row.get("lease_expires_at")
    return DeviceInventoryItem(
        device_ref=str(row["device_ref"]),
        backend=backend,
        profile_id=str(row["profile_id"]),
        enabled=enabled,
        health_state=health_state,
        occupied=occupied,
        occupancy_status=occupancy_status,
        lease_task_id=(
            str(row["lease_task_id"]) if row.get("lease_task_id") is not None else None
        ),
        lease_session_id=(
            str(row["lease_session_id"])
            if row.get("lease_session_id") is not None
            else None
        ),
        lease_state=(
            str(row["lease_state"]) if row.get("lease_state") is not None else None
        ),
        lease_expires_at=(
            _aware(lease_expires_at, "lease_expires_at")
            if lease_expires_at is not None
            else None
        ),
        morelogin_env_id=_morelogin_env_id_from_capabilities(
            row.get("capabilities"),
            backend=backend,
        )
        or "",
        queued_preferred_count=int(row.get("queued_preferred_count") or 0),
    )


def _parse_lease(
    row: Mapping[str, Any],
    *,
    backend: str | None = None,
    profile_id: str | None = None,
    morelogin_env_id: str | None = None,
) -> DeviceLease:
    resolved_backend = str(backend or row.get("backend") or "unknown")
    return DeviceLease(
        lease_id=str(row["lease_id"]),
        device_ref=str(row["device_ref"]),
        task_id=str(row["task_id"]),
        session_id=str(row["session_id"]),
        claimed_at=_aware(row["claimed_at"], "claimed_at"),
        expires_at=_aware(row["expires_at"], "expires_at"),
        backend=resolved_backend,
        profile_id=str(profile_id or row.get("profile_id") or "default"),
        lease_token=str(row["lease_token"]),
        lease_owner=str(row.get("lease_owner") or ""),
        fencing_token=int(row["fencing_token"]),
        state=str(row["state"]),
        last_renewed_at=(
            _aware(row["last_renewed_at"], "last_renewed_at")
            if row.get("last_renewed_at") is not None
            else None
        ),
        morelogin_env_id=(
            str(morelogin_env_id or "").strip()
            or _morelogin_env_id_from_capabilities(
                row.get("capabilities"),
                backend=resolved_backend,
            )
        ),
    )


def _lease_owner(lease: DeviceLease) -> str:
    # DeviceLease 的公开结构不携带 owner；生产 claim owner 使用 session 绑定的稳定 worker 前缀。
    # Postgres 实现创建时将此值写入 lease_owner，调用方可通过可选属性覆盖。
    owner = getattr(lease, "lease_owner", None)
    if isinstance(owner, str) and owner.strip():
        return owner
    return f"device-claim:{lease.session_id}"


def _validate_lease(lease: DeviceLease) -> None:
    if not isinstance(lease, DeviceLease):
        raise TypeError("lease 必须为 DeviceLease")
    if not lease.lease_token or lease.fencing_token < 1:
        raise ValueError("数据库设备租约必须携带 lease_token 与 fencing_token")


def _validate_positive_seconds(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 60:
        raise ValueError("lease_seconds 必须为不小于 60 的整数")


def _validate_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _validate_text(value, "preferred_device_ref")


def _aware(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须为带时区时间")
    return value.astimezone(UTC)


_CLAIMABLE_RESOURCE_SQL = """
SELECT
    resource.device_ref,
    resource.backend,
    resource.profile_id,
    resource.fencing_epoch,
    resource.capabilities
FROM qc_device_resource AS resource
WHERE resource.enabled
  AND resource.health_state = 'available'
  AND ($1::varchar IS NULL OR resource.device_ref = $1::varchar)
  AND NOT EXISTS (
      SELECT 1
      FROM qc_device_lease AS active_lease
      WHERE active_lease.device_ref = resource.device_ref
        AND active_lease.state IN (
            'claimed', 'active', 'releasing', 'recovering'
        )
  )
ORDER BY resource.device_ref
FOR UPDATE OF resource SKIP LOCKED
LIMIT 1
"""


async def _lock_claimable_resource(connection: Any, preferred: str | None) -> Any:
    """锁定一台可领取设备。preferred 为空时按设备名选取任意空闲设备。"""
    return await connection.fetchrow(_CLAIMABLE_RESOURCE_SQL, preferred)


async def _claim_miss_error(
    connection: Any,
    *,
    preferred: str | None,
) -> DeviceLeasePersistenceError:
    """把 claim 未命中拆成资源不存在、占用中与池满，避免调用方误推进安装。"""
    if preferred is None:
        return DeviceLeasePersistenceError(
            "没有可领取的 PostgreSQL App 设备资源",
            error_code="DEVICE_POOL_EXHAUSTED",
        )
    existing = await connection.fetchrow(
        """
        SELECT device_ref, enabled, health_state
        FROM qc_device_resource
        WHERE device_ref = $1
        """,
        preferred,
    )
    if existing is None:
        return DeviceLeasePersistenceError(
            "指定 App 设备资源不存在",
            error_code="DEVICE_RESOURCE_NOT_FOUND",
            retryable=False,
        )
    return DeviceLeasePersistenceError(
        "指定 App 设备资源正在使用或不可领取",
        error_code="DEVICE_BUSY",
    )


def _rowcount(result: object) -> int:
    text = str(result).strip()
    if not text:
        return 0
    try:
        return int(text.split()[-1])
    except ValueError:
        return 0


def _morelogin_env_id_from_capabilities(capabilities: Any, *, backend: str) -> str:
    if backend != "cloud_phone":
        return ""
    payload = capabilities
    if isinstance(payload, str) and payload.strip():
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return ""
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("env_id") or "").strip()


def _resource_seeds(
    resources: Sequence[tuple[str, str, str]] | Sequence[DeviceSlotSeed] | None,
) -> tuple[DeviceSlotSeed, ...]:
    if resources is None:
        return parse_device_pool_slot_seeds()
    seeds: list[DeviceSlotSeed] = []
    for item in resources:
        if isinstance(item, DeviceSlotSeed):
            seeds.append(item)
            continue
        device_ref, backend, profile_id = item
        seeds.append(
            DeviceSlotSeed(
                device_ref=str(device_ref),
                backend=str(backend),
                profile_id=str(profile_id),
            )
        )
    return tuple(seeds)
