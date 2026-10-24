"""App Session 生产收口：先 seal 再关；失败写 lost，禁止残留 active。"""

from __future__ import annotations

import logging
from typing import Any

from services.api.app.domain.interaction import SurfaceDriver, SurfaceKind, SurfaceSessionStatus
from services.api.app.domain.interaction.surface_driver import SurfaceSessionRef

logger = logging.getLogger(__name__)


async def close_app_runtime_session(
    *,
    driver: SurfaceDriver | None,
    surface_sessions: Any,
    task_id: str,
    session_id: str,
    reason: str,
    session_command_gate: Any | None = None,
    defer_close: bool = False,
) -> str:
    """按 Web 关闭顺序 seal Session；返回 closed / lost / skipped / draining。

    defer_close=True 表示暂不截止：只 begin_draining，禁止立刻 seal／lost。
    """
    if surface_sessions is None:
        return "skipped"
    get = getattr(surface_sessions, "get", None)
    if not callable(get):
        return "skipped"
    try:
        snapshot = await get(session_id=session_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "task_id=%s session_id=%s 读取 Session 失败，尝试 mark_lost",
            task_id,
            session_id,
            exc_info=True,
        )
        return await _force_lost(
            surface_sessions=surface_sessions,
            task_id=task_id,
            session_id=session_id,
            expected_row_version=0,
            reason=f"{reason}:session_lookup_failed",
            session_command_gate=session_command_gate,
        )
    if snapshot is None or snapshot.task_id != task_id:
        return "skipped"
    if snapshot.surface_kind is SurfaceKind.WEB:
        return "skipped"
    if snapshot.status.is_terminal:
        await _release_command_gate(
            session_command_gate=session_command_gate,
            task_id=task_id,
            session_id=session_id,
        )
        return snapshot.status.value
    try:
        if snapshot.status is SurfaceSessionStatus.ACTIVE:
            snapshot = await surface_sessions.begin_draining(
                task_id=task_id,
                session_id=session_id,
                expected_row_version=snapshot.row_version,
            )
        if defer_close and snapshot.status is SurfaceSessionStatus.DRAINING:
            # 暂不截止：只停新动作，不 seal／lost，保留 draining 待原写入落地。
            return SurfaceSessionStatus.DRAINING.value
        if snapshot.status is SurfaceSessionStatus.DRAINING:
            snapshot = await surface_sessions.seal_for_close(
                task_id=task_id,
                session_id=session_id,
                expected_row_version=snapshot.row_version,
            )
        if snapshot.status is SurfaceSessionStatus.CLOSED:
            await _release_command_gate(
                session_command_gate=session_command_gate,
                task_id=task_id,
                session_id=session_id,
            )
            return SurfaceSessionStatus.CLOSED.value
        if snapshot.status is not SurfaceSessionStatus.CLOSING:
            return await _force_lost(
                surface_sessions=surface_sessions,
                task_id=task_id,
                session_id=session_id,
                expected_row_version=snapshot.row_version,
                reason=f"{reason}:not_closable:{snapshot.status.value}",
                session_command_gate=session_command_gate,
            )
        await _close_driver(
            driver=driver,
            snapshot=snapshot,
            task_id=task_id,
            session_id=session_id,
            reason=reason,
        )
        await surface_sessions.mark_closed(
            task_id=task_id,
            session_id=session_id,
            expected_row_version=snapshot.row_version,
        )
        await _release_command_gate(
            session_command_gate=session_command_gate,
            task_id=task_id,
            session_id=session_id,
        )
        return SurfaceSessionStatus.CLOSED.value
    except Exception:  # noqa: BLE001 - 关闭失败必须 lost，禁止停在 active
        logger.warning(
            "task_id=%s session_id=%s App Session seal/close 失败，写入 lost",
            task_id,
            session_id,
            exc_info=True,
        )
        current = await _reload(get, session_id)
        return await _force_lost(
            surface_sessions=surface_sessions,
            task_id=task_id,
            session_id=session_id,
            expected_row_version=getattr(current, "row_version", snapshot.row_version),
            reason=f"{reason}:close_failed",
            session_command_gate=session_command_gate,
        )


async def _close_driver(
    *,
    driver: SurfaceDriver | None,
    snapshot: Any,
    task_id: str,
    session_id: str,
    reason: str,
) -> None:
    if driver is None or not hasattr(driver, "close_session"):
        return
    surface_ref = snapshot.current_surface_ref or snapshot.device_ref
    if not surface_ref:
        return
    session = SurfaceSessionRef(
        task_id=task_id,
        session_id=session_id,
        surface_kind=snapshot.surface_kind,
        primary_surface_ref=surface_ref,
    )
    await driver.close_session(session=session, reason=reason)


async def _force_lost(
    *,
    surface_sessions: Any,
    task_id: str,
    session_id: str,
    expected_row_version: int,
    reason: str,
    session_command_gate: Any | None,
) -> str:
    mark_lost = getattr(surface_sessions, "mark_lost", None)
    if callable(mark_lost):
        try:
            await mark_lost(
                task_id=task_id,
                session_id=session_id,
                expected_row_version=expected_row_version,
                reason=reason,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "task_id=%s session_id=%s mark_lost 失败",
                task_id,
                session_id,
                exc_info=True,
            )
    await _release_command_gate(
        session_command_gate=session_command_gate,
        task_id=task_id,
        session_id=session_id,
    )
    return SurfaceSessionStatus.LOST.value


async def _reload(get: Any, session_id: str) -> Any | None:
    try:
        return await get(session_id=session_id)
    except Exception:  # noqa: BLE001
        return None


async def _release_command_gate(
    *,
    session_command_gate: Any | None,
    task_id: str,
    session_id: str,
) -> None:
    release = getattr(session_command_gate, "release_session", None)
    if not callable(release):
        return
    try:
        await release(task_id=task_id, session_id=session_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "task_id=%s session_id=%s 释放命令门禁失败",
            task_id,
            session_id,
            exc_info=True,
        )
