"""Web Capture 证据索引只选择主 PNG 截图。"""

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageStructureFacts,
    WebReadyPageCapture,
)
from services.api.app.application.web_page_type_assessment import (
    WebPageTypeAssessmentRecorder,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    _PERSIST_WEB_PAGE_TYPE_ASSESSMENT_SQL,
    PostgresWebCaptureEvidenceRepository,
    WebCapturePageTypePersistenceError,
)


class AcquireContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return None


@pytest.fixture(autouse=True)
def isolate_graph_in_sql_contract_tests(monkeypatch):
    """本文件只验证 SQL 写回合同；真实投影和回滚在独立 PostgreSQL 文件覆盖。"""
    from services.api.app.infrastructure.db.page_graph_repository import PostgresPageGraphRepository

    async def projected(self, connection, *, capture_id):
        assert capture_id == "capture-1"
        assert connection.transactions > connection.commits + connection.rollbacks

    monkeypatch.setattr(PostgresPageGraphRepository,
                        "project_capture_from_database_on_connection", projected)
    monkeypatch.setattr(PostgresPageGraphRepository, "_refresh_capture_actions", projected)


class Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return AcquireContext(self.connection)


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""

    async def fetch(self, query, *args):
        del args
        self.query = query
        return self.rows


class TransactionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.transactions += 1
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.connection.commits += 1
        else:
            self.connection.rollbacks += 1
        return None


class PageTypePersistenceConnection:
    def __init__(self, *, session_active: bool = True, assessment_conflict: bool = False):
        self.session_active = session_active
        self.assessment_conflict = assessment_conflict
        self.query = ""
        self.args: tuple[object, ...] = ()
        self.transactions = 0
        self.commits = 0
        self.rollbacks = 0

    def transaction(self):
        return TransactionContext(self)

    async def fetchrow(self, query, *args):
        self.query = query
        self.args = args
        if (
            args[:4] != ("task-1", "run-1", "web:run-1", "capture-1")
            or not self.session_active
        ):
            return None
        return {
            "capture_id": "capture-1",
            "canonical_page_type": args[4],
            "assessment_record_id": (
                None if self.assessment_conflict else "web-assessment-1"
            ),
        }


class StatefulPageTypeTransaction:
    def __init__(self, connection):
        self.connection = connection
        self.snapshot = None

    async def __aenter__(self):
        self.connection.transactions += 1
        self.snapshot = (
            self.connection.capture_page_type,
            deepcopy(self.connection.capture_metadata),
            deepcopy(self.connection.assessment),
            self.connection.capture_update_count,
        )
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.connection.commits += 1
        else:
            self.connection.rollbacks += 1
            (
                self.connection.capture_page_type,
                self.connection.capture_metadata,
                self.connection.assessment,
                self.connection.capture_update_count,
            ) = self.snapshot
        return None


class StatefulPageTypePersistenceConnection:
    """以数据库冲突语义模拟单 Capture 的两次原子写入。"""

    def __init__(self):
        self.query = ""
        self.args: tuple[object, ...] = ()
        self.transactions = 0
        self.commits = 0
        self.rollbacks = 0
        self.capture_page_type: str | None = None
        self.capture_metadata: dict[str, object] = {"stable": "before"}
        self.assessment: dict[str, object] | None = None
        self.capture_update_count = 0

    def transaction(self):
        return StatefulPageTypeTransaction(self)

    async def fetchrow(self, query, *args):
        self.query = query
        self.args = args
        if args[:4] != ("task-1", "run-1", "web:run-1", "capture-1"):
            return None

        assessment_precedes_capture = query.index("upserted_assessment AS (") < (
            query.index("updated_capture AS (")
        )

        def update_capture() -> None:
            if args[4] is not None:
                self.capture_page_type = str(args[4])
            self.capture_metadata.update(json.loads(str(args[5])))
            self.capture_update_count += 1

        if not assessment_precedes_capture:
            update_capture()
        incoming = {
            "assessment_record_id": str(args[6]),
            "payload": json.loads(str(args[10])),
            "payload_hash": str(args[11]),
            "detection_status": str(args[12]),
            "page_type": str(args[13]),
            "row_version": 1,
        }
        assessment_record_id: str | None = incoming["assessment_record_id"]
        assessment_inserted = False
        if self.assessment is None:
            self.assessment = incoming
            assessment_inserted = True
        elif self.assessment["payload_hash"] != incoming["payload_hash"]:
            immutable_conflict_guard = (
                "AND existing.payload_hash = EXCLUDED.payload_hash" in query
                or "existing_assessment.payload_hash = $12" in query
            )
            if immutable_conflict_guard:
                assessment_record_id = None
            else:
                incoming["row_version"] = int(self.assessment["row_version"]) + 1
                self.assessment = incoming
        else:
            assessment_record_id = str(self.assessment["assessment_record_id"])

        if assessment_precedes_capture and assessment_inserted:
            update_capture()

        return {
            "capture_id": "capture-1",
            "canonical_page_type": self.capture_page_type,
            "assessment_record_id": assessment_record_id,
        }


