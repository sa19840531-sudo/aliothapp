"""聚合 Work 的 PostgreSQL 领取、证据读取与原子判定落库。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from services.api.app.application.aggregate_inspection_execution import (
    AggregateExecutionLease,
    AggregateJudgeContext,
    AggregateJudgement,
    AggregateWakeupClaim,
    AggregateWakeupClaimDisposition,
)
from services.api.app.domain.evidence import EvidenceInputSet
from services.api.app.domain.inspection import build_aggregate_inspection_work_available
from services.api.app.infrastructure.db.aggregate_rule_inventory import read_frozen_aggregate_rule
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.pool import get_postgres_pool

_TERMINAL_WORK_STATUSES = {
    "succeeded",
    "evidence_insufficient",
    "failed",
    "cancelled",
    "superseded",
}


class AggregateInspectionExecutionPersistenceError(RuntimeError):
    """聚合租约、输入闭包或结果事务已发生漂移。"""


class PostgresAggregateInspectionExecutionRepository:
    """Redis 只负责唤醒，领取与判定结果始终以 PostgreSQL 为准。"""

    def __init__(
        self, *, database_url: str | None = None, pool: Any | None = None, snapshot_reader=None,
        artifact_store=None,
    ) -> None:
        self._database_url = database_url
        self._snapshot_reader = snapshot_reader
        self._pool = pool
        self._artifact_store = artifact_store

    async def claim_for_wakeup(
        self,
        *,
        work_item_id: str,
        task_id: str,
        message_version: int,
        lease_owner: str,
        lease_seconds: int,
    ) -> AggregateWakeupClaim:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT work_item.*,
                    work_item.producer_agent_run_id::text AS producer_run_text,
                    work_item.rule_id::text AS rule_id_text
                FROM qc_aggregate_inspection_work_item AS work_item
                WHERE work_item.id = $1::uuid
                  AND work_item.task_id = $2::uuid
                FOR UPDATE
                """,
                work_item_id,
                task_id,
            )
            if row is None:
                return AggregateWakeupClaim(disposition=AggregateWakeupClaimDisposition.STALE)
            status = str(row["status"])
            if status in _TERMINAL_WORK_STATUSES:
                return AggregateWakeupClaim(disposition=AggregateWakeupClaimDisposition.COMPLETED)
            if int(row["row_version"]) != message_version:
                return AggregateWakeupClaim(disposition=AggregateWakeupClaimDisposition.STALE)
            now = datetime.now(UTC)
            if status in {"leased", "running"} and row["lease_expires_at"] > now:
                return AggregateWakeupClaim(disposition=AggregateWakeupClaimDisposition.BUSY)
            if status == "retry_wait" and row["next_attempt_at"] > now:
                return AggregateWakeupClaim(disposition=AggregateWakeupClaimDisposition.BUSY)
            if int(row["attempt_count"]) >= int(row["max_attempts"]):
                await _fail_exhausted_work(connection, row=row)
                return AggregateWakeupClaim(disposition=AggregateWakeupClaimDisposition.COMPLETED)
            lease_token = str(uuid4())
            claimed = await connection.fetchrow(
                """
                UPDATE qc_aggregate_inspection_work_item AS work_item
                SET status = 'leased',
                    consumer_agent_run_id = work_item.producer_agent_run_id,
                    lease_owner = $4,
                    lease_token = $5::uuid,
                    lease_expires_at = now() + make_interval(secs => $6),
                    attempt_count = work_item.attempt_count + 1,
                    row_version = work_item.row_version + 1,
                    started_at = COALESCE(work_item.started_at, now()),
                    last_error_code = NULL,
                    last_error_message = NULL,
                    updated_at = now()
                WHERE work_item.id = $1::uuid
                  AND work_item.task_id = $2::uuid
                  AND work_item.row_version = $3
                  AND EXISTS (
                      SELECT 1 FROM qc_task AS task
                      WHERE task.id = work_item.task_id
                        AND (
                            task.status = 'running'
                            OR (
                                task.status = 'review'
                                AND task.target_platform IN ('h5', 'app')
                            )
                        )
                        AND task.finalization_state = 'session_draining'
                        AND task.finalization_agent_run_id =
                            work_item.producer_agent_run_id
                        AND task.finalization_epoch = work_item.finalization_epoch
                  )
                RETURNING work_item.*,
                    work_item.consumer_agent_run_id::text AS consumer_run_text,
                    work_item.rule_id::text AS rule_id_text
                """,
                work_item_id,
                task_id,
                message_version,
                lease_owner,
                lease_token,
                lease_seconds,
            )
            if claimed is None:
                return AggregateWakeupClaim(disposition=AggregateWakeupClaimDisposition.STALE)
            await connection.execute(
                """
                UPDATE qc_aggregate_inspection_attempt
                SET status = 'abandoned',
                    error_code = COALESCE(
                        error_code, 'AGGREGATE_ATTEMPT_LEASE_RECLAIMED'
                    ),
                    error_message = COALESCE(
                        error_message, '聚合工作旧租约已被新消费者接管'
                    ),
                    finished_at = COALESCE(finished_at, now()),
                    updated_at = now()
                WHERE work_item_id = $1::uuid
                  AND attempt_no < $2
                  AND status = 'running'
                """,
                work_item_id,
                int(claimed["attempt_count"]),
            )
            attempt_id = await connection.fetchval(
                """
                INSERT INTO qc_aggregate_inspection_attempt (
                    task_id, work_item_id, consumer_agent_run_id, attempt_no,
                    lease_token, input_hash
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4, $5::uuid, $6
                )
                ON CONFLICT ON CONSTRAINT uq_qc_aggregate_attempt_number
                DO UPDATE SET updated_at = qc_aggregate_inspection_attempt.updated_at
                RETURNING id::text
                """,
                task_id,
                work_item_id,
                str(claimed["consumer_run_text"]),
                int(claimed["attempt_count"]),
                lease_token,
                str(claimed["input_hash"]),
            )
            if attempt_id is None:
                raise AggregateInspectionExecutionPersistenceError("聚合 Attempt 创建失败")
        return AggregateWakeupClaim(
            disposition=AggregateWakeupClaimDisposition.CLAIMED,
            lease=_lease_from_row(claimed),
        )

    async def load_judge_context(
        self,
        *,
        lease: AggregateExecutionLease,
    ) -> AggregateJudgeContext:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT input_set.input_set_payload
                FROM qc_aggregate_inspection_work_item AS work_item
                INNER JOIN qc_evidence_input_set AS input_set
                    ON input_set.task_id = work_item.task_id
                   AND input_set.input_set_id = work_item.evidence_input_set_id
                WHERE work_item.id = $1::uuid
                  AND work_item.task_id = $2::uuid
                  AND work_item.evidence_input_set_id = $3
                  AND work_item.input_hash = $4
                  AND work_item.rule_id = $5::uuid
                  AND work_item.rule_contract_hash = $6
                  AND work_item.finalization_epoch = $7
                  AND work_item.consumer_agent_run_id = $8::uuid
                  AND work_item.lease_token = $9::uuid
                  AND work_item.row_version = $10
                  AND work_item.status = 'leased'
                  AND work_item.lease_expires_at > now()
                """,
                lease.work_item_id,
                lease.task_id,
                lease.evidence_input_set_id,
                lease.input_hash,
                lease.rule_id,
                lease.rule_contract_hash,
                lease.finalization_epoch,
                lease.consumer_agent_run_id,
                lease.lease_token,
                lease.row_version,
            )
            if row is None:
                raise AggregateInspectionExecutionPersistenceError(
                    "聚合 Work 租约或 EvidenceInputSet 身份已变化"
                )
            input_set = EvidenceInputSet.from_dict(_json_mapping(row["input_set_payload"]))
            if input_set.input_set_hash != lease.input_hash:
                raise AggregateInspectionExecutionPersistenceError(
                    "EvidenceInputSet hash 与 Work input_hash 不一致"
                )
            packet_ids = tuple(
                dict.fromkeys(
                    packet_id
                    for member in input_set.members
                    for packet_id in member.observation_packet_ids
                )
            )
            packet_rows = (
                await connection.fetch(
                    """
                    SELECT id::text AS packet_id, packet_payload
                    FROM qc_observation_packet
                    WHERE task_id = $1::uuid
                      AND id = ANY($2::uuid[])
                      AND quality_gate_status = 'pass'
                    """,
                    lease.task_id,
                    list(packet_ids),
                )
                if packet_ids
                else ()
            )
            # 成员清单封存于捕获提交时，彼时判定包尚未产生（包引用结构性
            # 为空）；按 capture 兜底装载该捕获最新 PASS 观察包供聚合策略
            # 读取 capture 级事实（快照、动作日志），不改变可信证据边界。
            fallback_capture_ids = list(
                dict.fromkeys(
                    member.capture_id
                    for member in input_set.members
                    if not member.observation_packet_ids
                )
            )
            capture_packet_rows = (
                await connection.fetch(
                    """
                    SELECT DISTINCT ON (page_capture_id)
                        page_capture_id::text AS capture_id,
                        packet_payload
                    FROM qc_observation_packet
                    WHERE task_id = $1::uuid
                      AND page_capture_id = ANY($2::uuid[])
                      AND quality_gate_status = 'pass'
                    ORDER BY page_capture_id, created_at DESC
                    """,
                    lease.task_id,
                    fallback_capture_ids,
                )
                if fallback_capture_ids
                else ()
            )
            green_evidence = None
            frozen_green_rule = {}
            if lease.scope_key in {"green_interaction_stability.v1", "green_temporal_rules.v1"}:
                from services.api.app.infrastructure.db.green_process_evidence import (
                    load_green_process_evidence,
                )

                async with connection.transaction(isolation="repeatable_read", readonly=True):
                    frozen_green_rule = await read_frozen_aggregate_rule(
                        connection, task_id=lease.task_id, rule_id=lease.rule_id,
                        rule_code=lease.rule_code, contract_hash=lease.rule_contract_hash,
                        scope=lease.scope, scope_key=lease.scope_key,
                        source_agent_run_id=input_set.source_agent_run_id,
                        finalization_epoch=lease.finalization_epoch,
                        ruleset_snapshot_hash=input_set.ruleset_snapshot_hash,
                    )
                    if frozen_green_rule is None:
                        raise AggregateInspectionExecutionPersistenceError(
                            "绿色过程缺少正式冻结规则"
                        )
                    green_evidence = await load_green_process_evidence(
                        connection, input_set=input_set,
                        artifact_store=self._green_artifact_store(),
                    )
            journey_contract = None
            if lease.scope == "journey" and lease.scope_key == "journey_observation":
                from services.api.app.infrastructure.db.journey_input_repository import (
                    load_journey_judge_input,
                )

                journey_contract = await load_journey_judge_input(connection, input_set=input_set)
        packets = {
            str(item["packet_id"]): _json_mapping(item["packet_payload"]) for item in packet_rows
        }
        if set(packets) != set(packet_ids):
            raise AggregateInspectionExecutionPersistenceError(
                "EvidenceInputSet 引用的 PASS Observation Packet 不完整"
            )
        capture_packets = {
            str(item["capture_id"]): _json_mapping(item["packet_payload"])
            for item in capture_packet_rows
        }
        journey_input = None
        if journey_contract is not None:
            from services.api.app.application.h5_checkpoint_observation import (
                checkpoint_needs_series,
            )
            from services.api.app.domain.journey.dsl import parse_journey_execution_plan
            from services.api.app.domain.journey.rule_selection import contract_checkpoints
            from services.api.app.infrastructure.browser.ui_snapshot_reader import (
                PostgresTrustedUiSnapshotReader,
            )

            reader = self._snapshot_reader or PostgresTrustedUiSnapshotReader(pool=pool)
            plan = parse_journey_execution_plan(journey_contract["plan"])
            observations = {}
            for checkpoint, frozen in zip(
                contract_checkpoints(journey_contract, plan),
                journey_contract["checkpoints"], strict=True,
            ):
                captures = [item["capture_id"] for item in frozen["bindings"]]
                snapshots = [
                    await reader.read_ui_snapshot(task_id=lease.task_id, page_capture_id=capture)
                    for capture in captures
                ]
                samples = None
                if checkpoint_needs_series(checkpoint):
                    samples = await reader.read_checkpoint_observation(
                        task_id=lease.task_id, page_capture_id=captures[-1]
                    )
                observations[checkpoint.checkpoint_id] = {
                    "before": dict(snapshots[0]),
                    "after": dict(snapshots[1]),
                    "series": (samples or {}).get("samples", ()),
                }
            journey_input = {"contract": journey_contract, "observations": observations}
        return AggregateJudgeContext(
            lease=lease,
            input_set=input_set,
            observation_packets=packets,
            capture_packets=capture_packets,
            journey_input=journey_input,
            rule_payload=frozen_green_rule,
            green_evidence=green_evidence,
        )

    async def complete_judgement(
        self,
        *,
        context: AggregateJudgeContext,
        judgement: AggregateJudgement,
    ) -> None:
        lease = context.lease
        quality_gate_status = (
            "fail" if judgement.execution_status == "evidence_insufficient" else "pass"
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            work = await _lock_valid_lease(connection, lease=lease)
            attempt = await _lock_running_attempt(connection, lease=lease)
            frozen_rule = await read_frozen_aggregate_rule(
                connection, task_id=lease.task_id, rule_id=lease.rule_id,
                rule_code=lease.rule_code, contract_hash=lease.rule_contract_hash,
                scope=lease.scope, scope_key=lease.scope_key,
                source_agent_run_id=context.input_set.source_agent_run_id,
                finalization_epoch=lease.finalization_epoch,
                ruleset_snapshot_hash=context.input_set.ruleset_snapshot_hash,
            )
            if lease.scope_key in {"green_interaction_stability.v1", "green_temporal_rules.v1"}:
                from services.api.app.infrastructure.db.green_process_evidence import (
                    load_green_process_evidence,
                )

                if frozen_rule is None or frozen_rule != context.rule_payload:
                    raise AggregateInspectionExecutionPersistenceError("绿色规则冻结合同已变化")
                refreshed = await load_green_process_evidence(
                    connection, input_set=context.input_set,
                    artifact_store=self._green_artifact_store(),
                )
                if context.green_evidence is None or (
                    refreshed.fingerprint != context.green_evidence.fingerprint
                ):
                    raise AggregateInspectionExecutionPersistenceError(
                        "绿色判定原件或正式动作已变化"
                    )
            if frozen_rule is not None and (
                str(work["producer_agent_run_id"]) != context.input_set.source_agent_run_id
                or context.input_set.input_set_id != lease.evidence_input_set_id
                or context.input_set.input_set_hash != lease.input_hash
                or context.input_set.task_id != lease.task_id
                or context.input_set.rule_id != lease.rule_id
                or context.input_set.finalization_epoch != lease.finalization_epoch
                or any(
                    str(work[field]) != str(getattr(lease, field))
                    for field in (
                        "rule_id", "rule_code", "rule_contract_hash", "scope", "scope_key",
                        "finalization_epoch", "evidence_input_set_id", "input_hash",
                    )
                )
            ):
                raise AggregateInspectionExecutionPersistenceError(
                    "冻结聚合判定输入与 Work 身份不匹配"
                )
            packet_row = await connection.fetchrow(
                """
                INSERT INTO qc_observation_packet AS packet (
                    task_id, page_capture_id, rule_id, evidence_artifact_ids,
                    packet_payload, quality_gate_status, quality_gate_reason,
                    created_by_agent_run_id, evidence_input_set_id,
                    aggregate_work_item_id
                ) VALUES (
                    $1::uuid, NULL, $2::uuid, $3::jsonb,
                    $4::jsonb, $5, $6,
                    $7::uuid, $8, $9::uuid
                )
                ON CONFLICT (aggregate_work_item_id)
                    WHERE aggregate_work_item_id IS NOT NULL
                DO UPDATE SET updated_at = packet.updated_at
                RETURNING
                    packet.id::text AS packet_id,
                    packet.evidence_artifact_ids::text AS evidence_artifact_ids,
                    packet.packet_payload::text AS packet_payload,
                    packet.quality_gate_status AS quality_gate_status,
                    packet.quality_gate_reason AS quality_gate_reason
                """,
                lease.task_id,
                lease.rule_id,
                _json_dumps(list(judgement.evidence_artifact_ids)),
                _json_dumps(dict(judgement.wrapper_payload)),
                quality_gate_status,
                judgement.problem_desc,
                lease.consumer_agent_run_id,
                lease.evidence_input_set_id,
                lease.work_item_id,
            )
            if packet_row is None:
                raise AggregateInspectionExecutionPersistenceError(
                    "聚合 Observation Packet 写入失败"
                )
            _ensure_frozen_aggregate_packet_matches(
                row=packet_row,
                evidence_artifact_ids=list(judgement.evidence_artifact_ids),
                packet_payload=dict(judgement.wrapper_payload),
                quality_gate_status=quality_gate_status,
                quality_gate_reason=judgement.problem_desc,
            )
            packet_id = str(packet_row["packet_id"])
            judge_input_payload = {
                "evidence_input_set_id": lease.evidence_input_set_id,
                "input_hash": lease.input_hash,
                "scope": lease.scope,
                "scope_key": lease.scope_key,
            }
            judge_output_payload = judgement.to_result_payload()
            execution_args = [
                lease.work_item_id, packet_id, judgement.execution_status,
                judgement.confidence, judgement.result_summary, judgement.problem_desc,
                _json_dumps(list(judgement.evidence_artifact_ids)),
                _json_dumps(judge_input_payload), _json_dumps(judge_output_payload),
                judgement.review_status, judgement.review_reason, judgement.error_code,
                str(attempt["id"]),
            ]
            executor_sql, severity_sql = "rule.executor_type", "rule.severity"
            if frozen_rule is not None:
                executor_sql, severity_sql = "$14::varchar", "$15::varchar"
                execution_args.extend([frozen_rule["executor_type"], frozen_rule["severity"]])
            execution_row = await connection.fetchrow(
                f"""
                INSERT INTO qc_rule_execution AS execution (
                    task_id, rule_id, page_capture_id, observation_packet_id,
                    agent_run_id, execution_status, judge_strategy, confidence,
                    severity, result_summary, problem_desc,
                    evidence_artifact_ids, judge_input, judge_output,
                    review_status, review_reason, error_code, error_message,
                    started_at, finished_at, evidence_input_set_id,
                    aggregate_work_item_id
                )
                SELECT
                    work_item.task_id, work_item.rule_id, NULL, $2::uuid,
                    work_item.consumer_agent_run_id, $3, {executor_sql}, $4,
                    {severity_sql}, $5, $6,
                    $7::jsonb, $8::jsonb, $9::jsonb,
                    $10, $11, $12, NULL,
                    attempt.started_at, now(), work_item.evidence_input_set_id,
                    work_item.id
                FROM qc_aggregate_inspection_work_item AS work_item
                INNER JOIN qc_rule AS rule ON rule.id = work_item.rule_id
                INNER JOIN qc_aggregate_inspection_attempt AS attempt
                    ON attempt.id = $13::uuid
                   AND attempt.work_item_id = work_item.id
                WHERE work_item.id = $1::uuid
                ON CONFLICT (aggregate_work_item_id)
                    WHERE aggregate_work_item_id IS NOT NULL
                DO UPDATE SET updated_at = execution.updated_at
                RETURNING
                    execution.id::text AS execution_id,
                    execution.observation_packet_id::text AS observation_packet_id,
                    execution.execution_status AS execution_status,
                    execution.confidence AS confidence,
                    execution.result_summary AS result_summary,
                    execution.problem_desc AS problem_desc,
                    execution.evidence_artifact_ids::text AS evidence_artifact_ids,
                    execution.judge_input::text AS judge_input,
                    execution.judge_output::text AS judge_output,
                    execution.review_status AS review_status,
                    execution.review_reason AS review_reason,
                    execution.error_code AS error_code
                """,
                *execution_args,
            )
            if execution_row is None:
                raise AggregateInspectionExecutionPersistenceError("聚合 RuleExecution 写入失败")
            _ensure_frozen_aggregate_rule_execution_matches(
                row=execution_row,
                observation_packet_id=packet_id,
                execution_status=judgement.execution_status,
                confidence=judgement.confidence,
                result_summary=judgement.result_summary,
                problem_desc=judgement.problem_desc,
                evidence_artifact_ids=list(judgement.evidence_artifact_ids),
                judge_input=judge_input_payload,
                judge_output=judge_output_payload,
                review_status=judgement.review_status,
                review_reason=judgement.review_reason,
                error_code=judgement.error_code,
            )
            execution_id = str(execution_row["execution_id"])
            review_args = [
                execution_id, judgement.review_reason, lease.work_item_id,
                _review_trigger_for_judgement(context, judgement),
            ]
            review_severity_sql = "rule.severity"
            if frozen_rule is not None:
                review_severity_sql = "$5::varchar"
                review_args.append(frozen_rule["severity"])
            await connection.execute(
                f"""
                INSERT INTO qc_review_item AS review (
                    rule_execution_id, task_id, trigger_reason, status,
                    priority, review_reason
                )
                SELECT
                    $1::uuid, work_item.task_id, $4::varchar,
                    'pending',
                    CASE {review_severity_sql}
                        WHEN 'critical' THEN 10
                        WHEN 'high' THEN 20
                        WHEN 'medium' THEN 50
                        ELSE 100
                    END,
                    $2
                FROM qc_aggregate_inspection_work_item AS work_item
                INNER JOIN qc_rule AS rule ON rule.id = work_item.rule_id
                WHERE work_item.id = $3::uuid
                  AND $4::varchar IS NOT NULL
                ON CONFLICT (rule_execution_id) DO UPDATE SET
                    trigger_reason = EXCLUDED.trigger_reason,
                    status = EXCLUDED.status,
                    priority = EXCLUDED.priority,
                    review_reason = EXCLUDED.review_reason,
                    reviewed_at = NULL,
                    updated_at = now()
                """,
                *review_args,
            )
            result_payload = judgement.to_result_payload()
            result_hash = judgement.result_hash
            attempt_status = (
                "evidence_insufficient"
                if judgement.execution_status == "evidence_insufficient"
                else "succeeded"
            )
            work_status = (
                "evidence_insufficient"
                if judgement.execution_status == "evidence_insufficient"
                else "succeeded"
            )
            # Candidate 只表达该预期规则是否已经产生可审计 RuleExecution。
            # 证据不足的人工处置由 qc_review_item/review_status 单独进入 Review Gate；
            # 若同时把 Candidate 保持 needs_review，审核通过后仍会永久阻塞收口。
            candidate_status = "completed"
            attempt_updated = await connection.fetchval(
                """
                UPDATE qc_aggregate_inspection_attempt
                SET status = $2,
                    result_payload = $3::jsonb,
                    result_hash = $4,
                    finished_at = now(),
                    updated_at = now()
                WHERE id = $1::uuid AND status = 'running'
                RETURNING id
                """,
                str(attempt["id"]),
                attempt_status,
                _json_dumps(result_payload),
                result_hash,
            )
            work_updated = await connection.fetchval(
                """
                UPDATE qc_aggregate_inspection_work_item
                SET status = $2,
                    result_payload = $3::jsonb,
                    result_hash = $4,
                    consumer_agent_run_id = NULL,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    finished_at = now(),
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND status = 'leased'
                  AND lease_token = $5::uuid
                  AND row_version = $6
                RETURNING id
                """,
                lease.work_item_id,
                work_status,
                _json_dumps(result_payload),
                result_hash,
                lease.lease_token,
                lease.row_version,
            )
            candidate_updated = await connection.fetchval(
                """
                UPDATE qc_aggregate_inspection_candidate
                SET status = $2,
                    disposition_code = $3,
                    disposition_message = $4,
                    finished_at = now(),
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND work_item_id = $5::uuid
                  AND status = 'published'
                RETURNING id
                """,
                lease.task_id,
                candidate_status,
                judgement.error_code,
                judgement.review_reason,
                lease.work_item_id,
            )
            if not (attempt_updated and work_updated and candidate_updated):
                raise AggregateInspectionExecutionPersistenceError(
                    "聚合 Attempt、Work 或 Candidate 终态写入不完整"
                )
            await _wake_app_finalization(connection, work_item_id=lease.work_item_id)

    async def retry_or_fail(
        self,
        *,
        lease: AggregateExecutionLease,
        error: Exception,
        retry_delay_seconds: int,
    ) -> None:
        message = f"{type(error).__name__}: {error}"[:2000]
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            work = await _lock_valid_lease(connection, lease=lease)
            attempt = await _lock_running_attempt(connection, lease=lease)
            exhausted = int(work["attempt_count"]) >= int(work["max_attempts"])
            await connection.execute(
                """
                UPDATE qc_aggregate_inspection_attempt
                SET status = $2,
                    error_code = 'AGGREGATE_JUDGE_EXECUTION_ERROR',
                    error_message = $3,
                    finished_at = now(),
                    updated_at = now()
                WHERE id = $1::uuid AND status = 'running'
                """,
                str(attempt["id"]),
                "terminal_error" if exhausted else "retryable_error",
                message,
            )
            row = await connection.fetchrow(
                """
                UPDATE qc_aggregate_inspection_work_item
                SET status = $2::varchar,
                    next_attempt_at = CASE WHEN $2::varchar = 'retry_wait'
                        THEN now() + make_interval(secs => $6)
                        ELSE next_attempt_at END,
                    consumer_agent_run_id = NULL,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = 'AGGREGATE_JUDGE_EXECUTION_ERROR',
                    last_error_message = $3,
                    finished_at = CASE WHEN $2::varchar = 'failed' THEN now() ELSE NULL END,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND status = 'leased'
                  AND lease_token = $4::uuid
                  AND row_version = $5
                RETURNING id::text, task_id::text, evidence_input_set_id,
                    input_hash, finalization_epoch, row_version, status
                """,
                lease.work_item_id,
                "failed" if exhausted else "retry_wait",
                message,
                lease.lease_token,
                lease.row_version,
                retry_delay_seconds,
            )
            if row is None:
                raise AggregateInspectionExecutionPersistenceError("聚合错误回写时租约已丢失")
            if exhausted:
                await _settle_exhausted_candidate(
                    connection,
                    work_item_id=lease.work_item_id,
                    work=work,
                    code="AGGREGATE_JUDGE_EXECUTION_ERROR",
                    disposition_message=message,
                    error_message=message,
                )
            else:
                await _insert_work_wakeup(connection, row=row)

    async def reconcile(self, *, limit: int = 100) -> int:
        """恢复过期租约，并为当前版本缺失的 due Work 重建 Outbox。"""

        pool = await self._get_pool()
        recovered = 0
        async with pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                SELECT work_item.*
                FROM qc_aggregate_inspection_work_item AS work_item
                INNER JOIN qc_task AS task ON task.id = work_item.task_id
                WHERE (
                    task.status = 'running'
                    OR (
                        task.status = 'review'
                        AND task.target_platform IN ('h5', 'app')
                    )
                )
                  AND task.finalization_state = 'session_draining'
                  AND task.finalization_epoch = work_item.finalization_epoch
                  AND work_item.control_mode = 'enforced'
                  AND (
                    (work_item.status IN ('leased', 'running')
                        AND work_item.lease_expires_at <= now())
                    OR (work_item.status IN ('pending', 'retry_wait')
                        AND work_item.next_attempt_at <= now()
                        AND NOT EXISTS (
                            SELECT 1 FROM qc_outbox_event AS event
                            WHERE event.aggregate_type =
                                'aggregate_inspection_work_item'
                              AND event.aggregate_id = work_item.id::text
                              AND event.aggregate_version = work_item.row_version
                        ))
                  )
                ORDER BY work_item.updated_at
                FOR UPDATE OF work_item SKIP LOCKED
                LIMIT $1
                """,
                limit,
            )
            for row in rows:
                current = row
                if str(row["status"]) in {"leased", "running"}:
                    exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
                    await connection.execute(
                        """
                        UPDATE qc_aggregate_inspection_attempt
                        SET status = 'abandoned',
                            error_code = 'AGGREGATE_ATTEMPT_LEASE_EXPIRED',
                            error_message = '聚合 Judge 租约过期，由对账器恢复',
                            finished_at = now(), updated_at = now()
                        WHERE work_item_id = $1::uuid AND status = 'running'
                        """,
                        str(row["id"]),
                    )
                    current = await connection.fetchrow(
                        """
                        UPDATE qc_aggregate_inspection_work_item
                        SET status = $2::varchar,
                            next_attempt_at = now(),
                            consumer_agent_run_id = NULL,
                            lease_owner = NULL,
                            lease_token = NULL,
                            lease_expires_at = NULL,
                            last_error_code = 'AGGREGATE_ATTEMPT_LEASE_EXPIRED',
                            last_error_message = '聚合 Judge 租约过期，由对账器恢复',
                            finished_at = CASE WHEN $2::varchar = 'failed' THEN now() ELSE NULL END,
                            row_version = row_version + 1,
                            updated_at = now()
                        WHERE id = $1::uuid
                        RETURNING *
                        """,
                        str(row["id"]),
                        "failed" if exhausted else "retry_wait",
                    )
                    if exhausted:
                        await _settle_exhausted_candidate(
                            connection,
                            work_item_id=str(row["id"]),
                            work=row,
                            code="AGGREGATE_ATTEMPT_LEASE_EXPIRED",
                            disposition_message="聚合 Judge 重试次数已耗尽",
                            error_message="聚合 Judge 租约过期，由对账器恢复",
                        )
                if current is not None and str(current["status"]) in {
                    "pending",
                    "retry_wait",
                }:
                    await _insert_work_wakeup(connection, row=current)
                recovered += 1
        return recovered

    def _green_artifact_store(self):
        """只在显式绿色过程读取时构建既有存储实现，旧判定路径不新增依赖。"""
        if self._artifact_store is None:
            from services.api.app.core.config import get_settings
            from services.api.app.infrastructure.storage.factory import (
                build_evidence_artifact_store,
            )

            self._artifact_store = build_evidence_artifact_store(get_settings().evidence_storage)
        return self._artifact_store

    async def _get_pool(self) -> Any:
        return self._pool or await get_postgres_pool(self._database_url)


