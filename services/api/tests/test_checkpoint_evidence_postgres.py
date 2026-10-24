"""真实隔离库中的检查点准入、版本绑定、串属和重复动作拒绝。"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.capture_coordination import CaptureCoordinationResult
from services.api.app.application.explorer_turn import ExplorerTurnResult
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
    CaptureKind,
    CaptureProfile,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
)
from services.api.app.domain.journey import parse_journey_definition
from services.api.app.infrastructure.db.checkpoint_execution_repository import (
    PostgresCheckpointExecutionRepository,
)
from services.api.app.infrastructure.db.journey_definition_repository import (
    PostgresJourneyDefinitionRepository,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database,
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_h5_exploration_turn import _work
from services.api.tests.test_journey_checkpoint_contracts import checkpoint_definition


@asynccontextmanager
async def case(*, checkpoint_payload=None, configure=None, canonical_h5=False, retain_sealed=False):
    async with _database(
        surface_kind="web", target_platform="h5", canonical_h5=canonical_h5,
        retain_sealed=retain_sealed,
    ) as (connection, pool, ids):
        sample = _work(capture_id=ids["capture"])
        await connection.execute(
            """INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,url,
                capture_status,surface_session_id,capture_role,captured_at,metadata,
                session_command_sequence)
               VALUES($1::uuid,$2::uuid,$3::uuid,10,'source','https://m.example.test/home',
                'captured',$4,'bootstrap',now(),'{"finalization_epoch":0}'::jsonb,1)""",
            ids["capture"],
            ids["task"],
            ids["run"],
            ids["session"],
        )
        frontier = replace(
            sample.frontier,
            observation=replace(
                sample.frontier.observation,
                page_state="stable",
            ),
        )
        work = build_exploration_work(
            task_id=ids["task"],
            session_id=ids["session"],
            branch_id="checkpoint-fixture",
            objective=sample.objective,
            surface_kind=SurfaceKind.WEB,
            source_capture_id=ids["capture"],
            frontier_snapshot_id=frontier.snapshot_id,
            frontier=frontier,
            boundaries=sample.boundaries,
            budget=sample.budget,
            deadline=sample.deadline,
        )
        token = str(uuid4())
        await connection.execute(
            """INSERT INTO qc_v2_work_item(work_id,work_type,task_id,session_id,branch_id,
                source_capture_id,protocol_version,payload_hash,work_payload,status,deadline,
                lease_owner,lease_token,lease_expires_at,started_at)
               VALUES($1,'exploration',$2::uuid,$3,$4,$5::uuid,'exploration-work.v1',
                 'fixture',$6::jsonb,'running',$7,'checkpoint-test',$8::uuid,$7,now())""",
            work.work_id,
            work.task_id,
            work.session_id,
            work.branch_id,
            work.source_capture_id,
            json.dumps(work.to_dict()),
            work.deadline,
            token,
        )
        definitions = PostgresJourneyDefinitionRepository(pool=pool)
        service = JourneyDefinitionService(repository=definitions)
        payload = checkpoint_payload or checkpoint_definition(f"player-{uuid4()}")
        rule_hashes = await configure(connection, ids) if configure is not None else ()
        await service.publish(parse_journey_definition(payload))
        plan = await service.create_run_execution_plan(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            definition_id=payload["definition_id"],
            version=1,
            scenario_key="manual",
            parameters={},
            rule_contract_hashes=rule_hashes,
        )
        context = dict(
            attempt_id=f"attempt-{uuid4()}",
            execution_plan_id=plan.execution_plan_id,
            checkpoint_id="ready",
            task_id=work.task_id,
            agent_run_id=ids["run"],
            session_id=work.session_id,
            source_capture_id=work.source_capture_id,
            work_id=work.work_id,
        )
        decision = ActionIntent(
            task_id=work.task_id,
            session_id=work.session_id,
            intent_id=f"intent-{uuid4()}",
            idempotency_key=f"key-{uuid4()}",
            source_capture_id=work.source_capture_id,
            surface_kind=SurfaceKind.WEB,
            action_type=InteractionOperation.CLICK,
            semantic_target="受控只读按钮",
            purpose="检查点隔离用例",
            capture_profile=CaptureProfile(
                profile_id="fixture",
                before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
                after=CaptureDirective(role="after_action", artifact_types=("screenshot",)),
            ),
            deadline=work.deadline,
        )
        repo = PostgresCheckpointExecutionRepository(pool=pool)
        yield connection, repo, ids, work, context, decision


async def save_receipt(connection, ids, work, decision, *, command_sequence=2):
    now = datetime.now(UTC)
    page_seq_base = max(20, 1 + (await connection.fetchval(
        "SELECT coalesce(max(page_seq),0) FROM qc_page_capture WHERE task_id=$1::uuid",
        work.task_id,
    )))
    before, after = str(uuid4()), str(uuid4())
    action = f"action-{uuid4()}"
    await connection.execute(
        """INSERT INTO qc_action_execution(action_execution_id,task_id,session_id,intent_id,
            idempotency_key,intent_hash,intent_protocol_version,intent_payload,command_sequence,
            finalization_epoch,requested_source_capture_id)
           VALUES($1,$2::uuid,$3,$4,$5,'fixture','action-intent.v1',$6::jsonb,$8,0,$7::uuid)""",
        action,
        work.task_id,
        work.session_id,
        decision.intent_id,
        decision.idempotency_key,
        json.dumps(decision.to_dict()),
        work.source_capture_id,
        command_sequence,
    )
    references = []
    for index, capture_id in enumerate((before, after)):
        role = "before_action" if index == 0 else "after_action"
        parent = work.source_capture_id if index == 0 else before
        instant = now - timedelta(milliseconds=1) if index == 0 else now + timedelta(milliseconds=2)
        await connection.execute(
            """INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,url,
                 capture_role,capture_status,surface_session_id,action_execution_id,
                 session_command_sequence,parent_page_capture_id,captured_at,metadata)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4,$5,'https://m.example.test/home',
                 $6,'captured',$7,$8,$11,$9::uuid,$10,'{"finalization_epoch":0}'::jsonb)""",
            capture_id,
            work.task_id,
            ids["run"],
            page_seq_base + index,
            role,
            role,
            work.session_id,
            action,
            parent,
            instant,
            command_sequence,
        )
        artifact_id = str(uuid4())
        checksum = "sha256:" + ("a" if index == 0 else "b") * 64
        path = f"{work.task_id}/{capture_id}/{artifact_id}.png"
        await connection.execute(
            """INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,source,
                storage_path,checksum,size_bytes,content_type)
               VALUES($1::uuid,$2::uuid,$3::uuid,'screenshot','baseline',$4,$5,12,'image/png')""",
            artifact_id,
            work.task_id,
            capture_id,
            path,
            checksum,
        )
        manifest = build_evidence_manifest_reference(
            task_id=work.task_id,
            session_id=work.session_id,
            capture_id=capture_id,
            manifest_version=1,
            quality_status=EvidenceQualityStatus.PENDING,
            created_at=instant,
            artifacts=(
                ArtifactReference(
                    artifact_id=artifact_id,
                    artifact_type="screenshot",
                    role=role,
                    checksum=checksum,
                    storage_path=path,
                    size_bytes=12,
                    content_type="image/png",
                ),
            ),
        ).to_dict()
        manifest_id = manifest["manifest_id"]
        await connection.execute(
            """INSERT INTO qc_evidence_manifest(manifest_id,manifest_version,task_id,session_id,
                capture_id,protocol_version,manifest_hash,payload_hash,quality_status,
                manifest_payload,created_at) VALUES($1,1,$2::uuid,$3,$4::uuid,
                'evidence-manifest.v1',$7,'fixture','pending',$5::jsonb,$6)""",
            manifest_id,
            work.task_id,
            work.session_id,
            capture_id,
            json.dumps(manifest),
            instant,
            manifest["manifest_hash"],
        )
        references.append(
            CaptureRef(
                task_id=work.task_id,
                session_id=work.session_id,
                capture_id=capture_id,
                surface_kind=SurfaceKind.WEB,
                capture_kind=CaptureKind.PAGE,
                role=role,
                parent_capture_id=parent,
                action_execution_id=action,
                artifact_ids=(artifact_id,),
            )
        )
    await connection.execute(
        """UPDATE qc_action_execution SET source_capture_id=$2::uuid,result_capture_id=$3::uuid,
            status='completed',started_at=$4,action_finished_at=$4,evidence_finished_at=$4
            WHERE action_execution_id=$1""",
        action,
        before,
        after,
        now,
    )
    return ExplorerTurnResult(
        work_id=work.work_id,
        task_id=work.task_id,
        decision=decision,
        coordination=CaptureCoordinationResult(
            before_capture=references[0],
            after_capture=references[1],
            interaction=InteractionResult(
                task_id=work.task_id,
                session_id=work.session_id,
                intent_id=decision.intent_id,
                action_execution_id=action,
                source_capture_id=before,
                result_capture_id=after,
                status=InteractionResultStatus.SUCCEEDED,
            ),
        ),
    )


async def test_reservation_is_unique_and_cannot_replay_uncertain_attempt():
    async with case() as (_, repo, _, work, context, decision):
        assert await repo.reserve(work=work, context=context, decision=decision) == "new"
        assert await repo.reserve(work=work, context=context, decision=decision) == "uncertain"
        with pytest.raises(ValueError, match="合同"):
            await repo.reserve(
                work=work, context=context, decision=replace(decision, purpose="另一个动作")
            )


async def test_concurrent_reservation_has_one_owner_and_no_second_action_permission():
    async with case() as (_, repo, _, work, context, decision):
        results = await asyncio.gather(
            *(repo.reserve(work=work, context=context, decision=decision) for _ in range(4))
        )
        assert sorted(results) == ["new", "uncertain", "uncertain", "uncertain"]


async def test_required_journey_gap_blocks_actual_gate_reader_even_after_binding():
    from services.api.app.application.task_finalization import EvaluateCompletionGateCommand
    from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome
    from services.api.app.infrastructure.db.task_finalization_repository import (
        _read_completion_gate_facts,
    )

    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await repo.bind(work=work, context=context, turn=turn)
        command = EvaluateCompletionGateCommand(
            task_id=work.task_id,
            agent_run_id=ids["run"],
            expected_finalization_epoch=0,
            expected_row_version=0,
            execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.PASS,
        )
        _, facts = await _read_completion_gate_facts(connection, command)
        assert facts.journey_result_gap_count == 1
        assert not facts.gate_passed
        assert "journey_results_incomplete" in {reason.value for reason in facts.block_reasons}


async def test_binding_and_reuse_keep_pending_version_and_never_run_action_again():
    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await repo.bind(work=work, context=context, turn=turn)
        rows = await connection.fetch(
            "SELECT * FROM qc_journey_checkpoint_evidence WHERE attempt_id=$1",
            context["attempt_id"],
        )
        assert len(rows) == 2
        payloads = [json.loads(row["binding_payload"]) for row in rows]
        after_binding = next(item for item in payloads if item["role"] == "after_action")
        assert after_binding["evidence_roles"] == ["baseline"]
        assert after_binding["checkpoint_hash"]
        assert after_binding["manifest_hash"]
        assert after_binding["execution_plan_id"] == context["execution_plan_id"]
        assert after_binding["checkpoint_id"] == "ready"
        assert await repo.reserve(work=work, context=context, decision=decision) == "bound"
        assert await repo.read_turn(work=work, context=context) == turn
        assert (
            await connection.fetchval(
                "SELECT observation_status FROM qc_journey_checkpoint_attempt WHERE attempt_id=$1",
                context["attempt_id"],
            )
            == "needs_review"
        )


@pytest.mark.parametrize("fault", ["epoch", "artifact", "old_frame"])
async def test_failed_binding_is_atomic_and_does_not_discard_raw_capture(fault):
    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        after = turn.coordination.after_capture.capture_id
        if fault == "epoch":
            await connection.execute(
                "UPDATE qc_capture SET metadata='{"
                + '"finalization_epoch":1'
                + "}'::jsonb WHERE id=$1::uuid",
                after,
            )
        elif fault == "artifact":
            await connection.execute(
                "UPDATE qc_evidence_artifact SET checksum=$2 WHERE page_capture_id=$1::uuid",
                after,
                "sha256:" + "c" * 64,
            )
        else:
            await connection.execute(
                "UPDATE qc_capture SET capture_role='probe_before' WHERE id=$1::uuid", after
            )
        with pytest.raises(ValueError):
            await repo.bind(work=work, context=context, turn=turn)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_journey_checkpoint_evidence WHERE attempt_id=$1",
                context["attempt_id"],
            )
            == 0
        )
        assert (
            await connection.fetchval("SELECT count(*) FROM qc_capture WHERE id=$1::uuid", after)
            == 1
        )


async def test_stale_epoch_rejects_late_binding_and_finalized_identity_cannot_be_overwritten():
    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await repo.bind(work=work, context=context, turn=turn)
        with pytest.raises(asyncpg.RaiseError, match="冻结"):
            await connection.execute(
                "UPDATE qc_journey_checkpoint_attempt SET checkpoint_id='forged' "
                "WHERE attempt_id=$1",
                context["attempt_id"],
            )


async def test_returned_reference_cannot_lie_about_role_even_when_database_capture_is_valid():
    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        forged = replace(
            turn,
            coordination=replace(
                turn.coordination,
                after_capture=replace(turn.coordination.after_capture, role="probe_before"),
            ),
        )
        with pytest.raises(ValueError):
            await repo.bind(work=work, context=context, turn=forged)


async def test_new_epoch_cannot_reuse_old_bound_turn_as_current_observation():
    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await repo.bind(work=work, context=context, turn=turn)
        await connection.execute(
            "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1::uuid", work.task_id
        )
        with pytest.raises(ValueError):
            await repo.reserve(work=work, context=context, decision=decision)
        with pytest.raises(ValueError):
            await repo.read_turn(work=work, context=context)


async def test_bound_turn_cannot_be_read_under_foreign_checkpoint_context():
    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await repo.bind(work=work, context=context, turn=turn)
        with pytest.raises(ValueError):
            await repo.read_turn(work=work, context={**context, "checkpoint_id": "paused"})


async def test_checkpoint_cannot_retroactively_claim_action_that_precedes_reservation():
    async with case() as (connection, repo, ids, work, context, decision):
        turn = await save_receipt(connection, ids, work, decision)
        await repo.reserve(work=work, context=context, decision=decision)
        with pytest.raises(ValueError, match="早于"):
            await repo.bind(work=work, context=context, turn=turn)


async def test_correct_role_cannot_hide_before_frame_older_than_checkpoint_attempt():
    async with case() as (connection, repo, ids, work, context, decision):
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await connection.execute(
            "UPDATE qc_capture SET captured_at=now()-interval '1 hour' WHERE id=$1::uuid",
            turn.coordination.before_capture.capture_id,
        )
        with pytest.raises(ValueError, match="早于"):
            await repo.bind(work=work, context=context, turn=turn)


async def append_quality(connection, capture_id, quality, version):
    from services.api.app.domain.evidence import EvidenceManifestReference

    payload = json.loads(
        await connection.fetchval(
            "SELECT manifest_payload FROM qc_evidence_manifest WHERE capture_id=$1::uuid "
            "ORDER BY manifest_version DESC LIMIT 1",
            capture_id,
        )
    )
    original = EvidenceManifestReference.from_dict(payload)
    manifest = build_evidence_manifest_reference(
        task_id=original.task_id,
        session_id=original.session_id,
        capture_id=capture_id,
        manifest_version=version,
        quality_status=EvidenceQualityStatus(quality),
        artifacts=original.artifacts,
        quality_reason="controlled-verdict",
        created_at=datetime.now(UTC),
    )
    await connection.execute(
        """INSERT INTO qc_evidence_manifest(manifest_id,manifest_version,task_id,session_id,
            capture_id,protocol_version,manifest_hash,payload_hash,quality_status,quality_reason,
            manifest_payload,created_at) VALUES($1,$2,$3::uuid,$4,$5::uuid,
            'evidence-manifest.v1',$6,'fixture',$7,$8,$9::jsonb,now())""",
        manifest.manifest_id,
        version,
        manifest.task_id,
        manifest.session_id,
        capture_id,
        manifest.manifest_hash,
        quality,
        manifest.quality_reason,
        json.dumps(manifest.to_dict()),
    )


async def test_late_manifest_refresh_is_append_only_and_latest_failure_revokes_readiness():
    async with case() as (connection, repo, ids, work, context, decision):
        repo._observer = SimpleNamespace(
            entry_matches=AsyncMock(return_value=True),
            result_matches=AsyncMock(
                side_effect=lambda **kw: all(
                    item["quality_status"] == "pass" for item in kw["manifests"]
                )
            ),
        )
        await repo.reserve(work=work, context=context, decision=decision)
        turn = await save_receipt(connection, ids, work, decision)
        await repo.bind(work=work, context=context, turn=turn)
        original = await connection.fetch(
            "SELECT binding_payload FROM qc_journey_checkpoint_evidence "
            "WHERE attempt_id=$1 ORDER BY role",
            context["attempt_id"],
        )
        plans = await repo.list_plans(task_id=work.task_id, agent_run_id=ids["run"])
        assert await repo.next_checkpoint(work=work, plans=plans) is None
        for capture in (turn.coordination.before_capture, turn.coordination.after_capture):
            await append_quality(connection, capture.capture_id, "pass", 2)
        # 最新质量通过也不能使动作前的旧来源进入依赖检查点。
        assert await repo.next_checkpoint(work=work, plans=plans) is None
        frontier = replace(
            work.frontier,
            observation=replace(
                work.frontier.observation, capture_id=turn.coordination.after_capture.capture_id
            ),
        )
        work = replace(
            work,
            source_capture_id=turn.coordination.after_capture.capture_id,
            frontier=frontier,
            frontier_snapshot_id=frontier.snapshot_id,
        )
        next_step = await repo.next_checkpoint(work=work, plans=plans)
        assert next_step[1].checkpoint_id == "paused"
        assert (
            await connection.fetch(
                "SELECT binding_payload FROM qc_journey_checkpoint_evidence "
                "WHERE attempt_id=$1 ORDER BY role",
                context["attempt_id"],
            )
            == original
        )
        await append_quality(connection, turn.coordination.after_capture.capture_id, "fail", 3)
        assert await repo.next_checkpoint(work=work, plans=plans) is None
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_action_execution WHERE task_id=$1::uuid", work.task_id
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_journey_checkpoint_observation WHERE attempt_id=$1",
                context["attempt_id"],
            )
            == 3
        )
