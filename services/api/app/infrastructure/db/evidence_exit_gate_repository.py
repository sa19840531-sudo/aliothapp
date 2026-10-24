"""Evidence Exit Requirement Snapshot 与离场预留的 PostgreSQL 实现。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from services.api.app.application.evidence_exit_gate import (
    EvidenceExitGateBlockedError,
    EvidenceExitRequirementRevisionConflict,
    PersistedEvidenceExitRequirementSet,
    build_evidence_exit_blocked_outcome,
    requires_evidence_exit_gate,
)
from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.exploration import (
    EvidenceExitBlockedOutcome,
    EvidenceExitGateDecision,
    EvidenceExitGateSnapshot,
    EvidenceExitRequirementSet,
    EvidenceExitRequirementStatus,
    EvidenceExitReservationStatus,
    build_evidence_exit_gate_decision,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    build_action_execution_id,
    build_action_intent_hash,
)
from services.api.app.domain.protocol import require_text, stable_protocol_hash
from services.api.app.infrastructure.db.pool import get_postgres_pool


class EvidenceExitGatePersistenceError(RuntimeError):
    """需求快照、Session Lease、离场事实或预留状态不满足约束。"""


class PostgresEvidenceExitRequirementRepository:
    """追加不可变需求收敛快照，以 revision 明确覆盖顺序。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def add(
        self,
        *,
        requirement_set: EvidenceExitRequirementSet,
    ) -> PersistedEvidenceExitRequirementSet:
        if not isinstance(requirement_set, EvidenceExitRequirementSet):
            raise TypeError("requirement_set 必须为 EvidenceExitRequirementSet")
        if requirement_set.protocol_version == "web-payload-requirement-set.v1":
            raise ValueError("WEB_PAYLOAD_REQUIRES_SCOPED_REPOSITORY")
        payload = requirement_set.to_dict()
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _lock_capture(
                connection,
                task_id=requirement_set.task_id,
                capture_id=requirement_set.capture_id,
            )
            previous_payload = await connection.fetchrow(
                """
                SELECT revision, requirement_payload
                FROM qc_evidence_exit_requirement_set
                WHERE task_id = $1::uuid
                  AND session_id = $2
                  AND capture_id = $3::uuid
                ORDER BY revision DESC
                LIMIT 1
                FOR UPDATE
                """,
                requirement_set.task_id,
                requirement_set.session_id,
                requirement_set.capture_id,
            )
            previous = (
                EvidenceExitRequirementSet.from_dict(
                    _as_mapping(
                        previous_payload["requirement_payload"],
                        "requirement_payload",
                    )
                )
                if previous_payload is not None
                else None
            )
            if getattr(previous, "protocol_version", None) == "web-payload-requirement-set.v1":
                raise ValueError("WEB_PAYLOAD_LEGACY_WRITE_REJECTED")
            if (
                previous is not None
                and requirement_set.requirement_set_id == previous.requirement_set_id
            ):
                return PersistedEvidenceExitRequirementSet(
                    requirement_set=previous,
                    created=False,
                )
            _validate_requirement_revision(previous=previous, current=requirement_set)
            await _validate_requirement_resolutions(
                connection,
                requirement_set=requirement_set,
            )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_evidence_exit_requirement_set AS existing (
                    requirement_set_id, revision, task_id, session_id, capture_id,
                    matcher_snapshot_hash, protocol_version, payload_hash,
                    requirement_count, nonterminal_count, requirement_payload,
                    created_at
                )
                SELECT
                    $1, $2, capture.task_id, $4::varchar, capture.id,
                    $6, $7, $8, $9, $10, $11::jsonb, $12
                FROM qc_capture AS capture
                LEFT JOIN qc_surface_session AS session
                    ON session.task_id = capture.task_id
                   AND session.session_id = capture.surface_session_id
                   AND session.status IN ('active', 'draining', 'closing')
                INNER JOIN qc_task AS task
                    ON task.id = capture.task_id
                   AND task.status = 'running'
                WHERE capture.id = $5::uuid
                  AND capture.task_id = $3::uuid
                  AND capture.capture_status = 'captured'
                  AND (
                    (
                      capture.surface_session_id IS NOT NULL
                      AND session.session_id = $4::varchar
                    )
                    OR (
                      capture.surface_session_id IS NULL
                      AND $4::varchar = ('agent-run:' || capture.agent_run_id::text)
                    )
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM qc_evidence_exit_requirement_set AS newer
                    WHERE newer.capture_id = capture.id
                      AND newer.revision > $2
                  )
                ON CONFLICT (capture_id, revision) DO UPDATE
                SET requirement_set_id = existing.requirement_set_id
                WHERE existing.requirement_set_id = EXCLUDED.requirement_set_id
                  AND existing.payload_hash = EXCLUDED.payload_hash
                RETURNING
                    existing.requirement_set_id,
                    (xmax = 0) AS created
                """,
                requirement_set.requirement_set_id,
                requirement_set.revision,
                requirement_set.task_id,
                requirement_set.session_id,
                requirement_set.capture_id,
                requirement_set.matcher_snapshot_hash,
                requirement_set.protocol_version,
                stable_protocol_hash(payload),
                len(requirement_set.requirements),
                requirement_set.nonterminal_count,
                _json_dumps(payload),
                requirement_set.created_at,
            )
        if row is None or str(row["requirement_set_id"]) != requirement_set.requirement_set_id:
            raise EvidenceExitGatePersistenceError(
                "需求快照归属无效、revision 已落后或同 revision 内容发生漂移"
            )
        return PersistedEvidenceExitRequirementSet(
            requirement_set=requirement_set,
            created=bool(row["created"]),
        )

    async def get_latest(
        self,
        *,
        task_id: str,
        session_id: str,
        capture_id: str,
    ) -> EvidenceExitRequirementSet | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                """
                SELECT requirement_payload
                FROM qc_evidence_exit_requirement_set
                WHERE task_id = $1::uuid
                  AND session_id = $2
                  AND capture_id = $3::uuid
                ORDER BY revision DESC
                LIMIT 1
                """,
                task_id,
                session_id,
                capture_id,
            )
        if payload is None:
            return None
        return EvidenceExitRequirementSet.from_dict(_as_mapping(payload, "requirement_payload"))

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


class PostgresEvidenceExitGate:
    """在 DB Session Lease 内计算四项合取式并创建短期离场预留。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        reservation_seconds: int = 120,
    ) -> None:
        if reservation_seconds < 15:
            raise ValueError("reservation_seconds 不能小于 15 秒")
        self._database_url = database_url
        self._pool = pool
        self._reservation_seconds = reservation_seconds

    async def reserve(
        self,
        *,
        intent: ActionIntent,
        permit: SessionCommandPermit,
    ) -> EvidenceExitGateDecision | None:
        return await self._reserve(
            intent=intent,
            permit=permit,
            decision_action_intent_id=intent.intent_id,
            ignored_action_execution_id=None,
            recovery_before_capture_id=None,
        )

    async def reserve_action_recovery(
        self,
        *,
        intent: ActionIntent,
        permit: SessionCommandPermit,
        action_execution_id: str,
        before_capture_id: str | None,
    ) -> EvidenceExitGateDecision | None:
        """为 planned App 动作重新计算门禁，并允许游标停在可信 Before Capture。"""

        require_text(action_execution_id, "action_execution_id")
        if before_capture_id is not None:
            require_text(before_capture_id, "before_capture_id")
        recovery_revision = stable_protocol_hash(
            {
                "command_sequence": permit.command_sequence,
                "finalization_epoch": permit.finalization_epoch,
            }
        ).removeprefix("sha256:")[:12]
        decision_action_intent_id = (
            f"app-recovery:{action_execution_id}:{recovery_revision}"
        )
        if len(decision_action_intent_id) > 128:
            raise EvidenceExitGatePersistenceError("App 动作恢复门禁审计身份超过上限")
        return await self._reserve(
            intent=intent,
            permit=permit,
            decision_action_intent_id=decision_action_intent_id,
            ignored_action_execution_id=action_execution_id,
            recovery_before_capture_id=before_capture_id,
        )

    async def finalize_stale_action_reservations(
        self,
        *,
        intent: ActionIntent,
        permit: SessionCommandPermit,
        action_execution_id: str,
        status: EvidenceExitReservationStatus,
    ) -> int:
        """按动作不可变身份终结原始及历次恢复预留。"""

        require_text(action_execution_id, "action_execution_id")
        _validate_permit(intent=intent, permit=permit)
        if action_execution_id != build_action_execution_id(intent):
            raise ValueError("旧 App 动作预留与 ActionIntent 身份不一致")
        if status not in {
            EvidenceExitReservationStatus.CONSUMED,
            EvidenceExitReservationStatus.CANCELLED,
        }:
            raise ValueError("旧 App 动作预留只能终结为 consumed 或 cancelled")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            count = await connection.fetchval(
                """
                WITH finalized AS (
                    UPDATE qc_evidence_exit_gate_evaluation AS evaluation
                    SET reservation_status = $6,
                        finalized_at = now(),
                        row_version = evaluation.row_version + 1,
                        updated_at = now()
                    FROM qc_action_execution AS action
                    WHERE action.action_execution_id = $1
                      AND action.task_id = $2::uuid
                      AND action.session_id = $3
                      AND action.intent_id = $4
                      AND action.intent_hash = $5
                      AND action.intent_payload->>'surface_kind' IN ('android', 'ios')
                      AND EXISTS (
                          SELECT 1
                          FROM qc_surface_session AS session
                          WHERE session.task_id = action.task_id
                            AND session.session_id = action.session_id
                            AND session.surface_kind = $7
                            AND session.lease_owner = $8
                            AND session.lease_token = $9::uuid
                            AND session.lease_expires_at > now()
                            AND session.command_sequence = $10
                            AND session.finalization_epoch = $11
                      )
                      AND evaluation.task_id = action.task_id
                      AND evaluation.session_id = action.session_id
                      AND (
                          evaluation.action_intent_id = action.intent_id
                          OR evaluation.action_intent_id LIKE
                              'app-recovery:' || action.action_execution_id || ':%'
                      )
                      AND evaluation.reservation_status = 'reserved'
                    RETURNING evaluation.evaluation_id
                )
                SELECT count(*) FROM finalized
                """,
                action_execution_id,
                intent.task_id,
                intent.session_id,
                intent.intent_id,
                build_action_intent_hash(intent),
                status.value,
                permit.surface_kind.value,
                permit.lease_owner,
                permit.lease_token,
                permit.command_sequence,
                permit.finalization_epoch,
            )
        return int(count or 0)

    async def _reserve(
        self,
        *,
        intent: ActionIntent,
        permit: SessionCommandPermit,
        decision_action_intent_id: str,
        ignored_action_execution_id: str | None,
        recovery_before_capture_id: str | None,
    ) -> EvidenceExitGateDecision | None:
        if not requires_evidence_exit_gate(intent):
            return None
        _validate_permit(intent=intent, permit=permit)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _lock_capture(
                connection,
                task_id=intent.task_id,
                capture_id=intent.source_capture_id,
            )
            if recovery_before_capture_id is not None:
                await _lock_capture(
                    connection,
                    task_id=intent.task_id,
                    capture_id=recovery_before_capture_id,
                )
            row = await connection.fetchrow(
                _EVALUATE_GATE_SQL,
                intent.task_id,
                intent.session_id,
                intent.source_capture_id,
                permit.surface_kind.value,
                permit.lease_owner,
                permit.lease_token,
                permit.command_sequence,
                permit.finalization_epoch,
                ignored_action_execution_id,
                recovery_before_capture_id,
            )
            if row is None:
                raise EvidenceExitGatePersistenceError(
                    "当前 Capture 不是 Session 最新事实，或 Session Lease/Finalization Epoch 已失效"
                )
            snapshot = _parse_gate_snapshot(row, surface_kind=permit.surface_kind.value)
            decision = build_evidence_exit_gate_decision(
                action_intent_id=decision_action_intent_id,
                snapshot=snapshot,
            )
            outcome = (
                None
                if decision.allow_leave
                else build_evidence_exit_blocked_outcome(decision=decision)
            )
            expires_at = (
                min(
                    intent.deadline,
                    snapshot.evaluated_at + timedelta(seconds=self._reservation_seconds),
                )
                if decision.allow_leave
                else None
            )
            if (
                decision.allow_leave
                and expires_at is not None
                and expires_at <= snapshot.evaluated_at
            ):
                raise EvidenceExitGatePersistenceError(
                    "ActionIntent deadline 已到，禁止创建离场预留"
                )
            inserted = await connection.fetchrow(
                """
                INSERT INTO qc_evidence_exit_gate_evaluation (
                    evaluation_id, task_id, session_id, source_capture_id,
                    action_intent_id, requirement_set_id,
                    session_command_sequence, finalization_epoch,
                    baseline_pass_manifest_count,
                    nonterminal_active_session_requirement_count,
                    nonterminal_inline_evidence_request_count,
                    evidence_commit_in_progress_count,
                    allow_leave, block_reasons, snapshot_payload, decision_payload,
                    manifest_id, manifest_version, manifest_hash,
                    manifest_quality_status, business_outcome, outcome_payload,
                    reservation_status, expires_at, evaluated_at, finalized_at
                )
                VALUES (
                    $1, $2::uuid, $3, $4::uuid, $5, $6, $7, $8,
                    $9, $10, $11, $12, $13, $14::jsonb, $15::jsonb, $16::jsonb,
                    $17::varchar, $18::integer, $19::varchar, $20::varchar,
                    $21::varchar, $22::jsonb,
                    -- asyncpg：$25 同时写入 evaluated_at 与 CASE(finalized_at)
                    $23::varchar, $24::timestamptz, $25::timestamptz,
                    CASE
                        WHEN $13::boolean THEN NULL::timestamptz
                        ELSE $25::timestamptz
                    END
                )
                ON CONFLICT (evaluation_id) DO NOTHING
                RETURNING
                    decision_payload,
                    outcome_payload,
                    manifest_id,
                    manifest_version,
                    manifest_hash,
                    manifest_quality_status,
                    business_outcome,
                    reservation_status,
                    expires_at,
                    (expires_at > now()) AS reservation_live
                """,
                decision.evaluation_id,
                intent.task_id,
                intent.session_id,
                intent.source_capture_id,
                decision_action_intent_id,
                snapshot.requirement_set_id,
                permit.command_sequence,
                permit.finalization_epoch,
                snapshot.baseline_pass_manifest_count,
                snapshot.nonterminal_active_session_requirement_count,
                snapshot.nonterminal_inline_evidence_request_count,
                snapshot.evidence_commit_in_progress_count,
                decision.allow_leave,
                _json_dumps([item.value for item in decision.block_reasons]),
                _json_dumps(snapshot.to_dict()),
                _json_dumps(decision.to_dict()),
                snapshot.latest_manifest_id,
                snapshot.latest_manifest_version,
                snapshot.latest_manifest_hash,
                snapshot.latest_manifest_quality_status,
                outcome.outcome.value if outcome is not None else None,
                _json_dumps(outcome.to_dict()) if outcome is not None else None,
                (
                    EvidenceExitReservationStatus.RESERVED.value
                    if decision.allow_leave
                    else EvidenceExitReservationStatus.BLOCKED.value
                ),
                expires_at,
                snapshot.evaluated_at,
            )
            if inserted is None:
                inserted = await connection.fetchrow(
                    """
                    SELECT
                        decision_payload,
                        outcome_payload,
                        manifest_id,
                        manifest_version,
                        manifest_hash,
                        manifest_quality_status,
                        business_outcome,
                        reservation_status,
                        expires_at,
                        (expires_at > now()) AS reservation_live
                    FROM qc_evidence_exit_gate_evaluation
                    WHERE evaluation_id = $1
                    """,
                    decision.evaluation_id,
                )
                if inserted is None:
                    raise EvidenceExitGatePersistenceError("离场门禁幂等记录读取失败")
                decision = EvidenceExitGateDecision.from_dict(
                    _as_mapping(inserted["decision_payload"], "decision_payload")
                )
            persisted_outcome = (
                EvidenceExitBlockedOutcome.from_dict(
                    _as_mapping(inserted["outcome_payload"], "outcome_payload")
                )
                if inserted["outcome_payload"] is not None
                else None
            )
            if decision.allow_leave != (persisted_outcome is None):
                raise EvidenceExitGatePersistenceError("门禁结论与业务结果持久化状态不一致")
            expected_outcome = (
                None
                if decision.allow_leave
                else build_evidence_exit_blocked_outcome(decision=decision)
            )
            if persisted_outcome != expected_outcome:
                raise EvidenceExitGatePersistenceError("门禁业务结果载荷与结论不一致")
            _validate_persisted_lineage_columns(
                row=inserted,
                decision=decision,
                outcome=persisted_outcome,
            )
            reservation_status = EvidenceExitReservationStatus(str(inserted["reservation_status"]))
            if (
                decision.allow_leave
                and reservation_status is not EvidenceExitReservationStatus.RESERVED
            ):
                raise EvidenceExitGatePersistenceError(
                    f"离场预留已进入 {reservation_status.value}，禁止重复执行动作"
                )
            if decision.allow_leave and not bool(inserted["reservation_live"]):
                raise EvidenceExitGatePersistenceError("离场预留已过期，禁止执行动作")
        if not decision.allow_leave:
            if persisted_outcome is None:
                raise EvidenceExitGatePersistenceError("阻断门禁缺少业务结果")
            raise EvidenceExitGateBlockedError(decision, persisted_outcome)
        return decision

    async def finalize(
        self,
        *,
        decision: EvidenceExitGateDecision,
        status: EvidenceExitReservationStatus,
    ) -> None:
        if not isinstance(decision, EvidenceExitGateDecision) or not decision.allow_leave:
            raise ValueError("只能终结已允许离场的门禁预留")
        if status not in {
            EvidenceExitReservationStatus.CONSUMED,
            EvidenceExitReservationStatus.CANCELLED,
        }:
            raise ValueError("门禁预留只能终结为 consumed 或 cancelled")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            persisted_status = await connection.fetchval(
                """
                UPDATE qc_evidence_exit_gate_evaluation AS evaluation
                SET
                    reservation_status = $2,
                    finalized_at = now(),
                    row_version = evaluation.row_version + 1,
                    updated_at = now()
                WHERE evaluation.evaluation_id = $1
                  AND evaluation.allow_leave
                  AND evaluation.reservation_status = 'reserved'
                RETURNING evaluation.reservation_status
                """,
                decision.evaluation_id,
                status.value,
            )
            if persisted_status is None:
                persisted_status = await connection.fetchval(
                    """
                    SELECT reservation_status
                    FROM qc_evidence_exit_gate_evaluation
                    WHERE evaluation_id = $1
                    """,
                    decision.evaluation_id,
                )
        if persisted_status != status.value:
            raise EvidenceExitGatePersistenceError("离场预留不存在、已过期或已被另一执行路径终结")

    async def expire_stale(self, *, limit: int = 100) -> int:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            result = await connection.fetchval(
                """
                WITH due AS (
                    SELECT evaluation_id
                    FROM qc_evidence_exit_gate_evaluation
                    WHERE reservation_status = 'reserved'
                      AND expires_at <= now()
                    ORDER BY expires_at, evaluation_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                ), updated AS (
                    UPDATE qc_evidence_exit_gate_evaluation AS evaluation
                    SET
                        reservation_status = 'expired',
                        finalized_at = now(),
                        row_version = evaluation.row_version + 1,
                        updated_at = now()
                    FROM due
                    WHERE evaluation.evaluation_id = due.evaluation_id
                    RETURNING 1
                )
                SELECT count(*) FROM updated
                """,
                limit,
            )
        return int(result or 0)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


