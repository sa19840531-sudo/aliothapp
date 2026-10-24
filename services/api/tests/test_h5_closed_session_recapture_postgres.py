"""原数据库事务回滚验证 H5 新会话补采，不建立新库、不迁移或清理真实任务。"""

import json
import os
from contextlib import asynccontextmanager
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.evidence_requests import (
    EvidenceRequestStatus,
    SettleEvidenceRequestLeaseCommand,
)
from services.api.app.application.h5_targeted_recapture import H5TargetedRecapturePlanService
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceSessionDependency,
    EvidenceSourceGrade,
    build_evidence_request,
)
from services.api.app.domain.interaction import SurfaceKind, SurfaceSessionSpec
from services.api.app.domain.interaction.surface_driver import web_primary_surface_ref
from services.api.app.infrastructure.db.command_scope_reader import PostgresTaskCommandScopeReader
from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)
from services.api.app.infrastructure.db.h5_closed_session_context import (
    PostgresH5ClosedSessionContextReader,
)
from services.api.app.infrastructure.db.h5_probe_terminal_repository import (
    PostgresH5ProbeTerminalSettlementRepository,
)
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    PostgresRestorationAttemptRepository,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
    SurfaceSessionPersistenceError,
)
from services.api.tests.test_targeted_recapture_chain_postgres import (
    _budget,
    _ObserveOnlyExecutor,
    _request,
    _seed,
    _snapshot,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需显式提供 PostgreSQL 事务测试连接",
)


@pytest.mark.parametrize("operation", ["release", "settle"])
@pytest.mark.parametrize("mutation", ["token", "version", "expired", "terminal"])
async def test_h5_rejected_recapture_lease_does_not_mutate_authoritative_request(
    operation, mutation,
):
    from services.api.app.application.h5_targeted_recapture import (
        H5TargetedRecaptureConsumer,
        H5TargetedRecaptureDisposition,
    )
    from services.api.app.application.supplemental_evidence_policy import (
        build_h5_supplemental_evidence_policy,
    )

    async with _closed_request() as (connection, _, _, _, plan, _, requests, lease):
        assignments = {
            "token": "lease_token=gen_random_uuid()",
            "version": "row_version=row_version+1",
            "expired": "lease_expires_at=now()-interval '1 second'",
            "terminal": (
                "status='unfulfillable',completed_at=now(),row_version=row_version+1,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL"
            ),
        }
        await connection.execute(
            "UPDATE qc_evidence_request SET " + assignments[mutation] + " WHERE request_id=$1",
            plan.request_id,
        )
        before = await connection.fetchrow(
            "SELECT * FROM qc_evidence_request WHERE request_id=$1", plan.request_id,
        )
        consumer = H5TargetedRecaptureConsumer(
            requests=requests,
            runtime_factory=object(),
            policy=build_h5_supplemental_evidence_policy(enabled=True, h5_v2_mode="on"),
            lease_owner=lease.lease_owner,
        )
        if operation == "release":
            result = await consumer._release(lease=lease, reason_code="H5_RESTORATION_DIVERGENT")
        else:
            result = await consumer._settle(
                lease=lease, status=EvidenceRequestStatus.UNFULFILLABLE,
                disposition=H5TargetedRecaptureDisposition.UNFULFILLABLE,
                reason_code="H5_RECAPTURE_DEADLINE_EXHAUSTED",
            )
        assert result.disposition is H5TargetedRecaptureDisposition.LEASE_REJECTED
        assert await connection.fetchrow(
            "SELECT * FROM qc_evidence_request WHERE request_id=$1", plan.request_id,
        ) == before