async def _lock_valid_lease(connection: Any, *, lease: AggregateExecutionLease) -> Any:
    row = await connection.fetchrow(
        """
        SELECT * FROM qc_aggregate_inspection_work_item
        WHERE id = $1::uuid
          AND task_id = $2::uuid
          AND status = 'leased'
          AND lease_token = $3::uuid
          AND row_version = $4
          AND lease_expires_at > now()
        FOR UPDATE
        """,
        lease.work_item_id,
        lease.task_id,
        lease.lease_token,
        lease.row_version,
    )
    if row is None:
        raise AggregateInspectionExecutionPersistenceError("聚合 Work 租约已失效")
    return row


async def _lock_running_attempt(connection: Any, *, lease: AggregateExecutionLease) -> Any:
    row = await connection.fetchrow(
        """
        SELECT * FROM qc_aggregate_inspection_attempt
        WHERE work_item_id = $1::uuid
          AND attempt_no = $2
          AND lease_token = $3::uuid
          AND status = 'running'
        FOR UPDATE
        """,
        lease.work_item_id,
        lease.attempt_no,
        lease.lease_token,
    )
    if row is None:
        raise AggregateInspectionExecutionPersistenceError("聚合 Attempt 已失效")
    return row


async def _insert_work_wakeup(connection: Any, *, row: Mapping[str, Any]) -> None:
    work_id = str(row["id"])
    task_id = str(row["task_id"])
    await insert_outbox_event(
        connection,
        aggregate_type="aggregate_inspection_work_item",
        aggregate_id=work_id,
        envelope=build_aggregate_inspection_work_available(
            work_item_id=work_id,
            task_id=task_id,
            evidence_input_set_id=str(row["evidence_input_set_id"]),
            input_hash=str(row["input_hash"]),
            finalization_epoch=int(row["finalization_epoch"]),
            row_version=int(row["row_version"]),
            occurred_at=datetime.now(UTC),
        ),
    )


