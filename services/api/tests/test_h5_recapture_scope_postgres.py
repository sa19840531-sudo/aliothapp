"""补采原会话、恢复会话及关闭屏障在 PostgreSQL 临时表中的回归。"""

import json
import os
from contextlib import asynccontextmanager
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest

from services.api.app.application.exploration_work_queue import (
    ClaimExplorerWorkCommand,
    ExplorerWorkPersistenceError,
    ExplorerWorkStatus,
    ExplorerWorkType,
)
from services.api.app.application.h5_live_inspection_publish import (
    PostgresH5ActiveSessionInspectionStateReader,
)
from services.api.app.application.h5_probe_work import H5ProbeWorkError, H5ProbeWorkOutcome
from services.api.app.application.h5_targeted_recapture import H5TargetedRecapturePlanService
from services.api.app.application.orchestration import QcOrchestrator
from services.api.app.application.restoration import RestorationCoordinator
from services.api.app.application.restored_probe_publication import RestoredProbePublicationService
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.application.supplemental_evidence_policy import (
    build_lab_supplemental_evidence_policy,
)
from services.api.app.domain.evidence import EvidenceQualityStatus
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    RestorationStrategy,
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.evidence_request_repository import (
    EvidenceRequestPersistenceError,
    PostgresEvidenceRequestRepository,
)
from services.api.app.infrastructure.db.explorer_work_repository import (
    PostgresExplorerWorkRepository,
    PostgresProbeWorkPublisher,
)
from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)
from services.api.app.infrastructure.db.h5_probe_completion_repository import (
    PostgresH5ProbeEvidencePreparer,
)
from services.api.app.infrastructure.db.h5_probe_terminal_repository import (
    PostgresH5ProbeTerminalSettlementRepository,
)
from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
    PostgresH5TargetedRecaptureRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    PostgresRestorationAttemptRepository,
)
from services.api.tests.test_h5_targeted_recapture import _request
from services.api.tests.test_inspection_closure_settlement_postgres import (
    _seed_task as _seed_closure_task,
)
from services.api.tests.test_inspection_closure_settlement_postgres import (
    _seed_work_item as _seed_closure_work,
)
from services.api.tests.test_targeted_recapture_chain_postgres import (
    _budget,
    _ObserveOnlyExecutor,
    _seed,
    _snapshot,
)
from services.api.tests.test_targeted_recapture_chain_postgres import (
    _request as _chain_request,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式指定 PostgreSQL 测试连接",
)


