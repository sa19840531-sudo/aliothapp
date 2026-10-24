"""在显式隔离 PostgreSQL 中验证 Capture 原始阶段，不依赖页面图实现。"""

import json
import os
from uuid import uuid4

import pytest

from services.api.app.application.app_capture_persistence import AppCapturePersistenceService
from services.api.app.infrastructure.db.app_bootstrap_repository import (
    PostgresAppBootstrapRepository,
)
from services.api.app.infrastructure.db.app_capture_repository import PostgresAppCaptureRepository
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_postgres_integration import (
    _advance_to_bootstrap_capturing,
    _app_database,
    _register_starting_session,
    _screenshot_artifacts,
    _start_and_claim,
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库"
)


async def _capture(connection, ids, metadata, *, role="observation"):
    capture_id = str(uuid4())
    await connection.execute(
        """
        INSERT INTO qc_capture (id, task_id, agent_run_id, surface_kind, surface_session_id,
            capture_role, capture_status, captured_at, metadata)
        VALUES ($1::uuid, $2::uuid, $3::uuid, 'android', $4, $5, 'captured', now(), $6::jsonb)
        """,
        capture_id,
        ids["task"],
        ids["run"],
        ids["session"],
        role,
        json.dumps(metadata),
    )
    return capture_id


async def _epoch(connection, capture_id):
    return await connection.fetchval(
        f"SELECT {capture_epoch_sql()} FROM qc_capture AS capture WHERE capture.id=$1::uuid",
        capture_id,
    )


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"finalization_epoch": 0}, 0),
        ({"finalization_epoch": "3"}, 3),
        ({}, None),
        ({"finalization_epoch": True}, None),
        ({"finalization_epoch": "9999999999999999999999"}, None),
    ],
)
async def test_capture_epoch_does_not_follow_mutable_session(metadata, expected):
    async with _app_database() as (connection, pool, ids):
        await _register_starting_session(pool, ids)
        capture = await _capture(connection, ids, metadata)
        await connection.execute(
            "UPDATE qc_surface_session SET finalization_epoch=17 WHERE session_id=$1",
            ids["session"],
        )
        assert await _epoch(connection, capture) == expected


async def test_historical_epoch_requires_actual_action_capture_binding():
    async with _app_database() as (connection, pool, ids):
        await _register_starting_session(pool, ids)
        source = await _capture(connection, ids, {}, role="before_action")
        target = await _capture(connection, ids, {"finalization_epoch": 7}, role="after_action")
        orphan = await _capture(connection, ids, {})
        action = f"epoch-action-{uuid4()}"
        await connection.execute(
            """
            INSERT INTO qc_action_execution (
                action_execution_id, task_id, session_id, intent_id, idempotency_key,
                intent_hash, intent_protocol_version, intent_payload, command_sequence,
                finalization_epoch, source_capture_id, result_capture_id, status,
                started_at, action_finished_at, evidence_finished_at
            ) VALUES ($1, $2::uuid, $3, $1, $1, 'hash', 'test-v1', '{}', 1, 0,
                $4::uuid, $5::uuid, 'completed', now(), now(), now())
            """,
            action,
            ids["task"],
            ids["session"],
            source,
            target,
        )
        await connection.execute(
            "UPDATE qc_capture SET action_execution_id=$1 WHERE id IN ($2::uuid,$3::uuid,$4::uuid)",
            action,
            source,
            target,
            orphan,
        )
        await connection.execute(
            "UPDATE qc_surface_session SET finalization_epoch=17 WHERE session_id=$1",
            ids["session"],
        )
        assert await _epoch(connection, source) == 0
        assert await _epoch(connection, target) is None
        assert await _epoch(connection, orphan) is None


async def test_bootstrap_writer_freezes_epoch_in_saved_capture(tmp_path):
    async with _app_database() as (connection, pool, ids):
        bootstrap = PostgresAppBootstrapRepository(pool=pool)
        snapshot = await _start_and_claim(connection, pool, ids, owner="worker-a")
        snapshot = await _advance_to_bootstrap_capturing(bootstrap, ids, snapshot, "worker-a")
        service = AppCapturePersistenceService(
            repository=PostgresAppCaptureRepository(pool=pool),
            artifact_store=LocalEvidenceArtifactStore(root=tmp_path / "evidence"),
            manifest_repository=PostgresEvidenceManifestRepository(pool=pool),
        )
        saved = await service.persist_bootstrap(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            artifacts=_screenshot_artifacts(ids),
            expected_bootstrap_row_version=snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_a"],
        )
        assert (
            await connection.fetchval(
                "SELECT metadata->>'finalization_epoch' FROM qc_capture WHERE id=$1::uuid",
                saved.capture.capture_id,
            )
            == "0"
        )
        await connection.execute(
            "UPDATE qc_surface_session SET finalization_epoch=17 WHERE session_id=$1",
            ids["session"],
        )
        assert await _epoch(connection, saved.capture.capture_id) == 0
