"""H5 主动登录对既有 Surface Phase 账本的无秘密适配。"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from services.api.app.application.capture_coordination import SessionCommandGate
from services.api.app.application.h5_auth_bootstrap import (
    H5AuthAuditState,
    H5AuthBootstrapCommand,
    H5AuthNavigationResult,
)
from services.api.app.domain.exploration import (
    H5AuthStatus,
    SurfacePhaseRunSnapshot,
    SurfacePhaseRunStatus,
    SurfacePhaseStepSnapshot,
    SurfacePhaseStepStatus,
    build_surface_phase_run_id,
)
from services.api.app.domain.interaction import CaptureRef, SurfaceKind

H5_AUTH_PHASE_KEY = "h5_auth_bootstrap"
H5_AUTH_PHASE_PROTOCOL_VERSION = "h5-auth-bootstrap.v1"


class H5AuthSurfacePhaseRepository(Protocol):
    """H5 Auth 审计所需的通用 Surface Phase 仓储子集。"""

    async def get_run(self, *, phase_run_id: str) -> SurfacePhaseRunSnapshot | None: ...

    async def list_steps(
        self,
        *,
        phase_run_id: str,
    ) -> tuple[SurfacePhaseStepSnapshot, ...]: ...

    async def start_run(self, **kwargs: Any) -> SurfacePhaseRunSnapshot: ...

    async def record_step(self, **kwargs: Any) -> SurfacePhaseStepSnapshot: ...

    async def finish_run(self, **kwargs: Any) -> SurfacePhaseRunSnapshot: ...


class SurfacePhaseH5AuthAudit:
    """只把固定认证状态、原因码和 Capture/Action 引用写入账本。"""

    def __init__(
        self,
        *,
        repository: H5AuthSurfacePhaseRepository,
        session_command_gate: SessionCommandGate,
    ) -> None:
        self._repository = repository
        self._session_command_gate = session_command_gate

    async def begin(
        self,
        *,
        command: H5AuthBootstrapCommand,
        source_capture: CaptureRef,
        credential_scope: Literal["domain", "global"] | None,
    ) -> H5AuthAuditState:
        idempotency_key = f"h5-auth:{command.agent_run_id}"
        phase_run_id = build_surface_phase_run_id(
            task_id=command.task_id,
            session_id=command.session_id,
            phase_key=H5_AUTH_PHASE_KEY,
            idempotency_key=idempotency_key,
        )
        run = await self._repository.get_run(phase_run_id=phase_run_id)
        if run is None:
            async with self._session_command_gate.hold(
                task_id=command.task_id,
                session_id=command.session_id,
                surface_kind=SurfaceKind.WEB,
            ) as permit:
                run = await self._repository.start_run(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    surface_kind=SurfaceKind.WEB,
                    phase_key=H5_AUTH_PHASE_KEY,
                    phase_protocol_version=H5_AUTH_PHASE_PROTOCOL_VERSION,
                    idempotency_key=idempotency_key,
                    source_capture_id=source_capture.capture_id,
                    context_payload={
                        "credential_scope": credential_scope,
                        "target_hostname": command.target_hostname,
                    },
                    permit=permit,
                )
        _validate_run(run, command=command)
        persisted_scope = _credential_scope(run.context_payload.get("credential_scope"))
        if persisted_scope != credential_scope:
            raise RuntimeError("H5 认证账号作用域与已存在阶段账本不一致")
        steps = await self._repository.list_steps(phase_run_id=run.phase_run_id)
        attempted = any(
            step.step_key in {
                "credential_submit_planned", "credential_submit_result",
                "registration_submit_planned", "registration_submit_result",
            }
            for step in steps
        )
        registration_attempted = any(
            step.step_key in {"registration_submit_planned", "registration_submit_result"}
            for step in steps
        )
        registration_submitted = any(
            step.step_key == "registration_submit_result"
            and step.decision_status == H5AuthStatus.CHECKING.value
            for step in steps
        )
        login_attempted = any(
            step.step_key in {"credential_submit_planned", "credential_submit_result"}
            for step in steps
        )
        terminal_status, terminal_reason = _terminal_auth_result(run=run, steps=steps)
        return H5AuthAuditState(
            phase_run_id=run.phase_run_id,
            row_version=run.row_version,
            attempted=attempted,
            registration_attempted=registration_attempted,
            registration_submitted=registration_submitted,
            login_attempted=login_attempted,
            terminal=run.status.is_terminal,
            credential_scope=persisted_scope,
            terminal_status=terminal_status,
            terminal_reason_code=terminal_reason,
        )

    async def record(
        self,
        state: H5AuthAuditState,
        *,
        step_key: str,
        status: H5AuthStatus,
        reason_code: str,
        source_capture: CaptureRef,
        result: H5AuthNavigationResult | None = None,
    ) -> H5AuthAuditState:
        if state.terminal or state.phase_run_id is None:
            raise RuntimeError("H5 认证阶段账本已终止或缺少 phase_run_id")
        await self._repository.record_step(
            phase_run_id=state.phase_run_id,
            step_key=step_key,
            status=_step_status(step_key=step_key, status=status, result=result),
            decision_status=status.value,
            reason_code=reason_code,
            source_capture_id=source_capture.capture_id,
            decision_payload={
                "credential_scope": state.credential_scope,
                "reason_code": reason_code,
                "status": status.value,
            },
            action_execution_id=(result.action_execution_id if result is not None else None),
            result_capture_id=(result.capture.capture_id if result is not None else None),
        )
        run = await self._repository.get_run(phase_run_id=state.phase_run_id)
        if run is None:
            raise RuntimeError("H5 认证阶段步骤写入后账本丢失")
        return H5AuthAuditState(
            phase_run_id=state.phase_run_id,
            row_version=run.row_version,
            attempted=state.attempted
            or step_key in {
                "credential_submit_planned", "credential_submit_result",
                "registration_submit_planned", "registration_submit_result",
            },
            registration_attempted=state.registration_attempted
            or step_key in {"registration_submit_planned", "registration_submit_result"},
            registration_submitted=state.registration_submitted
            or (step_key == "registration_submit_result" and status is H5AuthStatus.CHECKING),
            login_attempted=state.login_attempted
            or step_key in {"credential_submit_planned", "credential_submit_result"},
            terminal=False,
            credential_scope=state.credential_scope,
        )

    async def finish(
        self,
        state: H5AuthAuditState,
        *,
        status: H5AuthStatus,
        reason_code: str,
    ) -> None:
        if state.terminal:
            return
        if state.phase_run_id is None:
            raise RuntimeError("H5 认证阶段账本缺少 phase_run_id")
        await self._repository.finish_run(
            phase_run_id=state.phase_run_id,
            status=_run_status(status),
            terminal_reason_code=reason_code,
            expected_row_version=state.row_version,
        )


def _validate_run(
    run: SurfacePhaseRunSnapshot,
    *,
    command: H5AuthBootstrapCommand,
) -> None:
    if (
        run.task_id != command.task_id
        or run.session_id != command.session_id
        or run.surface_kind is not SurfaceKind.WEB
        or run.phase_key != H5_AUTH_PHASE_KEY
        or run.phase_protocol_version != H5_AUTH_PHASE_PROTOCOL_VERSION
    ):
        raise RuntimeError("H5 认证阶段账本归属或协议不一致")
    if run.context_payload.get("target_hostname") != command.target_hostname:
        raise RuntimeError("H5 认证阶段账本 target hostname 不一致")


def _credential_scope(value: object) -> Literal["domain", "global"] | None:
    if value is None:
        return None
    if value in {"domain", "global"}:
        return value  # type: ignore[return-value]
    raise RuntimeError("H5 认证阶段账本 credential_scope 非法")


def _terminal_auth_result(
    *,
    run: SurfacePhaseRunSnapshot,
    steps: tuple[SurfacePhaseStepSnapshot, ...],
) -> tuple[H5AuthStatus | None, str | None]:
    if not run.status.is_terminal:
        return None, None
    for step in sorted(steps, key=lambda item: item.step_index, reverse=True):
        try:
            status = H5AuthStatus(step.decision_status)
        except ValueError:
            continue
        if status is not H5AuthStatus.CHECKING:
            return status, run.terminal_reason_code or step.reason_code
    fallback = (
        H5AuthStatus.GUEST_ONLY
        if run.status is SurfacePhaseRunStatus.BLOCKED
        else H5AuthStatus.FAILED
    )
    return fallback, run.terminal_reason_code or "auth_audit_terminal_state_invalid"


def _step_status(
    *,
    step_key: str,
    status: H5AuthStatus,
    result: H5AuthNavigationResult | None,
) -> SurfacePhaseStepStatus:
    if step_key in {"credential_submit_planned", "registration_submit_planned"}:
        return SurfacePhaseStepStatus.ACTION_PLANNED
    if status in {
        H5AuthStatus.ALREADY_AUTHENTICATED,
        H5AuthStatus.AUTHENTICATED,
        H5AuthStatus.DISABLED,
    }:
        return SurfacePhaseStepStatus.COMPLETED
    if status is H5AuthStatus.GUEST_ONLY:
        return SurfacePhaseStepStatus.BLOCKED
    if status is H5AuthStatus.FAILED:
        return SurfacePhaseStepStatus.FAILED
    if result is not None:
        return SurfacePhaseStepStatus.ACTION_VERIFIED
    return SurfacePhaseStepStatus.DECIDED


def _run_status(status: H5AuthStatus) -> SurfacePhaseRunStatus:
    if status in {
        H5AuthStatus.ALREADY_AUTHENTICATED,
        H5AuthStatus.AUTHENTICATED,
        H5AuthStatus.DISABLED,
    }:
        return SurfacePhaseRunStatus.COMPLETED
    if status is H5AuthStatus.GUEST_ONLY:
        return SurfacePhaseRunStatus.BLOCKED
    return SurfacePhaseRunStatus.FAILED


__all__ = [
    "H5_AUTH_PHASE_KEY",
    "H5_AUTH_PHASE_PROTOCOL_VERSION",
    "SurfacePhaseH5AuthAudit",
]
