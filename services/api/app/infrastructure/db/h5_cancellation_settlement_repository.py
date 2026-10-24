"""H5 任务取消后的 Evidence Request 与 Explorer Work 原子收口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool


@dataclass(frozen=True, slots=True)
class H5CancellationSettlementSummary:
    """一次 H5 取消对账实际改变的记录数量。"""

    request_count: int
    explorer_work_count: int
    inspection_work_count: int
    inspection_attempt_count: int
    inspection_rule_count: int
    aggregate_work_count: int
    aggregate_attempt_count: int
    publication_count: int

    @property
    def total_count(self) -> int:
        return sum(
            (
                self.request_count,
                self.explorer_work_count,
                self.inspection_work_count,
                self.inspection_attempt_count,
                self.inspection_rule_count,
                self.aggregate_work_count,
                self.aggregate_attempt_count,
                self.publication_count,
            )
        )


class PostgresH5CancellationSettlementRepository:
    """只结算 H5 当前 Epoch 中尚未进入终态的请求与工作。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def settle_canceled(
        self,
        *,
        task_id: str,
        reason: str,
    ) -> H5CancellationSettlementSummary:
        _require_text(task_id, "task_id")
        _require_text(reason, "reason")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            row = await connection.fetchrow(
                """
                WITH task_scope AS MATERIALIZED (
                    SELECT task.id, task.finalization_epoch
                    FROM qc_task AS task
                    WHERE task.id = $1::uuid
                      AND task.target_platform = 'h5'
                      AND task.status IN ('running', 'canceled')
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                ),
                canceled_request AS (
                    UPDATE qc_evidence_request AS request
                    SET
                        status = 'cancelled',
                        resolution_reason = CASE
                            WHEN NULLIF(BTRIM(request.resolution_reason), '') IS NULL
                            THEN $2::text
                            ELSE request.resolution_reason || ';' || $2::text
                        END,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        row_version = request.row_version + 1,
                        completed_at = now(),
                        updated_at = now()
                    FROM task_scope AS task
                    WHERE request.task_id = task.id
                      AND request.finalization_epoch = task.finalization_epoch
                      AND request.status IN ('pending', 'routed', 'running')
                    RETURNING 1
                ),
                abandoned_inspection_attempt AS (
                    UPDATE qc_inspection_attempt AS attempt
                    SET
                        status = 'abandoned',
                        error_code = COALESCE(attempt.error_code, 'WORKFLOW_CANCELED'),
                        error_message = COALESCE(attempt.error_message, $2::text),
                        finished_at = COALESCE(attempt.finished_at, now()),
                        updated_at = now()
                    FROM
                        qc_inspection_work_item AS work_item,
                        task_scope AS task
                    WHERE work_item.task_id = task.id
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.status IN (
                          'pending', 'leased', 'running', 'retry_wait',
                          'waiting_evidence'
                      )
                      AND attempt.work_item_id = work_item.id
                      AND attempt.status IN ('running', 'judged')
                    RETURNING 1
                ),
                canceled_inspection_rule AS (
                    UPDATE qc_inspection_work_item_rule AS member
                    SET
                        status = 'error',
                        last_error_code = COALESCE(
                            member.last_error_code,
                            'WORKFLOW_CANCELED'
                        ),
                        last_error_message = COALESCE(
                            member.last_error_message,
                            $2::text
                        ),
                        updated_at = now()
                    FROM
                        qc_inspection_work_item AS work_item,
                        task_scope AS task
                    WHERE work_item.task_id = task.id
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.status IN (
                          'pending', 'leased', 'running', 'retry_wait',
                          'waiting_evidence'
                      )
                      AND member.work_item_id = work_item.id
                      AND member.status IN ('pending', 'judged')
                    RETURNING 1
                ),
                canceled_inspection_work AS (
                    UPDATE qc_inspection_work_item AS work_item
                    SET
                        status = 'cancelled',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = COALESCE(
                            work_item.last_error_code,
                            'WORKFLOW_CANCELED'
                        ),
                        last_error_message = COALESCE(
                            work_item.last_error_message,
                            $2::text
                        ),
                        finished_at = now(),
                        row_version = work_item.row_version + 1,
                        updated_at = now()
                    FROM task_scope AS task
                    WHERE work_item.task_id = task.id
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.status IN (
                          'pending', 'leased', 'running', 'retry_wait',
                          'waiting_evidence'
                      )
                    RETURNING 1
                ),
                abandoned_aggregate_attempt AS (
                    UPDATE qc_aggregate_inspection_attempt AS attempt
                    SET
                        status = 'abandoned',
                        error_code = COALESCE(attempt.error_code, 'WORKFLOW_CANCELED'),
                        error_message = COALESCE(attempt.error_message, $2::text),
                        finished_at = COALESCE(attempt.finished_at, now()),
                        updated_at = now()
                    FROM
                        qc_aggregate_inspection_work_item AS work_item,
                        task_scope AS task
                    WHERE work_item.task_id = task.id
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.status IN (
                          'pending', 'leased', 'running', 'retry_wait'
                      )
                      AND attempt.work_item_id = work_item.id
                      AND attempt.status = 'running'
                    RETURNING 1
                ),
                canceled_aggregate_work AS (
                    UPDATE qc_aggregate_inspection_work_item AS work_item
                    SET
                        status = 'cancelled',
                        consumer_agent_run_id = NULL,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        result_payload = NULL,
                        result_hash = NULL,
                        last_error_code = COALESCE(
                            work_item.last_error_code,
                            'WORKFLOW_CANCELED'
                        ),
                        last_error_message = COALESCE(
                            work_item.last_error_message,
                            $2::text
                        ),
                        finished_at = now(),
                        row_version = work_item.row_version + 1,
                        updated_at = now()
                    FROM task_scope AS task
                    WHERE work_item.task_id = task.id
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.status IN (
                          'pending', 'leased', 'running', 'retry_wait'
                      )
                    RETURNING 1
                ),
                canceled_publication AS (
                    UPDATE qc_h5_inspection_publication AS publication
                    SET
                        status = 'cancelled',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        failure_code = COALESCE(
                            publication.failure_code,
                            'WORKFLOW_CANCELED'
                        ),
                        failure_message = COALESCE(
                            publication.failure_message,
                            $2::text
                        ),
                        finished_at = now(),
                        row_version = publication.row_version + 1,
                        updated_at = now()
                    FROM task_scope AS task
                    WHERE publication.task_id = task.id
                      AND publication.finalization_epoch = task.finalization_epoch
                      AND publication.status IN ('pending', 'leased', 'retry_wait')
                    RETURNING 1
                ),
                canceled_explorer_work AS (
                    UPDATE qc_v2_work_item AS work_item
                    SET
                        status = 'cancelled',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        terminal_payload = COALESCE(
                            work_item.terminal_payload,
                            '{}'::jsonb
                        ) || jsonb_build_object('cancel_reason', $2::text),
                        last_error_code = COALESCE(
                            work_item.last_error_code,
                            'WORKFLOW_CANCELED'
                        ),
                        last_error_message = COALESCE(
                            work_item.last_error_message,
                            $2::text
                        ),
                        finished_at = now(),
                        row_version = work_item.row_version + 1,
                        updated_at = now()
                    FROM task_scope AS task
                    WHERE work_item.task_id = task.id
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.work_type IN ('exploration', 'probe')
                      AND work_item.work_payload->>'surface_kind' = 'web'
                      AND work_item.status IN (
                          'pending', 'leased', 'running', 'retry_wait'
                      )
                    RETURNING 1
                )
                SELECT
                    (SELECT count(*) FROM canceled_request)::int AS request_count,
                    (SELECT count(*) FROM canceled_explorer_work)::int
                        AS explorer_work_count,
                    (SELECT count(*) FROM canceled_inspection_work)::int
                        AS inspection_work_count,
                    (SELECT count(*) FROM abandoned_inspection_attempt)::int
                        AS inspection_attempt_count,
                    (SELECT count(*) FROM canceled_inspection_rule)::int
                        AS inspection_rule_count,
                    (SELECT count(*) FROM canceled_aggregate_work)::int
                        AS aggregate_work_count,
                    (SELECT count(*) FROM abandoned_aggregate_attempt)::int
                        AS aggregate_attempt_count,
                    (SELECT count(*) FROM canceled_publication)::int
                        AS publication_count
                """,
                task_id,
                reason,
            )
        if row is None:
            return H5CancellationSettlementSummary(
                request_count=0,
                explorer_work_count=0,
                inspection_work_count=0,
                inspection_attempt_count=0,
                inspection_rule_count=0,
                aggregate_work_count=0,
                aggregate_attempt_count=0,
                publication_count=0,
            )
        return H5CancellationSettlementSummary(
            request_count=int(row["request_count"] or 0),
            explorer_work_count=int(row["explorer_work_count"] or 0),
            inspection_work_count=int(row["inspection_work_count"] or 0),
            inspection_attempt_count=int(row["inspection_attempt_count"] or 0),
            inspection_rule_count=int(row["inspection_rule_count"] or 0),
            aggregate_work_count=int(row["aggregate_work_count"] or 0),
            aggregate_attempt_count=int(row["aggregate_attempt_count"] or 0),
            publication_count=int(row["publication_count"] or 0),
        )


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")


__all__ = [
    "H5CancellationSettlementSummary",
    "PostgresH5CancellationSettlementRepository",
]
