"""App PNG 在真实 PostgreSQL 中的持久化、租约与幂等性验收。

测试仅在显式提供 ``ALIOTH_TEST_DATABASE_URL`` 时执行，并会在测试库执行
``alembic upgrade head``。未提供该变量时，绝不回退连接开发库。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from services.api.app.application.app_bootstrap_lease import AppBootstrapLeaseLostError
from services.api.app.application.app_bootstrap_state import (
    CancelAppBootstrapCommand,
    FailAppBootstrapCommand,
    RenewAppBootstrapLeaseCommand,
    TransitionAppBootstrapCommand,
)
from services.api.app.application.app_capture_persistence import (
    AppCapturePersistenceService,
)
from services.api.app.application.app_frontier_queue import (
    ClaimAppFrontierCommand,
    TransitionAppFrontierCommand,
)
from services.api.app.application.app_global_frontier import AppGlobalFrontierService
from services.api.app.application.app_workflow import (
    AppQcWorkflow,
    RunAppWorkflowCommand,
)
from services.api.app.application.exploration_work_queue import (
    ClaimExplorerWorkCommand,
    CommitExplorerWorkSuccessorCommand,
    ExplorerWorkStatus,
    ExplorerWorkType,
    RenewExplorerWorkLeaseCommand,
    TransitionExplorerWorkCommand,
)
from services.api.app.domain.exploration import (
    AppFrontierStatus,
    AppVisualAnalysis,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    AppBootstrapSnapshot,
    AppBootstrapStage,
    AppInstallRequest,
    AppInstallResult,
    AppOpenRequest,
    AppOpenResult,
    AppReadyHandoff,
    CaptureRef,
    InteractionOperation,
    InteractionRiskLevel,
    RawArtifact,
    RawArtifactSet,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSpec,
    build_android_screenshot_only_manifest,
)
from services.api.app.domain.interaction.surface_driver import app_primary_surface_ref
from services.api.app.domain.task.platform_build import PlatformBuild, PlatformBuildStatus
from services.api.app.infrastructure.db.app_bootstrap_repository import (
    AppBootstrapPersistenceError,
    PostgresAppBootstrapRepository,
)
from services.api.app.infrastructure.db.app_capture_repository import (
    AppCapturePersistenceError,
    PostgresAppCaptureRepository,
)
from services.api.app.infrastructure.db.app_frontier_repository import (
    PostgresAppFrontierRepository,
)
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    PostgresAppVisualObservationRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.explorer_work_repository import (
    PostgresExplorerWorkRepository,
)
from services.api.app.infrastructure.db.page_graph_repository import (
    PostgresPageGraphRepository,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
    SurfaceSessionPersistenceError,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.app.infrastructure.storage.local_evidence_store import (
    LocalEvidenceArtifactStore,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)

_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _ROOT / "services" / "api" / "migrations" / "alembic.ini"


@pytest.fixture(scope="session", autouse=True)
def _upgrade_isolated_postgres() -> None:
    """只迁移显式声明的测试库，避免意外修改本地开发数据库。"""
    database_url = _test_database_url()
    previous = os.environ.get("ALIOTH_DATABASE_URL")
    os.environ["ALIOTH_DATABASE_URL"] = database_url
    try:
        configuration = AlembicConfig(str(_ALEMBIC_INI))
        with patch("logging.config.fileConfig"):
            command.upgrade(configuration, "head")
    finally:
        if previous is None:
            os.environ.pop("ALIOTH_DATABASE_URL", None)
        else:
            os.environ["ALIOTH_DATABASE_URL"] = previous


async def test_postgres_app_bootstrap_capture_persists_png_atomically(
    tmp_path: Path,
) -> None:
    """首屏 PNG 仅入对象存储，数据库只保存 Artifact 引用与元数据。"""
    async with _app_database() as (connection, pool, ids):
        store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
        captures = AppCapturePersistenceService(
            repository=PostgresAppCaptureRepository(pool=pool),
            artifact_store=store,
            manifest_repository=PostgresEvidenceManifestRepository(pool=pool),
        )
        bootstrap = PostgresAppBootstrapRepository(pool=pool)
        snapshot = await _start_and_claim(connection, pool, ids, owner="worker-a")
        snapshot = await _advance_to_bootstrap_capturing(bootstrap, ids, snapshot, "worker-a")
        renewed = await bootstrap.renew(
            RenewAppBootstrapLeaseCommand(
                task_id=ids["task"],
                session_id=ids["session"],
                platform_build_ref=ids["build_ref"],
                lease_owner="worker-a",
                lease_token=ids["token_a"],
                lease_seconds=120,
            )
        )
        assert renewed.row_version == snapshot.row_version

        persisted = await captures.persist_bootstrap(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            artifacts=_screenshot_artifacts(ids),
            expected_bootstrap_row_version=snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_a"],
        )

        assert await store.read(persisted.screenshot.storage_path) == _valid_png()
        artifact = await connection.fetchrow(
            """
            SELECT storage_path, checksum, size_bytes
            FROM qc_evidence_artifact
            WHERE id = $1::uuid
            """,
            persisted.screenshot.artifact_id,
        )
        assert artifact is not None
        assert artifact["storage_path"] == persisted.screenshot.storage_path
        assert artifact["checksum"] == f"sha256:{hashlib.sha256(_valid_png()).hexdigest()}"
        assert artifact["size_bytes"] == len(_valid_png())
        assert persisted.bootstrap_row_version == snapshot.row_version + 1
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_capture WHERE task_id = $1::uuid", ids["task"]
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_evidence_artifact WHERE task_id = $1::uuid",
                ids["task"],
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_app_capture WHERE capture_id = $1::uuid",
                persisted.capture.capture_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_page_capture WHERE task_id = $1::uuid", ids["task"]
            )
            == 0
        )
        bytea_columns = await connection.fetch(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name IN ('qc_capture', 'qc_evidence_artifact', 'qc_app_capture')
              AND data_type = 'bytea'
            """
        )
        assert bytea_columns == []
        await _assert_no_orphan_pngs(
            root=tmp_path / "evidence", connection=connection, task_id=ids["task"]
        )


