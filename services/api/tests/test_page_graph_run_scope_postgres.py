"""在真实 PostgreSQL 中验证页面图 Run 归属和回放约束。"""

import json
import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.page_graph_repository import (
    PostgresPageGraphRepository,
    _run_id_from_intent,
)
from services.api.tests.test_app_postgres_integration import (
    _app_database,
    _register_starting_session,
    _upgrade_isolated_postgres,  # noqa: F401 - 复用显式测试库迁移 fixture
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库"
)


@pytest.mark.parametrize(
    "marker",
    (
        "h5-entry:" + ("0" * 32),
        "h5-entry:" + ("-" * 36),
        "h5-entry:00000000-0000-0000-0000-00000000000A",
    ),
)
def test_entry_marker_requires_lowercase_canonical_uuid(marker):
    assert _run_id_from_intent({"source_capture_id": marker}) is None


async def _prepare_web_session(pool, connection, ids) -> int:
    await _register_starting_session(pool, ids)
    await connection.execute(
        "UPDATE qc_surface_session SET surface_kind = 'web' WHERE session_id = $1",
        ids["session"],
    )
    return int(
        await connection.fetchval(
            "SELECT finalization_epoch FROM qc_surface_session WHERE session_id = $1",
            ids["session"],
        )
    )


async def _insert_run(connection, ids, run_id: str) -> None:
    await connection.execute(
        """
        INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
        VALUES ($1::uuid, $2::uuid, 'main', $3, 'running')
        """,
        run_id,
        ids["task"],
        f"Page graph scope run {run_id}",
    )


async def _insert_capture(
    connection,
    ids,
    *,
    run_id: str,
    epoch: int = 0,
    role: str = "observation",
) -> str:
    capture_id = str(uuid4())
    sequence = int(
        await connection.fetchval(
            "SELECT COALESCE(max(session_command_sequence), 0) + 1 "
            "FROM qc_capture WHERE task_id = $1::uuid",
            ids["task"],
        )
    )
    metadata = {
        "business_page_identity": f"scope-page:{capture_id}",
        "finalization_epoch": epoch,
    }
    await connection.execute(
        """
        INSERT INTO qc_capture (
            id, task_id, agent_run_id, surface_kind, surface_session_id,
            capture_role, session_command_sequence, capture_status, captured_at, metadata
        ) VALUES ($1::uuid, $2::uuid, $3::uuid, 'web', $4, $5, $6,
            'captured', now(), $7::jsonb)
        """,
        capture_id,
        ids["task"],
        run_id,
        ids["session"],
        role,
        sequence,
        json.dumps(metadata),
    )
    page_sequence = int(
        await connection.fetchval(
            "SELECT COALESCE(max(page_seq), 0) + 1 FROM qc_page_capture WHERE task_id = $1::uuid",
            ids["task"],
        )
    )
    await connection.execute(
        """
        INSERT INTO qc_page_capture (
            id, task_id, agent_run_id, page_seq, page_slug, url, page_type, page_state,
            capture_status, metadata, surface_session_id, capture_role,
            session_command_sequence
        ) VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'scope-page', $5,
            'home', 'normal', 'captured', $6::jsonb, $7, $8, $9)
        """,
        capture_id,
        ids["task"],
        run_id,
        page_sequence,
        f"https://scope.example/{capture_id}",
        json.dumps(metadata),
        ids["session"],
        role,
        sequence,
    )
    return capture_id