@pytest.mark.asyncio
async def test_sequence_json_cannot_replace_primary_png() -> None:
    connection = Connection(
        [
            {
                "url": "https://example.test/",
                "captured_at": datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
                "id": "primary-shot",
                "artifact_type": "screenshot",
                "content_type": "image/png",
            },
            {
                "url": "https://example.test/",
                "captured_at": datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
                "id": "sequence-shot",
                "artifact_type": "screenshot",
                "content_type": "application/json",
            },
            {
                "url": "https://example.test/",
                "captured_at": datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
                "id": "dom-id",
                "artifact_type": "dom",
                "content_type": "text/html; charset=utf-8",
            },
        ]
    )

    result = await PostgresWebCaptureEvidenceRepository(pool=Pool(connection)).read(
        task_id="task-1", capture_id="capture-1"
    )

    assert result.artifact_ids["screenshot"] == "primary-shot"
    assert "artifact.content_type = 'image/png'" in connection.query


class BundleConnection:
    def __init__(self, *, qualification=True, menu_limit=10):
        self.qualification = qualification
        self.menu_limit = menu_limit
        self.queries: list[str] = []
        self.args: list[tuple[object, ...]] = []
        self.fetch_count = 0

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        self.args.append(args)
        if not self.qualification or "FROM qc_web_run_policy_snapshot" in query:
            return None
        return {
            "task_id": "task-1",
            "agent_run_id": "run-1",
            "session_id": "web:run-1",
            "site_id": "site-1",
            "menu_limit": self.menu_limit,
            "normalization_version": "web-page-type-assessment.v3",
            "budget_state": "sealed",
            "finalization_epoch": 0,
        }

    async def fetch(self, query, *args):
        self.queries.append(query)
        self.args.append(args)
        self.fetch_count += 1
        if "FROM qc_web_menu_ledger_entry" in query:
            return [{"menu_entry_id": "home", "status": "home_baseline"}]
        if "FROM qc_capture AS common_capture" in query:
            return [{"capture_id": "capture-1", "url": "https://example.test/"}]
        if "FROM qc_action_execution AS action" in query:
            return [{"action_execution_id": "action-1", "status": "completed"}]
        if "FROM qc_evidence_artifact AS artifact" in query:
            return [{"artifact_id": "artifact-1", "artifact_type": "dom"}]
        raise AssertionError(query)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_field", [None, "pipeline_metrics", "web_evidence_admission"])
async def test_bundle_repository_decodes_asyncpg_jsonb_before_readiness_admission(
    invalid_field: str | None,
    monkeypatch,
) -> None:
    from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
    from services.api.tests.test_web_evidence_bundle import _valid_snapshot

    snapshot = _valid_snapshot()

    async def trusted_identities(connection, **kwargs):
        # 本测试只验证 JSONB 解码；身份读端的真实分类/冲突另有隔离 PG 验证。
        return {row["capture_id"]: {key: row[key] for key in ("logical_page_id", "logical_page_identity_status")} for row in snapshot["captures"]}

    monkeypatch.setattr("services.api.app.infrastructure.db.web_capture_identity.read_web_capture_identities", trusted_identities)
    json_fields = (
        "pipeline_metrics",
        "web_evidence_admission",
        "web_product_capability_assessment",
        "primary_content_capture",
    )

    class AsyncpgJsonBundleConnection(BundleConnection):
        async def fetchrow(self, query, *args):
            return {
                key: snapshot[key]
                for key in (
                    "task_id", "agent_run_id", "session_id",
                    "site_id", "menu_limit", "budget_state", "finalization_epoch",
                    "normalization_version",
                )
            }

        async def fetch(self, query, *args):
            if "FROM qc_capture AS common_capture" in query:
                rows = deepcopy(snapshot["captures"])
                for row in rows:
                    for field in json_fields:
                        row[field] = json.dumps(row.get(field, {}))
                    if invalid_field:
                        row[invalid_field] = "invalid-json"
                return rows
            for table, key in (
                ("qc_web_menu_ledger_entry", "ledger"),
                ("qc_action_execution", "actions"),
                ("qc_evidence_artifact", "artifacts"),
            ):
                if f"FROM {table}" in query:
                    return deepcopy(snapshot[key])
            raise AssertionError(query)

    result = await PostgresWebCaptureEvidenceRepository(
        pool=Pool(AsyncpgJsonBundleConnection())
    ).list_for_task(
        task_id=snapshot["task_id"],
        agent_run_id=snapshot["agent_run_id"],
        access_scope=AccessScope(
            actor_user_id="reader", tenant_id="tenant", permissions=frozenset(),
        ),
    )
    bundle = build_web_evidence_bundle(result)
    assert bundle["status"] == ("evidence_ready" if invalid_field is None else "needs_review")
    assert all(
        isinstance(row[field], dict)
        for row in result["captures"]
        for field in json_fields
    )


