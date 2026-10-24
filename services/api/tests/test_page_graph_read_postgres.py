"""真实 PG 验证页面图读侧、树进度与连续恢复路线，不连接开发数据库。"""

import json
import os
from uuid import uuid4

import pytest

from services.api.app.application.page_graph import PageGraphService
from services.api.app.application.task_insight import TaskInsightNotFoundError, TaskInsightService
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.page_graph_progress_repository import (
    read_page_graph_progress,
)
from services.api.app.infrastructure.db.page_graph_repository import PostgresPageGraphRepository
from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.tests.test_app_postgres_integration import (
    _app_database,
    _register_starting_session,
    _upgrade_isolated_postgres,  # noqa: F401 - 复用显式测试库迁移 fixture
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库"
)


async def _capture(connection, pool, ids, *, page="home", inspection="completed", **metadata):
    capture_id = str(uuid4())
    await connection.execute(
        """
        INSERT INTO qc_capture (id, task_id, agent_run_id, surface_kind, surface_session_id,
            capture_role, session_command_sequence, capture_status, captured_at, metadata)
        VALUES ($1::uuid, $2::uuid, $3::uuid, 'web', $4, 'observation', 1,
            'captured', now(), $5::jsonb)
        """,
        capture_id,
        ids["task"],
        ids["run"],
        ids["session"],
        json.dumps(
            {
                "business_page_identity": page,
                "inspection_status": inspection,
                "finalization_epoch": 0,
                **metadata,
            }
        ),
    )
    async with connection.transaction():
        await PostgresPageGraphRepository(pool=pool).project_capture_from_database_on_connection(
            connection, capture_id=capture_id
        )
    return capture_id


async def _session(connection, pool, ids):
    await _register_starting_session(pool, ids)
    await connection.execute(
        "UPDATE qc_surface_session SET surface_kind = 'web' WHERE session_id = $1",
        ids["session"],
    )
    await connection.execute(
        "UPDATE qc_task SET target_platform = 'h5', target_url = 'https://example.test' "
        "WHERE id = $1::uuid",
        ids["task"],
    )
    return await connection.fetchval(
        "SELECT finalization_epoch FROM qc_surface_session WHERE session_id = $1", ids["session"]
    )


async def _manifest(connection, ids, capture_id, *, quality="pass"):
    await connection.execute(
        """
        INSERT INTO qc_evidence_manifest (manifest_id, manifest_version, task_id, session_id,
            capture_id, protocol_version, manifest_hash, payload_hash, quality_status,
            quality_reason, manifest_payload, created_at)
        VALUES ($1, 1, $2::uuid, $3, $4::uuid, 'evidence_manifest.v1', $5, $5, $6,
            '测试质量事实', '{}'::jsonb, now())
        """,
        f"manifest-{capture_id}",
        ids["task"],
        ids["session"],
        capture_id,
        "a" * 64,
        quality,
    )


async def _coverage(connection, ids, epoch):
    await connection.execute(
        """
        INSERT INTO qc_h5_coverage_snapshot (coverage_snapshot_id, task_id, agent_run_id,
            finalization_epoch, revision, authority_source, required_page_types,
            covered_page_types, missing_page_types, coverage_scopes, assessment_scope_ready,
            frontier_terminal, completion_ready, schema_version, payload_hash)
        VALUES ($1, $2::uuid, $3::uuid, $4, 1, 'h5_page_type_assessment', '["home"]',
            '["home"]', '[]', '[]', true, true, true, 'h5_coverage.v1', $5)
        """,
        f"coverage-{uuid4()}",
        ids["task"],
        ids["run"],
        epoch,
        "b" * 64,
    )


async def test_real_graph_tree_reads_quality_and_inspection_without_changing_coverage():
    async with _app_database() as (connection, pool, ids):
        epoch = await _session(connection, pool, ids)
        capture = await _capture(connection, pool, ids)
        await _coverage(connection, ids, epoch)
        service = PageGraphService(repository=PostgresPageGraphRepository(pool=pool))
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=epoch)
        result = await service.get_graph(**scope)
        assert result["status"] == "available"
        assert result["exploration_tree"]["nodes"][0]["status"] == "visited"
        await _manifest(connection, ids, capture)
        result = await service.get_graph(**scope)
        assert result["exploration_tree"]["nodes"][0]["status"] == "completed"
        missing = await _capture(connection, pool, ids, page="detail")
        await _manifest(connection, ids, missing, quality="fail")
        result = await service.get_graph(**scope)
        statuses = {node["status"] for node in result["exploration_tree"]["nodes"]}
        assert statuses == {"completed", "needs_evidence"}
        assert result["exploration_tree"]["coverage_authority_changed"] is False
        assert (
            await connection.fetchval(
                "SELECT covered_page_types::text FROM qc_h5_coverage_snapshot "
                "WHERE task_id=$1::uuid",
                ids["task"],
            )
            == '["home"]'
        )
        other_epoch = await service.get_graph(**{**scope, "finalization_epoch": epoch + 1})
        assert other_epoch["states"] == []
        assert other_epoch["exploration_tree"]["nodes"] == []


