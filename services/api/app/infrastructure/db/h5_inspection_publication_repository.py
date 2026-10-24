"""H5 Publication 的 PostgreSQL 权威仓储与 Outbox 原子写入。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from services.api.app.application.h5_capture_readiness import h5_capture_is_inspection_ready
from services.api.app.application.h5_inspection_publication import (
    H5InspectionPublicationClaimDisposition,
    H5InspectionPublicationClaimResult,
    H5InspectionPublicationLease,
    H5InspectionPublicationTaskSummary,
    H5InspectionPublicationWriteResult,
)
from services.api.app.application.inspection_work_items import (
    PublishInspectionWorkItemCommand,
    build_inspection_work_item_input_hash,
    validate_inspection_work_item_publish_command,
)
from services.api.app.application.outbox import INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE
from services.api.app.domain.inspection import (
    H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
    H5_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
    build_h5_inspection_publication_id,
)
from services.api.app.domain.protocol import build_protocol_envelope, stable_protocol_hash
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    assert_finalization_recovery_fence,
)
from services.api.app.infrastructure.db.h5_completion_gate_sql import (
    H5_ALREADY_INSPECTED_LOGICAL_STATE_SQL,
)
from services.api.app.infrastructure.db.h5_consumer_run_lifecycle import (
    finish_h5_consumer_run_if_idle_on_connection,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
    publish_inspection_work_item_on_connection,
)
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.task_rule_inventory_sql import frozen_h5_rule_identity_sql

_TERMINAL_STATUSES = frozenset(
    {"published", "not_applicable", "needs_review", "failed", "cancelled", "superseded"}
)


class WebActiveSessionWorkPendingError(RuntimeError):
    """Publication 先于 Web live Work 到达时保持可重试。"""

    preserve_attempt_budget = True


class H5InspectionConsumerRunIdentityError(RuntimeError):
    """当前任务的逻辑消费 Run 不能在既有身份围栏内继续。"""


class PostgresH5InspectionPublicationRepository:
    """以 Publication 行作为证据到质检工作的可靠发布待办。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        max_attempts: int = 3,
        retry_delay_seconds: int = 5,
    ) -> None:
        if max_attempts <= 0 or retry_delay_seconds < 0:
            raise ValueError("Publication 重试配置无效")
        self._database_url = database_url
        self._pool = pool
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds

    async def count_unresolved_active_session_rules_for_capture(
        self,
        *,
        task_id: str,
        source_agent_run_id: str,
        capture_id: str,
        excluded_rules: tuple[dict[str, Any], ...],
    ) -> int:
        """核对 off 旧链 live Work 终态，只返回仍未可信完成的规则数量。"""

        if not excluded_rules:
            return 0
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT DISTINCT
                    member.rule_id::text AS rule_id,
                    member.rule_code,
                    member.contract_hash
                FROM qc_task AS task
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.task_id = task.id
                   AND work_item.page_capture_id = $3::uuid
                   AND work_item.producer_agent_run_id = $2::uuid
                   AND work_item.session_owner_agent_run_id = $2::uuid
                   AND work_item.ruleset_id = task.ruleset_id
                   AND work_item.finalization_epoch = task.finalization_epoch
                INNER JOIN qc_inspection_work_item_rule AS member
                    ON member.work_item_id = work_item.id
                WHERE task.id = $1::uuid
                  AND task.target_platform IN ('h5', 'web')
                  AND task.status = 'running'
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND COALESCE(
                      task.task_config->>'execution_mode', 'full_inspection'
                  ) = 'full_inspection'
                  AND COALESCE(
                      task.task_config->>'h5_inspection_notification_mode', 'off'
                  ) = 'off'
                  AND work_item.session_dependency = 'active_session'
                  AND work_item.control_mode = 'enforced'
                  AND work_item.status IN ('succeeded', 'partial')
                  AND member.status IN (
                      'written', 'evidence_insufficient', 'error', 'skipped'
                  )
                """,
                task_id,
                source_agent_run_id,
                capture_id,
            )
        resolved = {
            (
                str(row["rule_id"]),
                str(row["rule_code"]),
                str(row["contract_hash"]),
            )
            for row in rows
        }
        return sum(
            (
                str(rule.get("rule_id") or ""),
                str(rule.get("rule_code") or ""),
                str(rule.get("contract_hash") or ""),
            )
            not in resolved
            for rule in excluded_rules
        )

    async def create_for_capture(
        self,
        *,
        task_id: str,
        source_agent_run_id: str,
        capture_id: str,
        page_type_assessment_id: str,
        assessment_hash: str,
        page_type: str,
        page_state: str,
        strict_web_gate: bool = False,
        strict_web_final_handoff_epoch: int | None = None,
        logical_page_id: str | None = None,
        content_fingerprint: str | None = None,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> H5InspectionPublicationWriteResult:
        """在同一事务创建 Publication 与首个可用事件。"""

        if strict_web_final_handoff_epoch is not None and (
            not strict_web_gate
            or isinstance(strict_web_final_handoff_epoch, bool)
            or not isinstance(strict_web_final_handoff_epoch, int)
            or strict_web_final_handoff_epoch < 0
        ):
            raise ValueError(
                "strict_web_final_handoff_epoch 仅允许严格 Web 门禁使用非负整数"
            )

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
            if strict_web_gate:
                await self._validate_strict_web_incremental_gate(
                    connection,
                    task_id=task_id,
                    source_agent_run_id=source_agent_run_id,
                    capture_id=capture_id,
                    page_type_assessment_id=page_type_assessment_id,
                    final_handoff_epoch=strict_web_final_handoff_epoch,
                )
            source = await connection.fetchrow(
                """
                SELECT
                    task.id::text AS task_id,
                    task.finalization_epoch,
                    task.task_config,
                    ruleset.id::text AS ruleset_id,
                    ruleset.version AS ruleset_version,
                    ruleset.snapshot_hash AS ruleset_snapshot_hash,
                    capture.id::text AS page_capture_id,
                    capture.page_type,
                    COALESCE(capture.page_state, 'stable') AS page_state,
                    assessment.assessment_record_id,
                    assessment.payload_hash AS assessment_hash,
                    COALESCE(
                        capture.metadata->>'logical_page_id',
                        common_capture.metadata->>'logical_page_id'
                    ) AS logical_page_id,
                    (
                        SELECT md5(string_agg(
                            artifact.artifact_type || ':' || artifact.checksum,
                            '|' ORDER BY artifact.artifact_type, artifact.checksum
                        ))
                        FROM qc_evidence_artifact AS artifact
                        WHERE artifact.task_id = task.id
                          AND artifact.page_capture_id = capture.id
                          AND artifact.artifact_type IN ('dom', 'visible_text')
                          AND artifact.lifecycle_status IN ('active', 'archived')
                    ) AS content_fingerprint
                FROM qc_task AS task
                INNER JOIN qc_ruleset AS ruleset ON ruleset.id = task.ruleset_id
                INNER JOIN qc_agent_run AS source_run
                    ON source_run.id = $2::uuid AND source_run.task_id = task.id
                INNER JOIN qc_page_capture AS capture
                    ON capture.id = $3::uuid AND capture.task_id = task.id
                INNER JOIN qc_capture AS common_capture
                    ON common_capture.id = capture.id
                   AND common_capture.agent_run_id = source_run.id
                   AND common_capture.capture_role = 'after_action'
                   AND common_capture.capture_status = 'captured'
                INNER JOIN qc_h5_evidence_inventory_item AS inventory
                    ON inventory.task_id = task.id
                   AND inventory.agent_run_id = source_run.id
                   AND inventory.after_capture_id = capture.id
                   AND inventory.status = 'pass'
                   AND inventory.page_type = capture.page_type
                INNER JOIN qc_action_execution AS action
                    ON action.task_id = task.id
                   AND action.session_id = inventory.session_id
                   AND action.action_execution_id = inventory.action_execution_id
                INNER JOIN qc_surface_session AS surface_session
                    ON surface_session.task_id = task.id
                   AND surface_session.session_id = action.session_id
                   AND surface_session.surface_kind = 'web'
                   AND action.finalization_epoch = surface_session.finalization_epoch
                   AND surface_session.finalization_epoch = task.finalization_epoch
                INNER JOIN qc_h5_page_type_assessment AS assessment
                    ON assessment.assessment_record_id = $4
                   AND assessment.task_id = task.id
                   AND assessment.agent_run_id = source_run.id
                   AND assessment.page_capture_id = capture.id
                WHERE task.id = $1::uuid
                  AND task.target_platform IN ('h5', 'web')
                  AND task.status = 'running'
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND task.task_config->>'execution_mode' = 'full_inspection'
                  AND task.task_config->>'h5_inspection_notification_mode' = 'enforced'
                  AND source_run.status IN (
                      'pending', 'running', 'done', 'failed', 'aborted', 'canceled'
                  )
                  AND (
                        task.finalization_agent_run_id = source_run.id
                        OR (
                            task.finalization_agent_run_id IS NULL
                            AND NOT EXISTS (
                                SELECT 1
                                FROM qc_agent_run AS competing_run
                                WHERE competing_run.task_id = task.id
                                  AND competing_run.id <> source_run.id
                                  AND competing_run.status IN ('pending', 'running')
                                  AND NOT (
                                      competing_run.agent_type = 'rule_judge'
                                      AND competing_run.input_context->>'source'
                                          = 'h5_inspection_publication'
                                      AND competing_run.input_context->>'finalization_epoch'
                                          = task.finalization_epoch::text
                                  )
                            )
                        )
                  )
                  AND assessment.detection_status = 'recognized'
                  AND assessment.page_type = capture.page_type
                  AND (
                        lower(task.target_platform) <> 'h5'
                        OR COALESCE(
                            action.intent_payload->'parameters'
                                ->>'supplemental_evidence_source',
                            ''
                        ) <> 'targeted_recapture'
                      )
                  AND COALESCE(
                        capture.inspection_lifecycle_status,
                        common_capture.metadata->>'inspection_lifecycle_status',
                        ''
                      ) NOT IN ('superseded', 'abandoned')
                FOR UPDATE OF task, ruleset, capture, common_capture, assessment,
                    inventory, action, surface_session
                """,
                task_id,
                source_agent_run_id,
                capture_id,
                page_type_assessment_id,
            )
            if source is None:
                raise RuntimeError("H5 Publication 来源、任务快照或 enforced 模式不一致")
            if str(source["assessment_hash"]) != assessment_hash:
                raise RuntimeError("H5 Publication assessment_hash 发生漂移")
            if str(source["page_type"] or "") != page_type:
                raise RuntimeError("H5 Publication page_type 发生漂移")
            normalized_state = str(source["page_state"] or "stable")
            if normalized_state != page_state:
                raise RuntimeError("H5 Publication page_state 发生漂移")

            source_logical_page_id = _optional_text(source["logical_page_id"])
            source_content_fingerprint = _optional_text(source["content_fingerprint"])
            if strict_web_gate and (
                source_logical_page_id != _optional_text(logical_page_id)
                or source_content_fingerprint != _optional_text(content_fingerprint)
            ):
                raise RuntimeError("Web Publication 逻辑页面或内容指纹发生漂移")

            finalization_epoch = int(source["finalization_epoch"])
            ruleset_snapshot_hash = _optional_text(source["ruleset_snapshot_hash"])
            if (
                strict_web_gate
                and source_logical_page_id is not None
                and source_content_fingerprint is not None
            ):
                existing_logical = await connection.fetchrow(
                    """
                    SELECT publication.id::text AS publication_id,
                           publication.row_version
                    FROM qc_h5_inspection_publication AS publication
                    INNER JOIN qc_capture AS existing_capture
                      ON existing_capture.id = publication.page_capture_id
                    INNER JOIN qc_page_capture AS existing_page
                      ON existing_page.id = publication.page_capture_id
                     AND existing_page.task_id = publication.task_id
                    WHERE publication.task_id = $1::uuid
                      AND publication.source_agent_run_id = $2::uuid
                      AND publication.finalization_epoch = $3
                      AND publication.web_evidence_sequence_id IS NULL
                      AND publication.mode = 'enforced'
                      AND COALESCE(
                            existing_page.metadata->>'logical_page_id',
                            existing_capture.metadata->>'logical_page_id',
                            ''
                          ) = $4
                      AND (
                            SELECT md5(string_agg(
                                artifact.artifact_type || ':' || artifact.checksum,
                                '|' ORDER BY artifact.artifact_type, artifact.checksum
                            ))
                            FROM qc_evidence_artifact AS artifact
                            WHERE artifact.task_id = publication.task_id
                              AND artifact.page_capture_id = publication.page_capture_id
                              AND artifact.artifact_type IN ('dom', 'visible_text')
                              AND artifact.lifecycle_status IN ('active', 'archived')
                          ) = $5
                    ORDER BY publication.created_at, publication.id
                    LIMIT 1
                    FOR UPDATE OF publication
                    """,
                    task_id,
                    source_agent_run_id,
                    finalization_epoch,
                    source_logical_page_id,
                    source_content_fingerprint,
                )
                if existing_logical is not None:
                    existing_publication_id = str(existing_logical["publication_id"])
                    existing_row_version = int(existing_logical["row_version"])
                    existing_message_id = await self._insert_available_event(
                        connection,
                        publication_id=existing_publication_id,
                        task_id=task_id,
                        finalization_epoch=finalization_epoch,
                        row_version=existing_row_version,
                        occurred_at=datetime.now(UTC),
                    )
                    existing_outbox_status = await connection.fetchval(
                        "SELECT status FROM qc_outbox_event WHERE message_id = $1",
                        existing_message_id,
                    )
                    return H5InspectionPublicationWriteResult(
                        publication_id=existing_publication_id,
                        created=False,
                        row_version=existing_row_version,
                        outbox_message_id=existing_message_id,
                        outbox_status=_optional_text(existing_outbox_status),
                    )
            publication_id = build_h5_inspection_publication_id(
                task_id=task_id,
                finalization_epoch=finalization_epoch,
                page_capture_id=capture_id,
                assessment_id=page_type_assessment_id,
                ruleset_snapshot_hash=ruleset_snapshot_hash,
            )
            input_hash = stable_protocol_hash(
                {
                    "protocol_version": H5_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
                    "task_id": task_id,
                    "source_agent_run_id": source_agent_run_id,
                    "page_capture_id": capture_id,
                    "page_type_assessment_id": page_type_assessment_id,
                    "assessment_hash": assessment_hash,
                    "page_type": page_type,
                    "page_state": page_state,
                    "ruleset_id": str(source["ruleset_id"]),
                    "ruleset_version": int(source["ruleset_version"]),
                    "ruleset_snapshot_hash": ruleset_snapshot_hash,
                    "finalization_epoch": finalization_epoch,
                }
            )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_h5_inspection_publication AS existing (
                    id, task_id, source_agent_run_id, page_capture_id,
                    page_type_assessment_id, assessment_hash,
                    ruleset_id, ruleset_version, ruleset_snapshot_hash,
                    finalization_epoch, input_hash, mode, max_attempts
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                    $5, $6, $7::uuid, $8, $9, $10, $11, 'enforced', $12
                )
                ON CONFLICT ON CONSTRAINT uq_qc_h5_publication_capture_epoch
                DO UPDATE SET updated_at = existing.updated_at
                RETURNING
                    id::text AS publication_id,
                    input_hash,
                    row_version,
                    (xmax = 0) AS created
                """,
                publication_id,
                task_id,
                source_agent_run_id,
                capture_id,
                page_type_assessment_id,
                assessment_hash,
                str(source["ruleset_id"]),
                int(source["ruleset_version"]),
                ruleset_snapshot_hash,
                finalization_epoch,
                input_hash,
                self._max_attempts,
            )
            if row is None or str(row["input_hash"]) != input_hash:
                raise RuntimeError("H5 Publication 幂等身份已绑定不同输入")
            row_version = int(row["row_version"])
            outbox_message_id = await self._insert_available_event(
                connection,
                publication_id=publication_id,
                task_id=task_id,
                finalization_epoch=finalization_epoch,
                row_version=row_version,
                occurred_at=datetime.now(UTC),
            )
            outbox_status = None
            if strict_web_gate:
                outbox_status = await connection.fetchval(
                    """
                    SELECT status
                    FROM qc_outbox_event
                    WHERE message_id = $1
                    """,
                    outbox_message_id,
                )
                if _optional_text(outbox_status) is None:
                    raise RuntimeError("Web Publication Outbox 状态不可核验")
            return H5InspectionPublicationWriteResult(
                publication_id=publication_id,
                created=bool(row["created"]),
                row_version=row_version,
                outbox_message_id=outbox_message_id,
                outbox_status=_optional_text(outbox_status),
            )

    async def record_shadow_for_capture(
        self,
        *,
        task_id: str,
        source_agent_run_id: str,
        capture_id: str,
        page_type_assessment_id: str,
        assessment_hash: str,
        page_type: str,
        page_state: str,
        result: object,
    ) -> H5InspectionPublicationWriteResult:
        """冻结 shadow 候选规则计划，但不创建 Work、Outbox 或消费 Run。"""

        commands = tuple(
            command
            for command in (getattr(result, "prepared_commands", ()) or ())
            if isinstance(command, PublishInspectionWorkItemCommand)
        )
        excluded_rules = tuple(
            item
            for item in (getattr(result, "excluded_active_session_rules", ()) or ())
            if isinstance(item, dict)
        )
        excluded_count = int(getattr(result, "excluded_active_session_rule_count", 0) or 0)
        if excluded_count != len(excluded_rules):
            raise RuntimeError("H5 shadow active-session 排除计数与规则快照不一致")
        if int(getattr(result, "published_count", 0) or 0) != len(commands):
            raise RuntimeError("H5 shadow 候选批次计数与准备命令不一致")

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            source = await connection.fetchrow(
                """
                SELECT
                    task.finalization_epoch,
                    ruleset.id::text AS ruleset_id,
                    ruleset.version AS ruleset_version,
                    ruleset.snapshot_hash AS ruleset_snapshot_hash,
                    capture.page_type,
                    COALESCE(capture.page_state, 'stable') AS page_state,
                    assessment.payload_hash AS assessment_hash
                FROM qc_task AS task
                INNER JOIN qc_ruleset AS ruleset ON ruleset.id = task.ruleset_id
                INNER JOIN qc_agent_run AS source_run
                    ON source_run.id = $2::uuid AND source_run.task_id = task.id
                INNER JOIN qc_page_capture AS capture
                    ON capture.id = $3::uuid AND capture.task_id = task.id
                INNER JOIN qc_capture AS common_capture
                    ON common_capture.id = capture.id
                   AND common_capture.agent_run_id = source_run.id
                   AND common_capture.capture_role = 'after_action'
                   AND common_capture.capture_status = 'captured'
                INNER JOIN qc_h5_evidence_inventory_item AS inventory
                    ON inventory.task_id = task.id
                   AND inventory.agent_run_id = source_run.id
                   AND inventory.after_capture_id = capture.id
                   AND inventory.status = 'pass'
                   AND inventory.page_type = capture.page_type
                INNER JOIN qc_action_execution AS action
                    ON action.task_id = task.id
                   AND action.session_id = inventory.session_id
                   AND action.action_execution_id = inventory.action_execution_id
                INNER JOIN qc_surface_session AS surface_session
                    ON surface_session.task_id = task.id
                   AND surface_session.session_id = action.session_id
                   AND surface_session.surface_kind = 'web'
                   AND action.finalization_epoch = surface_session.finalization_epoch
                   AND surface_session.finalization_epoch = task.finalization_epoch
                INNER JOIN qc_h5_page_type_assessment AS assessment
                    ON assessment.assessment_record_id = $4
                   AND assessment.task_id = task.id
                   AND assessment.agent_run_id = source_run.id
                   AND assessment.page_capture_id = capture.id
                WHERE task.id = $1::uuid
                  AND task.target_platform IN ('h5', 'web')
                  AND task.status = 'running'
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND task.task_config->>'execution_mode' = 'full_inspection'
                  AND task.task_config->>'h5_inspection_notification_mode' = 'shadow'
                  AND source_run.status IN (
                      'pending', 'running', 'done', 'failed', 'aborted', 'canceled'
                  )
                  AND (
                        task.finalization_agent_run_id = source_run.id
                        OR (
                            task.finalization_agent_run_id IS NULL
                            AND NOT EXISTS (
                                SELECT 1
                                FROM qc_agent_run AS competing_run
                                WHERE competing_run.task_id = task.id
                                  AND competing_run.id <> source_run.id
                                  AND competing_run.status IN ('pending', 'running')
                                  AND NOT (
                                      competing_run.agent_type = 'rule_judge'
                                      AND competing_run.input_context->>'source'
                                          = 'h5_inspection_publication'
                                      AND competing_run.input_context->>'finalization_epoch'
                                          = task.finalization_epoch::text
                                  )
                            )
                        )
                  )
                  AND assessment.detection_status = 'recognized'
                  AND assessment.page_type = capture.page_type
                  AND COALESCE(
                        capture.inspection_lifecycle_status,
                        common_capture.metadata->>'inspection_lifecycle_status',
                        ''
                      ) NOT IN ('superseded', 'abandoned')
                FOR UPDATE OF task, ruleset, capture, common_capture, assessment,
                    inventory, action, surface_session
                """,
                task_id,
                source_agent_run_id,
                capture_id,
                page_type_assessment_id,
            )
            if source is None:
                raise RuntimeError("H5 shadow 来源、任务快照或当前 epoch 不一致")
            if str(source["assessment_hash"]) != assessment_hash:
                raise RuntimeError("H5 shadow assessment_hash 发生漂移")
            if str(source["page_type"] or "") != page_type:
                raise RuntimeError("H5 shadow page_type 发生漂移")
            if str(source["page_state"] or "stable") != page_state:
                raise RuntimeError("H5 shadow page_state 发生漂移")

            finalization_epoch = int(source["finalization_epoch"])
            ruleset_id = str(source["ruleset_id"])
            ruleset_version = int(source["ruleset_version"])
            ruleset_snapshot_hash = _optional_text(source["ruleset_snapshot_hash"])
            for command in commands:
                valid = (
                    command.task_id == task_id
                    and command.page_capture_id == capture_id
                    and command.producer_agent_run_id == source_agent_run_id
                    and command.ruleset_id == ruleset_id
                    and command.ruleset_version == ruleset_version
                    and command.ruleset_snapshot_hash == ruleset_snapshot_hash
                    and command.session_dependency == "snapshot"
                    and command.session_owner_agent_run_id is None
                    and command.control_mode == "enforced"
                    and command.defer_wakeup
                )
                if not valid:
                    raise RuntimeError("H5 shadow 候选批次与冻结身份不一致")
                validate_inspection_work_item_publish_command(command)

            publication_id = build_h5_inspection_publication_id(
                task_id=task_id,
                finalization_epoch=finalization_epoch,
                page_capture_id=capture_id,
                assessment_id=page_type_assessment_id,
                ruleset_snapshot_hash=ruleset_snapshot_hash,
            )
            input_hash = stable_protocol_hash(
                {
                    "protocol_version": H5_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
                    "mode": "shadow",
                    "task_id": task_id,
                    "source_agent_run_id": source_agent_run_id,
                    "page_capture_id": capture_id,
                    "page_type_assessment_id": page_type_assessment_id,
                    "assessment_hash": assessment_hash,
                    "page_type": page_type,
                    "page_state": page_state,
                    "ruleset_id": ruleset_id,
                    "ruleset_version": ruleset_version,
                    "ruleset_snapshot_hash": ruleset_snapshot_hash,
                    "finalization_epoch": finalization_epoch,
                    "candidate_batches": [
                        build_inspection_work_item_input_hash(command) for command in commands
                    ],
                    "excluded_rules": list(excluded_rules),
                }
            )
            candidate_rule_count = len(
                {rule.rule_id for command in commands for rule in command.rules}
                | {
                    str(rule.get("rule_id") or "")
                    for rule in excluded_rules
                    if str(rule.get("rule_id") or "")
                }
            )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_h5_inspection_publication AS existing (
                    id, task_id, source_agent_run_id, page_capture_id,
                    page_type_assessment_id, assessment_hash,
                    ruleset_id, ruleset_version, ruleset_snapshot_hash,
                    finalization_epoch, input_hash, mode, status,
                    candidate_rule_count, expected_batch_count,
                    published_work_item_count, excluded_active_session_rule_count,
                    max_attempts, finished_at
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                    $5, $6, $7::uuid, $8, $9,
                    $10, $11, 'shadow', 'not_applicable',
                    $12, $13, 0, $14, $15, now()
                )
                ON CONFLICT ON CONSTRAINT uq_qc_h5_publication_capture_epoch
                DO UPDATE SET updated_at = existing.updated_at
                RETURNING id::text AS publication_id, input_hash, mode, status,
                    (xmax = 0) AS created
                """,
                publication_id,
                task_id,
                source_agent_run_id,
                capture_id,
                page_type_assessment_id,
                assessment_hash,
                ruleset_id,
                ruleset_version,
                ruleset_snapshot_hash,
                finalization_epoch,
                input_hash,
                candidate_rule_count,
                len(commands),
                excluded_count,
                self._max_attempts,
            )
            if (
                row is None
                or str(row["input_hash"]) != input_hash
                or str(row["mode"]) != "shadow"
                or str(row["status"]) != "not_applicable"
            ):
                raise RuntimeError("H5 shadow Publication 幂等身份已绑定不同输入")
            if bool(row["created"]):
                ordinal = 0
                for command in commands:
                    for rule in command.rules:
                        await self._insert_shadow_rule(
                            connection,
                            publication_id=publication_id,
                            ruleset_id=ruleset_id,
                            rule_id=rule.rule_id,
                            rule_code=rule.rule_code,
                            ordinal=ordinal,
                            contract_hash=rule.contract_hash,
                            strategy=command.strategy,
                            category=command.category,
                            session_dependency="snapshot",
                            execution_plan={"batch_key": command.batch_key},
                        )
                        ordinal += 1
                for excluded in excluded_rules:
                    await self._insert_shadow_rule(
                        connection,
                        publication_id=publication_id,
                        ruleset_id=ruleset_id,
                        rule_id=str(excluded.get("rule_id") or ""),
                        rule_code=str(excluded.get("rule_code") or ""),
                        ordinal=ordinal,
                        contract_hash=str(excluded.get("contract_hash") or ""),
                        strategy=str(excluded.get("strategy") or ""),
                        category=_optional_text(excluded.get("category")),
                        session_dependency="active_session",
                        execution_plan=(
                            excluded.get("execution_plan")
                            if isinstance(excluded.get("execution_plan"), dict)
                            else {}
                        ),
                    )
                    ordinal += 1
            return H5InspectionPublicationWriteResult(
                publication_id=publication_id,
                created=bool(row["created"]),
            )

    @staticmethod
    async def _insert_shadow_rule(
        connection: Any,
        *,
        publication_id: str,
        ruleset_id: str,
        rule_id: str,
        rule_code: str,
        ordinal: int,
        contract_hash: str,
        strategy: str,
        category: str | None,
        session_dependency: str,
        execution_plan: dict[str, Any],
    ) -> None:
        inserted = await connection.fetchval(
            """
            INSERT INTO qc_h5_inspection_publication_rule (
                publication_id, rule_id, rule_code, ordinal, contract_hash,
                strategy, category, session_dependency, execution_plan
            )
            SELECT
                publication.id, rule.id, $10::varchar, $4, $5, $6, $7, $8,
                $9::jsonb
            FROM qc_h5_inspection_publication AS publication
            JOIN qc_task AS inventory_task ON inventory_task.id = publication.task_id
            INNER JOIN qc_rule AS rule
                ON rule.id = $2::uuid
            WHERE publication.id = $1::uuid
              AND publication.ruleset_id = $3::uuid
              AND __H5_RULE_IDENTITY__
            RETURNING rule_id::text
            """.replace("__H5_RULE_IDENTITY__", frozen_h5_rule_identity_sql(
                task="inventory_task", rule="rule", code="$10::varchar", ruleset="$3::uuid",
            )),
            publication_id,
            rule_id,
            ruleset_id,
            ordinal,
            contract_hash,
            strategy,
            category,
            session_dependency,
            json.dumps(execution_plan, ensure_ascii=False),
            rule_code,
        )
        if inserted is None:
            raise RuntimeError("H5 shadow 候选规则不属于锁定规则集")

    async def claim(
        self,
        *,
        publication_id: str,
        task_id: str,
        message_version: int,
        lease_owner: str,
        lease_seconds: int,
    ) -> H5InspectionPublicationClaimResult:
        """按消息版本领取 Publication；旧消息与终态均可安全 ACK。"""

        lease_token = str(uuid4())
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            row = await connection.fetchrow(
                """
                SELECT
                    publication.id::text AS publication_id,
                    publication.task_id::text AS task_id,
                    publication.source_agent_run_id::text AS source_agent_run_id,
                    publication.consumer_agent_run_id::text AS consumer_agent_run_id,
                    publication.page_capture_id::text AS page_capture_id,
                    publication.page_type_assessment_id,
                    publication.assessment_hash,
                    publication.ruleset_id::text AS publication_ruleset_id,
                    publication.ruleset_version AS publication_ruleset_version,
                    publication.ruleset_snapshot_hash AS publication_ruleset_snapshot_hash,
                    publication.status,
                    publication.attempt_count,
                    publication.max_attempts,
                    publication.row_version,
                    publication.next_attempt_at,
                    publication.lease_expires_at,
                    publication.finalization_epoch AS publication_finalization_epoch,
                    task.status AS task_status,
                    task.finalization_state AS task_finalization_state,
                    task.finalization_epoch AS task_finalization_epoch,
                    task.finalization_agent_run_id::text AS task_finalization_agent_run_id,
                    source_run.status AS source_run_status,
                    (
                        source_run.status IN (
                            'pending', 'running', 'done', 'failed', 'aborted', 'canceled'
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_agent_run AS competing_run
                            WHERE competing_run.task_id = task.id
                              AND competing_run.id <> source_run.id
                              AND competing_run.status IN ('pending', 'running')
                              AND NOT (
                                  competing_run.agent_type = 'rule_judge'
                                  AND competing_run.input_context->>'source'
                                      = 'h5_inspection_publication'
                                  AND competing_run.input_context->>'finalization_epoch'
                                      = task.finalization_epoch::text
                              )
                        )
                    ) AS source_run_is_unique_active,
                    task.target_platform,
                    task.task_config,
                    task.ruleset_id::text AS task_ruleset_id,
                    ruleset.version AS current_ruleset_version,
                    ruleset.snapshot_hash AS current_ruleset_snapshot_hash,
                    capture.page_type,
                    COALESCE(capture.page_state, 'stable') AS page_state,
                    capture.inspection_lifecycle_status,
                    common_capture.metadata AS capture_metadata,
                    common_capture.capture_role,
                    common_capture.capture_status,
                    common_capture.agent_run_id::text AS capture_agent_run_id,
                    assessment.assessment_record_id,
                    assessment.payload_hash AS current_assessment_hash,
                    assessment.detection_status,
                    assessment.page_type AS assessment_page_type,
                    inventory.inventory_item_id,
                    action.finalization_epoch AS action_finalization_epoch,
                    surface_session.finalization_epoch AS session_finalization_epoch
                FROM qc_h5_inspection_publication AS publication
                INNER JOIN qc_task AS task
                    ON task.id = publication.task_id
                INNER JOIN qc_agent_run AS source_run
                    ON source_run.id = publication.source_agent_run_id
                   AND source_run.task_id = task.id
                INNER JOIN qc_ruleset AS ruleset
                    ON ruleset.id = publication.ruleset_id
                INNER JOIN qc_page_capture AS capture
                    ON capture.id = publication.page_capture_id
                INNER JOIN qc_capture AS common_capture
                    ON common_capture.id = publication.page_capture_id
                INNER JOIN qc_h5_page_type_assessment AS assessment
                    ON assessment.assessment_record_id = publication.page_type_assessment_id
                LEFT JOIN qc_h5_evidence_inventory_item AS inventory
                    ON inventory.task_id = task.id
                   AND inventory.agent_run_id = publication.source_agent_run_id
                   AND inventory.after_capture_id = publication.page_capture_id
                   AND inventory.status = 'pass'
                LEFT JOIN qc_action_execution AS action
                    ON action.task_id = task.id
                   AND action.session_id = inventory.session_id
                   AND action.action_execution_id = inventory.action_execution_id
                LEFT JOIN qc_surface_session AS surface_session
                    ON surface_session.task_id = task.id
                   AND surface_session.session_id = action.session_id
                   AND surface_session.surface_kind = 'web'
                WHERE publication.id = $1::uuid
                  AND publication.task_id = $2::uuid
                FOR UPDATE OF publication, task, ruleset, capture, common_capture, assessment
                """,
                publication_id,
                task_id,
            )
            if row is None:
                from services.api.app.infrastructure.db.web_sequence_publication_claim import (
                    claim_web_sequence_on_connection,
                )

                sequence_claim = await claim_web_sequence_on_connection(self, connection,
                    publication_id=publication_id, task_id=task_id, message_version=message_version,
                    lease_owner=lease_owner, lease_seconds=lease_seconds)
                if sequence_claim is not None:
                    return sequence_claim
                return H5InspectionPublicationClaimResult(
                    disposition=H5InspectionPublicationClaimDisposition.STALE
                )
            status = str(row["status"])
            current_version = int(row["row_version"])
            if status in _TERMINAL_STATUSES:
                return H5InspectionPublicationClaimResult(
                    disposition=H5InspectionPublicationClaimDisposition.COMPLETED
                )
            if current_version != message_version:
                return H5InspectionPublicationClaimResult(
                    disposition=H5InspectionPublicationClaimDisposition.STALE
                )
            task_active = (
                str(row["task_status"]) == "running"
                and str(row["task_finalization_state"])
                in {"open", "exploration_draining", "session_draining"}
                and int(row["publication_finalization_epoch"])
                == int(row["task_finalization_epoch"])
            )
            if not task_active:
                await connection.execute(
                    """
                    UPDATE qc_h5_inspection_publication
                    SET status = 'superseded',
                        failure_code = 'task_not_active',
                        failure_message = '任务已离开可发布状态或收口 epoch 已变化',
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL,
                        row_version = row_version + 1,
                        finished_at = now(), updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    publication_id,
                )
                if row["consumer_agent_run_id"] is not None:
                    await self._finish_consumer_run_if_idle(
                        connection,
                        task_id=task_id,
                        consumer_agent_run_id=str(row["consumer_agent_run_id"]),
                    )
                return H5InspectionPublicationClaimResult(
                    disposition=H5InspectionPublicationClaimDisposition.COMPLETED
                )
            now = datetime.now(UTC)
            # 重复领取不得用后续校验覆盖仍在执行的有效租约。
            if status == "leased" and row["lease_expires_at"] > now:
                return H5InspectionPublicationClaimResult(
                    disposition=H5InspectionPublicationClaimDisposition.BUSY
                )
            task_config = _json_object(row["task_config"])
            capture_metadata = _json_object(row["capture_metadata"])
            lifecycle_status = str(
                row["inspection_lifecycle_status"]
                or capture_metadata.get("inspection_lifecycle_status")
                or ""
            )
            source_run_status = str(row["source_run_status"])
            source_run_active = source_run_status in {"pending", "running"}
            source_run_terminal = source_run_status in {
                "done",
                "failed",
                "aborted",
                "canceled",
            }
            frozen_identity_valid = (
                str(row["target_platform"]).casefold() in {"h5", "web"}
                and str(task_config.get("execution_mode") or "") == "full_inspection"
                and str(task_config.get("h5_inspection_notification_mode") or "") == "enforced"
                and str(row["task_ruleset_id"]) == str(row["publication_ruleset_id"])
                and int(row["current_ruleset_version"]) == int(row["publication_ruleset_version"])
                and _optional_text(row["current_ruleset_snapshot_hash"])
                == _optional_text(row["publication_ruleset_snapshot_hash"])
                and str(row["capture_agent_run_id"]) == str(row["source_agent_run_id"])
                and (source_run_active or source_run_terminal)
                and (
                    str(row["task_finalization_agent_run_id"])
                    == str(row["source_agent_run_id"])
                    or (
                        row["task_finalization_agent_run_id"] is None
                        and (
                            bool(row["source_run_is_unique_active"])
                            or source_run_terminal
                        )
                    )
                )
                and row["inventory_item_id"] is not None
                and row["action_finalization_epoch"] is not None
                and row["session_finalization_epoch"] is not None
                and int(row["action_finalization_epoch"]) == int(row["task_finalization_epoch"])
                and int(row["session_finalization_epoch"]) == int(row["task_finalization_epoch"])
                and str(row["capture_role"]) == "after_action"
                and str(row["capture_status"]) == "captured"
                and lifecycle_status not in {"superseded", "abandoned"}
                and str(row["assessment_record_id"]) == str(row["page_type_assessment_id"])
                and str(row["current_assessment_hash"]) == str(row["assessment_hash"])
                and str(row["detection_status"]) == "recognized"
                and str(row["assessment_page_type"]) == str(row["page_type"])
            )
            screenshot_checksums = None
            primary_screenshot_checksums = None
            if str(row["target_platform"]).casefold() == "h5" and (
                str(row["page_type"]).casefold() == "video_detail"
                or "h5_video_publication" in _json_object(capture_metadata.get("pipeline_metrics"))
            ):
                frames = await connection.fetch(
                    "SELECT checksum,metadata->>'purpose' AS purpose FROM qc_evidence_artifact "
                    "WHERE task_id=$1::uuid AND page_capture_id=$2::uuid "
                    "AND artifact_type='screenshot' AND lifecycle_status IN ('active','archived')",
                    task_id, str(row["page_capture_id"]),
                )
                screenshot_checksums = [item["checksum"] for item in frames]
                primary_screenshot_checksums = [item["checksum"] for item in frames
                                               if item["purpose"] == "h5_video_primary"]
            readiness_valid = (str(row["target_platform"]).casefold() != "h5"
                or h5_capture_is_inspection_ready(
                    capture_metadata, page_state=row["page_state"], page_type=row["page_type"],
                    screenshot_checksums=screenshot_checksums,
                    primary_screenshot_checksums=primary_screenshot_checksums,
                ))
            if not frozen_identity_valid or not readiness_valid:
                await connection.execute(
                    """
                    UPDATE qc_h5_inspection_publication
                    SET status = 'needs_review',
                        failure_code = $2,
                        failure_message = $3,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL,
                        row_version = row_version + 1,
                        finished_at = now(), updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    publication_id,
                    "frozen_identity_drift" if not frozen_identity_valid else "capture_not_inspection_ready",
                    ("任务、证据、Assessment 或规则集冻结身份发生漂移" if not frozen_identity_valid
                     else "H5 本帧正文未证明就绪，保留诊断证据，不进入正式规则质检"),
                )
                if row["consumer_agent_run_id"] is not None:
                    await self._finish_consumer_run_if_idle(
                        connection,
                        task_id=task_id,
                        consumer_agent_run_id=str(row["consumer_agent_run_id"]),
                    )
                return H5InspectionPublicationClaimResult(
                    disposition=H5InspectionPublicationClaimDisposition.COMPLETED
                )
            now = datetime.now(UTC)
            if status == "retry_wait" and row["next_attempt_at"] > now:
                return H5InspectionPublicationClaimResult(
                    disposition=H5InspectionPublicationClaimDisposition.BUSY
                )
            if int(row["attempt_count"]) >= int(row["max_attempts"]):
                await connection.execute(
                    """
                    UPDATE qc_h5_inspection_publication
                    SET status = 'failed', failure_code = 'attempts_exhausted',
                        failure_message = 'Publication 重试次数已耗尽',
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                        row_version = row_version + 1, finished_at = now(), updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    publication_id,
                )
                if row["consumer_agent_run_id"] is not None:
                    await self._finish_consumer_run_if_idle(
                        connection,
                        task_id=task_id,
                        consumer_agent_run_id=str(row["consumer_agent_run_id"]),
                    )
                return H5InspectionPublicationClaimResult(
                    disposition=H5InspectionPublicationClaimDisposition.COMPLETED
                )
            try:
                consumer_agent_run_id = await self._ensure_consumer_run(
                    connection,
                    task_id=task_id,
                    finalization_epoch=int(row["task_finalization_epoch"]),
                )
            except H5InspectionConsumerRunIdentityError as exc:
                if str(row["target_platform"]) != "h5":
                    raise
                await connection.execute(
                    """
                    UPDATE qc_h5_inspection_publication
                    SET status = 'needs_review',
                        failure_code = 'consumer_run_unavailable',
                        failure_message = $2,
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                        row_version = row_version + 1, finished_at = now(), updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    publication_id,
                    str(exc),
                )
                return H5InspectionPublicationClaimResult(
                    disposition=H5InspectionPublicationClaimDisposition.COMPLETED
                )
            claimed = await connection.fetchrow(
                """
                UPDATE qc_h5_inspection_publication
                SET status = 'leased', lease_owner = $2, lease_token = $3::uuid,
                    lease_expires_at = now() + ($4 * interval '1 second'),
                    consumer_agent_run_id = $6::uuid,
                    attempt_count = attempt_count + 1, updated_at = now()
                WHERE id = $1::uuid AND row_version = $5
                RETURNING attempt_count, max_attempts, row_version
                """,
                publication_id,
                lease_owner,
                lease_token,
                lease_seconds,
                current_version,
                consumer_agent_run_id,
            )
            if claimed is None:
                return H5InspectionPublicationClaimResult(
                    disposition=H5InspectionPublicationClaimDisposition.BUSY
                )
            return H5InspectionPublicationClaimResult(
                disposition=H5InspectionPublicationClaimDisposition.CLAIMED,
                lease=H5InspectionPublicationLease(
                    publication_id=publication_id,
                    task_id=task_id,
                    source_agent_run_id=str(row["source_agent_run_id"]),
                    consumer_agent_run_id=consumer_agent_run_id,
                    page_capture_id=str(row["page_capture_id"]),
                    page_type_assessment_id=str(row["page_type_assessment_id"]),
                    assessment_hash=str(row["assessment_hash"]),
                    ruleset_id=str(row["publication_ruleset_id"]),
                    ruleset_version=int(row["publication_ruleset_version"]),
                    ruleset_snapshot_hash=_optional_text(row["publication_ruleset_snapshot_hash"]),
                    finalization_epoch=int(row["publication_finalization_epoch"]),
                    page_type=str(row["page_type"]),
                    page_state=str(row["page_state"]),
                    lease_token=lease_token,
                    row_version=int(claimed["row_version"]),
                    attempt_count=int(claimed["attempt_count"]),
                    max_attempts=int(claimed["max_attempts"]),
                ),
            )

    async def _ensure_consumer_run(
        self,
        connection: Any,
        *,
        task_id: str,
        finalization_epoch: int,
    ) -> str:
        """幂等创建当前任务 epoch 的持久化逻辑 rule_judge Run。"""

        logical_identity = f"h5-inspection-consumer:{task_id}:{finalization_epoch}"
        consumer_agent_run_id = str(uuid5(NAMESPACE_URL, f"alioth:{logical_identity}"))
        value = await connection.fetchval(
            """
            INSERT INTO qc_agent_run AS existing (
                id, task_id, agent_type, agent_name, status,
                input_context, loop_guard, started_at
            )
            SELECT
                $1::uuid, task.id, 'rule_judge', 'H5 Inspection Consumer', 'running',
                jsonb_build_object(
                    'logical_identity', $3::text,
                    'finalization_epoch', $4::integer,
                    'source', 'h5_inspection_publication'
                ),
                '{}'::jsonb,
                now()
            FROM qc_task AS task
            WHERE task.id = $2::uuid
              AND task.status = 'running'
              AND task.finalization_epoch = $4::integer
              AND task.finalization_state <> 'sealed'
            ON CONFLICT (id) DO UPDATE
            SET status = 'running',
                finished_at = CASE WHEN existing.status = 'done' THEN NULL
                                   ELSE existing.finished_at END,
                stop_reason = CASE WHEN existing.status = 'done' THEN NULL
                                   ELSE existing.stop_reason END,
                updated_at = CASE WHEN existing.status = 'done' THEN now()
                                  ELSE existing.updated_at END
            WHERE existing.task_id = EXCLUDED.task_id
              AND existing.agent_type = 'rule_judge'
              AND existing.input_context->>'source' = 'h5_inspection_publication'
              AND existing.input_context->>'finalization_epoch' = ($4::integer)::text
              AND (
                  existing.status = 'running'
                  OR (
                      existing.status = 'done'
                      AND existing.stop_reason = 'h5_inspection_work_drained'
                      AND EXISTS (
                          SELECT 1 FROM qc_task AS owner_task
                          WHERE owner_task.id = existing.task_id
                            AND owner_task.target_platform = 'h5'
                            AND owner_task.finalization_state <> 'sealed'
                      )
                  )
              )
              AND existing.input_context->>'logical_identity' = $3::text
            RETURNING id::text
            """,
            consumer_agent_run_id,
            task_id,
            logical_identity,
            finalization_epoch,
        )
        if value is None:
            raise H5InspectionConsumerRunIdentityError("H5 Publication 逻辑消费 Run 身份冲突")
        return str(value)

    async def process_web_sequence(self, *, lease: H5InspectionPublicationLease):
        """已认证序列使用原队列，专属封存逻辑不改变 stable complete。"""
        from services.api.app.infrastructure.db.web_sequence_publication_processor import (
            process_web_sequence_publication,
        )

        return await process_web_sequence_publication(self, lease=lease)

    async def complete(
        self,
        *,
        lease: H5InspectionPublicationLease,
        result: object,
    ) -> None:
        """冻结实际规则/批次关联，并以租约围栏写入 Publication 终态。"""

        status_text = str(getattr(result, "status", ""))
        declared_excluded_active_session_rule_count = int(
            getattr(result, "excluded_active_session_rule_count", 0) or 0
        )
        commands = tuple(
            command
            for command in (getattr(result, "prepared_commands", ()) or ())
            if isinstance(command, PublishInspectionWorkItemCommand)
        )
        excluded_rules = tuple(
            item
            for item in (getattr(result, "excluded_active_session_rules", ()) or ())
            if isinstance(item, dict)
        )
        if declared_excluded_active_session_rule_count != len(excluded_rules):
            raise RuntimeError("Publication active-session 排除计数与规则快照不一致")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{lease.task_id}",
            )
            identity = await self._validate_completion_identity(connection, lease=lease)
            if identity is None:
                await self._finish_consumer_run_if_idle(
                    connection,
                    task_id=lease.task_id,
                    consumer_agent_run_id=lease.consumer_agent_run_id,
                )
                return
            linked_active_session_work_item_ids = (
                await self._link_active_session_work_items(
                    connection,
                    lease=lease,
                    excluded_rules=excluded_rules,
                )
            )
            if self._is_web_v2_full_inspection(identity):
                unresolved_active_session_rules = (
                    await self._missing_active_session_work_rules(
                        connection,
                        lease=lease,
                        excluded_rules=excluded_rules,
                    )
                )
                if (
                    unresolved_active_session_rules
                    and str(identity["session_status"]) == "active"
                ):
                    raise WebActiveSessionWorkPendingError(
                        "Web active-session Work 尚未创建，等待 capture.committed 消费"
                    )
            else:
                unresolved_active_session_rules = (
                    await self._unresolved_active_session_rules(
                        connection,
                        lease=lease,
                        excluded_rules=excluded_rules,
                    )
                )
            excluded_active_session_rule_count = len(unresolved_active_session_rules)
            resolved_active_session_rule_count = (
                len(excluded_rules) - excluded_active_session_rule_count
            )
            terminal_status = {
                "published": "published",
                "no_rules_applicable": "not_applicable",
                "active_session_rules_excluded": (
                    "published"
                    if resolved_active_session_rule_count > 0
                    else "needs_review"
                ),
                "unmatched_page_type": "needs_review",
            }.get(status_text)
            if terminal_status is None:
                raise RuntimeError(f"H5 Publication 不支持的发布结果: {status_text}")
            if (
                terminal_status == "published"
                and not commands
                and resolved_active_session_rule_count == 0
            ):
                raise RuntimeError("H5 Publication 发布结果缺少单事务或活会话 Work")
            work_item_rows: list[Any] = []
            for command in commands:
                self._validate_prepared_command(
                    lease=lease,
                    command=command,
                    identity=identity,
                )
                validate_inspection_work_item_publish_command(command)
                for rule in command.rules:
                    locked_rule_id = await connection.fetchval(
                        """
                        SELECT id::text
                        FROM qc_rule
                        WHERE id = $1::uuid
                          AND ruleset_id = ANY(qc_ruleset_scope($2::uuid))
                          AND rule_code = $3
                        FOR UPDATE
                        """,
                        rule.rule_id,
                        command.ruleset_id,
                        rule.rule_code,
                    )
                    if locked_rule_id is None:
                        raise RuntimeError("Publication snapshot 规则身份发生漂移")
                published = await publish_inspection_work_item_on_connection(
                    connection,
                    command=command,
                    input_hash=build_inspection_work_item_input_hash(command),
                )
                work_item_id = published.work_item_id
                batch_key = command.batch_key
                linked = await connection.fetchval(
                    """
                    INSERT INTO qc_h5_inspection_publication_work_item (
                        publication_id, work_item_id, batch_key
                    )
                    SELECT publication.id, work_item.id, $3
                    FROM qc_h5_inspection_publication AS publication
                    INNER JOIN qc_inspection_work_item AS work_item
                        ON work_item.id = $2::uuid
                       AND work_item.task_id = publication.task_id
                       AND work_item.page_capture_id = publication.page_capture_id
                       AND work_item.finalization_epoch = publication.finalization_epoch
                       AND work_item.ruleset_id = publication.ruleset_id
                       AND work_item.ruleset_version = publication.ruleset_version
                       AND work_item.ruleset_snapshot_hash
                           IS NOT DISTINCT FROM publication.ruleset_snapshot_hash
                    WHERE publication.id = $1::uuid
                    ON CONFLICT (publication_id, work_item_id) DO NOTHING
                    RETURNING work_item_id::text
                    """,
                    lease.publication_id,
                    work_item_id,
                    batch_key,
                )
                if linked is None:
                    replay = await connection.fetchval(
                        """
                        SELECT work_item_id::text
                        FROM qc_h5_inspection_publication_work_item
                        WHERE publication_id = $1::uuid
                          AND work_item_id = $2::uuid
                          AND batch_key = $3
                        """,
                        lease.publication_id,
                        work_item_id,
                        batch_key,
                    )
                    if replay is None:
                        raise RuntimeError("Publication 工作项关联漂移")
                work_item = await connection.fetchrow(
                    """
                    SELECT
                        id::text AS work_item_id,
                        task_id::text AS task_id,
                        page_capture_id::text AS page_capture_id,
                        protocol_version,
                        batch_key,
                        strategy,
                        category,
                        session_dependency,
                        control_mode,
                        input_hash
                    FROM qc_inspection_work_item
                    WHERE id = $1::uuid
                      AND task_id = $2::uuid
                      AND control_mode = 'enforced'
                    """,
                    work_item_id,
                    lease.task_id,
                )
                if work_item is None:
                    raise RuntimeError("Publication 关联工作项权威快照不存在")
                work_item_rows.append(work_item)
            for work_item in work_item_rows:
                work_item_id = str(work_item["work_item_id"])
                input_hash = str(work_item["input_hash"])
                # 可用性公告的幂等键是 input_hash：活会话增量发布可能已用
                # aggregate_version=0 公告过同一工作项，这里必须保持同一事件
                # 身份做精确重放，否则 Outbox 判定同键漂移拒写（重开/重唤醒
                # 场景另有带版本的专用幂等键，不走此身份）。
                await insert_outbox_event(
                    connection,
                    aggregate_type="inspection_work_item",
                    aggregate_id=work_item_id,
                    envelope=build_protocol_envelope(
                        message_type=INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
                        payload_protocol_version=str(work_item["protocol_version"]),
                        idempotency_key=input_hash,
                        task_id=str(work_item["task_id"]),
                        aggregate_type="inspection_work_item",
                        aggregate_id=work_item_id,
                        # available 表示 Work 初始可领取事实；同一确定性 Work 可能已由
                        # 活会话增量链创建并完成，此时必须复核版本 0 的原始事件，
                        # 不能拿当前行版本制造第二个同幂等键事件。
                        aggregate_version=0,
                        occurred_at=datetime.now(UTC),
                        payload={
                            "protocol_version": str(work_item["protocol_version"]),
                            "work_item_id": work_item_id,
                            "task_id": str(work_item["task_id"]),
                            "page_capture_id": str(work_item["page_capture_id"]),
                            "batch_key": str(work_item["batch_key"]),
                            "strategy": str(work_item["strategy"]),
                            "category": work_item["category"],
                            "session_dependency": str(work_item["session_dependency"]),
                            "control_mode": str(work_item["control_mode"]),
                            "input_hash": input_hash,
                        },
                        correlation_id=str(work_item["task_id"]),
                    ),
                )
            await connection.execute(
                """
                INSERT INTO qc_h5_inspection_publication_rule (
                    publication_id, rule_id, rule_code, ordinal, contract_hash,
                    strategy, category, session_dependency, execution_plan
                )
                SELECT
                    link.publication_id,
                    member.rule_id,
                    member.rule_code,
                    row_number() OVER (
                        ORDER BY link.batch_key, member.ordinal, member.rule_code
                    ) - 1,
                    member.contract_hash,
                    work_item.strategy,
                    work_item.category,
                    work_item.session_dependency,
                    jsonb_build_object('batch_key', link.batch_key)
                FROM qc_h5_inspection_publication_work_item AS link
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = link.work_item_id
                INNER JOIN qc_inspection_work_item_rule AS member
                    ON member.work_item_id = work_item.id
                WHERE link.publication_id = $1::uuid
                ON CONFLICT (publication_id, rule_id) DO NOTHING
                """,
                lease.publication_id,
            )
            excluded_ordinal_base = int(
                await connection.fetchval(
                    """
                    SELECT COALESCE(max(ordinal) + 1, 0)
                    FROM qc_h5_inspection_publication_rule
                    WHERE publication_id = $1::uuid
                    """,
                    lease.publication_id,
                )
                or 0
            )
            for ordinal_offset, excluded in enumerate(unresolved_active_session_rules):
                locked_rule_id = await connection.fetchval(
                    """
                    SELECT rule.id::text
                    FROM qc_rule AS rule
                    JOIN qc_task AS inventory_task ON inventory_task.id = $4::uuid
                    WHERE rule.id = $1::uuid
                      AND inventory_task.ruleset_id = $2::uuid
                      AND __H5_RULE_IDENTITY__
                    FOR UPDATE OF rule
                    """.replace("__H5_RULE_IDENTITY__", frozen_h5_rule_identity_sql(
                        task="inventory_task", rule="rule", code="$3::varchar",
                        ruleset="$2::uuid",
                    )),
                    str(excluded.get("rule_id") or ""),
                    lease.ruleset_id,
                    str(excluded.get("rule_code") or ""),
                    lease.task_id,
                )
                if locked_rule_id is None:
                    raise RuntimeError("Publication active-session 规则身份发生漂移")
                inserted = await connection.fetchval(
                    """
                    INSERT INTO qc_h5_inspection_publication_rule (
                        publication_id, rule_id, rule_code, ordinal, contract_hash,
                        strategy, category, session_dependency, execution_plan
                    )
                    SELECT
                        publication.id,
                        rule.id,
                        $3::varchar,
                        $4,
                        $5,
                        $6,
                        $7,
                        'active_session',
                        $8::jsonb
                    FROM qc_h5_inspection_publication AS publication
                    JOIN qc_task AS inventory_task ON inventory_task.id = publication.task_id
                    INNER JOIN qc_rule AS rule
                        ON rule.id = $2::uuid
                    WHERE publication.id = $1::uuid
                      AND __H5_RULE_IDENTITY__
                    ON CONFLICT (publication_id, rule_id) DO UPDATE
                    SET ordinal = EXCLUDED.ordinal,
                        contract_hash = EXCLUDED.contract_hash,
                        strategy = EXCLUDED.strategy,
                        category = EXCLUDED.category,
                        session_dependency = EXCLUDED.session_dependency,
                        execution_plan = EXCLUDED.execution_plan
                    RETURNING rule_id::text
                    """.replace("__H5_RULE_IDENTITY__", frozen_h5_rule_identity_sql(
                        task="inventory_task", rule="rule", code="$3::varchar",
                        ruleset="publication.ruleset_id",
                    )),
                    lease.publication_id,
                    str(excluded.get("rule_id") or ""),
                    str(excluded.get("rule_code") or ""),
                    excluded_ordinal_base + ordinal_offset,
                    str(excluded.get("contract_hash") or ""),
                    str(excluded.get("strategy") or ""),
                    _optional_text(excluded.get("category")),
                    json.dumps(excluded.get("execution_plan") or {}, ensure_ascii=False),
                )
                if inserted is None:
                    raise RuntimeError("Publication 排除规则不属于锁定规则集")
            counts = await connection.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM qc_h5_inspection_publication_rule
                     WHERE publication_id = $1::uuid)::integer AS candidate_rule_count,
                    (SELECT count(*) FROM qc_h5_inspection_publication_work_item
                     WHERE publication_id = $1::uuid)::integer AS work_item_count
                """,
                lease.publication_id,
            )
            snapshot_published_count = int(getattr(result, "published_count", 0) or 0)
            expected_work_item_count = len(commands) + len(
                linked_active_session_work_item_ids
            )
            if (
                int(counts["work_item_count"]) != expected_work_item_count
                or snapshot_published_count != len(commands)
            ):
                raise RuntimeError("Publication 预期批次与实际工作项数量不一致")
            if int(counts["candidate_rule_count"]) < excluded_active_session_rule_count:
                raise RuntimeError("Publication 冻结规则成员数量不完整")
            published_count = expected_work_item_count
            updated = await connection.fetchval(
                """
                UPDATE qc_h5_inspection_publication
                SET status = $4,
                    candidate_rule_count = $5,
                    expected_batch_count = $6,
                    published_work_item_count = $7,
                    excluded_active_session_rule_count = $8,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    failure_code = NULL, failure_message = NULL,
                    row_version = row_version + 1,
                    finished_at = now(), updated_at = now()
                WHERE id = $1::uuid
                  AND lease_token = $2::uuid
                  AND row_version = $3
                  AND status = 'leased'
                RETURNING id::text
                """,
                lease.publication_id,
                lease.lease_token,
                lease.row_version,
                terminal_status,
                int(counts["candidate_rule_count"]),
                expected_work_item_count,
                published_count,
                excluded_active_session_rule_count,
            )
            if updated is None:
                raise RuntimeError("Publication 完成写回被租约围栏拒绝")
            inventory_updated = await connection.fetchval(
                """
                UPDATE qc_h5_evidence_inventory_item
                SET published_inspection_count = GREATEST(
                        published_inspection_count, $4::integer
                    ),
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND agent_run_id = $2::uuid
                  AND after_capture_id = $3::uuid
                  AND status = 'pass'
                RETURNING inventory_item_id
                """,
                lease.task_id,
                lease.source_agent_run_id,
                lease.page_capture_id,
                published_count,
            )
            if inventory_updated is None:
                raise RuntimeError("Publication 完成时 H5 Inventory 权威投影不存在")
            await self._annotate_capture_in_transaction(
                connection,
                lease=lease,
                result=result,
                published_count=published_count,
                excluded_active_session_rule_count=excluded_active_session_rule_count,
                business_status=(
                    "published" if terminal_status == "published" else status_text
                ),
            )
            await self._finish_consumer_run_if_idle(
                connection,
                task_id=lease.task_id,
                consumer_agent_run_id=lease.consumer_agent_run_id,
            )

    async def _validate_completion_identity(
        self,
        connection: Any,
        *,
        lease: H5InspectionPublicationLease,
    ) -> Any | None:
        """在创建全部 Work 前再次锁定并校验 Publication 冻结身份。"""

        row = await connection.fetchrow(
            """
            SELECT
                publication.ruleset_id::text AS ruleset_id,
                publication.ruleset_version,
                publication.ruleset_snapshot_hash,
                task.target_platform,
                task.task_config,
                surface_session.status AS session_status,
                capture.page_type,
                COALESCE(capture.page_state, 'stable') AS page_state
            FROM qc_h5_inspection_publication AS publication
            INNER JOIN qc_task AS task ON task.id = publication.task_id
            INNER JOIN qc_agent_run AS source_run
                ON source_run.id = publication.source_agent_run_id
               AND source_run.task_id = task.id
            INNER JOIN qc_ruleset AS ruleset ON ruleset.id = publication.ruleset_id
            INNER JOIN qc_page_capture AS capture
                ON capture.id = publication.page_capture_id
               AND capture.task_id = task.id
            INNER JOIN qc_capture AS common_capture
                ON common_capture.id = capture.id
               AND common_capture.agent_run_id = publication.source_agent_run_id
               AND common_capture.capture_role = 'after_action'
               AND common_capture.capture_status = 'captured'
            INNER JOIN qc_h5_page_type_assessment AS assessment
                ON assessment.assessment_record_id = publication.page_type_assessment_id
               AND assessment.task_id = task.id
               AND assessment.agent_run_id = publication.source_agent_run_id
               AND assessment.page_capture_id = capture.id
               AND assessment.payload_hash = publication.assessment_hash
               AND assessment.detection_status = 'recognized'
               AND assessment.page_type = capture.page_type
            INNER JOIN qc_h5_evidence_inventory_item AS inventory
                ON inventory.task_id = task.id
               AND inventory.agent_run_id = publication.source_agent_run_id
               AND inventory.after_capture_id = publication.page_capture_id
               AND inventory.status = 'pass'
            INNER JOIN qc_action_execution AS action
                ON action.task_id = task.id
               AND action.session_id = inventory.session_id
               AND action.action_execution_id = inventory.action_execution_id
            INNER JOIN qc_surface_session AS surface_session
                ON surface_session.task_id = task.id
               AND surface_session.session_id = action.session_id
               AND surface_session.surface_kind = 'web'
               AND action.finalization_epoch = surface_session.finalization_epoch
            WHERE publication.id = $1::uuid
              AND publication.task_id = $2::uuid
              AND publication.lease_token = $3::uuid
              AND publication.row_version = $4
              AND publication.status = 'leased'
              AND publication.finalization_epoch = $5
              AND task.status = 'running'
              AND task.finalization_state IN (
                  'open', 'exploration_draining', 'session_draining'
              )
              AND task.finalization_epoch = publication.finalization_epoch
              AND source_run.status IN (
                  'pending', 'running', 'done', 'failed', 'aborted', 'canceled'
              )
              AND (
                    task.finalization_agent_run_id = publication.source_agent_run_id
                    OR (
                        task.finalization_agent_run_id IS NULL
                        AND source_run.status IN (
                            'pending', 'running', 'done', 'failed', 'aborted', 'canceled'
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_agent_run AS competing_run
                            WHERE competing_run.task_id = task.id
                              AND competing_run.id <> source_run.id
                              AND competing_run.status IN ('pending', 'running')
                              AND NOT (
                                  competing_run.agent_type = 'rule_judge'
                                  AND competing_run.input_context->>'source'
                                      = 'h5_inspection_publication'
                                  AND competing_run.input_context->>'finalization_epoch'
                                      = task.finalization_epoch::text
                              )
                        )
                    )
              )
              AND surface_session.finalization_epoch = task.finalization_epoch
              AND task.target_platform IN ('h5', 'web')
              AND task.task_config->>'execution_mode' = 'full_inspection'
              AND task.task_config->>'h5_inspection_notification_mode' = 'enforced'
              AND task.ruleset_id = publication.ruleset_id
              AND ruleset.version = publication.ruleset_version
              AND ruleset.snapshot_hash
                    IS NOT DISTINCT FROM publication.ruleset_snapshot_hash
              AND COALESCE(
                    capture.inspection_lifecycle_status,
                    common_capture.metadata->>'inspection_lifecycle_status',
                    ''
                  ) NOT IN ('superseded', 'abandoned')
            FOR UPDATE OF publication, task, ruleset, capture, common_capture, assessment,
                inventory, action, surface_session
            """,
            lease.publication_id,
            lease.task_id,
            lease.lease_token,
            lease.row_version,
            lease.finalization_epoch,
        )
        if row is not None:
            return row
        await connection.execute(
            """
            UPDATE qc_h5_inspection_publication
            SET status = 'needs_review',
                failure_code = 'completion_identity_drift',
                failure_message = 'Publication 完成前冻结身份复核失败',
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                row_version = row_version + 1,
                finished_at = now(), updated_at = now()
            WHERE id = $1::uuid
              AND task_id = $2::uuid
              AND lease_token = $3::uuid
              AND row_version = $4
              AND status = 'leased'
            """,
            lease.publication_id,
            lease.task_id,
            lease.lease_token,
            lease.row_version,
        )
        return None

    @staticmethod
    def _validate_prepared_command(
        *,
        lease: H5InspectionPublicationLease,
        command: PublishInspectionWorkItemCommand,
        identity: Any,
    ) -> None:
        """禁止把其他任务、证据或规则集的 Work 关联到当前 Publication。"""

        valid = (
            command.task_id == lease.task_id
            and command.page_capture_id == lease.page_capture_id
            and command.producer_agent_run_id == lease.source_agent_run_id
            and command.session_dependency == "snapshot"
            and command.session_owner_agent_run_id is None
            and command.control_mode == "enforced"
            and command.defer_wakeup
            and command.ruleset_id == str(identity["ruleset_id"])
            and command.ruleset_version == int(identity["ruleset_version"])
            and command.ruleset_snapshot_hash == _optional_text(identity["ruleset_snapshot_hash"])
        )
        if not valid:
            raise RuntimeError("Publication 准备工作项与冻结身份不一致")

    @staticmethod
    async def _link_active_session_work_items(
        connection: Any,
        *,
        lease: H5InspectionPublicationLease,
        excluded_rules: tuple[dict[str, Any], ...],
    ) -> tuple[str, ...]:
        """把 live 已发布 Work 纳入同一 Publication 的批次与成员事实。"""

        expected_rules = {
            (
                str(rule.get("rule_id") or ""),
                str(rule.get("rule_code") or ""),
                str(rule.get("contract_hash") or ""),
            )
            for rule in excluded_rules
        }
        rows = await connection.fetch(
            """
            SELECT
                work_item.id::text AS work_item_id,
                work_item.batch_key,
                member.rule_id::text AS rule_id,
                member.rule_code,
                member.contract_hash
            FROM qc_inspection_work_item AS work_item
            INNER JOIN qc_inspection_work_item_rule AS member
                ON member.work_item_id = work_item.id
            WHERE work_item.task_id = $1::uuid
              AND work_item.page_capture_id = $2::uuid
              AND work_item.producer_agent_run_id = $3::uuid
              AND work_item.session_owner_agent_run_id = $3::uuid
              AND work_item.ruleset_id = $4::uuid
              AND work_item.ruleset_version = $5
              AND work_item.ruleset_snapshot_hash IS NOT DISTINCT FROM $6::varchar
              AND work_item.finalization_epoch = $7
              AND work_item.session_dependency = 'active_session'
              AND work_item.control_mode = 'enforced'
            ORDER BY work_item.batch_key, work_item.id, member.ordinal
            FOR UPDATE OF work_item, member
            """,
            lease.task_id,
            lease.page_capture_id,
            lease.source_agent_run_id,
            lease.ruleset_id,
            lease.ruleset_version,
            lease.ruleset_snapshot_hash,
            lease.finalization_epoch,
        )
        work_items: dict[str, dict[str, Any]] = {}
        for row in rows:
            work_item_id = str(row["work_item_id"])
            item = work_items.setdefault(
                work_item_id,
                {
                    "batch_key": str(row["batch_key"]),
                    "rules": set(),
                },
            )
            if item["batch_key"] != str(row["batch_key"]):
                raise RuntimeError("H5 live 工作项批次身份发生漂移")
            item["rules"].add(
                (
                    str(row["rule_id"]),
                    str(row["rule_code"]),
                    str(row["contract_hash"]),
                )
            )

        linked_work_item_ids: list[str] = []
        for work_item_id, item in work_items.items():
            member_rules = set(item["rules"])
            if not member_rules or not member_rules.issubset(expected_rules):
                raise RuntimeError("H5 live 工作项成员与收口冻结候选不一致")
            batch_key = str(item["batch_key"])
            linked = await connection.fetchval(
                """
                INSERT INTO qc_h5_inspection_publication_work_item (
                    publication_id, work_item_id, batch_key
                )
                SELECT publication.id, work_item.id, work_item.batch_key
                FROM qc_h5_inspection_publication AS publication
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = $2::uuid
                   AND work_item.task_id = publication.task_id
                   AND work_item.page_capture_id = publication.page_capture_id
                   AND work_item.producer_agent_run_id = publication.source_agent_run_id
                   AND work_item.session_owner_agent_run_id = publication.source_agent_run_id
                   AND work_item.finalization_epoch = publication.finalization_epoch
                   AND work_item.ruleset_id = publication.ruleset_id
                   AND work_item.ruleset_version = publication.ruleset_version
                   AND work_item.ruleset_snapshot_hash
                       IS NOT DISTINCT FROM publication.ruleset_snapshot_hash
                   AND work_item.session_dependency = 'active_session'
                   AND work_item.control_mode = 'enforced'
                WHERE publication.id = $1::uuid
                  AND work_item.batch_key = $3
                ON CONFLICT (publication_id, work_item_id) DO NOTHING
                RETURNING work_item_id::text
                """,
                lease.publication_id,
                work_item_id,
                batch_key,
            )
            if linked is None:
                replay = await connection.fetchval(
                    """
                    SELECT work_item_id::text
                    FROM qc_h5_inspection_publication_work_item
                    WHERE publication_id = $1::uuid
                      AND work_item_id = $2::uuid
                      AND batch_key = $3
                    """,
                    lease.publication_id,
                    work_item_id,
                    batch_key,
                )
                if replay is None:
                    raise RuntimeError("H5 live 工作项 Publication 关联漂移")
            linked_work_item_ids.append(work_item_id)
        return tuple(linked_work_item_ids)

    @staticmethod
    def _is_web_v2_full_inspection(identity: Any) -> bool:
        """只为锁定的 Web V2 正式质检启用跨流等待语义。"""

        task_config = _json_object(identity["task_config"])
        return (
            str(identity["target_platform"]).casefold() == "web"
            and str(task_config.get("workflow_version") or "").casefold() == "v2"
            and str(task_config.get("execution_mode") or "") == "full_inspection"
            and str(task_config.get("web_inspection_notification_mode") or "")
            == "enforced"
        )

    @staticmethod
    async def _missing_active_session_work_rules(
        connection: Any,
        *,
        lease: H5InspectionPublicationLease,
        excluded_rules: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, Any], ...]:
        """返回尚未被同身份 Web live Work 承接的 active-session 规则。"""

        if not excluded_rules:
            return ()
        rows = await connection.fetch(
            """
            SELECT DISTINCT
                member.rule_id::text AS rule_id,
                member.rule_code,
                member.contract_hash
            FROM qc_inspection_work_item AS work_item
            INNER JOIN qc_inspection_work_item_rule AS member
                ON member.work_item_id = work_item.id
            WHERE work_item.task_id = $1::uuid
              AND work_item.page_capture_id = $2::uuid
              AND work_item.producer_agent_run_id = $3::uuid
              AND work_item.session_owner_agent_run_id = $3::uuid
              AND work_item.ruleset_id = $4::uuid
              AND work_item.ruleset_version = $5
              AND work_item.ruleset_snapshot_hash IS NOT DISTINCT FROM $6::varchar
              AND work_item.finalization_epoch = $7
              AND work_item.session_dependency = 'active_session'
              AND work_item.control_mode = 'enforced'
              AND work_item.status NOT IN ('cancelled', 'superseded')
            """,
            lease.task_id,
            lease.page_capture_id,
            lease.source_agent_run_id,
            lease.ruleset_id,
            lease.ruleset_version,
            lease.ruleset_snapshot_hash,
            lease.finalization_epoch,
        )
        covered = {
            (
                str(row["rule_id"]),
                str(row["rule_code"]),
                str(row["contract_hash"]),
            )
            for row in rows
        }
        return tuple(
            rule
            for rule in excluded_rules
            if (
                str(rule.get("rule_id") or ""),
                str(rule.get("rule_code") or ""),
                str(rule.get("contract_hash") or ""),
            )
            not in covered
        )

    @staticmethod
    async def _unresolved_active_session_rules(
        connection: Any,
        *,
        lease: H5InspectionPublicationLease,
        excluded_rules: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, Any], ...]:
        """只把当前 Capture 尚无可信活会话终态的规则保留为覆盖缺口。"""
        if not excluded_rules:
            return ()
        rows = await connection.fetch(
            """
            SELECT DISTINCT
                member.rule_id::text AS rule_id,
                member.rule_code,
                member.contract_hash
            FROM qc_inspection_work_item AS work_item
            INNER JOIN qc_inspection_work_item_rule AS member
                ON member.work_item_id = work_item.id
            WHERE work_item.task_id = $1::uuid
              AND work_item.page_capture_id = $2::uuid
              AND work_item.producer_agent_run_id = $3::uuid
              AND work_item.session_owner_agent_run_id = $3::uuid
              AND work_item.ruleset_id = $4::uuid
              AND work_item.ruleset_version = $5
              AND work_item.ruleset_snapshot_hash IS NOT DISTINCT FROM $6::varchar
              AND work_item.finalization_epoch = $7
              AND work_item.session_dependency = 'active_session'
              AND work_item.control_mode = 'enforced'
              AND work_item.status IN ('succeeded', 'partial')
              AND member.status IN (
                  'written', 'evidence_insufficient', 'error', 'skipped'
              )
            """,
            lease.task_id,
            lease.page_capture_id,
            lease.source_agent_run_id,
            lease.ruleset_id,
            lease.ruleset_version,
            lease.ruleset_snapshot_hash,
            lease.finalization_epoch,
        )
        resolved = {
            (
                str(row["rule_id"]),
                str(row["rule_code"]),
                str(row["contract_hash"]),
            )
            for row in rows
        }
        return tuple(
            rule
            for rule in excluded_rules
            if (
                str(rule.get("rule_id") or ""),
                str(rule.get("rule_code") or ""),
                str(rule.get("contract_hash") or ""),
            )
            not in resolved
        )

    async def _annotate_capture_in_transaction(
        self,
        connection: Any,
        *,
        lease: H5InspectionPublicationLease,
        result: object,
        published_count: int,
        excluded_active_session_rule_count: int,
        business_status: str,
    ) -> None:
        """在 Publication 终态事务内写入 H5 Capture 质检投影。"""

        excluded_count = excluded_active_session_rule_count
        projected_business_status = (
            "active_session_rules_excluded" if excluded_count > 0 else business_status
        )
        formal_status = {
            "published": "evaluating",
            "no_rules_applicable": "completed",
            "active_session_rules_excluded": "degraded",
            "unmatched_page_type": "degraded",
        }.get(projected_business_status)
        if formal_status is None:
            raise RuntimeError("Publication Capture 标注状态无效")
        metadata = {
            "page_type": getattr(result, "page_type", None) or lease.page_type,
            "page_state": lease.page_state,
            "inspection": {
                "status": projected_business_status,
                "published_count": published_count,
            },
            "inspection_status": projected_business_status,
        }
        formal_capture_id = await connection.fetchval(
            """
            UPDATE qc_page_capture
            SET inspection_status = $3, updated_at = now()
            WHERE id = $1::uuid AND task_id = $2::uuid
            RETURNING id::text
            """,
            lease.page_capture_id,
            lease.task_id,
            formal_status,
        )
        updated_capture_id = await connection.fetchval(
            """
            UPDATE qc_capture
            SET metadata = COALESCE(metadata, '{}'::jsonb) || $3::jsonb
            WHERE id = $1::uuid AND task_id = $2::uuid
            RETURNING id::text
            """,
            lease.page_capture_id,
            lease.task_id,
            json.dumps(metadata, ensure_ascii=False),
        )
        if formal_capture_id is None or updated_capture_id is None:
            raise RuntimeError("Publication Capture 质检投影写入失败")

    async def _finish_consumer_run_if_idle(
        self,
        connection: Any,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> bool:
        """复用统一 SQL 收口当前 epoch 的 H5 逻辑消费 Run。"""

        return await finish_h5_consumer_run_if_idle_on_connection(
            connection,
            task_id=task_id,
            consumer_agent_run_id=consumer_agent_run_id,
        )

    async def retry(
        self,
        *,
        lease: H5InspectionPublicationLease,
        error: Exception,
    ) -> None:
        """失败时生成新行版本与新 Outbox；耗尽后进入可审计 failed 终态。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{lease.task_id}",
            )
            row = await connection.fetchrow(
                """
                SELECT task_id::text, finalization_epoch, attempt_count, max_attempts
                FROM qc_h5_inspection_publication
                WHERE id = $1::uuid AND lease_token = $2::uuid
                  AND row_version = $3 AND status = 'leased'
                FOR UPDATE
                """,
                lease.publication_id,
                lease.lease_token,
                lease.row_version,
            )
            if row is None:
                raise RuntimeError("Publication 重试写回被租约围栏拒绝")
            preserve_attempt_budget = isinstance(
                error, WebActiveSessionWorkPendingError
            )
            exhausted = (
                int(row["attempt_count"]) >= int(row["max_attempts"])
                and not preserve_attempt_budget
            )
            updated = await connection.fetchrow(
                """
                UPDATE qc_h5_inspection_publication
                SET status = CASE WHEN $4 THEN 'failed' ELSE 'retry_wait' END,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    next_attempt_at = now() + ($5 * interval '1 second'),
                    attempt_count = CASE
                        WHEN $8 THEN GREATEST(attempt_count - 1, 0)
                        ELSE attempt_count
                    END,
                    row_version = row_version + 1,
                    failure_code = $6,
                    failure_message = $7,
                    finished_at = CASE WHEN $4 THEN now() ELSE NULL END,
                    updated_at = now()
                WHERE id = $1::uuid AND lease_token = $2::uuid AND row_version = $3
                RETURNING row_version
                """,
                lease.publication_id,
                lease.lease_token,
                lease.row_version,
                exhausted,
                self._retry_delay_seconds,
                type(error).__name__[:128],
                str(error)[:1000] or type(error).__name__,
                preserve_attempt_budget,
            )
            if updated is None:
                raise RuntimeError("Publication 重试状态写入失败")
            if not exhausted:
                await self._insert_available_event(
                    connection,
                    publication_id=lease.publication_id,
                    task_id=str(row["task_id"]),
                    finalization_epoch=int(row["finalization_epoch"]),
                    row_version=int(updated["row_version"]),
                    occurred_at=datetime.now(UTC),
                )
            else:
                await self._finish_consumer_run_if_idle(
                    connection,
                    task_id=lease.task_id,
                    consumer_agent_run_id=lease.consumer_agent_run_id,
                )

    async def reconcile(self, *, limit: int = 100) -> int:
        """补建漏单、补发丢失唤醒，并回收过期租约。"""

        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        await self.reconcile_expired_evidence(limit=limit)
        await self.reconcile_waiting_evidence(limit=limit)
        reconciled = await self.reconcile_missing_publications(limit=limit)
        remaining = limit - reconciled
        if remaining <= 0:
            return reconciled
        resolved_reviews = await self.reconcile_review_publications(limit=remaining)
        reconciled += resolved_reviews
        remaining -= resolved_reviews
        if remaining <= 0:
            return reconciled
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                SELECT id::text AS publication_id, task_id::text AS task_id,
                       finalization_epoch, status, row_version
                FROM qc_h5_inspection_publication
                WHERE (
                    (
                        status = 'pending'
                        AND updated_at <= now() - ($2 * interval '1 second')
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_outbox_event AS outbox
                            WHERE outbox.aggregate_type = 'h5_inspection_publication'
                              AND outbox.aggregate_id = qc_h5_inspection_publication.id::text
                              AND outbox.aggregate_version
                                    = qc_h5_inspection_publication.row_version
                              AND outbox.status IN (
                                  'pending', 'publishing', 'retry_wait'
                              )
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_event_inbox AS inbox
                            WHERE inbox.aggregate_type = 'h5_inspection_publication'
                              AND inbox.aggregate_id = qc_h5_inspection_publication.id::text
                              AND inbox.aggregate_version
                                    = qc_h5_inspection_publication.row_version
                              AND inbox.status IN (
                                  'pending', 'processing', 'retry_wait'
                              )
                        )
                    )
                    OR (status = 'retry_wait' AND next_attempt_at <= now())
                    OR (status = 'leased' AND lease_expires_at <= now())
                )
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT $1
                """,
                remaining,
                max(self._retry_delay_seconds, 1),
            )
            for row in rows:
                version = int(
                    await connection.fetchval(
                        """
                        UPDATE qc_h5_inspection_publication
                        SET status = 'pending',
                            lease_owner = NULL, lease_token = NULL,
                            lease_expires_at = NULL,
                            next_attempt_at = now(),
                            row_version = row_version + 1,
                            updated_at = now()
                        WHERE id = $1::uuid
                          AND row_version = $2
                          AND status = $3
                        RETURNING row_version
                        """,
                        str(row["publication_id"]),
                        int(row["row_version"]),
                        str(row["status"]),
                    )
                )
                await self._insert_available_event(
                    connection,
                    publication_id=str(row["publication_id"]),
                    task_id=str(row["task_id"]),
                    finalization_epoch=int(row["finalization_epoch"]),
                    row_version=version,
                    occurred_at=datetime.now(UTC),
                )
                reconciled += 1
        remaining = limit - reconciled
        if remaining > 0:
            reconciled += await self.reconcile_linked_work_delivery(limit=remaining)
        remaining = limit - reconciled
        if remaining > 0:
            reconciled += await self.reconcile_consumer_run_lifecycle(limit=remaining)
        return reconciled

    async def reconcile_review_publications(self, *, limit: int = 100) -> int:
        """在活会话规则迟到终态后，幂等排空 H5 Publication 的旧复核缺口。"""

        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                SELECT
                    publication.id::text AS publication_id,
                    publication.task_id::text AS task_id,
                    publication.page_capture_id::text AS page_capture_id,
                    publication.consumer_agent_run_id::text AS consumer_agent_run_id,
                    page_capture.page_type,
                    COALESCE(page_capture.page_state, 'stable') AS page_state,
                    publication.published_work_item_count
                FROM qc_h5_inspection_publication AS publication
                INNER JOIN qc_task AS task ON task.id = publication.task_id
                INNER JOIN qc_page_capture AS page_capture
                  ON page_capture.id = publication.page_capture_id
                 AND page_capture.task_id = publication.task_id
                WHERE publication.status = 'needs_review'
                  AND task.target_platform = 'h5'
                  AND publication.finalization_epoch = task.finalization_epoch
                  AND task.finalization_state <> 'sealed'
                  AND publication.excluded_active_session_rule_count > 0
                  AND (
                    publication.published_work_item_count > 0
                    OR EXISTS (
                      SELECT 1
                      FROM qc_h5_inspection_publication_rule AS frozen_rule
                      WHERE frozen_rule.publication_id = publication.id
                        AND frozen_rule.session_dependency = 'active_session'
                        AND frozen_rule.execution_plan->>'disposition' = 'needs_review'
                    )
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_h5_inspection_publication_rule AS frozen_rule
                      WHERE frozen_rule.publication_id = publication.id
                        AND frozen_rule.session_dependency = 'active_session'
                        AND frozen_rule.execution_plan->>'disposition' = 'needs_review'
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_inspection_work_item AS work_item
                            INNER JOIN qc_inspection_work_item_rule AS member
                              ON member.work_item_id = work_item.id
                             AND member.rule_id = frozen_rule.rule_id
                             AND member.rule_code = frozen_rule.rule_code
                             AND member.contract_hash = frozen_rule.contract_hash
                            WHERE work_item.task_id = publication.task_id
                              AND work_item.page_capture_id = publication.page_capture_id
                              AND work_item.producer_agent_run_id
                                    = publication.source_agent_run_id
                              AND work_item.session_owner_agent_run_id
                                    = publication.source_agent_run_id
                              AND work_item.ruleset_id = publication.ruleset_id
                              AND work_item.ruleset_version = publication.ruleset_version
                              AND work_item.ruleset_snapshot_hash IS NOT DISTINCT FROM
                                    publication.ruleset_snapshot_hash
                              AND work_item.finalization_epoch
                                    = publication.finalization_epoch
                              AND work_item.session_dependency = 'active_session'
                              AND work_item.control_mode = 'enforced'
                              AND work_item.status IN ('succeeded', 'partial')
                              AND member.status IN (
                                  'written', 'evidence_insufficient', 'error', 'skipped'
                              )
                        )
                  )
                ORDER BY publication.created_at, publication.id
                FOR UPDATE OF publication SKIP LOCKED
                LIMIT $1
                """,
                limit,
            )
            reconciled_count = 0
            for row in rows:
                task_id = str(row["task_id"])
                publication_id = str(row["publication_id"])
                updated = await connection.fetchval(
                    """
                    UPDATE qc_h5_inspection_publication
                    SET status = 'published',
                        excluded_active_session_rule_count = 0,
                        failure_code = NULL,
                        failure_message = NULL,
                        row_version = row_version + 1,
                        finished_at = COALESCE(finished_at, now()),
                        updated_at = now()
                    WHERE id = $1::uuid
                      AND task_id = $2::uuid
                      AND status = 'needs_review'
                      AND EXISTS (
                          SELECT 1 FROM qc_task AS task
                          WHERE task.id = qc_h5_inspection_publication.task_id
                            AND task.finalization_epoch
                                  = qc_h5_inspection_publication.finalization_epoch
                            AND task.finalization_state <> 'sealed'
                      )
                    RETURNING id::text
                    """,
                    publication_id,
                    task_id,
                )
                if updated is None:
                    continue
                reconciled_count += 1
                await connection.execute(
                    """
                    UPDATE qc_h5_inspection_publication_rule
                    SET execution_plan = execution_plan || jsonb_build_object(
                            'disposition', 'resolved_after_publication',
                            'resolved_reason', 'active_session_work_terminal'
                        )
                    WHERE publication_id = $1::uuid
                      AND session_dependency = 'active_session'
                      AND execution_plan->>'disposition' = 'needs_review'
                    """,
                    publication_id,
                )
                await connection.execute(
                    """
                    UPDATE qc_page_capture
                    SET inspection_status = 'evaluating', updated_at = now()
                    WHERE id = $1::uuid AND task_id = $2::uuid
                    """,
                    str(row["page_capture_id"]),
                    task_id,
                )
                metadata = {
                    "page_type": row["page_type"],
                    "page_state": row["page_state"],
                    "inspection": {
                        "status": "published",
                        "published_count": int(row["published_work_item_count"] or 0),
                    },
                    "inspection_status": "published",
                }
                await connection.execute(
                    """
                    UPDATE qc_capture
                    SET metadata = COALESCE(metadata, '{}'::jsonb) || $3::jsonb
                    WHERE id = $1::uuid AND task_id = $2::uuid
                    """,
                    str(row["page_capture_id"]),
                    task_id,
                    json.dumps(metadata, ensure_ascii=False),
                )
                consumer_agent_run_id = row["consumer_agent_run_id"]
                if consumer_agent_run_id is not None:
                    await self._finish_consumer_run_if_idle(
                        connection,
                        task_id=task_id,
                        consumer_agent_run_id=str(consumer_agent_run_id),
                    )
        return reconciled_count

    async def reconcile_missing_publications(self, *, limit: int = 100) -> int:
        """从已校验 H5 清单反向补建当前 epoch 缺失的 Publication。"""

        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT
                    task.id::text AS task_id,
                    inventory.agent_run_id::text AS source_agent_run_id,
                    inventory.after_capture_id::text AS page_capture_id,
                    assessment.assessment_record_id AS page_type_assessment_id,
                    assessment.payload_hash AS assessment_hash,
                    page_capture.page_type,
                    COALESCE(page_capture.page_state, 'stable') AS page_state
                FROM qc_task AS task
                INNER JOIN qc_h5_evidence_inventory_item AS inventory
                    ON inventory.task_id = task.id
                   AND inventory.status = 'pass'
                   AND inventory.after_capture_id IS NOT NULL
                INNER JOIN qc_agent_run AS source_run
                    ON source_run.id = inventory.agent_run_id
                   AND source_run.task_id = task.id
                INNER JOIN qc_action_execution AS action
                    ON action.task_id = task.id
                   AND action.session_id = inventory.session_id
                   AND action.action_execution_id = inventory.action_execution_id
                INNER JOIN qc_surface_session AS surface_session
                    ON surface_session.task_id = task.id
                   AND surface_session.session_id = action.session_id
                   AND surface_session.surface_kind = 'web'
                   AND action.finalization_epoch = surface_session.finalization_epoch
                   AND surface_session.finalization_epoch = task.finalization_epoch
                INNER JOIN qc_capture AS capture
                    ON capture.id = inventory.after_capture_id
                   AND capture.task_id = task.id
                   AND capture.agent_run_id = inventory.agent_run_id
                   AND capture.capture_role = 'after_action'
                INNER JOIN qc_page_capture AS page_capture
                    ON page_capture.id = capture.id
                   AND page_capture.task_id = task.id
                   AND page_capture.page_type = inventory.page_type
                INNER JOIN LATERAL (
                    SELECT stored.assessment_record_id, stored.payload_hash
                    FROM qc_h5_page_type_assessment AS stored
                    WHERE stored.task_id = task.id
                      AND stored.agent_run_id = inventory.agent_run_id
                      AND stored.page_capture_id = inventory.after_capture_id
                      AND stored.detection_status = 'recognized'
                      AND stored.page_type = page_capture.page_type
                    ORDER BY stored.updated_at DESC, stored.assessment_record_id ASC
                    LIMIT 1
                ) AS assessment ON TRUE
                LEFT JOIN qc_h5_inspection_publication AS publication
                    ON publication.task_id = task.id
                   AND publication.page_capture_id = inventory.after_capture_id
                   AND publication.finalization_epoch = task.finalization_epoch
                WHERE task.target_platform IN ('h5', 'web')
                  AND task.status = 'running'
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND task.task_config->>'execution_mode' = 'full_inspection'
                  AND task.task_config->>'h5_inspection_notification_mode' = 'enforced'
                  AND source_run.status IN (
                      'pending', 'running', 'done', 'failed', 'aborted', 'canceled'
                  )
                  AND (
                        task.finalization_agent_run_id = source_run.id
                        OR (
                            task.finalization_agent_run_id IS NULL
                            AND NOT EXISTS (
                                SELECT 1
                                FROM qc_agent_run AS competing_run
                                WHERE competing_run.task_id = task.id
                                  AND competing_run.id <> source_run.id
                                  AND competing_run.status IN ('pending', 'running')
                                  AND NOT (
                                      competing_run.agent_type = 'rule_judge'
                                      AND competing_run.input_context->>'source'
                                          = 'h5_inspection_publication'
                                      AND competing_run.input_context->>'finalization_epoch'
                                          = task.finalization_epoch::text
                                  )
                            )
                        )
                  )
                  AND COALESCE(
                        page_capture.inspection_lifecycle_status,
                        capture.metadata->>'inspection_lifecycle_status',
                        ''
                      ) NOT IN ('superseded', 'abandoned')
                  AND (
                        lower(task.target_platform) <> 'h5'
                        OR COALESCE(
                            action.intent_payload->'parameters'
                                ->>'supplemental_evidence_source',
                            ''
                        ) <> 'targeted_recapture'
                      )
                  AND publication.id IS NULL
                  AND NOT ({H5_ALREADY_INSPECTED_LOGICAL_STATE_SQL})
                ORDER BY inventory.created_at, inventory.after_capture_id
                LIMIT $1
                """,
                limit,
            )
        created_count = 0
        for row in rows:
            result = await self.create_for_capture(
                task_id=str(row["task_id"]),
                source_agent_run_id=str(row["source_agent_run_id"]),
                capture_id=str(row["page_capture_id"]),
                page_type_assessment_id=str(row["page_type_assessment_id"]),
                assessment_hash=str(row["assessment_hash"]),
                page_type=str(row["page_type"]),
                page_state=str(row["page_state"]),
            )
            created_count += int(result.created)
        return created_count

    async def reconcile_expired_evidence(self, *, limit: int = 100) -> int:
        """主探索退出后也须推进 H5 请求到期结算，不能只靠浏览器领取时处理。"""
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        from services.api.app.infrastructure.db.h5_probe_terminal_repository import (
            PostgresH5ProbeTerminalSettlementRepository,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT DISTINCT task.id::text AS task_id,
                                capture.agent_run_id::text AS agent_run_id
                FROM qc_evidence_request AS request
                INNER JOIN qc_task AS task ON task.id = request.task_id
                INNER JOIN qc_capture AS capture
                    ON capture.id = request.target_capture_id
                   AND capture.task_id = task.id
                WHERE task.target_platform = 'h5'
                  AND task.status IN ('running', 'review')
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND request.finalization_epoch = task.finalization_epoch
                  AND request.session_dependency IN ('active_session', 'snapshot')
                  AND request.status IN ('pending', 'routed', 'running')
                  AND request.expires_at <= now()
                ORDER BY task_id, agent_run_id
                LIMIT $1
                """, limit,
            )
        settlement = PostgresH5ProbeTerminalSettlementRepository(pool=pool)
        settled = 0
        for row in rows:
            settled += await settlement.settle_ready(
                task_id=str(row["task_id"]), agent_run_id=str(row["agent_run_id"]),
            )
        return settled

    async def reconcile_waiting_evidence(self, *, limit: int = 100) -> int:
        """主流程退出后，复用正式结算入口处理已终态补采，不等待新 Judge。"""
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT DISTINCT work_item.task_id::text AS task_id
                FROM qc_h5_inspection_publication_work_item AS link
                INNER JOIN qc_h5_inspection_publication AS publication
                    ON publication.id = link.publication_id
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = link.work_item_id
                INNER JOIN qc_task AS task ON task.id = work_item.task_id
                WHERE task.target_platform = 'h5'
                  AND task.status IN ('running', 'review')
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND publication.task_id = task.id
                  AND publication.finalization_epoch = task.finalization_epoch
                  AND work_item.finalization_epoch = task.finalization_epoch
                  AND work_item.control_mode = 'enforced'
                  AND work_item.status = 'waiting_evidence'
                  AND EXISTS (
                      SELECT 1 FROM qc_evidence_request AS terminal_request
                      WHERE terminal_request.task_id = task.id
                        AND terminal_request.work_item_id = work_item.id
                        AND terminal_request.finalization_epoch = task.finalization_epoch
                        AND terminal_request.status IN ('satisfied', 'unfulfillable', 'expired', 'cancelled')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM qc_evidence_request AS inflight
                      WHERE inflight.work_item_id = work_item.id
                        AND inflight.status IN ('pending', 'routed', 'running')
                  )
                ORDER BY task_id
                LIMIT $1
                """,
                limit,
            )
        settlement = PostgresInspectionWorkItemRepository(pool=pool)
        reopened = 0
        for row in rows:
            reopened += await settlement.reopen_waiting_evidence(task_id=str(row["task_id"]))
        return reopened

    async def reconcile_consumer_run_lifecycle(self, *, limit: int = 100) -> int:
        """在 Inbox 完成后补做逻辑 Run 终态收口，避免没有后续消息时永久 running。"""

        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                SELECT
                    consumer.id::text AS consumer_agent_run_id,
                    consumer.task_id::text AS task_id
                FROM qc_agent_run AS consumer
                INNER JOIN qc_task AS task
                    ON task.id = consumer.task_id
                WHERE consumer.agent_type = 'rule_judge'
                  AND consumer.status = 'running'
                  AND consumer.input_context->>'source' = 'h5_inspection_publication'
                  AND task.finalization_state <> 'sealed'
                  AND EXISTS (
                      SELECT 1
                      FROM qc_h5_inspection_publication AS publication
                      WHERE publication.task_id = consumer.task_id
                        AND publication.finalization_epoch
                              = (consumer.input_context->>'finalization_epoch')::integer
                  )
                ORDER BY consumer.created_at, consumer.id
                LIMIT $1
                """,
                limit,
            )
            closed_count = 0
            for row in rows:
                task_id = str(row["task_id"])
                lock_acquired = await connection.fetchval(
                    "SELECT pg_try_advisory_xact_lock(hashtext($1))",
                    f"task-finalization:{task_id}",
                )
                if not lock_acquired:
                    continue
                closed_count += int(
                    await self._finish_consumer_run_if_idle(
                        connection,
                        task_id=task_id,
                        consumer_agent_run_id=str(row["consumer_agent_run_id"]),
                    )
                )
        return closed_count

    async def reconcile_linked_work_delivery(self, *, limit: int = 100) -> int:
        """为 H5 关联工作项的到期重试、过期租约和失联 pending 生成新唤醒。"""

        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                SELECT
                    work_item.id::text AS work_item_id,
                    work_item.task_id::text AS task_id,
                    work_item.page_capture_id::text AS page_capture_id,
                    work_item.protocol_version,
                    work_item.batch_key,
                    work_item.strategy,
                    work_item.category,
                    work_item.session_dependency,
                    work_item.input_hash,
                    work_item.control_mode,
                    work_item.status,
                    work_item.row_version,
                    task.finalization_epoch
                FROM qc_h5_inspection_publication_work_item AS link
                INNER JOIN qc_h5_inspection_publication AS publication
                    ON publication.id = link.publication_id
                   AND publication.mode = 'enforced'
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = link.work_item_id
                INNER JOIN qc_task AS task
                    ON task.id = work_item.task_id
                   AND task.id = publication.task_id
                   AND task.finalization_epoch = publication.finalization_epoch
                   AND work_item.finalization_epoch = task.finalization_epoch
                WHERE task.status = 'running'
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND work_item.control_mode = 'enforced'
                  AND work_item.attempt_count < work_item.max_attempts
                  AND (
                      (work_item.status = 'retry_wait' AND work_item.next_attempt_at <= now())
                      OR (
                          work_item.status IN ('leased', 'running')
                          AND work_item.lease_expires_at <= now()
                      )
                      OR (
                          work_item.status = 'pending'
                          AND work_item.updated_at <= now() - ($2 * interval '1 second')
                          AND NOT EXISTS (
                              SELECT 1
                              FROM qc_outbox_event AS active_delivery
                              WHERE active_delivery.aggregate_type = 'inspection_work_item'
                                AND active_delivery.aggregate_id = work_item.id::text
                                AND active_delivery.aggregate_version = work_item.row_version
                                AND active_delivery.status IN ('pending', 'leased')
                          )
                      )
                  )
                ORDER BY work_item.next_attempt_at, work_item.updated_at, work_item.id
                FOR UPDATE OF work_item SKIP LOCKED
                LIMIT $1
                """,
                limit,
                max(1, self._retry_delay_seconds),
            )
            for row in rows:
                work_item_id = str(row["work_item_id"])
                if str(row["status"]) in {"leased", "running"}:
                    await connection.execute(
                        """
                        UPDATE qc_inspection_attempt
                        SET status = 'abandoned',
                            error_code = COALESCE(
                                error_code, 'INSPECTION_LEASE_EXPIRED'
                            ),
                            error_message = COALESCE(
                                error_message, 'H5 关联工作项租约过期，已创建新版本唤醒'
                            ),
                            finished_at = COALESCE(finished_at, now()),
                            updated_at = now()
                        WHERE work_item_id = $1::uuid
                          AND status IN ('running', 'judged')
                        """,
                        work_item_id,
                    )
                updated = await connection.fetchrow(
                    """
                    UPDATE qc_inspection_work_item
                    SET status = 'pending',
                        next_attempt_at = now(),
                        consumer_agent_run_id = NULL,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE id = $1::uuid
                    RETURNING row_version
                    """,
                    work_item_id,
                )
                if updated is None:
                    raise RuntimeError("H5 关联工作项重唤醒状态推进失败")
                row_version = int(updated["row_version"])
                await self._ensure_consumer_run(
                    connection,
                    task_id=str(row["task_id"]),
                    finalization_epoch=int(row["finalization_epoch"]),
                )
                await insert_outbox_event(
                    connection,
                    aggregate_type="inspection_work_item",
                    aggregate_id=work_item_id,
                    envelope=build_protocol_envelope(
                        message_type=INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
                        payload_protocol_version=str(row["protocol_version"]),
                        idempotency_key=f"h5-work-rewake:{work_item_id}:{row_version}",
                        task_id=str(row["task_id"]),
                        aggregate_type="inspection_work_item",
                        aggregate_id=work_item_id,
                        aggregate_version=row_version,
                        occurred_at=datetime.now(UTC),
                        payload={
                            "protocol_version": str(row["protocol_version"]),
                            "work_item_id": work_item_id,
                            "task_id": str(row["task_id"]),
                            "page_capture_id": str(row["page_capture_id"]),
                            "batch_key": str(row["batch_key"]),
                            "strategy": str(row["strategy"]),
                            "category": row["category"],
                            "session_dependency": str(row["session_dependency"]),
                            "control_mode": str(row["control_mode"]),
                            "input_hash": str(row["input_hash"]),
                            "reopen_reason": "h5_linked_work_reconcile",
                        },
                        correlation_id=str(row["task_id"]),
                    ),
                )
        return len(rows)

    async def await_task_terminal(
        self,
        *,
        task_id: str,
        minimum_total_count: int = 0,
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 0.2,
    ) -> H5InspectionPublicationTaskSummary:
        """等待任务当前 epoch 的全部 Publication 终态，供正式收口建立屏障。"""

        if minimum_total_count < 0:
            raise ValueError("Publication 最小数量不能小于 0")
        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("Publication 等待配置必须大于 0")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            pool = await self._get_pool()
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT
                        count(publication.id)::integer AS total_count,
                        count(publication.id) FILTER (
                            WHERE publication.status IN ('pending', 'leased', 'retry_wait')
                        )::integer AS nonterminal_count,
                        COALESCE(sum(publication.published_work_item_count), 0)::integer
                            AS published_work_item_count,
                        COALESCE(sum(publication.excluded_active_session_rule_count), 0)::integer
                            AS excluded_active_session_rule_count,
                        count(publication.id) FILTER (
                            WHERE publication.status = 'needs_review'
                        )::integer AS needs_review_count,
                        count(publication.id) FILTER (
                            WHERE publication.status = 'failed'
                        )::integer AS failed_count
                    FROM qc_task AS task
                    LEFT JOIN qc_h5_inspection_publication AS publication
                        ON publication.task_id = task.id
                       AND publication.finalization_epoch = task.finalization_epoch
                    WHERE task.id = $1::uuid
                    """,
                    task_id,
                )
            if row is None:
                raise RuntimeError("等待 H5 Publication 时任务不存在")
            summary = H5InspectionPublicationTaskSummary(
                total_count=int(row["total_count"]),
                nonterminal_count=int(row["nonterminal_count"]),
                published_work_item_count=int(row["published_work_item_count"]),
                excluded_active_session_rule_count=int(row["excluded_active_session_rule_count"]),
                needs_review_count=int(row["needs_review_count"]),
                failed_count=int(row["failed_count"]),
            )
            if summary.needs_review_count > 0:
                resolved_count = await self.reconcile_review_publications(
                    limit=max(summary.needs_review_count, 1)
                )
                if resolved_count > 0:
                    continue
            if (
                summary.total_count >= minimum_total_count
                and summary.nonterminal_count == 0
            ):
                return summary
            if loop.time() >= deadline:
                raise TimeoutError(
                    "H5 Publication 完成屏障超时："
                    f"期望至少 {minimum_total_count} 条，实际 {summary.total_count} 条，"
                    f"仍有 {summary.nonterminal_count} 条非终态"
                )
            await asyncio.sleep(poll_interval_seconds)

    async def _validate_strict_web_incremental_gate(
        self,
        connection: Any,
        *,
        task_id: str,
        source_agent_run_id: str,
        capture_id: str,
        page_type_assessment_id: str,
        final_handoff_epoch: int | None,
    ) -> None:
        """在 Publication 事务内重验 Web 单 Capture 的完整增量门禁。"""

        from services.api.app.application.web_page_type_assessment import (
            WEB_CANONICAL_PAGE_TYPES,
        )

        finalization_gate_sql = "task.finalization_state = 'open'"
        run_status_gate_sql = "source_run.status IN ('pending', 'running')"
        query_args: tuple[object, ...] = (
            task_id,
            source_agent_run_id,
            capture_id,
            page_type_assessment_id,
            sorted(WEB_CANONICAL_PAGE_TYPES),
        )
        if final_handoff_epoch is not None:
            run_status_gate_sql = (
                "source_run.status IN "
                "('pending', 'running', 'done', 'failed', 'aborted')"
            )
            finalization_gate_sql = """
              $6::integer IS NOT NULL
              AND task.finalization_state = 'exploration_draining'
              AND task.finalization_agent_run_id = source_run.id
              AND task.finalization_epoch = $6::integer
            """
            query_args = (*query_args, final_handoff_epoch)

        row = await connection.fetchrow(
            f"""
            SELECT 1 AS gate_passed
            FROM qc_task AS task
            INNER JOIN qc_agent_run AS source_run
                ON source_run.task_id = task.id
               AND source_run.id = $2::uuid
               AND source_run.agent_type = 'main'
               AND {run_status_gate_sql}
            INNER JOIN qc_surface_session AS session
                ON session.task_id = task.id
               AND session.session_id = ('web:' || source_run.id::text)
               AND session.surface_kind = 'web'
               AND session.session_owner = 'web-v2-workflow'
               AND session.status = 'active'
               AND session.finalization_epoch = task.finalization_epoch
            INNER JOIN qc_action_execution AS action
                ON action.task_id = task.id
               AND action.session_id = session.session_id
               AND action.finalization_epoch = task.finalization_epoch
               AND action.status = 'completed'
            INNER JOIN qc_capture AS common_capture
                ON common_capture.task_id = task.id
               AND common_capture.id = $3::uuid
               AND common_capture.agent_run_id = source_run.id
               AND common_capture.surface_kind = 'web'
               AND common_capture.surface_session_id = session.session_id
               AND common_capture.action_execution_id = action.action_execution_id
               AND common_capture.capture_role = 'after_action'
               AND common_capture.capture_status = 'captured'
               AND common_capture.parent_capture_id = action.source_capture_id
               AND action.result_capture_id = common_capture.id
            INNER JOIN qc_page_capture AS capture
                ON capture.task_id = task.id
               AND capture.id = common_capture.id
               AND capture.agent_run_id = source_run.id
               AND capture.surface_session_id = session.session_id
               AND capture.action_execution_id = action.action_execution_id
               AND capture.capture_role = 'after_action'
               AND capture.capture_status = 'captured'
               AND capture.parent_page_capture_id = action.source_capture_id
            INNER JOIN qc_capture AS before_capture
                ON before_capture.task_id = task.id
               AND before_capture.id = action.source_capture_id
               AND before_capture.id = common_capture.parent_capture_id
               AND before_capture.agent_run_id = source_run.id
               AND before_capture.surface_kind = 'web'
               AND before_capture.surface_session_id = session.session_id
               AND before_capture.action_execution_id = action.action_execution_id
               AND before_capture.capture_role = 'before_action'
               AND before_capture.capture_status = 'captured'
            INNER JOIN qc_h5_evidence_inventory_item AS inventory
                ON inventory.task_id = task.id
               AND inventory.agent_run_id = source_run.id
               AND inventory.session_id = session.session_id
               AND inventory.action_execution_id = action.action_execution_id
               AND inventory.before_capture_id = before_capture.id
               AND inventory.after_capture_id = common_capture.id
               AND inventory.status = 'pass'
               AND inventory.page_type = capture.page_type
            INNER JOIN qc_h5_page_type_assessment AS assessment
                ON assessment.assessment_record_id = $4
               AND assessment.task_id = task.id
               AND assessment.agent_run_id = source_run.id
               AND assessment.session_id = session.session_id
               AND assessment.page_capture_id = capture.id
               AND assessment.detection_status = 'recognized'
               AND assessment.schema_version IN ('web-page-type-assessment.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4')
               AND assessment.page_type = capture.page_type
            INNER JOIN qc_evidence_artifact AS after_screenshot
                ON after_screenshot.task_id = task.id
               AND after_screenshot.page_capture_id = capture.id
               AND after_screenshot.artifact_type = 'screenshot'
               AND after_screenshot.content_type = 'image/png'
               AND after_screenshot.source IN ('baseline', 'rule_supplement')
               AND after_screenshot.lifecycle_status IN ('active', 'archived')
            INNER JOIN qc_evidence_artifact AS after_dom
                ON after_dom.task_id = task.id
               AND after_dom.page_capture_id = capture.id
               AND after_dom.artifact_type = 'dom'
               AND after_dom.source IN ('baseline', 'rule_supplement')
               AND after_dom.lifecycle_status IN ('active', 'archived')
            INNER JOIN qc_evidence_artifact AS after_text
                ON after_text.task_id = task.id
               AND after_text.page_capture_id = capture.id
               AND after_text.artifact_type = 'visible_text'
               AND after_text.source IN ('baseline', 'rule_supplement')
               AND after_text.lifecycle_status IN ('active', 'archived')
            INNER JOIN qc_evidence_artifact AS before_screenshot
                ON before_screenshot.task_id = task.id
               AND before_screenshot.page_capture_id = before_capture.id
               AND before_screenshot.artifact_type = 'screenshot'
               AND before_screenshot.content_type = 'image/png'
               AND before_screenshot.source IN ('baseline', 'rule_supplement')
               AND before_screenshot.lifecycle_status IN ('active', 'archived')
            INNER JOIN qc_evidence_artifact AS before_dom
                ON before_dom.task_id = task.id
               AND before_dom.page_capture_id = before_capture.id
               AND before_dom.artifact_type = 'dom'
               AND before_dom.source IN ('baseline', 'rule_supplement')
               AND before_dom.lifecycle_status IN ('active', 'archived')
            INNER JOIN qc_evidence_manifest AS after_manifest
                ON after_manifest.task_id = task.id
               AND after_manifest.session_id = session.session_id
               AND after_manifest.capture_id = capture.id
               AND after_manifest.manifest_version = (
                    SELECT MAX(latest_after.manifest_version)
                    FROM qc_evidence_manifest AS latest_after
                    WHERE latest_after.task_id = task.id
                      AND latest_after.capture_id = capture.id
               )
               AND after_manifest.quality_status = 'pass'
               AND NULLIF(btrim(after_manifest.manifest_id), '') IS NOT NULL
               AND NULLIF(btrim(after_manifest.manifest_hash), '') IS NOT NULL
            INNER JOIN qc_evidence_manifest AS before_manifest
                ON before_manifest.task_id = task.id
               AND before_manifest.session_id = session.session_id
               AND before_manifest.capture_id = before_capture.id
               AND before_manifest.manifest_version = (
                    SELECT MAX(latest_before.manifest_version)
                    FROM qc_evidence_manifest AS latest_before
                    WHERE latest_before.task_id = task.id
                      AND latest_before.capture_id = before_capture.id
               )
               AND before_manifest.quality_status = 'pass'
               AND NULLIF(btrim(before_manifest.manifest_id), '') IS NOT NULL
               AND NULLIF(btrim(before_manifest.manifest_hash), '') IS NOT NULL
            WHERE task.id = $1::uuid
              AND task.target_platform = 'web'
              AND LOWER(task.task_config->>'workflow_version') = 'v2'
              AND LOWER(task.task_config #>> '{{_task_target,platform}}') = 'web'
              AND task.status = 'running'
              AND {finalization_gate_sql}
              AND task.task_config->>'execution_mode' = 'full_inspection'
              AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
              AND source_run.id = (
                    SELECT latest.id
                    FROM qc_agent_run AS latest
                    WHERE latest.task_id = task.id
                      AND latest.agent_type = 'main'
                    ORDER BY latest.created_at DESC, latest.id DESC
                    LIMIT 1
              )
              AND capture.page_type = ANY($5::varchar[])
              AND COALESCE(
                    capture.inspection_lifecycle_status,
                    common_capture.metadata->>'inspection_lifecycle_status',
                    ''
                  ) NOT IN ('superseded', 'abandoned')
            FOR UPDATE OF task, source_run, session, action, common_capture,
                capture, before_capture, inventory, assessment,
                after_screenshot, after_dom, after_text,
                before_screenshot, before_dom, after_manifest, before_manifest
            """,
            *query_args,
        )
        if row is None:
            raise RuntimeError(
                "WEB_INSPECTION_STRICT_GATE_REJECTED: Web 增量门禁事实已变化"
            )

    async def _insert_available_event(
        self,
        connection: Any,
        *,
        publication_id: str,
        task_id: str,
        finalization_epoch: int,
        row_version: int,
        occurred_at: datetime,
    ) -> str:
        envelope = build_protocol_envelope(
            message_type=H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
            payload_protocol_version=H5_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
            idempotency_key=f"h5-inspection-publication:{publication_id}:{row_version}",
            task_id=task_id,
            aggregate_type="h5_inspection_publication",
            aggregate_id=publication_id,
            aggregate_version=row_version,
            occurred_at=occurred_at,
            payload={
                "protocol_version": H5_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
                "publication_id": publication_id,
                "task_id": task_id,
                "finalization_epoch": finalization_epoch,
                "message_version": row_version,
            },
        )
        await insert_outbox_event(
            connection,
            aggregate_type="h5_inspection_publication",
            aggregate_id=publication_id,
            envelope=envelope,
        )
        return envelope.message_id

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json_object(value: object) -> dict[str, Any]:
    """兼容 asyncpg 默认返回的 JSON 文本，并对非对象值失败关闭。"""

    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