async def test_postgres_two_workers_only_one_bootstrap_lease_and_capture(
    tmp_path: Path,
) -> None:
    """两个 Worker 同抢一个 Session 时，数据库只允许一个租约和一份首屏事实。"""
    async with _app_database() as (connection, pool, ids):
        store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
        bootstrap = PostgresAppBootstrapRepository(pool=pool)
        captures = AppCapturePersistenceService(
            repository=PostgresAppCaptureRepository(pool=pool),
            artifact_store=store,
            manifest_repository=PostgresEvidenceManifestRepository(pool=pool),
        )
        await _register_starting_session(pool, ids)

        results = await asyncio.gather(
            _claim(bootstrap, ids, owner="worker-a", token_key="token_a"),
            _claim(bootstrap, ids, owner="worker-b", token_key="token_b"),
            return_exceptions=True,
        )
        claimed = [item for item in results if not isinstance(item, BaseException)]
        rejected = [item for item in results if isinstance(item, BaseException)]
        assert len(claimed) == 1
        assert len(rejected) == 1
        assert isinstance(rejected[0], AppBootstrapPersistenceError)
        winner = "worker-a" if results[0] is claimed[0] else "worker-b"
        winner_token = "token_a" if winner == "worker-a" else "token_b"
        stale_owner = "worker-b" if winner == "worker-a" else "worker-a"
        stale_token = "token_b" if winner == "worker-a" else "token_a"
        snapshot = await _advance_to_bootstrap_capturing(bootstrap, ids, claimed[0], winner)

        persisted, stale_result = await asyncio.gather(
            captures.persist_bootstrap(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                artifacts=_screenshot_artifacts(ids),
                expected_bootstrap_row_version=snapshot.row_version,
                workflow_lease_owner=winner,
                workflow_lease_token=ids[winner_token],
            ),
            _capture_error(
                captures=captures,
                ids=ids,
                expected_row_version=snapshot.row_version,
                owner=stale_owner,
                token=ids[stale_token],
            ),
        )

        assert isinstance(stale_result, AppCapturePersistenceError)
        assert persisted.capture.session_id == ids["session"]
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_capture WHERE task_id = $1::uuid", ids["task"]
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_evidence_artifact WHERE task_id = $1::uuid",
                ids["task"],
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_app_capture WHERE capture_id = $1::uuid",
                persisted.capture.capture_id,
            )
            == 1
        )
        await _assert_no_orphan_pngs(
            root=tmp_path / "evidence", connection=connection, task_id=ids["task"]
        )


