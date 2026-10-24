"""App sealed Index Publication 的 PostgreSQL 权威仓储。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from services.api.app.application.app_inspection_publication import (
    AppInspectionPublicationClaimDisposition,
    AppInspectionPublicationClaimResult,
    AppInspectionPublicationEntryInput,
    AppInspectionPublicationEntryLease,
    AppInspectionPublicationLease,
    AppInspectionPublicationTaskSummary,
    AppInspectionPublicationWriteResult,
    AppPublicationSnapshotMismatch,
    AppUnpublishedSealedIndex,
)
from services.api.app.application.app_inspection_publisher import (
    CAPTURE_INSPECTION_METADATA_MERGE_SQL,
    build_capture_metadata_patch,
)
from services.api.app.application.inspection_work_items import (
    PublishInspectionWorkItemCommand,
    build_inspection_work_item_input_hash,
    validate_inspection_work_item_publish_command,
)
from services.api.app.application.outbox import INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE
from services.api.app.domain.inspection.app_publication import (
    APP_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
    build_app_inspection_publication_available,
    build_app_inspection_publication_id,
)
from services.api.app.domain.protocol import build_protocol_envelope, stable_protocol_hash
from services.api.app.infrastructure.db.app_consumer_run_lifecycle import (
    finish_app_consumer_run_if_idle_on_connection,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    publish_inspection_work_item_on_connection,
)
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.pool import get_postgres_pool

_TERMINAL_STATUSES = frozenset(
    {"published", "not_applicable", "needs_review", "failed", "cancelled", "superseded"}
)


class PostgresAppInspectionPublicationRepository:
    """以整份 sealed Index 为原子发布单元，禁止部分 Entry 静默遗漏。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        max_attempts: int = 3,
        retry_delay_seconds: int = 5,
    ) -> None:
        if max_attempts <= 0 or retry_delay_seconds < 0:
            raise ValueError("App Publication 重试配置无效")
        self._database_url = database_url
        self._pool = pool
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds

    async def create_for_index(
        self,
        *,
        task_id: str,
        source_agent_run_id: str,
        evidence_index_id: str,
        evidence_index_hash: str,
        entries: tuple[AppInspectionPublicationEntryInput, ...],
        expected_finalization_epoch: int | None = None,
    ) -> AppInspectionPublicationWriteResult:
        """复核 sealed Index 后，同事务创建 Publication、Entry 与可用事件。"""

        if tuple(item.sequence_no for item in entries) != tuple(range(1, len(entries) + 1)):
            raise RuntimeError("App Publication Entry sequence_no 必须连续且从 1 开始")
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
                    index.index_id,
                    index.content_sha256
                FROM qc_task AS task
                INNER JOIN qc_ruleset AS ruleset ON ruleset.id = task.ruleset_id
                INNER JOIN qc_agent_run AS source_run
                    ON source_run.id = $2::uuid AND source_run.task_id = task.id
                INNER JOIN qc_app_evidence_index AS index
                    ON index.index_id = $3
                   AND index.task_id = task.id
                   AND index.agent_run_id = source_run.id
                INNER JOIN qc_surface_session AS surface_session
                    ON surface_session.task_id = task.id
                   AND surface_session.session_id = index.surface_session_id
                   AND surface_session.platform_build_ref = index.build_id
                INNER JOIN qc_app_exploration_result AS result
                    ON result.task_id = task.id
                   AND result.agent_run_id = source_run.id
                   AND result.surface_session_id = index.surface_session_id
                   AND result.evidence_index_id = index.index_id
                   AND result.evidence_index_hash = index.content_sha256
                WHERE task.id = $1::uuid
                  AND task.target_platform = 'app'
                  AND task.status = 'running'
                  AND task.deleted_at IS NULL
                  AND task.finalization_agent_run_id = source_run.id
                  AND ($5::integer IS NULL OR task.finalization_epoch = $5)
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND task.task_config->>'execution_mode' = 'full_inspection'
                  AND source_run.status IN (
                      'pending', 'running', 'done', 'failed', 'aborted'
                  )
                  AND COALESCE(source_run.output_context->>'cancel_requested', 'false')
                      <> 'true'
                  AND index.status = 'sealed'
                  AND index.content_sha256 = $4
                  AND surface_session.status IN ('closed', 'lost', 'failed')
                  AND surface_session.finalization_epoch = task.finalization_epoch
                FOR UPDATE OF task, ruleset, source_run, index
                """,
                task_id,
                source_agent_run_id,
                evidence_index_id,
                evidence_index_hash,
                expected_finalization_epoch,
            )
            if source is None:
                raise AppPublicationSnapshotMismatch()
            stored_entries = await connection.fetch(
                """
                SELECT
                    entry.sequence_no,
                    entry.capture_id::text AS capture_id,
                    entry.evidence_manifest_id,
                    entry.evidence_manifest_version,
                    entry.semantic_role,
                    NULLIF(visual.visual_page_type, '') AS page_type,
                    COALESCE(NULLIF(visual.page_state, ''), 'unknown') AS page_state,
                    visual.observation_id IS NULL OR (
                        visual.task_id = index.task_id
                        AND visual.session_id = index.surface_session_id
                        AND visual.screenshot_artifact_id = app_capture.screenshot_artifact_id
                        AND EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements(
                                COALESCE(manifest.manifest_payload->'artifacts', '[]'::jsonb)
                            ) AS artifact
                            WHERE artifact->>'artifact_type' = 'screenshot'
                              AND artifact->>'artifact_id'
                                  = visual.screenshot_artifact_id::text
                        )
                    ) AS visual_fact_valid
                FROM qc_app_evidence_index_entry AS entry
                INNER JOIN qc_app_evidence_index AS index
                    ON index.index_id = entry.index_id
                INNER JOIN qc_capture AS capture
                    ON capture.id = entry.capture_id
                   AND capture.task_id = index.task_id
                   AND capture.agent_run_id = index.agent_run_id
                   AND capture.surface_session_id = index.surface_session_id
                   AND capture.capture_status = 'captured'
                   AND capture.surface_kind IN ('android', 'ios')
                INNER JOIN qc_evidence_manifest AS manifest
                    ON manifest.manifest_id = entry.evidence_manifest_id
                   AND manifest.manifest_version = entry.evidence_manifest_version
                   AND manifest.task_id = index.task_id
                   AND manifest.capture_id = capture.id
                   AND manifest.session_id = index.surface_session_id
                   AND manifest.quality_status = 'pass'
                LEFT JOIN qc_app_visual_observation AS visual
                    ON visual.capture_id = entry.capture_id
                LEFT JOIN qc_app_capture AS app_capture
                    ON app_capture.capture_id = capture.id
                WHERE entry.index_id = $1
                ORDER BY entry.sequence_no
                """,
                evidence_index_id,
            )
            if any(not row["visual_fact_valid"] for row in stored_entries):
                raise RuntimeError("App Publication 视觉事实不属于同帧封存 Manifest")
            normalized_stored = tuple(
                (
                    int(row["sequence_no"]),
                    str(row["capture_id"]),
                    str(row["evidence_manifest_id"]),
                    int(row["evidence_manifest_version"]),
                    str(row["semantic_role"]),
                    _optional_text(row["page_type"]),
                    str(row["page_state"]),
                )
                for row in stored_entries
            )
            normalized_input = tuple(
                (
                    item.sequence_no,
                    item.capture_id,
                    item.evidence_manifest_id,
                    item.evidence_manifest_version,
                    item.semantic_role,
                    item.page_type,
                    item.page_state,
                )
                for item in entries
            )
            if normalized_stored != normalized_input:
                raise RuntimeError("App Publication Entry 与数据库 sealed Index 发生漂移")

            finalization_epoch = int(source["finalization_epoch"])
            ruleset_snapshot_hash = _optional_text(source["ruleset_snapshot_hash"])
            publication_id = build_app_inspection_publication_id(
                task_id=task_id,
                finalization_epoch=finalization_epoch,
                evidence_index_id=evidence_index_id,
                evidence_index_hash=evidence_index_hash,
                ruleset_snapshot_hash=ruleset_snapshot_hash,
            )
            input_hash = stable_protocol_hash(
                {
                    "protocol_version": APP_INSPECTION_PUBLICATION_PROTOCOL_VERSION,
                    "task_id": task_id,
                    "source_agent_run_id": source_agent_run_id,
                    "evidence_index_id": evidence_index_id,
                    "evidence_index_hash": evidence_index_hash,
                    "ruleset_id": str(source["ruleset_id"]),
                    "ruleset_version": int(source["ruleset_version"]),
                    "ruleset_snapshot_hash": ruleset_snapshot_hash,
                    "finalization_epoch": finalization_epoch,
                    "entries": [
                        {
                            "sequence_no": item.sequence_no,
                            "capture_id": item.capture_id,
                            "evidence_manifest_id": item.evidence_manifest_id,
                            "evidence_manifest_version": item.evidence_manifest_version,
                            "semantic_role": item.semantic_role,
                            "page_type": item.page_type,
                            "page_state": item.page_state,
                        }
                        for item in entries
                    ],
                }
            )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_app_inspection_publication AS existing (
                    id, task_id, source_agent_run_id, evidence_index_id,
                    evidence_index_hash, ruleset_id, ruleset_version,
                    ruleset_snapshot_hash, finalization_epoch, input_hash,
                    expected_entry_count, max_attempts, status,
                    terminal_entry_count, expected_rule_count,
                    failure_code, failure_message, finished_at
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4, $5, $6::uuid, $7,
                    $8, $9, $10, $11, $12,
                    CASE WHEN $11 = 0 THEN 'needs_review' ELSE 'pending' END,
                    0,
                    CASE WHEN $11 = 0 THEN 0 ELSE NULL END,
                    CASE WHEN $11 = 0 THEN 'empty_sealed_index' ELSE NULL END,
                    CASE WHEN $11 = 0 THEN 'sealed App Evidence Index 不含可发布条目'
                        ELSE NULL END,
                    CASE WHEN $11 = 0 THEN now() ELSE NULL END
                )
                ON CONFLICT ON CONSTRAINT uq_qc_app_publication_index_epoch
                DO UPDATE SET updated_at = existing.updated_at
                RETURNING id::text AS publication_id, input_hash, row_version,
                    expected_entry_count, status, (xmax = 0) AS created
                """,
                publication_id,
                task_id,
                source_agent_run_id,
                evidence_index_id,
                evidence_index_hash,
                str(source["ruleset_id"]),
                int(source["ruleset_version"]),
                ruleset_snapshot_hash,
                finalization_epoch,
                input_hash,
                len(entries),
                self._max_attempts,
            )
            if (
                row is None
                or str(row["input_hash"]) != input_hash
                or int(row["expected_entry_count"]) != len(entries)
            ):
                raise RuntimeError("App Publication 幂等身份已绑定不同输入")
            for item in entries:
                await connection.execute(
                    """
                    INSERT INTO qc_app_inspection_publication_entry (
                        publication_id, sequence_no, capture_id,
                        evidence_manifest_id, evidence_manifest_version,
                        semantic_role, page_type, page_state
                    )
                    VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7, $8)
                    ON CONFLICT (publication_id, sequence_no) DO NOTHING
                    """,
                    publication_id,
                    item.sequence_no,
                    item.capture_id,
                    item.evidence_manifest_id,
                    item.evidence_manifest_version,
                    item.semantic_role,
                    item.page_type,
                    item.page_state,
                )
            persisted_count = int(
                await connection.fetchval(
                    "SELECT count(*) FROM qc_app_inspection_publication_entry "
                    "WHERE publication_id = $1::uuid",
                    publication_id,
                )
                or 0
            )
            if persisted_count != len(entries):
                raise RuntimeError("App Publication Entry 未完整持久化")
            persisted_entries = await connection.fetch(
                """
                SELECT sequence_no, capture_id::text AS capture_id,
                    evidence_manifest_id, evidence_manifest_version,
                    semantic_role, page_type, page_state
                FROM qc_app_inspection_publication_entry
                WHERE publication_id = $1::uuid
                ORDER BY sequence_no
                """,
                publication_id,
            )
            normalized_persisted = tuple(
                (
                    int(item["sequence_no"]),
                    str(item["capture_id"]),
                    str(item["evidence_manifest_id"]),
                    int(item["evidence_manifest_version"]),
                    str(item["semantic_role"]),
                    _optional_text(item["page_type"]),
                    str(item["page_state"]),
                )
                for item in persisted_entries
            )
            if normalized_persisted != normalized_input:
                raise RuntimeError("App Publication 幂等 Entry 与封存输入不一致")
            if entries and str(row["status"]) == "pending":
                await self._insert_available_event(
                    connection,
                    publication_id=publication_id,
                    task_id=task_id,
                    evidence_index_id=evidence_index_id,
                    evidence_index_hash=evidence_index_hash,
                    finalization_epoch=finalization_epoch,
                    row_version=int(row["row_version"]),
                    input_hash=input_hash,
                )
            return AppInspectionPublicationWriteResult(
                publication_id=publication_id,
                created=bool(row["created"]),
                entry_count=len(entries),
            )

    async def claim(
        self,
        *,
        publication_id: str,
        task_id: str,
        message_version: int,
        lease_owner: str,
        lease_seconds: int,
    ) -> AppInspectionPublicationClaimResult:
        """按行版本领取整份 Index；Redis 内容不参与业务授权。"""

        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT *
                FROM qc_app_inspection_publication
                WHERE id = $1::uuid AND task_id = $2::uuid
                FOR UPDATE
                """,
                publication_id,
                task_id,
            )
            if row is None:
                return AppInspectionPublicationClaimResult(
                    disposition=AppInspectionPublicationClaimDisposition.STALE
                )
            status = str(row["status"])
            if status in _TERMINAL_STATUSES:
                return AppInspectionPublicationClaimResult(
                    disposition=AppInspectionPublicationClaimDisposition.COMPLETED
                )
            if int(row["row_version"]) != message_version:
                return AppInspectionPublicationClaimResult(
                    disposition=AppInspectionPublicationClaimDisposition.STALE
                )
            if status == "leased" and row["lease_expires_at"] > datetime.now(UTC):
                return AppInspectionPublicationClaimResult(
                    disposition=AppInspectionPublicationClaimDisposition.BUSY
                )
            if row["next_attempt_at"] > datetime.now(UTC):
                return AppInspectionPublicationClaimResult(
                    disposition=AppInspectionPublicationClaimDisposition.BUSY
                )
            if int(row["attempt_count"]) >= int(row["max_attempts"]):
                await connection.execute(
                    """
                    UPDATE qc_app_inspection_publication
                    SET status = 'failed', failure_code = 'attempts_exhausted',
                        failure_message = 'App Publication 尝试次数已耗尽',
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, finished_at = now(), updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    publication_id,
                )
                return AppInspectionPublicationClaimResult(
                    disposition=AppInspectionPublicationClaimDisposition.COMPLETED
                )
            lease_token = str(uuid4())
            claimed = await connection.fetchrow(
                """
                UPDATE qc_app_inspection_publication
                SET status = 'leased', lease_owner = $3, lease_token = $4::uuid,
                    lease_expires_at = now() + make_interval(secs => $5),
                    attempt_count = attempt_count + 1, updated_at = now()
                WHERE id = $1::uuid AND task_id = $2::uuid AND row_version = $6
                RETURNING *
                """,
                publication_id,
                task_id,
                lease_owner,
                lease_token,
                lease_seconds,
                message_version,
            )
            if claimed is None:
                return AppInspectionPublicationClaimResult(
                    disposition=AppInspectionPublicationClaimDisposition.STALE
                )
            entry_rows = await connection.fetch(
                """
                SELECT entry_id, sequence_no, capture_id::text AS capture_id,
                    page_type, page_state
                FROM qc_app_inspection_publication_entry
                WHERE publication_id = $1::uuid
                ORDER BY sequence_no
                """,
                publication_id,
            )
            if len(entry_rows) != int(claimed["expected_entry_count"]):
                raise RuntimeError("App Publication 领取时 Entry 数量不完整")
            lease = AppInspectionPublicationLease(
                publication_id=publication_id,
                task_id=task_id,
                source_agent_run_id=str(claimed["source_agent_run_id"]),
                evidence_index_id=str(claimed["evidence_index_id"]),
                evidence_index_hash=str(claimed["evidence_index_hash"]),
                ruleset_id=str(claimed["ruleset_id"]),
                ruleset_version=int(claimed["ruleset_version"]),
                ruleset_snapshot_hash=_optional_text(claimed["ruleset_snapshot_hash"]),
                finalization_epoch=int(claimed["finalization_epoch"]),
                lease_token=lease_token,
                row_version=int(claimed["row_version"]),
                attempt_count=int(claimed["attempt_count"]),
                max_attempts=int(claimed["max_attempts"]),
                entries=tuple(
                    AppInspectionPublicationEntryLease(
                        entry_id=int(item["entry_id"]),
                        sequence_no=int(item["sequence_no"]),
                        capture_id=str(item["capture_id"]),
                        page_type=_optional_text(item["page_type"]),
                        page_state=str(item["page_state"]),
                    )
                    for item in entry_rows
                ),
            )
            return AppInspectionPublicationClaimResult(
                disposition=AppInspectionPublicationClaimDisposition.CLAIMED,
                lease=lease,
            )

    async def complete(
        self,
        *,
        lease: AppInspectionPublicationLease,
        results: tuple[object, ...],
    ) -> None:
        """在一个事务内冻结全部规则成员、Work 关联与 Entry 终态。"""

        if len(results) != len(lease.entries):
            raise RuntimeError("App Publication 结果数量与 Entry 数量不一致")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{lease.task_id}",
            )
            identity = await connection.fetchrow(
                """
                SELECT publication.id
                FROM qc_app_inspection_publication AS publication
                INNER JOIN qc_task AS task ON task.id = publication.task_id
                INNER JOIN qc_ruleset AS ruleset ON ruleset.id = task.ruleset_id
                INNER JOIN qc_app_evidence_index AS index
                    ON index.index_id = publication.evidence_index_id
                   AND index.task_id = task.id
                   AND index.agent_run_id = publication.source_agent_run_id
                WHERE publication.id = $1::uuid
                  AND publication.task_id = $2::uuid
                  AND publication.lease_token = $3::uuid
                  AND publication.lease_expires_at > now()
                  AND publication.row_version = $4
                  AND publication.status = 'leased'
                  AND task.status = 'running'
                  AND task.finalization_epoch = publication.finalization_epoch
                  AND ruleset.id = publication.ruleset_id
                  AND ruleset.version = publication.ruleset_version
                  AND ruleset.snapshot_hash
                      IS NOT DISTINCT FROM publication.ruleset_snapshot_hash
                  AND index.status = 'sealed'
                  AND index.content_sha256 = publication.evidence_index_hash
                FOR UPDATE OF publication, task, ruleset, index
                """,
                lease.publication_id,
                lease.task_id,
                lease.lease_token,
                lease.row_version,
            )
            if identity is None:
                raise RuntimeError("App Publication 完成身份或租约围栏已变化")

            publication_needs_review = False
            total_linked_work_items = 0
            total_exclusions = 0
            total_rules = 0
            for entry, result in zip(lease.entries, results, strict=True):
                if str(getattr(result, "page_capture_id", "") or "") != entry.capture_id:
                    raise RuntimeError("App Publication 结果 Capture 身份不一致")
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
                if int(getattr(result, "published_count", 0) or 0) != len(commands):
                    raise RuntimeError("App Publication 准备批次数量不一致")
                if int(getattr(result, "excluded_active_session_rule_count", 0) or 0) != len(
                    excluded_rules
                ):
                    raise RuntimeError("App Publication active-session 排除数量不一致")
                status_text = str(getattr(result, "status", ""))
                if status_text == "out_of_scope":
                    if commands or excluded_rules:
                        raise RuntimeError("范围外 Capture 不允许发布或排除规则结论")
                    # prepare_only 不写 Capture；范围说明必须与整份发布处置同事务保存。
                    await connection.execute(
                        f"""UPDATE qc_capture AS capture
                           SET metadata={CAPTURE_INSPECTION_METADATA_MERGE_SQL},
                               updated_at=now()
                           WHERE id=$1::uuid AND task_id=$2::uuid""",
                        entry.capture_id,
                        lease.task_id,
                        json.dumps(build_capture_metadata_patch(
                            page_type=entry.page_type,
                            page_state=entry.page_state,
                            inspection_status=status_text,
                            published_count=0,
                        ), ensure_ascii=False),
                    )
                resolved_active_rules = await self._load_resolved_active_session_rules(
                    connection,
                    lease=lease,
                    entry=entry,
                    excluded_rules=excluded_rules,
                )

                linked_count = 0
                ordinal = 0
                linked_work_item_ids: set[str] = set()
                seen_batch_keys: set[str] = set()
                seen_rule_ids: set[str] = set()
                for command in commands:
                    self._validate_prepared_command(lease=lease, entry=entry, command=command)
                    if command.batch_key in seen_batch_keys:
                        raise RuntimeError("App Publication 同一 Entry 出现重复批次")
                    seen_batch_keys.add(command.batch_key)
                    command_with_wakeup = replace(command, defer_wakeup=False)
                    validate_inspection_work_item_publish_command(command_with_wakeup)
                    published = await publish_inspection_work_item_on_connection(
                        connection,
                        command=command_with_wakeup,
                        input_hash=build_inspection_work_item_input_hash(command_with_wakeup),
                    )
                    if published.work_item_id in linked_work_item_ids:
                        raise RuntimeError("App Publication 同一 Entry 重复关联 Work")
                    linked_work_item_ids.add(published.work_item_id)
                    linked = await connection.fetchval(
                        """
                        INSERT INTO qc_app_inspection_publication_work_item (
                            publication_id, entry_id, work_item_id, batch_key
                        ) VALUES ($1::uuid, $2, $3::uuid, $4)
                        ON CONFLICT (publication_id, work_item_id) DO NOTHING
                        RETURNING work_item_id::text
                        """,
                        lease.publication_id,
                        entry.entry_id,
                        published.work_item_id,
                        command.batch_key,
                    )
                    if linked is None:
                        replay_entry = await connection.fetchval(
                            """
                            SELECT entry_id
                            FROM qc_app_inspection_publication_work_item
                            WHERE publication_id = $1::uuid
                              AND work_item_id = $2::uuid
                              AND entry_id = $3
                              AND batch_key = $4
                            """,
                            lease.publication_id,
                            published.work_item_id,
                            entry.entry_id,
                            command.batch_key,
                        )
                        if replay_entry is None:
                            raise RuntimeError("App Publication Work 关联发生漂移")
                    linked_count += 1
                    for rule in command.rules:
                        if rule.rule_id in seen_rule_ids:
                            raise RuntimeError("App Publication 同一 Entry 出现重复规则")
                        seen_rule_ids.add(rule.rule_id)
                        await connection.execute(
                            """
                            INSERT INTO qc_app_inspection_publication_rule (
                                publication_id, entry_id, rule_id, rule_code,
                                ordinal, contract_hash, strategy, category,
                                session_dependency, disposition, work_item_id,
                                execution_plan
                            ) VALUES (
                                $1::uuid, $2, $3::uuid, $4, $5, $6, $7, $8,
                                'snapshot', 'linked', $9::uuid, $10::jsonb
                            )
                            ON CONFLICT (publication_id, entry_id, rule_id) DO UPDATE
                            SET work_item_id = EXCLUDED.work_item_id,
                                disposition = EXCLUDED.disposition
                            """,
                            lease.publication_id,
                            entry.entry_id,
                            rule.rule_id,
                            rule.rule_code,
                            ordinal,
                            rule.contract_hash,
                            command.strategy,
                            command.category,
                            published.work_item_id,
                            json.dumps(
                                {"batch_key": command.batch_key},
                                ensure_ascii=False,
                            ),
                        )
                        ordinal += 1
                        total_rules += 1
                unresolved_active_rule_count = 0
                for excluded in excluded_rules:
                    rule_id = str(excluded.get("rule_id") or "").strip()
                    rule_code = str(excluded.get("rule_code") or "").strip()
                    contract_hash = str(excluded.get("contract_hash") or "").strip()
                    if not rule_id or not rule_code:
                        raise RuntimeError("App Publication 排除规则身份不完整")
                    if rule_id in seen_rule_ids:
                        raise RuntimeError("App Publication 规则不得同时关联和排除")
                    seen_rule_ids.add(rule_id)
                    resolved = resolved_active_rules.get((rule_id, rule_code, contract_hash))
                    if resolved is not None:
                        work_item_id = str(resolved["work_item_id"])
                        batch_key = str(resolved["batch_key"])
                        if work_item_id not in linked_work_item_ids:
                            if batch_key in seen_batch_keys:
                                raise RuntimeError("App Publication 活会话 Work 批次身份发生冲突")
                            seen_batch_keys.add(batch_key)
                            linked_work_item_ids.add(work_item_id)
                            linked = await connection.fetchval(
                                """
                                INSERT INTO qc_app_inspection_publication_work_item (
                                    publication_id, entry_id, work_item_id, batch_key
                                ) VALUES ($1::uuid, $2, $3::uuid, $4)
                                ON CONFLICT (publication_id, work_item_id) DO NOTHING
                                RETURNING work_item_id::text
                                """,
                                lease.publication_id,
                                entry.entry_id,
                                work_item_id,
                                batch_key,
                            )
                            if linked is None:
                                replay_entry = await connection.fetchval(
                                    """
                                    SELECT entry_id
                                    FROM qc_app_inspection_publication_work_item
                                    WHERE publication_id = $1::uuid
                                      AND work_item_id = $2::uuid
                                      AND entry_id = $3
                                      AND batch_key = $4
                                    """,
                                    lease.publication_id,
                                    work_item_id,
                                    entry.entry_id,
                                    batch_key,
                                )
                                if replay_entry is None:
                                    raise RuntimeError("App Publication 活会话 Work 关联发生漂移")
                            linked_count += 1
                        await connection.execute(
                            """
                            INSERT INTO qc_app_inspection_publication_rule (
                                publication_id, entry_id, rule_id, rule_code,
                                ordinal, contract_hash, strategy, category,
                                session_dependency, disposition, work_item_id,
                                execution_plan
                            ) VALUES (
                                $1::uuid, $2, $3::uuid, $4, $5, $6, $7, $8,
                                'active_session', 'linked', $9::uuid, $10::jsonb
                            )
                            ON CONFLICT (publication_id, entry_id, rule_id) DO UPDATE
                            SET work_item_id = EXCLUDED.work_item_id,
                                disposition = EXCLUDED.disposition,
                                execution_plan = EXCLUDED.execution_plan
                            """,
                            lease.publication_id,
                            entry.entry_id,
                            rule_id,
                            rule_code,
                            ordinal,
                            contract_hash,
                            str(excluded.get("strategy") or ""),
                            _optional_text(excluded.get("category")),
                            work_item_id,
                            json.dumps(
                                {
                                    "batch_key": batch_key,
                                    "disposition": "linked",
                                    "reason": "app_active_session_rule_completed",
                                },
                                ensure_ascii=False,
                            ),
                        )
                        ordinal += 1
                        total_rules += 1
                        continue
                    unresolved_active_rule_count += 1
                    inserted = await connection.fetchval(
                        """
                        INSERT INTO qc_app_inspection_publication_rule (
                            publication_id, entry_id, rule_id, rule_code,
                            ordinal, contract_hash, strategy, category,
                            session_dependency, disposition, execution_plan
                        )
                        SELECT $1::uuid, $2, rule.id, rule.rule_code, $5, $6,
                            $7, $8, 'active_session', 'needs_review', $9::jsonb
                        FROM qc_rule AS rule
                        WHERE rule.id = $3::uuid
                          AND rule.ruleset_id = ANY(qc_ruleset_scope($10::uuid))
                          AND rule.rule_code = $4
                        ON CONFLICT (publication_id, entry_id, rule_id) DO UPDATE
                        SET disposition = 'needs_review', work_item_id = NULL
                        RETURNING rule_id::text
                        """,
                        lease.publication_id,
                        entry.entry_id,
                        rule_id,
                        rule_code,
                        ordinal,
                        contract_hash,
                        str(excluded.get("strategy") or ""),
                        _optional_text(excluded.get("category")),
                        json.dumps(
                            {
                                **dict(excluded.get("execution_plan") or {}),
                                "disposition": "needs_review",
                                "reason": "app_active_session_rule_unresolved_before_close",
                            },
                            ensure_ascii=False,
                        ),
                        lease.ruleset_id,
                    )
                    if inserted is None:
                        raise RuntimeError("App Publication 排除规则不属于锁定规则集")
                    ordinal += 1
                    total_rules += 1
                if status_text == "unmatched_page_type":
                    entry_status = "needs_review"
                    entry_failure_code = "page_type_unavailable"
                    entry_failure_message = "App Capture 页面类型不可识别，无法匹配候选规则"
                elif unresolved_active_rule_count > 0:
                    entry_status = "needs_review"
                    entry_failure_code = "active_session_rules_unresolved"
                    entry_failure_message = "App Session 关闭前仍有活会话规则未形成可信终态"
                elif commands or resolved_active_rules:
                    entry_status = "linked"
                    entry_failure_code = None
                    entry_failure_message = None
                elif status_text in {
                    "no_rules_applicable",
                    "out_of_scope",
                    "not_applicable",
                    "evidence_rules_deferred",
                }:
                    # 遮挡帧本身不可判底层业务；规则是否完成仍由冻结清单门禁核验。
                    entry_status = "not_applicable"
                    entry_failure_code = None
                    entry_failure_message = None
                else:
                    raise RuntimeError(f"App Publication 不支持的 Entry 发布结果: {status_text}")
                candidate_count = ordinal
                await connection.execute(
                    """
                    UPDATE qc_app_inspection_publication_entry
                    SET status = $3::varchar(32), candidate_rule_count = $4,
                        linked_work_item_count = $5,
                        explicit_exclusion_count = $6,
                        failure_code = $7,
                        failure_message = $8,
                        finished_at = now(), updated_at = now()
                    WHERE publication_id = $1::uuid AND entry_id = $2
                    """,
                    lease.publication_id,
                    entry.entry_id,
                    entry_status,
                    candidate_count,
                    linked_count,
                    unresolved_active_rule_count,
                    entry_failure_code,
                    entry_failure_message,
                )
                publication_needs_review = publication_needs_review or (
                    entry_status == "needs_review"
                )
                total_linked_work_items += linked_count
                total_exclusions += unresolved_active_rule_count

            terminal_status = (
                "needs_review"
                if publication_needs_review
                else ("published" if total_linked_work_items else "not_applicable")
            )
            counts = await connection.fetchrow(
                """
                SELECT
                    count(*)::integer AS terminal_entry_count,
                    COALESCE(sum(linked_work_item_count), 0)::integer AS work_count,
                    COALESCE(sum(explicit_exclusion_count), 0)::integer AS exclusion_count,
                    COALESCE(sum(candidate_rule_count), 0)::integer AS rule_count,
                    (
                        SELECT count(*)::integer
                        FROM qc_app_inspection_publication_work_item AS link
                        WHERE link.publication_id = $1::uuid
                    ) AS actual_work_count,
                    (
                        SELECT count(*)::integer
                        FROM qc_app_inspection_publication_rule AS member
                        WHERE member.publication_id = $1::uuid
                    ) AS actual_rule_count
                FROM qc_app_inspection_publication_entry
                WHERE publication_id = $1::uuid AND status <> 'pending'
                """,
                lease.publication_id,
            )
            if (
                counts is None
                or int(counts["terminal_entry_count"]) != len(lease.entries)
                or int(counts["work_count"]) != total_linked_work_items
                or int(counts["exclusion_count"]) != total_exclusions
                or int(counts["rule_count"]) != total_rules
                or int(counts["actual_work_count"]) != total_linked_work_items
                or int(counts["actual_rule_count"]) != total_rules
            ):
                raise RuntimeError("App Publication Entry/规则/Work 数量不一致")
            if total_linked_work_items:
                await self._ensure_consumer_run(
                    connection,
                    task_id=lease.task_id,
                    finalization_epoch=lease.finalization_epoch,
                )
            updated = await connection.fetchval(
                """
                UPDATE qc_app_inspection_publication
                SET status = $5::varchar(32), terminal_entry_count = $6,
                    expected_rule_count = $7, linked_work_item_count = $8,
                    explicit_exclusion_count = $9,
                    failure_code = CASE WHEN $5::varchar(32) = 'needs_review'
                        THEN 'entry_needs_review' ELSE NULL END,
                    failure_message = CASE WHEN $5::varchar(32) = 'needs_review'
                        THEN '至少一个 App Index Entry 需要人工复核' ELSE NULL END,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, row_version = row_version + 1,
                    finished_at = now(), updated_at = now()
                WHERE id = $1::uuid AND task_id = $2::uuid
                  AND lease_token = $3::uuid AND row_version = $4
                  AND lease_expires_at > now()
                  AND status = 'leased'
                RETURNING id::text
                """,
                lease.publication_id,
                lease.task_id,
                lease.lease_token,
                lease.row_version,
                terminal_status,
                len(lease.entries),
                total_rules,
                total_linked_work_items,
                total_exclusions,
            )
            if updated is None:
                raise RuntimeError("App Publication 完成写回被租约围栏拒绝")

    async def retry(
        self,
        *,
        lease: AppInspectionPublicationLease,
        error: Exception,
    ) -> None:
        """释放失败租约；未耗尽时创建新消息版本，耗尽时明确失败。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_app_inspection_publication
                SET status = CASE WHEN attempt_count >= max_attempts
                        THEN 'failed' ELSE 'retry_wait' END,
                    failure_code = CASE WHEN attempt_count >= max_attempts
                        THEN 'attempts_exhausted' ELSE 'publication_failed' END,
                    failure_message = $5,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    next_attempt_at = now() + make_interval(secs => $6),
                    row_version = row_version + 1,
                    finished_at = CASE WHEN attempt_count >= max_attempts
                        THEN now() ELSE NULL END,
                    updated_at = now()
                WHERE id = $1::uuid AND task_id = $2::uuid
                  AND lease_token = $3::uuid AND row_version = $4
                  AND status = 'leased'
                RETURNING status, row_version, input_hash, evidence_index_id,
                    evidence_index_hash, finalization_epoch
                """,
                lease.publication_id,
                lease.task_id,
                lease.lease_token,
                lease.row_version,
                str(error)[:2000],
                self._retry_delay_seconds,
            )
            if row is None:
                raise RuntimeError("App Publication 重试写回被租约围栏拒绝")
            # retry_wait 到期后由 reconcile 提升为新的 pending 版本并补 Outbox；
            # 这里不能提前发布尚不可领取的消息，否则 Consumer 会形成忙重试。

    async def list_unpublished_sealed_indexes(
        self,
        *,
        limit: int = 100,
        task_id: str | None = None,
        source_agent_run_id: str | None = None,
        finalization_epoch: int | None = None,
    ) -> tuple[AppUnpublishedSealedIndex, ...]:
        """只恢复已停止主 Run 的当前证据交接，不与正常主链竞争或解封历史任务。"""

        if limit <= 0:
            return ()
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT task.id::text AS task_id,
                    source_run.id::text AS source_agent_run_id,
                    task.finalization_epoch
                FROM qc_task AS task
                INNER JOIN qc_agent_run AS source_run
                    ON source_run.id = task.finalization_agent_run_id
                   AND source_run.task_id = task.id
                INNER JOIN qc_app_evidence_index AS index
                    ON index.task_id = task.id
                   AND index.agent_run_id = source_run.id
                INNER JOIN qc_surface_session AS surface_session
                    ON surface_session.task_id = task.id
                   AND surface_session.session_id = index.surface_session_id
                   AND surface_session.platform_build_ref = index.build_id
                   AND surface_session.finalization_epoch = task.finalization_epoch
                INNER JOIN qc_app_exploration_result AS result
                    ON result.task_id = task.id
                   AND result.agent_run_id = source_run.id
                   AND result.surface_session_id = index.surface_session_id
                   AND result.evidence_index_id = index.index_id
                   AND result.evidence_index_hash = index.content_sha256
                WHERE task.target_platform = 'app'
                  AND ($2::uuid IS NULL OR task.id = $2)
                  AND ($3::uuid IS NULL OR source_run.id = $3)
                  AND ($4::integer IS NULL OR task.finalization_epoch = $4)
                  AND task.deleted_at IS NULL
                  AND task.status = 'running'
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND task.task_config->>'execution_mode' = 'full_inspection'
                  AND source_run.status IN ('done', 'failed', 'aborted')
                  AND COALESCE(source_run.output_context->>'cancel_requested', 'false')
                      <> 'true'
                  AND index.status = 'sealed'
                  AND surface_session.status IN ('closed', 'lost', 'failed')
                  AND NOT EXISTS (
                      SELECT 1 FROM qc_app_inspection_publication AS publication
                      WHERE publication.task_id = task.id
                        AND publication.source_agent_run_id = source_run.id
                        AND publication.finalization_epoch = task.finalization_epoch
                        AND publication.evidence_index_id = index.index_id
                  )
                ORDER BY result.completed_at, task.id
                LIMIT $1
                """,
                limit,
                task_id,
                source_agent_run_id,
                finalization_epoch,
            )
        return tuple(
            AppUnpublishedSealedIndex(
                task_id=str(row["task_id"]),
                source_agent_run_id=str(row["source_agent_run_id"]),
                finalization_epoch=int(row["finalization_epoch"]),
            )
            for row in rows
        )

    async def reconcile(self, *, limit: int = 100) -> int:
        """恢复 Publication，并补偿曾在缺少消费 Run 时被确认的 Work 唤醒。"""

        if limit <= 0:
            return 0
        pool = await self._get_pool()
        recovered = 0
        async with pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT id
                    FROM qc_app_inspection_publication
                    WHERE (
                        status = 'leased' AND lease_expires_at <= now()
                    ) OR (
                        status = 'retry_wait' AND next_attempt_at <= now()
                    )
                    ORDER BY updated_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                UPDATE qc_app_inspection_publication AS publication
                SET status = CASE WHEN publication.attempt_count >= publication.max_attempts
                        THEN 'failed' ELSE 'pending' END,
                    failure_code = CASE WHEN publication.attempt_count >= publication.max_attempts
                        THEN 'attempts_exhausted' ELSE publication.failure_code END,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    row_version = publication.row_version + 1,
                    finished_at = CASE WHEN publication.attempt_count >= publication.max_attempts
                        THEN now() ELSE NULL END,
                    updated_at = now()
                FROM candidates
                WHERE publication.id = candidates.id
                RETURNING publication.id::text AS publication_id,
                    publication.task_id::text AS task_id,
                    publication.evidence_index_id, publication.evidence_index_hash,
                    publication.finalization_epoch, publication.row_version,
                    publication.input_hash, publication.status
                """,
                limit,
            )
            for row in rows:
                if str(row["status"]) != "pending":
                    continue
                await self._insert_available_event(
                    connection,
                    publication_id=str(row["publication_id"]),
                    task_id=str(row["task_id"]),
                    evidence_index_id=str(row["evidence_index_id"]),
                    evidence_index_hash=str(row["evidence_index_hash"]),
                    finalization_epoch=int(row["finalization_epoch"]),
                    row_version=int(row["row_version"]),
                    input_hash=str(row["input_hash"]),
                )
                recovered += 1
            recovered += await self._recover_completed_wakeup_work(
                connection,
                limit=max(0, limit - recovered),
            )
            await self._reconcile_consumer_run_lifecycle(
                connection,
                limit=limit,
            )
        return recovered

    async def _reconcile_consumer_run_lifecycle(
        self,
        connection: Any,
        *,
        limit: int,
    ) -> int:
        """周期关闭消息与关联 Work 已全部排空的 App 逻辑消费 Run。"""

        rows = await connection.fetch(
            """
            SELECT consumer.id::text AS consumer_agent_run_id,
                consumer.task_id::text AS task_id
            FROM qc_agent_run AS consumer
            INNER JOIN qc_task AS task ON task.id = consumer.task_id
            WHERE consumer.agent_type = 'rule_judge'
              AND consumer.status = 'running'
              AND consumer.input_context->>'source' = 'app_inspection_publication'
              AND (consumer.input_context->>'finalization_epoch')::integer
                  = task.finalization_epoch
            ORDER BY consumer.created_at, consumer.id
            FOR UPDATE OF consumer SKIP LOCKED
            LIMIT $1
            """,
            limit,
        )
        closed = 0
        for row in rows:
            closed += int(
                await finish_app_consumer_run_if_idle_on_connection(
                    connection,
                    task_id=str(row["task_id"]),
                    consumer_agent_run_id=str(row["consumer_agent_run_id"]),
                )
            )
        return closed

    async def _ensure_consumer_run(
        self,
        connection: Any,
        *,
        task_id: str,
        finalization_epoch: int,
    ) -> str:
        """幂等创建当前任务 epoch 的持久化 App 规则消费 Run。"""

        logical_identity = f"app-inspection-consumer:v2:{task_id}:{finalization_epoch}"
        consumer_agent_run_id = str(uuid5(NAMESPACE_URL, f"alioth:{logical_identity}"))
        value = await connection.fetchval(
            """
            INSERT INTO qc_agent_run AS existing (
                id, task_id, agent_type, agent_name, status,
                input_context, loop_guard, started_at
            )
            SELECT
                $1::uuid, task.id, 'rule_judge', 'App Inspection Consumer', 'running',
                jsonb_build_object(
                    'logical_identity', $3::text,
                    'finalization_epoch', $4::integer,
                    'source', 'app_inspection_publication'
                ),
                '{}'::jsonb,
                now()
            FROM qc_task AS task
            WHERE task.id = $2::uuid
              AND task.status = 'running'
              AND task.finalization_epoch = $4::integer
            ON CONFLICT (id) DO UPDATE
            SET updated_at = existing.updated_at
            WHERE existing.task_id = EXCLUDED.task_id
              AND existing.agent_type = 'rule_judge'
              AND existing.status = 'running'
              AND existing.input_context->>'logical_identity' = $3::text
            RETURNING id::text
            """,
            consumer_agent_run_id,
            task_id,
            logical_identity,
            finalization_epoch,
        )
        if value is None:
            raise RuntimeError("App Publication 逻辑消费 Run 身份冲突")
        return str(value)

    async def _recover_completed_wakeup_work(
        self,
        connection: Any,
        *,
        limit: int,
    ) -> int:
        """一次性补投 row_version=0 且曾因缺少消费 Run 被 ACK 的 App Work。"""

        if limit <= 0:
            return 0
        rows = await connection.fetch(
            """
            WITH candidates AS (
                SELECT work_item.id
                FROM qc_task AS task
                INNER JOIN qc_app_inspection_publication AS publication
                    ON publication.task_id = task.id
                   AND publication.finalization_epoch = task.finalization_epoch
                INNER JOIN qc_app_inspection_publication_work_item AS link
                    ON link.publication_id = publication.id
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = link.work_item_id
                WHERE task.status = 'running'
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND publication.status IN ('published', 'needs_review')
                  AND work_item.control_mode = 'enforced'
                  AND work_item.finalization_epoch = task.finalization_epoch
                  AND work_item.status = 'pending'
                  AND work_item.row_version = 0
                  AND work_item.consumer_agent_run_id IS NULL
                  AND work_item.attempt_count < work_item.max_attempts
                  AND EXISTS (
                      SELECT 1
                      FROM qc_outbox_event AS outbox
                      INNER JOIN qc_event_inbox AS inbox
                          ON inbox.message_id = outbox.message_id
                         AND inbox.task_id = outbox.task_id
                         AND inbox.aggregate_type = outbox.aggregate_type
                         AND inbox.aggregate_id = outbox.aggregate_id
                         AND inbox.aggregate_version = outbox.aggregate_version
                      WHERE outbox.task_id = task.id
                        AND outbox.aggregate_type = 'inspection_work_item'
                        AND outbox.aggregate_id = work_item.id::text
                        AND outbox.aggregate_version = work_item.row_version
                        AND outbox.message_type = 'alioth.v2.inspection-work.available'
                        AND outbox.status = 'published'
                        AND inbox.status = 'completed'
                  )
                ORDER BY work_item.updated_at, work_item.id
                FOR UPDATE OF work_item SKIP LOCKED
                LIMIT $1
            )
            UPDATE qc_inspection_work_item AS work_item
            SET next_attempt_at = now(),
                row_version = work_item.row_version + 1,
                updated_at = now()
            FROM candidates
            WHERE work_item.id = candidates.id
            RETURNING
                work_item.id::text,
                work_item.task_id::text,
                work_item.page_capture_id::text,
                work_item.protocol_version,
                work_item.batch_key,
                work_item.strategy,
                work_item.category,
                work_item.session_dependency,
                work_item.input_hash,
                work_item.control_mode,
                work_item.row_version,
                work_item.finalization_epoch
            """,
            limit,
        )
        for task_id, finalization_epoch in {
            (str(row["task_id"]), int(row["finalization_epoch"])) for row in rows
        }:
            await self._ensure_consumer_run(
                connection,
                task_id=task_id,
                finalization_epoch=finalization_epoch,
            )
        for row in rows:
            await insert_outbox_event(
                connection,
                aggregate_type="inspection_work_item",
                aggregate_id=str(row["id"]),
                envelope=build_protocol_envelope(
                    message_type=INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
                    payload_protocol_version=str(row["protocol_version"]),
                    idempotency_key=(f"app-consumer-recovery:{row['id']}:{row['row_version']}"),
                    task_id=str(row["task_id"]),
                    aggregate_type="inspection_work_item",
                    aggregate_id=str(row["id"]),
                    aggregate_version=int(row["row_version"]),
                    occurred_at=datetime.now(UTC),
                    payload={
                        "protocol_version": str(row["protocol_version"]),
                        "work_item_id": str(row["id"]),
                        "task_id": str(row["task_id"]),
                        "page_capture_id": str(row["page_capture_id"]),
                        "batch_key": str(row["batch_key"]),
                        "strategy": str(row["strategy"]),
                        "category": row["category"],
                        "session_dependency": row["session_dependency"],
                        "control_mode": str(row["control_mode"]),
                        "input_hash": str(row["input_hash"]),
                        "reopen_reason": "app_publication_consumer_missing",
                    },
                    correlation_id=str(row["task_id"]),
                ),
            )
        return len(rows)

    async def await_task_terminal(
        self,
        *,
        task_id: str,
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 0.2,
    ) -> AppInspectionPublicationTaskSummary:
        """等待当前 epoch 的整份 Publication 进入明确终态。"""

        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("等待 Publication 的超时与轮询间隔必须大于 0")
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            summary = await self.task_summary(task_id=task_id)
            if summary.total_count > 0 and summary.nonterminal_count == 0:
                return summary
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("等待 App Publication 终态超时")
            await asyncio.sleep(poll_interval_seconds)

    async def task_summary(
        self,
        *,
        task_id: str,
    ) -> AppInspectionPublicationTaskSummary:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT count(publication.id)::integer AS total_count,
                    count(publication.id) FILTER (WHERE publication.status IN (
                        'pending','leased','retry_wait'
                    ))::integer
                        AS nonterminal_count,
                    COALESCE(sum(publication.expected_entry_count), 0)::integer
                        AS expected_entry_count,
                    COALESCE(sum(publication.terminal_entry_count), 0)::integer
                        AS terminal_entry_count,
                    COALESCE(sum(publication.linked_work_item_count), 0)::integer
                        AS linked_work_item_count,
                    COALESCE(sum(publication.explicit_exclusion_count), 0)::integer
                        AS explicit_exclusion_count,
                    count(publication.id) FILTER (
                        WHERE publication.status = 'needs_review'
                    )::integer
                        AS needs_review_count,
                    count(publication.id) FILTER (
                        WHERE publication.status = 'failed'
                    )::integer AS failed_count
                FROM qc_task AS task
                LEFT JOIN qc_app_inspection_publication AS publication
                    ON publication.task_id = task.id
                   AND publication.finalization_epoch = task.finalization_epoch
                WHERE task.id = $1::uuid
                """,
                task_id,
            )
        if row is None:
            raise RuntimeError("等待 App Publication 时任务不存在")
        return AppInspectionPublicationTaskSummary(
            total_count=int(row["total_count"]),
            nonterminal_count=int(row["nonterminal_count"]),
            expected_entry_count=int(row["expected_entry_count"]),
            terminal_entry_count=int(row["terminal_entry_count"]),
            linked_work_item_count=int(row["linked_work_item_count"]),
            explicit_exclusion_count=int(row["explicit_exclusion_count"]),
            needs_review_count=int(row["needs_review_count"]),
            failed_count=int(row["failed_count"]),
        )

    @staticmethod
    async def _load_resolved_active_session_rules(
        connection: Any,
        *,
        lease: AppInspectionPublicationLease,
        entry: AppInspectionPublicationEntryLease,
        excluded_rules: tuple[dict[str, Any], ...],
    ) -> dict[tuple[str, str, str], dict[str, str]]:
        """读取当前 Capture 已在 Session 存活期完成的规则与 Work 身份。"""

        if not excluded_rules:
            return {}
        expected = {
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
              AND work_item.status IN ('succeeded', 'partial')
              AND member.status IN (
                  'written', 'evidence_insufficient', 'error', 'skipped'
              )
            ORDER BY work_item.batch_key, work_item.id, member.ordinal
            FOR UPDATE OF work_item, member
            """,
            lease.task_id,
            entry.capture_id,
            lease.source_agent_run_id,
            lease.ruleset_id,
            lease.ruleset_version,
            lease.ruleset_snapshot_hash,
            lease.finalization_epoch,
        )
        resolved: dict[tuple[str, str, str], dict[str, str]] = {}
        for row in rows:
            key = (
                str(row["rule_id"]),
                str(row["rule_code"]),
                str(row["contract_hash"]),
            )
            if key not in expected:
                continue
            identity = {
                "work_item_id": str(row["work_item_id"]),
                "batch_key": str(row["batch_key"]),
            }
            existing = resolved.get(key)
            if existing is not None and existing != identity:
                raise RuntimeError("App active-session 规则出现多个可信 Work 终态")
            resolved[key] = identity
        return resolved

    def _validate_prepared_command(
        self,
        *,
        lease: AppInspectionPublicationLease,
        entry: AppInspectionPublicationEntryLease,
        command: PublishInspectionWorkItemCommand,
    ) -> None:
        if (
            command.task_id != lease.task_id
            or command.page_capture_id != entry.capture_id
            or command.producer_agent_run_id != lease.source_agent_run_id
            or command.ruleset_id != lease.ruleset_id
            or command.ruleset_version != lease.ruleset_version
            or command.ruleset_snapshot_hash != lease.ruleset_snapshot_hash
            or command.session_dependency != "snapshot"
            or command.control_mode != "enforced"
            or not command.defer_wakeup
        ):
            raise RuntimeError("App Publication 准备命令与冻结身份不一致")

    async def _insert_available_event(
        self,
        connection: Any,
        *,
        publication_id: str,
        task_id: str,
        evidence_index_id: str,
        evidence_index_hash: str,
        finalization_epoch: int,
        row_version: int,
        input_hash: str,
    ) -> None:
        await insert_outbox_event(
            connection,
            aggregate_type="app_inspection_publication",
            aggregate_id=publication_id,
            envelope=build_app_inspection_publication_available(
                publication_id=publication_id,
                task_id=task_id,
                evidence_index_id=evidence_index_id,
                evidence_index_hash=evidence_index_hash,
                finalization_epoch=finalization_epoch,
                row_version=row_version,
                input_hash=input_hash,
                occurred_at=datetime.now(UTC),
            ),
        )

    async def _get_pool(self) -> Any:
        return self._pool or await get_postgres_pool(self._database_url)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
