"""真实 PostgreSQL 验证视觉登录事实晚到、重新匹配与封存不可变。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.agents.runtime.business_middlewares.base import TaskRuntimeSnapshot
from services.api.app.application import app_inspection_publisher as publisher_module
from services.api.app.application.app_capture_persistence import AppCapturePersistenceService
from services.api.app.application.app_inspection_publisher import (
    AppCaptureInspectionPublisher,
    PostgresAppCaptureInspectionAnnotator,
)
from services.api.app.application.inspection_work_items import InspectionWorkItemService
from services.api.app.application.task_query import TaskQueryService
from services.api.app.domain.exploration import AppVisualAnalysis
from services.api.app.infrastructure.db.app_bootstrap_repository import (
    PostgresAppBootstrapRepository,
)
from services.api.app.infrastructure.db.app_capture_repository import PostgresAppCaptureRepository
from services.api.app.infrastructure.db.app_live_inspection_repository import (
    PostgresAppLiveInspectionRepository,
)
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    AppVisualObservationPersistenceError,
    PostgresAppVisualObservationRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_evidence_index_postgres_integration import (
    _gap,
    _insert_building_index,
    _seal,
    _SingleConnectionPool,
)
from services.api.tests.test_app_postgres_integration import (
    _advance_to_bootstrap_capturing,
    _ids,
    _screenshot_artifacts,
    _seed_app_task,
    _start_and_claim,
    _test_database_url,
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)


@asynccontextmanager
async def _database(tmp_path, *, driver_auth=None):
    connection = await asyncpg.connect(_test_database_url())
    transaction = connection.transaction()
    await transaction.start()
    pool = _SingleConnectionPool(connection)
    ids = _ids()
    try:
        await _seed_app_task(connection, ids)
        store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
        captures = AppCapturePersistenceService(
            repository=PostgresAppCaptureRepository(pool=pool, page_graph_repository=None),
            artifact_store=store,
            manifest_repository=PostgresEvidenceManifestRepository(pool=pool),
        )
        snapshot = await _start_and_claim(connection, pool, ids, owner="worker-a")
        snapshot = await _advance_to_bootstrap_capturing(
            PostgresAppBootstrapRepository(pool=pool), ids, snapshot, "worker-a"
        )
        artifacts = _screenshot_artifacts(ids)
        if driver_auth is not None:
            artifacts = replace(
                artifacts, metadata={**artifacts.metadata, "session_auth": driver_auth}
            )
        saved = await captures.persist_bootstrap(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            artifacts=artifacts,
            expected_bootstrap_row_version=snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_a"],
        )
        await connection.execute(
            "UPDATE qc_surface_session SET status='active', activated_at=now() WHERE session_id=$1",
            ids["session"],
        )
        await connection.execute(
            "UPDATE qc_task SET task_config=task_config || "
            '\'{"execution_mode":"full_inspection"}\'::jsonb WHERE id=$1::uuid',
            ids["task"],
        )
        yield connection, pool, ids, saved
    finally:
        await transaction.rollback()
        await connection.close()


def _analysis(saved, facts):
    return AppVisualAnalysis.build(
        capture_id=saved.capture_id,
        screenshot_artifact_id=saved.screenshot.artifact_id,
        image_signature=saved.image_signature,
        width=saved.width,
        height=saved.height,
        orientation=saved.orientation,
        visual_page_type="home",
        visual_summary="账号状态证据",
        key_elements=("退出登录",),
        candidates=(),
        provider_id="controlled-auth-test",
        model_id="controlled-payload",
        page_state="stable",
        surface_facts=facts,
    )


async def _metadata(connection, capture_id):
    import json

    raw = await connection.fetchval("SELECT metadata FROM qc_capture WHERE id=$1::uuid", capture_id)
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"session_auth": "authenticated"}, "authenticated"),
        ({"login_form_visible": True}, "guest"),
        ({"auth_gate_visible": True, "requires_auth": False}, "unknown"),
        ({}, "unknown"),
        ({"session_auth": "authenticated", "login_form_visible": True}, "unknown"),
    ],
)
async def test_visual_save_projects_only_explicit_same_capture_auth(tmp_path, facts, expected):
    async with _database(tmp_path) as (connection, pool, _ids_value, saved):
        observations = PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None)
        assert (await _metadata(connection, saved.capture_id))["session_auth"] == "unknown"

        await observations.save(_analysis(saved, facts))

        assert (await _metadata(connection, saved.capture_id))["session_auth"] == expected


@pytest.mark.parametrize(
    ("driver_auth", "expected", "blocked"),
    [
        ("authenticated", "authenticated", "guest"),
        ("logged_out", "guest", "authenticated"),
        ("unknown", "unknown", "authenticated"),
        ("invalid", "unknown", "authenticated"),
    ],
)
@pytest.mark.parametrize("no_test_account", [True, False])
async def test_initial_driver_auth_reaches_capture_and_publisher_gate(
    tmp_path, monkeypatch, driver_auth, expected, blocked, no_test_account
):
    async with _database(tmp_path, driver_auth=driver_auth) as (connection, pool, ids, saved):
        await connection.execute(
            "UPDATE qc_task SET task_config=task_config || "
            "jsonb_build_object('no_test_account',$2::boolean) "
            "WHERE id=$1::uuid",
            ids["task"],
            no_test_account,
        )
        assert (await _metadata(connection, saved.capture_id))["session_auth"] == expected
        assert saved.screenshot.metadata["session_auth"] == expected

        async def get_pool():
            return pool

        monkeypatch.setattr(publisher_module, "get_postgres_pool", get_pool)
        publisher = object.__new__(AppCaptureInspectionPublisher)
        assert (
            await publisher._resolve_auth_gate(
                snapshot=SimpleNamespace(target_platform="app"),
                task_id=ids["task"],
                capture_id=saved.capture_id,
            )
            == blocked
        )


async def test_late_auth_fact_reopens_incremental_publish_and_replay_is_idempotent(
    tmp_path, monkeypatch
):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        rule_id = str(uuid4())
        await connection.execute(
            """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,
                                   status,check_point,applicable_page_types,input_contract)
               VALUES($1::uuid,$2::uuid,'auth-live','账号页交互','visual','llm','medium',
                      'enabled','检查账号页','["home"]'::jsonb,
                      jsonb_build_object('auth_requirement','authenticated',
                          'required_artifacts',jsonb_build_array('screenshot','action_trace')))""",
            rule_id,
            ids["ruleset"],
        )

        class SnapshotProvider:
            async def get_task_snapshot(self, task_id):
                return TaskRuntimeSnapshot(
                    task_id=task_id,
                    status="running",
                    ruleset_version=1,
                    ruleset_id=ids["ruleset"],
                    ruleset_snapshot_hash="app-test",
                    target_platform="app",
                )

        async def get_pool():
            return pool

        monkeypatch.setattr(publisher_module, "get_postgres_pool", get_pool)
        publisher = AppCaptureInspectionPublisher(
            task_query=TaskQueryService(repository=PostgresTaskRepository(pool=pool)),
            snapshot_provider=SnapshotProvider(),
            annotator=PostgresAppCaptureInspectionAnnotator(pool=pool),
            work_item_service=InspectionWorkItemService(
                repository=PostgresInspectionWorkItemRepository(pool=pool)
            ),
        )
        kwargs = dict(
            task_id=ids["task"],
            capture_id=saved.capture_id,
            agent_run_id=ids["run"],
            visual_page_type="home",
            active_session_only=True,
        )
        first = await publisher.publish_for_capture(**kwargs)
        assert first.status == "no_rules_applicable" and first.published_count == 0
        assert (await _metadata(connection, saved.capture_id))[
            "inspection_status"
        ] == "no_rules_applicable"

        observations = PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None)
        analysis = _analysis(saved, {"session_auth": "authenticated"})
        await observations.save(analysis)
        pending = await PostgresAppLiveInspectionRepository(pool=pool).list_pending_captures(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        assert [item.capture_id for item in pending] == [saved.capture_id]
        second = await publisher.publish_for_capture(**kwargs)
        assert second.status == "published" and second.published_count == 1
        after_publish = await _metadata(connection, saved.capture_id)
        assert after_publish["inspection_status"] == "published"

        await observations.save(analysis)
        assert await _metadata(connection, saved.capture_id) == after_publish
        assert not await PostgresAppLiveInspectionRepository(pool=pool).list_pending_captures(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        await publisher.publish_for_capture(**kwargs)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_inspection_work_item WHERE task_id=$1::uuid", ids["task"]
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT rule_id::text FROM qc_inspection_work_item_rule WHERE rule_id=$1::uuid",
                rule_id,
            )
            == rule_id
        )


async def test_visual_hash_conflict_cannot_replace_persisted_auth(tmp_path):
    async with _database(tmp_path) as (connection, pool, _ids_value, saved):
        observations = PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None)
        await observations.save(_analysis(saved, {}))
        with pytest.raises(AppVisualObservationPersistenceError):
            await observations.save(_analysis(saved, {"session_auth": "authenticated"}))
        assert (await _metadata(connection, saved.capture_id))["session_auth"] == "unknown"


async def test_saved_driver_auth_is_not_overwritten_by_visual_fact(tmp_path):
    async with _database(tmp_path) as (connection, pool, _ids_value, saved):
        await connection.execute(
            'UPDATE qc_capture SET metadata=metadata || \'{"session_auth":"guest"}\'::jsonb '
            "WHERE id=$1::uuid",
            saved.capture_id,
        )
        await PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None).save(
            _analysis(saved, {"session_auth": "authenticated"})
        )
        assert (await _metadata(connection, saved.capture_id))["session_auth"] == "guest"


async def test_sealed_index_keeps_auth_and_publication_metadata_immutable(tmp_path):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        index_id = await _insert_building_index(connection, ids)
        await _seal(
            connection,
            index_id=index_id,
            outcome="evidence_insufficient",
            gaps=(
                _gap(
                    capture_id=saved.capture_id,
                    semantic_role="bootstrap",
                    reason_code="auth_unknown",
                ),
            ),
        )
        before = await _metadata(connection, saved.capture_id)
        saved_visual = await PostgresAppVisualObservationRepository(
            pool=pool, page_graph_repository=None
        ).save(_analysis(saved, {"session_auth": "authenticated"}))
        assert saved_visual.created
        assert await _metadata(connection, saved.capture_id) == before


def _popup_without_page_type(saved, *, page_state: str, summary: str):
    return AppVisualAnalysis.build(
        capture_id=saved.capture_id,
        screenshot_artifact_id=saved.screenshot.artifact_id,
        image_signature=saved.image_signature,
        width=saved.width,
        height=saved.height,
        orientation=saved.orientation,
        visual_page_type=None,
        visual_summary=summary,
        key_elements=(),
        candidates=(),
        provider_id="controlled-auth-test",
        model_id="controlled-payload",
        page_state=page_state,
        package_name=saved.package_name,
        activity=saved.activity,
        surface_facts={},
    )


@pytest.mark.parametrize("page_state", ["popup", "modal", "overlay", "dialog"])
async def test_pending_captures_include_popup_without_page_type(tmp_path, page_state):
    """开屏弹窗常无 visual_page_type；遮挡态仍须进活会话发布（宿主回退挂规则）。"""

    async with _database(tmp_path) as (_connection, pool, ids, saved):
        await PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None).save(
            _popup_without_page_type(
                saved,
                page_state=page_state,
                summary="中央广告弹层遮挡，页型未给出",
            )
        )
        pending = await PostgresAppLiveInspectionRepository(pool=pool).list_pending_captures(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        assert [(item.capture_id, item.page_type, item.page_state) for item in pending] == [
            (saved.capture_id, None, page_state)
        ]
        drain = await PostgresAppLiveInspectionRepository(pool=pool).load_drain_state(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        assert drain.pending_capture_count == 1


async def test_pending_captures_still_require_page_type_when_stable(tmp_path):
    async with _database(tmp_path) as (_connection, pool, ids, saved):
        await PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None).save(
            _popup_without_page_type(
                saved,
                page_state="stable",
                summary="稳定帧但未识别页型",
            )
        )
        pending = await PostgresAppLiveInspectionRepository(pool=pool).list_pending_captures(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        assert pending == ()
