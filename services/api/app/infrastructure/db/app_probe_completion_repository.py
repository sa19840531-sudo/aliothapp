"""成功 App Probe 的补采挂接恢复读取。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.api.app.application.app_exploration_turn import AppExplorationTurnOutcome
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.domain.exploration import ProbeWork
from services.api.app.infrastructure.db.pool import get_postgres_pool


class AppProbeCompletionRecoveryPersistenceError(RuntimeError):
    """持久化 Probe 与动作结果无法形成唯一可信恢复候选。"""


class PostgresAppProbeCompletionRecoveryReader:
    """从 Work、Action 和 Request 事实恢复待挂接 Probe。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def find_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> AppExplorationTurnOutcome | None:
        _require_text(task_id, "task_id")
        _require_text(agent_run_id, "agent_run_id")
        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    work.work_payload,
                    action.result_capture_id::text AS result_capture_id
                FROM qc_v2_work_item AS work
                INNER JOIN qc_task AS task
                    ON task.id = work.task_id
                   AND task.status = 'running'
                   AND task.finalization_epoch = work.finalization_epoch
                INNER JOIN qc_evidence_request AS request
                    ON request.request_id = work.evidence_request_id
                   AND request.task_id = work.task_id
                   AND request.finalization_epoch = work.finalization_epoch
                   AND request.session_dependency = 'active_session'
                   AND request.status IN ('routed', 'running')
                INNER JOIN qc_capture AS source_capture
                    ON source_capture.id = work.source_capture_id
                   AND source_capture.task_id = work.task_id
                   AND source_capture.agent_run_id = $2::uuid
                INNER JOIN qc_action_execution AS action
                    ON action.task_id = work.task_id
                   AND action.session_id = work.session_id
                   AND action.finalization_epoch = work.finalization_epoch
                   AND action.status = 'completed'
                   AND action.result_capture_id IS NOT NULL
                   AND action.intent_payload
                        #>> '{parameters,exploration_work_id}' = work.work_id
                LEFT JOIN qc_evidence_supplement AS supplement
                    ON supplement.request_id = request.request_id
                   AND supplement.probe_work_id = work.work_id
                WHERE work.task_id = $1::uuid
                  AND work.work_type = 'probe'
                  AND work.work_payload->>'surface_kind' IN ('android', 'ios')
                  AND work.status = 'succeeded'
                  AND work.finalization_epoch = $3
                  AND supplement.supplement_id IS NULL
                ORDER BY work.finished_at, work.work_id
                LIMIT 1
                """,
                task_id,
                agent_run_id,
                finalization_epoch,
            )
        if not rows:
            return None
        work = ProbeWork.from_dict(_as_mapping(rows[0]["work_payload"]))
        result_capture_id = str(rows[0]["result_capture_id"])
        if work.task_id != task_id or not result_capture_id:
            raise AppProbeCompletionRecoveryPersistenceError("待挂接 App Probe 身份无效")
        return AppExplorationTurnOutcome(
            claimed=True,
            work_id=work.work_id,
            final_status=ExplorerWorkStatus.SUCCEEDED,
            result_capture_id=result_capture_id,
            evidence_request_id=work.evidence_request.request_id,
            source_capture_id=work.source_capture_id,
            work_type="probe",
            restoration_plan=work.restoration_plan,
            restoration_attempt_id=work.restoration_attempt_id,
            equivalence_assessment_id=work.equivalence_assessment_id,
            source_grade=work.source_grade,
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise AppProbeCompletionRecoveryPersistenceError("Probe Work 载荷必须为对象")
    return value