_EVALUATE_GATE_SQL = """
    WITH target AS (
        SELECT
            session.task_id,
            session.session_id,
            capture.id AS capture_id
        FROM qc_surface_session AS session
        INNER JOIN qc_capture AS capture
            ON capture.id = $3::uuid
           AND capture.task_id = session.task_id
           AND capture.surface_session_id = session.session_id
           AND capture.capture_status = 'captured'
        INNER JOIN qc_task AS task
            ON task.id = session.task_id
           AND task.status = 'running'
        WHERE session.task_id = $1::uuid
          AND session.session_id = $2
          AND session.surface_kind = $4
          AND session.status = 'active'
          AND (
                session.current_capture_id = capture.id
                OR (
                    $10::uuid IS NOT NULL
                    AND session.current_capture_id = $10::uuid
                    AND EXISTS (
                        SELECT 1
                        FROM qc_capture AS recovery_before
                        WHERE recovery_before.id = $10::uuid
                          AND recovery_before.task_id = session.task_id
                          AND recovery_before.surface_session_id = session.session_id
                          AND recovery_before.surface_kind = session.surface_kind
                          AND recovery_before.capture_status = 'captured'
                          AND recovery_before.capture_role = 'before_action'
                          AND recovery_before.action_execution_id = $9
                          AND recovery_before.parent_capture_id = capture.id
                    )
                )
          )
          AND session.lease_owner = $5
          AND session.lease_token = $6::uuid
          AND session.lease_expires_at > now()
          AND session.command_sequence = $7
          AND session.finalization_epoch = $8
    ), latest_manifest AS (
        SELECT
            manifest.manifest_id,
            manifest.manifest_version,
            manifest.manifest_hash,
            manifest.quality_status,
            manifest.quality_reason,
            manifest.task_id,
            manifest.session_id,
            manifest.capture_id,
            manifest.manifest_payload
        FROM qc_evidence_manifest AS manifest
        INNER JOIN target
            ON target.task_id = manifest.task_id
           AND target.capture_id = manifest.capture_id
           AND target.session_id = manifest.session_id
        ORDER BY manifest.manifest_version DESC
        LIMIT 1
    ), latest_requirements AS (
        SELECT requirement.requirement_set_id, requirement.nonterminal_count
        FROM qc_evidence_exit_requirement_set AS requirement
        INNER JOIN target
            ON target.task_id = requirement.task_id
           AND target.session_id = requirement.session_id
           AND target.capture_id = requirement.capture_id
        ORDER BY requirement.revision DESC
        LIMIT 1
    ), manifest_integrity AS (
        SELECT
            CASE
                WHEN latest_manifest.manifest_id IS NULL THEN false
                WHEN jsonb_typeof(latest_manifest.manifest_payload) IS DISTINCT FROM 'object'
                  OR latest_manifest.manifest_payload->>'manifest_id'
                        IS DISTINCT FROM latest_manifest.manifest_id
                  OR latest_manifest.manifest_payload->>'manifest_version'
                        IS DISTINCT FROM latest_manifest.manifest_version::text
                  OR latest_manifest.manifest_payload->>'manifest_hash'
                        IS DISTINCT FROM latest_manifest.manifest_hash
                  OR latest_manifest.manifest_payload->>'task_id'
                        IS DISTINCT FROM latest_manifest.task_id::text
                  OR latest_manifest.manifest_payload->>'session_id'
                        IS DISTINCT FROM latest_manifest.session_id
                  OR latest_manifest.manifest_payload->>'capture_id'
                        IS DISTINCT FROM latest_manifest.capture_id::text
                  OR latest_manifest.manifest_payload->>'quality_status'
                        IS DISTINCT FROM latest_manifest.quality_status
                  OR latest_manifest.manifest_payload->>'quality_reason'
                        IS DISTINCT FROM latest_manifest.quality_reason
                  OR CASE
                        WHEN jsonb_typeof(
                            latest_manifest.manifest_payload->'artifacts'
                        ) = 'array'
                        THEN jsonb_array_length(
                            latest_manifest.manifest_payload->'artifacts'
                        ) = 0
                        ELSE true
                    END
                  OR (
                        SELECT count(*) <> count(
                            DISTINCT payload_artifact->>'artifact_id'
                        )
                        FROM jsonb_array_elements(
                            CASE
                                WHEN jsonb_typeof(
                                    latest_manifest.manifest_payload->'artifacts'
                                ) = 'array'
                                THEN latest_manifest.manifest_payload->'artifacts'
                                ELSE '[]'::jsonb
                            END
                        ) AS payload_artifact
                    )
                  OR EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(
                            CASE
                                WHEN jsonb_typeof(
                                    latest_manifest.manifest_payload->'artifacts'
                                ) = 'array'
                                THEN latest_manifest.manifest_payload->'artifacts'
                                ELSE '[]'::jsonb
                            END
                        ) AS payload_artifact
                        LEFT JOIN qc_evidence_artifact AS artifact
                            ON artifact.id::text = payload_artifact->>'artifact_id'
                        WHERE artifact.id IS NULL
                           OR artifact.task_id <> target.task_id
                           OR artifact.page_capture_id <> target.capture_id
                           OR artifact.artifact_type IS DISTINCT FROM (
                                payload_artifact->>'artifact_type'
                           )
                           OR artifact.storage_path IS DISTINCT FROM (
                                payload_artifact->>'storage_path'
                           )
                           OR artifact.checksum IS DISTINCT FROM (
                                payload_artifact->>'checksum'
                           )
                           OR artifact.size_bytes::text IS DISTINCT FROM (
                                payload_artifact->>'size_bytes'
                           )
                    )
                THEN true
                ELSE false
            END AS lineage_integrity_failed
        FROM target
        LEFT JOIN latest_manifest ON TRUE
    )
    SELECT
        target.task_id::text AS task_id,
        target.session_id,
        target.capture_id::text AS source_capture_id,
        latest_requirements.requirement_set_id,
        latest_manifest.manifest_id,
        latest_manifest.manifest_version,
        latest_manifest.manifest_hash,
        latest_manifest.quality_status AS manifest_quality_status,
        latest_manifest.quality_reason AS manifest_quality_reason,
        manifest_integrity.lineage_integrity_failed,
        CASE
            WHEN latest_manifest.quality_status = 'pass'
             AND NOT manifest_integrity.lineage_integrity_failed
            THEN 1 ELSE 0
        END::integer
            AS baseline_pass_manifest_count,
        COALESCE(
            (
                SELECT count(*)::integer
                FROM qc_evidence_manifest AS all_manifest
                INNER JOIN target
                    ON target.task_id = all_manifest.task_id
                   AND target.capture_id = all_manifest.capture_id
                   AND target.session_id = all_manifest.session_id
            ),
            0
        ) AS baseline_manifest_total_count,
        COALESCE(latest_requirements.nonterminal_count, 0)::integer
            AS nonterminal_active_session_requirement_count,
        (
            SELECT count(*)
            FROM qc_evidence_request AS request
            WHERE request.task_id = target.task_id
              AND request.target_capture_id = target.capture_id
              AND request.session_dependency = 'active_session'
              AND request.status IN ('pending', 'routed', 'running')
        )::integer AS nonterminal_inline_evidence_request_count,
        (
            (
                SELECT count(*)
                FROM qc_action_execution AS action
                WHERE action.task_id = target.task_id
                  AND action.session_id = target.session_id
                  AND action.status IN ('planned', 'executing', 'evidence_pending')
                  AND ($9::varchar IS NULL OR action.action_execution_id <> $9)
            )
            + CASE WHEN latest_manifest.quality_status = 'pending' THEN 1 ELSE 0 END
            + (
                SELECT count(*)
                FROM qc_observation_packet AS packet
                WHERE packet.task_id = target.task_id
                  AND packet.page_capture_id = target.capture_id
                  AND packet.quality_gate_status = 'pending'
            )
        )::integer AS evidence_commit_in_progress_count,
        now() AS evaluated_at
    FROM target
    LEFT JOIN latest_manifest ON TRUE
    LEFT JOIN latest_requirements ON TRUE
    LEFT JOIN manifest_integrity ON TRUE
"""


