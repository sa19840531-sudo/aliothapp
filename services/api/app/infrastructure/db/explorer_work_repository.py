"""Explorer Work、Evidence Request 与 Outbox 的 PostgreSQL 控制面。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.exploration_work_queue import (
    AppFrontierSettlement,
    ClaimExplorerWorkCommand,
    CommitExplorerWorkSuccessorCommand,
    CommittedExplorerWorkSuccessor,
    ExplorerWorkDeadlineExceededError,
    ExplorerWorkLease,
    ExplorerWorkPersistenceError,
    ExplorerWorkStatus,
    ExplorerWorkType,
    PublishedExplorerWork,
    RenewExplorerWorkLeaseCommand,
    TransitionExplorerWorkCommand,
    UpdateExplorerWorkPayloadCommand,
)
from services.api.app.application.h5_exploration_recovery import (
    H5BrowserRecoveryAttemptFacts,
    H5BrowserRecoveryFacts,
    H5FailureStage,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.exploration import (
    ExplorationTerminalStatus,
    ExplorationWork,
    ExplorerWork,
    ProbeWork,
)
from services.api.app.domain.exploration.h5_coverage import (
    H5_COVERAGE_METADATA_KEY,
    H5CoverageSnapshot,
)
from services.api.app.domain.exploration.h5_frontier import (
    H5_TASK_FRONTIER_METADATA_KEY,
    H5TaskFrontierSnapshot,
)
from services.api.app.domain.inspection import EvidenceSessionDependency
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.domain.protocol import (
    build_protocol_envelope,
    stable_protocol_hash,
    validate_protocol_payload,
)
from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)
from services.api.app.infrastructure.db.explorer_work_terminal_settlement import (
    settle_expired_web_work_in_transaction,
    terminal_for_settlement,
)
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.pool import get_postgres_pool

EXPLORATION_WORK_AVAILABLE = "alioth.v2.exploration-work.available"
PROBE_WORK_AVAILABLE = "alioth.v2.probe-work.available"

# 发布被拒的稳定原因码：按 publish() 那条 INSERT 的前置条件顺序，只报第一条不满足的。
PUBLISH_REJECTION_TASK_NOT_RUNNABLE = "TASK_NOT_RUNNABLE"
PUBLISH_REJECTION_SOURCE_CAPTURE_INVALID = "SOURCE_CAPTURE_INVALID"
PUBLISH_REJECTION_EVIDENCE_REQUEST_INVALID = "EVIDENCE_REQUEST_INVALID"
PUBLISH_REJECTION_RESTORATION_ATTEMPT_MISSING = "RESTORATION_ATTEMPT_MISSING"
PUBLISH_REJECTION_RESTORATION_NOT_EQUIVALENT = "RESTORATION_NOT_EQUIVALENT"
PUBLISH_REJECTION_EQUIVALENCE_PROOF_MISMATCH = "EQUIVALENCE_PROOF_MISMATCH"
PUBLISH_REJECTION_WORK_IDENTITY_DRIFT = "WORK_IDENTITY_DRIFT"
_PUBLISH_REJECTION_GENERIC_MESSAGE = "Explorer Work 归属无效、任务不可运行或幂等身份发生漂移"
_ROUTABLE_REQUEST_STATUSES = frozenset({"pending", "routed", "running"})


class PostgresExplorerWorkRepository:
    """原子保存 Explorer 工作、补采请求和待发布消息。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
        evidence_request_repository: Any | None = None,
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._supplemental_evidence_policy = (
            supplemental_evidence_policy
            if supplemental_evidence_policy is not None
            else SupplementalEvidencePolicy()
        )
        self._evidence_requests = (
            evidence_request_repository
            if evidence_request_repository is not None
            else PostgresEvidenceRequestRepository(
                database_url=database_url,
                pool=pool,
            )
        )

    async def publish(
        self,
        work: ExplorerWork,
        *,
        emit_wakeup: bool = True,
        request_lease: EvidenceRequestLeaseFence | None = None,
    ) -> PublishedExplorerWork:
        """幂等发布工作；Probe 会在同一事务内先保存 Evidence Request。"""
        if not isinstance(work, (ExplorationWork, ProbeWork)):
            raise TypeError("work 必须为 ExplorationWork 或 ProbeWork")
        if isinstance(work, ProbeWork):
            self._supplemental_evidence_policy.require_enabled(
                SupplementalEvidenceOperation.PUBLISH_PROBE
            )
        if work.deadline <= datetime.now(UTC):
            raise ExplorerWorkPersistenceError("不能发布已超过 deadline 的 Explorer Work")

        pool = await self._get_pool()
        payload = _validated_work_payload(work)
        payload_hash = stable_protocol_hash(payload)
        work_type = (
            ExplorerWorkType.PROBE if isinstance(work, ProbeWork) else ExplorerWorkType.EXPLORATION
        )
        request = work.evidence_request if isinstance(work, ProbeWork) else None
        if request_lease is not None and request is None:
            raise ValueError("只有 Probe Work 可以携带 Evidence Request 租约")
        if request_lease is not None and (
            request_lease.request_id != request.request_id
            or request_lease.task_id != request.task_id
        ):
            raise ValueError("Evidence Request 租约与 Probe Work 身份不一致")
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{work.task_id}",
            )
            if request is not None:
                await self._evidence_requests.upsert_in_transaction(
                    connection,
                    request=request,
                )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_v2_work_item AS existing (
                    work_id, work_type, task_id, session_id, branch_id,
                    source_capture_id, evidence_request_id,
                    restoration_attempt_id, equivalence_assessment_id,
                    source_grade, protocol_version, payload_hash,
                    work_payload, deadline, priority, finalization_epoch
                )
                SELECT
                    $1::varchar,
                    $2::varchar,
                    task.id,
                    $4::varchar,
                    $5::varchar,
                    capture.id,
                    request.request_id,
                    restoration.attempt_id,
                    restoration.equivalence_assessment_id,
                    restoration.source_grade,
                    $8::varchar,
                    $9::varchar,
                    $10::jsonb,
                    $11::timestamptz,
                    CASE
                        WHEN $2::varchar = 'probe'
                             AND request.session_dependency = 'active_session'
                        THEN 10
                        ELSE 100
                    END,
                    task.finalization_epoch
                FROM qc_task AS task
                INNER JOIN qc_capture AS capture
                    ON capture.id = $6::uuid
                   AND capture.task_id = task.id
                   AND capture.capture_status = 'captured'
                LEFT JOIN qc_evidence_request AS request
                    ON request.request_id = $7::varchar
                   AND request.task_id = task.id
                   AND request.status IN ('pending', 'routed', 'running')
                LEFT JOIN qc_restoration_attempt AS restoration
                    ON restoration.attempt_id = $12::varchar
                   AND restoration.request_id = request.request_id
                   AND restoration.task_id = task.id
                   AND restoration.original_capture_id = request.target_capture_id
                   AND restoration.restored_capture_id = capture.id
                   AND restoration.status = 'equivalent'
                   AND restoration.equivalence_assessment_id = $13::varchar
                   AND restoration.source_grade = $14::varchar
                   AND restoration.plan_hash = $15::varchar
                   AND restoration.assessment_payload->>'passed' = 'true'
                WHERE task.id = $3::uuid
                  AND (
                    task.status IN ('queued', 'running')
                    OR (
                        $2::varchar = 'probe'
                        AND task.status = 'review'
                        AND lower(task.target_platform) = 'h5'
                        AND task.finalization_state = 'session_draining'
                        AND request.session_dependency IN ('active_session', 'snapshot')
                    )
                  )
                  AND (
                    ($2::varchar = 'exploration' AND task.finalization_state = 'open')
                    OR (
                        $2::varchar = 'probe'
                        AND (
                            task.finalization_state = 'open'
                            OR (
                                task.finalization_state IN (
                                    'exploration_draining', 'session_draining'
                                )
                                AND (
                                    request.session_dependency = 'active_session'
                                    OR (
                                        request.session_dependency = 'snapshot'
                                        AND lower(task.target_platform) = 'h5'
                                    )
                                )
                            )
                        )
                    )
                  )
                  AND (
                    (
                        $2::varchar = 'exploration'
                        AND $7::varchar IS NULL
                        AND $12::varchar IS NULL
                        AND $13::varchar IS NULL
                        AND $14::varchar IS NULL
                        AND $15::varchar IS NULL
                    )
                    OR (
                        $2::varchar = 'probe'
                        AND request.request_id IS NOT NULL
                        AND restoration.attempt_id IS NOT NULL
                    )
                  )
                ON CONFLICT (work_id) DO UPDATE
                SET work_id = existing.work_id
                WHERE existing.payload_hash = EXCLUDED.payload_hash
                  AND existing.work_type = EXCLUDED.work_type
                  AND existing.restoration_attempt_id
                        IS NOT DISTINCT FROM EXCLUDED.restoration_attempt_id
                  AND existing.equivalence_assessment_id
                        IS NOT DISTINCT FROM EXCLUDED.equivalence_assessment_id
                RETURNING
                    existing.status,
                    (xmax = 0) AS created
                """,
                work.work_id,
                work_type.value,
                work.task_id,
                work.session_id,
                work.branch_id,
                work.source_capture_id,
                request.request_id if request is not None else None,
                work.protocol_version,
                payload_hash,
                _json_dumps(payload),
                work.deadline,
                (work.restoration_attempt_id if isinstance(work, ProbeWork) else None),
                (work.equivalence_assessment_id if isinstance(work, ProbeWork) else None),
                work.source_grade.value if isinstance(work, ProbeWork) else None,
                (work.restoration_plan_hash if isinstance(work, ProbeWork) else None),
            )
            if row is None:
                reason_code, message = await _diagnose_publish_rejection(
                    connection,
                    work=work,
                    work_type=work_type,
                    payload_hash=payload_hash,
                )
                raise ExplorerWorkPersistenceError(message, reason_code=reason_code)
            await _persist_h5_coverage_snapshot(connection, work=work)
            if request is not None:
                routed_id = await connection.fetchval(
                    """
                    UPDATE qc_evidence_request AS request
                    SET
                        status = CASE
                            WHEN status = 'pending' THEN 'routed'
                            ELSE status
                        END,
                        row_version = CASE
                            WHEN status = 'pending' THEN row_version + 1
                            ELSE row_version
                        END,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        updated_at = now()
                    WHERE request.request_id = $1
                      AND request.status IN ('pending', 'routed', 'running')
                      AND (
                        (
                          $2::uuid IS NULL
                          AND request.lease_token IS NULL
                        )
                        OR (
                          request.lease_token = $2::uuid
                          AND request.lease_owner = $3
                          AND request.finalization_epoch = $4
                          AND request.row_version = $5
                          AND request.lease_expires_at > now()
                          AND request.task_id = $6::uuid
                          AND request.status = 'pending'
                        )
                      )
                    RETURNING request_id
                    """,
                    request.request_id,
                    request_lease.lease_token if request_lease is not None else None,
                    request_lease.lease_owner if request_lease is not None else None,
                    (request_lease.finalization_epoch if request_lease is not None else None),
                    request_lease.row_version if request_lease is not None else None,
                    request.task_id,
                )
                if routed_id is None:
                    raise ExplorerWorkPersistenceError("Evidence Request 已进入不可路由终态")
            if emit_wakeup:
                await _insert_work_outbox(
                    connection,
                    work=work,
                    work_type=work_type,
                    payload=payload,
                )
        return PublishedExplorerWork(
            work_id=work.work_id,
            status=ExplorerWorkStatus(str(row["status"])),
            created=bool(row["created"]),
        )

    async def claim(
        self,
        *,
        command: ClaimExplorerWorkCommand,
        lease_token: str,
    ) -> ExplorerWorkLease | None:
        if command.lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        if command.work_type is ExplorerWorkType.PROBE:
            self._supplemental_evidence_policy.require_enabled(
                SupplementalEvidenceOperation.CLAIM_PROBE
            )
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                WITH candidate AS (
                    SELECT work_item.work_id
                    FROM qc_v2_work_item AS work_item
                    INNER JOIN qc_task AS task ON task.id = work_item.task_id
                    WHERE work_item.work_type = $1
                      AND ($2::uuid IS NULL OR work_item.task_id = $2::uuid)
                      AND (
                          task.status = 'running'
                          OR (
                              work_item.work_type = 'probe'
                              AND task.status = 'review'
                              AND lower(task.target_platform) = 'h5'
                              AND task.finalization_state = 'session_draining'
                              AND EXISTS (
                                  SELECT 1
                                  FROM qc_evidence_request AS request
                                  WHERE request.request_id = work_item.evidence_request_id
                                    AND request.session_dependency IN ('active_session', 'snapshot')
                                    AND request.status IN ('pending', 'routed', 'running')
                              )
                          )
                      )
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND (
                          (
                              work_item.work_type = 'exploration'
                              AND task.finalization_state = 'open'
                          )
                          OR (
                              work_item.work_type = 'probe'
                              AND (
                                  task.finalization_state = 'open'
                                  OR (
                                      task.finalization_state IN (
                                          'exploration_draining', 'session_draining'
                                      )
                                      AND EXISTS (
                                          SELECT 1
                                          FROM qc_evidence_request AS request
                                          WHERE request.request_id
                                                = work_item.evidence_request_id
                                            AND (
                                                request.session_dependency = 'active_session'
                                                OR (
                                                    request.session_dependency = 'snapshot'
                                                    AND lower(task.target_platform) = 'h5'
                                                )
                                            )
                                            AND request.status IN (
                                                'pending', 'routed', 'running'
                                            )
                                      )
                                  )
                              )
                          )
                      )
                      AND work_item.deadline > now()
                      AND work_item.attempt_count < work_item.max_attempts
                      -- 执行器声明了能跑的界面类型就只交匹配的活；不声明保持旧行为。
                      -- Probe 按目标 Surface 路由，各端共用一张表，不筛会互相串台。
                      AND (
                          $6::varchar[] IS NULL
                          OR work_item.work_payload->>'surface_kind' = ANY($6::varchar[])
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
                        work_item.created_at,
                        work_item.work_id
                    FOR UPDATE OF work_item SKIP LOCKED
                    LIMIT 1
                )
                UPDATE qc_v2_work_item AS work_item
                SET
                    status = 'leased',
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
                WHERE work_item.work_id = candidate.work_id
                RETURNING
                    work_item.work_type,
                    work_item.work_payload,
                    work_item.status,
                    work_item.lease_token::text AS lease_token,
                    work_item.attempt_count,
                    work_item.row_version
                """,
                command.work_type.value,
                command.task_id,
                command.lease_owner,
                lease_token,
                command.lease_seconds,
                [item.value for item in command.surface_kinds] or None,
            )
        if row is None:
            return None
        return ExplorerWorkLease(
            work=_parse_work(row["work_type"], row["work_payload"]),
            status=ExplorerWorkStatus(str(row["status"])),
            lease_token=str(row["lease_token"]),
            attempt_count=int(row["attempt_count"]),
            row_version=int(row["row_version"]),
        )

    async def transition(self, *, command: TransitionExplorerWorkCommand) -> int:
        _validate_work_transition(command)
        terminal = terminal_for_settlement(
            command,
        )
        if command.settlement_work is not None:
            assert terminal is not None
            settlement_work = command.settlement_work
            assert isinstance(settlement_work, ExplorationWork)
            pool = await self._get_pool()
            async with pool.acquire() as connection, connection.transaction():
                row_version = await settle_expired_web_work_in_transaction(
                    connection,
                    command=command,
                    terminal_payload=terminal.to_dict(),
                )
                await _persist_h5_coverage_snapshot(
                    connection,
                    work=settlement_work,
                )
            return int(row_version)
        terminal_payload = command.terminal.to_dict() if command.terminal is not None else None
        package_boundary_task_id = _package_boundary_terminal_task_id(command)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            if package_boundary_task_id is not None:
                # 包边界终态是任务级终态；与当前 Work/Frontier 写回共用事务，
                # 防止剩余 sibling 在关闭守卫检查前仍保持 pending。
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"task-finalization:{package_boundary_task_id}",
                )
            row_version = await connection.fetchval(
                """
                UPDATE qc_v2_work_item
                SET
                    status = $2::varchar,
                    row_version = row_version + 1,
                    terminal_payload = $6::jsonb,
                    last_error_code = $7::varchar,
                    last_error_message = $8::text,
                    next_attempt_at = CASE
                        WHEN $2::varchar = 'retry_wait'
                        THEN now() + make_interval(secs => $9::integer)
                        ELSE next_attempt_at
                    END,
                    attempt_count = CASE
                        WHEN $11::boolean AND $2::varchar = 'retry_wait' THEN 0
                        ELSE attempt_count
                    END,
                    lease_owner = CASE
                        WHEN $2::varchar = 'running' THEN lease_owner
                        ELSE NULL
                    END,
                    lease_token = CASE
                        WHEN $2::varchar = 'running' THEN lease_token
                        ELSE NULL
                    END,
                    lease_expires_at = CASE
                        WHEN $2::varchar = 'running' THEN lease_expires_at
                        ELSE NULL
                    END,
                    finished_at = CASE
                        WHEN $2::varchar = ANY($10::varchar[]) THEN now()
                        ELSE NULL
                    END,
                    updated_at = now()
                WHERE work_id = $1::varchar
                  AND status = $3::varchar
                  AND row_version = $4::bigint
                  AND lease_token = $5::uuid
                  AND lease_expires_at > now()
                  AND (
                    $6::jsonb IS NULL
                    OR (
                        $6::jsonb->>'work_id' = work_id
                        AND $6::jsonb->>'task_id' = task_id::text
                    )
                  )
                RETURNING row_version
                """,
                command.work_id,
                command.target_status.value,
                command.expected_status.value,
                command.row_version,
                command.lease_token,
                _json_dumps(terminal_payload) if terminal_payload is not None else None,
                command.error_code,
                command.error_message,
                command.retry_delay_seconds,
                [status.value for status in ExplorerWorkStatus if status.is_terminal],
                command.reset_attempt_count,
            )
            if row_version is None:
                raise ExplorerWorkPersistenceError("Explorer Work 状态、版本或租约已变化")
            if command.app_frontier_settlement is not None:
                await _settle_app_frontier_in_transaction(
                    connection,
                    parent_work_id=command.work_id,
                    result_work_id=None,
                    settlement=command.app_frontier_settlement,
                )
            if package_boundary_task_id is not None:
                assert command.terminal is not None
                await _cancel_package_boundary_siblings_in_transaction(
                    connection,
                    task_id=package_boundary_task_id,
                    current_work_id=command.work_id,
                    reason=command.terminal.reason,
                )
        return int(row_version)

    async def renew(self, *, command: RenewExplorerWorkLeaseCommand) -> None:
        """为当前 owner/token 持有的 RUNNING Exploration/Probe Work 续期。"""
        if not isinstance(command, RenewExplorerWorkLeaseCommand):
            raise TypeError("command 必须为 RenewExplorerWorkLeaseCommand")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            work_id = await connection.fetchval(
                """
                UPDATE qc_v2_work_item
                SET
                    lease_expires_at = now() + make_interval(secs => $4),
                    updated_at = now()
                WHERE work_id = $1
                  AND status = 'running'
                  AND lease_owner = $2
                  AND lease_token = $3::uuid
                  AND lease_expires_at > now()
                  AND deadline > now()
                RETURNING work_id
                """,
                command.work_id,
                command.lease_owner,
                command.lease_token,
                command.lease_seconds,
            )
        if work_id is None:
            raise ExplorerWorkPersistenceError("Explorer Work 租约已失效或不再归当前 Worker")

    async def update_payload(self, *, command: UpdateExplorerWorkPayloadCommand) -> int:
        """在当前 RUNNING 租约下保存恢复尝试后的 H5 Frontier 快照。"""
        if not isinstance(command, UpdateExplorerWorkPayloadCommand):
            raise TypeError("command 必须为 UpdateExplorerWorkPayloadCommand")
        payload = _validated_work_payload(command.work)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row_version = await connection.fetchval(
                """
                UPDATE qc_v2_work_item AS work_item
                SET work_payload = $2::jsonb,
                    payload_hash = $3::varchar,
                    source_capture_id = capture.id,
                    row_version = work_item.row_version + 1,
                    updated_at = now()
                FROM qc_capture AS capture
                WHERE work_item.work_id = $1::varchar
                  AND work_item.status = 'running'
                  AND work_item.row_version = $4::bigint
                  AND work_item.lease_token = $5::uuid
                  AND work_item.lease_expires_at > now()
                  AND capture.id = $6::uuid
                  AND capture.task_id = work_item.task_id
                  AND capture.surface_session_id = work_item.session_id
                  AND capture.capture_status = 'captured'
                RETURNING work_item.row_version
                """,
                command.work.work_id,
                _json_dumps(payload),
                stable_protocol_hash(payload),
                command.row_version,
                command.lease_token,
                command.work.source_capture_id,
            )
            if row_version is not None:
                await _persist_h5_coverage_snapshot(connection, work=command.work)
        if row_version is None:
            raise ExplorerWorkPersistenceError("Explorer Work 载荷、版本或租约已变化")
        return int(row_version)

    async def commit_successor(
        self,
        *,
        command: CommitExplorerWorkSuccessorCommand,
        emit_wakeup: bool = True,
    ) -> CommittedExplorerWorkSuccessor:
        """原子完成父 Work 并写入后继 Work；按调用策略选择是否写唤醒 Outbox。"""
        if not isinstance(command, CommitExplorerWorkSuccessorCommand):
            raise TypeError("command 必须为 CommitExplorerWorkSuccessorCommand")
        successor = command.successor
        if successor.deadline <= datetime.now(UTC):
            raise ExplorerWorkDeadlineExceededError("不能提交已超过 deadline 的后继 Explorer Work")
        metadata = successor.frontier.observation.metadata
        parent_branch_id = _require_lineage_text(metadata, "parent_branch_id")
        parent_capture_id = _require_lineage_text(metadata, "parent_capture_id")
        payload = successor.to_dict()
        payload_hash = stable_protocol_hash(payload)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{command.task_id}",
            )
            parent_row_version = await connection.fetchval(
                """
                UPDATE qc_v2_work_item AS parent
                SET
                    status = $9::varchar,
                    row_version = parent.row_version + 1,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    terminal_payload = NULL,
                    last_error_code = NULL,
                    last_error_message = NULL,
                    finished_at = now(),
                    updated_at = now()
                FROM qc_task AS task
                WHERE parent.work_id = $1
                  AND parent.work_type = 'exploration'
                  AND parent.task_id = $2::uuid
                  AND parent.session_id = $3
                  AND parent.lease_owner = $4
                  AND parent.lease_token = $5::uuid
                  AND parent.row_version = $6
                  AND parent.branch_id = $7
                  AND parent.source_capture_id = $8::uuid
                  AND parent.status = 'running'
                  AND parent.lease_expires_at > now()
                  AND parent.deadline > now()
                  AND task.id = parent.task_id
                  AND task.status = 'running'
                  AND task.finalization_state = 'open'
                  AND parent.finalization_epoch = task.finalization_epoch
                RETURNING parent.row_version
                """,
                command.parent_work_id,
                command.task_id,
                command.session_id,
                command.lease_owner,
                command.lease_token,
                command.row_version,
                parent_branch_id,
                parent_capture_id,
                command.parent_target_status.value,
            )
            if parent_row_version is None:
                if successor.deadline <= datetime.now(UTC):
                    raise ExplorerWorkDeadlineExceededError(
                        "后继提交期间已超过 deadline，必须重新校验租约后结算"
                    )
                raise ExplorerWorkPersistenceError("父 Explorer Work 状态、版本、谱系或租约已失效")
            row = await connection.fetchrow(
                """
                INSERT INTO qc_v2_work_item AS existing (
                    work_id, work_type, task_id, session_id, branch_id,
                    source_capture_id, protocol_version, payload_hash,
                    work_payload, deadline, priority, finalization_epoch
                )
                SELECT
                    $1, 'exploration', task.id, $3, $4,
                    capture.id, $6, $7, $8::jsonb, $9, 100,
                    task.finalization_epoch
                FROM qc_task AS task
                INNER JOIN qc_capture AS capture
                    ON capture.id = $5::uuid
                   AND capture.task_id = task.id
                   AND capture.surface_session_id = $3
                   AND capture.capture_status = 'captured'
                WHERE task.id = $2::uuid
                  AND task.status = 'running'
                  AND task.finalization_state = 'open'
                ON CONFLICT (work_id) DO UPDATE
                SET work_id = existing.work_id
                WHERE existing.payload_hash = EXCLUDED.payload_hash
                  AND existing.work_type = EXCLUDED.work_type
                  AND existing.restoration_attempt_id IS NULL
                  AND existing.equivalence_assessment_id IS NULL
                RETURNING
                    existing.status,
                    (xmax = 0) AS created
                """,
                successor.work_id,
                successor.task_id,
                successor.session_id,
                successor.branch_id,
                successor.source_capture_id,
                successor.protocol_version,
                payload_hash,
                _json_dumps(payload),
                successor.deadline,
            )
            if row is None:
                raise ExplorerWorkPersistenceError(
                    "后继 Explorer Work 归属无效、任务不可运行或幂等身份发生漂移"
                )
            await _persist_h5_coverage_snapshot(connection, work=successor)
            if command.app_frontier_settlement is not None:
                await _settle_app_frontier_in_transaction(
                    connection,
                    parent_work_id=command.parent_work_id,
                    result_work_id=successor.work_id,
                    settlement=command.app_frontier_settlement,
                )
            if emit_wakeup:
                await _insert_work_outbox(
                    connection,
                    work=successor,
                    work_type=ExplorerWorkType.EXPLORATION,
                    payload=payload,
                )
        return CommittedExplorerWorkSuccessor(
            parent_row_version=int(parent_row_version),
            successor_work_id=successor.work_id,
            successor_created=bool(row["created"]),
        )

    async def expire_unclaimable(
        self,
        *,
        limit: int = 100,
        task_id: str | None = None,
    ) -> int:
        """把超过截止时间或耗尽尝试次数的工作推进到可审计终态。"""
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            result = await connection.fetchval(
                """
                WITH exhausted AS (
                    SELECT work_id
                    FROM qc_v2_work_item
                    WHERE status NOT IN (
                        'succeeded', 'bounded', 'budget_exhausted', 'blocked',
                        'no_progress', 'failed', 'cancelled', 'superseded'
                    )
                      AND ($2::uuid IS NULL OR task_id = $2::uuid)
                      AND (
                        deadline <= now()
                        OR attempt_count >= max_attempts
                      )
                    ORDER BY deadline, work_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                ), updated AS (
                    UPDATE qc_v2_work_item AS work_item
                    SET
                        status = 'failed',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = CASE
                            WHEN work_item.deadline <= now()
                            THEN 'EXPLORER_WORK_DEADLINE_EXCEEDED'
                            ELSE 'EXPLORER_WORK_ATTEMPTS_EXHAUSTED'
                        END,
                        last_error_message = CASE
                            WHEN work_item.deadline <= now()
                            THEN 'Explorer Work 已超过截止时间'
                            ELSE 'Explorer Work 已耗尽最大尝试次数'
                        END,
                        finished_at = now(),
                        row_version = work_item.row_version + 1,
                        updated_at = now()
                    FROM exhausted
                    WHERE work_item.work_id = exhausted.work_id
                    RETURNING 1
                )
                SELECT count(*) FROM updated
                """,
                limit,
                task_id,
            )
        return int(result or 0)

    async def next_claim_at(
        self,
        *,
        task_id: str,
        work_type: ExplorerWorkType,
    ) -> datetime | None:
        """读取 PostgreSQL 中下一次允许领取的时间，不复制重试计时状态。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT min(
                    CASE
                        WHEN work_item.status = 'retry_wait'
                        THEN work_item.next_attempt_at
                        WHEN work_item.status IN ('leased', 'running')
                        THEN work_item.lease_expires_at
                    END
                )
                FROM qc_v2_work_item AS work_item
                INNER JOIN qc_task AS task ON task.id = work_item.task_id
                WHERE work_item.task_id = $1::uuid
                  AND work_item.work_type = $2::varchar
                  AND work_item.attempt_count < work_item.max_attempts
                  AND work_item.deadline > now()
                  AND (
                    (
                        work_item.status = 'retry_wait'
                        AND work_item.next_attempt_at < work_item.deadline
                    )
                    OR (
                        work_item.status IN ('leased', 'running')
                        AND work_item.lease_expires_at IS NOT NULL
                        AND work_item.lease_expires_at < work_item.deadline
                    )
                  )
                  AND task.status = 'running'
                  AND task.finalization_state = 'open'
                  AND work_item.finalization_epoch = task.finalization_epoch
                """,
                task_id,
                work_type.value,
            )
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ExplorerWorkPersistenceError("Explorer Work next_attempt_at 无效")
        return value

    async def cancel_nonterminal(
        self,
        *,
        task_id: str,
        reason: str,
        work_types: tuple[ExplorerWorkType, ...] = (ExplorerWorkType.EXPLORATION,),
        h5_only: bool = False,
    ) -> int:
        """按平台与 Work 类型取消仍处于运行链路的 Explorer Work。"""
        if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
            raise ValueError("reason 必须为无首尾空白的非空字符串")
        if not work_types or any(not isinstance(item, ExplorerWorkType) for item in work_types):
            raise ValueError("work_types 必须包含有效 ExplorerWorkType")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            count = await connection.fetchval(
                """
                WITH canceled AS (
                    UPDATE qc_v2_work_item
                    SET
                        status = 'cancelled',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        terminal_payload = COALESCE(terminal_payload, '{}'::jsonb)
                            || jsonb_build_object('cancel_reason', $2::text),
                        last_error_code = COALESCE(last_error_code, 'WORKFLOW_CANCELED'),
                        last_error_message = COALESCE(last_error_message, $2::text),
                        finished_at = now(),
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE task_id = $1::uuid
                      AND work_type = ANY($3::varchar[])
                      AND (
                          $4::boolean = FALSE
                          OR work_payload->>'surface_kind' = 'web'
                      )
                      AND status IN ('pending', 'leased', 'running', 'retry_wait')
                    RETURNING 1
                )
                SELECT count(*) FROM canceled
                """,
                task_id,
                reason,
                [item.value for item in work_types],
                h5_only,
            )
        return int(count or 0)

    async def reconcile_h5_terminal_frontier(self, *, task_id: str) -> bool:
        """H5 收场前结清最终 Frontier，并原子刷新覆盖快照。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            row = await connection.fetchrow(
                """
                SELECT
                    work_item.work_id,
                    work_item.work_payload,
                    work_item.status,
                    work_item.terminal_payload
                FROM qc_v2_work_item AS work_item
                INNER JOIN qc_task AS task ON task.id = work_item.task_id
                WHERE work_item.task_id = $1::uuid
                  AND work_item.work_type = 'exploration'
                  AND work_item.finalization_epoch = task.finalization_epoch
                  AND work_item.work_payload->>'surface_kind' = 'web'
                  AND work_item.work_payload #> ARRAY[
                      'frontier', 'observation', 'metadata', 'h5_task_frontier'
                  ] IS NOT NULL
                  AND task.status = 'running'
                  AND task.finalization_state = 'open'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_v2_work_item AS active
                      WHERE active.task_id = task.id
                        AND active.work_type = 'exploration'
                        AND active.finalization_epoch = task.finalization_epoch
                        AND active.work_payload->>'surface_kind' = 'web'
                        AND active.status IN (
                            'pending', 'retry_wait', 'leased', 'running'
                        )
                  )
                ORDER BY work_item.created_at DESC, work_item.work_id DESC
                LIMIT 1
                FOR UPDATE OF work_item
                """,
                task_id,
            )
            if row is None:
                return False
            work = ExplorationWork.from_dict(
                _as_mapping(row["work_payload"], "H5 terminal work_payload")
            )
            metadata = work.frontier.observation.metadata
            task_frontier = H5TaskFrontierSnapshot.from_dict(
                _as_mapping(
                    metadata.get(H5_TASK_FRONTIER_METADATA_KEY),
                    "H5 terminal task frontier",
                )
            )
            terminal = _as_optional_mapping(row["terminal_payload"])
            successful = (
                str(row["status"]) == ExplorerWorkStatus.SUCCEEDED.value
                and terminal is not None
                and terminal.get("status") == ExplorationTerminalStatus.COMPLETED.value
            )
            reason = (
                "h5_terminal_objective_satisfied"
                if successful
                else str((terminal or {}).get("reason_code") or "h5_terminal_review")
            )
            settled_frontier = task_frontier.settle_remaining(
                successful=successful,
                reason=reason,
            )
            coverage = H5CoverageSnapshot.from_dict(
                _as_mapping(
                    metadata.get(H5_COVERAGE_METADATA_KEY),
                    "H5 terminal coverage",
                )
            ).reconcile_frontier(
                frontier=settled_frontier,
                requires_auth=metadata.get("requires_auth") is True,
                overlay_present=work.frontier.observation.overlay_signature is not None,
                completion_allowed=successful,
            )
            settled_metadata = {
                **dict(metadata),
                H5_TASK_FRONTIER_METADATA_KEY: settled_frontier.to_dict(),
                H5_COVERAGE_METADATA_KEY: coverage.to_dict(),
                "h5_task_frontier_selected_entry_id": None,
                "objective_satisfied": coverage.completion_ready,
            }
            settled_work = replace(
                work,
                frontier_snapshot_id=settled_frontier.snapshot_id,
                frontier=replace(
                    work.frontier,
                    snapshot_id=settled_frontier.snapshot_id,
                    observation=replace(
                        work.frontier.observation,
                        metadata=settled_metadata,
                    ),
                    candidates=(),
                    blocked_candidate_ids=(),
                ),
            )
            payload = _validated_work_payload(settled_work)
            updated = await connection.fetchval(
                """
                UPDATE qc_v2_work_item
                SET work_payload = $2::jsonb,
                    payload_hash = $3::varchar,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE work_id = $1::varchar
                RETURNING work_id
                """,
                str(row["work_id"]),
                _json_dumps(payload),
                stable_protocol_hash(payload),
            )
            if updated is None:
                raise ExplorerWorkPersistenceError("H5 最终 Frontier 回写失败")
            await _persist_h5_coverage_snapshot(connection, work=settled_work)
            return True

    async def cancel_app_exploration_nonterminal(
        self,
        *,
        task_id: str,
        reason: str,
    ) -> int:
        """在同一事务取消普通 App Work 与任务级 Frontier。"""

        if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
            raise ValueError("reason 必须为无首尾空白的非空字符串")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"app-frontier-cancel:{task_id}",
            )
            work_count = await connection.fetchval(
                """
                WITH canceled AS (
                    UPDATE qc_v2_work_item
                    SET
                        status = 'cancelled',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = 'WORKFLOW_CANCELED',
                        last_error_message = $2,
                        finished_at = now(),
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE task_id = $1::uuid
                      AND work_type = 'exploration'
                      AND status IN ('pending', 'leased', 'running', 'retry_wait')
                    RETURNING 1
                )
                SELECT count(*) FROM canceled
                """,
                task_id,
                reason,
            )
            frontier_count = await connection.fetchval(
                """
                WITH canceled AS (
                    UPDATE qc_app_exploration_frontier
                    SET
                        status = 'cancelled',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = 'WORKFLOW_CANCELED',
                        last_error_message = $2,
                        finished_at = now(),
                        row_version = row_version + 1,
                        updated_at = now()
                    WHERE task_id = $1::uuid
                      AND status IN ('pending', 'leased', 'running', 'retry_wait')
                    RETURNING 1
                )
                SELECT count(*) FROM canceled
                """,
                task_id,
                reason,
            )
        return int(work_count or 0) + int(frontier_count or 0)

    async def has_nonterminal_work(self, *, task_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM qc_v2_work_item
                        WHERE task_id = $1::uuid
                          AND status NOT IN (
                            'succeeded', 'bounded', 'budget_exhausted', 'blocked',
                            'no_progress', 'failed', 'cancelled', 'superseded'
                          )
                    )
                    """,
                    task_id,
                )
            )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


