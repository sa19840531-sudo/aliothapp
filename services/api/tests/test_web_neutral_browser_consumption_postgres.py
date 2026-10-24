"""中性 Chrome 实采→默认通知/清单→实际新 Consumer/Writer；仅网络正文和模型为替身。"""

import json
import os
from contextlib import asynccontextmanager
from hashlib import sha256
from uuid import uuid4

import pytest

from services.api.tests import test_web_discovery_runtime_postgres as runtime_test
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="需同时显式提供隔离测试库与非测试库 PostgreSQL DSN",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("interaction", [False, True, "budget", "fences"])
async def test_neutral_real_browser_material_reaches_current_consumer_and_report(
    tmp_path, monkeypatch, interaction, manifest_mode="off"
):
    from services.api.app.application import inspection_composition
    from services.api.app.infrastructure.db import (
        web_capability_revision_repository as capabilities,
    )
    from services.api.app.infrastructure.db import web_evidence_sequence_repository as sequences
    from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
        PostgresH5InspectionPublicationRepository,
    )
    from services.api.app.infrastructure.db.task_insight_repository import (
        PostgresTaskInsightRepository,
    )
    from services.api.tests.web_sequence_consumer_pg_support import (
        consume_sequence_with_actual_work,
    )
    from services.api.tests.web_sequence_stable_pg_support import complete_stable_publication

    captured = {}
    if interaction:
        from services.api.app.application.web_page_readiness import WebObstructionAssessment

        browser_class = runtime_test.DiscoveryBrowser

        class InteractionBrowser(browser_class):
            async def _create_session(self, async_playwright, *, request):
                session = await super()._create_session(async_playwright, request=request)
                await session.context.unroute("**/*")

                async def neutral_page(route):
                    await route.fulfill(
                        content_type="text/html",
                        body='<html><meta charset="utf-8"><title>中性互动</title><body>'
                        '<nav><a href="/news">资讯</a></nav><article><h1>中性文章</h1><p>'
                        + "自然交互测试正文。"
                        * (120 if interaction == "budget" else 25)
                        + "</p></article>"
                        '<div id="modal" class="modal" role="dialog" style="position:fixed;inset:0;z-index:900;background:white">'
                        '<p>中性界面提示</p><button class="modal-close" aria-label="关闭" '
                        'style="position:absolute;right:20px;top:20px;width:100px;height:60px" '
                        "onclick=\"document.getElementById('modal').remove()\">关闭</button></div></body></html>",
                    )

                await session.context.route("**/*", neutral_page)
                return session

        model_class = runtime_test.ModelOnly

        class InteractionModel(model_class):
            async def assess_obstruction(self, **kwargs):
                candidates = kwargs["candidates"]
                return WebObstructionAssessment(
                    obstruction_present=bool(candidates),
                    dismiss_candidate_id=candidates[0]["candidate_id"] if candidates else None,
                    confidence=0.99,
                    reason="固定中性模型响应",
                    human_verification_present=False,
                    age_gate_present=False,
                )

        monkeypatch.setattr(runtime_test, "DiscoveryBrowser", InteractionBrowser)
        monkeypatch.setattr(runtime_test, "ModelOnly", InteractionModel)
    database = runtime_test._database

    @asynccontextmanager
    async def scoped_database():
        async with database() as values:
            captured.update(connection=values[0], pool=values[1], ids=values[3])
            try:
                yield values
            finally:
                try:
                    if interaction == "fences" and captured.get("completed"):
                        from services.api.tests.web_payload_pg_audit_support import (
                            audit_real_payload_fences,
                        )

                        await audit_real_payload_fences(
                            values[0], values[1], values[3], captured["store"], captured["ready"]
                        )
                finally:
                    async with values[0].transaction():
                        await values[0].execute("SET LOCAL session_replication_role=replica")
                        for table in (
                            "qc_completion_gate_snapshot",
                            "qc_review_policy_gate_decision",
                        ):
                            await values[0].execute(
                                f"DELETE FROM {table} WHERE task_id=$1::uuid", values[3]["task"]
                            )
                    if captured.get("rule"):
                        await values[0].execute(
                            "DELETE FROM qc_h5_inspection_publication_work_item WHERE publication_id IN (SELECT id FROM qc_h5_inspection_publication WHERE task_id=$1::uuid)",
                            values[3]["task"],
                        )
                        await values[0].execute(
                            "DELETE FROM qc_h5_inspection_publication WHERE task_id=$1::uuid",
                            values[3]["task"],
                        )
                        await values[0].execute(
                            "DELETE FROM qc_inspection_work_item_rule WHERE work_item_id IN (SELECT id FROM qc_inspection_work_item WHERE task_id=$1::uuid)",
                            values[3]["task"],
                        )
                        await values[0].execute(
                            "DELETE FROM qc_web_exploration_budget WHERE agent_run_id=$1::uuid",
                            values[3]["run"],
                        )
                        await values[0].execute(
                            "DELETE FROM qc_task WHERE id=$1::uuid", values[3]["task"]
                        )
                        await values[0].execute(
                            "DELETE FROM qc_rule WHERE id=$1::uuid", captured["rule"]
                        )

    monkeypatch.setattr(runtime_test, "_database", scoped_database)
    store_type = runtime_test.LocalEvidenceArtifactStore

    def real_store(**kwargs):
        captured["store"] = store_type(**kwargs)
        return captured["store"]

    monkeypatch.setattr(runtime_test, "LocalEvidenceArtifactStore", real_store)

    def real_payload_gate():
        from services.api.app.application.web_capture_preservation import (
            build_web_payload_exit_gate,
        )
        from services.api.app.core.config import Settings

        settings = Settings()
        settings.web_evidence_exit_gate.mode = "on"
        return build_web_payload_exit_gate(
            settings=settings, pool=captured["pool"], artifact_store=captured["store"]
        )

    monkeypatch.setattr(runtime_test, "RecordingEvidenceExitGate", real_payload_gate)
    registry = runtime_test._registry

    def neutral_registry():
        body = registry().to_payload()
        body["catalog_id"] = "t10-neutral-controlled"
        body["version"] = "t10-neutral-20260911"
        for site in body["sites"]:
            site["site_id"] = "t10-neutral"
            site["catalog_version"] = body["version"]
            site["entry_defaults"]["version"] = body["version"]
        return runtime_test.WebSiteMenuRegistry(payload=body)

    monkeypatch.setattr(runtime_test, "_registry", neutral_registry)
    provider = runtime_test.composition.PostgresWebRuntimeProvider

    class CurrentProvider(provider):
        async def build(self, *, record, settings):
            settings.web_evidence_exit_gate.mode = "on"
            if "notification" not in captured:
                connection, pool, ids = (captured[key] for key in ("connection", "pool", "ids"))
                record.task_config.update(
                    execution_mode="full_inspection",
                    web_inspection_notification_mode="enforced",
                    h5_inspection_notification_mode="enforced",
                )
                await connection.execute(
                    "UPDATE qc_task SET task_config=$2::jsonb WHERE id=$1::uuid",
                    ids["task"],
                    json.dumps(record.task_config),
                )
                rule = str(uuid4())
                captured["rule"] = rule
                from services.api.tests.test_web_rule_evidence_contract import _contract

                contract = _contract()
                contract.update(
                    accepted_roles=["stable_page", "interaction_sequence"],
                    minimum_duration_ms=0,
                    accept_unclassified=False,
                )
                input_contract = {
                    "target_clients": ["WEB"],
                    "required_artifacts": ["screenshot", "dom"],
                }
                if interaction:
                    input_contract["web_evidence"] = contract
                await connection.execute(
                    "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,status,check_point,"
                    "input_contract,judge_config,manual_review_policy,applicable_page_types) VALUES($1::uuid,$2::uuid,"
                    "'t10_neutral_material','中性材料验证','visual','llm','medium','enabled','验证当前自然正文',"
                    "$3::jsonb,"
                    '\'{"tool_refs":["semantic_rule_judge_tool"]}\',\'{"min_confidence":0.8}\',\'["content_detail"]\')',
                    rule,
                    ids["ruleset"],
                    json.dumps(input_contract),
                )
                for name in (
                    "PostgresWebCaptureEvidenceRepository",
                    "PostgresH5EvidenceInventoryRepository",
                    "PostgresH5InspectionPublicationRepository",
                    "PostgresEvidenceManifestRepository",
                ):
                    original = getattr(inspection_composition, name)
                    monkeypatch.setattr(
                        inspection_composition,
                        name,
                        lambda _cls=original, **kwargs: _cls(pool=pool, **kwargs),
                    )
                for module, name in (
                    (sequences, "PostgresWebEvidenceSequenceRepository"),
                    (capabilities, "PostgresWebCapabilityRevisionRepository"),
                ):
                    original = getattr(module, name)
                    monkeypatch.setattr(
                        module, name, lambda _cls=original, **kwargs: _cls(pool=pool, **kwargs)
                    )
                monkeypatch.setattr(
                    inspection_composition,
                    "build_evidence_artifact_store",
                    lambda _: captured["store"],
                )
                from services.api.app.infrastructure.db import (
                    web_payload_readiness_repository as payload_repository,
                )

                original_payload_repository = (
                    payload_repository.PostgresWebPayloadReadinessRepository
                )
                monkeypatch.setattr(
                    payload_repository,
                    "PostgresWebPayloadReadinessRepository",
                    lambda **kwargs: original_payload_repository(
                        pool=kwargs.get("pool", pool), artifact_store=captured["store"]
                    ),
                )
                captured["notification"] = (
                    inspection_composition.build_default_web_page_inspection_notification(
                        settings=settings
                    )
                )
                original_retry = captured["notification"]._retryable_result

                async def record_retry(**kwargs):
                    captured["notification_error"] = kwargs["error"]
                    return await original_retry(**kwargs)

                monkeypatch.setattr(captured["notification"], "_retryable_result", record_retry)
            self._page_inspection_notification = captured["notification"]
            return await super().build(record=record, settings=settings)

    monkeypatch.setattr(runtime_test.composition, "PostgresWebRuntimeProvider", CurrentProvider)
    prepare_home = runtime_test.composition.WebV2Runtime.prepare_home

    async def consume_home(runtime, command, **kwargs):
        result = await prepare_home(runtime, command, **kwargs)
        connection, pool, ids = (captured[key] for key in ("connection", "pool", "ids"))
        row = await connection.fetchrow(
            "SELECT id::text,row_version FROM qc_h5_inspection_publication "
            "WHERE task_id=$1::uuid AND page_capture_id=$2::uuid "
            "AND (web_evidence_sequence_id IS NOT NULL)=$3",
            ids["task"],
            result.ready_capture_id,
            bool(interaction),
        )
        diagnostic = await connection.fetch(
            "SELECT id::text,page_state,metadata->'web_inspection_notification' AS notification "
            "FROM qc_page_capture WHERE task_id=$1::uuid",
            ids["task"],
        )
        if row is None:
            print("T10_NEUTRAL_DIAGNOSTIC", json.dumps([dict(item) for item in diagnostic]))
            if captured.get("notification_error"):
                raise captured["notification_error"]
        assert row is not None, [dict(item) for item in diagnostic]
        publications = PostgresH5InspectionPublicationRepository(pool=pool)
        claim = await publications.claim(
            publication_id=row["id"],
            task_id=ids["task"],
            message_version=row["row_version"],
            lease_owner="t10-neutral",
            lease_seconds=60,
        )
        assert claim.lease
        if interaction:
            assert await publications.process_web_sequence(lease=claim.lease)
        else:
            await complete_stable_publication(
                pool, ids, result.ready_capture_id, publications, claim.lease, monkeypatch
            )
        stored = {}
        artifacts = await connection.fetch(
            "SELECT id::text,storage_path,checksum,artifact_type FROM qc_evidence_artifact WHERE task_id=$1::uuid",
            ids["task"],
        )
        for artifact in artifacts:
            content = await captured["store"].read(artifact["storage_path"])
            assert artifact["checksum"] == "sha256:" + sha256(content).hexdigest()
            if artifact["artifact_type"] == "screenshot":
                assert content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) > 1000
            stored[artifact["storage_path"]] = content
        consumed, packets = await consume_sequence_with_actual_work(
            connection, pool, stored, ids, claim.lease.consumer_agent_run_id, monkeypatch,
            factory_mode="on" if manifest_mode == "on" else None,
        )
        assert consumed.completed_count == 1
        assert bool(packets) is (interaction != "budget")
        observed_artifacts = {artifact["id"] for artifact in artifacts}
        assert all(set(packet.evidence_artifact_ids) <= observed_artifacts for packet in packets)
        members = await connection.fetch(
            "SELECT manifest.manifest_payload FROM qc_evidence_input_set_member member "
            "JOIN qc_evidence_input_set inputs USING(input_set_id) "
            "JOIN qc_evidence_manifest manifest ON manifest.manifest_id=member.manifest_id "
            "AND manifest.manifest_hash=member.manifest_hash AND manifest.manifest_version=member.manifest_version "
            "WHERE inputs.task_id=$1::uuid",
            ids["task"],
        )
        if interaction:
            assert members
        else:
            # 默认 stable 按 Capture/Artifact 消费；只做同源对账，不伪造持久 Manifest 绑定。
            assert not members
            assert packets and all(packet.page_capture_id == result.ready_capture_id for packet in packets)
            members = await connection.fetch(
                "SELECT manifest_id,manifest_payload FROM qc_evidence_manifest "
                "WHERE task_id=$1::uuid AND capture_id=$2::uuid AND quality_status='pass' "
                "ORDER BY manifest_version DESC LIMIT 1", ids["task"], result.ready_capture_id,
            )
            assert len(members) == 1
            if manifest_mode == "on":
                assert all(packet.packet_payload["quality_gate"]["manifest_id"] == members[0]["manifest_id"] for packet in packets)
            else:
                assert all("manifest_id" not in packet.packet_payload["quality_gate"] for packet in packets)
                assert all("evidence_manifest_id" not in packet.packet_payload["identity"] for packet in packets)
        frozen_artifacts = set()
        for member in members:
            for artifact in json.loads(member["manifest_payload"])["artifacts"]:
                assert artifact["artifact_id"] in observed_artifacts
                content = stored[artifact["storage_path"]]
                assert artifact["size_bytes"] == len(content)
                assert artifact["checksum"] == "sha256:" + sha256(content).hexdigest()
                frozen_artifacts.add(artifact["artifact_id"])
        assert all(set(packet.evidence_artifact_ids) <= frozen_artifacts for packet in packets)
        assert await connection.fetchval(
            "SELECT execution_status FROM qc_rule_execution WHERE task_id=$1::uuid AND rule_id=$2::uuid",
            ids["task"],
            captured["rule"],
        ) == ("evidence_insufficient" if interaction == "budget" else "pass")
        if interaction == "budget":
            packet = json.loads(
                await connection.fetchval(
                    "SELECT packet_payload FROM qc_observation_packet WHERE task_id=$1::uuid",
                    ids["task"],
                )
            )
            assert packet["web_evidence"]["reason_code"] == "WEB_SEQUENCE_FACT_BUDGET_EXCEEDED"
            assert (
                packet["web_evidence"]["input_set_id"]
                and packet["web_evidence"]["rule_contract_hash"]
            )
        view = await PostgresTaskInsightRepository(pool=pool).fetch_web_report_explanation(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        expected_member_status = "evidence_insufficient" if interaction == "budget" else "written"
        assert any(item["status"] == expected_member_status for item in view["rule_materials"])
        if interaction:
            assert any(
                item["status"] == expected_member_status
                and item["material_role"] == "sequence"
                and item["evidence_input_set_id"]
                and item["sequence_id"]
                for item in view["rule_materials"]
            )
        assert (
            runtime.policy_snapshot.to_payload()["menu_priors"]["version"] == "t10-neutral-20260911"
        )
        captured["completed"] = True
        captured["ready"] = result.ready_capture_id
        return result

    monkeypatch.setattr(runtime_test.composition.WebV2Runtime, "prepare_home", consume_home)
    await runtime_test.test_real_v2_frozen_discovery_runtime_preserves_sources_after_actions_and_cold_read(
        tmp_path, monkeypatch, href=True, same_url=False, enabled=False
    )
    assert captured.get("completed")


@pytest.mark.asyncio
async def test_neutral_stable_uses_real_shadow_comparison_and_manifest_on_consumer(tmp_path, monkeypatch):
    from services.api.tests import web_sequence_factory_pg_support as factories
    from services.api.tests.web_neutral_manifest_read_support import build_neutral_stable_factory

    monkeypatch.setattr(factories, "build_actual_factory", build_neutral_stable_factory)
    await test_neutral_real_browser_material_reaches_current_consumer_and_report(
        tmp_path, monkeypatch, False, manifest_mode="on")
