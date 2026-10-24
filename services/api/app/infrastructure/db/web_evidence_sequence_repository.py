"""由真实 readiness 出口登记有界 Web 序列，复用原 Publication/Outbox。"""

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from services.api.app.application.web_readiness_contracts import read_web_readiness_assessment
from services.api.app.application.web_rule_evidence_contract import WebRuleEvidenceContract
from services.api.app.application.web_run_policy import materialize_web_policy
from services.api.app.application.web_sequence_capture_integrity import (
    web_sequence_capture_integrity,
)
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.evidence.presentation import PresentationAssessment
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    read_web_policy_on_connection,
)
from services.api.app.infrastructure.db.web_sequence_binding_reader import _mapping


class PostgresWebEvidenceSequenceRepository:
    """仅创建序列及原队列公告，不创建分类、逻辑主页面或新发布队列。"""

    def __init__(self, *, pool=None, database_url=None):
        self._pool = pool
        self._database_url = database_url

    async def capture_ids_for_failure(
        self, *, task_id, agent_run_id, session_id, evidence_capture_ids
    ):
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            rows = await self._capture_rows(
                connection, task_id, agent_run_id, session_id, evidence_capture_ids
            )
        if not set(evidence_capture_ids).issubset({str(row["id"]) for row in rows}):
            return ()
        return tuple(str(row["id"]) for row in rows)

    async def wake_after_classification(self, **scope):
        from services.api.app.infrastructure.db.web_sequence_classification_wait import (
            wake_classified_sequences,
        )

        pool = self._pool or await get_postgres_pool(self._database_url)
        await wake_classified_sequences(PostgresH5InspectionPublicationRepository(pool=pool), **scope)

    async def register_failure(
        self,
        *,
        task_id,
        agent_run_id,
        session_id,
        source_capture_id,
        evidence_capture_ids,
        page_role,
        readiness_status,
    ):
        return await self._register(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            source_capture_id=source_capture_id,
            evidence_capture_ids=evidence_capture_ids,
            page_role=page_role,
            readiness_status=readiness_status,
            purpose="failure_sequence",
        )

    async def register_interaction(
        self, *, interaction_outcomes, interaction_action_refs, **kwargs
    ):
        return await self._register(
            **kwargs,
            purpose="interaction_sequence",
            interaction_outcomes=interaction_outcomes,
            interaction_action_refs=interaction_action_refs,
        )

    async def _register(
        self,
        *,
        task_id,
        agent_run_id,
        session_id,
        source_capture_id,
        evidence_capture_ids,
        page_role,
        readiness_status,
        purpose,
        interaction_outcomes=(),
        interaction_action_refs=(),
    ):
        if not 2 <= len(set(evidence_capture_ids)) <= 64:
            return {
                "status": "evidence_insufficient",
                "reason_code": "WEB_SEQUENCE_SAMPLES_INSUFFICIENT",
            }
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task_id}"
            )
            scope = await connection.fetchrow(
                """
                SELECT task.finalization_epoch, task.ruleset_id::text, ruleset.version, ruleset.snapshot_hash
                FROM qc_task task JOIN qc_agent_run run ON run.task_id=task.id AND run.id=$2::uuid
                JOIN qc_ruleset ruleset ON ruleset.id=task.ruleset_id
                JOIN qc_surface_session session ON session.task_id=task.id AND session.session_id=$3
                WHERE task.id=$1::uuid AND task.target_platform='web' AND task.status='running'
                    AND task.task_config->>'workflow_version'='v2'
                    AND task.task_config->>'execution_mode'='full_inspection'
                    AND task.task_config->>'web_inspection_notification_mode'='enforced'
                    AND task.finalization_state IN ('open','exploration_draining','session_draining')
                    AND run.agent_type='main' AND run.status IN ('running','done','failed','aborted')
                    AND COALESCE(run.output_context->>'cancel_requested','false')='false'
                    AND run.id=(SELECT latest.id FROM qc_agent_run latest WHERE latest.task_id=task.id
                        AND latest.agent_type='main' ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
                    AND session.session_id='web:'||run.id::text AND session.session_owner='web-v2-workflow'
                    AND session.surface_kind='web' AND session.status IN ('active','draining')
                    AND session.finalization_epoch=task.finalization_epoch
                FOR UPDATE OF task,run,session
                """,
                task_id,
                agent_run_id,
                session_id,
            )
            if scope is None:
                return {
                    "status": "evidence_insufficient",
                    "reason_code": "WEB_SEQUENCE_SCOPE_INVALID",
                }
            snapshot = await read_web_policy_on_connection(connection, task_id, agent_run_id)
            if snapshot is None:
                return {
                    "status": "evidence_insufficient",
                    "reason_code": "WEB_POLICY_HISTORY_MISSING",
                }
            policy = materialize_web_policy(snapshot)
            rows = await self._capture_rows(
                connection, task_id, agent_run_id, session_id, evidence_capture_ids
            )
            requested = set(evidence_capture_ids)
            if not requested.issubset({str(row["id"]) for row in rows}):
                return {
                    "status": "evidence_insufficient",
                    "reason_code": "WEB_SEQUENCE_CAPTURE_SET_INCOMPLETE",
                }
            afters = [r for r in rows if r["capture_role"] == "after_action"]
            # 首页 bootstrap 没有更早父源：它是后续自然动作的源身份，不是序列动作样本。
            # 仅剔除这一对无前驱 bootstrap；后续 close/observe 仍逐对核验真实 Action 闭包。
            if (afters and source_capture_id == str(afters[0]["id"])
                and rows[0]["capture_role"] == "before_action" and rows[0]["parent_capture_id"] is None
                and rows[0]["action_execution_id"] == afters[0]["action_execution_id"]):
                if not await _trusted_bootstrap_pair(connection, task_id, agent_run_id, session_id,
                    scope["finalization_epoch"], rows[0], afters[0]):
                    return {"status": "evidence_insufficient", "reason_code": "WEB_SEQUENCE_SOURCE_CHAIN_INVALID"}
                bootstrap_action = afters[0]["action_execution_id"]
                rows = [row for row in rows if row["action_execution_id"] != bootstrap_action]
                afters = [row for row in rows if row["capture_role"] == "after_action"]
            if len(afters) < 2:
                return {
                    "status": "evidence_insufficient",
                    "reason_code": "WEB_SEQUENCE_SAMPLES_INSUFFICIENT",
                }
            action_source_id = (
                str(rows[0]["parent_capture_id"])
                if source_capture_id == str(afters[0]["id"])
                else source_capture_id
            )
            if action_source_id in {"", "None"}:
                return {"status": "evidence_insufficient", "reason_code": "WEB_SEQUENCE_SOURCE_CHAIN_INVALID"}
            source_valid = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM qc_capture source JOIN qc_action_execution action "
                "ON action.task_id=source.task_id AND action.session_id=source.surface_session_id "
                "WHERE source.id=$1::uuid AND source.task_id=$2::uuid AND source.agent_run_id=$3::uuid "
                "AND source.surface_session_id=$4 AND source.capture_status='captured' "
                "AND action.action_execution_id=$5 AND action.requested_source_capture_id=source.id "
                "AND action.source_capture_id=$6::uuid AND action.result_capture_id=$7::uuid "
                "AND action.finalization_epoch=$8 AND action.status='completed')",
                action_source_id,
                task_id,
                agent_run_id,
                session_id,
                afters[0]["action_execution_id"],
                str(afters[0]["parent_capture_id"]),
                str(afters[0]["id"]),
                scope["finalization_epoch"],
            )
            if not source_valid or str(rows[0]["parent_capture_id"]) != action_source_id:
                return {
                    "status": "evidence_insufficient",
                    "reason_code": "WEB_SEQUENCE_SOURCE_CHAIN_INVALID",
                }
            target = stable_protocol_hash(
                {
                    "logical_page_id": policy.identity_policy.logical_id(afters[-1]["url"]),
                    "identity_policy_hash": snapshot.payload_sha256,
                }
            )
            occurrence = stable_protocol_hash(
                {
                    "task_id": task_id,
                    "agent_run_id": agent_run_id,
                    "source_capture_id": source_capture_id,
                    "page_role": page_role,
                    "target_id": target,
                }
            )
            identity = {
                "task_id": task_id,
                "agent_run_id": agent_run_id,
                "session_id": session_id,
                "finalization_epoch": scope["finalization_epoch"],
            }
            frames = []
            for row in rows:
                metadata = _mapping(row["metadata"])
                try:
                    integrity_hash = web_sequence_capture_integrity(
                        metadata, capture_role=row["capture_role"], profile=policy.readiness_profile
                    )
                except ValueError:
                    return {
                        "status": "evidence_insufficient",
                        "reason_code": "WEB_SEQUENCE_CAPTURE_INCONSISTENT",
                    }
                presentation = PresentationAssessment.from_dict(metadata.get("presentation"))
                manifest_row = await connection.fetchrow(
                    "SELECT manifest_payload FROM qc_evidence_manifest WHERE task_id=$1::uuid AND capture_id=$2::uuid "
                    "AND quality_status='pass' ORDER BY manifest_version DESC LIMIT 1",
                    task_id,
                    str(row["id"]),
                )
                if presentation is None or presentation.is_diagnostic or manifest_row is None:
                    return {
                        "status": "evidence_insufficient",
                        "reason_code": "WEB_SEQUENCE_SOURCE_UNTRUSTED",
                    }
                manifest = EvidenceManifestReference.from_dict(
                    _mapping(manifest_row["manifest_payload"])
                )
                readiness = read_web_readiness_assessment(
                    metadata.get("pipeline_metrics", {}), profile=policy.readiness_profile
                )
                frame_target = (
                    target
                    if row["capture_role"] == "before_action"
                    else stable_protocol_hash(
                        {
                            "logical_page_id": policy.identity_policy.logical_id(row["url"]),
                            "identity_policy_hash": snapshot.payload_sha256,
                        }
                    )
                )
                frames.append(
                    {
                        **identity,
                        "capture_id": str(row["id"]),
                        "capture_role": row["capture_role"],
                        "parent_capture_id": str(row["parent_capture_id"])
                        if row["parent_capture_id"]
                        else None,
                        "action_execution_id": row["action_execution_id"],
                        "captured_at": row["captured_at"].isoformat(),
                        "target_id": frame_target,
                        "occurrence_id": occurrence,
                        "evidence_mode": "natural",
                        "page_state": readiness.status.value if readiness is not None else None,
                        "capture_consistency_hash": integrity_hash,
                        "artifact_types": [a.artifact_type for a in manifest.artifacts],
                        "artifact_ids": [a.artifact_id for a in manifest.artifacts],
                        "artifacts_verified": True,
                    }
                )
            measured_status = frames[-1]["page_state"]
            if (
                purpose == "failure_sequence"
                and (
                    str(readiness_status) not in {measured_status, "timeout"}
                    or measured_status == "stable"
                )
            ) or (
                purpose == "interaction_sequence"
                and (measured_status != "stable" or str(readiness_status) != "stable")
            ):
                return {
                    "status": "evidence_insufficient",
                    "reason_code": "WEB_SEQUENCE_TERMINAL_FACT_MISMATCH",
                }
            sequence = {
                **identity,
                "target_platform": "web",
                "workflow_version": "v2",
                "session_owner": "web-v2-workflow",
                "target_id": target,
                "occurrence_id": occurrence,
                "purpose": purpose,
                "readiness_status": measured_status,
                "readiness_exit_reason": str(readiness_status),
                "frames": frames,
                "policy_hash": snapshot.payload_sha256,
            }
            from services.api.app.infrastructure.db.web_capability_revision_repository import (
                read_capture_revision_on_connection,
            )

            revision = await read_capture_revision_on_connection(
                connection,
                task_id=task_id,
                agent_run_id=agent_run_id,
                finalization_epoch=scope["finalization_epoch"],
                capture_id=str(afters[-1]["id"]),
            )
            if revision is not None:
                if revision["target_id"] != target:
                    raise ValueError("WEB_CAPABILITY_TARGET_DRIFT")
                sequence["capability_revision"] = revision
            if purpose == "interaction_sequence":
                from services.api.app.infrastructure.db.web_interaction_sequence_facts import (
                    authenticate_interaction_facts,
                )

                try:
                    sequence["interaction_outcomes"] = await authenticate_interaction_facts(
                        connection,
                        sequence=sequence,
                        outcomes=interaction_outcomes,
                        action_refs=interaction_action_refs,
                    )
                except ValueError as exc:
                    return {"status": "evidence_insufficient", "reason_code": str(exc)}
            validation = WebRuleEvidenceContract(
                rule_version="source.v1",
                accepted_roles=(purpose,),
                required_artifact_types=("screenshot", "dom"),
                minimum_samples=2,
                minimum_duration_ms=0,
                requires_action=True,
                accept_unclassified=purpose == "failure_sequence",
            ).validate_sequence(
                sequence,
                task_id=task_id,
                agent_run_id=agent_run_id,
                finalization_epoch=scope["finalization_epoch"],
            )
            if validation.status != "admissible":
                return {"status": validation.status, "reason_code": validation.reason_code}
            sequence_hash = stable_protocol_hash(sequence)
            sequence_id = str(uuid5(NAMESPACE_URL, "alioth:web-sequence:" + sequence_hash))
            anchor = str(afters[-1]["id"])
            await connection.execute(
                "INSERT INTO qc_web_evidence_sequence(sequence_id,task_id,source_agent_run_id,session_id,finalization_epoch,"
                "anchor_capture_id,target_id,occurrence_id,purpose,readiness_status,sequence_hash,sequence_payload) "
                "VALUES($1::uuid,$2::uuid,$3::uuid,$4,$5,$6::uuid,$7,$8,$12,$9,$10,$11::jsonb) "
                "ON CONFLICT(sequence_id) DO NOTHING",
                sequence_id,
                task_id,
                agent_run_id,
                session_id,
                scope["finalization_epoch"],
                anchor,
                target,
                occurrence,
                measured_status,
                sequence_hash,
                json.dumps(sequence, ensure_ascii=False),
                purpose,
            )
            for index, (row, frame) in enumerate(zip(rows, frames, strict=True), start=1):
                await connection.execute(
                    "INSERT INTO qc_web_evidence_sequence_member(sequence_id,ordinal,task_id,capture_id,captured_at,frame_payload) "
                    "VALUES($1::uuid,$2,$3::uuid,$4::uuid,$5,$6::jsonb) ON CONFLICT(sequence_id,ordinal) DO NOTHING",
                    sequence_id,
                    index,
                    task_id,
                    str(row["id"]),
                    row["captured_at"],
                    json.dumps(frame, ensure_ascii=False),
                )
            publication_id = str(
                uuid5(NAMESPACE_URL, "alioth:web-sequence-publication:" + sequence_id)
            )
            input_hash = stable_protocol_hash(
                {
                    "sequence_id": sequence_id,
                    "sequence_hash": sequence_hash,
                    "ruleset_id": scope["ruleset_id"],
                    "ruleset_version": scope["version"],
                    "snapshot_hash": scope["snapshot_hash"],
                }
            )
            row = await connection.fetchrow(
                "INSERT INTO qc_h5_inspection_publication(id,task_id,source_agent_run_id,page_capture_id,"
                "ruleset_id,ruleset_version,ruleset_snapshot_hash,finalization_epoch,input_hash,mode,web_evidence_sequence_id) "
                "VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,$6,$7,$8,$9,'enforced',$10::uuid) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=qc_h5_inspection_publication.updated_at RETURNING row_version,(xmax=0) AS created",
                publication_id,
                task_id,
                agent_run_id,
                anchor,
                scope["ruleset_id"],
                scope["version"],
                scope["snapshot_hash"],
                scope["finalization_epoch"],
                input_hash,
                sequence_id,
            )
            publication = PostgresH5InspectionPublicationRepository(pool=pool)
            await publication._insert_available_event(
                connection,
                publication_id=publication_id,
                task_id=task_id,
                finalization_epoch=scope["finalization_epoch"],
                row_version=row["row_version"],
                occurred_at=datetime.now(UTC),
            )
            return {
                "status": "published",
                "publication_id": publication_id,
                "sequence_id": sequence_id,
                "created": row["created"],
                "reason_code": "WEB_SEQUENCE_PUBLICATION_RECORDED",
            }

    async def _capture_rows(
        self, connection, task_id, agent_run_id, session_id, evidence_capture_ids
    ):
        if not evidence_capture_ids or len(evidence_capture_ids) > 64:
            return []
        return await connection.fetch(
            """
            SELECT capture.id, capture.capture_role, capture.parent_capture_id, capture.action_execution_id,
                   capture.captured_at, capture.metadata, COALESCE(page.url,capture.metadata->>'url') AS url
            FROM qc_capture capture LEFT JOIN qc_page_capture page ON page.id=capture.id
            JOIN qc_task task ON task.id=capture.task_id AND task.target_platform='web'
                AND task.task_config->>'workflow_version'='v2'
                AND task.task_config->>'execution_mode'='full_inspection'
                AND task.task_config->>'web_inspection_notification_mode'='enforced'
                AND task.status='running' AND task.finalization_state IN ('open','exploration_draining','session_draining')
            JOIN qc_agent_run run ON run.id=capture.agent_run_id AND run.task_id=task.id AND run.agent_type='main'
                AND run.status IN ('running','done','failed','aborted')
                AND COALESCE(run.output_context->>'cancel_requested','false')='false'
                AND run.id=(SELECT latest.id FROM qc_agent_run latest WHERE latest.task_id=task.id
                    AND latest.agent_type='main' ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
            JOIN qc_surface_session session ON session.task_id=task.id AND session.session_id=capture.surface_session_id
                AND session.session_id='web:'||run.id::text AND session.session_owner='web-v2-workflow'
                AND session.surface_kind='web' AND session.status IN ('active','draining')
                AND session.finalization_epoch=task.finalization_epoch
            JOIN qc_web_run_policy_snapshot policy ON policy.task_id=task.id AND policy.agent_run_id=run.id
            WHERE capture.task_id=$1::uuid AND capture.agent_run_id=$2::uuid AND capture.surface_session_id=$3
                AND capture.capture_status='captured' AND (capture.id=ANY($4::uuid[]) OR capture.id IN (
                    SELECT after_capture.parent_capture_id FROM qc_capture after_capture
                    WHERE after_capture.task_id=$1::uuid AND after_capture.agent_run_id=$2::uuid
                        AND after_capture.surface_session_id=$3 AND after_capture.id=ANY($4::uuid[])
                        AND after_capture.capture_role='after_action'))
            ORDER BY capture.captured_at,capture.id
            """,
            task_id,
            agent_run_id,
            session_id,
            list(evidence_capture_ids),
        )