class PostgresExplorationWorkPublisher:
    """MAF Orchestrator 的普通探索工作发布适配器。"""

    def __init__(
        self,
        *,
        repository: PostgresExplorerWorkRepository,
        emit_wakeup: bool = True,
    ) -> None:
        self._repository = repository
        self._emit_wakeup = emit_wakeup

    async def publish(self, work: ExplorationWork) -> None:
        await self._repository.publish(work, emit_wakeup=self._emit_wakeup)


class PostgresProbeWorkPublisher:
    """MAF Orchestrator 的 Probe 工作发布适配器。"""

    def __init__(
        self,
        *,
        repository: PostgresExplorerWorkRepository,
        supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
        emit_wakeup: bool = True,
    ) -> None:
        self._repository = repository
        self._emit_wakeup = emit_wakeup
        self._supplemental_evidence_policy = (
            supplemental_evidence_policy
            if supplemental_evidence_policy is not None
            else SupplementalEvidencePolicy()
        )

    async def publish(
        self,
        work: ProbeWork,
        *,
        request_lease: EvidenceRequestLeaseFence | None = None,
    ) -> None:
        self._supplemental_evidence_policy.require_enabled(
            SupplementalEvidenceOperation.PUBLISH_PROBE
        )
        if request_lease is None:
            await self._repository.publish(work, emit_wakeup=self._emit_wakeup)
        else:
            await self._repository.publish(
                work,
                emit_wakeup=self._emit_wakeup,
                request_lease=request_lease,
            )