async def _fail_exhausted_work(connection: Any, *, row: Mapping[str, Any]) -> None:
    await connection.execute(
        """
        UPDATE qc_aggregate_inspection_work_item
        SET status = 'failed',
            consumer_agent_run_id = NULL,
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            last_error_code = 'AGGREGATE_ATTEMPTS_EXHAUSTED',
            last_error_message = '聚合 Judge 重试次数已耗尽',
            finished_at = now(), row_version = row_version + 1,
            updated_at = now()
        WHERE id = $1::uuid
        """,
        str(row["id"]),
    )
    await _settle_exhausted_candidate(
        connection,
        work_item_id=str(row["id"]),
        work=row,
        code="AGGREGATE_ATTEMPTS_EXHAUSTED",
        disposition_message="聚合 Judge 重试次数已耗尽",
        error_message="聚合 Judge 重试次数已耗尽",
    )


# 运营看到的原因写大白话；技术细节留在 error_message
_EXHAUSTED_REVIEW_REASONS = {
    "AGGREGATE_JUDGE_EXECUTION_ERROR": "这条规则判定时出错，重试几次都没成功，已转人工确认",
    "AGGREGATE_ATTEMPT_LEASE_EXPIRED": "这条规则判定超时，重试几次都没完成，已转人工确认",
    "AGGREGATE_ATTEMPTS_EXHAUSTED": "这条规则多次判定都没完成，已转人工确认",
}


