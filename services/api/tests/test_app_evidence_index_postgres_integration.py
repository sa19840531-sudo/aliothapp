"""App Evidence Index 结果门禁的真实 PostgreSQL 验收。

测试只使用显式隔离的 ``ALIOTH_TEST_DATABASE_URL``。未配置时禁止回退到开发库。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from services.api.app.domain.evidence import (
    AppEvidenceGap,
    AppEvidenceIndexEntry,
    AppEvidenceSemanticRole,
    AppExplorationResultOutcome,
    build_app_evidence_index,
    seal_app_evidence_index,
)
from services.api.app.infrastructure.db.app_evidence_index_repository import (
    PostgresAppEvidenceIndexRepository,
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
_SEALED_AT = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
_RESULT_CONTEXT_SHA256 = "sha256:" + "c" * 64


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


async def test_completed_seals_all_latest_pass_entries_and_is_immutable() -> None:
    async with _database() as (connection, ids):
        capture_id = await _insert_capture(
            connection,
            ids,
            capture_role="observation",
            semantic_role="final_observation",
        )
        manifest_id = await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=1,
            quality_status="pass",
        )
        index_id = await _insert_building_index(connection, ids)
        await _insert_entry(
            connection,
            index_id=index_id,
            ids=ids,
            capture_id=capture_id,
            manifest_id=manifest_id,
            manifest_version=1,
            semantic_role="final_observation",
        )

        await _seal(
            connection,
            index_id=index_id,
            outcome="exploration_completed",
            gaps=(),
        )
        await _seal(
            connection,
            index_id=index_id,
            outcome="exploration_completed",
            gaps=(),
        )

        assert (
            await connection.fetchval(
                "SELECT status FROM qc_app_evidence_index WHERE index_id = $1",
                index_id,
            )
            == "sealed"
        )
        with pytest.raises(asyncpg.PostgresError, match="已封存"):
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE qc_app_evidence_index
                    SET terminal_reason_code = 'changed'
                    WHERE index_id = $1
                    """,
                    index_id,
                )

        with pytest.raises(asyncpg.PostgresError, match="禁止追加、修改或删除 Capture"):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE qc_capture SET metadata = '{\"changed\": true}'::jsonb "
                    "WHERE id = $1::uuid",
                    capture_id,
                )

        with pytest.raises(asyncpg.PostgresError, match="Session 证据身份"):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE qc_surface_session SET platform_build_ref = 'app-build:changed' "
                    "WHERE session_id = $1",
                    ids["session"],
                )