async def _diagnose_publish_rejection(
    connection: Any,
    *,
    work: ExplorerWork,
    work_type: ExplorerWorkType,
    payload_hash: str,
) -> tuple[str | None, str]:
    """发布被拒后，按 INSERT 的前置条件顺序找出第一条不满足的，给稳定原因码。

    只读，不改任何行。诊断本身拿不到事实（例如假连接不返回行）就退回笼统文案，
    原因码为 ``None``，行为与拆码之前一致。
    """
    is_probe = isinstance(work, ProbeWork)
    request = work.evidence_request if is_probe else None
    facts = await connection.fetchrow(
        """
        SELECT
            task.id IS NOT NULL AS task_exists,
            task.status AS task_status,
            task.target_platform AS task_target_platform,
            task.finalization_state AS task_finalization_state,
            capture.id IS NOT NULL AS capture_exists,
            capture.task_id::text AS capture_task_id,
            capture.capture_status AS capture_status,
            request.request_id IS NOT NULL AS request_exists,
            request.task_id::text AS request_task_id,
            request.status AS request_status,
            restoration.attempt_id IS NOT NULL AS attempt_exists,
            restoration.request_id AS attempt_request_id,
            restoration.task_id::text AS attempt_task_id,
            restoration.status AS attempt_status,
            restoration.assessment_payload->>'passed' AS attempt_passed,
            restoration.equivalence_assessment_id AS attempt_assessment_id,
            restoration.source_grade AS attempt_source_grade,
            restoration.plan_hash AS attempt_plan_hash,
            restoration.original_capture_id::text AS attempt_original_capture_id,
            restoration.restored_capture_id::text AS attempt_restored_capture_id,
            existing.work_id IS NOT NULL AS work_exists,
            existing.work_type AS existing_work_type,
            existing.payload_hash AS existing_payload_hash,
            existing.restoration_attempt_id AS existing_attempt_id,
            existing.equivalence_assessment_id AS existing_assessment_id
        FROM (SELECT 1) AS anchor
        LEFT JOIN qc_task AS task ON task.id = $1::uuid
        LEFT JOIN qc_capture AS capture ON capture.id = $2::uuid
        LEFT JOIN qc_evidence_request AS request ON request.request_id = $3::varchar
        LEFT JOIN qc_restoration_attempt AS restoration
            ON restoration.attempt_id = $4::varchar
        LEFT JOIN qc_v2_work_item AS existing ON existing.work_id = $5::varchar
        """,
        work.task_id,
        work.source_capture_id,
        request.request_id if request is not None else None,
        work.restoration_attempt_id if is_probe else None,
        work.work_id,
    )
    if facts is None:
        return None, _PUBLISH_REJECTION_GENERIC_MESSAGE
    h5_review_session_draining = (
        is_probe
        and facts["task_status"] == "review"
        and str(facts["task_target_platform"] or "").casefold() == "h5"
        and facts["task_finalization_state"] == "session_draining"
        and request.session_dependency
        in {EvidenceSessionDependency.ACTIVE_SESSION, EvidenceSessionDependency.SNAPSHOT}
    )
    if (
        not facts["task_exists"]
        or (
            facts["task_status"] not in {"queued", "running"}
            and not h5_review_session_draining
        )
    ):
        return (
            PUBLISH_REJECTION_TASK_NOT_RUNNABLE,
            f"任务不存在或已不可运行（status={facts['task_status']}），"
            f"不能发布 {work_type.value}",
        )
    state = facts["task_finalization_state"]
    admits_stage = state == "open" or (
        is_probe
        and state in {"exploration_draining", "session_draining"}
        and (
            request.session_dependency is EvidenceSessionDependency.ACTIVE_SESSION
            or (
                request.session_dependency is EvidenceSessionDependency.SNAPSHOT
                and str(facts["task_target_platform"] or "").casefold() == "h5"
            )
        )
    )
    if not admits_stage:
        return (
            PUBLISH_REJECTION_TASK_NOT_RUNNABLE,
            f"任务已进入 {state}，"
            + ("只接受活会话依赖的 probe" if is_probe else "不再接受新的 exploration"),
        )
    if (
        not facts["capture_exists"]
        or facts["capture_task_id"] != work.task_id
        or facts["capture_status"] != "captured"
    ):
        return (
            PUBLISH_REJECTION_SOURCE_CAPTURE_INVALID,
            f"来源 Capture {work.source_capture_id} 不存在、不属于该任务或尚未采集成功",
        )
    if is_probe:
        if not facts["request_exists"] or facts["request_task_id"] != work.task_id:
            return (
                PUBLISH_REJECTION_EVIDENCE_REQUEST_INVALID,
                f"Evidence Request {request.request_id} 不存在或不属于该任务",
            )
        if facts["request_status"] not in _ROUTABLE_REQUEST_STATUSES:
            return (
                PUBLISH_REJECTION_EVIDENCE_REQUEST_INVALID,
                f"Evidence Request {request.request_id} 已进入 {facts['request_status']}，"
                "不能再发布 Probe",
            )
        if not facts["attempt_exists"]:
            return (
                PUBLISH_REJECTION_RESTORATION_ATTEMPT_MISSING,
                f"恢复尝试 {work.restoration_attempt_id} 不存在："
                "Probe 只能从已完成等价评估的恢复尝试发布，不能先发 Probe 再补评估",
            )
        if (
            facts["attempt_request_id"] != request.request_id
            or facts["attempt_task_id"] != work.task_id
        ):
            return (
                PUBLISH_REJECTION_EQUIVALENCE_PROOF_MISMATCH,
                f"恢复尝试 {work.restoration_attempt_id} 属于其他请求或任务",
            )
        if facts["attempt_status"] != "equivalent" or facts["attempt_passed"] != "true":
            return (
                PUBLISH_REJECTION_RESTORATION_NOT_EQUIVALENT,
                f"恢复尝试 {work.restoration_attempt_id} 状态为 {facts['attempt_status']}，"
                "没有通过状态等价门禁",
            )
        mismatched = [
            label
            for label, matched in (
                ("等价评估 ID", facts["attempt_assessment_id"] == work.equivalence_assessment_id),
                ("来源等级", facts["attempt_source_grade"] == work.source_grade.value),
                ("恢复计划哈希", facts["attempt_plan_hash"] == work.restoration_plan_hash),
                ("原 Capture", facts["attempt_original_capture_id"] == request.target_capture_id),
                ("恢复 Capture", facts["attempt_restored_capture_id"] == work.source_capture_id),
            )
            if not matched
        ]
        if mismatched:
            return (
                PUBLISH_REJECTION_EQUIVALENCE_PROOF_MISMATCH,
                "等价证明与 Probe 不一致：" + "、".join(mismatched),
            )
    if facts["work_exists"] and (
        facts["existing_work_type"] != work_type.value
        or facts["existing_payload_hash"] != payload_hash
        or facts["existing_attempt_id"] != (work.restoration_attempt_id if is_probe else None)
        or facts["existing_assessment_id"]
        != (work.equivalence_assessment_id if is_probe else None)
    ):
        return (
            PUBLISH_REJECTION_WORK_IDENTITY_DRIFT,
            f"Explorer Work {work.work_id} 已存在且内容不同，幂等身份发生漂移",
        )
    return None, _PUBLISH_REJECTION_GENERIC_MESSAGE