@pytest.mark.asyncio
async def test_bundle_repository_preserves_nonempty_detail_selection_for_rejection(
    monkeypatch,
) -> None:
    from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
    from services.api.tests.test_web_evidence_bundle import _valid_snapshot

    snapshot = _valid_snapshot()
    snapshot["ledger"][1].update(
        status="menu_page_captured",
        menu_page_type="content_list",
        detail_action_count=0,
        diagnostic_type=None,
        diagnostic={},
    )
    snapshot["ledger"][1]["detail_selection"] = {
        "candidate_id": "unexpected-detail-candidate"
    }
    for capture in snapshot["captures"]:
        if str(capture["capture_id"]).startswith("menu-"):
            capture["page_type"] = "content_list"

    async def trusted_identities(connection, **kwargs):
        return {
            row["capture_id"]: {
                key: row[key]
                for key in ("logical_page_id", "logical_page_identity_status")
            }
            for row in snapshot["captures"]
        }

    monkeypatch.setattr(
        "services.api.app.infrastructure.db.web_capture_identity.read_web_capture_identities",
        trusted_identities,
    )

    class ProjectingBundleConnection(BundleConnection):
        async def fetchrow(self, query, *args):
            return {
                key: snapshot[key]
                for key in (
                    "task_id", "agent_run_id", "session_id", "site_id", "menu_limit",
                    "budget_state", "finalization_epoch", "normalization_version",
                )
            }

        async def fetch(self, query, *args):
            if "FROM qc_web_menu_ledger_entry" in query:
                rows = deepcopy(snapshot["ledger"])
                if "entry.detail_selection" not in query:
                    for row in rows:
                        row.pop("detail_selection", None)
                return rows
            for table, key in (
                ("qc_capture AS common_capture", "captures"),
                ("qc_action_execution", "actions"),
                ("qc_evidence_artifact", "artifacts"),
            ):
                if f"FROM {table}" in query:
                    return deepcopy(snapshot[key])
            raise AssertionError(query)

    decoded = await PostgresWebCaptureEvidenceRepository(
        pool=Pool(ProjectingBundleConnection())
    ).list_for_task(
        task_id=snapshot["task_id"],
        agent_run_id=snapshot["agent_run_id"],
        access_scope=AccessScope(
            actor_user_id="reader", tenant_id="tenant", permissions=frozenset(),
        ),
    )

    assert decoded["ledger"][1]["detail_selection"] == {
        "candidate_id": "unexpected-detail-candidate"
    }
    assert build_web_evidence_bundle(decoded)["status"] == "needs_review"


@pytest.mark.asyncio
@pytest.mark.parametrize("menu_limit", [None, 10, 12])
async def test_list_for_task_rechecks_web_v2_run_session_and_tenant_scope(menu_limit) -> None:
    connection = BundleConnection(menu_limit=menu_limit)
    scope = AccessScope(
        actor_user_id="00000000-0000-0000-0000-000000000101",
        tenant_id="00000000-0000-0000-0000-000000000201",
        permissions=frozenset({"taskObjects:read:tenant"}),
    )

    result = await PostgresWebCaptureEvidenceRepository(pool=Pool(connection)).list_for_task(
        task_id="task-1",
        agent_run_id="run-1",
        access_scope=scope,
    )

    assert result is not None
    assert result["task_id"] == "task-1"
    assert result["menu_limit"] == menu_limit
    assert result["ledger"][0]["menu_entry_id"] == "home"
    assert result["captures"][0]["capture_id"] == "capture-1"
    assert result["normalization_version"] == "web-page-type-assessment.v3"
    qualification_sql = connection.queries[0]
    assert "task.target_platform = 'web'" in qualification_sql
    assert "task.task_config->>'workflow_version'" in qualification_sql
    assert (
        "run.input_context #>> '{task_config,_task_target,platform}'"
        in qualification_sql
    )
    assert "run.agent_type = 'main'" in qualification_sql
    assert "COUNT(*)" in qualification_sql
    assert "budget.surface_kind = 'web'" in qualification_sql
    assert "budget.session_owner = 'web-v2-workflow'" in qualification_sql
    assert "session.surface_kind = 'web'" in qualification_sql
    assert "session.session_owner = 'web-v2-workflow'" in qualification_sql
    assert "home_entry.normalization_version" in qualification_sql
    assert "home_entry.status = 'home_baseline'" in qualification_sql
    assert "task.tenant_id" in qualification_sql
    assert "task.submitter_id" in qualification_sql
    assert scope.tenant_id in connection.args[0]
    assert scope.actor_user_id in connection.args[0]
    all_sql = "\n".join(connection.queries).casefold()
    for forbidden in (
        "storage_path",
        "result_payload",
        "idempotency_key",
        "lease_token",
        "lease_owner",
        "task_config as",
    ):
        assert forbidden not in all_sql
    # 仅允许在数据库中抽取固定操作语义，不读出完整 intent 或参数对象。
    assert "action.intent_payload->>'action_type' as action_type" in all_sql
    assert "action.intent_payload," not in all_sql
    assert "action.intent_payload as" not in all_sql


