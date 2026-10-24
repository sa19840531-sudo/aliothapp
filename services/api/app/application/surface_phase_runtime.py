"""W17-W20 Surface 阶段机运行时接线。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.api.app.domain.exploration import (
    AUTH_GATE_PHASE_PROTOCOL_VERSION,
    POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
    STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION,
    AuthGateDecision,
    AuthGateDecisionStatus,
    AuthGateObservation,
    AuthGatePhaseContext,
    PopupChainDecision,
    PopupChainDecisionStatus,
    PopupChainObservation,
    PopupChainPhaseContext,
    StartupAdCountdownDecision,
    StartupAdCountdownDecisionStatus,
    StartupAdCountdownObservation,
    StartupAdCountdownPhaseContext,
    SurfacePhaseRunSnapshot,
    SurfacePhaseRunStatus,
    SurfacePhaseStepSnapshot,
    SurfacePhaseStepStatus,
    SystemDialogDecision,
    SystemDialogDecisionStatus,
    SystemDialogObservation,
    SystemDialogPhaseContext,
    evaluate_auth_gate_phase,
    evaluate_popup_chain_phase,
    evaluate_startup_ad_countdown_phase,
    evaluate_system_dialog_phase,
)

type SurfacePhaseContext = (
    StartupAdCountdownPhaseContext
    | PopupChainPhaseContext
    | AuthGatePhaseContext
    | SystemDialogPhaseContext
)
type SurfacePhaseObservation = (
    StartupAdCountdownObservation
    | PopupChainObservation
    | AuthGateObservation
    | SystemDialogObservation
)
type SurfacePhaseDecision = (
    StartupAdCountdownDecision
    | PopupChainDecision
    | AuthGateDecision
    | SystemDialogDecision
)


class SurfacePhaseRuntimeError(RuntimeError):
    """阶段机运行时输入或持久化结果不满足接线合同。"""


class SurfacePhaseRepository(Protocol):
    """W17-W20 运行时需要的阶段账本仓储能力。"""

    async def record_step(
        self,
        *,
        phase_run_id: str,
        step_key: str,
        status: SurfacePhaseStepStatus,
        decision_status: str,
        reason_code: str,
        source_capture_id: str,
        decision_payload: dict[str, object],
        evidence_artifact_ids: tuple[str, ...] = (),
        next_action_payload: dict[str, object] | None = None,
        action_execution_id: str | None = None,
        result_capture_id: str | None = None,
    ) -> SurfacePhaseStepSnapshot: ...

    async def finish_run(
        self,
        *,
        phase_run_id: str,
        status: SurfacePhaseRunStatus,
        terminal_reason_code: str,
        expected_row_version: int,
    ) -> SurfacePhaseRunSnapshot: ...


@dataclass(frozen=True, kw_only=True)
class SurfacePhaseTurnResult:
    """一次阶段机判定和账本写入结果。"""

    decision: SurfacePhaseDecision
    step: SurfacePhaseStepSnapshot
    finished_run: SurfacePhaseRunSnapshot | None = None


class SurfacePhaseRuntime:
    """把 W17-W20 纯领域阶段机决策写入 Surface 阶段账本。"""

    def __init__(self, *, repository: SurfacePhaseRepository) -> None:
        self._repository = repository

    async def evaluate_and_record(
        self,
        *,
        run: SurfacePhaseRunSnapshot,
        context: SurfacePhaseContext,
        observation: SurfacePhaseObservation,
    ) -> SurfacePhaseTurnResult:
        """执行一次阶段判定，记录步骤，并在终态时封存阶段运行。"""

        if run.status is not SurfacePhaseRunStatus.RUNNING:
            raise SurfacePhaseRuntimeError("只有 running 阶段运行可以继续判定")
        if context.phase_run_id != run.phase_run_id:
            raise SurfacePhaseRuntimeError("阶段上下文与阶段运行不一致")
        if context.task_id != run.task_id or context.session_id != run.session_id:
            raise SurfacePhaseRuntimeError("阶段上下文与阶段运行任务会话不一致")
        if observation.source_capture_id != run.source_capture_id and run.step_count == 0:
            raise SurfacePhaseRuntimeError("首轮观测必须使用阶段运行来源 Capture")

        decision = _evaluate(context=context, observation=observation)
        _validate_protocol(run=run, decision=decision)
        step_status = _step_status(decision)
        step = await self._repository.record_step(
            phase_run_id=run.phase_run_id,
            step_key=decision.decision_id,
            status=step_status,
            decision_status=decision.status.value,
            reason_code=decision.reason_code,
            source_capture_id=decision.source_capture_id,
            decision_payload=decision.to_dict(),
            evidence_artifact_ids=decision.evidence_artifact_ids,
            next_action_payload=(
                decision.next_action.to_dict() if decision.next_action is not None else None
            ),
        )
        run_status = _terminal_run_status(decision)
        if run_status is None:
            return SurfacePhaseTurnResult(decision=decision, step=step)
        finished = await self._repository.finish_run(
            phase_run_id=run.phase_run_id,
            status=run_status,
            terminal_reason_code=decision.reason_code,
            expected_row_version=(
                run.row_version + 1
                if step.step_index > run.step_count
                else run.row_version
            ),
        )
        return SurfacePhaseTurnResult(decision=decision, step=step, finished_run=finished)


def _evaluate(
    *,
    context: SurfacePhaseContext,
    observation: SurfacePhaseObservation,
) -> SurfacePhaseDecision:
    if isinstance(context, StartupAdCountdownPhaseContext) and isinstance(
        observation,
        StartupAdCountdownObservation,
    ):
        return evaluate_startup_ad_countdown_phase(context=context, observation=observation)
    if isinstance(context, PopupChainPhaseContext) and isinstance(
        observation,
        PopupChainObservation,
    ):
        return evaluate_popup_chain_phase(context=context, observation=observation)
    if isinstance(context, AuthGatePhaseContext) and isinstance(observation, AuthGateObservation):
        return evaluate_auth_gate_phase(context=context, observation=observation)
    if isinstance(context, SystemDialogPhaseContext) and isinstance(
        observation,
        SystemDialogObservation,
    ):
        return evaluate_system_dialog_phase(context=context, observation=observation)
    raise SurfacePhaseRuntimeError("阶段上下文与观测类型不匹配")


def _validate_protocol(*, run: SurfacePhaseRunSnapshot, decision: SurfacePhaseDecision) -> None:
    expected = {
        "startup_ad_countdown": STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
        "popup_chain": POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
        "auth_gate": AUTH_GATE_PHASE_PROTOCOL_VERSION,
        "system_dialog": SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION,
    }.get(run.phase_key)
    if expected is None:
        raise SurfacePhaseRuntimeError("阶段运行 phase_key 不受支持")
    if run.phase_protocol_version != expected:
        raise SurfacePhaseRuntimeError("阶段运行 phase_key 与协议版本不一致")
    if decision.protocol_version != expected:
        raise SurfacePhaseRuntimeError("阶段运行协议版本与决策协议版本不一致")


def _step_status(decision: SurfacePhaseDecision) -> SurfacePhaseStepStatus:
    if decision.next_action is not None:
        return SurfacePhaseStepStatus.ACTION_PLANNED
    run_status = _terminal_run_status(decision)
    if run_status is SurfacePhaseRunStatus.COMPLETED:
        return SurfacePhaseStepStatus.COMPLETED
    if run_status is SurfacePhaseRunStatus.BLOCKED:
        return SurfacePhaseStepStatus.BLOCKED
    return SurfacePhaseStepStatus.DECIDED


def _terminal_run_status(decision: SurfacePhaseDecision) -> SurfacePhaseRunStatus | None:
    if isinstance(decision, StartupAdCountdownDecision):
        return _startup_ad_terminal_status(decision.status)
    if isinstance(decision, PopupChainDecision):
        return _popup_chain_terminal_status(decision.status)
    if isinstance(decision, AuthGateDecision):
        return _auth_gate_terminal_status(decision.status)
    if isinstance(decision, SystemDialogDecision):
        return _system_dialog_terminal_status(decision.status)
    raise SurfacePhaseRuntimeError("阶段决策类型不受支持")


def _startup_ad_terminal_status(
    status: StartupAdCountdownDecisionStatus,
) -> SurfacePhaseRunStatus | None:
    if status is StartupAdCountdownDecisionStatus.COMPLETED:
        return SurfacePhaseRunStatus.COMPLETED
    if status is StartupAdCountdownDecisionStatus.BLOCKED:
        return SurfacePhaseRunStatus.BLOCKED
    return None


def _popup_chain_terminal_status(status: PopupChainDecisionStatus) -> SurfacePhaseRunStatus | None:
    if status is PopupChainDecisionStatus.COMPLETED:
        return SurfacePhaseRunStatus.COMPLETED
    if status is PopupChainDecisionStatus.BLOCKED:
        return SurfacePhaseRunStatus.BLOCKED
    return None


def _auth_gate_terminal_status(status: AuthGateDecisionStatus) -> SurfacePhaseRunStatus | None:
    if status in {
        AuthGateDecisionStatus.DISMISSED,
        AuthGateDecisionStatus.NOT_PRESENT,
    }:
        return SurfacePhaseRunStatus.COMPLETED
    if status in {
        AuthGateDecisionStatus.REQUIRES_AUTH,
        AuthGateDecisionStatus.BLOCKED,
    }:
        return SurfacePhaseRunStatus.BLOCKED
    return None


def _system_dialog_terminal_status(
    status: SystemDialogDecisionStatus,
) -> SurfacePhaseRunStatus | None:
    if status in {
        SystemDialogDecisionStatus.HANDLED,
        SystemDialogDecisionStatus.NOT_PRESENT,
    }:
        return SurfacePhaseRunStatus.COMPLETED
    if status is SystemDialogDecisionStatus.BLOCKED:
        return SurfacePhaseRunStatus.BLOCKED
    return None
