"""确定性 AppQcWorkflow 顺序、恢复、失败与取消测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.app_bootstrap_lease import AppBootstrapLeaseLostError
from services.api.app.application.app_bootstrap_state import (
    CancelAppBootstrapCommand,
    FailAppBootstrapCommand,
    RenewAppBootstrapLeaseCommand,
    TransitionAppBootstrapCommand,
)
from services.api.app.application.app_capture_persistence import PersistedAppCapture
from services.api.app.application.app_workflow import (
    AppQcWorkflow,
    RunAppWorkflowCommand,
)
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.domain.interaction import (
    AppBootstrapSnapshot,
    AppBootstrapStage,
    AppInstallRequest,
    AppInstallResult,
    AppOpenRequest,
    AppOpenResult,
    AppReadyHandoff,
    AppWorkflowFailure,
    CapabilityDescriptor,
    CapabilityStatus,
    CaptureKind,
    CaptureRef,
    RawArtifact,
    RawArtifactSet,
    RawSurfaceObservation,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSnapshot,
    SurfaceSessionSpec,
    SurfaceSessionStatus,
    build_android_screenshot_only_manifest,
)
from services.api.app.domain.interaction.surface_driver import app_primary_surface_ref
from services.api.app.domain.task.platform_build import PlatformBuild, PlatformBuildStatus

TASK_ID = "11111111-1111-1111-1111-111111111111"
RUN_ID = "22222222-2222-2222-2222-222222222222"
LEASE_TOKEN = "33333333-3333-3333-3333-333333333333"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


class InMemoryBootstrapRepository:
    def __init__(
        self,
        snapshot: AppBootstrapSnapshot | None = None,
        *,
        trace: list[str] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.transitions: list[tuple[AppBootstrapStage, AppBootstrapStage]] = []
        self.trace = trace if trace is not None else []
        self.renew_error: BaseException | None = None

    async def get(self, *, session_id: str) -> AppBootstrapSnapshot | None:
        del session_id
        return self.snapshot

    async def get_or_create(self, **kwargs: Any) -> AppBootstrapSnapshot:
        self.trace.append("bootstrap_claimed")
        if self.snapshot is None:
            now = datetime.now(UTC)
            self.snapshot = AppBootstrapSnapshot(
                task_id=kwargs["task_id"],
                session_id=kwargs["session_id"],
                platform_build_ref=kwargs["platform_build_ref"],
                stage=AppBootstrapStage.VALIDATING,
                install_attempt=0,
                open_attempt=0,
                row_version=0,
                started_at=now,
                updated_at=now,
            )
        return self.snapshot

    async def renew(
        self,
        command: RenewAppBootstrapLeaseCommand,
    ) -> AppBootstrapSnapshot:
        self.trace.append("bootstrap_renewed")
        if self.renew_error is not None:
            raise self.renew_error
        assert self.snapshot is not None
        return self.snapshot

    async def transition(
        self,
        command: TransitionAppBootstrapCommand,
    ) -> AppBootstrapSnapshot:
        assert self.snapshot is not None
        assert self.snapshot.stage is command.expected_stage
        assert self.snapshot.row_version == command.expected_row_version
        self.transitions.append((command.expected_stage, command.target_stage))
        self.snapshot = replace(
            self.snapshot,
            stage=command.target_stage,
            install_request_id=(
                command.install_request_id or self.snapshot.install_request_id
            ),
            install_attempt=(
                command.install_attempt
                if command.install_attempt is not None
                else self.snapshot.install_attempt
            ),
            installed_package=command.installed_package or self.snapshot.installed_package,
            open_request_id=command.open_request_id or self.snapshot.open_request_id,
            open_attempt=(
                command.open_attempt
                if command.open_attempt is not None
                else self.snapshot.open_attempt
            ),
            row_version=self.snapshot.row_version + 1,
            updated_at=datetime.now(UTC),
        )
        return self.snapshot

    async def fail(self, command: FailAppBootstrapCommand) -> AppBootstrapSnapshot:
        assert self.snapshot is not None
        self.snapshot = replace(
            self.snapshot,
            stage=AppBootstrapStage.FAILED,
            error_code=command.error_code,
            error_message=command.error_message,
            failed_at=datetime.now(UTC),
            row_version=self.snapshot.row_version + 1,
            updated_at=datetime.now(UTC),
        )
        return self.snapshot

    async def cancel(self, command: CancelAppBootstrapCommand) -> AppBootstrapSnapshot:
        assert self.snapshot is not None
        self.snapshot = replace(
            self.snapshot,
            stage=AppBootstrapStage.CANCELED,
            row_version=self.snapshot.row_version + 1,
            updated_at=datetime.now(UTC),
        )
        return self.snapshot


class InMemorySurfaceSessions:
    def __init__(
        self,
        bootstraps: InMemoryBootstrapRepository,
        *,
        trace: list[str] | None = None,
    ) -> None:
        self.bootstraps = bootstraps
        self.snapshot: SurfaceSessionSnapshot | None = None
        self.events: list[str] = []
        self.trace = trace if trace is not None else []
        self.last_spec: SurfaceSessionSpec | None = None

    async def register_starting(
        self,
        *,
        spec: SurfaceSessionSpec,
        session_owner: str,
        primary_surface_ref: str,
    ) -> SurfaceSessionSnapshot:
        self.events.append("register_starting")
        self.trace.append("starting_registered")
        self.last_spec = spec
        if self.snapshot is None:
            now = datetime.now(UTC)
            self.snapshot = SurfaceSessionSnapshot(
                session_id=spec.session_id,
                task_id=spec.task_id,
                surface_kind=spec.surface_kind,
                session_owner=session_owner,
                status=SurfaceSessionStatus.STARTING,
                command_sequence=0,
                finalization_epoch=0,
                row_version=0,
                account_environment_ref=spec.account_environment_ref,
                platform_build_ref=spec.platform_build_ref,
                device_ref=spec.device_ref,
                current_surface_ref=primary_surface_ref,
                current_capture_id=None,
                started_at=now,
                updated_at=now,
            )
        return self.snapshot

    async def activate_from_bootstrap(self, **kwargs: Any) -> SurfaceSessionSnapshot:
        self.events.append("activate")
        handoff: AppReadyHandoff = kwargs["handoff"]
        assert self.snapshot is not None
        assert self.bootstraps.snapshot is not None
        self.bootstraps.snapshot = replace(
            self.bootstraps.snapshot,
            stage=AppBootstrapStage.APP_READY,
            bootstrap_capture_id=handoff.capture.capture_id,
            ready_at=datetime.now(UTC),
            row_version=handoff.bootstrap_row_version,
            updated_at=datetime.now(UTC),
        )
        self.snapshot = replace(
            self.snapshot,
            status=SurfaceSessionStatus.ACTIVE,
            current_capture_id=handoff.capture.capture_id,
            row_version=self.snapshot.row_version + 1,
            updated_at=datetime.now(UTC),
        )
        return self.snapshot

    async def mark_lost(self, **kwargs: Any) -> SurfaceSessionSnapshot:
        self.events.append("mark_lost")
        assert self.snapshot is not None
        self.snapshot = replace(
            self.snapshot,
            status=SurfaceSessionStatus.LOST,
            row_version=self.snapshot.row_version + 1,
            updated_at=datetime.now(UTC),
        )
        return self.snapshot


class FakeBuildReader:
    def __init__(self, *, package_name: str | None = "com.example.target") -> None:
        self.build = PlatformBuild(
            build_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            platform="android",
            original_filename="target.apk",
            storage_path="aa/target.apk",
            sha256="a" * 64,
            size_bytes=1024,
            status=PlatformBuildStatus.VERIFIED,
            created_by="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            package_name=package_name,
        )

    async def get_verified_by_ref(self, platform_build_ref: str) -> PlatformBuild | None:
        if platform_build_ref == self.build.platform_build_ref:
            return self.build
        return None


class RuntimeFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.error_code = code
        self.retryable = retryable


class RecordingRuntime:
    def __init__(
        self,
        *,
        trace: list[str] | None = None,
        installed_package: str = "com.example.target",
    ) -> None:
        self.events: list[str] = []
        self.trace = trace if trace is not None else []
        self.install_error: str | None = None
        self.install_retryable = False
        self.open_error: str | None = None
        self.capture_error: str | None = None
        self.installed_package = installed_package
        self.opened_packages: list[str] = []
        self.manifest = build_android_screenshot_only_manifest(
            adapter_id="test-runtime",
            adapter_version="1.0",
            backend="fake",
        )

    def describe_capabilities(self):
        self.events.append("capabilities")
        self.trace.append("external:capabilities")
        return self.manifest

    async def start_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef:
        self.events.append("start_session")
        self.trace.append("external:start_session")
        return SurfaceSessionRef(
            task_id=spec.task_id,
            session_id=spec.session_id,
            surface_kind=spec.surface_kind,
            primary_surface_ref=app_primary_surface_ref(spec.session_id),
        )

    async def ensure_installed(self, request: AppInstallRequest) -> AppInstallResult:
        self.events.append("install")
        self.trace.append("external:install")
        if self.install_error is not None:
            return AppInstallResult(
                request_id="install-result",
                session_id=request.session_id,
                succeeded=False,
                retryable=self.install_retryable,
                error_code=self.install_error,
            )
        return AppInstallResult(
            request_id="install-result",
            session_id=request.session_id,
            succeeded=True,
            retryable=False,
            installed_package=self.installed_package,
        )

    async def open_app(self, request: AppOpenRequest) -> AppOpenResult:
        self.events.append("open")
        self.trace.append("external:open")
        self.opened_packages.append(request.package_name)
        if self.open_error is not None:
            return AppOpenResult(
                request_id="open-result",
                session_id=request.session_id,
                succeeded=False,
                retryable=False,
                package_name=request.package_name,
                foreground_verified=False,
                error_code=self.open_error,
            )
        return AppOpenResult(
            request_id="open-result",
            session_id=request.session_id,
            succeeded=True,
            retryable=False,
            package_name=request.package_name,
            foreground_verified=True,
            activity=".MainActivity",
        )

    async def capture(self, **kwargs: Any) -> RawArtifactSet:
        self.events.append("capture")
        self.trace.append("external:capture")
        session: SurfaceSessionRef = kwargs["session"]
        if self.capture_error is not None:
            raise RuntimeFailure(self.capture_error)
        return RawArtifactSet(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_ref=session.primary_surface_ref,
            artifacts=(RawArtifact(artifact_type="screenshot", content=PNG_1X1),),
            metadata={
                "package_name": "com.example.target",
                "activity": ".MainActivity",
                "width": 1,
                "height": 1,
                "orientation": "portrait",
            },
        )

    async def close_session(self, **kwargs: Any) -> None:
        self.events.append("close_session")
        self.trace.append("external:close_session")

    async def observe(self, **kwargs: Any) -> RawSurfaceObservation:
        raise AssertionError("Bootstrap 不应调用 observe")


class InMemoryCaptures:
    def __init__(self, bootstraps: InMemoryBootstrapRepository) -> None:
        self.bootstraps = bootstraps
        self.existing: PersistedAppCapture | None = None
        self.save_count = 0

    async def get_bootstrap_capture(self, **kwargs: Any) -> PersistedAppCapture | None:
        del kwargs
        return self.existing

    async def persist_bootstrap(self, **kwargs: Any) -> PersistedAppCapture:
        self.save_count += 1
        assert self.bootstraps.snapshot is not None
        screenshot = PersistedEvidenceArtifact(
            artifact_id="artifact-bootstrap",
            artifact_type="screenshot",
            storage_path="task/capture/screenshot.png",
            content_type="image/png",
            checksum="sha256:image",
            size_bytes=len(PNG_1X1),
            metadata={},
        )
        capture = CaptureRef(
            task_id=kwargs["task_id"],
            session_id=kwargs["session_id"],
            capture_id=CAPTURE_ID,
            surface_kind=SurfaceKind.ANDROID,
            capture_kind=CaptureKind.APP,
            role="bootstrap",
            artifact_ids=(screenshot.artifact_id,),
        )
        next_version = self.bootstraps.snapshot.row_version + 1
        self.bootstraps.snapshot = replace(
            self.bootstraps.snapshot,
            bootstrap_capture_id=CAPTURE_ID,
            row_version=next_version,
            updated_at=datetime.now(UTC),
        )
        self.existing = PersistedAppCapture(
            capture=capture,
            agent_run_id=kwargs["agent_run_id"],
            screenshot=screenshot,
            width=1,
            height=1,
            orientation="portrait",
            image_signature="sha256:image",
            package_name="com.example.target",
            activity=".MainActivity",
            bootstrap_row_version=next_version,
        )
        return self.existing


class RecordingPublisher:
    def __init__(self) -> None:
        self.handoffs: list[AppReadyHandoff] = []

    async def publish(self, handoff: AppReadyHandoff) -> None:
        self.handoffs.append(handoff)


class CancelOnSecondCheck:
    def __init__(self) -> None:
        self.count = 0

    async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
        del task_id, agent_run_id
        self.count += 1
        return self.count >= 2


class ToggleCancellation:
    def __init__(self) -> None:
        self.requested = False

    async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
        del task_id, agent_run_id
        return self.requested


def _command() -> RunAppWorkflowCommand:
    return RunAppWorkflowCommand(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        session_id="app-session-1",
        platform_build_ref="app-build:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        lease_owner="app-worker-1",
        lease_token=LEASE_TOKEN,
        lease_seconds=120,
    )


def _bootstrap(stage: AppBootstrapStage) -> AppBootstrapSnapshot:
    now = datetime.now(UTC)
    installed = stage in {
        AppBootstrapStage.INSTALLED,
        AppBootstrapStage.OPENING,
        AppBootstrapStage.OPENED,
        AppBootstrapStage.BOOTSTRAP_CAPTURING,
        AppBootstrapStage.APP_READY,
    }
    opened = stage in {
        AppBootstrapStage.OPENED,
        AppBootstrapStage.BOOTSTRAP_CAPTURING,
        AppBootstrapStage.APP_READY,
    }
    ready = stage is AppBootstrapStage.APP_READY
    return AppBootstrapSnapshot(
        task_id=TASK_ID,
        session_id="app-session-1",
        platform_build_ref=_command().platform_build_ref,
        stage=stage,
        install_request_id="install-result" if installed else None,
        install_attempt=1 if installed else 0,
        installed_package="com.example.target" if installed else None,
        open_request_id="open-result" if opened else None,
        open_attempt=1 if opened else 0,
        bootstrap_capture_id=CAPTURE_ID if ready else None,
        row_version=7,
        started_at=now,
        ready_at=now if ready else None,
        updated_at=now,
    )


def _harness(
    *,
    snapshot: AppBootstrapSnapshot | None = None,
    cancellation: Any | None = None,
    build_package_name: str | None = "com.example.target",
    installed_package: str = "com.example.target",
    cancellation_poll_interval_seconds: float = 1.0,
) -> tuple[
    AppQcWorkflow,
    RecordingRuntime,
    InMemoryBootstrapRepository,
    InMemorySurfaceSessions,
    InMemoryCaptures,
    RecordingPublisher,
    list[str],
]:
    trace: list[str] = []
    bootstraps = InMemoryBootstrapRepository(snapshot, trace=trace)
    sessions = InMemorySurfaceSessions(bootstraps, trace=trace)
    if snapshot is not None and snapshot.stage is AppBootstrapStage.APP_READY:
        now = datetime.now(UTC)
        sessions.snapshot = SurfaceSessionSnapshot(
            session_id=snapshot.session_id,
            task_id=snapshot.task_id,
            surface_kind=SurfaceKind.ANDROID,
            session_owner="app-workflow",
            status=SurfaceSessionStatus.ACTIVE,
            command_sequence=0,
            finalization_epoch=0,
            row_version=1,
            account_environment_ref=None,
            platform_build_ref=snapshot.platform_build_ref,
            device_ref=None,
            current_surface_ref=app_primary_surface_ref("app-session-1"),
            current_capture_id=CAPTURE_ID,
            started_at=now,
            updated_at=now,
        )
    captures = InMemoryCaptures(bootstraps)
    if snapshot is not None and snapshot.bootstrap_capture_id is not None:
        screenshot = PersistedEvidenceArtifact(
            artifact_id="artifact-bootstrap",
            artifact_type="screenshot",
            storage_path="task/capture/screenshot.png",
            content_type="image/png",
            checksum="sha256:image",
            size_bytes=len(PNG_1X1),
            metadata={},
        )
        captures.existing = PersistedAppCapture(
            capture=CaptureRef(
                task_id=TASK_ID,
                session_id="app-session-1",
                capture_id=CAPTURE_ID,
                surface_kind=SurfaceKind.ANDROID,
                capture_kind=CaptureKind.APP,
                role="bootstrap",
                artifact_ids=(screenshot.artifact_id,),
            ),
            agent_run_id=RUN_ID,
            screenshot=screenshot,
            width=1,
            height=1,
            orientation="portrait",
            image_signature="sha256:image",
            package_name="com.example.target",
            activity=".MainActivity",
            bootstrap_row_version=snapshot.row_version,
        )
    runtime = RecordingRuntime(trace=trace, installed_package=installed_package)
    publisher = RecordingPublisher()
    workflow = AppQcWorkflow(
        platform_builds=FakeBuildReader(package_name=build_package_name),
        surface_sessions=sessions,
        bootstraps=bootstraps,
        bootstrap_port=runtime,
        driver=runtime,
        captures=captures,
        publisher=publisher,
        cancellation=cancellation,
        cancellation_poll_interval_seconds=cancellation_poll_interval_seconds,
    )
    return workflow, runtime, bootstraps, sessions, captures, publisher, trace


async def test_success_runs_install_open_capture_commit_then_publish() -> None:
    workflow, runtime, bootstraps, sessions, captures, publisher, trace = _harness()

    handoff = await workflow.run(_command())

    assert runtime.events == ["capabilities", "start_session", "install", "open", "capture"]
    assert sessions.events == ["register_starting", "activate"]
    assert trace.index("starting_registered") < trace.index("external:capabilities")
    assert trace.index("bootstrap_claimed") < trace.index("external:capabilities")
    assert captures.save_count == 1
    assert bootstraps.snapshot is not None
    assert bootstraps.snapshot.stage is AppBootstrapStage.APP_READY
    assert publisher.handoffs == [handoff]
    assert handoff.capture.capture_id == CAPTURE_ID
    assert handoff.installed_package == "com.example.target"


async def test_success_uses_runtime_installed_package_when_build_metadata_is_missing() -> None:
    workflow, runtime, bootstraps, _, _, _, _ = _harness(
        build_package_name=None,
        installed_package="com.example.actual",
    )

    handoff = await workflow.run(_command())

    assert bootstraps.snapshot is not None
    assert bootstraps.snapshot.installed_package == "com.example.actual"
    assert runtime.opened_packages == ["com.example.actual"]
    assert handoff.installed_package == "com.example.actual"


@pytest.mark.parametrize(
    ("stage", "forbidden_events"),
    [
        (AppBootstrapStage.INSTALLED, {"install"}),
        (AppBootstrapStage.OPENED, {"install", "open"}),
    ],
)
async def test_recovery_skips_confirmed_external_side_effects(
    stage: AppBootstrapStage,
    forbidden_events: set[str],
) -> None:
    workflow, runtime, _, _, _, _, _ = _harness(snapshot=_bootstrap(stage))

    await workflow.run(_command())

    assert forbidden_events.isdisjoint(runtime.events)
    assert "capture" in runtime.events


async def test_capturing_recovery_does_not_duplicate_capture() -> None:
    capturing = _bootstrap(AppBootstrapStage.BOOTSTRAP_CAPTURING)
    capturing = replace(capturing, bootstrap_capture_id=CAPTURE_ID)
    workflow, runtime, _, _, captures, _, _ = _harness(snapshot=capturing)

    await workflow.run(_command())

    assert "capture" not in runtime.events
    assert captures.save_count == 0



async def test_ready_recovery_without_attached_runtime_fails_closed_without_external_call() -> None:
    workflow, runtime, _, _, captures, publisher, _ = _harness(
        snapshot=_bootstrap(AppBootstrapStage.APP_READY)
    )

    with pytest.raises(AppWorkflowFailure) as error:
        await workflow.run(_command())

    assert error.value.error_code == "APP_SESSION_REATTACH_REQUIRED"
    assert runtime.events == []
    assert captures.save_count == 0
    assert publisher.handoffs == []


@pytest.mark.parametrize("failure_at", ["install", "open", "capture"])
async def test_nonretryable_failure_does_not_publish_or_close_revoked_session(
    failure_at: str,
) -> None:
    workflow, runtime, bootstraps, sessions, _, publisher, _ = _harness()
    if failure_at == "install":
        runtime.install_error = "INSTALL_FAILED"
    elif failure_at == "open":
        runtime.open_error = "OPEN_FAILED"
    else:
        runtime.capture_error = "CAPTURE_FAILED"

    with pytest.raises(RuntimeError):
        await workflow.run(_command())

    assert bootstraps.snapshot is not None
    assert bootstraps.snapshot.stage is AppBootstrapStage.FAILED
    assert publisher.handoffs == []
    assert "close_session" not in runtime.events
    assert "mark_lost" in sessions.events


async def test_retryable_install_failure_preserves_installing_for_replay() -> None:
    workflow, runtime, bootstraps, sessions, _, publisher, _ = _harness()
    runtime.install_error = "DEVICE_BUSY"
    runtime.install_retryable = True

    with pytest.raises(RuntimeError):
        await workflow.run(_command())

    assert bootstraps.snapshot is not None
    assert bootstraps.snapshot.stage is AppBootstrapStage.INSTALLING
    assert publisher.handoffs == []
    assert "close_session" not in runtime.events
    assert "mark_lost" not in sessions.events


async def test_lease_loss_after_external_call_blocks_capture_and_session_close() -> None:
    workflow, runtime, bootstraps, _, captures, _, _ = _harness()
    original_open = runtime.open_app

    async def slow_open(request: AppOpenRequest) -> AppOpenResult:
        result = await original_open(request)
        bootstraps.renew_error = RuntimeError("lease rejected")
        await asyncio.sleep(1.05)
        return result

    runtime.open_app = slow_open  # type: ignore[method-assign]

    with pytest.raises(AppBootstrapLeaseLostError):
        await workflow.run(replace(_command(), lease_seconds=1))

    assert "capture" not in runtime.events
    assert "close_session" not in runtime.events
    assert captures.save_count == 0


async def test_claimed_lease_rejection_blocks_first_capability_and_session_start() -> None:
    workflow, runtime, bootstraps, _, _, _, trace = _harness()
    bootstraps.renew_error = RuntimeError("lease rejected")

    with pytest.raises(AppBootstrapLeaseLostError):
        await workflow.run(_command())

    assert "bootstrap_claimed" in trace
    assert "external:capabilities" not in trace
    assert "external:start_session" not in trace
    assert runtime.events == []


async def test_device_pool_busy_does_not_start_session_or_install() -> None:
    from services.api.app.application.device_pool import reset_device_pool_for_tests

    occupied = reset_device_pool_for_tests(slots=["cloud_phone:a"])
    occupied.claim(
        task_id="other-task",
        session_id="other-session",
        preferred_device_ref="cloud_phone:a",
        lease_seconds=120,
    )
    workflow, runtime, _, sessions, _, publisher, _ = _harness()

    with pytest.raises(AppWorkflowFailure) as failure:
        await workflow.run(replace(_command(), device_ref="cloud_phone:a"))

    assert failure.value.error_code == "DEVICE_BUSY"
    assert failure.value.retryable is True
    assert failure.value.stage is AppBootstrapStage.ACQUIRING_SESSION
    assert runtime.events == []
    assert sessions.events == []
    assert publisher.handoffs == []


async def test_postgres_backend_claims_even_without_preferred_and_blocks_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.api.app.application import app_workflow as app_workflow_module
    from services.api.app.application.device_pool import reset_device_pool_for_tests

    pool = reset_device_pool_for_tests(slots=["cloud_phone:a"])
    pool.claim(
        task_id="other-task",
        session_id="other-session",
        lease_seconds=120,
    )
    monkeypatch.setattr(app_workflow_module, "is_postgres_device_lease_backend", lambda: True)
    workflow, runtime, _, sessions, _, publisher, _ = _harness()

    with pytest.raises(AppWorkflowFailure) as failure:
        await workflow.run(_command())

    assert failure.value.error_code == "DEVICE_POOL_EXHAUSTED"
    assert runtime.events == []
    assert sessions.events == []
    assert publisher.handoffs == []


async def test_claimed_lease_writes_morelogin_env_id_into_session_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_SLOTS", "cloud_phone:a,cloud_phone:b")
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", "env-a,env-b")
    monkeypatch.delenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", raising=False)
    from services.api.app.application.device_pool import reset_device_pool_for_tests

    reset_device_pool_for_tests(slots=["cloud_phone:a", "cloud_phone:b"])
    workflow, _, _, sessions, _, _, _ = _harness()

    await workflow.run(replace(_command(), device_ref="cloud_phone:b"))

    assert sessions.last_spec is not None
    assert sessions.last_spec.device_ref == "cloud_phone:b"
    assert sessions.last_spec.options["morelogin_env_id"] == "env-b"


async def test_capability_mismatch_fails_before_session_creation() -> None:
    workflow, runtime, _, sessions, _, publisher, _ = _harness()
    manifest = runtime.manifest
    runtime.manifest = replace(
        manifest,
        artifacts={
            **dict(manifest.artifacts),
            "screenshot": CapabilityDescriptor(status=CapabilityStatus.UNSUPPORTED),
        },
    )

    with pytest.raises(RuntimeError):
        await workflow.run(_command())

    assert runtime.events == ["capabilities"]
    assert sessions.events == ["register_starting"]
    assert publisher.handoffs == []


async def test_cancellation_after_session_creation_does_not_close_revoked_session() -> None:
    workflow, runtime, bootstraps, sessions, _, publisher, _ = _harness(
        cancellation=CancelOnSecondCheck()
    )

    with pytest.raises(RuntimeError):
        await workflow.run(_command())

    assert bootstraps.snapshot is not None
    assert bootstraps.snapshot.stage is AppBootstrapStage.CANCELED
    assert "close_session" not in runtime.events
    assert "mark_lost" in sessions.events
    assert publisher.handoffs == []


async def test_cancellation_during_install_cancels_wait_and_quarantines_device() -> None:
    cancellation = ToggleCancellation()
    workflow, runtime, bootstraps, sessions, _, publisher, _ = _harness(
        cancellation=cancellation,
        cancellation_poll_interval_seconds=0.001,
    )
    install_started = asyncio.Event()
    install_canceled = asyncio.Event()

    async def blocked_install(request: AppInstallRequest) -> AppInstallResult:
        del request
        runtime.events.append("install")
        runtime.trace.append("external:install")
        install_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            install_canceled.set()
            raise

    runtime.ensure_installed = blocked_install  # type: ignore[method-assign]
    release_calls: list[tuple[str, bool]] = []

    async def record_release(session_id: str, *, quarantine: bool) -> None:
        release_calls.append((session_id, quarantine))

    workflow._release_device_claim = record_release  # type: ignore[method-assign]

    run_task = asyncio.create_task(workflow.run(_command()))
    await install_started.wait()
    cancellation.requested = True

    with pytest.raises(AppWorkflowFailure) as failure:
        await asyncio.wait_for(run_task, timeout=1)

    assert failure.value.error_code == "WORKFLOW_CANCELED"
    assert install_canceled.is_set()
    assert bootstraps.snapshot is not None
    assert bootstraps.snapshot.stage is AppBootstrapStage.CANCELED
    assert sessions.snapshot is not None
    assert sessions.snapshot.status is SurfaceSessionStatus.LOST
    assert sessions.events == ["register_starting", "mark_lost"]
    assert release_calls == [("app-session-1", True)]
    assert "close_session" not in runtime.events
    assert "open" not in runtime.events
    assert "capture" not in runtime.events
    assert publisher.handoffs == []


async def test_device_wait_timeout_does_not_start_session_or_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.api.app.application import app_workflow as app_workflow_module
    from services.api.app.application.device_pool import reset_device_pool_for_tests

    monkeypatch.setenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", "0.05")
    occupied = reset_device_pool_for_tests(slots=["cloud_phone:a"])
    occupied.claim(
        task_id="other-task",
        session_id="other-session",
        lease_seconds=120,
    )
    monkeypatch.setattr(app_workflow_module, "is_postgres_device_lease_backend", lambda: True)
    workflow, runtime, _, sessions, _, publisher, _ = _harness()

    with pytest.raises(AppWorkflowFailure) as failure:
        await workflow.run(_command())

    assert failure.value.error_code == "DEVICE_WAIT_TIMEOUT"
    assert failure.value.retryable is False
    assert failure.value.stage is AppBootstrapStage.ACQUIRING_SESSION
    assert runtime.events == []
    assert sessions.events == []
    assert publisher.handoffs == []


async def test_device_wait_then_claim_proceeds_to_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.api.app.application import app_workflow as app_workflow_module
    from services.api.app.application.device_pool import reset_device_pool_for_tests

    monkeypatch.setenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", "2")
    pool = reset_device_pool_for_tests(slots=["cloud_phone:a"])
    occupied = pool.claim(
        task_id="other-task",
        session_id="other-session",
        lease_seconds=120,
    )
    monkeypatch.setattr(app_workflow_module, "is_postgres_device_lease_backend", lambda: True)
    workflow, runtime, _, sessions, _, publisher, _ = _harness()

    async def _free_slot() -> None:
        await asyncio.sleep(0.05)
        pool.release(lease=occupied)

    releaser = asyncio.create_task(_free_slot())
    await workflow.run(_command())
    await releaser
    assert "install" in runtime.events
    assert sessions.events
    assert publisher.handoffs