async def test_sealed_capture_allows_inspection_overlay_and_blocks_identity() -> None:
    """实连证明 seal 后只放行质检投影 overlay，证据身份仍不可变。"""

    async with _database() as (connection, ids):
        capture_id = await _insert_capture(
            connection,
            ids,
            capture_role="observation",
            semantic_role="final_observation",
        )
        manifest_id = await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=1,
            quality_status="pass",
        )
        index_id = await _insert_building_index(connection, ids)
        await _insert_entry(
            connection,
            index_id=index_id,
            ids=ids,
            capture_id=capture_id,
            manifest_id=manifest_id,
            manifest_version=1,
            semantic_role="final_observation",
        )
        await _seal(
            connection,
            index_id=index_id,
            outcome="exploration_completed",
            gaps=(),
        )

        await connection.execute(
            """
            UPDATE qc_capture
            SET metadata = COALESCE(metadata, '{}'::jsonb)
                || jsonb_build_object(
                    'inspection_status', 'needs_review',
                    'page_type', 'home',
                    'page_state', 'stable'
                ),
                updated_at = now()
            WHERE id = $1::uuid
            """,
            capture_id,
        )
        metadata = await connection.fetchval(
            "SELECT metadata FROM qc_capture WHERE id = $1::uuid",
            capture_id,
        )
        # asyncpg 默认将 JSONB 解码为文本，断言应检查实际 JSON 对象。
        metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
        assert metadata["inspection_status"] == "needs_review"
        assert metadata["page_type"] == "home"
        assert metadata["semantic_role"] == "final_observation"

        with pytest.raises(asyncpg.PostgresError, match="禁止追加、修改或删除 Capture"):
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE qc_capture
                    SET metadata = COALESCE(metadata, '{}'::jsonb)
                        || jsonb_build_object('semantic_role', 'tampered')
                    WHERE id = $1::uuid
                    """,
                    capture_id,
                )


async def test_sealed_overlay_publication_terminal_matches_expected() -> None:
    """实连 Publication：overlay 写回后 terminal_entry_count == expected_entry_count。"""

    async with _database() as (connection, ids):
        capture_id = await _insert_capture(
            connection,
            ids,
            capture_role="observation",
            semantic_role="final_observation",
        )
        manifest_id = await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=1,
            quality_status="pass",
        )
        index_id = await _insert_building_index(connection, ids)
        await _insert_entry(
            connection,
            index_id=index_id,
            ids=ids,
            capture_id=capture_id,
            manifest_id=manifest_id,
            manifest_version=1,
            semantic_role="final_observation",
        )
        await _seal(
            connection,
            index_id=index_id,
            outcome="evidence_insufficient",
            gaps=(),
        )
        await connection.execute(
            """
            UPDATE qc_capture
            SET metadata = COALESCE(metadata, '{}'::jsonb)
                || jsonb_build_object('inspection_status', 'needs_review'),
                updated_at = now()
            WHERE id = $1::uuid
            """,
            capture_id,
        )
        content_sha = await connection.fetchval(
            "SELECT content_sha256 FROM qc_app_evidence_index WHERE index_id = $1",
            index_id,
        )
        publication_id = str(uuid4())
        await connection.execute(
            """
            INSERT INTO qc_app_inspection_publication (
                id, task_id, source_agent_run_id, evidence_index_id,
                evidence_index_hash, ruleset_id, ruleset_version,
                ruleset_snapshot_hash, finalization_epoch, input_hash,
                status, expected_entry_count, terminal_entry_count,
                linked_work_item_count, explicit_exclusion_count, finished_at
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4, $5, $6::uuid, 1,
                'app-evidence-index-test', 0, $7, 'needs_review', 1, 1,
                0, 0, now()
            )
            """,
            publication_id,
            ids["task"],
            ids["run"],
            index_id,
            content_sha,
            ids["ruleset"],
            f"sha256:publication-{publication_id}",
        )
        row = await connection.fetchrow(
            """
            SELECT expected_entry_count, terminal_entry_count, status,
                   (
                     SELECT metadata ->> 'inspection_status'
                     FROM qc_capture
                     WHERE id = $2::uuid
                   ) AS inspection_status
            FROM qc_app_inspection_publication
            WHERE id = $1::uuid
            """,
            publication_id,
            capture_id,
        )
        assert row is not None
        assert int(row["expected_entry_count"]) == int(row["terminal_entry_count"]) == 1
        assert row["status"] == "needs_review"
        assert row["inspection_status"] == "needs_review"

        mismatched = await connection.fetchval(
            """
            SELECT count(*)::integer
            FROM qc_app_inspection_publication
            WHERE task_id = $1::uuid
              AND expected_entry_count <> terminal_entry_count
            """,
            ids["task"],
        )
        assert mismatched == 0


async def test_closure_lock_serializes_same_run_across_connections() -> None:
    pool = await asyncpg.create_pool(_test_database_url(), min_size=0, max_size=2)
    repository = PostgresAppEvidenceIndexRepository(pool=pool)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_closure() -> None:
        async with repository.closure_lock(run_id="closure-lock-run"):
            first_entered.set()
            await release_first.wait()

    async def second_closure() -> None:
        async with repository.closure_lock(run_id="closure-lock-run"):
            second_entered.set()

    first_task = asyncio.create_task(first_closure())
    await first_entered.wait()
    second_task = asyncio.create_task(second_closure())
    await asyncio.sleep(0.05)
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()
    await pool.close()


async def test_completed_rejects_capture_without_latest_pass_manifest() -> None:
    async with _database() as (connection, ids):
        final_capture = await _insert_capture(
            connection,
            ids,
            capture_role="observation",
            semantic_role="final_observation",
        )
        final_manifest = await _insert_manifest(
            connection,
            ids,
            capture_id=final_capture,
            version=1,
            quality_status="pass",
        )
        await _insert_capture(
            connection,
            ids,
            capture_role="before_action",
            semantic_role="action_before",
        )
        index_id = await _insert_building_index(connection, ids)
        await _insert_entry(
            connection,
            index_id=index_id,
            ids=ids,
            capture_id=final_capture,
            manifest_id=final_manifest,
            manifest_version=1,
            semantic_role="final_observation",
        )

        with pytest.raises(asyncpg.PostgresError, match="最新 Manifest 均为 PASS"):
            async with connection.transaction():
                await _seal(
                    connection,
                    index_id=index_id,
                    outcome="exploration_completed",
                    gaps=(),
                )


async def test_insufficient_allows_unindexed_capture_only_with_matching_gap() -> None:
    async with _database() as (connection, ids):
        final_capture = await _insert_capture(
            connection,
            ids,
            capture_role="observation",
            semantic_role="final_observation",
        )
        final_manifest = await _insert_manifest(
            connection,
            ids,
            capture_id=final_capture,
            version=1,
            quality_status="pass",
        )
        failed_capture = await _insert_capture(
            connection,
            ids,
            capture_role="before_action",
            semantic_role="action_before",
        )
        await _insert_manifest(
            connection,
            ids,
            capture_id=failed_capture,
            version=1,
            quality_status="fail",
        )
        index_id = await _insert_building_index(connection, ids)
        await _insert_entry(
            connection,
            index_id=index_id,
            ids=ids,
            capture_id=final_capture,
            manifest_id=final_manifest,
            manifest_version=1,
            semantic_role="final_observation",
        )
        gap = _gap(
            capture_id=failed_capture,
            semantic_role="action_before",
            reason_code="artifact_unreadable",
        )

        await _seal(
            connection,
            index_id=index_id,
            outcome="evidence_insufficient",
            gaps=(gap,),
        )

        stored = await connection.fetchval(
            "SELECT evidence_gaps FROM qc_app_evidence_index WHERE index_id = $1",
            index_id,
        )
        assert _json_value(stored) == [gap]
        with pytest.raises(asyncpg.PostgresError, match="禁止.*修改或删除 Manifest"):
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM qc_evidence_manifest WHERE manifest_id = $1",
                    await connection.fetchval(
                        """
                        SELECT manifest_id
                        FROM qc_evidence_manifest
                        WHERE capture_id = $1::uuid AND quality_status = 'fail'
                        """,
                        failed_capture,
                    ),
                )
        with pytest.raises(asyncpg.PostgresError, match="禁止追加、修改或删除 Capture"):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE qc_capture SET metadata = '{\"changed\": true}'::jsonb "
                    "WHERE id = $1::uuid",
                    failed_capture,
                )
        with pytest.raises(asyncpg.PostgresError, match="禁止追加、修改或删除 Capture"):
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM qc_capture WHERE id = $1::uuid",
                    failed_capture,
                )
        with pytest.raises(asyncpg.PostgresError, match="禁止追加、修改或删除 Capture"):
            async with connection.transaction():
                await _insert_capture(
                    connection,
                    ids,
                    capture_role="observation",
                    semantic_role="surface_reobserve",
                )


async def test_insufficient_rejects_missing_or_wrong_capture_gap() -> None:
    async with _database() as (connection, ids):
        capture_id = await _insert_capture(
            connection,
            ids,
            capture_role="before_action",
            semantic_role="action_before",
        )
        await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=1,
            quality_status="pending",
        )
        index_id = await _insert_building_index(connection, ids)

        with pytest.raises(asyncpg.PostgresError, match="缺少对应 Evidence Gap"):
            async with connection.transaction():
                await _seal(
                    connection,
                    index_id=index_id,
                    outcome="evidence_insufficient",
                    gaps=(),
                )

        wrong_role_gap = _gap(
            capture_id=capture_id,
            semantic_role="action_after",
            reason_code="manifest_pending",
        )
        with pytest.raises(asyncpg.PostgresError, match="semantic_role 不一致"):
            async with connection.transaction():
                await _seal(
                    connection,
                    index_id=index_id,
                    outcome="evidence_insufficient",
                    gaps=(wrong_role_gap,),
                )


async def test_seal_rejects_entry_that_became_stale_after_new_manifest() -> None:
    async with _database() as (connection, ids):
        capture_id = await _insert_capture(
            connection,
            ids,
            capture_role="observation",
            semantic_role="final_observation",
        )
        pass_manifest = await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=1,
            quality_status="pass",
        )
        index_id = await _insert_building_index(connection, ids)
        await _insert_entry(
            connection,
            index_id=index_id,
            ids=ids,
            capture_id=capture_id,
            manifest_id=pass_manifest,
            manifest_version=1,
            semantic_role="final_observation",
        )
        await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=2,
            quality_status="fail",
        )
        gap = _gap(
            capture_id=capture_id,
            semantic_role="final_observation",
            reason_code="artifact_unreadable",
        )

        with pytest.raises(asyncpg.PostgresError, match="未引用当前最新 PASS Manifest"):
            async with connection.transaction():
                await _seal(
                    connection,
                    index_id=index_id,
                    outcome="evidence_insufficient",
                    gaps=(gap,),
                )


async def test_entry_rejects_manifest_that_is_not_latest() -> None:
    async with _database() as (connection, ids):
        capture_id = await _insert_capture(
            connection,
            ids,
            capture_role="observation",
            semantic_role="final_observation",
        )
        stale_manifest = await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=1,
            quality_status="pass",
        )
        await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=2,
            quality_status="fail",
        )
        index_id = await _insert_building_index(connection, ids)

        with pytest.raises(asyncpg.PostgresError, match="最新 Manifest"):
            async with connection.transaction():
                await _insert_entry(
                    connection,
                    index_id=index_id,
                    ids=ids,
                    capture_id=capture_id,
                    manifest_id=stale_manifest,
                    manifest_version=1,
                    semantic_role="final_observation",
                )


async def test_entry_rejects_semantic_role_that_disagrees_with_capture() -> None:
    async with _database() as (connection, ids):
        capture_id = await _insert_capture(
            connection,
            ids,
            capture_role="before_action",
            semantic_role="action_before",
        )
        manifest_id = await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=1,
            quality_status="pass",
        )
        index_id = await _insert_building_index(connection, ids)

        with pytest.raises(asyncpg.PostgresError, match="semantic_role"):
            async with connection.transaction():
                await _insert_entry(
                    connection,
                    index_id=index_id,
                    ids=ids,
                    capture_id=capture_id,
                    manifest_id=manifest_id,
                    manifest_version=1,
                    semantic_role="final_observation",
                )


async def test_building_entry_and_manifest_identity_are_immutable() -> None:
    async with _database() as (connection, ids):
        capture_id = await _insert_capture(
            connection,
            ids,
            capture_role="observation",
            semantic_role="final_observation",
        )
        manifest_id = await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=1,
            quality_status="pass",
        )
        index_id = await _insert_building_index(connection, ids)
        await _insert_entry(
            connection,
            index_id=index_id,
            ids=ids,
            capture_id=capture_id,
            manifest_id=manifest_id,
            manifest_version=1,
            semantic_role="final_observation",
        )

        with pytest.raises(asyncpg.PostgresError, match="Entry 身份字段"):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE qc_app_evidence_index_entry SET sequence_no = 2 WHERE index_id = $1",
                    index_id,
                )
        with pytest.raises(asyncpg.PostgresError, match="已被 App Evidence Index 引用"):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE qc_evidence_manifest "
                    "SET manifest_payload = '{\"changed\": true}'::jsonb "
                    "WHERE manifest_id = $1",
                    manifest_id,
                )


async def test_completed_rejects_index_without_final_observation_entry() -> None:
    async with _database() as (connection, ids):
        capture_id = await _insert_capture(
            connection,
            ids,
            capture_role="bootstrap",
            semantic_role="bootstrap",
        )
        manifest_id = await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=1,
            quality_status="pass",
        )
        index_id = await _insert_building_index(connection, ids)
        await _insert_entry(
            connection,
            index_id=index_id,
            ids=ids,
            capture_id=capture_id,
            manifest_id=manifest_id,
            manifest_version=1,
            semantic_role="bootstrap",
        )

        with pytest.raises(asyncpg.PostgresError, match="Final Observation"):
            async with connection.transaction():
                await _seal(
                    connection,
                    index_id=index_id,
                    outcome="exploration_completed",
                    gaps=(),
                )


async def test_direct_sealed_insert_cannot_bypass_outcome_guards() -> None:
    async with _database() as (connection, ids):
        with pytest.raises(asyncpg.PostgresError, match="禁止直接 INSERT sealed"):
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO qc_app_evidence_index (
                        index_id, task_id, agent_run_id, build_id,
                        surface_session_id, protocol_version, status,
                        outcome, terminal_reason_code, content_sha256,
                        evidence_gaps, sealed_at, created_at
                    )
                    VALUES (
                        $1, $2::uuid, $3::uuid, $4, $5,
                        'app-evidence-index.v1', 'sealed',
                        'exploration_completed', 'exploration_completed',
                        $6, '[]'::jsonb, $7, $7
                    )
                    """,
                    f"app-evidence-index:{uuid4()}",
                    ids["task"],
                    ids["run"],
                    ids["build_ref"],
                    ids["session"],
                    "sha256:" + "a" * 64,
                    _SEALED_AT,
                )


