"""Completion Gate、Finalization Epoch 与报告绑定的 PostgreSQL 实现。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

from services.api.app.application.evidence_presentation import (
    presentation_from_capture_row,
    rule_accepts_diagnostic_evidence,
    rule_evidence_layer_target,
)
from services.api.app.application.h5_report_coverage import bind_h5_report_coverage
from services.api.app.application.report_payloads import (
    normalize_report_json_value,
    normalize_report_payload,
    stable_report_payload_hash,
)
from services.api.app.application.task_finalization import (
    BeginExplorationDrainingCommand,
    BeginSessionDrainingCommand,
    BindReportToGateCommand,
    EvaluateCompletionGateCommand,
    FinalizationRecoveryLease,
    FinalizeTaskWithReportCommand,
    RecordReviewPolicyDecisionCommand,
    TaskFinalizationService,
    TerminateTaskFinalizationCommand,
)
from services.api.app.application.web_final_report_binding import (
    WebFinalReportBindingError,
    WebFinalReportBindingExpectation,
    validate_web_final_report_binding,
)
from services.api.app.core.config import get_settings
from services.api.app.domain.evidence.presentation import evaluate_presentation_for_rule
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.domain.task import (
    REPORT_GATE_BINDING_PROTOCOL_VERSION,
    CompletionBlockReason,
    CompletionGateDecision,
    CompletionGateFacts,
    CompletionGateSnapshot,
    ReportGateBinding,
    TaskExecutionStatus,
    TaskFinalizationSnapshot,
    TaskFinalizationState,
    TaskQualityOutcome,
    build_completion_gate_snapshot,
)
from services.api.app.infrastructure.db.aggregate_rule_inventory_sql import (
    aggregate_expected_rule_count_sql,
)
from services.api.app.infrastructure.db.app_rule_inventory_completion_gate import (
    apply_app_rule_inventory_gate,
)
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    FinalizationRecoveryFenceError,
    assert_finalization_recovery_fence,
)
from services.api.app.infrastructure.db.h5_browser_stop_receipt import unconfirmed_h5_stop_sql
from services.api.app.infrastructure.db.h5_completion_gate_sql import (
    AUDITABLE_H5_INVENTORY_CTE_SQL,
    QUALIFIED_H5_INVENTORY_CTE_SQL,
)
from services.api.app.infrastructure.db.h5_report_coverage import load_h5_report_coverage
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.stalled_finalization_sql import (
    STALLED_FINALIZATION_READY_SQL,
    STALLED_FINALIZATION_READY_SQL_WITHOUT_WEB_CHECKPOINT,
)
from services.api.app.infrastructure.db.web_completion_gate_sql import (
    WEB_COMPLETION_GATE_INVENTORY_CTES_SQL,
)
from services.api.app.infrastructure.db.web_coverage_snapshot_row import (
    InvalidWebCoverageSnapshotRowError,
    parse_web_coverage_snapshot_row,
)
from services.api.app.infrastructure.db.web_gap_rule_sql import (
    WEB_GUEST_GAP_RULE_APPLICABILITY_SQL,
)
from services.api.app.infrastructure.db.web_sequence_completion_gate_sql import (
    WEB_SEQUENCE_PUBLICATION_INVENTORY_MATCH_SQL,
)


class TaskFinalizationPersistenceError(RuntimeError):
    """任务状态、epoch、乐观锁或门禁快照不满足收口约束。"""


logger = logging.getLogger(__name__)


class PostgresCompletionGatePreparer:
    """先收束遗留动作，再结算停滞质检工作项，最后发布聚合规则事实。

    结算必须排在门禁评估之前：门禁被 nonterminal_enforced_work_item 挡住时，
    报告阶段的 finalize_task_work 根本执行不到，任务会一直卡在收口重试
    （01 专项 §6 步骤 9「停止新动作后结算剩余工作，再冻结报告输入并释放资源」）。
    """

    def __init__(
        self,
        *,
        action_executions: Any,
        aggregate_publication: Any,
        inspection_work_settlement: Any | None = None,
    ) -> None:
        self._action_executions = action_executions
        self._aggregate_publication = aggregate_publication
        self._inspection_work_settlement = inspection_work_settlement

    async def prepare_app_aggregate_for_recovery(
        self, *, task_id: str, agent_run_id: str, finalization_epoch: int,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> bool:
        """异常超时封存前，给已经完成正式快照交接的 App 聚合规则一次调度。"""

        prepare = getattr(self._aggregate_publication, "prepare_app_recovery", None)
        return bool(callable(prepare) and await prepare(
            task_id=task_id, agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            recovery_lease_token=recovery_lease_token,
            recovery_generation=recovery_generation,
            recovery_lease_expires_at=recovery_lease_expires_at,
        ))

    async def prepare(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: object | None = None,
    ) -> object:
        fence = _optional_recovery_fence_kwargs(
            recovery_lease_token=recovery_lease_token,
            recovery_generation=recovery_generation,
            recovery_lease_expires_at=recovery_lease_expires_at,
        )
        await self._action_executions.fail_nonterminal_for_finalization(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            error_code="TASK_FINALIZATION_ABORTED_ACTION",
            error_message="任务已进入收口阶段，遗留动作已失去执行者",
            **fence,
        )
        await self._settle_stalled_inspection_work(task_id)
        return await self._aggregate_publication.prepare(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            **fence,
        )

    async def _settle_stalled_inspection_work(self, task_id: str) -> None:
        """结算再也推进不了的工作项；结算本身失败只告警，不能把收口整条堵死。"""
        settlement = self._inspection_work_settlement
        if settlement is None:
            return
        try:
            result = await settlement.settle_stalled_work_for_closure(task_id=task_id)
        except Exception:  # noqa: BLE001 - 门禁前置不能因结算失败而中断
            logger.warning(
                "task_id=%s 收口前结算停滞质检工作项失败，门禁按现有事实评估",
                task_id,
                exc_info=True,
            )
            return
        if getattr(result, "settled", False):
            logger.info(
                "task_id=%s 收口前结算停滞质检工作项：工作项 %s 个、规则 %s 条，原因 %s",
                task_id,
                result.settled_work_item_count,
                result.settled_rule_count,
                result.reason_counts,
            )


class PostgresTaskFinalizationRepository:
    """以 Task 行锁和事务内重算实现不可穿透的最终完成门禁。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def app_completion_ready_before_timeout(
        self, *, command: EvaluateCompletionGateCommand,
    ) -> bool:
        """只在封存材料复核及真实完成门禁全部通过时避开异常超时终止。"""

        from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
            PostgresAggregateInspectionPublicationRepository,
        )

        pool = await self._get_pool()
        source = PostgresAggregateInspectionPublicationRepository(pool=pool)
        if not await source.is_app_recovery_ready(
            task_id=command.task_id, source_agent_run_id=command.agent_run_id,
            finalization_epoch=command.expected_finalization_epoch, require_missing=False,
        ):
            return False
        async with pool.acquire() as connection, connection.transaction():
            await _lock_task_finalization(connection, command.task_id)
            await _assert_recovery_fence(connection, command)
            valid = await connection.fetchval(
                """SELECT task.id FROM qc_task AS task JOIN qc_agent_run AS owner
                       ON owner.id=task.finalization_agent_run_id AND owner.task_id=task.id
                   WHERE task.id=$1::uuid AND owner.id=$2::uuid
                     AND task.finalization_epoch=$3 AND task.target_platform='app'
                     AND task.status='running' AND task.deleted_at IS NULL
                     AND task.finalization_state='session_draining'
                     AND COALESCE(owner.output_context->>'cancel_requested','false')<>'true'
                   FOR UPDATE OF task,owner""",
                command.task_id, command.agent_run_id, command.expected_finalization_epoch,
            )
            if not valid:
                return False
            _row, facts = await _read_completion_gate_facts(connection, command)
            return facts.gate_passed

    async def get(self, *, task_id: str) -> TaskFinalizationSnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _FINALIZATION_SELECT_SQL + " WHERE id = $1::uuid",
                task_id,
            )
        return _parse_finalization(row) if row is not None else None

    async def list_session_draining(
        self,
        *,
        limit: int = 100,
    ) -> list[TaskFinalizationSnapshot]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _FINALIZATION_SELECT_SQL
                + " WHERE status = 'running'"
                + " AND finalization_state = 'session_draining'"
                + " AND NOT EXISTS ("
                + " SELECT 1 FROM qc_agent_run AS session_run"
                + " WHERE session_run.id = qc_task.finalization_agent_run_id"
                + " AND COALESCE("
                + " session_run.output_context->>'cancel_requested', 'false'"
                + " ) = 'true'"
                + " )"
                + " ORDER BY session_draining_at NULLS FIRST, id"
                + " LIMIT $1",
                limit,
            )
        return [_parse_finalization(row) for row in rows]

    async def list_recoverable_finalizations(
        self,
        *,
        limit: int = 100,
    ) -> list[TaskFinalizationSnapshot]:
        """扫描已绑定终态 Run、无活跃 Run 且仍需收口或报告绑定的任务。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _FINALIZATION_SELECT_SQL
                + " WHERE (("
                + " ("
                + " (qc_task.status = 'running'"
                + " AND finalization_state IN ("
                + " 'open', 'exploration_draining', 'session_draining'"
                + " ))"
                + " OR (qc_task.status IN ('review', 'done')"
                + " AND finalization_state = 'session_draining')"
                + " )"
                + " AND finalization_agent_run_id IS NOT NULL"
                + " AND EXISTS ("
                + " SELECT 1 FROM qc_agent_run AS terminal_run"
                + " WHERE terminal_run.id = qc_task.finalization_agent_run_id"
                + " AND terminal_run.task_id = qc_task.id"
                + " AND terminal_run.status IN ('done', 'failed', 'aborted', 'canceled')"
                + " )"
                + " AND NOT EXISTS ("
                + " SELECT 1 FROM qc_agent_run AS active_run"
                + " WHERE active_run.task_id = qc_task.id"
                + " AND active_run.status IN ('pending', 'running')"
                + " AND NOT ("
                + " ("
                + " ("
                + " qc_task.target_platform = 'web'"
                + " AND LOWER(COALESCE("
                + " qc_task.task_config->>'workflow_version', ''"
                + " )) = 'v2'"
                + " AND qc_task.task_config->>'execution_mode' = 'full_inspection'"
                + " AND qc_task.task_config->>'web_inspection_notification_mode'"
                + " = 'enforced'"
                + " ) OR ("
                + " qc_task.target_platform = 'h5'"
                + " AND LOWER(COALESCE("
                + " qc_task.task_config->>'workflow_version', ''"
                + " )) = 'v2'"
                + " AND qc_task.task_config->>'execution_mode' = 'full_inspection'"
                + " AND qc_task.task_config->>'h5_inspection_notification_mode'"
                + " = 'enforced'"
                + " )"
                + " )"
                + " AND active_run.agent_type = 'rule_judge'"
                + " AND active_run.input_context->>'source'"
                + " = 'h5_inspection_publication'"
                + " AND active_run.input_context->>'finalization_epoch'"
                + " = qc_task.finalization_epoch::text"
                + " )"
                + " )"
                + " )"
                + " OR ("
                + " finalization_state = 'sealed'"
                + " AND finalization_agent_run_id IS NOT NULL"
                + " AND NOT EXISTS ("
                + " SELECT 1 FROM qc_report_gate_binding AS binding"
                + " INNER JOIN qc_report_payload_snapshot AS payload"
                + " ON payload.report_id = binding.report_id"
                + " AND payload.report_version = binding.report_version"
                + " WHERE binding.task_id = qc_task.id"
                + " AND binding.gate_snapshot_id = qc_task.completion_gate_snapshot_id"
                + " AND binding.finalization_epoch = qc_task.finalization_epoch"
                + " )"
                + " )"
                + ")"
                + " AND COALESCE(qc_task.execution_status, '')"
                + " NOT IN ('needs_review', 'failed')"
                + " AND finalization_recovery_state = 'active'"
                + " AND NOT ("
                + " qc_task.target_platform = 'web'"
                + " AND LOWER(COALESCE(qc_task.task_config->>'workflow_version', '')) = 'v2'"
                + " AND qc_task.task_config->>'execution_mode' = 'full_inspection'"
                + " AND qc_task.task_config->>'web_inspection_notification_mode' = 'enforced'"
                + " )"
                + " AND (finalization_retry_at IS NULL"
                + " OR finalization_retry_at <= now()"
                # H5人工待复核不等于技术收口；超龄终态仍须复核停止证明，不能永久休眠。
                + " OR (qc_task.target_platform='h5'"
                + " AND qc_task.finalization_state='session_draining'"
                + " AND qc_task.finalization_retry_at='infinity'::timestamptz"
                + " AND EXISTS(SELECT 1 FROM qc_agent_run terminal_h5"
                + " WHERE terminal_h5.id=qc_task.finalization_agent_run_id"
                + " AND terminal_h5.task_id=qc_task.id"
                + " AND terminal_h5.status IN ('done','failed','aborted')"
                + " AND terminal_h5.finished_at <= now()-($2 * interval '1 second')))"
                # 零采集的 APP 技术失败没有可等待的业务复核；相同 Gate 不能永久屏蔽超时检查。
                + " OR (qc_task.target_platform = 'app'"
                + " AND LOWER(COALESCE(qc_task.task_config->>'workflow_version', '')) = 'v2'"
                + " AND COALESCE(qc_task.task_config->>'execution_mode', 'full_inspection')"
                + " = 'full_inspection'"
                + " AND qc_task.status = 'running'"
                + " AND qc_task.deleted_at IS NULL"
                + " AND qc_task.finalization_state = 'session_draining'"
                + " AND qc_task.finalization_retry_at = 'infinity'::timestamptz"
                + " AND EXISTS ("
                + " SELECT 1 FROM qc_agent_run AS bootstrap_failure"
                + " WHERE bootstrap_failure.id = qc_task.finalization_agent_run_id"
                + " AND bootstrap_failure.task_id = qc_task.id"
                + " AND bootstrap_failure.agent_type = 'main'"
                + " AND bootstrap_failure.status = 'failed'"
                + " AND bootstrap_failure.finished_at <= now()-($2 * interval '1 second')"
                + " )"
                + " AND NOT EXISTS ("
                # qc_app_capture 必须引用 qc_capture；同时保守保留尚无 APP 扩展的真实公共采集。
                + " SELECT 1 FROM qc_capture AS bootstrap_capture"
                + " WHERE bootstrap_capture.task_id = qc_task.id"
                + " AND bootstrap_capture.agent_run_id = qc_task.finalization_agent_run_id"
                + " )"
                + " ))"
                + " AND NOT EXISTS ("
                + " SELECT 1 FROM qc_agent_run AS owner_run"
                + " WHERE owner_run.id = qc_task.finalization_agent_run_id"
                + " AND COALESCE("
                + " owner_run.output_context->>'cancel_requested', 'false'"
                + " ) = 'true'"
                + " )"
                + " ORDER BY CASE finalization_state"
                + " WHEN 'exploration_draining' THEN 0"
                + " WHEN 'open' THEN 1"
                + " WHEN 'session_draining' THEN 2"
                + " WHEN 'sealed' THEN 3"
                + " ELSE 4 END,"
                + " finalization_retry_at NULLS FIRST,"
                + " updated_at NULLS FIRST, id LIMIT $1",
                limit,
                get_settings().worker.finalization_stall_timeout_seconds,
            )
        return [_parse_finalization(row) for row in rows]

    async def claim_recoverable_finalizations(
        self,
        *,
        limit: int,
        lease_owner: str,
        lease_seconds: float,
    ) -> list[FinalizationRecoveryLease]:
        """用 SKIP LOCKED 原子领取 Web V2/full/enforced 恢复任务。"""

        token = str(uuid4())
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT task.id
                    FROM qc_task AS task
                    WHERE task.target_platform = 'web'
                      AND LOWER(COALESCE(task.task_config->>'workflow_version', '')) = 'v2'
                      AND task.task_config->>'execution_mode' = 'full_inspection'
                      AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
                      AND task.finalization_recovery_state = 'active'
                      AND COALESCE(task.execution_status, '')
                          NOT IN ('needs_review', 'failed')
                      AND (task.finalization_retry_at IS NULL
                           OR task.finalization_retry_at <= now())
                      AND (task.finalization_recovery_lease_expires_at IS NULL
                           OR task.finalization_recovery_lease_expires_at <= now())
                      AND task.finalization_agent_run_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM qc_agent_run AS terminal_run
                          WHERE terminal_run.id = task.finalization_agent_run_id
                            AND terminal_run.task_id = task.id
                            AND terminal_run.status IN ('done','failed','aborted','canceled')
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM qc_agent_run AS owner_run
                          WHERE owner_run.id = task.finalization_agent_run_id
                            AND COALESCE(
                                owner_run.output_context->>'cancel_requested', 'false'
                            ) = 'true'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM qc_agent_run AS active_run
                          WHERE active_run.task_id = task.id
                            AND active_run.status IN ('pending','running')
                            AND NOT (
                                active_run.agent_type = 'rule_judge'
                                AND active_run.input_context->>'source'
                                    = 'h5_inspection_publication'
                                AND active_run.input_context->>'finalization_epoch'
                                    = task.finalization_epoch::text
                            )
                      )
                      AND (
                          (task.status = 'running' AND task.finalization_state IN (
                              'open','exploration_draining','session_draining'
                          ))
                          OR (task.status IN ('review', 'done')
                              AND task.finalization_state = 'session_draining')
                          OR task.finalization_state = 'sealed'
                          OR (task.finalization_state = 'sealed' AND NOT EXISTS (
                              SELECT 1 FROM qc_report_gate_binding AS binding
                              INNER JOIN qc_report_payload_snapshot AS payload
                                ON payload.report_id = binding.report_id
                               AND payload.report_version = binding.report_version
                              INNER JOIN qc_completion_gate_snapshot AS gate
                                ON gate.gate_snapshot_id = binding.gate_snapshot_id
                               AND gate.task_id = binding.task_id
                               AND gate.gate_passed
                              INNER JOIN qc_web_coverage_snapshot AS coverage
                                ON coverage.task_id = binding.task_id
                               AND coverage.agent_run_id
                                   = task.finalization_agent_run_id
                               AND coverage.finalization_epoch
                                   = binding.finalization_epoch
                              WHERE binding.task_id = task.id
                                AND binding.gate_snapshot_id
                                    = task.completion_gate_snapshot_id
                                AND binding.finalization_epoch = task.finalization_epoch
                                AND binding.report_payload_hash
                                    = payload.report_payload_hash
                                AND payload.report_payload->>'report_id'
                                    = binding.report_id
                                AND binding.report_id = (
                                    'task:' || task.id::text || ':run:' ||
                                    task.finalization_agent_run_id::text
                                )
                                AND payload.report_payload->>'report_stage' = 'final'
                                AND payload.report_payload->>'is_final' = 'true'
                                AND payload.report_payload#>>'{report_contract,target_platform}'
                                    = 'web'
                                AND LOWER(
                                    payload.report_payload
                                        #>>'{report_contract,workflow_version}'
                                )
                                    = 'v2'
                                AND payload.report_payload#>>'{report_contract,execution_mode}'
                                    = 'full_inspection'
                                AND payload.report_payload#>>'{report_contract,notification_mode}'
                                    = 'enforced'
                                AND payload.report_payload->>'report_binding_version'
                                    = binding.report_version::text
                                AND payload.report_payload->>'agent_run_id'
                                    = task.finalization_agent_run_id::text
                                AND payload.report_payload#>>'{task,task_id}'
                                    = task.id::text
                                AND payload.report_payload
                                    #>>'{completion_gate,gate_snapshot_id}'
                                    = binding.gate_snapshot_id
                                AND payload.report_payload
                                    #>>'{completion_gate,finalization_epoch}'
                                    = binding.finalization_epoch::text
                                AND payload.report_payload
                                    #>>'{completion_gate,gate_passed}' = 'true'
                                AND payload.report_payload
                                    #>>'{page_exploration,web_coverage,coverage_snapshot_id}'
                                    = coverage.id::text
                                AND payload.report_payload
                                    #>>'{page_exploration,web_coverage,snapshot_hash}'
                                    = coverage.snapshot_hash
                                AND payload.report_payload
                                    #>>'{page_exploration,web_coverage,finalization_epoch}'
                                    = coverage.finalization_epoch::text
                                AND payload.report_payload
                                    #>'{page_exploration,web_coverage,required_page_types}'
                                    = coverage.required_page_types
                                AND payload.report_payload
                                    #>'{page_exploration,web_coverage,observed_page_types}'
                                    = coverage.observed_page_types
                                AND payload.report_payload
                                    #>'{page_exploration,web_coverage,missing_page_types}'
                                    = coverage.missing_page_types
                                AND payload.report_payload
                                    #>'{page_exploration,web_coverage,unrecognized_page_types}'
                                    = coverage.unrecognized_page_types
                                AND payload.report_payload
                                    #>>'{page_exploration,web_coverage,normalization_version}'
                                    = coverage.normalization_version
                                AND payload.report_payload
                                    #>>'{page_exploration,web_coverage,coverage_status}'
                                    = coverage.coverage_status
                                AND coverage.applicability_snapshot IS NOT NULL
                                AND payload.report_payload
                                    #>'{page_exploration,web_coverage,applicability_snapshot}'
                                    = coverage.applicability_snapshot
                          ))
                      )
                    ORDER BY task.finalization_retry_at NULLS FIRST, task.updated_at, task.id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                UPDATE qc_task AS task
                SET finalization_recovery_lease_token = $4::uuid,
                    finalization_recovery_lease_owner = $2,
                    finalization_recovery_lease_expires_at =
                        now() + ($3 * interval '1 second'),
                    finalization_retry_at = now() + ($3 * interval '1 second'),
                    finalization_retry_count = finalization_retry_count + 1,
                    finalization_recovery_generation =
                        finalization_recovery_generation + 1,
                    finalization_row_version = finalization_row_version + 1,
                    updated_at = now()
                FROM candidates
                WHERE task.id = candidates.id
                RETURNING task.id::text AS task_id,
                    task.finalization_state,
                    task.finalization_epoch,
                    task.finalization_row_version AS row_version,
                    task.finalization_agent_run_id::text AS finalization_agent_run_id,
                    task.completion_gate_snapshot_id,
                    task.execution_status,
                    task.quality_outcome,
                    task.status AS expected_status,
                    task.finalization_recovery_generation AS recovery_generation,
                    task.finalization_recovery_lease_token::text AS lease_token,
                    task.finalization_recovery_lease_expires_at AS lease_expires_at
                """,
                limit,
                lease_owner,
                lease_seconds,
                token,
            )
        return [
            FinalizationRecoveryLease(
                snapshot=_parse_finalization(row),
                lease_token=str(row["lease_token"]),
                recovery_generation=int(row["recovery_generation"]),
                expected_status=str(row["expected_status"]),
                lease_expires_at=row["lease_expires_at"],
            )
            for row in rows
        ]

    async def renew_finalization_recovery_lease(
        self,
        *,
        task_id: str,
        lease_token: str,
        recovery_generation: int,
        lease_seconds: float,
    ) -> FinalizationRecoveryLease:
        """续租只允许当前未过期 Web holder，旧 generation 无法复活。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_task AS task
                SET finalization_recovery_lease_expires_at =
                        now() + ($4 * interval '1 second'),
                    finalization_retry_at = now() + ($4 * interval '1 second'),
                    updated_at = now()
                WHERE task.id = $1::uuid
                  AND task.finalization_recovery_lease_token = $2::uuid
                  AND task.finalization_recovery_generation = $3
                  AND task.finalization_recovery_lease_expires_at > now()
                  AND task.finalization_recovery_state = 'active'
                  AND task.target_platform = 'web'
                  AND LOWER(COALESCE(task.task_config->>'workflow_version', '')) = 'v2'
                  AND task.task_config->>'execution_mode' = 'full_inspection'
                  AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
                RETURNING task.id::text AS task_id,
                    task.finalization_state,
                    task.finalization_epoch,
                    task.finalization_row_version AS row_version,
                    task.finalization_agent_run_id::text AS finalization_agent_run_id,
                    task.completion_gate_snapshot_id,
                    task.execution_status,
                    task.quality_outcome,
                    task.status AS expected_status,
                    task.finalization_recovery_generation AS recovery_generation,
                    task.finalization_recovery_lease_token::text AS lease_token,
                    task.finalization_recovery_lease_expires_at AS lease_expires_at
                """,
                task_id,
                lease_token,
                recovery_generation,
                lease_seconds,
            )
        if row is None:
            raise TaskFinalizationPersistenceError("收口恢复租约已过期或 fencing 身份已变化")
        return FinalizationRecoveryLease(
            snapshot=_parse_finalization(row),
            lease_token=str(row["lease_token"]),
            recovery_generation=int(row["recovery_generation"]),
            expected_status=str(row["expected_status"]),
            lease_expires_at=row["lease_expires_at"],
        )

    async def defer_finalization_retry(
        self,
        *,
        task_id: str,
        retry_after_seconds: float,
        error_message: str | None = None,
    ) -> None:
        """领取一次恢复机会并延后下次扫描，避免固定批次任务饥饿。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE qc_task
                SET finalization_retry_at = now() + ($2 * interval '1 second'),
                    finalization_retry_count = finalization_retry_count + 1,
                    finalization_last_error = NULLIF($3, '')
                WHERE id = $1::uuid
                  AND finalization_state IN (
                      'open', 'exploration_draining', 'session_draining', 'sealed'
                  )
                """,
                task_id,
                retry_after_seconds,
                (error_message or "")[:2000],
            )

    async def record_finalization_retry_error(
        self,
        *,
        task_id: str,
        error_message: str,
        lease_token: str,
        recovery_generation: int,
        expected_status: str,
        lease_expires_at: object | None = None,
    ) -> None:
        """记录恢复错误，保留领取时已累计的重试次数。"""

        del lease_expires_at
        error_summary = error_message[:2000]
        error_signature = sha256(error_summary.encode("utf-8")).hexdigest()
        quarantine_threshold = 3
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE qc_task
                SET finalization_last_error = $2,
                    finalization_error_signature = $3::varchar(64),
                    finalization_same_error_count = CASE
                        WHEN finalization_error_signature = $3::varchar(64)
                        THEN finalization_same_error_count + 1
                        ELSE 1
                    END,
                    finalization_recovery_state = CASE
                        WHEN (
                            CASE
                                WHEN finalization_error_signature = $3::varchar(64)
                                THEN finalization_same_error_count + 1
                                ELSE 1
                            END
                        ) >= $4::integer
                        THEN 'quarantined'
                        ELSE 'active'
                    END,
                    finalization_quarantined_at = CASE
                        WHEN (
                            CASE
                                WHEN finalization_error_signature = $3::varchar(64)
                                THEN finalization_same_error_count + 1
                                ELSE 1
                            END
                        ) >= $4::integer
                        THEN now()
                        ELSE NULL
                    END,
                    finalization_retry_at = CASE
                        WHEN (
                            CASE
                                WHEN finalization_error_signature = $3::varchar(64)
                                THEN finalization_same_error_count + 1
                                ELSE 1
                            END
                        ) >= $4::integer
                        THEN NULL
                        ELSE finalization_retry_at
                    END,
                    finalization_recovery_lease_token = NULL,
                    finalization_recovery_lease_owner = NULL,
                    finalization_recovery_lease_expires_at = NULL,
                    status = CASE
                        WHEN (
                            CASE
                                WHEN finalization_error_signature = $3::varchar(64)
                                THEN finalization_same_error_count + 1
                                ELSE 1
                            END
                        ) >= $4::integer
                        AND finalization_state = 'session_draining'
                        THEN 'review'
                        ELSE status
                    END,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND finalization_recovery_lease_token = $5::uuid
                  AND finalization_recovery_generation = $6
                  AND finalization_recovery_lease_expires_at > now()
                  AND status = $7
                  AND target_platform = 'web'
                  AND LOWER(COALESCE(task_config->>'workflow_version', '')) = 'v2'
                  AND task_config->>'execution_mode' = 'full_inspection'
                  AND task_config->>'web_inspection_notification_mode' = 'enforced'
                  AND finalization_state IN (
                      'open', 'exploration_draining', 'session_draining', 'sealed'
                  )
                  AND finalization_recovery_state = 'active'
                """,
                task_id,
                error_summary,
                error_signature,
                quarantine_threshold,
                lease_token,
                recovery_generation,
                expected_status,
            )

    async def record_finalization_retry_success(
        self,
        *,
        task_id: str,
        lease_token: str,
        recovery_generation: int,
        expected_status: str,
        lease_expires_at: object | None = None,
    ) -> None:
        """成功推进一次收口后清除连续异常计数。"""

        del lease_expires_at
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE qc_task
                SET finalization_last_error = NULL,
                    finalization_error_signature = NULL,
                    finalization_same_error_count = 0,
                    finalization_recovery_state = CASE
                        WHEN finalization_state = 'sealed' THEN 'verified'
                        ELSE 'active'
                    END,
                    finalization_quarantined_at = NULL,
                    finalization_recovery_lease_token = NULL,
                    finalization_recovery_lease_owner = NULL,
                    finalization_recovery_lease_expires_at = NULL,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND finalization_recovery_lease_token = $2::uuid
                  AND finalization_recovery_generation = $3
                  AND finalization_recovery_lease_expires_at > now()
                  AND status = $4
                  AND target_platform = 'web'
                  AND LOWER(COALESCE(task_config->>'workflow_version', '')) = 'v2'
                  AND task_config->>'execution_mode' = 'full_inspection'
                  AND task_config->>'web_inspection_notification_mode' = 'enforced'
                  AND finalization_recovery_state = 'active'
                """,
                task_id,
                lease_token,
                recovery_generation,
                expected_status,
            )

    async def quarantine_missing_sealed_report(
        self,
        *,
        task_id: str,
        lease_token: str,
        recovery_generation: int,
        expected_status: str,
        lease_expires_at: object | None = None,
    ) -> None:
        """历史 sealed 缺正文不可重算，直接隔离并等待管理员显式恢复。"""

        del lease_expires_at
        message = "SEALED_FINAL_REPORT_PAYLOAD_MISSING: 历史封存正文不可重算"
        signature = sha256(message.encode("utf-8")).hexdigest()
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE qc_task
                SET finalization_last_error = $5,
                    finalization_error_signature = $6,
                    finalization_same_error_count = 3,
                    finalization_recovery_state = 'quarantined',
                    finalization_quarantined_at = now(),
                    finalization_retry_at = NULL,
                    finalization_recovery_lease_token = NULL,
                    finalization_recovery_lease_owner = NULL,
                    finalization_recovery_lease_expires_at = NULL,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND finalization_recovery_lease_token = $2::uuid
                  AND finalization_recovery_generation = $3
                  AND finalization_recovery_lease_expires_at > now()
                  AND status = $4
                  AND finalization_state = 'sealed'
                  AND target_platform = 'web'
                  AND LOWER(COALESCE(task_config->>'workflow_version', '')) = 'v2'
                  AND task_config->>'execution_mode' = 'full_inspection'
                  AND task_config->>'web_inspection_notification_mode' = 'enforced'
                  AND finalization_recovery_state = 'active'
                """,
                task_id,
                lease_token,
                recovery_generation,
                expected_status,
                message,
                signature,
            )

    async def begin_exploration_draining(
        self,
        *,
        command: BeginExplorationDrainingCommand,
    ) -> TaskFinalizationSnapshot:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _lock_task_finalization(connection, command.task_id)
            await _assert_recovery_fence(connection, command)
            row = await connection.fetchrow(
                """
                UPDATE qc_task
                SET
                    finalization_state = 'exploration_draining',
                    exploration_draining_at = now(),
                    finalization_row_version = finalization_row_version + 1,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND status = 'running'
                  AND finalization_state = 'open'
                  AND finalization_row_version = $2
                  AND finalization_agent_run_id = $3::uuid
                RETURNING
                    id::text AS task_id,
                    finalization_state,
                    finalization_epoch,
                    finalization_row_version AS row_version,
                    finalization_agent_run_id::text AS finalization_agent_run_id,
                    completion_gate_snapshot_id,
                    execution_status,
                    quality_outcome
                """,
                command.task_id,
                command.expected_row_version,
                command.agent_run_id,
            )
        if row is None:
            raise TaskFinalizationPersistenceError(
                "任务不可运行、已开始收口或 finalization_row_version 已变化"
            )
        return _parse_finalization(row)

    async def begin_session_draining(
        self,
        *,
        command: BeginSessionDrainingCommand,
    ) -> TaskFinalizationSnapshot:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _lock_task_finalization(connection, command.task_id)
            await _assert_recovery_fence(connection, command)
            row = await connection.fetchrow(
                """
                UPDATE qc_task
                SET
                    finalization_state = 'session_draining',
                    session_draining_at = now(),
                    finalization_row_version = finalization_row_version + 1,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND status = 'running'
                  AND finalization_state = 'exploration_draining'
                  AND finalization_epoch = $2
                  AND finalization_row_version = $3
                  AND finalization_agent_run_id = $4::uuid
                RETURNING
                    id::text AS task_id,
                    finalization_state,
                    finalization_epoch,
                    finalization_row_version AS row_version,
                    finalization_agent_run_id::text AS finalization_agent_run_id,
                    completion_gate_snapshot_id,
                    execution_status,
                    quality_outcome
                """,
                command.task_id,
                command.expected_finalization_epoch,
                command.expected_row_version,
                command.agent_run_id,
            )
            if row is None:
                raise TaskFinalizationPersistenceError(
                    "任务尚未进入 Exploration Draining，或 epoch/版本已变化"
                )
            await connection.execute(
                """
                UPDATE qc_surface_session
                SET
                    status = 'draining',
                    draining_at = COALESCE(draining_at, now()),
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND status = 'active'
                """,
                command.task_id,
            )
            await connection.execute(
                """
                UPDATE qc_surface_ref
                SET
                    status = 'draining',
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND status = 'active'
                """,
                command.task_id,
            )
        return _parse_finalization(row)

    async def terminate(
        self,
        *,
        command: TerminateTaskFinalizationCommand,
    ) -> TaskFinalizationSnapshot:
        """把无法继续的正式收口原子固化为 review / failed。"""

        result = await self._terminate(command=command)
        assert result is not None
        return result

    async def terminate_stalled(
        self,
        *,
        command: TerminateTaskFinalizationCommand,
        minimum_terminal_age_seconds: float,
    ) -> TaskFinalizationSnapshot | None:
        """Main 终态超龄不等于任务终态；先锁住并复核所有活动工作。"""
        return await self._terminate(
            command=command, minimum_terminal_age_seconds=minimum_terminal_age_seconds
        )

    async def _terminate(
        self,
        *,
        command: TerminateTaskFinalizationCommand,
        minimum_terminal_age_seconds: float | None = None,
    ) -> TaskFinalizationSnapshot | None:

        legacy_status = (
            "review"
            if command.execution_status is TaskExecutionStatus.NEEDS_REVIEW
            else "failed"
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _lock_task_finalization(connection, command.task_id)
            await _assert_recovery_fence(connection, command)
            if minimum_terminal_age_seconds is not None:
                web_checkpoint_table = await connection.fetchval(
                    "SELECT to_regclass('qc_web_run_stage_checkpoint') IS NOT NULL"
                )
                ready_sql = (
                    STALLED_FINALIZATION_READY_SQL
                    if web_checkpoint_table
                    else STALLED_FINALIZATION_READY_SQL_WITHOUT_WEB_CHECKPOINT
                )
                ready = await connection.fetchval(
                    ready_sql,
                    command.task_id,
                    command.agent_run_id,
                    minimum_terminal_age_seconds,
                )
                if not ready:
                    return None
            task = await connection.fetchrow(
                """
                SELECT
                    id::text AS task_id,
                    finalization_state,
                    finalization_epoch,
                    finalization_row_version AS row_version
                FROM qc_task
                WHERE id = $1::uuid
                  AND (status IN ('running', $5)
                       OR (target_platform='h5' AND status='review'))
                  AND finalization_state = 'session_draining'
                  AND finalization_epoch = $2
                  AND finalization_row_version = $3
                  AND finalization_agent_run_id = $4::uuid
                  AND EXISTS (
                      SELECT 1
                      FROM qc_agent_run AS terminal_run
                      WHERE terminal_run.id = $4::uuid
                        AND terminal_run.task_id = qc_task.id
                        AND terminal_run.status IN (
                            'done', 'failed', 'aborted', 'canceled'
                        )
                  )
                FOR UPDATE
                """,
                command.task_id,
                command.expected_finalization_epoch,
                command.expected_row_version,
                command.agent_run_id,
                legacy_status,
            )
            if task is None:
                if minimum_terminal_age_seconds is not None:
                    return None
                row = await connection.fetchrow(
                    _FINALIZATION_SELECT_SQL
                    + " WHERE id = $1::uuid"
                    + " AND status = $2"
                    + " AND finalization_state = 'sealed'"
                    + " AND finalization_epoch = $3"
                    + " AND finalization_agent_run_id = $4::uuid"
                    + " AND execution_status = $5",
                    command.task_id,
                    legacy_status,
                    command.expected_finalization_epoch,
                    command.agent_run_id,
                    command.execution_status.value,
                )
                if row is None:
                    raise TaskFinalizationPersistenceError(
                        "Task 权威终态写入失败，owner/epoch/版本或 Run 终态不匹配"
                    )
                return _parse_finalization(row)

            facts_row = await connection.fetchrow(
                _GATE_FACTS_SQL,
                command.task_id,
                command.execution_status.value,
            )
            if facts_row is None:
                raise TaskFinalizationPersistenceError("无法读取终态审计事实")
            facts = replace(
                _parse_gate_facts(facts_row),
                natural_evidence_presentation_violation_count=(
                    await _count_natural_evidence_presentation_violations(
                        connection, task_id=command.task_id
                    )
                ),
            )
            from services.api.app.infrastructure.db.h5_rule_inventory_completion_gate import (
                apply_h5_rule_inventory_gate,
            )

            # 异常终止也先审计 H5 清单，排空不能隐藏未完成或损坏的规则清单。
            facts = await apply_h5_rule_inventory_gate(
                connection, task_id=command.task_id, row=facts_row, facts=facts,
            )
            facts = await apply_app_rule_inventory_gate(
                connection, task_id=command.task_id, row=facts_row, facts=facts,
            )
            if minimum_terminal_age_seconds is not None and any(
                (
                    facts.pending_action_count,
                    facts.pending_capture_commit_count,
                    facts.active_session_request_count,
                    facts.background_execution_count,
                )
            ):
                return None
            unconfirmed_stop = (
                facts_row.get('inventory_target_platform') == 'h5'
                and await connection.fetchval(
                    f"SELECT EXISTS(SELECT 1 FROM qc_surface_session AS session "
                    f"WHERE session.task_id=$1::uuid AND {unconfirmed_h5_stop_sql()})",
                    command.task_id,
                )
            )
            if unconfirmed_stop:
                raise TaskFinalizationPersistenceError('H5_PHYSICAL_STOP_UNCONFIRMED')
            if facts_row.get('inventory_target_platform') == 'h5' and any((
                facts.pending_action_count, facts.pending_capture_commit_count,
                facts.active_session_request_count, facts.background_execution_count,
            )):
                raise TaskFinalizationPersistenceError('H5_EXECUTION_RESOURCES_NOT_DRAINED')
            evaluation_sequence = int(
                await connection.fetchval(
                    """
                    SELECT COALESCE(max(evaluation_sequence), 0) + 1
                    FROM qc_completion_gate_snapshot
                    WHERE task_id = $1::uuid
                    """,
                    command.task_id,
                )
            )
            snapshot = build_completion_gate_snapshot(
                task_id=command.task_id,
                finalization_epoch=command.expected_finalization_epoch,
                evaluation_sequence=evaluation_sequence,
                facts=facts,
                evaluated_at=facts_row["evaluated_at"],
            )
            await _insert_gate_snapshot(connection, snapshot)
            row = await connection.fetchrow(
                """
                UPDATE qc_task
                SET
                    status = $5,
                    finalization_state = 'sealed',
                    completion_gate_snapshot_id = $6,
                    execution_status = $7,
                    quality_outcome = 'unknown',
                    sealed_at = now(),
                    finished_at = COALESCE(finished_at, now()),
                    finalization_last_error = $8,
                    finalization_row_version = finalization_row_version + 1,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND (status IN ('running', $5)
                       OR (target_platform='h5' AND status='review'))
                  AND finalization_state = 'session_draining'
                  AND finalization_epoch = $2
                  AND finalization_row_version = $3
                  AND finalization_agent_run_id = $4::uuid
                RETURNING
                    id::text AS task_id,
                    finalization_state,
                    finalization_epoch,
                    finalization_row_version AS row_version,
                    finalization_agent_run_id::text AS finalization_agent_run_id,
                    completion_gate_snapshot_id,
                    execution_status,
                    quality_outcome
                """,
                command.task_id,
                command.expected_finalization_epoch,
                command.expected_row_version,
                command.agent_run_id,
                legacy_status,
                snapshot.gate_snapshot_id,
                command.execution_status.value,
                command.reason_code,
            )
            if row is None:
                raise TaskFinalizationPersistenceError("Task 权威终态提交时版本已变化")
        return _parse_finalization(row)

    async def record_review_policy_decision(
        self,
        *,
        command: RecordReviewPolicyDecisionCommand,
    ) -> TaskFinalizationSnapshot:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _lock_task_finalization(connection, command.task_id)
            task = await connection.fetchrow(
                """
                SELECT
                    id::text AS task_id,
                    review_generation,
                    finalization_state,
                    finalization_epoch,
                    finalization_row_version AS row_version
                FROM qc_task
                WHERE id = $1::uuid
                  AND status IN ('running', 'review')
                  AND finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND finalization_row_version = $2
                FOR UPDATE
                """,
                command.task_id,
                command.expected_row_version,
            )
            if task is None:
                raise TaskFinalizationPersistenceError(
                    "任务已封存、不可记录 Review Policy 决议或版本已变化"
                )
            open_review_count = int(
                await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM qc_review_item
                    WHERE task_id = $1::uuid
                      AND status IN ('pending', 'needs_recheck')
                    """,
                    command.task_id,
                )
                or 0
            )
            decision_id = await connection.fetchval(
                """
                INSERT INTO qc_review_policy_gate_decision AS existing (
                    decision_id, task_id, review_generation, policy_hash,
                    open_review_count, satisfied, rationale
                )
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7)
                ON CONFLICT (decision_id) DO UPDATE
                SET decision_id = existing.decision_id
                WHERE existing.task_id = EXCLUDED.task_id
                  AND existing.review_generation = EXCLUDED.review_generation
                  AND existing.policy_hash = EXCLUDED.policy_hash
                  AND existing.open_review_count = EXCLUDED.open_review_count
                  AND existing.satisfied = EXCLUDED.satisfied
                  AND existing.rationale = EXCLUDED.rationale
                RETURNING existing.decision_id
                """,
                command.decision_id,
                command.task_id,
                task["review_generation"],
                command.policy_hash,
                open_review_count,
                command.satisfied,
                command.rationale,
            )
            if decision_id is None:
                raise TaskFinalizationPersistenceError("Review Policy 决议幂等身份发生漂移")
            row = await connection.fetchrow(
                """
                UPDATE qc_task
                SET
                    review_policy_decision_id = $3,
                    finalization_retry_at = CASE
                        WHEN finalization_state IN (
                            'open', 'exploration_draining', 'session_draining'
                        )
                         AND finalization_recovery_state = 'active' THEN now()
                        ELSE finalization_retry_at
                    END,
                    finalization_row_version = finalization_row_version + 1,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND finalization_row_version = $2
                  AND review_generation = $4
                RETURNING
                    id::text AS task_id,
                    finalization_state,
                    finalization_epoch,
                    finalization_row_version AS row_version,
                    finalization_agent_run_id::text AS finalization_agent_run_id,
                    completion_gate_snapshot_id,
                    execution_status,
                    quality_outcome
                """,
                command.task_id,
                command.expected_row_version,
                command.decision_id,
                task["review_generation"],
            )
            if row is None:
                raise TaskFinalizationPersistenceError("Review 集合或任务版本已变化")
        return _parse_finalization(row)

    async def defer_unchanged_completion_gate(
        self,
        *,
        command: EvaluateCompletionGateCommand,
    ) -> bool:
        """报告生成前复核阻塞事实；不变时只安排复查，不封存或改写报告。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _lock_task_finalization(connection, command.task_id)
            await _assert_recovery_fence(connection, command)
            task = await connection.fetchrow(
                """
                SELECT
                    status AS task_status,
                    target_platform,
                    task_config->>'workflow_version' AS workflow_version,
                    finalization_epoch,
                    completion_gate_snapshot_id
                FROM qc_task
                WHERE id = $1::uuid
                  AND status IN ('running', 'review', 'done')
                  AND finalization_state = 'session_draining'
                  AND finalization_epoch = $2
                  AND finalization_agent_run_id = $3::uuid
                  AND finalization_recovery_state = 'active'
                  AND NOT EXISTS (
                      SELECT 1 FROM qc_agent_run AS owner_run
                      WHERE owner_run.id = $3::uuid
                        AND COALESCE(owner_run.output_context->>'cancel_requested', 'false')
                            = 'true'
                  )
                FOR UPDATE
                """,
                command.task_id,
                command.expected_finalization_epoch,
                command.agent_run_id,
            )
            if (
                task is None
                or task["completion_gate_snapshot_id"] is None
                or task["task_status"] == "done"
            ):
                # 历史完成标记尚未封存时，交由原 Gate 重新评估并纠正任务状态。
                return False
            if _requires_h5_review_status_transition(
                task=task,
                execution_status=command.execution_status,
            ):
                # Gate 事实即使不变，H5 的人工复核结论仍须先投影到任务终态。
                return False
            facts_row, facts = await _read_completion_gate_facts(connection, command)
            snapshot = await _load_unchanged_blocked_gate_snapshot(
                connection,
                task_id=command.task_id,
                finalization_epoch=int(task["finalization_epoch"]),
                gate_snapshot_id=task["completion_gate_snapshot_id"],
                facts=facts,
            )
            if snapshot is None:
                return False
            await _defer_blocked_completion_gate(
                connection,
                task_id=command.task_id,
                finalization_epoch=snapshot.finalization_epoch,
                gate_snapshot_id=snapshot.gate_snapshot_id,
                facts=facts,
                execution_inflight_work_count=facts_row.get("h5_execution_inflight_work_count"),
            )
            return True

    async def evaluate_and_seal(
        self,
        *,
        command: EvaluateCompletionGateCommand,
    ) -> CompletionGateDecision:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _lock_task_finalization(connection, command.task_id)
            await _assert_recovery_fence(connection, command)
            task = await connection.fetchrow(
                """
                SELECT
                    id::text AS task_id,
                    status AS task_status,
                    finalization_state,
                    finalization_epoch,
                    finalization_row_version AS row_version,
                    completion_gate_snapshot_id,
                    target_platform,
                    LOWER(task_config->>'workflow_version') AS workflow_version
                FROM qc_task
                WHERE id = $1::uuid
                  AND status IN ('running', 'review', 'done')
                  AND finalization_state = 'session_draining'
                  AND finalization_epoch = $2
                  AND finalization_row_version = $3
                                    AND finalization_agent_run_id = $4::uuid
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_agent_run AS finalization_run
                      WHERE finalization_run.id = $4::uuid
                        AND COALESCE(
                            (finalization_run.output_context->>'cancel_requested')::boolean,
                            FALSE
                        ) = TRUE
                  )
                FOR UPDATE
                """,
                command.task_id,
                command.expected_finalization_epoch,
                command.expected_row_version,
                command.agent_run_id,
            )
            if task is None:
                # 主 Worker 与后台恢复器都可能在同一 Session Draining 窗口内
                # 评估门禁。行锁已保证当前事实串行读取；若只有 row_version
                # 被同一 owner 的前一轮评估提升，重新读取当前版本即可继续，
                # 但 epoch、状态、owner 和持久化取消标记仍必须全部匹配。
                task = await connection.fetchrow(
                    """
                    SELECT
                        id::text AS task_id,
                        status AS task_status,
                        finalization_state,
                        finalization_epoch,
                        finalization_row_version AS row_version,
                        completion_gate_snapshot_id,
                        target_platform,
                        LOWER(task_config->>'workflow_version') AS workflow_version
                    FROM qc_task
                    WHERE id = $1::uuid
                      AND status IN ('running', 'review', 'done')
                      AND finalization_state = 'session_draining'
                      AND finalization_epoch = $2
                      AND finalization_agent_run_id = $3::uuid
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_agent_run AS finalization_run
                          WHERE finalization_run.id = $3::uuid
                            AND COALESCE(
                                (finalization_run.output_context->>'cancel_requested')::boolean,
                                FALSE
                            ) = TRUE
                      )
                    FOR UPDATE
                    """,
                    command.task_id,
                    command.expected_finalization_epoch,
                    command.agent_run_id,
                )
            if task is None:
                raise TaskFinalizationPersistenceError(
                    "任务尚未进入 Session Draining，或 epoch/版本已变化"
                )
            task_row_version = int(task["row_version"])
            facts_row, facts = await _read_completion_gate_facts(connection, command)
            unchanged_snapshot = await _load_unchanged_blocked_gate_snapshot(
                connection,
                task_id=command.task_id,
                finalization_epoch=int(task["finalization_epoch"]),
                gate_snapshot_id=(
                    None
                    if task.get("task_status") == "done"
                    or _requires_h5_review_status_transition(
                        task=task,
                        execution_status=command.execution_status,
                    )
                    else task.get("completion_gate_snapshot_id")
                ),
                facts=facts,
            )
            if unchanged_snapshot is not None:
                await _defer_blocked_completion_gate(
                    connection,
                    task_id=command.task_id,
                    finalization_epoch=int(task["finalization_epoch"]),
                    gate_snapshot_id=unchanged_snapshot.gate_snapshot_id,
                    facts=facts,
                    execution_inflight_work_count=facts_row.get("h5_execution_inflight_work_count"),
                )
                return CompletionGateDecision(
                    snapshot=unchanged_snapshot,
                    finalization=TaskFinalizationSnapshot(
                        task_id=command.task_id,
                        state=TaskFinalizationState.SESSION_DRAINING,
                        finalization_epoch=int(task["finalization_epoch"]),
                        row_version=int(task["row_version"]),
                        agent_run_id=command.agent_run_id,
                        gate_snapshot_id=unchanged_snapshot.gate_snapshot_id,
                    ),
                )
            evaluation_sequence = int(
                await connection.fetchval(
                    """
                    SELECT COALESCE(max(evaluation_sequence), 0) + 1
                    FROM qc_completion_gate_snapshot
                    WHERE task_id = $1::uuid
                    """,
                    command.task_id,
                )
            )
            snapshot_epoch = int(task["finalization_epoch"])
            if facts.gate_passed:
                snapshot_epoch += 1
                finalizing = await connection.fetchval(
                    """
                    UPDATE qc_task
                    SET
                        finalization_state = 'finalizing',
                        finalization_epoch = finalization_epoch + 1,
                        completion_gate_snapshot_id = NULL,
                        finalization_row_version = finalization_row_version + 1,
                        updated_at = now()
                    WHERE id = $1::uuid
                      AND finalization_state = 'session_draining'
                      AND finalization_epoch = $2
                      AND finalization_row_version = $3
                    RETURNING finalization_epoch
                    """,
                    command.task_id,
                    command.expected_finalization_epoch,
                    task_row_version,
                )
                if finalizing is None or int(finalizing) != snapshot_epoch:
                    raise TaskFinalizationPersistenceError("提升 finalization_epoch 失败")
            snapshot = build_completion_gate_snapshot(
                task_id=command.task_id,
                finalization_epoch=snapshot_epoch,
                evaluation_sequence=evaluation_sequence,
                facts=facts,
                evaluated_at=facts_row["evaluated_at"],
            )
            await _insert_gate_snapshot(connection, snapshot)
            if not facts.gate_passed:
                review_ready = _should_mark_task_review(
                    execution_status=command.execution_status,
                    facts=facts,
                    publication_review_required_count=int(
                        facts_row.get("h5_publication_review_required_count", 0) or 0
                    ),
                    h5_rule_inventory_missing_count=int(
                        facts_row.get("h5_rule_inventory_missing_count", 0) or 0
                    ),
                    allow_natural_evidence_review=(
                        str(task.get("target_platform") or "").casefold() == "h5"
                    ),
                    require_execution_drained=(
                        str(task.get("target_platform") or "").casefold() == "h5"
                    ),
                    execution_inflight_work_count=facts_row.get("h5_execution_inflight_work_count"),
                )
                row = await connection.fetchrow(
                    """
                    UPDATE qc_task
                    SET
                        status = CASE
                            WHEN $5::boolean OR status = 'done' THEN 'review'
                            ELSE status
                        END,
                        finished_at = CASE
                            WHEN $5::boolean THEN COALESCE(finished_at, now())
                            ELSE finished_at
                        END,
                        completion_gate_snapshot_id = $4,
                        -- 已进入人工复核投影时停止后台空转；事实变更入口再显式唤醒。
                        finalization_retry_at = CASE
                            WHEN $5::boolean THEN 'infinity'::timestamptz
                            ELSE now() + interval '30 seconds'
                        END,
                        finalization_row_version = finalization_row_version + 1,
                        updated_at = now()
                    WHERE id = $1::uuid
                      AND finalization_state = 'session_draining'
                      AND finalization_epoch = $2
                      AND finalization_row_version = $3
                    RETURNING
                        id::text AS task_id,
                        finalization_state,
                        finalization_epoch,
                        finalization_row_version AS row_version,
                        finalization_agent_run_id::text AS finalization_agent_run_id,
                        completion_gate_snapshot_id,
                        execution_status,
                        quality_outcome
                    """,
                    command.task_id,
                    command.expected_finalization_epoch,
                    task_row_version,
                    snapshot.gate_snapshot_id,
                    review_ready,
                )
            else:
                await connection.execute(
                    """
                    UPDATE qc_surface_session
                    SET
                        finalization_epoch = $2,
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE task_id = $1::uuid
                      AND finalization_epoch < $2
                      AND status IN ('closed', 'lost', 'failed')
                    """,
                    command.task_id,
                    snapshot_epoch,
                )
                legacy_status = _legacy_task_status(command.execution_status)
                row = await connection.fetchrow(
                    """
                    UPDATE qc_task
                    SET
                        status = $5,
                        finalization_state = 'sealed',
                        completion_gate_snapshot_id = $4,
                        execution_status = $6,
                        quality_outcome = $7,
                        sealed_at = now(),
                        finished_at = COALESCE(finished_at, now()),
                        finalization_row_version = finalization_row_version + 1,
                        updated_at = now()
                    WHERE id = $1::uuid
                      AND finalization_state = 'finalizing'
                      AND finalization_epoch = $2
                      AND finalization_row_version = $3 + 1
                    RETURNING
                        id::text AS task_id,
                        finalization_state,
                        finalization_epoch,
                        finalization_row_version AS row_version,
                        finalization_agent_run_id::text AS finalization_agent_run_id,
                        completion_gate_snapshot_id,
                        execution_status,
                        quality_outcome
                    """,
                    command.task_id,
                    snapshot_epoch,
                    task_row_version,
                    snapshot.gate_snapshot_id,
                    legacy_status,
                    command.execution_status.value,
                    command.quality_outcome.value,
                )
            if row is None:
                raise TaskFinalizationPersistenceError("保存 Completion Gate 结论失败")
        return CompletionGateDecision(
            snapshot=snapshot,
            finalization=_parse_finalization(row),
        )

    async def finalize_task_with_report(
        self,
        *,
        command: FinalizeTaskWithReportCommand,
    ) -> CompletionGateDecision:
        """在任务封存前原子完成 Agent Run、报告绑定和终态写入。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _lock_task_finalization(connection, command.task_id)
            await _assert_recovery_fence(connection, command)
            task = await connection.fetchrow(
                """
                SELECT
                    id::text AS task_id,
                    status AS task_status,
                    finalization_state,
                    finalization_epoch,
                    finalization_row_version AS row_version,
                    completion_gate_snapshot_id,
                    target_platform,
                    LOWER(task_config->>'workflow_version') AS workflow_version,
                    task_config->>'execution_mode' AS execution_mode,
                    task_config->>'web_inspection_notification_mode' AS notification_mode
                FROM qc_task
                WHERE id = $1::uuid
                  AND status IN ('running', 'review', 'done')
                  AND finalization_state = 'session_draining'
                  AND finalization_epoch = $2
                  AND finalization_row_version = $3
                                    AND finalization_agent_run_id = $4::uuid
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_agent_run AS finalization_run
                      WHERE finalization_run.id = $4::uuid
                        AND COALESCE(
                            (finalization_run.output_context->>'cancel_requested')::boolean,
                            FALSE
                        ) = TRUE
                  )
                FOR UPDATE
                """,
                command.task_id,
                command.expected_finalization_epoch,
                command.expected_row_version,
                command.agent_run_id,
            )
            if task is None:
                # 主 Worker 与后台恢复器都可能在同一 Session Draining 窗口内
                # 评估门禁。行锁已保证当前事实串行读取；若只有 row_version
                # 被同一 owner 的前一轮评估提升，重新读取当前版本即可继续，
                # 但 epoch、状态、owner 和持久化取消标记仍必须全部匹配。
                task = await connection.fetchrow(
                    """
                    SELECT
                        id::text AS task_id,
                        status AS task_status,
                        finalization_state,
                        finalization_epoch,
                        finalization_row_version AS row_version,
                        completion_gate_snapshot_id,
                        target_platform,
                        LOWER(task_config->>'workflow_version') AS workflow_version,
                        task_config->>'execution_mode' AS execution_mode,
                        task_config->>'web_inspection_notification_mode' AS notification_mode
                    FROM qc_task
                    WHERE id = $1::uuid
                      AND status IN ('running', 'review', 'done')
                      AND finalization_state = 'session_draining'
                      AND finalization_epoch = $2
                      AND finalization_agent_run_id = $3::uuid
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_agent_run AS finalization_run
                          WHERE finalization_run.id = $3::uuid
                            AND COALESCE(
                                (finalization_run.output_context->>'cancel_requested')::boolean,
                                FALSE
                            ) = TRUE
                      )
                    FOR UPDATE
                    """,
                    command.task_id,
                    command.expected_finalization_epoch,
                    command.agent_run_id,
                )
            if task is None:
                raise TaskFinalizationPersistenceError(
                    "任务尚未进入 Session Draining，或 epoch/版本已变化"
                )
            task_row_version = int(task["row_version"])
            facts_row, facts = await _read_completion_gate_facts(connection, command)
            unchanged_snapshot = await _load_unchanged_blocked_gate_snapshot(
                connection,
                task_id=command.task_id,
                finalization_epoch=int(task["finalization_epoch"]),
                gate_snapshot_id=(
                    None
                    if task.get("task_status") == "done"
                    or _requires_h5_review_status_transition(
                        task=task,
                        execution_status=command.execution_status,
                    )
                    else task.get("completion_gate_snapshot_id")
                ),
                facts=facts,
            )
            if unchanged_snapshot is not None:
                await _defer_blocked_completion_gate(
                    connection,
                    task_id=command.task_id,
                    finalization_epoch=int(task["finalization_epoch"]),
                    gate_snapshot_id=unchanged_snapshot.gate_snapshot_id,
                    facts=facts,
                    execution_inflight_work_count=facts_row.get("h5_execution_inflight_work_count"),
                )
                return CompletionGateDecision(
                    snapshot=unchanged_snapshot,
                    finalization=TaskFinalizationSnapshot(
                        task_id=command.task_id,
                        state=TaskFinalizationState.SESSION_DRAINING,
                        finalization_epoch=int(task["finalization_epoch"]),
                        row_version=int(task["row_version"]),
                        agent_run_id=command.agent_run_id,
                        gate_snapshot_id=unchanged_snapshot.gate_snapshot_id,
                    ),
                )
            evaluation_sequence = int(
                await connection.fetchval(
                    """
                    SELECT COALESCE(max(evaluation_sequence), 0) + 1
                    FROM qc_completion_gate_snapshot
                    WHERE task_id = $1::uuid
                    """,
                    command.task_id,
                )
            )
            snapshot_epoch = int(task["finalization_epoch"])
            is_web_formal = (
                str(task.get("target_platform") or "").casefold() == "web"
                and str(task.get("workflow_version") or "").casefold() == "v2"
                and task.get("execution_mode") == "full_inspection"
                and task.get("notification_mode") == "enforced"
            )
            # Web V2 对账已在当前 epoch 冻结 Coverage；正式报告必须与该不可变
            # Snapshot 共用身份。其他平台仍以新 epoch 表示通用封存轮次。
            reuse_current_epoch = (
                str(task.get("target_platform") or "").casefold() == "web"
                and str(task.get("workflow_version") or "").casefold() == "v2"
                and str(task.get("execution_mode") or "") == "full_inspection"
            )
            if facts.gate_passed and not reuse_current_epoch:
                snapshot_epoch += 1
            snapshot = build_completion_gate_snapshot(
                task_id=command.task_id,
                finalization_epoch=snapshot_epoch,
                evaluation_sequence=evaluation_sequence,
                facts=facts,
                evaluated_at=facts_row["evaluated_at"],
            )
            if not facts.gate_passed:
                await _insert_gate_snapshot(connection, snapshot)
                review_ready = _should_mark_task_review(
                    execution_status=command.execution_status,
                    facts=facts,
                    publication_review_required_count=int(
                        facts_row.get("h5_publication_review_required_count", 0) or 0
                    ),
                    h5_rule_inventory_missing_count=int(
                        facts_row.get("h5_rule_inventory_missing_count", 0) or 0
                    ),
                    allow_natural_evidence_review=(
                        str(task.get("target_platform") or "").casefold() == "h5"
                    ),
                    require_execution_drained=(
                        str(task.get("target_platform") or "").casefold() == "h5"
                    ),
                    execution_inflight_work_count=facts_row.get("h5_execution_inflight_work_count"),
                )
                row = await connection.fetchrow(
                    """
                    UPDATE qc_task
                    SET
                        status = CASE
                            WHEN $5::boolean OR status = 'done' THEN 'review'
                            ELSE status
                        END,
                        finished_at = CASE
                            WHEN $5::boolean THEN COALESCE(finished_at, now())
                            ELSE finished_at
                        END,
                        completion_gate_snapshot_id = $4,
                        -- 已进入人工复核投影时停止后台空转；事实变更入口再显式唤醒。
                        finalization_retry_at = CASE
                            WHEN $5::boolean THEN 'infinity'::timestamptz
                            ELSE now() + interval '30 seconds'
                        END,
                        finalization_row_version = finalization_row_version + 1,
                        updated_at = now()
                    WHERE id = $1::uuid
                      AND finalization_state = 'session_draining'
                      AND finalization_epoch = $2
                      AND finalization_row_version = $3
                    RETURNING
                        id::text AS task_id,
                        finalization_state,
                        finalization_epoch,
                        finalization_row_version AS row_version,
                        finalization_agent_run_id::text AS finalization_agent_run_id,
                        completion_gate_snapshot_id,
                        execution_status,
                        quality_outcome
                    """,
                    command.task_id,
                    command.expected_finalization_epoch,
                    task_row_version,
                    snapshot.gate_snapshot_id,
                    review_ready,
                )
            else:
                report_payload = _final_report_payload(
                    command.final_report,
                    report_id=command.report_id,
                    report_version=command.report_version,
                    snapshot=snapshot,
                )
                if (
                    task.get("target_platform") == "h5"
                    and task.get("workflow_version") == "v2"
                ):
                    try:
                        h5_coverage = await load_h5_report_coverage(
                            connection, task_id=command.task_id, agent_run_id=command.agent_run_id,
                            snapshot_id=facts.h5_coverage_snapshot_id,
                            snapshot_hash=facts.h5_coverage_snapshot_hash,
                            snapshot_epoch=facts.h5_coverage_snapshot_epoch,
                        )
                        bind_h5_report_coverage(
                            report_payload,
                            coverage=h5_coverage,
                            task_id=command.task_id,
                            agent_run_id=command.agent_run_id,
                            gate_snapshot_id=snapshot.gate_snapshot_id,
                            gate_finalization_epoch=snapshot.finalization_epoch,
                        )
                        from services.api.app.infrastructure.db.journey_report_binding import (
                            bind_journey_report,
                        )

                        await bind_journey_report(
                            connection,
                            report_payload,
                            task_id=command.task_id,
                            agent_run_id=command.agent_run_id,
                            epoch=snapshot.finalization_epoch,
                            source_epoch=int(task["finalization_epoch"]),
                        )
                    except ValueError as exc:
                        raise TaskFinalizationPersistenceError(
                            f"H5 正式报告绑定校验失败：{exc}"
                        ) from exc
                if is_web_formal:
                    try:
                        coverage_snapshot = await _load_validated_web_coverage_snapshot(
                            connection,
                            task_id=command.task_id,
                            agent_run_id=command.agent_run_id,
                            finalization_epoch=snapshot.finalization_epoch,
                        )
                        validate_web_final_report_binding(
                            report_payload,
                            expectation=WebFinalReportBindingExpectation(
                                task_id=command.task_id,
                                agent_run_id=command.agent_run_id,
                                report_id=command.report_id,
                                report_version=command.report_version,
                                report_payload_hash=stable_report_payload_hash(
                                    report_payload
                                ),
                                gate_snapshot_id=snapshot.gate_snapshot_id,
                                finalization_epoch=snapshot.finalization_epoch,
                                coverage_snapshot_id=str(
                                    facts_row["web_coverage_snapshot_id"]
                                ),
                                coverage_snapshot_hash=str(
                                    facts_row["web_coverage_snapshot_hash"]
                                ),
                                coverage_snapshot=coverage_snapshot,
                            ),
                        )
                    except (
                        KeyError,
                        WebFinalReportBindingError,
                        TaskFinalizationPersistenceError,
                    ) as exc:
                        raise TaskFinalizationPersistenceError(
                            f"Web 正式报告绑定校验失败：{exc}"
                        ) from exc
                run_updated = await connection.fetchrow(
                    """
                    UPDATE qc_agent_run
                    SET
                        output_context = jsonb_set(
                            COALESCE(output_context, '{}'::jsonb),
                            '{final_report}',
                            $3::jsonb,
                            true
                        ),
                        updated_at = now()
                    WHERE id = $1::uuid
                      AND task_id = $2::uuid
                    RETURNING id
                    """,
                    command.agent_run_id,
                    command.task_id,
                    _json_dumps(report_payload),
                )
                if run_updated is None:
                    raise TaskFinalizationPersistenceError(
                        "最终报告绑定的 Agent Run 不存在或不属于当前任务"
                    )
                finalizing = await connection.fetchval(
                    """
                    UPDATE qc_task
                    SET
                        finalization_state = 'finalizing',
                        finalization_epoch = $4,
                        completion_gate_snapshot_id = NULL,
                        finalization_row_version = finalization_row_version + 1,
                        updated_at = now()
                    WHERE id = $1::uuid
                      AND finalization_state = 'session_draining'
                      AND finalization_epoch = $2
                      AND finalization_row_version = $3
                    RETURNING finalization_epoch
                    """,
                    command.task_id,
                    command.expected_finalization_epoch,
                    task_row_version,
                    snapshot_epoch,
                )
                if finalizing is None or int(finalizing) != snapshot_epoch:
                    raise TaskFinalizationPersistenceError("切换 finalization_epoch 失败")
                await _insert_gate_snapshot(connection, snapshot)
                await connection.execute(
                    """
                    UPDATE qc_surface_session
                    SET
                        finalization_epoch = $2,
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE task_id = $1::uuid
                      AND finalization_epoch < $2
                      AND status IN ('closed', 'lost', 'failed')
                    """,
                    command.task_id,
                    snapshot_epoch,
                )
                binding_row = await connection.fetchrow(
                    """
                    INSERT INTO qc_report_gate_binding AS existing (
                        report_id, report_version, report_payload_hash,
                                                protocol_version, task_id, gate_snapshot_id,
                        finalization_epoch, execution_status, quality_outcome
                    )
                    VALUES ($1, $2, $3, $4, $5::uuid, $6, $7, $8, $9)
                    ON CONFLICT (report_id, report_version) DO UPDATE
                    SET report_id = existing.report_id
                    WHERE existing.report_payload_hash = EXCLUDED.report_payload_hash
                      AND existing.task_id = EXCLUDED.task_id
                      AND existing.gate_snapshot_id = EXCLUDED.gate_snapshot_id
                      AND existing.finalization_epoch = EXCLUDED.finalization_epoch
                      AND existing.execution_status = EXCLUDED.execution_status
                      AND existing.quality_outcome = EXCLUDED.quality_outcome
                    RETURNING *
                    """,
                    command.report_id,
                    command.report_version,
                    stable_report_payload_hash(report_payload),
                    REPORT_GATE_BINDING_PROTOCOL_VERSION,
                    command.task_id,
                    snapshot.gate_snapshot_id,
                    snapshot.finalization_epoch,
                    command.execution_status.value,
                    command.quality_outcome.value,
                )
                if binding_row is None:
                    raise TaskFinalizationPersistenceError("报告版本幂等身份发生漂移")
                await _insert_report_payload_snapshot(
                    connection,
                    report_id=command.report_id,
                    report_version=command.report_version,
                    report_payload_hash=stable_report_payload_hash(report_payload),
                    report_payload=report_payload,
                )
                legacy_status = _legacy_task_status(command.execution_status)
                row = await connection.fetchrow(
                    """
                    UPDATE qc_task
                    SET
                        status = $5,
                        finalization_state = 'sealed',
                        finalization_recovery_state = CASE
                            WHEN $8::boolean THEN 'verified'
                            ELSE finalization_recovery_state
                        END,
                        completion_gate_snapshot_id = $4,
                        execution_status = $6,
                        quality_outcome = $7,
                        sealed_at = now(),
                        finished_at = COALESCE(finished_at, now()),
                        finalization_row_version = finalization_row_version + 1,
                        updated_at = now()
                    WHERE id = $1::uuid
                      AND finalization_state = 'finalizing'
                      AND finalization_epoch = $2
                      AND finalization_row_version = $3 + 1
                    RETURNING
                        id::text AS task_id,
                        finalization_state,
                        finalization_epoch,
                        finalization_row_version AS row_version,
                        finalization_agent_run_id::text AS finalization_agent_run_id,
                        completion_gate_snapshot_id,
                        execution_status,
                        quality_outcome
                    """,
                    command.task_id,
                    snapshot_epoch,
                    task_row_version,
                    snapshot.gate_snapshot_id,
                    legacy_status,
                    command.execution_status.value,
                    command.quality_outcome.value,
                    is_web_formal and command.recovery_lease_token is None,
                )
            if row is None:
                raise TaskFinalizationPersistenceError("保存 Completion Gate 结论失败")
        return CompletionGateDecision(
            snapshot=snapshot,
            finalization=_parse_finalization(row),
        )

    async def bind_report(
        self,
        *,
        command: BindReportToGateCommand,
    ) -> ReportGateBinding:
        pool = await self._get_pool()
        report_payload = normalize_report_payload(command.final_report)
        report_payload_hash = stable_report_payload_hash(report_payload)
        if report_payload_hash != command.report_payload_hash:
            raise TaskFinalizationPersistenceError("报告载荷哈希与持久化内容不一致")
        async with pool.acquire() as connection, connection.transaction():
            await _lock_task_finalization(connection, command.task_id)
            await _assert_recovery_fence(connection, command)
            task = await connection.fetchrow(
                """
                SELECT
                    task.execution_status,
                    task.quality_outcome,
                    task.finalization_agent_run_id::text AS agent_run_id,
                    task.target_platform,
                    LOWER(task.task_config->>'workflow_version') AS workflow_version,
                    task.task_config->>'execution_mode' AS execution_mode,
                    task.task_config->>'web_inspection_notification_mode' AS notification_mode,
                    coverage.id::text AS web_coverage_snapshot_id,
                    coverage.snapshot_hash AS web_coverage_snapshot_hash,
                    gate.h5_coverage_snapshot_id,
                    gate.h5_coverage_snapshot_hash,
                    gate.h5_coverage_snapshot_epoch
                FROM qc_task AS task
                INNER JOIN qc_completion_gate_snapshot AS gate
                    ON gate.gate_snapshot_id = task.completion_gate_snapshot_id
                   AND gate.task_id = task.id
                   AND gate.finalization_epoch = task.finalization_epoch
                   AND gate.gate_passed
                LEFT JOIN qc_web_coverage_snapshot AS coverage
                    ON coverage.task_id = task.id
                   AND coverage.agent_run_id = task.finalization_agent_run_id
                   AND coverage.finalization_epoch = task.finalization_epoch
                WHERE task.id = $1::uuid
                  AND task.finalization_state = 'sealed'
                  AND task.completion_gate_snapshot_id = $2
                  AND task.finalization_epoch = $3
                FOR UPDATE OF task
                """,
                command.task_id,
                command.gate_snapshot_id,
                command.finalization_epoch,
            )
            if task is None:
                raise TaskFinalizationPersistenceError(
                    "报告只能绑定当前任务同 epoch 的已放行 Completion Gate 快照"
                )
            if task.get("target_platform") == "h5" and task.get("workflow_version") == "v2":
                try:
                    if command.agent_run_id not in {None, str(task["agent_run_id"])}:
                        raise ValueError("H5 报告 Run 与任务 owner 不一致")
                    if (
                        report_payload.get("report_id") != command.report_id
                        or report_payload.get("report_binding_version") != command.report_version
                    ):
                        raise ValueError("H5 报告正文版本与绑定命令不一致")
                    h5_coverage = await load_h5_report_coverage(
                        connection,
                        task_id=command.task_id,
                        agent_run_id=str(task["agent_run_id"]),
                        snapshot_id=task["h5_coverage_snapshot_id"],
                        snapshot_hash=task["h5_coverage_snapshot_hash"],
                        snapshot_epoch=task["h5_coverage_snapshot_epoch"],
                    )
                    bind_h5_report_coverage(
                        report_payload,
                        coverage=h5_coverage,
                        task_id=command.task_id,
                        agent_run_id=str(task["agent_run_id"]),
                        gate_snapshot_id=command.gate_snapshot_id,
                        gate_finalization_epoch=command.finalization_epoch,
                    )
                    from services.api.app.infrastructure.db.journey_report_binding import (
                        bind_journey_report,
                    )

                    await bind_journey_report(connection, report_payload,
                        task_id=command.task_id, agent_run_id=str(task["agent_run_id"]),
                        epoch=command.finalization_epoch,
                        source_epoch=task["h5_coverage_snapshot_epoch"])
                    if stable_report_payload_hash(report_payload) != report_payload_hash:
                        raise ValueError("H5 报告缺少匹配的 Gate 元数据，禁止绑定时改写正文")
                except ValueError as exc:
                    raise TaskFinalizationPersistenceError(
                        f"H5 正式报告绑定校验失败：{exc}"
                    ) from exc
            if (
                str(task.get("target_platform") or "").casefold() == "web"
                and str(task.get("workflow_version") or "").casefold() == "v2"
                and task.get("execution_mode") == "full_inspection"
                and task.get("notification_mode") == "enforced"
            ):
                try:
                    if (
                        command.agent_run_id is not None
                        and command.agent_run_id != str(task["agent_run_id"])
                    ):
                        raise WebFinalReportBindingError("run identity 与任务 owner 不一致")
                    coverage_snapshot = await _load_validated_web_coverage_snapshot(
                        connection,
                        task_id=command.task_id,
                        agent_run_id=str(task["agent_run_id"]),
                        finalization_epoch=command.finalization_epoch,
                    )
                    validate_web_final_report_binding(
                        report_payload,
                        expectation=WebFinalReportBindingExpectation(
                            task_id=command.task_id,
                            agent_run_id=str(task["agent_run_id"]),
                            report_id=command.report_id,
                            report_version=command.report_version,
                            report_payload_hash=report_payload_hash,
                            gate_snapshot_id=command.gate_snapshot_id,
                            finalization_epoch=command.finalization_epoch,
                            coverage_snapshot_id=str(
                                task["web_coverage_snapshot_id"]
                            ),
                            coverage_snapshot_hash=str(
                                task["web_coverage_snapshot_hash"]
                            ),
                            coverage_snapshot=coverage_snapshot,
                        ),
                    )
                except (
                    KeyError,
                    WebFinalReportBindingError,
                    TaskFinalizationPersistenceError,
                ) as exc:
                    raise TaskFinalizationPersistenceError(
                        f"Web 正式报告绑定校验失败：{exc}"
                    ) from exc
            row = await connection.fetchrow(
                """
                INSERT INTO qc_report_gate_binding AS existing (
                    report_id, report_version, report_payload_hash,
                                        protocol_version, task_id, gate_snapshot_id,
                    finalization_epoch, execution_status, quality_outcome
                )
                VALUES ($1, $2, $3, $4, $5::uuid, $6, $7, $8, $9)
                ON CONFLICT (report_id, report_version) DO UPDATE
                SET report_id = existing.report_id
                WHERE existing.report_payload_hash = EXCLUDED.report_payload_hash
                  AND existing.task_id = EXCLUDED.task_id
                  AND existing.gate_snapshot_id = EXCLUDED.gate_snapshot_id
                  AND existing.finalization_epoch = EXCLUDED.finalization_epoch
                  AND existing.execution_status = EXCLUDED.execution_status
                  AND existing.quality_outcome = EXCLUDED.quality_outcome
                RETURNING *
                """,
                command.report_id,
                command.report_version,
                report_payload_hash,
                REPORT_GATE_BINDING_PROTOCOL_VERSION,
                command.task_id,
                command.gate_snapshot_id,
                command.finalization_epoch,
                task["execution_status"],
                task["quality_outcome"],
            )
            if row is None:
                raise TaskFinalizationPersistenceError("报告版本幂等身份发生漂移")
            await _insert_report_payload_snapshot(
                connection,
                report_id=command.report_id,
                report_version=command.report_version,
                report_payload_hash=report_payload_hash,
                report_payload=report_payload,
            )
        return _parse_report_binding(row)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def build_default_task_finalization_service(
    *,
    database_url: str | None = None,
    pool: Any | None = None,
) -> TaskFinalizationService:
    """组合默认 PostgreSQL 实现，便于 Orchestrator、Review、Report 独立接线。"""

    from services.api.app.application.aggregate_inspection_publication import (
        AggregateInspectionPublicationService,
    )
    from services.api.app.application.inspection_work_items import (
        InspectionWorkItemService,
    )
    from services.api.app.infrastructure.db.action_execution_repository import (
        PostgresActionExecutionRepository,
    )
    from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
        PostgresAggregateInspectionPublicationRepository,
    )
    from services.api.app.infrastructure.db.inspection_work_item_repository import (
        PostgresInspectionWorkItemRepository,
    )

    aggregate_publication = AggregateInspectionPublicationService(
        repository=PostgresAggregateInspectionPublicationRepository(
            database_url=database_url,
            pool=pool,
        )
    )

    return TaskFinalizationService(
        PostgresTaskFinalizationRepository(database_url=database_url, pool=pool),
        completion_gate_preparer=PostgresCompletionGatePreparer(
            action_executions=PostgresActionExecutionRepository(
                database_url=database_url,
                pool=pool,
            ),
            aggregate_publication=aggregate_publication,
            inspection_work_settlement=InspectionWorkItemService(
                repository=PostgresInspectionWorkItemRepository(
                    database_url=database_url,
                    pool=pool,
                )
            ),
        ),
    )


