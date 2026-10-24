"""进程内串行化与 PostgreSQL Session Lease 组合的生产命令门禁。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from services.api.app.application.capture_coordinator import InProcessSessionCommandGate
from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import (
    SurfaceKind,
    SurfaceSessionSpec,
    SurfaceSessionStatus,
)
from services.api.app.domain.interaction.surface_driver import (
    app_primary_surface_ref,
    web_primary_surface_ref,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
    SurfaceSessionPersistenceError,
)


class PostgresSessionCommandGate:
    """同进程先排队、跨进程再取 DB 租约，保证 Session Actor 单写。"""

    def __init__(
        self,
        *,
        repository: PostgresSurfaceSessionRepository,
        lease_owner: str,
        session_owner: str = "interaction-runtime",
        platform_build_ref: str | None = None,
        device_ref: str | None = None,
        lease_seconds: int = 120,
    ) -> None:
        if lease_seconds < 15:
            raise ValueError("lease_seconds 不能小于 15 秒")
        self._repository = repository
        self._lease_owner = _require_text(lease_owner, "lease_owner")
        self._session_owner = _require_text(session_owner, "session_owner")
        self._platform_build_ref = (
            _require_text(platform_build_ref, "platform_build_ref")
            if platform_build_ref is not None
            else None
        )
        self._device_ref = (
            _require_text(device_ref, "device_ref") if device_ref is not None else None
        )
        self._lease_seconds = lease_seconds
        self._local_gate = InProcessSessionCommandGate()

    @asynccontextmanager
    async def hold(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind | None = None,
        allow_draining: bool = False,
    ) -> AsyncIterator[SessionCommandPermit]:
        actual_kind = surface_kind or SurfaceKind.WEB
        async with self._local_gate.hold(
            task_id=task_id,
            session_id=session_id,
            surface_kind=actual_kind,
            allow_draining=allow_draining,
        ):
            # App Bootstrap 已经把设备租约和 APK 身份写入同一条 Session。命令
            # 阶段必须读取并复用这条事实，不能用缺少设备租约身份的 spec 再次
            # register_active，否则会被 PostgreSQL 的幂等身份条件 fail-closed。
            existing = await self._repository.get(session_id=session_id)
            if existing is None:
                await self._repository.register_active(
                    spec=SurfaceSessionSpec(
                        task_id=task_id,
                        session_id=session_id,
                        surface_kind=actual_kind,
                        platform_build_ref=(
                            self._platform_build_ref
                            if actual_kind is not SurfaceKind.WEB
                            else None
                        ),
                        device_ref=(
                            self._device_ref if actual_kind is not SurfaceKind.WEB else None
                        ),
                    ),
                    session_owner=self._session_owner,
                    primary_surface_ref=(
                        app_primary_surface_ref(session_id)
                        if actual_kind is not SurfaceKind.WEB
                        else web_primary_surface_ref(session_id)
                    ),
                )
            elif (
                existing.task_id != task_id
                or existing.surface_kind is not actual_kind
                or existing.status
                not in {SurfaceSessionStatus.ACTIVE, SurfaceSessionStatus.DRAINING}
                or (
                    actual_kind is not SurfaceKind.WEB
                    and (
                        existing.session_owner != self._session_owner
                        or existing.platform_build_ref != self._platform_build_ref
                        or existing.device_ref != self._device_ref
                    )
                )
            ):
                raise SurfaceSessionPersistenceError(
                    "Web Session 归属或生命周期不允许取得命令租约"
                    if actual_kind is SurfaceKind.WEB
                    else "App Session 归属、身份或生命周期不允许取得命令租约"
                )
            permit = await self._repository.acquire_command(
                task_id=task_id,
                session_id=session_id,
                surface_kind=actual_kind,
                lease_owner=self._lease_owner,
                lease_token=str(uuid4()),
                lease_seconds=self._lease_seconds,
                allow_draining=allow_draining,
            )
            acquired_current_capture_id = permit.current_capture_id
            stop_renewal = asyncio.Event()
            renewal = asyncio.create_task(
                self._renew_until_stopped(permit=permit, stop=stop_renewal)
            )
            body_completed = False
            try:
                yield permit
                body_completed = True
            finally:
                stop_renewal.set()
                try:
                    await renewal
                except Exception:  # noqa: BLE001 - 最终释放仍会校验真实租约
                    pass
                try:
                    release_current_capture_id = permit.current_capture_id
                    if release_current_capture_id == acquired_current_capture_id:
                        release_current_capture_id = None
                    released = await self._repository.release_command(
                        permit=permit,
                        current_capture_id=release_current_capture_id,
                    )
                except Exception:
                    if body_completed:
                        raise
                    released = False
                if body_completed and not released:
                    raise SurfaceSessionPersistenceError(
                        "Session 命令已完成但租约释放失败，禁止返回普通成功"
                    )

    async def release_session(self, *, task_id: str, session_id: str) -> None:
        """只释放进程内绑定；权威会话必须经 draining/closed 状态机关闭。"""
        await self._local_gate.release_session(task_id=task_id, session_id=session_id)

    async def cancel_session(
        self,
        *,
        task_id: str,
        session_id: str,
        reason: str = "canceled",
    ) -> None:
        """排期第 13 列 cancel：清 PG Session Lease，并作废本进程绑定。

        内部复用 ``mark_lost``（清空 owner/token/expiry），旧 Worker 写回必然失败。
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须为非空字符串")
        reason_text = reason.strip()
        snapshot = await self._repository.get(session_id=session_id)
        if snapshot is not None:
            if snapshot.task_id != task_id:
                raise ValueError("Surface Session 不属于当前任务，禁止取消")
            if not snapshot.status.is_terminal:
                try:
                    await self._repository.mark_lost(
                        task_id=task_id,
                        session_id=session_id,
                        expected_row_version=snapshot.row_version,
                        reason=reason_text,
                    )
                except SurfaceSessionPersistenceError:
                    # 并发取消/状态已推进时，仍继续清本进程绑定。
                    pass
        await self._local_gate.cancel_session(
            task_id=task_id,
            session_id=session_id,
            reason=reason_text,
        )

    async def cancel_expired_session(
        self,
        *,
        task_id: str,
        session_id: str,
        reason: str,
    ) -> bool:
        """仅清理数据库已确认过期的 Web 命令租约，并失效本地绑定。"""

        reason_text = _require_text(reason, "reason")
        cancelled = await self._repository.mark_web_lost_if_command_lease_expired(
            task_id=task_id,
            session_id=session_id,
            reason=reason_text,
        )
        if cancelled:
            await self._local_gate.cancel_session(
                task_id=task_id,
                session_id=session_id,
                reason=reason_text,
            )
        return cancelled

    async def _renew_until_stopped(
        self,
        *,
        permit: SessionCommandPermit,
        stop: asyncio.Event,
    ) -> None:
        interval = max(self._lease_seconds // 3, 5)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                renewed = await self._repository.renew_command(
                    permit=permit,
                    lease_seconds=self._lease_seconds,
                )
                permit.lease_expires_at = renewed.lease_expires_at


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
