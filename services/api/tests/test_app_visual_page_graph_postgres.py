"""真实存储与图投影接线；模型载荷是受控样本，不是真机识别准确率验收。"""

import json
import os

import pytest

from services.api.app.application.app_capture_persistence import AppCapturePersistenceService
from services.api.app.application.app_screenshot_understanding import UnderstandAppScreenshotRequest
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.app_vision.llm import build_analysis_from_llm_payload
from services.api.app.infrastructure.db.app_bootstrap_repository import (
    PostgresAppBootstrapRepository,
)
from services.api.app.infrastructure.db.app_capture_repository import PostgresAppCaptureRepository
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    PostgresAppVisualObservationRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.page_graph_repository import PostgresPageGraphRepository
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_postgres_integration import (
    _advance_to_bootstrap_capturing,
    _app_database,
    _screenshot_artifacts,
    _start_and_claim,
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)
from services.api.tests.test_app_visual_page_identity import _anchor, _observation

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库"
)


@pytest.mark.parametrize("has_identity", [True, False])
async def test_saved_production_visual_output_projects_and_rebuilds_page_identity(
    tmp_path, has_identity
):
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
        saved = await captures.persist_bootstrap(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            artifacts=_screenshot_artifacts(ids),
            expected_bootstrap_row_version=snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_a"],
        )
        request = UnderstandAppScreenshotRequest(
            task_id=ids["task"],
            session_id=ids["session"],
            capture_id=saved.capture_id,
            screenshot_artifact_id=saved.screenshot.artifact_id,
            screenshot_png=await store.read(saved.screenshot.storage_path),
            image_signature=saved.image_signature,
            width=saved.width,
            height=saved.height,
            orientation=saved.orientation,
            package_name=saved.package_name,
            activity=saved.activity,
            allowed_actions=(InteractionOperation.OBSERVE,),
            max_candidates=1,
        )
        analysis = build_analysis_from_llm_payload(
            request=request,
            payload={
                "visual_page_type": "content_detail",
                "visual_page_type_confidence": 0.96,
                "page_state": "stable",
                "page_name": "作品详情",
                "page_name_confidence": 0.99,
                "visual_summary": "作品详情",
                "page_identity_observation": _observation(object_id="作品编号 123")
                if has_identity
                else None,
                "local_state_observation": {"page_index": _anchor("第2页")},
            },
            page_types=get_default_page_type_registry(),
            model_id="controlled-payload-test",
        )
        observations = PostgresAppVisualObservationRepository(pool=pool)
        # 历史元数据可能陈旧，图必须使用 App Capture 的权威运行面和截图列。
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || $2::jsonb WHERE id=$1::uuid",
            saved.capture_id,
            json.dumps(
                {
                    "package_name": "com.foreign.metadata",
                    "activity": ".Foreign",
                    "image_signature": "sha256:" + "0" * 64,
                    "screenshot_artifact_id": "foreign-artifact",
                }
            ),
        )
        assert (await observations.save(analysis)).created
        assert not (await observations.save(analysis)).created
        loaded = await observations.get_by_capture_id(capture_id=saved.capture_id)
        assert loaded is not None and loaded.analysis == analysis

        async def projected():
            return dict(
                await connection.fetchrow(
                    "SELECT logical_page_key AS page_identity, identity_status, "
                    "state_fingerprint, state_payload "
                    "FROM qc_page_graph_state WHERE state_id=$1::uuid",
                    saved.capture_id,
                )
            )

        initial = await projected()
        assert initial["identity_status"] == ("confirmed" if has_identity else "unknown")
        if has_identity:
            assert initial["page_identity"].endswith(
                analysis.surface_facts["app_page_identity"]["identity"]
            )
            payload = json.loads(initial["state_payload"])
            assert payload["local_state_facts"]["local_state"] == {"page_index": ["第2页"]}
        else:
            assert initial["page_identity"] == f"unknown:{saved.capture_id}"

        graph = PostgresPageGraphRepository(pool=pool)
        await graph.rebuild_run(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0)
        rebuilt = await projected()
        assert rebuilt["page_identity"] == initial["page_identity"]
        assert rebuilt["state_fingerprint"] == initial["state_fingerprint"]
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_page_graph_state WHERE task_id=$1::uuid", ids["task"]
            )
            == 1
        )