async def test_real_h5_frontier_targets_remain_unvisited_and_block_completion():
    async with _app_database() as (connection, pool, ids):
        epoch = await _session(connection, pool, ids)
        capture = await _capture(connection, pool, ids)
        await _manifest(connection, ids, capture)
        await _coverage(connection, ids, epoch)
        payload = {
            "frontier": {
                "observation": {
                    "metadata": {
                        "agent_run_id": ids["run"],
                        "h5_task_frontier": {
                            "entries": [
                                {
                                    "entry_id": "detail-entry",
                                    "source_capture_id": capture,
                                    "status": "selected",
                                    "candidate": {"semantic_target": "打开详情"},
                                }
                            ]
                        },
                    }
                }
            }
        }
        await connection.execute(
            """
            INSERT INTO qc_v2_work_item (work_id, work_type, task_id, session_id, branch_id,
                source_capture_id, protocol_version, payload_hash, work_payload, status,
                finalization_epoch, deadline)
            VALUES ($1, 'exploration', $2::uuid, $3, 'main', $4::uuid,
                'exploration_work.v1', $5, $6::jsonb, 'pending', $7, now() + interval '1 minute')
            """,
            f"work-{uuid4()}",
            ids["task"],
            ids["session"],
            capture,
            "c" * 64,
            json.dumps(payload),
            epoch,
        )
        result = await PageGraphService(
            repository=PostgresPageGraphRepository(pool=pool)
        ).get_graph(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=epoch)
        nodes = result["exploration_tree"]["nodes"]
        assert len(nodes) == 1
        assert nodes[0]["status"] == "visited"
        assert nodes[0]["targets"][0]["progress_status"] == "unvisited"


async def _edge(connection, ids, epoch, source, target, name, *, status="completed"):
    await connection.execute(
        """
        INSERT INTO qc_page_graph_transition (transition_id, task_id, agent_run_id,
            surface_kind, surface_session_id, finalization_epoch, source_state_id,
            target_state_id, status, terminal, proof, risk_level, counts_as_business_arrival,
            replay_eligible, action_kind)
        VALUES ($1, $2::uuid, $3::uuid, 'web', $4, $5, $6::uuid, $7::uuid, $8, true,
            'trusted', 'low', $9, $9, 'click')
        """,
        f"{ids['run']}:{name}",
        ids["task"],
        ids["run"],
        ids["session"],
        epoch,
        source,
        target,
        status,
        status == "completed",
    )


async def test_real_recovery_query_builds_continuous_routes_and_excludes_failed_edges():
    async with _app_database() as (connection, pool, ids):
        epoch = await _session(connection, pool, ids)
        home = await _capture(connection, pool, ids)
        listing = await _capture(connection, pool, ids, page="list")
        detail = await _capture(connection, pool, ids, page="detail")
        await _edge(connection, ids, epoch, home, listing, "one")
        await _edge(connection, ids, epoch, listing, detail, "two")
        service = PageGraphService(repository=PostgresPageGraphRepository(pool=pool))
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=epoch)
        graph = await service.get_graph(**scope)
        target = next(
            row["logical_page_key"] for row in graph["states"] if row["state_id"] == detail
        )
        route = await service.get_recovery_route(
            **scope, source_state_id=home, target_identity=target
        )
        assert route["status"] == "available"
        steps = route["routes"][0]["steps"]
        assert [(row["source_state_id"], row["target_state_id"]) for row in steps] == [
            (home, listing),
            (listing, detail),
        ]
        assert route["requires_stepwise_reverification"] is True
        await connection.execute(
            "UPDATE qc_page_graph_transition SET status='failed', replay_eligible=false, "
            "counts_as_business_arrival=false WHERE transition_id=$1",
            f"{ids['run']}:one",
        )
        route = await service.get_recovery_route(
            **scope, source_state_id=home, target_identity=target
        )
        assert route["status"] == "unreachable"
        assert route["routes"] == []


async def test_real_recovery_denies_unprojected_graph_instead_of_claiming_full_path():
    async with _app_database() as (connection, pool, ids):
        epoch = await _session(connection, pool, ids)
        capture = await _capture(connection, pool, ids)
        await connection.execute("DELETE FROM qc_page_graph_state WHERE state_id=$1::uuid", capture)
        service = PageGraphService(repository=PostgresPageGraphRepository(pool=pool))
        route = await service.get_recovery_route(
            task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=epoch
        )
        assert route["status"] == "unavailable"
        assert route["graph_status"] == "legacy_unprojected"
        assert route["routes"] == []


