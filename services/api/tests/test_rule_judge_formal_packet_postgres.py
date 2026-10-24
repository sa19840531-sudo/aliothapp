"""真实 PG 复现确定性 Judge 的正式观察包与旧简包同身份冲突。"""

import json
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw

from services.api.app.application.manifest_backed_packet import ManifestBackedPacketLoader
from services.api.app.application.observation_packet import (
    BuildObservationPacketRequest,
    ObservationPacketService,
)
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeEvidenceInsufficientError,
    RuleJudgeRequest,
    RuleJudgeService,
    RuleJudgeValidationError,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.observation_packet_repository import (
    PostgresObservationPacketRepository,
)
from services.api.app.infrastructure.rule_judge_evidence import PostgresRuleJudgeEvidenceReader
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_action_scene_repository_postgres import _pair
from services.api.tests.test_app_capture_session_auth_postgres import _database
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)
from services.api.tests.test_rule_judge import FakeRuleJudgeRepository


async def _setup(connection, pool, ids, saved, tmp_path, *, manifest):
    saved = replace(saved, width=64, height=128)
    await connection.execute(
        "UPDATE qc_app_capture SET width=64,height=128 WHERE capture_id=$1::uuid",
        saved.capture_id,
    )
    output = BytesIO()
    image = Image.new("RGB", (saved.width, saved.height), "white")
    ImageDraw.Draw(image).rectangle((10, 10, saved.width // 2, saved.height // 2), fill="blue")
    image.save(output, format="PNG")
    before, after, _action = await _pair(
        connection,
        pool,
        ids,
        saved,
        tmp_path,
        screenshot_payload=output.getvalue(),
    )
    rule_id = str(uuid4())
    await connection.execute(
        """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,description,category,severity,
               executor_type,check_point,judge_config,input_contract)
           VALUES($1::uuid,$2::uuid,'pair-assertion','配对截图','可信前后截图','ad','medium',
                  'rule_engine','截图发生变化','{}','{}')""",
        rule_id,
        ids["ruleset"],
    )
    await connection.execute(
        "UPDATE qc_capture SET metadata=metadata || "
        '\'{"page_type":"home","page_state":"stable"}\'::jsonb WHERE id=$1::uuid',
        after,
    )
    definition = RuleJudgeDefinition(
        rule_id=rule_id,
        rule_code="pair-assertion",
        executor_type="rule_engine",
        input_contract={
            "required_capture_roles": ["before", "primary"],
            "required_artifacts": ["screenshot"],
            "quality_gate": {"allow_empty_visible_text": True},
        },
        judge_config={
            "engine_config": {
                "handler": "screenshot_change_assertion",
                "before_role": "before",
                "after_role": "primary",
            }
        },
    )
    store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
    packet_service = ObservationPacketService(
        repository=PostgresObservationPacketRepository(
            pool=pool,
            artifact_store=store,
        )
    )
    loader = (
        ManifestBackedPacketLoader(
            manifests=PostgresEvidenceManifestRepository(pool=pool),
            artifact_reader=store,
        )
        if manifest
        else None
    )
    reader = PostgresRuleJudgeEvidenceReader(
        pool=pool, artifact_store=store, manifest_loader=loader
    )
    request = RuleJudgeRequest(
        task_id=ids["task"],
        page_capture_id=after,
        rule_code=definition.rule_code,
        evidence_artifact_ids=(),
        judge_input={},
        agent_run_id=ids["run"],
        source_agent_run_id=ids["run"],
        evidence_capture_roles={"before": before, "primary": after},
    )
    return definition, packet_service, reader, request


def _build_request(definition, request):
    return BuildObservationPacketRequest(
        task_id=request.task_id,
        page_capture_id=request.page_capture_id,
        rule_id=definition.rule_id,
        rule_code=definition.rule_code,
        rule_name="配对截图",
        category="ad",
        executor_type="rule_engine",
        check_point="截图变化",
        input_contract=definition.input_contract,
        judge_config=definition.judge_config,
        manual_review_policy={},
        requested_evidence_artifact_ids=(),
        agent_input={},
        agent_run_id=request.agent_run_id,
        source_agent_run_id=request.source_agent_run_id,
        evidence_capture_roles=request.evidence_capture_roles,
    )


def _read_options(definition, request, packet):
    return dict(
        task_id=request.task_id,
        agent_run_id=request.agent_run_id,
        source_agent_run_id=request.source_agent_run_id,
        rule_id=definition.rule_id,
        page_capture_id=request.page_capture_id,
        evidence_capture_roles=request.evidence_capture_roles,
        evidence_artifact_ids=(),
        required_artifact_types=("screenshot",),
        frozen_observation_packet=packet,
    )


@pytest.mark.parametrize("manifest", [False, True])
async def test_deterministic_judge_reuses_exact_formal_packet_without_second_schema_write(
    tmp_path,
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
        service = RuleJudgeService(
            repository=FakeRuleJudgeRepository(definition),
            observation_packet_service=packets,
            evidence_reader=reader,
        )
        first = await service.judge(request)
        second = await service.judge(request)
        assert first.execution_status == second.execution_status == "fail"
        assert first.observation_packet_id == second.observation_packet_id
        rows = await connection.fetch(
            "SELECT packet_payload FROM qc_observation_packet WHERE task_id=$1::uuid",
            ids["task"],
        )
        assert len(rows) == 1
        assert json.loads(rows[0]["packet_payload"])["schema_version"] == "observation-packet.v1"
        # 旧入口仍拒绝对同身份写不同简包；修复没有放松冻结校验。
        options = _read_options(definition, request, None)
        with pytest.raises(RuleJudgeValidationError, match="冻结"):
            await reader.prepare_observation_packet(**options)


@pytest.mark.parametrize(
    "fence",
    [
        "payload",
        "reason",
        "roles",
        "artifacts",
        "consumer_run",
        "producer_run",
        "manifest_version",
        "quality",
    ],
)
async def test_formal_packet_reuse_preserves_frozen_content_and_evidence_fences(tmp_path, fence):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        definition, packets, reader, request = await _setup(
            connection,
            pool,
            ids,
            saved,
            tmp_path,
            manifest=True,
        )
        packet = await packets.build(_build_request(definition, request))
        options = _read_options(definition, request, packet)
        if fence in {"payload", "roles"}:
            payload = deepcopy(packet.packet_payload)
            if fence == "payload":
                payload["page"]["page_state"] = "drifted"
            else:
                payload["identity"]["capture_roles"]["before"] = str(uuid4())
            options["frozen_observation_packet"] = replace(packet, packet_payload=payload)
        elif fence == "reason":
            options["frozen_observation_packet"] = replace(packet, quality_gate_reason="漂移原因")
        elif fence == "artifacts":
            options["frozen_observation_packet"] = replace(
                packet,
                evidence_artifact_ids=packet.evidence_artifact_ids[:1],
            )
        elif fence == "consumer_run":
            options["agent_run_id"] = str(uuid4())
        elif fence == "producer_run":
            options["source_agent_run_id"] = str(uuid4())
        elif fence == "quality":
            options["frozen_observation_packet"] = replace(packet, quality_gate_status="pending")
        else:
            latest = await PostgresEvidenceManifestRepository(pool=pool).get_latest_for_capture(
                task_id=ids["task"],
                capture_id=request.page_capture_id,
            )
            options["evidence_manifest_refs"] = {
                "primary": {
                    "manifest_id": latest.manifest_id,
                    "manifest_version": str(latest.manifest_version + 1),
                }
            }
        with pytest.raises((RuleJudgeValidationError, RuleJudgeEvidenceInsufficientError)):
            await reader.prepare_observation_packet(**options)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_observation_packet WHERE task_id=$1::uuid",
                ids["task"],
            )
            == 1
        )


async def test_reader_accepts_declared_primary_subset_of_formal_frozen_pair(tmp_path):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        definition, packets, reader, request = await _setup(
            connection,
            pool,
            ids,
            saved,
            tmp_path,
            manifest=True,
        )
        packet = await packets.build(_build_request(definition, request))
        options = _read_options(definition, request, packet)
        options["evidence_capture_roles"] = {"primary": request.page_capture_id}
        bundle = await reader.prepare_observation_packet(**options)
        assert bundle.observation_packet_id == packet.observation_packet_id
        assert {item.page_capture_id for item in bundle.artifacts} == {request.page_capture_id}
        assert {item.artifact_id for item in bundle.artifacts} < set(packet.evidence_artifact_ids)
