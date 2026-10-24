"""H5 导航完全失败的真实 PostgreSQL 事实、任务读侧与最终化回归。"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import fakeredis
import pytest

from services.api.app import worker
from services.api.app.agents.runtime.models import TerminationReason
from services.api.app.application.h5_failed_run_finalization import terminate_failed_h5_task
from services.api.app.application.h5_run_failure import classify_h5_run_failure
from services.api.app.application.task_finalization import (
    BeginExplorationDrainingCommand,
    BeginSessionDrainingCommand,
    TaskFinalizationService,
    TerminateTaskFinalizationCommand,
)
from services.api.app.application.workstation_tasks import WorkstationTaskListFilter
from services.api.app.core.config import Settings
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.interaction import SurfaceKind, SurfaceSessionSpec
from services.api.app.domain.interaction.surface_driver import web_primary_surface_ref
from services.api.app.domain.task import TaskExecutionStatus
from services.api.app.infrastructure.browser.capture_runtime import WebCaptureRuntime
from services.api.app.infrastructure.db.h5_run_failure_repository import (
    PostgresH5RunFailureFactsRepository,
)
from services.api.app.infrastructure.db.session_command_gate import PostgresSessionCommandGate
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
)
from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import _database

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)


@dataclass
class _Case:
    connection: asyncpg.Connection
    pool: asyncpg.Pool
    queue: RedisAgentRunQueue
    record: object
    scope: AccessScope
    capture_id: str
    rule_id: str
    other_run_id: str

    @property
    def service(self):
        return TaskFinalizationService(PostgresTaskFinalizationRepository(pool=self.pool))

    @property
    def reader(self):
        return PostgresH5RunFailureFactsRepository(pool=self.pool)


@pytest.fixture
async def pg_case():
    # 读侧需要一致快照、关闭侧需要新快照；不能用单连接外层事务伪造两者。
    context = _database(
        surface_kind="web",
        target_platform="h5",
        retain_sealed=True,
    )
    connection, pool, _ = await context.__aenter__()
    try:
        ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "task", "capture")}
        config = {
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
            "h5_inspection_notification_mode": "enforced",
            "ruleset_rule_total": 73,
            "_task_target": {"platform": "h5", "target_url": "https://dys18.com/"},
        }
        queue = RedisAgentRunQueue(
            fakeredis.aioredis.FakeRedis(decode_responses=True),
            ttl_seconds=3600,
        )
        record = await queue.enqueue("H5 失败回归", task_id=ids["task"], task_config=config)
        record.status = AgentRunQueueStatus.FAILED
        record.termination_reason = TerminationReason.ERROR.value
        await connection.execute(
            "INSERT INTO qc_tenant(id,tenant_key,display_name) VALUES($1::uuid,$2,'H5 回归公司')",
            ids["tenant"],
            f"h5-failure-{ids['tenant']}",
        )
        await connection.execute(
            """INSERT INTO qc_user(id,username,full_name,password_hash,role,tenant_id)
               VALUES($1::uuid,$2,'H5 回归用户','integration-only','平台管理员',$3::uuid)""",
            ids["user"],
            f"h5-failure-{ids['user']}",
            ids["tenant"],
        )
        await connection.execute(
            """INSERT INTO qc_ruleset(id,ruleset_code,name,version,status,source_type,rule_count)
               VALUES($1::uuid,$2,'H5 回归规则集',1,'enabled','manual',73)""",
            ids["ruleset"],
            f"h5-failure-{ids['ruleset']}",
        )
        await connection.execute(
            """INSERT INTO qc_task(id,target_url,ruleset_id,submitter_id,tenant_id,
                                   target_platform,status,task_config)
               VALUES($1::uuid,'https://dys18.com/',$2::uuid,$3::uuid,$4::uuid,
                      'h5','running',$5::jsonb)""",
            ids["task"],
            ids["ruleset"],
            ids["user"],
            ids["tenant"],
            json.dumps(config),
        )
        other_run_id = str(uuid4())
        for run_id in (record.run_id, other_run_id):
            await connection.execute(
                """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,stop_reason)
                   VALUES($1::uuid,$2::uuid,'main','h5-failure','failed','error')""",
                run_id,
                ids["task"],
            )
        await connection.execute(
            "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
            ids["task"],
            record.run_id,
        )
        await connection.execute(
            """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,capture_role,
                                      capture_status,metadata)
               VALUES($1::uuid,$2::uuid,$3::uuid,'web','after_action','captured',
                      '{"finalization_epoch":0}'::jsonb)""",
            ids["capture"],
            ids["task"],
            record.run_id,
        )
        await connection.execute(
            """INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,
                                           url,capture_status,
                                           detection_status,inspection_lifecycle_status)
               VALUES($1::uuid,$2::uuid,$4::uuid,1,$3,'https://example.com/','captured',
                      'recognized','active')""",
            ids["capture"],
            ids["task"],
            f"h5-failure-{ids['capture']}",
            record.run_id,
        )
        rule_id = ""
        for _index, (status, reason, bootstrap, run_id) in enumerate(
            (
                ("skipped", None, True, record.run_id),
                ("evidence_insufficient", None, False, record.run_id),
                ("skipped", "auth_requirement_not_met", False, record.run_id),
                ("pass", None, False, other_run_id),
            )
        ):
            rule_id = str(uuid4())
            await connection.execute(
                """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,
                                      executor_type,check_point)
                   VALUES($1::uuid,$2::uuid,$3,'H5 回归规则','text','hybrid','回归')""",
                rule_id,
                ids["ruleset"],
                f"H5-{rule_id}",
            )
            await connection.execute(
                """INSERT INTO qc_rule_execution(task_id,rule_id,agent_run_id,execution_status,
                        judge_strategy,judge_input,judge_output,started_at,finished_at)
                   VALUES($1::uuid,$2::uuid,$3::uuid,$4,'hybrid',$5::jsonb,
                          $6::jsonb,now(),now())""",
                ids["task"],
                rule_id,
                run_id,
                status,
                json.dumps({"bootstrap": bootstrap}),
                json.dumps({"reason": reason}),
            )
        yield _Case(
            connection,
            pool,
            queue,
            record,
            AccessScope(
                actor_user_id=ids["user"],
                tenant_id=ids["tenant"],
                permissions=frozenset({"taskObjects:read:tenant"}),
            ),
            ids["capture"],
            rule_id,
            other_run_id,
        )
    finally:
        await context.__aexit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("driver_close_failed", [False, True])
async def test_outside_only_failure_real_worker_termination_is_failed_sealed_unknown(
    pg_case,
    monkeypatch,
    driver_close_failed,
) -> None:
    case = pg_case
    sessions, command_gate, session_id = await _register_session(case)
    action_id = f"diagnostic-{uuid4()}"
    await case.connection.execute(
        """INSERT INTO qc_action_execution(action_execution_id,task_id,session_id,intent_id,
               idempotency_key,intent_hash,intent_protocol_version,intent_payload,command_sequence,
               finalization_epoch,status,source_capture_id,result_capture_id,started_at,
               action_finished_at,evidence_finished_at,error_code)
           VALUES($1,$2::uuid,$3,$4,$4,$4,'action-intent.v1','{}'::jsonb,1,0,'blocked',
                  $5::uuid,$5::uuid,now(),now(),now(),'COMMAND_HOST_BOUNDARY_VIOLATION')""",
        action_id,
        case.record.task_id,
        session_id,
        f"intent-{uuid4()}",
        case.capture_id,
    )

    class Browser:
        async def close_session(self, identifier):
            assert identifier == session_id
            if driver_close_failed:
                raise RuntimeError("诊断浏览器关闭失败")
            # 隔离驱动替身提供完整回执；真实原实例/PID链另有浏览器到数据库用例。
            return {
                "session_id": session_id,
                "resource_id": str(uuid4()),
                "browser_pid": 987654321,
                "method": "owned_browser_close_ack_and_pid_absent",
                "driver_stop_ack": True,
                "observed_at": datetime.now(UTC).isoformat(),
            }

    runtime = WebCaptureRuntime(
        coordinator=None,
        session_command_gate=command_gate,
        _browser=Browser(),
        _executor=None,
        _evidence_buffer=None,
        surface_sessions=sessions,
    )
    if driver_close_failed:
        with pytest.raises(RuntimeError, match="诊断浏览器关闭失败"):
            await runtime.close_h5_exploration_session(
                task_id=case.record.task_id, session_id=session_id
            )
    else:
        await runtime.close_h5_exploration_session(
            task_id=case.record.task_id, session_id=session_id
        )
    facts = await case.reader.fetch(task_id=case.record.task_id, agent_run_id=case.record.run_id)
    assert facts.completely_failed and facts.business_result_count == 0
    monkeypatch.setattr(worker, "PostgresH5RunFailureFactsRepository", lambda: case.reader)
    events = []

    async def drain(*args, **kwargs):
        events.append("drain")

    async def settle(*args, **kwargs):
        events.append("settle")

    monkeypatch.setattr(worker, "_drain_snapshot_inspection_work", drain)
    monkeypatch.setattr(worker, "_finalize_rule_executions", settle)
    monkeypatch.setattr(worker, "_finalize_persisted_inspection_work", settle)

    async def finish():
        return await worker._finish_h5_completely_failed_run(
            queue=case.queue,
            record=case.record,
            settings=Settings(),
            state_store=InMemoryAgentRunStateStore(),
            progress_recorder=None,
            worker_instance_id="h5-regression",
            task_finalization_service=case.service,
            work_drained=False,
        )

    if driver_close_failed:
        with pytest.raises(RuntimeError, match="NOT_DRAINED"):
            await finish()
        snapshot = await case.service.get(task_id=case.record.task_id)
        assert snapshot.state.value != "sealed"
        assert (
            await case.connection.fetchval(
                "SELECT count(*) FROM qc_report_gate_binding WHERE task_id=$1::uuid",
                case.record.task_id,
            )
            == 0
        )
        assert (
            await case.connection.fetchval(
                "SELECT status FROM qc_surface_session WHERE session_id=$1", session_id
            )
            == "lost"
        )
        return
    assert await finish()
    row = await case.connection.fetchrow(
        """SELECT status,finalization_state,execution_status,quality_outcome,finalization_last_error
           FROM qc_task WHERE id=$1::uuid""",
        case.record.task_id,
    )
    assert dict(row) == {
        "status": "failed",
        "finalization_state": "sealed",
        "execution_status": "failed",
        "quality_outcome": "unknown",
        "finalization_last_error": "COMMAND_HOST_BOUNDARY_VIOLATION",
    }
    assert events == ["drain", "settle", "settle"]
    assert case.record.final_report is None
    session = await sessions.get(session_id=session_id)
    assert session.status.value == ("lost" if driver_close_failed else "closed")
    row = await case.connection.fetchrow(
        "SELECT lease_owner,lease_token,lease_expires_at "
        "FROM qc_surface_session WHERE session_id=$1",
        session_id,
    )
    assert all(value is None for value in row.values())
    assert (
        await case.connection.fetchval(
            "SELECT status FROM qc_action_execution WHERE action_execution_id=$1",
            action_id,
        )
        == "blocked"
    )
    assert (
        await case.connection.fetchval(
            "SELECT count(*) FROM qc_report_gate_binding WHERE task_id=$1::uuid",
            case.record.task_id,
        )
        == 0
    )
    assert (
        await case.connection.fetchval(
            "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid",
            case.record.task_id,
        )
        == 1
    )
    await terminate_failed_h5_task(
        case.service,
        task_id=case.record.task_id,
        agent_run_id=case.record.run_id,
        reason_code="retry",
    )
    assert (
        await case.connection.fetchval(
            "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid",
            case.record.task_id,
        )
        == 1
    )


async def _register_session(case):
    sessions = PostgresSurfaceSessionRepository(pool=case.pool)
    session_id = f"h5:{case.record.run_id}"
    await sessions.register_active(
        spec=SurfaceSessionSpec(
            task_id=case.record.task_id, session_id=session_id, surface_kind=SurfaceKind.WEB
        ),
        session_owner="h5-explorer",
        primary_surface_ref=web_primary_surface_ref(session_id),
    )
    command_gate = PostgresSessionCommandGate(repository=sessions, lease_owner="h5-regression")
    return sessions, command_gate, session_id


@pytest.mark.asyncio
async def test_failed_task_is_not_sealed_while_current_session_command_lease_is_active(
    pg_case,
    monkeypatch,
):
    case = pg_case
    _sessions, gate, session_id = await _register_session(case)
    monkeypatch.setattr(worker, "PostgresH5RunFailureFactsRepository", lambda: case.reader)
    async with gate.hold(
        task_id=case.record.task_id, session_id=session_id, surface_kind=SurfaceKind.WEB
    ):
        with pytest.raises(RuntimeError, match="H5_FAILED_RUN_NOT_DRAINED"):
            await worker._finish_h5_completely_failed_run(
                queue=case.queue,
                record=case.record,
                settings=Settings(),
                state_store=InMemoryAgentRunStateStore(),
                progress_recorder=None,
                worker_instance_id="h5-regression",
                task_finalization_service=case.service,
                work_drained=True,
            )
    assert (
        await case.connection.fetchval(
            "SELECT finalization_state FROM qc_task WHERE id=$1::uuid",
            case.record.task_id,
        )
        == "open"
    )
    assert (
        await case.connection.fetchval(
            "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid",
            case.record.task_id,
        )
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verified,final_url,count",
    [
        (True, "https://login.allowed.test/", 1),
        (False, "https://login.allowed.test/", 0),
        (True, "https://different.test/", 0),
    ],
)
async def test_reader_uses_trusted_post_scope_and_current_run_only(
    pg_case, verified, final_url, count
):
    case = pg_case
    await case.connection.execute(
        "UPDATE qc_page_capture SET url='https://login.allowed.test/' WHERE id=$1::uuid",
        case.capture_id,
    )
    await case.connection.execute(
        "UPDATE qc_capture SET metadata=$2::jsonb WHERE id=$1::uuid",
        case.capture_id,
        json.dumps(
            {
                "pipeline_metrics": {
                    "navigation_scope": {"verified": verified, "final_url": final_url},
                }
            }
        ),
    )
    facts = await case.reader.fetch(task_id=case.record.task_id, agent_run_id=case.record.run_id)
    assert facts.valid_target_capture_count == count
    assert facts.business_result_count == 0
    assert facts.completely_failed is (count == 0)
    assert (
        await case.reader.fetch(task_id=case.record.task_id, agent_run_id=case.other_run_id) is None
    )


@pytest.mark.parametrize("outside", [False, True])
async def test_invalid_navigation_capture_pass_is_not_a_business_result(pg_case, outside):
    case = pg_case
    await case.connection.execute(
        "UPDATE qc_page_capture SET url=$2 WHERE id=$1::uuid",
        case.capture_id,
        "https://example.com/" if outside else "https://dys18.com/",
    )
    await case.connection.execute(
        "UPDATE qc_capture SET metadata=$2::jsonb WHERE id=$1::uuid",
        case.capture_id,
        json.dumps(
            {
                "pipeline_metrics": {
                    "navigation_document": {
                        "inspectable": False,
                        "error_code": "WEB_NAVIGATION_BLANK_DOCUMENT",
                    }
                }
            }
        ),
    )
    await case.connection.execute(
        """UPDATE qc_rule_execution SET page_capture_id=$2::uuid,
           execution_status='pass',judge_input='{}'::jsonb,judge_output='{}'::jsonb
           WHERE task_id=$1::uuid AND rule_id=$3::uuid""",
        case.record.task_id,
        case.capture_id,
        case.rule_id,
    )
    facts = await case.reader.fetch(task_id=case.record.task_id, agent_run_id=case.record.run_id)
    assert facts.valid_target_capture_count == facts.business_result_count == 0
    assert facts.completely_failed is True
    assert facts.technical_failure_reason == (
        "COMMAND_HOST_BOUNDARY_VIOLATION" if outside else "WEB_NAVIGATION_BLANK_DOCUMENT"
    )


@pytest.mark.asyncio
async def test_task_and_report_queries_reject_zero_business_preview_and_expose_run_identity(
    pg_case,
):
    case = pg_case
    await case.connection.execute(
        """UPDATE qc_task SET status='review',finalization_state='session_draining',
           exploration_draining_at=now(),session_draining_at=now() WHERE id=$1::uuid""",
        case.record.task_id,
    )
    await case.connection.execute(
        """UPDATE qc_agent_run SET output_context='{"final_report":{"task":{}}}'::jsonb
           WHERE id=$1::uuid""",
        case.record.run_id,
    )
    repository = PostgresWorkstationQueryRepository(pool=case.pool)
    listing = await repository.list_tasks(WorkstationTaskListFilter(), access_scope=case.scope)
    item = next(item for item in listing.items if item.task_id == case.record.task_id)
    detail = await repository.get_task_detail(case.record.task_id, access_scope=case.scope)
    assert item.progress == detail.progress == 100
    assert item.report_availability != "ready" and detail.report_availability != "ready"
    insight = PostgresTaskInsightRepository(pool=case.pool)
    pages = await insight.list_h5_page_captures(
        task_id=case.record.task_id, agent_run_id=case.record.run_id
    )
    rules = await insight.list_rule_execution_rows(
        task_id=case.record.task_id, agent_run_id=case.record.run_id
    )
    facts = classify_h5_run_failure(
        {"run_id": case.record.run_id, "target_url": "https://dys18.com/"},
        pages,
        rules,
    )
    assert facts.completely_failed and facts.business_result_count == 0
    assert all(page["agent_run_id"] == case.record.run_id for page in pages)


@pytest.mark.asyncio
async def test_real_partial_target_results_remain_review_and_have_no_success_binding(pg_case):
    case = pg_case
    await case.connection.execute(
        "UPDATE qc_page_capture SET url='https://dys18.com/home' WHERE id=$1::uuid",
        case.capture_id,
    )
    facts = await case.reader.fetch(task_id=case.record.task_id, agent_run_id=case.record.run_id)
    assert facts.has_partial_results and not facts.completely_failed
    snapshot = await case.service.get(task_id=case.record.task_id)
    snapshot = await case.service.begin_exploration_draining(
        BeginExplorationDrainingCommand(
            task_id=case.record.task_id,
            agent_run_id=case.record.run_id,
            expected_row_version=snapshot.row_version,
        )
    )
    snapshot = await case.service.begin_session_draining(
        BeginSessionDrainingCommand(
            task_id=case.record.task_id,
            agent_run_id=case.record.run_id,
            expected_finalization_epoch=snapshot.finalization_epoch,
            expected_row_version=snapshot.row_version,
        )
    )
    snapshot = await case.service.terminate(
        TerminateTaskFinalizationCommand(
            task_id=case.record.task_id,
            agent_run_id=case.record.run_id,
            expected_finalization_epoch=snapshot.finalization_epoch,
            expected_row_version=snapshot.row_version,
            execution_status=TaskExecutionStatus.NEEDS_REVIEW,
            reason_code="h5_partial_results",
        )
    )
    assert snapshot.execution_status is TaskExecutionStatus.NEEDS_REVIEW
    assert (
        await case.connection.fetchval(
            "SELECT status FROM qc_task WHERE id=$1::uuid",
            case.record.task_id,
        )
        == "review"
    )
    assert (
        await case.connection.fetchval(
            "SELECT count(*) FROM qc_report_gate_binding WHERE task_id=$1::uuid",
            case.record.task_id,
        )
        == 0
    )