@pytest.mark.asyncio
async def test_list_for_task_stops_before_evidence_reads_when_not_web_v2() -> None:
    connection = BundleConnection(qualification=False)
    scope = AccessScope(
        actor_user_id="00000000-0000-0000-0000-000000000101",
        tenant_id="00000000-0000-0000-0000-000000000201",
        permissions=frozenset(),
    )

    result = await PostgresWebCaptureEvidenceRepository(pool=Pool(connection)).list_for_task(
        task_id="task-1",
        agent_run_id="run-1",
        access_scope=scope,
    )

    assert result is None
    assert connection.fetch_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", ["web-page-type-assessment.v2", "web-page-type-assessment.v3"])
async def test_page_type_assessment_updates_capture_metadata_and_assessment_atomically(schema_version) -> None:
    connection = PageTypePersistenceConnection()

    repository = PostgresWebCaptureEvidenceRepository(pool=Pool(connection), assessment_schema_version=schema_version)
    result = await repository.persist_web_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-1",
        canonical_page_type="video_list",
        audit_metadata={
            "web_page_type_assessment": {
                "version": schema_version,
                "classification_status": "classified",
            }
        },
        assessment_payload={
            "assessment_id": "page-type-assessment:web-1",
            "page_type": "video_list",
            "status": "covered",
            "reason_code": "web_page_type_recognized",
            "catalog_version": "catalog-v1",
            "catalog_sha256": "a" * 64,
            "schema_version": schema_version,
            "classification_status": "classified",
        },
    )

    assert result == "web-assessment-1"
    assert connection.transactions == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert "UPDATE qc_page_capture AS page" in connection.query
    assert connection.args[4] == "video_list"
    assert "web_page_type_assessment" in connection.args[5]
    assert "qc_h5_page_type_assessment" in connection.query
    assert connection.args[17] == schema_version
    assert "stored.schema_version = $18::text" in connection.query
    assert "task.target_platform = 'web'" in connection.query
    assert "run.agent_type = 'main'" in connection.query
    assert "latest.id" in connection.query
    assert "session.session_owner = 'web-v2-workflow'" in connection.query
    assert "session.status = 'active'" in connection.query
    assert "session.session_id = ('web:' || run.id::text)" in connection.query
    assert "capture.surface_session_id = $3" in connection.query
    assert "capture.capture_status = 'captured'" in connection.query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assessment", "expected_capture_detection"),
    (
        (
            WebPageAssessment(
                page_type="video_list",
                confidence=0.93,
                evidence=("可信视频列表结构",),
            ),
            "recognized",
        ),
        (
            WebPageAssessment(
                page_type=None,
                raw_page_type="profile",
                confidence=0.81,
                evidence=("目录外页面结构",),
            ),
            "ambiguous",
        ),
        (
            WebPageAssessment(
                page_type=None,
                raw_page_type="content_list",
                confidence=0.42,
                evidence=("页面结构需要复核",),
                needs_review_reason="WEB_PAGE_TYPE_LOW_CONFIDENCE",
            ),
            "ambiguous",
        ),
    ),
)
async def test_real_assessment_syncs_capture_detection_contract_for_every_status(
    assessment: WebPageAssessment,
    expected_capture_detection: str,
) -> None:
    """Assessment 状态必须映射到共享 Capture 检测枚举且同事务写入。"""

    connection = PageTypePersistenceConnection()

    await _persist_real_assessment(connection, assessment)

    assert connection.args[14] == "unknown"
    assert connection.args[15] == pytest.approx(assessment.confidence)
    assert connection.args[16] == expected_capture_detection
    compact_sql = " ".join(connection.query.split())
    assert "$13, $14, $15::text, $9" in compact_sql
    assert "page_state = $15::text" in compact_sql
    assert "detection_status = $17::text" in compact_sql
    assert "detection_confidence = $16::double precision" in compact_sql
    assert "'assessment_detection_status', $13::text" in compact_sql
    assert "'confidence_source', 'web_page_type_assessment'" in compact_sql


@pytest.mark.asyncio
async def test_unknown_page_type_persists_terminal_unrecognized_assessment() -> None:
    connection = PageTypePersistenceConnection()
    payload = {
        "assessment_id": "web-page-type-unrecognized:capture-1",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "page_type": "__unclassified__",
        "proposed_page_type": None,
        "detection_status": "needs_review",
        "classification_status": "needs_review",
        "status": "evidence_insufficient",
        "reason_code": "web_page_type_unrecognized",
        "schema_version": "web-page-type-assessment.v3",
        "assessed_at": "2026-09-02T08:00:00+00:00",
        "page_capture_ids": ["capture-1"],
        "evidence_artifact_ids": ["shot-1", "dom-1", "text-1"],
        "page_states": ["web_page_ready_baseline"],
    }

    result = await PostgresWebCaptureEvidenceRepository(
        pool=Pool(connection)
    ).persist_web_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-1",
        canonical_page_type=None,
        audit_metadata={
            "web_page_type_assessment": {
                "version": "web-page-type-assessment.v3",
                "classification_status": "needs_review",
            }
        },
        assessment_payload=payload,
    )

    assert result == "web-assessment-1"
    assert connection.args[4] is None
    assert connection.args[12] == "needs_review"
    assert connection.args[13] == "__unclassified__"
    assert json.loads(connection.args[10]) == payload
    assert "$13, $14, $15::text" in connection.query
    assert "WHERE $14::text IS NOT NULL" in connection.query
    assert "ON CONFLICT (page_capture_id, schema_version) DO NOTHING" in connection.query
    assert "existing_assessment.payload_hash = $12" in connection.query


