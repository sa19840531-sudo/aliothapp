"""W17-W20 阶段机到 Interaction Runtime 的动作交接合同。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from services.api.app.domain.exploration import (
    AuthGateDecision,
    AuthGateNextAction,
    PopupChainDecision,
    PopupChainNextAction,
    StartupAdCountdownDecision,
    StartupAdCountdownNextAction,
    SurfacePhaseRunSnapshot,
    SystemDialogDecision,
    SystemDialogNextAction,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    InteractionRiskLevel,
)
from services.api.app.domain.protocol import stable_protocol_hash

SurfacePhaseActionDecision = (
    StartupAdCountdownDecision
    | PopupChainDecision
    | AuthGateDecision
    | SystemDialogDecision
)
SurfacePhaseNextAction = (
    StartupAdCountdownNextAction
    | PopupChainNextAction
    | AuthGateNextAction
    | SystemDialogNextAction
)


class SurfacePhaseActionHandoffError(ValueError):
    """阶段机动作无法安全交给 Interaction Runtime。"""


class SurfacePhaseActionHandoffKind(StrEnum):
    """阶段机下一步动作的运行时交接类型。"""

    ACTION = "action"
    TIMED_CAPTURE = "timed_capture"


@dataclass(frozen=True, kw_only=True)
class SurfacePhaseTimedCaptureRequest:
    """等待一段时间后重新采集当前 Surface 的请求。"""

    task_id: str
    session_id: str
    phase_run_id: str
    phase_key: str
    decision_id: str
    intent_id: str
    source_capture_id: str
    delay_ms: int
    directive: CaptureDirective


@dataclass(frozen=True, kw_only=True)
class SurfacePhaseActionHandoff:
    """阶段机下一步动作的统一交接结果。"""

    kind: SurfacePhaseActionHandoffKind
    phase_run_id: str
    decision_id: str
    action_intent: ActionIntent | None = None
    timed_capture: SurfacePhaseTimedCaptureRequest | None = None

    def __post_init__(self) -> None:
        if self.kind is SurfacePhaseActionHandoffKind.ACTION:
            if self.action_intent is None or self.timed_capture is not None:
                raise SurfacePhaseActionHandoffError("action 交接必须且只能携带 ActionIntent")
        if self.kind is SurfacePhaseActionHandoffKind.TIMED_CAPTURE:
            if self.timed_capture is None or self.action_intent is not None:
                raise SurfacePhaseActionHandoffError("timed_capture 交接必须且只能携带采集请求")


def build_surface_phase_action_handoff(
    *,
    run: SurfacePhaseRunSnapshot,
    decision: SurfacePhaseActionDecision,
    deadline: datetime,
) -> SurfacePhaseActionHandoff:
    """把阶段机 next_action 转成运行时可消费的稳定交接合同。"""

    if run.phase_run_id != decision.phase_run_id:
        raise SurfacePhaseActionHandoffError("阶段运行与决策不一致")
    action = decision.next_action
    if action is None:
        raise SurfacePhaseActionHandoffError("终态阶段决策没有可交接动作")
    if action.source_capture_id != decision.source_capture_id:
        raise SurfacePhaseActionHandoffError("阶段动作与决策来源 Capture 不一致")
    if action.action_type is InteractionOperation.TIMED_CAPTURE:
        return _timed_capture_handoff(run=run, decision=decision, action=action)
    return _action_intent_handoff(
        run=run,
        decision=decision,
        action=action,
        deadline=deadline,
    )


def _action_intent_handoff(
    *,
    run: SurfacePhaseRunSnapshot,
    decision: SurfacePhaseActionDecision,
    action: SurfacePhaseNextAction,
    deadline: datetime,
) -> SurfacePhaseActionHandoff:
    roles = _capture_roles(action, min_count=2)
    intent = ActionIntent(
        task_id=run.task_id,
        session_id=run.session_id,
        intent_id=action.action_id,
        idempotency_key=_idempotency_key(run=run, decision=decision, action=action),
        surface_kind=run.surface_kind,
        source_capture_id=action.source_capture_id,
        action_type=action.action_type,
        semantic_target=action.semantic_target,
        purpose=action.purpose,
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfile(
            profile_id=f"surface-phase:{run.phase_key}",
            before=CaptureDirective(
                role=roles[0],
                artifact_types=(
                    "screenshot",
                    "ui_tree",
                    "ui_snapshot",
                    "visible_text",
                ),
                inherit_parent_semantics=True,
            ),
            after=CaptureDirective(
                role=roles[1],
                artifact_types=(
                    "screenshot",
                    "ui_tree",
                    "ui_snapshot",
                    "visible_text",
                    "action_trace",
                ),
            ),
        ),
        parameters={
            "phase_run_id": run.phase_run_id,
            "phase_key": run.phase_key,
            "decision_id": decision.decision_id,
            "decision_status": decision.status.value,
            "reason_code": decision.reason_code,
        },
        deadline=deadline,
    )
    return SurfacePhaseActionHandoff(
        kind=SurfacePhaseActionHandoffKind.ACTION,
        phase_run_id=run.phase_run_id,
        decision_id=decision.decision_id,
        action_intent=intent,
    )


def _timed_capture_handoff(
    *,
    run: SurfacePhaseRunSnapshot,
    decision: SurfacePhaseActionDecision,
    action: SurfacePhaseNextAction,
) -> SurfacePhaseActionHandoff:
    roles = _capture_roles(action, min_count=1)
    delay_ms = action.delay_ms
    if delay_ms is None:
        raise SurfacePhaseActionHandoffError("timed_capture 必须携带 delay_ms")
    request = SurfacePhaseTimedCaptureRequest(
        task_id=run.task_id,
        session_id=run.session_id,
        phase_run_id=run.phase_run_id,
        phase_key=run.phase_key,
        decision_id=decision.decision_id,
        intent_id=action.action_id,
        source_capture_id=action.source_capture_id,
        delay_ms=delay_ms,
        directive=CaptureDirective(
            role=roles[0],
            artifact_types=(
                "screenshot",
                "ui_tree",
                "ui_snapshot",
                "visible_text",
                "temporal_trace",
            ),
            inherit_parent_semantics=True,
        ),
    )
    return SurfacePhaseActionHandoff(
        kind=SurfacePhaseActionHandoffKind.TIMED_CAPTURE,
        phase_run_id=run.phase_run_id,
        decision_id=decision.decision_id,
        timed_capture=request,
    )


def _capture_roles(
    action: SurfacePhaseNextAction,
    *,
    min_count: int,
) -> tuple[str, ...]:
    if len(action.capture_roles) < min_count:
        raise SurfacePhaseActionHandoffError("阶段动作缺少必要 Capture role")
    return action.capture_roles


def _idempotency_key(
    *,
    run: SurfacePhaseRunSnapshot,
    decision: SurfacePhaseActionDecision,
    action: SurfacePhaseNextAction,
) -> str:
    return "surface-phase-action:" + stable_protocol_hash(
        {
            "phase_run_id": run.phase_run_id,
            "decision_id": decision.decision_id,
            "action_id": action.action_id,
            "source_capture_id": action.source_capture_id,
            "phase_key": run.phase_key,
        }
    )
