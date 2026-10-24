"""App Probe 恢复执行端口：策略 → 设备原语 → 恢复后不变量快照。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from services.api.app.application.app_state_invariants import (
    build_app_state_invariant_snapshot,
)
from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationResult,
    ExecuteInteractionRequest,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidationError,
    InteractionCommandValidator,
)
from services.api.app.application.restoration import (
    RestorationExecutionError,
    RestorationExecutionRequest,
    RestorationExecutionResult,
)
from services.api.app.domain.inspection import (
    RestorationStrategy,
    StateInvariantSnapshot,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    AppOpenRequest,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceDriver,
    SurfaceKind,
    SurfaceSessionRef,
)
from services.api.app.infrastructure.app_runtime.fake import FakeAppRuntime


def _build_restore_intent(
    *,
    session: SurfaceSessionRef,
    operation: InteractionOperation,
    action_execution_id: str,
    parameters: Mapping[str, Any],
    source_capture_id: str | None = None,
) -> ActionIntent:
    now = datetime.now(UTC)
    reset_policy = str(parameters.get("reset_policy") or "").strip().casefold()
    return ActionIntent(
        task_id=session.task_id,
        session_id=session.session_id,
        intent_id=f"intent:{action_execution_id}",
        idempotency_key=f"idem:{action_execution_id}",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=source_capture_id or f"restore-source:{action_execution_id}",
        action_type=operation,
        semantic_target=None,
        purpose=f"app_restoration:{operation.value}",
        capture_profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID, operation),
        deadline=now + timedelta(minutes=2),
        parameters=dict(parameters),
        risk_level=(
            InteractionRiskLevel.HIGH
            if operation is InteractionOperation.RESET_APP
            and reset_policy == "clear_data"
            else InteractionRiskLevel.MEDIUM
        ),
    )

logger = logging.getLogger(__name__)


class RestorationCaptureCoordinator(Protocol):
    """恢复动作复用的统一 before/action/after Coordinator。"""

    async def coordinate(
        self,
        command: CaptureCoordinationCommand,
    ) -> CaptureCoordinationResult: ...


class RestoredSnapshotReader(Protocol):
    """只从 Coordinator 产生的 After Capture 读取恢复后不变量。"""

    async def read(self, *, capture: Any) -> StateInvariantSnapshot: ...


class AppRestorationInteractionExecutor:
    """恢复专用 Interaction Executor；不负责租约，租约由外层 Coordinator 持有。"""

    def __init__(
        self,
        *,
        driver: SurfaceDriver,
        surface_sessions: Any,
        package_name: str | None,
        activity: str | None = None,
        session: SurfaceSessionRef | None = None,
        command_validator: InteractionCommandValidator | None = None,
    ) -> None:
        self._driver = driver
        self._surface_sessions = surface_sessions
        self._package_name = package_name
        self._activity = activity
        self._session = session
        self._command_validator = command_validator

    def bind_session(self, session: SurfaceSessionRef) -> None:
        """NEW_SESSION_REPLAY 注册成功后把同一个引用交给后续动作。"""
        if not isinstance(session, SurfaceSessionRef):
            raise TypeError("session 必须为 SurfaceSessionRef")
        self._session = session

    def bind_context(
        self,
        *,
        session: SurfaceSessionRef,
        package_name: str,
        activity: str | None,
    ) -> None:
        """在每次恢复尝试开始前绑定已验证的 App 身份与 Session。"""
        if not isinstance(package_name, str) or not package_name.strip():
            raise ValueError("package_name 必须为非空字符串")
        self.bind_session(session)
        self._package_name = package_name
        self._activity = activity

    def bind_command_validator(
        self,
        command_validator: InteractionCommandValidator | None,
    ) -> None:
        """把外层正式 Coordinator 的任务范围校验器绑定到动作出口。"""

        if command_validator is not None and not isinstance(
            command_validator, InteractionCommandValidator
        ):
            raise TypeError("command_validator 必须为 InteractionCommandValidator")
        self._command_validator = command_validator

    async def execute(self, request: ExecuteInteractionRequest) -> InteractionResult:
        if request.action_execution_id is None:
            raise RestorationExecutionError(
                error_code="ACTION_EXECUTION_REQUIRED",
                message="恢复动作缺少 Action Execution 身份",
                unfulfillable=True,
            )
        if not self._package_name:
            raise RestorationExecutionError(
                error_code="PACKAGE_MISSING",
                message="恢复动作缺少已验证 App 包名",
                unfulfillable=True,
            )
        session = await self._resolve_session(request)
        operation = request.intent.action_type
        # Coordinator 生成的 before Capture 才是这次实际动作的来源；不能把
        # 恢复计划里过期的 source_capture_id 继续传给 Official/ADB 下游。
        driver_intent = replace(
            request.intent,
            source_capture_id=request.source_capture_id,
        )
        # 正式 Coordinator 下游必须统一走带租约上下文的 execute 端口。
        # 不能因为 warm/reset 有专用原语，就绕过 Official API 的
        # /apps/execute 和 Xingyu 的 execution_context 校验；否则这两个
        # 恢复动作会成为一个不带 before/action/after 账本的旁路。
        formal_execution = _requires_context_capable_driver(request)
        # Driver 不支持正式上下文时由 Broker 返回带回执的明确拒绝，
        # 避免 Coordinator 将执行器异常误记为已开始动作的未知结果。
        broker_receipt: Mapping[str, Any] | None = None
        if not formal_execution and operation in {
            InteractionOperation.WARM_RESTORE,
            InteractionOperation.RESET_APP,
        }:
            # 仅保留没有 Coordinator 租约的既有离线/Fake 兼容原语；正式恢复
            # 请求永远在下面经由 Broker 调用 execute_with_context。
            method_name = (
                "warm_restore"
                if operation is InteractionOperation.WARM_RESTORE
                else "reset_app"
            )
            method = getattr(self._driver, method_name, None)
            if callable(method):
                raw = await method(
                    AppOpenRequest(
                        task_id=session.task_id,
                        session_id=session.session_id,
                        package_name=self._package_name,
                        activity=self._activity,
                        cold_start=operation is InteractionOperation.RESET_APP,
                    )
                )
            else:
                from services.api.app.application.app_command_broker import (
                    execute_app_driver_action_via_broker,
                )

                raw, broker_receipt = await execute_app_driver_action_via_broker(
                    driver=self._driver,
                    session=session,
                    intent=driver_intent,
                    action_execution_id=request.action_execution_id,
                    execution_context=None,
                    package_allowlist=(self._package_name,),
                    capture_epoch=request.source_capture_id,
                    foreground_package=self._package_name,
                    agent_run_id=request.agent_run_id,
                    source_capture_id=request.source_capture_id,
                    command_validator=self._command_validator,
                )
        else:
            from services.api.app.application.app_command_broker import (
                execute_app_driver_action_via_broker,
            )

            raw, broker_receipt = await execute_app_driver_action_via_broker(
                driver=self._driver,
                session=session,
                intent=driver_intent,
                action_execution_id=request.action_execution_id,
                execution_context=(
                    _execution_context_from_request(request) if formal_execution else None
                ),
                package_allowlist=(self._package_name,),
                capture_epoch=request.source_capture_id,
                foreground_package=self._package_name,
                agent_run_id=request.agent_run_id,
                source_capture_id=request.source_capture_id,
                command_validator=self._command_validator,
            )
        if not raw.succeeded:
            return _restoration_failed_result(
                request,
                error_code=raw.error_code or "RESTORE_ACTION_FAILED",
                retryable=raw.retryable,
                broker_receipt=broker_receipt,
            )
        return _restoration_pending_result(
            request,
            state_summary={
                "surface_kind": request.intent.surface_kind.value,
                "restoration_operation": operation.value,
                **(
                    {"app_command_broker_receipt": broker_receipt}
                    if broker_receipt is not None
                    else {}
                ),
            },
        )

    async def _resolve_session(self, request: ExecuteInteractionRequest) -> SurfaceSessionRef:
        if self._session is not None and (
            self._session.task_id == request.intent.task_id
            and self._session.session_id == request.intent.session_id
        ):
            return self._session
        getter = getattr(self._surface_sessions, "get", None)
        if not callable(getter):
            raise RestorationExecutionError(
                error_code="APP_SESSION_NOT_ACTIVE",
                message="恢复动作缺少可验证的 App Session",
                unfulfillable=False,
            )
        snapshot = await getter(session_id=request.intent.session_id)
        if (
            snapshot is None
            or snapshot.task_id != request.intent.task_id
            or snapshot.surface_kind is not SurfaceKind.ANDROID
            or str(getattr(snapshot.status, "value", snapshot.status)) != "active"
            or snapshot.current_surface_ref is None
        ):
            # 不直接信任 request 中的 Surface 身份；必须从服务端会话事实恢复。
            raise RestorationExecutionError(
                error_code="APP_SESSION_NOT_ACTIVE",
                message="恢复动作对应的 App Session 不可执行",
                unfulfillable=False,
            )
        self._session = SurfaceSessionRef(
            task_id=snapshot.task_id,
            session_id=snapshot.session_id,
            surface_kind=snapshot.surface_kind,
            primary_surface_ref=snapshot.current_surface_ref,
        )
        return self._session


def _execution_context_from_request(request: ExecuteInteractionRequest) -> Mapping[str, object]:
    """把 Coordinator 租约字段交给底层服务；不写入动作状态或日志。"""
    return {
        "task_id": request.intent.task_id,
        "session_id": request.intent.session_id,
        "surface_kind": request.intent.surface_kind.value,
        "action_execution_id": request.action_execution_id,
        "source_capture_id": request.source_capture_id,
        "finalization_epoch": request.finalization_epoch,
        "command_sequence": request.command_sequence,
        "lease_owner": request.lease_owner,
        "lease_token": request.lease_token,
    }


def _requires_context_capable_driver(request: ExecuteInteractionRequest) -> bool:
    """判断请求是否已经进入 Coordinator 的正式序列/租约执行模式。"""

    # 只有 finalization_epoch 的历史 Web 请求仍保留裸 Driver 兼容形状；
    # command_sequence 或 lease_owner 一旦出现，就必须把正式上下文传到底层。
    return request.command_sequence is not None or request.lease_owner is not None


def _restoration_pending_result(
    request: ExecuteInteractionRequest,
    *,
    state_summary: Mapping[str, object],
) -> InteractionResult:
    return InteractionResult(
        task_id=request.intent.task_id,
        session_id=request.intent.session_id,
        intent_id=request.intent.intent_id,
        action_execution_id=request.action_execution_id or "unplanned",
        source_capture_id=request.source_capture_id,
        status=InteractionResultStatus.EVIDENCE_PENDING,
        state_summary=state_summary,
    )


def _restoration_failed_result(
    request: ExecuteInteractionRequest,
    *,
    error_code: str,
    retryable: bool,
    broker_receipt: Mapping[str, Any] | None = None,
) -> InteractionResult:
    state_summary: dict[str, Any] = {"surface_kind": request.intent.surface_kind.value}
    if broker_receipt is not None:
        state_summary["app_command_broker_receipt"] = dict(broker_receipt)
    return InteractionResult(
        task_id=request.intent.task_id,
        session_id=request.intent.session_id,
        intent_id=request.intent.intent_id,
        action_execution_id=request.action_execution_id or "unplanned",
        source_capture_id=request.source_capture_id,
        status=InteractionResultStatus.FAILED,
        error_code=error_code,
        error_message="恢复动作未成功",
        retryable=retryable,
        state_summary=state_summary,
    )


class AppRestorationExecutor:
    """把 RestorationPlan 映射为 warm_restore / deep_link / reset_app 与快照。"""

    def __init__(
        self,
        *,
        driver: SurfaceDriver | None,
        session: SurfaceSessionRef | None,
        package_name: str | None,
        activity: str | None = None,
        deep_link: str | None = None,
        probe_result_snapshot: StateInvariantSnapshot | None = None,
        restored_snapshot_factory: Callable[[], StateInvariantSnapshot] | None = None,
        platform_build_ref: str | None = None,
        device_ref: str | None = None,
        account_environment_ref: str | None = None,
        session_options: Mapping[str, Any] | None = None,
        command_validator: InteractionCommandValidator | None = None,
        capture_coordinator: RestorationCaptureCoordinator | None = None,
        restored_snapshot_reader: RestoredSnapshotReader | None = None,
        surface_sessions: Any | None = None,
        legacy_driver_fallback: bool = False,
    ) -> None:
        self._driver = driver
        self._session = session
        self._package_name = package_name
        self._activity = activity
        self._deep_link = deep_link
        self._probe_result_snapshot = probe_result_snapshot
        self._restored_snapshot_factory = restored_snapshot_factory
        self._platform_build_ref = platform_build_ref
        self._device_ref = device_ref
        self._account_environment_ref = account_environment_ref
        self._session_options = dict(session_options or {})
        self._command_validator = command_validator
        self._capture_coordinator = capture_coordinator
        self._restored_snapshot_reader = restored_snapshot_reader
        self._surface_sessions = surface_sessions
        # 仅供既有 Fake/单测显式保留旧的直驱合同；正式 App 恢复默认必须
        # 经过 CaptureCoordinator，避免 warm/reset/execute 绕过 before/after。
        if not isinstance(legacy_driver_fallback, bool):
            raise TypeError("legacy_driver_fallback 必须为 bool")
        if legacy_driver_fallback and not isinstance(self._driver, FakeAppRuntime):
            raise ValueError(
                "legacy_driver_fallback 仅允许实际 FakeAppRuntime，拒绝真实或未知 Driver"
            )
        self._legacy_driver_fallback = legacy_driver_fallback
        self._current_source_capture_id: str | None = None
        self._last_after_capture: Any | None = None

    async def restore(
        self,
        request: RestorationExecutionRequest,
    ) -> RestorationExecutionResult:
        plan = request.plan
        action_ids: list[str] = []
        # 恢复链路的第一步必须引用 Evidence Request 的真实目标 Capture；
        # 后续动作再沿用上一个动作的真实 After Capture，不生成伪造 source ID。
        self._current_source_capture_id = request.request.target_capture_id
        self._last_after_capture = None
        self._assert_restoration_execution_boundary(plan)
        # 会话事件专用恢复：权限框先尝试 dismiss；进程死亡优先 warm/reset
        event = str(self._session_options.get("session_event") or "").strip()
        if event == "permission_dialog" and plan.strategy is not RestorationStrategy.UNFULFILLABLE:
            dismissed = await self._dismiss_permission_or_back(
                attempt_id=request.restoration_attempt_id
            )
            if dismissed:
                action_ids.append(dismissed)
        if event in {"process_death", "process_rebuild"} and plan.strategy is (
            RestorationStrategy.SAME_APP_SESSION
        ):
            action_ids.extend(
                await self._run_restore_actions(
                    operations=(
                        InteractionOperation.WARM_RESTORE,
                        InteractionOperation.RESET_APP,
                    ),
                    attempt_id=request.restoration_attempt_id,
                )
            )
            snapshot = await self._snapshot_after_restore(
                session_id=plan.target_session_id,
                attempt_id=request.restoration_attempt_id,
            )
            return RestorationExecutionResult(
                restored_snapshot=snapshot,
                action_execution_ids=tuple(action_ids),
            )
        if plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN:
            snapshot = self._require_probe_snapshot(plan_target_session=plan.target_session_id)
            return RestorationExecutionResult(
                restored_snapshot=snapshot,
                action_execution_ids=(),
            )
        if plan.strategy is RestorationStrategy.SAME_APP_SESSION:
            action_ids.extend(
                await self._run_restore_actions(
                    operations=(
                        InteractionOperation.WARM_RESTORE,
                        InteractionOperation.DEEP_LINK,
                    ),
                    attempt_id=request.restoration_attempt_id,
                )
            )
            snapshot = await self._snapshot_after_restore(
                session_id=plan.target_session_id,
                attempt_id=request.restoration_attempt_id,
            )
            return RestorationExecutionResult(
                restored_snapshot=snapshot,
                action_execution_ids=tuple(action_ids),
            )
        if plan.strategy is RestorationStrategy.NEW_SESSION_REPLAY:
            started = await self._start_new_replay_session(
                target_session_id=plan.target_session_id,
                attempt_id=request.restoration_attempt_id,
            )
            if started:
                action_ids.append(started)
            action_ids.extend(
                await self._run_restore_actions(
                    operations=(
                        InteractionOperation.RESET_APP,
                        InteractionOperation.DEEP_LINK,
                    ),
                    attempt_id=request.restoration_attempt_id,
                )
            )
            snapshot = await self._snapshot_after_restore(
                session_id=plan.target_session_id,
                attempt_id=request.restoration_attempt_id,
            )
            return RestorationExecutionResult(
                restored_snapshot=snapshot,
                action_execution_ids=tuple(action_ids),
            )
        raise RestorationExecutionError(
            error_code="RESTORATION_STRATEGY_UNSUPPORTED",
            message=f"App 不支持恢复策略 {plan.strategy.value}",
            unfulfillable=True,
        )

    def _assert_restoration_execution_boundary(self, plan: Any) -> None:
        """正式 App 有设备时必须具备统一恢复 Coordinator。"""

        if (
            self._driver is None
            or self._capture_coordinator is not None
            or self._legacy_driver_fallback
            or plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN
        ):
            return
        raise RestorationExecutionError(
            error_code="RESTORATION_COORDINATOR_REQUIRED",
            message="正式 App 恢复缺少 CaptureCoordinator，拒绝触发设备动作",
            unfulfillable=True,
        )

    async def _start_new_replay_session(
        self,
        *,
        target_session_id: str,
        attempt_id: str,
    ) -> str | None:
        """真正 start_session(session-replay:*)，而不是只改写快照 Session 身份。"""
        if self._driver is None or self._session is None:
            logger.info("NEW_SESSION_REPLAY 跳过 start_session：Driver 或 Session 不可用")
            return None
        if target_session_id == self._session.session_id:
            return None
        if self._capture_coordinator is None and not self._legacy_driver_fallback:
            raise RestorationExecutionError(
                error_code="RESTORATION_COORDINATOR_REQUIRED",
                message="正式 App 恢复缺少 CaptureCoordinator，拒绝建立 Replay Session",
                unfulfillable=True,
            )
        if self._capture_coordinator is not None:
            # 目标 Session 尚未有属于自己的父 Capture，而 App Capture 的
            # 父子关系要求 parent_capture 与当前 Session 同源。当前组合根
            # 没有“目标 Session 首帧/seed Capture”端口时，不能先启动设备再
            # 让后续 Coordinator 失败，也不能跨 Session 借用源 Capture。
            raise RestorationExecutionError(
                error_code="REPLAY_SESSION_CAPTURE_SEED_REQUIRED",
                message="NEW_SESSION_REPLAY 需要先在目标 Session 建立父 Capture",
                unfulfillable=True,
            )
        from services.api.app.domain.interaction import SurfaceSessionSpec

        # Official/Fake 需要 platform_build_ref；ADB Driver 可忽略
        platform_build_ref = self._platform_build_ref or self._session_options.get(
            "platform_build_ref"
        )
        device_ref = self._device_ref or self._session_options.get("device_ref")
        account_ref = self._account_environment_ref or self._session_options.get(
            "account_environment_ref"
        )
        source_snapshot = None
        if self._surface_sessions is not None:
            getter = getattr(self._surface_sessions, "get", None)
            if callable(getter):
                source_snapshot = await getter(session_id=self._session.session_id)
                if source_snapshot is not None:
                    platform_build_ref = platform_build_ref or source_snapshot.platform_build_ref
                    device_ref = device_ref or source_snapshot.device_ref
                    account_ref = account_ref or source_snapshot.account_environment_ref
        options = {
            key: value
            for key, value in self._session_options.items()
            if key
            not in {
                "platform_build_ref",
                "device_ref",
                "account_environment_ref",
                "password",
                "access_token",
                "refresh_token",
                "cookie",
            }
        }
        options["replay_of_session_id"] = self._session.session_id
        options["restoration_attempt_id"] = attempt_id
        spec_kwargs: dict[str, Any] = {
            "task_id": self._session.task_id,
            "session_id": target_session_id,
            "surface_kind": SurfaceKind.ANDROID,
            "options": options,
        }
        if platform_build_ref:
            spec_kwargs["platform_build_ref"] = str(platform_build_ref)
        if device_ref:
            spec_kwargs["device_ref"] = str(device_ref)
        if account_ref:
            spec_kwargs["account_environment_ref"] = str(account_ref)
        if source_snapshot is not None:
            spec_kwargs["device_lease_id"] = source_snapshot.device_lease_id
            spec_kwargs["device_lease_fencing_token"] = source_snapshot.device_lease_fencing_token
        # Fake/Official 强制要求 platform_build_ref；缺失时明确失败而非静默同会话 reset
        if platform_build_ref is None and hasattr(self._driver, "_sessions"):
            raise RestorationExecutionError(
                error_code="PLATFORM_BUILD_MISSING",
                message="NEW_SESSION_REPLAY 缺少 platform_build_ref，无法 start_session",
                unfulfillable=True,
            )
        try:
            new_ref = await self._driver.start_session(SurfaceSessionSpec(**spec_kwargs))
        except Exception as exc:  # noqa: BLE001
            raise RestorationExecutionError(
                error_code="START_SESSION_FAILED",
                message=f"新 Session 建立失败：{type(exc).__name__}: {exc}",
                unfulfillable=False,
            ) from exc
        if self._capture_coordinator is not None:
            if self._surface_sessions is None:
                raise RestorationExecutionError(
                    error_code="REPLAY_SESSION_REGISTRATION_REQUIRED",
                    message="NEW_SESSION_REPLAY 缺少服务端 Session 注册端口",
                    unfulfillable=True,
                )
            register = getattr(self._surface_sessions, "register_active", None)
            if not callable(register):
                raise RestorationExecutionError(
                    error_code="REPLAY_SESSION_REGISTRATION_REQUIRED",
                    message="NEW_SESSION_REPLAY 无法注册目标 App Session",
                    unfulfillable=True,
                )
            try:
                await register(
                    spec=SurfaceSessionSpec(**spec_kwargs),
                    session_owner="app-workflow",
                    primary_surface_ref=new_ref.primary_surface_ref,
                )
            except Exception as exc:  # noqa: BLE001
                try:
                    await self._driver.close_session(
                        session=new_ref,
                        reason="replay_registration_failed",
                    )
                except Exception:  # noqa: BLE001
                    pass
                raise RestorationExecutionError(
                    error_code="REPLAY_SESSION_REGISTRATION_FAILED",
                    message="NEW_SESSION_REPLAY 目标 Session 注册失败",
                    unfulfillable=False,
                ) from exc
        self._session = new_ref
        binder = getattr(self._capture_coordinator, "_interaction_executor", None)
        bind_session = getattr(binder, "bind_session", None)
        if callable(bind_session):
            bind_session(new_ref)
        return f"restore:{attempt_id}:start_session"

    async def _run_restore_actions(
        self,
        *,
        operations: tuple[InteractionOperation, ...],
        attempt_id: str,
    ) -> list[str]:
        if self._capture_coordinator is not None:
            return await self._run_restore_actions_with_coordinator(
                operations=operations,
                attempt_id=attempt_id,
            )
        if self._driver is not None and not self._legacy_driver_fallback:
            raise RestorationExecutionError(
                error_code="RESTORATION_COORDINATOR_REQUIRED",
                message="正式 App 恢复缺少 CaptureCoordinator，拒绝触发设备动作",
                unfulfillable=True,
            )
        if self._driver is None or self._session is None:
            # 无 Driver 时仍允许用 Probe 结果快照完成等价格局（单测 / 缺设备）
            logger.info("App 恢复跳过设备动作：Driver 或 Session 不可用")
            return []
        if not self._package_name:
            raise RestorationExecutionError(
                error_code="PACKAGE_MISSING",
                message="App 恢复缺少 package_name",
                unfulfillable=True,
            )
        action_ids: list[str] = []
        for operation in operations:
            if operation is InteractionOperation.DEEP_LINK and not self._deep_link:
                continue
            action_execution_id = f"restore:{attempt_id}:{operation.value}"
            parameters: dict[str, Any] = {
                "package_name": self._package_name,
                "activity": self._activity,
            }
            if operation is InteractionOperation.DEEP_LINK:
                parameters["deep_link"] = self._deep_link
            if operation is InteractionOperation.RESET_APP:
                parameters["reset_policy"] = str(
                    self._session_options.get("reset_policy") or "force_stop"
                )
            intent = _build_restore_intent(
                session=self._session,
                operation=operation,
                action_execution_id=action_execution_id,
                parameters=parameters,
                source_capture_id=self._current_source_capture_id,
            )
            await self._validate_restore_intent(intent)
            # Official 专用原语优先，避免构造完整 ActionIntent 合同
            if operation is InteractionOperation.WARM_RESTORE and hasattr(
                self._driver, "warm_restore"
            ):
                from services.api.app.domain.interaction import AppOpenRequest

                open_result = await self._driver.warm_restore(  # type: ignore[misc]
                    AppOpenRequest(
                        task_id=self._session.task_id,
                        session_id=self._session.session_id,
                        package_name=self._package_name,
                        activity=self._activity,
                        cold_start=False,
                    )
                )
                if not getattr(open_result, "succeeded", False):
                    raise RestorationExecutionError(
                        error_code=str(
                            getattr(open_result, "error_code", None) or "WARM_RESTORE_FAILED"
                        ),
                        message="warm_restore 未成功",
                        unfulfillable=False,
                    )
                action_ids.append(action_execution_id)
                continue
            if operation is InteractionOperation.RESET_APP and hasattr(
                self._driver, "reset_app"
            ):
                from services.api.app.domain.interaction import AppOpenRequest

                open_result = await self._driver.reset_app(  # type: ignore[misc]
                    AppOpenRequest(
                        task_id=self._session.task_id,
                        session_id=self._session.session_id,
                        package_name=self._package_name,
                        activity=self._activity,
                        cold_start=True,
                    )
                )
                if not getattr(open_result, "succeeded", False):
                    raise RestorationExecutionError(
                        error_code=str(
                            getattr(open_result, "error_code", None) or "RESET_APP_FAILED"
                        ),
                        message="reset_app 未成功",
                        unfulfillable=False,
                    )
                self._mark_container_rotated_if_needed(
                    reset_policy=str(parameters.get("reset_policy") or "force_stop"),
                    container_rotated=(
                        str(parameters.get("reset_policy") or "").strip().casefold()
                        == "clear_data"
                    ),
                )
                action_ids.append(action_execution_id)
                continue
            try:
                result = await self._driver.execute(
                    session=self._session,
                    intent=intent,
                    action_execution_id=action_execution_id,
                )
            except Exception as exc:  # noqa: BLE001
                raise RestorationExecutionError(
                    error_code="SURFACE_DRIVER_FAILED",
                    message=f"恢复动作失败：{type(exc).__name__}: {exc}",
                    unfulfillable=False,
                ) from exc
            if not result.succeeded:
                raise RestorationExecutionError(
                    error_code=str(result.error_code or "RESTORE_ACTION_FAILED"),
                    message=f"恢复动作 {operation.value} 未成功",
                    unfulfillable=False,
                )
            if operation is InteractionOperation.RESET_APP:
                payload = dict(getattr(result, "payload", None) or {})
                self._mark_container_rotated_if_needed(
                    reset_policy=str(parameters.get("reset_policy") or "force_stop"),
                    container_rotated=bool(payload.get("container_rotated")),
                )
            action_ids.append(action_execution_id)
        return action_ids

    async def _run_restore_actions_with_coordinator(
        self,
        *,
        operations: tuple[InteractionOperation, ...],
        attempt_id: str,
    ) -> list[str]:
        """通过统一 Coordinator 完成恢复动作的 before/action/after 闭环。"""
        if self._session is None:
            raise RestorationExecutionError(
                error_code="APP_SESSION_REQUIRED",
                message="恢复动作缺少 App Session",
                unfulfillable=True,
            )
        if not self._package_name:
            raise RestorationExecutionError(
                error_code="PACKAGE_MISSING",
                message="App 恢复缺少 package_name",
                unfulfillable=True,
            )
        binder = getattr(self._capture_coordinator, "_interaction_executor", None)
        bind_validator = getattr(binder, "bind_command_validator", None)
        if callable(bind_validator) and self._command_validator is not None:
            bind_validator(self._command_validator)
        bind_context = getattr(binder, "bind_context", None)
        if callable(bind_context):
            bind_context(
                session=self._session,
                package_name=self._package_name,
                activity=self._activity,
            )
        bind_session = getattr(binder, "bind_session", None)
        if callable(bind_session) and not callable(bind_context):
            bind_session(self._session)
        action_ids: list[str] = []
        for operation in operations:
            if operation is InteractionOperation.DEEP_LINK and not self._deep_link:
                continue
            action_execution_id = f"restore:{attempt_id}:{operation.value}"
            parameters: dict[str, Any] = {
                "package_name": self._package_name,
                "activity": self._activity,
            }
            if operation is InteractionOperation.DEEP_LINK:
                parameters["deep_link"] = self._deep_link
            if operation is InteractionOperation.RESET_APP:
                parameters["reset_policy"] = str(
                    self._session_options.get("reset_policy") or "force_stop"
                )
            intent = _build_restore_intent(
                session=self._session,
                operation=operation,
                action_execution_id=action_execution_id,
                parameters=parameters,
                source_capture_id=self._current_source_capture_id,
            )
            try:
                coordinated = await self._capture_coordinator.coordinate(
                    CaptureCoordinationCommand(
                        agent_run_id=f"restoration:{attempt_id}",
                        intent=intent,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                raise RestorationExecutionError(
                    error_code="RESTORATION_COORDINATION_FAILED",
                    message=f"恢复动作未完成：{type(exc).__name__}",
                    unfulfillable=False,
                ) from exc
            self._last_after_capture = coordinated.after_capture
            self._current_source_capture_id = coordinated.after_capture.capture_id
            interaction = coordinated.interaction
            if interaction.status is not InteractionResultStatus.SUCCEEDED:
                raise RestorationExecutionError(
                    error_code=interaction.error_code or "RESTORE_ACTION_FAILED",
                    message=f"恢复动作 {operation.value} 未成功",
                    unfulfillable=False,
                )
            if operation is InteractionOperation.RESET_APP:
                self._mark_container_rotated_if_needed(
                    reset_policy=str(parameters.get("reset_policy") or "force_stop"),
                    container_rotated=bool(
                        interaction.state_summary.get("container_rotated", False)
                    ),
                )
            action_ids.append(interaction.action_execution_id)
        return action_ids

    def _mark_container_rotated_if_needed(
        self,
        *,
        reset_policy: str,
        container_rotated: bool,
    ) -> None:
        policy = str(reset_policy or "").strip().casefold()
        if policy != "clear_data" and not container_rotated:
            return
        from services.api.app.domain.interaction import rotate_app_data_container_ref

        current = self._session_options.get("app_data_container_ref")
        self._session_options["app_data_container_ref"] = rotate_app_data_container_ref(
            str(current) if current else None,
            device_ref=self._device_ref or self._session_options.get("device_ref"),
        )
        self._session_options["data_cleared"] = True
        self._session_options["reset_policy"] = "clear_data" if policy == "clear_data" else policy

    async def _dismiss_permission_or_back(self, *, attempt_id: str) -> str | None:
        """权限框：优先点允许/拒绝；失败则 BACK。"""
        if self._driver is None or self._session is None:
            return None
        action_execution_id = f"restore:{attempt_id}:permission_dismiss"
        # 尽力：BACK 作为通用系统对话框退出
        from datetime import UTC, datetime, timedelta

        from services.api.app.application.capture_profiles import CaptureProfilePolicy
        from services.api.app.domain.interaction import ActionIntent, InteractionRiskLevel

        now = datetime.now(UTC)
        intent = ActionIntent(
            task_id=self._session.task_id,
            session_id=self._session.session_id,
            intent_id=f"intent:{action_execution_id}",
            idempotency_key=f"idem:{action_execution_id}",
            surface_kind=SurfaceKind.ANDROID,
            source_capture_id=(
                self._current_source_capture_id
                or f"restore-source:{action_execution_id}"
            ),
            action_type=InteractionOperation.BACK,
            semantic_target=None,
            purpose="app_restoration:permission_dialog",
            capture_profile=CaptureProfilePolicy.for_surface(
                SurfaceKind.ANDROID, InteractionOperation.BACK
            ),
            deadline=now + timedelta(minutes=1),
            parameters={},
            risk_level=InteractionRiskLevel.LOW,
        )
        if self._capture_coordinator is not None:
            try:
                coordinated = await self._capture_coordinator.coordinate(
                    CaptureCoordinationCommand(
                        agent_run_id=f"restoration:{attempt_id}",
                        intent=intent,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                raise RestorationExecutionError(
                    error_code="RESTORATION_COORDINATION_FAILED",
                    message=f"权限弹窗返回动作未完成：{type(exc).__name__}",
                    unfulfillable=False,
                ) from exc
            self._last_after_capture = coordinated.after_capture
            self._current_source_capture_id = coordinated.after_capture.capture_id
            if coordinated.interaction.status is InteractionResultStatus.SUCCEEDED:
                self._session_options["session_event"] = None
                return coordinated.interaction.action_execution_id
            raise RestorationExecutionError(
                error_code=coordinated.interaction.error_code or "PERMISSION_DISMISS_FAILED",
                message="权限弹窗返回动作未成功",
                unfulfillable=False,
            )
        if not self._legacy_driver_fallback:
            raise RestorationExecutionError(
                error_code="RESTORATION_COORDINATOR_REQUIRED",
                message="正式 App 恢复缺少 CaptureCoordinator，拒绝触发设备动作",
                unfulfillable=True,
            )
        await self._validate_restore_intent(intent)
        try:
            result = await self._driver.execute(
                session=self._session,
                intent=intent,
                action_execution_id=action_execution_id,
            )
        except Exception:  # noqa: BLE001
            return None
        if getattr(result, "succeeded", False):
            self._session_options["session_event"] = None
            return action_execution_id
        return None

    async def _validate_restore_intent(self, intent: ActionIntent) -> None:
        """恢复专用原语也必须经过同一任务范围，不信任调用方自报风险。"""

        if self._command_validator is None:
            raise RestorationExecutionError(
                error_code="COMMAND_SCOPE_REQUIRED",
                message="App 恢复直接动作缺少服务端命令范围校验器",
                unfulfillable=True,
            )
        try:
            await self._command_validator.validate_for(
                CaptureCoordinationCommand(
                    agent_run_id=f"restoration:{intent.intent_id}",
                    intent=intent,
                )
            )
        except InteractionCommandValidationError as exc:
            raise RestorationExecutionError(
                error_code=exc.error_code,
                message=str(exc),
                unfulfillable=not exc.retryable,
            ) from exc

    def _require_probe_snapshot(
        self,
        *,
        plan_target_session: str,
    ) -> StateInvariantSnapshot:
        if self._probe_result_snapshot is None:
            raise RestorationExecutionError(
                error_code="PROBE_CAPTURE_MISSING",
                message="ORIGINAL_SURFACE_PIN 需要 Probe 结果快照",
                unfulfillable=False,
            )
        snap = self._probe_result_snapshot
        if snap.session_id != plan_target_session:
            # Pin 策略下 target==source；若不一致则不可履行
            raise RestorationExecutionError(
                error_code="SESSION_MISMATCH",
                message="Probe 快照 Session 与恢复计划不一致",
                unfulfillable=True,
            )
        return snap

    async def _snapshot_after_restore(
        self,
        *,
        session_id: str,
        attempt_id: str,
    ) -> StateInvariantSnapshot:
        del attempt_id
        if self._restored_snapshot_reader is not None:
            if self._last_after_capture is None:
                raise RestorationExecutionError(
                    error_code="RESTORED_CAPTURE_REQUIRED",
                    message="恢复动作后缺少真实 After Capture",
                    unfulfillable=False,
                )
            try:
                reader = self._restored_snapshot_reader
                read = getattr(reader, "read", None)
                snapshot = (
                    await read(capture=self._last_after_capture)
                    if callable(read)
                    else await reader(self._last_after_capture)  # type: ignore[operator]
                )
            except RestorationExecutionError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise RestorationExecutionError(
                    error_code="RESTORED_CAPTURE_VERIFICATION_FAILED",
                    message="恢复后的真实 Capture 无法验证",
                    unfulfillable=False,
                ) from exc
            if not isinstance(snapshot, StateInvariantSnapshot):
                raise RestorationExecutionError(
                    error_code="RESTORED_CAPTURE_VERIFICATION_FAILED",
                    message="恢复后的视觉/结构事实类型无效",
                    unfulfillable=False,
                )
            if (
                snapshot.task_id != self._last_after_capture.task_id
                or snapshot.session_id != session_id
                or snapshot.capture_id != self._last_after_capture.capture_id
                or snapshot.surface_kind is not SurfaceKind.ANDROID
            ):
                raise RestorationExecutionError(
                    error_code="RESTORED_CAPTURE_IDENTITY_MISMATCH",
                    message="恢复后的 Capture 与目标 Session 身份不一致",
                    unfulfillable=True,
                )
            return snapshot
        if self._capture_coordinator is not None:
            raise RestorationExecutionError(
                error_code="RESTORED_CAPTURE_VERIFICATION_REQUIRED",
                message="生产恢复必须配置真实 After Capture 验证器",
                unfulfillable=True,
            )
        if self._restored_snapshot_factory is not None:
            return self._restored_snapshot_factory()
        if self._probe_result_snapshot is not None:
            # 生产最小闭环：恢复动作后沿用 Probe 采集的结构不变量，但 Capture/Session 身份按计划改写
            base = self._probe_result_snapshot
            return StateInvariantSnapshot(
                task_id=base.task_id,
                capture_id=str(uuid4()),
                session_id=session_id,
                surface_kind=base.surface_kind,
                environment=dict(base.environment),
                business=dict(base.business),
                semantic=dict(base.semantic),
                structure=dict(base.structure),
                captured_at=datetime.now(UTC),
            )
        return build_app_state_invariant_snapshot(
            task_id=self._session.task_id if self._session else "unknown",
            capture_id=str(uuid4()),
            session_id=session_id,
            surface_kind=SurfaceKind.ANDROID,
            package_name=self._package_name,
            activity=self._activity,
            session_options={
                "deep_link": self._deep_link,
                "lifecycle_phase": self._session_options.get("lifecycle_phase")
                or "foreground",
                "session_event": self._session_options.get("session_event"),
                "process_alive": self._session_options.get("process_alive"),
                "safe_area": self._session_options.get("safe_area"),
            },
            captured_at=datetime.now(UTC),
        )


def snapshot_from_target_invariants(
    *,
    request: Any,
    session_id: str,
    surface_kind: SurfaceKind,
    captured_at: datetime | None = None,
) -> StateInvariantSnapshot:
    """从 EvidenceRequest.target_invariants 构造原 Capture 不变量。"""
    invariants = dict(getattr(request, "target_invariants", {}) or {})
    environment = dict(invariants.get("environment") or {})
    business = dict(invariants.get("business") or {})
    semantic = dict(invariants.get("semantic") or {})
    structure = dict(invariants.get("structure") or {})
    if "surface_kind" not in environment:
        environment["surface_kind"] = surface_kind.value
    return StateInvariantSnapshot(
        task_id=str(request.task_id),
        capture_id=str(request.target_capture_id),
        session_id=session_id,
        surface_kind=surface_kind,
        environment=environment,
        business=business,
        semantic=semantic,
        structure=structure,
        captured_at=captured_at or datetime.now(UTC),
    )


def probe_result_to_snapshot(
    *,
    task_id: str,
    session_id: str,
    capture_id: str,
    surface_kind: SurfaceKind,
    package_name: str | None = None,
    activity: str | None = None,
    screen_id: str | None = None,
    structure_signature: str | None = None,
    image_signature: str | None = None,
    deep_link: str | None = None,
    page_state: str | None = None,
    lifecycle_phase: str | None = None,
    session_event: str | None = None,
    process_alive: bool | None = None,
    safe_area: Mapping[str, Any] | None = None,
    captured_at: datetime | None = None,
) -> StateInvariantSnapshot:
    """把 Probe 结果 Capture 元数据投影为等价格局快照。"""
    return build_app_state_invariant_snapshot(
        task_id=task_id,
        capture_id=capture_id,
        session_id=session_id,
        surface_kind=surface_kind,
        package_name=package_name,
        activity=activity,
        page_state=page_state,
        image_signature=image_signature,
        session_options={
            "deep_link": deep_link,
            "lifecycle_phase": lifecycle_phase or "foreground",
            "session_event": session_event,
            "process_alive": process_alive,
            "screen_id": screen_id,
            "structure_signature": structure_signature,
            "has_ui_tree": True,
            "safe_area": dict(safe_area) if safe_area else None,
        },
        captured_at=captured_at or datetime.now(UTC),
    )
