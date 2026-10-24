"""Evidence Request 生命周期的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from services.api.app.application.evidence_requests import (
    EvidenceRequestClaimDisposition,
    EvidenceRequestClaimResult,
    EvidenceRequestLeaseReleaseDisposition,
    EvidenceRequestLeaseReleaseResult,
    EvidenceRequestProcessingLease,
    EvidenceRequestStatus,
    ReleaseEvidenceRequestLeaseCommand,
    SettleEvidenceRequestLeaseCommand,
    StoredEvidenceRequest,
    TransitionEvidenceRequestCommand,
)
from services.api.app.domain.inspection import EvidenceRequest, EvidenceSessionDependency
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.h5_snapshot_recovery_scope import (
    h5_snapshot_task_permission_sql,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class EvidenceRequestPersistenceError(RuntimeError):
    """请求归属、幂等身份或乐观锁不满足持久化要求。"""


class PostgresEvidenceRequestRepository:
    """保存补采请求，并负责过期、终态和关联 Probe 收敛。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None,
                 h5_snapshot_recovery: bool = False) -> None:
        self._database_url = database_url
        self._pool = pool
        self._h5_snapshot_recovery = h5_snapshot_recovery

    def _lease_task_permission(self) -> str:
        if not self._h5_snapshot_recovery:
            return "task.status = 'running'"
        return f"""(task.status = 'running' OR (
            request.session_dependency = 'snapshot'
            AND EXISTS (SELECT 1 FROM qc_capture AS h5_source
                WHERE h5_source.id = request.target_capture_id
                  AND h5_source.task_id = task.id
                  AND {h5_snapshot_task_permission_sql(capture='h5_source')})
        ))"""

    async def upsert(self, *, request: EvidenceRequest) -> None:
        """独立受理请求；不在同一事务内创建 Probe Work。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self.upsert_in_transaction(connection, request=request)

    async def upsert_in_transaction(
        self,
        connection: Any,
        *,
        request: EvidenceRequest,
    ) -> None:
        """由 Probe 发布事务调用，确保请求与工作不会出现半提交。"""
        payload = request.to_dict()
        # 所有多锁事务先取得 Task Finalization 锁，再取得 Capture Exit Gate 锁，
        # 同时避免完成门禁穿透和 Probe 发布时的反向锁死。
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            f"task-finalization:{request.task_id}",
        )
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            f"evidence-exit:{request.task_id}:{request.target_capture_id}",
        )
        row = await connection.fetchrow(
            """
            INSERT INTO qc_evidence_request AS existing (
                request_id, requirement_hash, payload_hash, protocol_version,
                work_item_id, attempt_id, task_id, rule_id, rule_code,
                target_capture_id, session_dependency, replay_policy,
                required_equivalence, side_effect_policy,
                minimum_source_grade, request_payload, expires_at,
                finalization_epoch
            )
            SELECT
                $1,
                $2,
                $3,
                $4,
                work_item.id,
                attempt.id,
                work_item.task_id,
                member.rule_id,
                member.rule_code,
                $10::uuid,
                $11,
                $12,
                $13,
                $14,
                $15,
                $16::jsonb,
                $17,
                task.finalization_epoch
            FROM qc_inspection_work_item AS work_item
            INNER JOIN qc_task AS task
                ON task.id = work_item.task_id
               AND (
                   task.status = 'running'
                   OR (
                       task.status = 'review'
                       AND lower(task.target_platform) = 'h5'
                       AND task.finalization_state = 'session_draining'
                       AND EXISTS (
                           SELECT 1 FROM qc_evidence_request AS accepted
                           WHERE accepted.request_id = $1
                             AND accepted.task_id = task.id
                             AND accepted.finalization_epoch = task.finalization_epoch
                             AND accepted.requirement_hash = $2
                             AND accepted.status IN ('pending', 'routed', 'running')
                       )
                   )
               )
               AND task.finalization_state IN (
                   'open', 'exploration_draining', 'session_draining'
               )
               AND work_item.finalization_epoch = task.finalization_epoch
            INNER JOIN qc_inspection_attempt AS attempt
                ON attempt.id = $6::uuid
               AND attempt.work_item_id = work_item.id
               AND attempt.status IN ('running', 'judged')
            INNER JOIN qc_inspection_work_item_rule AS member
                ON member.work_item_id = work_item.id
               AND member.rule_id = $8::uuid
               AND member.rule_code = $9
               AND member.status IN ('pending', 'judged', 'evidence_insufficient')
            WHERE work_item.id = $5::uuid
              AND work_item.task_id = $7::uuid
              AND (
                  work_item.page_capture_id = $10::uuid
                  OR (
                      lower(task.target_platform) = 'h5'
                      AND EXISTS (
                          SELECT 1
                          FROM qc_evidence_request AS prior_request
                          INNER JOIN qc_evidence_supplement AS supplement
                              ON supplement.request_id = prior_request.request_id
                             AND supplement.manifest_id = prior_request.resolution_manifest_id
                          INNER JOIN qc_evidence_manifest AS manifest
                              ON manifest.manifest_id = supplement.manifest_id
                             AND manifest.task_id = work_item.task_id
                             AND manifest.quality_status = 'pass'
                          INNER JOIN qc_capture AS capture
                              ON capture.id = manifest.capture_id
                             AND capture.task_id = work_item.task_id
                             AND capture.agent_run_id = work_item.producer_agent_run_id
                             AND capture.capture_status = 'captured'
                          WHERE prior_request.work_item_id = work_item.id
                            AND prior_request.task_id = work_item.task_id
                            AND prior_request.rule_id = member.rule_id
                            AND prior_request.rule_code = member.rule_code
                            AND prior_request.finalization_epoch = task.finalization_epoch
                            AND prior_request.status = 'satisfied'
                            AND (
                                capture.id = $10::uuid
                                OR EXISTS (
                                    SELECT 1 FROM qc_capture AS before_capture
                                    INNER JOIN qc_evidence_manifest AS before_manifest
                                        ON before_manifest.capture_id = before_capture.id
                                       AND before_manifest.task_id = work_item.task_id
                                       AND before_manifest.quality_status = 'pass'
                                    WHERE before_capture.id = $10::uuid
                                      AND before_capture.id = capture.parent_capture_id
                                      AND before_capture.task_id = capture.task_id
                                      AND before_capture.agent_run_id = capture.agent_run_id
                                      AND before_capture.surface_session_id
                                          = capture.surface_session_id
                                      AND before_capture.action_execution_id
                                          = capture.action_execution_id
                                      AND before_capture.capture_role = 'probe_before'
                                      AND capture.capture_role = 'probe_result_replay'
                                      AND before_capture.capture_status = 'captured'
                                )
                            )
                      )
                  )
              )
              AND work_item.control_mode = 'enforced'
              AND work_item.status IN ('running', 'waiting_evidence')
              AND $17 > now()
              AND NOT EXISTS (
                SELECT 1
                FROM qc_evidence_exit_gate_evaluation AS exit_gate
                WHERE exit_gate.task_id = work_item.task_id
                  AND exit_gate.source_capture_id IN (work_item.page_capture_id, $10::uuid)
                  AND exit_gate.reservation_status = 'reserved'
                  AND exit_gate.expires_at > now()
              )
            ON CONFLICT (request_id) DO UPDATE
            SET request_id = existing.request_id
            WHERE existing.requirement_hash = EXCLUDED.requirement_hash
              AND existing.protocol_version = EXCLUDED.protocol_version
              AND existing.work_item_id = EXCLUDED.work_item_id
              AND existing.task_id = EXCLUDED.task_id
              AND existing.rule_id = EXCLUDED.rule_id
              AND existing.rule_code = EXCLUDED.rule_code
              AND existing.target_capture_id = EXCLUDED.target_capture_id
            RETURNING existing.status
            """,
            request.request_id,
            request.requirement_hash,
            stable_protocol_hash(payload),
            request.protocol_version,
            request.work_item_id,
            request.attempt_id,
            request.task_id,
            request.rule_id,
            request.rule_code,
            request.target_capture_id,
            request.session_dependency.value,
            request.replay_policy.value,
            request.required_equivalence.value,
            request.side_effect_policy.value,
            request.minimum_source_grade.value,
            _json_dumps(payload),
            request.expires_at,
        )
        if row is None:
            raise EvidenceRequestPersistenceError("Evidence Request 归属无效或幂等身份发生漂移")

    async def get(self, *, request_id: str) -> StoredEvidenceRequest | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    request_payload,
                    status,
                    row_version,
                    resolution_manifest_id,
                    resolution_reason
                FROM qc_evidence_request
                WHERE request_id = $1
                """,
                request_id,
            )
        if row is None:
            return None
        return StoredEvidenceRequest(
            request=EvidenceRequest.from_dict(
                _as_mapping(row["request_payload"], "request_payload")
            ),
            status=EvidenceRequestStatus(str(row["status"])),
            row_version=int(row["row_version"]),
            resolution_manifest_id=(
                str(row["resolution_manifest_id"])
                if row["resolution_manifest_id"] is not None
                else None
            ),
            resolution_reason=row["resolution_reason"],
        )

    async def claim_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        lease_owner: str,
        lease_seconds: int,
        session_dependencies: tuple[EvidenceSessionDependency, ...] = (
            EvidenceSessionDependency.ACTIVE_SESSION,
        ),
        deferred_h5_snapshot_request_ids: tuple[str, ...] = (),
        h5_request_id: str | None = None,
    ) -> EvidenceRequestClaimResult:
        """按调用端声明的会话依赖原子领取请求；默认保持 App 活会话口径。"""

        for value, field_name in (
            (task_id, "task_id"),
            (agent_run_id, "agent_run_id"),
            (lease_owner, "lease_owner"),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise TypeError("lease_seconds 必须为整数")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于零")
        dependency_values = tuple(
            dependency.value
            if isinstance(dependency, EvidenceSessionDependency)
            else str(dependency).strip()
            for dependency in session_dependencies
        )
        if not dependency_values or any(
            value not in {item.value for item in EvidenceSessionDependency}
            for value in dependency_values
        ):
            raise ValueError("session_dependencies 必须为非空且受支持的会话依赖集合")
        if len(set(dependency_values)) != len(dependency_values):
            raise ValueError("session_dependencies 不能重复")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            row = await connection.fetchrow(
                """
                WITH candidate AS (
                    SELECT request.request_id
                    FROM qc_evidence_request AS request
                    INNER JOIN qc_capture AS capture
                        ON capture.id = request.target_capture_id
                       AND capture.task_id = request.task_id
                       AND capture.agent_run_id = $2::uuid
                    INNER JOIN qc_task AS task
                        ON task.id = request.task_id
                       AND __H5_LEASE_TASK_PERMISSION__
                       AND task.finalization_state IN (
                           'open', 'exploration_draining', 'session_draining'
                       )
                       AND task.finalization_epoch = request.finalization_epoch
                    WHERE request.task_id = $1::uuid
                      AND request.finalization_epoch = $3
                      AND request.session_dependency::text = ANY($6::text[])
                      __H5_DEFERRED_SNAPSHOTS__
                      __H5_EXACT_REQUEST__
                      AND request.expires_at > now()
                      AND request.next_attempt_at <= now()
                      AND request.attempt_count < request.max_attempts
                      AND (
                          request.lease_owner IS NULL
                          OR request.lease_expires_at <= now()
                      )
                      AND request.status = 'pending'
                    ORDER BY request.created_at, request.request_id
                    FOR UPDATE OF request SKIP LOCKED
                    LIMIT 1
                )
                UPDATE qc_evidence_request AS request
                SET
                    lease_owner = $4,
                    lease_token = gen_random_uuid(),
                    lease_expires_at = now() + make_interval(secs => $5),
                    attempt_count = request.attempt_count + 1,
                    row_version = request.row_version + 1,
                    updated_at = now()
                FROM candidate
                WHERE request.request_id = candidate.request_id
                RETURNING
                    request.request_payload,
                    request.lease_token::text AS lease_token,
                    request.finalization_epoch,
                    request.row_version,
                    request.attempt_count,
                    request.max_attempts
                """.replace("__H5_LEASE_TASK_PERMISSION__", self._lease_task_permission())
                .replace("__H5_DEFERRED_SNAPSHOTS__", (
                    "AND NOT (task.target_platform = 'h5' "
                    "AND request.session_dependency = 'snapshot' "
                    "AND request.request_id = ANY($7::text[]))"
                    if deferred_h5_snapshot_request_ids else ""
                )).replace("__H5_EXACT_REQUEST__", (
                    "AND task.target_platform = 'h5' AND request.request_id = "
                    + ("$8" if deferred_h5_snapshot_request_ids else "$7")
                    if h5_request_id else ""
                )),
                task_id,
                agent_run_id,
                finalization_epoch,
                lease_owner,
                lease_seconds,
                list(dependency_values),
                *([list(deferred_h5_snapshot_request_ids)]
                  if deferred_h5_snapshot_request_ids else []),
                *([h5_request_id] if h5_request_id else []),
            )
        if row is None:
            return EvidenceRequestClaimResult(
                disposition=EvidenceRequestClaimDisposition.EMPTY
            )
        return EvidenceRequestClaimResult(
            disposition=EvidenceRequestClaimDisposition.CLAIMED,
            lease=EvidenceRequestProcessingLease(
                request=EvidenceRequest.from_dict(
                    _as_mapping(row["request_payload"], "request_payload")
                ),
                lease_owner=lease_owner,
                lease_token=str(row["lease_token"]),
                finalization_epoch=int(row["finalization_epoch"]),
                row_version=int(row["row_version"]),
                attempt_count=int(row["attempt_count"]),
                max_attempts=int(row["max_attempts"]),
            ),
        )

    async def renew_h5_lease(
        self, *, lease: EvidenceRequestProcessingLease, agent_run_id: str,
        lease_seconds: int, deadline: datetime,
    ) -> bool:
        """只续当前H5请求围栏；正式Probe交接须有本次尝试的持久化证明。"""
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正整数")
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("deadline 必须包含时区")
        fence = lease.fence
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{fence.task_id}",
            )
            return bool(await connection.fetchval(
                """WITH scope AS (
                    SELECT request.* FROM qc_evidence_request AS request
                    JOIN qc_task AS task ON task.id=request.task_id
                    JOIN qc_capture AS capture ON capture.id=request.target_capture_id
                        AND capture.task_id=task.id AND capture.agent_run_id=$7::uuid
                        AND capture.surface_kind='web' AND capture.capture_status='captured'
                    WHERE request.request_id=$1 AND request.task_id=$2::uuid
                        AND request.finalization_epoch=$3 AND task.finalization_epoch=$3
                        AND task.target_platform='h5' AND __H5_TASK_PERMISSION__
                        AND task.finalization_state IN (
                            'open','exploration_draining','session_draining')
                        AND request.expires_at>now() AND $9::timestamptz>now()
                        AND request.attempt_count=$10
                ), renewed AS (
                    UPDATE qc_evidence_request AS request
                    SET lease_expires_at=LEAST(now()+make_interval(secs=>$8),expires_at,$9),
                        updated_at=now()
                    WHERE request.request_id IN (SELECT request_id FROM scope)
                        AND request.status='pending' AND request.lease_owner=$4
                        AND request.lease_token=$5::uuid AND request.row_version=$6
                        AND request.lease_expires_at>now()
                    RETURNING 1
                ) SELECT EXISTS(SELECT 1 FROM renewed) OR EXISTS(
                    SELECT 1 FROM scope AS request
                    JOIN qc_v2_work_item AS probe ON probe.evidence_request_id=request.request_id
                        AND probe.task_id=request.task_id AND probe.finalization_epoch=$3
                        AND probe.work_type='probe'
                    JOIN qc_restoration_attempt AS restoration
                        ON restoration.attempt_id=probe.work_payload->>'restoration_attempt_id'
                        AND restoration.request_id=request.request_id
                        AND restoration.task_id=request.task_id
                        AND restoration.finalization_epoch=$3
                        AND restoration.attempt_no=$10 AND restoration.status='equivalent'
                        AND restoration.assessment_payload->>'passed'='true'
                        AND restoration.equivalence_assessment_id
                            =probe.work_payload->>'equivalence_assessment_id'
                        AND restoration.restored_capture_id=probe.source_capture_id
                    JOIN qc_capture AS restored_capture
                        ON restored_capture.id=probe.source_capture_id
                        AND restored_capture.task_id=request.task_id
                        AND restored_capture.agent_run_id=$7::uuid
                        AND restored_capture.surface_kind='web'
                        AND restored_capture.capture_status='captured'
                        AND restored_capture.surface_session_id=probe.session_id
                    WHERE request.status IN ('routed','running','satisfied')
                        AND request.lease_token IS NULL AND request.lease_owner IS NULL
                        AND request.row_version>$6
                )""".replace("__H5_TASK_PERMISSION__", self._lease_task_permission()),
                fence.request_id, fence.task_id, fence.finalization_epoch, fence.lease_owner,
                fence.lease_token, fence.row_version, agent_run_id, lease_seconds, deadline,
                lease.attempt_count,
            ))

    async def release_lease(
        self,
        *,
        command: ReleaseEvidenceRequestLeaseCommand,
    ) -> EvidenceRequestLeaseReleaseResult:
        """释放有效租约并安排重试；耗尽或过期时直接关闭请求。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{command.task_id}",
            )
            row = await connection.fetchrow(
                """
                WITH eligible AS (
                    SELECT
                        request.request_id,
                        CASE
                            WHEN request.expires_at <= now() THEN 'expired'
                            WHEN request.attempt_count >= request.max_attempts
                              OR $7 >= request.expires_at
                            THEN 'unfulfillable'
                            ELSE 'pending'
                        END AS target_status
                    FROM qc_evidence_request AS request
                    INNER JOIN qc_task AS task
                        ON task.id = request.task_id
                       AND __H5_LEASE_TASK_PERMISSION__
                       AND task.finalization_state IN (
                           'open', 'exploration_draining', 'session_draining'
                       )
                       AND task.finalization_epoch = request.finalization_epoch
                    WHERE request.request_id = $1
                      AND request.task_id = $2::uuid
                      AND request.finalization_epoch = $3
                      AND request.lease_owner = $4
                      AND request.lease_token = $5::uuid
                      AND request.row_version = $6
                      AND request.lease_expires_at > now()
                      AND request.status = 'pending'
                      AND $7 > now()
                    FOR UPDATE OF request
                )
                UPDATE qc_evidence_request AS request
                SET
                    status = eligible.target_status,
                    next_attempt_at = CASE
                        WHEN eligible.target_status = 'pending' THEN $7
                        ELSE request.next_attempt_at
                    END,
                    resolution_reason = CASE
                        WHEN eligible.target_status = 'expired'
                        THEN 'EVIDENCE_REQUEST_EXPIRED'
                        WHEN eligible.target_status = 'unfulfillable'
                        THEN 'EVIDENCE_REQUEST_RETRY_EXHAUSTED:' || $8
                        ELSE $8
                    END,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    row_version = request.row_version + 1,
                    completed_at = CASE
                        WHEN eligible.target_status = 'pending' THEN NULL
                        ELSE now()
                    END,
                    updated_at = now()
                FROM eligible
                WHERE request.request_id = eligible.request_id
                RETURNING request.status, request.row_version
                """.replace("__H5_LEASE_TASK_PERMISSION__", self._lease_task_permission()),
                command.request_id,
                command.task_id,
                command.finalization_epoch,
                command.lease_owner,
                command.lease_token,
                command.row_version,
                command.next_attempt_at,
                command.reason_code,
            )
        if row is None:
            raise EvidenceRequestPersistenceError(
                "Evidence Request 任务执行域、状态、版本或租约已变化"
            )
        status = EvidenceRequestStatus(str(row["status"]))
        dispositions = {
            EvidenceRequestStatus.PENDING: (
                EvidenceRequestLeaseReleaseDisposition.RETRY_SCHEDULED
            ),
            EvidenceRequestStatus.UNFULFILLABLE: (
                EvidenceRequestLeaseReleaseDisposition.RETRY_EXHAUSTED
            ),
            EvidenceRequestStatus.EXPIRED: (
                EvidenceRequestLeaseReleaseDisposition.REQUEST_EXPIRED
            ),
        }
        try:
            disposition = dispositions[status]
        except KeyError as exc:
            raise EvidenceRequestPersistenceError("Evidence Request 租约释放结果无效") from exc
        return EvidenceRequestLeaseReleaseResult(
            disposition=disposition,
            row_version=int(row["row_version"]),
        )

    async def settle_lease(
        self,
        *,
        command: SettleEvidenceRequestLeaseCommand,
    ) -> int:
        """以完整租约 fencing 写入不可继续处理的确定性终态。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{command.task_id}",
            )
            row_version = await connection.fetchval(
                """
                UPDATE qc_evidence_request AS request
                SET
                    status = $7,
                    resolution_reason = $8,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    row_version = request.row_version + 1,
                    completed_at = now(),
                    updated_at = now()
                FROM qc_task AS task
                WHERE request.request_id = $1
                  AND request.task_id = $2::uuid
                  AND request.finalization_epoch = $3
                  AND request.lease_owner = $4
                  AND request.lease_token = $5::uuid
                  AND request.row_version = $6
                  AND request.lease_expires_at > now()
                  AND request.status = 'pending'
                  AND ($7::varchar <> 'expired' OR request.expires_at <= now())
                  AND task.id = request.task_id
                  AND __H5_LEASE_TASK_PERMISSION__
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND task.finalization_epoch = request.finalization_epoch
                RETURNING request.row_version
                """.replace("__H5_LEASE_TASK_PERMISSION__", self._lease_task_permission()),
                command.request_id,
                command.task_id,
                command.finalization_epoch,
                command.lease_owner,
                command.lease_token,
                command.row_version,
                command.target_status.value,
                command.resolution_reason,
            )
        if row_version is None:
            raise EvidenceRequestPersistenceError(
                "Evidence Request 任务执行域、状态、版本或租约已变化"
            )
        return int(row_version)

    async def transition(self, *, command: TransitionEvidenceRequestCommand) -> int:
        _validate_transition(command)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row_version = await connection.fetchval(
                """
                UPDATE qc_evidence_request AS request
                SET
                    status = $2,
                    resolution_manifest_id = $5,
                    resolution_reason = $6,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    row_version = request.row_version + 1,
                    -- $2 同时用于 SET status 和这里的终态判断；不显式转型
                    -- Postgres 会一边推 varchar 一边推 text，直接报参数类型冲突。
                    completed_at = CASE
                        WHEN $2::varchar = ANY($7::varchar[]) THEN now()
                        ELSE NULL
                    END,
                    updated_at = now()
                WHERE request.request_id = $1
                  AND request.status = $3
                  AND request.row_version = $4
                  AND (
                    request.lease_token IS NULL
                    OR request.lease_expires_at <= now()
                  )
                  AND (
                    $5::text IS NULL
                    OR EXISTS (
                        SELECT 1
                        FROM qc_evidence_manifest AS manifest
                        WHERE manifest.manifest_id = $5
                          AND manifest.task_id = request.task_id
                          AND manifest.quality_status = 'pass'
                          AND (
                            manifest.capture_id = request.target_capture_id
                            OR EXISTS (
                                SELECT 1
                                FROM qc_evidence_supplement AS supplement
                                WHERE supplement.request_id = request.request_id
                                  AND supplement.manifest_id = manifest.manifest_id
                            )
                          )
                    )
                  )
                RETURNING request.row_version
                """,
                command.request_id,
                command.target_status.value,
                command.expected_status.value,
                command.row_version,
                command.resolution_manifest_id,
                command.resolution_reason,
                [status.value for status in EvidenceRequestStatus if status.is_terminal],
            )
        if row_version is None:
            raise EvidenceRequestPersistenceError("Evidence Request 状态、版本或证据归属已变化")
        return int(row_version)

    async def expire_due(self, *, limit: int = 100) -> int:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            result = await connection.fetchval(
                """
                WITH due AS (
                    SELECT request_id
                    FROM qc_evidence_request
                    WHERE status IN ('pending', 'routed', 'running')
                      AND expires_at <= now()
                    ORDER BY expires_at, request_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                ), updated AS (
                    UPDATE qc_evidence_request AS request
                    SET
                        status = 'expired',
                        resolution_reason = 'request_expired',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        row_version = request.row_version + 1,
                        completed_at = now(),
                        updated_at = now()
                    FROM due
                    WHERE request.request_id = due.request_id
                    RETURNING 1
                )
                SELECT count(*) FROM updated
                """,
                limit,
            )
            await connection.execute(
                """
                UPDATE qc_v2_work_item AS work_item
                SET
                    status = 'cancelled',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = 'EVIDENCE_REQUEST_EXPIRED',
                    last_error_message = '关联 Evidence Request 已过期',
                    finished_at = now(),
                    row_version = work_item.row_version + 1,
                    updated_at = now()
                FROM qc_evidence_request AS request
                WHERE work_item.evidence_request_id = request.request_id
                  AND request.status = 'expired'
                  AND work_item.status NOT IN (
                    'succeeded', 'bounded', 'budget_exhausted', 'blocked',
                    'no_progress', 'failed', 'cancelled', 'superseded'
                  )
                """
            )
        return int(result or 0)


    async def annotate_resealed_input_set(
        self,
        *,
        request_id: str,
        input_set_id: str,
    ) -> str | None:
        """在 satisfied 请求的 resolution_reason 上幂等附加 resealed_input_set 标记。"""
        from services.api.app.application.supplement_input_set_reseal import (
            format_resolution_reason_with_reseal,
            parse_resealed_input_set_id,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT resolution_reason
                FROM qc_evidence_request
                WHERE request_id = $1
                  AND status = 'satisfied'
                FOR UPDATE
                """,
                request_id,
            )
            if row is None:
                return None
            current = row["resolution_reason"] or "satisfied"
            if parse_resealed_input_set_id(current) == input_set_id:
                return current
            updated = format_resolution_reason_with_reseal(
                base_reason=str(current),
                input_set_id=input_set_id,
            )
            await connection.execute(
                """
                UPDATE qc_evidence_request
                SET
                    resolution_reason = $2,
                    updated_at = now()
                WHERE request_id = $1
                  AND status = 'satisfied'
                """,
                request_id,
                updated,
            )
            return updated

    async def has_nonterminal_active_session_request(self, *, task_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM qc_evidence_request
                        WHERE task_id = $1::uuid
                          AND session_dependency = 'active_session'
                          AND status IN ('pending', 'routed', 'running')
                    )
                    """,
                    task_id,
                )
            )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _validate_transition(command: TransitionEvidenceRequestCommand) -> None:
    allowed = {
        EvidenceRequestStatus.PENDING: {
            EvidenceRequestStatus.ROUTED,
            EvidenceRequestStatus.EXPIRED,
            EvidenceRequestStatus.CANCELLED,
            EvidenceRequestStatus.UNFULFILLABLE,
        },
        EvidenceRequestStatus.ROUTED: {
            EvidenceRequestStatus.RUNNING,
            EvidenceRequestStatus.EXPIRED,
            EvidenceRequestStatus.CANCELLED,
            EvidenceRequestStatus.UNFULFILLABLE,
        },
        EvidenceRequestStatus.RUNNING: {
            EvidenceRequestStatus.SATISFIED,
            EvidenceRequestStatus.EXPIRED,
            EvidenceRequestStatus.CANCELLED,
            EvidenceRequestStatus.UNFULFILLABLE,
        },
    }
    if command.target_status not in allowed.get(command.expected_status, set()):
        raise ValueError("Evidence Request 状态迁移不受支持")
    if (
        command.target_status is EvidenceRequestStatus.SATISFIED
        and command.resolution_manifest_id is None
    ):
        raise ValueError("满足 Evidence Request 必须提供质量通过的 Manifest")
    if (
        command.target_status is not EvidenceRequestStatus.SATISFIED
        and command.resolution_manifest_id is not None
    ):
        raise ValueError("只有满足 Evidence Request 才能关联 Manifest")
    if (
        command.target_status is EvidenceRequestStatus.UNFULFILLABLE
        and command.resolution_reason is None
    ):
        raise ValueError("不可补采终态必须提供原因")


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise EvidenceRequestPersistenceError(f"{field_name} 必须为对象")
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
