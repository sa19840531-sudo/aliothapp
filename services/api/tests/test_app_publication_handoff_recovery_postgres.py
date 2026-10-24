"""真实隔离 PostgreSQL 验证 APP 封存证据交接恢复及并发围栏。"""

import os
from uuid import uuid4

import pytest

from services.api.app.application.app_inspection_publication import (
    AppInspectionPublicationEntryInput,
)
from services.api.app.application.app_inspection_publication_reconciliation import (
    AppInspectionPublicationReconciler,
)
from services.api.app.application.task_finalization import TerminateTaskFinalizationCommand
from services.api.app.domain.task.completion import TaskExecutionStatus
from services.api.app.infrastructure.db.app_inspection_publication_repository import (
    PostgresAppInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
)
from services.api.tests.test_app_evidence_index_postgres_integration import (
    _database,
    _insert_building_index,
    _insert_capture,
    _insert_entry,
    _insert_manifest,
    _seal,
    _SingleConnectionPool,
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库完整迁移
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库"
)


async def _seal_task_for_review(connection, ids):
    await connection.execute(
        """UPDATE qc_task SET finalization_state = 'session_draining',
               exploration_draining_at = now(), session_draining_at = now()
           WHERE id = $1::uuid""", ids["task"],
    )
    await PostgresTaskFinalizationRepository(pool=_SingleConnectionPool(connection)).terminate(
        command=TerminateTaskFinalizationCommand(
            task_id=ids["task"], agent_run_id=ids["run"], expected_finalization_epoch=0,
            expected_row_version=0, execution_status=TaskExecutionStatus.NEEDS_REVIEW,
            reason_code="TEST_REVIEW_SEAL",
        )
    )


async def _sealed_source(connection, ids, *, mode="eligible"):
    await connection.execute(
        """UPDATE qc_task SET finalization_agent_run_id = $2::uuid,
               task_config = task_config || '{"execution_mode":"full_inspection"}'::jsonb
           WHERE id = $1::uuid""",
        ids["task"], ids["run"],
    )
    capture_id = await _insert_capture(
        connection, ids, capture_role="observation", semantic_role="final_observation"
    )
    manifest_id = await _insert_manifest(
        connection, ids, capture_id=capture_id, version=1, quality_status="pass"
    )
    index_id = await _insert_building_index(connection, ids)
    await _insert_entry(
        connection, index_id=index_id, ids=ids, capture_id=capture_id,
        manifest_id=manifest_id, manifest_version=1, semantic_role="final_observation",
    )
    await _seal(connection, index_id=index_id, outcome="evidence_insufficient", gaps=())
    if mode != "missing_result":
        await connection.execute(
            """
            INSERT INTO qc_app_exploration_result (
                agent_run_id, task_id, surface_session_id, evidence_index_id,
                protocol_version, outcome, terminal_reason_code, evidence_index_hash,
                coverage_summary, evidence_gaps, started_at, completed_at, result_sha256
            )
            SELECT agent_run_id, task_id, surface_session_id, index_id,
                'app-exploration-result.v1', outcome, terminal_reason_code, content_sha256,
                '{}'::jsonb, evidence_gaps, created_at, greatest(created_at, sealed_at), $2
            FROM qc_app_evidence_index WHERE index_id = $1
            """,
            index_id, "sha256:" + "b" * 64,
        )
    await connection.execute(
        """UPDATE qc_surface_session SET status = $2::varchar,
               activated_at = now(), lost_at = CASE WHEN $2::varchar = 'lost' THEN now() END,
               failure_reason = CASE WHEN $2::varchar = 'lost' THEN '测试会话终止' END
           WHERE session_id = $1""",
        ids["session"], "active" if mode == "session_open" else "lost",
    )
    await connection.execute(
        "UPDATE qc_agent_run SET status = $2 WHERE id = $1::uuid",
        ids["run"], {"main_running": "running", "run_canceled": "canceled"}.get(mode, "done"),
    )
    changes = {
        "deleted": "deleted_at = now()",
        "exploration_only": (
            "task_config = task_config || '{\"execution_mode\":\"exploration_only\"}'::jsonb"
        ),
        "session_epoch_changed": "finalization_epoch = finalization_epoch + 1",
    }
    if mode in changes:
        await connection.execute(
            f"UPDATE qc_task SET {changes[mode]} WHERE id = $1::uuid", ids["task"]
        )
    if mode == "sealed_task":
        await _seal_task_for_review(connection, ids)
    if mode == "owner_changed":
        owner = str(uuid4())
        await connection.execute(
            """INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
               VALUES ($1::uuid, $2::uuid, 'main', '另一次运行', 'running')""",
            owner, ids["task"],
        )
        await connection.execute(
            "UPDATE qc_task SET finalization_agent_run_id = $2::uuid WHERE id = $1::uuid",
            ids["task"], owner,
        )
    if mode == "canceled_flag":
        await connection.execute(
            """UPDATE qc_agent_run SET output_context = '{"cancel_requested":true}'::jsonb
               WHERE id = $1::uuid""",
            ids["run"],
        )
    return index_id, AppInspectionPublicationEntryInput(
        sequence_no=1, capture_id=capture_id, evidence_manifest_id=manifest_id,
        evidence_manifest_version=1, semantic_role="final_observation", page_type=None,
        page_state="unknown",
    )


