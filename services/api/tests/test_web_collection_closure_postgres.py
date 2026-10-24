"""集合分类、两跳子游标、父菜单结算与报告读取使用真实隔离 PG。"""

import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.api.app.application.web_desktop_exploration import WebDesktopExplorationSummary
from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
)
from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_page_type_assessment import WebPageTypeAssessmentRecorder
from services.api.app.application.web_run_stage_orchestrator import WebRunStageOrchestrator
from services.api.app.application.web_runtime_discovery import (
    WebRuntimeMenuEntry,
    discovery_entry_id,
)
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
)
from services.api.app.infrastructure.db.web_runtime_stage_evidence_repository import (
    PostgresWebRuntimeStageEvidenceRepository,
)
from services.api.tests.test_web_auxiliary_stage_postgres import _page
from services.api.tests.test_web_capture_evidence_repository import _real_assessment_capture
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _CATALOG_SHA256,
    _CATALOG_VERSION,
    _command,
    _database,
    _seed_completed_action_evidence,
    _site,
    _upgrade_isolated_postgres,  # noqa: F401 -- 隔离数据库迁移夹具。
)
from services.api.tests.test_web_page_type_assessment import _catalog_identity
from services.api.tests.test_web_run_stage_checkpoint_postgres import seed_current
from services.api.tests.test_web_runtime_discovery_postgres import _enabled_case

pytestmark = pytest.mark.skipif(not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="需要隔离 PG")


