"""真实 PG 将列表连续动作交给正式工作项与冻结规则读端口。"""

import json
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw

from services.api.app.agents.runtime.business_middlewares.base import TaskRuntimeSnapshot
from services.api.app.application.app_inspection_publisher import (
    AppCaptureInspectionPublisher,
    PostgresAppCaptureInspectionAnnotator,
    PostgresCapturePresentationReader,
)
from services.api.app.application.app_rule_action_readiness import assess_app_rule_action_readiness
from services.api.app.application.inspection_work_items import InspectionWorkItemService
from services.api.app.application.task_query import TaskQueryService
from services.api.app.infrastructure.db.app_action_scene_repository import (
    PostgresAppActionBeforeSceneReader,
)
from services.api.app.infrastructure.db.app_rule_action_facts_repository import (
    PostgresAppRuleActionFactsReader,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    freeze_task_rule_inventory_on_connection,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_action_scene_repository_postgres import _pair
from services.api.tests.test_app_capture_session_auth_postgres import _database
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401
)
from services.api.tests.test_app_rule_action_readiness import RESPONSE


async def _seed_transition(connection, pool, ids, saved, tmp_path, mode):
    output = BytesIO()
    image = Image.new("RGB", (64, 128), "white")
    ImageDraw.Draw(image).rectangle((10, 10, 40, 80), fill="blue")
    image.save(output, format="PNG")
    payload = output.getvalue()
    scroll = await _pair(connection, pool, ids, saved, tmp_path, screenshot_payload=payload)
    await connection.execute(
        "UPDATE qc_capture SET session_command_sequence=40 WHERE action_execution_id=$1", scroll[2]
    )
    await connection.execute(
        "UPDATE qc_action_execution SET command_sequence=40,intent_payload=$2::jsonb "
        "WHERE action_execution_id=$1",
        scroll[2],
        json.dumps({"action_type": "scroll"}),
    )
    click = await _pair(connection, pool, ids, saved, tmp_path, screenshot_payload=payload)
    await connection.execute(
        "UPDATE qc_action_execution SET requested_source_capture_id=$2::uuid, "
        "intent_payload=$3::jsonb,result_payload=$4::jsonb WHERE action_execution_id=$1",
        click[2],
        scroll[1],
        json.dumps({"action_type": "click"}),
        json.dumps({"execution_outcome": "succeeded", "verification_outcome": "no_effect"}),
    )
    await connection.execute(
        "UPDATE qc_app_visual_observation SET page_state='stable',visual_page_type='home' "
        "WHERE capture_id=ANY($1::uuid[])",
        [*scroll[:2], *click[:2]],
    )
    after_type = (
        "home" if mode == "no_effect" else None if mode == "after_unknown" else "content_detail"
    )
    await connection.execute(
        "UPDATE qc_app_visual_observation SET visual_page_type=$2 WHERE capture_id=$1::uuid",
        click[1],
        after_type,
    )
    await connection.execute(
        "UPDATE qc_capture SET metadata=metadata || "
        "jsonb_build_object('page_type','home','page_state','stable') "
        "WHERE id=ANY($1::uuid[])",
        [*scroll[:2], *click[:2]],
    )
    await connection.execute(
        "UPDATE qc_capture SET metadata=metadata || jsonb_build_object('page_type',$2::text) "
        "WHERE id=$1::uuid",
        click[1],
        after_type,
    )
    if mode == "cross_package":
        await connection.execute(
            "UPDATE qc_app_capture SET package_name='other.app' WHERE capture_id=$1::uuid", click[1]
        )
    elif mode == "old_epoch":
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || '{\"finalization_epoch\":1}'::jsonb "
            "WHERE id=$1::uuid",
            scroll[1],
        )
    elif mode == "occluded":
        await connection.execute(
            "UPDATE qc_app_visual_observation SET page_state='popup' WHERE capture_id=$1::uuid",
            click[0],
        )
    elif mode == "wrong_requested":
        await connection.execute(
            "UPDATE qc_action_execution SET requested_source_capture_id=$2::uuid "
            "WHERE action_execution_id=$1",
            click[2],
            saved.capture_id,
        )
    elif mode == "not_adjacent":
        await connection.execute(
            "UPDATE qc_action_execution SET command_sequence=38 WHERE action_execution_id=$1",
            scroll[2],
        )
        await connection.execute(
            "UPDATE qc_capture SET session_command_sequence=38 WHERE action_execution_id=$1",
            scroll[2],
        )
    elif mode == "no_scroll":
        await connection.execute(
            'UPDATE qc_action_execution SET intent_payload=\'{"action_type":"observe"}\' '
            "WHERE action_execution_id=$1",
            scroll[2],
        )
    elif mode in {"after_popup", "after_unknown", "fake_proof", "wrong_role"}:
        await connection.execute(
            "UPDATE qc_app_visual_observation SET page_state='popup' WHERE capture_id=$1::uuid",
            click[1],
        )
        await connection.execute(
            'UPDATE qc_capture SET metadata=metadata || \'{"page_state":"popup"}\' '
            "WHERE id=$1::uuid",
            click[1],
        )
        if mode == "fake_proof":
            await connection.execute(
                "UPDATE qc_action_execution SET requested_source_capture_id=$2::uuid "
                "WHERE action_execution_id=$1",
                click[2],
                saved.capture_id,
            )
    elif mode == "bad_manifest":
        from services.api.app.domain.evidence import (
            EvidenceQualityStatus,
            build_evidence_manifest_reference,
        )
        from services.api.app.infrastructure.db.evidence_manifest_repository import (
            PostgresEvidenceManifestRepository,
        )

        manifests = PostgresEvidenceManifestRepository(pool=pool)
        previous = await manifests.get_latest_for_capture(task_id=ids["task"], capture_id=click[0])
        await manifests.add(
            manifest=build_evidence_manifest_reference(
                task_id=ids["task"],
                session_id=ids["session"],
                capture_id=click[0],
                manifest_version=previous.manifest_version + 1,
                artifacts=previous.artifacts,
                quality_status=EvidenceQualityStatus.PENDING,
                quality_reason="integrity_pending",
                created_at=datetime.now(UTC),
            )
        )
    return scroll, click, after_type


