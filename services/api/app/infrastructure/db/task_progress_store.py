"""任务过程可视化写仓储：领域日志、旅程页面节点与页面识别指标的落库出口。

只被 Worker 旁路观察者（``agents/runtime/progress_observer.py``）调用；全部写入
都是幂等或追加语义，失败由调用方按 fire-and-forget 记日志兜底，绝不影响 Agent
判定链路。
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.task_progress import JourneyStep
from services.api.app.infrastructure.db.page_exploration_repository import (
    PostgresPageExplorationRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

logger = logging.getLogger(__name__)

_DISCOVERED_INSERT_RETRIES = 3


class PostgresTaskProgressStore:
    """基于 PostgreSQL 的任务过程写仓储。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool
        self._exploration_repository = PostgresPageExplorationRepository(
            database_url=database_url,
            pool=pool,
        )

    async def insert_log(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        level: str,
        message: str,
        source: str = "observer",
        page_capture_id: str | None = None,
        rule_execution_id: str | None = None,
        context: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO qc_task_log (
                task_id, agent_run_id, ts, level, source, message,
                page_capture_id, rule_execution_id, context
            )
            VALUES (
                $1::uuid, $2::uuid, $3, $4, $5, $6, $7::uuid, $8::uuid, $9::jsonb
            )
            """,
            task_id,
            agent_run_id,
            ts or datetime.now(UTC),
            level,
            source,
            message,
            page_capture_id,
            rule_execution_id,
            json.dumps(context or {}, ensure_ascii=False, default=str),
        )

    async def ensure_journey_nodes(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        steps: list[JourneyStep],
    ) -> None:
        """任务启动时物化旅程计划节点；重复调用/重入安全（按 node_seq 幂等）。"""
        for index, step in enumerate(steps, start=1):
            await self._execute(
                """
                INSERT INTO qc_task_page_node (
                    task_id, agent_run_id, node_seq, source, step_name, page_type, status
                )
                VALUES ($1::uuid, $2::uuid, $3, 'journey', $4, $5, 'pending')
                ON CONFLICT (agent_run_id, node_seq) DO NOTHING
                """,
                task_id,
                agent_run_id,
                index,
                step.step_name,
                step.page_type,
            )

    async def attach_capture(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        page_capture_id: str,
        page_type: str | None,
        step_name: str,
        recognition_status: str | None,
    ) -> None:
        """把一次页面采集挂到旅程节点上。

        匹配顺序：已挂同一采集的节点 → 同 page_type 的最早 pending 计划节点 →
        追加 source=discovered 的计划外节点（含识别失败、旅程外页面）。
        ``recognition_status`` 为 None 时只建立关联，不改识别结论。
        """
        updated = await self._execute_with_count(
            """
            UPDATE qc_task_page_node
            SET
                status = COALESCE($3, status),
                finished_at = CASE WHEN $3 IS NOT NULL THEN now() ELSE finished_at END,
                updated_at = now()
            WHERE agent_run_id = $1::uuid
              AND page_capture_id = $2::uuid
            """,
            agent_run_id,
            page_capture_id,
            recognition_status,
        )
        if updated > 0:
            return

        if page_type is not None:
            claimed = await self._execute_with_count(
                """
                UPDATE qc_task_page_node
                SET
                    page_capture_id = $2::uuid,
                    status = COALESCE($4, 'visiting'),
                    first_visited_at = COALESCE(first_visited_at, now()),
                    finished_at = CASE WHEN $4 IS NOT NULL THEN now() ELSE finished_at END,
                    updated_at = now()
                WHERE id = (
                    SELECT id
                    FROM qc_task_page_node
                    WHERE agent_run_id = $1::uuid
                      AND page_capture_id IS NULL
                      AND status = 'pending'
                      AND page_type = $3
                    ORDER BY node_seq
                    LIMIT 1
                )
                """,
                agent_run_id,
                page_capture_id,
                page_type,
                recognition_status,
            )
            if claimed > 0:
                return

        for _ in range(_DISCOVERED_INSERT_RETRIES):
            inserted = await self._execute_with_count(
                """
                INSERT INTO qc_task_page_node (
                    task_id, agent_run_id, node_seq, source, step_name, page_type,
                    status, page_capture_id, first_visited_at, finished_at
                )
                SELECT
                    $1::uuid,
                    $2::uuid,
                    COALESCE(MAX(node_seq), 0) + 1,
                    'discovered',
                    $3,
                    $4,
                    COALESCE($6, 'visiting'),
                    $5::uuid,
                    now(),
                    CASE WHEN $6 IS NOT NULL THEN now() ELSE NULL END
                FROM qc_task_page_node
                WHERE agent_run_id = $2::uuid
                ON CONFLICT (agent_run_id, node_seq) DO NOTHING
                """,
                task_id,
                agent_run_id,
                step_name,
                page_type,
                page_capture_id,
                recognition_status,
            )
            if inserted > 0:
                return
        logger.warning(
            "task_id=%s page_capture_id=%s 追加计划外页面节点失败（并发冲突重试耗尽）。",
            task_id,
            page_capture_id,
        )

    async def set_candidate_total(
        self, *, agent_run_id: str, page_capture_id: str, total: int
    ) -> None:
        await self._execute(
            """
            UPDATE qc_task_page_node
            SET candidate_rule_total = $3, updated_at = now()
            WHERE agent_run_id = $1::uuid AND page_capture_id = $2::uuid
            """,
            agent_run_id,
            page_capture_id,
            total,
        )

    async def set_current_rule(
        self,
        *,
        agent_run_id: str,
        page_capture_id: str | None,
        rule_code: str | None,
    ) -> None:
        """更新"正在执行规则"指针；page_capture_id 为 None 时清空整个 Run 的指针。"""
        if page_capture_id is None:
            await self._execute(
                """
                UPDATE qc_task_page_node
                SET current_rule_code = NULL, updated_at = now()
                WHERE agent_run_id = $1::uuid AND current_rule_code IS NOT NULL
                """,
                agent_run_id,
            )
            return
        await self._execute(
            """
            UPDATE qc_task_page_node
            SET current_rule_code = $3, updated_at = now()
            WHERE agent_run_id = $1::uuid AND page_capture_id = $2::uuid
            """,
            agent_run_id,
            page_capture_id,
            rule_code,
        )

    async def finalize_run_nodes(self, *, agent_run_id: str) -> None:
        """Run 终态收尾：未访问的计划节点转已跳过，清空执行中指针。"""
        await self._execute(
            """
            UPDATE qc_task_page_node
            SET
                status = CASE WHEN status = 'pending' THEN 'skipped' ELSE status END,
                current_rule_code = NULL,
                finished_at = COALESCE(finished_at, now()),
                updated_at = now()
            WHERE agent_run_id = $1::uuid
            """,
            agent_run_id,
        )

    async def checkpoint_page_exploration(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        working_memory: dict[str, Any],
    ) -> None:
        """委托专用仓储幂等持久化动态 Frontier 与真实页面终态。"""
        await self._exploration_repository.checkpoint(
            task_id=task_id,
            agent_run_id=agent_run_id,
            working_memory=working_memory,
        )

    async def update_capture_detection(
        self,
        *,
        page_capture_id: str,
        detection_status: str,
        page_type: str | None,
        confidence: float | None,
        duration_ms: int | None,
    ) -> None:
        await self._execute(
            """
            UPDATE qc_page_capture
            SET
                detection_status = $2,
                page_type = COALESCE($3, page_type),
                detection_confidence = $4,
                detection_duration_ms = $5,
                updated_at = now()
            WHERE id = $1::uuid
            """,
            page_capture_id,
            detection_status,
            page_type,
            confidence,
            duration_ms,
        )

    async def update_capture_element_count(
        self, *, page_capture_id: str, element_count: int
    ) -> None:
        await self._execute(
            """
            UPDATE qc_page_capture
            SET element_count = $2, updated_at = now()
            WHERE id = $1::uuid
            """,
            page_capture_id,
            element_count,
        )

    async def fetch_capture_dom_path(self, *, page_capture_id: str) -> str | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return await connection.fetchval(
                "SELECT dom_path FROM qc_page_capture WHERE id = $1::uuid",
                page_capture_id,
            )

    async def _execute(self, query: str, *args: Any) -> None:
        await self._execute_with_count(query, *args)

    async def _execute_with_count(self, query: str, *args: Any) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            status = await connection.execute(query, *args)
        try:
            return int(str(status).rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return 0

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)
