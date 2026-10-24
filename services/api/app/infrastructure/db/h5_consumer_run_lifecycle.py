"""H5 Publication 逻辑 rule_judge Run 的统一数据库收口。"""

from __future__ import annotations

from typing import Any

from services.api.app.infrastructure.db.h5_completion_gate_sql import (
    QUALIFIED_H5_INVENTORY_CTE_SQL,
)


async def finish_h5_consumer_run_if_idle_on_connection(
    connection: Any,
    *,
    task_id: str,
    consumer_agent_run_id: str,
) -> bool:
    """全链无在途事实后，按取消或不可恢复失败事实写入唯一 Run 终态。"""

    value = await connection.fetchval(
        f"""
        WITH target_task AS (
            SELECT * FROM qc_task WHERE id = $1::uuid AND target_platform = 'h5'
        ), {QUALIFIED_H5_INVENTORY_CTE_SQL}, run_context AS (
            SELECT
                id AS consumer_id,
                task_id,
                (input_context->>'finalization_epoch')::integer AS finalization_epoch
            FROM qc_agent_run AS consumer
            WHERE consumer.id = $2::uuid
              AND consumer.task_id = $1::uuid
              AND consumer.agent_type = 'rule_judge'
              AND consumer.status = 'running'
              AND consumer.input_context->>'source' = 'h5_inspection_publication'
        ), publication_scope AS (
            SELECT publication.id, publication.status
            FROM qc_h5_inspection_publication AS publication
            INNER JOIN run_context
                ON run_context.task_id = publication.task_id
               AND run_context.finalization_epoch = publication.finalization_epoch
        ), work_scope AS (
            SELECT work_item.id, work_item.status
            FROM publication_scope AS publication
            INNER JOIN qc_h5_inspection_publication_work_item AS link
                ON link.publication_id = publication.id
            INNER JOIN qc_inspection_work_item AS work_item
                ON work_item.id = link.work_item_id
        ), chain_state AS (
            SELECT
                EXISTS (
                    SELECT 1 FROM qualified_h5_inventory AS inventory
                    WHERE NOT EXISTS (
                        SELECT 1 FROM publication_scope AS publication
                        INNER JOIN qc_h5_inspection_publication AS stored
                            ON stored.id = publication.id
                        WHERE stored.page_capture_id = inventory.after_capture_id
                    )
                )
                OR
                EXISTS (
                    SELECT 1
                    FROM publication_scope
                    WHERE status NOT IN (
                        'published', 'not_applicable', 'needs_review', 'failed',
                        'cancelled', 'superseded'
                    )
                )
                OR EXISTS (
                    SELECT 1
                    FROM work_scope AS work_item
                    WHERE work_item.status NOT IN (
                        'succeeded', 'partial', 'failed', 'cancelled', 'superseded'
                    )
                )
                OR EXISTS (
                    SELECT 1
                    FROM qc_inspection_attempt AS attempt
                    INNER JOIN work_scope ON work_scope.id = attempt.work_item_id
                    WHERE attempt.status IN ('running', 'judged')
                )
                OR EXISTS (
                    SELECT 1
                    FROM qc_outbox_event AS outbox
                    INNER JOIN run_context ON run_context.task_id = outbox.task_id
                    WHERE outbox.status IN ('pending', 'publishing', 'retry_wait')
                      AND (
                          (
                              outbox.aggregate_type = 'h5_inspection_publication'
                              AND EXISTS (
                                  SELECT 1 FROM publication_scope
                                  WHERE publication_scope.id::text = outbox.aggregate_id
                              )
                          )
                          OR (
                              outbox.aggregate_type = 'inspection_work_item'
                              AND EXISTS (
                                  SELECT 1 FROM work_scope
                                  WHERE work_scope.id::text = outbox.aggregate_id
                              )
                          )
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM qc_event_inbox AS inbox
                    INNER JOIN run_context ON run_context.task_id = inbox.task_id
                    WHERE inbox.status IN ('pending', 'processing', 'retry_wait')
                      AND (
                          (
                              inbox.aggregate_type = 'h5_inspection_publication'
                              AND EXISTS (
                                  SELECT 1 FROM publication_scope
                                  WHERE publication_scope.id::text = inbox.aggregate_id
                              )
                          )
                          OR (
                              inbox.aggregate_type = 'inspection_work_item'
                              AND EXISTS (
                                  SELECT 1 FROM work_scope
                                  WHERE work_scope.id::text = inbox.aggregate_id
                              )
                          )
                      )
                ) AS has_inflight,
                EXISTS (
                    SELECT 1 FROM publication_scope WHERE status = 'failed'
                )
                OR EXISTS (
                    SELECT 1 FROM work_scope WHERE status = 'failed'
                ) AS has_failed_work,
                EXISTS (
                    SELECT 1
                    FROM qc_outbox_event AS outbox
                    INNER JOIN run_context ON run_context.task_id = outbox.task_id
                    WHERE outbox.status = 'dead_letter'
                      AND (
                          (
                              outbox.aggregate_type = 'h5_inspection_publication'
                              AND EXISTS (
                                  SELECT 1 FROM publication_scope
                                  WHERE publication_scope.id::text = outbox.aggregate_id
                              )
                          )
                          OR (
                              outbox.aggregate_type = 'inspection_work_item'
                              AND EXISTS (
                                  SELECT 1 FROM work_scope
                                  WHERE work_scope.id::text = outbox.aggregate_id
                              )
                          )
                      )
                )
                OR EXISTS (
                    SELECT 1
                    FROM qc_event_inbox AS inbox
                    INNER JOIN run_context ON run_context.task_id = inbox.task_id
                    WHERE inbox.status = 'dead_letter'
                      AND (
                          (
                              inbox.aggregate_type = 'h5_inspection_publication'
                              AND EXISTS (
                                  SELECT 1 FROM publication_scope
                                  WHERE publication_scope.id::text = inbox.aggregate_id
                              )
                          )
                          OR (
                              inbox.aggregate_type = 'inspection_work_item'
                              AND EXISTS (
                                  SELECT 1 FROM work_scope
                                  WHERE work_scope.id::text = inbox.aggregate_id
                              )
                          )
                      )
                ) AS has_dead_letter
        )
        UPDATE qc_agent_run AS consumer
        SET status = CASE
                WHEN task.status = 'canceled' THEN 'canceled'
                WHEN chain_state.has_failed_work OR chain_state.has_dead_letter THEN 'failed'
                ELSE 'done'
            END,
            stop_reason = CASE
                WHEN task.status = 'canceled' THEN 'task_canceled'
                WHEN chain_state.has_failed_work OR chain_state.has_dead_letter
                    THEN 'h5_inspection_chain_failed'
                ELSE 'h5_inspection_work_drained'
            END,
            error_message = CASE
                WHEN task.status = 'canceled' THEN NULL
                WHEN chain_state.has_failed_work AND chain_state.has_dead_letter
                    THEN 'H5 Publication 链存在失败 Publication/Work 与消息死信'
                WHEN chain_state.has_failed_work
                    THEN 'H5 Publication 链存在失败 Publication/Work'
                WHEN chain_state.has_dead_letter
                    THEN 'H5 Publication 链存在消息死信'
                ELSE NULL
            END,
            finished_at = now(),
            updated_at = now()
        FROM qc_task AS task, run_context, chain_state
        WHERE task.id = $1::uuid
          AND task.finalization_state <> 'sealed'
          AND consumer.id = run_context.consumer_id
          AND consumer.task_id = task.id
          AND (
              task.finalization_agent_run_id IS NOT NULL
              OR task.status <> 'running'
          )
          AND NOT chain_state.has_inflight
          AND EXISTS (SELECT 1 FROM publication_scope)
        RETURNING consumer.id::text
        """,
        task_id,
        consumer_agent_run_id,
    )
    if value is None:
        return False
    await _settle_h5_page_inspection_statuses(
        connection,
        task_id=task_id,
        consumer_agent_run_id=consumer_agent_run_id,
    )
    await connection.execute(
        """
        UPDATE qc_task
        SET finalization_retry_at = now()
        WHERE id = $1::uuid
          AND status IN ('running', 'review')
          AND finalization_state = 'session_draining'
          AND completion_gate_snapshot_id IS NOT NULL
          AND finalization_recovery_state = 'active'
          AND finalization_epoch = (
              SELECT (consumer.input_context->>'finalization_epoch')::integer
              FROM qc_agent_run AS consumer
              WHERE consumer.id = $2::uuid
                AND consumer.task_id = $1::uuid
          )
        """,
        task_id,
        consumer_agent_run_id,
    )
    return True


