"""真实 PG 验证分类复用的来源认证、操作标记与正式库存。"""

import json
import os
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.api.app.application.web_logical_target import WebLogicalTargetClassifier
from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_page_type_assessment import WebPageTypeAssessmentRecorder
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_completion_gate_sql import (
    WEB_COMPLETION_GATE_INVENTORY_CTES_SQL,
)
from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
    PostgresWebCoverageSnapshotRepository,
)
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.app.infrastructure.db.web_logical_target_repository import (
    PostgresWebLogicalTargetRepository,
)
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
)
from services.api.app.infrastructure.db.web_runtime_stage_evidence_repository import (
    PostgresWebRuntimeStageEvidenceRepository,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _command,
    _site,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _upgrade_isolated_postgres as _upgrade_isolated_postgres,
)
from services.api.tests.test_web_logical_target_postgres import seed_material, target_database
from services.api.tests.test_web_page_type_assessment import _capture, _catalog_identity
from services.api.tests.test_web_run_stage_checkpoint_postgres import seed_current

pytestmark = [
    pytest.mark.skipif(not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="需显式隔离 PostgreSQL"),
    pytest.mark.usefixtures("_upgrade_isolated_postgres"),
]


async def classified_reuse(
    connection,
    pool,
    ids,
    *,
    original=None,
    with_budget=False,
    url="https://example.test/topics/",
    fresh_url=None,
    assessment=None,
):
    await seed_current(connection, ids)
    fresh_url = fresh_url or url
    scope = dict(task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"])
    if original is None:
        original = await seed_material(
            connection, pool, ids, url=url, sequence=1, with_material=False
        )
        targets = PostgresWebLogicalTargetRepository(pool=pool)
        await targets.claim(**scope, source_capture_id=original, url=url)
        assessment = assessment or WebPageAssessment(
            page_type="collection_page", confidence=0.95, evidence=("原集合页",)
        )
        await WebPageTypeAssessmentRecorder(
            repository=PostgresWebCaptureEvidenceRepository(
                pool=pool,
                catalog_identity=_catalog_identity(),
                assessment_schema_version="web-page-type-assessment.v4",
            ),
            catalog_identity=_catalog_identity(),
        ).persist(
            **scope,
            capture=replace(_capture(), capture_id=original, url=url, page_role="menu"),
            assessment=assessment,
        )
        await targets.materialize(
            **scope, capture_id=original, url=url, assessment=asdict(assessment)
        )
    reservation = None
    if with_budget:
        budget = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        await budget.bootstrap(
            _command(ids), _site(), catalog_version="reuse-contract", catalog_sha256="b" * 64
        )
        reservation = await budget.claim_menu(
            command=_command(ids), entry=_site().entries[1], source_capture_id=original
        )
    fresh = await seed_material(
        connection,
        pool,
        ids,
        url=fresh_url,
        sequence=2,
        with_material=False,
        source_capture_id=original,
        intent_id=reservation.intent_id if reservation else None,
    )

    class Inner:
        async def classify(self, capture):
            raise AssertionError("复用原评估不得再次调用模型或发布")

    class Ready:
        async def read(self, **kwargs):
            return replace(_capture(), capture_id=fresh, url=fresh_url)

    runtime = SimpleNamespace(
        page_type_classifier=WebLogicalTargetClassifier(
            classifier=Inner(), repository=PostgresWebLogicalTargetRepository(pool=pool), **scope
        ),
        stage_ready_capture_reader=Ready(),
    )
    evidence = PostgresWebRuntimeStageEvidenceRepository(
        task_id=ids["task"],
        run_id=ids["run"],
        session_id=ids["session"],
        pool=pool,
    )
    checkpoints = PostgresWebRunStageCheckpointRepository(pool=pool)
    await checkpoints.create(
        WebStageCursor.start(
            run_id=ids["run"],
            logical_page_key="menu:topics",
            stage=WebRunStage.CLASSIFICATION,
            cursor_payload={
                "capture_id": fresh,
                "session_id": ids["session"],
                "finalization_epoch": 0,
                **(
                    {
                        "menu_ledger_id": _site().entries[1].entry_id,
                        "menu_navigation_capture_id": fresh,
                    }
                    if with_budget
                    else {}
                ),
            },
        )
    )
    claimed = await checkpoints.claim(
        run_id=ids["run"],
        logical_page_key="menu:topics",
        expected_row_version=0,
        worker_id="reuse-contract",
    )
    effects = WebRuntimeStageEffects(runtime=runtime, evidence=evidence, command=_command(ids))
    result = await effects.execute(claimed)
    return original, fresh, evidence, effects, claimed, result


async def test_real_classification_marker_recovers_reuse_without_new_assessment():
    async with target_database() as (connection, pool, other, ids):
        original, fresh, evidence, effects, claimed, result = await classified_reuse(
            connection, pool, ids
        )
        assert result.reused_capture_id == original
        assert result.cursor_payload["capture_id"] == fresh
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_h5_page_type_assessment WHERE page_capture_id=$1::uuid",
                fresh,
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT page_type FROM qc_page_capture WHERE id=$1::uuid", fresh
            )
            is None
        )
        restored = await evidence.find_completed(claimed, operation="classification")
        assert restored.reused_capture_id == original
        # 恢复读取允许原动作帧已非 current，但原评估血缘必须仍有效。
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
            original,
            ids["session"],
        )
        assert (await effects.find_completed(claimed)).reused_capture_id == original
        await connection.execute(
            "UPDATE qc_page_capture SET metadata=metadata - 'web_page_type_assessment' "
            "WHERE id=$1::uuid",
            original,
        )
        with pytest.raises(Exception, match="WEB_CLASSIFICATION_REUSE_INVALID"):
            await effects.find_completed(claimed)


