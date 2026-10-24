"""H5 取消事实与连接归还故障分离；未知事实仍失败关闭。"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def read_h5_cancel_requested(*, pool: Any, run_id: str) -> bool:
    requested = None
    try:
        async with pool.acquire() as connection:
            requested = await connection.fetchval(
                """
                SELECT (run.status = 'canceled' OR COALESCE(
                    (run.output_context->>'cancel_requested')::boolean, FALSE))
                FROM qc_agent_run AS run JOIN qc_task AS task ON task.id = run.task_id
                WHERE run.id = $1::uuid AND lower(task.target_platform) = 'h5'
                """, run_id,
            )
    except TimeoutError:
        # 只有布尔查询已成功返回时才保留事实，获取/查询失败不得变成未取消。
        if type(requested) is not bool:
            raise
        logger.warning(
            "H5 run_id=%s 取消事实已读取，但 connection 归还失败；保留本次事实。",
            run_id, exc_info=True,
        )
    if type(requested) is not bool:
        raise RuntimeError("H5 长期取消事实缺失，拒绝继续执行")
    return requested
