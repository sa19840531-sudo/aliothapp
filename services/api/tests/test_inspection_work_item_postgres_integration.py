"""真实 PostgreSQL 持久化工作项链路测试。

默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import fakeredis
import pytest

from services.api.app import worker as worker_module
from services.api.app.agents.runtime.business_middlewares.base import TaskRuntimeSnapshot
from services.api.app.agents.runtime.h5_inspection_publication_wakeup import (
    H5InspectionPublicationWakeupHandler,
)
from services.api.app.application.app_inspection_publisher import (
    AppCaptureInspectionPublisher,
)
from services.api.app.application.h5_live_inspection_publish import (
    H5LiveInspectionPublishService,
    PostgresH5LiveInspectionTaskGateReader,
)
from services.api.app.application.inspection_work_items import (
    ClaimInspectionWorkItemCommand,
    InspectionAttemptStatus,
    InspectionWorkItemConflictError,
    InspectionWorkItemLease,
    InspectionWorkItemRule,
    InspectionWorkItemRuleStatus,
    InspectionWorkItemService,
    InspectionWorkItemStatus,
    InspectionWorkItemValidationError,
    PublishInspectionWorkItemCommand,
    RenewInspectionWorkItemLeaseCommand,
    StartInspectionAttemptCommand,
    TransitionInspectionAttemptCommand,
    TransitionInspectionWorkItemCommand,
    TransitionInspectionWorkItemRuleCommand,
)
from services.api.app.application.rule_execution import RuleExecutionService
from services.api.app.application.task_finalization import (
    BeginExplorationDrainingCommand,
    TaskFinalizationService,
)
from services.api.app.application.task_query import TaskQueryService
from services.api.app.application.web_inspection_reconciliation import (
    ReconcileWebInspectionCommand,
    WebInspectionReconciliation,
)
from services.api.app.application.web_page_inspection_notification import (
    WebPageInspectionNotification,
)
from services.api.app.domain.exploration.web_coverage_snapshot import (
    WebCoverageSnapshot,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.inspection import (
    H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
)
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    FinalizationRecoveryFenceError,
)
from services.api.app.infrastructure.db.h5_evidence_handoff_repository import (
    PostgresH5EvidenceCaptureReader,
)
from services.api.app.infrastructure.db.h5_evidence_inventory_repository import (
    PostgresH5EvidenceInventoryRepository,
)
from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
    TaskFinalizationPersistenceError,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.task_repository import (
    PostgresTaskRepository,
    _assert_inspection_write_lease,
)
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
    PostgresWebCoverageSnapshotRepository,
)
from services.api.app.infrastructure.db.web_formal_terminal_ledger_repository import (
    PostgresWebFormalTerminalLedgerRepository,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue
from services.api.app.infrastructure.queue.redis_stream import RedisStreamWakeup
from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore


async def test_postgres_work_item_survives_full_persisted_lifecycle() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in ("tenant", "user", "ruleset", "rule", "task", "run", "capture")
    }
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        service = InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository(pool=pool)
        )
        publish_command = _publish_command(ids)

        published = await service.publish(publish_command)
        duplicate = await service.publish(publish_command)

        assert published.work_item_id == duplicate.work_item_id
        assert published.created is True
        assert duplicate.created is False

        lease = await service.claim(
            ClaimInspectionWorkItemCommand(
                task_id=ids["task"],
                consumer_agent_run_id=ids["run"],
                lease_owner="postgres-integration-worker",
                lease_seconds=180,
                session_dependency="snapshot",
            )
        )
        assert lease is not None
        assert lease.rules[0].rule_code == "alioth_review_1"

        row_version = await service.transition(
            TransitionInspectionWorkItemCommand(
                work_item_id=lease.work_item_id,
                expected_status=InspectionWorkItemStatus.LEASED,
                target_status=InspectionWorkItemStatus.RUNNING,
                row_version=lease.row_version,
                lease_token=lease.lease_token,
            )
        )
        row_version = await service.renew_lease(
            RenewInspectionWorkItemLeaseCommand(
                work_item_id=lease.work_item_id,
                consumer_agent_run_id=ids["run"],
                lease_token=lease.lease_token,
                row_version=row_version,
                lease_seconds=180,
            )
        )
        attempt_id = await service.start_attempt(
            StartInspectionAttemptCommand(
                work_item_id=lease.work_item_id,
                consumer_agent_run_id=ids["run"],
                attempt_no=lease.attempt_no,
                lease_token=lease.lease_token,
                input_hash=lease.input_hash,
            )
        )
        packet_id, execution_id = await _seed_result(connection, ids)

        await service.transition_rule(
            TransitionInspectionWorkItemRuleCommand(
                work_item_id=lease.work_item_id,
                rule_id=ids["rule"],
                expected_status=InspectionWorkItemRuleStatus.PENDING,
                target_status=InspectionWorkItemRuleStatus.JUDGED,
                lease_token=lease.lease_token,
                observation_packet_id=packet_id,
            )
        )
        await service.transition_attempt(
            TransitionInspectionAttemptCommand(
                attempt_id=attempt_id,
                expected_status=InspectionAttemptStatus.RUNNING,
                target_status=InspectionAttemptStatus.JUDGED,
                lease_token=lease.lease_token,
                result_payload={
                    "items": [
                        {
                            "rule_code": "alioth_review_1",
                            "execution_status": "pass",
                            "evidence_artifact_ids": [],
                        }
                    ]
                },
            )
        )
        await _assert_inspection_write_lease(
            connection,
            task_id=ids["task"],
            work_item_id=lease.work_item_id,
            attempt_id=attempt_id,
            lease_token=lease.lease_token,
            agent_run_id=ids["run"],
        )
        await service.transition_rule(
            TransitionInspectionWorkItemRuleCommand(
                work_item_id=lease.work_item_id,
                rule_id=ids["rule"],
                expected_status=InspectionWorkItemRuleStatus.JUDGED,
                target_status=InspectionWorkItemRuleStatus.WRITTEN,
                lease_token=lease.lease_token,
                observation_packet_id=packet_id,
                rule_execution_id=execution_id,
            )
        )
        await service.transition_attempt(
            TransitionInspectionAttemptCommand(
                attempt_id=attempt_id,
                expected_status=InspectionAttemptStatus.JUDGED,
                target_status=InspectionAttemptStatus.WRITTEN,
                lease_token=lease.lease_token,
            )
        )
        final_version = await service.transition(
            TransitionInspectionWorkItemCommand(
                work_item_id=lease.work_item_id,
                expected_status=InspectionWorkItemStatus.RUNNING,
                target_status=InspectionWorkItemStatus.SUCCEEDED,
                row_version=row_version,
                lease_token=lease.lease_token,
            )
        )

        assert final_version == row_version + 1
        assert await service.has_nonterminal_work(task_id=ids["task"]) is False
        assert (
            await service.claim(
                ClaimInspectionWorkItemCommand(
                    task_id=ids["task"],
                    consumer_agent_run_id=ids["run"],
                    lease_owner="postgres-integration-worker",
                    lease_seconds=180,
                    session_dependency="snapshot",
                )
            )
            is None
        )
    finally:
        await connection.execute(
            "DELETE FROM qc_task WHERE id = $1::uuid",
            ids["task"],
        )
        await connection.execute(
            "DELETE FROM qc_rule WHERE id = $1::uuid",
            ids["rule"],
        )
        await connection.execute(
            "DELETE FROM qc_ruleset WHERE id = $1::uuid",
            ids["ruleset"],
        )
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def test_finalization_retry_quarantines_identical_error_and_admin_recovers() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in ("tenant", "user", "ruleset", "rule", "task", "run", "capture")
    }
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        finalization = PostgresTaskFinalizationRepository(pool=pool)
        await connection.execute(
            "UPDATE qc_task SET target_platform='web',task_config=task_config||"
            "'{\"workflow_version\":\"v2\",\"execution_mode\":\"full_inspection\","
            "\"web_inspection_notification_mode\":\"enforced\"}'::jsonb WHERE id=$1::uuid",
            ids["task"],
        )
        for _ in range(3):
            token = str(uuid4())
            await connection.execute(
                "UPDATE qc_task SET finalization_recovery_lease_token=$2::uuid,"
                "finalization_recovery_lease_owner='isolated-recovery',"
                "finalization_recovery_lease_expires_at=now()+interval '1 minute' "
                "WHERE id=$1::uuid",
                ids["task"], token,
            )
            # 旧租约不得消耗同错误计数，更不能制造隔离终态。
            await finalization.record_finalization_retry_error(
                task_id=ids["task"], error_message="RuntimeError: immutable binding failed",
                lease_token=str(uuid4()), recovery_generation=0, expected_status="running",
            )
            before = await connection.fetchval(
                "SELECT finalization_same_error_count FROM qc_task WHERE id=$1::uuid", ids["task"],
            )
            await finalization.record_finalization_retry_error(
                task_id=ids["task"],
                error_message="RuntimeError: immutable binding failed",
                lease_token=token, recovery_generation=0, expected_status="running",
            )
            assert await connection.fetchval(
                "SELECT finalization_same_error_count FROM qc_task WHERE id=$1::uuid", ids["task"],
            ) == before + 1

        quarantined = await connection.fetchrow(
            """
            SELECT status, finalization_recovery_state,
                   finalization_same_error_count, finalization_quarantined_at
            FROM qc_task
            WHERE id = $1::uuid
            """,
            ids["task"],
        )
        assert quarantined is not None
        assert quarantined["status"] == "running"
        assert quarantined["finalization_recovery_state"] == "quarantined"
        assert quarantined["finalization_same_error_count"] == 3
        assert quarantined["finalization_quarantined_at"] is not None

        recovery = PostgresWorkstationQueryRepository(pool=pool)
        result = await recovery.retry_quarantined_finalization(
            task_id=ids["task"],
            access_scope=AccessScope(
                actor_user_id=ids["user"],
                tenant_id=ids["tenant"],
                permissions=frozenset({"taskObjects:write:any"}),
            ),
        )
        assert result is not None
        assert result.status == "running"
        recovered = await connection.fetchrow(
            """
            SELECT finalization_recovery_state, finalization_same_error_count,
                   finalization_recovery_generation, finalization_recovered_at
            FROM qc_task
            WHERE id = $1::uuid
            """,
            ids["task"],
        )
        assert recovered is not None
        assert recovered["finalization_recovery_state"] == "active"
        assert recovered["finalization_same_error_count"] == 0
        assert recovered["finalization_recovery_generation"] == 1
        assert recovered["finalization_recovered_at"] is not None
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute(
            "DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"]
        )
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute(
            "DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"]
        )
        await pool.close()
        await connection.close()


async def test_postgres_work_item_recovers_judged_attempt_without_rejudging() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in ("tenant", "user", "ruleset", "rule", "task", "run", "capture")
    }
    missing_rule_id: str | None = None
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        service = InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository(pool=pool)
        )
        published = await service.publish(_publish_command(ids))
        first_lease = await service.claim(
            ClaimInspectionWorkItemCommand(
                task_id=ids["task"],
                consumer_agent_run_id=ids["run"],
                lease_owner="postgres-recovery-worker",
                lease_seconds=180,
                session_dependency="snapshot",
                page_capture_id=ids["capture"],
                rule_codes=("alioth_review_1",),
            )
        )
        assert first_lease is not None
        first_version = await service.transition(
            TransitionInspectionWorkItemCommand(
                work_item_id=published.work_item_id,
                expected_status=InspectionWorkItemStatus.LEASED,
                target_status=InspectionWorkItemStatus.RUNNING,
                row_version=first_lease.row_version,
                lease_token=first_lease.lease_token,
            )
        )
        first_attempt_id = await service.start_attempt(
            StartInspectionAttemptCommand(
                work_item_id=published.work_item_id,
                consumer_agent_run_id=ids["run"],
                attempt_no=first_lease.attempt_no,
                lease_token=first_lease.lease_token,
                input_hash=first_lease.input_hash,
            )
        )
        durable_payload = {
            "items": [
                {
                    "rule_id": ids["rule"],
                    "rule_code": "alioth_review_1",
                    "execution_status": "pass",
                    "observation_packet_id": None,
                    "screenshot_count": 1,
                }
            ]
        }
        await service.transition_attempt(
            TransitionInspectionAttemptCommand(
                attempt_id=first_attempt_id,
                expected_status=InspectionAttemptStatus.RUNNING,
                target_status=InspectionAttemptStatus.JUDGED,
                lease_token=first_lease.lease_token,
                result_payload=durable_payload,
            )
        )
        await service.transition_attempt(
            TransitionInspectionAttemptCommand(
                attempt_id=first_attempt_id,
                expected_status=InspectionAttemptStatus.JUDGED,
                target_status=InspectionAttemptStatus.RETRYABLE_ERROR,
                lease_token=first_lease.lease_token,
                error_code="WRITE_INTERRUPTED",
                error_message="模拟 Judge 后写回中断",
            )
        )
        await service.transition(
            TransitionInspectionWorkItemCommand(
                work_item_id=published.work_item_id,
                expected_status=InspectionWorkItemStatus.RUNNING,
                target_status=InspectionWorkItemStatus.RETRY_WAIT,
                row_version=first_version,
                lease_token=first_lease.lease_token,
                next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
                error_code="WRITE_INTERRUPTED",
                error_message="模拟 Judge 后写回中断",
            )
        )

        recovered_lease = await service.claim(
            ClaimInspectionWorkItemCommand(
                task_id=ids["task"],
                consumer_agent_run_id=ids["run"],
                lease_owner="postgres-recovery-worker",
                lease_seconds=180,
                page_capture_id=ids["capture"],
                rule_codes=("alioth_review_1",),
            )
        )
        assert recovered_lease is not None
        assert recovered_lease.attempt_no == 2
        assert recovered_lease.recoverable_result_payload == durable_payload
        second_version = await service.transition(
            TransitionInspectionWorkItemCommand(
                work_item_id=published.work_item_id,
                expected_status=InspectionWorkItemStatus.LEASED,
                target_status=InspectionWorkItemStatus.RUNNING,
                row_version=recovered_lease.row_version,
                lease_token=recovered_lease.lease_token,
            )
        )
        second_attempt_id = await service.start_attempt(
            StartInspectionAttemptCommand(
                work_item_id=published.work_item_id,
                consumer_agent_run_id=ids["run"],
                attempt_no=recovered_lease.attempt_no,
                lease_token=recovered_lease.lease_token,
                input_hash=recovered_lease.input_hash,
            )
        )
        await service.transition_attempt(
            TransitionInspectionAttemptCommand(
                attempt_id=second_attempt_id,
                expected_status=InspectionAttemptStatus.RUNNING,
                target_status=InspectionAttemptStatus.JUDGED,
                lease_token=recovered_lease.lease_token,
                result_payload=durable_payload,
            )
        )
        packet_id, execution_id = await _seed_result(connection, ids)
        synced = await service.sync_formal_results(
            work_item_id=published.work_item_id,
            lease_token=recovered_lease.lease_token,
        )
        assert synced.terminal_status == InspectionWorkItemStatus.SUCCEEDED
        assert synced.links[0].observation_packet_id == packet_id
        assert synced.links[0].rule_execution_id == execution_id
        await service.transition_attempt(
            TransitionInspectionAttemptCommand(
                attempt_id=second_attempt_id,
                expected_status=InspectionAttemptStatus.JUDGED,
                target_status=InspectionAttemptStatus.WRITTEN,
                lease_token=recovered_lease.lease_token,
            )
        )
        await service.transition(
            TransitionInspectionWorkItemCommand(
                work_item_id=published.work_item_id,
                expected_status=InspectionWorkItemStatus.RUNNING,
                target_status=InspectionWorkItemStatus.SUCCEEDED,
                row_version=second_version,
                lease_token=recovered_lease.lease_token,
            )
        )
        await service.publish(
            replace(
                _publish_command(ids),
                batch_key="hybrid:visual:snapshot:final-gate-recovery",
            )
        )
        finalized = await service.finalize_task_work(
            task_id=ids["task"],
            error_code="FINAL_GATE",
            error_message="最终门禁",
        )
        assert finalized.failed_work_item_count == 0
        assert finalized.recovered_work_item_count == 1
        assert await service.has_nonterminal_work(task_id=ids["task"]) is False

        missing_rule_id = str(uuid4())
        await connection.execute(
            """
            INSERT INTO qc_rule (
                id, ruleset_id, rule_code, name, category, executor_type,
                severity, status, check_point
            )
            VALUES (
                $1::uuid, $2::uuid, 'alioth_review_missing',
                'Missing Review Rule', 'visual', 'hybrid',
                'medium', 'enabled', '检查缺失结果'
            )
            """,
            missing_rule_id,
            ids["ruleset"],
        )
        await service.publish(
            replace(
                _publish_command(ids),
                batch_key="hybrid:visual:snapshot:missing-result",
                rules=(
                    InspectionWorkItemRule(
                        rule_id=missing_rule_id,
                        rule_code="alioth_review_missing",
                        ordinal=0,
                        contract_hash="contract-missing",
                    ),
                ),
            )
        )
        await service.publish(
            replace(
                _publish_command(ids),
                batch_key="hybrid:visual:active-session:missing-result",
                session_dependency="active_session",
                session_owner_agent_run_id=ids["run"],
                rules=(
                    InspectionWorkItemRule(
                        rule_id=missing_rule_id,
                        rule_code="alioth_review_missing",
                        ordinal=0,
                        contract_hash="contract-missing",
                    ),
                ),
            )
        )
        failed_gate = await service.finalize_task_work(
            task_id=ids["task"],
            error_code="FINAL_GATE",
            error_message="最终门禁缺少结果",
        )
        assert failed_gate.failed_work_item_count == 2
        assert failed_gate.active_session_failed_count == 1
        failed_rows = await connection.fetch(
            """
            SELECT batch_key, status, last_error_code
            FROM qc_inspection_work_item
            WHERE task_id = $1::uuid
              AND batch_key IN (
                  'hybrid:visual:snapshot:missing-result',
                  'hybrid:visual:active-session:missing-result'
              )
            ORDER BY batch_key
            """,
            ids["task"],
        )
        assert {
            row["batch_key"]: (row["status"], row["last_error_code"]) for row in failed_rows
        } == {
            "hybrid:visual:active-session:missing-result": (
                "failed",
                "INSPECTION_ACTIVE_SESSION_LOST",
            ),
            "hybrid:visual:snapshot:missing-result": ("failed", "FINAL_GATE"),
        }
        projection = await PostgresTaskInsightRepository(
            pool=pool
        ).list_inspection_work_item_rows(
            task_id=ids["task"],
            control_mode="enforced",
        )
        assert len(projection["work_items"]) == 4
        assert len(projection["rules"]) == 4
        assert len(projection["attempts"]) == 2
        assert all(
            item["control_mode"] == "enforced"
            for item in projection["work_items"]
        )
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        if missing_rule_id is not None:
            await connection.execute(
                "DELETE FROM qc_rule WHERE id = $1::uuid",
                missing_rule_id,
            )
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute(
            "DELETE FROM qc_ruleset WHERE id = $1::uuid",
            ids["ruleset"],
        )
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def test_review_session_draining_allows_only_publication_consumer_recovery() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in (
            "tenant",
            "user",
            "ruleset",
            "rule",
            "task",
            "run",
            "judge",
            "capture",
        )
    }
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        service = InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository(pool=pool)
        )
        await service.publish(_publish_command(ids))
        await connection.execute(
            """
            INSERT INTO qc_agent_run (
                id, task_id, agent_type, agent_name, status, input_context
            )
            VALUES (
                $1::uuid, $2::uuid, 'rule_judge', 'H5 Publication Judge',
                'running', '{"source":"h5_inspection_publication"}'::jsonb
            )
            """,
            ids["judge"],
            ids["task"],
        )
        await connection.execute(
            """
            UPDATE qc_task
            SET
                status = 'review',
                finalization_state = 'session_draining',
                exploration_draining_at = now(),
                session_draining_at = now()
            WHERE id = $1::uuid
            """,
            ids["task"],
        )

        main_claim = await service.claim(
            ClaimInspectionWorkItemCommand(
                task_id=ids["task"],
                consumer_agent_run_id=ids["run"],
                lease_owner="main-run-must-not-recover",
                lease_seconds=180,
                session_dependency="snapshot",
            )
        )
        publication_claim = await service.claim(
            ClaimInspectionWorkItemCommand(
                task_id=ids["task"],
                consumer_agent_run_id=ids["judge"],
                lease_owner="publication-recovery-worker",
                lease_seconds=180,
                session_dependency="snapshot",
            )
        )

        assert main_claim is None
        assert publication_claim is not None
        assert publication_claim.consumer_agent_run_id == ids["judge"]
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute(
            "DELETE FROM qc_ruleset WHERE id = $1::uuid",
            ids["ruleset"],
        )
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def test_claimable_count_excludes_active_session_owned_by_another_run() -> None:
    """收口等待只能统计指定消费者实际可领取的工作，不能为别人的会话工作空等。"""
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in (
            "tenant",
            "user",
            "ruleset",
            "rule",
            "task",
            "run",
            "judge",
            "capture",
        )
    }
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        service = InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository(pool=pool)
        )
        await service.publish(_publish_command(ids))
        await service.publish(
            replace(
                _publish_command(ids),
                batch_key="hybrid:visual:active-session",
                session_dependency="active_session",
                session_owner_agent_run_id=ids["run"],
            )
        )
        await connection.execute(
            """
            INSERT INTO qc_agent_run (
                id, task_id, agent_type, agent_name, status, input_context
            )
            VALUES (
                $1::uuid, $2::uuid, 'rule_judge', 'H5 Publication Judge',
                'running', '{"source":"h5_inspection_publication"}'::jsonb
            )
            """,
            ids["judge"],
            ids["task"],
        )

        judge_count = await service.count_claimable_pending_work(
            task_id=ids["task"],
            consumer_agent_run_id=ids["judge"],
        )
        main_count = await service.count_claimable_pending_work(
            task_id=ids["task"],
            consumer_agent_run_id=ids["run"],
        )
        judge_waitable_count = await service.count_waitable_nonterminal_work(
            task_id=ids["task"],
            consumer_agent_run_id=ids["judge"],
        )
        main_waitable_count = await service.count_waitable_nonterminal_work(
            task_id=ids["task"],
            consumer_agent_run_id=ids["run"],
        )

        assert judge_count == 1
        assert main_count == 2
        assert judge_waitable_count == 1
        assert main_waitable_count == 2
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute(
            "DELETE FROM qc_ruleset WHERE id = $1::uuid",
            ids["ruleset"],
        )
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def test_terminal_web_main_owner_claims_only_its_active_session_work() -> None:
    """真实 PG 证明 Web main Run 终态后只恢复同 owner/epoch 的活会话工作。"""
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in ("tenant", "user", "ruleset", "rule", "task", "run", "capture")
    }
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        await connection.execute(
            """
            UPDATE qc_task
            SET
                target_platform = 'web',
                task_config = '{
                    "workflow_version":"v2",
                    "execution_mode":"full_inspection",
                    "web_inspection_notification_mode":"enforced"
                }'::jsonb,
                finalization_state = 'exploration_draining',
                exploration_draining_at = now(),
                finalization_agent_run_id = $2::uuid
            WHERE id = $1::uuid
            """,
            ids["task"],
            ids["run"],
        )
        service = InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository(pool=pool)
        )
        await service.publish(
            replace(
                _publish_command(ids),
                batch_key="hybrid:visual:terminal-web-active-session",
                session_dependency="active_session",
                session_owner_agent_run_id=ids["run"],
            )
        )
        await service.publish(
            replace(
                _publish_command(ids),
                batch_key="hybrid:visual:terminal-web-snapshot",
            )
        )
        await connection.execute(
            """
            UPDATE qc_agent_run
            SET status = 'done', finished_at = now()
            WHERE id = $1::uuid
            """,
            ids["run"],
        )

        active_lease = await service.claim(
            ClaimInspectionWorkItemCommand(
                task_id=ids["task"],
                consumer_agent_run_id=ids["run"],
                lease_owner="terminal-web-owner-recovery",
                lease_seconds=180,
                session_dependency="active_session",
                allow_terminal_web_owner_recovery=True,
            )
        )
        assert active_lease is not None
        running_version = await service.transition(
            TransitionInspectionWorkItemCommand(
                work_item_id=active_lease.work_item_id,
                expected_status=InspectionWorkItemStatus.LEASED,
                target_status=InspectionWorkItemStatus.RUNNING,
                row_version=active_lease.row_version,
                lease_token=active_lease.lease_token,
            )
        )
        assert running_version is not None
        renewed_version = await service.renew_lease(
            RenewInspectionWorkItemLeaseCommand(
                work_item_id=active_lease.work_item_id,
                consumer_agent_run_id=ids["run"],
                lease_token=active_lease.lease_token,
                row_version=running_version,
                lease_seconds=180,
                allow_terminal_web_owner_recovery=True,
            )
        )
        ordinary_lease = await service.claim(
            ClaimInspectionWorkItemCommand(
                task_id=ids["task"],
                consumer_agent_run_id=ids["run"],
                lease_owner="terminal-run-must-not-claim-snapshot",
                lease_seconds=180,
                session_dependency="snapshot",
            )
        )

        assert active_lease.producer_agent_run_id == ids["run"]
        assert active_lease.consumer_agent_run_id == ids["run"]
        assert active_lease.session_dependency == "active_session"
        assert active_lease.target_platform == "web"
        assert running_version == active_lease.row_version + 1
        assert renewed_version == running_version + 1
        assert ordinary_lease is None
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute(
            "DELETE FROM qc_ruleset WHERE id = $1::uuid",
            ids["ruleset"],
        )
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


@pytest.mark.parametrize(
    "mismatch",
    ["snapshot", "cross_run", "cross_epoch", "h5", "app"],
)
async def test_terminal_web_owner_recovery_rejects_postgres_fence_mismatches(
    mismatch: str,
) -> None:
    """真实 PG 证明 terminal owner 恢复例外不越过依赖、owner、epoch 或平台。"""
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in ("tenant", "user", "ruleset", "rule", "task", "run", "capture")
    }
    other_run_id = str(uuid4())
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        await connection.execute(
            """
            UPDATE qc_task
            SET
                target_platform = 'web',
                task_config = '{
                    "workflow_version":"v2",
                    "execution_mode":"full_inspection",
                    "web_inspection_notification_mode":"enforced"
                }'::jsonb,
                finalization_state = 'exploration_draining',
                exploration_draining_at = now(),
                finalization_agent_run_id = $2::uuid
            WHERE id = $1::uuid
            """,
            ids["task"],
            ids["run"],
        )
        dependency = "snapshot" if mismatch == "snapshot" else "active_session"
        service = InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository(pool=pool)
        )
        await service.publish(
            replace(
                _publish_command(ids),
                batch_key=f"hybrid:visual:terminal-web-negative:{mismatch}",
                session_dependency=dependency,
                session_owner_agent_run_id=(
                    ids["run"] if dependency == "active_session" else None
                ),
            )
        )
        await connection.execute(
            """
            UPDATE qc_agent_run SET status = 'done', finished_at = now()
            WHERE id = $1::uuid
            """,
            ids["run"],
        )

        if mismatch == "snapshot":
            with pytest.raises(
                InspectionWorkItemValidationError,
                match="终态 Web owner 恢复只允许 active_session 工作",
            ):
                await service.claim(
                    ClaimInspectionWorkItemCommand(
                        task_id=ids["task"],
                        consumer_agent_run_id=ids["run"],
                        lease_owner="terminal-web-negative-snapshot",
                        lease_seconds=180,
                        session_dependency="snapshot",
                        allow_terminal_web_owner_recovery=True,
                    )
                )
            return

        if mismatch == "cross_run":
            await connection.execute(
                """
                INSERT INTO qc_agent_run (
                    id, task_id, agent_type, agent_name, status, finished_at
                )
                VALUES (
                    $1::uuid, $2::uuid, 'main', 'Wrong Recovery Main', 'done', now()
                )
                """,
                other_run_id,
                ids["task"],
            )
            await connection.execute(
                """
                UPDATE qc_task SET finalization_agent_run_id = $2::uuid
                WHERE id = $1::uuid
                """,
                ids["task"],
                other_run_id,
            )
        elif mismatch == "cross_epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch = 1 WHERE id = $1::uuid",
                ids["task"],
            )
        elif mismatch in {"h5", "app"}:
            if mismatch == "app":
                await connection.execute(
                    """
                    UPDATE qc_task
                    SET target_platform = 'app', target_url = NULL,
                        task_config = jsonb_set(
                            task_config, '{app}',
                            '{"platform_build_ref":"integration-app"}'::jsonb,
                            true
                        )
                    WHERE id = $1::uuid
                    """,
                    ids["task"],
                )
            else:
                await connection.execute(
                    "UPDATE qc_task SET target_platform = 'h5' WHERE id = $1::uuid",
                    ids["task"],
                )

        rejected = await service.claim(
            ClaimInspectionWorkItemCommand(
                task_id=ids["task"],
                consumer_agent_run_id=ids["run"],
                lease_owner=f"terminal-web-negative-{mismatch}",
                lease_seconds=180,
                session_dependency="active_session",
                allow_terminal_web_owner_recovery=True,
            )
        )
        assert rejected is None

        await connection.execute(
            """
            UPDATE qc_task
            SET target_platform = 'web', target_url = 'https://example.com',
                finalization_epoch = 0,
                finalization_agent_run_id = $2::uuid
            WHERE id = $1::uuid
            """,
            ids["task"],
            ids["run"],
        )
        lease = await service.claim(
            ClaimInspectionWorkItemCommand(
                task_id=ids["task"],
                consumer_agent_run_id=ids["run"],
                lease_owner=f"terminal-web-negative-{mismatch}",
                lease_seconds=180,
                session_dependency="active_session",
                allow_terminal_web_owner_recovery=True,
            )
        )
        assert lease is not None
        running_version = await service.transition(
            TransitionInspectionWorkItemCommand(
                work_item_id=lease.work_item_id,
                expected_status=InspectionWorkItemStatus.LEASED,
                target_status=InspectionWorkItemStatus.RUNNING,
                row_version=lease.row_version,
                lease_token=lease.lease_token,
            )
        )

        if mismatch == "cross_run":
            await connection.execute(
                """
                UPDATE qc_task SET finalization_agent_run_id = $2::uuid
                WHERE id = $1::uuid
                """,
                ids["task"],
                other_run_id,
            )
        elif mismatch == "cross_epoch":
            await connection.execute(
                """
                UPDATE qc_task SET finalization_epoch = 1
                WHERE id = $1::uuid
                """,
                ids["task"],
            )
        elif mismatch in {"h5", "app"}:
            if mismatch == "app":
                await connection.execute(
                    """
                    UPDATE qc_task
                    SET
                        target_platform = 'app',
                        target_url = NULL,
                        task_config = jsonb_set(
                            task_config,
                            '{app}',
                            '{"platform_build_ref":"integration-app"}'::jsonb,
                            true
                        )
                    WHERE id = $1::uuid
                    """,
                    ids["task"],
                )
            else:
                await connection.execute(
                    """
                    UPDATE qc_task SET target_platform = 'h5'
                    WHERE id = $1::uuid
                    """,
                    ids["task"],
                )

        with pytest.raises(InspectionWorkItemConflictError):
            await service.renew_lease(
                RenewInspectionWorkItemLeaseCommand(
                    work_item_id=lease.work_item_id,
                    consumer_agent_run_id=ids["run"],
                    lease_token=lease.lease_token,
                    row_version=running_version,
                    lease_seconds=180,
                    allow_terminal_web_owner_recovery=True,
                )
            )
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute(
            "DELETE FROM qc_ruleset WHERE id = $1::uuid",
            ids["ruleset"],
        )
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute(
            "DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"]
        )
        await pool.close()
        await connection.close()


@pytest.mark.parametrize("persisted_inspection_status", ["evaluating", "completed"])
async def test_web_retry_uses_real_postgres_gate_and_report_binding(
    monkeypatch: pytest.MonkeyPatch,
    persisted_inspection_status: str,
) -> None:
    """真实补偿链必须补发 active-session Work、落 Gap 并原子绑定报告。"""
    from services.api.app.infrastructure.db.web_capture_identity import (
        PostgresWebCaptureIdentityReader,
    )
    from services.api.tests.web_retry_identity_support import (
        freeze_before_retry_material,
        record_retry_stable_identity,
    )
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in (
            "tenant",
            "user",
            "ruleset",
            "rule",
            "gap_rule",
            "task",
            "run",
            "capture",
            "before_capture",
        )
    }
    connection = await asyncpg.connect(database_url)
    # 该 Web 夹具复用同一事务连接，隔离级别须与正式只读报告投影一致。
    transaction = connection.transaction(isolation="repeatable_read")
    await transaction.start()

    class TransactionScopedPool:
        def acquire(self):
            class Acquire:
                async def __aenter__(self):
                    return connection

                async def __aexit__(self, *_args):
                    return None

            return Acquire()

    class ReportGenerator:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_task_report(self, task_id: str, **kwargs: object):
            assert task_id == ids["task"]
            assert kwargs["agent_run_id"] == ids["run"]
            self.calls += 1
            return {
                "report_version": "v1",
                "agent_run_id": ids["run"],
                "task": {"task_id": ids["task"], "status": "done"},
                "summary": {"overall_status": "unknown", "unknown": 1},
                "page_exploration": kwargs["page_exploration"],
            }

    class StaticSnapshotProvider:
        async def get_task_snapshot(self, task_id: str) -> TaskRuntimeSnapshot:
            return TaskRuntimeSnapshot(
                task_id=task_id,
                status="running",
                ruleset_version=1,
                ruleset_id=ids["ruleset"],
                ruleset_snapshot_hash="snapshot-1",
            )

    class NoopAnnotator:
        async def annotate_inspection(self, **_kwargs: object) -> None:
            return None

    class DeterministicActiveConsumer:
        def __init__(self, service: InspectionWorkItemService) -> None:
            self._service = service
            self.completed_count = 0

        async def drain_available(self, **kwargs: object) -> None:
            assert kwargs["allow_terminal_web_owner_recovery"] is True
            while True:
                lease = await self._service.claim(
                    ClaimInspectionWorkItemCommand(
                        task_id=ids["task"],
                        consumer_agent_run_id=ids["run"],
                        lease_owner="web-active-integration",
                        lease_seconds=180,
                        session_dependency="active_session",
                        allow_terminal_web_owner_recovery=True,
                    )
                )
                if lease is None:
                    return
                row_version = await self._service.transition(
                    TransitionInspectionWorkItemCommand(
                        work_item_id=lease.work_item_id,
                        expected_status=InspectionWorkItemStatus.LEASED,
                        target_status=InspectionWorkItemStatus.RUNNING,
                        row_version=lease.row_version,
                        lease_token=lease.lease_token,
                    )
                )
                row_version = await self._service.renew_lease(
                    RenewInspectionWorkItemLeaseCommand(
                        work_item_id=lease.work_item_id,
                        consumer_agent_run_id=ids["run"],
                        lease_token=lease.lease_token,
                        row_version=row_version,
                        lease_seconds=180,
                        allow_terminal_web_owner_recovery=True,
                    )
                )
                await _complete_claimed_work(
                    connection=connection,
                    service=self._service,
                    ids=ids,
                    lease=lease,
                    row_version=row_version,
                )
                self.completed_count += 1

    pool = TransactionScopedPool()
    try:
        await _seed_task(connection, ids)
        await freeze_before_retry_material(connection, pool, ids)
        locked_config = {
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
            "web_inspection_notification_mode": "enforced",
            "h5_inspection_notification_mode": "enforced",
            "_task_target": {"platform": "web"},
        }
        await connection.execute(
            """
            UPDATE qc_rule
            SET applicable_page_types = '["content_list"]'::jsonb,
                input_contract = '{
                    "required_artifacts":["screenshot","action_trace"]
                }'::jsonb
            WHERE id = $1::uuid
            """,
            ids["rule"],
        )
        await connection.execute(
            """
            INSERT INTO qc_rule (
                id, ruleset_id, rule_code, name, category, executor_type,
                severity, status, check_point, applicable_page_types,
                input_contract
            )
            VALUES (
                $1::uuid, $2::uuid, 'alioth_web_gap', 'Web Gap Rule',
                'visual', 'hybrid', 'medium', 'enabled', '检查缺失详情页',
                '["content_detail"]'::jsonb,
                '{
                    "required_artifacts":["screenshot"],
                    "target_clients":["WEB"]
                }'::jsonb
            )
            """,
            ids["gap_rule"],
            ids["ruleset"],
        )
        await connection.execute(
            "UPDATE qc_ruleset SET rule_count = 2 WHERE id = $1::uuid",
            ids["ruleset"],
        )
        work_items = InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository(pool=pool)
        )
        await connection.execute(
            """
            UPDATE qc_task
            SET
                target_platform = 'web',
                task_config = $2::jsonb,
                finalization_state = 'exploration_draining',
                exploration_draining_at = now(),
                finalization_agent_run_id = $3::uuid
            WHERE id = $1::uuid
            """,
            ids["task"],
            json.dumps(locked_config),
            ids["run"],
        )
        await connection.execute(
            """
            UPDATE qc_agent_run
            SET
                status = 'done',
                input_context = $2::jsonb,
                output_context = '{}'::jsonb,
                stop_reason = 'completed',
                error_message = NULL,
                finished_at = now()
            WHERE id = $1::uuid
            """,
            ids["run"],
            json.dumps(
                {
                    "message": "recover Web formal finalization",
                    "task_config": locked_config,
                    "page_sampling_policy": {},
                }
            ),
        )
        session_id = f"web:{ids['run']}"
        await connection.execute(
            """
            INSERT INTO qc_surface_session (
                session_id, task_id, surface_kind, session_owner, status,
                finalization_epoch, activated_at
            )
            VALUES (
                $1, $2::uuid, 'web', 'web-v2-workflow', 'active',
                0, now()
            )
            """,
            session_id,
            ids["task"],
        )
        await connection.execute(
            """
            INSERT INTO qc_action_execution (
                action_execution_id, task_id, session_id, intent_id,
                idempotency_key, intent_hash, intent_protocol_version,
                intent_payload, command_sequence, finalization_epoch,
                requested_source_capture_id, source_capture_id, result_capture_id,
                status, result_payload, started_at, action_finished_at,
                evidence_finished_at
            ) VALUES (
                'web-action-integration', $1::uuid, $2, 'web-intent-integration',
                'web-intent-integration', $3, 'web-v2-workflow/v1',
                '{}'::jsonb, 1, 0,
                $4::uuid, $4::uuid, $5::uuid,
                'completed', '{}'::jsonb, now(), now(), now()
            )
            """,
            ids["task"],
            session_id,
            "d" * 64,
            ids["before_capture"],
            ids["capture"],
        )
        await connection.execute(
            """
            INSERT INTO qc_page_capture (
                id, task_id, agent_run_id, parent_page_capture_id,
                page_seq, page_slug, url, page_type, page_state,
                capture_status, captured_at, detection_status,
                inspection_status, surface_session_id, capture_role,
                action_execution_id, session_command_sequence
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, NULL,
                2, 'home-before', 'https://example.com', 'home', 'stable',
                'captured', now(), 'recognized',
                'completed', $4, 'before_action',
                'web-action-integration', 1
            )
            """,
            ids["before_capture"],
            ids["task"],
            ids["run"],
            session_id,
        )
        await connection.execute(
            """
            UPDATE qc_page_capture
            SET parent_page_capture_id = $2::uuid,
                page_type = 'content_list', page_state = 'stable',
                detection_status = 'recognized',
                inspection_status = $4,
                surface_session_id = $3,
                capture_role = 'after_action',
                action_execution_id = 'web-action-integration',
                session_command_sequence = 1,
                captured_at = now()
            WHERE id = $1::uuid
            """,
            ids["capture"],
            ids["before_capture"],
            session_id,
            persisted_inspection_status,
        )
        await connection.execute(
            """
            INSERT INTO qc_h5_page_type_assessment (
                assessment_record_id, assessment_id, task_id, agent_run_id,
                session_id, page_capture_id, detection_status, page_type,
                page_state, status, assessment_payload, schema_version,
                payload_hash
            ) VALUES (
                'web-assessment-integration', 'web-assessment-integration',
                $1::uuid, $2::uuid, $3, $4::uuid, 'recognized', 'content_list',
                'stable', 'covered', '{}'::jsonb,
                'web-page-type-assessment.v2', $5
            )
            """,
            ids["task"],
            ids["run"],
            session_id,
            ids["capture"],
            "e" * 64,
        )
        for capture_id, artifact_types in (
            (ids["before_capture"], ("screenshot", "dom")),
            (ids["capture"], ("screenshot", "dom", "visible_text")),
        ):
            for artifact_type in artifact_types:
                await connection.execute(
                    """
                    INSERT INTO qc_evidence_artifact (
                        task_id, page_capture_id, artifact_type, source,
                        storage_path, content_type, checksum, size_bytes
                    ) VALUES (
                        $1::uuid, $2::uuid, $3, 'baseline', $4,
                        $5, $6, 1
                    )
                    """,
                    ids["task"],
                    capture_id,
                    artifact_type,
                    f"tests/{capture_id}/{artifact_type}",
                    "image/png" if artifact_type == "screenshot" else "text/plain",
                    f"sha256:{'f' * 64}",
                )
            await connection.execute(
                """
                INSERT INTO qc_evidence_manifest (
                    manifest_id, manifest_version, task_id, session_id,
                    capture_id, protocol_version, manifest_hash, payload_hash,
                    quality_status, manifest_payload, created_at
                ) VALUES (
                    $1, 1, $2::uuid, $3, $4::uuid,
                    'evidence-manifest.v1', $5, $6, 'pass', '{}'::jsonb, now()
                )
                """,
                f"web-manifest:{capture_id}",
                ids["task"],
                session_id,
                capture_id,
                f"sha256:{'1' * 64}",
                f"sha256:{'2' * 64}",
            )
        await connection.execute(
            """
            INSERT INTO qc_web_exploration_budget (
                agent_run_id, site_id, task_id, session_id,
                target_origin_sha256, catalog_version, catalog_sha256,
                state, sealed_at
            )
            VALUES (
                $1::uuid, 'integration-site', $2::uuid, $3,
                $4, 'integration-v1', $5, 'sealed', now()
            )
            """,
            ids["run"],
            ids["task"],
            session_id,
            "a" * 64,
            "b" * 64,
        )
        await connection.execute(
            """
            INSERT INTO qc_v2_work_item (
                work_id, work_type, task_id, session_id, branch_id,
                source_capture_id, protocol_version, payload_hash,
                work_payload, status, deadline, finished_at
            )
            VALUES (
                $1, 'exploration', $2::uuid, $3, 'integration-branch',
                $4::uuid, 'v1', $5, '{}'::jsonb,
                'succeeded', now() + interval '1 minute', now()
            )
            """,
            f"web-recovery-exploration:{ids['run']}",
            ids["task"],
            session_id,
            ids["capture"],
            "c" * 64,
        )
        await connection.execute(
            """
            UPDATE qc_outbox_event
            SET status = 'published', published_at = now(), updated_at = now()
            WHERE task_id = $1::uuid
              AND status IN ('pending', 'retry_wait')
            """,
            ids["task"],
        )

        await record_retry_stable_identity(connection, pool, ids, session_id)
        state_store = PostgresAgentRunStateStore(pool=pool)
        finalization = TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=pool)
        )
        insights = PostgresTaskInsightRepository(pool=pool)
        monkeypatch.setattr(
            worker_module,
            "PostgresTaskInsightRepository",
            lambda: insights,
        )
        async def _pool(*_args: object):
            return pool

        monkeypatch.setattr(worker_module, "get_postgres_pool", _pool)
        queue = RedisAgentRunQueue(
            fakeredis.aioredis.FakeRedis(decode_responses=True),
            ttl_seconds=3600,
            queue_key=f"test:web-pg-retry:{ids['task']}",
        )
        generator = ReportGenerator()
        snapshot_repository = PostgresWebCoverageSnapshotRepository(pool=pool)
        capture_publisher = AppCaptureInspectionPublisher(
            task_query=TaskQueryService(repository=PostgresTaskRepository(pool=pool)),
            snapshot_provider=StaticSnapshotProvider(),
            work_item_service=work_items,
            annotator=NoopAnnotator(),
        )
        active_publisher = H5LiveInspectionPublishService(
            gate_reader=PostgresH5LiveInspectionTaskGateReader(pool=pool),
            capture_reader=PostgresH5EvidenceCaptureReader(pool=pool),
            publisher=capture_publisher,
            web_identity_reader=PostgresWebCaptureIdentityReader(pool=pool),
        )
        active_consumer = DeterministicActiveConsumer(work_items)
        active_drainer = worker_module._WebActiveSessionInspectionDrainer(
            publisher=active_publisher,
            consumer=active_consumer,
        )
        publication_repository = PostgresH5InspectionPublicationRepository(pool=pool)
        final_handoff = WebPageInspectionNotification(
            reader=PostgresWebCaptureEvidenceRepository(pool=pool),
            inventory=PostgresH5EvidenceInventoryRepository(pool=pool),
            publication_writer=publication_repository,
        )
        reconciliation = WebInspectionReconciliation(
            state_reader=snapshot_repository,
            manifest_finalizer=None,
            publication_publisher=None,
            active_session_drainer=active_drainer,
            finalization=finalization,
            snapshot_repository=snapshot_repository,
            gap_rule_reader=snapshot_repository,
            rule_execution_service=RuleExecutionService(
                repository=PostgresTaskRepository(pool=pool)
            ),
            final_handoff=final_handoff,
        )

        first_result = await reconciliation.reconcile(
            ReconcileWebInspectionCommand(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                finalization_epoch=0,
            )
        )
        draining_state = await connection.fetchrow(
            """
            SELECT finalization_state, completion_gate_snapshot_id
            FROM qc_task WHERE id = $1::uuid
            """,
            ids["task"],
        )
        assert first_result.status == "waiting_for_run_terminal"
        assert draining_state["finalization_state"] == "session_draining"
        assert draining_state["completion_gate_snapshot_id"] is None
        assert active_consumer.completed_count == 1

        publication = await connection.fetchrow(
            """
            SELECT id::text AS publication_id, row_version
            FROM qc_h5_inspection_publication
            WHERE task_id = $1::uuid AND finalization_epoch = 0
            """,
            ids["task"],
        )
        assert publication is not None
        publication_handler = H5InspectionPublicationWakeupHandler(
            repository=publication_repository,
            publisher=capture_publisher,
            lease_owner="web-publication-integration",
            lease_seconds=180,
        )
        assert await publication_handler.handle(
            RedisStreamWakeup(
                stream_name="alioth:v2:wakeup:h5-inspection-publication",
                stream_entry_id="1-0",
                message_id="web-publication-integration",
                message_type=H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
                task_id=ids["task"],
                aggregate_type="h5_inspection_publication",
                aggregate_id=publication["publication_id"],
                aggregate_version=int(publication["row_version"]),
                idempotency_key="web-publication-integration",
            )
        )

        await connection.execute(
            """
            UPDATE qc_surface_session
            SET status = 'closed', draining_at = now(), closed_at = now()
            WHERE task_id = $1::uuid AND session_id = $2
            """,
            ids["task"],
            session_id,
        )
        await connection.execute(
            """
            UPDATE qc_outbox_event
            SET status = 'published', published_at = now(), updated_at = now()
            WHERE task_id = $1::uuid
              AND status IN ('pending', 'retry_wait')
            """,
            ids["task"],
        )
        publication_consumer_run_id = await connection.fetchval(
            """
            SELECT id::text
            FROM qc_agent_run
            WHERE task_id = $1::uuid
              AND agent_type = 'rule_judge'
              AND input_context->>'source' = 'h5_inspection_publication'
            """,
            ids["task"],
        )
        assert publication_consumer_run_id is not None
        publication_lease = await work_items.claim(
            ClaimInspectionWorkItemCommand(
                task_id=ids["task"],
                consumer_agent_run_id=str(publication_consumer_run_id),
                lease_owner="web-publication-integration",
                lease_seconds=180,
                session_dependency="snapshot",
            )
        )
        if publication_lease is not None:
            publication_version = await work_items.transition(
                TransitionInspectionWorkItemCommand(
                    work_item_id=publication_lease.work_item_id,
                    expected_status=InspectionWorkItemStatus.LEASED,
                    target_status=InspectionWorkItemStatus.RUNNING,
                    row_version=publication_lease.row_version,
                    lease_token=publication_lease.lease_token,
                )
            )
            await _complete_claimed_work(
                connection=connection,
                service=work_items,
                ids=ids,
                lease=publication_lease,
                row_version=publication_version,
                consumer_agent_run_id=str(publication_consumer_run_id),
            )
        assert await work_items.finish_h5_consumer_run_if_idle(
            task_id=ids["task"],
            consumer_agent_run_id=str(publication_consumer_run_id),
        )
        await connection.execute(
            """
            UPDATE qc_task SET finalization_retry_at = NULL
            WHERE id = $1::uuid
            """,
            ids["task"],
        )
        second_retry = await worker_module._retry_pending_task_finalizations(
            queue,
            state_store,
            finalization,
            final_report_generator=generator,
            web_inspection_reconciliation=reconciliation,
        )
        replayed = await worker_module._retry_pending_task_finalizations(
            queue,
            state_store,
            finalization,
            final_report_generator=generator,
            web_inspection_reconciliation=reconciliation,
        )

        sealed = await connection.fetchrow(
            """
            SELECT finalization_state, completion_gate_snapshot_id,
                   finalization_epoch, execution_status
            FROM qc_task WHERE id = $1::uuid
            """,
            ids["task"],
        )
        persisted = await connection.fetchrow(
            """
            SELECT count(*)::integer AS binding_count,
                   count(payload.*)::integer AS payload_count
            FROM qc_report_gate_binding AS binding
            LEFT JOIN qc_report_payload_snapshot AS payload
              ON payload.report_id = binding.report_id
             AND payload.report_version = binding.report_version
            WHERE binding.task_id = $1::uuid
              AND binding.gate_snapshot_id = $2
              AND binding.finalization_epoch = $3
            """,
            ids["task"],
            sealed["completion_gate_snapshot_id"],
            sealed["finalization_epoch"],
        )
        gate = await connection.fetchrow(
            """
            SELECT gate_passed, block_reasons
            FROM qc_completion_gate_snapshot
            WHERE gate_snapshot_id = $1
            """,
            sealed["completion_gate_snapshot_id"],
        )
        gap = await connection.fetchrow(
            """
            SELECT page_capture_id, source_idempotency_key, judge_input
            FROM qc_rule_execution
            WHERE task_id = $1::uuid AND rule_id = $2::uuid
            """,
            ids["task"],
            ids["gap_rule"],
        )
        assert second_retry == 1, (dict(sealed), dict(gate or {}))
        assert replayed == 0
        assert sealed["finalization_state"] == "sealed", dict(gate or {})
        assert sealed["completion_gate_snapshot_id"] is not None
        assert sealed["execution_status"] == "completed_with_gaps"
        assert dict(persisted) == {"binding_count": 1, "payload_count": 1}
        assert generator.calls == 1
        assert gap["page_capture_id"] is None
        assert str(gap["source_idempotency_key"]).startswith("web-coverage-gap:")
        gap_input = json.loads(gap["judge_input"])
        assert gap_input == {
            "schema_version": "web-coverage-gap.v1",
            "source": "web_coverage_gap",
            "coverage_snapshot_id": gap_input["coverage_snapshot_id"],
            "missing_page_type": "content_detail",
            "reason": "required_page_type_not_discovered",
        }
        assert (
            await connection.fetchval(
                """
                SELECT count(*) FROM qc_rule_execution
                WHERE task_id = $1::uuid AND rule_id = $2::uuid
                """,
                ids["task"],
                ids["gap_rule"],
            )
            == 1
        )
        assert (
            await connection.fetchval(
                """
                SELECT count(*) FROM qc_web_coverage_snapshot
                WHERE task_id = $1::uuid AND agent_run_id = $2::uuid
                """,
                ids["task"],
                ids["run"],
            )
            == 1
        )
        assert (
            await connection.fetchval(
                """
                SELECT count(*)
                FROM qc_h5_inspection_publication
                WHERE task_id = $1::uuid AND finalization_epoch = 0
                """,
                ids["task"],
            )
            == 1
        )
        assert (
            await connection.fetchval(
                """
                SELECT count(*)
                FROM qc_inspection_work_item
                WHERE task_id = $1::uuid
                  AND producer_agent_run_id = $2::uuid
                  AND session_dependency = 'active_session'
                  AND status = 'succeeded'
                """,
                ids["task"],
                ids["run"],
            )
            == 1
        )
        visible_report = await PostgresTaskRepository(pool=pool).get_final_report(
            ids["task"],
            agent_run_id=ids["run"],
        )
        assert visible_report is not None
        assert visible_report["agent_run_id"] == ids["run"]
        assert visible_report["completion_gate"]["gate_passed"] is True
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.parametrize("target_platform", ["h5", "app"])
async def test_historical_non_web_run_report_remains_visible_on_postgres(
    target_platform: str,
) -> None:
    """真实 PG 证明指定历史 H5/App Run 仍读取其原始报告。"""

    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in ("tenant", "user", "ruleset", "rule", "task", "run", "capture")
    }
    current_run_id = str(uuid4())
    connection = await asyncpg.connect(database_url)
    transaction = connection.transaction()
    await transaction.start()

    class TransactionScopedPool:
        def acquire(self):
            class Acquire:
                async def __aenter__(self):
                    return connection

                async def __aexit__(self, *_args):
                    return None

            return Acquire()

    try:
        await _seed_task(connection, ids)
        historical_report = {
            "report_version": "v1",
            "agent_run_id": ids["run"],
            "task": {"task_id": ids["task"], "status": "done"},
            "summary": {"overall_status": "pass", "pass": 1},
        }
        await connection.execute(
            """
            UPDATE qc_agent_run
            SET status = 'done', output_context = $2::jsonb, finished_at = now()
            WHERE id = $1::uuid
            """,
            ids["run"],
            json.dumps({"final_report": historical_report}),
        )
        await connection.execute(
            """
            INSERT INTO qc_agent_run (
                id, task_id, agent_type, agent_name, status
            )
            VALUES ($1::uuid, $2::uuid, 'main', 'Current Main', 'running')
            """,
            current_run_id,
            ids["task"],
        )
        if target_platform == "app":
            await connection.execute(
                """
                UPDATE qc_task
                SET target_platform = 'app', target_url = NULL,
                    task_config = '{
                        "workflow_version":"v2",
                        "execution_mode":"full_inspection",
                        "app":{"platform_build_ref":"integration-app"}
                    }'::jsonb,
                    finalization_agent_run_id = $2::uuid
                WHERE id = $1::uuid
                """,
                ids["task"],
                current_run_id,
            )
        else:
            await connection.execute(
                """
                UPDATE qc_task
                SET target_platform = 'h5',
                    task_config = '{
                        "workflow_version":"v2",
                        "execution_mode":"full_inspection"
                    }'::jsonb,
                    finalization_agent_run_id = $2::uuid
                WHERE id = $1::uuid
                """,
                ids["task"],
                current_run_id,
            )

        report = await PostgresTaskRepository(
            pool=TransactionScopedPool()
        ).get_final_report(ids["task"], agent_run_id=ids["run"])

        assert report == historical_report
    finally:
        await transaction.rollback()
        await connection.close()


async def test_changed_claim_renew_and_report_queries_prepare_on_postgres() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    class CaptureTransaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class CaptureConnection:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def transaction(self):
            return CaptureTransaction()

        async def fetchrow(self, query: str, *_args):
            self.queries.append(query)
            return None

        async def fetchval(self, query: str, *_args):
            self.queries.append(query)
            return None

    class CapturePool:
        def __init__(self, capture: CaptureConnection) -> None:
            self.capture = capture

        def acquire(self):
            capture = self.capture

            class Acquire:
                async def __aenter__(self):
                    return capture

                async def __aexit__(self, *_args):
                    return None

            return Acquire()

    capture = CaptureConnection()
    pool = CapturePool(capture)
    inspection = PostgresInspectionWorkItemRepository(pool=pool)
    await inspection.claim(
        command=ClaimInspectionWorkItemCommand(
            task_id="00000000-0000-0000-0000-000000000001",
            consumer_agent_run_id="00000000-0000-0000-0000-000000000002",
            lease_owner="prepare-check",
            lease_seconds=180,
            session_dependency="active_session",
            allow_terminal_web_owner_recovery=True,
        ),
        lease_token="00000000-0000-0000-0000-000000000003",
    )
    await inspection.renew_lease(
        command=RenewInspectionWorkItemLeaseCommand(
            work_item_id="00000000-0000-0000-0000-000000000004",
            consumer_agent_run_id="00000000-0000-0000-0000-000000000002",
            lease_token="00000000-0000-0000-0000-000000000003",
            row_version=2,
            lease_seconds=180,
            allow_terminal_web_owner_recovery=True,
        )
    )
    await PostgresTaskRepository(pool=pool).get_final_report(
        "00000000-0000-0000-0000-000000000001",
        agent_run_id="00000000-0000-0000-0000-000000000002",
    )

    connection = await asyncpg.connect(database_url)
    try:
        for query in capture.queries:
            await connection.prepare(query)
    finally:
        await connection.close()

    assert len(capture.queries) == 3


def _publish_command(ids: dict[str, str]) -> PublishInspectionWorkItemCommand:
    return PublishInspectionWorkItemCommand(
        task_id=ids["task"],
        page_capture_id=ids["capture"],
        producer_agent_run_id=ids["run"],
        ruleset_id=ids["ruleset"],
        ruleset_version=1,
        ruleset_snapshot_hash="snapshot-1",
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        rules=(
            InspectionWorkItemRule(
                rule_id=ids["rule"],
                rule_code="alioth_review_1",
                ordinal=0,
                contract_hash="contract-1",
            ),
        ),
    )


async def test_web_finalization_recovery_fence_rejects_stale_worker_after_reclaim() -> None:
    """真实 PG 中新 claim 提升 fence 后，旧 worker 的错误和状态写都必须失效。"""

    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    ids = {
        name: str(uuid4())
        for name in ("tenant", "user", "ruleset", "rule", "task", "run", "capture")
    }
    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=4)
    try:
        async with pool.acquire() as connection:
            await _seed_task(connection, ids)
            config = {
                "workflow_version": "v2",
                "execution_mode": "full_inspection",
                "web_inspection_notification_mode": "enforced",
                "_task_target": {"platform": "web"},
            }
            await connection.execute(
                """
                UPDATE qc_task
                SET target_platform = 'web', task_config = $2::jsonb,
                    finalization_agent_run_id = $3::uuid
                WHERE id = $1::uuid
                """,
                ids["task"],
                json.dumps(config),
                ids["run"],
            )
            await connection.execute(
                """
                UPDATE qc_agent_run
                SET status = 'done', stop_reason = 'completed', finished_at = now()
                WHERE id = $1::uuid
                """,
                ids["run"],
            )
        repository = PostgresTaskFinalizationRepository(pool=pool)
        concurrent_claims = await asyncio.gather(
            repository.claim_recoverable_finalizations(
                limit=100, lease_owner="worker-old", lease_seconds=30
            ),
            repository.claim_recoverable_finalizations(
                limit=100, lease_owner="worker-racer", lease_seconds=30
            ),
        )
        winners = [
            lease
            for leases in concurrent_claims
            for lease in leases
            if lease.task_id == ids["task"]
        ]
        assert len(winners) == 1
        first = winners[0]
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE qc_task
                SET finalization_recovery_lease_expires_at = now() - interval '1 second',
                    finalization_retry_at = now() - interval '1 second'
                WHERE id = $1::uuid
                """,
                ids["task"],
            )
        second = next(
            lease
            for lease in await repository.claim_recoverable_finalizations(
                limit=100, lease_owner="worker-new", lease_seconds=30
            )
            if lease.task_id == ids["task"]
        )
        assert second.recovery_generation == first.recovery_generation + 1
        assert second.snapshot.row_version == first.snapshot.row_version + 1

        await repository.record_finalization_retry_error(
            task_id=ids["task"],
            error_message="stale worker must not write",
            lease_token=first.lease_token,
            recovery_generation=first.recovery_generation,
            expected_status=first.expected_status,
            lease_expires_at=first.lease_expires_at,
        )
        with pytest.raises(TaskFinalizationPersistenceError, match="fencing"):
            await repository.begin_exploration_draining(
                command=BeginExplorationDrainingCommand(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    expected_row_version=first.snapshot.row_version,
                    recovery_lease_token=first.lease_token,
                    recovery_generation=first.recovery_generation,
                    recovery_lease_expires_at=first.lease_expires_at,
                )
            )
        stale_fence = {
            "recovery_lease_token": first.lease_token,
            "recovery_generation": first.recovery_generation,
            "recovery_lease_expires_at": first.lease_expires_at,
        }
        coverage_repository = PostgresWebCoverageSnapshotRepository(pool=pool)
        with pytest.raises(FinalizationRecoveryFenceError, match="fencing"):
            await coverage_repository.seal_completed_budget(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                finalization_epoch=first.snapshot.finalization_epoch,
                **stale_fence,
            )
        with pytest.raises(FinalizationRecoveryFenceError, match="fencing"):
            await coverage_repository.create(
                WebCoverageSnapshot.create(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    finalization_epoch=first.snapshot.finalization_epoch,
                    required_page_types=(
                        "content_detail",
                        "content_list",
                        "video_detail",
                        "video_list",
                    ),
                    observed_page_types=(),
                    unrecognized_page_types=(),
                ),
                **stale_fence,
            )
        with pytest.raises(FinalizationRecoveryFenceError, match="fencing"):
            await coverage_repository.record_auth_not_applicable(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                finalization_epoch=first.snapshot.finalization_epoch,
                **stale_fence,
            )
        with pytest.raises(FinalizationRecoveryFenceError, match="fencing"):
            await PostgresActionExecutionRepository(pool=pool).fail_nonterminal_for_finalization(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                finalization_epoch=first.snapshot.finalization_epoch,
                error_code="STALE_WORKER",
                error_message="旧 worker 不得修改动作账本",
                **stale_fence,
            )
        with pytest.raises(FinalizationRecoveryFenceError, match="fencing"):
            await PostgresWebFormalTerminalLedgerRepository(pool=pool).terminalize(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                outcome="failed",
                reason_code="STALE_WORKER",
                reason_message="旧 worker 不得修改正式账本",
                **stale_fence,
            )
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT finalization_state, finalization_last_error,
                       finalization_recovery_generation,
                       finalization_recovery_lease_token::text AS lease_token
                FROM qc_task WHERE id = $1::uuid
                """,
                ids["task"],
            )
        assert row["finalization_state"] == "open"
        assert row["finalization_last_error"] is None
        assert row["finalization_recovery_generation"] == second.recovery_generation
        assert row["lease_token"] == second.lease_token
    finally:
        async with pool.acquire() as connection:
            await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
            await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
            await connection.execute(
                "DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"]
            )
            await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
            await connection.execute(
                "DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"]
            )
        await pool.close()


async def _seed_task(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $2, 'Inspection Integration')
        """,
        ids["tenant"],
        f"inspection-integration-{ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (
            id, username, full_name, password_hash, role, tenant_id
        )
        VALUES (
            $1::uuid, $2, 'Inspection Integration', 'not-a-login-secret',
            '平台管理员', $3::uuid
        )
        """,
        ids["user"],
        f"inspection-integration-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash,
            rule_count, source_type
        )
        VALUES (
            $1::uuid, $2, 'Review', 1, 'enabled',
            'snapshot-1', 1, 'manual'
        )
        """,
        ids["ruleset"],
        f"review-ruleset-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_rule (
            id, ruleset_id, rule_code, name, category, executor_type,
            severity, status, check_point
        )
        VALUES (
            $1::uuid, $2::uuid, 'alioth_review_1', 'Review Rule',
            'visual', 'hybrid', 'medium', 'enabled', '检查页面'
        )
        """,
        ids["rule"],
        ids["ruleset"],
    )
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, submitter_id, tenant_id
        )
        VALUES (
            $1::uuid, 'https://example.com', $2::uuid, 'running',
            $3::uuid, $4::uuid
        )
        """,
        ids["task"],
        ids["ruleset"],
        ids["user"],
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (
            id, task_id, agent_type, agent_name, status
        )
        VALUES ($1::uuid, $2::uuid, 'main', 'Review Main', 'running')
        """,
        ids["run"],
        ids["task"],
    )
    await connection.execute(
        """
        INSERT INTO qc_page_capture (
            id, task_id, agent_run_id, page_seq, page_slug,
            url, page_type, capture_status
        )
        VALUES (
            $1::uuid, $2::uuid, $3::uuid, 1, 'home',
            'https://example.com', 'home', 'captured'
        )
        """,
        ids["capture"],
        ids["task"],
        ids["run"],
    )


async def _seed_result(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    agent_run_id: str | None = None,
) -> tuple[str, str]:
    result_run_id = agent_run_id or ids["run"]
    packet_id = str(
        await connection.fetchval(
            """
            INSERT INTO qc_observation_packet (
                task_id, page_capture_id, rule_id, quality_gate_status,
                created_by_agent_run_id
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, 'pass', $4::uuid)
            RETURNING id
            """,
            ids["task"],
            ids["capture"],
            ids["rule"],
            result_run_id,
        )
    )
    execution_id = str(
        await connection.fetchval(
            """
            INSERT INTO qc_rule_execution (
                task_id, rule_id, page_capture_id, observation_packet_id,
                agent_run_id, execution_status, judge_strategy
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                $5::uuid, 'pass', 'hybrid'
            )
            RETURNING id
            """,
            ids["task"],
            ids["rule"],
            ids["capture"],
            packet_id,
            result_run_id,
        )
    )
    return packet_id, execution_id


async def _complete_claimed_work(
    *,
    connection: asyncpg.Connection,
    service: InspectionWorkItemService,
    ids: dict[str, str],
    lease: InspectionWorkItemLease,
    row_version: int,
    consumer_agent_run_id: str | None = None,
) -> None:
    """用确定性持久化结果把已续租工作推进到 succeeded。"""

    consumer_run_id = consumer_agent_run_id or ids["run"]
    attempt_id = await service.start_attempt(
        StartInspectionAttemptCommand(
            work_item_id=lease.work_item_id,
            consumer_agent_run_id=consumer_run_id,
            attempt_no=lease.attempt_no,
            lease_token=lease.lease_token,
            input_hash=lease.input_hash,
        )
    )
    packet_id, execution_id = await _seed_result(
        connection,
        ids,
        agent_run_id=consumer_run_id,
    )
    await service.transition_rule(
        TransitionInspectionWorkItemRuleCommand(
            work_item_id=lease.work_item_id,
            rule_id=ids["rule"],
            expected_status=InspectionWorkItemRuleStatus.PENDING,
            target_status=InspectionWorkItemRuleStatus.JUDGED,
            lease_token=lease.lease_token,
            observation_packet_id=packet_id,
        )
    )
    await service.transition_attempt(
        TransitionInspectionAttemptCommand(
            attempt_id=attempt_id,
            expected_status=InspectionAttemptStatus.RUNNING,
            target_status=InspectionAttemptStatus.JUDGED,
            lease_token=lease.lease_token,
            result_payload={
                "items": [
                    {
                        "rule_code": "alioth_review_1",
                        "execution_status": "pass",
                        "evidence_artifact_ids": [],
                    }
                ]
            },
        )
    )
    await service.transition_rule(
        TransitionInspectionWorkItemRuleCommand(
            work_item_id=lease.work_item_id,
            rule_id=ids["rule"],
            expected_status=InspectionWorkItemRuleStatus.JUDGED,
            target_status=InspectionWorkItemRuleStatus.WRITTEN,
            lease_token=lease.lease_token,
            observation_packet_id=packet_id,
            rule_execution_id=execution_id,
        )
    )
    await service.transition_attempt(
        TransitionInspectionAttemptCommand(
            attempt_id=attempt_id,
            expected_status=InspectionAttemptStatus.JUDGED,
            target_status=InspectionAttemptStatus.WRITTEN,
            lease_token=lease.lease_token,
        )
    )
    await service.transition(
        TransitionInspectionWorkItemCommand(
            work_item_id=lease.work_item_id,
            expected_status=InspectionWorkItemStatus.RUNNING,
            target_status=InspectionWorkItemStatus.SUCCEEDED,
            row_version=row_version,
            lease_token=lease.lease_token,
        )
    )
