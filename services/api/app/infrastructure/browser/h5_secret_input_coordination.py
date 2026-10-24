"""把专用凭证窗口接入统一动作账本，秘密值始终不进入公共合同。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
    CapturePhase,
    CaptureWriter,
    CaptureWriteRequest,
    ExecuteInteractionRequest,
    InteractionExecutor,
)
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.h5_secret_input import H5SecretInputCommand, H5SecretInputResult
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidationError,
)
from services.api.app.application.interaction_runtime import (
    ActionEvidenceBuffer,
    PendingActionEvidence,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    CaptureRef,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
    build_action_execution_id,
)
from services.api.app.infrastructure.browser.h5_secret_input_runtime import (
    PlaywrightH5SecretInputRuntime,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureResult,
)


class CoordinatedH5SecretInputRuntime:
    """正式登录提交也使用同一租约、前后采集及持久动作记录。"""

    def __init__(
        self,
        *,
        runtime: PlaywrightH5SecretInputRuntime,
        capture_writer: CaptureWriter,
        evidence_buffer: ActionEvidenceBuffer,
        coordinator_factory: Callable[[CaptureWriter, InteractionExecutor], CaptureCoordinator],
    ) -> None:
        self._runtime = runtime
        self._capture_writer = capture_writer
        self._evidence_buffer = evidence_buffer
        self._coordinator_factory = coordinator_factory

    async def execute(self, command: H5SecretInputCommand) -> H5SecretInputResult:
        if not command.agent_run_id:
            return _failure(command, "secret_input_run_required")
        intent_id = f"h5-auth-secret:{command.agent_run_id}"
        intent = ActionIntent(
            task_id=command.task_id,
            session_id=command.session_id,
            source_capture_id=command.source_capture_id,
            intent_id=intent_id,
            idempotency_key=intent_id,
            surface_kind=SurfaceKind.WEB,
            action_type=InteractionOperation.INPUT,
            semantic_target="已配置凭证的密码登录表单",
            purpose="在专用秘密窗口提交已授权登录表单，随后清空输入并重新观察",
            risk_level=InteractionRiskLevel.MEDIUM,
            parameters={},
            capture_profile=CaptureProfile(
                profile_id="h5-secret-input.v1",
                before=CaptureDirective(
                    role="before_action",
                    artifact_types=("screenshot", "dom", "visible_text", "ui_snapshot"),
                ),
                after=CaptureDirective(
                    role="after_action",
                    artifact_types=(
                        "screenshot",
                        "dom",
                        "visible_text",
                        "ui_snapshot",
                        "action_trace",
                    ),
                ),
            ),
            deadline=command.deadline,
        )
        executor = _SecretExecutor(self._runtime, command, self._evidence_buffer)
        writer = _SecretCaptureWriter(self._capture_writer, executor)
        coordinator = self._coordinator_factory(writer, executor)
        try:
            coordinated = await coordinator.coordinate(
                CaptureCoordinationCommand(agent_run_id=command.agent_run_id, intent=intent)
            )
        except CaptureCoordinationError as exc:
            if executor.result is not None:
                # 未能留下完整后态时不得对上游宣告登录提交已验证；清空状态仍如实传递。
                return replace(executor.result, submitted=False, reason_code=exc.error_code)
            return _failure(command, exc.error_code)
        if executor.result is None:
            # 持久账本恢复可返回旧动作，但这里不推断当前登录状态、不再次填写凭证。
            return _failure(command, "secret_input_already_recorded")
        return replace(
            executor.result,
            after_capture=coordinated.after_capture,
            action_execution_id=coordinated.interaction.action_execution_id,
        )


class _SecretExecutor:
    def __init__(self, runtime, command, evidence_buffer) -> None:
        self._runtime = runtime
        self._command = command
        self._evidence_buffer = evidence_buffer
        self.result: H5SecretInputResult | None = None

    async def execute(self, request: ExecuteInteractionRequest) -> InteractionResult:
        # 重新绑定 Coordinator 刚保存的前态，不能继续用旧 Snapshot 的选择器。
        command = replace(self._command, source_capture_id=request.source_capture_id)
        try:
            self.result = await self._runtime.execute_in_permit(
                command, interaction_request=request
            )
        except InteractionCommandValidationError as exc:
            self.result = _failure(command, exc.error_code)
        except Exception:  # noqa: BLE001 - 秘密窗口异常不得带出原始异常链
            self.result = replace(
                _failure(command, "secret_input_outcome_unknown"), fields_scrubbed=False
            )
        success = self.result.submitted and self.result.fields_scrubbed
        interaction = InteractionResult(
            task_id=request.intent.task_id,
            session_id=request.intent.session_id,
            intent_id=request.intent.intent_id,
            action_execution_id=request.action_execution_id
            or build_action_execution_id(request.intent),
            source_capture_id=request.source_capture_id,
            status=(
                InteractionResultStatus.EVIDENCE_PENDING
                if success
                else InteractionResultStatus.FAILED
            ),
            error_code=None if success else self.result.reason_code,
            error_message=None if success else "凭证窗口未完成安全提交",
            state_summary={
                "secret_input": self.result.to_safe_dict(),
                "post_action_evidence_available": self.result.fields_scrubbed,
                "authentication_verified": False,
            },
        )
        # 只放入无秘密的执行回执；原请求、输入值、请求正文和选择器不进入产物。
        safe_trace = [{"action": "credential_submit", **self.result.to_safe_dict()}]
        await self._evidence_buffer.put(
            PendingActionEvidence(
                interaction=interaction,
                browser_result=PageCaptureResult(
                    execution_status="success" if success else "failed",
                    mode="single_trigger",
                    reachable=None,
                    final_url=None,
                    title=None,
                    status_code=None,
                    action_trace=safe_trace,
                    metrics={
                        "secret_input": self.result.to_safe_dict(),
                        "authentication_verified": False,
                    },
                    artifacts=[
                        PageCaptureArtifact(
                            artifact_type="action_trace",
                            artifact_id=None,
                            content=safe_trace,
                        )
                    ],
                ),
            )
        )
        return interaction


class _SecretCaptureWriter:
    def __init__(self, writer: CaptureWriter, executor: _SecretExecutor) -> None:
        self._writer = writer
        self._executor = executor

    async def capture(self, request: CaptureWriteRequest) -> CaptureRef:
        if request.phase is CapturePhase.AFTER_ACTION and (
            self._executor.result is None or not self._executor.result.fields_scrubbed
        ):
            raise RuntimeError("秘密字段未确认清空，禁止动作后采集")
        return await self._writer.capture(request)


def _failure(command: H5SecretInputCommand, reason: str) -> H5SecretInputResult:
    return H5SecretInputResult(
        submitted=False,
        fields_scrubbed=True,
        hostname=command.target_hostname,
        reason_code=reason,
    )
