"""App Publication 逻辑 rule_judge Run 的统一数据库收口。"""

from __future__ import annotations

from typing import Any


async def finish_app_consumer_run_if_idle_on_connection(
    connection: Any,
    *,
    task_id: str,
    consumer_agent_run_id: str,
) -> bool:
    """当前 epoch 的 App Publication 全链无在途事实后关闭逻辑消费 Run。"""

    value = await connection.fetchval(
        """
        WITH run_context AS (
            SELECT
                id AS consumer_id,
                task_id,
                (input_context->>'finalization_epoch')::integer AS finalization_epoch
            FROM qc_agent_run AS consumer
            WHERE consumer.id = $2::uuid
              AND consumer.task_id = $1::uuid
              AND consumer.agent_type = 'rule_judge'
              AND consumer.status = 'running'
              AND consumer.input_context->>'source' = 'app_inspection_publication'
        ), publication_scope AS (
            SELECT publication.id, publication.status
            FROM qc_app_inspection_publication AS publication
            INNER JOIN run_context
                ON run_context.task_id = publication.task_id
               AND run_context.finalization_epoch = publication.finalization_epoch
        ), work_scope AS (
            SELECT work_item.id, work_item.status
            FROM publication_scope AS publication
            INNER JOIN qc_app_inspection_publication_work_item AS link
                ON link.publication_id = publication.id
            INNER JOIN qc_inspection_work_item AS work_item
                ON work_item.id = link.work_item_id
        ), chain_state AS (
            SELECT
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
                              outbox.aggregate_type = 'app_inspection_publication'
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
                              inbox.aggregate_type = 'app_inspection_publication'
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
                )
                OR EXISTS (
                    SELECT 1
                    FROM qc_outbox_event AS outbox
                    INNER JOIN run_context ON run_context.task_id = outbox.task_id
                    WHERE outbox.status = 'dead_letter'
                      AND (
                          (
                              outbox.aggregate_type = 'app_inspection_publication'
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
                              inbox.aggregate_type = 'app_inspection_publication'
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
                ) AS has_failed
        )
        UPDATE qc_agent_run AS consumer
        SET status = CASE
                WHEN task.status = 'canceled' THEN 'canceled'
                WHEN chain_state.has_failed THEN 'failed'
                ELSE 'done'
            END,
            stop_reason = CASE
                WHEN task.status = 'canceled' THEN 'task_canceled'
                WHEN chain_state.has_failed THEN 'app_inspection_chain_failed'
                ELSE 'app_inspection_work_drained'
            END,
            error_message = CASE
                WHEN task.status = 'canceled' THEN NULL
                WHEN chain_state.has_failed
                    THEN 'App Publication 链存在失败 Work 或消息死信'
                ELSE NULL
            END,
            finished_at = now(),
            updated_at = now()
        FROM qc_task AS task, run_context, chain_state
        WHERE task.id = $1::uuid
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