async def _validate_requirement_resolutions(
    connection: Any,
    *,
    requirement_set: EvidenceExitRequirementSet,
) -> None:
    for requirement in requirement_set.requirements:
        request_status: str | None = None
        request_manifest_id: str | None = None
        if requirement.evidence_request_id is not None:
            request_row = await connection.fetchrow(
                """
                SELECT status, resolution_manifest_id
                FROM qc_evidence_request
                WHERE request_id = $1
                  AND task_id = $2::uuid
                  AND target_capture_id = $3::uuid
                  AND rule_id = $4::uuid
                  AND rule_code = $5
                  AND session_dependency = 'active_session'
                """,
                requirement.evidence_request_id,
                requirement_set.task_id,
                requirement_set.capture_id,
                requirement.rule_id,
                requirement.rule_code,
            )
            if request_row is None:
                raise EvidenceExitGatePersistenceError(
                    f"需求 {requirement.requirement_id} 的 Evidence Request 归属无效"
                )
            request_status = str(request_row["status"])
            request_manifest_id = (
                str(request_row["resolution_manifest_id"])
                if request_row["resolution_manifest_id"] is not None
                else None
            )
        _validate_request_status(
            requirement_status=requirement.status,
            request_status=request_status,
        )
        if requirement.status is not EvidenceExitRequirementStatus.SATISFIED:
            continue
        manifest_payload = await connection.fetchval(
            """
            SELECT manifest.manifest_payload
            FROM qc_evidence_manifest AS manifest
            WHERE manifest.manifest_id = $1
              AND manifest.task_id = $2::uuid
              AND manifest.quality_status = 'pass'
              AND (
                manifest.capture_id = $3::uuid
                OR EXISTS (
                    SELECT 1
                    FROM qc_evidence_supplement AS supplement
                    INNER JOIN qc_evidence_request AS request
                        ON request.request_id = supplement.request_id
                    WHERE supplement.manifest_id = manifest.manifest_id
                      AND request.task_id = $2::uuid
                      AND request.target_capture_id = $3::uuid
                      AND request.rule_id = $4::uuid
                )
              )
            """,
            requirement.resolution_manifest_id,
            requirement_set.task_id,
            requirement_set.capture_id,
            requirement.rule_id,
        )
        if manifest_payload is None:
            raise EvidenceExitGatePersistenceError(
                f"需求 {requirement.requirement_id} 的 Manifest 不属于当前 Capture 或未通过质量门禁"
            )
        if (
            requirement.evidence_request_id is not None
            and request_manifest_id != requirement.resolution_manifest_id
        ):
            raise EvidenceExitGatePersistenceError("Evidence Request 与需求快照的 Manifest 不一致")
        manifest = EvidenceManifestReference.from_dict(
            _as_mapping(manifest_payload, "manifest_payload")
        )
        artifact_types = {item.artifact_type for item in manifest.artifacts}
        capture_roles = {item.role for item in manifest.artifacts}
        missing_artifacts = sorted(
            set(requirement.required_artifact_types).difference(artifact_types)
        )
        missing_roles = sorted(set(requirement.required_capture_roles).difference(capture_roles))
        if missing_artifacts or missing_roles:
            raise EvidenceExitGatePersistenceError(
                "质量通过的 Manifest 仍缺少需求声明的 Artifact/Role："
                + ", ".join([*missing_artifacts, *missing_roles])
            )


