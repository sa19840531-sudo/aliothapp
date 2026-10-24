"""真实数据库验证业务就绪只消费本轮精确前后 Capture。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.api.app.application.app_evidence_manifest import AppEvidenceManifestFinalizer
from services.api.app.application.app_rule_action_readiness import assess_app_rule_action_readiness
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.infrastructure.db.app_rule_action_facts_repository import (
    PostgresAppRuleActionFactsReader,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_action_scene_repository_postgres import _pair
from services.api.tests.test_app_capture_session_auth_postgres import _database
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)
from services.api.tests.test_app_rule_action_readiness import native_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["no_effect", "wrong_target", "old_epoch", "no_bottom_geometry", "native_scroll"]
)
async def test_readiness_uses_completed_pair_without_fabricating_effect_or_scroll(mode, tmp_path):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        before, after, action = await _pair(connection, pool, ids, saved, tmp_path)
        await connection.execute(
            "UPDATE qc_app_visual_observation SET page_state='stable' WHERE capture_id=$1::uuid",
            before,
        )
        candidate = {
            "candidate_id": "top-candidate",
            "source_capture_id": saved.capture_id,
            "source_image_signature": saved.image_signature,
            "expected_state_hint": "home_top_tab:最新",
        }
        if mode == "wrong_target":
            candidate["source_image_signature"] = "sha256:" + "f" * 64
        await connection.execute(
            """INSERT INTO qc_app_visual_observation(observation_id,task_id,session_id,capture_id,
                 screenshot_artifact_id,contract_version,provider_id,model_id,result_hash,
                 visual_page_type,visual_summary,page_state,candidate_targets)
               VALUES($1::uuid,$2::uuid,$3,$4::uuid,$5::uuid,'app-visual-analysis.v2',
                 'readiness-test','readiness-test',$6,'home','当前目录','stable',$7::jsonb)""",
            str(uuid4()),
            ids["task"],
            ids["session"],
            saved.capture_id,
            saved.screenshot.artifact_id,
            "b" * 64,
            json.dumps([candidate]),
        )
        await connection.execute(
            "UPDATE qc_action_execution SET intent_payload=$2::jsonb,result_payload=$3::jsonb "
            "WHERE action_execution_id=$1",
            action,
            json.dumps(
                {
                    "action_type": "scroll"
                    if mode in {"no_bottom_geometry", "native_scroll"}
                    else "click",
                    "parameters": {"candidate_id": "top-candidate"},
                }
            ),
            json.dumps({"execution_outcome": "succeeded", "verification_outcome": "no_effect"}),
        )
        if mode == "old_epoch":
            await connection.execute(
                "UPDATE qc_capture SET metadata=metadata || '{\"finalization_epoch\":1}'::jsonb "
                "WHERE id=$1::uuid",
                before,
            )
        if mode == "native_scroll":
            store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
            manifests = PostgresEvidenceManifestRepository(pool=pool)
            for capture_id, role in ((before, "before_action"), (after, "after_action")):
                previous = await manifests.get_latest_for_capture(
                    task_id=ids["task"], capture_id=capture_id
                )
                artifact = await store.store(
                    task_id=ids["task"],
                    page_capture_id=capture_id,
                    artifact_id=str(uuid4()),
                    artifact_type="ui_snapshot",
                    content=native_snapshot(),
                )
                await connection.execute(
                    """INSERT INTO qc_evidence_artifact(
                        id, task_id, page_capture_id, artifact_type, source,
                        storage_path, content_type, checksum, size_bytes, metadata
                    ) VALUES($1::uuid, $2::uuid, $3::uuid, 'ui_snapshot', 'app_runtime',
                             $4, $5, $6, $7, '{}')""",
                    artifact.artifact_id,
                    ids["task"],
                    capture_id,
                    artifact.storage_path,
                    artifact.content_type,
                    artifact.checksum,
                    artifact.size_bytes,
                )
                manifest = build_evidence_manifest_reference(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    capture_id=capture_id,
                    manifest_version=previous.manifest_version + 1,
                    artifacts=(
                        *previous.artifacts,
                        ArtifactReference(
                            artifact_id=artifact.artifact_id,
                            artifact_type="ui_snapshot",
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
                await manifests.add(manifest=manifest)
                await AppEvidenceManifestFinalizer(
                    repository=manifests, artifact_store=store
                ).finalize(manifest)
        facts = await PostgresAppRuleActionFactsReader(
            pool=pool,
            artifact_reader=LocalEvidenceArtifactStore(root=tmp_path / "evidence"),
        ).get_facts(
            task_id=ids["task"],
            capture_id=after,
            agent_run_id=ids["run"],
        )
        rule = {
            "judge_config": {
                "capability_config": {
                    "requires_journey": True,
                    "batch_group": "visual-nav",
                    "deterministic_tools": ["state_snapshot_tool", "interaction_state_diff_tool"],
                }
            }
        }
        if mode in {"no_bottom_geometry", "native_scroll"}:
            rule["judge_config"]["capability_config"].update(
                batch_group="list-scroll", deterministic_tools=["layout_geometry_assertion_handler"]
            )
            rule["judge_config"]["thresholds"] = {"max_bottom_blank_viewport_ratio": 0.15}
        outcome = assess_app_rule_action_readiness(rule, **facts)
        assert outcome.ready is (mode in {"no_effect", "native_scroll"})
        if mode == "no_bottom_geometry":
            assert outcome.missing_evidence == ("trusted_bottom_position",)
        if mode == "native_scroll":
            assert outcome.missing_evidence == ("native_scroll_position_unavailable",)