@pytest.mark.asyncio
async def test_v4_canonical_unknown_remains_needs_review_in_database_columns() -> None:
    from services.api.tests.test_web_page_type_assessment import _catalog_identity

    connection = PageTypePersistenceConnection()
    identity = _catalog_identity()
    payload = {
        "assessment_id": "web-page-type-unknown:capture-1",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "session_id": "web:run-1",
        "page_type": "unknown",
        "final_page_type": "unknown",
        "proposed_page_type": "unknown",
        "raw_page_type": "unknown",
        "detection_status": "needs_review",
        "classification_status": "needs_review",
        "coverage_group": "neither",
        "status": "evidence_insufficient",
        "needs_review": True,
        "reason_code": "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT",
        "reason_codes": ["WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT"],
        "assessed_at": "2026-09-13T00:00:00+00:00",
        "confidence": 0.7,
        "schema_version": "web-page-type-assessment.v4",
        "page_capture_ids": ["capture-1"],
        "evidence_artifact_ids": ["shot-1", "dom-1", "text-1"],
        "primary_evidence_artifact_ids": ["shot-1", "dom-1", "text-1"],
        "supplemental_screenshot_artifact_ids": [],
        "supplemental_evidence_status": "not_attempted",
        "supplemental_evidence_reason_code": None,
        "catalog_version": identity.catalog_version,
        "catalog_sha256": identity.catalog_sha256,
        "page_states": ["stable"],
        "web_capture_stage": "web_page_ready_baseline",
        "page_role": "home",
        "attempt_count": 1,
    }

    await PostgresWebCaptureEvidenceRepository(
        pool=Pool(connection),
        assessment_schema_version="web-page-type-assessment.v4",
        catalog_identity=identity,
    ).persist_web_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-1",
        canonical_page_type="unknown",
        audit_metadata={
            "web_page_type_assessment": {
                **payload,
                "version": "web-page-type-assessment.v4",
                "evidence": {"artifact_ids": ["shot-1", "dom-1", "text-1"]},
            }
        },
        assessment_payload=payload,
    )

    assert connection.args[4] == "unknown"
    assert connection.args[12] == "needs_review"
    assert connection.args[13] == "unknown"
    assert connection.args[15] == payload["confidence"]
    assert connection.args[16] == "ambiguous"


@pytest.mark.asyncio
async def test_review_required_page_type_preserves_detection_status_and_reason() -> None:
    connection = PageTypePersistenceConnection()
    payload = {
        "assessment_id": "web-page-type-review:capture-1",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "page_type": "content_list",
        "detection_status": "needs_review",
        "status": "evidence_insufficient",
        "reason_code": "WEB_PAGE_TYPE_LOW_CONFIDENCE",
        "schema_version": "web-page-type-assessment.v3",
        "classification_status": "needs_review",
        "assessed_at": "2026-09-02T08:00:00+00:00",
        "page_capture_ids": ["capture-1"],
        "evidence_artifact_ids": ["shot-1", "dom-1", "text-1"],
        "page_states": ["web_page_ready_baseline"],
    }

    result = await PostgresWebCaptureEvidenceRepository(
        pool=Pool(connection)
    ).persist_web_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-1",
        canonical_page_type=None,
        audit_metadata={
            "web_page_type_assessment": {
                "version": "web-page-type-assessment.v3",
                "classification_status": "needs_review",
            }
        },
        assessment_payload=payload,
    )

    assert result == "web-assessment-1"
    assert connection.args[4] is None
    assert connection.args[12] == "needs_review"
    assert connection.args[13] == "content_list"
    assert json.loads(connection.args[9]) == ["WEB_PAGE_TYPE_LOW_CONFIDENCE"]
    assert json.loads(connection.args[10]) == payload


@pytest.mark.asyncio
async def test_review_without_raw_type_uses_noncanonical_storage_placeholder() -> None:
    connection = PageTypePersistenceConnection()
    payload = {
        "assessment_id": "web-page-type-review:capture-1",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "page_type": "__unclassified__",
        "proposed_page_type": None,
        "detection_status": "needs_review",
        "status": "evidence_insufficient",
        "reason_code": "WEB_PAGE_TYPE_UNSUPPORTED",
        "schema_version": "web-page-type-assessment.v3",
        "classification_status": "needs_review",
        "assessed_at": "2026-09-02T08:00:00+00:00",
        "page_capture_ids": ["capture-1"],
        "evidence_artifact_ids": ["shot-1", "dom-1", "text-1"],
        "page_states": ["web_page_ready_baseline"],
    }

    result = await PostgresWebCaptureEvidenceRepository(
        pool=Pool(connection)
    ).persist_web_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-1",
        canonical_page_type=None,
        audit_metadata={
            "web_page_type_assessment": {
                "version": "web-page-type-assessment.v3",
                "classification_status": "needs_review",
            }
        },
        assessment_payload=payload,
    )

    assert result == "web-assessment-1"
    assert connection.args[4] is None
    assert connection.args[12] == "needs_review"
    assert connection.args[13] == "__unclassified__"
    assert json.loads(connection.args[10])["proposed_page_type"] is None


