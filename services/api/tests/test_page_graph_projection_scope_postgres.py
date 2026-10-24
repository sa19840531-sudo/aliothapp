"""验证页面图投影对账按权威来源 ID 和 Run/Epoch 精确核对。"""

import json
import os
from uuid import uuid4

import pytest

from services.api.app.application.page_graph import PageGraphService
from services.api.app.infrastructure.db.page_capture_repository import PostgresPageCaptureRepository
from services.api.app.infrastructure.db.page_graph_repository import (
    PostgresPageGraphRepository,
    _projection_counts,
)
from services.api.tests.test_app_postgres_integration import (
    _app_database,
    _register_starting_session,
    _upgrade_isolated_postgres,  # noqa: F401 - 复用显式测试库迁移 fixture
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库"
)


async def _prepare_web_session(pool, connection, ids):
    await _register_starting_session(pool, ids)
    await connection.execute(
        "UPDATE qc_surface_session SET surface_kind = 'web' WHERE session_id = $1",
        ids["session"],
    )
    return await connection.fetchval(
        "SELECT finalization_epoch FROM qc_surface_session WHERE session_id = $1",
        ids["session"],
    )


async def _insert_capture(connection, ids, *, status="captured", epoch=0):
    capture_id = str(uuid4())
    await connection.execute(
        """
        INSERT INTO qc_capture (
            id, task_id, agent_run_id, surface_kind, surface_session_id,
            capture_role, session_command_sequence, capture_status, captured_at, metadata
        ) VALUES ($1::uuid, $2::uuid, $3::uuid, 'web', $4, 'observation', $5,
            $6, now(), $7::jsonb)
        """,
        capture_id,
        ids["task"],
        ids["run"],
        ids["session"],
        int(
            await connection.fetchval(
                "SELECT count(*) + 1 FROM qc_capture WHERE task_id = $1::uuid", ids["task"]
            )
        ),
        status,
        json.dumps(
            {
                "business_page_identity": capture_id,
                **({"finalization_epoch": epoch} if epoch is not None else {}),
            }
        ),
    )
    return capture_id


async def _insert_requested_page_capture(connection, ids, capture_id):
    await connection.execute(
        """
        INSERT INTO qc_page_capture (
            id, task_id, agent_run_id, page_seq, page_slug, url, capture_status,
            surface_session_id, capture_role, session_command_sequence
        ) VALUES ($1::uuid, $2::uuid, $3::uuid,
            (SELECT COALESCE(max(page_seq), 0) + 1 FROM qc_page_capture WHERE task_id=$2::uuid),
            'home', 'https://example.test', 'captured', $4, 'after_action', 99)
        """,
        capture_id,
        ids["task"],
        ids["run"],
        ids["session"],
    )
    return capture_id


async def _insert_action(connection, ids, requested_capture, *, run_marker):
    action_id = f"scope-action-{uuid4()}"
    sequence = int(
        await connection.fetchval(
            "SELECT COALESCE(max(command_sequence), 0) + 1 "
            "FROM qc_action_execution WHERE session_id=$1",
            ids["session"],
        )
    )
    await connection.execute(
        """
        INSERT INTO qc_action_execution (
            action_execution_id, task_id, session_id, intent_id, idempotency_key,
            intent_hash, intent_protocol_version, intent_payload, command_sequence,
            finalization_epoch, requested_source_capture_id
        ) VALUES ($1, $2::uuid, $3, $4, $5, 'scope-hash', 'scope/v1', $6::jsonb,
            $7, 0, $8::uuid)
        """,
        action_id,
        ids["task"],
        ids["session"],
        f"scope-intent-{uuid4()}",
        f"scope-idempotency-{uuid4()}",
        json.dumps({"source_capture_id": f"h5-entry:{run_marker}"}),
        sequence,
        requested_capture,
    )
    return action_id


async def _insert_edge(connection, ids, action_id, *, suffix):
    await connection.execute(
        """
        INSERT INTO qc_page_graph_transition (
            transition_id, task_id, agent_run_id, surface_kind, surface_session_id,
            finalization_epoch, action_execution_id, status, terminal, proof,
            counts_as_business_arrival, replay_eligible, action_kind
        ) VALUES ($1, $2::uuid, $3::uuid, 'web', $4, 0, $5, 'completed', true,
            'insufficient', false, false, 'click')
        """,
        f"scope-edge-{suffix}-{uuid4()}",
        ids["task"],
        ids["run"],
        ids["session"],
        action_id,
    )


async def test_projection_counts_do_not_mask_missing_source_rows_or_foreign_edges():
    async with _app_database() as (connection, pool, ids):
        epoch = await _prepare_web_session(pool, connection, ids)
        source = await _insert_capture(connection, ids)
        missing = await _insert_capture(connection, ids)
        stale_state = await _insert_capture(connection, ids)
        repository = PostgresPageGraphRepository(pool=pool)
        async with connection.transaction():
            await repository.project_capture_from_database_on_connection(
                connection, capture_id=source
            )
            await repository.project_capture_from_database_on_connection(
                connection, capture_id=stale_state
            )
        await connection.execute(
            "UPDATE qc_capture SET capture_status='failed' WHERE id=$1::uuid", stale_state
        )
        requested = await _insert_requested_page_capture(connection, ids, source)
        current_action = await _insert_action(connection, ids, requested, run_marker=ids["run"])
        foreign_action = await _insert_action(connection, ids, requested, run_marker=str(uuid4()))
        await _insert_edge(connection, ids, current_action, suffix="current")
        await _insert_edge(connection, ids, foreign_action, suffix="foreign")

        counts = await _projection_counts(
            connection,
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=epoch,
        )
        assert counts == (2, 1, 1, 1)
        assert missing != source
        unknown_counts = await _projection_counts(
            connection,
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=None,
        )
        assert unknown_counts == (0, 0, 0, 0)


