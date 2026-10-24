"""Web 阻断页的诊断分类，不生成业务页识别或完成证据。"""

from __future__ import annotations

import json

from services.api.app.infrastructure.db.pool import get_postgres_pool


class WebPopupBlockedPersistenceError(RuntimeError):
    """阻断证据归属或运行围栏失效；reason_code 是稳定的失效原因，便于事后定位。"""

    def __init__(self, message: str, *, reason_code: str = "unknown"):
        super().__init__(message)
        self.reason_code = reason_code


class PostgresWebPopupBlockedRepository:
    def __init__(self, *, pool=None, database_url=None):
        self._pool = pool
        self._database_url = database_url

    async def record(self, *, task_id, agent_run_id, session_id, capture_id, blocking_reasons):
        # 只收稳定原因码，模型原文与异常正文不能进入诊断字段。
        reasons = tuple(dict.fromkeys(
            reason for reason in blocking_reasons
            if isinstance(reason, str)
            and (reason == "popup_blocked" or reason.startswith("WEB_OBSTRUCTION_"))
            and len(reason) <= 128
            and reason.replace("_", "").isalnum()
        ))
        metadata = {
            "is_obstructed": True,
            "web_popup_blocked": {
                "schema_version": "web-popup-blocked.v1",
                "page_type": "popup_ad",
                "page_state": "popup_blocked",
                "capture_id": capture_id,
                "blocking_reasons": list(reasons),
            },
        }
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                _RECORD_POPUP_BLOCKED_SQL,
                task_id, agent_run_id, session_id, capture_id,
                json.dumps(metadata, ensure_ascii=False),
            )
            if row is None or str(row["capture_id"]) != capture_id:
                if row is None:
                    diagnosis = await connection.fetchrow(
                        _DIAGNOSE_STALE_SQL, task_id, session_id, capture_id
                    )
                    reason = _stale_reason(
                        diagnosis,
                        agent_run_id=agent_run_id,
                        session_id=session_id,
                        capture_id=capture_id,
                    )
                else:
                    reason = "capture_mismatch"
                raise WebPopupBlockedPersistenceError(
                    f"Web 弹窗阻断证据已失效，拒绝写入（{reason}）", reason_code=reason
                )
            return bool(row["recorded"])


_RECORD_POPUP_BLOCKED_SQL = """
WITH qualified AS (
    SELECT page.id, page.page_type
    FROM qc_task AS task
    JOIN qc_agent_run AS run ON run.task_id = task.id AND run.id = $2::uuid
      AND run.agent_type = 'main'
    JOIN qc_surface_session AS session ON session.task_id = task.id
      AND session.session_id = $3 AND session.session_id = 'web:' || run.id::text
      AND session.surface_kind = 'web' AND session.session_owner = 'web-v2-workflow'
      AND session.status = 'active' AND session.finalization_epoch = task.finalization_epoch
    JOIN qc_capture AS capture ON capture.task_id = task.id AND capture.id = $4::uuid
      AND capture.agent_run_id = run.id AND capture.surface_kind = 'web'
      AND capture.surface_session_id = session.session_id
      AND capture.id = session.current_capture_id AND capture.capture_status = 'captured'
      AND capture.metadata->>'finalization_epoch' = task.finalization_epoch::text
    JOIN qc_page_capture AS page ON page.id = capture.id AND page.task_id = task.id
      AND page.agent_run_id = run.id AND page.surface_session_id = session.session_id
      AND page.capture_status = 'captured'
      AND page.metadata->>'finalization_epoch' = task.finalization_epoch::text
    WHERE task.id = $1::uuid AND task.target_platform = 'web'
      AND LOWER(task.task_config->>'workflow_version') = 'v2'
      AND task.execution_status <> 'canceled' AND task.finalization_state <> 'sealed'
      AND run.id = (SELECT latest.id FROM qc_agent_run AS latest
          WHERE latest.task_id = task.id AND latest.agent_type = 'main'
          ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1)
      AND page.url ~ '^https?://'
    FOR UPDATE OF task, run, session, capture, page
), updated_page AS (
    UPDATE qc_page_capture AS page
    SET page_type = 'popup_ad', page_state = 'popup_blocked',
        metadata = COALESCE(page.metadata, '{}'::jsonb) || $5::jsonb
    FROM qualified
    WHERE page.id = qualified.id
      AND (qualified.page_type IS NULL OR qualified.page_type = 'popup_ad')
      AND NOT EXISTS (SELECT 1 FROM qc_h5_page_type_assessment AS assessment
          WHERE assessment.page_capture_id = page.id AND assessment.task_id = page.task_id)
    RETURNING page.id
), updated_capture AS (
    UPDATE qc_capture AS capture
    SET metadata = COALESCE(capture.metadata, '{}'::jsonb) || $5::jsonb
    FROM updated_page WHERE capture.id = updated_page.id
    RETURNING capture.id
)
SELECT qualified.id::text AS capture_id, updated_capture.id IS NOT NULL AS recorded
FROM qualified LEFT JOIN updated_capture ON updated_capture.id = qualified.id
"""