async def test_postgres_two_workers_race_renew_transition_fail_and_cancel() -> None:
    """租约、阶段与终态都只允许当前 owner/token 成功写入一次。"""
    async with _app_database() as (connection, pool, ids):
        bootstrap = PostgresAppBootstrapRepository(pool=pool)
        await _register_starting_session(pool, ids)
        claimed = await _claim(bootstrap, ids, owner="worker-a", token_key="token_a")

        renew_results = await asyncio.gather(
            bootstrap.renew(
                RenewAppBootstrapLeaseCommand(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    platform_build_ref=ids["build_ref"],
                    lease_owner="worker-a",
                    lease_token=ids["token_a"],
                    lease_seconds=120,
                )
            ),
            bootstrap.renew(
                RenewAppBootstrapLeaseCommand(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    platform_build_ref=ids["build_ref"],
                    lease_owner="worker-b",
                    lease_token=ids["token_b"],
                    lease_seconds=120,
                )
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in renew_results) == 1
        assert any(isinstance(item, AppBootstrapPersistenceError) for item in renew_results)
        renewed = next(item for item in renew_results if not isinstance(item, BaseException))
        assert renewed.row_version == claimed.row_version

        transition_results = await asyncio.gather(
            _transition_with_identity(
                bootstrap,
                ids,
                snapshot=renewed,
                owner="worker-a",
                token_key="token_a",
            ),
            _transition_with_identity(
                bootstrap,
                ids,
                snapshot=renewed,
                owner="worker-b",
                token_key="token_b",
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in transition_results) == 1
        assert any(isinstance(item, AppBootstrapPersistenceError) for item in transition_results)
        transitioned = next(
            item for item in transition_results if not isinstance(item, BaseException)
        )
        assert transitioned.stage is AppBootstrapStage.ACQUIRING_SESSION
        assert transitioned.row_version == claimed.row_version + 1

        terminal_results = await asyncio.gather(
            bootstrap.fail(
                FailAppBootstrapCommand(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    platform_build_ref=ids["build_ref"],
                    expected_stage=transitioned.stage,
                    expected_row_version=transitioned.row_version,
                    lease_owner="worker-a",
                    lease_token=ids["token_a"],
                    error_code="WORKER_A_TERMINAL",
                )
            ),
            bootstrap.cancel(
                CancelAppBootstrapCommand(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    platform_build_ref=ids["build_ref"],
                    expected_stage=transitioned.stage,
                    expected_row_version=transitioned.row_version,
                    lease_owner="worker-b",
                    lease_token=ids["token_b"],
                )
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in terminal_results) == 1
        assert any(isinstance(item, AppBootstrapPersistenceError) for item in terminal_results)
        terminal = next(item for item in terminal_results if not isinstance(item, BaseException))
        assert terminal.stage is AppBootstrapStage.FAILED
        assert terminal.row_version == transitioned.row_version + 1
        final = await connection.fetchrow(
            """
            SELECT stage, workflow_lease_owner, workflow_lease_token, row_version
            FROM qc_app_bootstrap
            WHERE session_id = $1
            """,
            ids["session"],
        )
        assert final is not None
        assert dict(final) == {
            "stage": "failed",
            "workflow_lease_owner": None,
            "workflow_lease_token": None,
            "row_version": terminal.row_version,
        }


async def test_postgres_expired_lease_rejects_old_worker_and_allows_new_worker(
    tmp_path: Path,
) -> None:
    """过期旧 token 不能更新阶段、提交 Capture 或 APP_READY，新的 token 可以接管。"""
    async with _app_database() as (connection, pool, ids):
        store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
        bootstrap = PostgresAppBootstrapRepository(pool=pool)
        sessions = PostgresSurfaceSessionRepository(pool=pool)
        captures = AppCapturePersistenceService(
            repository=PostgresAppCaptureRepository(pool=pool),
            artifact_store=store,
            manifest_repository=PostgresEvidenceManifestRepository(pool=pool),
        )
        snapshot = await _start_and_claim(connection, pool, ids, owner="worker-a")
        snapshot = await _advance_to_stage(
            bootstrap,
            ids,
            snapshot,
            "worker-a",
            target_stage=AppBootstrapStage.OPENED,
        )
        await connection.execute(
            """
            UPDATE qc_app_bootstrap
            SET workflow_lease_expires_at = now() - interval '1 second'
            WHERE session_id = $1
            """,
            ids["session"],
        )
        new_snapshot = await _claim(bootstrap, ids, owner="worker-a", token_key="token_b")
        assert new_snapshot.row_version == snapshot.row_version

        with pytest.raises(AppBootstrapPersistenceError):
            await bootstrap.renew(
                RenewAppBootstrapLeaseCommand(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    platform_build_ref=ids["build_ref"],
                    lease_owner="worker-a",
                    lease_token=ids["token_a"],
                    lease_seconds=120,
                )
            )
        with pytest.raises(AppBootstrapPersistenceError):
            await bootstrap.transition(
                TransitionAppBootstrapCommand(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    platform_build_ref=ids["build_ref"],
                    expected_stage=AppBootstrapStage.OPENED,
                    target_stage=AppBootstrapStage.BOOTSTRAP_CAPTURING,
                    expected_row_version=snapshot.row_version,
                    lease_owner="worker-a",
                    lease_token=ids["token_a"],
                )
            )
        new_snapshot = await bootstrap.transition(
            TransitionAppBootstrapCommand(
                task_id=ids["task"],
                session_id=ids["session"],
                platform_build_ref=ids["build_ref"],
                expected_stage=AppBootstrapStage.OPENED,
                target_stage=AppBootstrapStage.BOOTSTRAP_CAPTURING,
                expected_row_version=new_snapshot.row_version,
                lease_owner="worker-a",
                lease_token=ids["token_b"],
            )
        )
        with pytest.raises(AppBootstrapPersistenceError):
            await bootstrap.fail(
                FailAppBootstrapCommand(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    platform_build_ref=ids["build_ref"],
                    expected_stage=AppBootstrapStage.BOOTSTRAP_CAPTURING,
                    expected_row_version=new_snapshot.row_version,
                    lease_owner="worker-a",
                    lease_token=ids["token_a"],
                    error_code="STALE_WORKER",
                )
            )
        with pytest.raises(AppBootstrapPersistenceError):
            await bootstrap.cancel(
                CancelAppBootstrapCommand(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    platform_build_ref=ids["build_ref"],
                    expected_stage=AppBootstrapStage.BOOTSTRAP_CAPTURING,
                    expected_row_version=new_snapshot.row_version,
                    lease_owner="worker-a",
                    lease_token=ids["token_a"],
                )
            )
        with pytest.raises(AppCapturePersistenceError):
            await captures.persist_bootstrap(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                artifacts=_screenshot_artifacts(ids),
                expected_bootstrap_row_version=new_snapshot.row_version,
                workflow_lease_owner="worker-a",
                workflow_lease_token=ids["token_a"],
            )

        persisted = await captures.persist_bootstrap(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            artifacts=_screenshot_artifacts(ids),
            expected_bootstrap_row_version=new_snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_b"],
        )
        handoff = _handoff(ids, persisted.capture, persisted.bootstrap_row_version or 0)
        session_snapshot = await sessions.get(session_id=ids["session"])
        assert session_snapshot is not None

        with pytest.raises(SurfaceSessionPersistenceError):
            await sessions.activate_from_bootstrap(
                handoff=handoff,
                expected_bootstrap_stage=AppBootstrapStage.BOOTSTRAP_CAPTURING,
                expected_bootstrap_row_version=persisted.bootstrap_row_version or 0,
                expected_session_row_version=session_snapshot.row_version,
                workflow_lease_owner="worker-a",
                workflow_lease_token=ids["token_a"],
            )
        activated = await sessions.activate_from_bootstrap(
            handoff=handoff,
            expected_bootstrap_stage=AppBootstrapStage.BOOTSTRAP_CAPTURING,
            expected_bootstrap_row_version=persisted.bootstrap_row_version or 0,
            expected_session_row_version=session_snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_b"],
        )

        assert activated.status.value == "active"
        assert (
            await connection.fetchval(
                "SELECT bootstrap_capture_id::text FROM qc_app_bootstrap WHERE session_id = $1",
                ids["session"],
            )
            == persisted.capture.capture_id
        )
        await _assert_no_orphan_pngs(
            root=tmp_path / "evidence", connection=connection, task_id=ids["task"]
        )


async def test_postgres_replayed_capture_and_visual_observation_are_idempotent(
    tmp_path: Path,
) -> None:
    """同一 CaptureCommitted 重放不会新增 Capture、Artifact 或视觉观察。"""
    async with _app_database() as (connection, pool, ids):
        store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
        bootstrap = PostgresAppBootstrapRepository(pool=pool)
        captures = AppCapturePersistenceService(
            repository=PostgresAppCaptureRepository(pool=pool),
            artifact_store=store,
            manifest_repository=PostgresEvidenceManifestRepository(pool=pool),
        )
        snapshot = await _start_and_claim(connection, pool, ids, owner="worker-a")
        snapshot = await _advance_to_bootstrap_capturing(bootstrap, ids, snapshot, "worker-a")

        first = await captures.persist_bootstrap(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            artifacts=_screenshot_artifacts(ids),
            expected_bootstrap_row_version=snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_a"],
        )
        replayed = await captures.persist_bootstrap(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            artifacts=_screenshot_artifacts(ids),
            expected_bootstrap_row_version=snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_a"],
        )
        analysis = AppVisualAnalysis.build(
            capture_id=first.capture.capture_id,
            screenshot_artifact_id=first.screenshot.artifact_id,
            image_signature=first.image_signature,
            width=first.width,
            height=first.height,
            orientation=first.orientation,
            visual_page_type="home",
            visual_summary="首页首屏截图",
            key_elements=("主页面",),
            candidates=(),
            provider_id="postgres-integration",
            model_id="test-model",
            package_name=first.package_name,
            activity=first.activity,
            page_state="stable",
            overlay_signature="overlay:dialog-1",
            surface_facts={"dialog": {"buttons": ["close"]}},
        )
        observations = PostgresAppVisualObservationRepository(pool=pool)
        created = await observations.save(analysis)
        replayed_observation = await observations.save(analysis)
        loaded_observation = await observations.get_by_capture_id(capture_id=analysis.capture_id)

        assert replayed.capture.capture_id == first.capture.capture_id
        assert created.created is True
        assert replayed_observation.created is False
        assert loaded_observation is not None
        assert loaded_observation.analysis == analysis
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_capture WHERE task_id = $1::uuid", ids["task"]
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_evidence_artifact WHERE task_id = $1::uuid",
                ids["task"],
            )
            == 1
        )
        assert (
            await connection.fetchval(
                """
                SELECT count(*)
                FROM qc_app_capture AS app_capture
                INNER JOIN qc_capture AS capture ON capture.id = app_capture.capture_id
                WHERE capture.task_id = $1::uuid
                """,
                ids["task"],
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_app_visual_observation WHERE task_id = $1::uuid",
                ids["task"],
            )
            == 1
        )
        graph = await PostgresPageGraphRepository(pool=pool).get_graph(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )
        assert graph["status"] == "available"
        assert graph["counts"]["projected_states"] == 1
        assert graph["counts"]["projected_transitions"] == 0
        await _assert_no_orphan_pngs(
            root=tmp_path / "evidence", connection=connection, task_id=ids["task"]
        )


async def test_postgres_app_exploration_replay_and_double_worker_claim(
    tmp_path: Path,
) -> None:
    """真实 App Capture 的 Work 重放只落一条，并且双 Worker 只能领取一次。"""
    async with _app_database() as (connection, pool, ids):
        bootstrap = PostgresAppBootstrapRepository(pool=pool)
        captures = AppCapturePersistenceService(
            repository=PostgresAppCaptureRepository(pool=pool),
            artifact_store=LocalEvidenceArtifactStore(root=tmp_path / "evidence"),
            manifest_repository=PostgresEvidenceManifestRepository(pool=pool),
        )
        snapshot = await _start_and_claim(connection, pool, ids, owner="worker-a")
        snapshot = await _advance_to_bootstrap_capturing(
            bootstrap,
            ids,
            snapshot,
            "worker-a",
        )
        persisted = await captures.persist_bootstrap(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            artifacts=_screenshot_artifacts(ids),
            expected_bootstrap_row_version=snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_a"],
        )
        work = _app_exploration_work(ids, capture=persisted.capture)
        repository = PostgresExplorerWorkRepository(pool=pool)

        first = await repository.publish(work)
        replay = await repository.publish(work)
        explorer_token_a = str(uuid4())
        explorer_token_b = str(uuid4())
        claims = await asyncio.gather(
            repository.claim(
                command=ClaimExplorerWorkCommand(
                    work_type=ExplorerWorkType.EXPLORATION,
                    lease_owner="explorer-worker-a",
                    task_id=ids["task"],
                ),
                lease_token=explorer_token_a,
            ),
            repository.claim(
                command=ClaimExplorerWorkCommand(
                    work_type=ExplorerWorkType.EXPLORATION,
                    lease_owner="explorer-worker-b",
                    task_id=ids["task"],
                ),
                lease_token=explorer_token_b,
            ),
        )

        assert first.created is True
        assert replay.created is False
        claimed = [item for item in claims if item is not None]
        assert len(claimed) == 1
        assert claimed[0].work == work
        winner_index = 0 if claims[0] is not None else 1
        winner_owner = f"explorer-worker-{'a' if winner_index == 0 else 'b'}"
        winner_lease = claimed[0]
        running_version = await repository.transition(
            command=TransitionExplorerWorkCommand(
                work_id=work.work_id,
                expected_status=ExplorerWorkStatus.LEASED,
                target_status=ExplorerWorkStatus.RUNNING,
                lease_token=winner_lease.lease_token,
                row_version=winner_lease.row_version,
            )
        )
        await repository.renew(
            command=RenewExplorerWorkLeaseCommand(
                work_id=work.work_id,
                lease_owner=winner_owner,
                lease_token=winner_lease.lease_token,
                lease_seconds=180,
            )
        )
        successor = _app_successor_work(parent=work, capture=persisted.capture)
        committed = await repository.commit_successor(
            command=CommitExplorerWorkSuccessorCommand(
                parent_work_id=work.work_id,
                task_id=ids["task"],
                session_id=ids["session"],
                lease_owner=winner_owner,
                lease_token=winner_lease.lease_token,
                row_version=running_version,
                successor=successor,
            )
        )

        assert committed.successor_work_id == successor.work_id
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_v2_work_item WHERE work_id = $1",
                work.work_id,
            )
            == "succeeded"
        )
        successor_lease = await repository.claim(
            command=ClaimExplorerWorkCommand(
                work_type=ExplorerWorkType.EXPLORATION,
                lease_owner="explorer-worker-c",
                task_id=ids["task"],
            ),
            lease_token=str(uuid4()),
        )
        assert successor_lease is not None
        assert successor_lease.work == successor
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_v2_work_item WHERE work_id = $1",
                work.work_id,
            )
            == 1
        )