_FINALIZATION_SELECT_SQL = """
    SELECT
        id::text AS task_id,
        finalization_state,
        finalization_epoch,
        finalization_row_version AS row_version,
        finalization_agent_run_id::text AS finalization_agent_run_id,
        completion_gate_snapshot_id,
        execution_status,
        quality_outcome
    FROM qc_task
"""


# 对应产品脚本整份跑完才写收口标记，不以点到「我的」提前停。
# 规则执行或质检工作项近 3 分钟还有更新时不封口；停更后才按手动封口放行。
_HLAPP_SCRIPT_SEAL_READY_SQL = """
    {result}.outcome = 'exploration_completed'
    AND {result}.agent_run_id = {task}.finalization_agent_run_id
    AND (
        {result}.coverage_summary->>'hlapp_script_completed' = 'true'
        OR {result}.coverage_summary->>'hjll_script_completed' = 'true'
        OR {result}.coverage_summary->>'hlwapp_script_completed' = 'true'
        OR {result}.coverage_summary->>'jmcomic_script_completed' = 'true'
        OR {result}.coverage_summary->>'jinguo_script_completed' = 'true'
        OR {result}.coverage_summary->>'yms_script_completed' = 'true'
        OR {result}.coverage_summary->>'pzhan_script_completed' = 'true'
        OR {result}.coverage_summary->>'pc51_script_completed' = 'true'
        OR {result}.coverage_summary->>'hlwwhite_script_completed' = 'true'
        OR {result}.coverage_summary->>'head_script_completed' = 'true'
        OR {result}.coverage_summary->>'xingba_script_completed' = 'true'
        OR {result}.coverage_summary->>'latest_script_completed' = 'true'
        OR {result}.coverage_summary->>'mama_script_completed' = 'true'
    )
    AND {result}.coverage_summary->>'coverage_complete' = 'true'
    AND {result}.coverage_summary->>'visual_quality_satisfied' = 'true'
    AND {result}.coverage_summary
        ->'app_runtime_policy'
        ->'app_target_snapshot'
        ->>'target_hash'
        = {task}.task_config->'app_exploration_target'->>'target_hash'
    AND NOT EXISTS (
        SELECT 1
        FROM qc_rule_execution AS rule_activity
        WHERE rule_activity.task_id = {task}.id
          AND rule_activity.updated_at > now() - interval '3 minutes'
    )
    AND NOT EXISTS (
        SELECT 1
        FROM qc_inspection_work_item AS work_activity
        WHERE work_activity.task_id = {task}.id
          AND work_activity.updated_at > now() - interval '3 minutes'
    )
"""
_HLAPP_SCRIPT_SEAL_ON_RESULT_SQL = _HLAPP_SCRIPT_SEAL_READY_SQL.format(
    result="result",
    task="task",
)
_HLAPP_SCRIPT_SEAL_ON_SCRIPTED_SQL = _HLAPP_SCRIPT_SEAL_READY_SQL.format(
    result="scripted",
    task="task",
)
_HLAPP_SCRIPT_SEAL_ON_SCRIPT_TASK_SQL = _HLAPP_SCRIPT_SEAL_READY_SQL.format(
    result="scripted",
    task="script_task",
)


