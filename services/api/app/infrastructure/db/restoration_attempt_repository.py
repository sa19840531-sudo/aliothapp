"""Restoration Attempt 与 State Equivalence 的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.evidence_requests import EvidenceRequestLeaseFence
from services.api.app.application.restoration import new_running_restoration_attempt
from services.api.app.domain.inspection import (
    RestorationAttempt,
    RestorationAttemptStatus,
    RestorationPlan,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.h5_snapshot_recovery_scope import (
    h5_snapshot_request_constraints_sql,
    h5_snapshot_task_permission_sql,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class RestorationAttemptPersistenceError(RuntimeError):
    """恢复归属、状态等价证明或乐观锁不满足持久化要求。

    ``reason_code`` 是稳定原因码：创建恢复尝试被拒时由仓储诊断出到底是 Evidence
    Request、任务、原 Capture、重放计划还是幂等身份不满足；没有诊断结果时为 ``None``。
    """

    def __init__(self, message: str = "", *, reason_code: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code


# 创建恢复尝试被拒的稳定原因码：按 start() 那条 INSERT 的前置条件顺序，只报第一条不满足的。
ATTEMPT_REJECTION_EVIDENCE_REQUEST_MISSING = "EVIDENCE_REQUEST_MISSING"
ATTEMPT_REJECTION_EVIDENCE_REQUEST_INVALID = "EVIDENCE_REQUEST_INVALID"
ATTEMPT_REJECTION_EVIDENCE_REQUEST_EXPIRED = "EVIDENCE_REQUEST_EXPIRED"
ATTEMPT_REJECTION_TASK_NOT_RUNNABLE = "TASK_NOT_RUNNABLE"
ATTEMPT_REJECTION_ORIGINAL_CAPTURE_INVALID = "ORIGINAL_CAPTURE_INVALID"
ATTEMPT_REJECTION_REPLAY_PLAN_MISSING = "REPLAY_PLAN_MISSING"
ATTEMPT_REJECTION_ATTEMPT_IDENTITY_DRIFT = "ATTEMPT_IDENTITY_DRIFT"
_ATTEMPT_REJECTION_GENERIC_MESSAGE = "恢复尝试归属无效、请求已终结或幂等计划发生漂移"
_RESTORABLE_REQUEST_STATUSES = frozenset({"pending", "routed", "running"})
_RESTORABLE_TASK_STAGES = frozenset({"open", "exploration_draining", "session_draining"})


class PostgresRestorationAttemptRepository:
    """幂等创建恢复尝试，并以乐观锁提交唯一终态。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def start(
        self,
        *,
        plan: RestorationPlan,
        attempt_no: int,
    ) -> RestorationAttempt:
        candidate = new_running_restoration_attempt(
            plan=plan,
            attempt_no=attempt_no,
            started_at=datetime.now(UTC),
        )
        plan_payload = plan.to_dict()
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{plan.task_id}",
            )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_restoration_attempt AS existing (
                    attempt_id, plan_id, plan_hash, plan_protocol_version,
                    plan_payload, request_id, task_id, original_capture_id,
                    source_session_id, target_session_id, surface_kind,
                    strategy, replay_execution_plan_id, attempt_no,
                    source_grade, attempt_protocol_version, started_at,
                    finalization_epoch
                )
                SELECT
                    $1,
                    $2,
                    $3,
                    $4,
                    $5::jsonb,
                    request.request_id,
                    request.task_id,
                    capture.id,
                    $9::text,
                    $10::text,
                    $11::text,
                    $12::text,
                    replay_plan.execution_plan_id,
                    $14,
                    $15::text,
                    $16,
                    -- H5 开始和结束均由执行器计时，避免跨主机时钟差
                    -- 把快速恢复变成 finished_at 早于 started_at。
                    CASE WHEN lower(task.target_platform) = 'h5'
                        THEN $18::timestamptz ELSE now() END,
                    task.finalization_epoch
                FROM qc_evidence_request AS request
                INNER JOIN qc_task AS task
                    ON task.id = request.task_id
                   AND (
                       task.status = 'running'
                       OR (
                           task.status = 'review'
                           AND lower(task.target_platform) = 'h5'
                           AND task.finalization_state = 'session_draining'
                           AND (request.session_dependency = 'active_session'
                                OR ($12::text = 'new_session_replay'
                                    AND __H5_SNAPSHOT_CONSTRAINTS__))
                       )
                   )
                   AND task.finalization_state IN (
                       'open', 'exploration_draining', 'session_draining'
                   )
                   AND request.finalization_epoch = task.finalization_epoch
                INNER JOIN qc_capture AS capture
                    ON capture.id = request.target_capture_id
                   AND capture.task_id = task.id
                   AND capture.capture_status = 'captured'
                   AND capture.captured_at IS NOT NULL
                LEFT JOIN qc_journey_execution_plan AS replay_plan
                    ON replay_plan.execution_plan_id = $13
                   AND replay_plan.task_id = task.id
                WHERE request.request_id = $6
                  AND request.task_id = $7::uuid
                  AND request.target_capture_id = $8::uuid
                  AND request.status IN ('pending', 'routed', 'running')
                  AND request.expires_at > now()
                  AND $17 <= request.expires_at
                  AND (
                    $13::text IS NULL
                    OR replay_plan.execution_plan_id IS NOT NULL
                    OR (
                        $12::text = 'new_session_replay' AND $15::text = 'C' AND $11::text = 'web'
                        AND $13::text ~ '^h5-new-session-route:[0-9a-f]{64}$'
                        AND __H5_SNAPSHOT_CONSTRAINTS__ AND __H5_SNAPSHOT_TASK_PERMISSION__
                        AND request.status = 'pending' AND request.lease_token IS NOT NULL
                        AND request.lease_expires_at > now()
                        AND $10::text ~ ('^h5:' || capture.agent_run_id::text
                            || ':recapture:[0-9a-f]{20}$')
                        AND $9::text = capture.surface_session_id
                        AND EXISTS (SELECT 1 FROM qc_surface_session AS source
                            WHERE source.session_id = $9::text AND source.task_id = task.id
                              AND source.finalization_epoch = task.finalization_epoch
                                AND source.status IN ('closed', 'lost')
                                AND source.surface_kind = 'web'
                              AND ((source.session_owner = 'h5-workflow'
                                    AND source.session_id = 'h5:' || capture.agent_run_id::text)
                                   OR (source.session_owner =
                                           'h5-browser-recovery:' || capture.agent_run_id::text
                                       AND source.session_id ~ ('^h5:' || capture.agent_run_id::text
                                           || ':recovery:[0-9a-f]{20}$'))))
                    )
                  )
                ON CONFLICT (attempt_id) DO UPDATE
                SET attempt_id = existing.attempt_id
                WHERE existing.plan_id = EXCLUDED.plan_id
                  AND existing.plan_hash = EXCLUDED.plan_hash
                  AND existing.request_id = EXCLUDED.request_id
                  AND existing.task_id = EXCLUDED.task_id
                  AND existing.attempt_no = EXCLUDED.attempt_no
                RETURNING
                    existing.attempt_id,
                    existing.plan_payload,
                    existing.attempt_no,
                    existing.status,
                    existing.restored_capture_id::text AS restored_capture_id,
                    existing.assessment_payload,
                    existing.action_execution_ids,
                    existing.error_code,
                    existing.error_message,
                    existing.started_at,
                    existing.finished_at,
                    existing.row_version,
                    existing.attempt_protocol_version
                """.replace("__H5_SNAPSHOT_CONSTRAINTS__", h5_snapshot_request_constraints_sql())
                .replace("__H5_SNAPSHOT_TASK_PERMISSION__", h5_snapshot_task_permission_sql()),
                candidate.attempt_id,
                plan.plan_id,
                plan.plan_hash,
                plan.protocol_version,
                _json_dumps(plan_payload),
                plan.request_id,
                plan.task_id,
                plan.original_capture_id,
                plan.source_session_id,
                plan.target_session_id,
                plan.surface_kind.value,
                plan.strategy.value,
                plan.replay_execution_plan_id,
                attempt_no,
                plan.source_grade.value,
                candidate.protocol_version,
                plan.deadline,
                candidate.started_at,
            )
        if row is None:
            reason_code, message = await self._diagnose_start_rejection(
                pool,
                plan=plan,
                attempt_no=attempt_no,
                candidate_attempt_id=candidate.attempt_id,
            )
            raise RestorationAttemptPersistenceError(message, reason_code=reason_code)
        return _parse_attempt(row)

    async def _diagnose_start_rejection(
        self,
        pool: Any,
        *,
        plan: RestorationPlan,
        attempt_no: int,
        candidate_attempt_id: str,
    ) -> tuple[str | None, str]:
        """创建被拒后，按 INSERT 的前置条件顺序找出第一条不满足的，给稳定原因码。

        只读；诊断拿不到事实时退回笼统文案、原因码为 ``None``。
        """
        async with pool.acquire() as connection:
            facts = await connection.fetchrow(
                """
                SELECT
                    request.request_id IS NOT NULL AS request_exists,
                    request.task_id::text AS request_task_id,
                    request.target_capture_id::text AS request_target_capture_id,
                    request.status AS request_status,
                    request.session_dependency AS request_session_dependency,
                    request.expires_at <= now() AS request_expired,
                    request.expires_at < $5 AS plan_deadline_beyond_request,
                    request.finalization_epoch AS request_epoch,
                    task.id IS NOT NULL AS task_exists,
                    task.status AS task_status,
                    task.target_platform AS task_target_platform,
                    task.finalization_state AS task_finalization_state,
                    task.finalization_epoch AS task_epoch,
                    capture.id IS NOT NULL AS capture_exists,
                    capture.task_id::text AS capture_task_id,
                    capture.capture_status AS capture_status,
                    capture.captured_at IS NOT NULL AS capture_captured,
                    replay_plan.execution_plan_id IS NOT NULL AS replay_plan_exists,
                    replay_plan.task_id::text AS replay_plan_task_id,
                    existing.attempt_id IS NOT NULL AS attempt_exists,
                    existing.plan_id AS existing_plan_id,
                    existing.plan_hash AS existing_plan_hash,
                    existing.request_id AS existing_request_id,
                    existing.attempt_no AS existing_attempt_no
                FROM (SELECT 1) AS anchor
                LEFT JOIN qc_evidence_request AS request ON request.request_id = $1
                LEFT JOIN qc_task AS task ON task.id = request.task_id
                LEFT JOIN qc_capture AS capture ON capture.id = $2::uuid
                LEFT JOIN qc_journey_execution_plan AS replay_plan
                    ON replay_plan.execution_plan_id = $3::text
                LEFT JOIN qc_restoration_attempt AS existing ON existing.attempt_id = $4
                """,
                plan.request_id,
                plan.original_capture_id,
                plan.replay_execution_plan_id,
                candidate_attempt_id,
                plan.deadline,
            )
        if facts is None:
            return None, _ATTEMPT_REJECTION_GENERIC_MESSAGE
        if not facts["request_exists"]:
            return (
                ATTEMPT_REJECTION_EVIDENCE_REQUEST_MISSING,
                f"Evidence Request {plan.request_id} 不存在，无法创建恢复尝试",
            )
        if (
            facts["request_task_id"] != plan.task_id
            or facts["request_target_capture_id"] != plan.original_capture_id
        ):
            return (
                ATTEMPT_REJECTION_EVIDENCE_REQUEST_INVALID,
                f"Evidence Request {plan.request_id} 的任务或目标 Capture 与恢复计划不一致",
            )
        if facts["request_status"] not in _RESTORABLE_REQUEST_STATUSES:
            return (
                ATTEMPT_REJECTION_EVIDENCE_REQUEST_INVALID,
                f"Evidence Request {plan.request_id} 已进入 {facts['request_status']}，"
                "不能再恢复",
            )
        if facts["request_expired"]:
            return (
                ATTEMPT_REJECTION_EVIDENCE_REQUEST_EXPIRED,
                f"Evidence Request {plan.request_id} 已过期",
            )
        if facts["plan_deadline_beyond_request"]:
            return (
                ATTEMPT_REJECTION_EVIDENCE_REQUEST_EXPIRED,
                "恢复计划截止时间晚于 Evidence Request 过期时间",
            )
        h5_review_session_draining = (
            facts["task_status"] == "review"
            and str(facts["task_target_platform"] or "").casefold() == "h5"
            and facts["task_finalization_state"] == "session_draining"
            and facts["request_session_dependency"] == "active_session"
        )
        if (
            not facts["task_exists"]
            or (facts["task_status"] != "running" and not h5_review_session_draining)
            or facts["task_finalization_state"] not in _RESTORABLE_TASK_STAGES
            or facts["task_epoch"] != facts["request_epoch"]
        ):
            return (
                ATTEMPT_REJECTION_TASK_NOT_RUNNABLE,
                f"任务不可恢复：status={facts['task_status']} "
                f"finalization_state={facts['task_finalization_state']} "
                f"epoch={facts['task_epoch']}（请求 epoch={facts['request_epoch']}）",
            )
        if (
            not facts["capture_exists"]
            or facts["capture_task_id"] != plan.task_id
            or facts["capture_status"] != "captured"
            or not facts["capture_captured"]
        ):
            return (
                ATTEMPT_REJECTION_ORIGINAL_CAPTURE_INVALID,
                f"原 Capture {plan.original_capture_id} 不存在、不属于该任务或尚未采集成功",
            )
        if plan.replay_execution_plan_id is not None and (
            not facts["replay_plan_exists"] or facts["replay_plan_task_id"] != plan.task_id
        ):
            return (
                ATTEMPT_REJECTION_REPLAY_PLAN_MISSING,
                f"重放计划 {plan.replay_execution_plan_id} 不存在或不属于该任务",
            )
        if facts["attempt_exists"] and (
            facts["existing_plan_id"] != plan.plan_id
            or facts["existing_plan_hash"] != plan.plan_hash
            or facts["existing_request_id"] != plan.request_id
            or facts["existing_attempt_no"] != attempt_no
        ):
            return (
                ATTEMPT_REJECTION_ATTEMPT_IDENTITY_DRIFT,
                f"恢复尝试 {candidate_attempt_id} 已存在且计划不同，幂等身份发生漂移",
            )
        return None, _ATTEMPT_REJECTION_GENERIC_MESSAGE

    async def complete(
        self,
        *,
        attempt: RestorationAttempt,
        expected_row_version: int,
    ) -> RestorationAttempt:
        if attempt.status is RestorationAttemptStatus.RUNNING:
            raise ValueError("不能把运行中恢复尝试作为终态提交")
        if expected_row_version < 0:
            raise ValueError("expected_row_version 不能小于 0")
        if attempt.row_version != expected_row_version:
            raise ValueError("attempt.row_version 与 expected_row_version 不一致")
        assessment_payload = (
            attempt.assessment.to_dict() if attempt.assessment is not None else None
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE qc_restoration_attempt AS restoration
                SET
                    status = $2,
                    restored_capture_id = restored_capture.id,
                    equivalence_assessment_id = $4,
                    assessment_hash = $5,
                    assessment_payload = $6::jsonb,
                    action_execution_ids = $7::jsonb,
                    error_code = $8,
                    error_message = $9,
                    finished_at = $10,
                    row_version = restoration.row_version + 1,
                    updated_at = now()
                FROM qc_evidence_request AS request
                LEFT JOIN qc_capture AS restored_capture
                    ON restored_capture.id = $3::uuid
                   AND restored_capture.capture_status = 'captured'
                   AND restored_capture.captured_at IS NOT NULL
                WHERE restoration.attempt_id = $1
                  AND restoration.request_id = request.request_id
                  AND restoration.status = 'running'
                  AND restoration.row_version = $11
                  AND restoration.plan_id = $12
                  AND restoration.plan_hash = $13
                  AND request.task_id = restoration.task_id
                  AND (
                    $3::uuid IS NULL
                    OR (
                        -- 被更新的表不能出现在 FROM 的 JOIN 条件里（PostgreSQL 会拒绝准备
                        -- 语句），恢复 Capture 必须同任务的归属校验放在这里做
                        restored_capture.id IS NOT NULL
                        AND restored_capture.task_id = restoration.task_id
                    )
                  )
                  AND (
                    $6::jsonb IS NULL
                    OR (
                        $6::jsonb->>'restoration_attempt_id'
                            = restoration.attempt_id
                        AND $6::jsonb->>'request_id' = restoration.request_id
                        AND $6::jsonb->>'task_id' = restoration.task_id::text
                        AND $6::jsonb->>'original_capture_id'
                            = restoration.original_capture_id::text
                        AND $6::jsonb->>'restored_capture_id' = $3::text
                        AND $6::jsonb->>'source_grade'
                            = restoration.source_grade
                    )
                  )
                RETURNING
                    restoration.attempt_id,
                    restoration.plan_payload,
                    restoration.attempt_no,
                    restoration.status,
                    restoration.restored_capture_id::text AS restored_capture_id,
                    restoration.assessment_payload,
                    restoration.action_execution_ids,
                    restoration.error_code,
                    restoration.error_message,
                    restoration.started_at,
                    restoration.finished_at,
                    restoration.row_version,
                    restoration.attempt_protocol_version
                """,
                attempt.attempt_id,
                attempt.status.value,
                attempt.restored_capture_id,
                (attempt.assessment.assessment_id if attempt.assessment is not None else None),
                (
                    stable_protocol_hash(assessment_payload)
                    if assessment_payload is not None
                    else None
                ),
                (_json_dumps(assessment_payload) if assessment_payload is not None else None),
                _json_dumps(list(attempt.action_execution_ids)),
                attempt.error_code,
                attempt.error_message,
                attempt.finished_at,
                expected_row_version,
                attempt.plan.plan_id,
                attempt.plan.plan_hash,
            )
            if row is None:
                row = await connection.fetchrow(
                    """
                    SELECT
                        attempt_id,
                        plan_payload,
                        attempt_no,
                        status,
                        restored_capture_id::text AS restored_capture_id,
                        assessment_payload,
                        action_execution_ids,
                        error_code,
                        error_message,
                        started_at,
                        finished_at,
                        row_version,
                        attempt_protocol_version
                    FROM qc_restoration_attempt
                    WHERE attempt_id = $1
                    """,
                    attempt.attempt_id,
                )
        if row is None:
            raise RestorationAttemptPersistenceError("恢复尝试不存在或终态提交失败")
        persisted = _parse_attempt(row)
        if (
            persisted.status is not attempt.status
            or replace(
                persisted,
                row_version=attempt.row_version,
            )
            != attempt
        ):
            raise RestorationAttemptPersistenceError("恢复尝试状态、版本或状态等价证明已发生变化")
        return persisted

    async def get(self, *, attempt_id: str) -> RestorationAttempt | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    attempt_id,
                    plan_payload,
                    attempt_no,
                    status,
                    restored_capture_id::text AS restored_capture_id,
                    assessment_payload,
                    action_execution_ids,
                    error_code,
                    error_message,
                    started_at,
                    finished_at,
                    row_version,
                    attempt_protocol_version
                FROM qc_restoration_attempt
                WHERE attempt_id = $1
                """,
                attempt_id,
            )
        return _parse_attempt(row) if row is not None else None

    async def find_equivalent_for_lease(
        self,
        *,
        fence: EvidenceRequestLeaseFence,
    ) -> RestorationAttempt | None:
        """读取当前有效请求租约可复用的最新等价恢复结果。"""

        if not isinstance(fence, EvidenceRequestLeaseFence):
            raise TypeError("fence 必须为 EvidenceRequestLeaseFence")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    restoration.attempt_id,
                    restoration.plan_payload,
                    restoration.attempt_no,
                    restoration.status,
                    restoration.restored_capture_id::text AS restored_capture_id,
                    restoration.assessment_payload,
                    restoration.action_execution_ids,
                    restoration.error_code,
                    restoration.error_message,
                    restoration.started_at,
                    restoration.finished_at,
                    restoration.row_version,
                    restoration.attempt_protocol_version
                FROM qc_restoration_attempt AS restoration
                INNER JOIN qc_evidence_request AS request
                    ON request.request_id = restoration.request_id
                   AND request.task_id = restoration.task_id
                   AND request.status = 'pending'
                   AND request.finalization_epoch = restoration.finalization_epoch
                   AND request.expires_at > now()
                   AND request.lease_expires_at > now()
                INNER JOIN qc_task AS task
                    ON task.id = request.task_id
                   AND task.status = 'running'
                   AND task.finalization_state IN (
                       'open', 'exploration_draining', 'session_draining'
                   )
                   AND task.finalization_epoch = request.finalization_epoch
                INNER JOIN qc_capture AS restored_capture
                    ON restored_capture.id = restoration.restored_capture_id
                   AND restored_capture.task_id = restoration.task_id
                   AND restored_capture.capture_status = 'captured'
                   AND restored_capture.captured_at IS NOT NULL
                WHERE restoration.request_id = $1
                  AND restoration.task_id = $2::uuid
                  AND restoration.finalization_epoch = $3
                  AND restoration.status = 'equivalent'
                  AND restoration.assessment_payload->>'passed' = 'true'
                  AND NOT (lower(task.target_platform) = 'h5' AND EXISTS (
                      SELECT 1 FROM qc_v2_work_item AS stale_probe
                      WHERE stale_probe.task_id = task.id
                        AND stale_probe.evidence_request_id = request.request_id
                        AND stale_probe.finalization_epoch = restoration.finalization_epoch
                        AND stale_probe.work_payload->>'restoration_attempt_id'
                            = restoration.attempt_id
                        AND stale_probe.status = 'superseded'
                        AND stale_probe.last_error_code = 'H5_SHARED_PROBE_SOURCE_REPLACED'
                  ))
                  AND request.lease_owner = $4
                  AND request.lease_token = $5::uuid
                  AND request.row_version = $6
                ORDER BY restoration.attempt_no DESC, restoration.finished_at DESC
                LIMIT 1
                """,
                fence.request_id,
                fence.task_id,
                fence.finalization_epoch,
                fence.lease_owner,
                fence.lease_token,
                fence.row_version,
            )
        return _parse_attempt(row) if row is not None else None

    async def has_nonterminal(self, *, task_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM qc_restoration_attempt
                        WHERE task_id = $1::uuid
                          AND status = 'running'
                    )
                    """,
                    task_id,
                )
            )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _parse_attempt(row: Mapping[str, Any]) -> RestorationAttempt:
    assessment = row["assessment_payload"]
    payload = {
        "protocol_version": str(row["attempt_protocol_version"]),
        "attempt_id": str(row["attempt_id"]),
        "plan": dict(_as_mapping(row["plan_payload"], "plan_payload")),
        "attempt_no": int(row["attempt_no"]),
        "status": str(row["status"]),
        "restored_capture_id": row["restored_capture_id"],
        "assessment": (
            dict(_as_mapping(assessment, "assessment_payload")) if assessment is not None else None
        ),
        "action_execution_ids": list(
            _as_sequence(row["action_execution_ids"], "action_execution_ids")
        ),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "started_at": row["started_at"].isoformat(),
        "finished_at": (row["finished_at"].isoformat() if row["finished_at"] is not None else None),
        "state_version": int(row["row_version"]),
    }
    return RestorationAttempt.from_dict(payload)


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise RestorationAttemptPersistenceError(f"{field_name} 必须为对象")
    return value


def _as_sequence(value: Any, field_name: str) -> Sequence[Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RestorationAttemptPersistenceError(f"{field_name} 必须为数组")
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