@pytest.mark.parametrize("mode", [
    "eligible", "main_running", "owner_changed", "session_epoch_changed", "session_open",
    "exploration_only", "deleted", "canceled_flag", "run_canceled", "sealed_task",
    "missing_result",
])
async def test_only_current_terminal_run_with_coherent_unpublished_evidence_is_selected(mode):
    async with _database() as (connection, ids):
        await _sealed_source(connection, ids, mode=mode)
        repository = PostgresAppInspectionPublicationRepository(
            pool=_SingleConnectionPool(connection)
        )
        candidates = await repository.list_unpublished_sealed_indexes()
        assert [item.task_id for item in candidates] == (
            [ids["task"]] if mode == "eligible" else []
        )


async def test_handoff_creates_durable_publication_and_outbox_once():
    async with _database() as (connection, ids):
        index_id, entry = await _sealed_source(connection, ids)
        repository = PostgresAppInspectionPublicationRepository(
            pool=_SingleConnectionPool(connection)
        )

        class Publisher:
            async def publish(self, *, task_id, agent_run_id, expected_finalization_epoch):
                return await repository.create_for_index(
                    task_id=task_id, source_agent_run_id=agent_run_id,
                    evidence_index_id=index_id, evidence_index_hash="sha256:" + "a" * 64,
                    entries=(entry,), expected_finalization_epoch=expected_finalization_epoch,
                )

        reconciler = AppInspectionPublicationReconciler(
            repository=repository, publisher=Publisher()
        )
        assert await reconciler.reconcile() == 1
        assert await reconciler.reconcile() == 0
        replay = await Publisher().publish(
            task_id=ids["task"], agent_run_id=ids["run"], expected_finalization_epoch=0
        )
        assert replay.created is False
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_app_inspection_publication WHERE task_id=$1::uuid",
            ids["task"],
        ) == 1
        assert await connection.fetchval(
            """SELECT count(*) FROM qc_outbox_event WHERE task_id=$1::uuid
               AND aggregate_type='app_inspection_publication'""", ids["task"],
        ) == 1


@pytest.mark.parametrize("changed", [None, "task", "run", "epoch"])
async def test_exact_handoff_filter_matches_only_requested_owner_and_epoch(changed):
    async with _database() as (connection, ids):
        await _sealed_source(connection, ids)
        repository = PostgresAppInspectionPublicationRepository(
            pool=_SingleConnectionPool(connection)
        )
        candidates = await repository.list_unpublished_sealed_indexes(
            limit=1,
            task_id=str(uuid4()) if changed == "task" else ids["task"],
            source_agent_run_id=str(uuid4()) if changed == "run" else ids["run"],
            finalization_epoch=1 if changed == "epoch" else 0,
        )
        assert len(candidates) == (1 if changed is None else 0)


@pytest.mark.parametrize("race", ["epoch", "owner", "cancel", "deleted", "sealed"])
async def test_formal_write_rechecks_selection_fence_after_concurrent_change(race):
    async with _database() as (connection, ids):
        index_id, entry = await _sealed_source(connection, ids)
        repository = PostgresAppInspectionPublicationRepository(
            pool=_SingleConnectionPool(connection)
        )
        candidate, = await repository.list_unpublished_sealed_indexes()
        changes = {
            "epoch": "finalization_epoch = finalization_epoch + 1",
            "owner": "finalization_agent_run_id = NULL",
            "deleted": "deleted_at = now()",
        }
        if race == "sealed":
            await _seal_task_for_review(connection, ids)
        elif race in changes:
            await connection.execute(
                f"UPDATE qc_task SET {changes[race]} WHERE id=$1::uuid", ids["task"]
            )
            if race == "epoch":
                # Session 也已进入新轮次时，仍必须拒绝旧扫描结果携带的 epoch。
                await connection.execute(
                    """UPDATE qc_surface_session SET finalization_epoch = 1
                       WHERE session_id = $1""", ids["session"],
                )
        else:
            await connection.execute(
                """UPDATE qc_agent_run SET output_context='{"cancel_requested":true}'::jsonb
                   WHERE id=$1::uuid""", ids["run"],
            )
        with pytest.raises(RuntimeError, match="来源、封存 Index 或任务快照不一致"):
            await repository.create_for_index(
                task_id=ids["task"], source_agent_run_id=ids["run"],
                evidence_index_id=index_id, evidence_index_hash="sha256:" + "a" * 64,
                entries=(entry,), expected_finalization_epoch=candidate.finalization_epoch,
            )
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_app_inspection_publication WHERE task_id=$1::uuid",
            ids["task"],
        ) == 0