_GATE_FACTS_SQL = f"""
    WITH target_task AS (
        SELECT
            task.id,
            task.target_platform,
            task.task_config,
            task.ruleset_id,
            task.channel,
            task.finalization_epoch,
            task.finalization_agent_run_id,
            task.review_generation,
            task.review_policy_decision_id
        FROM qc_task AS task
        WHERE task.id = $1::uuid
    ), exploration AS (
        SELECT
            count(*) FILTER (WHERE work_type = 'exploration')::integer AS total,
            count(*) FILTER (
                WHERE work_type = 'exploration'
                  AND status NOT IN (
                      'succeeded', 'bounded', 'budget_exhausted', 'blocked',
                      'no_progress', 'failed', 'cancelled', 'superseded'
                  )
            )::integer AS nonterminal
        FROM qc_v2_work_item
        WHERE task_id = $1::uuid
    ), h5_coverage AS (
        SELECT
            CASE
                WHEN task.target_platform <> 'h5' THEN TRUE
                ELSE COALESCE(coverage.coverage_satisfied, FALSE)
            END AS completion_ready,
            coverage.coverage_snapshot_id AS snapshot_id,
            coverage.payload_hash AS snapshot_hash,
            coverage.finalization_epoch AS snapshot_epoch
        FROM target_task AS task
        LEFT JOIN LATERAL (
            SELECT
                snapshot.coverage_snapshot_id,
                snapshot.payload_hash,
                snapshot.finalization_epoch,
                (
                    snapshot.completion_ready
                    AND jsonb_array_length(snapshot.required_page_types) > 0
                ) AS coverage_satisfied
            FROM qc_h5_coverage_snapshot AS snapshot
            WHERE task.target_platform = 'h5'
              AND snapshot.task_id = task.id
              AND snapshot.finalization_epoch = task.finalization_epoch
              AND (
                    task.finalization_agent_run_id IS NULL
                    OR snapshot.agent_run_id = task.finalization_agent_run_id
                  )
            ORDER BY snapshot.revision DESC
            LIMIT 1
        ) AS coverage ON TRUE
    ), web_coverage AS (
        SELECT
            (
                task.target_platform = 'web'
                AND task.task_config->>'execution_mode' = 'full_inspection'
                AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
            ) AS required,
            CASE
                WHEN NOT (
                    task.target_platform = 'web'
                    AND task.task_config->>'execution_mode' = 'full_inspection'
                    AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
                ) THEN TRUE
                ELSE COALESCE(
                    snapshot.coverage_status IN ('complete', 'completed_with_gaps')
                    AND NULLIF(snapshot.snapshot_hash, '') IS NOT NULL
                    AND snapshot.normalization_version
                        IN ('web-page-type-normalization.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4')
                    AND jsonb_array_length(snapshot.required_page_types) > 0
                    AND (
                        (snapshot.coverage_status = 'complete'
                         AND jsonb_array_length(snapshot.missing_page_types) = 0)
                        OR
                        (snapshot.coverage_status = 'completed_with_gaps'
                         AND jsonb_array_length(snapshot.missing_page_types) > 0)
                    ),
                    FALSE
                )
            END AS completion_ready,
            snapshot.id::text AS snapshot_id,
            snapshot.snapshot_hash,
            snapshot.finalization_epoch AS snapshot_epoch,
            snapshot.coverage_status
        FROM target_task AS task
        LEFT JOIN LATERAL (
            SELECT snapshot.*
            FROM qc_web_coverage_snapshot AS snapshot
            WHERE snapshot.task_id = task.id
              AND snapshot.agent_run_id = task.finalization_agent_run_id
              AND snapshot.finalization_epoch = task.finalization_epoch
            LIMIT 1
        ) AS snapshot ON TRUE
    ), web_gap_inventory AS (
        SELECT
            count(rule.id)::integer AS candidate_count,
            count(rule.id) FILTER (
                WHERE execution.id IS NOT NULL
                  AND execution.execution_status = 'evidence_insufficient'
                  AND execution.page_capture_id IS NULL
                  AND execution.source_idempotency_key = (
                      'web-coverage-gap:' || snapshot.id::text || ':'
                      || missing.page_type || ':' || rule.rule_code
                  )
                  AND execution.judge_input->>'schema_version'
                      = 'web-coverage-gap.v1'
                  AND execution.judge_input->>'source' = 'web_coverage_gap'
                  AND execution.judge_input->>'coverage_snapshot_id'
                      = snapshot.id::text
                  AND execution.judge_input->>'missing_page_type'
                      = missing.page_type
                  AND execution.judge_input->>'reason'
                      = 'required_page_type_not_discovered'
            )::integer AS terminal_count
        FROM target_task AS task
        LEFT JOIN qc_web_coverage_snapshot AS snapshot
            ON snapshot.task_id = task.id
           AND snapshot.agent_run_id = task.finalization_agent_run_id
           AND snapshot.finalization_epoch = task.finalization_epoch
           AND snapshot.coverage_status IN ('complete', 'completed_with_gaps')
        LEFT JOIN LATERAL jsonb_array_elements_text(
            COALESCE(snapshot.missing_page_types, '[]'::jsonb)
        ) AS missing(page_type) ON TRUE
        LEFT JOIN qc_rule AS rule
            ON snapshot.id IS NOT NULL
           AND rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
           AND {WEB_GUEST_GAP_RULE_APPLICABILITY_SQL}
        LEFT JOIN qc_rule_execution AS execution
            ON execution.task_id = task.id
           AND execution.agent_run_id = task.finalization_agent_run_id
           AND execution.rule_id = rule.id
           AND execution.page_capture_id IS NULL
           AND execution.source_idempotency_key = (
                'web-coverage-gap:' || snapshot.id::text || ':'
                || missing.page_type || ':' || rule.rule_code
               )
    ), app_coverage AS (
        SELECT
            CASE
                WHEN task.target_platform <> 'app' THEN TRUE
                ELSE COALESCE(
                    result.outcome = 'exploration_completed'
                    AND result.coverage_summary->>'coverage_complete' = 'true'
                    AND result.coverage_summary->>'visual_quality_satisfied' = 'true'
                    AND (
                        COALESCE(result.coverage_summary->>'completion_mode', '')
                            <> 'dynamic_discovery'
                        OR (
                            result.coverage_summary->>'assessment_scope_complete' = 'true'
                            AND COALESCE(
                                result.coverage_summary
                                    ->'page_type_applicability'
                                    ->>'assessment_scope_complete',
                                'false'
                            ) = 'true'
                            AND COALESCE(
                                jsonb_array_length(
                                    COALESCE(
                                        result.coverage_summary
                                            ->'page_type_applicability'
                                            ->'unresolved_applicability_page_types',
                                        '[]'::jsonb
                                    )
                                ),
                                0
                            ) = 0
                            AND COALESCE(
                                jsonb_array_length(
                                    COALESCE(
                                        result.coverage_summary
                                            ->'page_type_applicability'
                                            ->'known_uncovered_page_types',
                                        '[]'::jsonb
                                    )
                                ),
                                0
                            ) = 0
                        )
                    )
                    AND result.coverage_summary
                        ->'app_runtime_policy'
                        ->'app_target_snapshot'
                        ->>'target_hash'
                        = task.task_config
                            ->'app_exploration_target'
                            ->>'target_hash'
                    AND (
                        {_HLAPP_SCRIPT_SEAL_ON_RESULT_SQL}
                        OR NOT EXISTS (
                            SELECT 1
                            FROM qc_app_visual_observation AS visual
                            WHERE visual.task_id = task.id
                              AND visual.session_id = result.surface_session_id
                              AND visual.surface_facts
                                  ->'visual_provider_quality'
                                  ->>'status' = 'degraded'
                        )
                    )
                    AND (
                        -- Gate SQL 读图：仅当覆盖摘要声明 mutates_completion_gate_sql=true
                        -- （生产 M7 + ALIOTH_APP_GATE_SQL_CONSUMES_GRAPH）时生效。
                        COALESCE(
                            result.coverage_summary
                                ->'app_gate_graph_shadow'
                                ->>'mutates_completion_gate_sql',
                            'false'
                        ) <> 'true'
                        OR (
                            COALESCE(
                                result.coverage_summary
                                    ->'app_gate_graph_shadow'
                                    ->>'page_graph_consistent',
                                'false'
                            ) = 'true'
                            AND COALESCE(
                                jsonb_array_length(
                                    COALESCE(
                                        result.coverage_summary
                                            ->'app_gate_graph_shadow'
                                            ->'would_block_reasons',
                                        '[]'::jsonb
                                    )
                                ),
                                0
                            ) = 0
                        )
                    ),
                    FALSE
                )
            END AS completion_ready
        FROM target_task AS task
        LEFT JOIN LATERAL (
            SELECT result.*
            FROM qc_app_exploration_result AS result
            WHERE result.task_id = task.id
              AND (
                    task.finalization_agent_run_id IS NULL
                    OR result.agent_run_id = task.finalization_agent_run_id
                  )
            ORDER BY result.completed_at DESC
            LIMIT 1
        ) AS result ON TRUE
    ), actions AS (
        SELECT
            count(*) FILTER (
                WHERE status IN ('planned', 'executing', 'evidence_pending')
            )::integer AS pending,
            count(*) FILTER (WHERE status = 'evidence_pending')::integer
                AS pending_evidence
        FROM qc_action_execution
        WHERE task_id = $1::uuid
    ), capture_commits AS (
        SELECT
            (
                SELECT count(*)
                FROM qc_capture
                WHERE task_id = $1::uuid AND capture_status = 'pending'
            )
            + (
                SELECT count(*)
                FROM (
                    SELECT DISTINCT ON (manifest.capture_id)
                        manifest.capture_id,
                        manifest.quality_status
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.task_id = $1::uuid
                    ORDER BY
                        manifest.capture_id,
                        manifest.manifest_version DESC
                ) AS latest_manifest
                CROSS JOIN target_task AS task
                WHERE task.target_platform NOT IN ('h5', 'web')
                  AND latest_manifest.quality_status = 'pending'
            ) AS pending
    ), page_enforced_work AS (
        SELECT count(*)::integer AS nonterminal
        FROM qc_inspection_work_item AS work_item
        INNER JOIN target_task AS task
            ON task.id = work_item.task_id
           AND work_item.finalization_epoch = task.finalization_epoch
        WHERE work_item.control_mode = 'enforced'
          AND work_item.status NOT IN (
              'succeeded', 'partial', 'failed', 'cancelled', 'superseded'
          )
    ), {QUALIFIED_H5_INVENTORY_CTE_SQL}, qualified_h5_captures AS (
        SELECT
            count(qualified.after_capture_id)::integer AS qualified_capture_count,
            count(qualified.after_capture_id) FILTER (
                WHERE task.target_platform <> 'web'
                  AND publication.id IS NULL
            )::integer AS missing_publication_count
        FROM target_task AS task
        LEFT JOIN qualified_h5_inventory AS qualified ON TRUE
        LEFT JOIN qc_h5_inspection_publication AS publication
            ON publication.task_id = task.id
           AND publication.page_capture_id = qualified.after_capture_id
           AND publication.finalization_epoch = task.finalization_epoch
           AND publication.mode = 'enforced'
    ), {AUDITABLE_H5_INVENTORY_CTE_SQL}, {WEB_COMPLETION_GATE_INVENTORY_CTES_SQL}, h5_publication_gate AS (
        SELECT
            count(publication.id) FILTER (
                WHERE publication.status IN ('pending', 'leased', 'retry_wait')
            ) AS execution_inflight,
            count(publication.id) FILTER (
            WHERE publication.status IN ('pending', 'leased', 'retry_wait')
               OR publication.status IN ('cancelled', 'superseded')
               OR (
                    publication.status IN ('published', 'not_applicable')
                    AND (
                        publication.expected_batch_count IS DISTINCT FROM
                            publication.published_work_item_count
                        OR publication.expected_batch_count IS DISTINCT FROM (
                            SELECT count(*)
                            FROM qc_h5_inspection_publication_work_item AS link
                            WHERE link.publication_id = publication.id
                        )
                        OR publication.candidate_rule_count IS DISTINCT FROM (
                            SELECT count(*)
                            FROM qc_h5_inspection_publication_rule AS member
                            WHERE member.publication_id = publication.id
                        )
                        OR CASE WHEN task.target_platform = 'web'
                            AND task.task_config->>'workflow_version' = 'v2'
                            AND publication.web_evidence_sequence_id IS NOT NULL
                            THEN NOT ({WEB_SEQUENCE_PUBLICATION_INVENTORY_MATCH_SQL})
                            ELSE qualified.published_inspection_count IS DISTINCT FROM
                                publication.published_work_item_count
                            END
                        OR EXISTS (
                            SELECT 1
                            FROM qc_inspection_work_item AS live_work
                            WHERE live_work.task_id = publication.task_id
                              AND live_work.page_capture_id = publication.page_capture_id
                              AND live_work.producer_agent_run_id =
                                  publication.source_agent_run_id
                              AND live_work.session_owner_agent_run_id =
                                  publication.source_agent_run_id
                              AND live_work.ruleset_id = publication.ruleset_id
                              AND live_work.ruleset_version = publication.ruleset_version
                              AND live_work.ruleset_snapshot_hash
                                  IS NOT DISTINCT FROM publication.ruleset_snapshot_hash
                              AND live_work.finalization_epoch =
                                  publication.finalization_epoch
                              AND live_work.session_dependency = 'active_session'
                              AND live_work.control_mode = 'enforced'
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM qc_h5_inspection_publication_work_item AS live_link
                                  WHERE live_link.publication_id = publication.id
                                    AND live_link.work_item_id = live_work.id
                              )
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM qc_h5_inspection_publication_rule AS live_member
                            WHERE live_member.publication_id = publication.id
                              AND live_member.session_dependency = 'active_session'
                              AND COALESCE(
                                  live_member.execution_plan->>'disposition', ''
                              ) <> 'needs_review'
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM qc_h5_inspection_publication_work_item AS live_link
                                  INNER JOIN qc_inspection_work_item_rule AS work_member
                                      ON work_member.work_item_id = live_link.work_item_id
                                  WHERE live_link.publication_id = publication.id
                                    AND work_member.rule_id = live_member.rule_id
                              )
                        )
                    )
               )
        )::integer AS active_or_inconsistent,
        count(publication.id) FILTER (
            WHERE publication.status IN ('failed', 'needs_review')
        )::integer AS review_required
        FROM qc_h5_inspection_publication AS publication
        INNER JOIN target_task AS task
           ON task.id = publication.task_id
           AND publication.finalization_epoch = task.finalization_epoch
        LEFT JOIN auditable_h5_inventory AS qualified
            ON qualified.after_capture_id = publication.page_capture_id
           AND qualified.agent_run_id = publication.source_agent_run_id
        WHERE publication.mode = 'enforced'
    ), app_sealed_index AS (
        SELECT
            count(index.index_id)::integer AS sealed_index_count,
            count(index.index_id) FILTER (
                WHERE publication.id IS NULL
            )::integer AS missing_publication_count
        FROM target_task AS task
        LEFT JOIN qc_app_evidence_index AS index
            ON index.task_id = task.id
           AND index.agent_run_id = task.finalization_agent_run_id
           AND index.status = 'sealed'
        LEFT JOIN qc_app_inspection_publication AS publication
            ON publication.task_id = task.id
           AND publication.source_agent_run_id = task.finalization_agent_run_id
           AND publication.evidence_index_id = index.index_id
           AND publication.evidence_index_hash = index.content_sha256
           AND publication.finalization_epoch = task.finalization_epoch
        WHERE task.target_platform = 'app'
          AND task.task_config->>'execution_mode' = 'full_inspection'
    ), app_publication_gate AS (
        SELECT
            count(publication.id) FILTER (
                WHERE publication.status IN ('pending', 'leased', 'retry_wait')
                   OR publication.status IN ('cancelled', 'superseded')
                   OR (
                        publication.status IN (
                            'published', 'not_applicable', 'needs_review', 'failed'
                        )
                        AND (
                            publication.expected_entry_count IS DISTINCT FROM
                                publication.terminal_entry_count
                            OR publication.expected_entry_count IS DISTINCT FROM (
                                SELECT count(*)
                                FROM qc_app_inspection_publication_entry AS entry
                                WHERE entry.publication_id = publication.id
                                  AND entry.status IN (
                                      'linked', 'not_applicable', 'needs_review'
                                  )
                            )
                            OR publication.expected_rule_count IS NULL
                            OR publication.expected_rule_count IS DISTINCT FROM (
                                SELECT count(*)
                                FROM qc_app_inspection_publication_rule AS member
                                WHERE member.publication_id = publication.id
                            )
                            OR publication.linked_work_item_count IS DISTINCT FROM (
                                SELECT count(*)
                                FROM qc_app_inspection_publication_work_item AS link
                                WHERE link.publication_id = publication.id
                            )
                            OR EXISTS (
                                SELECT 1
                                FROM qc_app_inspection_publication_rule AS member
                                WHERE member.publication_id = publication.id
                                  AND member.session_dependency = 'active_session'
                                  AND member.disposition NOT IN (
                                      'linked', 'needs_review'
                                  )
                            )
                            OR EXISTS (
                                SELECT 1
                                FROM qc_app_inspection_publication_rule AS member
                                WHERE member.publication_id = publication.id
                                  AND member.disposition = 'linked'
                                  AND NOT EXISTS (
                                      SELECT 1
                                      FROM qc_app_inspection_publication_work_item AS link
                                      INNER JOIN qc_inspection_work_item_rule AS work_member
                                          ON work_member.work_item_id = link.work_item_id
                                      WHERE link.publication_id = publication.id
                                        AND link.entry_id = member.entry_id
                                        AND link.work_item_id = member.work_item_id
                                        AND work_member.rule_id = member.rule_id
                                  )
                            )
                        )
                   )
            )::integer AS active_or_inconsistent,
            count(publication.id) FILTER (
                WHERE publication.status = 'failed'
                   OR (
                        publication.status = 'needs_review'
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_app_exploration_result AS scripted
                            WHERE scripted.task_id = task.id
                              AND {_HLAPP_SCRIPT_SEAL_ON_SCRIPTED_SQL}
                        )
                   )
            )::integer AS review_required
        FROM qc_app_inspection_publication AS publication
        INNER JOIN target_task AS task
            ON task.id = publication.task_id
           AND publication.finalization_epoch = task.finalization_epoch
        WHERE task.target_platform = 'app'
          AND task.task_config->>'execution_mode' = 'full_inspection'
    ), aggregate_expected AS (
        SELECT {aggregate_expected_rule_count_sql('task')} AS total
        FROM target_task AS task
    ), aggregate_candidate_gate AS (
        -- evidence_insufficient 候选已在同一事务写下证据不足的规则执行与待复核项，
        -- 人工部分由复核策略（open_review_count）承担，这里只按终态计、不再算未结算。
        SELECT
            count(candidate.id)::integer AS total,
            count(candidate.id) FILTER (
                WHERE candidate.status IN (
                    'completed', 'not_applicable', 'needs_review', 'failed',
                    'evidence_insufficient'
                )
            )::integer AS terminal,
            count(candidate.id) FILTER (
                WHERE candidate.status IN ('expected', 'published')
                   OR (
                        candidate.status IN ('published', 'completed')
                        AND NOT EXISTS (
                            SELECT 1
                            FROM qc_aggregate_inspection_work_item AS expected_work
                            WHERE expected_work.id = candidate.work_item_id
                              AND expected_work.task_id = candidate.task_id
                              AND expected_work.evidence_input_set_id =
                                  candidate.evidence_input_set_id
                              AND expected_work.rule_id = candidate.rule_id
                              AND expected_work.finalization_epoch =
                                  candidate.finalization_epoch
                        )
                   )
            )::integer AS active_or_inconsistent,
            count(candidate.id) FILTER (
                WHERE candidate.status IN ('needs_review', 'failed')
            )::integer AS review_required
        FROM qc_aggregate_inspection_candidate AS candidate
        INNER JOIN target_task AS task
            ON task.id = candidate.task_id
           AND candidate.finalization_epoch = task.finalization_epoch
    ), aggregate_work AS (
        SELECT
            count(*)::integer AS total,
            count(*) FILTER (
                WHERE work_item.status NOT IN (
                    'succeeded', 'evidence_insufficient', 'failed',
                    'cancelled', 'superseded'
                )
            )::integer AS nonterminal,
            count(*) FILTER (
                WHERE work_item.status IN (
                    'succeeded', 'evidence_insufficient', 'failed',
                    'cancelled', 'superseded'
                )
            )::integer AS terminal
        FROM qc_aggregate_inspection_work_item AS work_item
        INNER JOIN target_task AS task
            ON task.id = work_item.task_id
           AND work_item.finalization_epoch = task.finalization_epoch
        WHERE work_item.control_mode = 'enforced'
    ), page_candidate_coverage AS (
        SELECT
            count(member.id)::integer AS total,
            count(member.id) FILTER (
                WHERE member.status IN (
                    'written', 'evidence_insufficient', 'error', 'skipped'
                )
                  -- 无原合同的缺口已执行终结，但不能作为合格规则输入通过质量门禁。
                  AND member.last_error_code IS DISTINCT FROM 'H5_RESULT_INPUT_CONTRACT_UNVERIFIED'
            )::integer AS terminal
        FROM qc_inspection_work_item_rule AS member
        INNER JOIN qc_inspection_work_item AS work_item
            ON work_item.id = member.work_item_id
        INNER JOIN target_task AS task
            ON task.id = work_item.task_id
           AND work_item.finalization_epoch = task.finalization_epoch
        WHERE work_item.control_mode = 'enforced'
    ), app_inventory AS (
        SELECT
            EXISTS (
                SELECT 1
                FROM qc_capture AS capture
                WHERE capture.task_id = $1::uuid
                  AND capture.metadata->'inspection'->>'status'
                      IN ('no_rules_applicable', 'not_applicable')
            ) AS no_rules_applicable
    ), active_requests AS (
        SELECT count(*)::integer AS total
        FROM qc_evidence_request
        WHERE task_id = $1::uuid
          AND session_dependency = 'active_session'
          AND status IN ('pending', 'routed', 'running')
    ), reviews AS (
        SELECT count(*)::integer AS open_count
        FROM qc_review_item
        WHERE task_id = $1::uuid
          AND status IN ('pending', 'needs_recheck')
    ), valid_review_decision AS (
        SELECT decision.decision_id
        FROM target_task AS task
        INNER JOIN reviews ON TRUE
        INNER JOIN qc_review_policy_gate_decision AS decision
            ON decision.decision_id = task.review_policy_decision_id
           AND decision.task_id = task.id
           AND decision.review_generation = task.review_generation
           AND decision.open_review_count = reviews.open_count
           AND decision.satisfied
    ), background AS (
        SELECT (
            (
                SELECT count(*) FROM qc_v2_work_item
                WHERE task_id = $1::uuid
                  AND status NOT IN (
                      'succeeded', 'bounded', 'budget_exhausted', 'blocked',
                      'no_progress', 'failed', 'cancelled', 'superseded'
                  )
            )
            + (
                SELECT count(*) FROM qc_inspection_attempt AS attempt
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = attempt.work_item_id
                INNER JOIN target_task AS task
                    ON task.id = work_item.task_id
                   AND work_item.finalization_epoch = task.finalization_epoch
                WHERE work_item.task_id = $1::uuid
                  AND attempt.status IN ('running', 'judged')
            )
            + (
                SELECT count(*)
                FROM qc_aggregate_inspection_attempt AS attempt
                INNER JOIN qc_aggregate_inspection_work_item AS work_item
                    ON work_item.id = attempt.work_item_id
                INNER JOIN target_task AS task
                    ON task.id = work_item.task_id
                   AND work_item.finalization_epoch = task.finalization_epoch
                WHERE attempt.status = 'running'
            )
            + (
                SELECT count(*) FROM qc_journey_execution
                WHERE task_id = $1::uuid AND status = 'running'
            )
            + (
                SELECT count(*) FROM qc_h5_session_journey_guard
                WHERE task_id = $1::text AND status IN ('active','quarantined')
            )
            + (
                SELECT count(*) FROM qc_restoration_attempt
                WHERE task_id = $1::uuid AND status = 'running'
            )
            + (
                SELECT count(*) FROM qc_agent_run
                WHERE task_id = $1::uuid AND status IN ('pending', 'running')
            )
            + (
                SELECT count(*) FROM qc_surface_session AS session
                WHERE task_id = $1::uuid
                  -- Session Draining 阶段会话本就会处于 draining/closing；
                  -- 计入 background 会导致 Gate 永远无法通过。
                  AND (status IN ('starting', 'active') OR {unconfirmed_h5_stop_sql()})
            )
            + (
                SELECT count(*) FROM qc_evidence_request
                WHERE task_id = $1::uuid
                  AND status IN ('pending', 'routed', 'running')
            )
            + (
                SELECT count(*) FROM qc_outbox_event
                WHERE task_id = $1::uuid
                  AND status IN ('pending', 'publishing', 'retry_wait', 'dead_letter')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_delivery_operation_audit AS operation
                      WHERE operation.task_id = qc_outbox_event.task_id
                        AND operation.action = 'mark_needs_review'
                        AND operation.result = 'applied'
                        AND (
                            (
                                operation.platform = 'h5'
                                AND qc_outbox_event.aggregate_type =
                                    'h5_inspection_publication'
                                AND qc_outbox_event.aggregate_id =
                                    operation.publication_id::text
                            )
                            OR (
                                operation.platform = 'app'
                                AND qc_outbox_event.aggregate_type =
                                    'app_inspection_publication'
                                AND qc_outbox_event.aggregate_id =
                                    operation.publication_id::text
                            )
                            OR (
                                qc_outbox_event.aggregate_type =
                                    'inspection_work_item'
                                AND (
                                    (
                                        operation.platform = 'h5'
                                        AND EXISTS (
                                            SELECT 1
                                            FROM qc_h5_inspection_publication_work_item
                                                AS h5_link
                                            WHERE h5_link.publication_id =
                                                operation.publication_id
                                              AND h5_link.work_item_id::text =
                                                qc_outbox_event.aggregate_id
                                        )
                                    )
                                    OR (
                                        operation.platform = 'app'
                                        AND EXISTS (
                                            SELECT 1
                                            FROM qc_app_inspection_publication_work_item
                                                AS app_link
                                            WHERE app_link.publication_id =
                                                operation.publication_id
                                              AND app_link.work_item_id::text =
                                                qc_outbox_event.aggregate_id
                                        )
                                    )
                                )
                            )
                        )
                  )
                  -- 仅统计可路由的唤醒消息；app-workflow.ready 等非唤醒 Outbox
                  -- 不应阻塞 Completion Gate。
                  AND message_type = ANY(
                      ARRAY[
                          'alioth.v2.exploration-work.available',
                          'alioth.v2.probe-work.available',
                          'alioth.v2.inspection-work.available',
                          'alioth.v2.capture.committed',
                          'alioth.v2.evidence-bundle.ready',
                          'alioth.v2.evidence-supplement.attached',
                          'alioth.v2.h5-inspection-publication.available',
                          'alioth.v2.app-inspection-publication.available',
                          'alioth.v2.aggregate-inspection-work.available'
                      ]::varchar[]
                  )
            )
            + (
                SELECT count(*) FROM qc_evidence_exit_gate_evaluation
                WHERE task_id = $1::uuid
                  AND reservation_status = 'reserved'
                  AND expires_at > now()
            )
        )::integer AS total
    )
    SELECT
        target_task.task_config AS inventory_task_config,
        target_task.target_platform AS inventory_target_platform,
        target_task.ruleset_id::text AS inventory_ruleset_id,
        target_task.channel AS inventory_channel,
        (
            exploration.nonterminal = 0
            AND (
                exploration.total > 0
                OR $2::varchar IN ('needs_review', 'failed', 'canceled')
            )
        )
            AS exploration_terminal,
        actions.pending AS pending_action_count,
        (actions.pending_evidence + capture_commits.pending)::integer
            AS pending_capture_commit_count,
        (
            page_enforced_work.nonterminal + aggregate_work.nonterminal
                + GREATEST(
                    aggregate_expected.total - aggregate_candidate_gate.total,
                    0
                  )
                + aggregate_candidate_gate.active_or_inconsistent
                + aggregate_candidate_gate.review_required
                + qualified_h5_captures.missing_publication_count
                + web_classification_gate.missing_assessment_count
                + web_classification_gate.review_assessment_count
                + web_publication_gate.missing_publication_count
                + web_publication_gate.missing_member_count
                + h5_publication_gate.active_or_inconsistent
                + h5_publication_gate.review_required
                + app_sealed_index.missing_publication_count
                + app_publication_gate.active_or_inconsistent
                + app_publication_gate.review_required
        )::integer AS nonterminal_enforced_work_item_count,
        (
            page_enforced_work.nonterminal + aggregate_work.nonterminal
                + h5_publication_gate.execution_inflight
        )::integer AS h5_execution_inflight_work_count,
        (
            h5_publication_gate.review_required
                + app_publication_gate.review_required
        )::integer
            AS h5_publication_review_required_count,
        active_requests.total AS active_session_request_count,
        (
            page_candidate_coverage.total + aggregate_candidate_gate.total
                + web_gap_inventory.candidate_count
        )::integer AS candidate_rule_count,
        (
            page_candidate_coverage.terminal + aggregate_candidate_gate.terminal
                + web_gap_inventory.terminal_count
        )::integer AS terminal_candidate_rule_count,
        background.total AS background_execution_count,
        (
            reviews.open_count = 0
            OR EXISTS (SELECT 1 FROM valid_review_decision)
            OR EXISTS (
                SELECT 1
                FROM qc_app_exploration_result AS scripted
                INNER JOIN target_task AS script_task
                    ON script_task.id = scripted.task_id
                WHERE scripted.task_id = $1::uuid
                  AND {_HLAPP_SCRIPT_SEAL_ON_SCRIPT_TASK_SQL}
            )
        ) AS review_policy_satisfied,
        reviews.open_count AS open_review_count,
        target_task.review_generation,
        (SELECT decision_id FROM valid_review_decision LIMIT 1)
            AS review_policy_decision_id,
        (target_task.target_platform = 'app')
            AS require_nonempty_candidate_inventory,
        app_inventory.no_rules_applicable AS no_rules_applicable,
        app_coverage.completion_ready AS app_coverage_satisfied,
        h5_coverage.completion_ready AS h5_coverage_satisfied,
        h5_coverage.snapshot_id AS h5_coverage_snapshot_id,
        h5_coverage.snapshot_hash AS h5_coverage_snapshot_hash,
        h5_coverage.snapshot_epoch AS h5_coverage_snapshot_epoch,
        web_coverage.required AS web_coverage_required,
        web_coverage.completion_ready AS web_coverage_satisfied,
        web_coverage.snapshot_id AS web_coverage_snapshot_id,
        web_coverage.snapshot_hash AS web_coverage_snapshot_hash,
        web_coverage.snapshot_epoch AS web_coverage_snapshot_epoch,
        web_coverage.coverage_status AS web_coverage_status,
        now() AS evaluated_at,
        target_task.target_platform AS inventory_target_platform,
        target_task.task_config AS inventory_task_config,
        target_task.ruleset_id::text AS inventory_ruleset_id,
        target_task.channel AS inventory_channel
    FROM target_task
    CROSS JOIN exploration
    CROSS JOIN app_coverage
    CROSS JOIN h5_coverage
    CROSS JOIN web_coverage
    CROSS JOIN web_gap_inventory
    CROSS JOIN actions
    CROSS JOIN capture_commits
    CROSS JOIN page_enforced_work
    CROSS JOIN qualified_h5_captures
    CROSS JOIN web_classification_gate
    CROSS JOIN web_publication_gate
    CROSS JOIN h5_publication_gate
    CROSS JOIN app_sealed_index
    CROSS JOIN app_publication_gate
    CROSS JOIN aggregate_expected
    CROSS JOIN aggregate_candidate_gate
    CROSS JOIN aggregate_work
    CROSS JOIN page_candidate_coverage
    CROSS JOIN app_inventory
    CROSS JOIN active_requests
    CROSS JOIN reviews
    CROSS JOIN background
"""