async def _insert_work_outbox(
    connection: Any,
    *,
    work: ExplorerWork,
    work_type: ExplorerWorkType,
    payload: Mapping[str, Any],
) -> None:
    message_type = (
        PROBE_WORK_AVAILABLE if work_type is ExplorerWorkType.PROBE else EXPLORATION_WORK_AVAILABLE
    )
    envelope = build_protocol_envelope(
        message_type=message_type,
        payload_protocol_version=work.protocol_version,
        idempotency_key=work.work_id,
        task_id=work.task_id,
        aggregate_type="explorer_work",
        aggregate_id=work.work_id,
        aggregate_version=0,
        occurred_at=datetime.now(UTC),
        payload=payload,
        correlation_id=work.task_id,
    )
    await insert_outbox_event(
        connection,
        aggregate_type="explorer_work",
        aggregate_id=work.work_id,
        envelope=envelope,
    )


async def _settle_app_frontier_in_transaction(
    connection: Any,
    *,
    parent_work_id: str,
    result_work_id: str | None,
    settlement: AppFrontierSettlement,
) -> None:
    """与 Explorer Work 状态在同一事务收口对应的 App sibling。"""

    if settlement.frontier_item_id is not None:
        frontier_item_id = await connection.fetchval(
            """
            UPDATE qc_app_exploration_frontier
            SET
                status = $7::varchar,
                result_work_id = COALESCE($8::varchar, result_work_id),
                result_capture_id = COALESCE($9::uuid, result_capture_id),
                last_error_code = $10::varchar,
                last_error_message = $11::text,
                next_attempt_at = CASE
                    WHEN $7::varchar = 'retry_wait'
                    THEN now() + make_interval(secs => $14::integer)
                    ELSE next_attempt_at
                END,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                finished_at = CASE
                    WHEN $7::varchar IN (
                        'visited', 'blocked', 'failed', 'cancelled', 'superseded'
                    ) THEN now()
                    ELSE NULL
                END,
                row_version = row_version + 1,
                updated_at = now()
            WHERE frontier_item_id = $1::varchar
              AND task_id = $2::uuid
              AND agent_run_id = $3::uuid
              AND surface_session_id = $4::varchar
              AND candidate_fingerprint = $5::varchar
              AND status = 'running'
              AND row_version = $6::bigint
              AND lease_token = $12::uuid
              AND lease_expires_at > now()
              AND EXISTS (
                    SELECT 1
                    FROM qc_v2_work_item AS parent
                    INNER JOIN qc_agent_run AS run
                        ON run.id = $3::uuid
                       AND run.task_id = parent.task_id
                    WHERE parent.work_id = $13::varchar
                      AND parent.task_id = $2::uuid
                      AND parent.session_id = $4::varchar
              )
            RETURNING frontier_item_id
            """,
            settlement.frontier_item_id,
            settlement.task_id,
            settlement.agent_run_id,
            settlement.session_id,
            settlement.candidate_fingerprint,
            settlement.row_version,
            settlement.target_status.value,
            result_work_id,
            settlement.result_capture_id,
            settlement.error_code,
            settlement.error_message,
            settlement.lease_token,
            parent_work_id,
            settlement.retry_delay_seconds,
        )
    else:
        frontier_item_id = await connection.fetchval(
            """
            UPDATE qc_app_exploration_frontier
            SET
                status = $5::varchar,
                result_work_id = COALESCE($6::varchar, result_work_id),
                result_capture_id = COALESCE($7::uuid, result_capture_id),
                last_error_code = $8::varchar,
                last_error_message = $9::text,
                finished_at = now(),
                row_version = row_version + 1,
                updated_at = now()
            WHERE task_id = $1::uuid
              AND agent_run_id = $2::uuid
              AND surface_session_id = $3::varchar
              AND candidate_fingerprint = $4::varchar
              AND status = 'pending'
              AND EXISTS (
                    SELECT 1
                    FROM qc_v2_work_item AS parent
                    INNER JOIN qc_agent_run AS run
                        ON run.id = $2::uuid
                       AND run.task_id = parent.task_id
                    WHERE parent.work_id = $10::varchar
                      AND parent.task_id = $1::uuid
                      AND parent.session_id = $3::varchar
              )
            RETURNING frontier_item_id
            """,
            settlement.task_id,
            settlement.agent_run_id,
            settlement.session_id,
            settlement.candidate_fingerprint,
            settlement.target_status.value,
            result_work_id,
            settlement.result_capture_id,
            settlement.error_code,
            settlement.error_message,
            parent_work_id,
        )
    if frontier_item_id is None:
        raise ExplorerWorkPersistenceError("App Frontier 状态必须与父 Work、后继 Work 原子提交")