async def test_postgres_app_global_frontier_keeps_siblings_and_reclaims_expired_lease(
    tmp_path: Path,
) -> None:
    """A/B/C 安全候选同 Run 去重，已访问 A 后 B/C 仍可领取。"""

    async with _app_database() as (connection, pool, ids):
        bootstrap = PostgresAppBootstrapRepository(pool=pool)
        captures = AppCapturePersistenceService(
            repository=PostgresAppCaptureRepository(pool=pool),
            artifact_store=LocalEvidenceArtifactStore(root=tmp_path / "evidence"),
            manifest_repository=PostgresEvidenceManifestRepository(pool=pool),
        )
        snapshot = await _start_and_claim(connection, pool, ids, owner="worker-a")
        snapshot = await _advance_to_bootstrap_capturing(
            bootstrap,
            ids,
            snapshot,
            "worker-a",
        )
        persisted = await captures.persist_bootstrap(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            artifacts=_screenshot_artifacts(ids),
            expected_bootstrap_row_version=snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_a"],
        )
        sessions = PostgresSurfaceSessionRepository(pool=pool)
        session_snapshot = await sessions.get(session_id=ids["session"])
        assert session_snapshot is not None
        await sessions.activate_from_bootstrap(
            handoff=_handoff(
                ids,
                persisted.capture,
                persisted.bootstrap_row_version or 0,
            ),
            expected_bootstrap_stage=AppBootstrapStage.BOOTSTRAP_CAPTURING,
            expected_bootstrap_row_version=persisted.bootstrap_row_version or 0,
            expected_session_row_version=session_snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_a"],
        )
        work = _app_global_frontier_work(ids, capture=persisted.capture)
        await PostgresExplorerWorkRepository(pool=pool).publish(work)
        repository = PostgresAppFrontierRepository(pool=pool)
        global_frontier = AppGlobalFrontierService(repository=repository)

        first = await global_frontier.register_work_candidates(
            work=work,
            agent_run_id=ids["run"],
        )
        replay = await global_frontier.register_work_candidates(
            work=work,
            agent_run_id=ids["run"],
        )

        assert len(first) == 3
        assert replay == first
        assert (await global_frontier.counts(work=work, agent_run_id=ids["run"])).pending == 3

        first_lease = await repository.claim(
            command=ClaimAppFrontierCommand(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                lease_owner="frontier-worker-a",
                lease_seconds=180,
                frontier_item_id=first[0].frontier_item_id,
            ),
            lease_token=str(uuid4()),
        )
        assert first_lease is not None
        running_version = await repository.transition(
            command=TransitionAppFrontierCommand(
                frontier_item_id=first_lease.item.frontier_item_id,
                expected_status=AppFrontierStatus.LEASED,
                target_status=AppFrontierStatus.RUNNING,
                lease_token=first_lease.lease_token,
                row_version=first_lease.row_version,
            )
        )
        await repository.transition(
            command=TransitionAppFrontierCommand(
                frontier_item_id=first_lease.item.frontier_item_id,
                expected_status=AppFrontierStatus.RUNNING,
                target_status=AppFrontierStatus.VISITED,
                lease_token=first_lease.lease_token,
                row_version=running_version,
                result_capture_id=persisted.capture.capture_id,
            )
        )
        counts = await global_frontier.counts(work=work, agent_run_id=ids["run"])
        assert counts.visited == 1
        assert counts.pending == 2
        assert counts.exhausted is False

        expiring = await repository.claim(
            command=ClaimAppFrontierCommand(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                lease_owner="frontier-worker-b",
                lease_seconds=1,
            ),
            lease_token=str(uuid4()),
        )
        assert expiring is not None
        await connection.execute(
            "UPDATE qc_app_exploration_frontier "
            "SET lease_expires_at = now() - interval '1 second' "
            "WHERE frontier_item_id = $1",
            expiring.item.frontier_item_id,
        )
        reclaimed = await repository.claim(
            command=ClaimAppFrontierCommand(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                lease_owner="frontier-worker-c",
                lease_seconds=180,
                frontier_item_id=expiring.item.frontier_item_id,
            ),
            lease_token=str(uuid4()),
        )
        assert reclaimed is not None
        assert reclaimed.item.frontier_item_id == expiring.item.frontier_item_id
        assert reclaimed.attempt_count == expiring.attempt_count + 1
        assert (
            await connection.fetchval(
                """
                SELECT count(*)
                FROM qc_outbox_event
                WHERE aggregate_type = 'explorer_work'
                  AND aggregate_id = $1
                """,
                work.work_id,
            )
            == 1
        )