async def _lock_task_finalization(connection: Any, task_id: str) -> None:
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"task-finalization:{task_id}",
    )


async def _load_validated_web_coverage_snapshot(
    connection: Any,
    *,
    task_id: str,
    agent_run_id: str,
    finalization_epoch: int,
) -> object:
    row = await connection.fetchrow(
        """
        SELECT *
        FROM qc_web_coverage_snapshot
        WHERE task_id = $1::uuid
          AND agent_run_id = $2::uuid
          AND finalization_epoch = $3
        """,
        task_id,
        agent_run_id,
        finalization_epoch,
    )
    if row is None:
        raise TaskFinalizationPersistenceError("缺少 Web Coverage Snapshot")
    try:
        return parse_web_coverage_snapshot_row(row)
    except (InvalidWebCoverageSnapshotRowError, TypeError, ValueError) as exc:
        raise TaskFinalizationPersistenceError(
            f"Web Coverage Snapshot 不可变校验失败：{exc}"
        ) from exc


async def _assert_recovery_fence(connection: Any, command: Any) -> None:
    """事务内拒绝过期 holder；无 fence 的正常入口也不能越过活跃 Web 租约。"""

    try:
        await assert_finalization_recovery_fence(
            connection,
            task_id=str(command.task_id),
            recovery_lease_token=getattr(command, "recovery_lease_token", None),
            recovery_generation=getattr(command, "recovery_generation", None),
            recovery_lease_expires_at=getattr(
                command,
                "recovery_lease_expires_at",
                None,
            ),
        )
    except FinalizationRecoveryFenceError as exc:
        raise TaskFinalizationPersistenceError(str(exc)) from exc