async def _settle_exhausted_candidate(
    connection: Any,
    *,
    work_item_id: str,
    work: Mapping[str, Any],
    code: str,
    disposition_message: str,
    error_message: str,
) -> None:
    """重试耗尽时留下一条「检测异常」规则执行和待复核项，候选按已有可审计执行记 completed。

    只记 failed 时，门禁把它算未结算、又没有人能处理的复核项，任务会停在收口出不来；
    任务已不在收口中（不能再新增规则执行）时仍退回 failed。
    """
    frozen_rule = await read_frozen_aggregate_rule(
        connection, task_id=str(work["task_id"]), rule_id=str(work["rule_id"]),
        rule_code=str(work["rule_code"]), contract_hash=str(work["rule_contract_hash"]),
        scope=str(work["scope"]), scope_key=str(work["scope_key"]),
        source_agent_run_id=str(work["producer_agent_run_id"]),
        finalization_epoch=int(work["finalization_epoch"]),
    )
    args = [work_item_id, code, _EXHAUSTED_REVIEW_REASONS[code], error_message,
            disposition_message]
    rule_columns = "rule.executor_type, rule.severity"
    if frozen_rule is not None:
        rule_columns = "$6::varchar AS executor_type, $7::varchar AS severity"
        args.extend([frozen_rule["executor_type"], frozen_rule["severity"]])
    await connection.execute(
        f"""
        WITH work AS (
            SELECT work_item.*, {rule_columns}
            FROM qc_aggregate_inspection_work_item AS work_item
            INNER JOIN qc_rule AS rule ON rule.id = work_item.rule_id
            INNER JOIN qc_task AS task
                ON task.id = work_item.task_id
               AND task.finalization_state = 'session_draining'
            WHERE work_item.id = $1::uuid
        ), execution AS (
            INSERT INTO qc_rule_execution (
                task_id, rule_id, page_capture_id, agent_run_id,
                execution_status, judge_strategy, severity,
                result_summary, problem_desc, judge_input, judge_output,
                review_status, review_reason, error_code, error_message,
                started_at, finished_at, evidence_input_set_id, aggregate_work_item_id
            )
            SELECT
                work.task_id, work.rule_id, NULL, work.producer_agent_run_id,
                'error', work.executor_type, work.severity,
                '规则判定没有完成，已转人工确认', $3::text,
                jsonb_build_object(
                    'schema_version', 'v1',
                    'source', 'aggregate_judge_exhausted',
                    'evidence_input_set_id', work.evidence_input_set_id,
                    'input_hash', work.input_hash,
                    'scope', work.scope,
                    'scope_key', work.scope_key,
                    'attempt_count', work.attempt_count
                ),
                jsonb_build_object(
                    'schema_version', 'v1',
                    'conclusion', 'error',
                    'reason_code', $2::text
                ),
                'pending', $3::text, $2::text, $4::text,
                now(), now(), work.evidence_input_set_id, work.id
            FROM work
            ON CONFLICT (aggregate_work_item_id)
                WHERE aggregate_work_item_id IS NOT NULL
            DO NOTHING
            RETURNING id, task_id, severity, review_reason
        ), review AS (
            INSERT INTO qc_review_item (
                rule_execution_id, task_id, trigger_reason, status, priority, review_reason
            )
            SELECT
                execution.id, execution.task_id, 'manual_flag', 'pending',
                CASE execution.severity
                    WHEN 'critical' THEN 10
                    WHEN 'high' THEN 20
                    WHEN 'medium' THEN 50
                    ELSE 100
                END,
                execution.review_reason
            FROM execution
            ON CONFLICT (rule_execution_id) DO NOTHING
        )
        UPDATE qc_aggregate_inspection_candidate
        SET status = CASE
                WHEN EXISTS (SELECT 1 FROM execution)
                    OR EXISTS (
                        SELECT 1
                        FROM qc_rule_execution AS existing
                        WHERE existing.aggregate_work_item_id = $1::uuid
                    )
                THEN 'completed'
                ELSE 'failed'
            END,
            disposition_code = $2::text,
            disposition_message = $5::text,
            finished_at = now(),
            updated_at = now()
        WHERE work_item_id = $1::uuid AND status = 'published'
        """,
        *args,
    )
    await _wake_app_finalization(connection, work_item_id=work_item_id)