@pytest.mark.parametrize(
    "invalid", ["source_session_id", "target_session_id", "strategy", "foreign_session", "android"]
)
async def test_page_graph_replay_constraint_rejects_missing_or_foreign_proof(invalid):
    async with _closed_request() as (connection, pool, _, _, plan, _, _, lease):
        attempt = await PostgresRestorationAttemptRepository(pool=pool).start(
            plan=plan,
            attempt_no=lease.attempt_count,
        )
        payload = json.loads(
            await connection.fetchval(
                "SELECT plan_payload FROM qc_restoration_attempt WHERE attempt_id=$1",
                attempt.attempt_id,
            )
        )
        if invalid in {"source_session_id", "target_session_id", "strategy"}:
            payload.pop(invalid)
        elif invalid == "foreign_session":
            payload["target_session_id"] = f"h5:{uuid4()}:recapture:" + "a" * 20
        # 单个负例回滚到保存点，不影响后续夹具清理。
        with pytest.raises(asyncpg.CheckViolationError):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE qc_restoration_attempt SET plan_payload=$2::jsonb, surface_kind=$3"
                    " WHERE attempt_id=$1",
                    attempt.attempt_id,
                    json.dumps(payload),
                    "android" if invalid == "android" else "web",
                )


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@asynccontextmanager
async def _closed_request(*, task_status="review", request_changes=None, check_constraint=True,
                          isolation="repeatable_read", separate_connections=False):
    database_context = None
    if separate_connections:
        from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
            _database,
        )

        # 真浏览器恢复同时使用一致读与独立锁连接，必须提交到私有隔离库。
        database_context = _database(
            surface_kind="web", target_platform="h5", retain_sealed=True,
        )
        connection, pool, _ = await database_context.__aenter__()
        transaction = None
    else:
        connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
        pool = _Pool(connection)
        transaction = connection.transaction(isolation=isolation)
        await transaction.start()
    try:
        migration = import_module(
            "services.api.migrations.versions.20261008_0131_h5_snapshot_page_graph_replay"
        )
        if check_constraint:
            await connection.execute(
                "ALTER TABLE qc_restoration_attempt "
                "DROP CONSTRAINT ck_qc_restoration_attempt_replay"
            )
            await connection.execute(
                "ALTER TABLE qc_restoration_attempt ADD CONSTRAINT "
                "ck_qc_restoration_attempt_replay CHECK ("
                + migration.ORIGINAL_REPLAY
                + " OR "
                + migration.H5_GRAPH_REPLAY
                + ")"
            )
        ids = {
            name: str(uuid4())
            for name in (
                "tenant",
                "user",
                "ruleset",
                "rule",
                "task",
                "run",
                "work_item",
                "attempt",
                "original",
                "restored",
            )
        }
        await _seed(connection, ids)
        await connection.execute(
            "UPDATE qc_task SET target_platform='h5',finalization_agent_run_id=$2 WHERE id=$1",
            ids["task"],
            ids["run"],
        )
        sessions = PostgresSurfaceSessionRepository(pool=pool)
        source_session = f"h5:{ids['run']}"
        await sessions.register_active(
            spec=SurfaceSessionSpec(
                task_id=ids["task"], session_id=source_session, surface_kind=SurfaceKind.WEB
            ),
            session_owner="h5-workflow",
            primary_surface_ref=web_primary_surface_ref(source_session),
        )
        await connection.execute(
            "UPDATE qc_capture SET surface_session_id=$2,capture_role='after_action' WHERE id=$1",
            ids["original"],
            source_session,
        )
        await connection.execute(
            """INSERT INTO qc_page_graph_state(state_id,task_id,agent_run_id,surface_kind,
                surface_session_id,finalization_epoch,capture_role,logical_page_key,
                identity_status,identity_source,state_fingerprint,counts_as_business_page)
                VALUES($1,$2,$3,'web',$4,0,'after_action','detail:42','confirmed',
                    'logical_surface',$5,true)""",
            ids["original"],
            ids["task"],
            ids["run"],
            source_session,
            "a" * 64,
        )
        now = datetime.now(UTC)
        original = _request(ids, rule_code="alioth_chain_1", expires_at=now + timedelta(hours=1))
        payload = {
            field.name: getattr(original, field.name)
            for field in fields(original)
            if field.name not in {"request_id", "requirement_hash", "protocol_version"}
        }
        request = build_evidence_request(
            **{
                **payload,
                "replay_policy": EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
                "minimum_source_grade": EvidenceSourceGrade.C,
                **(request_changes or {}),
            }
        )
        requests = PostgresEvidenceRequestRepository(pool=pool, h5_snapshot_recovery=True)
        await requests.upsert(request=request)
        await connection.execute(
            "UPDATE qc_surface_session SET status='closed',draining_at=now(),closed_at=now()"
            " WHERE session_id=$1",
            source_session,
        )
        await connection.execute(
            "UPDATE qc_task SET status=$2,finalization_state='session_draining',"
            "exploration_draining_at=now(),session_draining_at=now() WHERE id=$1",
            ids["task"],
            task_status,
        )
        claim = await requests.claim_next(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
            lease_owner="h5-test-new-session",
            lease_seconds=240,
            session_dependencies=(EvidenceSessionDependency.SNAPSHOT,),
        )
        assert claim.lease is not None
        contexts = PostgresH5ClosedSessionContextReader(pool=pool)
        context = await contexts.read(request=request)
        assert context is not None and context.source_session_closed
        runtime = SimpleNamespace(remember_new_session_plan=AsyncMock())
        resolution = await H5TargetedRecapturePlanService(
            contexts=contexts,
            new_session_runtime=runtime,
        ).resolve(request=request, agent_run_id=ids["run"], finalization_epoch=0)
        if request_changes is None:
            assert resolution.plan is not None
        yield connection, pool, ids, request, resolution.plan, sessions, requests, claim.lease
    finally:
        if database_context is not None:
            await database_context.__aexit__(None, None, None)
        else:
            await transaction.rollback()
            await connection.close()