async def _cancel_package_boundary_siblings_in_transaction(
    connection: Any,
    *,
    task_id: str,
    current_work_id: str,
    reason: str,
) -> None:
    """包边界终态原子取消同任务的其余 App Work 与全局 Frontier。"""

    await connection.execute(
        """
        UPDATE qc_v2_work_item
        SET
            status = 'cancelled',
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            last_error_code = 'APP_PACKAGE_BOUNDARY_VIOLATION',
            last_error_message = $3,
            finished_at = now(),
            row_version = row_version + 1,
            updated_at = now()
        WHERE task_id = $1::uuid
          AND work_id <> $2::varchar
          AND work_type = 'exploration'
          AND status IN ('pending', 'leased', 'running', 'retry_wait')
        """,
        task_id,
        current_work_id,
        reason,
    )
    await connection.execute(
        """
        UPDATE qc_app_exploration_frontier
        SET
            status = 'cancelled',
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            last_error_code = 'APP_PACKAGE_BOUNDARY_VIOLATION',
            last_error_message = $2,
            finished_at = now(),
            row_version = row_version + 1,
            updated_at = now()
        WHERE task_id = $1::uuid
          AND status IN ('pending', 'leased', 'running', 'retry_wait')
        """,
        task_id,
        reason,
    )


def _package_boundary_terminal_task_id(
    command: TransitionExplorerWorkCommand,
) -> str | None:
    terminal = command.terminal
    if (
        command.error_code == "APP_PACKAGE_BOUNDARY_VIOLATION"
        and terminal is not None
        and terminal.reason_code == "app_package_boundary_violation"
    ):
        return terminal.task_id
    return None