@pytest.mark.parametrize("invalid", [
    None, "old_catalog", "catalog_hash", "catalog_audit", "catalog_both",
    "failed_action", "wrong_ready",
])
async def test_formal_collection_children_settlement_and_report_roundtrip(invalid):
    async with _database() as (control, pool, other, ids):
        initial_revision = await control.fetchval("SELECT version_num FROM alembic_version")
        assert isinstance(initial_revision, str)
        policy = await _enabled_case(control, pool, ids, menu_limit=5)
        await seed_current(control, ids)
        await _page(control, ids, ids["capture"], page_type=None)
        command = _command(ids)
        context = SimpleNamespace(site=_site(), content_sha256=_CATALOG_SHA256)
        ledger = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        await ledger.bootstrap(
            command,
            context.site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=context.content_sha256,
        )
        candidate = WebNavigationCandidate(
            entry_id="people",
            label="人物目录",
            url="https://example.test/people",
            menu_path=("人物目录",),
            dom_fingerprint="people-nav",
            source_signal="semantic_nav",
            page_type_key="unknown",
            source_capture_id=ids["capture"],
        )
        await ledger.record_discovery(
            command=command,
            site=context.site,
            candidate=candidate,
            source_capture_id=ids["capture"],
            error_code="WEB_DISCOVERY_PENDING",
        )
        reservation = await ledger.claim_menu(
            command=command,
            entry=WebRuntimeMenuEntry.from_candidate(candidate),
            source_capture_id=ids["capture"],
        )
        captures = {}
        sequence = 1

        async def page_record(capture, url):
            page_seq = await control.fetchval(
                "SELECT COALESCE(MAX(page_seq),0)+1 FROM qc_page_capture WHERE task_id=$1::uuid",
                ids["task"],
            )
            await control.execute(
                "INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,url,"
                "capture_status,surface_session_id,capture_role,session_command_sequence,"
                "metadata,action_execution_id) SELECT id,task_id,agent_run_id,$3,'controlled',$2,"
                "capture_status,surface_session_id,capture_role,session_command_sequence,"
                "metadata,action_execution_id FROM qc_capture WHERE id=$1::uuid",
                capture,
                url,
                page_seq,
            )

        async def navigate(source, url, intent):
            nonlocal sequence
            sequence += 1
            action = str(uuid4())
            before, probe = await _seed_completed_action_evidence(
                control,
                ids,
                action_execution_id=action,
                intent_id=intent,
                command_sequence=sequence,
                requested_source_capture_id=source,
                parent_capture_id=source,
            )
            for capture in (before, probe):
                await control.execute(
                    "UPDATE qc_capture SET metadata='{"
                    + '"finalization_epoch":0'
                    + "}' WHERE id=$1::uuid",
                    capture,
                )
                await page_record(capture, url)
                captures[capture] = url
            await control.execute(
                "UPDATE qc_surface_session SET current_capture_id=$1::uuid,command_sequence=$3 "
                "WHERE session_id=$2",
                probe,
                ids["session"],
                sequence,
            )
            return probe

        class Readiness:
            async def prepare(self, *, source_capture_id, page_role):
                nonlocal sequence
                sequence += 1
                capture = str(uuid4())
                await control.execute(
                    "INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,"
                    "surface_session_id,parent_capture_id,capture_role,capture_status,"
                    "captured_at,metadata,session_command_sequence) "
                    "VALUES($1::uuid,$2::uuid,$3::uuid,'web',$4,$5::uuid,'probe_before','captured',now(),"
                    "'{\"finalization_epoch\":0}',$6)",
                    capture,
                    ids["task"],
                    ids["run"],
                    ids["session"],
                    source_capture_id,
                    sequence,
                )
                captures[capture] = captures[source_capture_id]
                await page_record(capture, captures[capture])
                await control.execute(
                    "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
                    capture,
                    ids["session"],
                )
                return SimpleNamespace(status="stable", ready_capture_id=capture)

        class Reader:
            async def read(self, *, capture_id, page_role):
                return replace(
                    _real_assessment_capture(), capture_id=capture_id, url=captures[capture_id]
                )

        identity = _catalog_identity()
        recorder = WebPageTypeAssessmentRecorder(
            repository=PostgresWebCaptureEvidenceRepository(
                pool=pool,
                assessment_schema_version="web-page-type-assessment.v4",
                catalog_identity=identity,
            ),
            catalog_identity=identity,
        )

        class Classifier:
            async def classify(self, ready):
                kind = {
                    "/people": "collection_page",
                    "/person/one": "video_list",
                    "/video/one": "video_detail",
                }
                assessment = WebPageAssessment(
                    page_type=kind[ready.url.removeprefix("https://example.test")],
                    confidence=0.85,
                    evidence=("受控模型边界提供分类，正式判定走真实仓储",),
                )
                await recorder.persist(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    session_id=ids["session"],
                    capture=ready,
                    assessment=assessment,
                )
                return assessment

        class Collection:
            async def stage_collection_candidates(self, command, *, source_capture_id):
                path = (
                    "/person/one"
                    if captures[source_capture_id].endswith("/people")
                    else "/video/one"
                )
                return (SimpleNamespace(url="https://example.test" + path, candidate_id=path),)

            async def stage_collection_navigate(
                self, command, *, source_capture_id, candidate_id, requested_url
            ):
                return await navigate(
                    source_capture_id, requested_url, f"web-collection:{ids['run']}:{candidate_id}"
                )

        async def age_read(*, capture_id):
            return SimpleNamespace(capture_id=capture_id)

        async def age_handle(evidence, **kwargs):
            return SimpleNamespace(after_capture_id=evidence.capture_id)

        async def summary(command):
            return WebDesktopExplorationSummary(page_timeline=(), page_type_keys=())

        runtime = SimpleNamespace(
            page_readiness=Readiness(),
            stage_ready_capture_reader=Reader(),
            page_type_classifier=Classifier(),
            auxiliary_navigation_explorer=Collection(),
            pre_gate_supplemental_capture=None,
            age_evidence_reader=SimpleNamespace(read=age_read),
            age_gate_handler=SimpleNamespace(handle=age_handle),
            read_persisted_exploration=summary,
        )
        checkpoints = PostgresWebRunStageCheckpointRepository(pool=pool)
        evidence = PostgresWebRuntimeStageEvidenceRepository(
            task_id=ids["task"], run_id=ids["run"], session_id=ids["session"], pool=pool
        )
        frontier = WebRuntimeStageFrontier(
            ledger=ledger,
            candidate_source=None,
            navigator=None,
            site=context.site,
            evidence=evidence,
        )
        effects = WebRuntimeStageEffects(
            runtime=runtime,
            evidence=evidence,
            command=command,
            checkpoints=checkpoints,
            frontier=frontier,
        )
        orchestrator = WebRunStageOrchestrator(
            checkpoints=checkpoints, effects=effects, worker_id="collection-chain"
        )
        probe = await navigate(ids["capture"], candidate.url, reservation.intent_id)
        ready = (
            await runtime.page_readiness.prepare(source_capture_id=probe, page_role="menu")
        ).ready_capture_id
        parent_key = "menu:people"
        await checkpoints.create(
            WebStageCursor.start(
                run_id=ids["run"],
                logical_page_key=parent_key,
                stage=WebRunStage.CLASSIFICATION,
                cursor_payload={
                    "session_id": ids["session"],
                    "finalization_epoch": 0,
                    "capture_id": ready,
                    "current_url": candidate.url,
                    "menu_ledger_id": discovery_entry_id(candidate),
                    "menu_navigation_capture_id": probe,
                    "parent_capture_id": probe,
                },
            )
        )

        async def drive(key):
            for _ in range(24):
                row = await checkpoints.get(run_id=ids["run"], logical_page_key=key)
                if row.is_terminal:
                    assert not row.gaps
                    return
                if key == parent_key and row.next_stage is WebRunStage.EVIDENCE_RECONCILIATION:
                    if invalid == "old_catalog":
                        await control.execute(
                            "UPDATE qc_h5_page_type_assessment SET assessment_payload="
                            "jsonb_set(assessment_payload,'{catalog_version}',"
                            "'\"2026-09-15.web.v10\"') WHERE page_capture_id=$1::uuid",
                            ready,
                        )
                    elif invalid in {"catalog_hash", "catalog_audit", "catalog_both"}:
                        if invalid != "catalog_audit":
                            await control.execute(
                                "UPDATE qc_h5_page_type_assessment SET assessment_payload="
                                "jsonb_set(assessment_payload,'{catalog_sha256}',"
                                "'\"sha256:untrusted\"') WHERE page_capture_id=$1::uuid", ready,
                            )
                        if invalid != "catalog_hash":
                            await control.execute(
                                "UPDATE qc_page_capture SET metadata=jsonb_set(metadata,"
                                "'{web_page_type_assessment,catalog_sha256}',"
                                "'\"sha256:untrusted\"') WHERE id=$1::uuid", ready,
                            )
                    elif invalid == "failed_action":
                        await control.execute(
                            "UPDATE qc_action_execution SET status='failed',"
                            "error_code='WEB_TEST_FAILED' "
                            "WHERE result_capture_id=$1::uuid",
                            probe,
                        )
                    elif invalid == "wrong_ready":
                        await control.execute(
                            "UPDATE qc_h5_page_type_assessment SET page_capture_id=$1::uuid "
                            "WHERE page_capture_id=$2::uuid",
                            ids["capture"],
                            ready,
                        )
                result = await orchestrator.advance_once(ids["run"], key)
                assert not result.cursor.gaps, result.cursor.gap_codes
                children = [
                    item
                    for item in await checkpoints.list_for_run(run_id=ids["run"])
                    if item.logical_page_key in ("collection:" + key, "collection-detail:" + key)
                ]
                for child in children:
                    if not child.is_terminal:
                        await drive(child.logical_page_key)
            pytest.fail("集合阶段未按有界图终止")

        if invalid:
            with pytest.raises(WebExplorationBudgetLedgerConflictError):
                await drive(parent_key)
            return
        await drive(parent_key)
        restored = PostgresWebExplorationBudgetLedgerRepository(pool=other, frozen_policy=policy)
        budget = await restored.read_summary(agent_run_id=ids["run"], site_id=context.site.site_id)
        row = next(
            item
            for item in budget.runtime_discovered_ledger
            if item.entry_id == discovery_entry_id(candidate)
        )
        assert row.execution.status.value == "menu_page_captured"
        assert row.execution.menu_page_type == "collection_page"
        assert row.execution.detail_action_count == 0
        assert budget.executed_same_origin_count == 1
        fresh_evidence = PostgresWebRuntimeStageEvidenceRepository(
            task_id=ids["task"], run_id=ids["run"], session_id=ids["session"], pool=other
        )
        report = await fresh_evidence.report_captures()
        assert {"collection_page", "video_list", "video_detail"} <= {item[1] for item in report}
        final = await WebRuntimeStageEffects(
            runtime=runtime,
            evidence=fresh_evidence,
            command=command,
            checkpoints=checkpoints,
            frontier=frontier,
        ).final_result(ids["run"])
        assert {"collection_page", "video_list", "video_detail"} <= set(final.page_type_keys)
        assert (
            await control.fetchval(
                "SELECT normalization_version FROM qc_web_menu_ledger_entry "
                "WHERE agent_run_id=$1::uuid AND menu_entry_id=$2",
                ids["run"],
                discovery_entry_id(candidate),
            )
            == "web-page-type-assessment.v4"
        )
        from services.api.tests.test_web_collection_migration_postgres import migrate_collection

        guard_sql = (
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='qc_web_menu_ledger_entry'::regclass "
            "AND conname='ck_qc_web_menu_success_evidence'"
        )
        guard = await control.fetchval(guard_sql)
        code, output = await migrate_collection("downgrade", "20260918_0124")
        assert code != 0
        assert "Cannot downgrade immutable Web collection evidence" in output
        assert await control.fetchval(guard_sql) == guard
        assert await control.fetchval("SELECT version_num FROM alembic_version") == initial_revision
