"""隔离 PG 实际恢复/动作/Manifest 行的来源绑定，不执行原站恢复。"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

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
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    SurfaceKind,
    SurfaceSessionSpec,
)
from services.api.app.domain.interaction.surface_driver import web_primary_surface_ref
from services.api.app.infrastructure.db.checkpoint_recovery_binding import load_checkpoint_lineage
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    PostgresRestorationAttemptRepository,
)
from services.api.tests.test_checkpoint_evidence_postgres import append_quality, save_receipt
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_h5_closed_session_recapture_postgres import _closed_request
from services.api.tests.test_targeted_recapture_chain_postgres import _snapshot


@pytest.mark.parametrize("fault", [None, "old_epoch", "no_action", "late_fail", "wrong_policy"])
async def test_restored_source_binds_only_equivalent_real_action_and_latest_manifest(fault):
    async with _closed_request(isolation="read_committed") as (
        connection,
        pool,
        ids,
        request,
        plan,
        sessions,
        _,
        lease,
    ):
        await connection.execute(
            "UPDATE qc_capture SET metadata='{"
            + '"finalization_epoch":0}'
            + "'::jsonb WHERE id=$1",
            ids["original"],
        )
        artifact = ArtifactReference(
            artifact_id=str(uuid4()),
            artifact_type="screenshot",
            role="after_action",
            storage_path="controlled.png",
            checksum="sha256:" + "a" * 64,
            size_bytes=12,
            content_type="image/png",
        )
        manifest = build_evidence_manifest_reference(
            task_id=ids["task"],
            session_id=plan.source_session_id,
            capture_id=ids["original"],
            manifest_version=1,
            quality_status=EvidenceQualityStatus.PASS,
            created_at=datetime.now(UTC),
            artifacts=(artifact,),
        )
        await connection.execute(
            "INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,source,"
            "storage_path,checksum,size_bytes,content_type) VALUES($1::uuid,$2::uuid,$3::uuid,"
            "'screenshot','baseline',$4,$5,12,'image/png')",
            artifact.artifact_id,
            ids["task"],
            ids["original"],
            artifact.storage_path,
            artifact.checksum,
        )
        await connection.execute(
            "INSERT INTO qc_evidence_manifest(manifest_id,manifest_version,task_id,session_id,"
            "capture_id,protocol_version,manifest_hash,payload_hash,quality_status,"
            "manifest_payload,created_at) "
            "VALUES($1,1,$2::uuid,$3,$4::uuid,'evidence-manifest.v1',$5,'fixture','pass',$6::jsonb,now())",
            manifest.manifest_id,
            ids["task"],
            plan.source_session_id,
            ids["original"],
            manifest.manifest_hash,
            json.dumps(manifest.to_dict()),
        )
        original = replace(
            _snapshot(
                task_id=ids["task"],
                capture_id=ids["original"],
                captured_at=await connection.fetchval(
                    "SELECT captured_at FROM qc_capture WHERE id=$1", ids["original"]
                ),
            ),
            session_id=plan.source_session_id,
        )
        trace = {}

        class Executor:
            async def restore(self, command):
                await sessions.register_active(
                    spec=SurfaceSessionSpec(
                        task_id=ids["task"],
                        session_id=plan.target_session_id,
                        surface_kind=SurfaceKind.WEB,
                    ),
                    session_owner=f"h5-snapshot-recapture:{ids['run']}",
                    primary_surface_ref=web_primary_surface_ref(plan.target_session_id),
                )
                work = SimpleNamespace(
                    task_id=ids["task"],
                    session_id=plan.target_session_id,
                    source_capture_id=ids["original"],
                    work_id="isolated-recovery",
                )
                identity = "restore-" + str(uuid4())
                decision = ActionIntent(
                    task_id=ids["task"],
                    session_id=plan.target_session_id,
                    source_capture_id=ids["original"],
                    intent_id=identity,
                    idempotency_key=identity,
                    surface_kind=SurfaceKind.WEB,
                    action_type=InteractionOperation.NAVIGATE,
                    semantic_target="隔离恢复入口",
                    purpose="恢复账本验证",
                    deadline=plan.deadline,
                    parameters={"restoration_plan_id": plan.plan_id},
                    capture_profile=CaptureProfile(
                        profile_id="fixture",
                        before=CaptureDirective(
                            role="before_action", artifact_types=("screenshot",)
                        ),
                        after=CaptureDirective(role="after_action", artifact_types=("screenshot",)),
                    ),
                )
                turn = await save_receipt(connection, ids, work, decision)
                capture = turn.coordination.after_capture.capture_id
                await append_quality(connection, capture, "pass", 2)
                trace["capture"] = capture
                instant = await connection.fetchval(
                    "SELECT captured_at FROM qc_capture WHERE id=$1::uuid", capture
                )
                # 夹具的动作后时刻为 now+2ms，等到其实际时间后再完成恢复。
                import asyncio

                await asyncio.sleep(0.01)
                return RestorationExecutionResult(
                    restored_snapshot=replace(
                        original,
                        capture_id=capture,
                        session_id=plan.target_session_id,
                        captured_at=instant,
                    ),
                    action_execution_ids=()
                    if fault == "no_action"
                    else (turn.coordination.interaction.action_execution_id,),
                )

        attempt = await RestorationCoordinator(
            repository=PostgresRestorationAttemptRepository(pool=pool),
            executor=Executor(),
            equivalence_gate=StateEquivalenceGate(),
        ).restore(
            request=request, plan=plan, attempt_no=lease.attempt_count, original_snapshot=original
        )
        assert attempt.assessment.passed
        if fault == "old_epoch":
            await connection.execute(
                "UPDATE qc_capture SET metadata='{"
                + '"finalization_epoch":1}'
                + "'::jsonb WHERE id=$1::uuid",
                trace["capture"],
            )
        elif fault == "late_fail":
            await append_quality(connection, trace["capture"], "fail", 3)
        proof = await load_checkpoint_lineage(
            connection,
            task_id=ids["task"],
            agent_run_id=ids["run"],
            epoch=0,
            source_capture_id=trace["capture"],
            origin_capture_id=ids["original"],
            recovery_policy="same_session" if fault == "wrong_policy" else "new_instance",
            before=datetime.now(UTC) + timedelta(seconds=1),
            deadline=plan.deadline,
        )
        assert (proof is not None) is (fault is None)
        if proof:
            assert proof["restorations"][0]["attempt_id"] == attempt.attempt_id
            assert proof["restorations"][0]["action_execution_ids"] == list(
                attempt.action_execution_ids
            )
