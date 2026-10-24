"""App 活会话规则增量发布与排空的 PostgreSQL 事实读取。"""

from __future__ import annotations

from typing import Any

from services.api.app.application.app_live_inspection_publish import (
    AppActiveSessionDrainState,
    AppLiveInspectionCapture,
)
from services.api.app.infrastructure.db.app_action_scene_repository import (
    startup_result_pair_candidate_sql,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

# 与 AppCaptureInspectionPublisher._POPUP_OBSTRUCTION_PAGE_STATES 对齐：
# 遮挡帧常无 visual_page_type，但仍须进入发布，由宿主回退挂上弹窗／广告规则。
_POPUP_OBSTRUCTION_PAGE_STATE_SQL = (
    "lower(btrim(COALESCE(visual.page_state, ''))) "
    "IN ('popup', 'modal', 'overlay', 'dialog')"
)

_PENDING_CAPTURE_SQL = f"""
    SELECT
        capture.id::text AS capture_id,
        visual.visual_page_type AS page_type,
        COALESCE(visual.page_state, 'stable') AS page_state
    FROM qc_task AS task
    INNER JOIN qc_agent_run AS run
        ON run.task_id = task.id
       AND run.id = $2::uuid
       AND run.status = 'running'
    INNER JOIN qc_capture AS capture
        ON capture.task_id = task.id
       AND capture.agent_run_id = run.id
       AND capture.capture_status = 'captured'
       AND capture.surface_kind IN ('android', 'ios')
    INNER JOIN qc_surface_session AS session
        ON session.task_id = task.id
       AND session.session_id = capture.surface_session_id
       AND session.surface_kind = capture.surface_kind
       AND session.status = 'active'
       AND session.finalization_epoch = task.finalization_epoch
    INNER JOIN qc_app_capture AS app_capture ON app_capture.capture_id = capture.id
    INNER JOIN LATERAL (
        SELECT observation.visual_page_type, observation.page_state
        FROM qc_app_visual_observation AS observation
        WHERE observation.task_id = task.id
          AND observation.capture_id = capture.id
          AND observation.session_id = session.session_id
          AND observation.screenshot_artifact_id = app_capture.screenshot_artifact_id
        ORDER BY observation.created_at DESC, observation.observation_id DESC
        LIMIT 1
    ) AS visual ON (
        NULLIF(btrim(visual.visual_page_type), '') IS NOT NULL
        OR {_POPUP_OBSTRUCTION_PAGE_STATE_SQL}
        OR {startup_result_pair_candidate_sql()}
    )
    WHERE task.id = $1::uuid
      AND task.target_platform = 'app'
      AND task.status = 'running'
      AND task.finalization_state IN (
          'open', 'exploration_draining', 'session_draining'
      )
      AND task.task_config->>'execution_mode' = 'full_inspection'
      AND COALESCE(capture.metadata->>'inspection_status', '') NOT IN (
          'published',
          'snapshot_rules_deferred',
          'evidence_rules_deferred',
          'no_rules_applicable',
          'out_of_scope',
          'unmatched_page_type',
          'active_session_rules_excluded'
      )
"""


class PostgresAppLiveInspectionRepository:
    """以任务、Run、Session 与最新视觉事实筛选待发布 App Capture。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_pending_captures(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> tuple[AppLiveInspectionCapture, ...]:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _PENDING_CAPTURE_SQL + " ORDER BY capture.captured_at, capture.id",
                task_id,
                agent_run_id,
            )
        return tuple(
            AppLiveInspectionCapture(
                capture_id=str(row["capture_id"]),
                page_type=str(row["page_type"]) if row["page_type"] is not None else None,
                page_state=str(row["page_state"] or "stable").strip() or "stable",
            )
            for row in rows
        )

    async def load_drain_state(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> AppActiveSessionDrainState:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                WITH pending_capture AS (
                """
                + _PENDING_CAPTURE_SQL
                + """
                ), nonterminal_work AS (
                    SELECT work_item.status, work_item.next_attempt_at
                    FROM qc_inspection_work_item AS work_item
                    INNER JOIN qc_task AS task ON task.id = work_item.task_id
                    WHERE work_item.task_id = $1::uuid
                      AND work_item.producer_agent_run_id = $2::uuid
                      AND work_item.session_owner_agent_run_id = $2::uuid
                      AND work_item.session_dependency = 'active_session'
                      AND work_item.control_mode = 'enforced'
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.status NOT IN (
                          'succeeded', 'partial', 'failed',
                          'cancelled', 'superseded'
                      )
                )
                SELECT
                    (SELECT count(*)::integer FROM pending_capture)
                        AS pending_capture_count,
                    (SELECT count(*)::integer FROM nonterminal_work)
                        AS nonterminal_work_item_count,
                    (SELECT min(next_attempt_at) FROM nonterminal_work)
                        AS next_attempt_at
                """,
                task_id,
                agent_run_id,
            )
        if row is None:
            return AppActiveSessionDrainState(
                pending_capture_count=0,
                nonterminal_work_item_count=0,
            )
        return AppActiveSessionDrainState(
            pending_capture_count=int(row["pending_capture_count"] or 0),
            nonterminal_work_item_count=int(row["nonterminal_work_item_count"] or 0),
            next_attempt_at=row["next_attempt_at"],
        )