async def test_rebuild_preserves_capture_and_action_epochs_after_session_advances():
    async with _app_database() as (connection, pool, ids):
        await _prepare_web_session(pool, connection, ids)
        frozen = await _insert_capture(connection, ids)
        legacy = await _insert_capture(connection, ids, epoch=None)
        orphan = await _insert_capture(connection, ids, epoch=None)
        await _insert_requested_page_capture(connection, ids, frozen)
        await _insert_requested_page_capture(connection, ids, legacy)
        action = await _insert_action(connection, ids, frozen, run_marker=ids["run"])
        await connection.execute(
            "UPDATE qc_action_execution SET source_capture_id=$2::uuid, "
            "result_capture_id=$3::uuid, status='completed', started_at=now(), "
            "action_finished_at=now(), evidence_finished_at=now() WHERE action_execution_id=$1",
            action,
            frozen,
            legacy,
        )
        await connection.execute(
            "UPDATE qc_capture SET action_execution_id=$1, "
            "capture_role=CASE WHEN id=$2::uuid THEN 'before_action' ELSE 'after_action' END "
            "WHERE id IN ($2::uuid, $3::uuid)",
            action,
            frozen,
            legacy,
        )
        await connection.execute(
            "UPDATE qc_surface_session SET finalization_epoch=1 WHERE session_id=$1",
            ids["session"],
        )
        repository = PostgresPageGraphRepository(pool=pool)
        await repository.rebuild_run(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )
        states = await connection.fetch(
            "SELECT state_id::text, finalization_epoch FROM qc_page_graph_state "
            "WHERE task_id=$1::uuid",
            ids["task"],
        )
        assert {row["state_id"]: row["finalization_epoch"] for row in states} == {
            frozen: 0,
            legacy: 0,
        }
        counts = await _projection_counts(
            connection,
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )
        assert counts == (2, 1, 2, 1)
        assert await _projection_counts(
            connection,
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=1,
        ) == (0, 0, 0, 0)
        await repository.rebuild_task(task_id=ids["task"])
        assert (
            await connection.fetchval(
                "SELECT finalization_epoch FROM qc_page_graph_state WHERE state_id=$1::uuid",
                orphan,
            )
            is None
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_page_graph_state WHERE task_id=$1::uuid "
                "AND finalization_epoch=1",
                ids["task"],
            )
            == 0
        )


async def test_real_detection_writer_promotes_local_state_route_only_after_loaded_observation():
    async with _app_database() as (connection, pool, ids):
        await _prepare_web_session(pool, connection, ids)
        source = await _insert_capture(connection, ids)
        target = await _insert_capture(connection, ids)
        await _insert_requested_page_capture(connection, ids, source)
        await _insert_requested_page_capture(connection, ids, target)
        action = await _insert_action(connection, ids, source, run_marker=ids["run"])
        await connection.execute(
            "UPDATE qc_action_execution SET source_capture_id=$2::uuid, "
            "result_capture_id=$3::uuid, status='completed', started_at=now(), "
            "action_finished_at=now(), evidence_finished_at=now(), "
            'intent_payload=intent_payload || '
            '\'{"action_type":"click","risk_level":"low"}\'::jsonb, '
            'result_payload=\'{"state_diff":{"location_changed":true}}\'::jsonb '
            "WHERE action_execution_id=$1",
            action,
            source,
            target,
        )
        for capture_id, role, page in ((source, "before_action", 1), (target, "after_action", 2)):
            url = f"https://example.test/list?page={page}"
            metadata = {"url": url, "reachable": True, "finalization_epoch": 0}
            await connection.execute(
                "UPDATE qc_page_capture SET url=$2, capture_role=$3, action_execution_id=$4, "
                "metadata=$5::jsonb WHERE id=$1::uuid",
                capture_id,
                url,
                role,
                action,
                json.dumps(metadata),
            )
        repository = PostgresPageGraphRepository(pool=pool)
        async with connection.transaction():
            await repository.project_action_on_connection(connection, action_execution_id=action)
        writer = PostgresPageCaptureRepository(pool=pool, page_graph_repository=repository)

        async def recognize(capture_id, state):
            assert await writer.update_detection_result(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                page_capture_id=capture_id,
                page_type="content_list",
                page_state=state,
                detection_status="recognized",
                confidence=0.95,
                catalog_version="test-v1",
                catalog_sha256="a" * 64,
            )

        await recognize(source, "normal")
        await recognize(target, "loading")
        edge = await connection.fetchrow(
            "SELECT proof, replay_eligible FROM qc_page_graph_transition "
            "WHERE action_execution_id=$1",
            action,
        )
        assert edge["proof"] == "insufficient"
        assert not edge["replay_eligible"]
        await recognize(target, "normal")
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0)
        graph = await PageGraphService(repository=repository).get_graph(**scope)
        assert len(graph["exploration_tree"]["nodes"]) == 1
        assert len(graph["exploration_tree"]["nodes"][0]["states"]) == 2
        assert graph["transitions"][0]["proof"] == "trusted"
        assert graph["transitions"][0]["replay_eligible"]
        assert not graph["transitions"][0]["counts_as_business_arrival"]
        from services.api.app.application.page_graph_projection import build_recovery_routes

        routes = build_recovery_routes(graph, source_state_id=source, target_identity=None)
        assert len(routes["routes"]) == 1
        assert routes["routes"][0]["target_state_id"] == target