def _optional_recovery_fence_kwargs(
    *,
    recovery_lease_token: str | None,
    recovery_generation: int | None,
    recovery_lease_expires_at: object | None,
) -> dict[str, object]:
    if recovery_lease_token is None:
        return {}
    return {
        "recovery_lease_token": recovery_lease_token,
        "recovery_generation": recovery_generation,
        "recovery_lease_expires_at": recovery_lease_expires_at,
    }


async def _read_completion_gate_facts(
    connection: Any,
    command: EvaluateCompletionGateCommand | FinalizeTaskWithReportCommand,
) -> tuple[Any, CompletionGateFacts]:
    """预检与正式封存读取同一套权威事实，防止轻量检查漏掉证据门禁。"""

    row = await connection.fetchrow(
        _GATE_FACTS_SQL, command.task_id, command.execution_status.value,
    )
    if row is None:
        raise TaskFinalizationPersistenceError("无法读取 Completion Gate 事实")
    facts = replace(
        _parse_gate_facts(row),
        natural_evidence_presentation_violation_count=(
            await _count_natural_evidence_presentation_violations(
                connection, task_id=command.task_id,
            )
        ),
    )
    from services.api.app.infrastructure.db.h5_rule_inventory_completion_gate import (
        apply_h5_rule_inventory_gate,
    )

    candidate_rule_count = facts.candidate_rule_count
    facts = await apply_h5_rule_inventory_gate(
        connection, task_id=command.task_id, row=row, facts=facts,
    )
    # 清单缺口仍阻断正式报告，但不等于还有消费者工作正在执行。
    row = dict(row)
    row["h5_rule_inventory_missing_count"] = facts.candidate_rule_count - candidate_rule_count
    facts = await apply_app_rule_inventory_gate(
        connection, task_id=command.task_id, row=row, facts=facts,
    )
    if row.get("inventory_target_platform") == "h5":
        from services.api.app.infrastructure.db.journey_report_binding import (
            load_journey_report_binding,
        )

        binding = await load_journey_report_binding(connection, task_id=command.task_id)
        facts = replace(facts, journey_result_gap_count=binding["gap_count"])
        row["journey_result_gap_count"] = facts.journey_result_gap_count
    return row, facts