async def test_postgres_workflow_lease_loss_stops_external_calls_and_capture(
    tmp_path: Path,
) -> None:
    """真实租约失效后，Workflow 不得继续触发 Adapter 或写入首屏事实。"""
    async with _app_database() as (connection, pool, ids):
        renewal_failed = asyncio.Event()
        bootstrap = _SecondRenewalFails(
            PostgresAppBootstrapRepository(pool=pool),
            renewal_failed=renewal_failed,
        )
        runtime = _LeaseCountingRuntime(renewal_failed=renewal_failed)
        captures = AppCapturePersistenceService(
            repository=PostgresAppCaptureRepository(pool=pool),
            artifact_store=LocalEvidenceArtifactStore(root=tmp_path / "evidence"),
            manifest_repository=PostgresEvidenceManifestRepository(pool=pool),
        )
        workflow = AppQcWorkflow(
            platform_builds=_WorkflowBuildReader(ids),
            surface_sessions=PostgresSurfaceSessionRepository(pool=pool),
            bootstraps=bootstrap,
            bootstrap_port=runtime,
            driver=runtime,
            captures=captures,
            publisher=_NoopPublisher(),
        )

        with pytest.raises(AppBootstrapLeaseLostError):
            await workflow.run(
                RunAppWorkflowCommand(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    session_id=ids["session"],
                    platform_build_ref=ids["build_ref"],
                    lease_owner="worker-a",
                    lease_token=ids["token_a"],
                    lease_seconds=1,
                    session_owner="app-postgres-workflow",
                )
            )

        events_after_failure = tuple(runtime.events)
        await asyncio.sleep(0.05)
        assert tuple(runtime.events) == events_after_failure
        assert events_after_failure == ("capabilities", "start_session", "install", "open")
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_capture WHERE task_id = $1::uuid", ids["task"]
            )
            == 0
        )
        assert (
            await connection.fetchval(
                """
            SELECT count(*)
            FROM qc_app_capture AS app_capture
            INNER JOIN qc_capture AS capture ON capture.id = app_capture.capture_id
            WHERE capture.task_id = $1::uuid
            """,
                ids["task"],
            )
            == 0
        )


