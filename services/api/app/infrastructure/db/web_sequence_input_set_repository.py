"""Web 专属封存写入，复用旧成员核验，不扩大旧仓储的任务阶段。"""

import json

from services.api.app.domain.evidence import EvidenceInputSet
from services.api.app.infrastructure.db.evidence_input_set_repository import (
    PostgresEvidenceInputSetRepository,
)


async def seal_web_input_on_connection(connection, *, input_set, lease):
    """调用方先锁 task-finalization；新封存再认证真实 Publication 租约与活动阶段。"""
    verifier = PostgresEvidenceInputSetRepository()
    for member in input_set.members:
        await verifier._validate_member(connection, input_set=input_set, member=member)
    payload = input_set.to_dict()
    record = {
        **payload,
        "input_set_payload": payload,
        "web_evidence_sequence_id": lease.web_evidence_sequence_id,
    }
    persisted = await connection.fetchval(
        """
        INSERT INTO qc_evidence_input_set (
            input_set_id,input_set_hash,task_id,source_agent_run_id,rule_id,rule_code,rule_contract_hash,
            ruleset_snapshot_hash,scope,scope_key,finalization_epoch,eligibility_snapshot_id,
            eligibility_snapshot_hash,selection_policy_hash,eligible_capture_ids,protocol_version,
            input_set_payload,sealed_at,web_evidence_sequence_id)
        SELECT value.input_set_id,value.input_set_hash,value.task_id,value.source_agent_run_id,value.rule_id,
            value.rule_code,value.rule_contract_hash,value.ruleset_snapshot_hash,value.scope,value.scope_key,
            value.finalization_epoch,value.eligibility_snapshot_id,value.eligibility_snapshot_hash,
            value.selection_policy_hash,value.eligible_capture_ids,value.protocol_version,
            value.input_set_payload,value.sealed_at,value.web_evidence_sequence_id
        FROM jsonb_populate_record(NULL::qc_evidence_input_set,$1::jsonb) value
        JOIN qc_h5_inspection_publication publication ON publication.id=$2::uuid
            AND publication.task_id=value.task_id AND publication.source_agent_run_id=value.source_agent_run_id
            AND publication.finalization_epoch=value.finalization_epoch
            AND publication.web_evidence_sequence_id=value.web_evidence_sequence_id
        JOIN qc_task task ON task.id=publication.task_id AND task.target_platform='web'
            AND task.task_config->>'workflow_version'='v2' AND task.status='running'
            AND task.finalization_epoch=publication.finalization_epoch
            AND task.finalization_state IN ('open','exploration_draining','session_draining')
        JOIN qc_rule rule ON rule.id=value.rule_id AND rule.ruleset_id=ANY(qc_ruleset_scope(publication.ruleset_id))
            AND rule.rule_code=value.rule_code AND rule.status='enabled'
        JOIN qc_agent_run run ON run.id=value.source_agent_run_id AND run.task_id=task.id
            AND run.agent_type='main' AND run.status IN ('running','done','failed','aborted')
            AND COALESCE(run.output_context->>'cancel_requested','false')='false'
            AND run.id=(SELECT latest.id FROM qc_agent_run latest WHERE latest.task_id=task.id
                AND latest.agent_type='main' ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
        JOIN qc_web_evidence_sequence sequence ON sequence.sequence_id=value.web_evidence_sequence_id
            AND sequence.sequence_hash=value.eligibility_snapshot_hash
            AND sequence.sequence_id::text=value.eligibility_snapshot_id
        JOIN qc_surface_session session ON session.session_id=sequence.session_id AND session.task_id=task.id
            AND session.session_owner='web-v2-workflow' AND session.session_id='web:'||value.source_agent_run_id::text
            AND session.finalization_epoch=value.finalization_epoch AND session.status IN ('active','draining')
        WHERE publication.status='leased' AND publication.lease_token=$3::uuid
            AND publication.row_version=$4 AND publication.lease_expires_at>now()
            AND value.scope='web_sequence'
        ON CONFLICT(input_set_id) DO UPDATE SET input_set_id=qc_evidence_input_set.input_set_id
        RETURNING input_set_payload
        """,
        json.dumps(record, ensure_ascii=False),
        lease.publication_id,
        lease.lease_token,
        lease.row_version,
    )
    if persisted is None:
        raise RuntimeError("WEB_SEQUENCE_SEAL_AUTHORITY_REJECTED")
    restored = EvidenceInputSet.from_dict(
        json.loads(persisted) if isinstance(persisted, str) else persisted
    )
    if restored.input_set_hash != input_set.input_set_hash:
        raise RuntimeError("WEB_SEQUENCE_INPUT_SET_DRIFT")
    for member in input_set.members:
        await connection.execute(
            """
            INSERT INTO qc_evidence_input_set_member(
                input_set_id,task_id,ordinal,capture_id,capture_role,page_sequence,
                manifest_id,manifest_version,manifest_hash,artifact_ids,observation_packet_ids,
                journey_execution_id,environment_profile_id)
            VALUES($1,$2::uuid,$3,$4::uuid,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12,$13)
            ON CONFLICT(input_set_id,ordinal) DO NOTHING
            """,
            input_set.input_set_id,
            input_set.task_id,
            member.ordinal,
            member.capture_id,
            member.capture_role,
            member.page_sequence,
            member.manifest_id,
            member.manifest_version,
            member.manifest_hash,
            json.dumps(list(member.artifact_ids)),
            json.dumps(list(member.observation_packet_ids)),
            member.journey_execution_id,
            member.environment_profile_id,
        )
    await verifier._verify_persisted_members(connection, input_set=input_set)