async def _load_unchanged_blocked_gate_snapshot(
    connection: Any,
    *,
    task_id: str,
    finalization_epoch: int,
    gate_snapshot_id: object,
    facts: CompletionGateFacts,
) -> CompletionGateSnapshot | None:
    """相同阻塞事实只保留一份快照，避免后台恢复器持续放大审计数据。"""

    if gate_snapshot_id is None or facts.gate_passed:
        return None
    facts_hash = stable_protocol_hash(facts.to_dict())
    row = await connection.fetchrow(
        """
        SELECT
            gate_snapshot_id,
            evaluation_sequence,
            protocol_version,
            facts_hash,
            gate_passed,
            evaluated_at
        FROM qc_completion_gate_snapshot
        WHERE gate_snapshot_id = $1
          AND task_id = $2::uuid
          AND finalization_epoch = $3
        """,
        str(gate_snapshot_id),
        task_id,
        finalization_epoch,
    )
    if (
        row is None
        or bool(row["gate_passed"])
        or str(row["facts_hash"]) != facts_hash
    ):
        return None
    return CompletionGateSnapshot(
        gate_snapshot_id=str(row["gate_snapshot_id"]),
        task_id=task_id,
        finalization_epoch=finalization_epoch,
        evaluation_sequence=int(row["evaluation_sequence"]),
        protocol_version=str(row["protocol_version"]),
        facts_hash=facts_hash,
        facts=facts,
        evaluated_at=row["evaluated_at"],
    )