async def _insert_action(
    connection,
    ids,
    *,
    marker: str,
    source_capture_id: str | None = None,
    target_capture_id: str | None = None,
    requested_source_capture_id: str | None = None,
    status: str = "planned",
) -> str:
    action_id = f"scope-action-{uuid4()}"
    sequence = int(
        await connection.fetchval(
            "SELECT COALESCE(max(command_sequence), 0) + 1 "
            "FROM qc_action_execution WHERE session_id = $1",
            ids["session"],
        )
    )
    completed_fields = (
        "'completed', now(), now(), now()"
        if status == "completed"
        else "'planned', NULL, NULL, NULL"
    )
    await connection.execute(
        f"""
        INSERT INTO qc_action_execution (
            action_execution_id, task_id, session_id, intent_id, idempotency_key,
            intent_hash, intent_protocol_version, intent_payload, command_sequence,
            finalization_epoch, requested_source_capture_id, source_capture_id,
            result_capture_id, status, started_at, action_finished_at,
            evidence_finished_at, result_payload
        ) VALUES ($1, $2::uuid, $3, $4, $5, 'scope-intent-hash', 'action-intent.v1',
            $6::jsonb, $7, 0, $8::uuid, $9::uuid, $10::uuid,
            {completed_fields}, '{{}}'::jsonb)
        """,
        action_id,
        ids["task"],
        ids["session"],
        f"scope-intent-{uuid4()}",
        f"scope-idempotency-{uuid4()}",
        json.dumps(
            {
                "surface_kind": "web",
                "action_type": "click",
                "source_capture_id": marker,
            }
        ),
        sequence,
        requested_source_capture_id,
        source_capture_id,
        target_capture_id,
    )
    if source_capture_id is not None or target_capture_id is not None:
        await connection.execute(
            """
            UPDATE qc_capture
            SET action_execution_id = $1,
                capture_role = CASE
                    WHEN id = $2::uuid THEN 'before_action'
                    ELSE 'after_action'
                END
            WHERE id IN ($2::uuid, $3::uuid)
            """,
            action_id,
            source_capture_id,
            target_capture_id,
        )
        await connection.execute(
            """
            UPDATE qc_page_capture
            SET action_execution_id = $1,
                capture_role = CASE
                    WHEN id = $2::uuid THEN 'before_action'
                    ELSE 'after_action'
                END
            WHERE id IN ($2::uuid, $3::uuid)
            """,
            action_id,
            source_capture_id,
            target_capture_id,
        )
    return action_id


async def test_rebuild_run_uses_real_capture_run_over_conflicting_entry_marker():
    async with _app_database() as (connection, pool, ids):
        epoch = await _prepare_web_session(pool, connection, ids)
        source_run = str(uuid4())
        target_run = str(uuid4())
        await _insert_run(connection, ids, source_run)
        await _insert_run(connection, ids, target_run)
        source = await _insert_capture(connection, ids, run_id=source_run, epoch=epoch)
        target = await _insert_capture(connection, ids, run_id=target_run, epoch=epoch)
        action = await _insert_action(
            connection,
            ids,
            marker=f"h5-entry:{ids['run']}",
            source_capture_id=source,
            target_capture_id=target,
            requested_source_capture_id=source,
            status="completed",
        )
        repository = PostgresPageGraphRepository(pool=pool)

        requested_result = await repository.rebuild_run(
            task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=epoch
        )
        assert requested_result.transition_count == 0
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_page_graph_transition WHERE action_execution_id = $1",
                action,
            )
            == 0
        )

        source_result = await repository.rebuild_run(
            task_id=ids["task"], agent_run_id=source_run, finalization_epoch=epoch
        )
        assert source_result.transition_count == 1
        edge = await connection.fetchrow(
            "SELECT agent_run_id::text AS agent_run_id, source_capture_id::text "
            "AS source_capture_id, target_capture_id::text AS target_capture_id, "
            "status, proof, counts_as_business_arrival, replay_eligible, reason_codes, "
            "outcome_payload "
            "FROM qc_page_graph_transition WHERE action_execution_id = $1",
            action,
        )
        assert edge is not None
        assert edge["agent_run_id"] == source_run
        assert edge["source_capture_id"] is None
        assert edge["target_capture_id"] is None
        assert edge["status"] == "completed"
        assert edge["proof"] == "insufficient"
        assert edge["counts_as_business_arrival"] is False
        assert edge["replay_eligible"] is False
        outcome_payload = (
            json.loads(edge["outcome_payload"])
            if isinstance(edge["outcome_payload"], str)
            else edge["outcome_payload"]
        )
        assert "cross_run_capture_pair" in outcome_payload["page_graph_scope_issues"]

        target_result = await repository.rebuild_run(
            task_id=ids["task"], agent_run_id=target_run, finalization_epoch=epoch
        )
        assert target_result.transition_count == 0
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_page_graph_transition "
                "WHERE action_execution_id = $1 AND agent_run_id = $2::uuid",
                action,
                target_run,
            )
            == 0
        )


async def test_rebuild_run_keeps_unbound_bootstrap_marker_as_explicit_fallback():
    async with _app_database() as (connection, pool, ids):
        epoch = await _prepare_web_session(pool, connection, ids)
        bootstrap = await _insert_action(
            connection,
            ids,
            marker=f"agent-entry:{ids['run']}",
            requested_source_capture_id=None,
        )
        repository = PostgresPageGraphRepository(pool=pool)

        result = await repository.rebuild_run(
            task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=epoch
        )
        assert result.transition_count == 1
        assert (
            await connection.fetchval(
                "SELECT agent_run_id::text FROM qc_page_graph_transition "
                "WHERE action_execution_id = $1",
                bootstrap,
            )
            == ids["run"]
        )