@pytest.mark.parametrize("platform,ready", [("h5", True), ("web", False), ("app", False)])
async def test_active_snapshot_replay_preflight_does_not_claim_or_change_request(platform, ready):
    async with _closed_request(task_status="running", check_constraint=False) as fixture:
        connection, pool, ids, request, _, _, _, _ = fixture
        await connection.execute(
            "UPDATE qc_surface_session SET status='active',draining_at=NULL,closed_at=NULL"
            " WHERE session_id=$1", f"h5:{ids['run']}",
        )
        await connection.execute(
            "UPDATE qc_task SET target_platform=$2::text,"
            "target_url=CASE WHEN $2='app' THEN NULL ELSE target_url END,"
            "task_config=CASE WHEN $2='app' THEN "
            "'{\"app\":{\"platform_build_ref\":\"test-build\"}}'::jsonb "
            "ELSE task_config END WHERE id=$1",
            ids["task"], platform,
        )
        await connection.execute(
            "UPDATE qc_evidence_request SET lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL"
            " WHERE request_id=$1", request.request_id,
        )
        before = await connection.fetchrow(
            "SELECT * FROM qc_evidence_request WHERE request_id=$1", request.request_id,
        )
        runtime = SimpleNamespace(remember_new_session_plan=AsyncMock())
        planner = H5TargetedRecapturePlanService(
            contexts=PostgresH5ClosedSessionContextReader(pool=pool), new_session_runtime=runtime,
        )
        assert await planner.can_defer_snapshot(
            request=request, agent_run_id=ids["run"], finalization_epoch=0,
        ) is ready
        assert await connection.fetchrow(
            "SELECT * FROM qc_evidence_request WHERE request_id=$1", request.request_id,
        ) == before
        runtime.remember_new_session_plan.assert_not_awaited()


