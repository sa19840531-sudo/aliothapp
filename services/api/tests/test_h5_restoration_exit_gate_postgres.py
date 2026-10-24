"""原库事务回滚验证 H5 恢复动作许可，拒绝跨任务与过期现场。"""

import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.api.app.application.h5_exploration_workflow import H5SurfaceBootstrapper
from services.api.app.domain.inspection import build_restoration_plan
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    SurfaceSessionSpec,
)
from services.api.app.domain.interaction.surface_driver import web_primary_surface_ref
from services.api.app.infrastructure.db.evidence_exit_gate_repository import (
    EvidenceExitGatePersistenceError,
)
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    PostgresRestorationAttemptRepository,
)
from services.api.tests.test_h5_closed_session_recapture_postgres import _closed_request

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="需原库事务测试连接"
)


@pytest.mark.parametrize("phase", ["clearance", "route"])
@pytest.mark.parametrize("mutation", [
    None, "cancel", "finalizing", "epoch", "foreign_run", "expired_request",
    "expired_request_lease", "terminal_request", "terminal_attempt", "expired_plan",
    "expired_session_lease", "session_owner", "stale_capture", "wrong_plan",
    "wrong_request", "wrong_token", "wrong_sequence", "business_click", "high_risk",
])
async def test_entry_clearance_requires_live_persisted_h5_restoration(mutation, phase):
    from services.api.app.infrastructure.db.h5_restoration_exit_gate import (
        PostgresH5RestorationExitGate,
    )

    async with _closed_request(
        task_status="running" if mutation == "cancel" else "review", check_constraint=False,
    ) as fixture:
        connection, pool, ids, request, plan, sessions, _, lease = fixture
        edge_id = "test-restoration-edge:" + str(uuid4())
        if phase == "route":
            plan = build_restoration_plan(
                request=request, source_session_id=plan.source_session_id,
                target_session_id=plan.target_session_id, surface_kind=plan.surface_kind,
                strategy=plan.strategy, deadline=plan.deadline,
                replay_execution_plan_id=plan.replay_execution_plan_id,
                normalization_step_ids=(edge_id,),
            )
            await connection.execute(
                """INSERT INTO qc_page_graph_transition (
                    transition_id,task_id,agent_run_id,surface_kind,surface_session_id,
                    finalization_epoch,source_state_id,target_state_id,action_kind,
                    semantic_target,risk_level,status,terminal,proof,replay_eligible
                ) VALUES($1,$2,$3,'web',$4,0,$5,$5,'click','详情入口',
                    'low','completed',true,'trusted',true)""",
                edge_id, ids["task"], ids["run"], plan.source_session_id, ids["original"],
            )
        attempt = await PostgresRestorationAttemptRepository(pool=pool).start(
            plan=plan, attempt_no=lease.attempt_count,
        )
        spec = SurfaceSessionSpec(
            task_id=ids["task"], session_id=plan.target_session_id, surface_kind=SurfaceKind.WEB,
        )
        await sessions.register_active(
            spec=spec, session_owner=f"h5-snapshot-recapture:{ids['run']}",
            primary_surface_ref=web_primary_surface_ref(spec.session_id),
        )
        await connection.execute(
            "UPDATE qc_capture SET surface_session_id=$2 WHERE id=$1", ids["restored"],
            spec.session_id,
        )
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=$2 WHERE session_id=$1",
            spec.session_id, ids["restored"],
        )
        permit = await sessions.acquire_command(
            task_id=spec.task_id, session_id=spec.session_id, surface_kind=SurfaceKind.WEB,
            lease_owner="h5-clearance-test", lease_token=str(uuid4()), lease_seconds=60,
            allow_draining=True,
        )
        intent = ActionIntent(
            task_id=spec.task_id, session_id=spec.session_id, surface_kind=SurfaceKind.WEB,
            intent_id="h5-recapture-entry-normalize:" + "a" * 64,
            idempotency_key="h5-recapture-entry-normalize:" + "a" * 64,
            action_type=InteractionOperation.CLICK, source_capture_id=ids["restored"],
            semantic_target="关闭当前弹窗", purpose="恢复入口清障",
            capture_profile=H5SurfaceBootstrapper.capture_profile(), deadline=plan.deadline,
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "rendered_modal_close",
                "obstruction_signature": "h5-obstruction:" + "b" * 64,
                "supplemental_evidence_source": "targeted_recapture",
                "evidence_request_id": request.request_id, "restoration_plan_id": plan.plan_id,
                "viewport_width": 390, "viewport_height": 844,
            },
        )
        if phase == "route":
            identity = "h5-targeted-recapture-route:" + "a" * 64
            intent = replace(
                intent, intent_id=identity, idempotency_key=identity, semantic_target="详情入口",
                parameters={
                    "supplemental_evidence_source": "targeted_recapture",
                    "evidence_request_id": request.request_id,
                    "restoration_plan_id": plan.plan_id, "recovery_transition_id": edge_id,
                },
            )
        task_changes = {
            "cancel": "status='canceled'",
            "finalizing": "finalization_state='finalizing'",
            "epoch": "finalization_epoch=finalization_epoch+1",
            "foreign_run": "finalization_agent_run_id=NULL",
        }
        if mutation in task_changes:
            await connection.execute(
                "UPDATE qc_task SET " + task_changes[mutation] + " WHERE id=$1", ids["task"],
            )
        request_changes = {
            "expired_request": "expires_at=now()-interval '1 second'",
            "expired_request_lease": "lease_expires_at=now()-interval '1 second'",
            "terminal_request": (
                "status='unfulfillable',completed_at=now(),"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL"
            ),
        }
        if mutation in request_changes:
            await connection.execute(
                "UPDATE qc_evidence_request SET " + request_changes[mutation]
                + " WHERE request_id=$1", request.request_id,
            )
        if mutation == "terminal_attempt":
            await connection.execute(
                "UPDATE qc_restoration_attempt SET status='failed',error_code='TEST_FAILED',"
                "finished_at=clock_timestamp() WHERE attempt_id=$1", attempt.attempt_id,
            )
        if mutation == "expired_plan":
            await connection.execute(
                "UPDATE qc_restoration_attempt SET plan_payload=jsonb_set(plan_payload,"
                "'{deadline}',to_jsonb((now()-interval '1 second')::text)) WHERE attempt_id=$1",
                attempt.attempt_id,
            )
        session_changes = {
            "expired_session_lease": "lease_expires_at=now()-interval '1 second'",
            "session_owner": "session_owner='h5-workflow'",
            "stale_capture": "current_capture_id=NULL",
        }
        if mutation in session_changes:
            await connection.execute(
                "UPDATE qc_surface_session SET " + session_changes[mutation]
                + " WHERE session_id=$1", spec.session_id,
            )
        if mutation in {"wrong_plan", "wrong_request", "business_click"}:
            parameters = dict(intent.parameters)
            key = {"wrong_plan": "restoration_plan_id", "wrong_request": "evidence_request_id",
                   "business_click": (
                       "recovery_transition_id" if phase == "route" else "target_mode"
                   )}[mutation]
            parameters[key] = "wrong"
            intent = replace(intent, parameters=parameters)
        if mutation == "high_risk":
            intent = replace(intent, risk_level=InteractionRiskLevel.HIGH)
        if mutation == "wrong_token":
            permit = replace(permit, lease_token=str(uuid4()))
        if mutation == "wrong_sequence":
            permit = replace(permit, command_sequence=permit.command_sequence+1)
        gate = PostgresH5RestorationExitGate(pool=pool)
        if mutation is None:
            assert await gate.reserve(intent=intent, permit=permit) is None
            assert await connection.fetchval(
                "SELECT status FROM qc_restoration_attempt WHERE attempt_id=$1", attempt.attempt_id,
            ) == "running"
            assert permit.lease_expires_at > datetime.now(UTC)
        else:
            with pytest.raises(EvidenceExitGatePersistenceError):
                await gate.reserve(intent=intent, permit=permit)
