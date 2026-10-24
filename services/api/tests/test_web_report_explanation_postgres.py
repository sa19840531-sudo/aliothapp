"""隔离 PG 验证真实失败序列和分类后诊断的读取及封存围栏。"""

import json
import os
from uuid import uuid4

import pytest

from services.api.tests.test_web_logical_target_postgres import target_database
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)
from services.api.tests.web_sequence_pg_support import seed_failure_frames

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="需显式隔离 PG 双 DSN"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stable_current", [False, True])
async def test_actual_partial_writer_batch_retains_each_rule_projection(
    monkeypatch, stable_current
):
    from services.api.app.infrastructure.db.task_insight_repository import (
        PostgresTaskInsightRepository,
    )
    from services.api.tests import test_web_sequence_batch_contract_postgres as batch

    original = batch.consume_sequence_with_actual_work
    observed = []

    async def consume(*args, **kwargs):
        result = await original(*args, **kwargs)
        ids, pool = args[3], args[1]
        observed.append(
            await PostgresTaskInsightRepository(pool=pool).fetch_web_report_explanation(
                task_id=ids["task"], agent_run_id=ids["run"]
            )
        )
        return result

    monkeypatch.setattr(batch, "consume_sequence_with_actual_work", consume)
    await batch.test_real_batch_contract_conflict_preserves_written_sources_and_closes_work(
        1, monkeypatch, stable_current=stable_current
    )
    partial = [
        item
        for view in observed
        for item in view["rule_materials"]
        if item["work_status"] == "failed" and item["status"] == "written"
    ]
    assert partial
    assert any(
        attempt["status"] == "terminal_error" for item in partial for attempt in item["attempts"]
    )
    assert all(item["canonical"] and item["canonical"]["sources"] for item in partial)
    if stable_current:
        stable = [
            item
            for view in observed
            for item in view["rule_materials"]
            if item["canonical"] and item["input_scope"] != "web_sequence"
        ]
        assert stable and all(item["material_role"] == "stable" for item in stable)