@pytest.mark.parametrize("prefix", ["clearance", "observe", "business", "foreign_entry"])
async def test_closed_session_entry_root_only_skips_proven_clearance_prefix(prefix):
    async with _closed_request(check_constraint=False) as fixture:
        connection, pool, ids, request, _, _, _, _ = fixture
        source_session = f"h5:{ids['run']}"
        await connection.execute(
            "UPDATE qc_page_graph_state SET counts_as_business_page=false,is_overlay=true,"
            "logical_page_key='startup-ad' WHERE state_id=$1", ids["original"],
        )
        await connection.execute(
            "UPDATE qc_capture SET surface_session_id=$2,capture_role='after_action' "
            "WHERE id=$1", ids["restored"], source_session,
        )
        await connection.execute(
            """INSERT INTO qc_page_graph_state(state_id,task_id,agent_run_id,surface_kind,
                surface_session_id,finalization_epoch,capture_role,logical_page_key,
                identity_status,identity_source,state_fingerprint,counts_as_business_page)
                VALUES($1,$2,$3,'web',$4,0,'after_action','home','confirmed',
                    'logical_surface',$5,true)""",
            ids["restored"], ids["task"], ids["run"], source_session, "b" * 64,
        )
        parameters = {"target_mode": "popup_dismissal"} if prefix == "clearance" else {}
        if prefix == "foreign_entry":
            parameters = {"initial_surface_action": True, "destination_url": "https://other.test/"}
        operation = "observe" if prefix == "observe" else (
            "navigate" if prefix == "foreign_entry" else "click"
        )
        await connection.execute(
            """INSERT INTO qc_action_execution (
                action_execution_id,task_id,session_id,intent_id,idempotency_key,
                intent_hash,intent_protocol_version,intent_payload,command_sequence,
                finalization_epoch,requested_source_capture_id,status,started_at,
                source_capture_id,result_capture_id,action_finished_at,evidence_finished_at
            ) VALUES($1,$2::uuid,$3,$1,$1,'h5-prefix-hash','action_intent.v1',$4::jsonb,1,
                0,$5::uuid,'completed',now()-interval '1 minute',$5::uuid,$6::uuid,now(),now())""",
            f"test-root:{uuid4()}", ids["task"], source_session,
            json.dumps({"action_type": operation, "risk_level": "low", "parameters": parameters}),
            ids["original"], ids["restored"],
        )
        context = await PostgresH5ClosedSessionContextReader(pool=pool).read(request=request)
        assert context is not None
        allowed = prefix in {"clearance", "observe"}
        assert context.current_capture_id == ids["restored" if allowed else "original"]
        assert context.entry_normalization_required is allowed
        assert context.entry_url == await connection.fetchval(
            "SELECT target_url FROM qc_task WHERE id=$1", ids["task"],
        )


