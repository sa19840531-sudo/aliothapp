"""真实 PostgreSQL 验证异常收口围栏、预览保留及 Worker 取消/超时终态。"""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import fakeredis
import pytest

from services.api.app import worker as worker_module
from services.api.app.application.h5_evidence_handoff import H5EvidenceHandoffSummary
from services.api.app.application.task_finalization import (
    TaskFinalizationService,
    TerminateTaskFinalizationCommand,
)
from services.api.app.core.config import Settings
from services.api.app.domain.task import TaskExecutionStatus, TaskFinalizationState
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    FinalizationRecoveryFenceError,
)
from services.api.app.infrastructure.db.h5_probe_terminal_repository import (
    PostgresH5ProbeTerminalSettlementRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
    TaskFinalizationPersistenceError,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore
from services.api.tests.test_inspection_closure_settlement_postgres import (
    _seed_task,
    _seed_work_item,
)
from services.api.tests.test_workstation_task_list_postgres import (
    _ConnectionPool,
    _scope,
)
from services.api.tests.test_workstation_task_list_postgres import (
    pg_connection as pg_connection,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)


@pytest.fixture
async def stalled_case(pg_connection):
    transaction = pg_connection.transaction()
    await transaction.start()
    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    try:
        await _seed_task(pg_connection, ids)
        config = {
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
            "h5_inspection_notification_mode": "enforced",
        }
        await pg_connection.execute(
            """UPDATE qc_task SET target_platform='h5',task_config=$2::jsonb,
                   finalization_agent_run_id=$3::uuid,started_at=now()-interval '42 hours',
                   finished_at=NULL WHERE id=$1::uuid""",
            ids["task"],
            json.dumps(config),
            ids["run"],
        )
        await pg_connection.execute(
            """UPDATE qc_agent_run SET status='failed',stop_reason='incomplete',
                   started_at=now()-interval '42 hours',
                   finished_at=now()-interval '42 hours'+interval '99 seconds',
                   error_message='popup_dismissal_stalled',
                   output_context='{"final_report":{"summary":{"overall_status":"incomplete"}}}'
                     ::jsonb WHERE id=$1::uuid""",
            ids["run"],
        )
        await pg_connection.execute(
            """INSERT INTO qc_rule_execution(task_id,rule_id,agent_run_id,execution_status,
                   judge_strategy,started_at,finished_at)
               VALUES($1::uuid,$2::uuid,$3::uuid,'pass','hybrid',now(),now())""",
            ids["task"],
            ids["rule"],
            ids["run"],
        )
        await _candidate(pg_connection, ids, "evidence_insufficient")
        yield ids
    finally:
        await transaction.rollback()


async def _candidate(connection, ids, status):
    await connection.execute(
        """INSERT INTO qc_aggregate_inspection_candidate(
               id,task_id,source_agent_run_id,rule_id,rule_code,rule_contract_hash,
               scope,scope_key,finalization_epoch,status,finished_at)
           VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'alioth_closure_1',$5,
                  'task',$6,0,$7::varchar,
                  CASE WHEN $7::varchar='expected' THEN NULL ELSE now() END)""",
        str(uuid4()),
        ids["task"],
        ids["run"],
        ids["rule"],
        uuid4().hex,
        "task-" + uuid4().hex,
        status,
    )


def _command(ids, **overrides):
    command = TerminateTaskFinalizationCommand(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        expected_finalization_epoch=0,
        expected_row_version=0,
        execution_status=TaskExecutionStatus.FAILED,
        reason_code="FINALIZATION_STALLED_TIMEOUT",
    )
    return replace(command, **overrides)


def _service(connection):
    return TaskFinalizationService(
        repository=PostgresTaskFinalizationRepository(pool=_ConnectionPool(connection))
    )


async def test_drained_old_main_failure_seals_failed_task_and_preserves_main_preview(
    pg_connection,
    stalled_case,
):
    ids = stalled_case
    query = PostgresWorkstationQueryRepository(pool=_ConnectionPool(pg_connection))
    scope = _scope(actor_user_id=ids["user"], tenant_id=ids["tenant"])
    before = await query.get_task_detail(ids["task"], access_scope=scope)
    assert before is not None and before.report_availability == "preview"
    original_run = await pg_connection.fetchrow(
        "SELECT * FROM qc_agent_run WHERE id=$1::uuid",
        ids["run"],
    )

    snapshot = await _service(pg_connection).terminate_stalled(
        _command(ids),
        minimum_terminal_age_seconds=600,
    )

    assert snapshot is not None and snapshot.state is TaskFinalizationState.SEALED
    assert snapshot.execution_status is TaskExecutionStatus.FAILED
    task = await pg_connection.fetchrow(
        """SELECT status,finalization_last_error,finished_at,quality_outcome
           FROM qc_task WHERE id=$1::uuid""",
        ids["task"],
    )
    assert task["status"] == "failed"
    assert task["finished_at"] is not None
    assert task["quality_outcome"] == "unknown"
    assert task["finalization_last_error"] == "FINALIZATION_STALLED_TIMEOUT"
    assert (
        await pg_connection.fetchrow(
            "SELECT * FROM qc_agent_run WHERE id=$1::uuid",
            ids["run"],
        )
        == original_run
    )
    after = await query.get_task_detail(ids["task"], access_scope=scope)
    assert after is not None and after.report_availability == "preview"
    gate = await pg_connection.fetchrow(
        "SELECT gate_passed,block_reasons FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid",
        ids["task"],
    )
    assert gate["gate_passed"] is False
    assert "h5_coverage_scope_incomplete" in gate["block_reasons"]
    assert (
        await pg_connection.fetchval(
            "SELECT count(*) FROM qc_report_gate_binding WHERE task_id=$1::uuid",
            ids["task"],
        )
        == 0
    )
    # 幂等重扫没有第二次封存，也没有新的正式报告。
    assert (
        await _service(pg_connection).terminate_stalled(
            _command(ids),
            minimum_terminal_age_seconds=600,
        )
        is None
    )
    assert (
        await pg_connection.fetchval(
            "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid",
            ids["task"],
        )
        == 1
    )


async def test_terminal_web_checkpoint_keeps_expired_claim_audit_without_blocking_finalization(
    pg_connection,
    stalled_case,
):
    ids = stalled_case
    await pg_connection.execute(
        """INSERT INTO qc_web_run_stage_checkpoint(run_id,logical_page_key,initial_identity,
               stage_status,attempt_count,claimed_worker_id,claim_token,claimed_at,lease_expires_at)
           VALUES($1::uuid,'page',repeat('a',64),'succeeded',1,'old-worker',gen_random_uuid(),
                  now()-interval '2 hours',now()-interval '1 hour')""",
        ids["run"],
    )
    result = await _service(pg_connection).terminate_stalled(
        _command(ids),
        minimum_terminal_age_seconds=600,
    )
    assert result is not None and result.state is TaskFinalizationState.SEALED


async def _closed_session_phase(connection, ids, *, phase_key, status):
    session_id, capture_id, phase_id = uuid4().hex, str(uuid4()), uuid4().hex
    await connection.execute(
        """INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,status,
               activated_at,draining_at,closed_at)
           VALUES($1,$2::uuid,'web','mock-closed-owner','closed',now(),now(),now())""",
        session_id, ids["task"],
    )
    await connection.execute(
        """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,
               capture_role,capture_status,captured_at)
           VALUES($1::uuid,$2::uuid,$3::uuid,'web',$4,'observation','captured',now())""",
        capture_id, ids["task"], ids["run"], session_id,
    )
    await connection.execute(
        """INSERT INTO qc_surface_phase_run(phase_run_id,task_id,session_id,surface_kind,
               phase_key,phase_protocol_version,idempotency_key,source_capture_id,status,
               command_sequence,finalization_epoch,context_payload,context_payload_hash,
               terminal_reason_code,finished_at)
           VALUES($1,$2::uuid,$3,'web',$4,'mock-phase.v1',$1,$5::uuid,$6::varchar,
                  1,0,'{}'::jsonb,'mock-hash',
                  CASE WHEN $6::varchar='running' THEN NULL ELSE 'MOCK_TERMINAL' END,
                  CASE WHEN $6::varchar='running' THEN NULL ELSE now() END)""",
        phase_id, ids["task"], session_id, phase_key, capture_id, status,
    )


@pytest.mark.parametrize("phase_key", ["web_guide_page", "auth_gate"])
async def test_running_phase_blocks_finalization_even_after_shared_session_closed(
    pg_connection, stalled_case, phase_key,
):
    ids = stalled_case
    await _closed_session_phase(pg_connection, ids, phase_key=phase_key, status="running")
    original_task = await pg_connection.fetchrow(
        "SELECT * FROM qc_task WHERE id=$1::uuid", ids["task"]
    )
    original_run = await pg_connection.fetchrow(
        "SELECT * FROM qc_agent_run WHERE id=$1::uuid", ids["run"]
    )
    assert await _service(pg_connection).terminate_stalled(
        _command(ids), minimum_terminal_age_seconds=600,
    ) is None
    assert await pg_connection.fetchrow(
        "SELECT * FROM qc_task WHERE id=$1::uuid", ids["task"]
    ) == original_task
    assert await pg_connection.fetchrow(
        "SELECT * FROM qc_agent_run WHERE id=$1::uuid", ids["run"]
    ) == original_run
    assert await pg_connection.fetchval(
        "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid", ids["task"]
    ) == 0


@pytest.mark.parametrize("status", ["completed", "blocked", "failed", "canceled"])
async def test_terminal_phase_does_not_block_stalled_finalization(
    pg_connection, stalled_case, status,
):
    await _closed_session_phase(
        pg_connection, stalled_case, phase_key="web_guide_page", status=status,
    )
    snapshot = await _service(pg_connection).terminate_stalled(
        _command(stalled_case), minimum_terminal_age_seconds=600,
    )
    assert snapshot is not None and snapshot.state is TaskFinalizationState.SEALED


async def test_other_tasks_running_phase_does_not_block_current_task(pg_connection, stalled_case):
    other = {**stalled_case, "task": str(uuid4()), "run": str(uuid4())}
    await pg_connection.execute(
        """INSERT INTO qc_task(id,target_url,ruleset_id,status,submitter_id,tenant_id)
           VALUES($1::uuid,'https://other.example.test',$2::uuid,'running',$3::uuid,$4::uuid)""",
        other["task"], other["ruleset"], other["user"], other["tenant"],
    )
    await pg_connection.execute(
        """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
           VALUES($1::uuid,$2::uuid,'main','其他模拟任务','running')""",
        other["run"], other["task"],
    )
    await _closed_session_phase(pg_connection, other, phase_key="web_guide_page", status="running")
    snapshot = await _service(pg_connection).terminate_stalled(
        _command(stalled_case), minimum_terminal_age_seconds=600,
    )
    assert snapshot is not None and snapshot.state is TaskFinalizationState.SEALED


@pytest.mark.parametrize(
    "outcome,expected_status", [("failed", "failed"), ("needs_review", "review")]
)
async def test_real_retry_loop_closes_stalled_full_inspection_and_keeps_original_main(
    pg_connection,
    stalled_case,
    monkeypatch,
    outcome,
    expected_status,
):
    ids = stalled_case
    config = {
        "workflow_version": "v2",
        "execution_mode": "full_inspection",
        "h5_inspection_notification_mode": "enforced",
        "_task_target": {"platform": "h5"},
    }
    await pg_connection.execute(
        """UPDATE qc_agent_run SET input_context=$2::jsonb,
               output_context=output_context || $3::jsonb,stop_reason=$4 WHERE id=$1::uuid""",
        ids["run"],
        json.dumps({"message": "异常收口回归", "task_config": config}),
        json.dumps({"execution_outcome": outcome}),
        "error" if outcome == "failed" else "incomplete",
    )
    original = await pg_connection.fetchrow(
        "SELECT * FROM qc_agent_run WHERE id=$1::uuid", ids["run"]
    )
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(client, ttl_seconds=60, queue_key="test:stalled:" + uuid4().hex)
    state = PostgresAgentRunStateStore(pool=_ConnectionPool(pg_connection))
    monkeypatch.setattr(
        worker_module, "PostgresH5ProbeTerminalSettlementRepository",
        lambda: PostgresH5ProbeTerminalSettlementRepository(pool=_ConnectionPool(pg_connection)),
    )
    try:
        assert (
            await worker_module._retry_pending_task_finalizations(
                queue,
                state,
                _service(pg_connection),
                finalization_stall_timeout_seconds=600,
            )
            == 1
        )
        assert (
            await pg_connection.fetchval(
                "SELECT status FROM qc_task WHERE id=$1::uuid",
                ids["task"],
            )
            == expected_status
        )
        assert (
            await pg_connection.fetchrow(
                "SELECT * FROM qc_agent_run WHERE id=$1::uuid",
                ids["run"],
            )
            == original
        )
        record = await queue.get(ids["run"])
        assert record is not None and record.status is AgentRunQueueStatus.FAILED
        assert record.termination_reason == ("error" if outcome == "failed" else "incomplete")
        assert record.execution_outcome == outcome
        assert record.final_report is not None
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "blocker",
    [
        "recent_main",
        "main_running",
        "new_main",
        "judge",
        "session",
        "session_lease",
        "inspection",
        "snapshot_request",
        "inspection_attempt",
        "expected_aggregate",
        "aggregate_work",
        "app_lease",
        "device_lease",
        "web_checkpoint",
        "web_claim",
        "cancel_requested",
        "epoch",
        "version",
        "sealed",
    ],
)
async def test_actual_active_work_or_changed_fence_prevents_stalled_finalization(
    pg_connection,
    stalled_case,
    blocker,
):
    ids = stalled_case
    if blocker == "recent_main":
        await pg_connection.execute(
            "UPDATE qc_agent_run SET finished_at=now() WHERE id=$1::uuid",
            ids["run"],
        )
    elif blocker == "main_running":
        await pg_connection.execute(
            "UPDATE qc_agent_run SET status='running' WHERE id=$1::uuid",
            ids["run"],
        )
    elif blocker in {"new_main", "judge"}:
        await pg_connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
               VALUES($1::uuid,$2::uuid,$3,'新执行者','pending')""",
            str(uuid4()),
            ids["task"],
            "main" if blocker == "new_main" else "rule_judge",
        )
    elif blocker in {"session", "session_lease"}:
        await pg_connection.execute(
            """INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,status,
                   lease_owner,lease_token,lease_expires_at,activated_at,draining_at)
               VALUES($1,$2::uuid,'web','test-owner',$3::varchar,$4,$5::uuid,$6,now(),
                      CASE WHEN $3::varchar='draining' THEN now() ELSE NULL END)""",
            uuid4().hex,
            ids["task"],
            "active" if blocker == "session" else "draining",
            None if blocker == "session" else "worker",
            None if blocker == "session" else str(uuid4()),
            None if blocker == "session" else datetime.now(UTC),
        )
    elif blocker in {"inspection", "snapshot_request", "inspection_attempt"}:
        work_id, _capture = await _seed_work_item(
            pg_connection,
            ids,
            name=uuid4().hex,
            page_seq=1,
            status="pending",
            attempt_count=1,
            request_status="pending" if blocker == "snapshot_request" else None,
        )
        if blocker != "inspection":
            await pg_connection.execute(
                """UPDATE qc_inspection_work_item SET status='succeeded',finished_at=now()
                   WHERE id=$1::uuid""",
                work_id,
            )
        if blocker != "inspection_attempt":
            await pg_connection.execute(
                """UPDATE qc_inspection_attempt SET status='abandoned',finished_at=now()
                   WHERE work_item_id=$1::uuid""",
                work_id,
            )
    elif blocker == "expected_aggregate":
        await _candidate(pg_connection, ids, "expected")
    elif blocker == "aggregate_work":
        input_hash = uuid4().hex + uuid4().hex
        input_set = "evidence-input-set:" + input_hash
        await pg_connection.execute(
            """INSERT INTO qc_evidence_input_set(
                   input_set_id,input_set_hash,task_id,source_agent_run_id,rule_id,rule_code,
                   rule_contract_hash,ruleset_snapshot_hash,scope,scope_key,finalization_epoch,
                   eligibility_snapshot_id,eligibility_snapshot_hash,selection_policy_hash,
                   eligible_capture_ids,protocol_version,input_set_payload,sealed_at)
               VALUES($1,$2,$3::uuid,$4::uuid,$5::uuid,'alioth_closure_1','contract','snapshot',
                      'task','test_scope',0,'eligibility','hash','policy','["capture-1"]'::jsonb,
                      'evidence-input-set.v1','{}'::jsonb,now())""",
            input_set,
            input_hash,
            ids["task"],
            ids["run"],
            ids["rule"],
        )
        await pg_connection.execute(
            """INSERT INTO qc_aggregate_inspection_work_item(
                   id,task_id,evidence_input_set_id,producer_agent_run_id,rule_id,rule_code,
                   rule_contract_hash,scope,scope_key,protocol_version,input_hash,finalization_epoch)
               VALUES(gen_random_uuid(),$1::uuid,$2,$3::uuid,$4::uuid,'alioth_closure_1','contract',
                      'task','test_scope','aggregate-inspection-work-item.v1',$5,0)""",
            ids["task"],
            input_set,
            ids["run"],
            ids["rule"],
            input_hash,
        )
    elif blocker == "app_lease":
        await pg_connection.execute(
            """UPDATE qc_task SET target_platform='app',target_url=NULL,
                   task_config=task_config || '{"app":{"platform_build_ref":"test-only"}}'::jsonb
               WHERE id=$1::uuid""",
            ids["task"],
        )
        await pg_connection.execute(
            "UPDATE qc_agent_run SET status='running' WHERE id=$1::uuid",
            ids["run"],
        )
        await pg_connection.execute(
            """INSERT INTO qc_app_run_execution_lease(
                   agent_run_id,task_id,lease_owner,lease_token,lease_expires_at)
               VALUES($1::uuid,$2::uuid,'worker',gen_random_uuid(),now()+interval '1 minute')""",
            ids["run"],
            ids["task"],
        )
        await pg_connection.execute(
            "UPDATE qc_agent_run SET status='failed' WHERE id=$1::uuid",
            ids["run"],
        )
    elif blocker == "device_lease":
        device = uuid4().hex
        await pg_connection.execute(
            """INSERT INTO qc_device_resource(device_ref,backend,profile_id)
               VALUES($1,'test-only','test-only')""",
            device,
        )
        await pg_connection.execute(
            """INSERT INTO qc_device_lease(lease_id,device_ref,task_id,session_id,lease_owner,
                   lease_token,fencing_token,expires_at)
               VALUES(gen_random_uuid(),$1,$2::uuid,'test-session','worker',
                      gen_random_uuid(),1,now()+interval '1 minute')""",
            device,
            ids["task"],
        )
    elif blocker in {"web_checkpoint", "web_claim"}:
        await pg_connection.execute(
            """INSERT INTO qc_web_run_stage_checkpoint(run_id,logical_page_key,initial_identity,
                   stage_status,attempt_count,claimed_worker_id,claim_token,claimed_at,lease_expires_at)
               VALUES($1::uuid,'page',repeat('a',64),$2,1,$3::text,$4::uuid,now(),
                      CASE WHEN $3::text IS NULL THEN NULL ELSE now()+interval '1 minute' END)""",
            ids["run"],
            "pending" if blocker == "web_checkpoint" else "succeeded",
            None if blocker == "web_checkpoint" else "worker",
            None if blocker == "web_checkpoint" else str(uuid4()),
        )
    elif blocker == "cancel_requested":
        await pg_connection.execute(
            """UPDATE qc_agent_run SET output_context=output_context ||
                   '{"cancel_requested":true}'::jsonb WHERE id=$1::uuid""",
            ids["run"],
        )
    elif blocker in {"epoch", "version"}:
        column = "finalization_epoch" if blocker == "epoch" else "finalization_row_version"
        await pg_connection.execute(
            f"UPDATE qc_task SET {column}={column}+1 WHERE id=$1::uuid",
            ids["task"],
        )
    elif blocker == "sealed":
        await _service(pg_connection).terminate_stalled(
            _command(ids),
            minimum_terminal_age_seconds=600,
        )
    before = await pg_connection.fetchrow("SELECT * FROM qc_task WHERE id=$1::uuid", ids["task"])
    original_gate_count = await pg_connection.fetchval(
        "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid",
        ids["task"],
    )
    assert (
        await _service(pg_connection).terminate_stalled(
            _command(ids),
            minimum_terminal_age_seconds=600,
        )
        is None
    )
    assert (
        await pg_connection.fetchrow(
            "SELECT * FROM qc_task WHERE id=$1::uuid",
            ids["task"],
        )
        == before
    )
    assert (
        await pg_connection.fetchval(
            "SELECT count(*) FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid",
            ids["task"],
        )
        == original_gate_count
    )


async def test_active_recovery_lease_cannot_be_bypassed(pg_connection, stalled_case):
    ids = stalled_case
    await pg_connection.execute(
        """UPDATE qc_task SET finalization_recovery_lease_owner='other-worker',
               finalization_recovery_lease_token=gen_random_uuid(),
               finalization_recovery_lease_expires_at=now()+interval '1 minute'
           WHERE id=$1::uuid""",
        ids["task"],
    )
    with pytest.raises(TaskFinalizationPersistenceError) as error:
        await _service(pg_connection).terminate_stalled(
            _command(ids),
            minimum_terminal_age_seconds=600,
        )
    assert isinstance(error.value.__cause__, FinalizationRecoveryFenceError)
    assert (
        await pg_connection.fetchval(
            "SELECT status FROM qc_task WHERE id=$1::uuid",
            ids["task"],
        )
        == "running"
    )


@pytest.mark.parametrize("platform", ["h5", "web"])
@pytest.mark.parametrize("outcome", ["canceled", "timeout"])
async def test_real_worker_route_persists_run_and_task_when_workflow_is_stopped(
    pg_connection,
    stalled_case,
    monkeypatch,
    platform,
    outcome,
):
    ids = stalled_case
    await pg_connection.execute(
        """DELETE FROM qc_rule_execution WHERE task_id=$1::uuid;
        """,
        ids["task"],
    )
    await pg_connection.execute(
        "DELETE FROM qc_aggregate_inspection_candidate WHERE task_id=$1::uuid",
        ids["task"],
    )
    await pg_connection.execute(
        """UPDATE qc_task SET target_platform=$2,finalization_state='open',
               finalization_agent_run_id=NULL,exploration_draining_at=NULL,session_draining_at=NULL
           WHERE id=$1::uuid""",
        ids["task"],
        platform,
    )
    await pg_connection.execute("DELETE FROM qc_agent_run WHERE id=$1::uuid", ids["run"])
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(
        client, ttl_seconds=60, queue_key="test:route-control:" + uuid4().hex
    )
    config = {
        "workflow_version": "v2",
        "web_runtime_version": "web-runtime.v2",
        "execution_mode": "exploration_only",
        "_task_target": {"platform": platform, "target_url": "https://example.test/"},
    }
    record = await queue.enqueue(
        "真实路由运行控制",
        task_id=ids["task"],
        task_config=config,
        loop_guard={"max_duration_seconds": 0.1 if outcome == "timeout" else 5},
    )
    state = PostgresAgentRunStateStore(pool=_ConnectionPool(pg_connection))
    await state.record_submitted(record)
    record.status = AgentRunQueueStatus.RUNNING
    record.started_at = datetime.now(UTC)
    await state.record_started(record)
    started = asyncio.Event()
    cleaned = asyncio.Event()

    class Dispatcher:
        async def dispatch_h5_v2(self, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        dispatch_web_v2 = dispatch_h5_v2

    async def no_side_effect(**_kwargs):
        return 0

    async def no_record_side_effect(_record, *_args, **_kwargs):
        return False

    class Handoff:
        async def prepare(self, **_kwargs):
            return H5EvidenceHandoffSummary(
                evidence_ready=True,
                judgeable_capture_count=0,
                page_types=(),
                published_work_item_count=0,
            )

    # 替换跨模块证据服务和遥测；真正路由、运行监护及 PostgreSQL 状态 Store 保持原实现。
    monkeypatch.setattr(worker_module, "build_task_progress_recorder", lambda **_kwargs: None)
    monkeypatch.setattr(
        worker_module, "_attach_h5_v2_exploration_action_trace", no_record_side_effect
    )
    monkeypatch.setattr(
        worker_module, "_reconcile_run_llm_usage_from_events", no_record_side_effect
    )
    monkeypatch.setattr(worker_module, "_handoff_failed_h5_run_evidence", no_record_side_effect)
    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", no_side_effect)
    monkeypatch.setattr(worker_module, "_generate_final_report", no_record_side_effect)
    handler = (
        worker_module._process_h5_v2_one if platform == "h5" else worker_module._process_web_v2_one
    )
    dependencies = {"h5_evidence_handoff": Handoff()} if platform == "h5" else {}
    task = asyncio.create_task(
        handler(
            queue,
            record,
            settings=Settings(),
            state_store=state,
            dispatcher=Dispatcher(),
            **dependencies,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        if outcome == "canceled":
            assert await state.record_cancel_requested(record.run_id)
        await asyncio.wait_for(task, timeout=3)
        assert cleaned.is_set()
        main = await pg_connection.fetchrow(
            "SELECT status,stop_reason,finished_at FROM qc_agent_run WHERE id=$1::uuid",
            record.run_id,
        )
        assert main["status"] == ("canceled" if outcome == "canceled" else "failed")
        assert main["stop_reason"] == outcome
        assert main["finished_at"] is not None
        assert await pg_connection.fetchval(
            "SELECT status FROM qc_task WHERE id=$1::uuid",
            ids["task"],
        ) == ("canceled" if outcome == "canceled" else "review")
        projected = await queue.get(record.run_id)
        assert projected is not None and projected.termination_reason == outcome
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()