def _validate_request_status(
    *,
    requirement_status: EvidenceExitRequirementStatus,
    request_status: str | None,
) -> None:
    if request_status is None:
        return
    expected = {
        EvidenceExitRequirementStatus.PENDING: {"pending", "routed", "running"},
        EvidenceExitRequirementStatus.SATISFIED: {"satisfied"},
        EvidenceExitRequirementStatus.EVIDENCE_INSUFFICIENT: {
            "unfulfillable",
            "expired",
            "cancelled",
        },
        EvidenceExitRequirementStatus.MANUAL_REVIEW: {
            "unfulfillable",
            "expired",
            "cancelled",
        },
        EvidenceExitRequirementStatus.SKIPPED: {
            "unfulfillable",
            "expired",
            "cancelled",
        },
    }[requirement_status]
    if request_status not in expected:
        raise EvidenceExitGatePersistenceError(
            f"需求状态 {requirement_status.value} 与 Evidence Request 状态 {request_status} 不一致"
        )


def _validate_requirement_revision(
    *,
    previous: EvidenceExitRequirementSet | None,
    current: EvidenceExitRequirementSet,
) -> None:
    """保证需求成员只追加、pending 只向终态前进，不能靠删成员绕过门禁。"""
    if previous is None:
        if current.revision != 1:
            raise EvidenceExitGatePersistenceError("首个需求快照 revision 必须为 1")
        return
    if current.revision <= previous.revision:
        raise EvidenceExitRequirementRevisionConflict(
            '需求快照 revision 已被并发发布者推进'
        )
    if current.revision != previous.revision + 1:
        raise EvidenceExitGatePersistenceError("需求快照 revision 必须连续递增")
    current_members = {item.requirement_id: item for item in current.requirements}
    for old in previous.requirements:
        new = current_members.get(old.requirement_id)
        if new is None:
            raise EvidenceExitGatePersistenceError(
                f"需求 {old.requirement_id} 不能从后续快照中删除"
            )
        if (
            new.rule_id != old.rule_id
            or new.rule_code != old.rule_code
            or new.required_artifact_types != old.required_artifact_types
            or new.required_capture_roles != old.required_capture_roles
            or new.min_screenshot_count != old.min_screenshot_count
            or new.allow_empty_visible_text != old.allow_empty_visible_text
        ):
            raise EvidenceExitGatePersistenceError(
                f"需求 {old.requirement_id} 的规则或证据约束不能漂移"
            )
        if (
            old.evidence_request_id is not None
            and new.evidence_request_id != old.evidence_request_id
        ):
            raise EvidenceExitGatePersistenceError(
                f"需求 {old.requirement_id} 不能更换 Evidence Request"
            )
        if old.status.is_terminal and new != old:
            raise EvidenceExitGatePersistenceError(
                f"需求 {old.requirement_id} 已终态，后续快照不能回退或改写结果"
            )