def test_page_type_assessment_conflict_sql_allows_only_same_hash_replay() -> None:
    compact_sql = " ".join(_PERSIST_WEB_PAGE_TYPE_ASSESSMENT_SQL.split())
    assert "ON CONFLICT (page_capture_id, schema_version) DO NOTHING" in compact_sql
    assert "existing_assessment.payload_hash = $12" in compact_sql
    assert "ELSE existing.row_version + 1" not in compact_sql
    assert compact_sql.index("upserted_assessment AS (") < compact_sql.index(
        "updated_capture AS ("
    )


@pytest.mark.asyncio
async def test_recognized_audit_drift_is_rejected_before_capture_update() -> None:
    connection = StatefulPageTypePersistenceConnection()
    await _persist_real_assessment(
        connection,
        WebPageAssessment(
            page_type="video_list",
            confidence=0.93,
            evidence=("可信视频列表结构",),
        ),
    )
    before_metadata = deepcopy(connection.capture_metadata)
    before_assessment = deepcopy(connection.assessment)

    with pytest.raises(WebCapturePageTypePersistenceError, match="Assessment"):
        await _persist_real_assessment(
            connection,
            WebPageAssessment(
                page_type="video_list",
                confidence=0.88,
                evidence=("另一组视频列表证据",),
            ),
        )

    assert connection.capture_update_count == 1
    assert connection.capture_metadata == before_metadata
    assert connection.assessment == before_assessment


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_assessment", "changed_assessment"),
    (
        (
            WebPageAssessment(
                page_type=None,
                raw_page_type="profile",
                confidence=0.93,
                evidence=("目录外个人页证据 A",),
            ),
            WebPageAssessment(
                page_type=None,
                raw_page_type="profile",
                confidence=0.88,
                evidence=("目录外个人页证据 B",),
            ),
        ),
        (
            WebPageAssessment(
                page_type=None,
                raw_page_type="content_list",
                confidence=0.42,
                evidence=("分类复核证据 A",),
                needs_review_reason="WEB_PAGE_TYPE_LOW_CONFIDENCE",
            ),
            WebPageAssessment(
                page_type=None,
                raw_page_type="content_list",
                confidence=0.51,
                evidence=("分类复核证据 B",),
                needs_review_reason="WEB_PAGE_TYPE_LOW_CONFIDENCE",
            ),
        ),
    ),
)
async def test_noncanonical_audit_drift_is_rejected_before_capture_update(
    first_assessment: WebPageAssessment,
    changed_assessment: WebPageAssessment,
) -> None:
    connection = StatefulPageTypePersistenceConnection()
    await _persist_real_assessment(connection, first_assessment)
    before_metadata = deepcopy(connection.capture_metadata)
    before_assessment = deepcopy(connection.assessment)

    with pytest.raises(WebCapturePageTypePersistenceError, match="Assessment"):
        await _persist_real_assessment(connection, changed_assessment)

    assert connection.capture_update_count == 1
    assert connection.capture_metadata == before_metadata
    assert connection.assessment == before_assessment


@pytest.mark.asyncio
async def test_exact_assessment_replay_does_not_issue_second_capture_update() -> None:
    connection = StatefulPageTypePersistenceConnection()
    assessment = WebPageAssessment(
        page_type="video_list",
        confidence=0.93,
        evidence=("可信视频列表结构",),
    )

    first = await _persist_real_assessment(connection, assessment)
    before_metadata = deepcopy(connection.capture_metadata)
    second = await _persist_real_assessment(connection, assessment)

    assert first.assessment_record_id == second.assessment_record_id
    assert connection.capture_update_count == 1
    assert connection.capture_metadata == before_metadata
    assert connection.commits == 2
    assert connection.rollbacks == 0


@pytest.mark.asyncio
async def test_recognized_to_unknown_conflict_is_rejected_and_rolls_back_metadata() -> None:
    connection = StatefulPageTypePersistenceConnection()
    await _persist_page_type_variant(
        connection,
        raw_page_type="video_list",
        canonical_page_type="video_list",
    )
    before_page_type = connection.capture_page_type
    before_metadata = deepcopy(connection.capture_metadata)
    before_assessment = deepcopy(connection.assessment)

    with pytest.raises(WebCapturePageTypePersistenceError, match="Assessment"):
        await _persist_page_type_variant(
            connection,
            raw_page_type="unknown",
            canonical_page_type=None,
        )

    assert connection.capture_page_type == before_page_type
    assert connection.capture_metadata == before_metadata
    assert connection.assessment == before_assessment
    assert connection.commits == 1
    assert connection.rollbacks == 1


