"""隔离 PostgreSQL 验证 APP 零采集技术失败不能被无限延迟永久屏蔽收尾。"""

import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from services.api.app import worker
from services.api.app.application.task_finalization import (
    EvaluateCompletionGateCommand,
    TaskFinalizationService,
    TerminateTaskFinalizationCommand,
)
from services.api.app.core.config import Settings, WorkerSettings
from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome
from services.api.app.infrastructure.db import task_finalization_repository as repository_module
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
)
from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore
from services.api.tests.test_app_bootstrap_settlement_postgres import _configure
from services.api.tests.test_app_evidence_index_postgres_integration import (
    _database,
    _insert_capture,
    _SingleConnectionPool,
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库完整迁移
)
from services.api.tests.test_worker import queue as queue  # noqa: F401 - 独立 Redis 队列夹具

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库",
)


def _command(ids, snapshot):
    return EvaluateCompletionGateCommand(
        task_id=ids["task"], agent_run_id=ids["run"], expected_finalization_epoch=0,
        expected_row_version=snapshot.row_version, execution_status=TaskExecutionStatus.FAILED,
        quality_outcome=TaskQualityOutcome.UNKNOWN,
    )


@asynccontextmanager
async def _failed_bootstrap(*, output_context=None):
    async with _database() as (connection, ids):
        rules, config = await _configure(connection, ids)
        config["_task_target"] = {"platform": "app"}
        await connection.execute(
            """UPDATE qc_task SET finalization_state='session_draining',
                      exploration_draining_at=now(),session_draining_at=now(),
                      task_config=task_config || $2::jsonb WHERE id=$1::uuid""",
            ids["task"], json.dumps(config),
        )
        await connection.execute(
            """UPDATE qc_surface_session SET status='lost',activated_at=now(),lost_at=now(),
                      failure_reason='启动技术失败' WHERE session_id=$1""", ids["session"],
        )
        await connection.execute(
            """UPDATE qc_agent_run SET status='failed',stop_reason='error',
                      finished_at=now()-interval '2 hours',input_context=$2::jsonb,
                      output_context=$3::jsonb,
                      error_message='SESSION_START_FAILED' WHERE id=$1::uuid""",
            ids["run"], json.dumps({"task_config": config}), json.dumps(output_context or {}),
        )
        pool = _SingleConnectionPool(connection)
        repository = PostgresTaskFinalizationRepository(pool=pool)
        first = await repository.evaluate_and_seal(
            command=_command(ids, await repository.get(task_id=ids["task"])),
        )
        assert not first.snapshot.gate_passed
        assert await repository.defer_unchanged_completion_gate(
            command=_command(ids, first.finalization),
        )
        assert await connection.fetchval(
            "SELECT finalization_retry_at='infinity'::timestamptz FROM qc_task WHERE id=$1::uuid",
            ids["task"],
        )
        yield connection, ids, rules, pool, repository


@pytest.mark.parametrize("runtime_review", [False, True])
async def test_real_blocked_infinity_task_seals_without_trusting_preview(queue, runtime_review):
    # 实跑 APP 启动失败按既有合同保留 needs_review；旧预览中的虚假完成计数不能改变门禁。
    output = {"execution_outcome": "needs_review", "error_category": "infrastructure",
              "final_report": {"summary": {
                  "overall_status": "incomplete", "total_rules": 73, "skipped": 73,
                  "evaluated_rules": 73, "qc_success_rate": 100.0, "not_applicable_rules": 73,
                  "candidate_rule_count": 73, "completed_candidate_rules": 73,
                  "candidate_execution_complete": False, "reached_target_pages": 0,
              }}} if runtime_review else {}
    async with _failed_bootstrap(output_context=output) as (
        connection, ids, _rules, pool, repository,
    ):
        assert [item.task_id for item in await repository.list_recoverable_finalizations()] == [
            ids["task"],
        ]
        original_main = await connection.fetchrow(
            "SELECT * FROM qc_agent_run WHERE id=$1::uuid", ids["run"],
        )
        assert await worker._retry_pending_task_finalizations(
            queue, PostgresAgentRunStateStore(pool=pool),
            TaskFinalizationService(repository=repository),
        ) == 1
        task = await connection.fetchrow(
            """SELECT status,finalization_state,execution_status,quality_outcome,
                      finalization_last_error FROM qc_task WHERE id=$1::uuid""", ids["task"],
        )
        assert dict(task) == {
            "status": "review" if runtime_review else "failed", "finalization_state": "sealed",
            "execution_status": "needs_review" if runtime_review else "failed",
            "quality_outcome": "unknown", "finalization_last_error": "FINALIZATION_STALLED_TIMEOUT",
        }
        assert await connection.fetchrow(
            "SELECT * FROM qc_agent_run WHERE id=$1::uuid", ids["run"],
        ) == original_main
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_report_gate_binding WHERE task_id=$1::uuid", ids["task"],
        ) == 0
        assert await connection.fetchval(
            """SELECT gate_passed FROM qc_completion_gate_snapshot WHERE gate_snapshot_id=(
                   SELECT completion_gate_snapshot_id FROM qc_task WHERE id=$1::uuid)""",
            ids["task"],
        ) is False
        assert await repository.list_recoverable_finalizations() == []