@pytest.mark.asyncio
async def test_real_ledger_primary_failure_survives_read_model_without_private_payload():
    from services.api.app.infrastructure.db.task_insight_repository import (
        PostgresTaskInsightRepository,
    )
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        PostgresWebExplorationBudgetLedgerRepository,
    )
    from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
        _command,
        _site,
    )

    async with target_database() as (connection, pool, _, ids):
        await PostgresWebExplorationBudgetLedgerRepository(pool=pool).bootstrap(
            _command(ids), _site(), catalog_version="t10-test", catalog_sha256="a" * 64
        )
        await connection.execute(
            "UPDATE qc_web_menu_ledger_entry SET diagnostic=$2::jsonb "
            "WHERE agent_run_id=$1::uuid AND menu_entry_id='menu-1'",
            ids["run"],
            json.dumps(
                {
                    "primary_content_diagnostics": [
                        {
                            "status": "failed",
                            "parent_capture_id": ids["capture"],
                            "reason_code": "WEB_PRIMARY_CONTENT_CAPTURE_FAILED",
                            "raw": "PRIVATE",
                            "attempts": [
                                {
                                    "stage": "observe",
                                    "source_capture_id": ids["capture"],
                                    "action_execution_id": "action-test",
                                }
                            ],
                        }
                    ]
                }
            ),
        )
        view = await PostgresTaskInsightRepository(pool=pool).fetch_web_report_explanation(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        assert view["primary_content"][0]["parent_capture_id"] == ids["capture"]
        assert view["primary_content"][0]["attempts"][0]["stage"] == "observe"
        assert "PRIVATE" not in str(view)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_reason", ["timeout", "loading"])
async def test_real_sequence_projection_scope_and_notification_seal_fence(exit_reason):
    from services.api.app.domain.iam.access_scope import AccessScope
    from services.api.app.infrastructure.db.task_insight_repository import (
        PostgresTaskInsightRepository,
    )
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        PostgresWebCaptureEvidenceRepository,
    )
    from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
        PostgresWebEvidenceSequenceRepository,
    )

    async with target_database() as (connection, pool, _, ids):
        try:
            captures, _ = await seed_failure_frames(connection, pool, ids)
            # 共享夹具已从规范表写入完整来源与帧血缘，不再重复插根或倒灌公共表。
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_page_capture p JOIN qc_capture c ON p.id=c.id "
                    "WHERE p.task_id=$1::uuid AND p.metadata=c.metadata "
                    "AND p.captured_at=c.captured_at AND p.surface_session_id=c.surface_session_id",
                    ids["task"],
                )
                == 5
            )
            await PostgresWebEvidenceSequenceRepository(pool=pool).register_failure(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                source_capture_id=captures[1],
                evidence_capture_ids=(captures[1], captures[3]),
                page_role="menu",
                readiness_status=exit_reason,
            )
            repository = PostgresTaskInsightRepository(pool=pool)
            from services.api.tests.web_frozen_assessment_support import frozen_assessment_options

            options = await frozen_assessment_options(connection, ids)
            writer = PostgresWebCaptureEvidenceRepository(pool=pool, **options)
            from dataclasses import replace

            from services.api.app.application.web_page_classification import WebPageAssessment
            from services.api.app.application.web_page_inspection_notification import (
                WebPageInspectionNotification,
                WebPageInspectionNotifyingClassifier,
            )
            from services.api.app.application.web_page_type_assessment import (
                WebPageTypeAssessmentRecorder,
                WebPageTypeAssessmentRecordingClassifier,
            )
            from services.api.tests.test_web_page_type_assessment import _capture

            class FixedModel:
                async def classify(self, capture):
                    return WebPageAssessment(
                        page_type="content_list", confidence=1.0, evidence=("固定结构样本",)
                    )

            index = await writer.read(task_id=ids["task"], capture_id=captures[1])
            capture = replace(
                _capture(),
                capture_id=captures[1],
                captured_at=index.captured_at,
                screenshot_artifact_id=index.artifact_ids["screenshot"],
                dom_artifact_id=index.artifact_ids["dom"],
                visible_text_artifact_id=index.artifact_ids["visible_text"],
                url=index.url,
                inspection_readiness=None,
            )
            recording = WebPageTypeAssessmentRecordingClassifier(
                classifier=FixedModel(),
                recorder=WebPageTypeAssessmentRecorder(repository=writer, **options),
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
            )
            notifying = WebPageInspectionNotifyingClassifier(
                classifier=recording,
                notification=WebPageInspectionNotification(
                    reader=writer, inventory=object(), publication_writer=object()
                ),
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
            )
            await notifying.classify(capture)
            classification = await connection.fetchrow(
                "SELECT detection_status,page_type,metadata->'web_inspection_notification' AS notification "
                "FROM qc_page_capture WHERE id=$1::uuid",
                captures[1],
            )
            assert classification["detection_status"] == "recognized"
            assert json.loads(classification["notification"])["status"] == "blocked"
            classification_before = tuple(
                classification[key] for key in ("detection_status", "page_type")
            )
            kwargs = dict(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                capture_id=captures[1],
                reason_code="WEB_CAPTURE_ARTIFACT_MISSING",
                diagnostic_type=None,
                publication_id=None,
                retryable=False,
                status="blocked",
            )
            await writer.record_notification_diagnostic(**kwargs)
            result = await repository.fetch_web_report_explanation(
                task_id=ids["task"], agent_run_id=ids["run"]
            )
            assert len(result["sequences"]) == 1
            assert result["sequences"][0]["readiness_status"] == "loading"
            assert result["sequences"][0]["readiness_exit_reason"] == exit_reason
            assert all(
                frame["page_state"] == "loading" for frame in result["sequences"][0]["frames"]
            )
            assert result["sequences"][0]["frames"][0]["capture_id"] == captures[0]
            assert captures[1] in [row["capture_id"] for row in result["notifications"]], result[
                "notifications"
            ]
            row = next(row for row in result["notifications"] if row["capture_id"] == captures[1])
            assert row["notification"]["status"] == "blocked"
            denied = AccessScope(
                actor_user_id=str(uuid4()), tenant_id=str(uuid4()), permissions=frozenset()
            )
            assert (
                await repository.fetch_web_report_explanation(
                    task_id=ids["task"], agent_run_id=ids["run"], access_scope=denied
                )
                is None
            )
            await connection.execute(
                "UPDATE qc_surface_session SET finalization_epoch=1 WHERE session_id=$1",
                ids["session"],
            )
            await writer.record_notification_diagnostic(**(kwargs | {"status": "published"}))
            assert (
                json.loads(
                    await connection.fetchval(
                        "SELECT metadata->'web_inspection_notification' FROM qc_page_capture WHERE id=$1::uuid",
                        captures[1],
                    )
                )["status"]
                == "blocked"
            )
            await connection.execute(
                "UPDATE qc_surface_session SET finalization_epoch=0 WHERE session_id=$1",
                ids["session"],
            )
            newer_run = str(uuid4())
            await connection.execute(
                "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,created_at) VALUES($1::uuid,$2::uuid,'main','t10-newer','running',now()+interval '1 second')",
                newer_run,
                ids["task"],
            )
            await writer.record_notification_diagnostic(**(kwargs | {"status": "published"}))
            assert (
                json.loads(
                    await connection.fetchval(
                        "SELECT metadata->'web_inspection_notification' FROM qc_page_capture WHERE id=$1::uuid",
                        captures[1],
                    )
                )["status"]
                == "blocked"
            )
            assert (
                await repository.fetch_web_report_explanation(
                    task_id=ids["task"], agent_run_id=ids["run"]
                )
                is None
            )
            await connection.execute(
                "DELETE FROM qc_agent_run WHERE id=$1::uuid AND task_id=$2::uuid",
                newer_run,
                ids["task"],
            )
            from services.api.app.application.task_finalization import (
                TerminateTaskFinalizationCommand,
            )
            from services.api.app.domain.task import TaskExecutionStatus
            from services.api.app.infrastructure.db.task_finalization_repository import (
                PostgresTaskFinalizationRepository,
            )

            await connection.execute(
                "UPDATE qc_task SET finalization_state='session_draining',exploration_draining_at=now(), "
                "session_draining_at=now(),finalization_agent_run_id=$2::uuid,finalization_row_version=2 WHERE id=$1::uuid",
                ids["task"],
                ids["run"],
            )
            await connection.execute(
                "UPDATE qc_agent_run SET status='done',finished_at=now(),stop_reason='completed' WHERE id=$1::uuid",
                ids["run"],
            )
            await PostgresTaskFinalizationRepository(pool=pool).terminate(
                command=TerminateTaskFinalizationCommand(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    expected_finalization_epoch=0,
                    expected_row_version=2,
                    execution_status=TaskExecutionStatus.NEEDS_REVIEW,
                    reason_code="WEB_INSPECTION_PUBLICATION_NEEDS_REVIEW",
                )
            )
            await writer.record_notification_diagnostic(**(kwargs | {"status": "published"}))
            stored = await connection.fetchval(
                "SELECT metadata->'web_inspection_notification' FROM qc_page_capture WHERE id=$1::uuid",
                captures[1],
            )
            assert json.loads(stored)["status"] == "blocked"
            assert (
                tuple(
                    await connection.fetchrow(
                        "SELECT detection_status,page_type FROM qc_page_capture WHERE id=$1::uuid",
                        captures[1],
                    )
                )
                == classification_before
            )
        finally:
            async with connection.transaction():
                await connection.execute("SET LOCAL session_replication_role=replica")
                await connection.execute(
                    "DELETE FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid", ids["task"]
                )
                await connection.execute(
                    "DELETE FROM qc_review_policy_gate_decision WHERE task_id=$1::uuid", ids["task"]
                )
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id=$1::uuid", ids["task"]
            )
            await connection.execute(
                "DELETE FROM qc_h5_inspection_publication WHERE task_id=$1::uuid", ids["task"]
            )