async def _lock_capture(connection: Any, *, task_id: str, capture_id: str) -> None:
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        _capture_lock_key(task_id=task_id, capture_id=capture_id),
    )


def _capture_lock_key(*, task_id: str, capture_id: str) -> str:
    return f"evidence-exit:{task_id}:{capture_id}"


def _validate_permit(*, intent: ActionIntent, permit: SessionCommandPermit) -> None:
    if not isinstance(intent, ActionIntent):
        raise TypeError("intent 必须为 ActionIntent")
    if not isinstance(permit, SessionCommandPermit):
        raise TypeError("permit 必须为 SessionCommandPermit")
    if (
        permit.task_id != intent.task_id
        or permit.session_id != intent.session_id
        or permit.surface_kind is not intent.surface_kind
    ):
        raise ValueError("Session 命令许可与离场 ActionIntent 不一致")


def _parse_gate_snapshot(
    row: Mapping[str, Any],
    *,
    surface_kind: str | None = None,
) -> EvidenceExitGateSnapshot:
    return EvidenceExitGateSnapshot(
        task_id=str(row["task_id"]),
        session_id=str(row["session_id"]),
        source_capture_id=str(row["source_capture_id"]),
        requirement_set_id=(
            str(row["requirement_set_id"]) if row["requirement_set_id"] is not None else None
        ),
        baseline_pass_manifest_count=int(row["baseline_pass_manifest_count"]),
        baseline_manifest_total_count=int(
            row["baseline_manifest_total_count"]
            if row.get("baseline_manifest_total_count") is not None
            else 0
        ),
        nonterminal_active_session_requirement_count=int(
            row["nonterminal_active_session_requirement_count"]
        ),
        nonterminal_inline_evidence_request_count=int(
            row["nonterminal_inline_evidence_request_count"]
        ),
        evidence_commit_in_progress_count=int(row["evidence_commit_in_progress_count"]),
        evaluated_at=row["evaluated_at"],
        surface_kind=surface_kind,
        latest_manifest_id=(
            str(row["manifest_id"]) if row["manifest_id"] is not None else None
        ),
        latest_manifest_version=(
            int(row["manifest_version"]) if row["manifest_version"] is not None else None
        ),
        latest_manifest_hash=(
            str(row["manifest_hash"]) if row["manifest_hash"] is not None else None
        ),
        latest_manifest_quality_status=(
            str(row["manifest_quality_status"])
            if row["manifest_quality_status"] is not None
            else None
        ),
        latest_manifest_quality_reason=(
            str(row["manifest_quality_reason"])
            if row["manifest_quality_reason"] is not None
            else None
        ),
        lineage_integrity_failed=bool(row["lineage_integrity_failed"]),
    )