async def _defer_blocked_completion_gate(
    connection: Any,
    *,
    task_id: str,
    finalization_epoch: int,
    gate_snapshot_id: str,
    facts: CompletionGateFacts,
    execution_inflight_work_count: int | None = None,
) -> None:
    """相同阻塞事实复用快照，等待事实变更入口显式唤醒。"""

    await connection.execute(
        """
        UPDATE qc_task
        SET finalization_retry_at = CASE
                -- 晚到资源尚在途时保持有限重查，排空后才等待新事实或人工复核。
                WHEN target_platform = 'h5' AND NOT $4::boolean
                    THEN now() + interval '30 seconds'
                -- H5/App 正式质检：复核投影后禁止同事实空转；Web 仍短间隔复查。
                WHEN target_platform IN ('h5', 'app') THEN 'infinity'::timestamptz
                WHEN status = 'review' THEN 'infinity'::timestamptz
                ELSE now() + interval '30 seconds'
            END
        WHERE id = $1::uuid
          AND finalization_state = 'session_draining'
          AND finalization_epoch = $2
          AND completion_gate_snapshot_id = $3
        """,
        task_id,
        finalization_epoch,
        gate_snapshot_id,
        _execution_resources_drained(
            facts, execution_inflight_work_count=execution_inflight_work_count,
        ),
    )