@asynccontextmanager
async def _app_database() -> AsyncIterator[tuple[asyncpg.Connection, asyncpg.Pool, dict[str, str]]]:
    """为每个案例隔离一组任务事实，结束时从 ``qc_task`` 级联清理。"""
    ids = _ids()
    database_url = _test_database_url()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_app_task(connection, ids)
        yield connection, pool, ids
    finally:
        await connection.execute(
            """
            DELETE FROM qc_app_capture
            WHERE capture_id IN (
                SELECT id
                FROM qc_capture
                WHERE task_id = $1::uuid
            )
            """,
            ids["task"],
        )
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


def _ids() -> dict[str, str]:
    ids = {
        name: str(uuid4())
        for name in ("tenant", "user", "ruleset", "task", "run", "token_a", "token_b")
    }
    ids["session"] = f"app-session-{uuid4()}"
    ids["build_ref"] = f"app-build:{uuid4()}"
    return ids


def _test_database_url() -> str:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    return require_isolated_test_database_url(
        database_url,
        os.getenv("ALIOTH_DATABASE_URL"),
    )


async def _seed_app_task(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $2, 'App PostgreSQL Integration')
        """,
        ids["tenant"],
        f"app-postgres-{ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (
            id, username, full_name, password_hash, role, tenant_id
        )
        VALUES (
            $1::uuid, $2, 'App PostgreSQL Integration', 'not-a-login-secret',
            '平台管理员', $3::uuid
        )
        """,
        ids["user"],
        f"app-postgres-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        )
        VALUES ($1::uuid, $2, 'App Integration', 1, 'enabled', 'app-test', 0, 'manual')
        """,
        ids["ruleset"],
        f"app-integration-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, target_platform,
            submitter_id, tenant_id, task_config
        )
        VALUES (
            $1::uuid, NULL, $2::uuid, 'running', 'app', $3::uuid, $4::uuid,
            jsonb_build_object('app', jsonb_build_object('platform_build_ref', $5::text))
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
        VALUES ($1::uuid, $2::uuid, 'main', 'App PostgreSQL Integration', 'running')
        """,
        ids["run"],
        ids["task"],
    )


async def _start_and_claim(
    connection: asyncpg.Connection,
    pool: asyncpg.Pool,
    ids: dict[str, str],
    *,
    owner: str,
) -> object:
    del connection
    await _register_starting_session(pool, ids)
    return await _claim(
        PostgresAppBootstrapRepository(pool=pool),
        ids,
        owner=owner,
        token_key="token_a",
    )


async def _register_starting_session(pool: asyncpg.Pool, ids: dict[str, str]) -> None:
    sessions = PostgresSurfaceSessionRepository(pool=pool)
    await sessions.register_starting(
        spec=SurfaceSessionSpec(
            task_id=ids["task"],
            session_id=ids["session"],
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref=ids["build_ref"],
        ),
        session_owner="app-postgres-integration",
        primary_surface_ref=app_primary_surface_ref(ids["session"]),
    )


async def _claim(
    repository: PostgresAppBootstrapRepository,
    ids: dict[str, str],
    *,
    owner: str,
    token_key: str,
):
    return await repository.get_or_create(
        task_id=ids["task"],
        session_id=ids["session"],
        platform_build_ref=ids["build_ref"],
        lease_owner=owner,
        lease_token=ids[token_key],
        lease_seconds=120,
    )


async def _advance_to_bootstrap_capturing(
    repository: PostgresAppBootstrapRepository,
    ids: dict[str, str],
    snapshot: AppBootstrapSnapshot,
    owner: str,
):
    """按生产状态机推进到可写入首屏 Capture 的唯一阶段。"""
    return await _advance_to_stage(
        repository,
        ids,
        snapshot,
        owner,
        target_stage=AppBootstrapStage.BOOTSTRAP_CAPTURING,
    )


async def _advance_to_stage(
    repository: PostgresAppBootstrapRepository,
    ids: dict[str, str],
    snapshot: AppBootstrapSnapshot,
    owner: str,
    *,
    target_stage: AppBootstrapStage,
) -> AppBootstrapSnapshot:
    """按生产合法跃迁推进到指定阶段，便于精确验证 owner/token 门禁。"""
    current = snapshot
    transitions = (
        (AppBootstrapStage.ACQUIRING_SESSION, {}),
        (AppBootstrapStage.RESOLVING_BUILD, {}),
        (
            AppBootstrapStage.INSTALLING,
            {"install_request_id": f"install:{ids['session']}", "install_attempt": 1},
        ),
        (
            AppBootstrapStage.INSTALLED,
            {
                "install_request_id": f"install:{ids['session']}",
                "installed_package": "com.example.integration",
            },
        ),
        (
            AppBootstrapStage.OPENING,
            {"open_request_id": f"open:{ids['session']}", "open_attempt": 1},
        ),
        (AppBootstrapStage.OPENED, {"open_request_id": f"open:{ids['session']}"}),
        (AppBootstrapStage.BOOTSTRAP_CAPTURING, {}),
    )
    for target, updates in transitions:
        current = await repository.transition(
            TransitionAppBootstrapCommand(
                task_id=ids["task"],
                session_id=ids["session"],
                platform_build_ref=ids["build_ref"],
                expected_stage=current.stage,
                target_stage=target,
                expected_row_version=current.row_version,
                lease_owner=owner,
                lease_token=ids["token_a"] if owner == "worker-a" else ids["token_b"],
                **updates,
            )
        )
        if current.stage is target_stage:
            return current
    raise AssertionError(f"未找到到达 {target_stage.value} 的 App Bootstrap 路径")