async def test_closed_snapshot_can_claim_restore_register_and_issue_fenced_command_in_review():
    async with _closed_request() as (connection, pool, ids, request, plan, sessions, _, lease):
        attempt = await PostgresRestorationAttemptRepository(pool=pool).start(
            plan=plan,
            attempt_no=lease.attempt_count,
        )
        assert attempt.plan == plan
        # 页面图路线不是 JourneyExecutionPlan，不能伪造一个 FK 引用。
        assert (
            await connection.fetchval(
                "SELECT replay_execution_plan_id FROM qc_restoration_attempt WHERE attempt_id=$1",
                attempt.attempt_id,
            )
            is None
        )

        spec = SurfaceSessionSpec(
            task_id=ids["task"], session_id=plan.target_session_id, surface_kind=SurfaceKind.WEB
        )
        await sessions.register_active(
            spec=spec,
            session_owner=f"h5-snapshot-recapture:{ids['run']}",
            primary_surface_ref=web_primary_surface_ref(spec.session_id),
        )
        reader = PostgresTaskCommandScopeReader(pool=pool)
        assert (
            await reader.read_command_scope(
                task_id=ids["task"],
                session_id=spec.session_id,
                surface_kind=SurfaceKind.WEB,
            )
            is not None
        )
        permit = await sessions.acquire_command(
            task_id=ids["task"],
            session_id=spec.session_id,
            surface_kind=SurfaceKind.WEB,
            lease_owner="h5-command",
            lease_token=str(uuid4()),
            lease_seconds=60,
            allow_draining=True,
        )
        assert permit.finalization_epoch == 0
        assert (
            await reader.verify_execution_context(
                task_id=permit.task_id,
                session_id=permit.session_id,
                surface_kind=SurfaceKind.WEB,
                lease_owner=permit.lease_owner,
                lease_token=permit.lease_token,
                command_sequence=permit.command_sequence,
                finalization_epoch=permit.finalization_epoch,
            )
            is not None
        )
        from services.api.app.application.h5_exploration_workflow import H5SurfaceBootstrapper
        from services.api.app.domain.interaction import ActionIntent, InteractionOperation
        from services.api.app.infrastructure.db.action_execution_repository import (
            PostgresActionExecutionRepository,
        )
        from services.api.app.infrastructure.db.page_capture_repository import (
            PostgresPageCaptureRepository,
        )

        action = await PostgresActionExecutionRepository(pool=pool).plan(
            intent=ActionIntent(
                task_id=ids["task"],
                session_id=spec.session_id,
                intent_id="h5-recapture-bootstrap:" + plan.plan_hash,
                idempotency_key="h5-recapture-bootstrap:" + plan.plan_hash,
                surface_kind=SurfaceKind.WEB,
                source_capture_id=f"h5-entry:{ids['run']}",
                action_type=InteractionOperation.NAVIGATE,
                purpose="验证 H5 新会话入口",
                semantic_target="H5 任务入口",
                capture_profile=H5SurfaceBootstrapper.capture_profile(),
                deadline=plan.deadline,
                parameters={
                    "destination_url": "https://example.com",
                    "initial_surface_action": True,
                },
            ),
            permit=permit,
        )
        capture_id = str(uuid4())
        captures = PostgresPageCaptureRepository(pool=pool)
        await captures.save_capture(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            page_capture_id=capture_id,
            url="https://example.com",
            mode="goto",
            reachable=True,
            title="入口",
            status_code=200,
            action_log=[],
            artifacts=[],
            capture_role="after_action",
            surface_session_id=spec.session_id,
            action_execution_id=action.action_execution_id,
            session_command_sequence=permit.command_sequence,
            finalization_epoch=permit.finalization_epoch,
            declared_evidence_flags={"supplemental_evidence_source": "targeted_recapture"},
        )
        assert await captures.update_detection_result(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            page_capture_id=capture_id,
            page_type="home",
            page_state="stable",
            detection_status="recognized",
            confidence=0.9,
            catalog_version="test",
            catalog_sha256="a" * 64,
        )
        assert (
            await PostgresH5ProbeTerminalSettlementRepository(
                pool=pool,
                allow_new_session_recovery=True,
            ).settle_requests_after_session_close(task_id=ids["task"], agent_run_id=ids["run"])
            == 0
        )


@pytest.mark.parametrize("mutation", ["epoch", "cancel", "finalizing", "main", "lease", "platform"])
async def test_new_session_permission_revoked_before_browser_command(mutation):
    async with _closed_request(task_status="running" if mutation == "cancel" else "review") as (
        connection,
        pool,
        ids,
        _,
        plan,
        sessions,
        _,
        lease,
    ):
        await PostgresRestorationAttemptRepository(pool=pool).start(
            plan=plan,
            attempt_no=lease.attempt_count,
        )
        if mutation == "epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1", ids["task"]
            )
        elif mutation == "cancel":
            await connection.execute(
                "UPDATE qc_task SET status='canceled' WHERE id=$1", ids["task"]
            )
        elif mutation == "finalizing":
            await connection.execute(
                "UPDATE qc_task SET finalization_state='finalizing' WHERE id=$1", ids["task"]
            )
        elif mutation == "main":
            await connection.execute(
                "UPDATE qc_task SET finalization_agent_run_id=NULL WHERE id=$1", ids["task"]
            )
        elif mutation == "lease":
            await connection.execute(
                "UPDATE qc_evidence_request SET lease_expires_at=now()-interval '1 second'"
                " WHERE request_id=$1",
                plan.request_id,
            )
        else:
            await connection.execute(
                "UPDATE qc_task SET target_platform='web' WHERE id=$1", ids["task"]
            )
        with pytest.raises(SurfaceSessionPersistenceError):
            await sessions.register_active(
                spec=SurfaceSessionSpec(
                    task_id=ids["task"],
                    session_id=plan.target_session_id,
                    surface_kind=SurfaceKind.WEB,
                ),
                session_owner=f"h5-snapshot-recapture:{ids['run']}",
                primary_surface_ref=web_primary_surface_ref(plan.target_session_id),
            )