def _parse_work(work_type: Any, payload: Any) -> ExplorerWork:
    source = _as_mapping(payload, "work_payload")
    if str(work_type) == ExplorerWorkType.EXPLORATION.value:
        work = ExplorationWork.from_dict(source)
        _validate_h5_task_frontier_metadata(work)
        return work
    if str(work_type) == ExplorerWorkType.PROBE.value:
        return ProbeWork.from_dict(source)
    raise ExplorerWorkPersistenceError("数据库包含不受支持的 Explorer Work 类型")


def _validate_h5_task_frontier_metadata(work: ExplorationWork) -> None:
    """数据库回读时校验 H5 全局 Frontier 的完整版本化快照。"""
    payload = work.frontier.observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY)
    if payload is None:
        return
    if not isinstance(payload, Mapping):
        raise ExplorerWorkPersistenceError("H5 Work 的任务 Frontier 快照无效")
    H5TaskFrontierSnapshot.from_dict(payload)
    recovery = work.frontier.observation.metadata.get("h5_browser_recovery")
    if recovery is not None:
        try:
            facts = H5BrowserRecoveryFacts.from_dict(recovery)
        except (TypeError, ValueError) as exc:
            raise ExplorerWorkPersistenceError("H5 browser recovery facts 无效") from exc
        if facts.task_id != work.task_id or not (
            facts.target_session_id == work.session_id
            or (facts.reason_code is not None and facts.source_session_id == work.session_id)
        ):
            raise ExplorerWorkPersistenceError("H5 browser recovery facts 与 Work 身份不一致")
    attempt = work.frontier.observation.metadata.get("h5_browser_recovery_attempt")
    if attempt is not None:
        if not isinstance(attempt, Mapping):
            raise ExplorerWorkPersistenceError("H5 browser recovery attempt 无效")
        try:
            attempt_facts = H5BrowserRecoveryAttemptFacts.from_dict(attempt)
        except (TypeError, ValueError) as exc:
            raise ExplorerWorkPersistenceError(
                "H5 browser recovery attempt 版本或字段无效"
            ) from exc
        agent_run_id = work.frontier.observation.metadata.get("agent_run_id")
        if (
            attempt_facts.task_id != work.task_id
            or attempt_facts.agent_run_id != agent_run_id
            or attempt_facts.source_session_id != work.session_id
            or attempt_facts.failed_work_id != work.work_id
            or attempt_facts.source_state_version != work.frontier.observation.state_version
            or attempt_facts.frontier_snapshot_id != work.frontier_snapshot_id
            or attempt_facts.deadline != work.deadline.isoformat()
            or attempt_facts.last_safe_location != work.frontier.observation.location
            or attempt_facts.remaining_actions_after_reservation != work.budget.remaining_actions
        ):
            raise ExplorerWorkPersistenceError(
                "H5 browser recovery attempt 与 Work/Frontier/预算身份不一致"
            )
    history = work.frontier.observation.metadata.get(
        "h5_browser_recovery_attempt_history",
        (),
    )
    if not isinstance(history, (list, tuple)):
        raise ExplorerWorkPersistenceError("H5 browser recovery attempt history 无效")
    history_hashes: set[str] = set()
    previous_history: H5BrowserRecoveryAttemptFacts | None = None
    for item in history:
        try:
            historical = H5BrowserRecoveryAttemptFacts.from_dict(item)
        except (TypeError, ValueError) as exc:
            raise ExplorerWorkPersistenceError(
                "H5 browser recovery attempt history 事实无效"
            ) from exc
        if (
            historical.status != "completed"
            or historical.task_id != work.task_id
            or historical.failed_work_id != work.work_id
            or historical.agent_run_id != work.frontier.observation.metadata.get("agent_run_id")
            or historical.source_session_id != work.session_id
            or historical.source_state_version != work.frontier.observation.state_version
            or historical.frontier_snapshot_id != work.frontier_snapshot_id
            or historical.deadline != work.deadline.isoformat()
            or historical.last_safe_location != work.frontier.observation.location
            or historical.recovery_attempt_count
            > (1 if historical.failure_stage is H5FailureStage.BEFORE_CAPTURE else 2)
            or (
                previous_history is not None
                and (
                    historical.failure_stage is not previous_history.failure_stage
                    or historical.recovery_attempt_count
                    != previous_history.recovery_attempt_count + 1
                )
            )
            or historical.payload_hash in history_hashes
        ):
            raise ExplorerWorkPersistenceError("H5 browser recovery attempt history 身份或顺序无效")
        history_hashes.add(historical.payload_hash)
        previous_history = historical
    if history and H5BrowserRecoveryAttemptFacts.from_dict(history[0]).recovery_attempt_count != 1:
        raise ExplorerWorkPersistenceError(
            "H5 browser recovery attempt history 缺少 attempt 1 genesis"
        )
    if history and attempt is not None:
        active = H5BrowserRecoveryAttemptFacts.from_dict(attempt)
        assert previous_history is not None
        if (
            active.failure_stage is not previous_history.failure_stage
            or active.recovery_attempt_count != previous_history.recovery_attempt_count + 1
        ):
            raise ExplorerWorkPersistenceError(
                "H5 browser recovery active attempt 与 history 不连续"
            )
    if attempt is not None:
        active = H5BrowserRecoveryAttemptFacts.from_dict(attempt)
        if active.recovery_attempt_count > (
            1 if active.failure_stage is H5FailureStage.BEFORE_CAPTURE else 2
        ) or (not history and active.recovery_attempt_count != 1):
            raise ExplorerWorkPersistenceError(
                "H5 browser recovery active attempt genesis 或 stage bound 无效"
            )

    parent_audit = work.frontier.observation.metadata.get(
        "h5_browser_recovery_parent_attempt_audit",
        (),
    )
    if not isinstance(parent_audit, (list, tuple)):
        raise ExplorerWorkPersistenceError("H5 browser recovery parent attempt audit 无效")
    audit_hashes: set[str] = set()
    immediate_parent_audited = recovery is None
    for item in parent_audit:
        try:
            audited = H5BrowserRecoveryAttemptFacts.from_dict(item)
        except (TypeError, ValueError) as exc:
            raise ExplorerWorkPersistenceError(
                "H5 browser recovery parent attempt audit 事实无效"
            ) from exc
        if (
            audited.status != "completed"
            or audited.task_id != work.task_id
            or audited.agent_run_id != work.frontier.observation.metadata.get("agent_run_id")
            or audited.payload_hash in audit_hashes
        ):
            raise ExplorerWorkPersistenceError("H5 browser recovery parent attempt audit 身份无效")
        audit_hashes.add(audited.payload_hash)
        if recovery is not None and (
            audited.failed_work_id == facts.failed_work_id
            and audited.source_session_id == facts.source_session_id
            and audited.failure_stage is facts.failure_stage
            and audited.recovery_attempt_count == facts.recovery_attempt_count
        ):
            immediate_parent_audited = True
    if parent_audit and not immediate_parent_audited:
        raise ExplorerWorkPersistenceError(
            "H5 browser recovery successor 缺少直接父 Work attempt 审计"
        )