async def _transition_with_identity(
    repository: PostgresAppBootstrapRepository,
    ids: dict[str, str],
    *,
    snapshot: AppBootstrapSnapshot,
    owner: str,
    token_key: str,
) -> AppBootstrapSnapshot:
    """只替换 owner/token，其余阶段与版本均保持当前合法值。"""
    return await repository.transition(
        TransitionAppBootstrapCommand(
            task_id=ids["task"],
            session_id=ids["session"],
            platform_build_ref=ids["build_ref"],
            expected_stage=snapshot.stage,
            target_stage=AppBootstrapStage.ACQUIRING_SESSION,
            expected_row_version=snapshot.row_version,
            lease_owner=owner,
            lease_token=ids[token_key],
        )
    )


async def _capture_error(
    *,
    captures: AppCapturePersistenceService,
    ids: dict[str, str],
    expected_row_version: int,
    owner: str,
    token: str,
) -> BaseException:
    try:
        await captures.persist_bootstrap(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            artifacts=_screenshot_artifacts(ids),
            expected_bootstrap_row_version=expected_row_version,
            workflow_lease_owner=owner,
            workflow_lease_token=token,
        )
    except BaseException as exc:  # noqa: BLE001 - 断言真实持久化拒绝类型
        return exc
    raise AssertionError("旧 Worker 不应提交 App Capture")


def _screenshot_artifacts(ids: dict[str, str]) -> RawArtifactSet:
    return RawArtifactSet(
        task_id=ids["task"],
        session_id=ids["session"],
        surface_ref=app_primary_surface_ref(ids["session"]),
        artifacts=(RawArtifact(artifact_type="screenshot", content=_valid_png()),),
        metadata={
            "width": 1,
            "height": 1,
            "orientation": "portrait",
            "package_name": "com.example.integration",
            "activity": ".MainActivity",
            "device_snapshot": {"battery": 100},
        },
    )


def _app_exploration_work(ids: dict[str, str], *, capture: CaptureRef):
    observed_at = datetime.now(UTC)
    frontier = FrontierSnapshot(
        snapshot_id=f"frontier:{capture.capture_id}",
        observation=SurfaceObservation(
            capture_id=capture.capture_id,
            surface_kind=SurfaceKind.ANDROID,
            location="app://com.example.integration/.MainActivity",
            state_version="sha256:" + "a" * 64,
            observed_at=observed_at,
            metadata={"screenshot_artifact_id": capture.artifact_ids[0]},
        ),
        candidates=(
            CandidateAction(
                candidate_id=f"candidate:{capture.capture_id}",
                semantic_target="主要操作按钮",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=90,
                navigation_depth=1,
                expected_surface_kind=SurfaceKind.ANDROID,
            ),
        ),
    )
    return build_exploration_work(
        task_id=ids["task"],
        session_id=ids["session"],
        branch_id=f"app:{capture.capture_id}",
        objective="探索 App 关键页面",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=capture.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.example.integration",),
            allowed_operations=(InteractionOperation.CLICK,),
            max_navigation_depth=3,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=3,
            remaining_actions=3,
            max_depth=3,
            remaining_duration_ms=30_000,
        ),
        deadline=observed_at + timedelta(minutes=5),
    )


def _app_global_frontier_work(ids: dict[str, str], *, capture: CaptureRef):
    observed_at = datetime.now(UTC)
    candidates = tuple(
        CandidateAction(
            candidate_id=f"candidate-{name}:{capture.capture_id}",
            semantic_target=target,
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=value,
            navigation_depth=1,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={
                "candidate_id": f"candidate-{name}:{capture.capture_id}",
                "purpose": f"进入{target}",
                "expected_state_hint": target,
                "frontier_scope": "app",
            },
        )
        for name, target, value in (
            ("a", "首页 Tab", 95),
            ("b", "消息 Tab", 90),
            ("c", "个人中心 Tab", 85),
        )
    )
    frontier = FrontierSnapshot(
        snapshot_id=f"frontier:global:{capture.capture_id}",
        observation=SurfaceObservation(
            capture_id=capture.capture_id,
            surface_kind=SurfaceKind.ANDROID,
            location="app://com.example.integration/.MainActivity",
            state_version="sha256:" + "c" * 64,
            observed_at=observed_at,
            page_type="home",
            page_state="stable",
            metadata={
                "screenshot_artifact_id": capture.artifact_ids[0],
                "business_page_identity": "page:home",
                "app_navigation_path": ["page:app_ready", "page:home"],
            },
        ),
        candidates=candidates,
    )
    return build_exploration_work(
        task_id=ids["task"],
        session_id=ids["session"],
        branch_id=f"app:global:{capture.capture_id}",
        objective="探索 App 任务级页面分支",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=capture.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.example.integration",),
            allowed_operations=(InteractionOperation.CLICK,),
            max_navigation_depth=3,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=6,
            remaining_actions=6,
            max_depth=3,
            remaining_duration_ms=60_000,
        ),
        deadline=observed_at + timedelta(minutes=5),
    )


def _app_successor_work(*, parent, capture: CaptureRef):
    observed_at = parent.frontier.observation.observed_at + timedelta(milliseconds=10)
    frontier = FrontierSnapshot(
        snapshot_id=f"frontier:successor:{capture.capture_id}",
        observation=SurfaceObservation(
            capture_id=capture.capture_id,
            surface_kind=parent.surface_kind,
            location="app://com.example.integration/.MainActivity",
            state_version="sha256:" + "b" * 64,
            observed_at=observed_at,
            metadata={
                "screenshot_artifact_id": capture.artifact_ids[0],
                "parent_work_id": parent.work_id,
                "parent_branch_id": parent.branch_id,
                "parent_capture_id": parent.source_capture_id,
            },
        ),
        candidates=(),
        visited_state_ids=(parent.frontier.observation.state_version,),
    )
    return build_exploration_work(
        task_id=parent.task_id,
        session_id=parent.session_id,
        branch_id=f"app:successor:{capture.capture_id}",
        objective=parent.objective,
        surface_kind=parent.surface_kind,
        source_capture_id=capture.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=parent.boundaries,
        budget=ExplorationBudget(
            max_actions=parent.budget.max_actions,
            remaining_actions=parent.budget.remaining_actions - 1,
            max_depth=parent.budget.max_depth,
            remaining_duration_ms=parent.budget.remaining_duration_ms - 10,
        ),
        deadline=parent.deadline,
    )