@pytest.mark.parametrize("registered", [True, False])
async def test_interrupted_new_session_restoration_is_terminal_and_idempotent(registered):
    async with _closed_request() as (connection, pool, ids, _, plan, sessions, _, lease):
        attempt = await PostgresRestorationAttemptRepository(pool=pool).start(
            plan=plan,
            attempt_no=lease.attempt_count,
        )
        if registered:
            await sessions.register_active(
                spec=SurfaceSessionSpec(
                    task_id=ids["task"],
                    session_id=plan.target_session_id,
                    surface_kind=SurfaceKind.WEB,
                ),
                session_owner=f"h5-snapshot-recapture:{ids['run']}",
                primary_surface_ref=web_primary_surface_ref(plan.target_session_id),
            )
        contexts = PostgresH5ClosedSessionContextReader(pool=pool)
        await contexts.settle_interrupted_restorations(
            task_id=ids["task"], session_id=plan.target_session_id
        )
        await contexts.settle_interrupted_restorations(
            task_id=ids["task"], session_id=plan.target_session_id
        )
        row = await connection.fetchrow(
            "SELECT status,error_code,row_version FROM qc_restoration_attempt WHERE attempt_id=$1",
            attempt.attempt_id,
        )
        assert row["status"] == "failed" and row["error_code"] == "H5_NEW_SESSION_INTERRUPTED"
        assert row["row_version"] == 1


async def test_review_request_can_release_and_settle_using_original_lease():
    async with _closed_request() as (connection, _, ids, _, plan, _, requests, lease):
        await requests.settle_lease(
            command=SettleEvidenceRequestLeaseCommand(
                request_id=plan.request_id,
                task_id=ids["task"],
                finalization_epoch=0,
                lease_owner=lease.lease_owner,
                lease_token=lease.lease_token,
                row_version=lease.row_version,
                target_status=EvidenceRequestStatus.UNFULFILLABLE,
                resolution_reason="H5_NEW_SESSION_STATE_DIVERGED",
            )
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_evidence_request WHERE request_id=$1",
                plan.request_id,
            )
            == "unfulfillable"
        )


async def test_review_can_claim_unsupported_grade_to_reach_explainable_terminal():
    async with _closed_request(
        request_changes={"minimum_source_grade": EvidenceSourceGrade.A1}
    ) as (
        connection,
        _,
        ids,
        request,
        plan,
        _,
        requests,
        lease,
    ):
        assert plan is None
        await requests.settle_lease(
            command=SettleEvidenceRequestLeaseCommand(
                request_id=request.request_id,
                task_id=ids["task"],
                finalization_epoch=0,
                lease_owner=lease.lease_owner,
                lease_token=lease.lease_token,
                row_version=lease.row_version,
                target_status=EvidenceRequestStatus.UNFULFILLABLE,
                resolution_reason="H5_NEW_SESSION_SOURCE_GRADE_INSUFFICIENT",
            )
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_evidence_request WHERE request_id=$1",
                request.request_id,
            )
            == "unfulfillable"
        )