@pytest.mark.parametrize("change,expected", [
    (None, 1), ("platform", 0), ("epoch", 0), ("lease", 0),
    ("due", 0), ("dependency", 0), ("run", 0), ("closed", 0),
])
@pytest.mark.parametrize("include_ready", [False, True])
async def test_deferred_snapshot_inventory_is_h5_owned_future_unleased_only(
    change, expected, include_ready,
):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid,target_platform text,status text,
                finalization_state text,finalization_epoch int);
            CREATE TEMP TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid);
            CREATE TEMP TABLE qc_evidence_request(request_id text,request_payload jsonb,
                task_id uuid,target_capture_id uuid,finalization_epoch int,
                session_dependency text,status text,lease_token text,
                next_attempt_at timestamptz,expires_at timestamptz);
        """)
        task, run, capture = (str(uuid4()) for _ in range(3))
        request = build_evidence_request(
            work_item_id="work", attempt_id="attempt", task_id=task,
            rule_id="rule", rule_code="rule", target_capture_id=capture,
            reason_code="EVIDENCE_INSUFFICIENT", evidence_goal="补采目标截图",
            required_artifact_types=("screenshot",),
            session_dependency=EvidenceSessionDependency.SNAPSHOT,
            replay_policy=EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
            required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
            side_effect_policy=_request().side_effect_policy,
            target_invariants=_request().target_invariants,
            minimum_source_grade=_request().minimum_source_grade,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        await connection.execute(
            "INSERT INTO qc_task VALUES($1,$2,'running',$3,0)", task,
            "web" if change == "platform" else "h5",
            "sealed" if change == "closed" else "open",
        )
        await connection.execute(
            "INSERT INTO qc_capture VALUES($1,$2,$3)", capture, task,
            str(uuid4()) if change == "run" else run,
        )
        await connection.execute(
            "INSERT INTO qc_evidence_request VALUES($1,$2,$3,$4,$5,$6,'pending',$7,$8,$9)",
            request.request_id, json.dumps(request.to_dict()), task, capture,
            1 if change == "epoch" else 0,
            "active_session" if change == "dependency" else "snapshot",
            "lease" if change == "lease" else None,
            datetime.now(UTC) + timedelta(seconds=-1 if change == "due" else 60),
            request.expires_at,
        )

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield connection

        repository = PostgresH5TargetedRecaptureRepository(pool=Pool())
        requests = await repository.list_deferred_snapshot_requests(
            task_id=task, agent_run_id=run, include_ready=include_ready,
        )
        assert len(requests) == (1 if change == "due" and include_ready else expected)
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.parametrize("platform,dependency,session_status,attached", [
    ("h5", "active_session", "closed", False),
    ("h5", "snapshot", "closed", False),
    ("h5", "snapshot", "lost", False),
    ("h5", "active_session", "lost", False),
    ("h5", "snapshot", "active", False),
    ("h5", "active_session", "active", False),
    ("h5", "active_session", "closed", True),
    ("h5", "snapshot", "lost", True),
    ("web", "active_session", "closed", False),
    ("app", "snapshot", "lost", False),
    ("h5", "snapshot", "draining", False),
    ("h5", "active_session", "closing", False),
])
@pytest.mark.parametrize("main_status,live_lease,fence", [
    ("running", False, None), ("failed", False, None), ("failed", True, None),
    ("failed", False, "owner"), ("failed", False, "epoch"),
    ("failed", False, "main_run"), ("failed", False, "agent_type"),
])
async def test_closed_h5_session_settles_late_requests_without_touching_live_or_other_surfaces(
    platform, dependency, session_status, attached, main_status, live_lease, fence,
):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid,target_platform text,status text,
                finalization_state text,finalization_epoch int,finalization_agent_run_id uuid);
            CREATE TEMP TABLE qc_agent_run(id uuid,task_id uuid,agent_type text,status text);
            CREATE TEMP TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid,
                surface_session_id text);
            CREATE TEMP TABLE qc_surface_session(session_id text,task_id uuid,status text,
                surface_kind text,session_owner text,finalization_epoch int,
                lease_owner text,lease_token text,lease_expires_at timestamptz,
                current_surface_ref text,failure_reason text,lost_at timestamptz,
                row_version int,updated_at timestamptz);
            CREATE TEMP TABLE qc_surface_ref(task_id uuid,session_id text,status text,
                pin_reason text,released_at timestamptz,row_version int,updated_at timestamptz);
            CREATE TEMP TABLE qc_inspection_work_item(id uuid,task_id uuid,page_capture_id uuid,
                producer_agent_run_id uuid,finalization_epoch int,control_mode text);
            CREATE TEMP TABLE qc_evidence_request(request_id text,task_id uuid,work_item_id uuid,
                target_capture_id uuid,finalization_epoch int,session_dependency text,
                replay_policy text,status text,resolution_reason text,lease_owner text,
                lease_token text,lease_expires_at timestamptz,row_version int,
                completed_at timestamptz,updated_at timestamptz);
            CREATE TEMP TABLE qc_v2_work_item(work_id text,task_id uuid,evidence_request_id text,
                finalization_epoch int,work_type text,status text,work_payload jsonb,
                last_error_code text,last_error_message text,lease_owner text,
                lease_token text,lease_expires_at timestamptz,row_version int,
                finished_at timestamptz,updated_at timestamptz);
            CREATE TEMP TABLE qc_evidence_supplement(request_id text);
        """)
        task, run, capture, work = (str(uuid4()) for _ in range(4))
        session = f"h5:{run}"
        await connection.execute(
            "INSERT INTO qc_task VALUES($1,$2,'running','session_draining',0,$3)",
            task, platform, run,
        )
        await connection.execute("INSERT INTO qc_agent_run VALUES($1,$2,'main',$3)",
                                 run, task, main_status)
        await connection.execute(
            "INSERT INTO qc_capture VALUES($1,$2,$3,$4)", capture, task, run, session,
        )
        await connection.execute(
            "INSERT INTO qc_surface_session(session_id,task_id,status,surface_kind,session_owner,"
            "finalization_epoch,lease_expires_at,row_version) "
            "VALUES($1,$2,$3,'web','h5-workflow',0,$4,0)",
            session, task, session_status,
            datetime.now(UTC)+timedelta(minutes=10) if live_lease else None,
        )
        await connection.execute(
            "INSERT INTO qc_surface_ref VALUES($1,$2,'active',NULL,NULL,0,NULL)", task, session,
        )
        await connection.execute(
            "INSERT INTO qc_inspection_work_item VALUES($1,$2,$3,$4,0,'enforced')",
            work, task, capture, run,
        )
        await connection.execute(
            "INSERT INTO qc_evidence_request VALUES('request-late',$1,$2,$3,0,$4,$5,"
            "'pending',NULL,NULL,NULL,NULL,1,NULL,now())",
            task, work, capture, dependency,
            'new_session_allowed' if dependency == 'snapshot' else 'same_context_allowed',
        )
        await connection.execute(
            "INSERT INTO qc_v2_work_item VALUES('probe-late',$1,'request-late',0,'probe',"
            "'pending','{\"surface_kind\":\"web\"}',NULL,NULL,NULL,NULL,NULL,1,NULL,now())", task,
        )
        if attached:
            await connection.execute("INSERT INTO qc_evidence_supplement VALUES('request-late')")
        if fence == "owner":
            await connection.execute("UPDATE qc_surface_session SET session_owner='other-owner'")
        elif fence == "epoch":
            await connection.execute("UPDATE qc_surface_session SET finalization_epoch=1")
        elif fence == "main_run":
            await connection.execute(
                "UPDATE qc_task SET finalization_agent_run_id=$1", str(uuid4()),
            )
        elif fence == "agent_type":
            await connection.execute("UPDATE qc_agent_run SET agent_type='rule_judge'")
        repository = PostgresH5ProbeTerminalSettlementRepository(pool=_Pool(connection))
        orphan = (platform == 'h5' and session_status in {'active', 'draining', 'closing'}
                  and main_status == 'failed' and not live_lease and fence is None)
        eligible = (platform == 'h5' and (session_status in {'closed', 'lost'} or orphan)
                    and not attached and fence not in {'owner', 'epoch'})
        assert await repository.settle_requests_after_session_close(
            task_id=task, agent_run_id=run,
        ) == int(eligible)
        assert await connection.fetchval("SELECT status FROM qc_evidence_request") == (
            'unfulfillable' if eligible else 'pending'
        )
        assert await connection.fetchval("SELECT status FROM qc_v2_work_item") == (
            'failed' if eligible else 'pending'
        )
        if eligible:
            reason = await connection.fetchval("SELECT resolution_reason FROM qc_evidence_request")
            assert reason == (
                'H5_RECAPTURE_SESSION_LOST' if session_status == 'lost' or orphan
                else 'H5_RECAPTURE_SESSION_CLOSED'
            )
        assert await connection.fetchval("SELECT status FROM qc_surface_session") == (
            'lost' if orphan else session_status
        )
        assert await connection.fetchval("SELECT status FROM qc_surface_ref") == (
            'lost' if orphan else 'active'
        )
        assert await repository.settle_requests_after_session_close(
            task_id=task, agent_run_id=run,
        ) == 0
    finally:
        await transaction.rollback()
        await connection.close()


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.parametrize("platform,state,epoch,request_status,linked,eligible", [
    ("h5", "session_draining", 0, "unfulfillable", True, True),
    ("h5", "open", 0, "satisfied", True, True),
    ("h5", "exploration_draining", 0, "cancelled", True, True),
    ("h5", "session_draining", 0, "expired", True, True),
    ("h5", "session_draining", 0, "pending", True, False),
    ("h5", "session_draining", 0, "routed", True, False),
    ("h5", "session_draining", 0, "running", True, False),
    ("h5", "sealed", 0, "unfulfillable", True, False),
    ("h5", "session_draining", 1, "unfulfillable", True, False),
    ("h5", "session_draining", 0, "unfulfillable", False, False),
    ("web", "session_draining", 0, "unfulfillable", True, False),
    ("app", "session_draining", 0, "unfulfillable", True, False),
])
async def test_h5_background_terminal_settlement_scope(
    monkeypatch, platform, state, epoch, request_status, linked, eligible,
):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid,target_platform text,status text,
                finalization_state text,finalization_epoch int);
            CREATE TEMP TABLE qc_h5_inspection_publication(id uuid,task_id uuid,
                finalization_epoch int);
            CREATE TEMP TABLE qc_h5_inspection_publication_work_item(publication_id uuid,
                work_item_id uuid);
            CREATE TEMP TABLE qc_inspection_work_item(id uuid,task_id uuid,
                finalization_epoch int,control_mode text,status text);
            CREATE TEMP TABLE qc_evidence_request(task_id uuid,work_item_id uuid,
                finalization_epoch int,status text);
        """)
        task, publication, work = (uuid4() for _ in range(3))
        await connection.execute(
            "INSERT INTO qc_task VALUES($1,$2,'review',$3,0)", task, platform, state,
        )
        await connection.execute(
            "INSERT INTO qc_h5_inspection_publication VALUES($1,$2,$3)",
            publication, task, epoch,
        )
        await connection.execute(
            "INSERT INTO qc_inspection_work_item VALUES($1,$2,0,'enforced','waiting_evidence')",
            work, task,
        )
        if linked:
            await connection.execute(
                "INSERT INTO qc_h5_inspection_publication_work_item VALUES($1,$2)",
                publication, work,
            )
        await connection.execute(
            "INSERT INTO qc_evidence_request VALUES($1,$2,0,$3)", task, work, request_status,
        )
        settlement = SimpleNamespace(reopen_waiting_evidence=AsyncMock(return_value=0))
        monkeypatch.setattr(
            "services.api.app.infrastructure.db.h5_inspection_publication_repository."
            "PostgresInspectionWorkItemRepository",
            lambda **_: settlement,
        )
        repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))
        assert await repository.reconcile_waiting_evidence() == 0
        if eligible:
            settlement.reopen_waiting_evidence.assert_awaited_once_with(task_id=str(task))
            # 旧终态请求不能绕过同一工作项后来追加的活请求。
            settlement.reopen_waiting_evidence.reset_mock()
            await connection.execute(
                "INSERT INTO qc_evidence_request VALUES($1,$2,0,'pending')", task, work,
            )
            assert await repository.reconcile_waiting_evidence() == 0
        settlement.reopen_waiting_evidence.assert_not_awaited()
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.parametrize("platform,state,epoch,expired,eligible", [
    ("h5", "session_draining", 0, True, True),
    ("h5", "open", 0, True, True),
    ("h5", "sealed", 0, True, False),
    ("h5", "session_draining", 1, True, False),
    ("h5", "session_draining", 0, False, False),
    ("web", "session_draining", 0, True, False),
    ("app", "session_draining", 0, True, False),
])
async def test_h5_background_expires_only_owned_current_epoch_requests(
    monkeypatch, platform, state, epoch, expired, eligible,
):
    from services.api.app.infrastructure.db import h5_probe_terminal_repository

    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid,target_platform text,status text,
                finalization_state text,finalization_epoch int);
            CREATE TEMP TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid);
            CREATE TEMP TABLE qc_evidence_request(task_id uuid,target_capture_id uuid,
                finalization_epoch int,status text,session_dependency text,expires_at timestamptz);
        """)
        task, run, capture = (uuid4() for _ in range(3))
        await connection.execute(
            "INSERT INTO qc_task VALUES($1,$2,'review',$3,0)", task, platform, state
        )
        await connection.execute("INSERT INTO qc_capture VALUES($1,$2,$3)", capture, task, run)
        await connection.execute(
            "INSERT INTO qc_evidence_request VALUES($1,$2,$3,'pending','active_session',"
            "now()+($4::int * interval '1 second'))", task, capture, epoch, -60 if expired else 60,
        )
        settlement = SimpleNamespace(settle_ready=AsyncMock(return_value=1))
        monkeypatch.setattr(
            h5_probe_terminal_repository, "PostgresH5ProbeTerminalSettlementRepository",
            lambda **_: settlement,
        )
        repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))
        result = await repository.reconcile_expired_evidence()
        assert result == int(eligible)
        if eligible:
            settlement.settle_ready.assert_awaited_once_with(
                task_id=str(task), agent_run_id=str(run)
            )
        else:
            settlement.settle_ready.assert_not_awaited()
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.parametrize("platform,previous_status,placeholder", [
    ("h5", "skipped", True), ("h5", "pass", False), ("h5", "fail", False),
    ("h5", "skipped", False), ("web", "skipped", True), ("app", "skipped", True),
])
async def test_h5_terminal_request_reconciles_producer_placeholder_only(
    platform, previous_status, placeholder,
):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        for table in (
            "qc_tenant", "qc_user", "qc_ruleset", "qc_rule", "qc_task", "qc_agent_run",
            "qc_page_capture", "qc_inspection_work_item", "qc_inspection_work_item_rule",
            "qc_inspection_attempt", "qc_evidence_request", "qc_rule_execution",
            "qc_rule_execution_revision", "qc_review_item", "qc_outbox_event",
        ):
            await connection.execute(
                f"CREATE TEMP TABLE {table} "
                f"(LIKE public.{table} INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING INDEXES) "
                "ON COMMIT DROP"
            )
        await connection.execute("""
            CREATE TRIGGER test_source_identity_guard
            BEFORE UPDATE OF source_idempotency_key ON qc_rule_execution
            FOR EACH ROW EXECUTE FUNCTION public.qc_guard_rule_execution_source_identity();
            CREATE TRIGGER test_revision_retention
            AFTER UPDATE ON qc_rule_execution FOR EACH ROW
            WHEN (OLD.execution_status IS DISTINCT FROM NEW.execution_status)
            EXECUTE FUNCTION public.qc_keep_rule_execution_revision();
            CREATE TEMP TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid,capture_status text,
                parent_capture_id uuid,surface_session_id text,
                action_execution_id text,capture_role text);
            CREATE TEMP TABLE qc_evidence_manifest(manifest_id text,task_id uuid,
                capture_id uuid,quality_status text);
            CREATE TEMP TABLE qc_evidence_supplement(request_id text,manifest_id text);
        """)
        ids = {key: str(uuid4()) for key in ("tenant", "user", "ruleset", "rule", "task", "run")}
        await _seed_closure_task(connection, ids)
        await connection.execute(
            "UPDATE qc_task SET target_platform=$1,status='review',"
            "finalization_state='session_draining'",
            platform,
        )
        consumer = str(uuid4())
        work, capture = await _seed_closure_work(
            connection, ids, name="conflict", page_seq=1, status="waiting_evidence",
            attempt_count=1, request_status="unfulfillable", consumer_run_id=consumer,
        )
        execution_id = uuid4()
        await connection.execute("""
            INSERT INTO qc_rule_execution(id,task_id,rule_id,page_capture_id,agent_run_id,
                execution_status,judge_strategy,severity,result_summary,judge_output)
            VALUES($1,$2::uuid,$3::uuid,$4::uuid,$5::uuid,$6,'hybrid','medium','旧结果',$7::jsonb)
        """, execution_id, ids["task"], ids["rule"], capture, ids["run"], previous_status,
            json.dumps({"reason": "candidate_not_executed" if placeholder else "verified_result"}))
        repository = PostgresInspectionWorkItemRepository(pool=_Pool(connection))
        assert await repository.reopen_waiting_evidence(task_id=ids["task"]) == 0
        execution = await connection.fetchrow("SELECT * FROM qc_rule_execution")
        assert execution["id"] == execution_id
        assert execution["source_idempotency_key"] is None
        if platform == "h5" and placeholder:
            expected_status = "evidence_insufficient" if placeholder else previous_status
            assert execution["execution_status"] == expected_status
            expected_work = (
                "partial" if expected_status in {"skipped", "evidence_insufficient"}
                else "succeeded"
            )
            assert await connection.fetchval(
                "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid", work,
            ) == expected_work
            assert await connection.fetchval(
                "SELECT rule_execution_id FROM qc_inspection_work_item_rule",
            ) == execution_id
            assert await repository.reopen_waiting_evidence(task_id=ids["task"]) == 0
            assert await connection.fetchval("SELECT count(*) FROM qc_rule_execution") == 1
            revisions = await connection.fetchval("SELECT count(*) FROM qc_rule_execution_revision")
            assert revisions == int(placeholder)
        else:
            # 用户确认严格合同：没有原 InputSet 的已判结论不能跨消费者借用。
            assert execution["execution_status"] == previous_status
            status = await connection.fetchval("SELECT status FROM qc_inspection_work_item")
            assert status == "waiting_evidence"
            assert await connection.fetchval(
                "SELECT rule_execution_id FROM qc_inspection_work_item_rule",
            ) is None
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.parametrize(
    "platform,role,failure",
    [
        ("h5", "result", None),
        ("h5", "before", None),
        ("h5", "baseline", None),
        ("web", "baseline", None),
        ("app", "baseline", None),
        ("web", "result", None),
        ("app", "result", None),
        ("h5", "result", "run"),
        ("h5", "result", "task"),
        ("h5", "result", "epoch"),
        ("h5", "result", "rule"),
        ("h5", "result", "manifest"),
        ("h5", "result", "unattached"),
        ("h5", "result", "pending"),
        ("h5", "before", "action"),
        ("h5", "before", "session"),
        ("h5", "before", "before_manifest"),
        ("h5", "result", "exit_reserved"),
    ],
)
async def test_h5_followup_request_insert_requires_verified_supplement(platform, role, failure):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        for table in (
            "qc_tenant",
            "qc_user",
            "qc_ruleset",
            "qc_rule",
            "qc_task",
            "qc_agent_run",
            "qc_page_capture",
            "qc_inspection_work_item",
            "qc_inspection_work_item_rule",
            "qc_inspection_attempt",
            "qc_evidence_request",
            "qc_rule_execution",
            "qc_review_item",
            "qc_outbox_event",
        ):
            await connection.execute(
                f"CREATE TEMP TABLE {table} "
                f"(LIKE public.{table} INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING INDEXES) "
                "ON COMMIT DROP"
            )
        await connection.execute("""
            CREATE TEMP TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid,capture_status text,
                parent_capture_id uuid,surface_session_id text,
                action_execution_id text,capture_role text);
            CREATE TEMP TABLE qc_evidence_manifest(manifest_id text,task_id uuid,
                capture_id uuid,quality_status text);
            CREATE TEMP TABLE qc_evidence_supplement(request_id text,manifest_id text);
            CREATE TEMP TABLE qc_evidence_exit_gate_evaluation(task_id uuid,
                source_capture_id uuid,reservation_status text,expires_at timestamptz);
        """)
        ids = {key: str(uuid4()) for key in ("tenant", "user", "ruleset", "rule", "task", "run")}
        await _seed_closure_task(connection, ids)
        await connection.execute(
            "UPDATE qc_task SET target_platform=$1,status='running',finalization_state='open'",
            platform,
        )
        work, baseline = await _seed_closure_work(
            connection,
            ids,
            name="insert",
            page_seq=1,
            status="running",
            attempt_count=2,
            request_status="satisfied",
        )
        before, result = uuid4(), uuid4()
        await connection.execute(
            "INSERT INTO qc_capture VALUES($1,$2::uuid,$3::uuid,'captured',NULL,"
            "'session-1','action-1','probe_before'),"
            "($4,$2::uuid,$3::uuid,'captured',$1,'session-1','action-1','probe_result_replay')",
            before,
            ids["task"],
            ids["run"],
            result,
        )
        await connection.execute(
            "INSERT INTO qc_evidence_manifest VALUES('before',$1::uuid,$2,'pass'),"
            "('result',$1::uuid,$3,'pass')",
            ids["task"],
            before,
            result,
        )
        await connection.execute(
            "UPDATE qc_evidence_request SET resolution_manifest_id='result',"
            "created_at=now()-interval '2 seconds'"
        )
        await connection.execute(
            "INSERT INTO qc_evidence_supplement VALUES('request-insert','result')"
        )
        target = UUID(baseline) if role == "baseline" else before if role == "before" else result
        if failure in {"run", "task"}:
            column = "agent_run_id" if failure == "run" else "task_id"
            await connection.execute(
                f"UPDATE qc_capture SET {column}=$1 WHERE id=$2", uuid4(), result
            )
        elif failure in {"epoch", "rule"}:
            column, value = (
                ("finalization_epoch", 1) if failure == "epoch" else ("rule_id", uuid4())
            )
            await connection.execute(f"UPDATE qc_evidence_request SET {column}=$1", value)
        elif failure in {"manifest", "before_manifest"}:
            await connection.execute(
                "UPDATE qc_evidence_manifest SET quality_status='fail' WHERE manifest_id=$1",
                "before" if failure == "before_manifest" else "result",
            )
        elif failure == "unattached":
            await connection.execute("DELETE FROM qc_evidence_supplement")
        elif failure == "pending":
            await connection.execute("UPDATE qc_evidence_request SET status='pending'")
        elif failure in {"action", "session"}:
            column = "action_execution_id" if failure == "action" else "surface_session_id"
            await connection.execute(
                f"UPDATE qc_capture SET {column}='foreign' WHERE id=$1", before
            )
        elif failure == "exit_reserved":
            await connection.execute(
                "INSERT INTO qc_evidence_exit_gate_evaluation VALUES($1::uuid,$2,'reserved',"
                "now()+interval '1 minute')",
                ids["task"],
                target,
            )
        attempt = await connection.fetchval("SELECT id FROM qc_inspection_attempt")
        request = _chain_request(
            {**ids, "work_item": work, "attempt": str(attempt), "original": str(target)},
            rule_code="alioth_closure_1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        repository = PostgresEvidenceRequestRepository(pool=_Pool(connection))
        if failure is None and (platform == "h5" or role == "baseline"):
            await repository.upsert_in_transaction(connection, request=request)
            await repository.upsert_in_transaction(connection, request=request)
            stored = await connection.fetchrow(
                "SELECT target_capture_id,work_item_id FROM qc_evidence_request "
                "WHERE request_id=$1",
                request.request_id,
            )
            assert stored["target_capture_id"] == target
            assert str(stored["work_item_id"]) == work
            assert (
                str(
                    await connection.fetchval("SELECT page_capture_id FROM qc_inspection_work_item")
                )
                == baseline
            )
            if role != "baseline":
                await connection.execute(
                    "UPDATE qc_inspection_work_item SET status='waiting_evidence'"
                )
                await connection.execute(
                    "UPDATE qc_evidence_request SET status='unfulfillable',completed_at=now(),"
                    "resolution_reason='本次动作未获得所需证据' WHERE request_id=$1",
                    request.request_id,
                )
                work_repository = PostgresInspectionWorkItemRepository(pool=_Pool(connection))
                assert await work_repository.reopen_waiting_evidence(task_id=ids["task"]) == 0
                assert (
                    await connection.fetchval("SELECT status FROM qc_inspection_work_item")
                    == "partial"
                )
                execution = await connection.fetchrow("SELECT * FROM qc_rule_execution")
                assert str(execution["page_capture_id"]) == baseline
                assert execution["execution_status"] == "evidence_insufficient"
        else:
            with pytest.raises(EvidenceRequestPersistenceError):
                await repository.upsert_in_transaction(connection, request=request)
            assert await connection.fetchval("SELECT count(*) FROM qc_evidence_request") == 1
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "platform",
        "run",
        "epoch",
        "action",
        "satisfied",
        "attached",
    ],
)
async def test_permanent_probe_attachment_failure_is_scoped_and_idempotent(failure):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid,target_platform text,status text,
                finalization_epoch int, finalization_state text DEFAULT 'open');
            CREATE TEMP TABLE qc_evidence_request(request_id text,task_id uuid,
                finalization_epoch int,session_dependency text,status text,resolution_reason text,
                lease_owner text,lease_token text,lease_expires_at timestamptz,
                completed_at timestamptz,updated_at timestamptz,row_version int);
            CREATE TEMP TABLE qc_v2_work_item(work_id text,task_id uuid,evidence_request_id text,
                finalization_epoch int,work_type text,status text,work_payload jsonb,
                source_capture_id uuid,session_id text);
            CREATE TEMP TABLE qc_action_execution(action_execution_id text,task_id uuid,
                status text,session_id text,finalization_epoch int,intent_payload jsonb,
                result_capture_id uuid);
            CREATE TEMP TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid,
                surface_session_id text,action_execution_id text,capture_role text);
            CREATE TEMP TABLE qc_evidence_supplement(request_id text);
        """)
        task, run, source, result = (str(uuid4()) for _ in range(4))
        await connection.execute(
            "INSERT INTO qc_task VALUES($1::uuid,$2,'running',0)",
            task,
            "web" if failure == "platform" else "h5",
        )
        await connection.execute(
            "INSERT INTO qc_evidence_request VALUES('request-1',$1::uuid,0,'active_session',"
            "$2,NULL,'owner','token',now(),NULL,now(),1)",
            task,
            "satisfied" if failure == "satisfied" else "running",
        )
        await connection.execute(
            "INSERT INTO qc_v2_work_item VALUES('work-1',$1::uuid,'request-1',$2,'probe',"
            "'succeeded','{\"surface_kind\":\"web\"}',$3::uuid,'h5:session')",
            task,
            1 if failure == "epoch" else 0,
            source,
        )
        await connection.execute(
            "INSERT INTO qc_capture VALUES($1::uuid,$2::uuid,$3::uuid,'h5:session',NULL,'source'),"
            "($4::uuid,$2::uuid,$3::uuid,'h5:session',$5,'probe_result_replay')",
            source,
            task,
            str(uuid4()) if failure == "run" else run,
            result,
            "wrong-action" if failure == "action" else "action-1",
        )
        await connection.execute(
            "INSERT INTO qc_action_execution VALUES('action-1',$1::uuid,'completed','h5:session',"
            '0,\'{"parameters":{"exploration_work_id":"work-1"}}\',$2::uuid)',
            task,
            result,
        )
        if failure == "attached":
            await connection.execute("INSERT INTO qc_evidence_supplement VALUES('request-1')")
        outcome = H5ProbeWorkOutcome(
            claimed=True,
            work_id="work-1",
            final_status=ExplorerWorkStatus.SUCCEEDED,
            result_capture_id=result,
            evidence_request_id="request-1",
        )
        repository = PostgresH5ProbeTerminalSettlementRepository(pool=_Pool(connection))
        kwargs = dict(task_id=task, agent_run_id=run, outcome=outcome, error_code="QUALITY_FAILED")
        assert await repository.settle_completion_failure(**kwargs) == (1 if failure is None else 0)
        assert await repository.settle_completion_failure(**kwargs) == 0
        assert await connection.fetchval("SELECT status FROM qc_v2_work_item") == "succeeded"
        expected = (
            "unfulfillable"
            if failure is None
            else "satisfied"
            if failure == "satisfied"
            else "running"
        )
        assert await connection.fetchval("SELECT status FROM qc_evidence_request") == expected
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,latest_status,primary_before,failure",
    [
        ("h5", "satisfied", False, None),
        ("h5", "unfulfillable", False, None),
        ("h5", "satisfied", True, None),
        ("h5", "unfulfillable", True, None),
        ("web", "unfulfillable", False, None),
        ("app", "unfulfillable", False, None),
        ("h5", "unfulfillable", False, "run"),
        ("h5", "unfulfillable", False, "manifest"),
        ("h5", "unfulfillable", False, "epoch"),
        ("h5", "unfulfillable", True, "action"),
    ],
)
async def test_h5_followup_request_uses_verified_supplement_and_latest_terminal(
    platform,
    latest_status,
    primary_before,
    failure,
):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        for table in (
            "qc_tenant",
            "qc_user",
            "qc_ruleset",
            "qc_rule",
            "qc_task",
            "qc_agent_run",
            "qc_page_capture",
            "qc_inspection_work_item",
            "qc_inspection_work_item_rule",
            "qc_inspection_attempt",
            "qc_evidence_request",
            "qc_rule_execution",
            "qc_review_item",
            "qc_outbox_event",
        ):
            await connection.execute(
                f"CREATE TEMP TABLE {table} "
                f"(LIKE public.{table} INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING INDEXES) "
                "ON COMMIT DROP"
            )
        await connection.execute("""
            CREATE TEMP TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid,capture_status text,
                parent_capture_id uuid,surface_session_id text,
                action_execution_id text,capture_role text);
            CREATE TEMP TABLE qc_evidence_manifest(manifest_id text,task_id uuid,
                capture_id uuid,quality_status text);
            CREATE TEMP TABLE qc_evidence_supplement(request_id text,manifest_id text);
        """)
        ids = {key: str(uuid4()) for key in ("tenant", "user", "ruleset", "rule", "task", "run")}
        await _seed_closure_task(connection, ids)
        await connection.execute("UPDATE qc_task SET target_platform=$1", platform)
        work, baseline = await _seed_closure_work(
            connection,
            ids,
            name="followup",
            page_seq=1,
            status="waiting_evidence",
            attempt_count=2,
            request_status="satisfied",
        )
        supplement, latest_result = uuid4(), uuid4()
        await connection.execute(
            "INSERT INTO qc_capture(id,task_id,agent_run_id,capture_status) "
            "VALUES($1,$2::uuid,$3::uuid,'captured'),"
            "($4,$2::uuid,$3::uuid,'captured')",
            supplement,
            ids["task"],
            ids["run"],
            latest_result,
        )
        prior_result = supplement
        if primary_before:
            prior_result = uuid4()
            await connection.execute(
                "INSERT INTO qc_capture VALUES($1,$2::uuid,$3::uuid,'captured',$4,"
                "'session-1','action-1','probe_result_replay')",
                prior_result,
                ids["task"],
                ids["run"],
                supplement,
            )
            await connection.execute(
                "UPDATE qc_capture SET surface_session_id='session-1',"
                "action_execution_id='action-1',"
                "capture_role='probe_before' WHERE id=$1",
                supplement,
            )
            await connection.execute(
                "INSERT INTO qc_evidence_manifest VALUES('before-manifest',$1::uuid,$2,'pass')",
                ids["task"],
                supplement,
            )
        await connection.execute(
            "INSERT INTO qc_evidence_manifest VALUES('prior-manifest',$1::uuid,$2,'pass'),"
            "('latest-manifest',$1::uuid,$3,'pass')",
            ids["task"],
            prior_result,
            latest_result,
        )
        await connection.execute(
            "UPDATE qc_evidence_request SET resolution_manifest_id='prior-manifest',"
            "created_at=now()-interval '2 seconds'"
        )
        await connection.execute(
            "INSERT INTO qc_evidence_supplement VALUES('request-followup','prior-manifest')"
        )
        await connection.execute(
            """
            INSERT INTO qc_evidence_request (
                request_id,requirement_hash,payload_hash,protocol_version,
                work_item_id,attempt_id,task_id,rule_id,rule_code,target_capture_id,
                session_dependency,replay_policy,required_equivalence,side_effect_policy,
                minimum_source_grade,request_payload,status,expires_at,completed_at,
                finalization_epoch,resolution_manifest_id,created_at
            )
            SELECT 'latest-request','latest-requirement','latest-payload',protocol_version,
                work_item_id,attempt_id,task_id,rule_id,rule_code,$1,
                session_dependency,replay_policy,required_equivalence,side_effect_policy,
                minimum_source_grade,request_payload,$2,expires_at,now(),
                finalization_epoch,'latest-manifest',now()-interval '1 second'
            FROM qc_evidence_request WHERE request_id='request-followup'
        """,
            supplement,
            latest_status,
        )
        if latest_status == "satisfied":
            await connection.execute(
                "INSERT INTO qc_evidence_supplement VALUES('latest-request','latest-manifest')"
            )
        if failure == "run":
            await connection.execute(
                "UPDATE qc_capture SET agent_run_id=$1 WHERE id=$2",
                uuid4(),
                prior_result,
            )
        elif failure == "manifest":
            await connection.execute(
                "UPDATE qc_evidence_manifest SET quality_status='fail' "
                "WHERE manifest_id='prior-manifest'"
            )
        elif failure == "epoch":
            await connection.execute(
                "UPDATE qc_evidence_request SET finalization_epoch=1 "
                "WHERE request_id='latest-request'"
            )
        elif failure == "action":
            await connection.execute(
                "UPDATE qc_capture SET action_execution_id='foreign-action' WHERE id=$1",
                supplement,
            )
        repository = PostgresInspectionWorkItemRepository(pool=_Pool(connection))
        reopened = await repository.reopen_waiting_evidence(task_id=ids["task"])
        state = await connection.fetchval(
            "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid",
            work,
        )
        if platform == "h5" and latest_status == "unfulfillable" and failure is None:
            assert reopened == 0
            assert state == "partial"
            execution = await connection.fetchrow("SELECT * FROM qc_rule_execution")
            assert str(execution["page_capture_id"]) == baseline
            assert execution["execution_status"] == "evidence_insufficient"
            assert execution["source_idempotency_key"] == "evidence-request-terminal:latest-request"
            assert await repository.reopen_waiting_evidence(task_id=ids["task"]) == 0
        elif failure == "manifest":
            assert reopened == 0
            assert state == "waiting_evidence"
            assert await connection.fetchval("SELECT count(*) FROM qc_rule_execution") == 0
        else:
            assert reopened == 1
            assert state == "pending"
            assert await connection.fetchval("SELECT count(*) FROM qc_rule_execution") == 0
            assert await connection.fetchval("SELECT count(*) FROM qc_outbox_event") == 1
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["h5", "web", "app"])
async def test_h5_checkpoint_only_settles_exhausted_unleased_work(platform):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        # 临时表复用真实字段、默认值及唯一键，不复制业务外键和触发器，事务结束全部回滚。
        for table in (
            "qc_tenant",
            "qc_user",
            "qc_ruleset",
            "qc_rule",
            "qc_task",
            "qc_agent_run",
            "qc_page_capture",
            "qc_inspection_work_item",
            "qc_inspection_work_item_rule",
            "qc_inspection_attempt",
            "qc_evidence_request",
            "qc_rule_execution",
            "qc_review_item",
        ):
            await connection.execute(
                f"CREATE TEMP TABLE {table} "
                f"(LIKE public.{table} INCLUDING DEFAULTS INCLUDING GENERATED "
                "INCLUDING INDEXES) ON COMMIT DROP"
            )
        ids = {key: str(uuid4()) for key in ("tenant", "user", "ruleset", "rule", "task", "run")}
        await _seed_closure_task(connection, ids)
        await connection.execute(
            "UPDATE qc_task SET target_platform=$1, finalization_state='open'",
            platform,
        )
        work_ids = {}
        for seq, (name, status, attempts, request, dependency) in enumerate(
            (
                ("exhausted", "retry_wait", 3, None, "active_session"),
                ("waiting_exhausted", "waiting_evidence", 3, "satisfied", "active_session"),
                ("waiting_inflight", "waiting_evidence", 3, "running", "active_session"),
                ("retryable", "retry_wait", 1, None, "active_session"),
                ("waiting", "waiting_evidence", 1, "pending", "active_session"),
                ("pending", "pending", 0, None, "active_session"),
                ("leased", "retry_wait", 3, None, "active_session"),
                ("inflight", "retry_wait", 3, "running", "active_session"),
                ("snapshot", "retry_wait", 3, None, "snapshot"),
                ("wrong_producer", "retry_wait", 3, None, "active_session"),
                ("wrong_owner", "retry_wait", 3, None, "active_session"),
                ("stale_epoch", "retry_wait", 3, None, "active_session"),
            ),
            1,
        ):
            work_id, _ = await _seed_closure_work(
                connection,
                ids,
                name=name,
                page_seq=seq,
                status=status,
                attempt_count=attempts,
                request_status=request,
                session_dependency=dependency,
            )
            work_ids[name] = work_id
        await connection.execute(
            "UPDATE qc_inspection_work_item SET lease_token=gen_random_uuid(), "
            "lease_owner='judge',lease_expires_at=now()+interval '5 minutes' WHERE id=$1::uuid",
            work_ids["leased"],
        )
        for name, field in (
            ("wrong_producer", "producer_agent_run_id"),
            ("wrong_owner", "session_owner_agent_run_id"),
        ):
            await connection.execute(
                f"UPDATE qc_inspection_work_item SET {field}=$1::uuid WHERE id=$2::uuid",
                str(uuid4()),
                work_ids[name],
            )
        await connection.execute(
            "UPDATE qc_inspection_work_item SET finalization_epoch=1 WHERE id=$1::uuid",
            work_ids["stale_epoch"],
        )
        repository = PostgresInspectionWorkItemRepository(pool=_Pool(connection))
        result = await repository.settle_stalled_work_for_closure(
            task_id=ids["task"],
            active_session_agent_run_id=ids["run"],
            active_session_exhausted_only=True,
        )
        rows = await connection.fetch("SELECT id::text, status FROM qc_inspection_work_item")
        states = {row["id"]: row["status"] for row in rows}
        assert result.settled_work_item_count == (2 if platform == "h5" else 0)
        for name, work_id in work_ids.items():
            if name in {"exhausted", "waiting_exhausted"} and platform == "h5":
                assert states[work_id] == "partial"
            else:
                assert states[work_id] == {
                    "waiting": "waiting_evidence",
                    "waiting_exhausted": "waiting_evidence",
                    "waiting_inflight": "waiting_evidence",
                    "pending": "pending",
                }.get(name, "retry_wait")
        executions = await connection.fetch(
            "SELECT error_message, judge_output FROM qc_rule_execution"
        )
        assert len(executions) == (2 if platform == "h5" else 0)
        if executions:
            assert all(
                "重试次数" in row["error_message"] and "用尽" in row["error_message"]
                for row in executions
            )
            assert all("active_session_closing" not in row["judge_output"] for row in executions)
        again = await repository.settle_stalled_work_for_closure(
            task_id=ids["task"],
            active_session_agent_run_id=ids["run"],
            active_session_exhausted_only=True,
        )
        assert again.settled_work_item_count == 0
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_probe_manifest_preparation_uses_same_h5_run_action_and_epoch():
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid, finalization_epoch integer,
                target_platform text, status text);
            CREATE TEMP TABLE qc_v2_work_item(task_id uuid,work_id text,work_type text,status text,
                finalization_epoch integer,evidence_request_id text,source_capture_id uuid,
                session_id text,work_payload jsonb,terminal_payload jsonb,
                deadline timestamptz DEFAULT now()+interval '180 minutes');
            CREATE TEMP TABLE qc_evidence_request(request_id text,task_id uuid,
                finalization_epoch integer,
                expires_at timestamptz DEFAULT now()+interval '180 minutes');
            CREATE TEMP TABLE qc_action_execution(task_id uuid,session_id text,
                finalization_epoch integer,
                status text,intent_payload jsonb,result_capture_id uuid,action_execution_id text);
            CREATE TEMP TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid,
                surface_session_id text,action_execution_id text,capture_role text,
                capture_status text,parent_capture_id uuid);
        """)
        task, run, source, before, result = [uuid4() for _ in range(5)]
        await connection.execute("INSERT INTO qc_task VALUES($1,2,'h5','running')", task)
        await connection.execute(
            """INSERT INTO qc_v2_work_item VALUES(
            $1,'probe-1','probe','succeeded',2,'request-1',$2,'session-1',
            '{"surface_kind":"web"}',NULL,DEFAULT)""",
            task,
            source,
        )
        await connection.execute(
            "INSERT INTO qc_evidence_request VALUES('request-1',$1,2,DEFAULT)", task,
        )
        await connection.execute(
            """INSERT INTO qc_action_execution VALUES(
            $1,'session-1',2,'completed','{"parameters":{"exploration_work_id":"probe-1"}}',$2,'action-1')""",
            task,
            result,
        )
        for capture_id, role, parent in (
            (source, "after_action", None),
            (before, "probe_before", source),
            (result, "probe_result_replay", before),
        ):
            await connection.execute(
                "INSERT INTO qc_capture VALUES($1,$2,$3,'session-1','action-1',$4,'captured',$5)",
                capture_id,
                task,
                run,
                role,
                parent,
            )
        finalizer = SimpleNamespace(finalize_capture=AsyncMock())
        preparer = PostgresH5ProbeEvidencePreparer(
            finalizer=finalizer,
            detector=SimpleNamespace(
                detect=AsyncMock(
                    return_value={"detection_status": "recognized"},
                )
            ),
            manifests=SimpleNamespace(
                get_latest_for_capture=AsyncMock(
                    return_value=SimpleNamespace(
                        quality_status=EvidenceQualityStatus.PASS,
                    )
                ),
            ),
            pool=_Pool(connection),
        )
        outcome = H5ProbeWorkOutcome(
            claimed=True,
            work_id="probe-1",
            evidence_request_id="request-1",
            result_capture_id=str(result),
        )
        await preparer.prepare(task_id=str(task), outcome=outcome)
        assert [
            call.kwargs["capture_id"] for call in finalizer.finalize_capture.await_args_list
        ] == [str(before), str(result)]
        for mutation, reset in (
            ("UPDATE qc_task SET target_platform='web'", "UPDATE qc_task SET target_platform='h5'"),
            ("UPDATE qc_task SET finalization_epoch=3", "UPDATE qc_task SET finalization_epoch=2"),
            (
                "UPDATE qc_capture SET agent_run_id=NULL WHERE capture_role='probe_before'",
                "UPDATE qc_capture SET agent_run_id=$1",
            ),
            (
                "UPDATE qc_capture SET action_execution_id='wrong' "
                "WHERE capture_role='probe_before'",
                "UPDATE qc_capture SET action_execution_id='action-1'",
            ),
        ):
            await connection.execute(mutation)
            with pytest.raises(H5ProbeWorkError, match="Epoch 校验"):
                await preparer.prepare(task_id=str(task), outcome=outcome)
            await connection.execute(reset, *([run] if "$1" in reset else []))
        assert finalizer.finalize_capture.await_count == 2
        for table, column in (
            ("qc_v2_work_item", "deadline"), ("qc_evidence_request", "expires_at"),
        ):
            await connection.execute(
                f"UPDATE {table} SET {column}=now()-interval '1 second'",
            )
            with pytest.raises(H5ProbeWorkError) as error:
                await preparer.prepare(task_id=str(task), outcome=outcome)
            assert error.value.error_code == "H5_PROBE_ATTACHMENT_DEADLINE_EXHAUSTED"
            await connection.execute(f"UPDATE {table} SET {column}=now()+interval '180 minutes'")
        assert preparer._detector.detect.await_count == 2
        assert finalizer.finalize_capture.await_count == 2
        assert preparer._manifests.get_latest_for_capture.await_count == 2
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_h5_interrupted_restoration_is_terminal_in_original_database():
    import asyncio

    from services.api.app.application.h5_targeted_recapture import H5TargetedRecaptureRuntime
    from services.api.app.application.targeted_recapture_restoration import (
        TargetedRecaptureRestorationService,
    )
    from services.api.app.domain.interaction import CaptureKind, CaptureRef

    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        ids = {name: str(uuid4()) for name in (
            "tenant", "user", "ruleset", "rule", "task", "run", "work_item",
            "attempt", "original", "restored",
        )}
        await _seed(connection, ids)
        await connection.execute("UPDATE qc_task SET target_platform='h5' WHERE id=$1", ids["task"])
        pool = _Pool(connection)
        requests = PostgresEvidenceRequestRepository(pool=pool)
        now = datetime.now(UTC)
        request = _chain_request(ids, rule_code="alioth_chain_1",
                                 expires_at=now + timedelta(hours=1))
        await requests.upsert(request=request)
        session_id = f"h5:{ids['run']}"
        plan = build_restoration_plan(
            request=request, source_session_id=session_id, target_session_id=session_id,
            surface_kind=SurfaceKind.WEB, strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
            deadline=now + timedelta(minutes=5),
        )
        snapshot = replace(_snapshot(task_id=ids["task"], capture_id=ids["original"],
                                     captured_at=now), session_id=session_id)
        attempts = PostgresRestorationAttemptRepository(pool=pool)
        publisher = SimpleNamespace(publish=AsyncMock())
        capture = CaptureRef(task_id=ids["task"], session_id=session_id,
                             capture_id=ids["original"], surface_kind=SurfaceKind.WEB,
                             capture_kind=CaptureKind.PAGE, role="after_action")
        runtime = H5TargetedRecaptureRuntime(
            captures=SimpleNamespace(get_verified_capture=AsyncMock(
                return_value=SimpleNamespace(capture=capture))),
            snapshots=SimpleNamespace(read=AsyncMock(return_value=snapshot)),
            planning=SimpleNamespace(), restoration_attempts=attempts,
            restoration=TargetedRecaptureRestorationService(
                coordinator=RestorationCoordinator(
                    repository=attempts,
                    executor=SimpleNamespace(restore=AsyncMock(side_effect=asyncio.CancelledError())),
                    equivalence_gate=StateEquivalenceGate(),
                ), probe_publisher=publisher,
            ), agent_run_id=ids["run"], finalization_epoch=0,
        )
        with pytest.raises(asyncio.CancelledError):
            await runtime.restore_and_publish(request=request, plan=plan, attempt_no=1,
                                             budget=_budget(), request_lease=None)
        row = await connection.fetchrow(
            "SELECT status,error_code,finished_at FROM qc_restoration_attempt WHERE task_id=$1",
            ids["task"],
        )
        assert row["status"] == "failed"
        assert row["error_code"] == "H5_RESTORATION_INTERRUPTED"
        assert row["finished_at"] is not None
        assert await attempts.has_nonterminal(task_id=ids["task"]) is False
        assert await connection.fetchval(
            "SELECT status FROM qc_evidence_request WHERE request_id=$1", request.request_id
        ) == "pending"
        publisher.publish.assert_not_awaited()
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_h5_snapshot_publish_and_claim_keep_fence_during_draining():
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        # 所有库级触发器和外键照常执行；随机样例只在本事务内存在，结束回滚。
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
        await connection.execute("UPDATE qc_task SET target_platform='h5' WHERE id=$1", ids["task"])
        pool = _Pool(connection)
        requests = PostgresEvidenceRequestRepository(pool=pool)
        now = datetime.now(UTC)
        request = _chain_request(
            ids, rule_code="alioth_chain_1", expires_at=now + timedelta(hours=1)
        )
        await requests.upsert(request=request)
        deferred = await requests.claim_next(
            task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0,
            lease_owner="h5-recapture-regression", lease_seconds=180,
            session_dependencies=(EvidenceSessionDependency.SNAPSHOT,),
            deferred_h5_snapshot_request_ids=(request.request_id,),
        )
        assert deferred.lease is None
        claimed = await requests.claim_next(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
            lease_owner="h5-recapture-regression",
            lease_seconds=180,
            session_dependencies=(EvidenceSessionDependency.SNAPSHOT,),
        )
        assert claimed.lease is not None
        session_id = f"h5:{ids['run']}"
        plan = build_restoration_plan(
            request=request,
            source_session_id=session_id,
            target_session_id=session_id,
            surface_kind=SurfaceKind.WEB,
            strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
            deadline=now + timedelta(minutes=5),
        )
        original, restored = [
            replace(
                _snapshot(
                    task_id=ids["task"],
                    capture_id=ids[name],
                    captured_at=await connection.fetchval(
                        "SELECT captured_at FROM qc_capture WHERE id=$1", ids[name]
                    ),
                ),
                session_id=session_id,
            )
            for name in ("original", "restored")
        ]
        attempt = await RestorationCoordinator(
            repository=PostgresRestorationAttemptRepository(pool=pool),
            executor=_ObserveOnlyExecutor(restored),
            equivalence_gate=StateEquivalenceGate(),
        ).restore(request=request, plan=plan, attempt_no=1, original_snapshot=original)
        policy = build_lab_supplemental_evidence_policy()
        queue = PostgresExplorerWorkRepository(pool=pool, supplemental_evidence_policy=policy)
        service = RestoredProbePublicationService(
            orchestrator=QcOrchestrator(
                probe_publisher=PostgresProbeWorkPublisher(
                    repository=queue, supplemental_evidence_policy=policy
                ),
            )
        )
        await connection.execute(
            "UPDATE qc_task SET status='review',finalization_state='session_draining',"
            "exploration_draining_at=now(),session_draining_at=now() WHERE id=$1",
            ids["task"],
        )
        await connection.execute(
            "UPDATE qc_evidence_request SET lease_expires_at=now()-interval '1 second' "
            "WHERE request_id=$1",
            request.request_id,
        )
        with pytest.raises(ExplorerWorkPersistenceError):
            await service.publish(
                request=request,
                restoration_attempt=attempt,
                budget=_budget(),
                request_lease=claimed.lease.fence,
            )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_v2_work_item WHERE task_id=$1", ids["task"]
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_evidence_request WHERE request_id=$1", request.request_id
            )
            == "pending"
        )
        await connection.execute(
            "UPDATE qc_evidence_request SET lease_expires_at=now()+interval '3 minutes' "
            "WHERE request_id=$1",
            request.request_id,
        )
        probe = await service.publish(
            request=request,
            restoration_attempt=attempt,
            budget=_budget(),
            request_lease=claimed.lease.fence,
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_evidence_request WHERE request_id=$1", request.request_id
            )
            == "routed"
        )
        assert (
            await connection.fetchval(
                "SELECT lease_token FROM qc_evidence_request WHERE request_id=$1",
                request.request_id,
            )
            is None
        )
        command = ClaimExplorerWorkCommand(
            work_type=ExplorerWorkType.PROBE,
            task_id=ids["task"],
            lease_owner="h5-regression",
            surface_kinds=(SurfaceKind.WEB,),
        )
        await connection.execute(
            "UPDATE qc_task SET target_platform='web' WHERE id=$1", ids["task"]
        )
        assert await queue.claim(command=command, lease_token=str(uuid4())) is None
        await connection.execute("UPDATE qc_task SET target_platform='h5' WHERE id=$1", ids["task"])
        lease = await queue.claim(command=command, lease_token=str(uuid4()))
        assert lease is not None and lease.work.work_id == probe.work_id
        assert lease.attempt_count == 1
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", [False, True])
async def test_recapture_accepts_only_live_same_run_h5_session(recovered):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        # 临时表遮蔽业务表；事务回滚并关闭连接后不会残留任务、表或测试数据库。
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid, target_platform text, status text,
                finalization_state text, finalization_epoch integer);
            CREATE TEMP TABLE qc_evidence_request(request_id text, task_id uuid,
                target_capture_id uuid, status text, session_dependency text,
                expires_at timestamptz, finalization_epoch integer);
            CREATE TEMP TABLE qc_capture(id uuid, task_id uuid, agent_run_id uuid,
                surface_session_id text, surface_kind text, capture_status text);
            CREATE TEMP TABLE qc_surface_session(session_id text, task_id uuid,
                current_capture_id uuid, surface_kind text, session_owner text,
                finalization_epoch integer, status text);
            CREATE TEMP TABLE qc_page_graph_state(state_id uuid, task_id uuid,
                agent_run_id uuid, surface_session_id text, finalization_epoch integer,
                identity_status text, identity_source text, logical_page_key text,
                state_payload jsonb DEFAULT '{}');
            CREATE TEMP TABLE qc_page_capture(id uuid, task_id uuid, url text);
            CREATE TEMP TABLE qc_v2_work_item(work_id text, task_id uuid,
                finalization_epoch integer, work_type text, status text,
                work_payload jsonb, source_capture_id uuid, evidence_request_id text);
            CREATE TEMP TABLE qc_evidence_supplement(probe_work_id text);
        """)
        task_id, run_id, capture_id = str(uuid4()), str(uuid4()), str(uuid4())
        session_id = f"h5:{run_id}" + (":recovery:" + "a" * 20 if recovered else "")
        owner = f"h5-browser-recovery:{run_id}" if recovered else "h5-workflow"
        template = _request()
        arguments = {
            item.name: getattr(template, item.name)
            for item in fields(template)
            if item.name not in {"request_id", "requirement_hash", "protocol_version"}
        }
        request = build_evidence_request(
            **{**arguments, "task_id": task_id, "target_capture_id": capture_id}
        )
        await connection.execute("INSERT INTO qc_task VALUES($1,'h5','running','open',4)", task_id)
        await connection.execute(
            "INSERT INTO qc_capture VALUES($1,$2,$3,$4,'web','captured')",
            capture_id,
            task_id,
            run_id,
            session_id,
        )
        await connection.execute(
            "INSERT INTO qc_surface_session VALUES($1,$2,$3,'web',$4,4,'active')",
            session_id,
            task_id,
            capture_id,
            owner,
        )
        await connection.execute(
            "INSERT INTO qc_evidence_request "
            "VALUES($1,$2,$3,'pending','snapshot',now()+interval '10 minutes',4)",
            request.request_id,
            task_id,
            capture_id,
        )
        repository = PostgresH5TargetedRecaptureRepository(pool=_Pool(connection))
        assert (await repository.read(request=request)).session_id == session_id
        assert await repository.read_h5_epoch(task_id=task_id, agent_run_id=run_id) == 4
        await connection.execute(
            "INSERT INTO qc_v2_work_item VALUES('probe-current',$1,4,'probe','pending',"
            "'{\"surface_kind\":\"web\"}',$2,$3)", task_id, capture_id, request.request_id,
        )
        for status in ('pending', 'retry_wait', 'leased', 'running', 'succeeded'):
            await connection.execute("UPDATE qc_v2_work_item SET status=$1", status)
            assert await repository.read_h5_epoch(task_id=task_id, agent_run_id=run_id) is None
        for status in ('unfulfillable', 'expired', 'cancelled'):
            await connection.execute("UPDATE qc_evidence_request SET status=$1", status)
            assert await repository.read_h5_epoch(task_id=task_id, agent_run_id=run_id) == 4
        await connection.execute("UPDATE qc_evidence_request SET status='pending'")
        assert await repository.read_h5_epoch(task_id=task_id, agent_run_id=run_id) is None
        await connection.execute("INSERT INTO qc_evidence_supplement VALUES('probe-current')")
        assert await repository.read_h5_epoch(task_id=task_id, agent_run_id=run_id) == 4
        await connection.execute("DELETE FROM qc_v2_work_item")
        assert await repository.read_h5_epoch(task_id=task_id, agent_run_id=str(uuid4())) is None
        logical_request = build_evidence_request(
            **{
                **arguments,
                "task_id": task_id,
                "target_capture_id": capture_id,
                "replay_policy": EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
                "required_equivalence": EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
            }
        )
        await connection.execute(
            "UPDATE qc_evidence_request SET request_id=$1",
            logical_request.request_id,
        )
        next_capture = str(uuid4())
        for item in (capture_id, next_capture):
            await connection.execute(
                "INSERT INTO qc_page_graph_state VALUES($1,$2,$3,$4,4,"
                "'confirmed','logical_surface','logical:home')",
                item,
                task_id,
                run_id,
                session_id,
            )
            await connection.execute(
                "INSERT INTO qc_page_capture VALUES($1,$2,'https://m.example.test/home')",
                item,
                task_id,
            )
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1",
            next_capture,
        )
        planning = H5TargetedRecapturePlanService(
            contexts=repository,
            clock=lambda: template.expires_at - timedelta(minutes=1),
        )
        resolution = await planning.resolve(
            request=logical_request,
            agent_run_id=run_id,
            finalization_epoch=4,
        )
        assert resolution.executable
        for column, value in (
            ("logical_page_key", "logical:other"),
            ("identity_status", "unknown"),
        ):
            await connection.execute(
                f"UPDATE qc_page_graph_state SET {column}=$1 WHERE state_id=$2",
                value,
                next_capture,
            )
            resolution = await planning.resolve(
                request=logical_request,
                agent_run_id=run_id,
                finalization_epoch=4,
            )
            assert resolution.reason_code == "H5_ACTIVE_SESSION_TARGET_DRIFTED"
            await connection.execute(
                "UPDATE qc_page_graph_state SET logical_page_key='logical:home',"
                "identity_status='confirmed' WHERE state_id=$1",
                next_capture,
            )
        await connection.execute(
            "UPDATE qc_page_capture SET url='https://m.example.test/other' WHERE id=$1",
            next_capture,
        )
        assert not (
            await planning.resolve(
                request=logical_request,
                agent_run_id=run_id,
                finalization_epoch=4,
            )
        ).executable
        await connection.execute(
            "UPDATE qc_evidence_request SET request_id=$1",
            request.request_id,
        )
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1",
            capture_id,
        )
        await connection.execute("""
            ALTER TABLE qc_evidence_request ADD COLUMN resolution_reason text,
                ADD COLUMN lease_owner text, ADD COLUMN lease_token uuid,
                ADD COLUMN lease_expires_at timestamptz,
                ADD COLUMN row_version integer DEFAULT 1,
                ADD COLUMN completed_at timestamptz, ADD COLUMN updated_at timestamptz;
            DROP TABLE pg_temp.qc_v2_work_item;
            DROP TABLE pg_temp.qc_evidence_supplement;
            CREATE TEMP TABLE qc_v2_work_item(work_id text, task_id uuid,
                finalization_epoch integer, work_type text, work_payload jsonb,
                evidence_request_id text, status text, deadline timestamptz,
                attempt_count integer, max_attempts integer, last_error_code text,
                last_error_message text, lease_owner text, lease_token uuid,
                lease_expires_at timestamptz, finished_at timestamptz,
                row_version integer, updated_at timestamptz, source_capture_id uuid,
                session_id text);
            ALTER TABLE qc_surface_session ADD COLUMN command_sequence integer,
                ADD COLUMN lease_token uuid, ADD COLUMN lease_expires_at timestamptz;
            ALTER TABLE qc_capture ADD COLUMN action_execution_id text;
            CREATE TEMP TABLE qc_action_execution(task_id uuid,finalization_epoch integer,
                status text,started_at timestamptz,source_capture_id uuid,result_capture_id uuid,
                session_id text,command_sequence integer,intent_payload jsonb,
                action_execution_id text,error_code text,error_message text,
                action_finished_at timestamptz,evidence_finished_at timestamptz,
                row_version integer,updated_at timestamptz);
            CREATE TEMP TABLE qc_evidence_supplement(request_id text, probe_work_id text);
            UPDATE qc_evidence_request SET expires_at=now()-interval '1 second';
        """)
        for epoch in (4, 5):
            await connection.execute(
                "INSERT INTO qc_v2_work_item "
                "(work_id,task_id,finalization_epoch,work_type,work_payload,evidence_request_id,"
                "status,deadline,attempt_count,max_attempts,row_version) "
                "VALUES($1,$2,$3,'probe','{\"surface_kind\":\"web\"}',$4,'pending',"
                "now()+interval '5 minutes',0,3,1)",
                f"probe-{epoch}",
                task_id,
                epoch,
                request.request_id,
            )
        settlement = PostgresH5ProbeTerminalSettlementRepository(pool=_Pool(connection))
        assert await settlement.settle_ready(task_id=task_id, agent_run_id=str(uuid4())) == 0
        assert await settlement.settle_ready(task_id=task_id, agent_run_id=run_id) == 1
        assert await connection.fetchval("SELECT status FROM qc_evidence_request") == "expired"
        assert (
            await connection.fetchval("SELECT status FROM qc_v2_work_item WHERE work_id='probe-4'")
            == "cancelled"
        )
        assert (
            await connection.fetchval("SELECT status FROM qc_v2_work_item WHERE work_id='probe-5'")
            == "pending"
        )
        await connection.execute(
            "UPDATE qc_evidence_request SET status='pending',expires_at=now()+interval '10 minutes'"
        )
        await connection.execute("UPDATE qc_surface_session SET session_owner='web-workflow'")
        assert await repository.read(request=request) is None
        assert await repository.read_h5_epoch(task_id=task_id, agent_run_id=run_id) is None
        await connection.execute(
            "UPDATE qc_surface_session SET session_owner=$1,status='closed'", owner
        )
        assert await repository.read(request=request) is None
        assert await repository.read_h5_epoch(task_id=task_id, agent_run_id=run_id) is None
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_close_barrier_counts_snapshot_work_and_requests_without_blocking_page_checkpoint():
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid, finalization_epoch integer, target_platform text);
            CREATE TEMP TABLE qc_capture(id uuid, task_id uuid, agent_run_id uuid);
            CREATE TEMP TABLE qc_inspection_work_item(task_id uuid, producer_agent_run_id uuid,
                session_owner_agent_run_id uuid, session_dependency text, control_mode text,
                finalization_epoch integer, status text,
                attempt_count integer, max_attempts integer,
                lease_token uuid, lease_expires_at timestamptz, next_attempt_at timestamptz);
            CREATE TEMP TABLE qc_evidence_request(task_id uuid, target_capture_id uuid,
                session_dependency text, finalization_epoch integer, status text,
                lease_expires_at timestamptz, next_attempt_at timestamptz);
        """)
        task_id, run_id, capture_id = str(uuid4()), str(uuid4()), str(uuid4())
        await connection.execute("INSERT INTO qc_task VALUES($1,4,'h5')", task_id)
        await connection.execute(
            "INSERT INTO qc_capture VALUES($1,$2,$3)", capture_id, task_id, run_id
        )
        await connection.execute(
            """INSERT INTO qc_inspection_work_item
            VALUES($1,$2,NULL,'snapshot','enforced',4,'pending',0,3,NULL,NULL,now())""",
            task_id,
            run_id,
        )
        await connection.execute(
            """INSERT INTO qc_evidence_request
            VALUES($1,$2,'snapshot',4,'pending',NULL,now())""",
            task_id,
            capture_id,
        )

        class Captures:
            async def list_judgeable_captures(self, **kwargs):
                return []

        await connection.execute(
            "ALTER TABLE qc_inspection_work_item ADD COLUMN id uuid DEFAULT gen_random_uuid();"
            "ALTER TABLE qc_evidence_request ADD COLUMN work_item_id uuid;"
        )
        reader = PostgresH5ActiveSessionInspectionStateReader(
            capture_reader=Captures(), pool=_Pool(connection)
        )
        state = await reader.load_drain_state(task_id=task_id, agent_run_id=run_id)
        assert state.drained
        assert not state.close_drained
        assert state.snapshot_work_item_count == state.snapshot_evidence_request_count == 1
        assert state.scheduled_work_item_count == 0
        await connection.execute(
            "UPDATE qc_inspection_work_item SET status='running',attempt_count=3,"
            "lease_token=$1,lease_expires_at=now()+interval '1 minute'",
            uuid4(),
        )
        assert (
            await reader.load_drain_state(task_id=task_id, agent_run_id=run_id)
        ).scheduled_work_item_count == 1
        await connection.execute("UPDATE qc_task SET target_platform='web'")
        assert (
            await reader.load_drain_state(task_id=task_id, agent_run_id=run_id)
        ).scheduled_work_item_count == 0
        await connection.execute("UPDATE qc_task SET target_platform='h5'")
        await connection.execute(
            "UPDATE qc_inspection_work_item SET lease_expires_at=now()-interval '1 second'"
        )
        assert (
            await reader.load_drain_state(task_id=task_id, agent_run_id=run_id)
        ).scheduled_work_item_count == 0
        await connection.execute(
            "UPDATE qc_inspection_work_item SET status='retry_wait',attempt_count=2,"
            "lease_token=NULL,next_attempt_at=now()+interval '1 minute'"
        )
        assert (
            await reader.load_drain_state(task_id=task_id, agent_run_id=run_id)
        ).scheduled_work_item_count == 1
        await connection.execute("UPDATE qc_inspection_work_item SET attempt_count=3")
        assert (
            await reader.load_drain_state(task_id=task_id, agent_run_id=run_id)
        ).scheduled_work_item_count == 0
        await connection.execute(
            "UPDATE qc_inspection_work_item SET session_dependency='active_session',"
            "session_owner_agent_run_id=$1,status='waiting_evidence'",
            run_id,
        )
        state = await reader.load_drain_state(task_id=task_id, agent_run_id=run_id)
        assert state.nonterminal_work_item_count == state.exhausted_work_item_count == 1
        assert not state.drained
        await connection.execute(
            "UPDATE qc_evidence_request SET work_item_id=(SELECT id FROM qc_inspection_work_item)"
        )
        assert (
            await reader.load_drain_state(task_id=task_id, agent_run_id=run_id)
        ).exhausted_work_item_count == 0
        await connection.execute("UPDATE qc_evidence_request SET status='satisfied'")
        assert (
            await reader.load_drain_state(task_id=task_id, agent_run_id=run_id)
        ).exhausted_work_item_count == 1
        await connection.execute("UPDATE qc_task SET target_platform='web'")
        assert (
            await reader.load_drain_state(task_id=task_id, agent_run_id=run_id)
        ).exhausted_work_item_count == 0
        await connection.execute("UPDATE qc_task SET target_platform='h5'")
        other = await reader.load_drain_state(task_id=task_id, agent_run_id=str(uuid4()))
        assert other.close_drained
        await connection.execute("UPDATE qc_task SET finalization_epoch=5")
        assert (await reader.load_drain_state(task_id=task_id, agent_run_id=run_id)).close_drained
    finally:
        await transaction.rollback()
        await connection.close()
