"""开屏跳过按本次真实双图判断，未知结果页型保留且不借旅程文字伪判。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.api.app.application import app_inspection_publisher, inspection_candidate_publish
from services.api.app.application.app_action_evidence import PostgresAppActionEvidenceStore
from services.api.app.application.app_inspection_publisher import AppCaptureInspectionPublisher
from services.api.app.application.app_live_inspection_publish import AppLiveInspectionPublishService
from services.api.app.application.manifest_backed_observation import (
    ManifestBackedObservationRepository,
)
from services.api.app.application.manifest_backed_packet import ManifestBackedPacketLoader
from services.api.app.application.observation_packet import ObservationPacketService
from services.api.app.application.rule_judge import (
    RuleJudgeService,
    SemanticJudgeModelOutput,
    _has_startup_result_images,
)
from services.api.app.application.rule_page_state import AppActionBeforeScene
from services.api.app.domain.evidence import (
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.exploration import AppVisualAnalysis
from services.api.app.infrastructure.db.app_action_scene_repository import (
    PostgresAppActionBeforeSceneReader,
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
from services.api.app.infrastructure.db.observation_packet_repository import (
    PostgresObservationPacketRepository,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_action_scene_repository_postgres import _database
from services.api.tests.test_app_inspection_work_item_publish import (
    _FakeAgentRunReader,
    _FakeAnnotator,
    _FakeRule,
    _FakeSnapshotProvider,
    _FakeTaskQuery,
    _FakeWorkItemService,
)
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)
from services.api.tests.test_rule_judge import FakeRuleJudgeRepository
from services.api.tests.test_rule_judge_formal_packet_postgres import _setup


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        inspection_candidate_publish,
        "get_settings",
        lambda: SimpleNamespace(inspection_work_items=SimpleNamespace(max_attempts=3)),
    )


def _rule():
    return _FakeRule(
        rule_id="startup-rule",
        rule_code="custom-startup-skip",
        executor_type="llm",
        applicable_page_types=["startup_ad", "home"],
        input_contract={
            "required_artifacts": ["screenshot"],
            "required_capture_roles": ["primary"],
        },
        judge_config={
            "tool_refs": ["page_capture_tool", "semantic_rule_judge_tool"],
            "capability_config": {"batch_group": "startup-ad-skip", "requires_journey": True},
        },
    )


async def _save_unknown_result_visual(
    connection,
    pool,
    ids,
    after,
    *,
    page_state: str = "popup",
):
    await connection.execute(
        "UPDATE qc_task SET task_config=task_config || "
        '\'{"workflow_version":"v2"}\'::jsonb WHERE id=$1::uuid',
        ids["task"],
    )
    # 先建立已完成动作，再保存晚到视觉；这与正式采集时序一致。
    await connection.execute(
        "DELETE FROM qc_app_visual_observation WHERE capture_id=$1::uuid", after
    )
    row = await connection.fetchrow("SELECT * FROM qc_app_capture WHERE capture_id=$1::uuid", after)
    analysis = AppVisualAnalysis.build(
        capture_id=after,
        screenshot_artifact_id=str(row["screenshot_artifact_id"]),
        image_signature=row["image_signature"],
        width=row["width"],
        height=row["height"],
        orientation=row["orientation"],
        visual_page_type=None,
        page_state=page_state,
        visual_summary=(
            "真实动作后广告弹层，底层业务页型未知"
            if page_state in {"popup", "modal", "overlay", "dialog"}
            else "动作后页型未知且非遮挡态"
        ),
        key_elements=(),
        candidates=(),
        provider_id="startup-result-test",
        model_id="controlled-startup-result",
    )
    observations = PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None)
    await observations.save(analysis)
    await observations.save(analysis)
    return await connection.fetchval(
        "SELECT count(*) FROM qc_outbox_event WHERE task_id=$1::uuid "
        "AND aggregate_type='app_visual_observation'",
        ids["task"],
    )


@pytest.mark.parametrize(
    "after_page,before_page,expected",
    [
        (None, "startup_ad", 1),
        ("startup_ad", "startup_ad", 1),
        ("video_list", "startup_ad", 1),
        ("home", "startup_ad", 1),
        (None, None, 0),
        (None, "home", 0),
        ("home", "home", 0),
        ("home", None, 0),
    ],
)
async def test_only_completed_startup_result_can_publish_skip_rule(
    after_page,
    before_page,
    expected,
):
    class SceneReader:
        async def get_before_scene(self, **_kwargs):
            return (
                AppActionBeforeScene(capture_id="before", page_type=before_page, page_state="popup")
                if before_page
                else None
            )

    work = _FakeWorkItemService()
    annotator = _FakeAnnotator()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([_rule()]),
        snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
        work_item_service=work,
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
        action_scene_reader=SceneReader(),
    )
    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        agent_run_id="run-1",
        visual_page_type=after_page,
        page_state="popup",
        active_session_only=True,
    )
    assert result.published_count == expected
    if expected:
        assert work.commands[0].page_capture_id == "cap-1"
        assert [rule.rule_code for rule in work.commands[0].rules] == ["custom-startup-skip"]
        assert annotator.calls[-1]["page_type"] == after_page
        assert result.page_type == after_page


async def test_unknown_startup_result_cannot_publish_unrelated_startup_rules():
    class SceneReader:
        async def get_before_scene(self, **_kwargs):
            return AppActionBeforeScene(
                capture_id="before",
                page_type="startup_ad",
                page_state="popup",
            )

    unrelated = _FakeRule(rule_id="other", rule_code="other", applicable_page_types=["startup_ad"])
    work = _FakeWorkItemService()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([_rule(), unrelated]),
        snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
        work_item_service=work,
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        action_scene_reader=SceneReader(),
    )
    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        agent_run_id="run-1",
        visual_page_type=None,
        page_state="popup",
        active_session_only=True,
    )
    assert result.published_count == 1
    assert [rule.rule_code for rule in work.commands[0].rules] == ["custom-startup-skip"]


@pytest.mark.parametrize("manifest", [False, True])
async def test_real_unknown_after_pair_reaches_semantic_judge_with_both_original_pngs(
    tmp_path,
    monkeypatch,
    manifest,
):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        definition, packets, reader, request = await _setup(
            connection,
            pool,
            ids,
            saved,
            tmp_path,
            manifest=manifest,
        )
        before = request.evidence_capture_roles["before"]
        after = request.page_capture_id
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || "
            '\'{"page_type":"startup_ad","page_state":"popup"}\'::jsonb WHERE id=$1::uuid',
            before,
        )
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || "
            '\'{"page_type":null,"page_state":"popup"}\'::jsonb WHERE id=$1::uuid',
            after,
        )
        await connection.execute(
            "UPDATE qc_app_visual_observation SET visual_page_type="
            "CASE WHEN capture_id=$1::uuid THEN 'startup_ad' ELSE NULL END,"
            "page_state='popup' WHERE capture_id IN ($1::uuid,$2::uuid)",
            before,
            after,
        )
        actual_roles = await PostgresAppActionEvidenceStore(pool=pool).resolve(
            task_id=ids["task"],
            page_capture_id=after,
        )
        assert actual_roles == {"primary": after, "before": before}
        assert await _save_unknown_result_visual(connection, pool, ids, after) == 1
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || "
            '\'{"inspection_status":"snapshot_rules_deferred"}\'::jsonb '
            "WHERE task_id=$1::uuid AND id<>$2::uuid",
            ids["task"],
            after,
        )

        async def get_pool():
            return pool

        monkeypatch.setattr(app_inspection_publisher, "get_postgres_pool", get_pool)
        work = _FakeWorkItemService()
        publisher = AppCaptureInspectionPublisher(
            task_query=_FakeTaskQuery([_rule()]),
            snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
            work_item_service=work,
            annotator=_FakeAnnotator(),
            agent_run_reader=_FakeAgentRunReader(),
            action_scene_reader=PostgresAppActionBeforeSceneReader(pool=pool),
        )
        captures = PostgresAppLiveInspectionRepository(pool=pool)
        pending = await captures.list_pending_captures(task_id=ids["task"], agent_run_id=ids["run"])
        assert [(item.capture_id, item.page_type) for item in pending] == [(after, None)]
        assert (
            await captures.load_drain_state(
                task_id=ids["task"],
                agent_run_id=ids["run"],
            )
        ).pending_capture_count == 1
        published = await AppLiveInspectionPublishService(
            capture_reader=captures,
            publisher=publisher,
        ).publish_pending(task_id=ids["task"], agent_run_id=ids["run"])
        assert published == 1 and work.commands[0].page_capture_id == after
        definition = replace(
            definition,
            executor_type="llm",
            category="ad",
            judge_config={
                "tool_refs": ["page_capture_tool", "semantic_rule_judge_tool"],
                "capability_config": {"batch_group": "startup-ad-skip", "requires_journey": True},
            },
            input_contract={
                "required_artifacts": ["screenshot"],
                "required_capture_roles": ["primary"],
                "quality_gate": {"allow_empty_visible_text": True},
            },
        )
        if manifest:
            store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
            packets = ObservationPacketService(
                repository=ManifestBackedObservationRepository(
                    inner=PostgresObservationPacketRepository(pool=pool, artifact_store=store),
                    packet_loader=ManifestBackedPacketLoader(
                        manifests=PostgresEvidenceManifestRepository(pool=pool),
                        artifact_reader=store,
                    ),
                )
            )

        class SemanticJudge:
            packet = None

            async def judge(self, *, packet, tool_results):
                self.packet = packet
                assert packet.packet_payload["page"]["page_type"] is None
                roles = packet.packet_payload["facts"]["capture_roles"]
                assert roles["before"]["page_capture_id"] == before
                assert roles["primary"]["page_capture_id"] == after
                assert roles["before"]["page_type"] == "startup_ad"
                assert roles["before"]["quality_status"] == ("pass" if manifest else None)
                images = {ref: data for ref, data, _kind in packet.image_payloads}
                before_pngs = set(roles["before"]["artifact_ids"]).intersection(images)
                after_pngs = set(roles["primary"]["artifact_ids"]).intersection(images)
                assert before_pngs and after_pngs and before_pngs.isdisjoint(after_pngs)
                assert all(
                    images[ref].startswith(b"\x89PNG\r\n\x1a\n") for ref in before_pngs | after_pngs
                )
                return SemanticJudgeModelOutput(
                    conclusion="fail",
                    confidence=0.99,
                    reasoning_summary="操作后仍显示广告弹层，保留实际截图判定。",
                    evidence_refs=sorted(before_pngs | after_pngs),
                )

        semantic = SemanticJudge()
        judged = await RuleJudgeService(
            repository=FakeRuleJudgeRepository(definition),
            observation_packet_service=packets,
            evidence_reader=reader,
            semantic_judge=semantic,
        ).judge(replace(request, evidence_capture_roles=actual_roles))
        assert semantic.packet is not None, judged.judge_output
        assert judged.execution_status == "fail"
        assert judged.observation_packet_id == semantic.packet.observation_packet_id


@pytest.mark.parametrize(
    "fence",
    ["ordinary_unknown", "unfinished", "wrong_source", "latest_before_pending"],
)
async def test_unknown_visual_cannot_wake_or_enter_live_inventory_without_trusted_startup_pair(
    tmp_path,
    fence,
):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        _definition, _packets, _reader, request = await _setup(
            connection,
            pool,
            ids,
            saved,
            tmp_path,
            manifest=False,
        )
        before, after = request.evidence_capture_roles["before"], request.page_capture_id
        if fence != "ordinary_unknown":
            await connection.execute(
                "UPDATE qc_app_visual_observation SET visual_page_type='startup_ad' "
                "WHERE capture_id=$1::uuid",
                before,
            )
        if fence == "unfinished":
            await connection.execute(
                "UPDATE qc_action_execution SET status='evidence_pending',result_capture_id=NULL,"
                "evidence_finished_at=NULL WHERE result_capture_id=$1::uuid",
                after,
            )
        elif fence == "wrong_source":
            await connection.execute(
                "UPDATE qc_action_execution SET source_capture_id=$1::uuid "
                "WHERE result_capture_id=$2::uuid",
                saved.capture_id,
                after,
            )
        elif fence == "latest_before_pending":
            manifests = PostgresEvidenceManifestRepository(pool=pool)
            latest = await manifests.get_latest_for_capture(task_id=ids["task"], capture_id=before)
            await manifests.add(
                manifest=build_evidence_manifest_reference(
                    task_id=latest.task_id,
                    session_id=latest.session_id,
                    capture_id=before,
                    manifest_version=latest.manifest_version + 1,
                    artifacts=latest.artifacts,
                    quality_status=EvidenceQualityStatus.PENDING,
                    quality_reason="artifact_integrity_pending",
                    created_at=datetime.now(UTC),
                )
            )
        # 非遮挡未知页型不得靠开屏双图旁路进库存；遮挡 popup 另有发布合同。
        assert (
            await _save_unknown_result_visual(
                connection, pool, ids, after, page_state="stable"
            )
            == 0
        )
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || "
            '\'{"inspection_status":"snapshot_rules_deferred"}\'::jsonb '
            "WHERE task_id=$1::uuid AND id<>$2::uuid",
            ids["task"],
            after,
        )
        captures = PostgresAppLiveInspectionRepository(pool=pool)
        assert (
            await captures.list_pending_captures(task_id=ids["task"], agent_run_id=ids["run"]) == ()
        )
        assert (
            await captures.load_drain_state(
                task_id=ids["task"],
                agent_run_id=ids["run"],
            )
        ).pending_capture_count == 0


@pytest.mark.parametrize("latest_page", ["home", None])
async def test_startup_pair_uses_latest_same_screenshot_visual_after_future_review(
    tmp_path,
    latest_page,
):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        _definition, _packets, _reader, request = await _setup(
            connection, pool, ids, saved, tmp_path, manifest=False
        )
        before, after = request.evidence_capture_roles["before"], request.page_capture_id
        await connection.execute(
            "UPDATE qc_app_visual_observation SET visual_page_type='startup_ad' "
            "WHERE capture_id=$1::uuid", before,
        )
        await _save_unknown_result_visual(connection, pool, ids, after)
        # 当前正式仓储一帧一视觉；仅在回滚事务模拟未来复核版本，验证来源围栏。
        await connection.execute(
            "ALTER TABLE qc_app_visual_observation "
            "DROP CONSTRAINT qc_app_visual_observation_capture_id_key"
        )
        await connection.execute(
            """INSERT INTO qc_app_visual_observation(
                observation_id,task_id,session_id,capture_id,screenshot_artifact_id,
                contract_version,provider_id,model_id,result_hash,visual_page_type,
                visual_summary,key_elements,candidate_targets,page_state,overlay_signature,
                surface_facts,created_at)
            SELECT $2::uuid,task_id,session_id,capture_id,screenshot_artifact_id,
                contract_version,provider_id,model_id,result_hash,$3,
                visual_summary,key_elements,candidate_targets,'popup',overlay_signature,
                surface_facts,created_at + interval '1 second'
            FROM qc_app_visual_observation WHERE capture_id=$1::uuid""",
            before, str(uuid4()), latest_page,
        )
        scene = await PostgresAppActionBeforeSceneReader(pool=pool).get_before_scene(
            task_id=ids["task"], capture_id=after, agent_run_id=ids["run"]
        )
        assert scene is not None and scene.page_type == latest_page and scene.page_state == "popup"
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || "
            "'{\"inspection_status\":\"snapshot_rules_deferred\"}'::jsonb "
            "WHERE task_id=$1::uuid AND id<>$2::uuid", ids["task"], after,
        )
        captures = PostgresAppLiveInspectionRepository(pool=pool)
        # 遮挡态无页型仍须进入活库存，供宿主挂弹窗／广告规则（与 publisher 对齐）。
        pending = await captures.list_pending_captures(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        assert len(pending) == 1
        assert pending[0].capture_id == after
        assert pending[0].page_type is None
        assert pending[0].page_state == "popup"
        assert (await captures.load_drain_state(
            task_id=ids["task"], agent_run_id=ids["run"]
        )).pending_capture_count == 1


async def test_live_inventory_rejects_visual_for_noncurrent_screenshot(tmp_path):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        _definition, _packets, _reader, request = await _setup(
            connection, pool, ids, saved, tmp_path, manifest=False
        )
        before, after = request.evidence_capture_roles["before"], request.page_capture_id
        await connection.execute(
            "UPDATE qc_app_visual_observation SET screenshot_artifact_id="
            "(SELECT screenshot_artifact_id FROM qc_app_capture WHERE capture_id=$1::uuid) "
            "WHERE capture_id=$2::uuid", before, after,
        )
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || "
            "'{\"inspection_status\":\"snapshot_rules_deferred\"}'::jsonb "
            "WHERE task_id=$1::uuid AND id<>$2::uuid", ids["task"], after,
        )
        captures = PostgresAppLiveInspectionRepository(pool=pool)
        assert await captures.list_pending_captures(
            task_id=ids["task"], agent_run_id=ids["run"]
        ) == ()
        assert (await captures.load_drain_state(
            task_id=ids["task"], agent_run_id=ids["run"]
        )).pending_capture_count == 0


@pytest.mark.parametrize(
    "fence",
    [
        "valid",
        "before_missing",
        "primary_missing",
        "identity",
        "request",
        "same_capture",
        "nonstartup",
        "before_pending",
        "primary_pending",
        "before_png_missing",
        "after_png_missing",
        "same_artifact",
        "bad_content_type",
        "invalid_png",
        "packet_pending",
        "foreign_surface",
    ],
)
def test_startup_image_preflight_keeps_role_quality_and_actual_image_boundaries(fence):
    roles = {
        "before": {
            "page_capture_id": "before",
            "page_type": "startup_ad",
            "quality_status": "pass",
            "artifact_ids": ["before-png"],
        },
        "primary": {
            "page_capture_id": "after",
            "quality_status": "pass",
            "artifact_ids": ["after-png"],
        },
    }
    bindings = {"before": "before", "primary": "after"}
    payload = {
        "page": {"surface_kind": "android"},
        "identity": {"capture_roles": bindings},
        "facts": {"capture_roles": roles},
    }
    images = [
        ("before-png", b"\x89PNG\r\n\x1a\n", "image/png"),
        ("after-png", b"\x89PNG\r\n\x1a\n", "image/png"),
    ]
    packet = SimpleNamespace(
        packet_payload=payload,
        page_capture_id="after",
        quality_gate_status="pass",
        image_payloads=images,
    )
    request = SimpleNamespace(evidence_capture_roles=deepcopy(bindings))
    if fence == "before_missing":
        del roles["before"]
    elif fence == "primary_missing":
        del roles["primary"]
    elif fence == "identity":
        bindings["before"] = "foreign-before"
    elif fence == "request":
        request.evidence_capture_roles["before"] = "foreign-before"
    elif fence == "same_capture":
        roles["before"]["page_capture_id"] = "after"
        bindings["before"] = request.evidence_capture_roles["before"] = "after"
    elif fence == "nonstartup":
        roles["before"]["page_type"] = "home"
    elif fence == "before_pending":
        roles["before"]["quality_status"] = "pending"
    elif fence == "primary_pending":
        roles["primary"]["quality_status"] = "pending"
    elif fence == "before_png_missing":
        packet.image_payloads = images[1:]
    elif fence == "after_png_missing":
        packet.image_payloads = images[:1]
    elif fence == "same_artifact":
        roles["primary"]["artifact_ids"] = ["before-png"]
    elif fence == "bad_content_type":
        packet.image_payloads = [images[0], ("after-png", images[1][1], "application/json")]
    elif fence == "invalid_png":
        packet.image_payloads = [images[0], ("after-png", b"bad", "image/png")]
    elif fence == "packet_pending":
        packet.quality_gate_status = "pending"
    elif fence == "foreign_surface":
        payload["page"]["surface_kind"] = "web"
    assert _has_startup_result_images(packet, request) is (fence == "valid")