async def test_new_seal_requires_result_context_binding() -> None:
    async with _database() as (connection, ids):
        index_id = await _insert_building_index(
            connection,
            ids,
            result_context_sha256=None,
        )
        gap = _gap(
            capture_id=None,
            semantic_role="final_observation",
            reason_code="workflow_canceled",
            metadata={"scope": "run"},
        )

        with pytest.raises(asyncpg.PostgresError, match="探索结果上下文"):
            async with connection.transaction():
                await _seal(
                    connection,
                    index_id=index_id,
                    outcome="evidence_insufficient",
                    gaps=(gap,),
                )


async def test_repository_recovers_legacy_building_context_and_reconciles_entries() -> None:
    async with _database() as (connection, ids):
        repository = PostgresAppEvidenceIndexRepository(pool=_SingleConnectionPool(connection))
        legacy = build_app_evidence_index(
            task_id=ids["task"],
            run_id=ids["run"],
            build_id=ids["build_ref"],
            surface_session_id=ids["session"],
            entries=(),
            created_at=_SEALED_AT,
            result_context_sha256=None,
        )
        await connection.execute(
            """
            INSERT INTO qc_app_evidence_index (
                index_id, task_id, agent_run_id, build_id, surface_session_id,
                result_context_sha256, protocol_version, status,
                evidence_gaps, created_at
            )
            VALUES ($1, $2::uuid, $3::uuid, $4, $5, NULL,
                    'app-evidence-index.v1', 'building', '[]'::jsonb, $6)
            """,
            legacy.index_id,
            ids["task"],
            ids["run"],
            ids["build_ref"],
            ids["session"],
            _SEALED_AT,
        )
        rebound = build_app_evidence_index(
            task_id=ids["task"],
            run_id=ids["run"],
            build_id=ids["build_ref"],
            surface_session_id=ids["session"],
            entries=(),
            created_at=_SEALED_AT + timedelta(minutes=1),
            result_context_sha256=_RESULT_CONTEXT_SHA256,
        )

        persisted = await repository.create(index=rebound)

        assert persisted.created is False
        assert persisted.index.created_at == _SEALED_AT
        assert (
            await connection.fetchval(
                "SELECT result_context_sha256 FROM qc_app_evidence_index WHERE index_id = $1",
                legacy.index_id,
            )
            == _RESULT_CONTEXT_SHA256
        )

        capture_id = await _insert_capture(
            connection,
            ids,
            capture_role="observation",
            semantic_role="final_observation",
        )
        pass_manifest = await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=1,
            quality_status="pass",
        )
        with_entry = build_app_evidence_index(
            task_id=ids["task"],
            run_id=ids["run"],
            build_id=ids["build_ref"],
            surface_session_id=ids["session"],
            entries=(
                AppEvidenceIndexEntry(
                    sequence_no=1,
                    capture_id=capture_id,
                    evidence_manifest_id=pass_manifest,
                    evidence_manifest_version=1,
                    semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
                    captured_at=_SEALED_AT,
                ),
            ),
            created_at=_SEALED_AT + timedelta(minutes=2),
            result_context_sha256=_RESULT_CONTEXT_SHA256,
        )
        await repository.create(index=with_entry)
        await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=2,
            quality_status="fail",
        )
        reconciled = build_app_evidence_index(
            task_id=ids["task"],
            run_id=ids["run"],
            build_id=ids["build_ref"],
            surface_session_id=ids["session"],
            entries=(),
            created_at=_SEALED_AT + timedelta(minutes=3),
            result_context_sha256=_RESULT_CONTEXT_SHA256,
        )
        persisted = await repository.create(index=reconciled)
        assert persisted.index.created_at == _SEALED_AT
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_app_evidence_index_entry WHERE index_id = $1",
                legacy.index_id,
            )
            == 0
        )
        gap = AppEvidenceGap(
            semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
            reason_code="manifest_fail",
            description="最新 Manifest 未通过",
            capture_id=capture_id,
        )
        sealed = seal_app_evidence_index(
            persisted.index,
            outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
            terminal_reason_code="evidence_incomplete",
            evidence_gaps=(gap,),
            sealed_at=_SEALED_AT + timedelta(minutes=4),
        )
        stored = await repository.seal(index=sealed)
        assert stored.index.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT


