"""EvidenceInputSet 聚合质检工作项的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.aggregate_inspection_work_items import (
    AggregateInspectionWorkItemFinalization,
    AggregateInspectionWorkItemLease,
    AggregateInspectionWorkItemStatus,
    ClaimAggregateInspectionWorkItemCommand,
    PublishAggregateInspectionWorkItemCommand,
    PublishedAggregateInspectionWorkItem,
    RenewAggregateInspectionWorkItemLeaseCommand,
    StartAggregateInspectionAttemptCommand,
    TransitionAggregateInspectionAttemptCommand,
    TransitionAggregateInspectionWorkItemCommand,
)
from services.api.app.domain.evidence import EvidenceInputScope
from services.api.app.domain.inspection import (
    build_aggregate_inspection_work_available,
)
from services.api.app.infrastructure.db.aggregate_rule_inventory import read_frozen_aggregate_rule
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.pool import get_postgres_pool


class AggregateInspectionWorkItemPersistenceError(RuntimeError):
    """聚合工作项的输入集合、租约或持久化身份不可信。"""


class PostgresAggregateInspectionWorkItemRepository:
    """用独立表发布、领取并推进任务级规则工作项。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def publish(
        self,
        *,
        command: PublishAggregateInspectionWorkItemCommand,
    ) -> PublishedAggregateInspectionWorkItem:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{command.task_id}",
            )
            return await self.publish_on_connection(connection, command=command)

    async def publish_on_connection(
        self,
        connection: Any,
        *,
        command: PublishAggregateInspectionWorkItemCommand,
    ) -> PublishedAggregateInspectionWorkItem:
        """复用调用方事务，并把首条可用事件与 Work 原子写入。"""

        frozen_rule = await read_frozen_aggregate_rule(
            connection, task_id=command.task_id, rule_id=command.rule_id,
            rule_code=command.rule_code, contract_hash=command.rule_contract_hash,
            scope=command.scope.value, scope_key=command.scope_key,
            source_agent_run_id=command.producer_agent_run_id,
            finalization_epoch=command.finalization_epoch,
        )
        frozen_sql = ""
        rule_code_sql = "rule.rule_code"
        rule_membership_sql = "AND rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))"
        ruleset_join_sql = """INNER JOIN qc_ruleset AS ruleset
                    ON ruleset.id = task.ruleset_id
                   AND ruleset.snapshot_hash = input_set.ruleset_snapshot_hash"""
        args = [
            command.task_id, command.evidence_input_set_id, command.producer_agent_run_id,
            command.rule_id, command.rule_code, command.rule_contract_hash,
            command.scope.value, command.scope_key, command.protocol_version,
            command.control_mode, command.priority, command.max_attempts,
            command.finalization_epoch,
        ]
        if frozen_rule is not None:
            # InputSet 已由同事务按冻结快照校验；仍保留真实规则 FK 和 owner/epoch 围栏。
            rule_code_sql = "input_set.rule_code"
            rule_membership_sql = ""
            ruleset_join_sql = ""
            frozen_sql = """AND task.finalization_agent_run_id = producer.id
                  AND input_set.ruleset_snapshot_hash = $14"""
            args.append(frozen_rule["_ruleset_snapshot_hash"])
        row = await connection.fetchrow(
            f"""
                INSERT INTO qc_aggregate_inspection_work_item AS existing (
                    task_id, evidence_input_set_id, producer_agent_run_id,
                    rule_id, rule_code, rule_contract_hash, scope, scope_key,
                    protocol_version, input_hash, control_mode, priority,
                    max_attempts, finalization_epoch
                )
                SELECT
                    task.id,
                    input_set.input_set_id,
                    producer.id,
                    rule.id,
                    {rule_code_sql},
                    input_set.rule_contract_hash,
                    input_set.scope,
                    input_set.scope_key,
                    $9,
                    input_set.input_set_hash,
                    $10,
                    $11,
                    $12,
                    task.finalization_epoch
                FROM qc_task AS task
                INNER JOIN qc_evidence_input_set AS input_set
                    ON input_set.task_id = task.id
                   AND input_set.input_set_id = $2
                INNER JOIN qc_agent_run AS producer
                    ON producer.task_id = task.id
                   AND producer.id = $3::uuid
                   AND producer.id = input_set.source_agent_run_id
                INNER JOIN qc_rule AS rule
                    ON rule.id = $4::uuid
                   AND rule.id = input_set.rule_id
                   {rule_membership_sql}
                {ruleset_join_sql}
                WHERE task.id = $1::uuid
                  AND task.status IN ('running', 'review')
                  AND task.finalization_state = 'session_draining'
                  AND task.finalization_epoch = $13
                  AND input_set.finalization_epoch = task.finalization_epoch
                  AND input_set.rule_code = $5
                  AND input_set.rule_contract_hash = $6
                  AND input_set.scope = $7
                  AND input_set.scope_key = $8
                  {frozen_sql}
                ON CONFLICT ON CONSTRAINT uq_qc_aggregate_work_input_set
                DO UPDATE SET updated_at = existing.updated_at
                WHERE existing.task_id = EXCLUDED.task_id
                  AND existing.rule_id = EXCLUDED.rule_id
                  AND existing.input_hash = EXCLUDED.input_hash
                  AND existing.rule_contract_hash = EXCLUDED.rule_contract_hash
                  AND existing.scope = EXCLUDED.scope
                  AND existing.scope_key = EXCLUDED.scope_key
                  AND existing.finalization_epoch = EXCLUDED.finalization_epoch
                RETURNING
                    existing.id::text,
                    existing.input_hash,
                    existing.status,
                    existing.evidence_input_set_id,
                    existing.finalization_epoch,
                    existing.row_version,
                    (xmax = 0) AS created
                """,
            *args,
        )
        if row is None:
            raise AggregateInspectionWorkItemPersistenceError(
                "任务阶段、EvidenceInputSet、源 Run、规则合同或 Finalization Epoch 不匹配"
            )
        result = PublishedAggregateInspectionWorkItem(
            work_item_id=str(row["id"]),
            input_hash=str(row["input_hash"]),
            status=AggregateInspectionWorkItemStatus(str(row["status"])),
            created=bool(row["created"]),
        )
        if result.status is AggregateInspectionWorkItemStatus.PENDING:
            await insert_outbox_event(
                connection,
                aggregate_type="aggregate_inspection_work_item",
                aggregate_id=result.work_item_id,
                envelope=build_aggregate_inspection_work_available(
                    work_item_id=result.work_item_id,
                    task_id=command.task_id,
                    evidence_input_set_id=str(row["evidence_input_set_id"]),
                    input_hash=result.input_hash,
                    finalization_epoch=int(row["finalization_epoch"]),
                    row_version=int(row["row_version"]),
                    occurred_at=datetime.now(UTC),
                ),
            )
        return result

    async def claim(
        self,
        *,
        command: ClaimAggregateInspectionWorkItemCommand,
        lease_token: str,
    ) -> AggregateInspectionWorkItemLease | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                WITH candidate AS (
                    SELECT work_item.id
                    FROM qc_aggregate_inspection_work_item AS work_item
                    INNER JOIN qc_task AS task
                        ON task.id = work_item.task_id
                    WHERE work_item.task_id = $1::uuid
                      AND task.status IN ('running', 'review')
                      AND task.finalization_state = 'session_draining'
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.control_mode = 'enforced'
                      AND work_item.attempt_count < work_item.max_attempts
                      AND EXISTS (
                          SELECT 1
                          FROM qc_agent_run AS consumer
                          WHERE consumer.task_id = work_item.task_id
                            AND consumer.id = $2::uuid
                            AND consumer.status = 'running'
                      )
                      AND (
                          (
                              work_item.status IN ('pending', 'retry_wait')
                              AND work_item.next_attempt_at <= now()
                          )
                          OR (
                              work_item.status IN ('leased', 'running')
                              AND work_item.lease_expires_at <= now()
                          )
                      )
                    ORDER BY
                        work_item.priority,
                        work_item.next_attempt_at,
                        work_item.created_at
                    FOR UPDATE OF work_item SKIP LOCKED
                    LIMIT 1
                )
                UPDATE qc_aggregate_inspection_work_item AS work_item
                SET
                    status = 'leased',
                    consumer_agent_run_id = $2::uuid,
                    lease_owner = $3,
                    lease_token = $4::uuid,
                    lease_expires_at = now() + make_interval(secs => $5),
                    attempt_count = work_item.attempt_count + 1,
                    row_version = work_item.row_version + 1,
                    started_at = COALESCE(work_item.started_at, now()),
                    last_error_code = NULL,
                    last_error_message = NULL,
                    updated_at = now()
                FROM candidate
                WHERE work_item.id = candidate.id
                RETURNING
                    work_item.id::text,
                    work_item.task_id::text,
                    work_item.evidence_input_set_id,
                    work_item.input_hash,
                    work_item.rule_id::text,
                    work_item.rule_code,
                    work_item.rule_contract_hash,
                    work_item.scope,
                    work_item.scope_key,
                    work_item.finalization_epoch,
                    work_item.lease_token::text,
                    work_item.attempt_count,
                    work_item.row_version
                """,
                command.task_id,
                command.consumer_agent_run_id,
                command.lease_owner,
                lease_token,
                command.lease_seconds,
            )
            if row is not None:
                await connection.execute(
                    """
                    UPDATE qc_aggregate_inspection_attempt
                    SET
                        status = 'abandoned',
                        error_code = COALESCE(
                            error_code,
                            'AGGREGATE_ATTEMPT_LEASE_RECLAIMED'
                        ),
                        error_message = COALESCE(
                            error_message,
                            '任务级工作项旧租约已被新消费者接管'
                        ),
                        finished_at = COALESCE(finished_at, now()),
                        updated_at = now()
                    WHERE work_item_id = $1::uuid
                      AND attempt_no < $2
                      AND status = 'running'
                    """,
                    str(row["id"]),
                    int(row["attempt_count"]),
                )
        if row is None:
            return None
        return AggregateInspectionWorkItemLease(
            work_item_id=str(row["id"]),
            task_id=str(row["task_id"]),
            evidence_input_set_id=str(row["evidence_input_set_id"]),
            input_hash=str(row["input_hash"]),
            rule_id=str(row["rule_id"]),
            rule_code=str(row["rule_code"]),
            rule_contract_hash=str(row["rule_contract_hash"]),
            scope=EvidenceInputScope(str(row["scope"])),
            scope_key=str(row["scope_key"]),
            finalization_epoch=int(row["finalization_epoch"]),
            lease_token=str(row["lease_token"]),
            attempt_no=int(row["attempt_count"]),
            row_version=int(row["row_version"]),
        )

    async def transition(
        self,
        *,
        command: TransitionAggregateInspectionWorkItemCommand,
        result_hash: str | None,
    ) -> int | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                WITH transitioned AS (
                    UPDATE qc_aggregate_inspection_work_item AS work_item
                    SET
                        status = $2,
                        next_attempt_at = CASE
                            WHEN $2 = 'retry_wait' THEN $6::timestamptz
                            ELSE work_item.next_attempt_at
                        END,
                        consumer_agent_run_id = CASE
                            WHEN $2 IN ('leased', 'running')
                            THEN work_item.consumer_agent_run_id
                            ELSE NULL
                        END,
                        lease_owner = CASE
                            WHEN $2 IN ('leased', 'running') THEN work_item.lease_owner
                            ELSE NULL
                        END,
                        lease_token = CASE
                            WHEN $2 IN ('leased', 'running') THEN work_item.lease_token
                            ELSE NULL
                        END,
                        lease_expires_at = CASE
                            WHEN $2 IN ('leased', 'running') THEN work_item.lease_expires_at
                            ELSE NULL
                        END,
                        result_payload = $7::jsonb,
                        result_hash = $8,
                        last_error_code = $9,
                        last_error_message = $10,
                        finished_at = CASE
                            WHEN $2 IN (
                                'succeeded', 'evidence_insufficient', 'failed',
                                'cancelled', 'superseded'
                            ) THEN now()
                            ELSE NULL
                        END,
                        row_version = work_item.row_version + 1,
                        updated_at = now()
                    WHERE work_item.id = $1::uuid
                      AND work_item.status = $3
                      AND work_item.row_version = $4
                      AND work_item.lease_token = $5::uuid
                      AND work_item.lease_expires_at > now()
                      AND EXISTS (
                          SELECT 1
                          FROM qc_task AS task
                          WHERE task.id = work_item.task_id
                            AND task.status IN ('running', 'review')
                            AND task.finalization_state = 'session_draining'
                            AND task.finalization_epoch = work_item.finalization_epoch
                      )
                      AND (
                          $2 IN ('cancelled', 'superseded')
                          OR EXISTS (
                              SELECT 1
                              FROM qc_aggregate_inspection_attempt AS attempt
                              WHERE attempt.work_item_id = work_item.id
                                AND attempt.id = $11::uuid
                                AND attempt.attempt_no = work_item.attempt_count
                                AND attempt.lease_token = $5::uuid
                                AND attempt.result_hash IS NOT DISTINCT FROM $8
                                AND (
                                    ($2 = 'succeeded' AND attempt.status = 'succeeded')
                                    OR (
                                        $2 = 'evidence_insufficient'
                                        AND attempt.status = 'evidence_insufficient'
                                    )
                                    OR (
                                        $2 = 'retry_wait'
                                        AND attempt.status = 'retryable_error'
                                    )
                                    OR (
                                        $2 = 'failed'
                                        AND attempt.status = 'terminal_error'
                                    )
                                    OR (
                                        $2 = 'running'
                                        AND attempt.status = 'running'
                                    )
                                )
                          )
                      )
                    RETURNING work_item.row_version, work_item.task_id,
                        work_item.finalization_epoch, work_item.producer_agent_run_id
                ), gate_wakeup AS (
                    UPDATE qc_task AS task
                    SET finalization_retry_at = now()
                    FROM transitioned
                    WHERE task.id = transitioned.task_id
                      AND task.target_platform IN ('h5', 'app')
                      AND (task.target_platform = 'h5' OR (
                          task.deleted_at IS NULL
                          AND task.finalization_agent_run_id=transitioned.producer_agent_run_id
                          AND NOT EXISTS (SELECT 1 FROM qc_agent_run AS owner
                              WHERE owner.id=task.finalization_agent_run_id
                                AND COALESCE(owner.output_context->>'cancel_requested',
                                             'false')='true')
                      ))
                      AND task.status IN ('running', 'review')
                      AND task.finalization_state = 'session_draining'
                      AND task.finalization_epoch = transitioned.finalization_epoch
                      AND task.completion_gate_snapshot_id IS NOT NULL
                      AND task.finalization_recovery_state = 'active'
                      AND $2 IN (
                          'succeeded', 'evidence_insufficient', 'failed',
                          'cancelled', 'superseded'
                      )
                    RETURNING task.id
                )
                SELECT row_version FROM transitioned
                """,
                command.work_item_id,
                command.target_status.value,
                command.expected_status.value,
                command.row_version,
                command.lease_token,
                command.next_attempt_at,
                _json_dumps(command.result_payload),
                result_hash,
                command.error_code,
                command.error_message,
                command.attempt_id,
            )
        return int(value) if value is not None else None

    async def renew_lease(
        self,
        *,
        command: RenewAggregateInspectionWorkItemLeaseCommand,
    ) -> int | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                UPDATE qc_aggregate_inspection_work_item AS work_item
                SET
                    lease_expires_at = now() + make_interval(secs => $5),
                    row_version = work_item.row_version + 1,
                    updated_at = now()
                WHERE work_item.id = $1::uuid
                  AND work_item.consumer_agent_run_id = $2::uuid
                  AND work_item.lease_token = $3::uuid
                  AND work_item.row_version = $4
                  AND work_item.status IN ('leased', 'running')
                  AND work_item.lease_expires_at > now()
                  AND EXISTS (
                      SELECT 1
                      FROM qc_agent_run AS consumer
                      WHERE consumer.task_id = work_item.task_id
                        AND consumer.id = $2::uuid
                        AND consumer.status = 'running'
                  )
                RETURNING work_item.row_version
                """,
                command.work_item_id,
                command.consumer_agent_run_id,
                command.lease_token,
                command.row_version,
                command.lease_seconds,
            )
        return int(value) if value is not None else None

    async def start_attempt(
        self,
        *,
        command: StartAggregateInspectionAttemptCommand,
    ) -> str:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                INSERT INTO qc_aggregate_inspection_attempt AS existing (
                    task_id, work_item_id, consumer_agent_run_id, attempt_no,
                    lease_token, input_hash
                )
                SELECT
                    work_item.task_id,
                    work_item.id,
                    $2::uuid,
                    $3,
                    $4::uuid,
                    work_item.input_hash
                FROM qc_aggregate_inspection_work_item AS work_item
                WHERE work_item.id = $1::uuid
                  AND work_item.consumer_agent_run_id = $2::uuid
                  AND work_item.attempt_count = $3
                  AND work_item.lease_token = $4::uuid
                  AND work_item.input_hash = $5
                  AND work_item.status IN ('leased', 'running')
                  AND work_item.lease_expires_at > now()
                ON CONFLICT ON CONSTRAINT uq_qc_aggregate_attempt_number
                DO UPDATE SET updated_at = existing.updated_at
                WHERE existing.consumer_agent_run_id = EXCLUDED.consumer_agent_run_id
                  AND existing.lease_token = EXCLUDED.lease_token
                  AND existing.input_hash = EXCLUDED.input_hash
                RETURNING existing.id::text
                """,
                command.work_item_id,
                command.consumer_agent_run_id,
                command.attempt_no,
                command.lease_token,
                command.input_hash,
            )
        if value is None:
            raise AggregateInspectionWorkItemPersistenceError(
                "任务级工作项租约或 attempt_no 已变化"
            )
        return str(value)

    async def transition_attempt(
        self,
        *,
        command: TransitionAggregateInspectionAttemptCommand,
        result_hash: str | None,
    ) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                UPDATE qc_aggregate_inspection_attempt AS attempt
                SET
                    status = $2,
                    result_payload = $5::jsonb,
                    result_hash = $6,
                    error_code = $7,
                    error_message = $8,
                    finished_at = now(),
                    updated_at = now()
                FROM qc_aggregate_inspection_work_item AS work_item
                WHERE attempt.id = $1::uuid
                  AND attempt.status = $3
                  AND attempt.lease_token = $4::uuid
                  AND work_item.id = attempt.work_item_id
                  AND work_item.attempt_count = attempt.attempt_no
                  AND work_item.lease_token = attempt.lease_token
                  AND work_item.status IN ('leased', 'running')
                  AND work_item.lease_expires_at > now()
                RETURNING attempt.id::text
                """,
                command.attempt_id,
                command.target_status.value,
                command.expected_status.value,
                command.lease_token,
                _json_dumps(command.result_payload),
                result_hash,
                command.error_code,
                command.error_message,
            )
        return value is not None

    async def has_nonterminal_work(self, *, task_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM qc_aggregate_inspection_work_item AS work_item
                    INNER JOIN qc_task AS task ON task.id = work_item.task_id
                    WHERE work_item.task_id = $1::uuid
                      AND work_item.control_mode = 'enforced'
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.status NOT IN (
                          'succeeded', 'evidence_insufficient', 'failed',
                          'cancelled', 'superseded'
                      )
                )
                """,
                task_id,
            )
        return bool(value)

    async def finalize_task_work(
        self,
        *,
        task_id: str,
        error_code: str,
        error_message: str,
    ) -> AggregateInspectionWorkItemFinalization:
        """恢复已完成的当前尝试，并失败关闭收口时仍无结果的聚合工作。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            row = await connection.fetchrow(
                """
                WITH target_task AS (
                    SELECT task.id, task.finalization_epoch
                    FROM qc_task AS task
                    WHERE task.id = $1::uuid
                ), recovered AS (
                    UPDATE qc_aggregate_inspection_work_item AS work_item
                    SET
                        status = attempt.status,
                        consumer_agent_run_id = NULL,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        result_payload = attempt.result_payload,
                        result_hash = attempt.result_hash,
                        last_error_code = NULL,
                        last_error_message = NULL,
                        finished_at = COALESCE(attempt.finished_at, now()),
                        row_version = work_item.row_version + 1,
                        updated_at = now()
                    FROM
                        qc_aggregate_inspection_attempt AS attempt,
                        target_task AS task
                    WHERE work_item.task_id = task.id
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.control_mode = 'enforced'
                      AND work_item.status NOT IN (
                          'succeeded', 'evidence_insufficient', 'failed',
                          'cancelled', 'superseded'
                      )
                      AND attempt.work_item_id = work_item.id
                      AND attempt.attempt_no = work_item.attempt_count
                      AND attempt.input_hash = work_item.input_hash
                      AND attempt.status IN (
                          'succeeded', 'evidence_insufficient'
                      )
                      AND attempt.result_payload IS NOT NULL
                      AND attempt.result_hash IS NOT NULL
                    RETURNING work_item.id
                ), abandoned AS (
                    UPDATE qc_aggregate_inspection_attempt AS attempt
                    SET
                        status = 'abandoned',
                        error_code = COALESCE(attempt.error_code, $2),
                        error_message = COALESCE(attempt.error_message, $3),
                        finished_at = COALESCE(attempt.finished_at, now()),
                        updated_at = now()
                    FROM
                        qc_aggregate_inspection_work_item AS work_item,
                        target_task AS task
                    WHERE work_item.task_id = task.id
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.control_mode = 'enforced'
                      AND work_item.status NOT IN (
                          'succeeded', 'evidence_insufficient', 'failed',
                          'cancelled', 'superseded'
                      )
                      AND attempt.work_item_id = work_item.id
                      AND attempt.status = 'running'
                    RETURNING attempt.id
                ), failed AS (
                    UPDATE qc_aggregate_inspection_work_item AS work_item
                    SET
                        status = 'failed',
                        consumer_agent_run_id = NULL,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        result_payload = NULL,
                        result_hash = NULL,
                        last_error_code = $2,
                        last_error_message = $3,
                        finished_at = now(),
                        row_version = work_item.row_version + 1,
                        updated_at = now()
                    FROM target_task AS task
                    WHERE work_item.task_id = task.id
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.control_mode = 'enforced'
                      AND work_item.status NOT IN (
                          'succeeded', 'evidence_insufficient', 'failed',
                          'cancelled', 'superseded'
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM recovered
                          WHERE recovered.id = work_item.id
                      )
                    RETURNING work_item.id
                )
                SELECT
                    (SELECT count(*)::integer FROM recovered) AS recovered_count,
                    (SELECT count(*)::integer FROM failed) AS failed_count,
                    (SELECT count(*)::integer FROM abandoned) AS abandoned_count
                """,
                task_id,
                error_code,
                error_message,
            )
        if row is None:
            raise AggregateInspectionWorkItemPersistenceError(
                "聚合工作项收口未返回数据库摘要"
            )
        return AggregateInspectionWorkItemFinalization(
            task_id=task_id,
            recovered_work_item_count=int(row["recovered_count"]),
            failed_work_item_count=int(row["failed_count"]),
            abandoned_attempt_count=int(row["abandoned_count"]),
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _json_dumps(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
