"""真实 PostgreSQL 验证视觉晚到重新触发页面规则，不借用封存或旧会话事实。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.api.app.agents.runtime.business_middlewares.base import TaskRuntimeSnapshot
from services.api.app.agents.runtime.inspection_wakeup import InspectionWakeupHandler
from services.api.app.application import app_inspection_publisher as publisher_module
from services.api.app.application.app_evidence_manifest import AppEvidenceManifestFinalizer
from services.api.app.application.app_inspection_publisher import (
    AppCaptureInspectionPublisher,
    PostgresAppCaptureInspectionAnnotator,
)
from services.api.app.application.app_live_inspection_publish import AppLiveInspectionPublishService
from services.api.app.application.inspection_work_items import InspectionWorkItemService
from services.api.app.application.task_query import TaskQueryService
from services.api.app.domain.evidence import (
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.infrastructure.db.app_live_inspection_repository import (
    PostgresAppLiveInspectionRepository,
)
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    PostgresAppVisualObservationRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.queue.redis_stream import RedisStreamWakeup
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_capture_session_auth_postgres import _analysis, _database
from services.api.tests.test_app_evidence_index_postgres_integration import (
    _gap,
    _insert_building_index,
    _seal,
)
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)


async def _enable_v2(connection, task_id):
    await connection.execute(
        "UPDATE qc_task SET task_config=task_config || "
        '\'{"workflow_version":"v2"}\'::jsonb WHERE id=$1::uuid',
        task_id,
    )


async def _visual_events(connection, task_id):
    return await connection.fetch(
        "SELECT * FROM qc_outbox_event WHERE task_id=$1::uuid "
        "AND aggregate_type='app_visual_observation' ORDER BY occurred_at, message_id",
        task_id,
    )


def _wakeup(event):
    return RedisStreamWakeup(
        stream_name="alioth:v2:inspection",
        stream_entry_id="1-0",
        message_id=event["message_id"],
        message_type=event["message_type"],
        task_id=str(event["task_id"]),
        aggregate_type=event["aggregate_type"],
        aggregate_id=event["aggregate_id"],
        aggregate_version=event["aggregate_version"],
        idempotency_key=event["idempotency_key"],
    )


async def test_manifest_wakeup_consumed_before_visual_still_publishes_live_rule(
    tmp_path, monkeypatch
):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        await _enable_v2(connection, ids["task"])
        rule_id = str(uuid4())
        await connection.execute(
            """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,
                   severity,status,check_point,applicable_page_types,input_contract)
               VALUES($1::uuid,$2::uuid,'visual-ready-live','页面交互','visual','llm',
                   'medium','enabled','检查当前页','["home"]'::jsonb,
                   '{"required_artifacts":["screenshot","action_trace"]}'::jsonb)""",
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
        service = InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository(pool=pool)
        )
        live = AppLiveInspectionPublishService(
            capture_reader=PostgresAppLiveInspectionRepository(pool=pool),
            publisher=AppCaptureInspectionPublisher(
                task_query=TaskQueryService(repository=PostgresTaskRepository(pool=pool)),
                snapshot_provider=SnapshotProvider(),
                annotator=PostgresAppCaptureInspectionAnnotator(pool=pool),
                work_item_service=service,
            ),
        )

        class Dispatcher:
            async def submit(self, *, task_id, consumer_agent_run_id, prepare_publication):
                assert prepare_publication
                await live.publish_pending(task_id=task_id, agent_run_id=consumer_agent_run_id)
                return True

        handler = InspectionWakeupHandler(service=service, consumer=Dispatcher())
        early = await connection.fetchrow(
            "SELECT * FROM qc_outbox_event WHERE task_id=$1::uuid "
            "AND message_type='alioth.v2.evidence-bundle.ready' LIMIT 1",
            ids["task"],
        )
        assert early is not None and await handler.handle(_wakeup(early))
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_inspection_work_item WHERE task_id=$1::uuid", ids["task"]
            )
            == 0
        )

        repository = PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None)
        analysis = _analysis(saved, {})
        await repository.save(analysis)
        events = await _visual_events(connection, ids["task"])
        assert len(events) == 1 and events[0]["message_id"] != early["message_id"]
        envelope = json.loads(events[0]["envelope_payload"])
        assert envelope["payload"]["capture_id"] == saved.capture_id
        assert await handler.handle(_wakeup(events[0]))
        row = await connection.fetchrow(
            "SELECT page_capture_id::text AS capture_id,session_dependency,status "
            "FROM qc_inspection_work_item WHERE task_id=$1::uuid",
            ids["task"],
        )
        assert dict(row) == {
            "capture_id": saved.capture_id,
            "session_dependency": "active_session",
            "status": "pending",
        }
        await repository.save(analysis)
        assert len(await _visual_events(connection, ids["task"])) == 1
        assert await handler.handle(_wakeup(events[0]))
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_inspection_work_item WHERE task_id=$1::uuid", ids["task"]
            )
            == 1
        )


async def test_visual_before_manifest_pass_relies_on_later_real_manifest_ready(tmp_path):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        await _enable_v2(connection, ids["task"])
        manifests = PostgresEvidenceManifestRepository(pool=pool)
        latest = await manifests.get_latest_for_capture(
            task_id=ids["task"], capture_id=saved.capture_id
        )
        pending = build_evidence_manifest_reference(
            task_id=latest.task_id,
            session_id=latest.session_id,
            capture_id=latest.capture_id,
            manifest_version=latest.manifest_version + 1,
            artifacts=latest.artifacts,
            observation_packet_ids=(),
            quality_status=EvidenceQualityStatus.PENDING,
            quality_reason="artifact_integrity_pending",
            created_at=datetime.now(UTC),
        )
        await manifests.add(manifest=pending)
        before = await connection.fetchval(
            "SELECT count(*) FROM qc_outbox_event WHERE task_id=$1::uuid "
            "AND message_type='alioth.v2.evidence-bundle.ready'",
            ids["task"],
        )
        await PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None).save(
            _analysis(saved, {})
        )
        assert await _visual_events(connection, ids["task"]) == []
        await AppEvidenceManifestFinalizer(
            repository=manifests,
            artifact_store=LocalEvidenceArtifactStore(root=tmp_path / "evidence"),
        ).finalize(pending)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_outbox_event WHERE task_id=$1::uuid "
                "AND message_type='alioth.v2.evidence-bundle.ready'",
                ids["task"],
            )
            == before + 1
        )
        assert (
            len(
                await PostgresAppLiveInspectionRepository(pool=pool).list_pending_captures(
                    task_id=ids["task"], agent_run_id=ids["run"]
                )
            )
            == 1
        )


async def test_visual_replay_after_new_manifest_does_not_rewrite_its_ready_event(tmp_path):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        await _enable_v2(connection, ids["task"])
        repository = PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None)
        analysis = _analysis(saved, {})
        await repository.save(analysis)
        original = await _visual_events(connection, ids["task"])
        assert len(original) == 1
        manifests = PostgresEvidenceManifestRepository(pool=pool)
        latest = await manifests.get_latest_for_capture(
            task_id=ids["task"], capture_id=saved.capture_id
        )
        await manifests.add(
            manifest=build_evidence_manifest_reference(
                task_id=latest.task_id,
                session_id=latest.session_id,
                capture_id=latest.capture_id,
                manifest_version=latest.manifest_version + 1,
                artifacts=latest.artifacts,
                observation_packet_ids=(),
                quality_status=EvidenceQualityStatus.PASS,
                quality_reason="integrity_verified",
                created_at=datetime.now(UTC),
            )
        )
        replay = await repository.save(analysis)
        assert not replay.created
        assert await _visual_events(connection, ids["task"]) == original


@pytest.mark.parametrize(
    "fence",
    [
        "sealed",
        "old_epoch",
        "old_capture_epoch",
        "closed_session",
        "failed_run",
        "exploration_only",
    ],
)
async def test_visual_ready_does_not_wake_noncurrent_or_closed_inspection(tmp_path, fence):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        await _enable_v2(connection, ids["task"])
        if fence == "sealed":
            index_id = await _insert_building_index(connection, ids)
            await _seal(
                connection,
                index_id=index_id,
                outcome="evidence_insufficient",
                gaps=(
                    _gap(
                        capture_id=saved.capture_id,
                        semantic_role="bootstrap",
                        reason_code="test_gap",
                    ),
                ),
            )
        elif fence == "old_epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1::uuid", ids["task"]
            )
        elif fence == "old_capture_epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1::uuid", ids["task"]
            )
            await connection.execute(
                "UPDATE qc_surface_session SET finalization_epoch=1 WHERE session_id=$1",
                ids["session"],
            )
        elif fence == "closed_session":
            await connection.execute(
                "UPDATE qc_surface_session SET status='closed',draining_at=now(),"
                "closed_at=now() WHERE session_id=$1",
                ids["session"],
            )
        elif fence == "failed_run":
            await connection.execute(
                "UPDATE qc_agent_run SET status='failed' WHERE id=$1::uuid", ids["run"]
            )
        elif fence == "exploration_only":
            await connection.execute(
                "UPDATE qc_task SET task_config=task_config || "
                '\'{"execution_mode":"exploration_only"}\'::jsonb WHERE id=$1::uuid',
                ids["task"],
            )
        await PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None).save(
            _analysis(saved, {})
        )
        assert await _visual_events(connection, ids["task"]) == []