async def test_project_action_uses_target_run_when_target_capture_is_unavailable():
    async with _app_database() as (connection, pool, ids):
        epoch = await _prepare_web_session(pool, connection, ids)
        target_run = str(uuid4())
        marker_run = str(uuid4())
        await _insert_run(connection, ids, target_run)
        source = await _insert_capture(connection, ids, run_id=None, epoch=epoch)
        target = await _insert_capture(connection, ids, run_id=target_run, epoch=epoch)
        await connection.execute(
            "UPDATE qc_capture SET capture_status = 'pending' WHERE id = $1::uuid", target
        )
        action = await _insert_action(
            connection,
            ids,
            marker=f"h5-entry:{marker_run}",
            source_capture_id=source,
            target_capture_id=target,
            status="completed",
        )
        await connection.execute(
            "UPDATE qc_page_capture SET capture_status = 'pending' WHERE id = $1::uuid", target
        )
        repository = PostgresPageGraphRepository(pool=pool)
        async with connection.transaction():
            await repository.project_action_on_connection(
                connection, action_execution_id=action
            )

        edge = await connection.fetchrow(
            "SELECT agent_run_id::text AS agent_run_id, source_state_id::text "
            "AS source_state_id, target_state_id::text AS target_state_id, "
            "proof, counts_as_business_arrival, replay_eligible "
            "FROM qc_page_graph_transition WHERE action_execution_id = $1",
            action,
        )
        assert edge is not None
        assert target_run != marker_run
        assert edge["agent_run_id"] == target_run
        assert edge["source_state_id"] is None
        assert edge["target_state_id"] is None
        assert edge["proof"] == "insufficient"
        assert edge["counts_as_business_arrival"] is False
        assert edge["replay_eligible"] is False


async def test_rebuild_task_ignores_malformed_entry_marker_before_uuid_cast():
    async with _app_database() as (connection, pool, ids):
        await _prepare_web_session(pool, connection, ids)
        malformed_marker = "h5-entry:" + ("-" * 36)
        action = await _insert_action(
            connection,
            ids,
            marker=malformed_marker,
            requested_source_capture_id=None,
        )
        result = await PostgresPageGraphRepository(pool=pool).rebuild_task(task_id=ids["task"])
        assert result == ()
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_page_graph_transition WHERE action_execution_id = $1",
                action,
            )
            == 0
        )


async def test_replay_eligible_requires_non_null_low_risk():
    async with _app_database() as (connection, pool, ids):
        epoch = await _prepare_web_session(pool, connection, ids)
        source = await _insert_capture(connection, ids, run_id=ids["run"], epoch=epoch)
        target = await _insert_capture(connection, ids, run_id=ids["run"], epoch=epoch)
        repository = PostgresPageGraphRepository(pool=pool)
        async with connection.transaction():
            await repository.project_capture_from_database_on_connection(
                connection, capture_id=source
            )
            await repository.project_capture_from_database_on_connection(
                connection, capture_id=target
            )

        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                """
                INSERT INTO qc_page_graph_transition (
                    transition_id, task_id, agent_run_id, surface_kind,
                    surface_session_id, finalization_epoch, source_state_id,
                    target_state_id, action_kind, status, terminal, proof,
                    replay_eligible, risk_level
                ) VALUES ($1, $2::uuid, $3::uuid, 'web', $4, $5, $6::uuid,
                    $7::uuid, 'click', 'completed', true, 'trusted', true, NULL)
                """,
                f"scope-replay-null-{uuid4()}",
                ids["task"],
                ids["run"],
                ids["session"],
                epoch,
                source,
                target,
            )

        await connection.execute(
            """
            INSERT INTO qc_page_graph_transition (
                transition_id, task_id, agent_run_id, surface_kind,
                surface_session_id, finalization_epoch, source_state_id,
                target_state_id, action_kind, status, terminal, proof,
                replay_eligible, risk_level
            ) VALUES ($1, $2::uuid, $3::uuid, 'web', $4, $5, $6::uuid,
                $7::uuid, 'click', 'completed', true, 'trusted', true, 'low')
            """,
            f"scope-replay-low-{uuid4()}",
            ids["task"],
            ids["run"],
            ids["session"],
            epoch,
            source,
            target,
        )
