"""Web 正式任务终态前的持久化账本收束仓储。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services.api.app.infrastructure.db.finalization_recovery_fence import (
    assert_finalization_recovery_fence,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

_OUTCOME_STATUSES = {
    "canceled": ("cancelled", "cancelled", "failed", "cancelled", "cancelled"),
    "failed": ("failed", "failed", "failed", "unfulfillable", "failed"),
    "needs_review": (
        "needs_review",
        "failed",
        "needs_review",
        "unfulfillable",
        "failed",
    ),
}


class WebFormalTerminalLedgerError(RuntimeError):
    """Web 正式账本无法按当前 task/run/epoch 身份收束。"""


@dataclass(frozen=True, slots=True)
class WebFormalTerminalLedgerResult:
    """一次幂等终态收束实际更新的账本行数。"""

    publication_count: int
    page_work_count: int
    page_member_count: int
    page_attempt_count: int
    aggregate_work_count: int
    aggregate_attempt_count: int
    aggregate_candidate_count: int
    evidence_request_count: int
    restoration_attempt_count: int
    probe_work_count: int
    restoration_action_count: int
    report_attempt_count: int

    @property
    def terminalized_count(self) -> int:
        return (
            self.publication_count
            + self.page_work_count
            + self.page_member_count
            + self.page_attempt_count
            + self.aggregate_work_count
            + self.aggregate_attempt_count
            + self.aggregate_candidate_count
            + self.evidence_request_count
            + self.restoration_attempt_count
            + self.probe_work_count
            + self.restoration_action_count
            + self.report_attempt_count
        )


class PostgresWebFormalTerminalLedgerRepository:
    """在同一事务内终结当前 Web 正式 epoch 的发布与质检工作。"""

    def __init__(self, *, database_url: str | None = None, pool: Any = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def terminalize(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        outcome: str,
        reason_code: str,
        reason_message: str,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> WebFormalTerminalLedgerResult:
        _require_text(task_id, "task_id")
        _require_text(agent_run_id, "agent_run_id")
        _require_text(reason_code, "reason_code")
        _require_text(reason_message, "reason_message")
        statuses = _OUTCOME_STATUSES.get(outcome)
        if statuses is None:
            raise ValueError("outcome 必须为 canceled、failed 或 needs_review")
        (
            publication_status,
            work_status,
            aggregate_candidate_status,
            evidence_request_status,
            report_attempt_status,
        ) = statuses

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            if recovery_lease_token is not None:
                await assert_finalization_recovery_fence(
                    connection,
                    task_id=task_id,
                    recovery_lease_token=recovery_lease_token,
                    recovery_generation=recovery_generation,
                    recovery_lease_expires_at=recovery_lease_expires_at,
                )
            identity = await connection.fetchrow(
                _IDENTITY_SQL,
                task_id,
                agent_run_id,
            )
            _validate_identity(identity, task_id=task_id, agent_run_id=agent_run_id)
            row = await connection.fetchrow(
                _TERMINALIZE_SQL,
                task_id,
                agent_run_id,
                publication_status,
                work_status,
                reason_code,
                reason_message,
                aggregate_candidate_status,
                evidence_request_status,
                report_attempt_status,
            )
        if row is None:
            raise WebFormalTerminalLedgerError("Web 正式账本收束未返回数据库摘要")
        return WebFormalTerminalLedgerResult(
            publication_count=int(row["publication_count"]),
            page_work_count=int(row["page_work_count"]),
            page_member_count=int(row["page_member_count"]),
            page_attempt_count=int(row["page_attempt_count"]),
            aggregate_work_count=int(row["aggregate_work_count"]),
            aggregate_attempt_count=int(row["aggregate_attempt_count"]),
            aggregate_candidate_count=int(row["aggregate_candidate_count"]),
            evidence_request_count=int(row["evidence_request_count"]),
            restoration_attempt_count=int(row["restoration_attempt_count"]),
            probe_work_count=int(row["probe_work_count"]),
            restoration_action_count=int(row["restoration_action_count"]),
            report_attempt_count=int(row["report_attempt_count"]),
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _validate_identity(
    row: Any,
    *,
    task_id: str,
    agent_run_id: str,
) -> None:
    if row is None:
        raise WebFormalTerminalLedgerError("Web Task 或 main Run 不存在")
    if str(row["task_id"]) != task_id or str(row["run_task_id"]) != task_id:
        raise WebFormalTerminalLedgerError("main Run 不属于当前 Web Task")
    if str(row["agent_run_id"]) != agent_run_id:
        raise WebFormalTerminalLedgerError("main Run 身份不一致")
    if str(row["target_platform"]) != "web":
        raise WebFormalTerminalLedgerError("正式账本收束只允许 Web Task")
    if str(row["agent_type"]) != "main" or row["current_main_run"] is not True:
        raise WebFormalTerminalLedgerError("正式账本收束只允许当前 main Run")
    if str(row["execution_mode"]) != "full_inspection":
        raise WebFormalTerminalLedgerError("正式账本收束只允许 full_inspection")
    if str(row["notification_mode"]) != "enforced":
        raise WebFormalTerminalLedgerError("正式账本收束只允许 enforced 模式")
    owner = row["finalization_agent_run_id"]
    if owner is not None and str(owner) != agent_run_id:
        raise WebFormalTerminalLedgerError("当前 main Run 不是收口所有者")


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须为非空字符串")


_IDENTITY_SQL = """
    SELECT
        task.id::text AS task_id,
        task.target_platform,
        task.task_config->>'execution_mode' AS execution_mode,
        task.task_config->>'web_inspection_notification_mode' AS notification_mode,
        task.finalization_agent_run_id::text AS finalization_agent_run_id,
        run.id::text AS agent_run_id,
        run.task_id::text AS run_task_id,
        run.agent_type,
        run.id = (
            SELECT current_run.id
            FROM qc_agent_run AS current_run
            WHERE current_run.task_id = task.id
              AND current_run.agent_type = 'main'
            ORDER BY current_run.created_at DESC, current_run.id DESC
            LIMIT 1
        ) AS current_main_run
    FROM qc_task AS task
    INNER JOIN qc_agent_run AS run
        ON run.task_id = task.id
       AND run.id = $2::uuid
    WHERE task.id = $1::uuid
    FOR UPDATE OF task, run