@pytest.mark.asyncio
async def test_unknown_raw_type_change_is_rejected_and_rolls_back_metadata() -> None:
    connection = StatefulPageTypePersistenceConnection()
    await _persist_page_type_variant(
        connection,
        raw_page_type="unknown_a",
        canonical_page_type=None,
    )
    before_metadata = deepcopy(connection.capture_metadata)
    before_assessment = deepcopy(connection.assessment)

    with pytest.raises(WebCapturePageTypePersistenceError, match="Assessment"):
        await _persist_page_type_variant(
            connection,
            raw_page_type="unknown_b",
            canonical_page_type=None,
        )

    assert connection.capture_page_type is None
    assert connection.capture_metadata == before_metadata
    assert connection.assessment == before_assessment
    assert connection.commits == 1
    assert connection.rollbacks == 1


@pytest.mark.asyncio
async def test_same_assessment_hash_replay_returns_original_immutable_record() -> None:
    connection = StatefulPageTypePersistenceConnection()

    first = await _persist_page_type_variant(
        connection,
        raw_page_type="unknown_a",
        canonical_page_type=None,
    )
    second = await _persist_page_type_variant(
        connection,
        raw_page_type="unknown_a",
        canonical_page_type=None,
    )

    assert first == second
    assert connection.assessment is not None
    assert connection.assessment["row_version"] == 1
    assert connection.commits == 2
    assert connection.rollbacks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value", "required_sql"),
    (
        ("task_id", "task-other", "task.id = $1::uuid"),
        ("agent_run_id", "run-other", "run.id = $2::uuid"),
        ("session_id", "web:run-other", "session.session_id = ('web:' || run.id::text)"),
        ("capture_id", "capture-other", "capture.id = $4::uuid"),
    ),
)
async def test_persist_page_type_assessment_rejects_unqualified_web_v2_identity(
    field_name: str,
    value: str,
    required_sql: str,
) -> None:
    connection = PageTypePersistenceConnection()
    context = {
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "session_id": "web:run-1",
        "capture_id": "capture-1",
    }
    context[field_name] = value

    with pytest.raises(WebCapturePageTypePersistenceError, match="身份不匹配或状态无效"):
        repository = PostgresWebCaptureEvidenceRepository(pool=Pool(connection))
        await repository.persist_web_page_type_assessment(
            **context,
            canonical_page_type="video_list",
            audit_metadata={
                "web_page_type_assessment": {
                    "version": "web-page-type-assessment.v3",
                    "classification_status": "classified",
                }
            },
            assessment_payload={
                "assessment_id": "page-type-assessment:web-1",
                "page_type": "video_list",
                "status": "covered",
                "reason_code": "web_page_type_recognized",
                "catalog_version": "catalog-v1",
                "catalog_sha256": "a" * 64,
                "schema_version": "web-page-type-assessment.v3",
                "classification_status": "classified",
            },
        )

    assert connection.transactions == 1
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert required_sql in connection.query


@pytest.mark.asyncio
async def test_persist_page_type_assessment_rejects_inactive_session_with_rollback() -> None:
    connection = PageTypePersistenceConnection(session_active=False)

    with pytest.raises(WebCapturePageTypePersistenceError, match="身份不匹配或状态无效"):
        await _persist_recognized_page_type(connection)

    assert "session.status = 'active'" in connection.query
    assert connection.commits == 0
    assert connection.rollbacks == 1


@pytest.mark.asyncio
async def test_assessment_conflict_rolls_back_capture_page_type_and_metadata_update() -> None:
    connection = PageTypePersistenceConnection(assessment_conflict=True)

    with pytest.raises(WebCapturePageTypePersistenceError, match="未完整写入 Assessment"):
        await _persist_recognized_page_type(connection)

    assert connection.transactions == 1
    assert connection.commits == 0
    assert connection.rollbacks == 1


async def _persist_recognized_page_type(connection: PageTypePersistenceConnection) -> str | None:
    repository = PostgresWebCaptureEvidenceRepository(pool=Pool(connection))
    return await repository.persist_web_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-1",
        canonical_page_type="video_list",
        audit_metadata={
            "web_page_type_assessment": {
                "version": "web-page-type-assessment.v3",
                "classification_status": "classified",
            }
        },
        assessment_payload={
            "assessment_id": "page-type-assessment:web-1",
            "page_type": "video_list",
            "status": "covered",
            "reason_code": "web_page_type_recognized",
            "catalog_version": "catalog-v1",
            "catalog_sha256": "a" * 64,
            "schema_version": "web-page-type-assessment.v3",
            "classification_status": "classified",
        },
    )