async def add_inspection_artifacts(connection, ids):
    captures = await connection.fetch(
        "SELECT id FROM qc_page_capture WHERE task_id=$1::uuid", ids["task"]
    )
    for capture in captures:
        capture_id = str(capture["id"])
        for kind, mime in (
            ("screenshot", "image/png"),
            ("dom", "text/html"),
            ("visible_text", "text/plain"),
        ):
            await connection.execute(
                "INSERT INTO qc_evidence_artifact(task_id,page_capture_id,artifact_type,"
                "source,storage_path,content_type,checksum,size_bytes) "
                "VALUES($1::uuid,$2::uuid,$3,'baseline',$4,$5,$6,1)",
                ids["task"],
                capture_id,
                kind,
                f"reuse-test/{capture_id}/{kind}",
                mime,
                "sha256:" + "a" * 64,
            )
        await connection.execute(
            "INSERT INTO qc_evidence_manifest(manifest_id,manifest_version,task_id,"
            "session_id,capture_id,protocol_version,manifest_hash,payload_hash,"
            "quality_status,manifest_payload,created_at) "
            "VALUES($1,1,$2::uuid,$3,$4::uuid,'evidence-manifest.v1',$5,$5,'pass','{}'::jsonb,now())",
            "reuse-manifest:" + capture_id,
            ids["task"],
            ids["session"],
            capture_id,
            "sha256:" + "a" * 64,
        )


async def prepare_formal_inventory(connection, pool, ids, evidence, claimed):
    await WebRuntimeStageFrontier(
        ledger=PostgresWebExplorationBudgetLedgerRepository(pool=pool),
        candidate_source=None,
        navigator=None,
        site=_site(),
        evidence=evidence,
    ).reconcile(_command(ids), replace(claimed, current_stage=WebRunStage.EVIDENCE_RECONCILIATION))
    await add_inspection_artifacts(connection, ids)
    await connection.execute(
        "UPDATE qc_task SET task_config=task_config || $2::jsonb, "
        "finalization_agent_run_id=$3::uuid WHERE id=$1::uuid",
        ids["task"],
        json.dumps(
            {
                "execution_mode": "full_inspection",
                "web_inspection_notification_mode": "enforced",
            }
        ),
        ids["run"],
    )


