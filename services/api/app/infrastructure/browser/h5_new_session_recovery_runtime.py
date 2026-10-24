"""H5 Snapshot 在原会话关闭后，以新现场重放已证明的页面路线。"""

import hashlib
from datetime import datetime
from typing import Any

from services.api.app.application.capture_coordination import CaptureCoordinationCommand
from services.api.app.application.evidence_recovery_replay import EvidenceRecoveryReplayError
from services.api.app.application.h5_exploration_observation import build_h5_frontier
from services.api.app.application.h5_exploration_workflow import H5SurfaceBootstrapper
from services.api.app.application.h5_viewport import h5_viewport_parameters
from services.api.app.domain.exploration.h5_page_state import h5_page_state_has_overlay
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
    SurfaceSessionSpec,
)
from services.api.app.infrastructure.browser.h5_evidence_recovery_runtime import (
    H5EvidenceRecoveryRuntime,
)


class H5NewSessionRecoveryRuntime(H5EvidenceRecoveryRuntime):
    """不沿用旧 DOM、坐标或 Cookie；每条恢复边均从新 Capture 重新签发。"""

    def __init__(
        self,
        *,
        capture_runtime: Any,
        checkpoint: Any,
        deadline: datetime,
        settle_interrupted_restorations: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._capture_runtime = capture_runtime
        self._checkpoint = checkpoint
        self.deadline = deadline
        self._settle_interrupted_restorations = settle_interrupted_restorations
        self._plans: dict[str, Any] = {}
        self._owned_sessions: dict[str, str] = {}

    async def remember_new_session_plan(self, *, plan: Any, context: Any) -> None:
        if (
            (not context.source_session_closed and not context.isolated_display)
            or not context.entry_url
            or plan.task_id != context.task_id
            or plan.strategy.value != "new_session_replay"
            or plan.target_session_id == context.session_id
            or plan.target_session_id != context.replay_session_id
            or plan.source_session_id != context.session_id
            or plan.deadline > self.deadline
        ):
            raise _permanent("H5_NEW_SESSION_PLAN_SCOPE_INVALID")
        self._plans[plan.plan_id] = context

    async def prepare_new_session(
        self, *, plan: Any, agent_run_id: str, finalization_epoch: int
    ) -> tuple[Any, str | tuple[str, ...]]:
        self._validate_scope(
            task_id=plan.task_id,
            session_id=plan.target_session_id,
            plan=plan,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        context = self._plans.get(plan.plan_id)
        if (
            context is None
            or context.agent_run_id != agent_run_id
            or context.finalization_epoch != finalization_epoch
        ):
            raise _permanent("H5_NEW_SESSION_CONTEXT_UNAVAILABLE")
        await self._checkpoint()
        spec = SurfaceSessionSpec(
            task_id=plan.task_id,
            session_id=plan.target_session_id,
            surface_kind=SurfaceKind.WEB,
            options={"target_url": context.entry_url, **h5_viewport_parameters()},
        )
        # 启动或注册失败也要回收物理句柄，不能只在成功注册后记录所有权。
        self._owned_sessions[spec.session_id] = spec.task_id
        session = await self._capture_runtime.surface_driver.start_session(spec)
        await self._surface_sessions.register_active(
            spec=spec,
            session_owner=f"h5-snapshot-recapture:{agent_run_id}",
            primary_surface_ref=session.primary_surface_ref,
        )
        await self._checkpoint()
        action_id = f"h5-recapture-bootstrap:{plan.plan_hash}"
        result = await self._capture_coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=agent_run_id,
                allow_draining=True,
                intent=ActionIntent(
                    task_id=plan.task_id,
                    session_id=spec.session_id,
                    intent_id=action_id,
                    idempotency_key=action_id,
                    surface_kind=SurfaceKind.WEB,
                    source_capture_id=f"h5-entry:{agent_run_id}",
                    action_type=InteractionOperation.NAVIGATE,
                    semantic_target="H5 任务锁定入口",
                    purpose="打开任务锁定的 H5 目标 URL",
                    capture_profile=H5SurfaceBootstrapper.capture_profile(),
                    deadline=plan.deadline,
                    parameters={
                        "destination_url": context.entry_url,
                        "initial_surface_action": True,
                        "supplemental_evidence_source": "targeted_recapture",
                        "evidence_request_id": plan.request_id,
                        "restoration_plan_id": plan.plan_id,
                        **h5_viewport_parameters(),
                    },
                    risk_level=InteractionRiskLevel.LOW,
                ),
            )
        )
        if result.interaction.status is not InteractionResultStatus.SUCCEEDED:
            raise EvidenceRecoveryReplayError(
                reason_code=result.interaction.error_code or "H5_NEW_SESSION_ENTRY_FAILED",
                message=result.interaction.error_message or "H5 新会话入口动作失败",
            )
        capture = result.after_capture
        observed = await self._remember_entry_capture(
            plan=plan,
            capture=capture,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        if context.entry_normalization_required:
            return await self._normalize_entry(
                plan=plan,
                context=context,
                capture=capture,
                observed=observed,
                action_id=result.interaction.action_execution_id,
                agent_run_id=agent_run_id,
                finalization_epoch=finalization_epoch,
            )
        if observed.page_identity != context.current_identity:
            raise _permanent("H5_NEW_SESSION_ENTRY_STATE_DIVERGED")
        return capture, result.interaction.action_execution_id

    async def _remember_entry_capture(
        self,
        *,
        plan: Any,
        capture: Any,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> Any:
        if (
            capture.task_id != plan.task_id
            or capture.session_id != plan.target_session_id
            or capture.surface_kind is not SurfaceKind.WEB
        ):
            raise _permanent("H5_NEW_SESSION_CAPTURE_SCOPE_INVALID")
        await self._checkpoint()
        detection = await self._detector.detect(
            capture=capture,
            task_id=plan.task_id,
            agent_run_id=agent_run_id,
        )
        frontier = build_h5_frontier(
            detection_result=detection,
            observed_at=self._now(),
            navigation_depth=0,
            logical_surface_effective=self._logical_surface_effective,
            logical_targets_effective=self._logical_targets_effective,
            h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
            trusted_source_restoration=True,
            source_task_id=plan.task_id,
        )
        if frontier.observation.capture_id != capture.capture_id:
            raise _permanent("H5_NEW_SESSION_CAPTURE_SCOPE_INVALID")
        self._frontiers[capture.capture_id] = frontier
        observed = await self._read_observation(
            task_id=plan.task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            capture_id=capture.capture_id,
        )
        return observed

    async def _normalize_entry(
        self,
        *,
        plan: Any,
        context: Any,
        capture: Any,
        observed: Any,
        action_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> tuple[Any, str | tuple[str, ...]]:
        """只清障和等待入口，不沿未证明的业务控件另建一棵探索树。"""
        from services.api.app.application.h5_runtime_composition import (
            _trusted_popup_candidate_parameters_for_state,
            _trusted_startup_ad_candidate_parameters_for_page,
        )

        actions = [action_id]
        observations = 0
        while True:
            frontier = self._frontiers[capture.capture_id]
            state = frontier.observation.page_state
            transient = frontier.observation.page_type in {"startup_ad", "line_loading"}
            if (
                observed.page_identity == context.current_identity
                and not transient
                and not h5_page_state_has_overlay(state)
            ):
                return capture, actions[0] if len(actions) == 1 else tuple(actions)
            await self._checkpoint()
            if self._now() >= min(plan.deadline, self.deadline):
                raise _permanent("H5_NEW_SESSION_ENTRY_CLEARANCE_DEADLINE_EXCEEDED")
            matches = []
            for candidate in frontier.candidates:
                if candidate.candidate_id in frontier.blocked_candidate_ids:
                    continue
                parameters = _trusted_startup_ad_candidate_parameters_for_page(
                    page_type=frontier.observation.page_type,
                    candidate=candidate,
                ) or _trusted_popup_candidate_parameters_for_state(
                    page_state=state,
                    candidate=candidate,
                )
                if parameters:
                    matches.append((candidate, parameters))
            if len(matches) > 1:
                raise _permanent("H5_NEW_SESSION_ENTRY_CLEARANCE_AMBIGUOUS")
            if matches:
                candidate, parameters = matches[0]
                operation, target = candidate.action_type, candidate.semantic_target
            else:
                # 允许入口异步加载的一次短暂复核；不以稳定登录页、外站或
                # 未知按钮替代预期起点，也不反复观察无法关闭的遮挡。
                if observations >= 2:
                    raise _permanent("H5_NEW_SESSION_ENTRY_STATE_DIVERGED")
                observations += 1
                operation, target, parameters = InteractionOperation.OBSERVE, None, {}
            digest = hashlib.sha256(
                f"{plan.plan_id}:{capture.capture_id}:{operation.value}".encode()
            ).hexdigest()
            identity = f"h5-recapture-entry-normalize:{digest}"
            result = await self._capture_coordinator.coordinate(
                CaptureCoordinationCommand(
                    agent_run_id=agent_run_id,
                    allow_draining=True,
                    intent=ActionIntent(
                        task_id=plan.task_id,
                        session_id=plan.target_session_id,
                        intent_id=identity,
                        idempotency_key=identity,
                        surface_kind=SurfaceKind.WEB,
                        source_capture_id=capture.capture_id,
                        action_type=operation,
                        semantic_target=target,
                        purpose="在新 H5 会话恢复已记录的入口业务起点",
                        capture_profile=H5SurfaceBootstrapper.capture_profile(),
                        deadline=plan.deadline,
                        risk_level=InteractionRiskLevel.LOW,
                        parameters={
                            **parameters,
                            **h5_viewport_parameters(),
                            "supplemental_evidence_source": "targeted_recapture",
                            "evidence_request_id": plan.request_id,
                            "restoration_plan_id": plan.plan_id,
                        },
                    ),
                )
            )
            if result.interaction.status is not InteractionResultStatus.SUCCEEDED:
                raise _permanent(
                    result.interaction.error_code or "H5_NEW_SESSION_ENTRY_CLEARANCE_FAILED"
                )
            capture = result.after_capture
            actions.append(result.interaction.action_execution_id)
            observed = await self._remember_entry_capture(
                plan=plan,
                capture=capture,
                agent_run_id=agent_run_id,
                finalization_epoch=finalization_epoch,
            )

    async def observe(self, **kwargs: Any) -> Any:
        await self._checkpoint()
        return await super().observe(**kwargs)

    async def execute(self, **kwargs: Any) -> Any:
        await self._checkpoint()
        return await super().execute(**kwargs)

    async def read_probe_frontier(self, **kwargs: Any) -> Any:
        await self._checkpoint()
        return await super().read_probe_frontier(**kwargs)

    async def can_resume_new_session(self, *, attempt: Any) -> bool:
        session = await self._surface_sessions.get(session_id=attempt.plan.target_session_id)
        return bool(
            session is not None
            and session.task_id == attempt.plan.task_id
            and session.status.value in {"active", "draining"}
            and session.current_capture_id == attempt.restored_capture_id
            and attempt.plan.target_session_id in self._owned_sessions
        )

    async def close_owned_sessions(self) -> None:
        """挂接与规则唤醒完成后关闭；失败和取消同样释放会话及租约。"""
        failures = []
        for session_id, task_id in tuple(self._owned_sessions.items()):
            try:
                if self._settle_interrupted_restorations is not None:
                    try:
                        await self._settle_interrupted_restorations(
                            task_id=task_id,
                            session_id=session_id,
                        )
                    except Exception as error:
                        failures.append(error)
                persisted = await self._surface_sessions.get(session_id=session_id)
                if persisted is None or persisted.status.value in {"closed", "lost", "failed"}:
                    await self._capture_runtime.close_browser_session(session_id)
                    if persisted is not None:
                        await self._capture_runtime.session_command_gate.release_session(
                            task_id=task_id,
                            session_id=session_id,
                        )
                else:
                    await self._capture_runtime.close_h5_exploration_session(
                        task_id=task_id,
                        session_id=session_id,
                    )
                del self._owned_sessions[session_id]
            except Exception as error:
                failures.append(error)
                try:
                    await self._capture_runtime.close_browser_session(session_id)
                except Exception as physical_error:
                    failures.append(physical_error)
        if failures:
            raise ExceptionGroup("H5 新会话补采回收失败", failures)


def _permanent(reason: str) -> EvidenceRecoveryReplayError:
    error = EvidenceRecoveryReplayError(reason_code=reason, message=reason)
    error.retryable = False
    return error
