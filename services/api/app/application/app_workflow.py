"""Android APK 安装、打开、首屏截图与 APP_READY 的确定性编排。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.api.app.application.app_bootstrap_lease import (
    AppBootstrapLeaseGuard,
    AppBootstrapLeaseLostError,
)
from services.api.app.application.app_bootstrap_state import (
    AppBootstrapRepository,
    CancelAppBootstrapCommand,
    FailAppBootstrapCommand,
    RenewAppBootstrapLeaseCommand,
    TransitionAppBootstrapCommand,
)
from services.api.app.application.app_capture_persistence import (
    AppCapturePersistenceService,
    PersistedAppCapture,
)
from services.api.app.application.device_pool import (
    DeviceLease,
    DeviceLeaseGuard,
    DeviceLeaseLostError,
    DeviceLeasePool,
    DevicePoolError,
    get_device_lease,
    get_runtime_device_pool,
    is_postgres_device_lease_backend,
    maybe_power_off_cloud_phone_for_lease,
    renew_device_lease,
    resolve_morelogin_env_id_for_lease,
)
from services.api.app.application.device_wait import claim_device_lease_with_wait
from services.api.app.application.surface_sessions import SurfaceSessionRepository
from services.api.app.domain.interaction import (
    AppBootstrapPort,
    AppBootstrapSnapshot,
    AppBootstrapStage,
    AppInstallRequest,
    AppOpenRequest,
    AppReadyHandoff,
    AppWorkflowFailure,
    CapabilityMismatchError,
    CapabilityStatus,
    CaptureDirective,
    CaptureProfile,
    SurfaceCapabilityManifest,
    SurfaceDriver,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSnapshot,
    SurfaceSessionSpec,
    SurfaceSessionStatus,
    build_android_screenshot_only_manifest,
)
from services.api.app.domain.interaction.surface_driver import app_primary_surface_ref
from services.api.app.domain.interaction.validation import require_text
from services.api.app.domain.task.platform_build import PlatformBuild


@dataclass(frozen=True, kw_only=True)
class RunAppWorkflowCommand:
    """一次可重放 App Bootstrap 的稳定身份与 Workflow 租约。"""

    task_id: str
    agent_run_id: str
    session_id: str
    platform_build_ref: str
    lease_owner: str
    lease_token: str
    lease_seconds: int = 300
    session_owner: str = "app-workflow"
    device_ref: str | None = None
    # 任务备注：传给 Official Session，供同名降版扫描授权审计
    task_remark: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "agent_run_id",
            "session_id",
            "platform_build_ref",
            "lease_owner",
            "lease_token",
            "session_owner",
        ):
            require_text(getattr(self, field_name), field_name)
        if not self.platform_build_ref.startswith("app-build:"):
            raise ValueError("platform_build_ref 必须使用 app-build:<id>")
        if self.device_ref is not None:
            require_text(self.device_ref, "device_ref")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or self.lease_seconds <= 0
        ):
            raise ValueError("lease_seconds 必须大于 0")


class VerifiedPlatformBuildReader(Protocol):
    async def get_verified_by_ref(
        self,
        platform_build_ref: str,
    ) -> PlatformBuild | None: ...


class AppReadyPublisher(Protocol):
    """数据库 APP_READY 提交后的幂等应用内通知端口。"""

    async def publish(self, handoff: AppReadyHandoff) -> None: ...


class AppWorkflowCancellation(Protocol):
    async def is_cancel_requested(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> bool: ...


class NoopAppReadyPublisher:
    """只依赖事务 Outbox 时使用的空应用内通知实现。"""

    async def publish(self, handoff: AppReadyHandoff) -> None:
        del handoff


class _NeverCanceled:
    async def is_cancel_requested(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> bool:
        del task_id, agent_run_id
        return False


class _NoopAsyncContext:
    """没有设备池租约时保持旧测试和 Web 调用的异步上下文形状。"""

    async def __aenter__(self) -> _NoopAsyncContext:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback


class AppQcWorkflow:
    """不让 Agent 参与安装/打开副作用的可恢复 App Bootstrap 状态机。"""

    def __init__(
        self,
        *,
        platform_builds: VerifiedPlatformBuildReader,
        surface_sessions: SurfaceSessionRepository,
        bootstraps: AppBootstrapRepository,
        bootstrap_port: AppBootstrapPort,
        driver: SurfaceDriver,
        captures: AppCapturePersistenceService,
        publisher: AppReadyPublisher,
        cancellation: AppWorkflowCancellation | None = None,
        cancellation_poll_interval_seconds: float = 1.0,
    ) -> None:
        if cancellation_poll_interval_seconds <= 0:
            raise ValueError("取消轮询间隔必须大于 0")
        self._platform_builds = platform_builds
        self._surface_sessions = surface_sessions
        self._bootstraps = bootstraps
        self._bootstrap_port = bootstrap_port
        self._driver = driver
        self._captures = captures
        self._publisher = publisher
        self._cancellation = cancellation or _NeverCanceled()
        self._cancellation_poll_interval_seconds = cancellation_poll_interval_seconds
        self._attached_session_ids: set[str] = set()
        self._device_claims: dict[str, tuple[DeviceLeasePool, DeviceLease]] = {}

    async def run(self, command: RunAppWorkflowCommand) -> AppReadyHandoff:
        if not isinstance(command, RunAppWorkflowCommand):
            raise TypeError("command 必须为 RunAppWorkflowCommand")
        existing = await self._bootstraps.get(session_id=command.session_id)
        if existing is not None and existing.stage.is_terminal:
            return await self._resume_terminal(command, existing)
        if await self._is_canceled(command):
            raise AppWorkflowFailure(
                "App Workflow 在建立 Session 前已取消",
                stage=AppBootstrapStage.CANCELED,
                error_code="WORKFLOW_CANCELED",
                retryable=False,
            )

        device_lease = None
        runtime_device_pool: DeviceLeasePool | None = None
        effective_device_ref = command.device_ref
        # PostgreSQL 池下即使未指定 preferred 也必须先 claim，池满不得进入安装。
        must_claim_device = (
            command.device_ref is not None or is_postgres_device_lease_backend()
        )
        if must_claim_device:
            try:
                runtime_device_pool = await get_runtime_device_pool()
                existing_session = await self._get_existing_session(command.session_id)
                if existing_session is not None and existing_session.device_lease_id is None:
                    raise AppWorkflowFailure(
                        "已有 App Surface Session 缺少设备租约身份，拒绝重新领取",
                        stage=AppBootstrapStage.ACQUIRING_SESSION,
                        error_code="APP_DEVICE_LEASE_LOST",
                        retryable=True,
                    )
                if existing_session is not None:
                    device_lease = await self._get_pool_lease(
                        runtime_device_pool,
                        lease_id=existing_session.device_lease_id,
                        session_id=command.session_id,
                    )
                    device_lease = await _continue_same_device_lease(
                        device_lease,
                        lease_seconds=max(300, int(command.lease_seconds)),
                    )
                    if device_lease is None or device_lease.state not in {"claimed", "active"}:
                        raise AppWorkflowFailure(
                            "已有 App Surface Session 的设备租约不可恢复",
                            stage=AppBootstrapStage.ACQUIRING_SESSION,
                            error_code="APP_DEVICE_LEASE_LOST",
                            retryable=True,
                        )
                else:
                    device_lease = await claim_device_lease_with_wait(
                        task_id=command.task_id,
                        session_id=command.session_id,
                        preferred_device_ref=command.device_ref,
                        lease_seconds=max(300, int(command.lease_seconds)),
                        lease_owner=command.lease_owner,
                        is_canceled=lambda: self._is_canceled(command),
                    )
            except AppWorkflowFailure:
                raise
            except DevicePoolError as exc:
                raise AppWorkflowFailure(
                    str(exc),
                    stage=AppBootstrapStage.ACQUIRING_SESSION,
                    error_code=exc.error_code,
                    retryable=exc.retryable,
                ) from exc
            except Exception as exc:  # noqa: BLE001 - provider/DB 故障统一 fail closed
                raise AppWorkflowFailure(
                    "App 设备池不可用",
                    stage=AppBootstrapStage.ACQUIRING_SESSION,
                    error_code="DEVICE_POOL_UNAVAILABLE",
                    retryable=True,
                ) from exc
            effective_device_ref = device_lease.device_ref
            self._device_claims[command.session_id] = (runtime_device_pool, device_lease)

        session_options = _session_options_from_device_ref(
            effective_device_ref,
            morelogin_env_id=(
                resolve_morelogin_env_id_for_lease(device_lease)
                if device_lease is not None
                else ""
            ),
        )
        if device_lease is not None:
            session_options["device_lease_id"] = device_lease.lease_id
        remark = str(command.task_remark or "").strip()
        if remark:
            session_options["task_remark"] = remark

        spec = SurfaceSessionSpec(
            task_id=command.task_id,
            session_id=command.session_id,
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref=command.platform_build_ref,
            device_ref=effective_device_ref,
            device_lease_id=device_lease.lease_id if device_lease is not None else None,
            device_lease_fencing_token=(
                device_lease.fencing_token if device_lease is not None else None
            ),
            account_environment_ref=getattr(command, "account_environment_ref", None),
            options=session_options,
        )
        try:
            session_snapshot = await self._surface_sessions.register_starting(
                spec=spec,
                session_owner=command.session_owner,
                primary_surface_ref=app_primary_surface_ref(command.session_id),
            )
        except BaseException:
            if device_lease is not None:
                await self._release_device_claim(command.session_id, quarantine=False)
            raise
        snapshot = await self._bootstraps.get_or_create(
            task_id=command.task_id,
            session_id=command.session_id,
            platform_build_ref=command.platform_build_ref,
            lease_owner=command.lease_owner,
            lease_token=command.lease_token,
            lease_seconds=command.lease_seconds,
        )
        if snapshot.stage.is_terminal:
            if snapshot.stage is not AppBootstrapStage.APP_READY:
                await self._release_device_claim(command.session_id, quarantine=True)
            return await self._resume_terminal(command, snapshot)
        lease_guard = AppBootstrapLeaseGuard(
            self._bootstraps,
            RenewAppBootstrapLeaseCommand(
                task_id=command.task_id,
                session_id=command.session_id,
                platform_build_ref=command.platform_build_ref,
                lease_owner=command.lease_owner,
                lease_token=command.lease_token,
                lease_seconds=command.lease_seconds,
            ),
            renew_interval_seconds=max(1, command.lease_seconds // 3),
        )
        device_context = (
            DeviceLeaseGuard(
                pool=runtime_device_pool,
                lease=device_lease,
                lease_seconds=max(300, int(command.lease_seconds)),
            )
            if runtime_device_pool is not None and device_lease is not None
            else _NoopAsyncContext()
        )
        try:
            async with lease_guard, device_context:
                build = await self._platform_builds.get_verified_by_ref(
                    command.platform_build_ref
                )
                if build is None:
                    raise AppWorkflowFailure(
                        "APK Build 不存在或尚未 verified",
                        stage=AppBootstrapStage.RESOLVING_BUILD,
                        error_code="PACKAGE_MISSING",
                        retryable=False,
                    )
                manifest = await self._require_bootstrap_capabilities(lease_guard)
                try:
                    await lease_guard.assert_healthy()
                    session_ref = await self._driver.start_session(spec)
                    await lease_guard.assert_healthy()
                    self._attached_session_ids.add(command.session_id)
                except AppBootstrapLeaseLostError:
                    raise
                except Exception as exc:
                    raise _workflow_failure(
                        exc,
                        stage=AppBootstrapStage.ACQUIRING_SESSION,
                        default_code="SESSION_START_FAILED",
                    ) from exc
                _validate_session_ref(session_ref, spec)

                while True:
                    await lease_guard.assert_healthy()
                    if snapshot.stage.is_terminal:
                        return await self._resume_terminal(command, snapshot)
                    if await self._is_canceled(command):
                        await self._cancel(
                            command=command,
                            snapshot=snapshot,
                            session_ref=session_ref,
                            session_snapshot=session_snapshot,
                            lease_guard=lease_guard,
                        )
                    if snapshot.stage is AppBootstrapStage.VALIDATING:
                        snapshot = await self._transition(
                            command,
                            snapshot,
                            AppBootstrapStage.ACQUIRING_SESSION,
                        )
                        continue
                    if snapshot.stage is AppBootstrapStage.ACQUIRING_SESSION:
                        snapshot = await self._transition(
                            command,
                            snapshot,
                            AppBootstrapStage.RESOLVING_BUILD,
                        )
                        continue
                    if snapshot.stage is AppBootstrapStage.RESOLVING_BUILD:
                        install_request = _install_request(command, build)
                        snapshot = await self._transition(
                            command,
                            snapshot,
                            AppBootstrapStage.INSTALLING,
                            install_request_id=install_request.idempotency_key,
                            install_attempt=snapshot.install_attempt + 1,
                        )
                        snapshot = await self._install(
                            command=command,
                            snapshot=snapshot,
                            session_ref=session_ref,
                            session_snapshot=session_snapshot,
                            request=install_request,
                            lease_guard=lease_guard,
                        )
                        continue
                    if snapshot.stage is AppBootstrapStage.INSTALLING:
                        install_request = _install_request(command, build)
                        snapshot = await self._transition(
                            command,
                            snapshot,
                            AppBootstrapStage.INSTALLING,
                            install_request_id=(
                                snapshot.install_request_id or install_request.idempotency_key
                            ),
                            install_attempt=snapshot.install_attempt + 1,
                        )
                        snapshot = await self._install(
                            command=command,
                            snapshot=snapshot,
                            session_ref=session_ref,
                            session_snapshot=session_snapshot,
                            request=install_request,
                            lease_guard=lease_guard,
                        )
                        continue
                    if snapshot.stage is AppBootstrapStage.INSTALLED:
                        open_request = _open_request(command, snapshot)
                        snapshot = await self._transition(
                            command,
                            snapshot,
                            AppBootstrapStage.OPENING,
                            open_request_id=open_request.idempotency_key,
                            open_attempt=snapshot.open_attempt + 1,
                        )
                        snapshot = await self._open(
                            command=command,
                            snapshot=snapshot,
                            session_ref=session_ref,
                            session_snapshot=session_snapshot,
                            request=open_request,
                            lease_guard=lease_guard,
                        )
                        continue
                    if snapshot.stage is AppBootstrapStage.OPENING:
                        open_request = _open_request(command, snapshot)
                        snapshot = await self._transition(
                            command,
                            snapshot,
                            AppBootstrapStage.OPENING,
                            open_request_id=(
                                snapshot.open_request_id or open_request.idempotency_key
                            ),
                            open_attempt=snapshot.open_attempt + 1,
                        )
                        snapshot = await self._open(
                            command=command,
                            snapshot=snapshot,
                            session_ref=session_ref,
                            session_snapshot=session_snapshot,
                            request=open_request,
                            lease_guard=lease_guard,
                        )
                        continue
                    if snapshot.stage is AppBootstrapStage.OPENED:
                        snapshot = await self._transition(
                            command,
                            snapshot,
                            AppBootstrapStage.BOOTSTRAP_CAPTURING,
                        )
                        continue
                    if snapshot.stage is AppBootstrapStage.BOOTSTRAP_CAPTURING:
                        return await self._capture_and_activate(
                            command=command,
                            snapshot=snapshot,
                            session_ref=session_ref,
                            session_snapshot=session_snapshot,
                            manifest=manifest,
                            lease_guard=lease_guard,
                        )
                    raise AppWorkflowFailure(
                        "App Bootstrap 包含未知非终态",
                        stage=snapshot.stage,
                        error_code="BOOTSTRAP_STATE_INVALID",
                        retryable=False,
                    )
        except (AppBootstrapLeaseLostError, DeviceLeaseLostError):
            await self._release_device_claim(command.session_id, quarantine=True)
            raise

    async def _require_bootstrap_capabilities(
        self,
        lease_guard: AppBootstrapLeaseGuard,
    ) -> SurfaceCapabilityManifest:
        await lease_guard.assert_healthy()
        manifest = self._driver.describe_capabilities()
        await lease_guard.assert_healthy()
        try:
            if manifest.surface_kind is not SurfaceKind.ANDROID:
                raise CapabilityMismatchError("App Workflow 只接受 Android Capability")
            missing_actions = [
                name
                for name in ("session", "install", "open")
                if manifest.actions.get(name) is None
                or manifest.actions[name].status is not CapabilityStatus.READY
            ]
            screenshot = manifest.artifacts.get("screenshot")
            if screenshot is None or screenshot.status is not CapabilityStatus.READY:
                missing_actions.append("artifact:screenshot")
            if missing_actions:
                raise CapabilityMismatchError(
                    "Adapter 缺少 App Bootstrap ready 能力："
                    + ", ".join(missing_actions)
                )
        except CapabilityMismatchError as exc:
            raise AppWorkflowFailure(
                str(exc),
                stage=AppBootstrapStage.VALIDATING,
                error_code="CAPABILITY_MISMATCH",
                retryable=False,
            ) from exc
        return manifest

    async def _install(
        self,
        *,
        command: RunAppWorkflowCommand,
        snapshot: AppBootstrapSnapshot,
        session_ref: SurfaceSessionRef,
        session_snapshot: SurfaceSessionSnapshot,
        request: AppInstallRequest,
        lease_guard: AppBootstrapLeaseGuard,
    ) -> AppBootstrapSnapshot:
        # 延迟导入以避免取消辅助模块反向导入 App Workflow 造成循环依赖。
        from services.api.app.application.app_exploration_cancellation import (
            AppExplorationCanceledError,
            run_app_exploration_operation,
        )

        try:
            await lease_guard.assert_healthy()
            result = await run_app_exploration_operation(
                self._bootstrap_port.ensure_installed(request),
                cancellation=self._cancellation,
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                poll_interval_seconds=self._cancellation_poll_interval_seconds,
            )
            await lease_guard.assert_healthy()
        except AppExplorationCanceledError:
            # 本地 HTTP 等待可以取消，但 Official 服务端的同步安装线程可能仍在扫包；
            # 设备必须隔离，不能在未知设备副作用时回收到可用池。
            await self._cancel_inflight_install(
                command=command,
                snapshot=snapshot,
                session_ref=session_ref,
                session_snapshot=session_snapshot,
                lease_guard=lease_guard,
            )
        except Exception as exc:
            await self._handle_external_failure(
                command=command,
                snapshot=snapshot,
                session_ref=session_ref,
                session_snapshot=session_snapshot,
                exc=exc,
                default_code="INSTALL_FAILED",
                lease_guard=lease_guard,
            )
        if result.session_id != command.session_id:
            await self._handle_external_failure(
                command=command,
                snapshot=snapshot,
                session_ref=session_ref,
                session_snapshot=session_snapshot,
                exc=RuntimeError("安装结果 Session 身份不一致"),
                default_code="INSTALL_IDENTITY_MISMATCH",
                lease_guard=lease_guard,
            )
        if not result.succeeded:
            await self._handle_external_failure(
                command=command,
                snapshot=snapshot,
                session_ref=session_ref,
                session_snapshot=session_snapshot,
                exc=_ResultFailure(
                    result.error_code or "INSTALL_FAILED",
                    retryable=result.retryable,
                ),
                default_code="INSTALL_FAILED",
                lease_guard=lease_guard,
            )
        return await self._transition(
            command,
            snapshot,
            AppBootstrapStage.INSTALLED,
            install_request_id=result.request_id,
            installed_package=result.installed_package,
        )

    async def _cancel_inflight_install(
        self,
        *,
        command: RunAppWorkflowCommand,
        snapshot: AppBootstrapSnapshot,
        session_ref: SurfaceSessionRef,
        session_snapshot: SurfaceSessionSnapshot,
        lease_guard: AppBootstrapLeaseGuard,
    ) -> None:
        await self._cancel_checkpoint(
            command=command,
            session_ref=session_ref,
            session_snapshot=session_snapshot,
            snapshot=snapshot,
            lease_guard=lease_guard,
        )
        # 取消发生在外部安装等待期间，服务端可能仍占用设备；只隔离，不声称已停止 ADB。
        await self._release_device_claim(command.session_id, quarantine=True)
        raise AppWorkflowFailure(
            "App Workflow 已取消",
            stage=AppBootstrapStage.CANCELED,
            error_code="WORKFLOW_CANCELED",
            retryable=False,
        )

    async def _open(
        self,
        *,
        command: RunAppWorkflowCommand,
        snapshot: AppBootstrapSnapshot,
        session_ref: SurfaceSessionRef,
        session_snapshot: SurfaceSessionSnapshot,
        request: AppOpenRequest,
        lease_guard: AppBootstrapLeaseGuard,
    ) -> AppBootstrapSnapshot:
        try:
            await lease_guard.assert_healthy()
            result = await self._bootstrap_port.open_app(request)
            await lease_guard.assert_healthy()
        except Exception as exc:
            await self._handle_external_failure(
                command=command,
                snapshot=snapshot,
                session_ref=session_ref,
                session_snapshot=session_snapshot,
                exc=exc,
                default_code="OPEN_FAILED",
                lease_guard=lease_guard,
            )
        if (
            result.session_id != command.session_id
            or result.package_name != request.package_name
        ):
            await self._handle_external_failure(
                command=command,
                snapshot=snapshot,
                session_ref=session_ref,
                session_snapshot=session_snapshot,
                exc=RuntimeError("打开结果身份不一致"),
                default_code="OPEN_IDENTITY_MISMATCH",
                lease_guard=lease_guard,
            )
        if not result.succeeded or not result.foreground_verified:
            await self._handle_external_failure(
                command=command,
                snapshot=snapshot,
                session_ref=session_ref,
                session_snapshot=session_snapshot,
                exc=_ResultFailure(
                    result.error_code or "OPEN_NOT_FOREGROUND",
                    retryable=result.retryable,
                ),
                default_code="OPEN_FAILED",
                lease_guard=lease_guard,
            )
        return await self._transition(
            command,
            snapshot,
            AppBootstrapStage.OPENED,
            open_request_id=result.request_id,
        )

    async def _capture_and_activate(
        self,
        *,
        command: RunAppWorkflowCommand,
        snapshot: AppBootstrapSnapshot,
        session_ref: SurfaceSessionRef,
        session_snapshot: SurfaceSessionSnapshot,
        manifest: SurfaceCapabilityManifest,
        lease_guard: AppBootstrapLeaseGuard,
    ) -> AppReadyHandoff:
        persisted = await self._captures.get_bootstrap_capture(
            task_id=command.task_id,
            session_id=command.session_id,
        )
        if persisted is None:
            try:
                await lease_guard.assert_healthy()
                artifacts = await self._driver.capture(
                    session=session_ref,
                    profile=_bootstrap_capture_profile(),
                )
                await lease_guard.assert_healthy()
            except Exception as exc:
                await self._handle_external_failure(
                    command=command,
                    snapshot=snapshot,
                    session_ref=session_ref,
                    session_snapshot=session_snapshot,
                    exc=exc,
                    default_code="CAPTURE_FAILED",
                    lease_guard=lease_guard,
                )
            try:
                await lease_guard.assert_healthy()
                persisted = await self._captures.persist_bootstrap(
                    task_id=command.task_id,
                    agent_run_id=command.agent_run_id,
                    session_id=command.session_id,
                    artifacts=artifacts,
                    expected_bootstrap_row_version=snapshot.row_version,
                    workflow_lease_owner=command.lease_owner,
                    workflow_lease_token=command.lease_token,
                )
                await lease_guard.assert_healthy()
            except ValueError as exc:
                await self._handle_external_failure(
                    command=command,
                    snapshot=snapshot,
                    session_ref=session_ref,
                    session_snapshot=session_snapshot,
                    exc=exc,
                    default_code="CAPTURE_INVALID",
                    lease_guard=lease_guard,
                )
        current = await self._bootstraps.get(session_id=command.session_id)
        if (
            current is None
            or current.stage is not AppBootstrapStage.BOOTSTRAP_CAPTURING
            or current.bootstrap_capture_id != persisted.capture.capture_id
            or current.row_version != persisted.bootstrap_row_version
        ):
            raise AppWorkflowFailure(
                "Bootstrap Capture 与当前检查点不一致",
                stage=snapshot.stage,
                error_code="CAPTURE_STATE_STALE",
                retryable=False,
            )
        if await self._is_canceled(command):
            await self._cancel(
                command=command,
                snapshot=current,
                session_ref=session_ref,
                session_snapshot=session_snapshot,
                lease_guard=lease_guard,
            )
        handoff = _build_handoff(
            command=command,
            persisted=persisted,
            manifest=manifest,
            bootstrap_row_version=current.row_version + 1,
            installed_package=_require_installed_package(current),
        )
        await self._surface_sessions.activate_from_bootstrap(
            handoff=handoff,
            expected_bootstrap_stage=AppBootstrapStage.BOOTSTRAP_CAPTURING,
            expected_bootstrap_row_version=current.row_version,
            expected_session_row_version=session_snapshot.row_version,
            workflow_lease_owner=command.lease_owner,
            workflow_lease_token=command.lease_token,
        )
        await self._publisher.publish(handoff)
        return handoff

    async def _resume_terminal(
        self,
        command: RunAppWorkflowCommand,
        snapshot: AppBootstrapSnapshot,
    ) -> AppReadyHandoff:
        if snapshot.stage is not AppBootstrapStage.APP_READY:
            raise AppWorkflowFailure(
                snapshot.error_message or f"App Bootstrap 已进入 {snapshot.stage.value}",
                stage=snapshot.stage,
                error_code=snapshot.error_code or snapshot.stage.value.upper(),
                retryable=False,
            )
        persisted = await self._captures.get_bootstrap_capture(
            task_id=command.task_id,
            session_id=command.session_id,
        )
        if (
            persisted is None
            or snapshot.bootstrap_capture_id != persisted.capture.capture_id
            or persisted.bootstrap_row_version != snapshot.row_version
        ):
            raise AppWorkflowFailure(
                "APP_READY 缺少一致的 Bootstrap Capture",
                stage=snapshot.stage,
                error_code="APP_READY_CAPTURE_MISSING",
                retryable=False,
            )
        get_session = getattr(self._surface_sessions, "get", None)
        session_snapshot = (
            await get_session(session_id=command.session_id)
            if callable(get_session)
            else None
        )
        if (
            session_snapshot is None
            or session_snapshot.task_id != command.task_id
            or session_snapshot.status is not SurfaceSessionStatus.ACTIVE
        ):
            raise AppWorkflowFailure(
                "APP_READY 对应的 Surface Session 不可恢复",
                stage=snapshot.stage,
                error_code="APP_SESSION_REATTACH_REQUIRED",
                retryable=True,
            )
        try:
            device_lease = await get_device_lease(session_id=command.session_id)
        except Exception as exc:  # noqa: BLE001 - 续租/恢复必须 fail closed
            raise AppWorkflowFailure(
                "APP_READY 对应的设备租约事实不可读取",
                stage=AppBootstrapStage.APP_READY,
                error_code="APP_DEVICE_LEASE_UNAVAILABLE",
                retryable=True,
            ) from exc
        if session_snapshot.device_lease_id is not None and (
            device_lease is None
            or device_lease.lease_id != session_snapshot.device_lease_id
            or device_lease.fencing_token != session_snapshot.device_lease_fencing_token
        ):
            raise AppWorkflowFailure(
                "APP_READY 对应的设备租约身份与 Session 不一致",
                stage=AppBootstrapStage.APP_READY,
                error_code="APP_DEVICE_LEASE_LOST",
                retryable=True,
            )
        device_lease = await _continue_same_device_lease(
            device_lease,
            lease_seconds=max(300, int(command.lease_seconds)),
        )
        if device_lease is not None and device_lease.state not in {"claimed", "active"}:
            raise AppWorkflowFailure(
                "APP_READY 对应的设备租约已失效或正在恢复",
                stage=AppBootstrapStage.APP_READY,
                error_code="APP_DEVICE_LEASE_LOST",
                retryable=True,
            )
        spec = SurfaceSessionSpec(
            task_id=command.task_id,
            session_id=command.session_id,
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref=command.platform_build_ref,
            device_ref=command.device_ref,
            device_lease_id=device_lease.lease_id if device_lease is not None else None,
            device_lease_fencing_token=(
                device_lease.fencing_token if device_lease is not None else None
            ),
            options=_session_options_from_device_ref(
                command.device_ref,
                morelogin_env_id=(
                    resolve_morelogin_env_id_for_lease(device_lease)
                    if device_lease is not None
                    else ""
                ),
            ),
        )
        try:
            session_ref = await self._driver.reattach_session(spec)
            _validate_session_ref(session_ref, spec)
        except Exception as exc:  # noqa: BLE001 - 重连失败允许后续租约重试
            raise AppWorkflowFailure(
                "APP_READY 对应的设备会话重连失败",
                stage=snapshot.stage,
                error_code="APP_SESSION_REATTACH_FAILED",
                retryable=True,
            ) from exc
        self._attached_session_ids.add(command.session_id)
        handoff = _build_handoff(
            command=command,
            persisted=persisted,
            manifest=_persisted_app_capability_manifest(),
            bootstrap_row_version=snapshot.row_version,
            installed_package=_require_installed_package(snapshot),
        )
        await self._publisher.publish(handoff)
        return handoff

    async def _transition(
        self,
        command: RunAppWorkflowCommand,
        snapshot: AppBootstrapSnapshot,
        target_stage: AppBootstrapStage,
        **updates: object,
    ) -> AppBootstrapSnapshot:
        return await self._bootstraps.transition(
            TransitionAppBootstrapCommand(
                task_id=command.task_id,
                session_id=command.session_id,
                platform_build_ref=command.platform_build_ref,
                expected_stage=snapshot.stage,
                target_stage=target_stage,
                expected_row_version=snapshot.row_version,
                lease_owner=command.lease_owner,
                lease_token=command.lease_token,
                install_request_id=updates.get("install_request_id"),
                install_attempt=updates.get("install_attempt"),
                installed_package=updates.get("installed_package"),
                open_request_id=updates.get("open_request_id"),
                open_attempt=updates.get("open_attempt"),
            )
        )

    async def _handle_external_failure(
        self,
        *,
        command: RunAppWorkflowCommand,
        snapshot: AppBootstrapSnapshot,
        session_ref: SurfaceSessionRef,
        session_snapshot: SurfaceSessionSnapshot,
        exc: BaseException,
        default_code: str,
        lease_guard: AppBootstrapLeaseGuard,
    ) -> None:
        if isinstance(exc, AppBootstrapLeaseLostError):
            raise exc
        failure = _workflow_failure(exc, stage=snapshot.stage, default_code=default_code)
        if not failure.retryable:
            await self._bootstraps.fail(
                FailAppBootstrapCommand(
                    task_id=command.task_id,
                    session_id=command.session_id,
                    platform_build_ref=command.platform_build_ref,
                    expected_stage=snapshot.stage,
                    expected_row_version=snapshot.row_version,
                    lease_owner=command.lease_owner,
                    lease_token=command.lease_token,
                    error_code=failure.error_code,
                    error_message=str(failure),
                )
            )
            await self._mark_session_lost(
                command=command,
                session_ref=session_ref,
                session_snapshot=session_snapshot,
                reason=failure.error_code,
                lease_guard=lease_guard,
            )
            await self._release_device_claim(command.session_id, quarantine=True)
        raise failure

    async def _cancel(
        self,
        *,
        command: RunAppWorkflowCommand,
        snapshot: AppBootstrapSnapshot,
        session_ref: SurfaceSessionRef,
        session_snapshot: SurfaceSessionSnapshot,
        lease_guard: AppBootstrapLeaseGuard,
    ) -> None:
        await self._cancel_checkpoint(
            command=command,
            session_ref=session_ref,
            session_snapshot=session_snapshot,
            snapshot=snapshot,
            lease_guard=lease_guard,
        )
        # 用户取消是受控终态：干净释放设备，禁止 quarantine 造成后继 DEVICE_BUSY。
        # 不确定关闭／崩溃路径仍走 quarantine=True。
        await self._release_device_claim(command.session_id, quarantine=False)
        raise AppWorkflowFailure(
            "App Workflow 已取消",
            stage=AppBootstrapStage.CANCELED,
            error_code="WORKFLOW_CANCELED",
            retryable=False,
        )

    async def _cancel_checkpoint(
        self,
        *,
        command: RunAppWorkflowCommand,
        snapshot: AppBootstrapSnapshot,
        session_ref: SurfaceSessionRef,
        session_snapshot: SurfaceSessionSnapshot,
        lease_guard: AppBootstrapLeaseGuard,
    ) -> None:
        await lease_guard.assert_healthy()
        await self._bootstraps.cancel(
            CancelAppBootstrapCommand(
                task_id=command.task_id,
                session_id=command.session_id,
                platform_build_ref=command.platform_build_ref,
                expected_stage=snapshot.stage,
                expected_row_version=snapshot.row_version,
                lease_owner=command.lease_owner,
                lease_token=command.lease_token,
            )
        )
        await self._mark_session_lost(
            command=command,
            session_ref=session_ref,
            session_snapshot=session_snapshot,
            reason="WORKFLOW_CANCELED",
            lease_guard=lease_guard,
        )

    async def _mark_session_lost(
        self,
        *,
        command: RunAppWorkflowCommand,
        session_ref: SurfaceSessionRef,
        session_snapshot: SurfaceSessionSnapshot,
        reason: str,
        lease_guard: AppBootstrapLeaseGuard,
    ) -> None:
        del session_ref, lease_guard
        # fail/cancel 已撤销 Bootstrap 租约，旧 Worker 不再拥有外部调用权限。
        # 设备关闭必须由后续持有合法 Session Command Permit 的清理流程执行。
        self._attached_session_ids.discard(command.session_id)
        try:
            await self._surface_sessions.mark_lost(
                task_id=command.task_id,
                session_id=command.session_id,
                expected_row_version=session_snapshot.row_version,
                reason=reason,
            )
        except BaseException:  # noqa: BLE001  # 原始失败仍是调用方需要处理的主错误
            pass

    async def _is_canceled(self, command: RunAppWorkflowCommand) -> bool:
        return await self._cancellation.is_cancel_requested(
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
        )

    async def _release_device_claim(self, session_id: str, *, quarantine: bool) -> None:
        claim = self._device_claims.pop(session_id, None)
        if claim is None:
            return
        pool, lease = claim
        try:
            if quarantine:
                result = pool.quarantine(lease=lease, reason="app_workflow_session_lost")
            else:
                result = pool.release(lease=lease)
            if hasattr(result, "__await__"):
                result = await result
            # 到期、fencing 失效或 provider 拒绝释放时，设备不得回到 available。
            # 尝试隔离是幂等的；原始 Workflow 错误仍由调用方保留。
            if not quarantine and result is not True:
                quarantined = pool.quarantine(
                    lease=lease,
                    reason="app_workflow_release_unconfirmed",
                )
                if hasattr(quarantined, "__await__"):
                    await quarantined
                await maybe_power_off_cloud_phone_for_lease(
                    lease=lease,
                    reason="app_workflow_release_unconfirmed",
                )
                return
            if result is True:
                await maybe_power_off_cloud_phone_for_lease(
                    lease=lease,
                    reason=(
                        "app_workflow_quarantined"
                        if quarantine
                        else "app_workflow_released"
                    ),
                )
        except BaseException:  # noqa: BLE001 - 原始 Workflow 失败优先，设备进入人工对账
            return

    async def _get_existing_session(self, session_id: str) -> SurfaceSessionSnapshot | None:
        getter = getattr(self._surface_sessions, "get", None)
        if not callable(getter):
            return None
        return await getter(session_id=session_id)

    async def _get_pool_lease(
        self,
        pool: DeviceLeasePool,
        *,
        lease_id: str | None,
        session_id: str,
    ) -> DeviceLease | None:
        getter = getattr(pool, "get", None) or getattr(pool, "get_active", None)
        if not callable(getter):
            return None
        result = getter(lease_id=lease_id, session_id=session_id)
        if hasattr(result, "__await__"):
            return await result
        return result


class _ResultFailure(RuntimeError):
    def __init__(self, error_code: str, *, retryable: bool) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = retryable


def _workflow_failure(
    exc: BaseException,
    *,
    stage: AppBootstrapStage,
    default_code: str,
) -> AppWorkflowFailure:
    error_code = str(getattr(exc, "error_code", "") or default_code)
    retryable_value = getattr(exc, "retryable", None)
    retryable = (
        bool(retryable_value)
        if isinstance(retryable_value, bool)
        else isinstance(exc, (TimeoutError, ConnectionError))
    )
    message = str(exc).strip() or error_code
    return AppWorkflowFailure(
        message,
        stage=stage,
        error_code=error_code,
        retryable=retryable,
    )


def _install_request(
    command: RunAppWorkflowCommand,
    build: PlatformBuild,
) -> AppInstallRequest:
    return AppInstallRequest(
        task_id=command.task_id,
        session_id=command.session_id,
        platform_build_ref=command.platform_build_ref,
        sha256=build.sha256,
        package_name=build.package_name,
    )


def _open_request(
    command: RunAppWorkflowCommand,
    snapshot: AppBootstrapSnapshot,
) -> AppOpenRequest:
    if snapshot.installed_package is None:
        raise AppWorkflowFailure(
            "INSTALLED 检查点缺少 installed_package",
            stage=snapshot.stage,
            error_code="INSTALL_RESULT_MISSING",
            retryable=False,
        )
    return AppOpenRequest(
        task_id=command.task_id,
        session_id=command.session_id,
        package_name=snapshot.installed_package,
    )


def _bootstrap_capture_profile() -> CaptureProfile:
    screenshot = ("screenshot",)
    return CaptureProfile(
        profile_id="app-bootstrap",
        before=CaptureDirective(role="bootstrap", artifact_types=screenshot),
        after=CaptureDirective(role="bootstrap", artifact_types=screenshot),
    )


def _persisted_app_capability_manifest() -> SurfaceCapabilityManifest:
    """终态恢复只依据数据库事实，不能为了构建交接消息重新调用 Adapter。"""
    return build_android_screenshot_only_manifest(
        adapter_id="persisted-app-bootstrap",
        adapter_version="1.0",
        backend="persisted",
    )


def _build_handoff(
    *,
    command: RunAppWorkflowCommand,
    persisted: PersistedAppCapture,
    manifest: SurfaceCapabilityManifest,
    bootstrap_row_version: int,
    installed_package: str,
) -> AppReadyHandoff:
    return AppReadyHandoff(
        task_id=command.task_id,
        agent_run_id=persisted.agent_run_id,
        session_id=command.session_id,
        capture=persisted.capture,
        capability_manifest=manifest,
        bootstrap_row_version=bootstrap_row_version,
        installed_package=installed_package,
    )


async def _continue_same_device_lease(
    device_lease: DeviceLease | None,
    *,
    lease_seconds: int,
) -> DeviceLease | None:
    """同一任务的恢复中或自动隔离租约续回 active。别的失败隔离仍不能接着用。"""

    if device_lease is None or device_lease.state not in {"recovering", "quarantined"}:
        return device_lease
    try:
        return await renew_device_lease(
            pool=await get_runtime_device_pool(),
            lease=device_lease,
            lease_seconds=max(300, int(lease_seconds)),
        )
    except DevicePoolError:
        return device_lease


def _require_installed_package(snapshot: AppBootstrapSnapshot) -> str:
    package_name = snapshot.installed_package
    if not isinstance(package_name, str) or not package_name.strip():
        raise AppWorkflowFailure(
            "App Bootstrap 检查点缺少 installed_package",
            stage=snapshot.stage,
            error_code="INSTALL_RESULT_MISSING",
            retryable=False,
        )
    return package_name


def _session_options_from_device_ref(
    device_ref: str | None,
    *,
    morelogin_env_id: str | None = None,
) -> dict[str, str]:
    """把 ``cloud_phone:default`` 展开为 Official start_session 的 options，并声明业务会话身份。"""
    if not isinstance(device_ref, str) or not device_ref.strip():
        backend = "cloud_phone"
        profile = "default"
    else:
        parts = device_ref.strip().split(":", 1)
        backend = parts[0].strip() or "cloud_phone"
        profile = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "default"
    options = {
        "backend": backend,
        "device_profile_id": profile,
        # 同设备不等于同业务会话：显式记录进程／数据容器／生命周期相位
        "process_identity": f"{backend}:{profile}:process",
        "app_data_container_ref": f"{backend}:{profile}:app-data",
        "lifecycle_phase": "foreground",
        "reset_policy": "force_stop",
        "deep_link": "",
        "overlay_signature": "none",
    }
    env_id = str(morelogin_env_id or "").strip()
    if env_id:
        options["morelogin_env_id"] = env_id
    return options


def _validate_session_ref(reference: SurfaceSessionRef, spec: SurfaceSessionSpec) -> None:
    if (
        reference.task_id != spec.task_id
        or reference.session_id != spec.session_id
        or reference.surface_kind is not spec.surface_kind
        or reference.primary_surface_ref != app_primary_surface_ref(spec.session_id)
    ):
        raise AppWorkflowFailure(
            "Driver 返回的 Session 引用与请求身份不一致",
            stage=AppBootstrapStage.ACQUIRING_SESSION,
            error_code="SESSION_IDENTITY_MISMATCH",
            retryable=False,
        )
