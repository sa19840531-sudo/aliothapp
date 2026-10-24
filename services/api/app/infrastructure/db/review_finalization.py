"""人工复核只提交事实，已接管的任务继续由 Completion Gate 收口。"""

from typing import Any


async def wake_review_finalization(
    connection: Any,
    *,
    task_id: str,
) -> bool:
    """在复核事务内唤醒未封存任务，返回是否应跳过旧版直接完成流程。"""

    task = await connection.fetchrow(
        """
        SELECT status, finalization_state, finalization_agent_run_id
        FROM qc_task
        WHERE id = $1::uuid
        FOR UPDATE
        """,
        task_id,
    )
    if task is None or task.get("finalization_agent_run_id") is None:
        return False
    if task["finalization_state"] not in {
        "open", "exploration_draining", "session_draining",
    }:
        return False
    # 复核不得解除技术隔离、取消或失败；这些状态仍由原恢复合同处理。
    await connection.execute(
        """
        UPDATE qc_task
        SET finalization_retry_at = now()
        WHERE id = $1::uuid
          AND finalization_state IN ('open', 'exploration_draining', 'session_draining')
          AND finalization_recovery_state = 'active'
          AND status IN ('running', 'review', 'done')
        """,
        task_id,
    )
    return True