# 写入没选中行时，在同一事务里读出各围栏字段，定位是哪一条失效（只读）
_DIAGNOSE_STALE_SQL = """
SELECT task.target_platform, task.execution_status, task.finalization_state,
    task.finalization_epoch, LOWER(task.task_config->>'workflow_version') AS workflow_version,
    (SELECT latest.id::text FROM qc_agent_run AS latest
        WHERE latest.task_id = task.id AND latest.agent_type = 'main'
        ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1) AS latest_run_id,
    session.status AS session_status, session.finalization_epoch AS session_epoch,
    session.current_capture_id::text AS session_capture_id,
    capture.capture_status, capture.agent_run_id::text AS capture_run_id,
    capture.surface_session_id AS capture_session_id,
    capture.metadata->>'finalization_epoch' AS capture_epoch,
    page.capture_status AS page_status, page.surface_session_id AS page_session_id,
    page.metadata->>'finalization_epoch' AS page_epoch, page.url AS page_url
FROM qc_task AS task
LEFT JOIN qc_surface_session AS session
  ON session.task_id = task.id AND session.session_id = $2
LEFT JOIN qc_capture AS capture ON capture.task_id = task.id AND capture.id = $3::uuid
LEFT JOIN qc_page_capture AS page ON page.task_id = task.id AND page.id = $3::uuid
WHERE task.id = $1::uuid
"""


def _stale_reason(row, *, agent_run_id, session_id, capture_id) -> str:
    """按写入 SQL 的围栏顺序给出第一条不满足的条件。"""
    if row is None:
        return "task_missing"
    epoch = str(row["finalization_epoch"])
    checks = (
        (row["target_platform"] != "web" or row["workflow_version"] != "v2", "task_not_web_v2"),
        (row["execution_status"] == "canceled", "task_canceled"),
        (row["finalization_state"] == "sealed", "task_sealed"),
        (row["latest_run_id"] != agent_run_id, "run_not_latest"),
        (row["session_status"] is None, "session_missing"),
        (row["session_status"] != "active", f"session_{row['session_status']}"),
        (str(row["session_epoch"]) != epoch, "session_epoch_mismatch"),
        (row["capture_status"] is None, "capture_missing"),
        (
            row["capture_run_id"] != agent_run_id or row["capture_session_id"] != session_id,
            "capture_foreign",
        ),
        (row["session_capture_id"] != capture_id, "capture_not_current"),
        (row["capture_status"] != "captured", "capture_not_captured"),
        (row["capture_epoch"] != epoch, "capture_epoch_mismatch"),
        (row["page_status"] is None, "page_missing"),
        (
            row["page_status"] != "captured" or row["page_session_id"] != session_id,
            "page_not_captured",
        ),
        (row["page_epoch"] != epoch, "page_epoch_mismatch"),
        (not str(row["page_url"] or "").startswith(("http://", "https://")), "page_url_not_http"),
    )
    return next((reason for failed, reason in checks if failed), "unknown")
