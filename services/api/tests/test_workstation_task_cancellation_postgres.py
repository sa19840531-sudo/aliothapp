"""真实 PostgreSQL 验证 Main 终态后取消根任务的围栏、审计和回滚。"""

import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.task_finalization import (
    TaskFinalizationService,
    TerminateTaskFinalizationCommand,
)
from services.api.app.application.workstation_tasks import WorkstationTaskService
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import TaskExecutionStatus
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
    TaskFinalizationPersistenceError,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)
from services.api.app.infrastructure.db.workstation_task_cancellation_repository import (
    PostgresWorkstationTaskCancellationRepository,
)
from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore
from services.api.tests.test_inspection_closure_settlement_postgres import _seed_work_item

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL"
)


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
async def pg_case():
    url = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"],
        os.getenv("ALIOTH_DATABASE_URL"),
        allowed_roles=frozenset({"postgres", "alioth_test"}),
    )
    connection = await asyncpg.connect(url)
    transaction = connection.transaction()
    await transaction.start()
    ids = {key: str(uuid4()) for key in ("tenant", "user", "ruleset", "rule", "task", "run")}
    try:
        await connection.execute(
            "INSERT INTO qc_tenant(id,tenant_key,display_name) VALUES($1::uuid,$2,$2)",
            ids["tenant"],
            f"cancel-sync-{ids['tenant']}",
        )
        await connection.execute(
            """INSERT INTO qc_user(id,username,full_name,password_hash,role,tenant_id)
               VALUES($1::uuid,$2,'取消回归','integration-only','平台管理员',$3::uuid)""",
            ids["user"],
            f"cancel-sync-{ids['user']}",
            ids["tenant"],
        )
        await connection.execute(
            """INSERT INTO qc_ruleset(id,ruleset_code,name,version,status,source_type,rule_count)
               VALUES($1::uuid,$2,'取消回归',1,'enabled','manual',1)""",
            ids["ruleset"],
            f"cancel-sync-{ids['ruleset']}",
        )
        await connection.execute(
            """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,check_point)
               VALUES($1::uuid,$2::uuid,'alioth_closure_1','取消回归规则','visual','hybrid','检查页面')""",
            ids["rule"],
            ids["ruleset"],
        )
        await connection.execute(
            """INSERT INTO qc_task(id,target_url,ruleset_id,submitter_id,tenant_id,target_platform,
                      status,task_config,finalization_state,exploration_draining_at,session_draining_at)
               VALUES($1::uuid,'https://example.test',$2::uuid,$3::uuid,$4::uuid,'h5','running',
                      '{"workflow_version":"v2","execution_mode":"full_inspection"}'::jsonb,
                      'session_draining',now(),now())""",
            ids["task"],
            ids["ruleset"],
            ids["user"],
            ids["tenant"],
        )
        await connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,stop_reason,
                                        error_message,started_at,finished_at)
               VALUES($1::uuid,$2::uuid,'main','取消回归','failed','incomplete',
                      'popup_dismissal_stalled:3/3',now()-interval '2 days',
                      now()-interval '2 days')""",
            ids["run"],
            ids["task"],
        )
        await connection.execute(
            "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
            ids["task"],
            ids["run"],
        )
        session_id = f"cancel-session-{uuid4()}"
        await connection.execute(
            """INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,status,
                                               activated_at,draining_at,closed_at)
               VALUES($1,$2::uuid,'web','cancel-sync','closed',now(),now(),now())""",
            session_id,
            ids["task"],
        )
        work_id, _capture_id = await _seed_work_item(
            connection,
            ids,
            name=f"cancel-{uuid4()}",
            page_seq=1,
            status="waiting_evidence",
            attempt_count=1,
            request_status="pending",
        )
        pool = _Pool(connection)
        scope = AccessScope(
            actor_user_id=ids["user"],
            tenant_id=ids["tenant"],
            permissions=frozenset({"taskObjects:read:any", "taskObjects:write:any"}),
        )
        yield SimpleNamespace(
            ids=ids,
            connection=connection,
            pool=pool,
            scope=scope,
            session_id=session_id,
            work_id=work_id,
            repository=PostgresWorkstationTaskCancellationRepository(pool=pool),
        )
    finally:
        await transaction.rollback()
        await connection.close()


async def test_cancel_failed_main_root_task_preserves_history_and_cleans_work(pg_case):
    case = pg_case
    before = await case.connection.fetchrow(
        "SELECT status,error_message,stop_reason,started_at,finished_at "
        "FROM qc_agent_run WHERE id=$1::uuid",
        case.ids["run"],
    )

    class Submission:
        async def cancel_run(self, _run_id):
            raise AssertionError("终态 Main 不应被再次取消")

    service = WorkstationTaskService(
        repository=PostgresWorkstationQueryRepository(pool=case.pool),
        submission_service=Submission(),
        terminal_run_task_canceler=case.repository,
    )
    for _ in range(2):
        result = await service.perform_operation(
            task_id=case.ids["task"], operation="cancel", access_scope=case.scope
        )
        assert result.status == "canceled"
    after = await case.connection.fetchrow(
        "SELECT status,error_message,stop_reason,started_at,finished_at "
        "FROM qc_agent_run WHERE id=$1::uuid",
        case.ids["run"],
    )
    assert after == before
    assert (
        await case.connection.fetchval(
            "SELECT status FROM qc_task WHERE id=$1::uuid", case.ids["task"]
        )
        == "canceled"
    )
    assert (
        await case.connection.fetchval(
            "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid", case.work_id
        )
        == "cancelled"
    )
    assert (
        await case.connection.fetchval(
            "SELECT status FROM qc_evidence_request WHERE task_id=$1::uuid", case.ids["task"]
        )
        == "cancelled"
    )
    context = json.loads(
        await case.connection.fetchval(
            "SELECT output_context FROM qc_agent_run WHERE id=$1::uuid", case.ids["run"]
        )
    )
    assert context["cancel_requested"] is True
    assert context["cancellation_cleanup_completed"] is True
    assert context["cancellation_finalized"] is True
    assert context["task_cancellation"]["actor_user_id"] == case.ids["user"]
    assert context["task_cancellation"]["finalization_epoch"] == 0
    assert context["task_cancellation"]["run_status"] == "failed"
    recovery = await PostgresAgentRunStateStore(pool=case.pool).list_cancellation_recovery_records()
    assert case.ids["run"] not in {record.run_id for record in recovery}


@pytest.mark.parametrize(
    "guard", ["session_active", "new_main", "owner_changed", "sealed", "foreign_actor"]
)
async def test_cancel_terminal_main_rejects_changed_owner_live_session_and_sealed_task(
    pg_case,
    guard,
):
    case = pg_case
    scope = case.scope
    if guard == "session_active":
        await case.connection.execute(
            "UPDATE qc_surface_session SET status='draining',closed_at=NULL WHERE session_id=$1",
            case.session_id,
        )
    if guard in {"new_main", "owner_changed"}:
        run_id = str(uuid4())
        await case.connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,created_at)
               VALUES($1::uuid,$2::uuid,'main','新运行','pending',now()+interval '1 second')""",
            run_id,
            case.ids["task"],
        )
        if guard == "owner_changed":
            await case.connection.execute(
                "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
                case.ids["task"],
                run_id,
            )
    if guard == "sealed":
        finalization = TaskFinalizationService(PostgresTaskFinalizationRepository(pool=case.pool))
        snapshot = await finalization.get(task_id=case.ids["task"])
        command = TerminateTaskFinalizationCommand(
            task_id=case.ids["task"],
            agent_run_id=case.ids["run"],
            expected_finalization_epoch=snapshot.finalization_epoch,
            expected_row_version=snapshot.row_version,
            execution_status=TaskExecutionStatus.FAILED,
            reason_code="cancel_test_execution_failed",
        )
        with pytest.raises(
            TaskFinalizationPersistenceError, match="H5_EXECUTION_RESOURCES_NOT_DRAINED"
        ):
            await finalization.terminate(command)
        # 封存围栏的正例先结束隔离夹具中的尝试与请求，不把待补证当成已排空。
        await case.connection.execute(
            "UPDATE qc_inspection_attempt SET status='abandoned',finished_at=now() "
            "WHERE work_item_id=$1::uuid",
            case.work_id,
        )
        await case.connection.execute(
            "UPDATE qc_evidence_request SET status='unfulfillable',completed_at=now(),"
            "resolution_reason='隔离夹具停止取证' WHERE task_id=$1::uuid",
            case.ids["task"],
        )
        await finalization.terminate(command)
    if guard == "foreign_actor":
        scope = AccessScope(
            actor_user_id=str(uuid4()), tenant_id=str(uuid4()), permissions=frozenset()
        )
    result = await case.repository.cancel_finished_run_task(
        task_id=case.ids["task"], run_id=case.ids["run"], access_scope=scope
    )
    assert result is None
    assert await case.connection.fetchval(
        "SELECT status FROM qc_task WHERE id=$1::uuid", case.ids["task"]
    ) == ("failed" if guard == "sealed" else "running")
    assert (
        await case.connection.fetchval(
            "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid", case.work_id
        )
        == "waiting_evidence"
    )


async def test_cancel_cleanup_failure_rolls_back_root_status_work_and_audit(pg_case, monkeypatch):
    case = pg_case

    async def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("取消清理数据库故障")

    monkeypatch.setattr(PostgresInspectionWorkItemRepository, "cancel_nonterminal", fail_cleanup)
    with pytest.raises(RuntimeError, match="取消清理数据库故障"):
        await case.repository.cancel_finished_run_task(
            task_id=case.ids["task"], run_id=case.ids["run"], access_scope=case.scope
        )
    assert (
        await case.connection.fetchval(
            "SELECT status FROM qc_task WHERE id=$1::uuid", case.ids["task"]
        )
        == "running"
    )
    assert (
        await case.connection.fetchval(
            "SELECT status FROM qc_evidence_request WHERE task_id=$1::uuid", case.ids["task"]
        )
        == "pending"
    )
    assert (
        await case.connection.fetchval(
            "SELECT output_context->>'cancel_requested' FROM qc_agent_run WHERE id=$1::uuid",
            case.ids["run"],
        )
        is None
    )