def _handoff(
    ids: dict[str, str],
    capture: CaptureRef,
    row_version: int,
) -> AppReadyHandoff:
    return AppReadyHandoff(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        session_id=ids["session"],
        installed_package="com.example.integration",
        capture=capture,
        capability_manifest=build_android_screenshot_only_manifest(
            adapter_id="postgres-integration",
            adapter_version="test",
            backend="test",
        ),
        bootstrap_row_version=row_version + 1,
    )


class _SecondRenewalFails:
    """首个续租真实写库，第二个续租模拟 Worker 已失去数据库租约。"""

    def __init__(
        self,
        repository: PostgresAppBootstrapRepository,
        *,
        renewal_failed: asyncio.Event,
    ) -> None:
        self._repository = repository
        self._renewal_failed = renewal_failed
        self._renewal_count = 0

    async def get_or_create(self, **kwargs: Any) -> AppBootstrapSnapshot:
        return await self._repository.get_or_create(**kwargs)

    async def get(self, **kwargs: Any) -> AppBootstrapSnapshot | None:
        return await self._repository.get(**kwargs)

    async def renew(self, command: RenewAppBootstrapLeaseCommand) -> AppBootstrapSnapshot:
        self._renewal_count += 1
        if self._renewal_count >= 2:
            self._renewal_failed.set()
            raise AppBootstrapPersistenceError("测试模拟：续租已失效")
        return await self._repository.renew(command)

    async def transition(
        self,
        command: TransitionAppBootstrapCommand,
    ) -> AppBootstrapSnapshot:
        return await self._repository.transition(command)

    async def fail(self, command: FailAppBootstrapCommand) -> AppBootstrapSnapshot:
        return await self._repository.fail(command)

    async def cancel(self, command: CancelAppBootstrapCommand) -> AppBootstrapSnapshot:
        return await self._repository.cancel(command)


class _WorkflowBuildReader:
    """向 Workflow 提供已 verified 的测试 APK，不把 APK 二进制带入测试消息。"""

    def __init__(self, ids: dict[str, str]) -> None:
        self._build = PlatformBuild(
            build_id=ids["build_ref"].removeprefix("app-build:"),
            platform="android",
            original_filename="integration.apk",
            storage_path="builds/integration.apk",
            sha256="a" * 64,
            size_bytes=1,
            status=PlatformBuildStatus.VERIFIED,
            created_by=ids["run"],
            package_name="com.example.integration",
        )

    async def get_verified_by_ref(self, platform_build_ref: str) -> PlatformBuild | None:
        if platform_build_ref == self._build.platform_build_ref:
            return self._build
        return None


class _LeaseCountingRuntime:
    """记录所有 Adapter 边界调用；打开后等待后台续租失败再返回。"""

    def __init__(self, *, renewal_failed: asyncio.Event) -> None:
        self._renewal_failed = renewal_failed
        self.events: list[str] = []
        self._manifest = build_android_screenshot_only_manifest(
            adapter_id="postgres-lease-runtime",
            adapter_version="test",
            backend="fake",
        )

    def describe_capabilities(self):
        self.events.append("capabilities")
        return self._manifest

    async def start_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef:
        self.events.append("start_session")
        return SurfaceSessionRef(
            task_id=spec.task_id,
            session_id=spec.session_id,
            surface_kind=spec.surface_kind,
            primary_surface_ref=app_primary_surface_ref(spec.session_id),
        )

    async def ensure_installed(self, request: AppInstallRequest) -> AppInstallResult:
        self.events.append("install")
        return AppInstallResult(
            request_id="install-result",
            session_id=request.session_id,
            succeeded=True,
            retryable=False,
            installed_package=request.package_name,
        )

    async def open_app(self, request: AppOpenRequest) -> AppOpenResult:
        self.events.append("open")
        await asyncio.wait_for(self._renewal_failed.wait(), timeout=3)
        return AppOpenResult(
            request_id="open-result",
            session_id=request.session_id,
            succeeded=True,
            retryable=False,
            package_name=request.package_name,
            foreground_verified=True,
            activity=".MainActivity",
        )

    async def capture(self, **kwargs: Any) -> RawArtifactSet:
        self.events.append("capture")
        session = kwargs["session"]
        return RawArtifactSet(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_ref=session.primary_surface_ref,
            artifacts=(RawArtifact(artifact_type="screenshot", content=_valid_png()),),
        )

    async def close_session(self, **kwargs: Any) -> None:
        del kwargs
        self.events.append("close")

    async def observe(self, **kwargs: Any) -> None:
        del kwargs
        raise AssertionError("App Bootstrap 不应调用 observe")


class _NoopPublisher:
    async def publish(self, handoff: AppReadyHandoff) -> None:
        del handoff


async def _assert_no_orphan_pngs(
    *,
    root: Path,
    connection: asyncpg.Connection,
    task_id: str,
) -> None:
    referenced = {
        str(row["storage_path"])
        for row in await connection.fetch(
            "SELECT storage_path FROM qc_evidence_artifact WHERE task_id = $1::uuid",
            task_id,
        )
    }
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*.png") if path.is_file()}
    orphan_count = len(actual - referenced)
    if orphan_count:
        shutil.rmtree(root, ignore_errors=True)
    assert orphan_count == 0


def _valid_png() -> bytes:
    """1x1 PNG：仅为真实 PNG 二进制持久化验收提供稳定测试素材。"""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\xf8\x9f\xa7\x00\x00\x00\x00IEND\xaeB`\x82"
    )