@pytest.mark.parametrize("mode", [
    "review", "completed_main", "cancelled", "deleted", "capture", "app_capture",
    "old_owner", "paused", "finite_backoff", "recent_main",
])
async def test_infinity_retry_override_does_not_reopen_business_waits_or_wrong_owner(mode):
    async with _failed_bootstrap() as (connection, ids, _rules, _pool, repository):
        if mode == "review":
            await connection.execute(
                "UPDATE qc_task SET status='review',finished_at=now() WHERE id=$1::uuid",
                ids["task"],
            )
        elif mode == "completed_main":
            await connection.execute(
                "UPDATE qc_agent_run SET status='done' WHERE id=$1::uuid", ids["run"],
            )
        elif mode == "cancelled":
            await connection.execute(
                """UPDATE qc_agent_run SET output_context='{"cancel_requested":true}'::jsonb
                   WHERE id=$1::uuid""", ids["run"],
            )
        elif mode == "deleted":
            await connection.execute(
                "UPDATE qc_task SET deleted_at=now() WHERE id=$1::uuid", ids["task"],
            )
        elif mode in {"capture", "app_capture"}:
            capture = await _insert_capture(
                connection, ids, capture_role="observation", semantic_role="final_observation",
            )
            if mode == "app_capture":
                artifact = str(uuid4())
                await connection.execute(
                    """INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,
                              artifact_type,source,storage_path,checksum,size_bytes)
                       VALUES($1::uuid,$2::uuid,$3::uuid,'screenshot','baseline','tests/real.png',
                              'sha256:' || repeat('a',64),1)""",
                    artifact, ids["task"], capture,
                )
                await connection.execute(
                    """INSERT INTO qc_app_capture(capture_id,screenshot_artifact_id,width,height,
                              image_signature) VALUES($1::uuid,$2::uuid,100,200,'real')""",
                    capture, artifact,
                )
        elif mode == "old_owner":
            await connection.execute(
                "UPDATE qc_task SET finalization_agent_run_id=NULL WHERE id=$1::uuid", ids["task"],
            )
        elif mode == "paused":
            await connection.execute(
                """UPDATE qc_task SET finalization_recovery_state='quarantined',
                          finalization_error_signature=repeat('a',64),
                          finalization_same_error_count=3,finalization_quarantined_at=now()
                   WHERE id=$1::uuid""", ids["task"],
            )
        elif mode == "finite_backoff":
            await connection.execute(
                """UPDATE qc_task SET finalization_retry_at=now()+interval '1 hour'
                   WHERE id=$1::uuid""", ids["task"],
            )
        elif mode == "recent_main":
            await connection.execute(
                "UPDATE qc_agent_run SET finished_at=now() WHERE id=$1::uuid", ids["run"],
            )
        before = await connection.fetchrow("SELECT * FROM qc_task WHERE id=$1::uuid", ids["task"])
        assert await repository.list_recoverable_finalizations() == []
        assert await connection.fetchrow(
            "SELECT * FROM qc_task WHERE id=$1::uuid", ids["task"],
        ) == before


async def test_retry_override_uses_configured_stall_timeout(monkeypatch):
    async with _failed_bootstrap() as (connection, ids, _rules, _pool, repository):
        await connection.execute(
            """UPDATE qc_agent_run SET finished_at=now()-interval '15 minutes'
               WHERE id=$1::uuid""", ids["run"],
        )
        for timeout, expected in [(1800, []), (600, [ids["task"]])]:
            configured = Settings(worker=WorkerSettings(finalization_stall_timeout_seconds=timeout))
            monkeypatch.setattr(repository_module, "get_settings", lambda value=configured: value)
            actual = await repository.list_recoverable_finalizations()
            assert [item.task_id for item in actual] == expected


async def test_scanned_failure_with_real_pending_candidate_cannot_terminate():
    async with _failed_bootstrap() as (connection, ids, rules, _pool, repository):
        await connection.execute(
            """INSERT INTO qc_aggregate_inspection_candidate(id,task_id,source_agent_run_id,
                      rule_id,rule_code,rule_contract_hash,scope,scope_key,finalization_epoch,status)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'task','pending',0,'expected')""",
            str(uuid4()), ids["task"], ids["run"], rules[0]["rule_id"],
            rules[0]["rule_code"], "real-contract",
        )
        snapshot, = await repository.list_recoverable_finalizations()
        before = await connection.fetchrow("SELECT * FROM qc_task WHERE id=$1::uuid", ids["task"])
        assert await repository.terminate_stalled(command=TerminateTaskFinalizationCommand(
            task_id=ids["task"], agent_run_id=ids["run"], expected_finalization_epoch=0,
            expected_row_version=snapshot.row_version, execution_status=TaskExecutionStatus.FAILED,
            reason_code="FINALIZATION_STALLED_TIMEOUT",
        ), minimum_terminal_age_seconds=600) is None
        assert await connection.fetchrow(
            "SELECT * FROM qc_task WHERE id=$1::uuid", ids["task"],
        ) == before