def _validate_persisted_lineage_columns(
    *,
    row: Mapping[str, Any],
    decision: EvidenceExitGateDecision,
    outcome: EvidenceExitBlockedOutcome | None,
) -> None:
    snapshot = decision.snapshot
    persisted_lineage = (
        str(row["manifest_id"]) if row["manifest_id"] is not None else None,
        int(row["manifest_version"]) if row["manifest_version"] is not None else None,
        str(row["manifest_hash"]) if row["manifest_hash"] is not None else None,
        (
            str(row["manifest_quality_status"])
            if row["manifest_quality_status"] is not None
            else None
        ),
    )
    expected_lineage = (
        snapshot.latest_manifest_id,
        snapshot.latest_manifest_version,
        snapshot.latest_manifest_hash,
        snapshot.latest_manifest_quality_status,
    )
    if persisted_lineage != expected_lineage:
        raise EvidenceExitGatePersistenceError("门禁 Manifest 列与结论载荷不一致")
    persisted_business_outcome = (
        str(row["business_outcome"]) if row["business_outcome"] is not None else None
    )
    expected_business_outcome = outcome.outcome.value if outcome is not None else None
    if persisted_business_outcome != expected_business_outcome:
        raise EvidenceExitGatePersistenceError("门禁业务结果列与结论载荷不一致")


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise EvidenceExitGatePersistenceError(f"{field_name} 必须为对象")
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
