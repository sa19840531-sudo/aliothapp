"""Web 正式收口恢复租约的事务内 fencing 公共校验。"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class FinalizationRecoveryFenceError(RuntimeError):
    """恢复 holder 已过期、被 reclaim 或不属于正式 Web 范围。"""


async def assert_finalization_recovery_fence(
    connection: Any,
    *,
    task_id: str,
    recovery_lease_token: str | None,
    recovery_generation: int | None,
    recovery_lease_expires_at: datetime | None,
) -> None:
    """在变更事务中锁定 Task，并拒绝失效 holder 或绕过活跃租约。"""

    fence = (
        recovery_lease_token,
        recovery_generation,
        recovery_lease_expires_at,
    )
    if any(value is not None for value in fence) and not all(
        value is not None for value in fence
    ):
        raise FinalizationRecoveryFenceError("收口恢复 fencing 字段不完整")
    if recovery_lease_token is None:
        valid = await connection.fetchrow(
            """
            SELECT id FROM qc_task
            WHERE id = $1::uuid
              AND (
                  finalization_recovery_lease_token IS NULL
                  OR finalization_recovery_lease_expires_at <= now()
              )
            FOR UPDATE
            """,
            task_id,
        )
    else:
        valid = await connection.fetchrow(
            """
            SELECT id FROM qc_task
            WHERE id = $1::uuid
              AND finalization_recovery_lease_token = $2::uuid
              AND finalization_recovery_generation = $3
              AND finalization_recovery_lease_expires_at > now()
              AND finalization_recovery_lease_expires_at >= $4::timestamptz
              AND finalization_recovery_state = 'active'
              AND target_platform = 'web'
              AND LOWER(COALESCE(task_config->>'workflow_version', '')) = 'v2'
              AND task_config->>'execution_mode' = 'full_inspection'
              AND task_config->>'web_inspection_notification_mode' = 'enforced'
            FOR UPDATE
            """,
            task_id,
            recovery_lease_token,
            recovery_generation,
            recovery_lease_expires_at,
        )
    if not valid:
        raise FinalizationRecoveryFenceError(
            "收口恢复租约已过期或 fencing 身份已变化"
        )