@pytest.mark.parametrize(
    "mode",
    [
        "detail",
        "no_effect",
        "cross_package",
        "old_epoch",
        "occluded",
        "wrong_requested",
        "not_adjacent",
        "no_scroll",
        "after_popup",
        "after_unknown",
        "bad_manifest",
        "fake_proof",
        "wrong_role",
    ],
)
async def test_real_list_scroll_to_detail_creates_formal_work_without_relabeling(mode, tmp_path):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        scroll, click, after_type = await _seed_transition(
            connection, pool, ids, saved, tmp_path, mode
        )
        rule_id = str(uuid4())
        judge_config = {
            **RESPONSE["judge_config"],
            "tool_refs": [
                "interaction_state_diff_tool",
                "network_event_summary_tool",
                "semantic_rule_judge_tool",
            ],
            "llm_checks": ["滑动和点击应提供正常操作或反馈"],
            "capability_config": {
                **RESPONSE["judge_config"]["capability_config"],
                "semantic_judge": "semantic_rule_judge_tool",
            },
        }
        await connection.execute(
            """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,
                   status,check_point,applicable_page_types,input_contract,judge_config)
               VALUES($1::uuid,$2::uuid,'list-response','滑动后交互','interaction','hybrid','critical',
                   'enabled','滑动及点击后产生操作或反馈','["home"]'::jsonb,
                   '{"required_artifacts":["screenshot","ui_snapshot","dom"],
                     "required_fields":["page.url","facts.ui_snapshot"],
                     "reuse_policy":{"scope":"per_page"},
                     "recapture_policy":{"kind":"fresh_observation","fallback":"manual_only"},
                     "quality_gate":{"allow_empty_visible_text":true,"min_screenshot_count":1}}',
                   $3::jsonb)""",
            rule_id,
            ids["ruleset"],
            json.dumps(judge_config),
        )
        task = await connection.fetchrow(
            "SELECT task_config,channel FROM qc_task WHERE id=$1::uuid", ids["task"]
        )
        code = await connection.fetchval(
            "SELECT ruleset_code FROM qc_ruleset WHERE id=$1::uuid", ids["ruleset"]
        )
        config = await freeze_task_rule_inventory_on_connection(
            connection,
            target_platform="app",
            ruleset_code=code,
            ruleset_version=1,
            channel=task["channel"],
            task_config=json.loads(task["task_config"]),
        )
        await connection.execute(
            "UPDATE qc_task SET task_config=$2::jsonb WHERE id=$1::uuid",
            ids["task"],
            json.dumps(config),
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

        reader = PostgresAppRuleActionFactsReader(
            pool=pool, artifact_reader=LocalEvidenceArtifactStore(root=tmp_path / "evidence")
        )
        facts = await reader.get_facts(
            task_id=ids["task"], capture_id=click[1], agent_run_id=ids["run"]
        )
        expected = mode in {"detail", "no_effect", "after_popup", "after_unknown", "wrong_role"}
        assert assess_app_rule_action_readiness(RESPONSE, **facts).ready is expected
        repository = PostgresTaskRepository(pool=pool)
        publisher = AppCaptureInspectionPublisher(
            task_query=TaskQueryService(repository=repository),
            snapshot_provider=SnapshotProvider(),
            work_item_service=InspectionWorkItemService(
                repository=PostgresInspectionWorkItemRepository(pool=pool)
            ),
            annotator=PostgresAppCaptureInspectionAnnotator(pool=pool),
            action_scene_reader=PostgresAppActionBeforeSceneReader(pool=pool),
            presentation_reader=PostgresCapturePresentationReader(pool=pool),
            action_facts_reader=reader,
        )
        result = await publisher.publish_for_capture(
            task_id=ids["task"],
            capture_id=click[1],
            agent_run_id=ids["run"],
            visual_page_type=after_type,
            page_state="popup"
            if mode in {"after_popup", "after_unknown", "fake_proof", "wrong_role"}
            else "stable",
        )
        rows = await connection.fetch(
            "SELECT w.page_capture_id::text,r.rule_id::text FROM qc_inspection_work_item w "
            "JOIN qc_inspection_work_item_rule r ON r.work_item_id=w.id WHERE w.task_id=$1::uuid",
            ids["task"],
        )
        assert len(rows) == int(expected)
        if expected:
            assert result.status == "published"
            assert rows[0]["page_capture_id"] == click[1]
            assert rows[0]["rule_id"] == rule_id
            definition = await repository.get_rule_judge_definition(
                task_id=ids["task"], rule_code="list-response"
            )
            assert definition.rule_id == rule_id
        if expected or mode == "fake_proof":
            await _assert_formal_packet_reaches_judge(
                connection, pool, ids, tmp_path, repository, reader, click, scroll, mode
            )
        metadata_type = await connection.fetchval(
            "SELECT metadata->>'page_type' FROM qc_capture WHERE id=$1::uuid", click[1]
        )
        assert metadata_type == after_type


async def _assert_formal_packet_reaches_judge(
    connection, pool, ids, tmp_path, repository, reader, click, scroll, mode
):
    from services.api.app.application.app_action_evidence import PostgresAppActionEvidenceStore
    from services.api.app.application.manifest_backed_observation import (
        ManifestBackedObservationRepository,
    )
    from services.api.app.application.manifest_backed_packet import ManifestBackedPacketLoader
    from services.api.app.application.observation_packet import ObservationPacketService
    from services.api.app.application.rule_judge import RuleJudgeRequest, RuleJudgeService
    from services.api.app.application.semantic_rule_judge import SemanticJudgeModelOutput
    from services.api.app.infrastructure.db.evidence_manifest_repository import (
        PostgresEvidenceManifestRepository,
    )
    from services.api.app.infrastructure.db.observation_packet_repository import (
        PostgresObservationPacketRepository,
    )

    store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
    loader = ManifestBackedPacketLoader(
        manifests=PostgresEvidenceManifestRepository(pool=pool), artifact_reader=store
    )
    packets = ObservationPacketService(
        repository=ManifestBackedObservationRepository(
            inner=PostgresObservationPacketRepository(pool=pool, artifact_store=store),
            packet_loader=loader,
        ),
        app_action_facts_reader=reader,
    )
    roles = await PostgresAppActionEvidenceStore(pool=pool).resolve(
        task_id=ids["task"], page_capture_id=click[1]
    )
    if mode == "wrong_role":
        roles["before"] = scroll[0]

    class Semantic:
        calls = 0
        packet = None

        async def judge(self, *, packet, tool_results):
            self.calls += 1
            self.packet = packet
            return SemanticJudgeModelOutput(
                conclusion="evidence_insufficient",
                confidence=0.0,
                reasoning_summary="测试只验证真实前后输入交接，不伪造业务结论。",
                missing_evidence=["test_semantic_result"],
            )

    semantic = Semantic()
    service = RuleJudgeService(
        repository=repository,
        observation_packet_service=packets,
        semantic_judge=semantic,
        manifest_quality_authoritative=True,
    )
    result = await service.judge(
        RuleJudgeRequest(
            task_id=ids["task"],
            page_capture_id=click[1],
            rule_code="list-response",
            evidence_artifact_ids=(),
            agent_run_id=ids["run"],
            source_agent_run_id=ids["run"],
            evidence_capture_roles=roles,
            judge_input={
                "facts": {"app_list_interaction_source": {"source": "verified_app_action_facts"}}
            },
        )
    )
    assert result.execution_status == "evidence_insufficient"
    assert semantic.calls == (0 if mode in {"fake_proof", "wrong_role"} else 1)
    rows = await connection.fetch(
        "SELECT packet_payload FROM qc_observation_packet WHERE task_id=$1::uuid", ids["task"]
    )
    assert len(rows) == 1
    payload = json.loads(rows[0]["packet_payload"])
    proof = payload["facts"].get("app_list_interaction_source")
    if mode in {"fake_proof", "wrong_role"}:
        assert proof is None
    else:
        assert proof["source_capture_id"] == click[0]
        assert proof["primary_capture_id"] == click[1]
        assert payload["identity"]["capture_roles"] == {"before": click[0], "primary": click[1]}
        assert len(semantic.packet.image_payloads) == 2
        assert payload["page"]["page_type"] == (
            "home" if mode == "no_effect" else None if mode == "after_unknown" else "content_detail"
        )
        if mode in {"after_popup", "after_unknown"}:
            assert payload["page"]["page_state"] == "popup"
            from services.api.app.domain.evidence.presentation import PresentationAssessment

            assert PresentationAssessment.from_dict(
                payload["facts"]["presentation"]
            ).occluding_layer_present
