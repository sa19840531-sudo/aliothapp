"""设备租约结算：区分「暂态截图故障」与「真机丢失」。"""

from __future__ import annotations

from services.api.app.application.device_pool import (
    quarantine_device_lease_for_session,
    release_device_lease_for_session,
)

# 可重试 Official／Capture 故障不得把云手机打进 quarantine，否则外部连同一台也会「整池假死」。
_TRANSIENT_DEVICE_LOSS_MARKERS = frozenset(
    {
        "OFFICIAL_API_TIMEOUT",
        "OFFICIAL_HTTP_408",
        "OFFICIAL_HTTP_429",
        "OFFICIAL_HTTP_502",
        "OFFICIAL_HTTP_503",
        "OFFICIAL_HTTP_504",
        "CAPTURE_FAILED",
        "CAPTURE_CONTRACT_INVALID",
        "DEVICE_CAPTURE_FAILED",
        "APP_CAPTURE_TRANSPORT_FAILED",
        "APP_ACTION_AFTER_CAPTURE_MISSING",
        "APP_AFTER_CAPTURE_MISSING",
    }
)


# 任務取消只停新動作的帳本，不得因此放開或隔離雲手機，否則錄製流程跑不完。
_LEASE_PRESERVE_MARKERS = (
    "APP_WORKFLOW_CANCELED",
    "WORKFLOW_CANCELED",
    "workflow_canceled",
)


def preserves_cloud_phone_lease(reason: str | None) -> bool:
    """取消類原因保持雲手機租約，讓尚未跑完的質檢能繼續用同一台手機。"""

    text = str(reason or "")
    return any(marker in text for marker in _LEASE_PRESERVE_MARKERS)


def is_transient_device_loss_reason(reason: str | None) -> bool:
    """判断 Session LOST 原因是否属于可重试截图／Official 暂态故障。"""

    text = str(reason or "").upper()
    if not text:
        return False
    return any(marker in text for marker in _TRANSIENT_DEVICE_LOSS_MARKERS)


async def settle_device_lease_after_session_loss(
    *,
    session_id: str,
    reason: str,
) -> None:
    """Session LOST 后结算租约：暂态故障优先 release，真机故障才 quarantine。"""

    if preserves_cloud_phone_lease(reason):
        return
    if is_transient_device_loss_reason(reason):
        released = await release_device_lease_for_session(session_id=session_id)
        if released:
            return
        await quarantine_device_lease_for_session(
            session_id=session_id,
            reason=f"app_session_soft_release_unconfirmed:{reason}",
        )
        return
    await quarantine_device_lease_for_session(
        session_id=session_id,
        reason=f"app_session_lost:{reason}",
    )