async def test_new_session_c_grade_probe_attaches_reseals_and_wakes_original_rule():
    from services.api.app.application.evidence_request_consumption import (
        EvidenceRequestConsumptionService,
        ProbeCompletionProof,
    )
    from services.api.app.application.evidence_supplements import EvidenceSupplementAttacher
    from services.api.app.application.exploration_work_queue import (
        ClaimExplorerWorkCommand,
        ExplorerWorkStatus,
        ExplorerWorkType,
        TransitionExplorerWorkCommand,
    )
    from services.api.app.application.orchestration import QcOrchestrator
    from services.api.app.application.restoration import RestorationCoordinator
    from services.api.app.application.restored_probe_publication import (
        RestoredProbePublicationService,
    )
    from services.api.app.application.state_equivalence import StateEquivalenceGate
    from services.api.app.application.supplement_input_set_reseal import (
        SupplementInputSetResealService,
    )
    from services.api.app.application.supplemental_evidence_policy import SupplementalEvidencePolicy
    from services.api.app.domain.evidence import (
        ArtifactReference,
        EvidenceQualityStatus,
        build_evidence_manifest_reference,
    )
    from services.api.app.infrastructure.db.evidence_input_set_repository import (
        PostgresEvidenceInputSetRepository,
    )
    from services.api.app.infrastructure.db.evidence_manifest_repository import (
        PostgresEvidenceManifestRepository,
    )
    from services.api.app.infrastructure.db.evidence_supplement_repository import (
        PostgresEvidenceSupplementRepository,
    )
    from services.api.app.infrastructure.db.explorer_work_repository import (
        PostgresExplorerWorkRepository,
        PostgresProbeWorkPublisher,
    )
    from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
        H5ClaimedEvidenceRequestReference,
    )
    from services.api.app.infrastructure.db.inspection_work_item_repository import (
        PostgresInspectionWorkItemRepository,
    )

    async with _closed_request() as (connection, pool, ids, request, plan, _, requests, lease):
        original, restored = [
            replace(
                _snapshot(
                    task_id=ids["task"],
                    capture_id=ids[key],
                    captured_at=await connection.fetchval(
                        "SELECT captured_at FROM qc_capture WHERE id=$1",
                        ids[key],
                    ),
                ),
                session_id=plan.source_session_id if key == "original" else plan.target_session_id,
            )
            for key in ("original", "restored")
        ]
        await connection.execute(
            "UPDATE qc_capture SET surface_session_id=$2 WHERE id=$1",
            ids["restored"],
            plan.target_session_id,
        )
        attempt = await RestorationCoordinator(
            repository=PostgresRestorationAttemptRepository(pool=pool),
            executor=_ObserveOnlyExecutor(restored),
            equivalence_gate=StateEquivalenceGate(),
        ).restore(
            request=request, plan=plan, attempt_no=lease.attempt_count, original_snapshot=original
        )
        assert attempt.assessment.passed and attempt.plan.source_grade is EvidenceSourceGrade.C
        policy = SupplementalEvidencePolicy(enabled=True, runtime_ready=True)
        queue = PostgresExplorerWorkRepository(
            pool=pool,
            supplemental_evidence_policy=policy,
            evidence_request_repository=H5ClaimedEvidenceRequestReference(),
        )
        probe = await RestoredProbePublicationService(
            orchestrator=QcOrchestrator(
                probe_publisher=PostgresProbeWorkPublisher(
                    repository=queue, supplemental_evidence_policy=policy
                ),
            )
        ).publish(
            request=request,
            restoration_attempt=attempt,
            budget=_budget(),
            request_lease=lease.fence,
        )
        claimed = await queue.claim(
            command=ClaimExplorerWorkCommand(
                work_type=ExplorerWorkType.PROBE,
                task_id=ids["task"],
                lease_owner="h5-test-probe",
                surface_kinds=(SurfaceKind.WEB,),
            ),
            lease_token=str(uuid4()),
        )
        assert claimed is not None and claimed.work.work_id == probe.work_id
        version = await queue.transition(
            command=TransitionExplorerWorkCommand(
                work_id=probe.work_id,
                expected_status=ExplorerWorkStatus.LEASED,
                target_status=ExplorerWorkStatus.RUNNING,
                lease_token=claimed.lease_token,
                row_version=claimed.row_version,
            )
        )
        await queue.transition(
            command=TransitionExplorerWorkCommand(
                work_id=probe.work_id,
                expected_status=ExplorerWorkStatus.RUNNING,
                target_status=ExplorerWorkStatus.SUCCEEDED,
                lease_token=claimed.lease_token,
                row_version=version,
            )
        )
        result_id = str(uuid4())
        await connection.execute(
            "INSERT INTO qc_capture(id,task_id,agent_run_id,surface_session_id,surface_kind,"
            "capture_role,capture_status,parent_capture_id,captured_at,metadata)"
            " VALUES($1,$2,$3,$4,'web','probe_result_replay','captured',$5,now(),$6::jsonb)",
            result_id,
            ids["task"],
            ids["run"],
            plan.target_session_id,
            ids["restored"],
            json.dumps({"capture_role": "probe_result_replay"}),
        )
        manifests = PostgresEvidenceManifestRepository(
            pool=pool,
            permitted_task_statuses=("running", "review"),
        )
        manifest = build_evidence_manifest_reference(
            task_id=ids["task"],
            session_id=plan.target_session_id,
            capture_id=result_id,
            manifest_version=1,
            quality_status=EvidenceQualityStatus.PASS,
            quality_reason="evidence_requirements_satisfied",
            created_at=datetime.now(UTC),
            artifacts=tuple(
                ArtifactReference(
                    artifact_id=str(uuid4()),
                    artifact_type=kind,
                    role="probe_result_replay",
                    storage_path=f"task/{result_id}/{kind}",
                    checksum="sha256:" + "c" * 64,
                    size_bytes=3,
                    content_type="image/png" if kind == "screenshot" else "application/json",
                )
                for kind in ("screenshot", "action_trace")
            ),
        )
        for artifact in manifest.artifacts:
            await connection.execute(
                "INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,"
                "storage_path,checksum,size_bytes,content_type,source)"
                " VALUES($1,$2,$3,$4,$5,$6,$7,$8,'baseline')",
                artifact.artifact_id,
                ids["task"],
                result_id,
                artifact.artifact_type,
                artifact.storage_path,
                artifact.checksum,
                artifact.size_bytes,
                artifact.content_type,
            )
        await manifests.add(manifest=manifest)
        work_items = PostgresInspectionWorkItemRepository(pool=pool)
        consumption = EvidenceRequestConsumptionService(
            requests=requests,
            manifests=manifests,
            attacher=EvidenceSupplementAttacher(
                repository=PostgresEvidenceSupplementRepository(
                    pool=pool, supplemental_evidence_policy=policy
                ),
                supplemental_evidence_policy=policy,
            ),
            input_set_resealer=SupplementInputSetResealService(
                input_sets=PostgresEvidenceInputSetRepository(
                    pool=pool,
                    permitted_task_statuses=("running", "review"),
                    permitted_finalization_states=(
                        "open",
                        "exploration_draining",
                        "session_draining",
                    ),
                ),
                manifests=manifests,
                work_items=work_items,
                requests=requests,
            ),
        )
        proof = ProbeCompletionProof(
            request_id=request.request_id,
            result_capture_id=result_id,
            work_id=probe.work_id,
            restoration_attempt_id=attempt.attempt_id,
            equivalence_assessment_id=attempt.assessment.assessment_id,
            source_grade=EvidenceSourceGrade.C,
        )
        attached = await consumption.attach_supplement(task_id=ids["task"], proof=proof)
        assert attached.attached and attached.evidence_input_set_id
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_evidence_request WHERE request_id=$1",
                request.request_id,
            )
            == "satisfied"
        )
        assert (
            await connection.fetchval(
                "SELECT capture_id::text FROM qc_evidence_input_set_member WHERE input_set_id=$1",
                attached.evidence_input_set_id,
            )
            == result_id
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_outbox_event WHERE task_id=$1"
                " AND message_type='alioth.v2.evidence-supplement.attached'",
                ids["task"],
            )
            == 1
        )
        duplicate = await consumption.attach_supplement(task_id=ids["task"], proof=proof)
        assert duplicate.evidence_input_set_id == attached.evidence_input_set_id
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_evidence_supplement WHERE request_id=$1",
                request.request_id,
            )
            == 1
        )