async def _wake_app_finalization(connection: Any, *, work_item_id: str) -> None:
    """当前 App 聚合已真实终结时唤醒门禁，不让旧 Run 或旧轮次重启收口。"""

    await connection.execute(
        """
        UPDATE qc_task AS task SET finalization_retry_at=now()
        FROM qc_aggregate_inspection_work_item AS work
        WHERE work.id=$1::uuid AND work.task_id=task.id
          AND work.status IN ('succeeded','evidence_insufficient','failed')
          AND task.target_platform='app' AND task.status IN ('running','review')
          AND task.deleted_at IS NULL
          AND task.finalization_state='session_draining'
          AND task.finalization_agent_run_id=work.producer_agent_run_id
          AND task.finalization_epoch=work.finalization_epoch
          AND task.completion_gate_snapshot_id IS NOT NULL
          AND task.finalization_recovery_state='active'
          AND NOT EXISTS (SELECT 1 FROM qc_agent_run AS owner
              WHERE owner.id=task.finalization_agent_run_id
                AND COALESCE(owner.output_context->>'cancel_requested','false')='true')
        """,
        work_item_id,
    )


def _lease_from_row(row: Mapping[str, Any]) -> AggregateExecutionLease:
    return AggregateExecutionLease(
        work_item_id=str(row["id"]),
        task_id=str(row["task_id"]),
        evidence_input_set_id=str(row["evidence_input_set_id"]),
        input_hash=str(row["input_hash"]),
        rule_id=str(row["rule_id_text"]),
        rule_code=str(row["rule_code"]),
        rule_contract_hash=str(row["rule_contract_hash"]),
        scope=str(row["scope"]),
        scope_key=str(row["scope_key"]),
        finalization_epoch=int(row["finalization_epoch"]),
        consumer_agent_run_id=str(row["consumer_run_text"]),
        lease_token=str(row["lease_token"]),
        attempt_no=int(row["attempt_count"]),
        row_version=int(row["row_version"]),
    )


