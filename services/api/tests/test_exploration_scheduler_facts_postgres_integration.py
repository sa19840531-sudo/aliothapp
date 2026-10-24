"""探索调度事实读取器的真实 PostgreSQL 归属、快照和刷新验收。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from services.api.app.infrastructure.db.exploration_scheduler_facts_repository import (
    ExplorationSchedulerFactsScopeError,
    PostgresExplorationSchedulerFactsReader,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)

_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _ROOT / "services" / "api" / "migrations" / "alembic.ini"


@pytest.fixture(scope="session", autouse=True)
def _upgrade_isolated_postgres() -> None:
    database_url = _test_database_url()
    previous = os.environ.get("ALIOTH_DATABASE_URL")
    os.environ["ALIOTH_DATABASE_URL"] = database_url
    try:
        with patch("logging.config.fileConfig"):
            command.upgrade(AlembicConfig(str(_ALEMBIC_INI)), "head")
    finally:
        if previous is None:
            os.environ.pop("ALIOTH_DATABASE_URL", None)
        else:
            os.environ["ALIOTH_DATABASE_URL"] = previous


@pytest.mark.asyncio
async def test_h5_reader_uses_current_run_epoch_and_refreshes_missing_snapshot() -> None:
    async with _database(surface_kind="web", target_platform="h5") as (
        connection,
        pool,
        ids,
    ):
        reader = PostgresExplorationSchedulerFactsReader(pool=pool)

        first = await reader.read_h5_facts(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            finalization_epoch=0,
        )
        assert first["exploration_scheduler_facts_scope"]["agent_run_id"] == ids["run"]
        assert first["exploration_scheduler_facts_scope"]["surface_kind"] == "web"
        assert first["h5_coverage_snapshot_fact"]["missing_page_types"] == ["profile"]
        assert [request["request_id"] for request in first["rule_evidence_requests"]] == [
            ids["request"]
        ]

        await connection.execute(
            """
            DELETE FROM qc_h5_coverage_snapshot
            WHERE task_id = $1::uuid AND agent_run_id = $2::uuid
            """,
            ids["task"],
            ids["run"],
        )
        refreshed = await reader.read_h5_facts(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            finalization_epoch=0,
        )
        assert "h5_coverage_snapshot_fact" not in refreshed

        with pytest.raises(ExplorationSchedulerFactsScopeError):
            await reader.read_h5_facts(
                task_id=ids["task"],
                agent_run_id=str(uuid4()),
                session_id=ids["session"],
                finalization_epoch=0,
            )
        with pytest.raises(ExplorationSchedulerFactsScopeError):
            await reader.read_h5_facts(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                finalization_epoch=1,
            )


@pytest.mark.asyncio
async def test_app_reader_validates_capture_scope_and_refreshes_deleted_index() -> None:
    async with _database(surface_kind="android", target_platform="app") as (
        connection,
        pool,
        ids,
    ):
        reader = PostgresExplorationSchedulerFactsReader(pool=pool)
        first = await reader.read_app_facts(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            finalization_epoch=0,
        )
        assert first["exploration_scheduler_facts_scope"]["surface_kind"] == "android"
        entries = first["app_evidence_index"]["entries"]
        bootstrap_entry = next(
            entry for entry in entries if entry["capture_id"] == ids["bootstrap_capture"]
        )
        after_entry = next(entry for entry in entries if entry["capture_id"] == ids["capture"])
        assert bootstrap_entry["exploration_work_id"] is None
        assert bootstrap_entry["action_execution_id"] is None
        assert after_entry["exploration_work_id"] == ids["work"]
        assert after_entry["action_execution_id"] == ids["action"]

        # Work 的起点是动作前 Capture；把它错误改成 after Capture 后，读取器
        # 必须拒绝该条索引，证明校验的是实际 parent/action/work 谱系而非 ID 相等。
        await connection.execute(
            """
            UPDATE qc_v2_work_item
            SET source_capture_id = $2::uuid
            WHERE work_id = $1
            """,
            ids["work"],
            ids["capture"],
        )
        with pytest.raises(ExplorationSchedulerFactsScopeError, match="Work source"):
            await reader.read_app_facts(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                finalization_epoch=0,
            )
        await connection.execute(
            """
            UPDATE qc_v2_work_item
            SET source_capture_id = $2::uuid
            WHERE work_id = $1
            """,
            ids["work"],
            ids["before_capture"],
        )

        with pytest.raises(ExplorationSchedulerFactsScopeError):
            await reader.read_app_facts(
                task_id=ids["task"],
                agent_run_id=str(uuid4()),
                session_id=ids["session"],
                finalization_epoch=0,
            )
        with pytest.raises(ExplorationSchedulerFactsScopeError):
            await reader.read_app_facts(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                finalization_epoch=1,
            )
        await connection.execute(
            "DELETE FROM qc_app_evidence_index WHERE index_id = $1",
            ids["index"],
        )
        refreshed = await reader.read_app_facts(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            finalization_epoch=0,
        )
        assert "app_evidence_index" not in refreshed

        with pytest.raises(ExplorationSchedulerFactsScopeError):
            await reader.read_h5_facts(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                finalization_epoch=0,
            )


@asynccontextmanager
async def _database(
    *,
    surface_kind: str,
    target_platform: str,
    canonical_h5: bool = False,
    retain_sealed: bool = False,
) -> AsyncIterator[tuple[asyncpg.Connection, asyncpg.Pool, dict[str, str]]]:
    database_url = _test_database_url()
    if retain_sealed:
        # 封存证据不能删除；放入独立库保留，不能污染后续共享库的全局聚合。
        name = f"alioth_retained_case_test_{uuid4().hex}"
        shared = await asyncpg.connect(database_url)
        try:
            await shared.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
        finally:
            await shared.close()
        database_url = urlunsplit(urlsplit(database_url)._replace(path=f"/{name}"))
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alembic", "-c", str(_ALEMBIC_INI), "upgrade", "head",
            cwd=_ROOT, env=dict(os.environ, ALIOTH_DATABASE_URL=database_url),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=90)
        assert process.returncode == 0, output.decode()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    ids = {
        name: str(uuid4())
        for name in (
            "tenant",
            "user",
            "ruleset",
            "rule",
            "task",
            "run",
            "capture",
            "old_capture",
            "bootstrap_capture",
            "before_capture",
            "inspection_work_item",
            "old_inspection_work_item",
        )
    }
    ids["work"] = f"facts-reader-work-{uuid4()}"
    ids["session"] = f"h5:{ids['run']}" if canonical_h5 else f"facts-reader-session-{uuid4()}"
    ids["index"] = f"facts-reader-index-{uuid4()}"
    ids["action"] = f"facts-reader-action-{uuid4()}"
    ids["bootstrap_manifest"] = f"facts-reader-bootstrap-manifest-{uuid4()}"
    ids["request"] = f"facts-reader-request-{uuid4()}"
    ids["old_request"] = f"facts-reader-old-request-{uuid4()}"
    try:
        await _seed_context(
            connection,
            ids,
            surface_kind=surface_kind,
            target_platform=target_platform,
        )
        if canonical_h5:
            await connection.execute(
                "UPDATE qc_agent_run SET agent_type='main' WHERE id=$1::uuid", ids["run"]
            )
            await connection.execute(
                "UPDATE qc_surface_session SET session_owner='h5-workflow' WHERE session_id=$1",
                ids["session"],
            )
        yield connection, pool, ids
    finally:
        retained = retain_sealed and await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM qc_completion_gate_snapshot WHERE task_id=$1::uuid)",
            ids["task"],
        )
        if not retained:
            await connection.execute(
                "DELETE FROM qc_app_evidence_index WHERE task_id = $1::uuid", ids["task"],
            )
            await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
            await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
            await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
            await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
            await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def _seed_context(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    surface_kind: str,
    target_platform: str,
) -> None:
    now = datetime.now(UTC)
    build_ref = "facts-reader-build"
    await connection.execute(
        "INSERT INTO qc_tenant(id, tenant_key, display_name) VALUES($1::uuid, $2, $3)",
        ids["tenant"],
        f"facts-reader-tenant-{ids['tenant']}",
        f"Facts reader integration {ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user(id, username, full_name, password_hash, role, tenant_id)
        VALUES($1::uuid, $2, $3, 'integration-only', '平台管理员', $4::uuid)
        """,
        ids["user"],
        f"facts-reader-user-{ids['user']}",
        "Facts reader integration",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset(id, ruleset_code, name, version, status, source_type, rule_count,
                               page_sampling_policy, loop_guard)
        VALUES($1::uuid, $2, 'Facts reader integration', 1, 'enabled', 'manual', 1,
               '{}'::jsonb, '{}'::jsonb)
        """,
        ids["ruleset"],
        f"facts-reader-rules-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_rule(id, ruleset_id, rule_code, name, category, executor_type, check_point)
        VALUES($1::uuid, $2::uuid, 'FACT-GAP-1', 'Facts gap rule', 'text', 'rule_engine', 'detail')
        """,
        ids["rule"],
        ids["ruleset"],
    )
    if target_platform == "app":
        await connection.execute(
            """
            INSERT INTO qc_task(id, target_url, ruleset_id, submitter_id, tenant_id,
                                target_platform, status, task_config)
            VALUES($1::uuid, NULL, $2::uuid, $3::uuid, $4::uuid, 'app', 'running', $5::jsonb)
            """,
            ids["task"],
            ids["ruleset"],
            ids["user"],
            ids["tenant"],
            json.dumps({"app": {"platform_build_ref": build_ref}}),
        )
    else:
        await connection.execute(
            """
            INSERT INTO qc_task(id, target_url, ruleset_id, submitter_id, tenant_id,
                                target_platform, status)
            VALUES($1::uuid, 'https://example.test/', $2::uuid, $3::uuid, $4::uuid,
                   'h5', 'running')
            """,
            ids["task"],
            ids["ruleset"],
            ids["user"],
            ids["tenant"],
        )
    await connection.execute(
        """
        INSERT INTO qc_agent_run(id, task_id, agent_type, agent_name)
        VALUES($1::uuid, $2::uuid, 'explorer', 'facts-reader')
        """,
        ids["run"],
        ids["task"],
    )
    await connection.execute(
        """
        INSERT INTO qc_surface_session(
            session_id, task_id, surface_kind, session_owner, status,
            finalization_epoch, platform_build_ref, activated_at
        )
        VALUES($1, $2::uuid, $3, 'facts-reader', 'active', 0, $4, $5)
        """,
        ids["session"],
        ids["task"],
        surface_kind,
        build_ref if target_platform == "app" else None,
        now,
    )
    if target_platform == "h5":
        await connection.execute(
            """
            INSERT INTO qc_capture(
                id, task_id, agent_run_id, surface_kind, surface_session_id,
                capture_role, session_command_sequence, capture_status, captured_at
            )
            VALUES($1::uuid, $2::uuid, $3::uuid, $4, $5, 'bootstrap', 1, 'captured', $6)
            """,
            ids["capture"],
            ids["task"],
            ids["run"],
            surface_kind,
            ids["session"],
            now,
        )
        await connection.execute(
            """
            UPDATE qc_capture
            SET metadata = '{"finalization_epoch": 0}'::jsonb
            WHERE id = $1::uuid
            """,
            ids["capture"],
        )
        await connection.execute(
            """
            INSERT INTO qc_capture(
                id, task_id, agent_run_id, surface_kind, surface_session_id,
                capture_role, session_command_sequence, capture_status, captured_at,
                metadata
            )
            VALUES($1::uuid, $2::uuid, $3::uuid, $4, $5, 'observation', 2,
                   'captured', $6, '{"finalization_epoch": 1}'::jsonb)
            """,
            ids["old_capture"],
            ids["task"],
            ids["run"],
            surface_kind,
            ids["session"],
            now,
        )
        await connection.execute(
            """
            INSERT INTO qc_h5_coverage_snapshot(
                coverage_snapshot_id, task_id, agent_run_id, finalization_epoch, revision,
                authority_source, required_page_types, covered_page_types, missing_page_types,
                coverage_scopes, assessment_scope_ready, frontier_terminal, completion_ready,
                schema_version, payload_hash, created_at
            )
            VALUES($1, $2::uuid, $3::uuid, 0, 1, 'h5_page_type_assessment',
                   '["home", "profile"]'::jsonb, '["home"]'::jsonb, '["profile"]'::jsonb,
                   '[{"scope": "business"}]'::jsonb, true, false, false,
                   'h5_coverage.v1', 'sha256:facts-reader', $4)
            """,
            f"facts-reader-snapshot-{ids['task']}",
            ids["task"],
            ids["run"],
            now,
        )
        await _seed_h5_evidence_requests(connection, ids, now=now)
    else:
        await _seed_app_after_entry(connection, ids, now=now, surface_kind=surface_kind)