def _require_lineage_text(metadata: Mapping[str, Any], field_name: str) -> str:
    value = metadata.get(field_name)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ExplorerWorkPersistenceError(f"后继 Explorer Work 缺少 {field_name} 谱系")
    return value


async def _persist_h5_coverage_snapshot(connection: Any, *, work: ExplorerWork) -> None:
    """与 H5 Work 原子保存同一份覆盖事实；历史或非 H5 Work 不伪造快照。"""
    if not isinstance(work, ExplorationWork) or work.surface_kind is not SurfaceKind.WEB:
        return
    metadata = work.frontier.observation.metadata
    if not isinstance(metadata.get(H5_TASK_FRONTIER_METADATA_KEY), Mapping):
        return
    raw_coverage = metadata.get(H5_COVERAGE_METADATA_KEY)
    if not isinstance(raw_coverage, Mapping):
        if not isinstance(metadata.get("agent_run_id"), str):
            return
        raise ExplorerWorkPersistenceError("H5 Exploration Work 缺少 coverage snapshot")
    try:
        coverage = H5CoverageSnapshot.from_dict(raw_coverage)
    except (TypeError, ValueError) as exc:
        raise ExplorerWorkPersistenceError("H5 coverage snapshot 无效") from exc
    agent_run_id = metadata.get("agent_run_id")
    if not isinstance(agent_run_id, str) or not agent_run_id.strip():
        raise ExplorerWorkPersistenceError("H5 coverage snapshot 缺少 agent_run_id")
    finalization_epoch = await connection.fetchval(
        """
        SELECT finalization_epoch
        FROM qc_task
        WHERE id = $1::uuid
          AND target_platform IN ('h5', 'web')
          AND status IN ('queued', 'running')
          AND finalization_state = 'open'
        """,
        work.task_id,
    )
    if finalization_epoch is None:
        raise ExplorerWorkPersistenceError("H5 coverage 任务已不在可写 epoch")
    snapshot_id = "h5-coverage:" + stable_protocol_hash(
        {
            "task_id": work.task_id,
            "agent_run_id": agent_run_id,
            "finalization_epoch": int(finalization_epoch),
            "revision": coverage.revision,
        }
    )
    row = await connection.fetchrow(
        """
        INSERT INTO qc_h5_coverage_snapshot AS existing (
            coverage_snapshot_id, task_id, agent_run_id, finalization_epoch,
            revision, authority_source, required_page_types, covered_page_types,
            missing_page_types, coverage_scopes, assessment_scope_ready,
            frontier_terminal, completion_ready, schema_version, payload_hash
        )
        VALUES (
            $1, $2::uuid, $3::uuid, $4, $5, $6, $7::jsonb, $8::jsonb,
            $9::jsonb, $10::jsonb, $11, $12, $13, $14, $15
        )
        ON CONFLICT (coverage_snapshot_id) DO UPDATE
        SET coverage_snapshot_id = existing.coverage_snapshot_id
        WHERE existing.task_id = EXCLUDED.task_id
          AND existing.agent_run_id = EXCLUDED.agent_run_id
          AND existing.finalization_epoch = EXCLUDED.finalization_epoch
          AND existing.revision = EXCLUDED.revision
          AND existing.payload_hash = EXCLUDED.payload_hash
        RETURNING coverage_snapshot_id
        """,
        snapshot_id,
        work.task_id,
        agent_run_id,
        int(finalization_epoch),
        coverage.revision,
        coverage.authority_source,
        _json_dumps(coverage.required_page_types),
        _json_dumps(coverage.covered_page_types),
        _json_dumps(coverage.missing_page_types),
        _json_dumps(coverage.scopes),
        coverage.assessment_scope_ready,
        coverage.frontier_terminal,
        coverage.completion_ready,
        coverage.schema_version,
        coverage.payload_hash,
    )
    if row is None:
        raise ExplorerWorkPersistenceError("H5 coverage revision 发生身份漂移")