async def test_task_graph_query_enforces_tenant_scope_and_authoritative_run_pointer():
    async with _app_database() as (connection, pool, ids):
        await _session(connection, pool, ids)
        capture = await _capture(connection, pool, ids)
        await connection.execute(
            "INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status) "
            "VALUES ($1::uuid, $2::uuid, 'main', 'newer-unbound', 'running')",
            str(uuid4()),
            ids["task"],
        )
        await connection.execute(
            "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
            ids["task"],
            ids["run"],
        )
        service = TaskInsightService(
            repository=PostgresTaskInsightRepository(pool=pool),
            page_graph_service=PageGraphService(repository=PostgresPageGraphRepository(pool=pool)),
        )
        owner = AccessScope(
            actor_user_id=ids["user"], tenant_id=ids["tenant"], permissions=frozenset()
        )
        result = await service.page_graph(ids["task"], access_scope=owner)
        assert result["agent_run_id"] == ids["run"]
        assert [row["state_id"] for row in result["states"]] == [capture]
        outsider = AccessScope(
            actor_user_id=str(uuid4()), tenant_id=str(uuid4()), permissions=frozenset()
        )
        with pytest.raises(TaskInsightNotFoundError):
            await service.page_graph(ids["task"], access_scope=outsider)
        with pytest.raises(TaskInsightNotFoundError):
            await service.page_recovery_route(ids["task"], access_scope=outsider)


async def test_app_completion_progress_uses_indexed_capture_epoch_not_mutable_session_epoch():
    from services.api.tests.test_app_evidence_index_postgres_integration import (
        _database,
        _insert_building_index,
        _insert_capture,
        _insert_entry,
        _insert_manifest,
        _seal,
    )

    async with _database() as (connection, ids):
        epoch = await connection.fetchval(
            "SELECT finalization_epoch FROM qc_surface_session WHERE session_id=$1", ids["session"]
        )
        capture = await _insert_capture(
            connection, ids, capture_role="observation", semantic_role="final_observation"
        )
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || $2::jsonb WHERE id=$1::uuid",
            capture,
            json.dumps({"inspection_status": "completed", "finalization_epoch": epoch}),
        )
        manifest = await _insert_manifest(
            connection, ids, capture_id=capture, version=1, quality_status="pass"
        )
        index = await _insert_building_index(connection, ids)
        await _insert_entry(
            connection,
            index_id=index,
            ids=ids,
            capture_id=capture,
            manifest_id=manifest,
            manifest_version=1,
            semantic_role="final_observation",
        )
        await _seal(connection, index_id=index, outcome="exploration_completed", gaps=())
        # 本测试只验读侧归属，写侧身份判定另由页面图领域和 PG 投影用例验收。
        await connection.execute(
            """
            INSERT INTO qc_page_graph_state (state_id, task_id, agent_run_id, surface_kind,
                surface_session_id, finalization_epoch, capture_role, logical_page_key,
                state_fingerprint, captured_at)
            VALUES ($1::uuid, $2::uuid, $3::uuid, 'android', $4, $5,
                'observation', 'home', $6, now())
            """,
            capture,
            ids["task"],
            ids["run"],
            ids["session"],
            epoch,
            "c" * 64,
        )
        await connection.execute(
            """
            INSERT INTO qc_app_exploration_result (
                agent_run_id, task_id, surface_session_id, evidence_index_id, protocol_version,
                outcome, terminal_reason_code, evidence_index_hash, coverage_summary,
                evidence_gaps, started_at, completed_at, result_sha256)
            SELECT agent_run_id, task_id, surface_session_id, index_id, 'app-exploration-result.v1',
                outcome, terminal_reason_code, content_sha256, '{}', evidence_gaps,
                sealed_at, sealed_at, $2 FROM qc_app_evidence_index WHERE index_id=$1
            """,
            index,
            "sha256:" + "d" * 64,
        )
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=epoch)
        initial = await read_page_graph_progress(connection, **scope)
        assert initial["progress_facts"][0]["exploration_finished"] is True
        await connection.execute(
            "UPDATE qc_surface_session SET finalization_epoch=$2 WHERE session_id=$1",
            ids["session"],
            epoch + 1,
        )
        historical = await read_page_graph_progress(connection, **scope)
        assert historical["progress_facts"][0]["exploration_finished"] is True
        current = await read_page_graph_progress(
            connection, **{**scope, "finalization_epoch": epoch + 1}
        )
        assert current["progress_facts"] == []
        assert current["candidates"] == []
