"""隔离库的跨会话补采种子；不关闭外键、不修改原封存输入。"""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.infrastructure.db.evidence_supplement_reseal import (
    seal_supplement_input_set_on_connection,
)
from services.api.tests.test_h5_producer_result_reuse_postgres import _save_manifest


async def seed_restored_supplement(connection, ids):
    producer, before, after = (str(uuid4()) for _ in range(3))
    source_session = f"h5:{ids['run']}"
    session = source_session + ":recapture:" + uuid4().hex[:20]
    restoration, assessment, probe, supplement = (
        prefix + uuid4().hex for prefix in ("restore:", "assessment:", "probe:", "supplement:")
    )
    await connection.execute(
        "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name) "
        "VALUES($1::uuid,$2::uuid,'explorer','isolated-restored')",
        producer,
        ids["task"],
    )
    await connection.execute(
        "INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,"
        "status,activated_at) VALUES($1,$2::uuid,'web','h5-workflow','active',now())",
        session,
        ids["task"],
    )
    await connection.execute(
        "UPDATE qc_rule SET input_contract=$2::jsonb WHERE id=$1::uuid",
        ids["rule"],
        json.dumps({"required_capture_roles": ["primary", "after"]}),
    )
    for capture, role in ((before, "probe_before"), (after, "probe_result_replay")):
        await connection.execute(
            "INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,"
            "capture_role,capture_status,captured_at,metadata) "
            "VALUES($1::uuid,$2::uuid,$3::uuid,'web',$4,$5,'captured',clock_timestamp(),"
            '\'{"url":"https://fixture.test/membership","page_type":"membership"}\')',
            capture,
            ids["task"],
            producer,
            session,
            role,
        )
    action = "restored-action:" + uuid4().hex
    await connection.execute(
        "INSERT INTO qc_action_execution(action_execution_id,task_id,session_id,intent_id,"
        "idempotency_key,intent_hash,intent_protocol_version,intent_payload,command_sequence,"
        "finalization_epoch,requested_source_capture_id,status,result_payload) "
        "VALUES($1,$2::uuid,$3,$1,$1,'isolated-hash','action-intent.v1','{}',1,0,$4::uuid,"
        "'planned','{}')",
        action,
        ids["task"],
        session,
        before,
    )
    await connection.execute(
        "UPDATE qc_capture SET action_execution_id=$2 WHERE id=$1::uuid",
        before,
        action,
    )
    await connection.execute(
        "UPDATE qc_capture SET action_execution_id=$2,parent_capture_id=$3::uuid WHERE id=$1::uuid",
        after,
        action,
        before,
    )
    manifests = []
    for capture, role in ((before, "probe_before"), (after, "probe_result_replay")):
        payload = json.dumps(
            {
                "url": "https://fixture.test/membership",
                "body_text": "套餐 20 元",
                "viewport": {"width": 390, "height": 844},
                "elements": [],
            },
            ensure_ascii=False,
        ).encode()
        artifact = ArtifactReference(
            artifact_id=str(uuid4()),
            artifact_type="ui_snapshot",
            role=role,
            storage_path=f"{ids['task']}/{capture}/ui.json",
            checksum="sha256:" + hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            content_type="application/json",
        )
        await connection.execute(
            "INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,source,"
            "storage_path,checksum,size_bytes,content_type) "
            "VALUES($1::uuid,$2::uuid,$3::uuid,'ui_snapshot','baseline',$4,$5,$6,'application/json')",
            artifact.artifact_id,
            ids["task"],
            capture,
            artifact.storage_path,
            artifact.checksum,
            len(payload),
        )
        manifest = build_evidence_manifest_reference(
            task_id=ids["task"],
            session_id=session,
            capture_id=capture,
            manifest_version=1,
            artifacts=(artifact,),
            quality_status=EvidenceQualityStatus.PASS,
            quality_reason="isolated_integrity",
            created_at=datetime.now(UTC),
        )
        await _save_manifest(connection, manifest)
        manifests.append(manifest)
    await connection.execute(
        "INSERT INTO qc_restoration_attempt(attempt_id,plan_id,plan_hash,plan_protocol_version,"
        "plan_payload,request_id,task_id,original_capture_id,source_session_id,target_session_id,"
        "surface_kind,strategy,attempt_no,status,source_grade,restored_capture_id,"
        "equivalence_assessment_id,assessment_hash,assessment_payload,attempt_protocol_version,"
        "started_at,finished_at) VALUES($1,$1,'isolated-plan','restoration-plan.v1',$2::jsonb,$3,"
        "$4::uuid,$5::uuid,$6,$7,'web','new_session_replay',1,'equivalent','C',$8::uuid,$9,"
        "'isolated-assessment','{\"passed\":true}','restoration-attempt.v1',now(),now())",
        restoration,
        json.dumps(
            {
                "replay_execution_plan_id": "h5-new-session-route:" + "a" * 64,
                "source_session_id": source_session,
                "target_session_id": session,
                "strategy": "new_session_replay",
            }
        ),
        ids["request"],
        ids["task"],
        ids["capture"],
        source_session,
        session,
        before,
        assessment,
    )
    await connection.execute(
        "INSERT INTO qc_v2_work_item(work_id,work_type,task_id,session_id,branch_id,"
        "source_capture_id,evidence_request_id,protocol_version,payload_hash,work_payload,"
        "deadline,restoration_attempt_id,equivalence_assessment_id,source_grade) "
        "VALUES($1,'probe',$2::uuid,$3,'isolated',$4::uuid,$5,'explorer-work.v1',"
        "'isolated-probe','{}',now()+interval '10 minutes',$6,$7,'C')",
        probe,
        ids["task"],
        session,
        before,
        ids["request"],
        restoration,
        assessment,
    )
    await connection.execute(
        "UPDATE qc_evidence_request SET status='satisfied',completed_at=now(),"
        "resolution_manifest_id=$2 WHERE request_id=$1",
        ids["request"],
        manifests[1].manifest_id,
    )
    await connection.execute(
        "INSERT INTO qc_evidence_supplement(supplement_id,task_id,request_id,manifest_id,"
        "probe_work_id,restoration_attempt_id,equivalence_assessment_id,source_grade,"
        "origin,protocol_version,payload_hash) "
        "VALUES($1,$2::uuid,$3,$4,$5,$6,$7,'C','replay_probe','evidence-supplement.v1','isolated')",
        supplement,
        ids["task"],
        ids["request"],
        manifests[1].manifest_id,
        probe,
        restoration,
        assessment,
    )
    sealed = await seal_supplement_input_set_on_connection(connection, request_id=ids["request"])
    assert sealed is not None
    return producer, before, manifests, sealed[0], payload