async def test_building_index_identity_is_immutable() -> None:
    async with _database() as (connection, ids):
        index_id = await _insert_building_index(connection, ids)

        with pytest.raises(asyncpg.PostgresError, match="身份字段禁止修改"):
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE qc_app_evidence_index
                    SET build_id = 'app-build:changed'
                    WHERE index_id = $1
                    """,
                    index_id,
                )


async def test_seal_revalidates_app_session_identity() -> None:
    async with _database() as (connection, ids):
        index_id = await _insert_building_index(connection, ids)
        await connection.execute(
            """
            UPDATE qc_surface_session
            SET surface_kind = 'web'
            WHERE session_id = $1
            """,
            ids["session"],
        )
        gap = _gap(
            capture_id=None,
            semantic_role="final_observation",
            reason_code="workflow_canceled",
            metadata={"scope": "run"},
        )

        with pytest.raises(asyncpg.PostgresError, match="App Session 归属不一致"):
            async with connection.transaction():
                await _seal(
                    connection,
                    index_id=index_id,
                    outcome="evidence_insufficient",
                    gaps=(gap,),
                )


async def test_insufficient_accepts_explicit_run_scope_gap() -> None:
    async with _database() as (connection, ids):
        capture_id = await _insert_capture(
            connection,
            ids,
            capture_role="observation",
            semantic_role="final_observation",
        )
        manifest_id = await _insert_manifest(
            connection,
            ids,
            capture_id=capture_id,
            version=1,
            quality_status="pass",
        )
        index_id = await _insert_building_index(connection, ids)
        await _insert_entry(
            connection,
            index_id=index_id,
            ids=ids,
            capture_id=capture_id,
            manifest_id=manifest_id,
            manifest_version=1,
            semantic_role="final_observation",
        )
        gap = _gap(
            capture_id=None,
            semantic_role="final_observation",
            reason_code="workflow_canceled",
            metadata={"scope": "run"},
        )
        other_run_id = str(uuid4())
        await connection.execute(
            """
            INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
            VALUES ($1::uuid, $2::uuid, 'main', 'Manifest Guard Other Run', 'running')
            """,
            other_run_id,
            ids["task"],
        )
        other_capture = await _insert_capture(
            connection,
            {**ids, "run": other_run_id},
            capture_role="observation",
            semantic_role="surface_reobserve",
        )

        await _seal(
            connection,
            index_id=index_id,
            outcome="evidence_insufficient",
            gaps=(gap,),
        )

        await connection.execute(
            """
            INSERT INTO qc_evidence_manifest AS existing (
                manifest_id, manifest_version, task_id, session_id, capture_id,
                protocol_version, manifest_hash, payload_hash, quality_status,
                quality_reason, manifest_payload, created_at
            )
            SELECT
                manifest_id, manifest_version, task_id, session_id, capture_id,
                protocol_version, manifest_hash, payload_hash, quality_status,
                quality_reason, manifest_payload, created_at
            FROM qc_evidence_manifest
            WHERE manifest_id = $1
            ON CONFLICT (capture_id, manifest_version) DO UPDATE
            SET manifest_id = existing.manifest_id
            """,
            manifest_id,
        )
        with pytest.raises(asyncpg.PostgresError, match="禁止.*修改或删除 Manifest"):
            async with connection.transaction():
                await _insert_manifest(
                    connection,
                    ids,
                    capture_id=capture_id,
                    version=2,
                    quality_status="fail",
                )
        with pytest.raises(asyncpg.PostgresError, match="禁止修改或删除"):
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE qc_evidence_manifest
                    SET capture_id = $2::uuid
                    WHERE manifest_id = $1
                    """,
                    manifest_id,
                    other_capture,
                )


