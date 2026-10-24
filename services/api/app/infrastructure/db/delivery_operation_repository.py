"""H5/App Publication 投递事故的 PostgreSQL 统一投影与受控处置。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from services.api.app.application.delivery_operations import (
    DeliveryIncident,
    DeliveryIncidentNotFoundError,
    DeliveryOperationAction,
    DeliveryOperationAudit,
    DeliveryOperationCommand,
    DeliveryOperationConflictError,
    DeliveryOperationResult,
    DeliveryPlatform,
)
from services.api.app.application.outbox import INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE
from services.api.app.domain.inspection import (
    APP_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
    H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
    H5_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
    build_app_inspection_publication_available,
)
from services.api.app.domain.protocol import build_protocol_envelope
from services.api.app.infrastructure.db.delivery_incident_query_sql import (
    paged_delivery_incident_ctes_sql,
)
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.pool import get_postgres_pool

_ACTIVE_PUBLICATION_STATUSES = frozenset({"pending", "leased", "retry_wait"})
_MARK_REVIEW_STATUSES = _ACTIVE_PUBLICATION_STATUSES | {"failed"}


class PostgresDeliveryOperationRepository:
    """以数据库事务围栏执行投递重放、修复和转人工审查。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_incidents(
        self,
        *,
        task_id: str,
        limit: int,
    ) -> list[DeliveryIncident]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            h5_rows = await connection.fetch(
                _incident_query(DeliveryPlatform.H5, by_publication=False),
                task_id,
                limit,
            )
            app_rows = await connection.fetch(
                _incident_query(DeliveryPlatform.APP, by_publication=False),
                task_id,
                limit,
            )
        incidents = [
            *(_incident_from_row(row, DeliveryPlatform.H5) for row in h5_rows),
            *(_incident_from_row(row, DeliveryPlatform.APP) for row in app_rows),
        ]
        incidents.sort(
            key=lambda item: item.updated_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return incidents[:limit]

    async def execute(
        self,
        command: DeliveryOperationCommand,
    ) -> DeliveryOperationResult:
        pool = await self._get_pool()
        operation_id = str(uuid4())
        conflict_message: str | None = None
        result: DeliveryOperationResult | None = None
        async with pool.acquire() as connection, connection.transaction():
            locked = await self._load_locked_publication(connection, command)
            if locked is None:
                raise DeliveryIncidentNotFoundError(
                    f"{command.platform.value} Publication {command.publication_id} 不存在"
                )
            before = await self._fetch_incident(
                connection,
                platform=command.platform,
                publication_id=command.publication_id,
            )
            if before is None:
                raise DeliveryIncidentNotFoundError(
                    f"{command.platform.value} Publication {command.publication_id} 不存在"
                )

            conflict_message = self._validate_command(command, locked, before)
            if conflict_message is None:
                try:
                    affected = await self._apply_action(
                        connection,
                        command=command,
                        operation_id=operation_id,
                    )
                except DeliveryOperationConflictError as exc:
                    conflict_message = str(exc)
                    await self._insert_audit(
                        connection,
                        operation_id=operation_id,
                        command=command,
                        task_id=before.task_id,
                        before=before,
                        after=before,
                        result="rejected",
                        result_message=conflict_message,
                        affected_counts={},
                    )
                    affected = None
                if affected is None:
                    result = None
                    # 已持久化 rejected 审计，事务提交后再向 API 返回 409。
                else:
                    after = await self._fetch_incident(
                        connection,
                        platform=command.platform,
                        publication_id=command.publication_id,
                    )
                    if after is None:
                        raise RuntimeError("处置完成后 Publication 投影缺失")
                    await self._insert_audit(
                        connection,
                        operation_id=operation_id,
                        command=command,
                        task_id=before.task_id,
                        before=before,
                        after=after,
                        result="applied",
                        result_message="操作已应用",
                        affected_counts=affected,
                    )
                    final_incident = await self._fetch_incident(
                        connection,
                        platform=command.platform,
                        publication_id=command.publication_id,
                    )
                    if final_incident is None:
                        raise RuntimeError("处置审计写入后 Publication 投影缺失")
                    result = DeliveryOperationResult(
                        operation_id=operation_id,
                        action=command.action,
                        platform=command.platform,
                        publication_id=command.publication_id,
                        task_id=before.task_id,
                        result="applied",
                        affected_counts=affected,
                        incident=final_incident,
                    )
            else:
                await self._insert_audit(
                    connection,
                    operation_id=operation_id,
                    command=command,
                    task_id=before.task_id,
                    before=before,
                    after=before,
                    result="rejected",
                    result_message=conflict_message,
                    affected_counts={},
                )

        if conflict_message is not None:
            raise DeliveryOperationConflictError(conflict_message)
        if result is None:
            raise RuntimeError("投递处置没有产生结果")
        return result

    async def _fetch_incident(
        self,
        connection: Any,
        *,
        platform: DeliveryPlatform,
        publication_id: str,
    ) -> DeliveryIncident | None:
        row = await connection.fetchrow(
            _incident_query(platform, by_publication=True),
            publication_id,
        )
        return _incident_from_row(row, platform) if row is not None else None

    async def _load_locked_publication(
        self,
        connection: Any,
        command: DeliveryOperationCommand,
    ) -> Mapping[str, Any] | None:
        if command.platform is DeliveryPlatform.H5:
            return await connection.fetchrow(
                """
                SELECT
                    publication.id::text AS publication_id,
                    publication.task_id::text AS task_id,
                    publication.status,
                    publication.row_version,
                    publication.finalization_epoch,
                    publication.ruleset_id::text AS ruleset_id,
                    publication.ruleset_version,
                    publication.ruleset_snapshot_hash,
                    publication.input_hash,
                    task.status AS task_status,
                    task.finalization_state,
                    (
                        task.target_platform IN ('h5', 'web')
                        AND task.finalization_epoch = publication.finalization_epoch
                        AND task.ruleset_id = publication.ruleset_id
                        AND ruleset.version = publication.ruleset_version
                        AND ruleset.snapshot_hash IS NOT DISTINCT FROM
                            publication.ruleset_snapshot_hash
                        AND capture.task_id = task.id
                        AND common_capture.task_id = task.id
                        AND common_capture.agent_run_id = publication.source_agent_run_id
                        AND common_capture.capture_role = 'after_action'
                        AND common_capture.capture_status = 'captured'
                        AND assessment.task_id = task.id
                        AND assessment.agent_run_id = publication.source_agent_run_id
                        AND assessment.page_capture_id = publication.page_capture_id
                        AND assessment.payload_hash = publication.assessment_hash
                        AND assessment.detection_status = 'recognized'
                        AND assessment.page_type = capture.page_type
                        AND COALESCE(
                            capture.inspection_lifecycle_status,
                            common_capture.metadata->>'inspection_lifecycle_status',
                            ''
                        ) NOT IN ('superseded', 'abandoned')
                        AND EXISTS (
                            SELECT 1
                            FROM qc_h5_evidence_inventory_item AS inventory
                            WHERE inventory.task_id = task.id
                              AND inventory.agent_run_id =
                                  publication.source_agent_run_id
                              AND inventory.after_capture_id =
                                  publication.page_capture_id
                              AND inventory.status = 'pass'
                        )
                    ) AS source_valid
                FROM qc_h5_inspection_publication AS publication
                INNER JOIN qc_task AS task ON task.id = publication.task_id
                INNER JOIN qc_ruleset AS ruleset ON ruleset.id = publication.ruleset_id
                INNER JOIN qc_page_capture AS capture
                    ON capture.id = publication.page_capture_id
                INNER JOIN qc_capture AS common_capture
                    ON common_capture.id = publication.page_capture_id
                INNER JOIN qc_h5_page_type_assessment AS assessment
                    ON assessment.assessment_record_id =
                        publication.page_type_assessment_id
                WHERE publication.id = $1::uuid
                FOR UPDATE OF publication, task
                """,
                command.publication_id,
            )
        return await connection.fetchrow(
            """
            SELECT
                publication.id::text AS publication_id,
                publication.task_id::text AS task_id,
                publication.status,
                publication.row_version,
                publication.finalization_epoch,
                publication.ruleset_id::text AS ruleset_id,
                publication.ruleset_version,
                publication.ruleset_snapshot_hash,
                publication.input_hash,
                publication.evidence_index_id,
                publication.evidence_index_hash,
                task.status AS task_status,
                task.finalization_state,
                (
                    task.target_platform = 'app'
                    AND task.finalization_epoch = publication.finalization_epoch
                    AND task.ruleset_id = publication.ruleset_id
                    AND ruleset.version = publication.ruleset_version
                    AND ruleset.snapshot_hash IS NOT DISTINCT FROM
                        publication.ruleset_snapshot_hash
                    AND evidence_index.task_id = task.id
                    AND evidence_index.agent_run_id = publication.source_agent_run_id
                    AND evidence_index.status = 'sealed'
                    AND evidence_index.content_sha256 = publication.evidence_index_hash
                    AND (
                        SELECT count(*)
                        FROM qc_app_inspection_publication_entry AS entry
                        WHERE entry.publication_id = publication.id
                    ) = publication.expected_entry_count
                    AND (
                        SELECT count(*)
                        FROM qc_app_evidence_index_entry AS entry
                        WHERE entry.index_id = evidence_index.index_id
                    ) = publication.expected_entry_count
                    AND NOT EXISTS (
                        SELECT 1
                        FROM qc_app_inspection_publication_entry AS entry
                        LEFT JOIN qc_app_evidence_index_entry AS index_entry
                            ON index_entry.index_id = evidence_index.index_id
                           AND index_entry.sequence_no = entry.sequence_no
                           AND index_entry.capture_id = entry.capture_id
                           AND index_entry.evidence_manifest_id =
                                entry.evidence_manifest_id
                           AND index_entry.evidence_manifest_version =
                                entry.evidence_manifest_version
                           AND index_entry.semantic_role = entry.semantic_role
                        LEFT JOIN qc_capture AS capture
                            ON capture.id = entry.capture_id
                           AND capture.task_id = task.id
                           AND capture.agent_run_id = publication.source_agent_run_id
                           AND capture.surface_session_id =
                                evidence_index.surface_session_id
                           AND capture.capture_status = 'captured'
                           AND capture.surface_kind IN ('android', 'ios')
                        LEFT JOIN qc_evidence_manifest AS manifest
                            ON manifest.manifest_id = entry.evidence_manifest_id
                           AND manifest.manifest_version =
                                entry.evidence_manifest_version
                           AND manifest.task_id = task.id
                           AND manifest.capture_id = entry.capture_id
                           AND manifest.session_id = evidence_index.surface_session_id
                           AND manifest.quality_status = 'pass'
                        WHERE entry.publication_id = publication.id
                          AND (
                              index_entry.entry_id IS NULL
                              OR capture.id IS NULL
                              OR manifest.manifest_id IS NULL
                          )
                    )
                ) AS source_valid
            FROM qc_app_inspection_publication AS publication
            INNER JOIN qc_task AS task ON task.id = publication.task_id
            INNER JOIN qc_ruleset AS ruleset ON ruleset.id = publication.ruleset_id
            INNER JOIN qc_app_evidence_index AS evidence_index
                ON evidence_index.index_id = publication.evidence_index_id
            WHERE publication.id = $1::uuid
            FOR UPDATE OF publication, task
            """,
            command.publication_id,
        )

    def _validate_command(
        self,
        command: DeliveryOperationCommand,
        locked: Mapping[str, Any],
        incident: DeliveryIncident,
    ) -> str | None:
        if int(locked["row_version"]) != command.expected_row_version:
            return (
                "Publication 版本已变化："
                f"expected={command.expected_row_version}, actual={locked['row_version']}"
            )
        if command.action in {
            DeliveryOperationAction.RECONCILE,
            DeliveryOperationAction.REQUEUE,
        }:
            if str(locked["task_status"]) != "running":
                return "任务已不处于 running，拒绝重新投递"
            if str(locked["finalization_state"]) not in {
                "open",
                "exploration_draining",
                "session_draining",
            }:
                return "任务收口窗口已关闭，拒绝重新投递"
            if not bool(locked["source_valid"]):
                return "任务 epoch、规则快照或主证据来源身份已漂移，拒绝重新投递"
        if command.action not in incident.allowed_actions:
            return f"当前状态不允许执行 {command.action.value}"
        return None

    async def _apply_action(
        self,
        connection: Any,
        *,
        command: DeliveryOperationCommand,
        operation_id: str,
    ) -> dict[str, int]:
        if command.action is DeliveryOperationAction.REQUEUE:
            return await self._requeue_delivery(connection, command)
        if command.action is DeliveryOperationAction.MARK_NEEDS_REVIEW:
            return await self._mark_needs_review(connection, command)
        return await self._reconcile(
            connection,
            command=command,
            operation_id=operation_id,
        )

    async def _requeue_delivery(
        self,
        connection: Any,
        command: DeliveryOperationCommand,
    ) -> dict[str, int]:
        publication_type, link_table = _publication_delivery_identity(command.platform)
        row = await connection.fetchrow(
            f"""
            WITH current_aggregates AS (
                SELECT $1::varchar AS aggregate_type, $2::varchar AS aggregate_id,
                    $3::bigint AS aggregate_version
                UNION ALL
                SELECT 'inspection_work_item', work_item.id::text, work_item.row_version
                FROM {link_table} AS link
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = link.work_item_id
                WHERE link.publication_id = $2::uuid
                  AND work_item.task_id = $4::uuid
                  AND work_item.finalization_epoch = (
                      SELECT publication.finalization_epoch
                      FROM {_publication_tables(command.platform)[0]} AS publication
                      WHERE publication.id = $2::uuid
                  )
                  AND work_item.status IN (
                      'pending', 'leased', 'running', 'retry_wait', 'waiting_evidence'
                  )
            ), candidates AS (
                SELECT event.message_id
                FROM qc_outbox_event AS event
                INNER JOIN current_aggregates AS aggregate
                    ON aggregate.aggregate_type = event.aggregate_type
                   AND aggregate.aggregate_id = event.aggregate_id
                   AND aggregate.aggregate_version = event.aggregate_version
                WHERE event.task_id = $4::uuid
                  AND event.message_type = ANY($5::varchar[])
                  AND (
                      event.status = 'dead_letter'
                      OR EXISTS (
                          SELECT 1 FROM qc_event_inbox AS inbox
                          WHERE inbox.message_id = event.message_id
                            AND inbox.status = 'dead_letter'
                      )
                  )
                FOR UPDATE OF event
            ), reset_inbox AS (
                UPDATE qc_event_inbox AS inbox
                SET status = 'pending', attempt_count = 0,
                    next_attempt_at = now(), lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error = NULL, completed_at = NULL,
                    row_version = inbox.row_version + 1, updated_at = now()
                FROM candidates
                WHERE inbox.message_id = candidates.message_id
                  AND inbox.status = 'dead_letter'
                RETURNING 1
            ), reset_outbox AS (
                UPDATE qc_outbox_event AS event
                SET status = 'pending', attempt_count = 0,
                    next_attempt_at = now(), lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error = NULL, published_at = NULL,
                    replay_count = event.replay_count + 1,
                    last_requeued_at = now(), updated_at = now()
                FROM candidates
                WHERE event.message_id = candidates.message_id
                RETURNING 1
            )
            SELECT
                (SELECT count(*) FROM reset_outbox)::integer AS outbox_count,
                (SELECT count(*) FROM reset_inbox)::integer AS inbox_count
            """,
            publication_type,
            command.publication_id,
            command.expected_row_version,
            # task_id 从已锁定的 Publication 投影取得，避免信任客户端。
            (await self._task_id_for_publication(connection, command)),
            [
                _publication_message_type(command.platform),
                INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
            ],
        )
        counts = {
            "outbox_requeued": int(row["outbox_count"] if row else 0),
            "inbox_requeued": int(row["inbox_count"] if row else 0),
        }
        if not any(counts.values()):
            raise DeliveryOperationConflictError("没有可重放的当前版本死信")
        return counts

    async def _mark_needs_review(
        self,
        connection: Any,
        command: DeliveryOperationCommand,
    ) -> dict[str, int]:
        publication_table, link_table = _publication_tables(command.platform)
        publication_count_set = ""
        if command.platform is DeliveryPlatform.APP:
            publication_count_set = """
                    terminal_entry_count = expected_entry_count,
                    expected_rule_count = (
                        SELECT count(*)::integer
                        FROM qc_app_inspection_publication_rule AS member
                        WHERE member.publication_id = publication.id
                    ),
                    linked_work_item_count = (
                        SELECT count(*)::integer
                        FROM qc_app_inspection_publication_work_item AS link
                        WHERE link.publication_id = publication.id
                    ),
            """
        row = await connection.fetchrow(
            f"""
            WITH updated_publication AS (
                UPDATE {publication_table} AS publication
                SET status = 'needs_review',
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    failure_code = 'operator_marked_needs_review',
                    failure_message = $3,
                    {publication_count_set}
                    row_version = row_version + 1,
                    finished_at = now(), updated_at = now()
                WHERE publication.id = $1::uuid
                  AND publication.row_version = $2
                  AND publication.status IN (
                      'pending', 'leased', 'retry_wait', 'published', 'failed'
                  )
                RETURNING task_id, row_version
            ), linked_work AS (
                SELECT work_item_id
                FROM {link_table}
                WHERE publication_id = $1::uuid
            ), stopped_attempts AS (
                UPDATE qc_inspection_attempt AS attempt
                SET status = 'abandoned',
                    error_code = COALESCE(
                        attempt.error_code, 'OPERATOR_MARKED_NEEDS_REVIEW'
                    ),
                    error_message = COALESCE(attempt.error_message, $3),
                    finished_at = COALESCE(attempt.finished_at, now()),
                    updated_at = now()
                FROM linked_work, updated_publication
                WHERE attempt.work_item_id = linked_work.work_item_id
                  AND attempt.status IN ('running', 'judged')
                RETURNING attempt.work_item_id
            ), stopped_work AS (
                UPDATE qc_inspection_work_item AS work_item
                SET status = 'failed', consumer_agent_run_id = NULL,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = 'OPERATOR_MARKED_NEEDS_REVIEW',
                    last_error_message = $3,
                    finished_at = now(), updated_at = now()
                FROM linked_work, updated_publication
                WHERE work_item.id = linked_work.work_item_id
                  AND work_item.status IN (
                      'pending', 'leased', 'running', 'retry_wait', 'waiting_evidence'
                  )
                RETURNING work_item.id
            ), aggregate_ids AS (
                SELECT $4::varchar AS aggregate_type, $1::varchar AS aggregate_id
                UNION ALL
                SELECT 'inspection_work_item', linked_work.work_item_id::text
                FROM linked_work
            ), stopped_outbox AS (
                UPDATE qc_outbox_event AS event
                SET status = 'dead_letter', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, published_at = NULL,
                    last_error = 'operator_marked_needs_review', updated_at = now()
                FROM aggregate_ids, updated_publication
                WHERE event.task_id = updated_publication.task_id
                  AND event.aggregate_type = aggregate_ids.aggregate_type
                  AND event.aggregate_id = aggregate_ids.aggregate_id
                  AND event.status IN ('pending', 'publishing', 'retry_wait')
                RETURNING event.message_id
            ), stopped_inbox AS (
                UPDATE qc_event_inbox AS inbox
                SET status = 'dead_letter', lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, completed_at = NULL,
                    last_error = 'operator_marked_needs_review',
                    row_version = inbox.row_version + 1, updated_at = now()
                FROM qc_outbox_event AS event, aggregate_ids, updated_publication
                WHERE inbox.message_id = event.message_id
                  AND event.task_id = updated_publication.task_id
                  AND event.aggregate_type = aggregate_ids.aggregate_type
                  AND event.aggregate_id = aggregate_ids.aggregate_id
                  AND inbox.status IN ('pending', 'processing', 'retry_wait')
                RETURNING inbox.message_id
            )
            SELECT
                (SELECT count(*) FROM updated_publication)::integer AS publication_count,
                (SELECT count(*) FROM stopped_attempts)::integer AS attempt_count,
                (SELECT count(*) FROM stopped_work)::integer AS work_count,
                (SELECT count(*) FROM stopped_outbox)::integer AS outbox_count,
                (SELECT count(*) FROM stopped_inbox)::integer AS inbox_count
            """,
            command.publication_id,
            command.expected_row_version,
            f"操作人明确转人工审查：{command.reason}",
            _publication_delivery_identity(command.platform)[0],
        )
        publication_count = int(row["publication_count"] if row else 0)
        if publication_count != 1:
            raise DeliveryOperationConflictError("Publication 状态或版本已变化")
        entry_count = 0
        if command.platform is DeliveryPlatform.APP:
            status = await connection.execute(
                """
                UPDATE qc_app_inspection_publication_entry
                SET status = 'needs_review',
                    failure_code = 'operator_marked_needs_review',
                    failure_message = $2,
                    finished_at = now(), updated_at = now()
                WHERE publication_id = $1::uuid AND status = 'pending'
                """,
                command.publication_id,
                f"操作人明确转人工审查：{command.reason}",
            )
            entry_count = _execute_count(status)
        return {
            "publication_marked_needs_review": publication_count,
            "app_entries_marked_needs_review": entry_count,
            "attempts_abandoned": int(row["attempt_count"] if row else 0),
            "work_items_terminalized": int(row["work_count"] if row else 0),
            "outbox_stopped": int(row["outbox_count"] if row else 0),
            "inbox_stopped": int(row["inbox_count"] if row else 0),
        }

    async def _reconcile(
        self,
        connection: Any,
        *,
        command: DeliveryOperationCommand,
        operation_id: str,
    ) -> dict[str, int]:
        publication_row = await self._reconcile_publication(
            connection,
            command=command,
        )
        work_rows = await self._reconcile_linked_work(
            connection,
            command=command,
        )
        if publication_row is None and not work_rows:
            raise DeliveryOperationConflictError("没有到期租约、到期重试或失联待办可修复")
        if publication_row is not None:
            await self._insert_publication_event(
                connection,
                platform=command.platform,
                row=publication_row,
            )
        for row in work_rows:
            await self._insert_work_event(
                connection,
                row=row,
                operation_id=operation_id,
            )
        return {
            "publication_reconciled": int(publication_row is not None),
            "work_items_reconciled": len(work_rows),
        }

    async def _reconcile_publication(
        self,
        connection: Any,
        *,
        command: DeliveryOperationCommand,
    ) -> Mapping[str, Any] | None:
        publication_table, _ = _publication_tables(command.platform)
        extra_columns = (
            ", evidence_index_id, evidence_index_hash, input_hash"
            if command.platform is DeliveryPlatform.APP
            else ""
        )
        return await connection.fetchrow(
            f"""
            UPDATE {publication_table} AS publication
            SET status = 'pending', lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, next_attempt_at = now(),
                row_version = row_version + 1,
                failure_code = NULL, failure_message = NULL,
                finished_at = NULL, updated_at = now()
            WHERE publication.id = $1::uuid
              AND publication.row_version = $2
              AND (
                  (publication.status = 'leased' AND publication.lease_expires_at <= now())
                  OR (publication.status = 'retry_wait'
                      AND publication.next_attempt_at <= now()
                      AND publication.attempt_count < publication.max_attempts)
                  OR (
                      publication.status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1 FROM qc_outbox_event AS event
                          WHERE event.aggregate_type = $3
                            AND event.aggregate_id = publication.id::text
                            AND event.aggregate_version = publication.row_version
                            AND event.status IN ('pending', 'publishing', 'retry_wait')
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM qc_event_inbox AS inbox
                          WHERE inbox.aggregate_type = $3
                            AND inbox.aggregate_id = publication.id::text
                            AND inbox.aggregate_version = publication.row_version
                            AND inbox.status IN ('pending', 'processing', 'retry_wait')
                      )
                  )
              )
            RETURNING id::text AS publication_id, task_id::text AS task_id,
                finalization_epoch, row_version{extra_columns}
            """,
            command.publication_id,
            command.expected_row_version,
            _publication_delivery_identity(command.platform)[0],
        )

    async def _reconcile_linked_work(
        self,
        connection: Any,
        *,
        command: DeliveryOperationCommand,
    ) -> list[Mapping[str, Any]]:
        publication_table, link_table = _publication_tables(command.platform)
        return list(
            await connection.fetch(
                f"""
                WITH candidates AS (
                    SELECT work_item.id
                    FROM {link_table} AS link
                    INNER JOIN {publication_table} AS publication
                        ON publication.id = link.publication_id
                    INNER JOIN qc_inspection_work_item AS work_item
                        ON work_item.id = link.work_item_id
                       AND work_item.task_id = publication.task_id
                       AND work_item.finalization_epoch =
                            publication.finalization_epoch
                    INNER JOIN qc_task AS task
                        ON task.id = publication.task_id
                       AND task.finalization_epoch = publication.finalization_epoch
                    WHERE link.publication_id = $1::uuid
                      AND task.status = 'running'
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      AND work_item.control_mode = 'enforced'
                      AND work_item.attempt_count < work_item.max_attempts
                      AND (
                          (work_item.status = 'retry_wait'
                              AND work_item.next_attempt_at <= now())
                          OR (work_item.status IN ('leased', 'running')
                              AND work_item.lease_expires_at <= now())
                          OR (
                              work_item.status = 'pending'
                              AND NOT EXISTS (
                                  SELECT 1 FROM qc_outbox_event AS event
                                  WHERE event.aggregate_type = 'inspection_work_item'
                                    AND event.aggregate_id = work_item.id::text
                                    AND event.aggregate_version = work_item.row_version
                                    AND event.status IN (
                                        'pending', 'publishing', 'retry_wait'
                                    )
                              )
                              AND NOT EXISTS (
                                  SELECT 1 FROM qc_event_inbox AS inbox
                                  WHERE inbox.aggregate_type = 'inspection_work_item'
                                    AND inbox.aggregate_id = work_item.id::text
                                    AND inbox.aggregate_version = work_item.row_version
                                    AND inbox.status IN (
                                        'pending', 'processing', 'retry_wait'
                                    )
                              )
                          )
                      )
                    FOR UPDATE OF work_item
                ), abandoned_attempts AS (
                    UPDATE qc_inspection_attempt AS attempt
                    SET status = 'abandoned',
                        error_code = COALESCE(
                            attempt.error_code, 'OPERATOR_RECONCILE_LEASE_EXPIRED'
                        ),
                        error_message = COALESCE(
                            attempt.error_message,
                            '运维对账发现规则工作租约已过期'
                        ),
                        finished_at = COALESCE(attempt.finished_at, now()),
                        updated_at = now()
                    FROM candidates
                    WHERE attempt.work_item_id = candidates.id
                      AND attempt.status IN ('running', 'judged')
                    RETURNING attempt.work_item_id
                ), updated_work AS (
                    UPDATE qc_inspection_work_item AS work_item
                    SET status = 'pending', next_attempt_at = now(),
                        consumer_agent_run_id = NULL,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL,
                        row_version = work_item.row_version + 1,
                        last_error_code = NULL, last_error_message = NULL,
                        updated_at = now()
                    FROM candidates
                    WHERE work_item.id = candidates.id
                    RETURNING work_item.id::text AS work_item_id,
                        work_item.task_id::text AS task_id,
                        work_item.page_capture_id::text AS page_capture_id,
                        work_item.protocol_version, work_item.batch_key,
                        work_item.strategy, work_item.category,
                        work_item.session_dependency, work_item.input_hash,
                        work_item.control_mode, work_item.row_version
                )
                SELECT * FROM updated_work
                """,
                command.publication_id,
            )
        )

    async def _insert_publication_event(
        self,
        connection: Any,
        *,
        platform: DeliveryPlatform,
        row: Mapping[str, Any],
    ) -> None:
        if platform is DeliveryPlatform.H5:
            await insert_outbox_event(
                connection,
                aggregate_type="h5_inspection_publication",
                aggregate_id=str(row["publication_id"]),
                envelope=build_protocol_envelope(
                    message_type=H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
                    payload_protocol_version=H5_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
                    idempotency_key=(
                        "h5-inspection-publication:"
                        f"{row['publication_id']}:{row['row_version']}"
                    ),
                    task_id=str(row["task_id"]),
                    aggregate_type="h5_inspection_publication",
                    aggregate_id=str(row["publication_id"]),
                    aggregate_version=int(row["row_version"]),
                    occurred_at=datetime.now(UTC),
                    payload={
                        "protocol_version": H5_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
                        "publication_id": str(row["publication_id"]),
                        "task_id": str(row["task_id"]),
                        "finalization_epoch": int(row["finalization_epoch"]),
                        "message_version": int(row["row_version"]),
                    },
                ),
            )
            return
        await insert_outbox_event(
            connection,
            aggregate_type="app_inspection_publication",
            aggregate_id=str(row["publication_id"]),
            envelope=build_app_inspection_publication_available(
                publication_id=str(row["publication_id"]),
                task_id=str(row["task_id"]),
                evidence_index_id=str(row["evidence_index_id"]),
                evidence_index_hash=str(row["evidence_index_hash"]),
                finalization_epoch=int(row["finalization_epoch"]),
                row_version=int(row["row_version"]),
                input_hash=str(row["input_hash"]),
                occurred_at=datetime.now(UTC),
            ),
        )

    async def _insert_work_event(
        self,
        connection: Any,
        *,
        row: Mapping[str, Any],
        operation_id: str,
    ) -> None:
        work_item_id = str(row["work_item_id"])
        row_version = int(row["row_version"])
        protocol_version = str(row["protocol_version"])
        await insert_outbox_event(
            connection,
            aggregate_type="inspection_work_item",
            aggregate_id=work_item_id,
            envelope=build_protocol_envelope(
                message_type=INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
                payload_protocol_version=protocol_version,
                idempotency_key=f"operator-reconcile:{operation_id}:{work_item_id}:{row_version}",
                task_id=str(row["task_id"]),
                aggregate_type="inspection_work_item",
                aggregate_id=work_item_id,
                aggregate_version=row_version,
                occurred_at=datetime.now(UTC),
                payload={
                    "protocol_version": protocol_version,
                    "work_item_id": work_item_id,
                    "task_id": str(row["task_id"]),
                    "page_capture_id": str(row["page_capture_id"]),
                    "batch_key": str(row["batch_key"]),
                    "strategy": str(row["strategy"]),
                    "category": row["category"],
                    "session_dependency": str(row["session_dependency"]),
                    "control_mode": str(row["control_mode"]),
                    "input_hash": str(row["input_hash"]),
                    "reopen_reason": "operator_reconcile",
                },
                correlation_id=str(row["task_id"]),
            ),
        )

    async def _task_id_for_publication(
        self,
        connection: Any,
        command: DeliveryOperationCommand,
    ) -> str:
        publication_table, _ = _publication_tables(command.platform)
        value = await connection.fetchval(
            f"SELECT task_id::text FROM {publication_table} WHERE id = $1::uuid",
            command.publication_id,
        )
        if value is None:
            raise DeliveryIncidentNotFoundError("Publication 不存在")
        return str(value)

    async def _insert_audit(
        self,
        connection: Any,
        *,
        operation_id: str,
        command: DeliveryOperationCommand,
        task_id: str,
        before: DeliveryIncident,
        after: DeliveryIncident,
        result: str,
        result_message: str,
        affected_counts: dict[str, int],
    ) -> None:
        await connection.execute(
            """
            INSERT INTO qc_delivery_operation_audit (
                operation_id, task_id, platform, publication_id, action,
                operator_user_id, reason, expected_row_version,
                before_row_version, after_row_version, result, result_message,
                affected_counts, before_state, after_state
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4::uuid, $5,
                $6::uuid, $7, $8, $9, $10, $11, $12,
                $13::jsonb, $14::jsonb, $15::jsonb
            )
            """,
            operation_id,
            task_id,
            command.platform.value,
            command.publication_id,
            command.action.value,
            command.operator_user_id,
            command.reason,
            command.expected_row_version,
            before.row_version,
            after.row_version,
            result,
            result_message,
            _json_dump(affected_counts),
            _json_dump(_incident_state(before)),
            _json_dump(_incident_state(after)),
        )

    async def _get_pool(self) -> Any:
        return self._pool or await get_postgres_pool(self._database_url)


def _incident_query(platform: DeliveryPlatform, *, by_publication: bool) -> str:
    publication_table, link_table = _publication_tables(platform)
    aggregate_type, _ = _publication_delivery_identity(platform)
    source_identity = (
        "jsonb_build_object("
        "'source_agent_run_id', publication.source_agent_run_id::text, "
        "'page_capture_id', publication.page_capture_id::text, "
        "'page_type_assessment_id', publication.page_type_assessment_id, "
        "'assessment_hash', publication.assessment_hash)"
        if platform is DeliveryPlatform.H5
        else
        "jsonb_build_object("
        "'source_agent_run_id', publication.source_agent_run_id::text, "
        "'evidence_index_id', publication.evidence_index_id, "
        "'evidence_index_hash', publication.evidence_index_hash, "
        "'expected_entry_count', publication.expected_entry_count)"
    )
    where = (
        "publication.id = $1::uuid"
        if by_publication
        else "publication.task_id = $1::uuid"
    )
    limit = "" if by_publication else "LIMIT $2"
    prefix = ""
    source_table = publication_table
    delivery_join = _publication_delivery_join_sql(platform, link_table, aggregate_type)
    if not by_publication:
        prefix = paged_delivery_incident_ctes_sql(
            publication_table=publication_table,
            link_table=link_table,
            aggregate_type=aggregate_type,
            publication_message_type=_publication_message_type(platform),
            inspection_message_type=INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
        )
        source_table = "scoped_publications"
        delivery_join = (
            "LEFT JOIN delivery_stats AS delivery "
            "ON delivery.publication_id = publication.id"
        )
    return f"""
        {prefix}
        SELECT
            publication.id::text AS publication_id,
            publication.task_id::text AS task_id,
            publication.status, publication.row_version,
            publication.finalization_epoch,
            publication.ruleset_id::text AS ruleset_id,
            publication.ruleset_version,
            publication.ruleset_snapshot_hash,
            {source_identity} AS source_identity,
            publication.failure_code, publication.failure_message,
            publication.updated_at,
            COALESCE(delivery.outbox_pending_count, 0)::integer
                AS outbox_pending_count,
            COALESCE(delivery.outbox_dead_letter_count, 0)::integer
                AS outbox_dead_letter_count,
            COALESCE(delivery.inbox_pending_count, 0)::integer
                AS inbox_pending_count,
            COALESCE(delivery.inbox_dead_letter_count, 0)::integer
                AS inbox_dead_letter_count,
            COALESCE(work_items.total_count, 0)::integer AS linked_work_item_count,
            COALESCE(work_items.active_count, 0)::integer AS linked_work_active_count,
            COALESCE(work_items.failed_count, 0)::integer AS linked_work_failed_count,
            COALESCE(operations.items, '[]'::jsonb) AS operations
        FROM {source_table} AS publication
        {delivery_join}
        LEFT JOIN LATERAL (
            SELECT count(*) AS total_count,
                count(*) FILTER (WHERE work_item.status IN (
                    'pending', 'leased', 'running', 'retry_wait', 'waiting_evidence'
                )) AS active_count,
                count(*) FILTER (WHERE work_item.status = 'failed') AS failed_count
            FROM {link_table} AS work_link
            INNER JOIN qc_inspection_work_item AS work_item
                ON work_item.id = work_link.work_item_id
            WHERE work_link.publication_id = publication.id
        ) AS work_items ON TRUE
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(operation.item ORDER BY operation.created_at DESC) AS items
            FROM (
                SELECT audit.created_at,
                    jsonb_build_object(
                        'operation_id', audit.operation_id::text,
                        'action', audit.action,
                        'operator_user_id', audit.operator_user_id::text,
                        'reason', audit.reason,
                        'result', audit.result,
                        'before_state', audit.before_state,
                        'after_state', audit.after_state,
                        'created_at', audit.created_at
                    ) AS item
                FROM qc_delivery_operation_audit AS audit
                WHERE audit.platform = '{platform.value}'
                  AND audit.publication_id = publication.id
                ORDER BY audit.created_at DESC
                LIMIT 20
            ) AS operation
        ) AS operations ON TRUE
        WHERE {where}
        ORDER BY publication.updated_at DESC, publication.id
        {limit}
    """


def _publication_delivery_join_sql(
    platform: DeliveryPlatform, link_table: str, aggregate_type: str,
) -> str:
    """保留单个 Publication 的受控处置读模型，不改变写侧围栏。"""
    return f"""
        LEFT JOIN LATERAL (
            SELECT
                count(*) FILTER (
                    WHERE event.status IN ('pending', 'publishing', 'retry_wait')
                ) AS outbox_pending_count,
                count(*) FILTER (WHERE event.status = 'dead_letter')
                    AS outbox_dead_letter_count,
                COALESCE(sum((
                    SELECT count(*) FROM qc_event_inbox AS inbox
                    WHERE inbox.message_id = event.message_id
                      AND inbox.status IN ('pending', 'processing', 'retry_wait')
                )), 0) AS inbox_pending_count,
                COALESCE(sum((
                    SELECT count(*) FROM qc_event_inbox AS inbox
                    WHERE inbox.message_id = event.message_id
                      AND inbox.status = 'dead_letter'
                )), 0) AS inbox_dead_letter_count
            FROM qc_outbox_event AS event
            WHERE event.task_id = publication.task_id
              AND event.message_type = ANY(ARRAY[
                    '{_publication_message_type(platform)}',
                    '{INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE}'
                  ]::varchar[])
              AND (
                  (event.aggregate_type = '{aggregate_type}'
                    AND event.aggregate_id = publication.id::text
                    AND event.aggregate_version = publication.row_version)
                  OR (
                    event.aggregate_type = 'inspection_work_item'
                    AND EXISTS (
                        SELECT 1
                        FROM {link_table} AS delivery_link
                        INNER JOIN qc_inspection_work_item AS linked_work
                            ON linked_work.id = delivery_link.work_item_id
                        WHERE delivery_link.publication_id = publication.id
                          AND linked_work.id::text = event.aggregate_id
                          AND linked_work.row_version = event.aggregate_version
                    )
                  )
              )
        ) AS delivery ON TRUE
    """


def _incident_from_row(
    row: Mapping[str, Any],
    platform: DeliveryPlatform,
) -> DeliveryIncident:
    status = str(row["status"])
    outbox_dead = int(row["outbox_dead_letter_count"] or 0)
    inbox_dead = int(row["inbox_dead_letter_count"] or 0)
    allowed: list[DeliveryOperationAction] = []
    has_active_work = int(row["linked_work_active_count"] or 0) > 0
    has_failed_work = int(row["linked_work_failed_count"] or 0) > 0
    if status in _ACTIVE_PUBLICATION_STATUSES or (
        status == "published" and has_active_work
    ):
        allowed.append(DeliveryOperationAction.RECONCILE)
        if outbox_dead or inbox_dead:
            allowed.append(DeliveryOperationAction.REQUEUE)
    if status in _MARK_REVIEW_STATUSES or (
        status == "published"
        and (has_active_work or has_failed_work or outbox_dead or inbox_dead)
    ):
        allowed.append(DeliveryOperationAction.MARK_NEEDS_REVIEW)
    operation_values = _json_value(row.get("operations"), default=[])
    operations = tuple(
        DeliveryOperationAudit(
            operation_id=str(item["operation_id"]),
            action=DeliveryOperationAction(str(item["action"])),
            operator_user_id=str(item["operator_user_id"]),
            reason=str(item["reason"]),
            result=str(item["result"]),
            before_state=dict(item.get("before_state") or {}),
            after_state=dict(item.get("after_state") or {}),
            created_at=_as_datetime(item["created_at"]),
        )
        for item in operation_values
        if isinstance(item, Mapping)
    )
    return DeliveryIncident(
        platform=platform,
        publication_id=str(row["publication_id"]),
        task_id=str(row["task_id"]),
        status=status,
        row_version=int(row["row_version"]),
        finalization_epoch=int(row["finalization_epoch"]),
        ruleset_id=str(row["ruleset_id"]),
        ruleset_version=int(row["ruleset_version"]),
        ruleset_snapshot_hash=_optional_text(row["ruleset_snapshot_hash"]),
        source_identity=dict(_json_value(row["source_identity"], default={})),
        outbox_pending_count=int(row["outbox_pending_count"] or 0),
        outbox_dead_letter_count=outbox_dead,
        inbox_pending_count=int(row["inbox_pending_count"] or 0),
        inbox_dead_letter_count=inbox_dead,
        linked_work_item_count=int(row["linked_work_item_count"] or 0),
        linked_work_active_count=int(row["linked_work_active_count"] or 0),
        linked_work_failed_count=int(row["linked_work_failed_count"] or 0),
        failure_code=_optional_text(row["failure_code"]),
        failure_message=_optional_text(row["failure_message"]),
        allowed_actions=tuple(allowed),
        operations=operations,
        updated_at=row["updated_at"],
    )


def _incident_state(incident: DeliveryIncident) -> dict[str, Any]:
    return {
        "platform": incident.platform.value,
        "publication_id": incident.publication_id,
        "task_id": incident.task_id,
        "status": incident.status,
        "row_version": incident.row_version,
        "finalization_epoch": incident.finalization_epoch,
        "ruleset_id": incident.ruleset_id,
        "ruleset_version": incident.ruleset_version,
        "ruleset_snapshot_hash": incident.ruleset_snapshot_hash,
        "source_identity": incident.source_identity,
        "outbox_pending_count": incident.outbox_pending_count,
        "outbox_dead_letter_count": incident.outbox_dead_letter_count,
        "inbox_pending_count": incident.inbox_pending_count,
        "inbox_dead_letter_count": incident.inbox_dead_letter_count,
        "linked_work_item_count": incident.linked_work_item_count,
        "linked_work_active_count": incident.linked_work_active_count,
        "linked_work_failed_count": incident.linked_work_failed_count,
        "failure_code": incident.failure_code,
        "failure_message": incident.failure_message,
    }


def _publication_tables(platform: DeliveryPlatform) -> tuple[str, str]:
    if platform is DeliveryPlatform.H5:
        return (
            "qc_h5_inspection_publication",
            "qc_h5_inspection_publication_work_item",
        )
    return (
        "qc_app_inspection_publication",
        "qc_app_inspection_publication_work_item",
    )


def _publication_delivery_identity(platform: DeliveryPlatform) -> tuple[str, str]:
    _, link_table = _publication_tables(platform)
    aggregate_type = (
        "h5_inspection_publication"
        if platform is DeliveryPlatform.H5
        else "app_inspection_publication"
    )
    return aggregate_type, link_table


def _publication_message_type(platform: DeliveryPlatform) -> str:
    return (
        H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE
        if platform is DeliveryPlatform.H5
        else APP_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE
    )


def _json_value(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _execute_count(status: str) -> int:
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0