async def completion_inventory(connection, ids):
    return await connection.fetchrow(
        "WITH target_task AS (SELECT * FROM qc_task WHERE id=$1::uuid), "
        + WEB_COMPLETION_GATE_INVENTORY_CTES_SQL
        + " SELECT classification.*, publication.missing_publication_count "
        "FROM web_classification_gate classification"
        " CROSS JOIN web_publication_gate publication",
        ids["task"],
    )


async def test_formal_inventory_uses_original_without_own_menu_and_preserves_publication_gate():
    async with target_database() as (connection, pool, other, ids):
        original, fresh, evidence, effects, claimed, result = await classified_reuse(
            connection, pool, ids, with_budget=True
        )
        await prepare_formal_inventory(connection, pool, ids, evidence, claimed)
        state = await PostgresWebCoverageSnapshotRepository(pool=pool).load_reconciliation_state(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )
        assert state.missing_assessment_count == 0
        assert state.missing_publication_count == 1
        assert state.observed_page_types == ("collection_page",)
        gate = await completion_inventory(connection, ids)
        assert gate["missing_assessment_count"] == 0
        assert gate["missing_publication_count"] == 1
        # 正式通知仍绑定原评估；存在和删除都必须反映到两个验收门禁。
        await connection.execute(
            "INSERT INTO qc_h5_inspection_publication("
            "id,task_id,source_agent_run_id,page_capture_id,page_type_assessment_id,"
            "assessment_hash,ruleset_id,ruleset_version,finalization_epoch,input_hash,mode) "
            "SELECT $1::uuid,task_id,agent_run_id,page_capture_id,assessment_record_id,"
            "payload_hash,$3::uuid,1,0,$4,'enforced' FROM qc_h5_page_type_assessment "
            "WHERE page_capture_id=$2::uuid",
            str(uuid4()),
            original,
            ids["ruleset"],
            "reuse-publication:" + original,
        )
        state = await PostgresWebCoverageSnapshotRepository(pool=pool).load_reconciliation_state(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )
        assert state.missing_publication_count == 0
        assert (await completion_inventory(connection, ids))["missing_publication_count"] == 0
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_h5_inspection_publication WHERE page_capture_id=$1::uuid",
                fresh,
            )
            == 0
        )
        await connection.execute(
            "DELETE FROM qc_h5_inspection_publication WHERE page_capture_id=$1::uuid",
            original,
        )
        state = await PostgresWebCoverageSnapshotRepository(pool=pool).load_reconciliation_state(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )
        assert state.missing_publication_count == 1
        assert (await completion_inventory(connection, ids))["missing_publication_count"] == 1
        # 缺失原评估不可因复用终态而静默跳过。
        await connection.execute(
            "DELETE FROM qc_h5_page_type_assessment WHERE page_capture_id=$1::uuid", original
        )
        state = await PostgresWebCoverageSnapshotRepository(pool=pool).load_reconciliation_state(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )
        assert state.missing_assessment_count == 1
        assert (await completion_inventory(connection, ids))["missing_assessment_count"] == 1


