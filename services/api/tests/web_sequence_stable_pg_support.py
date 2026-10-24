"""同锚点稳定页沿原分类、通知和发布器接线，不制造第二种 Capture。"""

from services.api.app.application.web_logical_target import WebLogicalTargetClassifier
from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_page_type_assessment import (
    WebPageTypeAssessmentRecorder,
    WebPageTypeCatalogIdentity,
)
from services.api.app.application.web_run_policy import materialize_web_policy
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_logical_target_repository import (
    PostgresWebLogicalTargetRepository,
)
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    PostgresWebRunPolicySnapshotRepository,
)


async def publish_stable_anchor(
    pool, ids, source, notification, capture_id, publications, monkeypatch, *, defer_work=False
):
    # 与生产组合根一致，分类器使用实际 Run 冻结的协议与目录身份。
    snapshot = await PostgresWebRunPolicySnapshotRepository(pool=pool).read(
        task_id=ids["task"], agent_run_id=ids["run"]
    )
    assert snapshot is not None
    policy = materialize_web_policy(snapshot)
    catalog_identity = (
        WebPageTypeCatalogIdentity.from_frozen_catalog_payload(
            snapshot.to_payload()["page_catalog"]
        )
        if policy.assessment_schema_version == "web-page-type-assessment.v4"
        else None
    )

    class Classifier:
        async def classify(self, capture):
            assessment = WebPageAssessment(
                page_type="content_detail",
                confidence=0.95,
                evidence=("当前自然正文结构",),
                needs_review_reason=None,
            )
            await WebPageTypeAssessmentRecorder(
                repository=PostgresWebCaptureEvidenceRepository(
                    pool=pool,
                    assessment_schema_version=policy.assessment_schema_version,
                    catalog_identity=catalog_identity,
                ),
                assessment_schema_version=policy.assessment_schema_version,
                catalog_identity=catalog_identity,
            ).persist(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                capture=capture,
                assessment=assessment,
            )
            return assessment

    ready = await source.read_ready(capture_id=capture_id, page_role="menu")
    await WebLogicalTargetClassifier(
        classifier=Classifier(),
        repository=PostgresWebLogicalTargetRepository(pool=pool),
        task_id=ids["task"],
        agent_run_id=ids["run"],
        session_id=ids["session"],
    ).classify(ready)
    notified = await notification.notify_ready_capture(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        session_id=ids["session"],
        capture_id=capture_id,
    )
    assert notified.publication_id, notified
    claim = await publications.claim(
        publication_id=notified.publication_id,
        task_id=ids["task"],
        message_version=1,
        lease_owner="t9-stable",
        lease_seconds=60,
    )
    assert claim.lease and claim.lease.web_evidence_sequence_id is None
    if not defer_work:
        await complete_stable_publication(
            pool, ids, capture_id, publications, claim.lease, monkeypatch
        )
    return notified, claim.lease


async def complete_stable_publication(pool, ids, capture_id, publications, lease, monkeypatch):
    from services.api.app.agents.runtime.business_middlewares import base
    from services.api.app.application.app_inspection_publisher import AppCaptureInspectionPublisher
    from services.api.app.application.task_query import TaskQueryService
    from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository

    query = TaskQueryService(repository=PostgresTaskRepository(pool=pool))
    monkeypatch.setattr(base, "_get_task_query_service", lambda: query)
    prepared = await AppCaptureInspectionPublisher(task_query=query).publish_for_capture(
        task_id=ids["task"],
        capture_id=capture_id,
        visual_page_type=lease.page_type,
        page_state=lease.page_state,
        agent_run_id=ids["run"],
        snapshot_only=True,
        prepare_only=True,
    )
    assert prepared.published_count == 1, prepared
    await publications.complete(lease=lease, result=prepared)