def _ensure_frozen_aggregate_packet_matches(
    *,
    row: Mapping[str, Any],
    evidence_artifact_ids: list[str],
    packet_payload: Mapping[str, Any],
    quality_gate_status: str,
    quality_gate_reason: str | None,
) -> None:
    """命中同一 Work Item 幂等身份时验证冻结内容，禁止静默复用漂移判定。

    与常规 Observation Packet 的冻结语义一致：重放携带相同内容才幂等成功；
    并发消费者携带不同判定时 fail-closed，避免下游 RuleExecution 覆盖后
    指向内容不一致的观察包。
    """
    drifted = [
        field
        for field, stored, expected in (
            ("证据附件", json.loads(row["evidence_artifact_ids"]), evidence_artifact_ids),
            ("观察包内容", json.loads(row["packet_payload"]), dict(packet_payload)),
            ("证据门禁结论", row["quality_gate_status"], quality_gate_status),
            ("证据门禁原因", row["quality_gate_reason"], quality_gate_reason),
        )
        if stored != expected
    ]
    if drifted:
        raise AggregateInspectionExecutionPersistenceError(
            f"聚合 Observation Packet {row['packet_id']} 已冻结，"
            f"本次{'、'.join(drifted)}与冻结内容不一致"
        )


def _ensure_frozen_aggregate_rule_execution_matches(
    *,
    row: Mapping[str, Any],
    observation_packet_id: str,
    execution_status: str,
    confidence: float | None,
    result_summary: str | None,
    problem_desc: str | None,
    evidence_artifact_ids: list[str],
    judge_input: Mapping[str, Any],
    judge_output: Mapping[str, Any],
    review_status: str | None,
    review_reason: str | None,
    error_code: str | None,
) -> None:
    """聚合 RuleExecution 与 Packet 对称：冲突时比对冻结判定，禁止覆盖漂移。"""
    stored_confidence = row["confidence"]
    if stored_confidence is not None:
        stored_confidence = float(stored_confidence)
    drifted = [
        field
        for field, stored, expected in (
            ("观察包", row["observation_packet_id"], observation_packet_id),
            ("执行状态", row["execution_status"], execution_status),
            ("置信度", stored_confidence, confidence),
            ("结果摘要", row["result_summary"], result_summary),
            ("问题描述", row["problem_desc"], problem_desc),
            ("证据附件", json.loads(row["evidence_artifact_ids"]), evidence_artifact_ids),
            ("判定输入", json.loads(row["judge_input"]), dict(judge_input)),
            ("判定输出", json.loads(row["judge_output"]), dict(judge_output)),
            ("复核状态", row["review_status"], review_status),
            ("复核原因", row["review_reason"], review_reason),
            ("错误码", row["error_code"], error_code),
        )
        if stored != expected
    ]
    if drifted:
        raise AggregateInspectionExecutionPersistenceError(
            f"聚合 RuleExecution {row['execution_id']} 已冻结，"
            f"本次{'、'.join(drifted)}与冻结内容不一致"
        )


def _review_trigger_for_judgement(
    context: AggregateJudgeContext, judgement: AggregateJudgement,
) -> str | None:
    """绿色已有反例时仍投递其余缺证；不改变旧聚合规则的复核条件。"""
    if judgement.execution_status == "evidence_insufficient":
        return "evidence_insufficient"
    if context.lease.scope_key not in {
        "green_interaction_stability.v1", "green_temporal_rules.v1",
    }:
        return None
    green = judgement.wrapper_payload.get("green")
    if isinstance(green, Mapping) and green.get("missing_evidence"):
        return "evidence_insufficient"
    if judgement.execution_status == "manual_review":
        return "manual_flag"
    return None


def _json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise AggregateInspectionExecutionPersistenceError("JSON 载荷必须为对象")
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
