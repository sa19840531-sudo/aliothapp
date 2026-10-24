"""Web 正式质检 Session 的可恢复关闭端口。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from services.api.app.infrastructure.db.finalization_recovery_fence import (
    assert_finalization_recovery_fence,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class WebFormalSessionRuntime(Protocol):
    """执行正常 Session seal、浏览器释放与持久化终态写入。"""

    async def close_session(self, *, task_id: str, session_id: str) -> None: ...


class DurableWebFormalSessionCloser:
    """优先关闭本进程浏览器；重启后由可重建 Runtime 对账 PostgreSQL Session。"""

    def __init__(
        self,
        *,
        runtime_factory: Callable[[], WebFormalSessionRuntime],
        database_url: str | None = None,
        pool: Any | None = None,
    ) -> None:
        if not callable(runtime_factory):
            raise TypeError("runtime_factory 必须可调用")
        self._runtime_factory = runtime_factory
        self._database_url = database_url
        self._pool = pool
        self._live_runtimes: dict[str, WebFormalSessionRuntime] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def bind_live_runtime(
        self,
        *,
        session_id: str,
        runtime: WebFormalSessionRuntime,
    ) -> None:
        session = _require_web_session_id(session_id)
        if not callable(getattr(runtime, "close_session", None)):
            raise TypeError("runtime 必须实现 close_session")
        existing = self._live_runtimes.get(session)
        if existing is not None and existing is not runtime:
            raise RuntimeError("同一 Web Session 不允许绑定不同浏览器 Runtime")
        self._live_runtimes[session] = runtime

    async def close(
        self,
        *,
        task_id: str,
        session_id: str,
        cancel_active_commands: bool = False,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> None:
        task = _require_text(task_id, "task_id")
        session = _require_web_session_id(session_id)
        lock = self._locks.setdefault(session, asyncio.Lock())
        async with lock:
            await self._assert_recovery_fence(
                task_id=task,
                recovery_lease_token=recovery_lease_token,
                recovery_generation=recovery_generation,
                recovery_lease_expires_at=recovery_lease_expires_at,
            )
            runtime = self._live_runtimes.get(session)
            if runtime is None:
                runtime = self._runtime_factory()
            await _close_runtime(
                runtime,
                task_id=task,
                session_id=session,
                cancel_active_commands=cancel_active_commands,
            )
            self._live_runtimes.pop(session, None)
        self._locks.pop(session, None)

    async def _assert_recovery_fence(
        self,
        *,
        task_id: str,
        recovery_lease_token: str | None,
        recovery_generation: int | None,
        recovery_lease_expires_at: datetime | None,
    ) -> None:
        if (
            recovery_lease_token is None
            and recovery_generation is None
            and recovery_lease_expires_at is None
        ):
            return
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            await assert_finalization_recovery_fence(
                connection,
                task_id=task_id,
                recovery_lease_token=recovery_lease_token,
                recovery_generation=recovery_generation,
                recovery_lease_expires_at=recovery_lease_expires_at,
            )


async def _close_runtime(
    runtime: WebFormalSessionRuntime,
    *,
    task_id: str,
    session_id: str,
    cancel_active_commands: bool = False,
) -> None:
    sessions = getattr(runtime, "surface_sessions", None)
    get_session = getattr(sessions, "get", None)
    if not callable(get_session):
        if cancel_active_commands:
            raise RuntimeError("Web 取消收口缺少 Session 命令撤销能力")
        await runtime.close_session(task_id=task_id, session_id=session_id)
        return
    snapshot = await get_session(session_id=session_id)
    if snapshot is not None:
        if getattr(snapshot, "task_id", task_id) != task_id:
            raise RuntimeError("Surface Session 不属于当前 Web 任务")
        raw_status = getattr(snapshot, "status", None)
        status = getattr(raw_status, "value", raw_status)
        if status not in {"closed", "lost", "failed"}:
            command_gate = getattr(runtime, "session_command_gate", None)
            if cancel_active_commands:
                cancel_session = getattr(command_gate, "cancel_session", None)
                if not callable(cancel_session):
                    raise RuntimeError("Web 取消收口缺少 Session 命令撤销能力")
                await cancel_session(
                    task_id=task_id,
                    session_id=session_id,
                    reason="web_formal_run_canceled",
                )
                refresh_required = True
            else:
                cancel_expired_session = getattr(
                    command_gate,
                    "cancel_expired_session",
                    None,
                )
                refresh_required = bool(
                    callable(cancel_expired_session)
                    and await cancel_expired_session(
                        task_id=task_id,
                        session_id=session_id,
                        reason="web_formal_orphaned_command_lease_expired",
                    )
                )
            if refresh_required:
                snapshot = await get_session(session_id=session_id)
                if snapshot is None:
                    status = "lost"
                else:
                    if getattr(snapshot, "task_id", task_id) != task_id:
                        raise RuntimeError("Surface Session 不属于当前 Web 任务")
                    raw_status = getattr(snapshot, "status", None)
                    status = getattr(raw_status, "value", raw_status)
        if status not in {"closed", "lost", "failed"}:
            await runtime.close_session(task_id=task_id, session_id=session_id)
            return

    close_browser_session = getattr(runtime, "close_browser_session", None)
    command_gate = getattr(runtime, "session_command_gate", None)
    release_session = getattr(command_gate, "release_session", None)
    if not callable(close_browser_session) or not callable(release_session):
        raise RuntimeError("缺失 Surface Session 时无法释放 Web 浏览器与命令闸门")
    try:
        await close_browser_session(session_id)
    finally:
        await release_session(task_id=task_id, session_id=session_id)


def _require_web_session_id(value: object) -> str:
    session_id = _require_text(value, "session_id")
    if not session_id.startswith("web:"):
        raise ValueError("session_id 必须是 Web Session")
    return session_id


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


__all__ = ["DurableWebFormalSessionCloser", "WebFormalSessionRuntime"]