@pytest.mark.parametrize(
    "corruption",
    ["source_url", "page_version", "common_version", "marker_url", "page_type", "epoch", "claimed"],
)
async def test_source_identity_drift_fails_recovery_and_formal_reuse(corruption):
    async with target_database() as (connection, pool, other, ids):
        original, fresh, evidence, effects, claimed, result = await classified_reuse(
            connection, pool, ids, with_budget=True
        )
        await prepare_formal_inventory(connection, pool, ids, evidence, claimed)
        if corruption == "source_url":
            await connection.execute(
                "UPDATE qc_page_capture SET url='https://example.test/other' WHERE id=$1::uuid",
                original,
            )
        elif corruption == "page_type":
            await connection.execute(
                "UPDATE qc_page_capture SET page_type=NULL WHERE id=$1::uuid", original
            )
        elif corruption == "epoch":
            await connection.execute(
                "UPDATE qc_capture SET metadata=metadata || "
                "'{\"finalization_epoch\":1}'::jsonb WHERE id=$1::uuid",
                original,
            )
        elif corruption == "claimed":
            await connection.execute(
                "UPDATE qc_web_logical_target SET state='claimed',assessment=NULL,"
                "assessment_capture_id=NULL WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
        elif corruption == "marker_url":
            await connection.execute(
                "UPDATE qc_surface_phase_run SET context_payload=context_payload || "
                '\'{"reused_source_url_sha256":"invalid"}\'::jsonb '
                "WHERE source_capture_id=$1::uuid",
                fresh,
            )
        else:
            table = "qc_page_capture" if corruption == "page_version" else "qc_capture"
            await connection.execute(
                f"UPDATE {table} SET metadata=metadata || "
                '\'{"identity_policy_version":"obsolete"}\'::jsonb WHERE id=$1::uuid',
                original,
            )
        with pytest.raises(Exception, match="WEB_CLASSIFICATION_REUSE_INVALID"):
            await effects.find_completed(claimed)
        state = await PostgresWebCoverageSnapshotRepository(pool=pool).load_reconciliation_state(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )
        assert state.missing_assessment_count == 1
        assert (await completion_inventory(connection, ids))["missing_assessment_count"] == 1


@pytest.mark.parametrize("missing_material", ["screenshot", "dom", "manifest"])
async def test_completion_gate_does_not_hide_missing_source_material(missing_material):
    async with target_database() as (connection, pool, other, ids):
        original, fresh, evidence, effects, claimed, result = await classified_reuse(
            connection, pool, ids, with_budget=True
        )
        await prepare_formal_inventory(connection, pool, ids, evidence, claimed)
        if missing_material == "manifest":
            await connection.execute(
                "DELETE FROM qc_evidence_manifest WHERE capture_id=$1::uuid", original
            )
        else:
            await connection.execute(
                "DELETE FROM qc_evidence_artifact "
                "WHERE page_capture_id=$1::uuid AND artifact_type=$2",
                original,
                missing_material,
            )
        assert (await completion_inventory(connection, ids))["missing_assessment_count"] == 1


@pytest.mark.parametrize("case", ["state_query", "legacy", "needs_review"])
async def test_verified_reuse_supports_frozen_identity_and_keeps_review_gate(case):
    async with target_database() as (connection, pool, other, ids):
        kwargs = {}
        if case == "state_query":
            kwargs = {
                "url": "https://example.test/topics/?tab=first",
                "fresh_url": "https://example.test/topics/?tab=second",
            }
        elif case == "needs_review":
            kwargs = {
                "assessment": WebPageAssessment(
                    page_type=None,
                    confidence=0.3,
                    evidence=("原页面证据不足",),
                    needs_review_reason="WEB_PAGE_STRUCTURE_INSUFFICIENT",
                )
            }
        original, fresh, evidence, effects, claimed, result = await classified_reuse(
            connection, pool, ids, with_budget=True, **kwargs
        )
        await prepare_formal_inventory(connection, pool, ids, evidence, claimed)
        if case == "legacy":
            await connection.execute(
                "DELETE FROM qc_surface_phase_run WHERE source_capture_id=$1::uuid", fresh
            )
        else:
            assert (await effects.find_completed(claimed)).reused_capture_id == original
        state = await PostgresWebCoverageSnapshotRepository(pool=pool).load_reconciliation_state(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )
        gate = await completion_inventory(connection, ids)
        assert state.missing_assessment_count == gate["missing_assessment_count"] == 0
        assert (
            state.review_assessment_count
            == gate["review_assessment_count"]
            == int(case == "needs_review")
        )
        assert (
            state.missing_publication_count
            == gate["missing_publication_count"]
            == int(case != "needs_review")
        )
