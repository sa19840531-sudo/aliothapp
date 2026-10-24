"""隔离 PG 全链消费：受控恢复端口，真实请求、制品、输入、Judge 和报告正文。"""

import asyncio
import base64
import hashlib
import importlib
import io
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import pytest
from PIL import Image

from services.api.app.application.aggregate_inspection_execution import AggregateInspectionExecutor
from services.api.app.application.aggregate_inspection_publication import (
    AggregateInspectionPublicationService,
)
from services.api.app.application.evidence_requests import (
    EvidenceRequestStatus,
    ReleaseEvidenceRequestLeaseCommand,
    TransitionEvidenceRequestCommand,
)
from services.api.app.application.h5_checkpoint_observation import H5CheckpointObserver
from services.api.app.application.h5_report_coverage import h5_page_exploration_for_report
from services.api.app.application.h5_targeted_recapture import H5TargetedRecapturePlanService
from services.api.app.application.journey_report_binding import verified_journey_report
from services.api.app.application.restoration import (
    RestorationCoordinator,
    RestorationExecutionResult,
)
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.exploration import build_exploration_work
from services.api.app.domain.exploration.h5_coverage import H5CoverageSnapshot
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    build_evidence_request,
)
from services.api.app.domain.interaction import SurfaceKind, SurfaceSessionSpec
from services.api.app.domain.interaction.surface_driver import web_primary_surface_ref
from services.api.app.infrastructure.browser.h5_player_frames import video_region_digest
from services.api.app.infrastructure.browser.ui_snapshot_reader import (
    PostgresTrustedUiSnapshotReader,
)
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    PostgresAggregateInspectionExecutionRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)
from services.api.app.infrastructure.db.h5_closed_session_context import (
    PostgresH5ClosedSessionContextReader,
)
from services.api.app.infrastructure.db.journey_definition_repository import (
    PostgresJourneyDefinitionRepository,
)
from services.api.app.infrastructure.db.journey_input_repository import load_journey_selection
from services.api.app.infrastructure.db.journey_report_binding import bind_journey_report
from services.api.app.infrastructure.db.outbox_repository import PostgresOutboxRepository
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    PostgresRestorationAttemptRepository,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_checkpoint_evidence_postgres import (
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离迁移
    append_quality,
    case,
    save_receipt,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_h5_checkpoint_consumption import sample
from services.api.tests.test_h5_formal_report_binding_postgres import (
    _bind_command,
    _insert_domain_coverage,
    _report,
    _report_command,
)
from services.api.tests.test_journey_consumption_postgres import configure, definition
from services.api.tests.test_targeted_recapture_chain_postgres import _snapshot


def recovered_definition(*, same_session=False):
    payload = definition()
    second = {
        **payload["checkpoints"][0],
        "id": "recovered_pause",
        "depends_on": ["ready"],
        "recovery_policy": "same_session" if same_session else "new_instance",
        "exit_condition": {"stable_frame": True, "loaded_frame": True},
        "evidence_requirements": [
            {"role": "pause", "artifact_types": ["action_receipt"]},
            {"role": "loaded", "artifact_types": ["screenshot"], "minimum_count": 2},
        ],
    }
    payload["checkpoints"].append(second)
    return payload


@asynccontextmanager
async def recovery_case(*, fault):
    async with case(
        checkpoint_payload=recovered_definition(same_session=fault == "wrong_policy"),
        configure=configure,
        canonical_h5=True,
        retain_sealed=True,
    ) as fixture:
        connection, repo, ids, *_ = fixture
        # 仅去掉通用夹具预置的无关工作；不涉及真实业务库和历史记录。
        await connection.execute(
            "DELETE FROM qc_evidence_request WHERE task_id=$1::uuid", ids["task"]
        )
        await connection.execute(
            "DELETE FROM qc_inspection_work_item WHERE id=$1::uuid", ids["old_inspection_work_item"]
        )
        await connection.execute(
            "UPDATE qc_task SET task_config=$2::jsonb WHERE id=$1::uuid",
            ids["task"],
            json.dumps({"workflow_version": "v2", "execution_mode": "full_inspection"}),
        )
        yield fixture


async def attach_actual_material(connection, store, turn, *, instance, document, series=False):
    """保存实际 JSON/PNG 字节，Manifest 精确列出附件；不把测试状态当原站事实。"""
    references = []
    for index, reference in enumerate(
        (turn.coordination.before_capture, turn.coordination.after_capture)
    ):
        observed = sample(paused=bool(index))
        observed["snapshot"]["document"]["time_origin_ms"] = document
        observed["snapshot"]["elements"][0]["h5_media_instance_id"] = instance
        observed["video_frame"]["media_instance_id"] = instance
        observed["frame_origin"]["before"]["time_origin_ms"] = document
        observed["frame_origin"]["after"]["time_origin_ms"] = document
        buffer = io.BytesIO()
        Image.new("RGB", (360, 640), "green").save(buffer, format="PNG")
        png = buffer.getvalue()
        observed["screenshot_sha256"] = hashlib.sha256(png).hexdigest()
        observed["frame_origin"]["screenshot_sha256"] = observed["screenshot_sha256"]
        observed["video_region_sha256"] = video_region_digest(png, observed["snapshot"])
        primary = await store.store(
            task_id=turn.task_id,
            page_capture_id=reference.capture_id,
            artifact_id=reference.artifact_ids[0],
            artifact_type="screenshot",
            content={
                "encoding": "base64",
                "data": base64.b64encode(png).decode(),
                "purpose": "h5_video_primary",
            },
        )
        await connection.execute(
            "UPDATE qc_evidence_artifact SET checksum=$2,size_bytes=$3,metadata=$4::jsonb "
            "WHERE id=$1::uuid",
            primary.artifact_id,
            primary.checksum,
            primary.size_bytes,
            json.dumps(primary.metadata),
        )
        video_contract = None
        values = [("ui_snapshot", observed["snapshot"])]
        if series and index:
            last = json.loads(json.dumps(observed))
            last["observed_at_ms"] = 1600
            video_contract = {
                "schema_version": "h5-video-publication.v1",
                "samples": [observed, last],
                "primary_screenshot_sha256": last["screenshot_sha256"],
                "page_url": last["snapshot"]["url"],
            }
            values += [
                ("screenshot", {"encoding": "base64", "data": base64.b64encode(png).decode()}),
                ("screenshot", {"encoding": "base64", "data": base64.b64encode(png).decode()}),
                (
                    "checkpoint_observation",
                    {"schema_version": "checkpoint-observation.v1", "samples": [observed, last]},
                ),
            ]
        for kind, content in values:
            saved = await store.store(
                task_id=turn.task_id,
                page_capture_id=reference.capture_id,
                artifact_id=str(uuid4()),
                artifact_type=kind,
                content=content,
            )
            await connection.execute(
                "INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,source,"
                "storage_path,checksum,size_bytes,content_type) VALUES($1::uuid,$2::uuid,$3::uuid,"
                "$4,'baseline',$5,$6,$7,$8)",
                saved.artifact_id,
                turn.task_id,
                reference.capture_id,
                kind,
                saved.storage_path,
                saved.checksum,
                saved.size_bytes,
                saved.content_type,
            )
        rows = await connection.fetch(
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
                for row in rows
            ),
        )
        await connection.execute(
            "INSERT INTO qc_evidence_manifest(manifest_id,manifest_version,task_id,session_id,"
            "capture_id,protocol_version,manifest_hash,payload_hash,quality_status,"
            "manifest_payload,created_at) VALUES($1,2,$2::uuid,$3,$4::uuid,"
            "'evidence-manifest.v1',$5,'controlled','pass',$6::jsonb,now())",
            manifest.manifest_id,
            turn.task_id,
            reference.session_id,
            reference.capture_id,
            manifest.manifest_hash,
            json.dumps(manifest.to_dict()),
        )
        await connection.execute(
            "UPDATE qc_page_capture SET metadata=$2::jsonb,url=$3 WHERE id=$1::uuid",
            reference.capture_id,
            json.dumps(
                {
                    "finalization_epoch": 0,
                    "page_state": "stable",
                    "pipeline_metrics": {
                        "page_inspection_ready": True,
                        **({"h5_video_publication": video_contract} if video_contract else {}),
                    },
                    "presentation": {
                        "protocol_version": "presentation-stack.v1",
                        "topmost_layer_kind": "page",
                        "base_page_visibility": "visible",
                        "blocking_scope": "none",
                        "mutation_mode": "natural",
                    },
                }
            ),
            observed["snapshot"]["url"],
        )
        references.append(replace(reference, artifact_ids=tuple(str(row["id"]) for row in rows)))
    return replace(
        turn,
        coordination=replace(
            turn.coordination, before_capture=references[0], after_capture=references[1]
        ),
    )


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "no_restoration_action",
        "wrong_policy",
        "late_manifest",
        "old_epoch",
        "pending_review",
        "forged_report_recovery",
    ],
)
async def test_cross_session_restoration_is_consumed_by_input_judge_and_report(tmp_path, fault):
    async with recovery_case(fault=fault) as (
        connection,
        repo,
        ids,
        work,
        context,
        decision,
    ):
        pool = repo._pool
        await connection.execute(
            "INSERT INTO qc_h5_llm_call_budget(task_id,finalization_epoch,agent_run_id,max_calls,"
            "deadline) VALUES($1::uuid,0,$2::uuid,200,$3)",
            ids["task"],
            ids["run"],
            work.deadline,
        )
        entry_url = sample()["snapshot"]["url"]
        await connection.execute(
            "UPDATE qc_task SET target_url=$2 WHERE id=$1::uuid",
            ids["task"],
            entry_url,
        )
        await connection.execute(
            "UPDATE qc_page_capture SET url=$2 WHERE id=$1::uuid",
            ids["capture"],
            entry_url,
        )
        await connection.execute(
            "INSERT INTO qc_page_graph_state(state_id,task_id,agent_run_id,surface_kind,"
            "surface_session_id,finalization_epoch,capture_role,logical_page_key,identity_status,"
            "identity_source,state_fingerprint,counts_as_business_page) "
            "VALUES($1::uuid,$2::uuid,$3::uuid,'web',$4,0,'bootstrap','video:42',"
            "'confirmed','logical_surface',$5,true)",
            ids["capture"],
            ids["task"],
            ids["run"],
            work.session_id,
            "a" * 64,
        )
        store = LocalEvidenceArtifactStore(root=tmp_path)
        reader = PostgresTrustedUiSnapshotReader(pool=pool, artifact_store=store)
        repo._observer = H5CheckpointObserver(
            snapshots=reader, plans=PostgresJourneyDefinitionRepository(pool=pool)
        )
        assert await repo.reserve(work=work, context=context, decision=decision) == "new"
        first = await attach_actual_material(
            connection,
            store,
            await save_receipt(connection, ids, work, decision),
            instance="first-video",
            document=10,
            series=True,
        )
        assert await repo.bind(work=work, context=context, turn=first)
        origin = first.coordination.after_capture.capture_id
        await asyncio.sleep(0.01)
        # 本夹具补证工作对应第一检查点的真实 after Capture，不借用其它任务或规则。
        await connection.execute(
            "UPDATE qc_inspection_work_item SET page_capture_id=$2::uuid,status='waiting_evidence',"
            "control_mode='enforced' WHERE id=$1::uuid",
            ids["inspection_work_item"],
            origin,
        )
        attempt_id = await connection.fetchval(
            "SELECT id::text FROM qc_inspection_attempt WHERE work_item_id=$1::uuid",
            ids["inspection_work_item"],
        )
        await connection.execute(
            "INSERT INTO qc_inspection_work_item_rule(work_item_id,rule_id,rule_code,"
            "ordinal,contract_hash,status) VALUES($1::uuid,$2::uuid,'FACT-GAP-1',0,"
            "'controlled-contract','pending')",
            ids["inspection_work_item"],
            ids["rule"],
        )
        request = build_evidence_request(
            task_id=ids["task"],
            work_item_id=ids["inspection_work_item"],
            attempt_id=attempt_id,
            rule_id=ids["rule"],
            rule_code="FACT-GAP-1",
            target_capture_id=origin,
            reason_code="TRUSTED_EVIDENCE_INVALID",
            evidence_goal="隔离恢复后的原生暂停观测",
            required_artifact_types=("screenshot",),
            session_dependency=EvidenceSessionDependency.SNAPSHOT,
            replay_policy=EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
            required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
            minimum_source_grade=EvidenceSourceGrade.C,
            side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
            target_invariants={"business": {"route": "/detail/42"}},
            expires_at=work.deadline,
        )
        requests = PostgresEvidenceRequestRepository(pool=pool, h5_snapshot_recovery=True)
        await requests.upsert(request=request)
        await connection.execute(
            "INSERT INTO qc_page_graph_state(state_id,task_id,agent_run_id,surface_kind,"
            "surface_session_id,finalization_epoch,capture_role,logical_page_key,identity_status,"
            "identity_source,state_fingerprint,counts_as_business_page) "
            "VALUES($1::uuid,$2::uuid,$3::uuid,'web',$4,0,'after_action','video:42',"
            "'confirmed','logical_surface',$5,true)",
            origin,
            ids["task"],
            ids["run"],
            work.session_id,
            "a" * 64,
        )
        await connection.execute(
            "UPDATE qc_surface_session SET status='closed',draining_at=now(),closed_at=now() "
            "WHERE session_id=$1",
            work.session_id,
        )
        await connection.execute(
            "UPDATE qc_task SET status='review',finalization_state='session_draining',"
            "exploration_draining_at=now(),session_draining_at=now(),"
            "finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
            ids["task"],
            ids["run"],
        )
        claim = await requests.claim_next(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finalization_epoch=0,
            lease_owner="cross-session-test",
            lease_seconds=240,
            session_dependencies=(EvidenceSessionDependency.SNAPSHOT,),
        )
        # 种子中的旧 ActiveSession 请求不参与 Snapshot 领取。
        assert claim.lease is not None and claim.lease.request.request_id == request.request_id
        planner = H5TargetedRecapturePlanService(
            contexts=PostgresH5ClosedSessionContextReader(pool=pool),
            new_session_runtime=SimpleNamespace(remember_new_session_plan=AsyncMock()),
        )
        resolution = await planner.resolve(
            request=request, agent_run_id=ids["run"], finalization_epoch=0
        )
        assert resolution.plan is not None
        plan = resolution.plan
        original = replace(
            _snapshot(
                task_id=ids["task"],
                capture_id=origin,
                captured_at=await connection.fetchval(
                    "SELECT captured_at FROM qc_capture WHERE id=$1::uuid", origin
                ),
            ),
            session_id=work.session_id,
        )
        trace = {}

        class Executor:
            async def restore(self, command):
                await PostgresSurfaceSessionRepository(pool=pool).register_active(
                    spec=SurfaceSessionSpec(
                        task_id=ids["task"],
                        session_id=plan.target_session_id,
                        surface_kind=SurfaceKind.WEB,
                    ),
                    session_owner=f"h5-snapshot-recapture:{ids['run']}",
                    primary_surface_ref=web_primary_surface_ref(plan.target_session_id),
                )
                target_work = SimpleNamespace(
                    task_id=work.task_id,
                    session_id=plan.target_session_id,
                    source_capture_id=origin,
                    work_id="controlled-restore",
                )
                restore_decision = replace(
                    decision,
                    session_id=plan.target_session_id,
                    source_capture_id=origin,
                    intent_id="restore-" + str(uuid4()),
                    idempotency_key=str(uuid4()),
                    deadline=plan.deadline,
                    parameters={"restoration_plan_id": plan.plan_id},
                )
                restored = await save_receipt(connection, ids, target_work, restore_decision)
                await append_quality(
                    connection, restored.coordination.after_capture.capture_id, "pass", 2
                )
                capture = restored.coordination.after_capture.capture_id
                trace["restored"] = capture
                await asyncio.sleep(0.01)
                return RestorationExecutionResult(
                    restored_snapshot=replace(
                        original,
                        capture_id=capture,
                        session_id=plan.target_session_id,
                        captured_at=await connection.fetchval(
                            "SELECT captured_at FROM qc_capture WHERE id=$1::uuid", capture
                        ),
                    ),
                    action_execution_ids=()
                    if fault == "no_restoration_action"
                    else (restored.coordination.interaction.action_execution_id,),
                )

        restoration = await RestorationCoordinator(
            repository=PostgresRestorationAttemptRepository(pool=pool),
            executor=Executor(),
            equivalence_gate=StateEquivalenceGate(),
        ).restore(
            request=request,
            plan=plan,
            attempt_no=claim.lease.attempt_count,
            original_snapshot=original,
        )
        assert restoration.assessment is not None, restoration.error_message
        assert restoration.assessment.passed
        frontier = replace(
            work.frontier,
            observation=replace(work.frontier.observation, capture_id=trace["restored"]),
        )
        follow = build_exploration_work(
            task_id=work.task_id,
            session_id=plan.target_session_id,
            source_capture_id=trace["restored"],
            branch_id="recovered-checkpoint",
            objective=work.objective,
            surface_kind=work.surface_kind,
            frontier_snapshot_id=frontier.snapshot_id,
            frontier=frontier,
            boundaries=work.boundaries,
            budget=work.budget,
            deadline=work.deadline,
        )
        await connection.execute(
            "INSERT INTO qc_v2_work_item(work_id,work_type,task_id,session_id,branch_id,"
            "source_capture_id,protocol_version,payload_hash,work_payload,status,deadline,"
            "lease_owner,lease_token,lease_expires_at,started_at) VALUES($1,'exploration',"
            "$2::uuid,$3,$4,$5::uuid,'exploration-work.v1','controlled',$6::jsonb,'running',"
            "$7,'checkpoint-test',gen_random_uuid(),$7,now())",
            follow.work_id,
            follow.task_id,
            follow.session_id,
            follow.branch_id,
            follow.source_capture_id,
            json.dumps(follow.to_dict()),
            follow.deadline,
        )
        second_context = {
            **context,
            "attempt_id": "recovered-" + str(uuid4()),
            "checkpoint_id": "recovered_pause",
            "session_id": follow.session_id,
            "source_capture_id": follow.source_capture_id,
            "work_id": follow.work_id,
        }
        second_decision = replace(
            decision,
            session_id=follow.session_id,
            source_capture_id=follow.source_capture_id,
            intent_id="pause-" + str(uuid4()),
            idempotency_key=str(uuid4()),
        )
        if fault in {"no_restoration_action", "wrong_policy"}:
            with pytest.raises(ValueError, match="恢复链"):
                await repo.reserve(work=follow, context=second_context, decision=second_decision)
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_journey_checkpoint_attempt WHERE task_id=$1::uuid",
                    ids["task"],
                )
                == 1
            )
            return
        assert (
            await repo.reserve(work=follow, context=second_context, decision=second_decision)
            == "new"
        )
        second = await attach_actual_material(
            connection,
            store,
            await save_receipt(connection, ids, follow, second_decision, command_sequence=3),
            instance="recovered-video",
            document=20,
            series=True,
        )
        assert await repo.bind(work=follow, context=second_context, turn=second)
        if fault is None:
            migration = importlib.import_module(
                "services.api.migrations.versions.20261016_0138_checkpoint_artifact_type"
            )
            statements = []
            with patch.object(migration, "op", SimpleNamespace(execute=statements.append)):
                migration.downgrade()
            with pytest.raises(asyncpg.RaiseError, match="禁止回滚抹去"):
                async with connection.transaction():
                    for statement in statements:
                        await connection.execute(statement)
            with pytest.raises(asyncpg.CheckViolationError):
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,"
                        "source,storage_path,checksum,size_bytes,content_type) "
                        "SELECT gen_random_uuid(),task_id,page_capture_id,'invented_observation',"
                        "source,'unknown.json',checksum,size_bytes,content_type "
                        "FROM qc_evidence_artifact WHERE page_capture_id=$1::uuid LIMIT 1",
                        second.coordination.after_capture.capture_id,
                    )
        rule_hash = (
            await PostgresJourneyDefinitionRepository(pool=pool).get_execution_plan(
                execution_plan_id=context["execution_plan_id"]
            )
        ).rule_contract_hashes[0]
        if fault == "old_epoch":
            await connection.execute(
                "UPDATE qc_capture SET metadata=jsonb_set(metadata,'{finalization_epoch}','1') "
                "WHERE id=$1::uuid",
                trace["restored"],
            )
            assert (
                await load_journey_selection(
                    connection,
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    epoch=0,
                    rule_hash=rule_hash,
                )
                is None
            )
            return
        publisher = AggregateInspectionPublicationService(
            repository=PostgresAggregateInspectionPublicationRepository(pool=pool)
        )
        selection = await load_journey_selection(
            connection,
            task_id=ids["task"],
            agent_run_id=ids["run"],
            epoch=0,
            rule_hash=rule_hash,
        )
        assert selection is not None
        assert (
            await load_journey_selection(
                connection,
                task_id=ids["task"],
                agent_run_id=ids["run"],
                epoch=0,
                rule_hash=rule_hash,
                allow_closed_epoch=True,
            )
            is None
        )
        assert (
            await publisher.prepare(
                task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0
            )
        ).published_work_item_count == 1
        contract = json.loads(
            await connection.fetchval(
                "SELECT contract_payload FROM qc_journey_input_contract WHERE task_id=$1::uuid",
                ids["task"],
            )
        )
        proof = contract["checkpoints"][-1]["recovery"]["ready"]["restorations"][0]
        assert proof["attempt_id"] == restoration.attempt_id
        assert proof["action_execution_ids"] == list(restoration.action_execution_ids)
        queued = await connection.fetchrow(
            "SELECT id::text,row_version FROM qc_aggregate_inspection_work_item "
            "WHERE task_id=$1::uuid",
            ids["task"],
        )
        consumer = AggregateInspectionExecutor(
            repository=PostgresAggregateInspectionExecutionRepository(
                pool=pool, snapshot_reader=reader
            ),
            lease_owner="cross-session-test",
            lease_seconds=30,
            retry_delay_seconds=1,
        )
        assert await consumer.execute_wakeup(
            work_item_id=queued["id"], task_id=ids["task"], message_version=queued["row_version"]
        )
        execution = await connection.fetchrow(
            "SELECT execution_status,judge_output FROM qc_rule_execution WHERE task_id=$1::uuid "
            "AND evidence_input_set_id IS NOT NULL",
            ids["task"],
        )
        assert execution["execution_status"] == "pass", execution["judge_output"]
        if fault == "late_manifest":
            await append_quality(connection, trace["restored"], "fail", 3)
        elif fault == "pending_review":
            await connection.execute(
                "UPDATE qc_rule_execution SET review_status='pending' WHERE task_id=$1::uuid "
                "AND evidence_input_set_id IS NOT NULL",
                ids["task"],
            )
        elif fault == "forged_report_recovery":
            await connection.execute(
                "UPDATE qc_rule_execution SET judge_output=jsonb_set(judge_output,"
                "'{wrapper_payload,journey,checkpoints,1,recovery}','{}') "
                "WHERE task_id=$1::uuid AND evidence_input_set_id IS NOT NULL",
                ids["task"],
            )
        report = {}
        if fault:
            with pytest.raises(ValueError, match="未闭合"):
                await bind_journey_report(
                    connection, report, task_id=ids["task"], agent_run_id=ids["run"], epoch=0
                )
            assert "h5_journeys" not in report
        else:
            sealed_preview = {}
            await bind_journey_report(
                connection,
                sealed_preview,
                task_id=ids["task"],
                agent_run_id=ids["run"],
                epoch=1,
                source_epoch=0,
            )
            assert sealed_preview["h5_journeys"]["source_finalization_epoch"] == 0
            assert sealed_preview["h5_journeys"]["finalization_epoch"] == 1
            await bind_journey_report(
                connection, report, task_id=ids["task"], agent_run_id=ids["run"], epoch=0
            )
            assert (
                report["h5_journeys"]["results"][0]["checkpoints"][-1]["recovery"]["ready"][
                    "restorations"
                ][0]
                == proof
            )
            # 本请求的 screenshot 义务已有原来源 pass 清单，走乐观锁生命周期结算，
            # 不为结算重放动作；恢复链和新实例观测仍由上面的实际账本独立复核。
            released = await requests.release_lease(
                command=ReleaseEvidenceRequestLeaseCommand(
                    request_id=request.request_id,
                    task_id=ids["task"],
                    finalization_epoch=0,
                    lease_owner=claim.lease.lease_owner,
                    lease_token=claim.lease.lease_token,
                    row_version=claim.lease.row_version,
                    next_attempt_at=datetime.now(UTC) + timedelta(seconds=1),
                    reason_code="CONTROLLED_SOURCE_MANIFEST_ALREADY_AVAILABLE",
                )
            )
            version = released.row_version
            status = EvidenceRequestStatus.PENDING
            for target in (
                EvidenceRequestStatus.ROUTED,
                EvidenceRequestStatus.RUNNING,
                EvidenceRequestStatus.SATISFIED,
            ):
                version = await requests.transition(
                    command=TransitionEvidenceRequestCommand(
                        request_id=request.request_id,
                        expected_status=status,
                        target_status=target,
                        row_version=version,
                        resolution_manifest_id=(
                            await connection.fetchval(
                                "SELECT manifest_id FROM qc_evidence_manifest "
                                "WHERE capture_id=$1::uuid "
                                "AND quality_status='pass' ORDER BY manifest_version DESC LIMIT 1",
                                origin,
                            )
                            if target is EvidenceRequestStatus.SATISFIED
                            else None
                        ),
                    )
                )
                status = target
            execution_id = await connection.fetchval(
                "SELECT id FROM qc_rule_execution WHERE task_id=$1::uuid "
                "AND evidence_input_set_id IS NOT NULL",
                ids["task"],
            )
            await connection.execute(
                "UPDATE qc_inspection_work_item_rule SET status='written',rule_execution_id=$2 "
                "WHERE work_item_id=$1::uuid",
                ids["inspection_work_item"],
                execution_id,
            )
            await connection.execute(
                "UPDATE qc_inspection_work_item SET status='succeeded',finished_at=now() "
                "WHERE id=$1::uuid",
                ids["inspection_work_item"],
            )
            await connection.execute(
                "UPDATE qc_inspection_attempt SET status='written',finished_at=now() "
                "WHERE work_item_id=$1::uuid",
                ids["inspection_work_item"],
            )
            await connection.execute(
                "UPDATE qc_agent_run SET status='done',finished_at=now() WHERE task_id=$1::uuid",
                ids["task"],
            )
            # 受控运输端口已将唯一唤醒交给上面的真实消费器；只确认本用例事件。
            token = str(uuid4())
            message = await connection.fetchval(
                "UPDATE qc_outbox_event SET status='publishing',lease_owner='controlled-transport',"
                "lease_token=$2::uuid,lease_expires_at=now()+interval '60 seconds',"
                "attempt_count=attempt_count+1 WHERE task_id=$1::uuid AND status='pending' "
                "RETURNING message_id",
                ids["task"],
                token,
            )
            assert message is not None
            assert await PostgresOutboxRepository(pool=pool).mark_published(
                message_id=message,
                lease_token=token,
                stream_name="controlled-isolated-transport",
                stream_entry_id="1-0",
            )
            await connection.execute(
                "UPDATE qc_v2_work_item SET status='succeeded',finished_at=now(),lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL WHERE task_id=$1::uuid",
                ids["task"],
            )
            await connection.execute(
                "UPDATE qc_surface_ref SET status='released',released_at=now() "
                "WHERE task_id=$1::uuid",
                ids["task"],
            )
            await connection.execute(
                "UPDATE qc_surface_session SET status='closed',"
                "draining_at=coalesce(draining_at,now()),"
                "closed_at=now() WHERE task_id=$1::uuid",
                ids["task"],
            )
            coverage = H5CoverageSnapshot.advance(
                previous=None,
                frontier=H5TaskFrontierSnapshot.create(
                    max_pages=3, sampling_policy_summary={"include_page_types": ["video_detail"]}
                ),
                location=entry_url,
                capture_id=second.coordination.after_capture.capture_id,
                page_type="video_detail",
                page_state="stable",
                requires_auth=False,
                overlay_present=False,
                assessment={
                    "execution_status": "accepted",
                    "page_type_assessment": {"page_type": "video_detail", "status": "covered"},
                },
            ).to_dict()
            # 更高版本仍由领域重新计算摘要，不能直接改快照或补算其他页型。
            from services.api.tests.test_h5_formal_report_binding_postgres import _coverage_hash

            coverage = {
                **coverage,
                "revision": 2,
                "coverage_snapshot_id": "controlled-" + str(uuid4()),
                "task_id": ids["task"],
                "agent_run_id": ids["run"],
                "finalization_epoch": 0,
            }
            coverage["payload_hash"] = _coverage_hash(coverage)
            await _insert_domain_coverage(connection, coverage)
            actual_case = SimpleNamespace(task_id=ids["task"], run_id=ids["run"])
            full_report = _report(
                h5_page_exploration_for_report(
                    {
                        "page_results": [
                            {"page_capture_id": second.coordination.after_capture.capture_id}
                        ]
                    },
                    coverage=coverage,
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                )
            )
            task_row = await connection.fetchrow(
                "SELECT finalization_epoch,finalization_row_version AS row_version "
                "FROM qc_task WHERE id=$1::uuid",
                ids["task"],
            )
            finalizer = PostgresTaskFinalizationRepository(pool=pool)
            finalized = await finalizer.finalize_task_with_report(
                command=_report_command(actual_case, task_row, full_report),
            )
            assert finalized.snapshot.gate_passed, finalized.snapshot.block_reasons
            stored = json.loads(
                await connection.fetchval(
                    "SELECT report_payload FROM qc_report_payload_snapshot WHERE report_id=$1",
                    f"task:{ids['task']}:run:{ids['run']}",
                )
            )
            assert (
                verified_journey_report(
                    stored["h5_journeys"],
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    epoch=1,
                    source_epoch=0,
                )["gap_count"]
                == 0
            )
            await finalizer.bind_report(
                command=_bind_command(
                    actual_case,
                    stored,
                    gate_snapshot_id=finalized.snapshot.gate_snapshot_id,
                    finalization_epoch=1,
                )
            )
            # 展示只读取门禁明确绑定的旧证据周期，不把封存后的 epoch+1 当缺证。
            from services.api.app.infrastructure.db.h5_shortflow_read_repository import (
                PostgresH5ShortflowReadRepository,
            )
            from services.api.tests.test_h5_call_audit_postgres import scope

            displayed = await PostgresH5ShortflowReadRepository(pool=pool).read(
                task_id=ids["task"], access_scope=scope(ids),
            )
            assert displayed["finalization_epoch"] == 1
            assert displayed["plans"][0]["rule_results_status"] == "available"
            result = displayed["plans"][0]["rule_results"][0]
            assert result["finalization_epoch"] == 0 and result["execution_status"] == "pass"
            assert len(result["checkpoints"]) == 2
            assert (
                await load_journey_selection(
                    connection,
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    epoch=0,
                    rule_hash=rule_hash,
                )
                is None
            )
            assert (
                await load_journey_selection(
                    connection,
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    epoch=0,
                    rule_hash=rule_hash,
                    allow_closed_epoch=True,
                )
                is not None
            )
            with pytest.raises(ValueError, match="禁止新动作"):
                await repo.reserve(work=follow, context=second_context, decision=second_decision)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_journey_checkpoint_attempt WHERE task_id=$1::uuid",
                ids["task"],
            )
            == 2
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_action_execution WHERE task_id=$1::uuid", ids["task"]
            )
            == 3
        )
        budget = await connection.fetchrow(
            "SELECT max_calls,admitted_calls,deadline FROM qc_h5_llm_call_budget "
            "WHERE task_id=$1::uuid AND finalization_epoch=0",
            ids["task"],
        )
        assert tuple(budget) == (200, 0, work.deadline)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_llm_usage_event WHERE task_id=$1::uuid",
                ids["task"],
            )
            == 0
        )
