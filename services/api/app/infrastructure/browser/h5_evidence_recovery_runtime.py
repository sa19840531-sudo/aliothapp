"""H5 定向补采对统一 CaptureCoordinator 的受限适配。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.evidence_recovery_replay import (
    EvidenceRecoveryReplayError,
    RecoveryGraphStep,
    RecoveryObservation,
    RecoveryStepReceipt,
)
from services.api.app.application.h5_exploration_observation import build_h5_frontier
from services.api.app.application.h5_viewport import h5_viewport_parameters
from services.api.app.domain.exploration import FrontierSnapshot, ProbeWork
from services.api.app.domain.inspection import RestorationPlan
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)


class H5EvidenceRecoveryRuntime:
    """只允许 H5 同会话重新观察；不在补采阶段擅自导航或扩展页面树。"""

    def __init__(
        self,
        *,
        surface_sessions: Any,
        capture_coordinator: Any,
        detector: Any,
        page_graph: Any,
        captures: Any | None = None,
        logical_surface_effective: bool = True,
        logical_targets_effective: bool = True,
        h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
        now: Any | None = None,
    ) -> None:
        self._surface_sessions = surface_sessions
        self._capture_coordinator = capture_coordinator
        self._detector = detector
        self._page_graph = page_graph
        self._captures = captures
        self._logical_surface_effective = logical_surface_effective
        self._logical_targets_effective = logical_targets_effective
        self._h5_visual_coordinate_mode = h5_visual_coordinate_mode
        self._now = now or (lambda: datetime.now(UTC))
        self._frontiers: dict[str, Any] = {}
        self._shared_observation: ContextVar[dict | None] = ContextVar(
            "h5_shared_restoration_observation", default=None,
        )

    @contextmanager
    def share_observations(self):
        """仅在一次兼容请求派发批内共享真实新观察，退出即失效。"""
        token = self._shared_observation.set({})
        try:
            yield
        finally:
            self._shared_observation.reset(token)

    def has_shared_observation(self, *, task_id, agent_run_id, finalization_epoch, request):
        values = self._shared_observation.get() or {}
        return any(key[:3] == (task_id, agent_run_id, finalization_epoch)
                   and key[4] == request.target_capture_id for key in values)

    async def shared_observation_is_current(
        self, *, task_id, agent_run_id, finalization_epoch, request,
    ):
        for key, prior in (self._shared_observation.get() or {}).items():
            if (key[:3] != (task_id, agent_run_id, finalization_epoch)
                or key[4] != request.target_capture_id):
                continue
            session = await self._surface_sessions.get(session_id=key[3])
            return bool(session is not None and session.task_id == task_id
                        and session.finalization_epoch == finalization_epoch
                        and session.current_capture_id == prior[0].capture_id
                        and session.command_sequence == prior[1]
                        and session.status == prior[2] and session.session_owner == prior[3])
        return False

    async def read_probe_frontier(self, *, work: ProbeWork) -> FrontierSnapshot:
        """只读当前活会话的恢复后候选；进程重启后从受信 Capture 重新签发。"""
        session = await self._surface_sessions.get(session_id=work.session_id)
        run_id = work.session_id.split(":")[1] if work.session_id.startswith("h5:") else ""
        valid_session = bool(re.fullmatch(
            rf"h5:{re.escape(run_id)}(?::(?:recovery|recapture):[0-9a-f]{{20}})?", work.session_id,
        ))
        try:
            UUID(run_id)
        except ValueError:
            valid_session = False
        expected_owner = (
            "h5-workflow" if work.session_id == f"h5:{run_id}"
            else (f"h5-snapshot-recapture:{run_id}" if ":recapture:" in work.session_id
                  else f"h5-browser-recovery:{run_id}")
        )
        if (
            session is None
            or session.task_id != work.task_id
            or session.surface_kind is not SurfaceKind.WEB
            or str(session.status) not in {"active", "draining"}
            or session.current_capture_id != work.source_capture_id
            or not valid_session
            or session.session_owner != expected_owner
        ):
            raise EvidenceRecoveryReplayError(
                reason_code="H5_PROBE_FRONTIER_SCOPE_MISMATCH",
                message="H5 补采候选已离开当前活会话来源",
            )
        cached = self._frontiers.get(work.source_capture_id)
        if cached is not None:
            return cached
        persisted = (
            await self._captures.get_verified_capture(capture_id=work.source_capture_id)
            if self._captures is not None else None
        )
        capture = getattr(persisted, "capture", None)
        if (
            capture is None or capture.task_id != work.task_id
            or capture.session_id != work.session_id
            or capture.capture_id != work.source_capture_id
            or capture.surface_kind is not SurfaceKind.WEB
        ):
            raise EvidenceRecoveryReplayError(
                reason_code="H5_PROBE_FRONTIER_SOURCE_UNAVAILABLE",
                message="H5 补采缺少已校验的当前 Capture，不能沿用旧候选",
            )
        detection = await self._detector.detect(
            capture=capture, task_id=work.task_id, agent_run_id=run_id,
        )
        frontier = build_h5_frontier(
            detection_result=detection, observed_at=self._now(), navigation_depth=0,
            logical_surface_effective=self._logical_surface_effective,
            logical_targets_effective=self._logical_targets_effective,
            h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
            trusted_source_restoration=True, source_task_id=work.task_id,
        )
        if frontier.observation.capture_id != work.source_capture_id:
            raise EvidenceRecoveryReplayError(
                reason_code="H5_PROBE_FRONTIER_SOURCE_MISMATCH",
                message="H5 补采重新识别返回了其他来源 Capture",
            )
        self._frontiers[work.source_capture_id] = frontier
        return frontier

    async def observe(
        self,
        *,
        task_id: str,
        session_id: str,
        plan: RestorationPlan,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> RecoveryObservation:
        self._validate_scope(
            task_id=task_id,
            session_id=session_id,
            plan=plan,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        session = await self._surface_sessions.get(session_id=session_id)
        current_capture_id = getattr(session, "current_capture_id", None)
        if (
            session is None
            or getattr(session, "task_id", None) != task_id
            or getattr(session, "finalization_epoch", None) != finalization_epoch
            or getattr(session, "surface_kind", None) is not SurfaceKind.WEB
            or not isinstance(current_capture_id, str)
            or not current_capture_id
        ):
            raise EvidenceRecoveryReplayError(
                reason_code="H5_RECAPTURE_SESSION_SCOPE_INVALID",
                message="H5 补采会话不属于当前 Task/Run/Epoch",
            )
        identity = hashlib.sha256(
            f"{plan.plan_id}:{current_capture_id}".encode()
        ).hexdigest()
        shared = self._shared_observation.get()
        shared_key = (
            task_id, agent_run_id, finalization_epoch, session_id, plan.original_capture_id,
        )
        prior = shared.get(shared_key) if shared is not None else None
        if (
            prior is not None and plan.strategy.value == "original_surface_pin"
            and current_capture_id == prior[0].capture_id
            and getattr(session, "command_sequence", None) == prior[1]
            and getattr(session, "status", None) == prior[2]
            and getattr(session, "session_owner", None) == prior[3]
        ):
            return prior[0]
        if prior is not None:
            raise EvidenceRecoveryReplayError(
                reason_code="H5_RECAPTURE_SHARED_SURFACE_CHANGED",
                message="兼容补采派发期间现场已推进，不能继续引用原观察",
            )
        intent = ActionIntent(
            task_id=task_id,
            session_id=session_id,
            intent_id=f"h5-targeted-recapture-observe:{identity}",
            idempotency_key=f"h5-targeted-recapture-observe:{identity}",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=current_capture_id,
            action_type=InteractionOperation.OBSERVE,
            semantic_target=None,
            purpose="在当前 H5 活会话重新观察目标页面后执行定向补采",
            capture_profile=CaptureProfilePolicy.for_surface(
                SurfaceKind.WEB,
                InteractionOperation.OBSERVE,
            ),
            deadline=plan.deadline,
            parameters={
                "evidence_request_id": plan.request_id,
                "restoration_plan_id": plan.plan_id,
                "supplemental_evidence_source": "targeted_recapture",
                **h5_viewport_parameters(),
            },
            risk_level=InteractionRiskLevel.LOW,
        )
        result = await self._capture_coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=agent_run_id,
                intent=intent,
                allow_draining=True,
            )
        )
        if result.interaction.status is not InteractionResultStatus.SUCCEEDED:
            raise EvidenceRecoveryReplayError(
                reason_code=result.interaction.error_code or "H5_RECAPTURE_OBSERVE_FAILED",
                message=result.interaction.error_message or "H5 补采重新观察失败",
            )
        capture = result.after_capture
        detection = await self._detector.detect(
            capture=capture,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        if detection is not None:
            self._frontiers[capture.capture_id] = build_h5_frontier(
                detection_result=detection,
                observed_at=self._now(),
                navigation_depth=0,
                logical_surface_effective=self._logical_surface_effective,
                logical_targets_effective=self._logical_targets_effective,
                h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
                trusted_source_restoration=True,
                source_task_id=task_id,
            )
        observation = await self._read_observation(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            capture_id=capture.capture_id,
        )
        frontier = self._frontiers.get(capture.capture_id)
        if (shared is not None and plan.strategy.value == "original_surface_pin"
            and frontier is not None and frontier.observation.page_state == "stable"
            and frontier.observation.overlay_signature is None
            and frontier.observation.metadata.get("requires_auth") is not True
            and bool(frontier.observation.metadata.get("logical_surface_identity_fingerprint"))):
            latest = await self._surface_sessions.get(session_id=session_id)
            if (latest is not None and latest.current_capture_id == capture.capture_id
                and latest.finalization_epoch == finalization_epoch
                and latest.task_id == task_id
                and isinstance(getattr(latest, "command_sequence", None), int)
                and str(getattr(latest, "status", None)) in {"active", "draining"}
                and bool(getattr(latest, "session_owner", None))):
                shared[shared_key] = (observation, getattr(latest, "command_sequence", None),
                                      getattr(latest, "status", None),
                                      getattr(latest, "session_owner", None))
        return observation

    async def execute(
        self,
        *,
        task_id: str,
        session_id: str,
        plan: RestorationPlan,
        agent_run_id: str,
        finalization_epoch: int,
        step: RecoveryGraphStep,
        source: RecoveryObservation,
    ) -> RecoveryStepReceipt:
        self._validate_scope(
            task_id=task_id,
            session_id=session_id,
            plan=plan,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        frontier = self._frontiers.get(source.capture_id)
        if frontier is None:
            raise EvidenceRecoveryReplayError(
                reason_code="H5_RECAPTURE_SOURCE_FRONTIER_UNAVAILABLE",
                message="H5 补采起点缺少本轮重新观察形成的新鲜 Frontier",
                transition_id=step.transition_id,
            )
        matches = tuple(
            candidate
            for candidate in frontier.candidates
            if candidate.action_type.value == step.action_kind
            and candidate.semantic_target == step.semantic_target
            and candidate.risk_level is InteractionRiskLevel.LOW
            and candidate.candidate_id not in frontier.blocked_candidate_ids
        )
        if len(matches) != 1:
            raise EvidenceRecoveryReplayError(
                reason_code=(
                    "H5_RECAPTURE_ROUTE_CANDIDATE_NOT_FOUND"
                    if not matches
                    else "H5_RECAPTURE_ROUTE_CANDIDATE_AMBIGUOUS"
                ),
                message="H5 补采页面图边未匹配到唯一低风险新鲜候选",
                transition_id=step.transition_id,
            )
        candidate = matches[0]
        raw_parameters = candidate.metadata.get("action_parameters")
        if raw_parameters is not None and not isinstance(raw_parameters, Mapping):
            raise EvidenceRecoveryReplayError(
                reason_code="H5_RECAPTURE_ROUTE_PARAMETERS_INVALID",
                message="H5 补采候选执行参数无效",
                transition_id=step.transition_id,
            )
        parameters = dict(raw_parameters or {})
        from services.api.app.application.h5_runtime_composition import (
            _h5_candidate_parameters,
            _has_visual_navigation_provenance,
        )

        if (
            candidate.metadata.get("logical_target_required") is True
            or "target_mode" in parameters
            or _has_visual_navigation_provenance(
                candidate=candidate, action_parameters=parameters,
            )
        ):
            # 只构造签发所需的当前现场视图，不新增预算或另建探索工作项。
            current = SimpleNamespace(
                work_id=plan.plan_id, task_id=task_id, session_id=session_id,
                surface_kind=SurfaceKind.WEB, source_capture_id=source.capture_id,
                frontier_snapshot_id=frontier.snapshot_id, frontier=frontier,
            )
            try:
                parameters = _h5_candidate_parameters(
                    work=current, candidate=candidate,
                    h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
                )
            except (CaptureCoordinationError, ValueError) as error:
                raise EvidenceRecoveryReplayError(
                    reason_code=getattr(
                        error, "error_code", "H5_RECAPTURE_ROUTE_PARAMETERS_INVALID",
                    ),
                    message="H5 恢复候选未通过新现场签发校验",
                    transition_id=step.transition_id,
                ) from error
        identity = hashlib.sha256(
            f"{plan.plan_id}:{step.transition_id}:{source.capture_id}:{candidate.candidate_id}".encode()
        ).hexdigest()
        intent = ActionIntent(
            task_id=task_id,
            session_id=session_id,
            intent_id=f"h5-targeted-recapture-route:{identity}",
            idempotency_key=f"h5-targeted-recapture-route:{identity}",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=source.capture_id,
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_target,
            purpose=str(candidate.metadata.get("purpose") or "按可信页面图恢复 H5 补采目标"),
            capture_profile=CaptureProfilePolicy.for_surface(
                SurfaceKind.WEB,
                candidate.action_type,
            ),
            deadline=plan.deadline,
            parameters={
                **parameters,
                **h5_viewport_parameters(),
                "candidate_id": candidate.candidate_id,
                "navigation_depth": candidate.navigation_depth,
                "evidence_request_id": plan.request_id,
                "restoration_plan_id": plan.plan_id,
                "recovery_transition_id": step.transition_id,
                "supplemental_evidence_source": "targeted_recapture",
            },
            risk_level=InteractionRiskLevel.LOW,
        )
        result = await self._capture_coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=agent_run_id,
                intent=intent,
                allow_draining=True,
            )
        )
        if result.interaction.status is not InteractionResultStatus.SUCCEEDED:
            raise EvidenceRecoveryReplayError(
                reason_code=result.interaction.error_code or "H5_RECAPTURE_ROUTE_EXECUTION_FAILED",
                message=result.interaction.error_message or "H5 补采页面图边执行失败",
                transition_id=step.transition_id,
            )
        capture = result.after_capture
        detection = await self._detector.detect(
            capture=capture,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        if detection is not None:
            self._frontiers[capture.capture_id] = build_h5_frontier(
                detection_result=detection,
                observed_at=self._now(),
                navigation_depth=candidate.navigation_depth,
                logical_surface_effective=self._logical_surface_effective,
                logical_targets_effective=self._logical_targets_effective,
                h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
                trusted_source_restoration=True,
                source_task_id=task_id,
            )
        after = await self._read_observation(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            capture_id=capture.capture_id,
        )
        return RecoveryStepReceipt(
            transition_id=step.transition_id,
            action_execution_id=result.interaction.action_execution_id,
            after=after,
        )

    async def _read_observation(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        capture_id: str,
    ) -> RecoveryObservation:
        graph = await self._page_graph.get_graph(
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        state = next(
            (
                item
                for item in graph.get("states", ())
                if str(item.get("state_id")) == capture_id
            ),
            None,
        )
        if not isinstance(state, dict):
            raise EvidenceRecoveryReplayError(
                reason_code="H5_RECAPTURE_STATE_NOT_PROJECTED",
                message="H5 补采新 Capture 尚未形成页面图状态",
            )
        observed_at = state.get("captured_at")
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            observed_at = self._now()
        return RecoveryObservation(
            capture_id=capture_id,
            page_identity=str(state.get("logical_page_key") or ""),
            state_fingerprint=str(state.get("state_fingerprint") or ""),
            observed_at=observed_at,
        )

    @staticmethod
    def _validate_scope(
        *,
        task_id: str,
        session_id: str,
        plan: RestorationPlan,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> None:
        if (
            plan.task_id != task_id
            or (
                plan.strategy.value != "new_session_replay"
                and plan.source_session_id != session_id
            )
            or plan.target_session_id != session_id
            or plan.surface_kind is not SurfaceKind.WEB
            or not re.fullmatch(
                rf"h5:{re.escape(agent_run_id)}(?::(?:recovery|recapture):[0-9a-f]{{20}})?",
                session_id,
            )
            or (":recapture:" in session_id and plan.strategy.value != "new_session_replay")
            or finalization_epoch < 0
        ):
            raise EvidenceRecoveryReplayError(
                reason_code="H5_RECAPTURE_SCOPE_MISMATCH",
                message="H5 补采恢复计划越过当前执行域",
            )
