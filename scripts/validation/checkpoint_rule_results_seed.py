"""仅为独立实例建立受控展示样例，不代表真实站点采集或绿色规则验收。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
from PIL import Image, ImageDraw

from services.api.app.api.v1.auth import hash_password
from services.api.app.application.aggregate_inspection_execution import AggregateInspectionExecutor
from services.api.app.application.aggregate_inspection_publication import (
    AggregateInspectionPublicationService,
)
from services.api.app.application.journey_definitions import JourneyDefinitionService
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.exploration import build_exploration_work
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    SurfaceKind,
)
from services.api.app.domain.journey import parse_journey_definition
from services.api.app.infrastructure.browser.ui_snapshot_reader import (
    PostgresTrustedUiSnapshotReader,
)
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    PostgresAggregateInspectionExecutionRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.checkpoint_execution_repository import (
    PostgresCheckpointExecutionRepository,
)
from services.api.app.infrastructure.db.journey_definition_repository import (
    PostgresJourneyDefinitionRepository,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_checkpoint_evidence_postgres import save_receipt
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import _seed_context
from services.api.tests.test_h5_checkpoint_consumption import sample
from services.api.tests.test_h5_exploration_turn import _work
from services.api.tests.test_journey_consumption_postgres import configure, definition


def _ids():
    values = {
        key: str(uuid4())
        for key in (
            "tenant",
            "user",
            "ruleset",
            "rule",
            "task",
            "run",
            "capture",
            "old_capture",
            "bootstrap_capture",
            "before_capture",
            "inspection_work_item",
            "old_inspection_work_item",
        )
    }
    for key in (
        "work",
        "session",
        "index",
        "action",
        "bootstrap_manifest",
        "request",
        "old_request",
    ):
        values[key] = f"checkpoint-display-{key}-{uuid4()}"
    return values


async def _materials(connection, store, turn, outcome):
    references = []
    for index, reference in enumerate(
        (turn.coordination.before_capture, turn.coordination.after_capture)
    ):
        observed = sample(paused=bool(index) and outcome != "fail")["snapshot"]
        if index and outcome == "evidence_insufficient":
            observed["elements"][0]["media"].pop("paused")
        image = Image.new("RGB", (360, 640), "#eaf1f8" if not index else "#e3efe6")
        ImageDraw.Draw(image).multiline_text(
            (20, 25),
            f"CONTROLLED DISPLAY FIXTURE\n{outcome}\n{reference.role}\nNot a captured product page",
            fill="#17314b",
            spacing=10,
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        saved_image = await store.store(
            task_id=turn.task_id,
            page_capture_id=reference.capture_id,
            artifact_id=reference.artifact_ids[0],
            artifact_type="screenshot",
            content={"encoding": "base64", "data": base64.b64encode(buffer.getvalue()).decode()},
        )
        await connection.execute(
            "UPDATE qc_evidence_artifact SET storage_path=$2,checksum=$3,size_bytes=$4,"
            "metadata=$5::jsonb WHERE id=$1::uuid",
            saved_image.artifact_id,
            saved_image.storage_path,
            saved_image.checksum,
            saved_image.size_bytes,
            json.dumps({"fixture": "controlled-display", **saved_image.metadata}),
        )
        saved_snapshot = await store.store(
            task_id=turn.task_id,
            page_capture_id=reference.capture_id,
            artifact_id=str(uuid4()),
            artifact_type="ui_snapshot",
            content=observed,
        )
        await connection.execute(
            "INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,source,"
            "storage_path,checksum,size_bytes,content_type) VALUES($1::uuid,$2::uuid,$3::uuid,"
            "'ui_snapshot','baseline',$4,$5,$6,$7)",
            saved_snapshot.artifact_id,
            turn.task_id,
            reference.capture_id,
            saved_snapshot.storage_path,
            saved_snapshot.checksum,
            saved_snapshot.size_bytes,
            saved_snapshot.content_type,
        )
        artifacts = await connection.fetch(
            "SELECT * FROM qc_evidence_artifact WHERE page_capture_id=$1::uuid",
            reference.capture_id,
        )
        manifest = build_evidence_manifest_reference(
            task_id=turn.task_id,
            session_id=reference.session_id,
            capture_id=reference.capture_id,
            manifest_version=2,
            quality_status=EvidenceQualityStatus.PASS,
            created_at=datetime.now(UTC),
            artifacts=tuple(
                ArtifactReference(
                    artifact_id=str(row["id"]),
                    artifact_type=row["artifact_type"],
                    role=reference.role,
                    checksum=row["checksum"],
                    storage_path=row["storage_path"],
                    size_bytes=row["size_bytes"],
                    content_type=row["content_type"],
                )
                for row in artifacts
            ),
        )
        await connection.execute(
            "INSERT INTO qc_evidence_manifest(manifest_id,manifest_version,task_id,session_id,"
            "capture_id,protocol_version,manifest_hash,payload_hash,quality_status,"
            "manifest_payload,created_at) "
            "VALUES($1,2,$2::uuid,$3,$4::uuid,'evidence-manifest.v1',$5,'controlled-display',"
            "'pass',$6::jsonb,now())",
            manifest.manifest_id,
            turn.task_id,
            reference.session_id,
            reference.capture_id,
            manifest.manifest_hash,
            json.dumps(manifest.to_dict()),
        )
        metadata = {
            "finalization_epoch": 0,
            "page_state": "stable",
            "pipeline_metrics": {"page_inspection_ready": True},
            "presentation": {
                "protocol_version": "presentation-stack.v1",
                "topmost_layer_kind": "page",
                "base_page_visibility": "visible",
                "blocking_scope": "none",
                "mutation_mode": "natural",
            },
            "fixture": "controlled-display",
        }
        await connection.execute(
            "UPDATE qc_page_capture SET metadata=$2::jsonb,url=$3 WHERE id=$1::uuid",
            reference.capture_id,
            json.dumps(metadata),
            observed["url"],
        )
        references.append(
            replace(reference, artifact_ids=tuple(str(row["id"]) for row in artifacts))
        )
    return replace(
        turn,
        coordination=replace(
            turn.coordination,
            before_capture=references[0],
            after_capture=references[1],
        ),
    )


async def _seed_case(connection, pool, store, outcome):
    ids = _ids()
    await _seed_context(connection, ids, surface_kind="web", target_platform="h5")
    await connection.execute(
        "UPDATE qc_user SET role='公司管理员' WHERE id=$1::uuid",
        ids["user"],
    )
    await connection.execute(
        "UPDATE qc_agent_run SET agent_type='main',agent_name='受控展示夹具' WHERE id=$1::uuid",
        ids["run"],
    )
    await connection.execute(
        "UPDATE qc_task SET task_config=coalesce(task_config,'{}'::jsonb) || "
        "jsonb_build_object('task_name',$2::text,'fixture','controlled-display') WHERE id=$1::uuid",
        ids["task"],
        f"受控展示夹具 · {outcome}",
    )
    await connection.execute(
        "INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,url,capture_status,"
        "surface_session_id,capture_role,captured_at,metadata,session_command_sequence) "
        "VALUES($1::uuid,$2::uuid,$3::uuid,10,'source','https://example.test/video/1','captured',"
        "$4,'bootstrap',now(),'{\"finalization_epoch\":0}'::jsonb,1)",
        ids["capture"],
        ids["task"],
        ids["run"],
        ids["session"],
    )
    model = _work(capture_id=ids["capture"])
    frontier = replace(
        model.frontier, observation=replace(model.frontier.observation, page_state="stable")
    )
    work = build_exploration_work(
        task_id=ids["task"],
        session_id=ids["session"],
        branch_id="controlled-display",
        objective=model.objective,
        surface_kind=SurfaceKind.WEB,
        source_capture_id=ids["capture"],
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=model.boundaries,
        budget=model.budget,
        deadline=model.deadline,
    )
    await connection.execute(
        "INSERT INTO qc_v2_work_item(work_id,work_type,task_id,session_id,branch_id,"
        "source_capture_id,"
        "protocol_version,payload_hash,work_payload,status,deadline,lease_owner,lease_token,"
        "lease_expires_at,started_at) VALUES($1,'exploration',$2::uuid,$3,$4,$5::uuid,"
        "'exploration-work.v1','controlled-display',$6::jsonb,'running',$7,'display-seed',$8::uuid,$7,now())",
        work.work_id,
        ids["task"],
        ids["session"],
        work.branch_id,
        ids["capture"],
        json.dumps(work.to_dict()),
        work.deadline,
        str(uuid4()),
    )
    hashes = await configure(connection, ids) if outcome != "no_contract" else ()
    payload = definition()
    payload["name"] = f"受控展示流程 · {outcome}"
    service = JourneyDefinitionService(repository=PostgresJourneyDefinitionRepository(pool=pool))
    await service.publish(parse_journey_definition(payload))
    plan = await service.create_run_execution_plan(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        definition_id=payload["definition_id"],
        version=1,
        scenario_key="manual",
        parameters={},
        rule_contract_hashes=hashes,
    )
    context = dict(
        attempt_id=f"display-attempt-{uuid4()}",
        execution_plan_id=plan.execution_plan_id,
        checkpoint_id="ready",
        task_id=ids["task"],
        agent_run_id=ids["run"],
        session_id=ids["session"],
        source_capture_id=ids["capture"],
        work_id=work.work_id,
    )
    decision = ActionIntent(
        task_id=ids["task"],
        session_id=ids["session"],
        intent_id=f"display-intent-{uuid4()}",
        idempotency_key=f"display-key-{uuid4()}",
        source_capture_id=ids["capture"],
        surface_kind=SurfaceKind.WEB,
        action_type=InteractionOperation.CLICK,
        semantic_target="受控展示按钮",
        purpose="受控展示数据，未操作真实产品",
        capture_profile=CaptureProfile(
            profile_id="display-fixture",
            before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
            after=CaptureDirective(role="after_action", artifact_types=("screenshot",)),
        ),
        deadline=work.deadline,
    )
    checkpoint_repo = PostgresCheckpointExecutionRepository(pool=pool)
    assert await checkpoint_repo.reserve(work=work, context=context, decision=decision) == "new"
    turn = await _materials(
        connection, store, await save_receipt(connection, ids, work, decision), outcome
    )
    await checkpoint_repo.bind(work=work, context=context, turn=turn)
    await connection.execute(
        "UPDATE qc_task SET status='review',finalization_state='session_draining',"
        "exploration_draining_at=now(),session_draining_at=now(),"
        "finalization_agent_run_id=$2::uuid "
        "WHERE id=$1::uuid",
        ids["task"],
        ids["run"],
    )
    if hashes:
        publisher = AggregateInspectionPublicationService(
            repository=PostgresAggregateInspectionPublicationRepository(pool=pool),
        )
        summary = await publisher.prepare(
            task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0
        )
        assert summary.published_work_item_count == 1
        if outcome != "missing_result":
            queued = await connection.fetchrow(
                "SELECT id::text,row_version FROM qc_aggregate_inspection_work_item "
                "WHERE task_id=$1::uuid",
                ids["task"],
            )
            executor = AggregateInspectionExecutor(
                repository=PostgresAggregateInspectionExecutionRepository(
                    pool=pool,
                    snapshot_reader=PostgresTrustedUiSnapshotReader(
                        pool=pool, artifact_store=store
                    ),
                ),
                lease_owner="controlled-display-seed",
                lease_seconds=30,
                retry_delay_seconds=1,
            )
            assert await executor.execute_wakeup(
                work_item_id=queued["id"],
                task_id=ids["task"],
                message_version=queued["row_version"],
            )
    return {
        **ids,
        "execution_plan_id": plan.execution_plan_id,
        "attempt_id": context["attempt_id"],
        "outcome": outcome,
    }


async def _add_workstation_login(connection, manifest):
    """复用部门角色读取同部门他人任务，不更改真实权限映射或任务提交者。"""
    owner = manifest["cases"][0]
    username, password = "checkpoint-display-workstation", "WorkstationFixture-20261019!"
    department = "受控展示验证部门"
    await connection.execute(
        "INSERT INTO qc_user(id,username,full_name,password_hash,role,tenant_id,department_name) "
        "VALUES($1::uuid,$2,'受控展示部门账号',$3,'规则配置员',$4::uuid,$5) "
        "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,"
        "role=excluded.role,tenant_id=excluded.tenant_id,department_name=excluded.department_name",
        str(uuid4()),
        username,
        hash_password(password),
        owner["tenant"],
        department,
    )
    for item in manifest["cases"]:
        await connection.execute(
            "UPDATE qc_task SET department_name=$2 WHERE id=$1::uuid",
            item["task"],
            department,
        )
    manifest["workstation_login"] = {"username": username, "password": password}
    return manifest


async def main(args):
    database_url = os.environ[args.database_url_env]
    parsed = urlsplit(database_url)
    if (
        parsed.hostname != args.expected_host
        or parsed.path != "/alioth_checkpoint_results_test"
        or parsed.username != "checkpoint_results_test"
    ):
        raise ValueError("拒绝在非明确允许的独立数据库写入夹具")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    try:
        identity = dict(
            await connection.fetchrow(
                "SELECT current_database() AS database,current_user AS role,"
                "(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier",
            )
        )
        assert identity["database"] == "alioth_checkpoint_results_test"
        assert identity["role"] == "checkpoint_results_test"
        if args.add_workstation_user:
            manifest = json.loads((output / "seed-manifest.json").read_text(encoding="utf-8"))
            assert manifest["database_identity"] == identity
            await _add_workstation_login(connection, manifest)
            (output / "seed-manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps({"workstation_user_added": True, "database_identity": identity}))
            return
        store = LocalEvidenceArtifactStore(root=output / "evidence")
        cases = []
        for outcome in ("pass", "fail", "evidence_insufficient", "missing_result", "no_contract"):
            cases.append(await _seed_case(connection, pool, store, outcome))
        # 所有展示任务归同一测试租户，另建真实跨租户只读账号供权限拒绝验证。
        owner = cases[0]
        for item in cases:
            await connection.execute(
                "UPDATE qc_task SET tenant_id=$2::uuid,submitter_id=$3::uuid WHERE id=$1::uuid",
                item["task"],
                owner["tenant"],
                owner["user"],
            )
        username, password = "checkpoint-display-owner", "DisplayFixture-20261019!"
        await connection.execute(
            "UPDATE qc_user SET username=$2,password_hash=$3,full_name='受控展示测试账号' "
            "WHERE id=$1::uuid",
            owner["user"],
            username,
            hash_password(password),
        )
        foreign = cases[-1]
        denied_user, denied_password = "checkpoint-display-other-tenant", "OtherFixture-20261019!"
        await connection.execute(
            "UPDATE qc_user SET username=$2,password_hash=$3,role='审计员',"
            "full_name='跨租户只读测试账号' "
            "WHERE id=$1::uuid",
            foreign["user"],
            denied_user,
            hash_password(denied_password),
        )
        manifest = {
            "fixture_notice": (
                "受控展示夹具；动作账本、JSON观测和PNG均由本脚本生成，未采集真实产品。"
            ),
            "database_identity": identity,
            "cases": cases,
            "login": {"username": username, "password": password},
            "denied_login": {"username": denied_user, "password": denied_password},
        }
        await _add_workstation_login(connection, manifest)
        (output / "seed-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "database_identity": identity,
                    "cases": [
                        {"outcome": item["outcome"], "task_id": item["task"]} for item in cases
                    ],
                    "manifest": str(output / "seed-manifest.json"),
                },
                ensure_ascii=False,
            )
        )
    finally:
        await pool.close()
        await connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url-env", default="ALIOTH_CHECKPOINT_RESULTS_CONTAINER_DATABASE_URL"
    )
    parser.add_argument("--expected-host", default="alioth-checkpoint-results-pg-20261019")
    parser.add_argument("--output", default="/validation")
    parser.add_argument("--add-workstation-user", action="store_true")
    asyncio.run(main(parser.parse_args()))