async def _insert_gate_snapshot(
    connection: Any,
    snapshot: CompletionGateSnapshot,
) -> None:
    facts = snapshot.facts
    inserted_id = await connection.fetchval(
        """
        INSERT INTO qc_completion_gate_snapshot (
            gate_snapshot_id, task_id, finalization_epoch,
            evaluation_sequence, protocol_version, facts_hash,
            exploration_terminal, no_pending_action,
            no_pending_capture_commit, no_nonterminal_enforced_work_item,
            no_active_session_request, candidate_rule_coverage_terminal,
            no_background_execution, review_policy_satisfied,
            pending_action_count, pending_capture_commit_count,
            nonterminal_enforced_work_item_count, active_session_request_count,
            candidate_rule_count, terminal_candidate_rule_count,
            background_execution_count, open_review_count, review_generation,
            review_policy_decision_id, block_reasons, gate_passed, evaluated_at,
            require_nonempty_candidate_inventory, no_rules_applicable,
            candidate_inventory_satisfied, app_coverage_satisfied,
            h5_coverage_satisfied, h5_coverage_snapshot_id,
            h5_coverage_snapshot_hash, h5_coverage_snapshot_epoch
        )
        VALUES (
            $1, $2::uuid, $3, $4, $5, $6,
            $7, $8, $9, $10, $11, $12, $13, $14,
            $15, $16, $17, $18, $19, $20, $21, $22, $23,
            $24, $25::jsonb, $26, $27, $28, $29, $30, $31, $32, $33, $34, $35
        )
        RETURNING gate_snapshot_id
        """,
        snapshot.gate_snapshot_id,
        snapshot.task_id,
        snapshot.finalization_epoch,
        snapshot.evaluation_sequence,
        snapshot.protocol_version,
        snapshot.facts_hash,
        facts.exploration_terminal,
        facts.no_pending_action,
        facts.no_pending_capture_commit,
        facts.no_nonterminal_enforced_work_item,
        facts.no_active_session_request,
        facts.candidate_rule_coverage_terminal,
        facts.no_background_execution,
        facts.review_policy_satisfied,
        facts.pending_action_count,
        facts.pending_capture_commit_count,
        facts.nonterminal_enforced_work_item_count,
        facts.active_session_request_count,
        facts.candidate_rule_count,
        facts.terminal_candidate_rule_count,
        facts.background_execution_count,
        facts.open_review_count,
        facts.review_generation,
        facts.review_policy_decision_id,
        _json_dumps([reason.value for reason in facts.block_reasons]),
        facts.gate_passed,
        snapshot.evaluated_at,
        facts.require_nonempty_candidate_inventory,
        facts.no_rules_applicable,
        facts.candidate_inventory_satisfied,
        facts.app_coverage_satisfied,
        facts.h5_coverage_satisfied,
        facts.h5_coverage_snapshot_id,
        facts.h5_coverage_snapshot_hash,
        facts.h5_coverage_snapshot_epoch,
    )
    if inserted_id != snapshot.gate_snapshot_id:
        raise TaskFinalizationPersistenceError("Completion Gate 快照保存失败")


async def _insert_report_payload_snapshot(
    connection: Any,
    *,
    report_id: str,
    report_version: int,
    report_payload_hash: str,
    report_payload: dict[str, object],
) -> None:
    row = await connection.fetchrow(
        """
        INSERT INTO qc_report_payload_snapshot AS existing (
            report_id, report_version, report_payload_hash, report_payload
        )
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (report_id, report_version) DO UPDATE
        SET report_id = existing.report_id
        WHERE existing.report_payload_hash = EXCLUDED.report_payload_hash
          AND existing.report_payload = EXCLUDED.report_payload
        RETURNING report_id
        """,
        report_id,
        report_version,
        report_payload_hash,
        _json_dumps(report_payload),
    )
    if row is None:
        raise TaskFinalizationPersistenceError("报告正文快照幂等身份发生漂移")


def _parse_gate_facts(row: Mapping[str, Any]) -> CompletionGateFacts:
    return CompletionGateFacts(
        exploration_terminal=bool(row["exploration_terminal"]),
        pending_action_count=int(row["pending_action_count"]),
        pending_capture_commit_count=int(row["pending_capture_commit_count"]),
        nonterminal_enforced_work_item_count=int(row["nonterminal_enforced_work_item_count"]),
        active_session_request_count=int(row["active_session_request_count"]),
        candidate_rule_count=int(row["candidate_rule_count"]),
        terminal_candidate_rule_count=int(row["terminal_candidate_rule_count"]),
        background_execution_count=int(row["background_execution_count"]),
        review_policy_satisfied=bool(row["review_policy_satisfied"]),
        open_review_count=int(row["open_review_count"]),
        review_generation=int(row["review_generation"]),
        review_policy_decision_id=(
            str(row["review_policy_decision_id"])
            if row["review_policy_decision_id"] is not None
            else None
        ),
        require_nonempty_candidate_inventory=bool(
            row.get("require_nonempty_candidate_inventory", False)
        ),
        no_rules_applicable=bool(row.get("no_rules_applicable", False)),
        app_coverage_satisfied=bool(row.get("app_coverage_satisfied", True)),
        h5_coverage_satisfied=bool(row.get("h5_coverage_satisfied", True)),
        journey_result_gap_count=int(row.get("journey_result_gap_count", 0)),
        h5_coverage_snapshot_id=row.get("h5_coverage_snapshot_id"),
        h5_coverage_snapshot_hash=row.get("h5_coverage_snapshot_hash"),
        h5_coverage_snapshot_epoch=row.get("h5_coverage_snapshot_epoch"),
        web_coverage_required=bool(row.get("web_coverage_required", False)),
        web_coverage_satisfied=bool(row.get("web_coverage_satisfied", True)),
        web_coverage_snapshot_id=(
            str(row["web_coverage_snapshot_id"])
            if row.get("web_coverage_snapshot_id") is not None
            else None
        ),
        web_coverage_snapshot_hash=(
            str(row["web_coverage_snapshot_hash"])
            if row.get("web_coverage_snapshot_hash") is not None
            else None
        ),
        web_coverage_snapshot_epoch=(
            int(row["web_coverage_snapshot_epoch"])
            if row.get("web_coverage_snapshot_epoch") is not None
            else None
        ),
        web_coverage_status=(
            str(row["web_coverage_status"])
            if row.get("web_coverage_status") is not None
            else None
        ),
    )


def _parse_finalization(row: Mapping[str, Any]) -> TaskFinalizationSnapshot:
    return TaskFinalizationSnapshot(
        task_id=str(row["task_id"]),
        state=TaskFinalizationState(str(row["finalization_state"])),
        finalization_epoch=int(row["finalization_epoch"]),
        row_version=int(row["row_version"]),
        agent_run_id=(
            str(row["finalization_agent_run_id"])
            if row.get("finalization_agent_run_id") is not None
            else None
        ),
        gate_snapshot_id=(
            str(row["completion_gate_snapshot_id"])
            if row["completion_gate_snapshot_id"] is not None
            else None
        ),
        execution_status=(
            TaskExecutionStatus(str(row["execution_status"]))
            if row["execution_status"] is not None
            else None
        ),
        quality_outcome=(
            TaskQualityOutcome(str(row["quality_outcome"]))
            if row["quality_outcome"] is not None
            else None
        ),
    )


def _parse_report_binding(row: Mapping[str, Any]) -> ReportGateBinding:
    return ReportGateBinding(
        report_id=str(row["report_id"]),
        report_version=int(row["report_version"]),
        report_payload_hash=str(row["report_payload_hash"]),
        task_id=str(row["task_id"]),
        gate_snapshot_id=str(row["gate_snapshot_id"]),
        finalization_epoch=int(row["finalization_epoch"]),
        execution_status=TaskExecutionStatus(str(row["execution_status"])),
        quality_outcome=TaskQualityOutcome(str(row["quality_outcome"])),
        bound_at=row["bound_at"],
    )


def _legacy_task_status(execution_status: TaskExecutionStatus) -> str:
    if execution_status in {
        TaskExecutionStatus.COMPLETED,
        TaskExecutionStatus.COMPLETED_WITH_GAPS,
    }:
        return "done"
    if execution_status is TaskExecutionStatus.NEEDS_REVIEW:
        return "review"
    if execution_status is TaskExecutionStatus.CANCELED:
        return "canceled"
    return "failed"


def _execution_resources_drained(
    facts: CompletionGateFacts, *, execution_inflight_work_count: int | None = None,
) -> bool:
    # Gate 缺口包含已终止的待复核发布，不能把它们视为仍在执行的工作。
    inflight = (
        facts.nonterminal_enforced_work_item_count
        if execution_inflight_work_count is None else execution_inflight_work_count
    )
    return (
        facts.exploration_terminal
        and facts.no_pending_action
        and facts.no_pending_capture_commit
        and inflight == 0
        and facts.no_active_session_request
        and facts.no_background_execution
    )


def _should_mark_task_review(
    *,
    execution_status: TaskExecutionStatus,
    facts: CompletionGateFacts,
    publication_review_required_count: int = 0,
    h5_rule_inventory_missing_count: int = 0,
    allow_natural_evidence_review: bool = False,
    require_execution_drained: bool = False,
    execution_inflight_work_count: int | None = None,
) -> bool:
    """技术执行已排空但仅等待人工复核时，任务应停在 review 而不是 running。"""
    execution_drained = _execution_resources_drained(
        facts, execution_inflight_work_count=execution_inflight_work_count,
    )
    if require_execution_drained and not execution_drained:
        return False
    review_terminal_statuses = {
        TaskExecutionStatus.COMPLETED_WITH_GAPS,
        TaskExecutionStatus.NEEDS_REVIEW,
        TaskExecutionStatus.FAILED,
    }
    review_ready_reasons = {
        CompletionBlockReason.REVIEW_POLICY_UNSATISFIED,
        CompletionBlockReason.H5_COVERAGE_SCOPE_INCOMPLETE,
        CompletionBlockReason.APP_CANDIDATE_INVENTORY_EMPTY,
        CompletionBlockReason.APP_COVERAGE_OR_VISUAL_QUALITY_INCOMPLETE,
    }
    if allow_natural_evidence_review:
        review_ready_reasons.add(
            CompletionBlockReason.NATURAL_EVIDENCE_PRESENTATION_VIOLATION
        )
    if (
        h5_rule_inventory_missing_count > 0
        and facts.candidate_rule_count - h5_rule_inventory_missing_count
        == facts.terminal_candidate_rule_count
        and (
            facts.no_nonterminal_enforced_work_item
            or require_execution_drained and execution_drained
        )
    ):
        review_ready_reasons.add(CompletionBlockReason.CANDIDATE_RULE_COVERAGE_NONTERMINAL)
    block_reasons = set(facts.block_reasons)
    if (
        execution_status in review_terminal_statuses
        and block_reasons
        and block_reasons <= review_ready_reasons
    ):
        return True
    return (
        publication_review_required_count > 0
        and execution_status
        in {
            TaskExecutionStatus.NEEDS_REVIEW,
            TaskExecutionStatus.FAILED,
        }
        and set(facts.block_reasons)
        <= review_ready_reasons
        | {CompletionBlockReason.NONTERMINAL_ENFORCED_WORK_ITEM}
    )


def _requires_h5_review_status_transition(
    *,
    task: Mapping[str, Any],
    execution_status: TaskExecutionStatus,
) -> bool:
    """判断旧 Gate 快照是否会挡住 H5 的 running→review 投影。"""

    return (
        task.get("task_status") == "running"
        and str(task.get("target_platform") or "").casefold() == "h5"
        and str(task.get("workflow_version") or "").casefold() == "v2"
        and execution_status in {
            TaskExecutionStatus.NEEDS_REVIEW,
            TaskExecutionStatus.COMPLETED_WITH_GAPS,
            TaskExecutionStatus.FAILED,
        }
    )


def _final_report_payload(
    report: dict[str, object],
    *,
    report_id: str,
    report_version: int,
    snapshot: CompletionGateSnapshot,
) -> dict[str, object]:
    """构造最终报告持久化载荷，门禁元数据由数据库事实覆盖。"""
    payload = dict(report)
    payload["completion_gate"] = {
        "gate_passed": True,
        "gate_snapshot_id": snapshot.gate_snapshot_id,
        "finalization_epoch": snapshot.finalization_epoch,
        "block_reasons": [],
    }
    payload["report_stage"] = "final"
    payload["is_final"] = True
    payload["report_id"] = report_id
    payload["report_binding_version"] = report_version
    return normalize_report_payload(payload)


def _json_dumps(value: Any) -> str:
    return json.dumps(
        normalize_report_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


# 自然结论（pass/fail/warning）实际消费的采集及其展示层事实；只看当前阶段的采集。
# 只走公共采集表：识别出的页面状态由质检发布写进 qc_capture.metadata，不连平台专属采集表。
_NATURAL_EVIDENCE_PRESENTATION_SQL = f"""
SELECT
    execution.execution_status,
    rule.category,
    rule.name AS rule_name,
    rule.check_point,
    rule.judge_config,
    capture.surface_kind,
    capture.metadata,
    capture.metadata->>'page_state' AS page_state,
    visual.page_state AS visual_page_state,
    visual.overlay_signature AS visual_overlay_signature,
    visual.surface_facts AS visual_surface_facts
FROM qc_task AS task
INNER JOIN qc_rule_execution AS execution
    ON execution.task_id = task.id
   AND execution.execution_status IN ('pass', 'fail', 'warning')
INNER JOIN qc_rule AS rule
    ON rule.id = execution.rule_id
INNER JOIN qc_capture AS capture
    ON capture.id = execution.page_capture_id
   AND capture.task_id = task.id
LEFT JOIN qc_app_visual_observation AS visual
    ON visual.capture_id = capture.id
WHERE task.id = $1::uuid
  AND ({capture_epoch_sql("capture")}) = task.finalization_epoch
"""


async def _count_natural_evidence_presentation_violations(
    connection: Any,
    *,
    task_id: str,
) -> int:
    """专项七 §23：数出消费了被遮挡底页或诊断材料的自然结论，作为收口兜底事实。"""
    rows = await connection.fetch(_NATURAL_EVIDENCE_PRESENTATION_SQL, task_id)
    violations = 0
    for row in rows or ():
        judge_config = _json_object(row.get("judge_config"))
        surface_facts = row.get("visual_surface_facts")
        presentation = presentation_from_capture_row(
            surface_kind=_text_or_none(row.get("surface_kind")),
            metadata=_json_object(row.get("metadata")),
            page_state=_text_or_none(row.get("page_state")),
            visual_page_state=_text_or_none(row.get("visual_page_state")),
            visual_overlay_signature=_text_or_none(row.get("visual_overlay_signature")),
            visual_surface_facts=_json_object(surface_facts) if surface_facts is not None else None,
        )
        verdict = evaluate_presentation_for_rule(
            presentation,
            layer_target=rule_evidence_layer_target(
                category=_text_or_none(row.get("category")),
                judge_config=judge_config,
                check_point=_text_or_none(row.get("check_point")),
                rule_name=_text_or_none(row.get("rule_name")),
            ),
            accepts_diagnostic=rule_accepts_diagnostic_evidence(judge_config),
        )
        if not verdict.accepted:
            violations += 1
    return violations


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _text_or_none(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