async def _seed_h5_evidence_requests(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    now: datetime,
) -> None:
    """写入同阶段正例和旧阶段反例，验证缺口不会跨 epoch 混入。"""

    for suffix, capture_id, work_id, request_id in (
        (
            "current",
            ids["capture"],
            ids["inspection_work_item"],
            ids["request"],
        ),
        (
            "old",
            ids["old_capture"],
            ids["old_inspection_work_item"],
            ids["old_request"],
        ),
    ):
        await connection.execute(
            """
            INSERT INTO qc_inspection_work_item(
                id, task_id, page_capture_id, producer_agent_run_id,
                consumer_agent_run_id, session_owner_agent_run_id, ruleset_id,
                ruleset_version, protocol_version, batch_key, strategy, category,
                session_dependency, input_hash, status, priority, max_attempts,
                next_attempt_at
            )
            VALUES($1::uuid, $2::uuid, $3::uuid, $4::uuid, $4::uuid, $4::uuid,
                   $5::uuid, 1, 'inspection-work-item.v1', $6, 'rule_engine',
                   'text', 'active_session', $7, 'pending', 100, 3, $8)
            """,
            work_id,
            ids["task"],
            capture_id,
            ids["run"],
            ids["ruleset"],
            f"facts-reader-{suffix}-batch",
            f"facts-reader-{suffix}-input-hash",
            now,
        )
        await connection.execute(
            """
            INSERT INTO qc_inspection_attempt(
                work_item_id, consumer_agent_run_id, attempt_no, lease_token,
                status, input_hash, result_payload
            )
            VALUES($1::uuid, $2::uuid, 1, gen_random_uuid(), 'running', $3,
                   '{}'::jsonb)
            """,
            work_id,
            ids["run"],
            f"facts-reader-{suffix}-attempt-input",
        )
        await connection.execute(
            """
            INSERT INTO qc_evidence_request(
                request_id, requirement_hash, payload_hash, protocol_version,
                work_item_id, attempt_id, task_id, rule_id, rule_code,
                target_capture_id, session_dependency, replay_policy,
                required_equivalence, side_effect_policy, minimum_source_grade,
                request_payload, expires_at
            )
            SELECT $1, $2, $3, 'evidence-request.v1', work_item.id, attempt.id,
                   $4::uuid, $5::uuid, 'FACT-GAP-1', $6::uuid, 'active_session',
                   'same_surface_only', 'original_atomic', 'read_only', 'A0',
                   $7::jsonb, $8
            FROM qc_inspection_work_item AS work_item
            INNER JOIN qc_inspection_attempt AS attempt
                ON attempt.work_item_id = work_item.id
            WHERE work_item.id = $9::uuid
              AND attempt.attempt_no = 1
            """,
            request_id,
            f"facts-reader-{suffix}-requirement",
            f"facts-reader-{suffix}-payload",
            ids["task"],
            ids["rule"],
            capture_id,
            json.dumps({"target_page_type": "profile", "suffix": suffix}),
            now + timedelta(hours=1),
            work_id,
        )