@asynccontextmanager
async def _database() -> AsyncIterator[tuple[asyncpg.Connection, dict[str, str]]]:
    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "task", "run")}
    ids["session"] = f"app-session-{uuid4()}"
    ids["build_ref"] = f"app-build:{uuid4()}"
    database_url = _test_database_url()
    connection = await asyncpg.connect(database_url)
    transaction = connection.transaction()
    await transaction.start()
    try:
        await _seed_task(connection, ids)
        await connection.execute(
            """
            INSERT INTO qc_surface_session (
                session_id, task_id, surface_kind, session_owner,
                status, platform_build_ref
            )
            VALUES ($1, $2::uuid, 'android',
                    'app-evidence-index-integration', 'starting', $3)
            """,
            ids["session"],
            ids["task"],
            ids["build_ref"],
        )
        yield connection, ids
    finally:
        await transaction.rollback()
        await connection.close()


async def _seed_task(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $2, 'App Evidence Index Integration')
        """,
        ids["tenant"],
        f"app-evidence-index-{ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (
            id, username, full_name, password_hash, role, tenant_id
        )
        VALUES (
            $1::uuid, $2, 'App Evidence Index Integration', 'not-a-login-secret',
            '平台管理员', $3::uuid
        )
        """,
        ids["user"],
        f"app-evidence-index-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        )
        VALUES ($1::uuid, $2, 'App Evidence Index Integration', 1,
                'enabled', 'app-evidence-index-test', 0, 'manual')
        """,
        ids["ruleset"],
        f"app-evidence-index-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, target_platform,
            submitter_id, tenant_id, task_config
        )
        VALUES (
            $1::uuid, NULL, $2::uuid, 'running', 'app', $3::uuid, $4::uuid,
            jsonb_build_object(
                'app', jsonb_build_object('platform_build_ref', $5::text)
            )
        )
        """,
        ids["task"],
        ids["ruleset"],
        ids["user"],
        ids["tenant"],
        ids["build_ref"],
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
        VALUES ($1::uuid, $2::uuid, 'main', 'App Evidence Index Integration', 'running')
        """,
        ids["run"],
        ids["task"],
    )


async def _insert_capture(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    capture_role: str,
    semantic_role: str,
) -> str:
    capture_id = str(uuid4())
    await connection.execute(
        """
        INSERT INTO qc_capture (
            id, task_id, agent_run_id, surface_kind, surface_session_id,
            capture_role, capture_status, captured_at, metadata
        )
        VALUES ($1::uuid, $2::uuid, $3::uuid, 'android', $4,
                $5, 'captured', $6, $7::jsonb)
        """,
        capture_id,
        ids["task"],
        ids["run"],
        ids["session"],
        capture_role,
        _SEALED_AT,
        json.dumps({"semantic_role": semantic_role}),
    )
    return capture_id


async def _insert_manifest(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    capture_id: str,
    version: int,
    quality_status: str,
) -> str:
    manifest_id = f"manifest:{uuid4()}"
    await connection.execute(
        """
        INSERT INTO qc_evidence_manifest (
            manifest_id, manifest_version, task_id, session_id, capture_id,
            protocol_version, manifest_hash, payload_hash, quality_status,
            quality_reason, manifest_payload, created_at
        )
        VALUES ($1, $2, $3::uuid, $4, $5::uuid,
                'evidence-manifest.v1', $6, $7, $8, $9, '{}'::jsonb, $10)
        """,
        manifest_id,
        version,
        ids["task"],
        ids["session"],
        capture_id,
        f"sha256:{uuid4().hex}",
        f"sha256:{uuid4().hex}",
        quality_status,
        None if quality_status != "fail" else "artifact_unreadable",
        _SEALED_AT,
    )
    return manifest_id


async def _insert_building_index(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    result_context_sha256: str | None = _RESULT_CONTEXT_SHA256,
) -> str:
    index_id = f"app-evidence-index:{uuid4()}"
    await connection.execute(
        """
        INSERT INTO qc_app_evidence_index (
            index_id, task_id, agent_run_id, build_id, surface_session_id,
            result_context_sha256, protocol_version, status,
            evidence_gaps, created_at
        )
        VALUES ($1, $2::uuid, $3::uuid, $4, $5,
                $6, 'app-evidence-index.v1', 'building', '[]'::jsonb, $7)
        """,
        index_id,
        ids["task"],
        ids["run"],
        ids["build_ref"],
        ids["session"],
        result_context_sha256,
        datetime.now(UTC),
    )
    return index_id


async def _insert_entry(
    connection: asyncpg.Connection,
    *,
    index_id: str,
    ids: dict[str, str],
    capture_id: str,
    manifest_id: str,
    manifest_version: int,
    semantic_role: str,
) -> None:
    await connection.execute(
        """
        INSERT INTO qc_app_evidence_index_entry (
            index_id, task_id, sequence_no, capture_id,
            evidence_manifest_id, evidence_manifest_version,
            semantic_role, captured_at, metadata
        )
        VALUES ($1, $2::uuid, 1, $3::uuid, $4, $5, $6, $7, '{}'::jsonb)
        """,
        index_id,
        ids["task"],
        capture_id,
        manifest_id,
        manifest_version,
        semantic_role,
        datetime.now(UTC),
    )


async def _seal(
    connection: asyncpg.Connection,
    *,
    index_id: str,
    outcome: str,
    gaps: tuple[dict[str, object], ...],
) -> None:
    await connection.execute(
        """
        UPDATE qc_app_evidence_index
        SET status = 'sealed',
            outcome = $2,
            terminal_reason_code = $3,
            content_sha256 = $4,
            evidence_gaps = $5::jsonb,
            sealed_at = $6,
            updated_at = now()
        WHERE index_id = $1
        """,
        index_id,
        outcome,
        "exploration_completed" if outcome == "exploration_completed" else "evidence_incomplete",
        "sha256:" + "a" * 64,
        json.dumps(gaps),
        _SEALED_AT,
    )


def _gap(
    *,
    capture_id: str | None,
    semantic_role: str,
    reason_code: str,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": "app-evidence-gap.v1",
        "semantic_role": semantic_role,
        "reason_code": reason_code,
        "description": "App Evidence Index integration gap",
        "capture_id": capture_id,
        "metadata": metadata or {},
    }


def _json_value(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def _test_database_url() -> str:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    return require_isolated_test_database_url(
        database_url,
        os.getenv("ALIOTH_DATABASE_URL"),
    )


class _SingleConnectionAcquire:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> asyncpg.Connection:
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _SingleConnectionPool:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    def acquire(self) -> _SingleConnectionAcquire:
        return _SingleConnectionAcquire(self._connection)
