"""仅旧 Web 重试用例的 T4/T5 身份合同补齐；采样和附件仍是固定夹具。"""

import json
from dataclasses import replace

from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_page_type_assessment import (
    WebPageTypeAssessmentRecorder,
    WebPageTypeCatalogIdentity,
)
from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    PostgresWebRunPolicySnapshotRepository,
)
from services.api.tests.test_web_page_type_assessment import _capture
from services.api.tests.test_web_run_policy_snapshot import policy_payload


async def freeze_before_retry_material(connection, pool, ids):
    # 原共享种子带一个页面，本 Web 用例先移除自己的种子，真实冻结后再恢复。
    page = await connection.fetchval("SELECT row_to_json(p)::text FROM qc_page_capture p WHERE id=$1::uuid", ids["capture"])
    await connection.execute("DELETE FROM qc_page_capture WHERE id=$1::uuid", ids["capture"])
    await connection.execute("DELETE FROM qc_capture WHERE id=$1::uuid AND task_id=$2::uuid", ids["capture"], ids["task"])
    await connection.execute("UPDATE qc_task SET target_platform='web' WHERE id=$1::uuid", ids["task"])
    await PostgresWebRunPolicySnapshotRepository(pool=pool).freeze_or_read(
        WebRunPolicySnapshot.create(task_id=ids["task"], agent_run_id=ids["run"], payload=policy_payload())
    )
    await connection.execute("INSERT INTO qc_page_capture SELECT * FROM json_populate_record(NULL::qc_page_capture,$1::json)", page)


async def record_retry_stable_identity(connection, pool, ids, session_id):
    frozen = await PostgresWebRunPolicySnapshotRepository(pool=pool).read(
        task_id=ids["task"], agent_run_id=ids["run"],
    )
    identity = WebPageTypeCatalogIdentity.from_frozen_catalog_payload(frozen.to_payload()["page_catalog"])
    reader = PostgresWebCaptureEvidenceRepository(pool=pool, catalog_identity=identity,
        assessment_schema_version="web-page-type-assessment.v4")
    index = await reader.read(task_id=ids["task"], capture_id=ids["capture"])
    assert index is not None
    # 复用 T1/T3 合法稳定信号，只改变当前身份，不把旧空事实帧升级为稳定。
    original = _capture()
    from services.api.app.application.web_readiness_contracts import (
        WebEvidenceRole,
        assess_web_readiness,
    )
    readiness = assess_web_readiness(replace(original.inspection_readiness.signals,
        identity_before=index.url, identity_after=index.url), role=WebEvidenceRole.INSPECTION)
    capture = replace(original, capture_id=ids["capture"], url=index.url,
        captured_at=index.captured_at, screenshot_artifact_id=index.artifact_ids["screenshot"],
        dom_artifact_id=index.artifact_ids["dom"], visible_text_artifact_id=index.artifact_ids["visible_text"],
        inspection_readiness=readiness)
    await connection.execute("UPDATE qc_page_capture SET metadata=metadata||$2::jsonb WHERE id=$1::uuid",
                             ids["capture"], json.dumps({"finalization_epoch": 0}))
    # 移除本用例旧的任意 assessment_record_id 固定行，由真实 Recorder 创建认证身份。
    await connection.execute("DELETE FROM qc_h5_page_type_assessment WHERE task_id=$1::uuid AND page_capture_id=$2::uuid",
                             ids["task"], ids["capture"])
    await WebPageTypeAssessmentRecorder(repository=reader, catalog_identity=identity).persist(
        task_id=ids["task"], agent_run_id=ids["run"], session_id=session_id, capture=capture,
        assessment=WebPageAssessment(page_type="content_list", confidence=0.99, evidence=("固定稳定采样",)))
