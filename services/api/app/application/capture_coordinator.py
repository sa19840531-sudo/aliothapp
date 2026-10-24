"""动作前后 Capture 的确定性编排与完成门禁。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from services.api.app.application.action_executions import ActionExecutionRepository
from services.api.app.application.app_action_evidence import (
    AppActionEvidenceWriter,
    attach_app_action_log_safely,
)
from services.api.app.application.app_capture_persistence import AppCaptureRecoveryError
from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
    CaptureCoordinationResult,
    CaptureCoordinationStage,
    CapturePhase,
    CaptureWriter,
    CaptureWriteRequest,
    ExecuteInteractionRequest,
    InteractionExecutor,
    SessionCommandGate,
)
from services.api.app.application.evidence_exit_gate import (
    EvidenceExitGate,
    is_trusted_h5_browser_recovery_action,
    is_trusted_initial_surface_action,
)
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidationError,
    InteractionCommandValidator,
    TrustedTaskCommandScopeReader,
)
from services.api.app.application.page_capture_persistence import PageCaptureRecoveryError
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
    has_supplemental_evidence_lineage,
)
from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.exploration import (
    EvidenceExitGateDecision,
    EvidenceExitReservationStatus,
)
from services.api.app.domain.exploration.flow_foundation import DeviceExecutionPermit
from services.api.app.domain.interaction import (
    ActionExecutionSnapshot,
    ActionExecutionStatus,
    ActionIntent,
    CaptureDirective,
    CaptureRef,
    CommandPolicy,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    SurfaceKind,
    TimedObservationIntent,
    build_action_execution_id,
    build_action_intent_hash,
)

_LOGGER = logging.getLogger(__name__)


class AppActionRecoveryBlockedError(RuntimeError):
    """旧动作已经越过可安全重放边界，当前 Run 必须按证据不足收口。"""

    retryable = False

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class AppActionRecoveryCaptureReader(Protocol):
    """读取并校验既有 App 截图事实，恢复路径不得重新截图。"""

    async def get_verified_capture(self, *, capture_id: str) -> object | None: ...

    async def get_verified_action_capture(
        self,
        *,
        action_execution_id: str,
        role: str,
    ) -> object | None: ...


__all__ = [
    "CaptureCoordinationCommand",
    "CaptureCoordinationError",
    "CaptureCoordinationResult",
    "CaptureCoordinationStage",
    "CaptureCoordinator",
    "CapturePhase",
    "CaptureWriter",
    "CaptureWriteRequest",
    "ExecuteInteractionRequest",
    "InteractionExecutor",
    "AppActionRecoveryBlockedError",
    "InProcessSessionCommandGate",
    "SessionCommandCanceledError",
    "SessionCommandGate",
]


class SessionCommandCanceledError(RuntimeError):
    """Session 已被 cancel，旧 Worker / 旧 Permit 不得再持有命令租约。"""


class InProcessSessionCommandGate:
    """在单进程 M1 运行时内保证一个 Session 同时只有一个动作。"""

    def __init__(self) -> None:
        self._registry_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._task_owners: dict[str, str] = {}
        self._users: dict[str, int] = {}
        self._command_sequences: dict[str, int] = {}
        self._canceled: dict[str, str] = {}

    @asynccontextmanager
    async def hold(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind | None = None,
        allow_draining: bool = False,
    ) -> AsyncIterator[SessionCommandPermit]:
        del allow_draining
        actual_surface_kind = surface_kind or SurfaceKind.WEB
        async with self._registry_lock:
            if session_id in self._canceled:
                raise SessionCommandCanceledError(
                    f"Surface Session 已取消：{self._canceled[session_id]}"
                )
            owner = self._task_owners.get(session_id)
            if owner is not None and owner != task_id:
                raise ValueError("Surface Session 已绑定其他任务，禁止跨任务复用")
            self._task_owners[session_id] = task_id
            lock = self._locks.setdefault(session_id, asyncio.Lock())
            self._users[session_id] = self._users.get(session_id, 0) + 1
        try:
            async with lock:
                async with self._registry_lock:
                    if session_id in self._canceled:
                        raise SessionCommandCanceledError(
                            f"Surface Session 已取消：{self._canceled[session_id]}"
                        )
                    sequence = self._command_sequences.get(session_id, 0) + 1
                    self._command_sequences[session_id] = sequence
                yield SessionCommandPermit(
                    task_id=task_id,
                    session_id=session_id,
                    surface_kind=actual_surface_kind,
                    lease_owner="in-process",
                    lease_token=str(uuid4()),
                    command_sequence=sequence,
                    finalization_epoch=0,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
        finally:
            async with self._registry_lock:
                active_users = self._users.get(session_id)
                if active_users is not None:
                    remaining = active_users - 1
                    if remaining <= 0:
                        self._users.pop(session_id, None)
                    else:
                        self._users[session_id] = remaining

    async def release_session(self, *, task_id: str, session_id: str) -> None:
        """会话关闭后释放任务绑定；仍有命令执行时拒绝提前释放。"""
        async with self._registry_lock:
            if session_id in self._canceled:
                self._canceled.pop(session_id, None)
                self._task_owners.pop(session_id, None)
                self._locks.pop(session_id, None)
                self._command_sequences.pop(session_id, None)
                self._users.pop(session_id, None)
                return
            owner = self._task_owners.get(session_id)
            if owner is None:
                return
            if owner != task_id:
                raise ValueError("Surface Session 不属于当前任务")
            if self._users.get(session_id, 0) > 0:
                raise RuntimeError("Surface Session 仍有动作执行，不能释放")
            self._task_owners.pop(session_id, None)
            self._locks.pop(session_id, None)
            self._command_sequences.pop(session_id, None)

    async def cancel_session(
        self,
        *,
        task_id: str,
        session_id: str,
        reason: str = "canceled",
    ) -> None:
        """排期第 13 列 cancel：强制作废本进程 Session 绑定，旧 Permit 不得再 acquire。"""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须为非空字符串")
        async with self._registry_lock:
            owner = self._task_owners.get(session_id)
            if owner is not None and owner != task_id:
                raise ValueError("Surface Session 不属于当前任务，禁止取消")
            self._canceled[session_id] = reason.strip()
            self._task_owners.pop(session_id, None)
            self._users.pop(session_id, None)
            self._command_sequences.pop(session_id, None)
            # 锁对象保留，让进行中的 hold 结束后再自然释放；新 hold 会因 _canceled 失败。


class CaptureCoordinator:
    """固定前采集、单动作执行、后采集顺序，并校验审计关联。"""

    def __init__(
        self,
        *,
        capture_writer: CaptureWriter,
        interaction_executor: InteractionExecutor,
        evidence_exit_gate: EvidenceExitGate,
        session_command_gate: SessionCommandGate | None = None,
        action_execution_repository: ActionExecutionRepository | None = None,
        action_recovery_captures: AppActionRecoveryCaptureReader | None = None,
        action_evidence_writer: AppActionEvidenceWriter | None = None,
        supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
        command_policy: CommandPolicy | None = None,
        command_validator: InteractionCommandValidator | None = None,
        command_scope_reader: TrustedTaskCommandScopeReader | None = None,
        flow_foundation_p1: object | None = None,
    ) -> None:
        if command_validator is not None and (
            command_policy is not None or command_scope_reader is not None
        ):
            raise ValueError(
                "command_validator 不能与 command_policy 或 command_scope_reader 同时提供"
            )
        if command_policy is not None and command_scope_reader is not None:
            raise ValueError("command_policy 与 command_scope_reader 只能提供一个")
        self._capture_writer = capture_writer
        self._interaction_executor = interaction_executor
        self._evidence_exit_gate = evidence_exit_gate
        self._session_command_gate = session_command_gate or InProcessSessionCommandGate()
        self._action_executions = action_execution_repository
        self._action_recovery_captures = action_recovery_captures
        self._action_evidence_writer = action_evidence_writer
        self._supplemental_evidence_policy = (
            supplemental_evidence_policy
            if supplemental_evidence_policy is not None
            else SupplementalEvidencePolicy()
        )
        self._command_validator = command_validator or InteractionCommandValidator(
            policy=command_policy or CommandPolicy.default(),
            scope_reader=command_scope_reader,
        )
        # 可选注入；App 组合根接线 flow_foundation_p1 以启用 P1 入口守卫。
        self._flow_foundation_p1 = flow_foundation_p1

    async def find_recovery_intent(
        self,
        *,
        task_id: str,
        session_id: str,
        exploration_work_id: str,
    ) -> ActionIntent | None:
        """读取已经绑定 Work 的 App 动作意图；未配置账本时表示无恢复事实。"""

        if self._action_executions is None:
            return None
        finder = getattr(self._action_executions, "find_intent_for_work", None)
        if not callable(finder):
            return None
        return await finder(
            task_id=task_id,
            session_id=session_id,
            exploration_work_id=exploration_work_id,
        )

    async def coordinate(
        self,
        command: CaptureCoordinationCommand,
    ) -> CaptureCoordinationResult:
        if command.intent.action_type is InteractionOperation.TIMED_CAPTURE:
            raise ValueError("TIMED_CAPTURE 必须使用 TimedObservationIntent")
        if has_supplemental_evidence_lineage(command.intent.parameters):
            self._supplemental_evidence_policy.require_enabled(
                SupplementalEvidenceOperation.CAPTURE_SUPPLEMENT
            )
        try:
            await self._command_validator.validate_for(command)
        except InteractionCommandValidationError as exc:
            # 命令策略发生在 Session Gate、Evidence Gate 和 Capture 之前；
            # 该动作没有任何设备副作用，也不应消耗离场证据预留。离线
            # Coordinator 也必须遵循同一顺序，不能由测试替生产放宽目标校验。
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.BEFORE_CAPTURE,
                error_code=exc.error_code,
                message=str(exc),
                cause_type=type(exc).__name__,
                retryable=exc.retryable,
            ) from exc
        self._apply_flow_foundation_p1_gates(command)
        async with self._session_command_gate.hold(
            task_id=command.intent.task_id,
            session_id=command.intent.session_id,
            surface_kind=command.intent.surface_kind,
            allow_draining=command.allow_draining,
        ) as permit:
            decision: EvidenceExitGateDecision | None = None
            progress = _CoordinationProgress()
            try:
                await self._validate_command_for_permit(
                    command,
                    permit=permit,
                )
                recovered_action = await self._load_existing_recoverable_action(command.intent)
                if recovered_action is not None:
                    await self._finalize_stale_action_reservations(
                        intent=command.intent,
                        permit=permit,
                        action_state=recovered_action,
                    )
                recovered_before_capture = (
                    await self._load_optional_planned_before_capture(
                        command=command,
                        action_state=recovered_action,
                    )
                    if recovered_action is not None
                    and recovered_action.status is ActionExecutionStatus.PLANNED
                    else None
                )
                if recovered_action is None:
                    decision = await self._evidence_exit_gate.reserve(
                        intent=command.intent,
                        permit=permit,
                    )
                elif (
                    recovered_action.status is ActionExecutionStatus.PLANNED
                    or _can_retry_failed_before_capture(recovered_action)
                ):
                    reserve_recovery = getattr(
                        self._evidence_exit_gate,
                        "reserve_action_recovery",
                        None,
                    )
                    if callable(reserve_recovery):
                        decision = await reserve_recovery(
                            intent=command.intent,
                            permit=permit,
                            action_execution_id=recovered_action.action_execution_id,
                            before_capture_id=(
                                recovered_before_capture.capture_id
                                if recovered_before_capture is not None
                                else None
                            ),
                        )
                    else:
                        decision = await self._evidence_exit_gate.reserve(
                            intent=command.intent,
                            permit=permit,
                        )
                result = await self._coordinate_locked(
                    command,
                    permit=permit,
                    progress=progress,
                    action_state=recovered_action,
                    recovered_before_capture=recovered_before_capture,
                )
            except BaseException as coordination_error:
                if decision is not None:
                    try:
                        await self._evidence_exit_gate.finalize(
                            decision=decision,
                            status=(
                                EvidenceExitReservationStatus.CONSUMED
                                if progress.action_started
                                else EvidenceExitReservationStatus.CANCELLED
                            ),
                        )
                    except BaseException as finalization_error:
                        # Gate 持久化失败本身意味着预留事实未知，必须传播；原协调异常
                        # 作为 cause 保留，供调用方同时定位动作与持久化链路。
                        raise finalization_error from coordination_error
                raise
            if decision is not None:
                await self._evidence_exit_gate.finalize(
                    decision=decision,
                    status=(
                        EvidenceExitReservationStatus.CONSUMED
                        if progress.action_started
                        else EvidenceExitReservationStatus.CANCELLED
                    ),
                )
                result = replace(result, exit_gate_decision=decision)
            return result

    async def _finalize_stale_action_reservations(
        self,
        *,
        intent: ActionIntent,
        permit: SessionCommandPermit,
        action_state: ActionExecutionSnapshot,
    ) -> None:
        """恢复前先清理旧 Gate 预留，避免其阻断证据封存和 Session 关闭。"""

        finalizer = getattr(
            self._evidence_exit_gate,
            "finalize_stale_action_reservations",
            None,
        )
        if not callable(finalizer):
            return
        action_started = (
            action_state.source_capture_id is not None and action_state.started_at is not None
        )
        await finalizer(
            intent=intent,
            permit=permit,
            action_execution_id=action_state.action_execution_id,
            status=(
                EvidenceExitReservationStatus.CONSUMED
                if action_started
                else EvidenceExitReservationStatus.CANCELLED
            ),
        )

    def _apply_flow_foundation_p1_gates(
        self,
        command: CaptureCoordinationCommand,
    ) -> None:
        """App P1：唯一动作入口；观察类不得携带伪造 action_execution_id。"""

        gateway = self._flow_foundation_p1
        if gateway is None:
            return

        def _raise_as_coordination(exc: BaseException) -> None:
            error_code = str(getattr(exc, "error_code", "") or "FLOW_FOUNDATION_P1_REJECTED")
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.BEFORE_CAPTURE,
                error_code=error_code,
                message=str(exc),
                cause_type=type(exc).__name__,
                retryable=bool(getattr(exc, "retryable", False)),
            ) from exc

        gate_android = getattr(gateway, "gate_android_coordination", None)
        if callable(gate_android):
            try:
                gate_android(surface_kind=command.intent.surface_kind)
            except Exception as exc:  # noqa: BLE001 - 统一成协调错误
                _raise_as_coordination(exc)
        if not _is_app_surface(command.intent.surface_kind):
            return
        gate_operation = getattr(gateway, "gate_interaction_operation", None)
        if callable(gate_operation):
            forged_action_id = command.intent.parameters.get("action_execution_id")
            presented_action_id = (
                str(forged_action_id).strip() or None
                if forged_action_id is not None
                else None
            )
            try:
                gate_operation(
                    operation=command.intent.action_type,
                    action_execution_id=presented_action_id,
                )
            except Exception as exc:  # noqa: BLE001
                _raise_as_coordination(exc)
        # 可选 fence 门禁：参数齐全才校验；缺失则跳过以免破坏既有单测。
        require_permit = getattr(gateway, "require_permit", None)
        if not callable(require_permit):
            return
        permit = _try_build_device_execution_permit(command.intent.parameters)
        if permit is None:
            return
        presented = command.intent.parameters.get("fencing_token")
        if presented is None:
            presented = permit.fencing_token
        try:
            require_permit(permit, presented_fencing_token=presented)
        except Exception as exc:  # noqa: BLE001
            _raise_as_coordination(exc)

    async def _validate_command_for_permit(
        self,
        command: CaptureCoordinationCommand,
        *,
        permit: SessionCommandPermit,
        before_capture: CaptureRef | None = None,
    ) -> None:
        """在租约内重新读取命令范围，阻止关闭或换 Epoch 后继续执行。"""

        try:
            await self._command_validator.validate_for(
                command,
                expected_finalization_epoch=permit.finalization_epoch,
            )
        except InteractionCommandValidationError as exc:
            raise CaptureCoordinationError(
                stage=(
                    CaptureCoordinationStage.ACTION
                    if before_capture is not None
                    else CaptureCoordinationStage.BEFORE_CAPTURE
                ),
                error_code=exc.error_code,
                message=str(exc),
                cause_type=type(exc).__name__,
                before_capture=before_capture,
                retryable=exc.retryable,
            ) from exc

    async def _capture_interaction_refresh(
        self,
        *,
        command: CaptureCoordinationCommand,
        permit: SessionCommandPermit,
        intent: ActionIntent,
        after_capture: CaptureRef,
        action_execution_id: str,
    ) -> str | None:
        """关注／收藏／点赞成功后再截一张刷新画面，挂在操作后 Capture 下。"""

        try:
            refreshed = await self._capture_writer.capture(
                CaptureWriteRequest(
                    task_id=intent.task_id,
                    agent_run_id=command.agent_run_id,
                    session_id=intent.session_id,
                    surface_kind=intent.surface_kind,
                    intent_id=f"interaction-feedback:{action_execution_id}",
                    phase=CapturePhase.OBSERVATION,
                    directive=CaptureDirective(
                        role="observation",
                        artifact_types=intent.capture_profile.after.artifact_types,
                    ),
                    parent_capture_id=after_capture.capture_id,
                    action_execution_id=None,
                    session_command_sequence=permit.command_sequence,
                    finalization_epoch=permit.finalization_epoch,
                    deadline=intent.deadline,
                    lease_owner=permit.lease_owner,
                    lease_token=permit.lease_token,
                    observation_context={
                        "observation_id": f"interaction-feedback:{action_execution_id}",
                        "semantic_role": "surface_reobserve",
                    },
                )
            )
            _validate_capture_ref(
                refreshed,
                expected_task_id=intent.task_id,
                expected_session_id=intent.session_id,
                expected_surface_kind=intent.surface_kind,
                expected_role="observation",
                expected_parent_id=after_capture.capture_id,
                expected_action_execution_id=None,
            )
        except Exception:  # noqa: BLE001 - 缺刷新帧只记缺口，不把已成功的互动判失败
            _LOGGER.warning(
                "interaction refresh capture missing action=%s",
                action_execution_id,
                exc_info=True,
            )
            return None
        if refreshed.capture_id == after_capture.capture_id:
            return None
        permit.record_current_capture(refreshed.capture_id)
        return refreshed.capture_id

    async def capture_observation(
        self,
        *,
        agent_run_id: str,
        intent: TimedObservationIntent,
        allow_draining: bool = False,
        semantic_role: str = "surface_reobserve",
    ) -> CaptureRef:
        """在真实 Session Gate 内串行完成一次无动作观察截图。"""
        if semantic_role not in {"surface_reobserve", "final_observation"}:
            raise ValueError("Observation semantic_role 不受支持")
        async with self._session_command_gate.hold(
            task_id=intent.task_id,
            session_id=intent.session_id,
            surface_kind=intent.surface_kind,
            allow_draining=allow_draining,
        ) as permit:
            reference = await self._capture_writer.capture(
                CaptureWriteRequest(
                    task_id=intent.task_id,
                    agent_run_id=agent_run_id,
                    session_id=intent.session_id,
                    surface_kind=intent.surface_kind,
                    intent_id=intent.observation_id,
                    phase=CapturePhase.OBSERVATION,
                    directive=intent.directive,
                    parent_capture_id=intent.source_capture_id,
                    action_execution_id=None,
                    session_command_sequence=permit.command_sequence,
                    finalization_epoch=permit.finalization_epoch,
                    deadline=intent.deadline,
                    lease_owner=permit.lease_owner,
                    lease_token=permit.lease_token,
                    observation_context={
                        "observation_id": intent.observation_id,
                        "surface_phase_run_id": intent.phase_run_id,
                        "surface_phase_decision_id": intent.decision_id,
                        "semantic_role": semantic_role,
                        **(
                            {"phase_key": intent.phase_key}
                            if isinstance(intent.phase_key, str) and intent.phase_key.strip()
                            else {}
                        ),
                    },
                )
            )
            _validate_capture_ref(
                reference,
                expected_task_id=intent.task_id,
                expected_session_id=intent.session_id,
                expected_surface_kind=intent.surface_kind,
                expected_role="observation",
                expected_parent_id=intent.source_capture_id,
                expected_action_execution_id=None,
            )
            permit.record_current_capture(reference.capture_id)
            return reference

    async def _coordinate_locked(
        self,
        command: CaptureCoordinationCommand,
        *,
        permit: SessionCommandPermit,
        progress: _CoordinationProgress,
        action_state: ActionExecutionSnapshot | None = None,
        recovered_before_capture: CaptureRef | None = None,
    ) -> CaptureCoordinationResult:
        try:
            return await self._coordinate_locked_body(
                command, permit=permit, progress=progress, action_state=action_state,
                recovered_before_capture=recovered_before_capture,
            )
        except (Exception, asyncio.CancelledError):
            intent = command.intent
            if intent.surface_kind is SurfaceKind.WEB and intent.session_id.startswith("h5:"):
                async def settle() -> None:
                    try:
                        # 包括计划已提交但 await 尚未返回的窗口；只读取本动作稳定身份。
                        async with asyncio.timeout(5):
                            persisted = await self._load_existing_recoverable_action(intent)
                            await self._settle_h5_canceled_action(
                                intent=intent, permit=permit, action_state=persisted,
                                interaction=(InteractionResult.from_dict(persisted.result_payload)
                                             if persisted and persisted.result_payload else None),
                            )
                    except (Exception, asyncio.CancelledError) as exc:
                        _LOGGER.error("H5 中断动作账本收尾失败 intent=%s cause=%s",
                                      intent.intent_id, type(exc).__name__)

                # Probe 的再次取消不能打断补偿；必须等有界收尾退出后才释放命令许可。
                cleanup = asyncio.create_task(settle())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                await cleanup
            raise

    async def _coordinate_locked_body(
        self,
        command: CaptureCoordinationCommand,
        *,
        permit: SessionCommandPermit,
        progress: _CoordinationProgress,
        action_state: ActionExecutionSnapshot | None = None,
        recovered_before_capture: CaptureRef | None = None,
    ) -> CaptureCoordinationResult:
        """在 Session 原子边界内完成前采集、动作与后采集。"""
        intent = command.intent
        before_directive = intent.capture_profile.before
        after_directive = intent.capture_profile.after
        durable_recovery = _supports_durable_action_recovery(intent)
        if self._action_executions is not None:
            if action_state is None:
                action_state = await self._action_executions.plan(
                    intent=intent,
                    permit=permit,
                )
            if durable_recovery:
                _validate_action_state(action_state, intent=intent)
            if durable_recovery and action_state.status is ActionExecutionStatus.PLANNED:
                if recovered_before_capture is not None:
                    action_state = await self._action_executions.resume_planned_with_capture(
                        action_execution_id=action_state.action_execution_id,
                        permit=permit,
                        source_capture_id=recovered_before_capture.capture_id,
                        expected_row_version=action_state.row_version,
                    )
                    _validate_action_state(action_state, intent=intent)
                elif (
                    action_state.command_sequence != permit.command_sequence
                    or action_state.finalization_epoch != permit.finalization_epoch
                ):
                    action_state = await self._action_executions.recover(
                        action_execution_id=action_state.action_execution_id,
                        permit=permit,
                        expected_row_version=action_state.row_version,
                    )
                    _validate_action_state(action_state, intent=intent)
            elif durable_recovery:
                action_state = await self._action_executions.recover(
                    action_execution_id=action_state.action_execution_id,
                    permit=permit,
                    expected_row_version=action_state.row_version,
                )
                _validate_action_state(action_state, intent=intent)
            if durable_recovery and recovered_before_capture is None:
                replay = await self._replay_recovered_action(
                    command=command,
                    action_state=action_state,
                )
                if replay is not None:
                    progress.action_started = True
                    return replay
        planned_action_execution_id = (
            action_state.action_execution_id if action_state is not None else None
        )
        before_capture: CaptureRef | None = recovered_before_capture
        # 原丢失会话不能借空白页复活；独立恢复会话先记录自身初始帧，再受控导航。
        initial_surface_action = is_trusted_initial_surface_action(intent)
        before_parent_capture_id = (
            None
            if (initial_surface_action or is_trusted_h5_browser_recovery_action(intent))
            else intent.source_capture_id
        )
        try:
            if before_capture is None:
                before_capture = await self._capture_writer.capture(
                    CaptureWriteRequest(
                        task_id=intent.task_id,
                        agent_run_id=command.agent_run_id,
                        session_id=intent.session_id,
                        surface_kind=intent.surface_kind,
                        intent_id=intent.intent_id,
                        phase=CapturePhase.BEFORE_ACTION,
                        directive=before_directive,
                        parent_capture_id=before_parent_capture_id,
                        action_execution_id=planned_action_execution_id,
                        session_command_sequence=permit.command_sequence,
                        finalization_epoch=permit.finalization_epoch,
                        deadline=intent.deadline,
                        lease_owner=permit.lease_owner,
                        lease_token=permit.lease_token,
                        bootstrap_blank_surface=initial_surface_action,
                        recovery_bootstrap_intent=(
                            intent if is_trusted_h5_browser_recovery_action(intent) else None
                        ),
                        web_capture_stage=(
                            _optional_web_capture_stage(intent.parameters.get("web_capture_stage"))
                            if intent.surface_kind is SurfaceKind.WEB
                            else None
                        ),
                        viewport_width=_optional_viewport_dimension(
                            intent.parameters.get("viewport_width")
                        ),
                        viewport_height=_optional_viewport_dimension(
                            intent.parameters.get("viewport_height")
                        ),
                        screen_width=_optional_viewport_dimension(
                            intent.parameters.get("screen_width")
                        ),
                        screen_height=_optional_viewport_dimension(
                            intent.parameters.get("screen_height")
                        ),
                        device_scale_factor=_optional_device_scale_factor(
                            intent.parameters.get("device_scale_factor")
                        ),
                        is_mobile=_optional_bool(intent.parameters.get("is_mobile")),
                        has_touch=_optional_bool(intent.parameters.get("has_touch")),
                        color_scheme=_optional_color_scheme(intent.parameters.get("color_scheme")),
                    )
                )
                permit.record_current_capture(before_capture.capture_id)
            _validate_capture_ref(
                before_capture,
                expected_task_id=intent.task_id,
                expected_session_id=intent.session_id,
                expected_surface_kind=intent.surface_kind,
                expected_role=before_directive.role,
                expected_parent_id=before_parent_capture_id,
                expected_action_execution_id=planned_action_execution_id,
            )
        except asyncio.CancelledError:
            await self._settle_h5_canceled_action(
                intent=intent,
                permit=permit,
                action_state=action_state,
            )
            raise
        except Exception as exc:
            # 与 after-capture 同理：固定文案会吞掉截图超时、前台漂移等真实成因
            _LOGGER.warning(
                "action before-capture failed task=%s session=%s error=%s: %s",
                intent.task_id,
                intent.session_id,
                type(exc).__name__,
                str(exc)[:800],
            )
            if action_state is not None and self._action_executions is not None:
                try:
                    await self._action_executions.fail_before_action(
                        action_execution_id=action_state.action_execution_id,
                        permit=permit,
                        expected_row_version=action_state.row_version,
                        error_code="before_capture_failed",
                        error_message="动作前 Capture 未完成",
                    )
                except Exception:  # noqa: BLE001 - 不得用补偿失败遮蔽原始采集错误
                    pass
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.BEFORE_CAPTURE,
                error_code="before_capture_failed",
                message="动作前 Capture 未完成",
                cause_type=type(exc).__name__,
                before_capture=(before_capture if isinstance(before_capture, CaptureRef) else None),
            ) from exc

        interaction: InteractionResult | None = None
        await self._validate_command_for_permit(
            command,
            permit=permit,
            before_capture=before_capture,
        )
        try:
            if (
                action_state is not None
                and action_state.status is ActionExecutionStatus.PLANNED
                and self._action_executions is not None
            ):
                action_state = await self._action_executions.mark_executing(
                    action_execution_id=action_state.action_execution_id,
                    permit=permit,
                    source_capture_id=before_capture.capture_id,
                    expected_row_version=action_state.row_version,
                )
            # 从这一行起平台动作可能已改变 Surface；即使后续失败，也必须消费
            # 离场预留，并交由 Restoration/证据状态机处理未知结果。
            progress.action_started = True
            execution_intent = _with_web_primary_before_action_capture(
                intent,
                before_capture_id=before_capture.capture_id,
            )
            interaction = await self._interaction_executor.execute(
                ExecuteInteractionRequest(
                    agent_run_id=command.agent_run_id,
                    intent=execution_intent,
                    source_capture_id=before_capture.capture_id,
                    action_execution_id=planned_action_execution_id,
                    finalization_epoch=permit.finalization_epoch,
                    command_sequence=permit.command_sequence,
                    lease_owner=permit.lease_owner,
                    lease_token=permit.lease_token,
                )
            )
            _validate_interaction(
                interaction,
                command,
                before_capture,
                expected_action_execution_id=planned_action_execution_id,
            )
            if interaction.status not in {
                InteractionResultStatus.EVIDENCE_PENDING,
                InteractionResultStatus.FAILED,
                InteractionResultStatus.BLOCKED,
                InteractionResultStatus.TIMED_OUT,
            }:
                raise RuntimeError("Interaction Executor 返回了不允许的阶段状态")
            if action_state is not None and self._action_executions is not None:
                action_state = await self._action_executions.record_interaction(
                    interaction=interaction,
                    permit=permit,
                    expected_status=ActionExecutionStatus.EXECUTING,
                    expected_row_version=action_state.row_version,
                )
        except asyncio.CancelledError:
            await self._settle_h5_canceled_action(
                intent=intent,
                permit=permit,
                action_state=action_state,
                before_capture=before_capture,
            )
            if (
                progress.action_started
                and action_state is not None
                and action_state.status is ActionExecutionStatus.EXECUTING
                and self._action_executions is not None
                and _is_app_surface(intent.surface_kind)
            ):
                # 取消不代表设备没有执行。只收尾已有动作，不发起重放或新的后态采集。
                canceled_interaction = InteractionResult(
                    task_id=intent.task_id,
                    session_id=intent.session_id,
                    intent_id=intent.intent_id,
                    action_execution_id=action_state.action_execution_id,
                    source_capture_id=before_capture.capture_id,
                    status=InteractionResultStatus.FAILED,
                    error_code="APP_ACTION_OUTCOME_UNKNOWN_DURING_CANCELLATION",
                    error_message="操作已取消，设备侧结果未知，未继续采集后态",
                    state_summary={
                        "cause_type": "CancelledError",
                        "after_capture_status": "not_collected",
                        "after_capture_reason": "workflow_canceled",
                    },
                )
                try:
                    # 首次取消已经送达；在当前协程内有限等待，禁止留下后台补偿任务。
                    async with asyncio.timeout(5):
                        await self._action_executions.record_interaction(
                            interaction=canceled_interaction,
                            permit=permit,
                            expected_status=ActionExecutionStatus.EXECUTING,
                            expected_row_version=action_state.row_version,
                        )
                except (Exception, asyncio.CancelledError) as cleanup_error:
                    # 租约可能已撤销，正式任务取消入口还会用无租约终态清理兜底。
                    _LOGGER.error(
                        "App 取消动作账本收尾失败 action_execution_id=%s cause=%s；"
                        "需要任务取消清理继续收尾",
                        action_state.action_execution_id,
                        type(cleanup_error).__name__,
                    )
            raise
        except Exception as exc:
            if (
                interaction is None
                and action_state is not None
                and action_state.status is ActionExecutionStatus.EXECUTING
                and self._action_executions is not None
                and _is_app_surface(intent.surface_kind)
            ):
                # Driver 抛异常时动作可能已经送达设备。将“不确定”事实写成失败终态，
                # 避免 Action ledger 永久停在 executing，同时禁止恢复时重复执行。
                interaction = InteractionResult(
                    task_id=intent.task_id,
                    session_id=intent.session_id,
                    intent_id=intent.intent_id,
                    action_execution_id=action_state.action_execution_id,
                    source_capture_id=before_capture.capture_id,
                    status=InteractionResultStatus.FAILED,
                    error_code="APP_ACTION_OUTCOME_UNKNOWN_DURING_EXECUTION",
                    error_message="动作执行通道异常，设备侧结果未知",
                    state_summary={
                        "cause_type": type(exc).__name__,
                        "cause_message": str(exc)[:400],
                        "cause_error_code": getattr(exc, "error_code", None),
                    },
                )
                try:
                    action_state = await self._action_executions.record_interaction(
                        interaction=interaction,
                        permit=permit,
                        expected_status=ActionExecutionStatus.EXECUTING,
                        expected_row_version=action_state.row_version,
                    )
                except Exception:  # noqa: BLE001 - 不得用补偿失败遮蔽原始执行错误
                    pass
            if (
                _is_app_surface(intent.surface_kind)
                and isinstance(interaction, InteractionResult)
                and interaction.status is InteractionResultStatus.FAILED
                and interaction.error_code == "APP_ACTION_OUTCOME_UNKNOWN_DURING_EXECUTION"
                and before_capture is not None
            ):
                # 动作可能已送达设备；补采 after 并返回，让 App 探索链可继续换策略。
                try:
                    after_capture = await _capture_after_action_with_retry(
                        self._capture_writer,
                        CaptureWriteRequest(
                            task_id=intent.task_id,
                            agent_run_id=command.agent_run_id,
                            session_id=intent.session_id,
                            surface_kind=intent.surface_kind,
                            intent_id=intent.intent_id,
                            phase=CapturePhase.AFTER_ACTION,
                            directive=after_directive,
                            parent_capture_id=before_capture.capture_id,
                            action_execution_id=interaction.action_execution_id,
                            session_command_sequence=permit.command_sequence,
                            finalization_epoch=permit.finalization_epoch,
                            deadline=intent.deadline,
                            lease_owner=permit.lease_owner,
                            lease_token=permit.lease_token,
                            viewport_width=_optional_viewport_dimension(
                                intent.parameters.get("viewport_width")
                            ),
                            viewport_height=_optional_viewport_dimension(
                                intent.parameters.get("viewport_height")
                            ),
                            screen_width=_optional_viewport_dimension(
                                intent.parameters.get("screen_width")
                            ),
                            screen_height=_optional_viewport_dimension(
                                intent.parameters.get("screen_height")
                            ),
                            device_scale_factor=_optional_device_scale_factor(
                                intent.parameters.get("device_scale_factor")
                            ),
                            is_mobile=_optional_bool(intent.parameters.get("is_mobile")),
                            has_touch=_optional_bool(intent.parameters.get("has_touch")),
                            color_scheme=_optional_color_scheme(
                                intent.parameters.get("color_scheme")
                            ),
                        ),
                    )
                    interaction = interaction.attach_capture_preserving_execution(
                        after_capture.capture_id
                    )
                    if action_state is not None and self._action_executions is not None:
                        action_state = await self._action_executions.attach_terminal_evidence(
                            interaction=interaction,
                            permit=permit,
                            expected_row_version=action_state.row_version,
                        )
                    permit.record_current_capture(after_capture.capture_id)
                    await attach_app_action_log_safely(
                        self._action_evidence_writer,
                        intent=intent,
                        interaction=interaction,
                        before_capture_id=before_capture.capture_id,
                        after_capture_id=after_capture.capture_id,
                    )
                    return CaptureCoordinationResult(
                        before_capture=before_capture,
                        interaction=interaction,
                        after_capture=after_capture,
                    )
                except Exception:  # noqa: BLE001 - 补采失败仍走原有 fail-closed
                    pass
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.ACTION,
                error_code=(
                    interaction.error_code
                    if isinstance(interaction, InteractionResult)
                    and interaction.error_code is not None
                    else "interaction_failed"
                ),
                message="动作执行未完成",
                cause_type=type(exc).__name__,
                before_capture=before_capture,
                interaction=(interaction if isinstance(interaction, InteractionResult) else None),
            ) from exc

        if (
            interaction.status is not InteractionResultStatus.EVIDENCE_PENDING
            and not _is_app_surface(intent.surface_kind)
            and not _web_terminal_interaction_has_pending_evidence(interaction)
        ):
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.ACTION,
                error_code=interaction.error_code or "interaction_failed",
                message="动作执行未完成",
                before_capture=before_capture,
                interaction=interaction,
            )

        after_capture: CaptureRef | None = None
        try:
            after_capture = await _capture_after_action_with_retry(
                self._capture_writer,
                CaptureWriteRequest(
                    task_id=intent.task_id,
                    agent_run_id=command.agent_run_id,
                    session_id=intent.session_id,
                    surface_kind=intent.surface_kind,
                    intent_id=intent.intent_id,
                    phase=CapturePhase.AFTER_ACTION,
                    directive=after_directive,
                    parent_capture_id=before_capture.capture_id,
                    action_execution_id=interaction.action_execution_id,
                    session_command_sequence=permit.command_sequence,
                    finalization_epoch=permit.finalization_epoch,
                    deadline=intent.deadline,
                    lease_owner=permit.lease_owner,
                    lease_token=permit.lease_token,
                    web_capture_stage=(
                        _optional_web_capture_stage(intent.parameters.get("web_capture_stage"))
                        if intent.surface_kind is SurfaceKind.WEB
                        else None
                    ),
                    viewport_width=_optional_viewport_dimension(
                        intent.parameters.get("viewport_width")
                    ),
                    viewport_height=_optional_viewport_dimension(
                        intent.parameters.get("viewport_height")
                    ),
                    screen_width=_optional_viewport_dimension(
                        intent.parameters.get("screen_width")
                    ),
                    screen_height=_optional_viewport_dimension(
                        intent.parameters.get("screen_height")
                    ),
                    device_scale_factor=_optional_device_scale_factor(
                        intent.parameters.get("device_scale_factor")
                    ),
                    is_mobile=_optional_bool(intent.parameters.get("is_mobile")),
                    has_touch=_optional_bool(intent.parameters.get("has_touch")),
                    color_scheme=_optional_color_scheme(intent.parameters.get("color_scheme")),
                ),
            )
            _validate_capture_ref(
                after_capture,
                expected_task_id=intent.task_id,
                expected_session_id=intent.session_id,
                expected_surface_kind=intent.surface_kind,
                expected_role=after_directive.role,
                expected_parent_id=before_capture.capture_id,
                expected_action_execution_id=interaction.action_execution_id,
            )
            if interaction.status is InteractionResultStatus.EVIDENCE_PENDING:
                interaction = interaction.complete_with_capture(after_capture.capture_id)
                if action_state is not None and self._action_executions is not None:
                    action_state = await self._action_executions.complete_evidence(
                        interaction=interaction,
                        permit=permit,
                        expected_row_version=action_state.row_version,
                    )
            else:
                interaction = interaction.attach_capture_preserving_execution(
                    after_capture.capture_id
                )
                if action_state is not None and self._action_executions is not None:
                    action_state = await self._action_executions.attach_terminal_evidence(
                        interaction=interaction,
                        permit=permit,
                        expected_row_version=action_state.row_version,
                    )
            permit.record_current_capture(after_capture.capture_id)
        except asyncio.CancelledError:
            await self._settle_h5_canceled_action(
                intent=intent,
                permit=permit,
                action_state=action_state,
                before_capture=before_capture,
                interaction=interaction,
            )
            raise
        except Exception as exc:
            after_failure_code = (
                "APP_ACTION_AFTER_CAPTURE_MISSING"
                if _is_app_surface(intent.surface_kind)
                else "after_capture_failed"
            )
            after_retryable = _is_transient_after_capture_error(exc)
            # 失败原因（如动作把前台切走、截图超时）此前只留固定文案，无从定位
            _LOGGER.warning(
                "action after-capture failed code=%s retryable=%s action=%s error=%s: %s",
                after_failure_code,
                after_retryable,
                interaction.action_execution_id,
                type(exc).__name__,
                str(exc)[:800],
            )
            if interaction.status is InteractionResultStatus.EVIDENCE_PENDING:
                interaction = interaction.mark_evidence_failed(
                    error_code=after_failure_code,
                    error_message="动作后 Capture 未完成",
                    retryable=after_retryable,
                )
                if (
                    action_state is not None
                    and action_state.status is ActionExecutionStatus.EVIDENCE_PENDING
                    and self._action_executions is not None
                ):
                    try:
                        await self._action_executions.fail_evidence(
                            interaction=interaction,
                            permit=permit,
                            expected_row_version=action_state.row_version,
                        )
                    except Exception:  # noqa: BLE001 - 保留 evidence_pending 供 Sweeper 恢复
                        pass
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.AFTER_CAPTURE,
                error_code=after_failure_code,
                message="动作后 Capture 未完成",
                cause_type=type(exc).__name__,
                before_capture=before_capture,
                interaction=interaction,
                after_capture=(after_capture if isinstance(after_capture, CaptureRef) else None),
                retryable=after_retryable,
            ) from exc

        refreshed_capture_id = None
        from services.api.app.application.app_capture_role_supplement import (
            is_interaction_feedback_action,
        )

        if _is_app_surface(intent.surface_kind) and is_interaction_feedback_action(
            action=intent.action_type.value,
            semantic_target=intent.semantic_target,
        ):
            refreshed_capture_id = await self._capture_interaction_refresh(
                command=command,
                permit=permit,
                intent=intent,
                after_capture=after_capture,
                action_execution_id=interaction.action_execution_id,
            )
        await attach_app_action_log_safely(
            self._action_evidence_writer,
            intent=intent,
            interaction=interaction,
            before_capture_id=before_capture.capture_id,
            after_capture_id=after_capture.capture_id,
            refreshed_capture_id=refreshed_capture_id,
        )
        return CaptureCoordinationResult(
            before_capture=before_capture,
            interaction=interaction,
            after_capture=after_capture,
        )

    async def _settle_h5_canceled_action(
        self,
        *,
        intent: ActionIntent,
        permit: SessionCommandPermit,
        action_state: ActionExecutionSnapshot | None,
        before_capture: CaptureRef | None = None,
        interaction: InteractionResult | None = None,
    ) -> None:
        """H5 取消只结算已有账本，不重放动作或伪造后态证据。"""

        if (
            intent.surface_kind is not SurfaceKind.WEB
            or not intent.session_id.startswith("h5:")
            or action_state is None
            or self._action_executions is None
        ):
            return
        try:
            # 在命令许可释放前有界收尾；围栏失效时保留原始取消，不越权写账本。
            async with asyncio.timeout(5):
                if action_state.status is ActionExecutionStatus.PLANNED:
                    await self._action_executions.fail_before_action(
                        action_execution_id=action_state.action_execution_id,
                        permit=permit,
                        expected_row_version=action_state.row_version,
                        error_code="H5_ACTION_BEFORE_CAPTURE_CANCELED",
                        error_message="H5 动作前采集已取消，未执行动作",
                    )
                elif action_state.status is ActionExecutionStatus.EXECUTING:
                    await self._action_executions.record_interaction(
                        interaction=InteractionResult(
                            task_id=intent.task_id,
                            session_id=intent.session_id,
                            intent_id=intent.intent_id,
                            action_execution_id=action_state.action_execution_id,
                            source_capture_id=(before_capture.capture_id if before_capture
                                               else action_state.source_capture_id),
                            status=InteractionResultStatus.FAILED,
                            error_code="H5_ACTION_OUTCOME_UNKNOWN_DURING_CANCELLATION",
                            error_message="H5 动作已取消，浏览器侧执行结果未知",
                            state_summary={"after_capture_status": "not_collected"},
                        ),
                        permit=permit,
                        expected_status=ActionExecutionStatus.EXECUTING,
                        expected_row_version=action_state.row_version,
                    )
                elif (
                    action_state.status is ActionExecutionStatus.EVIDENCE_PENDING
                    and interaction is not None
                ):
                    await self._action_executions.fail_evidence(
                        interaction=interaction.mark_evidence_failed(
                            error_code="H5_ACTION_AFTER_CAPTURE_CANCELED",
                            error_message="H5 动作后采集已取消，后态证据未完成",
                            retryable=False,
                        ),
                        permit=permit,
                        expected_row_version=action_state.row_version,
                    )
        except (Exception, asyncio.CancelledError) as cleanup_error:
            _LOGGER.error(
                "H5 取消动作账本收尾失败 action_execution_id=%s cause=%s",
                action_state.action_execution_id,
                type(cleanup_error).__name__,
            )

    async def _load_existing_recoverable_action(
        self,
        intent: ActionIntent,
    ) -> ActionExecutionSnapshot | None:
        """App 与受信任 H5 会话只按稳定动作身份读取恢复事实。"""

        if not _supports_durable_action_recovery(intent) or self._action_executions is None:
            return None
        get_action = getattr(self._action_executions, "get", None)
        if not callable(get_action):
            return None
        persisted = await get_action(action_execution_id=build_action_execution_id(intent))
        if persisted is not None:
            _validate_action_state(persisted, intent=intent)
        return persisted

    async def _load_optional_planned_before_capture(
        self,
        *,
        command: CaptureCoordinationCommand,
        action_state: ActionExecutionSnapshot,
    ) -> CaptureRef | None:
        """planned 恢复先复用已提交的动作前 Capture，避免重复设备截图。"""

        reader = getattr(
            self._action_recovery_captures,
            "get_verified_action_capture",
            None,
        )
        if not callable(reader):
            raise AppActionRecoveryBlockedError(
                "Capture Writer 不支持按动作身份恢复 Before Capture",
                error_code="APP_ACTION_RECOVERY_CAPTURE_READER_MISSING",
            )
        try:
            persisted = await reader(
                action_execution_id=action_state.action_execution_id,
                role="before_action",
            )
        except (AppCaptureRecoveryError, PageCaptureRecoveryError) as exc:
            raise AppActionRecoveryBlockedError(
                str(exc),
                error_code=exc.error_code,
            ) from exc
        capture = getattr(persisted, "capture", None)
        if persisted is None:
            return None
        if not isinstance(capture, CaptureRef):
            raise AppActionRecoveryBlockedError(
                "App 动作恢复引用的 Before Capture 类型无效",
                error_code="APP_ACTION_RECOVERY_CAPTURE_INVALID",
            )
        try:
            _validate_capture_ref(
                capture,
                expected_task_id=command.intent.task_id,
                expected_session_id=command.intent.session_id,
                expected_surface_kind=command.intent.surface_kind,
                expected_role=command.intent.capture_profile.before.role,
                expected_parent_id=(
                    None
                    if is_trusted_initial_surface_action(command.intent)
                    else command.intent.source_capture_id
                ),
                expected_action_execution_id=action_state.action_execution_id,
            )
        except ValueError as exc:
            raise AppActionRecoveryBlockedError(
                "App 动作恢复引用的 Before Capture 身份无效",
                error_code="APP_ACTION_RECOVERY_CAPTURE_INVALID",
            ) from exc
        return capture

    async def _replay_recovered_action(
        self,
        *,
        command: CaptureCoordinationCommand,
        action_state: ActionExecutionSnapshot,
    ) -> CaptureCoordinationResult | None:
        """只回放完整 PostgreSQL 事实；未知动作结果不得再次进入 Driver。"""

        if action_state.status is ActionExecutionStatus.PLANNED:
            return None
        if (
            action_state.status is ActionExecutionStatus.FAILED
            and action_state.source_capture_id is None
            and action_state.started_at is None
            and action_state.error_code == "before_capture_failed"
        ):
            raise AppActionRecoveryBlockedError(
                "动作前 Capture 的一次安全重试已耗尽",
                error_code="before_capture_failed",
            )
        recoverable_statuses = {
            ActionExecutionStatus.COMPLETED: InteractionResultStatus.SUCCEEDED,
            ActionExecutionStatus.BLOCKED: InteractionResultStatus.BLOCKED,
            ActionExecutionStatus.FAILED: InteractionResultStatus.FAILED,
            ActionExecutionStatus.TIMED_OUT: InteractionResultStatus.TIMED_OUT,
        }
        expected_result_status = recoverable_statuses.get(action_state.status)
        if expected_result_status is None:
            raise AppActionRecoveryBlockedError(
                "App 动作恢复缺少可确认的 After Capture，禁止再次执行设备动作",
                error_code=_recovery_block_error_code(
                    action_state,
                    fallback="APP_ACTION_RECOVERY_BLOCKED",
                ),
            )
        if (
            action_state.source_capture_id is None
            or action_state.result_capture_id is None
            or not action_state.result_payload
        ):
            raise AppActionRecoveryBlockedError(
                "App 动作完成账本缺少 Capture 或结果事实",
                error_code=_recovery_block_error_code(
                    action_state,
                    fallback="APP_ACTION_RECOVERY_FACTS_MISSING",
                ),
            )
        before = await self._load_recovery_capture(action_state.source_capture_id)
        after = await self._load_recovery_capture(action_state.result_capture_id)
        try:
            interaction = InteractionResult.from_dict(action_state.result_payload)
        except (TypeError, ValueError) as exc:
            raise AppActionRecoveryBlockedError(
                "App 动作恢复结果合同无效",
                error_code="APP_ACTION_RECOVERY_RESULT_INVALID",
            ) from exc
        _validate_capture_ref(
            before,
            expected_task_id=command.intent.task_id,
            expected_session_id=command.intent.session_id,
            expected_surface_kind=command.intent.surface_kind,
            expected_role=command.intent.capture_profile.before.role,
            expected_parent_id=(
                None
                if is_trusted_initial_surface_action(command.intent)
                else command.intent.source_capture_id
            ),
            expected_action_execution_id=action_state.action_execution_id,
        )
        _validate_capture_ref(
            after,
            expected_task_id=command.intent.task_id,
            expected_session_id=command.intent.session_id,
            expected_surface_kind=command.intent.surface_kind,
            expected_role=command.intent.capture_profile.after.role,
            expected_parent_id=before.capture_id,
            expected_action_execution_id=action_state.action_execution_id,
        )
        _validate_interaction(
            interaction,
            command,
            before,
            expected_action_execution_id=action_state.action_execution_id,
        )
        if interaction.status is not expected_result_status or (
            interaction.result_capture_id != after.capture_id
        ):
            raise AppActionRecoveryBlockedError(
                "App 动作恢复结果与账本终态或 After Capture 不一致",
                error_code="APP_ACTION_RECOVERY_RESULT_INVALID",
            )
        await attach_app_action_log_safely(
            self._action_evidence_writer,
            intent=command.intent,
            interaction=interaction,
            before_capture_id=before.capture_id,
            after_capture_id=after.capture_id,
        )
        return CaptureCoordinationResult(
            before_capture=before,
            interaction=interaction,
            after_capture=after,
        )

    async def _load_recovery_capture(self, capture_id: str) -> CaptureRef:
        reader = getattr(self._action_recovery_captures, "get_verified_capture", None)
        if not callable(reader):
            raise AppActionRecoveryBlockedError(
                "Capture Writer 不支持动作恢复读取",
                error_code="APP_ACTION_RECOVERY_CAPTURE_READER_MISSING",
            )
        try:
            persisted = await reader(capture_id=capture_id)
        except (AppCaptureRecoveryError, PageCaptureRecoveryError) as exc:
            raise AppActionRecoveryBlockedError(
                str(exc),
                error_code=exc.error_code,
            ) from exc
        capture = getattr(persisted, "capture", None)
        if not isinstance(capture, CaptureRef) or capture.capture_id != capture_id:
            raise AppActionRecoveryBlockedError(
                "App 动作恢复引用的 Capture 不存在",
                error_code="APP_ACTION_RECOVERY_CAPTURE_MISSING",
            )
        return capture


@dataclass
class _CoordinationProgress:
    """一次命令栈内的不可逆动作边界标记。"""

    action_started: bool = False


def _validate_action_state(
    snapshot: ActionExecutionSnapshot,
    *,
    intent: ActionIntent,
) -> None:
    """恢复账本必须与本轮不可变意图完全一致。"""

    requested_source_capture_id = (
        None if is_trusted_initial_surface_action(intent) else intent.source_capture_id
    )
    if (
        snapshot.action_execution_id != build_action_execution_id(intent)
        or snapshot.task_id != intent.task_id
        or snapshot.session_id != intent.session_id
        or snapshot.intent_id != intent.intent_id
        or snapshot.intent_hash != build_action_intent_hash(intent)
        or snapshot.requested_source_capture_id != requested_source_capture_id
    ):
        raise AppActionRecoveryBlockedError(
            "App 动作恢复账本与当前 Work 意图不一致",
            error_code="APP_ACTION_RECOVERY_IDENTITY_MISMATCH",
        )


def _recovery_block_error_code(
    snapshot: ActionExecutionSnapshot,
    *,
    fallback: str,
) -> str:
    """仅透传平台定义的恢复阻断码，避免任意 Driver 错误污染终态路由。"""

    if snapshot.error_code in {
        "APP_ACTION_OUTCOME_UNKNOWN_AFTER_CRASH",
        "APP_AFTER_CAPTURE_MISSING_AFTER_CRASH",
    }:
        return snapshot.error_code
    return fallback


def _validate_capture_ref(
    reference: CaptureRef,
    *,
    expected_task_id: str,
    expected_session_id: str,
    expected_surface_kind: SurfaceKind,
    expected_role: str,
    expected_parent_id: str | None,
    expected_action_execution_id: str | None,
) -> None:
    if not isinstance(reference, CaptureRef):
        raise ValueError("Capture Writer 必须返回 CaptureRef")
    if reference.task_id != expected_task_id:
        raise ValueError("Capture 返回的 task_id 与请求不一致")
    if reference.session_id != expected_session_id:
        raise ValueError("Capture 返回的 session_id 与请求不一致")
    if reference.surface_kind is not expected_surface_kind:
        raise ValueError("Capture 返回的 surface_kind 与请求不一致")
    if reference.role != expected_role:
        raise ValueError("Capture 返回的 role 与请求不一致")
    if reference.parent_capture_id != expected_parent_id:
        raise ValueError("Capture 返回的父级引用与请求不一致")
    if reference.action_execution_id != expected_action_execution_id:
        raise ValueError("Capture 返回的动作执行引用与请求不一致")


def _can_retry_failed_before_capture(action: ActionExecutionSnapshot) -> bool:
    """只有尚未触发 Driver 的首次 Before Capture 失败可再次预留门禁。"""

    retry_count = action.result_payload.get("before_capture_retry_count")
    count_is_zero = retry_count is None or (
        isinstance(retry_count, int) and not isinstance(retry_count, bool) and retry_count == 0
    )
    return (
        action.status is ActionExecutionStatus.FAILED
        and action.source_capture_id is None
        and action.started_at is None
        and action.result_capture_id is None
        and action.evidence_finished_at is None
        and action.error_code == "before_capture_failed"
        and count_is_zero
    )


def _validate_interaction(
    result: InteractionResult,
    command: CaptureCoordinationCommand,
    before_capture: CaptureRef,
    *,
    expected_action_execution_id: str | None,
) -> None:
    if not isinstance(result, InteractionResult):
        raise ValueError("Interaction Executor 必须返回 InteractionResult")
    intent = command.intent
    if result.task_id != intent.task_id:
        raise ValueError("动作执行返回的 task_id 与 Action Intent 不一致")
    if result.session_id != intent.session_id:
        raise ValueError("动作执行返回的 session_id 与 Action Intent 不一致")
    if result.intent_id != intent.intent_id:
        raise ValueError("动作执行返回的 intent_id 与 Action Intent 不一致")
    if result.source_capture_id != before_capture.capture_id:
        raise ValueError("动作执行返回的 source_capture_id 与动作前 Capture 不一致")
    if (
        expected_action_execution_id is not None
        and result.action_execution_id != expected_action_execution_id
    ):
        raise ValueError("动作执行返回的 action_execution_id 与持久化计划不一致")


def _is_transient_after_capture_error(exc: BaseException) -> bool:
    """动作后截图的瞬时故障：允许同一次编排内有界重试，不得抬成误终态。"""

    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if getattr(exc, "retryable", None) is True:
        return True
    code = str(getattr(exc, "error_code", None) or getattr(exc, "code", None) or "")
    if code in {
        "CAPTURE_FAILED",
        "DEVICE_CAPTURE_FAILED",
        "OFFICIAL_API_ERROR",
        "OFFICIAL_API_TIMEOUT",
        "OFFICIAL_HTTP_408",
        "OFFICIAL_HTTP_429",
        "OFFICIAL_HTTP_502",
        "OFFICIAL_HTTP_503",
        "OFFICIAL_HTTP_504",
        "APP_CAPTURE_TRANSPORT_FAILED",
    }:
        return True
    message = str(exc).lower()
    tokens = (
        "502",
        "408",
        "timeout",
        "timed out",
        "老化",
        "aged",
        "screencap",
        "adb",
        "connection",
        "official_api",
    )
    return any(token in message for token in tokens)


async def _capture_after_action_with_retry(
    writer: CaptureWriter,
    request: CaptureWriteRequest,
) -> CaptureRef:
    """动作后 Capture 瞬时失败时同编排内最多再试两次（共 3 次）。"""

    attempts = 3
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await writer.capture(request)
        except Exception as exc:  # noqa: BLE001 - 瞬时故障需有界重试
            last_exc = exc
            if attempt + 1 >= attempts or not _is_transient_after_capture_error(exc):
                raise
            await asyncio.sleep(0.25 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _web_terminal_interaction_has_pending_evidence(
    interaction: InteractionResult,
) -> bool:
    """由受信 Executor 明确声明可安全观察的失败，仍必须保存后态。"""
    return bool(
        interaction.status
        in {
            InteractionResultStatus.BLOCKED,
            InteractionResultStatus.FAILED,
            InteractionResultStatus.TIMED_OUT,
        }
        and interaction.state_summary.get("post_action_evidence_available") is True
    )


def _try_build_device_execution_permit(
    parameters: Mapping[str, object] | dict[str, object] | None,
) -> DeviceExecutionPermit | None:
    """从 intent.parameters 尝试构造 DeviceExecutionPermit；字段不足则返回 None。"""

    if not isinstance(parameters, dict):
        return None
    raw = parameters.get("device_execution_permit")
    if isinstance(raw, DeviceExecutionPermit):
        return raw
    payload: dict[str, object]
    if isinstance(raw, dict):
        payload = dict(raw)
    else:
        # 扁平字段：须具备构建许可的最小集合
        required = (
            "device_id",
            "resource_id",
            "fencing_token",
            "session_id",
            "execution_segment_id",
            "task_id",
        )
        if not all(parameters.get(key) is not None for key in required):
            return None
        # 至少要有 fencing_token 才进入门禁（与设计「可选」一致）
        if parameters.get("fencing_token") is None:
            return None
        payload = {key: parameters[key] for key in required}
        if "revoked" in parameters:
            payload["revoked"] = parameters["revoked"]
    try:
        return DeviceExecutionPermit(
            device_id=str(payload["device_id"]),
            resource_id=str(payload.get("resource_id") or payload["device_id"]),
            fencing_token=str(payload["fencing_token"]),
            session_id=str(payload["session_id"]),
            execution_segment_id=str(payload["execution_segment_id"]),
            task_id=str(payload["task_id"]),
            revoked=bool(payload.get("revoked", False)),
        )
    except Exception:  # noqa: BLE001 - 参数不齐则跳过门禁
        return None


def _is_app_surface(surface_kind: SurfaceKind) -> bool:
    return surface_kind in {SurfaceKind.ANDROID, SurfaceKind.IOS}


def _supports_durable_action_recovery(intent: ActionIntent) -> bool:
    """只允许 App 与平台拥有持久化账本的 Web 会话回放动作。

    旧 H5 ``page_capture`` Tool 的 intent_id 采用 ``page-capture:*``，不能再
    用 intent 前缀把它排除；只要会话属于平台生成的 H5/Web V2 Session，实际
    是否启用仍由 Coordinator 是否注入账本与已验证 Capture Reader 决定。
    """

    if _is_app_surface(intent.surface_kind):
        return True
    return bool(
        intent.surface_kind is SurfaceKind.WEB and intent.session_id.startswith(("h5:", "web-v2:"))
    )


def _optional_viewport_dimension(value: object) -> int | None:
    """仅把合法整数视口下传给 Capture Writer，其他值保持既有默认行为。"""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _optional_device_scale_factor(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0.1 <= float(value) <= 4.0
    ):
        return None
    return float(value)


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_color_scheme(value: object) -> str | None:
    return value if value in {"light", "dark", "no-preference"} else None


def _with_web_primary_before_action_capture(
    intent: ActionIntent,
    *,
    before_capture_id: str,
) -> ActionIntent:
    """仅在执行边界附加最新 before Capture，保留发现 Capture 作为候选证据。"""

    if intent.surface_kind is not SurfaceKind.WEB:
        return intent
    target_mode = intent.parameters.get("target_mode")
    before_parameter = {
        "web_primary_content_candidate": "web_primary_content_before_action_capture_id",
        "web_search_discovery_candidate": "web_auxiliary_before_action_capture_id",
        "web_detail_candidate_url": "web_detail_before_action_capture_id",
    }.get(target_mode)
    if before_parameter is None:
        return intent
    return replace(
        intent,
        parameters={
            **intent.parameters,
            before_parameter: before_capture_id,
        },
    )


def _optional_web_capture_stage(value: object) -> str | None:
    return (
        value
        if value
        in {
            "web_entry_probe",
            "web_page_probe",
            "web_page_ready_baseline",
            "web_primary_content_evidence",
        }
        else None
    )
