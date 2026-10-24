"""受控真实 PG 动作账本贯通 H5 台账、局部脱敏版本与公开整组导出。"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import httpx

from services.api.app.api.v2 import h5_shortflows
from services.api.app.application.journey_definitions import JourneyDefinitionService
from services.api.app.domain.exploration import build_exploration_work
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    SurfaceKind,
)
from services.api.app.domain.journey import parse_journey_definition
from services.api.app.infrastructure.db.checkpoint_execution_repository import (
    PostgresCheckpointExecutionRepository,
)
from services.api.app.infrastructure.db.h5_shortflow_read_repository import (
    PostgresH5ShortflowReadRepository,
)
from services.api.app.infrastructure.db.journey_definition_repository import (
    PostgresJourneyDefinitionRepository,
)
from services.api.app.infrastructure.db.user_repository import PostgresUserRepository
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_checkpoint_evidence_postgres import save_receipt
from services.api.tests.test_evidence_redaction_api_postgres import (
    _sha,
    _write_json,
    assert_reviewable,
    build_app,
    materials,
    retained_case,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_h5_exploration_turn import _work
from services.api.tests.test_journey_checkpoint_contracts import checkpoint_definition


async def prepare_checkpoint(connection, pool, ids):
    """显式夹具定义只读动作，不把控制账本当作原站或真机验收。"""
    await connection.execute(
        "INSERT INTO qc_page_capture (id,task_id,agent_run_id,page_seq,page_slug,url,"
        "capture_status,surface_session_id,capture_role,captured_at,metadata,"
        "session_command_sequence) "
        "VALUES ($1::uuid,$2::uuid,$3::uuid,10,'source','https://example.test/',"
        "'captured',$4,'bootstrap',now(),'{\"finalization_epoch\":0}'::jsonb,1)",
        ids["capture"],
        ids["task"],
        ids["run"],
        ids["session"],
    )
    sample = _work(capture_id=ids["capture"])
    frontier = replace(
        sample.frontier, observation=replace(sample.frontier.observation, page_state="stable")
    )
    work = build_exploration_work(
        task_id=ids["task"],
        session_id=ids["session"],
        branch_id="redaction-controlled",
        objective=sample.objective,
        surface_kind=SurfaceKind.WEB,
        source_capture_id=ids["capture"],
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=sample.boundaries,
        budget=sample.budget,
        deadline=sample.deadline,
    )
    await connection.execute(
        "INSERT INTO qc_v2_work_item (work_id,work_type,task_id,session_id,branch_id,"
        "source_capture_id,"
        "protocol_version,payload_hash,work_payload,status,deadline,lease_owner,lease_token,"
        "lease_expires_at,started_at) VALUES ($1,'exploration',$2::uuid,$3,$4,$5::uuid,"
        "'exploration-work.v1','controlled',$6::jsonb,'running',$7,'redaction-test',"
        "gen_random_uuid(),$7,now())",
        work.work_id,
        work.task_id,
        work.session_id,
        work.branch_id,
        work.source_capture_id,
        json.dumps(work.to_dict()),
        work.deadline,
    )
    definitions = JourneyDefinitionService(
        repository=PostgresJourneyDefinitionRepository(pool=pool)
    )
    payload = checkpoint_definition("redaction-business-review-" + uuid4().hex)
    await definitions.publish(parse_journey_definition(payload))
    plan = await definitions.create_run_execution_plan(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        definition_id=payload["definition_id"],
        version=1,
        scenario_key="manual",
        parameters={},
        rule_contract_hashes=(),
    )
    context = {
        "attempt_id": "redaction-attempt-" + uuid4().hex,
        "execution_plan_id": plan.execution_plan_id,
        "checkpoint_id": "ready",
        "task_id": work.task_id,
        "agent_run_id": ids["run"],
        "session_id": work.session_id,
        "source_capture_id": work.source_capture_id,
        "work_id": work.work_id,
    }
    decision = ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="redaction-intent-" + uuid4().hex,
        idempotency_key="redaction-key-" + uuid4().hex,
        source_capture_id=work.source_capture_id,
        surface_kind=SurfaceKind.WEB,
        action_type=InteractionOperation.CLICK,
        semantic_target="受控只读按钮",
        purpose="脱敏整组导出隔离验收",
        capture_profile=CaptureProfile(
            profile_id="controlled",
            before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
            after=CaptureDirective(role="after_action", artifact_types=("screenshot",)),
        ),
        deadline=work.deadline,
    )
    repository = PostgresCheckpointExecutionRepository(pool=pool)
    await repository.reserve(work=work, context=context, decision=decision)
    return repository, work, context, decision


async def real_receipt(connection, ids, work, decision, store):
    """复用受控动作夹具后，封存前以真实文件重新追加 Manifest 版本。"""
    from services.api.app.domain.evidence import (
        ArtifactReference,
        EvidenceQualityStatus,
        build_evidence_manifest_reference,
    )
    from services.api.app.domain.protocol import stable_protocol_hash

    turn = await save_receipt(connection, ids, work, decision)
    manifests = []
    image = materials()[0][1]
    for capture in (turn.coordination.before_capture, turn.coordination.after_capture):
        artifact_id = capture.artifact_ids[0]
        actual = await store.store(
            task_id=ids["task"],
            page_capture_id=capture.capture_id,
            artifact_id=artifact_id,
            artifact_type="screenshot",
            content=image,
        )
        # 旧版为未通过的夹具占位，原本没有对象字节；实际放行前追加真实内容清单。
        await connection.execute(
            "UPDATE qc_evidence_artifact SET storage_path=$2,checksum=$3,size_bytes=$4 "
            "WHERE id=$1::uuid",
            artifact_id,
            actual.storage_path,
            actual.checksum,
            actual.size_bytes,
        )
        manifest = build_evidence_manifest_reference(
            task_id=ids["task"],
            session_id=ids["session"],
            capture_id=capture.capture_id,
            manifest_version=2,
            quality_status=EvidenceQualityStatus.PASS,
            created_at=datetime.now(UTC),
            artifacts=(
                ArtifactReference(
                    artifact_id=artifact_id,
                    artifact_type="screenshot",
                    role=capture.role,
                    storage_path=actual.storage_path,
                    checksum=actual.checksum,
                    size_bytes=actual.size_bytes,
                    content_type=actual.content_type,
                ),
            ),
        )
        await connection.execute(
            "INSERT INTO qc_evidence_manifest (manifest_id,manifest_version,task_id,session_id,"
            "capture_id,protocol_version,manifest_hash,payload_hash,quality_status,manifest_payload,"
            "created_at) VALUES ($1,2,$2::uuid,$3,$4::uuid,'evidence-manifest.v1',$5,$6,'pass',"
            "$7::jsonb,$8)",
            manifest.manifest_id,
            ids["task"],
            ids["session"],
            capture.capture_id,
            manifest.manifest_hash,
            stable_protocol_hash(manifest.to_dict()),
            json.dumps(manifest.to_dict()),
            manifest.created_at,
        )
        manifests.append(manifest)
    return turn, manifests


async def test_public_h5_archive_exports_frozen_reviewable_members_and_plan_audit():
    async with retained_case() as (connection, pool, ids, root):
        store = LocalEvidenceArtifactStore(root=root / "originals")
        repository, work, context, decision = await prepare_checkpoint(connection, pool, ids)
        turn, manifests = await real_receipt(connection, ids, work, decision, store)
        await repository.bind(work=work, context=context, turn=turn)
        original_rows = await connection.fetch(
            "SELECT * FROM qc_evidence_artifact WHERE task_id=$1::uuid ORDER BY id",
            ids["task"],
        )
        user = await PostgresUserRepository(pool=pool).find_by_id(ids["user"])
        app = build_app(pool, store, user)
        app.dependency_overrides[h5_shortflows._get_repository] = lambda: (
            PostgresH5ShortflowReadRepository(pool=pool)
        )
        redaction_ids, plans = {}, []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            snapshot_response = await client.get(f"/api/v2/tasks/{ids['task']}/h5-shortflows")
            assert snapshot_response.status_code == 200, snapshot_response.text
            snapshot = snapshot_response.json()
            bindings = snapshot["plans"][0]["checkpoints"][0]["attempts"][0]["bindings"]
            assert len(bindings) == 2 and all(item["integrity"] == "current" for item in bindings)
            for manifest in manifests:
                member = manifest.artifacts[0]
                created = await client.post(
                    f"/api/v2/evidence/{member.artifact_id}/redactions",
                    json={
                        "source_checksum": member.checksum,
                        "manifest_id": manifest.manifest_id,
                        "manifest_version": 2,
                        "spec": materials()[0][2],
                        "review_confirmed": True,
                    },
                )
                assert created.status_code == 200, created.text
                plans.append(created.json())
                redaction_ids[member.artifact_id] = created.json()["redaction_id"]
            exported = await client.post(
                f"/api/v2/tasks/{ids['task']}/h5-shortflows/{context['execution_plan_id']}/archive",
                json={"snapshot_hash": snapshot["snapshot_hash"], "redaction_ids": redaction_ids},
            )
            assert exported.status_code == 200, exported.text
        with ZipFile(BytesIO(exported.content)) as archive:
            receipt = json.loads(archive.read("receipt.json"))
            assert len(receipt["materials"]) == 2
            for member in receipt["materials"]:
                assert member["redaction_id"] == redaction_ids[member["artifact_id"]]
                assert member["business_reviewable"] is True
                assert_reviewable("screenshot", materials()[0][1], archive.read(member["path"]))
        (root / "business-review.zip").write_bytes(exported.content)
        _write_json(root / "h5-snapshot.json", snapshot)
        _write_json(root / "manifests.json", [item.to_dict() for item in manifests])
        audit = await connection.fetch(
            "SELECT * FROM qc_evidence_download_audit WHERE export_request_id=$1",
            receipt["request_id"],
        )
        assert len(audit) == 2
        assert {str(item["redaction_id"]) for item in audit} == set(redaction_ids.values())
        assert all(item["archive_checksum"] == _sha(exported.content) for item in audit)
        assert {item["redaction_plan_hash"] for item in audit} == {
            item["plan_hash"] for item in plans
        }
        assert (
            await connection.fetch(
                "SELECT * FROM qc_evidence_artifact WHERE task_id=$1::uuid ORDER BY id",
                ids["task"],
            )
            == original_rows
        )
        _write_json(
            root / "h5-receipt.json",
            {
                "ids": ids,
                "checkpoint": context,
                "plans": plans,
                "archive": receipt,
                "archive_checksum": _sha(exported.content),
                "audit": [dict(item) for item in audit],
                "controlled": True,
            },
        )
        print(f"H5 局部脱敏真实 API/PG 回执：{root / 'h5-receipt.json'}")
