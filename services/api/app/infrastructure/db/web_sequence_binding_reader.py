"""普通 Work 租约内的 Web 序列只读授权；不复用补采请求作为许可。"""

import json

from services.api.app.application.observation_packet import ObservationPacketValidationError
from services.api.app.application.web_run_policy import materialize_web_policy
from services.api.app.application.web_sequence_capture_integrity import (
    web_sequence_capture_integrity,
)
from services.api.app.domain.evidence import EvidenceInputSet
from services.api.app.domain.evidence.presentation import PresentationAssessment
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.evidence_input_set_repository import (
    PostgresEvidenceInputSetRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    read_web_policy_on_connection,
)


def _mapping(value):
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ObservationPacketValidationError("Web 序列持久载荷不是对象")
    return parsed


class PostgresWebSequenceBindingReader:
    """检查真实平台、当前周期、运行中 Work 以及不可变成员的完整闭包。"""

    def __init__(self, *, pool=None, database_url=None):
        self._pool = pool
        self._database_url = database_url

    async def load_authorized(
        self,
        *,
        task_id,
        page_capture_id,
        source_agent_run_id,
        input_set_id,
        rule_id,
        rule_contract_hash,
        work_item_id=None,
        attempt_id=None,
        consumer_agent_run_id=None,
        lease_token=None,
    ):
        if not all((work_item_id, attempt_id, consumer_agent_run_id, lease_token)):
            raise ObservationPacketValidationError("Web 序列读取缺少当前消费租约身份")
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task_id}"
            )
            row = await connection.fetchrow(
                """
                SELECT inputs.input_set_payload, sequence.sequence_payload, sequence.sequence_hash,
                       sequence.sequence_id::text, sequence.finalization_epoch,
                       sequence.target_id, sequence.occurrence_id, sequence.purpose
                FROM qc_evidence_input_set inputs
                JOIN qc_web_evidence_sequence sequence ON sequence.sequence_id=inputs.web_evidence_sequence_id
                    AND sequence.task_id=inputs.task_id AND sequence.source_agent_run_id=inputs.source_agent_run_id
                    AND sequence.finalization_epoch=inputs.finalization_epoch
                JOIN qc_inspection_work_item_rule member ON member.evidence_input_set_id=inputs.input_set_id
                    AND member.rule_id=inputs.rule_id AND member.contract_hash=inputs.rule_contract_hash
                JOIN qc_inspection_work_item work ON work.id=member.work_item_id
                    AND work.task_id=inputs.task_id AND work.producer_agent_run_id=inputs.source_agent_run_id
                    AND work.finalization_epoch=inputs.finalization_epoch AND work.page_capture_id=sequence.anchor_capture_id
                JOIN qc_task task ON task.id=inputs.task_id
                JOIN qc_inspection_attempt attempt ON attempt.work_item_id=work.id
                    AND attempt.id=$8::uuid AND attempt.consumer_agent_run_id=work.consumer_agent_run_id
                    AND attempt.lease_token=work.lease_token AND attempt.attempt_no=work.attempt_count
                    AND attempt.status='running'
                JOIN qc_agent_run run ON run.id=inputs.source_agent_run_id AND run.task_id=task.id
                JOIN qc_surface_session session ON session.task_id=task.id AND session.session_id=sequence.session_id
                JOIN qc_web_run_policy_snapshot policy ON policy.task_id=task.id AND policy.agent_run_id=run.id
                WHERE inputs.input_set_id=$1 AND inputs.task_id=$2::uuid AND inputs.source_agent_run_id=$3::uuid
                    AND inputs.rule_id=$4::uuid AND inputs.rule_contract_hash=$5 AND inputs.scope='web_sequence'
                    AND sequence.anchor_capture_id=$6::uuid AND task.target_platform='web'
                    AND task.task_config->>'workflow_version'='v2' AND task.status='running'
                    AND task.finalization_state IN ('open','exploration_draining','session_draining')
                    AND task.finalization_epoch=sequence.finalization_epoch AND run.agent_type='main'
                    AND run.id=(SELECT latest.id FROM qc_agent_run latest WHERE latest.task_id=task.id
                        AND latest.agent_type='main' ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
                    AND COALESCE(run.output_context->>'cancel_requested','false')='false'
                    AND session.session_id='web:'||run.id::text AND session.session_owner='web-v2-workflow'
                    AND session.surface_kind='web' AND session.finalization_epoch=sequence.finalization_epoch
                    AND session.status IN ('active','draining','closing','closed')
                    AND run.status IN ('running','done','failed','aborted')
                    AND work.status='running' AND work.control_mode='enforced'
                    AND work.session_dependency='snapshot' AND work.strategy='llm'
                    AND work.lease_token IS NOT NULL AND work.lease_expires_at>now()
                    AND work.id=$7::uuid AND work.consumer_agent_run_id=$9::uuid AND work.lease_token=$10::uuid
                LIMIT 1
                """,
                input_set_id,
                task_id,
                source_agent_run_id,
                rule_id,
                rule_contract_hash,
                page_capture_id,
                work_item_id,
                attempt_id,
                consumer_agent_run_id,
                lease_token,
            )
            if row is None:
                raise ObservationPacketValidationError(
                    "Web InputSet 没有当前真实 Work/平台/周期授权"
                )
            inputs = EvidenceInputSet.from_dict(_mapping(row["input_set_payload"]))
            sequence = _mapping(row["sequence_payload"])
            expected = {
                "task_id": task_id,
                "agent_run_id": source_agent_run_id,
                "session_id": f"web:{source_agent_run_id}",
                "finalization_epoch": row["finalization_epoch"],
                "target_platform": "web",
                "workflow_version": "v2",
                "session_owner": "web-v2-workflow",
                "target_id": row["target_id"],
                "occurrence_id": row["occurrence_id"],
                "purpose": row["purpose"],
            }
            if (
                inputs.input_set_id != input_set_id
                or inputs.rule_id != rule_id
                or inputs.task_id != task_id
                or inputs.source_agent_run_id != source_agent_run_id
                or inputs.rule_contract_hash != rule_contract_hash
                or inputs.finalization_epoch != row["finalization_epoch"]
                or inputs.eligibility_snapshot_id != row["sequence_id"]
                or inputs.eligibility_snapshot_hash != row["sequence_hash"]
                or stable_protocol_hash(sequence) != row["sequence_hash"]
                or any(sequence.get(k) != v for k, v in expected.items())
            ):
                raise ObservationPacketValidationError("Web InputSet 与序列冻结身份漂移")
            verifier = PostgresEvidenceInputSetRepository(pool=pool)
            await verifier._verify_persisted_members(connection, input_set=inputs)
            frames = sequence.get("frames", [])
            if len(frames) != len(inputs.members):
                raise ObservationPacketValidationError("Web 序列封存成员不完整")
            policy_snapshot = await read_web_policy_on_connection(
                connection, task_id, source_agent_run_id
            )
            if (
                policy_snapshot is None
                or sequence.get("policy_hash") != policy_snapshot.payload_sha256
            ):
                raise ObservationPacketValidationError("Web 序列冻结策略漂移")
            policy = materialize_web_policy(policy_snapshot)
            if "capability_revision" in sequence:
                from services.api.app.infrastructure.db.web_capability_revision_repository import (
                    read_capture_revision_on_connection,
                )

                revision = await read_capture_revision_on_connection(
                    connection,
                    task_id=task_id,
                    agent_run_id=source_agent_run_id,
                    finalization_epoch=inputs.finalization_epoch,
                    capture_id=page_capture_id,
                )
                if revision != sequence["capability_revision"]:
                    raise ObservationPacketValidationError("Web 局部能力修订冻结身份漂移")
            for member, frame in zip(inputs.members, frames, strict=True):
                await verifier._validate_member(connection, input_set=inputs, member=member)
                layer = await self._validate_frame(
                    connection, inputs, member, frame, row["sequence_id"], policy
                )
                if (
                    member.capture_id == page_capture_id
                    and "capability_revision" in sequence
                    and sequence["capability_revision"].get("visible_layer") != layer
                ):
                    raise ObservationPacketValidationError(
                        "Web 局部能力可见层与当前原始 Capture 不一致"
                    )
            if sequence["purpose"] == "interaction_sequence":
                from services.api.app.infrastructure.db.web_interaction_sequence_facts import (
                    authenticate_interaction_facts,
                )

                outcomes = sequence.get("interaction_outcomes", [])
                refs = [
                    {
                        key: item.get(key)
                        for key in (
                            "outcome_ordinal",
                            "intent_id",
                            "requested_source_capture_id",
                            "action_result_capture_id",
                        )
                    }
                    for item in outcomes
                ]
                try:
                    current = await authenticate_interaction_facts(
                        connection, sequence=sequence, outcomes=outcomes, action_refs=refs
                    )
                except ValueError as exc:
                    raise ObservationPacketValidationError(str(exc)) from exc
                if current != outcomes:
                    raise ObservationPacketValidationError("Web 动作序列原始引用漂移")
            return inputs, sequence

    async def _validate_frame(self, connection, inputs, member, frame, sequence_id, policy):
        row = await connection.fetchrow(
            """
            SELECT capture.id::text, capture.capture_role, capture.parent_capture_id::text,
                capture.action_execution_id, capture.captured_at, capture.metadata,
                member.frame_payload, action.source_capture_id::text AS action_source,
                action.result_capture_id::text AS action_result,
                action.finalization_epoch AS action_epoch
            FROM qc_web_evidence_sequence_member member
            JOIN qc_capture capture ON capture.id=member.capture_id AND capture.task_id=member.task_id
            LEFT JOIN qc_action_execution action ON action.action_execution_id=capture.action_execution_id
                AND action.task_id=capture.task_id AND action.session_id=capture.surface_session_id
            WHERE member.sequence_id=$1::uuid AND member.ordinal=$2 AND capture.id=$3::uuid
                AND capture.task_id=$4::uuid AND capture.agent_run_id=$5::uuid
                AND capture.surface_session_id='web:'||$5::text AND capture.capture_status='captured'
                AND capture.metadata->>'finalization_epoch'=$6::text
                AND member.captured_at=capture.captured_at
            """,
            sequence_id,
            member.ordinal,
            member.capture_id,
            inputs.task_id,
            inputs.source_agent_run_id,
            str(inputs.finalization_epoch),
        )
        if row is None or _mapping(row["frame_payload"]) != frame:
            raise ObservationPacketValidationError("Web 序列持久帧归属漂移")
        metadata = _mapping(row["metadata"])
        try:
            integrity_hash = web_sequence_capture_integrity(
                metadata, capture_role=row["capture_role"], profile=policy.readiness_profile
            )
        except ValueError as exc:
            raise ObservationPacketValidationError(str(exc)) from exc
        if integrity_hash != frame.get("capture_consistency_hash"):
            raise ObservationPacketValidationError("Web 序列采集边界证明漂移")
        presentation = PresentationAssessment.from_dict(metadata.get("presentation"))
        if presentation is None or presentation.is_diagnostic:
            raise ObservationPacketValidationError("Web 序列缺少自然展示来源证明")
        facts = {
            "capture_id": row["id"],
            "capture_role": row["capture_role"],
            "parent_capture_id": row["parent_capture_id"],
            "action_execution_id": row["action_execution_id"],
            "captured_at": row["captured_at"].isoformat(),
            "evidence_mode": "natural",
        }
        if any(frame.get(k) != v for k, v in facts.items()):
            raise ObservationPacketValidationError("Web 序列帧与真实 Capture 事实不一致")
        if (
            row["action_epoch"] != inputs.finalization_epoch
            or (
                member.capture_role == "after_action"
                and (
                    row["action_result"] != member.capture_id
                    or row["action_source"] != row["parent_capture_id"]
                )
            )
            or (
                member.capture_role == "before_action" and row["action_source"] != member.capture_id
            )
        ):
            raise ObservationPacketValidationError("Web 序列实际动作父链不一致")
        return {
            key: presentation.to_dict().get(key, "unknown")
            for key in ("topmost_layer_kind", "base_page_visibility", "blocking_scope")
        }