async def _seed_app_after_entry(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    now: datetime,
    surface_kind: str,
) -> None:
    """写入真实 App before/action/after 链，验证 Work 起点不等于 after。"""

    await connection.execute(
        """
        INSERT INTO qc_capture(
            id, task_id, agent_run_id, surface_kind, surface_session_id,
            capture_role, session_command_sequence, capture_status, captured_at, metadata
        )
        VALUES($1::uuid, $2::uuid, $3::uuid, $4, $5, 'bootstrap', 1, 'captured', $6,
               '{"finalization_epoch": 0}'::jsonb)
        """,
        ids["bootstrap_capture"],
        ids["task"],
        ids["run"],
        surface_kind,
        ids["session"],
        now,
    )
    await connection.execute(
        """
        INSERT INTO qc_action_execution(
            action_execution_id, task_id, session_id, intent_id,
            idempotency_key, intent_hash, intent_protocol_version,
            intent_payload, command_sequence, finalization_epoch,
            requested_source_capture_id, status, result_payload, created_at
        )
        VALUES($1, $2::uuid, $3, $4, $5, 'facts-reader-action-hash',
               'action-intent.v1', $6::jsonb, 2, 0, $7::uuid,
               'planned', '{}'::jsonb, $8)
        """,
        ids["action"],
        ids["task"],
        ids["session"],
        f"facts-reader-intent-{uuid4()}",
        f"facts-reader-idempotency-{uuid4()}",
        json.dumps({"source_capture_id": ids["bootstrap_capture"]}),
        ids["bootstrap_capture"],
        now,
    )
    await connection.execute(
        """
        INSERT INTO qc_capture(
            id, task_id, agent_run_id, surface_kind, surface_session_id,
            parent_capture_id, action_execution_id, capture_role,
            session_command_sequence, capture_status, captured_at, metadata
        )
        VALUES($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::uuid, $7,
               'before_action', 2, 'captured', $8, '{}'::jsonb)
        """,
        ids["before_capture"],
        ids["task"],
        ids["run"],
        surface_kind,
        ids["session"],
        ids["bootstrap_capture"],
        ids["action"],
        now,
    )
    await connection.execute(
        """
        UPDATE qc_action_execution
        SET source_capture_id = $2::uuid,
            status = 'executing',
            started_at = $3,
            updated_at = $3
        WHERE action_execution_id = $1
        """,
        ids["action"],
        ids["before_capture"],
        now,
    )
    await connection.execute(
        """
        INSERT INTO qc_capture(
            id, task_id, agent_run_id, surface_kind, surface_session_id,
            parent_capture_id, action_execution_id, capture_role,
            session_command_sequence, capture_status, captured_at, metadata
        )
        VALUES($1::uuid, $2::uuid, $3::uuid, $4, $5, $6::uuid, $7,
               'after_action', 2, 'captured', $8, '{}'::jsonb)
        """,
        ids["capture"],
        ids["task"],
        ids["run"],
        surface_kind,
        ids["session"],
        ids["before_capture"],
        ids["action"],
        now,
    )
    await connection.execute(
        """
        UPDATE qc_action_execution
        SET result_capture_id = $2::uuid,
            status = 'completed',
            action_finished_at = $3,
            evidence_finished_at = $3,
            updated_at = $3
        WHERE action_execution_id = $1
        """,
        ids["action"],
        ids["capture"],
        now,
    )
    await connection.execute(
        """
        INSERT INTO qc_app_evidence_index(
            index_id, task_id, agent_run_id, build_id, surface_session_id,
            protocol_version, status, evidence_gaps, created_at
        )
        VALUES($1, $2::uuid, $3::uuid, $4, $5,
               'app-evidence-index.v1', 'building',
               '[{"page_type": "profile", "reason_code": "missing_manifest"}]'::jsonb, $6)
        """,
        ids["index"],
        ids["task"],
        ids["run"],
        "facts-reader-build",
        ids["session"],
        now,
    )
    manifest_id = f"facts-reader-manifest-{ids['task']}"
    await connection.execute(
        """
        INSERT INTO qc_evidence_manifest(
            manifest_id, manifest_version, task_id, session_id, capture_id,
            protocol_version, manifest_hash, payload_hash, quality_status,
            manifest_payload, created_at
        )
        VALUES($1, 1, $2::uuid, $3, $4::uuid, 'evidence-manifest.v1',
               'facts-reader-manifest-hash', 'facts-reader-payload-hash', 'pass',
               '{}'::jsonb, $5)
        """,
        manifest_id,
        ids["task"],
        ids["session"],
        ids["capture"],
        now,
    )
    await connection.execute(
        """
        INSERT INTO qc_evidence_manifest(
            manifest_id, manifest_version, task_id, session_id, capture_id,
            protocol_version, manifest_hash, payload_hash, quality_status,
            manifest_payload, created_at
        )
        VALUES($1, 1, $2::uuid, $3, $4::uuid, 'evidence-manifest.v1',
               'facts-reader-bootstrap-manifest-hash',
               'facts-reader-bootstrap-payload-hash', 'pass', '{}'::jsonb, $5)
        """,
        ids["bootstrap_manifest"],
        ids["task"],
        ids["session"],
        ids["bootstrap_capture"],
        now,
    )
    await connection.execute(
        """
        INSERT INTO qc_v2_work_item(
            work_id, work_type, task_id, session_id, branch_id, source_capture_id,
            protocol_version, payload_hash, work_payload, deadline, finalization_epoch
        )
        VALUES($1, 'exploration', $2::uuid, $3, $4, $5::uuid,
               'exploration-work.v1', 'facts-reader-work-hash', '{}'::jsonb, $6, 0)
        """,
        ids["work"],
        ids["task"],
        ids["session"],
        f"facts-reader-branch-{uuid4()}",
        ids["before_capture"],
        now + timedelta(hours=1),
    )
    await connection.execute(
        """
        INSERT INTO qc_app_evidence_index_entry(
            index_id, task_id, sequence_no, capture_id, evidence_manifest_id,
            evidence_manifest_version, semantic_role, exploration_work_id,
            action_execution_id, captured_at, metadata
        )
        VALUES($1, $2::uuid, 1, $3::uuid, $4, 1, 'bootstrap', NULL, NULL, $5, '{}'::jsonb)
        """,
        ids["index"],
        ids["task"],
        ids["bootstrap_capture"],
        ids["bootstrap_manifest"],
        now,
    )
    await connection.execute(
        """
        INSERT INTO qc_app_evidence_index_entry(
            index_id, task_id, sequence_no, capture_id, evidence_manifest_id,
            evidence_manifest_version, semantic_role, exploration_work_id,
            action_execution_id, captured_at, metadata
        )
        VALUES($1, $2::uuid, 2, $3::uuid, $4, 1, 'action_after', $5, $6, $7, '{}'::jsonb)
        """,
        ids["index"],
        ids["task"],
        ids["capture"],
        manifest_id,
        ids["work"],
        ids["action"],
        now,
    )


def _test_database_url() -> str:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    return require_isolated_test_database_url(
        database_url,
        os.getenv("ALIOTH_DATABASE_URL"),
    )
