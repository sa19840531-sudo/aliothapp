"""Android Interaction Runtime 組裝：Executor + CaptureWriter + Demo 工廠。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureWriteRequest,
    ExecuteInteractionRequest,
)
from services.api.app.application.capture_coordinator import (
    CaptureCoordinator,
    InProcessSessionCommandGate,
)
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidationError,
    InteractionCommandValidator,
    TrustedTaskCommandScopeReader,
)
from services.api.app.application.interaction_runtime import (
    InMemoryActionEvidenceBuffer,
    PendingActionEvidence,
    SemanticTargetResolutionError,
)
from services.api.app.domain.exploration import (
    EvidenceExitGateSnapshot,
    EvidenceExitReservationStatus,
    build_evidence_exit_gate_decision,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureKind,
    CaptureProfile,
    CaptureRef,
    CommandPolicy,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
    SurfaceSessionSpec,
    TimedObservationIntent,
    build_action_execution_id,
)
from services.api.app.infrastructure.device.android_runtime_observation import (
    AndroidRuntimeObservation,
)
from services.api.app.infrastructure.device.android_surface_driver import (
    AndroidAdbSurfaceDriver,
)


class InProcessEvidenceExitGate:
    """Android Demo 的确定性 Evidence Exit Gate；正式服务可注入 PostgreSQL 实现。"""

    def __init__(self) -> None:
        self._reservations: set[str] = set()

    async def reserve(self, *, intent, permit):  # noqa: ANN001
        if intent.action_type not in {
            InteractionOperation.NAVIGATE,
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
            InteractionOperation.BACK,
            InteractionOperation.REFRESH,
        }:
            return None
        snapshot = EvidenceExitGateSnapshot(
            task_id=permit.task_id,
            session_id=permit.session_id,
            source_capture_id=permit.current_capture_id or intent.source_capture_id,
            requirement_set_id="android-runtime",
            baseline_pass_manifest_count=1,
            nonterminal_active_session_requirement_count=0,
            nonterminal_inline_evidence_request_count=0,
            evidence_commit_in_progress_count=0,
            evaluated_at=datetime.now(UTC),
        )
        decision = build_evidence_exit_gate_decision(
            action_intent_id=intent.intent_id,
            snapshot=snapshot,
        )
        self._reservations.add(decision.evaluation_id)
        return decision

    async def finalize(self, *, decision, status):  # noqa: ANN001
        if status in {
            EvidenceExitReservationStatus.CONSUMED,
            EvidenceExitReservationStatus.CANCELLED,
            EvidenceExitReservationStatus.EXPIRED,
        }:
            self._reservations.discard(decision.evaluation_id)

    async def expire_stale(self, *, limit: int = 100) -> int:
        del limit
        return 0


class AndroidInteractionExecutor:
    """把 Android ActionIntent 交給 AndroidAdbSurfaceDriver 的唯一執行口。"""

    def __init__(
        self,
        *,
        driver: AndroidAdbSurfaceDriver,
        evidence_buffer: InMemoryActionEvidenceBuffer,
        command_validator: InteractionCommandValidator | None = None,
    ) -> None:
        self._driver = driver
        self._evidence_buffer = evidence_buffer
        self._command_validator = command_validator
        self._execution_registry_lock = asyncio.Lock()
        self._execution_locks: dict[str, asyncio.Lock] = {}
        self._execution_users: dict[str, int] = {}

    async def execute(self, request: ExecuteInteractionRequest) -> InteractionResult:
        intent = request.intent
        action_execution_id = request.action_execution_id or build_action_execution_id(intent)
        async with self._hold_execution(action_execution_id):
            existing = await self._evidence_buffer.get(action_execution_id=action_execution_id)
            if existing is not None:
                if (
                    existing.interaction.intent_id == intent.intent_id
                    and existing.interaction.source_capture_id == request.source_capture_id
                ):
                    return existing.interaction
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.BLOCKED,
                    error_code="ACTION_ALREADY_EXECUTED",
                    error_message="相同幂等动作已经执行，禁止对另一份前 Capture 重放",
                )
            if intent.surface_kind is not SurfaceKind.ANDROID:
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.BLOCKED,
                    error_code="SURFACE_DRIVER_UNAVAILABLE",
                    error_message="当前适配器只支持 Android Surface",
                )
            if _remaining_timeout_seconds(intent.deadline) <= 0:
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.TIMED_OUT,
                    error_code="ACTION_DEADLINE_EXCEEDED",
                    error_message="ActionIntent deadline 已到",
                    retryable=True,
                )
            if self._command_validator is not None:
                trusted_target_texts, target_trust, target_role = (
                    await _read_android_target_semantics(
                        driver=self._driver,
                        intent=intent,
                    )
                )
                try:
                    await self._command_validator.validate_resolved_target_for(
                        CaptureCoordinationCommand(
                            agent_run_id=request.agent_run_id,
                            intent=intent,
                        ),
                        trusted_target_texts=trusted_target_texts,
                        target_trust=target_trust,
                        target_role=target_role,
                        expected_finalization_epoch=request.finalization_epoch,
                    )
                except InteractionCommandValidationError as exc:
                    # UI 目标复核发生在任何 ADB 写操作前；拒绝结果明确标记为
                    # 未调用 Driver，避免把策略阻断误记成设备侧未知结果。
                    return _error_result(
                        request,
                        action_execution_id=action_execution_id,
                        status=InteractionResultStatus.BLOCKED,
                        error_code=exc.error_code,
                        error_message=str(exc),
                        retryable=exc.retryable,
                    )
            try:
                raw = await self._driver.execute(
                    session=self._driver.session,
                    intent=intent,
                    action_execution_id=action_execution_id,
                )
            except SemanticTargetResolutionError as exc:
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.BLOCKED,
                    error_code=exc.error_code,
                    error_message=str(exc),
                    retryable=exc.retryable,
                )
            except Exception:  # noqa: BLE001
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.FAILED,
                    error_code="SURFACE_DRIVER_FAILED",
                    error_message="Surface Driver 执行失败",
                    retryable=True,
                )
            if not raw.succeeded:
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.FAILED,
                    error_code=raw.error_code or "SURFACE_DRIVER_FAILED",
                    error_message="Android Driver 返回失败",
                    retryable=raw.retryable,
                )
            script_recording = await _persist_script_recording(
                task_id=intent.task_id,
                payload=raw.payload,
            )
            if script_recording is not None and script_recording.get("persisted") is not True:
                return _error_result(
                    request,
                    action_execution_id=action_execution_id,
                    status=InteractionResultStatus.FAILED,
                    error_code="SCRIPT_EVIDENCE_PERSIST_FAILED",
                    error_message="录制脚本截图没有写入任务证据",
                    retryable=False,
                )
            state_summary: dict[str, Any] = {"android_payload": dict(raw.payload)}
            if script_recording is not None:
                state_summary["script_recording"] = script_recording
            interaction = InteractionResult(
                task_id=intent.task_id,
                session_id=intent.session_id,
                intent_id=intent.intent_id,
                action_execution_id=action_execution_id,
                source_capture_id=request.source_capture_id,
                status=InteractionResultStatus.EVIDENCE_PENDING,
                state_summary=state_summary,
            )
            await self._evidence_buffer.put(
                PendingActionEvidence(interaction=interaction, browser_result=raw)
            )
            return interaction

    @asynccontextmanager
    async def _hold_execution(self, action_execution_id: str) -> AsyncIterator[None]:
        async with self._execution_registry_lock:
            lock = self._execution_locks.setdefault(action_execution_id, asyncio.Lock())
            self._execution_users[action_execution_id] = (
                self._execution_users.get(action_execution_id, 0) + 1
            )
        try:
            async with lock:
                yield
        finally:
            async with self._execution_registry_lock:
                remaining = self._execution_users[action_execution_id] - 1
                if remaining == 0:
                    self._execution_users.pop(action_execution_id, None)
                    self._execution_locks.pop(action_execution_id, None)
                else:
                    self._execution_users[action_execution_id] = remaining


class AndroidCaptureWriter:
    """Capture Writer：Driver 截圖 → PNG 落 shots_dir + Evidence Store；回傳 artifact_id。"""

    def __init__(
        self,
        *,
        driver: AndroidAdbSurfaceDriver,
        shots_dir: Path,
        path_index: dict[str, Path] | None = None,
        artifact_service: Any | None = None,
        artifact_refs: dict[str, Any] | None = None,
    ) -> None:
        self._driver = driver
        self._shots_dir = shots_dir
        self._path_index = path_index if path_index is not None else {}
        self._artifact_service = artifact_service
        self._artifact_refs = artifact_refs if artifact_refs is not None else {}
        self._shots_dir.mkdir(parents=True, exist_ok=True)

    async def capture(self, request: CaptureWriteRequest) -> CaptureRef:
        if _remaining_timeout_seconds(request.deadline) <= 0:
            raise TimeoutError("Capture deadline 已到")
        profile = CaptureProfile(
            profile_id=f"android-{request.phase.value}",
            before=CaptureDirective(
                role=request.directive.role,
                artifact_types=request.directive.artifact_types,
            ),
            after=CaptureDirective(
                role=request.directive.role,
                artifact_types=request.directive.artifact_types,
            ),
        )
        raw = await self._driver.capture(session=self._driver.session, profile=profile)
        png = next(
            (item.content for item in raw.artifacts if item.artifact_type == "screenshot"),
            None,
        )
        if not isinstance(png, (bytes, bytearray)) or len(png) < 100:
            raise RuntimeError("Android Capture 未取得有效截图")
        png_bytes = bytes(png)
        page_capture_id = f"android-cap-{uuid4().hex[:16]}"
        artifact_id = str(uuid4())
        service = self._artifact_service
        if service is None:
            from services.api.app.application.app_screenshot_artifacts import (
                get_app_screenshot_artifact_service,
            )

            service = get_app_screenshot_artifact_service()
        ref = await service.persist_png(
            task_id=request.task_id,
            page_capture_id=page_capture_id,
            png=png_bytes,
            artifact_id=artifact_id,
        )
        dest = self._shots_dir / f"{page_capture_id}.png"
        dest.write_bytes(png_bytes)
        self._path_index[page_capture_id] = dest
        self._artifact_refs[artifact_id] = ref
        self._artifact_refs[page_capture_id] = ref
        return CaptureRef(
            task_id=request.task_id,
            session_id=request.session_id,
            capture_id=page_capture_id,
            surface_kind=request.surface_kind,
            capture_kind=CaptureKind.APP,
            role=request.directive.role,
            artifact_ids=(ref.artifact_id,),
            parent_capture_id=request.parent_capture_id,
            action_execution_id=request.action_execution_id,
        )


@dataclass
class AndroidCaptureRuntime:
    """Android Interaction Runtime：對外只暴露 Coordinator；Driver 僅內部持有。

    對齊架構文件 §15 / 路線圖「Driver 只能由 Runtime 持有」。
    """

    coordinator: CaptureCoordinator
    session_command_gate: Any
    capture_writer: AndroidCaptureWriter
    path_index: dict[str, Path]
    _driver: AndroidAdbSurfaceDriver

    async def start_session(self, spec: SurfaceSessionSpec) -> Any:
        return await self._driver.start_session(spec)

    async def close_session(
        self,
        *,
        task_id: str,
        session_id: str,
        reason: str = "runtime_close",
    ) -> None:
        try:
            if self._driver._session is not None:  # noqa: SLF001 - Runtime 關閉內部 session
                await self._driver.close_session(session=self._driver.session, reason=reason)
        finally:
            await self.session_command_gate.release_session(
                task_id=task_id,
                session_id=session_id,
            )


@dataclass
class MobileDemoAndroidRuntime:
    """mobile-demo agent_workflow 使用的 Android Runtime 句柄。

    禁止呼叫端直接取用 App Driver；截圖／動作一律經 CaptureWriter / CaptureCoordinator。
    排期第 13 列：observe 與 coordinate 必須經同一 SessionCommandGate 串行。
    """

    task_id: str
    session_id: str
    coordinator: CaptureCoordinator
    session_command_gate: InProcessSessionCommandGate
    capture_writer: AndroidCaptureWriter
    path_index: dict[str, Path]
    seed_capture_id: str
    last_capture_id: str
    _driver: AndroidAdbSurfaceDriver
    last_artifact_ref: Any | None = None
    _observation_sequence: int = 0
    last_runtime_observation: AndroidRuntimeObservation | None = None
    _explored_safe_tab_ids: set[str] = field(default_factory=set, repr=False)

    async def observe_to_file(self, dest: Path) -> str:
        """经 CaptureCoordinator.capture_observation（真实 Session Gate）串行截图。"""
        self._observation_sequence += 1
        command_sequence = self._observation_sequence
        ref = await self.coordinator.capture_observation(
            agent_run_id=f"mobile-demo:{self.session_id}",
            intent=TimedObservationIntent(
                task_id=self.task_id,
                session_id=self.session_id,
                phase_run_id=f"mobile-demo:{self.session_id}",
                decision_id=f"mobile-demo-observe:{command_sequence}",
                observation_id=f"observation:{self.session_id}:{command_sequence}",
                source_capture_id=self.last_capture_id,
                surface_kind=SurfaceKind.ANDROID,
                delay_ms=0,
                directive=CaptureDirective(
                    role="observation",
                    artifact_types=("screenshot",),
                    inherit_parent_semantics=True,
                ),
                deadline=datetime.now(UTC) + timedelta(seconds=60),
            ),
        )
        src = self.path_index.get(ref.capture_id)
        if src is None or not src.is_file():
            raise RuntimeError("observe 截图失败")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dest.resolve():
            dest.write_bytes(src.read_bytes())
            self.path_index[ref.capture_id] = dest
        self.last_capture_id = ref.capture_id
        if ref.artifact_ids:
            self.last_artifact_ref = self.capture_writer._artifact_refs.get(  # noqa: SLF001
                ref.artifact_ids[0]
            )
        await self.observe_runtime_state()
        return ref.capture_id

    async def observe_runtime_state(self) -> AndroidRuntimeObservation:
        """在同一 Session Command Gate 内读取设备 UI 层级。"""
        async with self.session_command_gate.hold(
            task_id=self.task_id,
            session_id=self.session_id,
            surface_kind=SurfaceKind.ANDROID,
        ):
            observation = await self._driver.observe_runtime_state(
                explored_safe_tab_ids=self._explored_safe_tab_ids,
            )
        self.last_runtime_observation = observation
        return observation

    def mark_explored_point(
        self,
        *,
        x: float,
        y: float,
        coordinate_space: str = "canonical_px",
        base_width: int = 1080,
        base_height: int = 1920,
    ) -> str | None:
        """仅依据最近一次真实 UI 层级，把实际点击的安全底栏节点记为已探索。"""
        observation = self.last_runtime_observation
        if observation is None or not observation.available:
            return None
        node = observation.safe_tab_at_point(
            x,
            y,
            coordinate_space=coordinate_space,
            base_width=base_width,
            base_height=base_height,
        )
        if node is None:
            return None
        self._explored_safe_tab_ids.add(node.node_id)
        return node.node_id

    async def coordinate_intent(self, intent: ActionIntent) -> Any:
        from services.api.app.application.capture_coordination import (
            CaptureCoordinationCommand,
        )

        result = await self.coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id=f"mobile-demo:{self.session_id}",
                intent=intent,
            )
        )
        self.last_capture_id = result.after_capture.capture_id
        return result

    async def close(self) -> None:
        try:
            if self._driver._session is not None:  # noqa: SLF001 - Runtime 内部关闭 session
                await self._driver.close_session(session=self._driver.session, reason="demo_done")
        finally:
            await self.session_command_gate.release_session(
                task_id=self.task_id,
                session_id=self.session_id,
            )

    async def cancel(self, *, reason: str = "canceled") -> None:
        """排期第 13 列：取消会话并清 Session Lease，旧动作不得再写回。"""
        await self.session_command_gate.cancel_session(
            task_id=self.task_id,
            session_id=self.session_id,
            reason=reason,
        )
        try:
            await self._driver.close_session(
                session=self._driver.session,
                reason=f"canceled:{reason}",
            )
        except Exception:  # noqa: BLE001 - 取消必须以清租约为准
            pass


def _assemble_android_runtime_parts(
    *,
    backend: str,
    shots_dir: Path,
    ld_index: int = 0,
    log: Callable[[str], None] | None = None,
    ops=None,  # noqa: ANN001 - 測試可注入 AdbDeviceOps
    artifact_service=None,  # noqa: ANN001 - 測試可注入 AppScreenshotArtifactService
    command_scope_reader: TrustedTaskCommandScopeReader | None = None,
    action_execution_repository: Any | None = None,
    action_recovery_captures: Any | None = None,
) -> tuple[
    AndroidAdbSurfaceDriver,
    AndroidInteractionExecutor,
    AndroidCaptureWriter,
    CaptureCoordinator,
    InProcessSessionCommandGate,
    dict[str, Path],
]:
    path_index: dict[str, Path] = {}
    artifact_refs: dict[str, Any] = {}
    if action_execution_repository is not None and action_recovery_captures is None:
        raise ValueError(
            "Android 动作账本启用时必须同时提供可验证 Capture 恢复读取器"
        )
    driver = AndroidAdbSurfaceDriver(
        backend=backend,
        ld_index=ld_index,
        log=log,
        capture_dir=shots_dir / "_runtime_raw",
        ops=ops,
    )
    evidence_buffer = InMemoryActionEvidenceBuffer()
    command_validator = InteractionCommandValidator(
        policy=CommandPolicy.default(),
        scope_reader=command_scope_reader,
    )
    executor = AndroidInteractionExecutor(
        driver=driver,
        evidence_buffer=evidence_buffer,
        command_validator=command_validator,
    )
    writer = AndroidCaptureWriter(
        driver=driver,
        shots_dir=shots_dir / "_runtime_caps",
        path_index=path_index,
        artifact_service=artifact_service,
        artifact_refs=artifact_refs,
    )
    gate = InProcessSessionCommandGate()
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=InProcessEvidenceExitGate(),
        session_command_gate=gate,
        action_execution_repository=action_execution_repository,
        action_recovery_captures=action_recovery_captures,
        command_validator=command_validator,
    )
    return driver, executor, writer, coordinator, gate, path_index


def build_default_android_capture_runtime(
    *,
    backend: str,
    shots_dir: Path,
    ld_index: int = 0,
    log: Callable[[str], None] | None = None,
    ops=None,  # noqa: ANN001
    artifact_service=None,  # noqa: ANN001
    command_scope_reader: TrustedTaskCommandScopeReader | None = None,
    action_execution_repository: Any | None = None,
    action_recovery_captures: Any | None = None,
) -> AndroidCaptureRuntime:
    """組裝正式 Android Interaction Runtime（InProcess gate；Driver 僅 Runtime 內部持有）。

    對齊 Web 的 ``build_default_web_capture_runtime``；截圖本體寫入 Evidence Store（PNG 二進位），
    跨模組以 artifact_id + URL 引用。
    """
    driver, _executor, writer, coordinator, gate, path_index = _assemble_android_runtime_parts(
        backend=backend,
        shots_dir=shots_dir,
        ld_index=ld_index,
        log=log,
        ops=ops,
        artifact_service=artifact_service,
        command_scope_reader=command_scope_reader,
        action_execution_repository=action_execution_repository,
        action_recovery_captures=action_recovery_captures,
    )
    return AndroidCaptureRuntime(
        coordinator=coordinator,
        session_command_gate=gate,
        capture_writer=writer,
        path_index=path_index,
        _driver=driver,
    )


def build_mobile_demo_android_runtime(
    *,
    task_id: str,
    session_id: str,
    backend: str,
    shots_dir: Path,
    ld_index: int = 0,
    log: Callable[[str], None] | None = None,
    ops=None,  # noqa: ANN001 - 測試可注入 AdbDeviceOps
    artifact_service=None,  # noqa: ANN001
    command_scope_reader: TrustedTaskCommandScopeReader | None = None,
    action_execution_repository: Any | None = None,
    action_recovery_captures: Any | None = None,
) -> MobileDemoAndroidRuntime:
    """組裝 Demo 用 Android CaptureCoordinator Runtime（同步工廠；start 需另呼叫）。"""
    driver, _executor, writer, coordinator, gate, path_index = _assemble_android_runtime_parts(
        backend=backend,
        shots_dir=shots_dir,
        ld_index=ld_index,
        log=log,
        ops=ops,
        artifact_service=artifact_service,
        command_scope_reader=command_scope_reader,
        action_execution_repository=action_execution_repository,
        action_recovery_captures=action_recovery_captures,
    )
    seed = f"android-seed:{session_id}"
    return MobileDemoAndroidRuntime(
        task_id=task_id,
        session_id=session_id,
        coordinator=coordinator,
        session_command_gate=gate,
        capture_writer=writer,
        path_index=path_index,
        seed_capture_id=seed,
        last_capture_id=seed,
        _driver=driver,
    )


async def start_mobile_demo_android_runtime(
    runtime: MobileDemoAndroidRuntime,
) -> MobileDemoAndroidRuntime:
    """啟動 Android Surface Session。"""
    await runtime._driver.start_session(
        SurfaceSessionSpec(
            task_id=runtime.task_id,
            session_id=runtime.session_id,
            surface_kind=SurfaceKind.ANDROID,
            device_ref=f"demo:{runtime._driver._backend}",
            options={"backend": runtime._driver._backend},
        )
    )
    return runtime


def build_android_action_intent(
    *,
    runtime: MobileDemoAndroidRuntime,
    intent_id: str,
    action_type: InteractionOperation,
    purpose: str,
    semantic_target: str | None,
    parameters: dict[str, Any] | None = None,
    deadline_seconds: float = 60.0,
) -> ActionIntent:
    """組裝 Demo 用 Android ActionIntent。"""
    targeted = action_type in {
        InteractionOperation.TAP,
        InteractionOperation.CLICK,
        InteractionOperation.INPUT,
        InteractionOperation.HOVER,
        InteractionOperation.NAVIGATE,
    }
    return ActionIntent(
        task_id=runtime.task_id,
        session_id=runtime.session_id,
        intent_id=intent_id,
        idempotency_key=f"{runtime.task_id}:{intent_id}",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=runtime.last_capture_id,
        action_type=action_type,
        semantic_target=semantic_target if targeted or semantic_target else None,
        purpose=purpose,
        parameters=parameters or {},
        risk_level=(
            InteractionRiskLevel.HIGH
            if action_type
            in {
                InteractionOperation.TAP,
                InteractionOperation.CLICK,
                InteractionOperation.INPUT,
                InteractionOperation.NAVIGATE,
            }
            else InteractionRiskLevel.MEDIUM
            if action_type in {InteractionOperation.SCROLL, InteractionOperation.BACK}
            else InteractionRiskLevel.LOW
        ),
        capture_profile=_screenshot_profile("android-demo"),
        deadline=datetime.now(UTC) + timedelta(seconds=deadline_seconds),
    )


def _screenshot_profile(profile_id: str) -> CaptureProfile:
    return CaptureProfile(
        profile_id=profile_id,
        before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
        after=CaptureDirective(role="after_action", artifact_types=("screenshot",)),
    )


def _remaining_timeout_seconds(deadline: datetime) -> float:
    return (deadline - datetime.now(UTC)).total_seconds()


async def _read_android_target_semantics(
    *,
    driver: AndroidAdbSurfaceDriver,
    intent: ActionIntent,
) -> tuple[tuple[str, ...], str | None, str | None]:
    """读取 Driver 刚观察到的 UI Hierarchy，返回可信目标语义。

    这里不把 ActionIntent 的 semantic_target 当作可信来源；只有当前设备
    UI Hierarchy 唯一命中的节点才能进入目标风险判定。观察失败时返回空
    语义，命令策略仍会对 Agent 自报的明显危险文案执行失败关闭。
    """

    if intent.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.INPUT,
    }:
        return (), None, None
    try:
        observation = await driver.observe_runtime_state()
    except Exception:  # noqa: BLE001 - 观察失败交由策略保守处理
        return (), None, None
    if not observation.available:
        return (), None, None
    node = _find_android_target_node(observation, intent)
    if node is None:
        return (), None, None
    texts = tuple(
        value
        for value in (node.text, node.content_desc, node.resource_id, node.class_name)
        if isinstance(value, str) and value.strip()
    )
    return texts, "android_ui_hierarchy", ("dismiss" if node.is_dismiss_control else None)


def _find_android_target_node(observation: object, intent: ActionIntent):
    """按语义或动作坐标选择当前 UI Hierarchy 的节点，不做模糊猜测。"""

    if not getattr(observation, "available", False):
        return None
    nodes = tuple(getattr(observation, "nodes", ()) or ())
    semantic = (intent.semantic_target or "").strip()
    if semantic:
        matches = tuple(
            node
            for node in nodes
            if getattr(node, "visible_to_user", False)
            and getattr(node, "enabled", False)
            and getattr(node, "clickable", False)
            and semantic in {
                str(getattr(node, "text", "") or "").strip(),
                str(getattr(node, "content_desc", "") or "").strip(),
            }
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None
    point = _android_intent_point(intent.parameters)
    if point is None:
        return None
    x, y, space, base_width, base_height = point
    return observation.node_at_point(
        x,
        y,
        coordinate_space=space,
        base_width=base_width,
        base_height=base_height,
    )


async def _persist_script_recording(
    *,
    task_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """把這次腳本寫出的 PNG 存進任務證據，只留 artifact id。"""

    recording = next(
        (
            value
            for key, value in payload.items()
            if isinstance(key, str) and key.endswith("_recording") and isinstance(value, Mapping)
        ),
        None,
    )
    if recording is None:
        return None
    shots = recording.get("shots")
    shot_list = shots if isinstance(shots, list) else []
    from services.api.app.application.app_screenshot_artifacts import (
        get_app_screenshot_artifact_service,
    )

    service = get_app_screenshot_artifact_service()
    stored: list[dict[str, str]] = []
    for shot in shot_list:
        if not isinstance(shot, Mapping):
            return {"persisted": False, "verified": False, "shots": stored, "operations": []}
        path_text = shot.get("path")
        name = str(shot.get("name") or "")
        if not isinstance(path_text, str) or not path_text.strip():
            return {"persisted": False, "verified": False, "shots": stored, "operations": []}
        try:
            png = Path(path_text).read_bytes()
        except OSError:
            return {"persisted": False, "verified": False, "shots": stored, "operations": []}
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            return {"persisted": False, "verified": False, "shots": stored, "operations": []}
        ref = await service.persist_png(
            task_id=task_id,
            page_capture_id=f"script-shot-{name or uuid4().hex[:8]}",
            png=png,
        )
        stored.append({"name": name, "artifact_id": ref.artifact_id})
    operations = recording.get("operations")
    verification = recording.get("verification")
    return {
        "persisted": True,
        "verified": isinstance(verification, Mapping) and verification.get("verified") is True,
        "shots": stored,
        "operations": list(operations) if isinstance(operations, list) else [],
    }


def _android_intent_point(
    parameters: Mapping[str, object],
) -> tuple[float, float, str, int, int] | None:
    raw_point = parameters.get("resolved_target")
    if not isinstance(raw_point, Mapping):
        raw_point = parameters.get("point")
    if not isinstance(raw_point, Mapping):
        if "x" not in parameters or "y" not in parameters:
            return None
        raw_point = parameters
    try:
        x = float(raw_point["x"])
        y = float(raw_point["y"])
    except (KeyError, TypeError, ValueError):
        return None
    space = str(
        raw_point.get("space")
        or parameters.get("coordinate_space")
        or parameters.get("space")
        or "canonical_px"
    )
    display = parameters.get("display")
    display_mapping = display if isinstance(display, Mapping) else {}
    try:
        base_width = int(
            display_mapping.get("width")
            or parameters.get("base_width")
            or 1080
        )
        base_height = int(
            display_mapping.get("height")
            or parameters.get("base_height")
            or 1920
        )
    except (TypeError, ValueError):
        return None
    if base_width <= 0 or base_height <= 0:
        return None
    return x, y, space, base_width, base_height


def _error_result(
    request: ExecuteInteractionRequest,
    *,
    action_execution_id: str,
    status: InteractionResultStatus,
    error_code: str,
    error_message: str,
    retryable: bool = False,
) -> InteractionResult:
    return InteractionResult(
        task_id=request.intent.task_id,
        session_id=request.intent.session_id,
        intent_id=request.intent.intent_id,
        action_execution_id=action_execution_id,
        source_capture_id=request.source_capture_id,
        status=status,
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
    )