"""


_TERMINALIZE_SQL = """
    WITH target_task AS MATERIALIZED (
        SELECT task.id, task.finalization_epoch
        FROM qc_task AS task
        WHERE task.id = $1::uuid
    ), terminal_fence AS (
        UPDATE qc_agent_run AS run
        SET output_context = jsonb_set(
                COALESCE(run.output_context, '{}'::jsonb),
                '{web_formal_terminalized}',
                COALESCE(
                    run.output_context->'web_formal_terminalized',
                    jsonb_build_object(
                        'outcome', CASE
                            WHEN $9::varchar = 'cancelled' THEN 'canceled'
                            WHEN $7::varchar = 'needs_review' THEN 'needs_review'
                            ELSE 'failed'
                        END,
                        'reason_code', $5::text
                    )
                ),
                TRUE
            ),
            updated_at = now()
        FROM target_task AS task
        WHERE run.id = $2::uuid
          AND run.task_id = task.id
          AND run.agent_type = 'main'
          AND NOT (
              COALESCE(run.output_context, '{}'::jsonb)
              ? 'web_formal_terminalized'
          )
        RETURNING run.id
    ), target_page_scope AS MATERIALIZED (
        SELECT work_item.id, work_item.status
        FROM qc_inspection_work_item AS work_item
        INNER JOIN target_task AS task ON task.id = work_item.task_id
        WHERE work_item.producer_agent_run_id = $2::uuid
          AND work_item.finalization_epoch = task.finalization_epoch
          AND work_item.control_mode = 'enforced'
    ), target_page_work AS MATERIALIZED (
        SELECT work_item.id
        FROM target_page_scope AS work_item
        WHERE work_item.status IN (
              'pending', 'leased', 'running', 'retry_wait', 'waiting_evidence'
        )
    ), target_evidence_request_scope AS MATERIALIZED (
        SELECT request.request_id
        FROM qc_evidence_request AS request
        INNER JOIN target_task AS task ON task.id = request.task_id
        INNER JOIN target_page_scope AS work_item
            ON work_item.id = request.work_item_id
        INNER JOIN qc_inspection_attempt AS attempt
            ON attempt.id = request.attempt_id
           AND attempt.work_item_id = work_item.id
        WHERE request.finalization_epoch = task.finalization_epoch
    ), target_evidence_request AS MATERIALIZED (
        SELECT request.request_id
        FROM qc_evidence_request AS request
        INNER JOIN target_evidence_request_scope AS target
            ON target.request_id = request.request_id
        WHERE request.status IN ('pending', 'routed', 'running')
    ), target_probe_work AS MATERIALIZED (
        SELECT work.work_id
        FROM qc_v2_work_item AS work
        INNER JOIN target_evidence_request_scope AS request
            ON request.request_id = work.evidence_request_id
        WHERE work.task_id = $1::uuid
          AND work.work_type = 'probe'
          AND work.status IN ('pending', 'leased', 'running', 'retry_wait')
    ), target_restoration_attempt AS MATERIALIZED (
        SELECT restoration.attempt_id, restoration.action_execution_ids
        FROM qc_restoration_attempt AS restoration
        INNER JOIN target_evidence_request_scope AS request
            ON request.request_id = restoration.request_id
        WHERE restoration.status = 'running'
    ), terminal_restoration_action AS (
        UPDATE qc_action_execution AS action
        SET
            status = 'failed',
            error_code = LEFT($5, 128),
            error_message = $6,
            action_finished_at = COALESCE(action.action_finished_at, now()),
            row_version = action.row_version + 1,
            updated_at = now()
        FROM target_restoration_attempt AS restoration
        WHERE action.task_id = $1::uuid
          AND action.finalization_epoch = (
              SELECT finalization_epoch FROM target_task
          )
          AND action.status IN ('planned', 'executing', 'evidence_pending')
          AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(
                  restoration.action_execution_ids
              ) AS linked(action_execution_id)
              WHERE linked.action_execution_id = action.action_execution_id
          )
        RETURNING action.action_execution_id
    ), failed_restoration_attempt AS (
        UPDATE qc_restoration_attempt AS restoration
        SET
            status = 'failed',
            error_code = LEFT($5, 128),
            error_message = $6,
            finished_at = now(),
            row_version = restoration.row_version + 1,
            updated_at = now()
        FROM target_restoration_attempt AS target
        WHERE restoration.attempt_id = target.attempt_id
        RETURNING restoration.attempt_id
    ), terminal_probe_work AS (
        UPDATE qc_v2_work_item AS work
        SET
            status = $4,
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            last_error_code = LEFT($5, 128),
            last_error_message = $6,
            finished_at = now(),
            row_version = work.row_version + 1,
            updated_at = now()
        FROM target_probe_work AS target
        WHERE work.work_id = target.work_id
        RETURNING work.work_id
    ), terminal_evidence_request AS (
        UPDATE qc_evidence_request AS request
        SET
            status = $8,
            resolution_reason = $5,
            completed_at = now(),
            row_version = request.row_version + 1,
            updated_at = now()
        FROM target_evidence_request AS target
        WHERE request.request_id = target.request_id
        RETURNING request.request_id
    ), abandoned_page_attempt AS (
        UPDATE qc_inspection_attempt AS attempt
        SET
            status = 'abandoned',
            error_code = COALESCE(attempt.error_code, $5),
            error_message = COALESCE(attempt.error_message, $6),
            finished_at = COALESCE(attempt.finished_at, now()),
            updated_at = now()
        FROM target_page_work AS work_item
        WHERE attempt.work_item_id = work_item.id
          AND attempt.status IN ('running', 'judged')
        RETURNING attempt.id
    ), failed_page_member AS (
        UPDATE qc_inspection_work_item_rule AS member
        SET
            status = 'error',
            last_error_code = $5,
            last_error_message = $6,
            updated_at = now()
        FROM target_page_work AS work_item
        WHERE member.work_item_id = work_item.id
          AND member.status IN ('pending', 'judged')
        RETURNING member.work_item_id, member.rule_id
    ), terminal_page_work AS (
        UPDATE qc_inspection_work_item AS work_item
        SET
            status = $4,
            consumer_agent_run_id = NULL,
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            last_error_code = $5,
            last_error_message = $6,
            finished_at = now(),
            row_version = work_item.row_version + 1,
            updated_at = now()
        FROM target_page_work AS target
        WHERE work_item.id = target.id
        RETURNING work_item.id
    ), target_aggregate_work AS MATERIALIZED (
        SELECT work_item.id
        FROM qc_aggregate_inspection_work_item AS work_item
        INNER JOIN target_task AS task ON task.id = work_item.task_id
        WHERE work_item.producer_agent_run_id = $2::uuid
          AND work_item.finalization_epoch = task.finalization_epoch
          AND work_item.control_mode = 'enforced'
          AND work_item.status IN ('pending', 'leased', 'running', 'retry_wait')
    ), abandoned_aggregate_attempt AS (
        UPDATE qc_aggregate_inspection_attempt AS attempt
        SET
            status = 'abandoned',
            error_code = COALESCE(attempt.error_code, $5),
            error_message = COALESCE(attempt.error_message, $6),
            finished_at = COALESCE(attempt.finished_at, now()),
            updated_at = now()
        FROM target_aggregate_work AS work_item
        WHERE attempt.work_item_id = work_item.id
          AND attempt.status = 'running'
        RETURNING attempt.id
    ), terminal_aggregate_work AS (
        UPDATE qc_aggregate_inspection_work_item AS work_item
        SET
            status = $4,
            consumer_agent_run_id = NULL,
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            result_payload = NULL,
            result_hash = NULL,
            last_error_code = $5,
            last_error_message = $6,
            finished_at = now(),
            row_version = work_item.row_version + 1,
            updated_at = now()
        FROM target_aggregate_work AS target
        WHERE work_item.id = target.id
        RETURNING work_item.id
    ), terminal_aggregate_candidate AS (
        UPDATE qc_aggregate_inspection_candidate AS candidate
        SET
            status = $7,
            disposition_code = $5,
            disposition_message = $6,
            finished_at = now(),
            updated_at = now()
        FROM target_task AS task
        WHERE candidate.task_id = task.id
          AND candidate.source_agent_run_id = $2::uuid
          AND candidate.finalization_epoch = task.finalization_epoch
          AND candidate.status IN ('expected', 'published')
        RETURNING candidate.id
    ), terminal_publication AS (
        UPDATE qc_h5_inspection_publication AS publication
        SET
            status = $3,
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            failure_code = $5,
            failure_message = $6,
            finished_at = now(),
            row_version = publication.row_version + 1,
            updated_at = now()
        FROM target_task AS task
        WHERE publication.task_id = task.id
          AND publication.source_agent_run_id = $2::uuid
          AND publication.finalization_epoch = task.finalization_epoch
          AND publication.mode = 'enforced'
          AND publication.status IN ('pending', 'leased', 'retry_wait')
        RETURNING publication.id
    ), terminal_report_attempt AS (
        UPDATE qc_web_report_generation_attempt AS attempt
        SET
            status = $9,
            failure_code = LEFT($5, 128),
            finished_at = clock_timestamp()
        FROM target_task AS task
        WHERE attempt.task_id = task.id
          AND attempt.agent_run_id = $2::uuid
          AND attempt.finalization_epoch = task.finalization_epoch
          AND attempt.status = 'running'
        RETURNING attempt.id
    )
    SELECT
        (SELECT count(*)::integer FROM terminal_publication) AS publication_count,
        (SELECT count(*)::integer FROM terminal_page_work) AS page_work_count,
        (SELECT count(*)::integer FROM failed_page_member) AS page_member_count,
        (SELECT count(*)::integer FROM abandoned_page_attempt) AS page_attempt_count,
        (SELECT count(*)::integer FROM terminal_aggregate_work) AS aggregate_work_count,
        (SELECT count(*)::integer FROM abandoned_aggregate_attempt)
            AS aggregate_attempt_count,
        (SELECT count(*)::integer FROM terminal_aggregate_candidate)
            AS aggregate_candidate_count,
        (SELECT count(*)::integer FROM terminal_evidence_request)
            AS evidence_request_count,
        (SELECT count(*)::integer FROM failed_restoration_attempt)
            AS restoration_attempt_count,
        (SELECT count(*)::integer FROM terminal_probe_work)
            AS probe_work_count,
        (SELECT count(*)::integer FROM terminal_restoration_action)
            AS restoration_action_count,
        (SELECT count(*)::integer FROM terminal_report_attempt)
            AS report_attempt_count
"""