async def _persist_page_type_variant(
    connection: StatefulPageTypePersistenceConnection,
    *,
    raw_page_type: str,
    canonical_page_type: str | None,
) -> str | None:
    recognized = canonical_page_type is not None
    payload = {
        "assessment_id": (
            "page-type-assessment:web-1"
            if recognized
            else "web-page-type-unrecognized:capture-1"
        ),
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "page_type": canonical_page_type if recognized else raw_page_type,
        "proposed_page_type": raw_page_type,
        "detection_status": "recognized" if recognized else "needs_review",
        "classification_status": "classified" if recognized else "needs_review",
        "status": "covered" if recognized else "evidence_insufficient",
        "reason_code": (
            "web_page_type_recognized"
            if recognized
            else "web_page_type_unrecognized"
        ),
        "schema_version": "web-page-type-assessment.v3",
        "assessed_at": "2026-09-02T08:00:00+00:00",
        "page_capture_ids": ["capture-1"],
        "evidence_artifact_ids": ["shot-1", "dom-1", "text-1"],
        "page_states": ["web_page_ready_baseline"],
    }
    return await PostgresWebCaptureEvidenceRepository(
        pool=Pool(connection)
    ).persist_web_page_type_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture_id="capture-1",
        canonical_page_type=canonical_page_type,
        audit_metadata={
            "web_page_type_assessment": {
                "version": "web-page-type-assessment.v3",
                "classification_status": "classified" if recognized else "needs_review",
            }
        },
        assessment_payload=payload,
    )


def _real_assessment_capture() -> WebReadyPageCapture:
    return WebReadyPageCapture(
        capture_id="capture-1",
        web_capture_stage="web_page_ready_baseline",
        screenshot_artifact_id="shot-1",
        dom_artifact_id="dom-1",
        visible_text_artifact_id="text-1",
        page_role="menu",
        captured_at=datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
        structure_facts=WebPageStructureFacts(
            navigation_element_count=1,
            repeated_link_count=2,
            video_element_count=1,
            single_subject_element_count=0,
            dom_tokens=("video-list",),
            primary_detail_link_count=2,
            repeated_primary_card_count=2,
            media_card_count=2,
        ),
    )


async def _persist_real_assessment(
    connection: StatefulPageTypePersistenceConnection,
    assessment: WebPageAssessment,
):
    repository = PostgresWebCaptureEvidenceRepository(pool=Pool(connection))
    return await WebPageTypeAssessmentRecorder(
        repository=repository, assessment_schema_version="web-page-type-assessment.v3"
    ).persist(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        capture=_real_assessment_capture(),
        assessment=assessment,
    )


@pytest.mark.asyncio
async def test_v4_age_gate_records_immutable_lifecycle_without_business_replacement():
    from dataclasses import replace

    from services.api.app.application.web_age_gate import WebAgeGateAssessment
    from services.api.app.application.web_page_type_assessment import WebAgeGatePageTypeRecorder
    from services.api.tests.test_web_age_gate import _evidence
    from services.api.tests.test_web_page_type_assessment import _catalog_identity

    connection = StatefulPageTypePersistenceConnection()
    identity = _catalog_identity()
    repository = PostgresWebCaptureEvidenceRepository(
        pool=Pool(connection), assessment_schema_version="web-page-type-assessment.v4",
        catalog_identity=identity)
    recorder = WebAgeGatePageTypeRecorder(repository=repository, catalog_identity=identity)
    evidence = replace(_evidence(capture_id="capture-1"), captured_at=datetime(2026, 9, 13, tzinfo=UTC))
    assessment = WebAgeGateAssessment(gate_present=True, confidence=0.75, reason="年龄门",
        confirmation_candidate_id=evidence.candidates[0].candidate_id, human_verification_present=False)
    first = await recorder.record(evidence=evidence, assessment=assessment)
    second = await recorder.record(evidence=evidence, assessment=assessment)
    assert first.assessment_record_id == second.assessment_record_id
    assert connection.capture_update_count == 1
    assert connection.capture_page_type == "age_gate"
    assert connection.assessment["payload"]["coverage_group"] == "lifecycle"
    with pytest.raises(WebCapturePageTypePersistenceError):
            await WebPageTypeAssessmentRecorder(
                repository=repository, catalog_identity=identity
            ).persist(
            task_id="task-1", agent_run_id="run-1", session_id="web:run-1",
            capture=_real_assessment_capture(),
            assessment=WebPageAssessment(page_type="home", confidence=0.9, evidence=("首页",)))
    assert connection.capture_page_type == "age_gate"
    assert connection.capture_update_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('old_catalog', [False, True])
async def test_collection_persists_only_with_frozen_v11_catalog(old_catalog):
    from dataclasses import replace

    from services.api.tests.test_web_page_type_assessment import _catalog_identity

    connection = StatefulPageTypePersistenceConnection()
    identity = _catalog_identity()
    if old_catalog:
        identity = replace(identity, catalog_version='2026-09-15.web.v10')
    repository = PostgresWebCaptureEvidenceRepository(pool=Pool(connection),
        assessment_schema_version='web-page-type-assessment.v4', catalog_identity=identity)
    recorder = WebPageTypeAssessmentRecorder(repository=repository, catalog_identity=identity)
    call = recorder.persist(task_id='task-1', agent_run_id='run-1', session_id='web:run-1',
        capture=_real_assessment_capture(), assessment=WebPageAssessment(
            page_type='collection_page', confidence=.85, evidence=('集合目录主体',)))
    if old_catalog:
        with pytest.raises(ValueError, match='对应版本闭集'):
            await call
        assert connection.capture_update_count == 0
    else:
        await call
        assert connection.capture_page_type == 'collection_page'
        assert connection.assessment['payload']['coverage_group'] == 'business'
