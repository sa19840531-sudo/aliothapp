"""真实 PostgreSQL 验证弹窗动作前场景的精确动作配对与清单围栏。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.api.app.application.app_evidence_manifest import AppEvidenceManifestFinalizer
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.exploration import AppVisualAnalysis
from services.api.app.infrastructure.db.app_action_scene_repository import (
    PostgresAppActionBeforeSceneReader,
)
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    PostgresAppVisualObservationRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_capture_session_auth_postgres import _database
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)


async def _pair(connection, pool, ids, saved, tmp_path, *, screenshot_payload=None):
    before, after, action = str(uuid4()), str(uuid4()), f"popup-action-{uuid4()}"
    store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
    manifests = PostgresEvidenceManifestRepository(pool=pool)
    for capture, role, parent, state in (
        (before, "before_action", saved.capture_id, "popup"),
        (after, "after_action", before, "stable"),
    ):
        artifact = await store.store(
            task_id=ids["task"],
            page_capture_id=capture,
            artifact_id=str(uuid4()),
            artifact_type="screenshot",
            content=screenshot_payload
            if screenshot_payload is not None
            else await store.read(saved.screenshot.storage_path),
        )
        await connection.execute(
            """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,
                   parent_capture_id,action_execution_id,capture_role,session_command_sequence,
                   capture_status,captured_at,metadata)
               VALUES($1::uuid,$2::uuid,$3::uuid,'android',$4,$5::uuid,$6,$7,41,
                      'captured',now(),'{"finalization_epoch":0}'::jsonb)""",
            capture,
            ids["task"],
            ids["run"],
            ids["session"],
            parent,
            action,
            role,
        )
        await connection.execute(
            """INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,source,
                   storage_path,content_type,checksum,size_bytes,metadata)
               VALUES($1::uuid,$2::uuid,$3::uuid,'screenshot','app_runtime',$4,$5,$6,$7,'{}')""",
            artifact.artifact_id,
            ids["task"],
            capture,
            artifact.storage_path,
            artifact.content_type,
            artifact.checksum,
            artifact.size_bytes,
        )
        await connection.execute(
            """INSERT INTO qc_app_capture(capture_id,screenshot_artifact_id,package_name,activity,
                   width,height,orientation,image_signature,device_snapshot)
               SELECT $1::uuid,$2::uuid,package_name,activity,width,height,orientation,
                      $3,device_snapshot
                 FROM qc_app_capture WHERE capture_id=$4::uuid""",
            capture,
            artifact.artifact_id,
            artifact.checksum,
            saved.capture_id,
        )
        pending = build_evidence_manifest_reference(
            task_id=ids["task"],
            session_id=ids["session"],
            capture_id=capture,
            manifest_version=1,
            artifacts=(
                ArtifactReference(
                    artifact_id=artifact.artifact_id,
                    artifact_type="screenshot",
                    role=role,
                    storage_path=artifact.storage_path,
                    checksum=artifact.checksum,
                    size_bytes=artifact.size_bytes,
                    content_type=artifact.content_type,
                ),
            ),
            quality_status=EvidenceQualityStatus.PENDING,
            quality_reason="artifact_integrity_pending",
            created_at=datetime.now(UTC),
        )
        await manifests.add(manifest=pending)
        await AppEvidenceManifestFinalizer(repository=manifests, artifact_store=store).finalize(
            pending
        )
        await PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None).save(
            AppVisualAnalysis.build(
                capture_id=capture,
                screenshot_artifact_id=artifact.artifact_id,
                image_signature=artifact.checksum,
                width=saved.width,
                height=saved.height,
                orientation=saved.orientation,
                visual_page_type="home",
                page_state=state,
                visual_summary="当前动作的真实场景",
                key_elements=(),
                candidates=(),
                provider_id="controlled-scene-test",
                model_id="controlled-scene-payload",
            )
        )
    await connection.execute(
        """INSERT INTO qc_action_execution(action_execution_id,task_id,session_id,intent_id,
               idempotency_key,intent_hash,intent_protocol_version,intent_payload,command_sequence,
               finalization_epoch,requested_source_capture_id,source_capture_id,result_capture_id,
               status,started_at,action_finished_at,evidence_finished_at)
           VALUES($1,$2::uuid,$3,$1,$1,'hash','test-v1','{}',41,0,$4::uuid,$5::uuid,$6::uuid,
                  'completed',now(),now(),now())""",
        action,
        ids["task"],
        ids["session"],
        saved.capture_id,
        before,
        after,
    )
    return before, after, action


async def test_reader_only_returns_real_before_scene_from_completed_current_action(tmp_path):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        before, after, _action = await _pair(connection, pool, ids, saved, tmp_path)
        reader = PostgresAppActionBeforeSceneReader(pool=pool)
        scene = await reader.get_before_scene(
            task_id=ids["task"],
            capture_id=after,
            agent_run_id=ids["run"],
        )
        assert (
            scene.capture_id == before and scene.page_type == "home" and scene.page_state == "popup"
        )
        assert (
            await reader.get_before_scene(
                task_id=ids["task"],
                capture_id=before,
                agent_run_id=ids["run"],
            )
            is None
        )


@pytest.mark.parametrize(
    "fence",
    [
        "wrong_run",
        "old_before_epoch",
        "old_after_epoch",
        "old_session_epoch",
        "wrong_action_source",
        "wrong_action_result",
        "before_latest_pending",
        "after_latest_pending",
    ],
)
async def test_reader_rejects_untrusted_or_noncurrent_action_scene(tmp_path, fence):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        before, after, action = await _pair(connection, pool, ids, saved, tmp_path)
        run_id = ids["run"]
        if fence == "wrong_run":
            run_id = str(uuid4())
        elif fence in {"old_before_epoch", "old_after_epoch"}:
            target = before if fence == "old_before_epoch" else after
            await connection.execute(
                "UPDATE qc_capture SET metadata=jsonb_set(metadata,'{finalization_epoch}','1') "
                "WHERE id=$1::uuid",
                target,
            )
        elif fence == "old_session_epoch":
            await connection.execute(
                "UPDATE qc_surface_session SET finalization_epoch=1 WHERE session_id=$1",
                ids["session"],
            )
        elif fence in {"wrong_action_source", "wrong_action_result"}:
            column = "source_capture_id" if fence == "wrong_action_source" else "result_capture_id"
            await connection.execute(
                f"UPDATE qc_action_execution SET {column}=$1::uuid WHERE action_execution_id=$2",
                saved.capture_id,
                action,
            )
        else:
            manifests = PostgresEvidenceManifestRepository(pool=pool)
            latest = await manifests.get_latest_for_capture(
                task_id=ids["task"],
                capture_id=before if fence.startswith("before") else after,
            )
            await manifests.add(
                manifest=build_evidence_manifest_reference(
                    task_id=latest.task_id,
                    session_id=latest.session_id,
                    capture_id=latest.capture_id,
                    manifest_version=latest.manifest_version + 1,
                    artifacts=latest.artifacts,
                    quality_status=EvidenceQualityStatus.PENDING,
                    quality_reason="artifact_integrity_pending",
                    created_at=datetime.now(UTC),
                )
            )
        assert (
            await PostgresAppActionBeforeSceneReader(pool=pool).get_before_scene(
                task_id=ids["task"],
                capture_id=after,
                agent_run_id=run_id,
            )
            is None
        )


async def test_reader_retains_popup_before_scene_with_unknown_business_page(tmp_path):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        before, after, _action = await _pair(connection, pool, ids, saved, tmp_path)
        # 遮挡弹层的业务页型可以未知；这不影响精确配对的真实 popup 状态。
        await connection.execute(
            "UPDATE qc_app_visual_observation SET visual_page_type=NULL WHERE capture_id=$1::uuid",
            before,
        )
        scene = await PostgresAppActionBeforeSceneReader(pool=pool).get_before_scene(
            task_id=ids["task"],
            capture_id=after,
            agent_run_id=ids["run"],
        )
        assert scene is not None
        assert scene.capture_id == before
        assert scene.page_type is None and scene.page_state == "popup"