def _validate_work_transition(command: TransitionExplorerWorkCommand) -> None:
    allowed = {
        ExplorerWorkStatus.LEASED: {ExplorerWorkStatus.RUNNING},
        ExplorerWorkStatus.RUNNING: {
            ExplorerWorkStatus.SUCCEEDED,
            ExplorerWorkStatus.BOUNDED,
            ExplorerWorkStatus.BUDGET_EXHAUSTED,
            ExplorerWorkStatus.BLOCKED,
            ExplorerWorkStatus.NO_PROGRESS,
            ExplorerWorkStatus.RETRY_WAIT,
            ExplorerWorkStatus.FAILED,
            ExplorerWorkStatus.CANCELLED,
        },
    }
    if command.target_status not in allowed.get(command.expected_status, set()):
        raise ValueError("Explorer Work 状态迁移不受支持")
    if command.reset_attempt_count and command.target_status is not ExplorerWorkStatus.RETRY_WAIT:
        raise ValueError("只有重投 Explorer Work 才能重置投递次数")
    terminal_statuses_requiring_payload = {
        ExplorerWorkStatus.BOUNDED,
        ExplorerWorkStatus.BUDGET_EXHAUSTED,
        ExplorerWorkStatus.BLOCKED,
        ExplorerWorkStatus.NO_PROGRESS,
    }
    if command.target_status in terminal_statuses_requiring_payload and command.terminal is None:
        raise ValueError("Explorer 无动作终态必须提供 ExplorationTerminal")
    if command.terminal is not None and command.terminal.work_id != command.work_id:
        raise ValueError("ExplorationTerminal 与工作身份不一致")
    if command.terminal is not None:
        target_by_terminal = {
            ExplorationTerminalStatus.COMPLETED: ExplorerWorkStatus.SUCCEEDED,
            ExplorationTerminalStatus.BOUNDED: ExplorerWorkStatus.BOUNDED,
            ExplorationTerminalStatus.BUDGET_EXHAUSTED: (ExplorerWorkStatus.BUDGET_EXHAUSTED),
            ExplorationTerminalStatus.BLOCKED: ExplorerWorkStatus.BLOCKED,
            ExplorationTerminalStatus.NO_PROGRESS: ExplorerWorkStatus.NO_PROGRESS,
        }
        if target_by_terminal[command.terminal.status] is not command.target_status:
            raise ValueError("ExplorationTerminal 与目标工作状态不一致")
    if command.retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds 不能小于 0")


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ExplorerWorkPersistenceError(f"{field_name} 必须为对象")
    return value


def _as_optional_mapping(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    return _as_mapping(value, "terminal_payload")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validated_work_payload(work: ExplorerWork) -> dict[str, Any]:
    payload = work.to_dict()
    validate_protocol_payload(payload, field_name=type(work).__name__)
    return payload