async def _settle_h5_page_inspection_statuses(
    connection: Any,
    *,
    task_id: str,
    consumer_agent_run_id: str,
) -> None:
    """规则消费链排空后，把 H5 页面质检状态写成明确终态。"""

    await connection.execute(
        """
        WITH run_context AS (
            SELECT (input_context->>'finalization_epoch')::integer AS finalization_epoch
            FROM qc_agent_run
            WHERE id = $2::uuid
              AND task_id = $1::uuid
              AND agent_type = 'rule_judge'
              AND input_context->>'source' = 'h5_inspection_publication'
        ), page_outcome AS (
            SELECT
                publication.page_capture_id,
                publication.status AS publication_status,
                count(work_item.id) AS work_count,
                bool_or(work_item.status = 'failed') AS has_failed_work,
                bool_or(work_item.status IN ('partial', 'cancelled'))
                    AS has_degraded_work
            FROM qc_h5_inspection_publication AS publication
            INNER JOIN run_context
                ON run_context.finalization_epoch = publication.finalization_epoch
            LEFT JOIN qc_h5_inspection_publication_work_item AS link
                ON link.publication_id = publication.id
            LEFT JOIN qc_inspection_work_item AS work_item
                ON work_item.id = link.work_item_id
            WHERE publication.task_id = $1::uuid
              AND publication.status IN (
                  'published', 'not_applicable', 'needs_review', 'failed'
              )
            GROUP BY publication.page_capture_id, publication.status
        ), resolved AS (
            SELECT
                page_capture_id,
                CASE
                    WHEN publication_status = 'failed' OR has_failed_work THEN 'failed'
                    WHEN publication_status = 'needs_review'
                      OR has_degraded_work
                      OR (publication_status = 'published' AND work_count = 0)
                        THEN 'degraded'
                    ELSE 'completed'
                END AS inspection_status
            FROM page_outcome
        )
        UPDATE qc_page_capture AS capture
        SET inspection_status = resolved.inspection_status,
            updated_at = now()
        FROM resolved
        WHERE capture.id = resolved.page_capture_id
          AND capture.task_id = $1::uuid
          AND capture.inspection_status IN (
              'captured', 'detecting', 'matching', 'evaluating'
          )
        """,
        task_id,
        consumer_agent_run_id,
    )
    await connection.execute(
        """
        UPDATE qc_capture AS capture
        SET metadata = COALESCE(capture.metadata, '{}'::jsonb)
            || jsonb_build_object(
                'inspection_status', page.inspection_status,
                'inspection',
                COALESCE(capture.metadata->'inspection', '{}'::jsonb)
                    || jsonb_build_object('status', page.inspection_status)
            )
        FROM qc_page_capture AS page
        WHERE capture.id = page.id
          AND capture.task_id = $1::uuid
          AND page.task_id = $1::uuid
          AND page.inspection_status IN ('completed', 'degraded', 'failed')
        """,
        task_id,
    )
