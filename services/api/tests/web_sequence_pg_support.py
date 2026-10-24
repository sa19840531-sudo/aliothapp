"""T9 隔离PG真实实体夹具；时间与页面信号为明确替身，不冒称浏览器测量。"""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.evidence.presentation import (
    BasePageVisibility,
    BlockingScope,
    PresentationAssessment,
    TopmostLayerKind,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.tests.test_web_capture_readiness_metrics import _stable_capture_metrics
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _seed_completed_action_evidence,
)


async def seed_failure_frames(connection, pool, ids):
    # 本夹具的初始采集属于第一条命令；旧公共表夹具尚未填写该必需字段。
    await connection.execute(
        "UPDATE qc_capture SET session_command_sequence=1 "
        "WHERE id=$1::uuid AND task_id=$2::uuid AND agent_run_id=$3::uuid "
        "AND session_command_sequence IS NULL",
        ids["capture"],
        ids["task"],
        ids["run"],
    )
    # 来源页先落到规范表，动作帧才能保留真实父链，而不是靠空父键绕过外键。
    await connection.execute(
        "INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,url,"
        "capture_status,surface_session_id,parent_page_capture_id,action_execution_id,"
        "capture_role,session_command_sequence,captured_at,metadata) "
        "SELECT id,task_id,agent_run_id,1,'t9-source','https://example.test/',"
        "capture_status,surface_session_id,parent_capture_id,action_execution_id,"
        "capture_role,session_command_sequence,captured_at,metadata FROM qc_capture "
        "WHERE id=$1::uuid AND task_id=$2::uuid AND agent_run_id=$3::uuid "
        "AND NOT EXISTS(SELECT 1 FROM qc_page_capture WHERE id=$1::uuid)",
        ids["capture"],
        ids["task"],
        ids["run"],
    )
    await connection.execute(
        "UPDATE qc_task SET task_config=task_config||$2::jsonb WHERE id=$1::uuid",
        ids["task"],
        json.dumps(
            {"execution_mode": "full_inspection", "web_inspection_notification_mode": "enforced"}
        ),
    )
    first = await _seed_completed_action_evidence(
        connection,
        ids,
        action_execution_id="t9-" + str(uuid4()),
        intent_id="t9-" + str(uuid4()),
        command_sequence=1,
        requested_source_capture_id=ids["capture"],
        parent_capture_id=ids["capture"],
    )
    second = await _seed_completed_action_evidence(
        connection,
        ids,
        action_execution_id="t9-" + str(uuid4()),
        intent_id="t9-" + str(uuid4()),
        command_sequence=2,
        requested_source_capture_id=first[1],
        parent_capture_id=first[1],
    )
    captures = (*first, *second)
    store = {}
    now = datetime.now(UTC)
    presentation = PresentationAssessment(
        topmost_layer_kind=TopmostLayerKind.PAGE,
        base_page_visibility=BasePageVisibility.VISIBLE,
        blocking_scope=BlockingScope.NONE,
    ).to_dict()
    signals = _stable_capture_metrics(
        page_settle_complete=False,
        readiness={
            "core_present": True,
            "visible_loader": True,
            "transition_active": False,
            "necessary_media_ready": True,
            "dom_quiet_ms": 1200,
        },
        settle_diagnostics={"settle_complete": False, "quorum_met": False},
    )
    for ordinal, capture_id in enumerate(captures):
        role = "before_action" if ordinal % 2 == 0 else "after_action"
        await connection.execute(
            "INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,url,"
            "capture_status,surface_session_id,parent_page_capture_id,action_execution_id,"
            "capture_role,session_command_sequence) "
            "SELECT id,task_id,agent_run_id,$4,$5,'https://example.test/list','captured',"
            "surface_session_id,parent_capture_id,action_execution_id,capture_role,"
            "session_command_sequence FROM qc_capture "
            "WHERE id=$1::uuid AND task_id=$2::uuid AND agent_run_id=$3::uuid",
            capture_id,
            ids["task"],
            ids["run"],
            ordinal + 2,
            "t9-sequence-" + str(ordinal),
        )
        # 同生产仓储从PageCapture写原事实，不能仅改公共表后被既有同步触发器覆写。
        await connection.execute(
            "UPDATE qc_page_capture SET captured_at=$2,metadata=$3::jsonb WHERE id=$1::uuid",
            capture_id,
            now + timedelta(seconds=ordinal),
            json.dumps(
                {
                    "finalization_epoch": 0,
                    "presentation": presentation,
                    "pipeline_metrics": {"web_readiness": signals},
                },
                ensure_ascii=False,
            ),
        )
        artifacts = []
        for kind, content_type, content in (
            ("screenshot", "image/png", b"\x89PNG\r\n\x1a\nsame-loading-image"),
            ("dom", "text/html", f"<main><button>LOADING_{ordinal}</button></main>".encode()),
            ("visible_text", "text/plain", f"LOADING_{ordinal}".encode()),
        ):
            artifact_id = str(uuid4())
            path = f"{ids['task']}/{capture_id}/t9-{kind}"
            checksum = "sha256:" + sha256(content).hexdigest()
            store[path] = content
            await connection.execute(
                "INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,"
                "source,storage_path,content_type,checksum,size_bytes) VALUES($1::uuid,$2::uuid,$3::uuid,$4,'baseline',$5,$6,$7,$8)",
                artifact_id,
                ids["task"],
                capture_id,
                kind,
                path,
                content_type,
                checksum,
                len(content),
            )
            artifacts.append(
                ArtifactReference(
                    artifact_id=artifact_id,
                    artifact_type=kind,
                    role=role,
                    storage_path=path,
                    content_type=content_type,
                    checksum=checksum,
                    size_bytes=len(content),
                )
            )
        manifest = build_evidence_manifest_reference(
            task_id=ids["task"],
            session_id=ids["session"],
            capture_id=capture_id,
            manifest_version=1,
            artifacts=tuple(artifacts),
            quality_status=EvidenceQualityStatus.PASS,
            quality_reason="artifact_integrity_verified",
            created_at=now,
        )
        await PostgresEvidenceManifestRepository(pool=pool).add(manifest=manifest)
    await connection.execute(
        "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
        captures[-1],
        ids["session"],
    )
    return captures, store