async def _trusted_bootstrap_pair(connection, task_id, run_id, session_id, epoch, before, after):
    from services.api.app.application.evidence_exit_gate import is_trusted_initial_surface_action
    from services.api.app.application.web_exploration_workflow import WebCaptureStage
    from services.api.app.domain.interaction import ActionIntent, build_action_intent_hash
    row = await connection.fetchrow(
        "SELECT action.intent_payload,action.intent_hash FROM qc_action_execution action "
        "WHERE action.action_execution_id=$1 AND action.task_id=$2::uuid AND action.session_id=$3 "
        "AND action.finalization_epoch=$4 AND action.status='completed' AND action.command_sequence=1 "
        "AND action.requested_source_capture_id IS NULL AND action.source_capture_id=$5::uuid "
        "AND action.result_capture_id=$6::uuid AND NOT EXISTS(SELECT 1 FROM qc_action_execution earlier "
        "WHERE earlier.task_id=action.task_id AND earlier.session_id=action.session_id "
        "AND earlier.command_sequence<action.command_sequence)",
        before["action_execution_id"],task_id,session_id,epoch,str(before["id"]),str(after["id"]))
    if row is None:
        return False
    try:
        intent = ActionIntent.from_dict(_mapping(row["intent_payload"]))
    except (TypeError, ValueError):
        return False
    return (is_trusted_initial_surface_action(intent) and intent.task_id == task_id and intent.session_id == session_id
        and session_id == f"web:{run_id}" and intent.source_capture_id == f"web-entry:{run_id}"
        and intent.intent_id == f"web-bootstrap:{run_id}" and intent.idempotency_key == intent.intent_id
        and build_action_intent_hash(intent) == row["intent_hash"] and intent.parameters.get("web_capture_stage") == WebCaptureStage.ENTRY_PROBE.value)
