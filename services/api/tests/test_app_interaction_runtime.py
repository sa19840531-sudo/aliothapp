"""App Screenshot 候选解析与动作执行边界测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.app_capture_persistence import PersistedAppCapture
from services.api.app.application.app_interaction_runtime import (
    APP_CANDIDATE_REVALIDATION_REQUIRED,
    AppCandidateResolver,
    AppCaptureWriter,
    AppInteractionExecutor,
    AppInteractionRuntimeError,
    AppRuntimeIdentity,
    DriverAppRuntimeIdentityReader,
)
from services.api.app.application.app_navigation_entry_provenance import (
    DISCOVERY_SOURCE_UIA_ISSUED,
    DISCOVERY_SOURCE_VISION,
)
from services.api.app.application.app_script_curriculum import APP_CURRICULUM_ACTION_VERSION
from services.api.app.application.app_semantic_progress import (
    APP_DYNAMIC_PAGE_SAMPLING_VERSION,
    APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT,
    APP_DYNAMIC_STARTUP_OBSERVATION_VERSION,
)
from services.api.app.application.capture_coordination import (
    CapturePhase,
    CaptureWriteRequest,
    ExecuteInteractionRequest,
)
from services.api.app.application.capture_profiles import (
    APP_DEFAULT_AFTER_ARTIFACT_TYPES,
    CaptureProfilePolicy,
)
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
    TrustedCommandScope,
)
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceDisabledError,
)
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureKind,
    CaptureRef,
    CommandPolicy,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    RawActionResult,
    RawArtifact,
    RawArtifactSet,
    RawSurfaceObservation,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSnapshot,
    SurfaceSessionStatus,
)
from services.api.app.domain.task import TaskTargetPlatform
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    SavedAppVisualObservation,
)

TASK_ID = "11111111-1111-1111-1111-111111111111"
RUN_ID = "22222222-2222-2222-2222-222222222222"
SESSION_ID = "app-session-1"
OBSERVATION_CAPTURE_ID = "33333333-3333-3333-3333-333333333333"
BEFORE_CAPTURE_ID = "44444444-4444-4444-4444-444444444444"
SIGNATURE = "sha256:" + "a" * 64
_DEFAULT_BEFORE_ANALYSIS = object()
_DEFAULT_NORMALIZED_BOUNDS = object()


class StaticSessionRepository:
    async def get(self, *, session_id: str) -> SurfaceSessionSnapshot | None:
        assert session_id == SESSION_ID
        now = datetime.now(UTC)
        return SurfaceSessionSnapshot(
            session_id=SESSION_ID,
            task_id=TASK_ID,
            surface_kind=SurfaceKind.ANDROID,
            session_owner="app-worker-1",
            status=SurfaceSessionStatus.ACTIVE,
            command_sequence=1,
            finalization_epoch=0,
            row_version=1,
            account_environment_ref=None,
            platform_build_ref="build-1",
            device_ref="device-1",
            current_surface_ref="fake-app:app-session-1",
            current_capture_id=BEFORE_CAPTURE_ID,
            started_at=now,
            updated_at=now,
        )


class StaticCaptureReader:
    def __init__(self, capture: PersistedAppCapture) -> None:
        self.capture = capture

    async def get_capture(self, *, capture_id: str) -> PersistedAppCapture | None:
        assert capture_id == BEFORE_CAPTURE_ID
        return self.capture


class StaticVisualRepository:
    def __init__(
        self,
        analysis: AppVisualAnalysis,
        before_analysis: AppVisualAnalysis | None = None,
    ) -> None:
        self.analysis = analysis
        self.before_analysis = before_analysis

    async def get_by_capture_id(self, *, capture_id: str):
        if capture_id == OBSERVATION_CAPTURE_ID:
            analysis = self.analysis
        elif capture_id == BEFORE_CAPTURE_ID:
            analysis = self.before_analysis
        else:
            raise AssertionError(f"未知视觉观察 Capture：{capture_id}")
        if analysis is None:
            return None
        return SavedAppVisualObservation(
            observation_id="55555555-5555-5555-5555-555555555555",
            analysis=analysis,
            observed_at=datetime.now(UTC),
            created=False,
        )


class RecordingDriver:
    def __init__(self) -> None:
        self.execute_count = 0
        self.last_intent = None

    async def execute(self, *, session, intent, action_execution_id):
        self.execute_count += 1
        self.last_intent = intent
        return RawActionResult(
            action_execution_id=action_execution_id,
            succeeded=True,
            retryable=False,
            payload={"action_trace": [{"action": "click"}]},
        )


class RuntimeObservationDriver(RecordingDriver):
    def __init__(self, *, package_name: str | None, activity: str | None) -> None:
        super().__init__()
        self._observation_payload = {
            "package_name": package_name,
            "activity": activity,
        }

    async def observe(self, *, session, profile):
        del profile
        return RawSurfaceObservation(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_kind=session.surface_kind,
            surface_ref=session.primary_surface_ref,
            payload=self._observation_payload,
        )


class StaticRuntimeIdentityReader:
    """AppRuntimeIdentityReader 的确定性测试替身。"""

    def __init__(
        self,
        *,
        package_name: str | None,
        activity: str | None,
        unavailable: bool = False,
    ) -> None:
        self.unavailable = unavailable
        self.identity = (
            None
            if unavailable or package_name is None
            else AppRuntimeIdentity(
                package_name=package_name,
                activity=activity,
            )
        )
        self.calls = []

    async def get_runtime_identity(
        self,
        *,
        session,
    ) -> AppRuntimeIdentity | None:
        self.calls.append(session)
        if self.unavailable:
            return None
        return self.identity


class CaptureDriver(RecordingDriver):
    def __init__(self) -> None:
        super().__init__()
        self.capture_calls = []

    async def capture(self, *, session, profile):
        self.capture_calls.append((session, profile))
        return RawArtifactSet(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            surface_ref=session.primary_surface_ref,
            artifacts=(
                RawArtifact(
                    artifact_type="screenshot",
                    content=(
                        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                        b"\x00\x00\x04\x38\x00\x00\x07\x80"
                        b"\x08\x06\x00\x00\x00\x00\x00\x00\x00"
                    ),
                ),
                RawArtifact(
                    artifact_type="action_trace",
                    content={"entries": []},
                ),
            ),
            metadata={
                "package_name": "com.example.target",
                "activity": ".MainActivity",
                "width": 1080,
                "height": 1920,
                "orientation": "portrait",
            },
        )


class MainProfileCaptureDriver(CaptureDriver):
    """返回默认 App 主 Profile 声明的全部正式制品。"""

    async def capture(self, *, session, profile):
        base = await super().capture(session=session, profile=profile)
        by_type = {item.artifact_type: item for item in base.artifacts}
        payloads = {
            "ui_tree": b'<hierarchy><node text="home" bounds="[0,0][1,1]"/></hierarchy>',
            "view_hierarchy": {"status": "captured", "nodes": []},
            "ui_snapshot": {"screen_id": "screen:main"},
            "visible_text": b"home",
            "media_state": {"schema_version": "app-media-state.v1", "items": []},
            "device_log": {"status": "captured"},
            "network_trace": {"status": "captured"},
            # 无代理时仍产出空 entries 负证据，与默认 after Profile 声明对齐。
            "har": {"log": {"version": "1.2", "entries": []}},
            "fault_artifact": {"fault_kind": "none", "status": "captured"},
        }
        for artifact_type, payload in payloads.items():
            if artifact_type in profile.after.artifact_types:
                by_type[artifact_type] = RawArtifact(
                    artifact_type=artifact_type,
                    content=payload,
                )
        return RawArtifactSet(
            task_id=base.task_id,
            session_id=base.session_id,
            surface_ref=base.surface_ref,
            artifacts=tuple(
                by_type[item] for item in profile.after.artifact_types if item in by_type
            ),
            metadata=base.metadata,
        )


class RecordingActionCapturePersistence:
    def __init__(self) -> None:
        self.calls = []

    async def persist_action_capture(self, **kwargs):
        self.calls.append(kwargs)
        return type(
            "Persisted",
            (),
            {
                "capture": CaptureRef(
                    task_id=kwargs["task_id"],
                    session_id=kwargs["session_id"],
                    capture_id="88888888-8888-8888-8888-888888888888",
                    surface_kind=kwargs["surface_kind"],
                    capture_kind=CaptureKind.APP,
                    role=kwargs["role"],
                    artifact_ids=("artifact-1",),
                    parent_capture_id=kwargs["parent_capture_id"],
                    action_execution_id=kwargs["action_execution_id"],
                )
            },
        )()

    async def persist_observation_capture(self, **kwargs):
        self.calls.append(kwargs)
        return type(
            "Persisted",
            (),
            {
                "capture": CaptureRef(
                    task_id=kwargs["task_id"],
                    session_id=kwargs["session_id"],
                    capture_id="99999999-9999-9999-9999-999999999999",
                    surface_kind=kwargs["surface_kind"],
                    capture_kind=CaptureKind.APP,
                    role="observation",
                    artifact_ids=("artifact-observation",),
                    parent_capture_id=kwargs["parent_capture_id"],
                    action_execution_id=None,
                )
            },
        )()


def _candidate(
    *,
    action_type: InteractionOperation = InteractionOperation.CLICK,
    semantic_name: str = "登录按钮",
    expected_state_hint: str | None = None,
    purpose: str = "进入登录页",
    risk_level: InteractionRiskLevel = InteractionRiskLevel.LOW,
    control_role: str | None = None,
    frontier_scope: str = "local",
    source_capture_id: str = OBSERVATION_CAPTURE_ID,
    source_image_signature: str = SIGNATURE,
    source_width: int = 1080,
    source_height: int = 1920,
    source_orientation: str = "portrait",
    normalized_bounds: NormalizedBounds | None | object = _DEFAULT_NORMALIZED_BOUNDS,
    confidence: float = 0.99,
) -> AppVisualCandidate:
    if normalized_bounds is _DEFAULT_NORMALIZED_BOUNDS:
        normalized_bounds = (
            NormalizedBounds(0.2, 0.7, 0.8, 0.9)
            if action_type is InteractionOperation.CLICK
            else None
        )
    return AppVisualCandidate.build(
        source_capture_id=source_capture_id,
        source_image_signature=source_image_signature,
        source_width=source_width,
        source_height=source_height,
        source_orientation=source_orientation,
        semantic_name=semantic_name,
        action_type=action_type,
        normalized_bounds=normalized_bounds,
        confidence=confidence,
        risk_level=risk_level,
        purpose=purpose,
        expected_state_hint=expected_state_hint,
        control_role=control_role,
        frontier_scope=frontier_scope,
    )


def _analysis(
    candidate: AppVisualCandidate,
    *,
    capture_id: str = OBSERVATION_CAPTURE_ID,
    screenshot_artifact_id: str = "66666666-6666-6666-6666-666666666666",
    image_signature: str = SIGNATURE,
    width: int = 1080,
    height: int = 1920,
    orientation: str = "portrait",
    package_name: str | None = "com.example.target",
    activity: str | None = ".MainActivity",
    visual_page_type: str = "login",
    page_state: str = "stable",
    overlay_signature: str | None = None,
    surface_facts: dict[str, object] | None = None,
    candidates: tuple[AppVisualCandidate, ...] | None = None,
) -> AppVisualAnalysis:
    return AppVisualAnalysis.build(
        capture_id=capture_id,
        screenshot_artifact_id=screenshot_artifact_id,
        image_signature=image_signature,
        width=width,
        height=height,
        orientation=orientation,
        package_name=package_name,
        activity=activity,
        visual_page_type=visual_page_type,
        visual_summary="登录页",
        key_elements=("登录按钮",),
        candidates=candidates if candidates is not None else (candidate,),
        provider_id="fake-vision",
        model_id="fake-model",
        page_state=page_state,
        overlay_signature=overlay_signature,
        surface_facts=surface_facts or {},
    )


def _before_visual_analysis(
    source_candidate: AppVisualCandidate,
    *,
    capture_id: str = BEFORE_CAPTURE_ID,
    screenshot_artifact_id: str = "77777777-7777-7777-7777-777777777777",
    image_signature: str = SIGNATURE,
    width: int = 1080,
    height: int = 1920,
    orientation: str = "portrait",
    package_name: str | None = "com.example.target",
    activity: str | None = ".MainActivity",
    semantic_name: str | None = None,
    expected_state_hint: str | None | object = _DEFAULT_BEFORE_ANALYSIS,
    purpose: str | None = None,
    control_role: str | None | object = _DEFAULT_BEFORE_ANALYSIS,
    frontier_scope: str | None | object = _DEFAULT_BEFORE_ANALYSIS,
    normalized_bounds: NormalizedBounds | None | object = _DEFAULT_NORMALIZED_BOUNDS,
    confidence: float = 0.99,
    risk_level: InteractionRiskLevel | None = None,
    visual_page_type: str = "login",
    page_state: str = "stable",
    overlay_signature: str | None = None,
    surface_facts: dict[str, object] | None = None,
    candidates: tuple[AppVisualCandidate, ...] | None = None,
) -> AppVisualAnalysis:
    """为动作前 Capture 构造与截图制品、当前候选一致的视觉观察。"""

    if expected_state_hint is _DEFAULT_BEFORE_ANALYSIS:
        expected_state_hint = source_candidate.expected_state_hint
    if control_role is _DEFAULT_BEFORE_ANALYSIS:
        control_role = source_candidate.control_role
    if frontier_scope is _DEFAULT_BEFORE_ANALYSIS:
        frontier_scope = source_candidate.frontier_scope
    if normalized_bounds is _DEFAULT_NORMALIZED_BOUNDS:
        normalized_bounds = source_candidate.normalized_bounds
    current_candidate = _candidate(
        action_type=source_candidate.action_type,
        semantic_name=semantic_name or source_candidate.semantic_name,
        expected_state_hint=expected_state_hint,
        purpose=purpose or source_candidate.purpose,
        risk_level=risk_level or source_candidate.risk_level,
        control_role=control_role,
        frontier_scope=frontier_scope or "local",
        source_capture_id=capture_id,
        source_image_signature=image_signature,
        source_width=width,
        source_height=height,
        source_orientation=orientation,
        normalized_bounds=normalized_bounds,
        confidence=confidence,
    )
    return _analysis(
        current_candidate,
        capture_id=capture_id,
        screenshot_artifact_id=screenshot_artifact_id,
        image_signature=image_signature,
        width=width,
        height=height,
        orientation=orientation,
        package_name=package_name,
        activity=activity,
        visual_page_type=visual_page_type,
        page_state=page_state,
        overlay_signature=overlay_signature,
        surface_facts=surface_facts,
        candidates=candidates,
    )


def _before_capture(
    *,
    image_signature: str = SIGNATURE,
    package_name: str | None = "com.example.target",
    activity: str | None = ".MainActivity",
    parent_capture_id: str = OBSERVATION_CAPTURE_ID,
    action_execution_id: str = "action-1",
    width: int = 1080,
    height: int = 1920,
) -> PersistedAppCapture:
    screenshot = PersistedEvidenceArtifact(
        artifact_id="77777777-7777-7777-7777-777777777777",
        artifact_type="screenshot",
        storage_path="task/capture/before.png",
        content_type="image/png",
        checksum=image_signature,
        size_bytes=33,
        metadata={"encoding": "binary"},
    )
    return PersistedAppCapture(
        capture=CaptureRef(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            capture_id=BEFORE_CAPTURE_ID,
            surface_kind=SurfaceKind.ANDROID,
            capture_kind=CaptureKind.APP,
            role="before_action",
            artifact_ids=(screenshot.artifact_id,),
            parent_capture_id=parent_capture_id,
            action_execution_id=action_execution_id,
        ),
        agent_run_id=RUN_ID,
        screenshot=screenshot,
        width=width,
        height=height,
        orientation="portrait",
        image_signature=image_signature,
        package_name=package_name,
        activity=activity,
    )


def _request(
    candidate: AppVisualCandidate,
    *,
    parameters: dict[str, object] | None = None,
    risk_level: InteractionRiskLevel | None = None,
) -> ExecuteInteractionRequest:
    request_parameters = {"candidate_id": candidate.candidate_id}
    request_parameters.update(parameters or {})
    intent = ActionIntent(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        intent_id="intent-1",
        idempotency_key="task-1:branch-1:intent-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=OBSERVATION_CAPTURE_ID,
        action_type=candidate.action_type,
        semantic_target=candidate.semantic_name,
        parameters=request_parameters,
        purpose="进入登录页",
        risk_level=risk_level if risk_level is not None else candidate.risk_level,
        capture_profile=CaptureProfilePolicy.for_surface(
            SurfaceKind.ANDROID,
            candidate.action_type,
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    return ExecuteInteractionRequest(
        agent_run_id=RUN_ID,
        intent=intent,
        source_capture_id=BEFORE_CAPTURE_ID,
        action_execution_id="action-1",
    )


def _executor(
    *,
    before_signature: str = SIGNATURE,
    candidate: AppVisualCandidate | None = None,
    analysis: AppVisualAnalysis | None = None,
    before_package_name: str | None = "com.example.target",
    before_activity: str | None = ".MainActivity",
    before_parent_capture_id: str = OBSERVATION_CAPTURE_ID,
    before_action_execution_id: str = "action-1",
    before_width: int = 1080,
    before_height: int = 1920,
    before_analysis=_DEFAULT_BEFORE_ANALYSIS,
    prepare_before_visual=None,
    driver: RecordingDriver | None = None,
    runtime_identity_reader: StaticRuntimeIdentityReader | None = None,
    allowed_packages: tuple[str, ...] = ("com.example.target",),
    command_validator: InteractionCommandValidator | None = None,
):
    command_validator = command_validator or InteractionCommandValidator()
    candidate = candidate or _candidate()
    driver = driver or RecordingDriver()
    source_analysis = analysis or _analysis(candidate)
    before_capture = _before_capture(
        image_signature=before_signature,
        package_name=before_package_name,
        activity=before_activity,
        parent_capture_id=before_parent_capture_id,
        action_execution_id=before_action_execution_id,
        width=before_width,
        height=before_height,
    )
    if before_analysis is _DEFAULT_BEFORE_ANALYSIS:
        before_analysis = _before_visual_analysis(
            candidate,
            image_signature=before_signature,
            width=before_width,
            height=before_height,
            package_name=before_package_name,
            activity=before_activity,
        )
    resolver = AppCandidateResolver(
        visual_observations=StaticVisualRepository(source_analysis, before_analysis),
        captures=StaticCaptureReader(before_capture),
        prepare_before_visual=prepare_before_visual,
    )
    return (
        AppInteractionExecutor(
            driver=driver,
            surface_sessions=StaticSessionRepository(),
            candidate_resolver=resolver,
            runtime_identity_reader=runtime_identity_reader,
            allowed_packages=allowed_packages,
            command_validator=command_validator,
        ),
        driver,
        candidate,
    )


def _bottom_nav_vision_candidate(
    *,
    capture_id: str,
    normalized_bounds: NormalizedBounds,
    semantic_name: str,
    purpose: str,
    expected_state_hint: str,
    frontier_scope: str,
    control_role: str | None = None,
) -> AppVisualCandidate:
    """构造绑定当前 Capture 的 Vision 底栏／邻近候选测试事实。"""

    return replace(
        _candidate(
            source_capture_id=capture_id,
            source_width=1440,
            source_height=3040,
            semantic_name=semantic_name,
            normalized_bounds=normalized_bounds,
            purpose=purpose,
            expected_state_hint=expected_state_hint,
            frontier_scope=frontier_scope,
            control_role=control_role,
        ),
        discovery_source=DISCOVERY_SOURCE_VISION,
    )


def _community_bottom_nav_runtime(
    *,
    source: AppVisualCandidate,
    current_candidates: tuple[AppVisualCandidate, ...],
    parameters: dict[str, object] | None = None,
) -> tuple[AppInteractionExecutor, RecordingDriver, ExecuteInteractionRequest]:
    """复用 App Runtime fixture，绑定社区源候选和动作前当前候选池。"""

    width, height = 1440, 3040
    surface_facts = {
        "trusted_accessibility_controls": {"bottom_navigation_count": 0},
    }
    before_analysis = _analysis(
        current_candidates[0],
        capture_id=BEFORE_CAPTURE_ID,
        screenshot_artifact_id="77777777-7777-7777-7777-777777777777",
        visual_page_type="video_list",
        page_state="stable",
        surface_facts=surface_facts,
        candidates=current_candidates,
        width=width,
        height=height,
    )
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(
            source,
            visual_page_type="video_list",
            page_state="stable",
            surface_facts=surface_facts,
            candidates=(source,),
            width=width,
            height=height,
        ),
        before_analysis=before_analysis,
        before_width=width,
        before_height=height,
    )
    return executor, driver, _request(source, parameters=parameters)


def test_interaction_executor_binds_verified_installed_package_once() -> None:
    executor, _, _ = _executor(allowed_packages=())

    executor.bind_installed_package("com.example.actual")
    executor.bind_installed_package("com.example.actual")

    assert executor._allowed_packages == ("com.example.actual",)


def test_interaction_executor_rejects_conflicting_installed_package() -> None:
    executor, _, _ = _executor(allowed_packages=("com.example.expected",))

    with pytest.raises(AppInteractionRuntimeError) as error:
        executor.bind_installed_package("com.example.actual")

    assert error.value.error_code == "APP_INSTALLED_PACKAGE_CONFLICT"


async def test_driver_runtime_identity_reader_accepts_only_complete_matching_observation() -> None:
    session = SurfaceSessionRef(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref="fake-app:app-session-1",
    )
    reader = DriverAppRuntimeIdentityReader(
        RuntimeObservationDriver(
            package_name="com.example.target",
            activity=".MainActivity",
        )
    )

    identity = await reader.get_runtime_identity(session=session)

    assert identity == AppRuntimeIdentity(
        package_name="com.example.target",
        activity=".MainActivity",
    )

    incomplete = DriverAppRuntimeIdentityReader(
        RuntimeObservationDriver(package_name="com.example.target", activity=None)
    )
    assert await incomplete.get_runtime_identity(session=session) is None


class LightweightIdentityDriver(RecordingDriver):
    def __init__(self, *, package_name: str, activity: str) -> None:
        super().__init__()
        self.observe_calls = 0
        self.identity_calls = 0
        self._package_name = package_name
        self._activity = activity

    async def observe(self, *, session, profile):
        del session, profile
        self.observe_calls += 1
        raise AssertionError("身分读取不得走全图 observe")

    async def read_runtime_identity(self, *, session):
        self.identity_calls += 1
        return RawSurfaceObservation(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_kind=session.surface_kind,
            surface_ref=session.primary_surface_ref,
            payload={
                "package_name": self._package_name,
                "activity": self._activity,
                "identity_source": "dumpsys",
            },
        )


@pytest.mark.asyncio
async def test_driver_runtime_identity_reader_prefers_lightweight_query() -> None:
    session = SurfaceSessionRef(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref="fake-app:app-session-1",
    )
    driver = LightweightIdentityDriver(
        package_name="com.example.target",
        activity=".MainActivity",
    )
    reader = DriverAppRuntimeIdentityReader(driver)

    identity = await reader.get_runtime_identity(session=session)

    assert identity == AppRuntimeIdentity(
        package_name="com.example.target",
        activity=".MainActivity",
    )
    assert driver.identity_calls == 1
    assert driver.observe_calls == 0


async def test_coordinated_before_capture_can_differ_in_pixels() -> None:
    """同一协调动作的 before 保全截图允许因弹窗/动画产生新的像素签名。"""
    executor, driver, candidate = _executor(before_signature="sha256:" + "b" * 64)

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1


async def test_resolved_visual_payment_target_cannot_use_agent_low_risk() -> None:
    candidate = _candidate(
        semantic_name="立即支付",
        purpose="完成订单支付",
        risk_level=InteractionRiskLevel.LOW,
    )

    class ScopeReader:
        async def read_command_scope(self, **_kwargs):
            return TrustedCommandScope(
                task_id=TASK_ID,
                session_id=SESSION_ID,
                surface_kind=SurfaceKind.ANDROID,
                policy=CommandPolicy.default(),
                finalization_epoch=0,
            )

    validator = InteractionCommandValidator(scope_reader=ScopeReader())
    executor, driver, _ = _executor(
        candidate=candidate,
        command_validator=validator,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "COMMAND_TARGET_RISK_UNDERRATED"
    assert result.state_summary["driver_invoked"] is False
    assert driver.execute_count == 0


async def test_resolved_target_rechecks_session_epoch_before_app_driver() -> None:
    candidate = _candidate()

    class ScopeReader:
        async def read_command_scope(self, **_kwargs):
            return TrustedCommandScope(
                task_id=TASK_ID,
                session_id=SESSION_ID,
                surface_kind=SurfaceKind.ANDROID,
                policy=CommandPolicy.default(),
                finalization_epoch=1,
            )

    validator = InteractionCommandValidator(scope_reader=ScopeReader())
    executor, driver, _ = _executor(
        candidate=candidate,
        command_validator=validator,
    )

    result = await executor.execute(
        replace(_request(candidate), finalization_epoch=0)
    )

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "COMMAND_SCOPE_EPOCH_MISMATCH"
    assert result.state_summary["driver_invoked"] is False
    assert driver.execute_count == 0


async def test_runtime_scope_rejects_unverified_current_package_before_app_driver() -> None:
    class ScopeReader:
        async def read_command_scope(self, **_kwargs):
            return TrustedCommandScope(
                task_id=TASK_ID,
                session_id=SESSION_ID,
                surface_kind=SurfaceKind.ANDROID,
                policy=CommandPolicy(allowed_packages=("com.example.target",)),
                finalization_epoch=0,
            )

    validator = InteractionCommandValidator(scope_reader=ScopeReader())
    runtime_reader = StaticRuntimeIdentityReader(
        package_name="com.example.unverified",
        activity=".MainActivity",
    )
    candidate = _candidate()
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(candidate, package_name="com.example.unverified"),
        before_package_name="com.example.unverified",
        allowed_packages=("com.example.unverified",),
        runtime_identity_reader=runtime_reader,
        command_validator=validator,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "COMMAND_PACKAGE_BOUNDARY_VIOLATION"
    assert result.state_summary["driver_invoked"] is False
    assert driver.execute_count == 0


async def test_touch_revalidation_rejects_business_candidate_on_current_ad() -> None:
    candidate = _candidate()
    before_analysis = _before_visual_analysis(
        candidate,
        visual_page_type="ad",
        page_state="startup_ad",
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_touch_revalidation_relocates_same_semantic_control_from_current_frame() -> None:
    candidate = _candidate()
    moved_bounds = NormalizedBounds(0.60, 0.50, 0.80, 0.70)
    before_analysis = _before_visual_analysis(
        candidate,
        normalized_bounds=moved_bounds,
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        before_analysis=before_analysis,
    )
    request = _request(
        candidate,
        parameters={"point": {"x": 1, "y": 2, "space": "device_px"}},
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.parameters["resolved_target"] == {
        "x": 756,
        "y": 1152,
        "space": "capture_px",
        "base_width": 1080,
        "base_height": 1920,
    }
    assert "point" not in driver.last_intent.parameters
    assert driver.last_intent.source_capture_id == BEFORE_CAPTURE_ID


async def test_touch_revalidation_rejects_disappeared_current_control() -> None:
    candidate = _candidate()
    before_analysis = _before_visual_analysis(candidate, candidates=())
    executor, driver, _ = _executor(
        candidate=candidate,
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_touch_revalidation_rejects_low_to_high_risk_control() -> None:
    candidate = _candidate()
    before_analysis = _before_visual_analysis(
        candidate,
        risk_level=InteractionRiskLevel.HIGH,
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_touch_revalidation_rejects_low_confidence_current_control() -> None:
    candidate = _candidate()
    before_analysis = _before_visual_analysis(candidate, confidence=0.69)
    executor, driver, _ = _executor(
        candidate=candidate,
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_touch_revalidation_rejects_multiple_matching_current_controls() -> None:
    candidate = _candidate()
    current_candidates = (
        _candidate(
            normalized_bounds=NormalizedBounds(0.10, 0.60, 0.30, 0.70),
            source_capture_id=BEFORE_CAPTURE_ID,
        ),
        _candidate(
            normalized_bounds=NormalizedBounds(0.70, 0.60, 0.90, 0.70),
            source_capture_id=BEFORE_CAPTURE_ID,
        ),
    )
    before_analysis = _before_visual_analysis(
        candidate,
        candidates=current_candidates,
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_dismiss_role_allows_language_rebind_on_popup() -> None:
    candidate = _candidate(
        semantic_name="关闭弹窗",
        expected_state_hint="close_popup_x",
        purpose="关闭当前弹窗",
        control_role="dismiss",
    )
    before_analysis = _before_visual_analysis(
        candidate,
        semantic_name="Fermer",
        expected_state_hint="close_popup_x",
        purpose="Fermer la fenêtre",
        control_role="dismiss",
        page_state="popup",
        surface_facts={"popup_visible": True},
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            page_state="popup",
            surface_facts={"popup_visible": True},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.parameters["resolved_target"] == {
        "x": 540,
        "y": 1536,
        "space": "capture_px",
        "base_width": 1080,
        "base_height": 1920,
    }


@pytest.mark.parametrize(
    ("has_conflict", "pixel_confirmed", "should_execute"),
    ((True, False, False), (False, False, True), (True, True, True)),
)
async def test_dismiss_rebind_resolves_current_accessibility_target_conflict(
    has_conflict: bool,
    pixel_confirmed: bool,
    should_execute: bool,
) -> None:
    """同帧普通控件反证关闭落点；当前像素关闭正证据仍允许覆盖该控件。"""

    source = _candidate(
        semantic_name="关闭弹窗",
        expected_state_hint="close_popup_x",
        purpose="关闭当前弹窗",
        control_role="dismiss",
        normalized_bounds=NormalizedBounds(0.93, 0.85, 0.95, 0.86),
    )
    current = replace(
        _candidate(
            semantic_name="关闭广告弹窗",
            expected_state_hint="close_popup_x",
            purpose=(
                "当前截图唯一孤立 X 像素结构校准"
                if pixel_confirmed
                else "关闭当前弹窗"
            ),
            control_role="dismiss",
            source_capture_id=BEFORE_CAPTURE_ID,
            normalized_bounds=NormalizedBounds(0.90, 0.88, 0.97, 0.91),
        ),
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    navigation = replace(
        _candidate(
            semantic_name="资料",
            purpose="进入资料页",
            frontier_scope="app",
            source_capture_id=BEFORE_CAPTURE_ID,
            normalized_bounds=(
                NormalizedBounds(0.81, 0.878, 0.98, 0.90)
                if has_conflict
                else NormalizedBounds(0.01, 0.878, 0.18, 0.90)
            ),
        ),
        discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
    )
    facts = {"popup_visible": True}
    if pixel_confirmed:
        facts["pixel_cross_close_calibration"] = {
            "source_candidate_id": current.candidate_id,
            "normalized_bounds": current.normalized_bounds.to_dict(),
            "detected_bounds": current.normalized_bounds.to_dict(),
            "mode": "issue",
            "vision_popup_surface": True,
            "ambiguity_count": 0,
            "precise_tap_refined": True,
        }
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(source, page_state="popup", surface_facts={"popup_visible": True}),
        before_analysis=_before_visual_analysis(
            source,
            page_state="popup",
            surface_facts=facts,
            candidates=(current, navigation),
        ),
    )

    result = await executor.execute(_request(source))

    if not should_execute:
        assert result.status is InteractionResultStatus.FAILED
        assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
        assert result.retryable is True
        assert driver.execute_count == 0
    else:
        assert result.status is InteractionResultStatus.EVIDENCE_PENDING
        assert driver.execute_count == 1
        assert driver.last_intent.source_capture_id == BEFORE_CAPTURE_ID
        assert driver.last_intent.parameters["resolved_target"] == {
            "x": 1010,
            "y": 1718,
            "space": "capture_px",
            "base_width": 1080,
            "base_height": 1920,
        }


async def test_dismiss_retry_fallback_does_not_revive_conflicting_vision_candidate() -> None:
    """dismiss 源候选即使带重试文案，也不得复活已被普通 UIA 反证的 Vision 落点。"""

    source = _candidate(
        semantic_name="重新加载",
        expected_state_hint="close_popup_x",
        purpose="关闭弹窗后重新加载",
        control_role="dismiss",
        normalized_bounds=NormalizedBounds(0.47, 0.47, 0.49, 0.49),
    )
    conflicting_vision = replace(
        _candidate(
            semantic_name="关闭广告弹窗",
            expected_state_hint="close_popup_x",
            purpose="关闭当前弹窗",
            control_role="dismiss",
            source_capture_id=BEFORE_CAPTURE_ID,
            normalized_bounds=NormalizedBounds(0.47, 0.47, 0.49, 0.49),
        ),
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    ordinary_accessibility = replace(
        _candidate(
            semantic_name="资料",
            purpose="进入资料页",
            source_capture_id=BEFORE_CAPTURE_ID,
            normalized_bounds=NormalizedBounds(0.35, 0.35, 0.75, 0.75),
        ),
        discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
    )
    before_analysis = _before_visual_analysis(
        source,
        visual_page_type="popup",
        page_state="popup",
        surface_facts={"popup_visible": True},
        candidates=(conflicting_vision, ordinary_accessibility),
    )
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(
            source,
            visual_page_type="popup",
            page_state="popup",
            surface_facts={"popup_visible": True},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(source))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_age_confirm_dismiss_allows_touch_when_page_type_unknown() -> None:
    """黑料年龄门：动作前帧 page_type 未知时仍须允许点「我年满18岁-进入」。"""

    candidate = _candidate(
        semantic_name="我年满18岁-进入",
        purpose="确认已满18岁并进入应用",
        expected_state_hint="confirm_age_18_enter",
        control_role="dismiss",
        normalized_bounds=NormalizedBounds(0.12, 0.72, 0.88, 0.84),
    )
    before_analysis = _before_visual_analysis(
        candidate,
        semantic_name="我年满18岁-进入",
        purpose="确认已满18岁并进入应用",
        expected_state_hint="confirm_age_18_enter",
        control_role="dismiss",
        visual_page_type="unknown",
        page_state="auth_gate",
        surface_facts={"dialog_kind": "age_verification"},
        normalized_bounds=NormalizedBounds(0.12, 0.72, 0.88, 0.84),
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="guide_page",
            page_state="auth_gate",
            surface_facts={"dialog_kind": "age_verification"},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert result.error_code is None


async def test_age_confirm_dismiss_allows_medium_risk_rebind() -> None:
    """Vision 将年龄门标为 medium 时，dismiss 重验仍须放行。"""

    candidate = _candidate(
        semantic_name="我年满18岁-进入",
        purpose="确认已满18岁并进入应用",
        expected_state_hint="confirm_age_18_enter",
        control_role="dismiss",
        risk_level=InteractionRiskLevel.MEDIUM,
        normalized_bounds=NormalizedBounds(0.12, 0.72, 0.88, 0.84),
    )
    before_candidate = _candidate(
        semantic_name="我年满18岁-进入",
        purpose="确认已满18岁并进入应用",
        expected_state_hint="confirm_age_18_enter",
        control_role="dismiss",
        risk_level=InteractionRiskLevel.MEDIUM,
        source_capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.12, 0.72, 0.88, 0.84),
    )
    before_analysis = _before_visual_analysis(
        candidate,
        candidates=(before_candidate,),
        visual_page_type="unknown",
        page_state="auth_gate",
        surface_facts={"auth_gate_visible": True},
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="guide_page",
            page_state="auth_gate",
            surface_facts={"auth_gate_visible": True},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(
        _request(candidate, risk_level=InteractionRiskLevel.LOW)
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1


async def test_dismiss_role_uses_position_to_disambiguate_multiple_close_controls() -> None:
    """远处的另一个关闭控件不应阻断源位置附近唯一控件的当前帧重绑。"""

    candidate = _candidate(
        semantic_name="弹窗主关闭",
        purpose="关闭当前主弹窗",
        control_role="dismiss",
    )
    matching = _candidate(
        semantic_name="关闭",
        purpose="关闭当前主弹窗",
        control_role="dismiss",
        normalized_bounds=NormalizedBounds(0.24, 0.71, 0.84, 0.89),
        source_capture_id=BEFORE_CAPTURE_ID,
    )
    distant = _candidate(
        semantic_name="次要关闭",
        purpose="关闭右上角次要浮层",
        control_role="dismiss",
        normalized_bounds=NormalizedBounds(0.84, 0.04, 0.96, 0.10),
        source_capture_id=BEFORE_CAPTURE_ID,
    )
    before_analysis = _before_visual_analysis(
        candidate,
        candidates=(matching, distant),
        page_state="popup",
        surface_facts={"popup_visible": True},
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            page_state="popup",
            surface_facts={"popup_visible": True},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.parameters["resolved_target"] == {
        "x": 583,
        "y": 1536,
        "space": "capture_px",
        "base_width": 1080,
        "base_height": 1920,
    }


async def test_dismiss_role_uses_position_when_source_misses_obstruction_state() -> None:
    """源帧漏报遮挡时，签名关闭角色和稳定位置仍可绑定当前帧控件。"""

    candidate = _candidate(
        semantic_name="关闭弹窗",
        expected_state_hint="close_popup_x",
        purpose="关闭当前弹窗",
        control_role="dismiss",
    )
    before_analysis = _before_visual_analysis(
        candidate,
        semantic_name="关闭广告",
        expected_state_hint="close_popup_x",
        purpose="关闭当前广告弹窗",
        control_role="dismiss",
        page_state="popup",
        surface_facts={
            "popup_visible": True,
            "topmost_ad_popup": {"is_ad": True},
        },
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(candidate, page_state="stable"),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.parameters["resolved_target"] == {
        "x": 540,
        "y": 1536,
        "space": "capture_px",
        "base_width": 1080,
        "base_height": 1920,
    }


async def test_skip_role_rebinds_rotating_startup_ad_with_current_frame_geometry() -> None:
    """轮播素材和控件框抖动不得阻断当前帧唯一的右上跳过控件。"""

    candidate = _candidate(
        semantic_name="跳过广告",
        expected_state_hint="close_startup_ad",
        purpose="跳过当前启动广告",
        control_role="skip",
        normalized_bounds=NormalizedBounds(0.78, 0.018, 0.96, 0.058),
    )
    current_bounds = NormalizedBounds(0.72, 0.07, 0.93, 0.125)
    before_analysis = _before_visual_analysis(
        candidate,
        semantic_name="跳过",
        expected_state_hint="dismiss_startup_ad",
        purpose="跳过轮播广告",
        control_role="skip",
        normalized_bounds=current_bounds,
        visual_page_type="startup_ad",
        page_state="countdown",
        surface_facts={"countdown_visible": True},
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="startup_ad",
            page_state="startup_ad",
            surface_facts={"startup_ad_visible": True},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.parameters["resolved_target"] == {
        "x": 891,
        "y": 187,
        "space": "capture_px",
        "base_width": 1080,
        "base_height": 1920,
    }


async def test_skip_role_prefers_trusted_accessibility_enter_when_vision_ambiguous() -> None:
    """当前帧已有唯一 a11y「进入」时，即使 Vision 同角色候选不唯一也采用 a11y 坐标。"""

    candidate = _candidate(
        semantic_name="跳过广告：进入",
        expected_state_hint="tap_skip",
        purpose="点击目标包唯一可访问性开屏退出控件，跳过广告进入业务页",
        control_role="skip",
        normalized_bounds=NormalizedBounds(0.81, 0.04, 0.95, 0.08),
    )
    trusted_bounds = NormalizedBounds(1171 / 1440, 144 / 3040, 1363 / 1440, 240 / 3040)
    vision_a = _candidate(
        semantic_name="跳过",
        expected_state_hint="tap_skip",
        purpose="视觉猜测跳过",
        control_role="skip",
        source_capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.70, 0.02, 0.90, 0.09),
    )
    vision_b = _candidate(
        semantic_name="进入应用",
        expected_state_hint="tap_skip",
        purpose="视觉猜测进入",
        control_role="skip",
        source_capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.75, 0.01, 0.98, 0.12),
    )
    before_analysis = _before_visual_analysis(
        candidate,
        visual_page_type="startup_ad",
        page_state="startup_ad",
        package_name="com.example.target",
        surface_facts={
            "startup_ad_visible": True,
            "trusted_accessibility_skip": {
                "package_name": "com.example.target",
                "label": "进入",
                "normalized_bounds": trusted_bounds.to_dict(),
            },
        },
        candidates=(vision_a, vision_b),
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="startup_ad",
            page_state="startup_ad",
            package_name="com.example.target",
            surface_facts={"startup_ad_visible": True},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    target = driver.last_intent.parameters["resolved_target"]
    assert target["x"] == round(((1171 + 1363) / 2) / 1440 * 1080)
    assert target["y"] == round(((144 + 240) / 2) / 3040 * 1920)


async def test_dismiss_role_prefers_trusted_accessibility_close_when_vision_ambiguous() -> None:
    """开屏／弹层唯一 a11y「关闭」在 Vision 重绑失败时仍须采用 a11y 坐标。"""

    candidate = _candidate(
        semantic_name="关闭弹层：关闭",
        expected_state_hint="close_popup_x",
        purpose="点击目标包当前弹层的可信关闭控件「关闭」并继续清障",
        control_role="dismiss",
        normalized_bounds=NormalizedBounds(0.94, 0.12, 1.0, 0.21),
    )
    trusted_bounds = NormalizedBounds(2569 / 2730, 182 / 1440, 2730 / 2730, 308 / 1440)
    vision_a = _candidate(
        semantic_name="关闭",
        expected_state_hint="close_popup_ad",
        purpose="视觉猜测关闭",
        control_role="dismiss",
        source_capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.70, 0.02, 0.90, 0.12),
    )
    vision_b = _candidate(
        semantic_name="倒计时跳过",
        expected_state_hint="tap_skip",
        purpose="视觉猜测跳过",
        control_role="dismiss",
        source_capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.75, 0.01, 0.98, 0.15),
    )
    before_analysis = _before_visual_analysis(
        candidate,
        visual_page_type="startup_ad",
        page_state="startup_ad",
        package_name="com.example.target",
        surface_facts={
            "startup_ad_visible": True,
            "popup_visible": True,
            "trusted_accessibility_dismiss": {
                "package_name": "com.example.target",
                "label": "关闭",
                "normalized_bounds": trusted_bounds.to_dict(),
            },
        },
        candidates=(vision_a, vision_b),
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="popup",
            page_state="popup",
            package_name="com.example.target",
            surface_facts={"popup_visible": True},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    target = driver.last_intent.parameters["resolved_target"]
    assert target["x"] == round(((2569 + 2730) / 2) / 2730 * 1080)
    assert target["y"] == round(((182 + 308) / 2) / 1440 * 1920)


async def test_dismiss_role_prefers_pixel_when_a11y_diverges_from_calibration() -> None:
    """a11y 关闭框偏得过远时，须改信像素精准校准落点。"""

    pixel_bounds = NormalizedBounds(0.46, 0.70, 0.54, 0.76)
    candidate = _candidate(
        semantic_name="关闭广告弹窗",
        expected_state_hint="close_popup_x",
        purpose="点击当前截图唯一孤立关闭控件并关闭广告弹层；当前截图唯一孤立 X 像素结构校准",
        control_role="dismiss",
        normalized_bounds=pixel_bounds,
    )
    trusted_bounds = NormalizedBounds(0.90, 0.08, 1.0, 0.16)
    before_analysis = _before_visual_analysis(
        candidate,
        visual_page_type="home",
        page_state="popup",
        package_name="com.example.target",
        surface_facts={
            "popup_visible": True,
            "trusted_accessibility_dismiss": {
                "package_name": "com.example.target",
                "label": "关闭",
                "normalized_bounds": trusted_bounds.to_dict(),
            },
            "pixel_cross_close_calibration": {
                "mode": "issue",
                "normalized_bounds": pixel_bounds.to_dict(),
                "precise_tap_refined": True,
            },
        },
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="popup",
            page_state="popup",
            package_name="com.example.target",
            surface_facts={"popup_visible": True},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    target = driver.last_intent.parameters["resolved_target"]
    assert target["x"] == round(0.5 * 1080)
    assert target["y"] == round(0.73 * 1920)


@pytest.mark.parametrize(
    ("page_type", "calibration_case", "expected_execute"),
    [
        (None, "matched", True),
        ("unknown", "matched", True),
        (None, "missing", False),
        (None, "mismatched_bounds", False),
        (None, "payment", False),
        (None, "no_role", False),
    ],
)
async def test_current_pixel_close_does_not_require_business_page_type(
    page_type: str | None,
    calibration_case: str,
    expected_execute: bool,
) -> None:
    """业务页型缺失时，只执行当前帧已证明的真实像素关闭控件。"""

    bounds = NormalizedBounds(0.46, 0.70, 0.54, 0.76)
    candidate = _candidate(
        semantic_name="关闭广告弹窗",
        expected_state_hint="close_popup_x",
        purpose="关闭当前弹窗；当前截图唯一孤立 X 像素结构校准",
        control_role=None if calibration_case == "no_role" else "dismiss",
        normalized_bounds=bounds,
    )
    facts = {"popup_visible": True}
    if calibration_case != "missing":
        facts["pixel_cross_close_calibration"] = {
            "normalized_bounds": (
                NormalizedBounds(0.80, 0.10, 0.90, 0.20)
                if calibration_case == "mismatched_bounds"
                else bounds
            ).to_dict(),
            "mode": "issue",
            "precise_tap_refined": True,
        }
    if calibration_case == "payment":
        facts["dialog_kind"] = "payment"
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="home",
            page_state="popup",
            surface_facts={"popup_visible": True},
        ),
        before_analysis=_before_visual_analysis(
            candidate,
            visual_page_type=page_type,
            page_state="popup",
            surface_facts=facts,
        ),
    )

    result = await executor.execute(_request(candidate))

    if expected_execute:
        assert result.status is InteractionResultStatus.EVIDENCE_PENDING
        assert driver.execute_count == 1
        assert driver.last_intent.source_capture_id == BEFORE_CAPTURE_ID
        assert driver.last_intent.parameters["resolved_target"] == {
            "x": round(bounds.center[0] * 1080),
            "y": round(bounds.center[1] * 1920),
            "space": "capture_px",
            "base_width": 1080,
            "base_height": 1920,
        }
    else:
        assert result.status is InteractionResultStatus.FAILED
        assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
        assert driver.execute_count == 0


@pytest.mark.parametrize("current_visual_page_type", [None, "home"])
async def test_dismiss_role_keeps_trusted_a11y_when_distant_pixel_is_filtered(
    current_visual_page_type: str | None,
) -> None:
    """远处像素候选被过滤后，仍须执行当前帧同位置可信 UIA 关闭控件。"""

    trusted_bounds = NormalizedBounds(0.84, 0.08, 0.90, 0.14)
    distant_pixel_bounds = NormalizedBounds(0.24, 0.08, 0.30, 0.14)
    candidate = _candidate(
        semantic_name="关闭弹层：关闭",
        expected_state_hint="close_popup_x",
        purpose="点击当前弹层可信 UIA 关闭控件",
        control_role="dismiss",
        normalized_bounds=trusted_bounds,
    )
    current_a11y = _candidate(
        semantic_name="关闭弹层：关闭",
        expected_state_hint="close_popup_x",
        purpose="点击当前弹层可信 UIA 关闭控件",
        control_role="dismiss",
        source_capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=trusted_bounds,
    )
    current_pixel = _candidate(
        semantic_name="关闭广告弹窗",
        expected_state_hint="close_popup_x",
        purpose=(
            "点击当前截图唯一孤立关闭控件并关闭广告弹层；"
            "当前截图唯一孤立 X 像素结构校准"
        ),
        control_role="dismiss",
        source_capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=distant_pixel_bounds,
    )
    surface_facts = {
        "popup_visible": True,
        "dismiss_control_visible": True,
        "dismiss_control_enabled": True,
        "dismiss_semantic_target": "关闭广告弹窗",
        "trusted_accessibility_dismiss": {
            "package_name": "com.example.target",
            "label": "关闭",
            "normalized_bounds": trusted_bounds.to_dict(),
        },
        "pixel_cross_close_calibration": {
            "source_candidate_id": current_pixel.candidate_id,
            "normalized_bounds": distant_pixel_bounds.to_dict(),
            "detected_bounds": distant_pixel_bounds.to_dict(),
            "mode": "issue",
            "vision_popup_surface": True,
            "ambiguity_count": 0,
            "precise_tap_refined": True,
        },
    }
    before_analysis = _before_visual_analysis(
        candidate,
        semantic_name="关闭弹层：关闭",
        expected_state_hint="close_popup_x",
        purpose="点击当前弹层可信 UIA 关闭控件",
        control_role="dismiss",
        visual_page_type=current_visual_page_type,
        page_state="popup",
        surface_facts=surface_facts,
        candidates=(current_pixel, current_a11y),
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="popup",
            page_state="popup",
            surface_facts={"popup_visible": True},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent is not None
    assert driver.last_intent.source_capture_id == BEFORE_CAPTURE_ID
    assert driver.last_intent.parameters["resolved_target"] == {
        "x": round(trusted_bounds.center[0] * 1080),
        "y": round(trusted_bounds.center[1] * 1920),
        "space": "capture_px",
        "base_width": 1080,
        "base_height": 1920,
    }


@pytest.mark.parametrize("filter_reason", ["distant", "high_risk", "low_confidence"])
async def test_dismiss_role_does_not_rebuild_filtered_pixel_without_trusted_a11y(
    filter_reason: str,
) -> None:
    """没有可信 UIA 时，事实中的失配像素候选不能被重造后执行。"""

    source_bounds = NormalizedBounds(0.84, 0.08, 0.90, 0.14)
    pixel_bounds = (
        NormalizedBounds(0.24, 0.08, 0.30, 0.14)
        if filter_reason == "distant"
        else source_bounds
    )
    source_candidate = _candidate(
        semantic_name="关闭广告弹窗",
        expected_state_hint="close_popup_x",
        purpose=(
            "点击当前截图唯一孤立关闭控件并关闭广告弹层；"
            "当前截图唯一孤立 X 像素结构校准"
        ),
        control_role="dismiss",
        normalized_bounds=source_bounds,
    )
    current_pixel = _candidate(
        semantic_name="关闭广告弹窗",
        expected_state_hint="close_popup_x",
        purpose=(
            "点击当前截图唯一孤立关闭控件并关闭广告弹层；"
            "当前截图唯一孤立 X 像素结构校准"
        ),
        control_role="dismiss",
        source_capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=pixel_bounds,
        confidence=0.69 if filter_reason == "low_confidence" else 0.99,
        risk_level=(
            InteractionRiskLevel.HIGH
            if filter_reason == "high_risk"
            else InteractionRiskLevel.LOW
        ),
    )
    surface_facts = {
        "popup_visible": True,
        "pixel_cross_close_calibration": {
            "source_candidate_id": current_pixel.candidate_id,
            "normalized_bounds": pixel_bounds.to_dict(),
            "detected_bounds": pixel_bounds.to_dict(),
            "mode": "issue",
            "vision_popup_surface": True,
            "ambiguity_count": 0,
            "precise_tap_refined": True,
        },
    }
    before_analysis = _before_visual_analysis(
        source_candidate,
        visual_page_type=None,
        page_state="popup",
        surface_facts=surface_facts,
        candidates=(current_pixel,),
    )
    executor, driver, _ = _executor(
        candidate=source_candidate,
        analysis=_analysis(
            source_candidate,
            visual_page_type="popup",
            page_state="popup",
            surface_facts={"popup_visible": True},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(source_candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_dismiss_role_ignores_animated_ad_page_classification_changes() -> None:
    """动图帧改写页型和弹层状态时，只要关闭控件位置稳定就使用当前坐标。"""

    candidate = _candidate(
        semantic_name="关闭广告",
        expected_state_hint="close_popup_ad",
        purpose="关闭当前广告弹窗",
        control_role="dismiss",
        normalized_bounds=NormalizedBounds(0.82, 0.05, 0.96, 0.11),
    )
    current_bounds = NormalizedBounds(0.77, 0.08, 0.93, 0.15)
    before_analysis = _before_visual_analysis(
        candidate,
        semantic_name="关闭",
        expected_state_hint="dismiss_overlay",
        purpose="关闭当前动图遮挡",
        control_role="dismiss",
        normalized_bounds=current_bounds,
        visual_page_type="content_list",
        page_state="overlay",
        surface_facts={"popup_visible": True},
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="home",
            page_state="popup",
            surface_facts={
                "popup_visible": True,
                "topmost_ad_popup": {"is_ad": True},
            },
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.parameters["resolved_target"] == {
        "x": 918,
        "y": 221,
        "space": "capture_px",
        "base_width": 1080,
        "base_height": 1920,
    }


async def test_skip_role_rejects_control_that_moves_to_another_screen_zone() -> None:
    """位置容差仍须拒绝从右上角跳到屏幕底部的可疑控件。"""

    candidate = _candidate(
        semantic_name="跳过广告",
        purpose="跳过当前启动广告",
        control_role="skip",
        normalized_bounds=NormalizedBounds(0.78, 0.018, 0.96, 0.058),
    )
    before_analysis = _before_visual_analysis(
        candidate,
        semantic_name="跳过",
        purpose="底部未知跳过控件",
        control_role="skip",
        normalized_bounds=NormalizedBounds(0.72, 0.80, 0.93, 0.90),
        visual_page_type="startup_ad",
        page_state="startup_ad",
        surface_facts={"startup_ad_visible": True},
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="startup_ad",
            page_state="startup_ad",
            surface_facts={"startup_ad_visible": True},
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert driver.execute_count == 0


async def test_bottom_nav_rebinds_from_accessibility_facts_on_popup_frame() -> None:
    """中部弹层裁掉 candidates 时，仍可按 a11y 底栏事实重绑并放行触控。"""

    from services.api.app.application.app_navigation_entry_provenance import (
        DISCOVERY_SOURCE_UIA_ISSUED,
    )

    candidate = AppVisualCandidate.build(
        source_capture_id=OBSERVATION_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="底栏「交友」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.40, 0.92, 0.60, 0.99),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开可见底栏导航「交友」",
        expected_state_hint="activity",
        frontier_scope="app",
        discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
    )
    close = AppVisualCandidate.build(
        source_capture_id=BEFORE_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="关闭弹层",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.45, 0.35, 0.55, 0.42),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose="关闭弹窗",
        expected_state_hint="close_popup_x",
        control_role="dismiss",
        frontier_scope="local",
    )
    surface_facts = {
        "popup_visible": True,
        "topmost_ad_popup": {
            "is_ad": True,
            "confidence": 0.95,
            "normalized_bounds": {
                "left": 0.10,
                "top": 0.18,
                "right": 0.90,
                "bottom": 0.70,
            },
        },
        "trusted_accessibility_controls": {
            "overlay_close": True,
            "bottom_navigation_count": 2,
            "bottom_navigation": (
                {
                    "label": "交友",
                    "page_type": "activity",
                    "action_allowed": True,
                    "selected": False,
                    "bounds": {
                        "left": 432,
                        "top": 1766,
                        "right": 648,
                        "bottom": 1900,
                    },
                },
                {
                    "label": "我的",
                    "page_type": "profile",
                    "action_allowed": True,
                    "selected": False,
                    "bounds": {
                        "left": 864,
                        "top": 1766,
                        "right": 1080,
                        "bottom": 1900,
                    },
                },
            ),
        },
    }
    before_analysis = _analysis(
        close,
        capture_id=BEFORE_CAPTURE_ID,
        screenshot_artifact_id="77777777-7777-7777-7777-777777777777",
        visual_page_type="video_list",
        page_state="popup",
        overlay_signature="popup:video-ad",
        surface_facts=surface_facts,
        candidates=(close,),
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="video_list",
            page_state="popup",
            surface_facts=surface_facts,
        ),
        before_analysis=before_analysis,
    )
    request = _request(
        candidate,
        parameters={"bottom_navigation_tab_seek": True},
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    resolved = driver.last_intent.parameters["resolved_target"]
    assert resolved["space"] == "capture_px"
    assert 1700 <= int(resolved["y"]) <= 1920


async def test_bottom_nav_rejects_source_only_slot_when_current_video_detail_has_no_nav() -> None:
    """当前详情帧没有底栏事实时，禁止按源帧水平槽合成触控落点。"""

    from services.api.app.application.app_navigation_entry_provenance import (
        DISCOVERY_SOURCE_UIA_ISSUED,
    )

    candidate = AppVisualCandidate.build(
        source_capture_id=OBSERVATION_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1440,
        source_height=3040,
        source_orientation="portrait",
        semantic_name="底栏主页",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.05, 0.85, 0.165, 0.90),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开底栏主页",
        expected_state_hint="home",
        frontier_scope="app",
        discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
    )
    before_analysis = _before_visual_analysis(
        candidate,
        width=1440,
        height=3040,
        visual_page_type="video_detail",
        page_state="stable",
        surface_facts={
            "trusted_accessibility_controls": {"bottom_navigation_count": 0},
        },
        candidates=(),
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            width=1440,
            height=3040,
            visual_page_type="home",
            page_state="stable",
        ),
        before_analysis=before_analysis,
        before_width=1440,
        before_height=3040,
    )

    result = await executor.execute(
        _request(candidate, parameters={"bottom_navigation_tab_seek": True})
    )

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_bottom_nav_rebinds_same_semantic_current_vision_candidate() -> None:
    """当前帧有同语义 Vision 底栏候选时，按当前 Capture 坐标只触控一次。"""

    from services.api.app.application.app_navigation_entry_provenance import (
        DISCOVERY_SOURCE_UIA_ISSUED,
        DISCOVERY_SOURCE_VISION,
    )

    source = AppVisualCandidate.build(
        source_capture_id=OBSERVATION_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1440,
        source_height=3040,
        source_orientation="portrait",
        semantic_name="底栏主页",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.05, 0.85, 0.165, 0.90),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开底栏主页",
        expected_state_hint="home",
        frontier_scope="app",
        discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
    )
    current = AppVisualCandidate.build(
        source_capture_id=BEFORE_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1440,
        source_height=3040,
        source_orientation="portrait",
        semantic_name="底栏主页",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.30, 0.80, 0.42, 0.87),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开底栏主页",
        expected_state_hint="home",
        frontier_scope="app",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    before_analysis = _before_visual_analysis(
        source,
        width=1440,
        height=3040,
        visual_page_type="video_detail",
        page_state="stable",
        surface_facts={
            "trusted_accessibility_controls": {"bottom_navigation_count": 0},
        },
        candidates=(current,),
    )
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(
            source,
            width=1440,
            height=3040,
            visual_page_type="home",
            page_state="stable",
        ),
        before_analysis=before_analysis,
        before_width=1440,
        before_height=3040,
    )

    result = await executor.execute(
        _request(source, parameters={"bottom_navigation_tab_seek": True})
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.source_capture_id == BEFORE_CAPTURE_ID
    assert driver.last_intent.parameters["resolved_target"] == {
        "x": 518,
        "y": 2538,
        "space": "capture_px",
        "base_width": 1440,
        "base_height": 3040,
    }


async def test_bottom_nav_prefers_a11y_over_vision_gesture_zone_bounds() -> None:
    """Vision 把底栏画进系统手势区时，必须改用 a11y 像素条带，避免点回桌面。"""

    from services.api.app.application.app_navigation_entry_provenance import (
        DISCOVERY_SOURCE_UIA_ISSUED,
        DISCOVERY_SOURCE_VISION,
    )

    width, height = 1440, 3040
    # Vision 错误：条带落在 0.925–0.985（约 y=2900，系统手势区）。
    # a11y 真条带：2644–2730（杏吧实机）。
    def _vision_wrong(*, capture_id: str) -> AppVisualCandidate:
        return AppVisualCandidate.build(
            source_capture_id=capture_id,
            source_image_signature=SIGNATURE,
            source_width=width,
            source_height=height,
            source_orientation="portrait",
            semantic_name="底栏「交友」",
            action_type=InteractionOperation.CLICK,
            normalized_bounds=NormalizedBounds(0.42, 0.925, 0.58, 0.985),
            confidence=0.99,
            risk_level=InteractionRiskLevel.LOW,
            purpose="打开可见底栏导航「交友」",
            expected_state_hint="activity",
            frontier_scope="app",
            discovery_source=DISCOVERY_SOURCE_VISION,
        )

    vision_before = _vision_wrong(capture_id=BEFORE_CAPTURE_ID)
    vision_obs = _vision_wrong(capture_id=OBSERVATION_CAPTURE_ID)
    source = AppVisualCandidate.build(
        source_capture_id=OBSERVATION_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=width,
        source_height=height,
        source_orientation="portrait",
        semantic_name="底栏「交友」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.42, 0.925, 0.58, 0.985),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开可见底栏导航「交友」",
        expected_state_hint="activity",
        frontier_scope="app",
        discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
    )
    surface_facts = {
        "trusted_accessibility_controls": {
            "overlay_close": False,
            "bottom_navigation_count": 1,
            "bottom_navigation": (
                {
                    "label": "交友",
                    "page_type": "activity",
                    "action_allowed": True,
                    "selected": False,
                    "bounds": {
                        "left": 576,
                        "top": 2644,
                        "right": 864,
                        "bottom": 2730,
                    },
                },
            ),
        },
    }
    before_analysis = _analysis(
        vision_before,
        capture_id=BEFORE_CAPTURE_ID,
        screenshot_artifact_id="77777777-7777-7777-7777-777777777777",
        visual_page_type="video_list",
        page_state="stable",
        surface_facts=surface_facts,
        candidates=(vision_before,),
        width=width,
        height=height,
    )
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(
            source,
            visual_page_type="video_list",
            page_state="stable",
            surface_facts=surface_facts,
            candidates=(vision_obs,),
            width=width,
            height=height,
        ),
        before_analysis=before_analysis,
        before_width=width,
        before_height=height,
    )
    request = _request(
        source,
        parameters={"bottom_navigation_tab_seek": True},
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    resolved = driver.last_intent.parameters["resolved_target"]
    assert resolved["space"] == "capture_px"
    # a11y 中心约 2687；Vision 错误中心约 2903。
    assert 2644 <= int(resolved["y"]) <= 2730
    assert int(resolved["y"]) < 2800


@pytest.mark.parametrize(
    ("current_frontier_scope", "tab_seek"),
    [
        pytest.param("app", False, id="app_bottom_nav"),
        pytest.param("page", True, id="tab_seek_vision_band"),
    ],
)
async def test_bottom_nav_rejects_vision_gesture_zone_when_a11y_missing(
    current_frontier_scope: str,
    tab_seek: bool,
) -> None:
    """无 a11y 时 Vision 落在系统手势带，必须拒绝并等待当前帧真实重绑。"""

    from services.api.app.application.app_navigation_entry_provenance import (
        DISCOVERY_SOURCE_VISION,
    )

    width, height = 1440, 3040
    source = AppVisualCandidate.build(
        source_capture_id=OBSERVATION_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=width,
        source_height=height,
        source_orientation="portrait",
        semantic_name="底栏「交友」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.42, 0.925, 0.58, 0.985),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开可见底栏导航「交友」",
        expected_state_hint="activity",
        frontier_scope="app",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    before = AppVisualCandidate.build(
        source_capture_id=BEFORE_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=width,
        source_height=height,
        source_orientation="portrait",
        semantic_name="底栏「交友」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.42, 0.925, 0.58, 0.985),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开可见底栏导航「交友」",
        expected_state_hint="activity",
        frontier_scope=current_frontier_scope,
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    surface_facts = {
        "trusted_accessibility_controls": {"bottom_navigation_count": 0},
    }
    before_analysis = _analysis(
        before,
        capture_id=BEFORE_CAPTURE_ID,
        screenshot_artifact_id="77777777-7777-7777-7777-777777777777",
        visual_page_type="video_list",
        page_state="stable",
        surface_facts=surface_facts,
        candidates=(before,),
        width=width,
        height=height,
    )
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(
            source,
            visual_page_type="video_list",
            page_state="stable",
            surface_facts=surface_facts,
            candidates=(source,),
            width=width,
            height=height,
        ),
        before_analysis=before_analysis,
        before_width=width,
        before_height=height,
    )
    parameters = {"bottom_navigation_tab_seek": True} if tab_seek else {}
    result = await executor.execute(_request(source, parameters=parameters))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


@pytest.mark.parametrize(
    "tab_seek",
    [pytest.param(False, id="normal_bottom_nav"), pytest.param(True, id="tab_seek")],
)
async def test_bottom_nav_rejects_announcement_close_as_community(
    tab_seek: bool,
) -> None:
    """社区在手势区时，邻近公告关闭控件不得冒充社区底栏入口。"""

    source = _bottom_nav_vision_candidate(
        capture_id=OBSERVATION_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.66, 0.925, 0.78, 0.985),
        semantic_name="底栏「社区」",
        purpose="打开可见底栏导航「社区」",
        expected_state_hint="post_list",
        frontier_scope="app",
    )
    current_community = _bottom_nav_vision_candidate(
        capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.66, 0.925, 0.78, 0.985),
        semantic_name="底栏「社区」",
        purpose="打开可见底栏导航「社区」",
        expected_state_hint="post_list",
        frontier_scope="app",
    )
    announcement_close = _bottom_nav_vision_candidate(
        capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.939, 0.854, 0.949, 0.859),
        semantic_name="关闭公告",
        purpose="关闭当前公告",
        expected_state_hint="close_popup_x",
        frontier_scope="local",
        control_role="dismiss",
    )
    executor, driver, request = _community_bottom_nav_runtime(
        source=source,
        current_candidates=(current_community, announcement_close),
        parameters={"bottom_navigation_tab_seek": True} if tab_seek else {},
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


@pytest.mark.parametrize(
    "tab_seek",
    [pytest.param(False, id="normal_bottom_nav"), pytest.param(True, id="tab_seek")],
)
async def test_bottom_nav_rejects_bottom_body_as_community_navigation(
    tab_seek: bool,
) -> None:
    """社区底部正文即使靠近导航槽，也不得被提升为社区底栏入口。"""

    source = _bottom_nav_vision_candidate(
        capture_id=OBSERVATION_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.66, 0.925, 0.78, 0.985),
        semantic_name="底栏「社区」",
        purpose="打开可见底栏导航「社区」",
        expected_state_hint="post_list",
        frontier_scope="app",
    )
    current_body = _bottom_nav_vision_candidate(
        capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.68, 0.84, 0.76, 0.90),
        semantic_name="社区正文卡片",
        purpose="打开当前社区正文内容卡片",
        expected_state_hint="post_detail",
        frontier_scope="page",
    )
    executor, driver, request = _community_bottom_nav_runtime(
        source=source,
        current_candidates=(current_body,),
        parameters={"bottom_navigation_tab_seek": True} if tab_seek else {},
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_bottom_nav_allows_formal_page_scope_community_navigation() -> None:
    """当前页型正式且同名的 page 级社区导航仍可按真实安全框执行。"""

    source = _bottom_nav_vision_candidate(
        capture_id=OBSERVATION_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.66, 0.84, 0.78, 0.90),
        semantic_name="底栏「社区」",
        purpose="打开可见底栏导航「社区」",
        expected_state_hint="post_list",
        frontier_scope="app",
    )
    current = _bottom_nav_vision_candidate(
        capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.66, 0.84, 0.78, 0.90),
        semantic_name="底栏「社区」",
        purpose="打开可见底栏导航「社区」",
        expected_state_hint="post_list",
        frontier_scope="page",
    )
    executor, driver, request = _community_bottom_nav_runtime(
        source=source,
        current_candidates=(current,),
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent is not None
    assert driver.last_intent.parameters["resolved_target"] == {
        "x": 1037,
        "y": 2645,
        "space": "capture_px",
        "base_width": 1440,
        "base_height": 3040,
    }


async def test_orientation_drift_rejects_stale_visual_candidate() -> None:
    """截图方向变化后禁止复用旧视觉坐标。"""

    source = AppVisualCandidate.build(
        source_capture_id=OBSERVATION_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="详情入口",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.2, 0.3, 0.8, 0.45),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开详情",
        expected_state_hint="content_detail",
        frontier_scope="page",
    )
    # before Capture 已是横屏尺寸／方向，与 observation 候选不一致。
    before = AppVisualCandidate.build(
        source_capture_id=BEFORE_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1920,
        source_height=1080,
        source_orientation="landscape",
        semantic_name="详情入口",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.2, 0.3, 0.8, 0.45),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开详情",
        expected_state_hint="content_detail",
        frontier_scope="page",
    )
    before_analysis = _analysis(
        before,
        capture_id=BEFORE_CAPTURE_ID,
        screenshot_artifact_id="77777777-7777-7777-7777-777777777777",
        visual_page_type="content_list",
        page_state="stable",
        candidates=(before,),
        width=1920,
        height=1080,
        orientation="landscape",
    )
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(
            source,
            visual_page_type="content_list",
            page_state="stable",
            candidates=(source,),
            width=1080,
            height=1920,
            orientation="portrait",
        ),
        before_analysis=before_analysis,
        before_width=1920,
        before_height=1080,
    )
    # before Capture 身份与 resolver 用的 before 分析一致；候选来自竖屏 observation。
    result = await executor.execute(_request(source))
    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code in {
        "STALE_VISUAL_CANDIDATE",
        APP_CANDIDATE_REVALIDATION_REQUIRED,
        "BEFORE_CAPTURE_IDENTITY_MISMATCH",
    }
    assert driver.execute_count == 0


async def test_vision_bottom_nav_allows_touch_on_popup_home_frame() -> None:
    """黑料：无 a11y 底栏时 Vision 条带候选须在 popup|home 帧可点「精选／片库」等。"""

    from services.api.app.application.app_navigation_entry_provenance import (
        DISCOVERY_SOURCE_VISION,
    )

    candidate = AppVisualCandidate.build(
        source_capture_id=OBSERVATION_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="底栏片库",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.28, 0.82, 0.42, 0.90),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="点击底栏「片库」并截图留证",
        expected_state_hint="video_list",
        frontier_scope="app",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    before_candidate = AppVisualCandidate.build(
        source_capture_id=BEFORE_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="底栏片库",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.28, 0.82, 0.42, 0.90),
        confidence=0.94,
        risk_level=InteractionRiskLevel.LOW,
        purpose="点击底栏「片库」并截图留证",
        expected_state_hint="video_list",
        frontier_scope="app",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    dismiss = AppVisualCandidate.build(
        source_capture_id=BEFORE_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="关闭弹层：跳过",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.80, 0.08, 0.95, 0.14),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose="关闭弹窗",
        expected_state_hint="close_popup_x",
        control_role="dismiss",
        frontier_scope="local",
    )
    surface_facts = {
        "popup_visible": True,
        "topmost_ad_popup": {
            "is_ad": True,
            "confidence": 0.9,
            "normalized_bounds": {
                "left": 0.08,
                "top": 0.16,
                "right": 0.92,
                "bottom": 0.72,
            },
        },
        "trusted_accessibility_controls": {"bottom_navigation_count": 0},
    }
    before_analysis = _analysis(
        before_candidate,
        capture_id=BEFORE_CAPTURE_ID,
        screenshot_artifact_id="77777777-7777-7777-7777-777777777777",
        visual_page_type="home",
        page_state="popup",
        overlay_signature="popup:home-promo",
        surface_facts=surface_facts,
        candidates=(dismiss, before_candidate),
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            visual_page_type="home",
            page_state="popup",
            surface_facts=surface_facts,
        ),
        before_analysis=before_analysis,
    )

    result = await executor.execute(
        _request(candidate, parameters={"bottom_navigation_tab_seek": True})
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    # 安全底部带（非系统手势区）中心约 0.86 → y≈1651。
    assert 1550 <= int(driver.last_intent.parameters["resolved_target"]["y"]) <= 1750


async def test_home_top_category_allows_touch_when_page_state_unknown_on_home() -> None:
    """上方分类：page_type=home 但 page_state=unknown 时仍允许触控（对齐底栏降级帧）。"""

    candidate = AppVisualCandidate.build(
        source_capture_id=OBSERVATION_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1440,
        source_height=3040,
        source_orientation="portrait",
        semantic_name="上方分类「动漫」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.60, 0.109, 0.66, 0.17),
        confidence=0.96,
        risk_level=InteractionRiskLevel.LOW,
        purpose="点击主页上方分类「动漫」并截图留证",
        expected_state_hint="home_top_tab:动漫",
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    before_analysis = _before_visual_analysis(
        candidate,
        width=1440,
        height=3040,
        visual_page_type="home",
        page_state="unknown",
        normalized_bounds=NormalizedBounds(0.60, 0.109, 0.66, 0.17),
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            width=1440,
            height=3040,
            visual_page_type="home",
            page_state="stable",
        ),
        before_analysis=before_analysis,
        before_width=1440,
        before_height=3040,
    )

    result = await executor.execute(
        _request(
            candidate,
            parameters={
                "home_top_category_tab_seek": True,
                "expected_state_hint": "home_top_tab:动漫",
                "normalized_bounds": {
                    "left": 0.60,
                    "top": 0.109,
                    "right": 0.66,
                    "bottom": 0.17,
                },
            },
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    resolved = driver.last_intent.parameters["resolved_target"]
    # 1440x3040：x≈0.63 → ~907；y 上方栏中心 ≈ 0.14 → ~425
    assert 850 <= int(resolved["x"]) <= 970
    assert 300 <= int(resolved["y"]) <= 550


async def test_home_top_category_allows_touch_when_soft_popup_on_home() -> None:
    """动作前帧 home|popup 但上方分类像素槽已在：须点「最新」，禁止 REVALIDATION 废轮。"""

    latest_bounds = NormalizedBounds(0.0428, 0.109, 0.0988, 0.17)
    candidate = AppVisualCandidate.build(
        source_capture_id=OBSERVATION_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1440,
        source_height=3040,
        source_orientation="portrait",
        semantic_name="上方分类「最新」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=latest_bounds,
        confidence=0.96,
        risk_level=InteractionRiskLevel.LOW,
        purpose="点击主页上方分类「最新」并截图留证",
        expected_state_hint="home_top_tab:最新",
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    calibration = {
        "tabs": [
            {
                "label": "最新",
                "bounds": {
                    "top": 0.109,
                    "left": 0.0428,
                    "right": 0.0988,
                    "bottom": 0.17,
                },
                "center_x": 0.0708,
                "selected": False,
                "slot_index": 0,
            }
        ],
        "tab_count": 1,
        "selected_slot_index": 1,
    }
    facts = {
        "pixel_home_top_category_calibration": calibration,
        "pixel_home_top_selected_labels": ["推荐"],
        "home_top_selected_label": "推荐",
        "popup_visible": True,
        "pixel_selected_labels": ["主页"],
    }
    before_analysis = _before_visual_analysis(
        candidate,
        width=1440,
        height=3040,
        visual_page_type="home",
        page_state="popup",
        normalized_bounds=latest_bounds,
        surface_facts=facts,
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            width=1440,
            height=3040,
            visual_page_type="home",
            page_state="stable",
            surface_facts=facts,
        ),
        before_analysis=before_analysis,
        before_width=1440,
        before_height=3040,
    )

    result = await executor.execute(
        _request(
            candidate,
            parameters={
                "home_top_category_tab_seek": True,
                "expected_state_hint": "home_top_tab:最新",
                "normalized_bounds": {
                    "left": 0.0428,
                    "top": 0.109,
                    "right": 0.0988,
                    "bottom": 0.17,
                },
            },
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert result.error_code != APP_CANDIDATE_REVALIDATION_REQUIRED
    assert driver.execute_count == 1
    resolved = driver.last_intent.parameters["resolved_target"]
    assert 50 <= int(resolved["x"]) <= 180
    assert 300 <= int(resolved["y"]) <= 550


async def test_home_top_sample_allows_first_card_when_soft_popup_on_home() -> None:
    """最新已选中、动作前帧 home|popup：须点列表第一片，禁止 REVALIDATION 废轮。"""

    card_bounds = NormalizedBounds(0.03, 0.28, 0.48, 0.52)
    candidate = AppVisualCandidate.build(
        source_capture_id=OBSERVATION_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1440,
        source_height=3040,
        source_orientation="portrait",
        semantic_name="列表第一片",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=card_bounds,
        confidence=0.93,
        risk_level=InteractionRiskLevel.LOW,
        purpose="点击上方分类「最新」列表第一片并截图取证",
        expected_state_hint="home_top_sample_first_card",
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    facts = {
        "pixel_home_top_selected_labels": ["最新"],
        "home_top_selected_label": "最新",
        "popup_visible": True,
        "pixel_selected_labels": ["主页"],
        "pixel_home_top_category_calibration": {
            "tabs": [
                {
                    "label": "最新",
                    "bounds": {
                        "top": 0.109,
                        "left": 0.04,
                        "right": 0.10,
                        "bottom": 0.17,
                    },
                    "selected": True,
                    "slot_index": 0,
                }
            ],
            "selected_slot_index": 0,
        },
    }
    before_analysis = _before_visual_analysis(
        candidate,
        width=1440,
        height=3040,
        visual_page_type="home",
        page_state="popup",
        normalized_bounds=card_bounds,
        surface_facts=facts,
    )
    sample_id = f"forced-pixel-home-top-sample-first-card:{BEFORE_CAPTURE_ID}:最新"
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            width=1440,
            height=3040,
            visual_page_type="home",
            page_state="stable",
            surface_facts=facts,
        ),
        before_analysis=before_analysis,
        before_width=1440,
        before_height=3040,
    )

    result = await executor.execute(
        _request(
            candidate,
            parameters={
                "candidate_id": sample_id,
                "home_top_category_sample_seek": True,
                "forced_pixel_home_top_sample": True,
                "expected_state_hint": "home_top_sample_first_card",
                "normalized_bounds": {
                    "left": 0.03,
                    "top": 0.28,
                    "right": 0.48,
                    "bottom": 0.52,
                },
            },
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert result.error_code != APP_CANDIDATE_REVALIDATION_REQUIRED
    assert driver.execute_count == 1
    resolved = driver.last_intent.parameters["resolved_target"]
    assert 180 <= int(resolved["x"]) <= 500
    assert 850 <= int(resolved["y"]) <= 1400


async def test_home_list_touch_not_blocked_by_festival_sticker_popup() -> None:
    """右下国庆浮标被标 home|popup：列表卡仍须能点，禁止当挡路弹窗废轮。"""

    card_bounds = NormalizedBounds(0.03, 0.28, 0.48, 0.52)
    candidate = AppVisualCandidate.build(
        source_capture_id=OBSERVATION_CAPTURE_ID,
        source_image_signature=SIGNATURE,
        source_width=1440,
        source_height=3040,
        source_orientation="portrait",
        semantic_name="列表第一片",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=card_bounds,
        confidence=0.93,
        risk_level=InteractionRiskLevel.LOW,
        purpose="点击列表第一片并截图取证",
        expected_state_hint="home_list_card",
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_VISION,
    )
    facts = {
        "pixel_home_top_selected_labels": ["最新"],
        "home_top_selected_label": "最新",
        "popup_visible": True,
        "pixel_selected_labels": ["主页"],
    }
    before_analysis = _before_visual_analysis(
        candidate,
        width=1440,
        height=3040,
        visual_page_type="home",
        page_state="popup",
        normalized_bounds=card_bounds,
        surface_facts=facts,
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            width=1440,
            height=3040,
            visual_page_type="home",
            page_state="stable",
            surface_facts=facts,
        ),
        before_analysis=before_analysis,
        before_width=1440,
        before_height=3040,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert result.error_code != APP_CANDIDATE_REVALIDATION_REQUIRED
    assert driver.execute_count == 1


async def test_app_navigation_rejects_catalog_rename_outside_current_bottom_band() -> None:
    """当前改名候选不在底栏条带时，禁止按源底栏槽位合成触控。"""

    candidate = _candidate(
        semantic_name="我的底栏",
        expected_state_hint="profile",
        purpose="切换到我的页面",
        frontier_scope="app",
    )
    before_analysis = _before_visual_analysis(
        candidate,
        semantic_name="个人中心 Tab",
        expected_state_hint="我的",
        purpose="进入个人中心",
        frontier_scope="app",
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_app_navigation_rejects_after_source_page_type_drifts() -> None:
    """源页型漂移时必须重新观察，不能跨视觉页面复用候选。"""

    candidate = _candidate(
        semantic_name="我的底栏",
        expected_state_hint="profile",
        purpose="切换到我的页面",
        frontier_scope="app",
    )
    before_analysis = _before_visual_analysis(
        candidate,
        semantic_name="个人中心 Tab",
        expected_state_hint="我的",
        purpose="进入个人中心",
        frontier_scope="app",
        visual_page_type="content_list",
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(candidate, visual_page_type="home"),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_page_navigation_cannot_rebind_after_source_page_type_drifts() -> None:
    """页面级入口不能借 App 级页型漂移规则跨页面触发。"""

    candidate = _candidate(
        semantic_name="查看全部",
        expected_state_hint="content_list",
        purpose="进入内容列表",
        frontier_scope="page",
    )
    before_analysis = _before_visual_analysis(
        candidate,
        expected_state_hint="content_list",
        frontier_scope="page",
        visual_page_type="profile",
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(candidate, visual_page_type="home"),
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_local_control_cannot_rebind_only_by_generic_state_hint() -> None:
    """局部点击仍要求同名，避免把同页多个普通控件按提示误合并。"""

    candidate = _candidate(
        semantic_name="第一张内容卡",
        expected_state_hint="content_detail",
        purpose="进入第一张内容详情",
        frontier_scope="local",
    )
    before_analysis = _before_visual_analysis(
        candidate,
        semantic_name="第二张内容卡",
        expected_state_hint="content_detail",
        purpose="进入第二张内容详情",
        frontier_scope="local",
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert driver.execute_count == 0


@pytest.mark.parametrize("page_state", ("startup_ad", "countdown"))
async def test_dismiss_role_can_clear_fullscreen_ad_without_overlay(
    page_state: str,
) -> None:
    candidate = _candidate(
        semantic_name="关闭",
        expected_state_hint="close_popup_x",
        purpose="关闭开屏遮挡",
        control_role="dismiss",
    )
    source_analysis = _analysis(
        candidate,
        visual_page_type="startup_ad",
        page_state=page_state,
    )
    before_analysis = _before_visual_analysis(
        candidate,
        visual_page_type="startup_ad",
        page_state=page_state,
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=source_analysis,
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1


async def test_real_post_type_picker_candidate_can_enter_editor_without_synthetic_contract() -> (
    None
):
    candidate = _candidate(
        semantic_name="图文发帖",
        expected_state_hint="post_publish",
        purpose="进入图文发帖编辑器",
        risk_level=InteractionRiskLevel.MEDIUM,
    )
    source_analysis = _analysis(
        candidate,
        visual_page_type="post_list",
        page_state="stable",
    )
    before_analysis = _before_visual_analysis(
        candidate,
        visual_page_type="post_list",
        page_state="stable",
        overlay_signature="post-type-picker",
        surface_facts={"popup_visible": True},
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=source_analysis,
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert "app_coverage_page_seek" not in driver.last_intent.parameters


async def test_real_post_type_picker_candidate_rejects_final_publish_semantics() -> None:
    candidate = _candidate(
        semantic_name="提交发布",
        expected_state_hint="post_publish",
        purpose="提交发布",
        risk_level=InteractionRiskLevel.MEDIUM,
    )
    source_analysis = _analysis(
        candidate,
        visual_page_type="post_list",
        page_state="stable",
    )
    before_analysis = _before_visual_analysis(
        candidate,
        visual_page_type="post_list",
        page_state="stable",
        overlay_signature="post-type-picker",
        surface_facts={"popup_visible": True},
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=source_analysis,
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_real_post_type_picker_candidate_rejects_ad_popup_fact() -> None:
    candidate = _candidate(
        semantic_name="图文发帖",
        expected_state_hint="post_publish",
        purpose="进入图文发帖编辑器",
        risk_level=InteractionRiskLevel.MEDIUM,
    )
    source_analysis = _analysis(
        candidate,
        visual_page_type="post_list",
        page_state="stable",
    )
    before_analysis = _before_visual_analysis(
        candidate,
        visual_page_type="post_list",
        page_state="stable",
        overlay_signature="post-type-picker",
        surface_facts={
            "popup_visible": True,
            "topmost_ad_popup": {"is_ad": True},
        },
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=source_analysis,
        before_analysis=before_analysis,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_legacy_coverage_seek_contract_cannot_bypass_real_candidate() -> None:
    candidate = _candidate(
        semantic_name="覆盖寻页：进入视频列表",
        expected_state_hint="video_list",
        purpose="覆盖寻页进入视频列表",
    )
    executor, driver, _ = _executor(candidate=candidate)
    request = _request(
        candidate,
        parameters={
            "app_coverage_page_seek": {
                "protocol_version": "app-coverage-page-seek.v1",
                "normalized_bounds": {
                    "left": 0.2,
                    "top": 0.7,
                    "right": 0.8,
                    "bottom": 0.9,
                },
                "expected_next_page_type": "video_list",
                "purpose": "覆盖寻页进入视频列表",
                "frontier_scope": "page",
                "confidence": 0.99,
            }
        },
    )

    with pytest.raises(AppInteractionRuntimeError) as error:
        await executor.execute(request)

    assert error.value.error_code == "VISUAL_CANDIDATE_MISMATCH"
    assert error.value.retryable is False
    assert driver.execute_count == 0


async def test_missing_current_before_visual_fails_closed_on_repeat() -> None:
    candidate = _candidate()
    executor, driver, _ = _executor(
        candidate=candidate,
        before_analysis=None,
    )
    request = _request(candidate)

    first = await executor.execute(request)
    second = await executor.execute(request)

    assert first.status is InteractionResultStatus.FAILED
    assert second.status is InteractionResultStatus.FAILED
    assert first.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert second.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert first.retryable is True
    assert second.retryable is True
    assert driver.execute_count == 0


async def test_prepare_before_visual_runs_before_current_visual_read() -> None:
    candidate = _candidate()
    calls: list[tuple[str, str]] = []

    async def prepare_before_visual(*, capture, source):
        calls.append((capture.capture_id, source.capture_id))
        return object()

    executor, driver, _ = _executor(
        candidate=candidate,
        prepare_before_visual=prepare_before_visual,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert calls == [(BEFORE_CAPTURE_ID, OBSERVATION_CAPTURE_ID)]
    assert driver.execute_count == 1


async def test_prepare_before_visual_failure_is_non_retryable_and_never_drives() -> None:
    candidate = _candidate()
    cause = RuntimeError("vision provider unavailable")

    async def prepare_before_visual(*, capture, source):
        del capture, source
        raise cause

    executor, driver, _ = _executor(
        candidate=candidate,
        prepare_before_visual=prepare_before_visual,
    )

    with pytest.raises(AppInteractionRuntimeError) as error:
        await executor.execute(_request(candidate))

    assert error.value.error_code == "APP_ACTION_BEFORE_VISUAL_UNDERSTANDING_FAILED"
    assert error.value.retryable is False
    assert error.value.__cause__ is cause
    assert driver.execute_count == 0


async def test_ad_accessibility_skip_label_cannot_alias_visual_candidate() -> None:
    """即使附带广告合同，跳过控件也不能改名成广告落地入口。"""

    candidate = _candidate(
        semantic_name="跳过广告：进入",
        expected_state_hint="tap_skip",
    )
    executor, driver, _ = _executor(candidate=candidate)
    request = _request(candidate)
    request = replace(
        request,
        intent=replace(
            request.intent,
            semantic_target="进入",
            parameters={
                "candidate_id": candidate.candidate_id,
                "capture_ad_landing_evidence": True,
                "ad_landing_capability": "ad_outbound",
                "navigation_depth": 1,
                "app_ad_landing_contract": "app-ad-landing-consistency.v1",
                "app_ad_accessibility_entry_label": "进入",
            },
        ),
    )

    with pytest.raises(AppInteractionRuntimeError) as error:
        await executor.execute(request)

    assert error.value.error_code == "VISUAL_CANDIDATE_MISMATCH"
    assert driver.execute_count == 0


async def test_signed_curriculum_observation_can_resolve_without_visual_candidate() -> None:
    visual_candidate = _candidate()
    phase_id = "splash_burst"
    action_count = 0
    semantic_target = f"课纲零触控观察：{phase_id}：第{action_count + 1}帧"
    purpose = f"按课纲推进 {phase_id}，只观察当前屏幕，不执行触控"
    observation_candidate = _candidate(
        action_type=InteractionOperation.OBSERVE,
        semantic_name=semantic_target,
    )
    executor, driver, _ = _executor(
        candidate=visual_candidate,
        analysis=_analysis(visual_candidate),
    )
    request = _request(observation_candidate)
    request = replace(
        request,
        intent=replace(
            request.intent,
            purpose=purpose,
            parameters={
                "candidate_id": observation_candidate.candidate_id,
                "app_curriculum_phase_id": phase_id,
                "app_curriculum_observation": {
                    "protocol_version": "app-curriculum-observation.v1",
                    "phase_id": phase_id,
                    "phase_action_count": action_count,
                },
            },
        ),
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.action_type is InteractionOperation.OBSERVE


async def test_signed_curriculum_scroll_can_resolve_without_visual_candidate() -> None:
    visual_candidate = _candidate()
    phase_id = "shenqi"
    action_count = 1
    semantic_target = f"课纲安全下滑：{phase_id}：第{action_count + 1}帧"
    purpose = f"按课纲推进 {phase_id}，只执行安全下滑并重新截图"
    expected_state_hint = phase_id
    scroll_candidate = _candidate(
        action_type=InteractionOperation.SCROLL,
        semantic_name=semantic_target,
        expected_state_hint=expected_state_hint,
    )
    executor, driver, _ = _executor(
        candidate=visual_candidate,
        analysis=_analysis(visual_candidate),
    )
    request = _request(scroll_candidate)
    request = replace(
        request,
        intent=replace(
            request.intent,
            purpose=purpose,
            parameters={
                "candidate_id": scroll_candidate.candidate_id,
                "app_curriculum_phase_id": phase_id,
                "app_curriculum_action": {
                    "protocol_version": APP_CURRICULUM_ACTION_VERSION,
                    "phase_id": phase_id,
                    "phase_action_count": action_count,
                    "action_type": InteractionOperation.SCROLL.value,
                    "expected_state_hint": expected_state_hint,
                },
                "direction": "down",
            },
        ),
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.action_type is InteractionOperation.SCROLL


async def test_signed_curriculum_scroll_rejects_tampered_identity_hint() -> None:
    """课纲滚动身份提示被改写后，Runtime 必须拒绝重建候选。"""

    visual_candidate = _candidate()
    phase_id = "shenqi"
    action_count = 1
    expected_state_hint = phase_id
    semantic_target = f"课纲安全下滑：{phase_id}：第{action_count + 1}帧"
    purpose = f"按课纲推进 {phase_id}，只执行安全下滑并重新截图"
    scroll_candidate = _candidate(
        action_type=InteractionOperation.SCROLL,
        semantic_name=semantic_target,
        expected_state_hint=expected_state_hint,
    )
    executor, driver, _ = _executor(
        candidate=visual_candidate,
        analysis=_analysis(visual_candidate),
    )
    request = _request(scroll_candidate)
    request = replace(
        request,
        intent=replace(
            request.intent,
            purpose=purpose,
            parameters={
                "candidate_id": scroll_candidate.candidate_id,
                "app_curriculum_phase_id": phase_id,
                "app_curriculum_action": {
                    "protocol_version": APP_CURRICULUM_ACTION_VERSION,
                    "phase_id": phase_id,
                    "phase_action_count": action_count,
                    "action_type": InteractionOperation.SCROLL.value,
                    "expected_state_hint": "shenqi tampered",
                },
                "direction": "down",
            },
        ),
    )

    with pytest.raises(AppInteractionRuntimeError) as error:
        await executor.execute(request)

    assert error.value.error_code == "VISUAL_CANDIDATE_NOT_FOUND"
    assert driver.execute_count == 0


async def test_signed_dynamic_page_sampling_can_resolve_without_visual_candidate() -> None:
    visual_candidate = _candidate()
    scope = "nav:profile"
    page_type = "login"
    purpose = (
        f"动态采样当前业务页面下一屏；scope={scope}；page_type={page_type}"
    )
    scroll_candidate = _candidate(
        action_type=InteractionOperation.SCROLL,
        semantic_name="动态页面安全下滑",
        expected_state_hint=page_type,
        purpose=purpose,
    )
    executor, driver, _ = _executor(
        candidate=visual_candidate,
        analysis=_analysis(visual_candidate),
    )
    request = _request(scroll_candidate)
    request = replace(
        request,
        intent=replace(
            request.intent,
            purpose=purpose,
            parameters={
                "candidate_id": scroll_candidate.candidate_id,
                "app_dynamic_page_sampling": {
                    "protocol_version": APP_DYNAMIC_PAGE_SAMPLING_VERSION,
                    "business_page_scope": scope,
                    "page_type": page_type,
                },
                "direction": "down",
            },
        ),
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.action_type is InteractionOperation.SCROLL


async def test_signed_dynamic_startup_observation_can_resolve_without_visual_candidate() -> None:
    visual_candidate = _candidate()
    observation_count = 2
    semantic_target = f"动态启动零触控观察：第{observation_count + 1}帧"
    purpose = "等待线路检测或启动表面自然变化，不执行任何触控"
    observation_candidate = _candidate(
        action_type=InteractionOperation.OBSERVE,
        semantic_name=semantic_target,
        expected_state_hint="line_loading",
        purpose=purpose,
    )
    executor, driver, _ = _executor(
        candidate=visual_candidate,
        analysis=_analysis(
            visual_candidate,
            visual_page_type="line_loading",
            page_state="loading",
        ),
    )
    request = _request(observation_candidate)
    request = replace(
        request,
        intent=replace(
            request.intent,
            purpose=purpose,
            parameters={
                "candidate_id": observation_candidate.candidate_id,
                "app_dynamic_startup_observation": {
                    "protocol_version": APP_DYNAMIC_STARTUP_OBSERVATION_VERSION,
                    "observation_count": observation_count,
                    "observation_limit": APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT,
                },
            },
        ),
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.action_type is InteractionOperation.OBSERVE


@pytest.mark.parametrize(
    "tampered_field",
    ("observation_count", "observation_limit", "semantic", "purpose"),
)
async def test_signed_dynamic_startup_observation_rejects_tampered_contract(
    tampered_field: str,
) -> None:
    visual_candidate = _candidate()
    observation_count = 2
    semantic_target = f"动态启动零触控观察：第{observation_count + 1}帧"
    purpose = "等待线路检测或启动表面自然变化，不执行任何触控"
    observation_candidate = _candidate(
        action_type=InteractionOperation.OBSERVE,
        semantic_name=semantic_target,
        expected_state_hint="line_loading",
        purpose=purpose,
    )
    payload = {
        "protocol_version": APP_DYNAMIC_STARTUP_OBSERVATION_VERSION,
        "observation_count": observation_count,
        "observation_limit": APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT,
    }
    intent_semantic_target = semantic_target
    intent_purpose = purpose
    if tampered_field == "observation_count":
        payload["observation_count"] = APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT
    elif tampered_field == "observation_limit":
        payload["observation_limit"] = APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT + 1
    elif tampered_field == "semantic":
        intent_semantic_target = "模型改写的启动观察"
    else:
        intent_purpose = "模型改写的观察目的"

    executor, driver, _ = _executor(
        candidate=visual_candidate,
        analysis=_analysis(
            visual_candidate,
            visual_page_type="line_loading",
            page_state="loading",
        ),
    )
    request = _request(observation_candidate)
    request = replace(
        request,
        intent=replace(
            request.intent,
            semantic_target=intent_semantic_target,
            purpose=intent_purpose,
            parameters={
                "candidate_id": observation_candidate.candidate_id,
                "app_dynamic_startup_observation": payload,
            },
        ),
    )

    with pytest.raises(AppInteractionRuntimeError) as error:
        await executor.execute(request)

    assert error.value.error_code == "VISUAL_CANDIDATE_NOT_FOUND"
    assert driver.execute_count == 0


@pytest.mark.parametrize(
    ("scope", "page_type", "purpose"),
    [
        (
            "nav:tampered",
            "login",
            "动态采样当前业务页面下一屏；scope=nav:profile；page_type=login",
        ),
        (
            "nav:profile",
            "profile",
            "动态采样当前业务页面下一屏；scope=nav:profile；page_type=profile",
        ),
        ("nav:profile", "login", "模型改写的滚动目的"),
    ],
)
async def test_signed_dynamic_page_sampling_rejects_tampered_identity(
    scope: str,
    page_type: str,
    purpose: str,
) -> None:
    visual_candidate = _candidate()
    scroll_candidate = _candidate(
        action_type=InteractionOperation.SCROLL,
        semantic_name="动态页面安全下滑",
        expected_state_hint="login",
        purpose="动态采样当前业务页面下一屏；scope=nav:profile；page_type=login",
    )
    executor, driver, _ = _executor(
        candidate=visual_candidate,
        analysis=_analysis(visual_candidate),
    )
    request = _request(scroll_candidate)
    request = replace(
        request,
        intent=replace(
            request.intent,
            purpose=purpose,
            parameters={
                "candidate_id": scroll_candidate.candidate_id,
                "app_dynamic_page_sampling": {
                    "protocol_version": APP_DYNAMIC_PAGE_SAMPLING_VERSION,
                    "business_page_scope": scope,
                    "page_type": page_type,
                },
                "direction": "down",
            },
        ),
    )

    with pytest.raises(AppInteractionRuntimeError) as error:
        await executor.execute(request)

    assert error.value.error_code == "VISUAL_CANDIDATE_NOT_FOUND"
    assert driver.execute_count == 0


async def test_before_capture_dimension_drift_stays_fail_closed() -> None:
    """动作前保全截图尺寸漂移时，不能复用原截图候选。"""
    executor, driver, candidate = _executor(before_width=720)

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_unrelated_before_capture_never_uses_current_candidate() -> None:
    """只有同一协调动作的 before Capture 才能承接当前截图候选。"""
    executor, driver, candidate = _executor(
        before_signature="sha256:" + "b" * 64,
        before_parent_capture_id="unrelated-capture",
    )

    with pytest.raises(AppInteractionRuntimeError) as error:
        await executor.execute(_request(candidate))

    assert error.value.error_code == "BEFORE_CAPTURE_IDENTITY_MISMATCH"
    assert driver.execute_count == 0


@pytest.mark.parametrize(
    ("analysis_package", "before_package", "error_code"),
    [
        (
            "com.example.other",
            "com.example.target",
            APP_CANDIDATE_REVALIDATION_REQUIRED,
        ),
        (
            "com.example.target",
            "com.example.other",
            APP_CANDIDATE_REVALIDATION_REQUIRED,
        ),
    ],
)
async def test_candidate_analysis_or_before_package_mismatch_never_calls_driver(
    analysis_package: str,
    before_package: str,
    error_code: str,
) -> None:
    """candidate 身份相同也不能跨包复用截图分析。"""
    candidate = _candidate()
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(candidate, package_name=analysis_package),
        before_package_name=before_package,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == error_code
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_runtime_package_drift_blocks_matching_click_before_driver() -> None:
    """当前设备已离开目标包时，不能因 candidate_id／截图仍匹配而点击。"""
    candidate = _candidate()
    reader = StaticRuntimeIdentityReader(
        package_name="com.android.chrome",
        activity="com.android.chrome.Main",
    )
    driver = RecordingDriver()
    executor, driver, _ = _executor(
        candidate=candidate,
        driver=driver,
        runtime_identity_reader=reader,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "APP_PACKAGE_BOUNDARY_VIOLATION"
    assert result.retryable is True
    assert driver.execute_count == 0
    assert len(reader.calls) == 1


async def test_runtime_identity_unavailable_blocks_click_closed() -> None:
    candidate = _candidate()
    reader = StaticRuntimeIdentityReader(
        package_name="com.example.target",
        activity=".MainActivity",
        unavailable=True,
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        runtime_identity_reader=reader,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "APP_RUNTIME_IDENTITY_UNAVAILABLE"
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_external_package_allows_only_back_and_target_package_recovers() -> None:
    back_candidate = _candidate(
        action_type=InteractionOperation.BACK,
        semantic_name="系统返回键",
        expected_state_hint="back",
    )
    reader = StaticRuntimeIdentityReader(
        package_name="com.android.chrome",
        activity="com.android.chrome.Main",
    )
    executor, driver, _ = _executor(
        candidate=back_candidate,
        runtime_identity_reader=reader,
    )

    result = await executor.execute(_request(back_candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1

    dialog_candidate = _candidate(
        semantic_name="系统弹窗确认",
        expected_state_hint="system_dialog",
    )
    dialog_request = _request(dialog_candidate)
    dialog_request = replace(
        dialog_request,
        intent=replace(
            dialog_request.intent,
            parameters={
                "candidate_id": dialog_candidate.candidate_id,
                "surface_phase_key": "system_dialog",
                "surface_phase_run_id": "phase-run-1",
                "surface_phase_decision_id": "phase-decision-1",
            },
        ),
    )
    reader.identity = AppRuntimeIdentity(
        package_name="android",
        activity="android/.PermissionController",
    )
    executor, driver, _ = _executor(
        candidate=dialog_candidate,
        analysis=_analysis(
            dialog_candidate,
            package_name="android",
            activity="android/.PermissionController",
        ),
        before_package_name="android",
        before_activity="android/.PermissionController",
        runtime_identity_reader=reader,
    )
    result = await executor.execute(dialog_request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1

    click_candidate = _candidate()
    reader.identity = AppRuntimeIdentity(
        package_name="com.example.target",
        activity=".DetailsActivity",
    )
    executor, driver, _ = _executor(
        candidate=click_candidate,
        analysis=_analysis(click_candidate, activity=".DetailsActivity"),
        before_activity=".DetailsActivity",
        runtime_identity_reader=reader,
    )
    result = await executor.execute(_request(click_candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1


async def test_launcher_package_allows_signed_warm_restore() -> None:
    candidate = _candidate(
        action_type=InteractionOperation.WARM_RESTORE,
        semantic_name="重新打开目标应用",
        expected_state_hint="app_ready",
        purpose="系统桌面重新拉起目标 App",
    )
    reader = StaticRuntimeIdentityReader(
        package_name="com.android.launcher3",
        activity="com.android.launcher3.Launcher",
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        analysis=_analysis(
            candidate,
            package_name="com.android.launcher3",
            activity="com.android.launcher3.Launcher",
            visual_page_type="home",
            page_state="stable",
        ),
        before_package_name="com.android.launcher3",
        before_activity="com.android.launcher3.Launcher",
        runtime_identity_reader=reader,
    )
    request = _request(candidate)
    request = replace(
        request,
        intent=replace(
            request.intent,
            parameters={
                "candidate_id": candidate.candidate_id,
                "package_name": "com.example.target",
            },
        ),
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.action_type is InteractionOperation.WARM_RESTORE
    assert driver.last_intent.parameters["package_name"] == "com.example.target"


async def test_external_package_rejects_non_back_actions_without_driver() -> None:
    for action in (
        InteractionOperation.CLICK,
        InteractionOperation.SCROLL,
        InteractionOperation.OBSERVE,
    ):
        if action is InteractionOperation.CLICK:
            candidate = _candidate()
        else:
            candidate = _candidate(
                action_type=action,
                semantic_name=f"外部包动作-{action.value}",
            )
        reader = StaticRuntimeIdentityReader(
            package_name="com.android.chrome",
            activity="com.android.chrome.Main",
        )
        executor, driver, _ = _executor(
            candidate=candidate,
            runtime_identity_reader=reader,
        )

        result = await executor.execute(_request(candidate))

        assert result.status is InteractionResultStatus.FAILED
        assert result.error_code == "APP_PACKAGE_BOUNDARY_VIOLATION"
        assert result.retryable is True
        assert driver.execute_count == 0


async def test_stale_runtime_identity_remains_technical_failure() -> None:
    """同包但 Activity 漂移仍需抛出技术错误，不能伪装成动作拒绝。"""
    candidate = _candidate()
    reader = StaticRuntimeIdentityReader(
        package_name="com.example.target",
        activity=".DetailsActivity",
    )
    executor, driver, _ = _executor(
        candidate=candidate,
        runtime_identity_reader=reader,
    )

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "STALE_APP_RUNTIME_IDENTITY"
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_click_resolves_normalized_center_inside_runtime() -> None:
    executor, driver, candidate = _executor()

    result = await executor.execute(_request(candidate))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert dict(driver.last_intent.parameters)["resolved_target"] == {
        "x": 540,
        "y": 1536,
        "space": "capture_px",
        "base_width": 1080,
        "base_height": 1920,
    }
    assert dict(driver.last_intent.parameters)["candidate_id"] == candidate.candidate_id
    assert result.source_capture_id == BEFORE_CAPTURE_ID
    assert "resolved_target" not in result.to_dict()["state_summary"]


async def test_supplemental_intent_never_reaches_app_driver_when_policy_is_disabled() -> None:
    executor, driver, candidate = _executor()
    request = _request(candidate)
    supplemental_request = replace(
        request,
        intent=replace(
            request.intent,
            parameters={
                "candidate_id": candidate.candidate_id,
                "evidence_request_id": "request-1",
            },
        ),
    )

    with pytest.raises(SupplementalEvidenceDisabledError) as error:
        await executor.execute(supplemental_request)

    assert error.value.operation.value == "execute_probe"
    assert driver.execute_count == 0


async def test_timed_capture_action_is_rejected_in_favor_of_observation_intent() -> None:
    executor, driver, _candidate = _executor()
    intent = ActionIntent(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        intent_id="intent-wait-1",
        idempotency_key="task-1:phase:wait-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=OBSERVATION_CAPTURE_ID,
        action_type=InteractionOperation.TIMED_CAPTURE,
        semantic_target=None,
        parameters={"delay_ms": 0},
        purpose="等待阶段状态变化",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            SurfaceKind.ANDROID,
            InteractionOperation.TIMED_CAPTURE,
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    with pytest.raises(AppInteractionRuntimeError) as error:
        await executor.execute(
            ExecuteInteractionRequest(
                agent_run_id=RUN_ID,
                intent=intent,
                source_capture_id=BEFORE_CAPTURE_ID,
                action_execution_id="action-wait-1",
            )
        )

    assert error.value.error_code == "TIMED_OBSERVATION_INTENT_REQUIRED"
    assert driver.execute_count == 0


async def test_app_capture_writer_passes_command_fence_to_persistence() -> None:
    driver = CaptureDriver()
    persistence = RecordingActionCapturePersistence()
    writer = AppCaptureWriter(
        driver=driver,
        surface_sessions=StaticSessionRepository(),
        persistence=persistence,
    )
    deadline = datetime.now(UTC) + timedelta(minutes=1)

    capture = await writer.capture(
        CaptureWriteRequest(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            session_id=SESSION_ID,
            surface_kind=SurfaceKind.ANDROID,
            intent_id="intent-1",
            phase=CapturePhase.BEFORE_ACTION,
            directive=CaptureDirective(
                role="before_action",
                artifact_types=("screenshot", "action_trace"),
            ),
            parent_capture_id=OBSERVATION_CAPTURE_ID,
            action_execution_id="action-1",
            session_command_sequence=3,
            finalization_epoch=2,
            deadline=deadline,
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )
    )

    assert capture.capture_kind is CaptureKind.APP
    assert driver.capture_calls[0][0].primary_surface_ref == "fake-app:app-session-1"
    assert set(driver.capture_calls[0][1].before.artifact_types) == {
        "screenshot",
        "action_trace",
    }
    call = persistence.calls[0]
    assert call["parent_capture_id"] == OBSERVATION_CAPTURE_ID
    assert call["action_execution_id"] == "action-1"
    assert call["session_command_sequence"] == 3
    assert call["finalization_epoch"] == 2
    assert call["lease_owner"] == "worker-1:app-command"
    assert call["lease_token"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


async def test_before_capture_requires_only_screenshot_and_keeps_trace_separate() -> None:
    driver = CaptureDriver()
    persistence = RecordingActionCapturePersistence()
    writer = AppCaptureWriter(
        driver=driver,
        surface_sessions=StaticSessionRepository(),
        persistence=persistence,
    )
    profile = CaptureProfilePolicy.for_surface(
        SurfaceKind.ANDROID,
        InteractionOperation.CLICK,
    )

    await writer.capture(
        CaptureWriteRequest(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            session_id=SESSION_ID,
            surface_kind=SurfaceKind.ANDROID,
            intent_id="intent-before-screenshot-only",
            phase=CapturePhase.BEFORE_ACTION,
            directive=profile.before,
            parent_capture_id=OBSERVATION_CAPTURE_ID,
            action_execution_id="action-before-screenshot-only",
            session_command_sequence=3,
            finalization_epoch=2,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )
    )

    call = persistence.calls[0]
    assert call["required_artifact_types"] == ("screenshot",)
    assert tuple(item.artifact_type for item in call["artifacts"].artifacts) == ("screenshot",)
    contract = call["artifacts"].metadata["capture_artifact_contract"]
    assert contract["required_artifact_types"] == ("screenshot",)
    assert "action_trace" not in call["artifacts"].metadata


async def test_default_app_profile_writer_forwards_all_main_artifacts_and_status() -> None:
    driver = MainProfileCaptureDriver()
    persistence = RecordingActionCapturePersistence()
    writer = AppCaptureWriter(
        driver=driver,
        surface_sessions=StaticSessionRepository(),
        persistence=persistence,
    )
    profile = CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP)

    await writer.capture(
        CaptureWriteRequest(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            session_id=SESSION_ID,
            surface_kind=SurfaceKind.ANDROID,
            intent_id="intent-default-profile-1",
            phase=CapturePhase.AFTER_ACTION,
            directive=profile.after,
            parent_capture_id=OBSERVATION_CAPTURE_ID,
            action_execution_id="action-default-profile-1",
            session_command_sequence=3,
            finalization_epoch=2,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )
    )

    call = persistence.calls[0]
    # 默认 after 不含 temporal_trace（仅 TIMED_CAPTURE／显式请求才加入）。
    assert call["required_artifact_types"] == APP_DEFAULT_AFTER_ARTIFACT_TYPES
    assert {item.artifact_type for item in call["artifacts"].artifacts} == set(
        APP_DEFAULT_AFTER_ARTIFACT_TYPES
    )
    contract = call["artifacts"].metadata["capture_artifact_contract"]
    assert contract["required_artifact_types"] == ("screenshot", "action_trace")
    assert contract["missing_soft_optional_artifact_types"] == ()
    assert contract["provider_only_artifact_types"] == ()


async def test_default_app_profile_missing_soft_optional_is_recorded_without_blocking() -> None:
    driver = CaptureDriver()
    persistence = RecordingActionCapturePersistence()
    writer = AppCaptureWriter(
        driver=driver,
        surface_sessions=StaticSessionRepository(),
        persistence=persistence,
    )
    profile = CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP)

    await writer.capture(
        CaptureWriteRequest(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            session_id=SESSION_ID,
            surface_kind=SurfaceKind.ANDROID,
            intent_id="intent-default-profile-soft-missing",
            phase=CapturePhase.AFTER_ACTION,
            directive=profile.after,
            parent_capture_id=OBSERVATION_CAPTURE_ID,
            action_execution_id="action-default-profile-soft-missing",
            session_command_sequence=3,
            finalization_epoch=2,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )
    )

    call = persistence.calls[0]
    assert call["required_artifact_types"] == ("screenshot", "action_trace")
    contract = call["artifacts"].metadata["capture_artifact_contract"]
    assert set(contract["missing_soft_optional_artifact_types"]) == {
        "ui_tree",
        "view_hierarchy",
        "ui_snapshot",
        "visible_text",
        "media_state",
        "device_log",
        "network_trace",
        "har",
        "fault_artifact",
    }


async def test_app_capture_writer_persists_observation_without_action_execution() -> None:
    driver = CaptureDriver()
    persistence = RecordingActionCapturePersistence()
    writer = AppCaptureWriter(
        driver=driver,
        surface_sessions=StaticSessionRepository(),
        persistence=persistence,
    )

    capture = await writer.capture(
        CaptureWriteRequest(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            session_id=SESSION_ID,
            surface_kind=SurfaceKind.ANDROID,
            intent_id="observation-1",
            phase=CapturePhase.OBSERVATION,
            directive=CaptureDirective(
                role="observation",
                artifact_types=("screenshot",),
                inherit_parent_semantics=True,
            ),
            parent_capture_id=OBSERVATION_CAPTURE_ID,
            action_execution_id=None,
            session_command_sequence=4,
            finalization_epoch=2,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            lease_owner="worker-1:app-command",
            lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            observation_context={
                "observation_id": "observation-1",
                "surface_phase_run_id": "phase-1",
                "surface_phase_decision_id": "decision-1",
                "semantic_role": "surface_reobserve",
            },
        )
    )

    assert capture.role == "observation"
    assert capture.parent_capture_id == OBSERVATION_CAPTURE_ID
    assert capture.action_execution_id is None
    call = persistence.calls[0]
    assert call["observation_id"] == "observation-1"
    assert call["surface_phase_run_id"] == "phase-1"
    assert call["surface_phase_decision_id"] == "decision-1"
    assert tuple(item.artifact_type for item in call["artifacts"].artifacts) == ("screenshot",)
    assert call["artifacts"].metadata["capture_artifact_contract"]["produced_artifact_types"] == (
        "screenshot",
        "action_trace",
    )


async def test_forced_system_back_resolves_without_visual_candidate() -> None:
    """付费／详情合成 forced-system-back 不得因不在源观察里而空转失败。"""

    visual_candidate = _candidate()
    back_intent_candidate = _candidate(
        action_type=InteractionOperation.BACK,
        semantic_name="系统 BACK",
        purpose="当前落在无根底栏的详情／付费遮罩；系统 BACK 返回后再巡检底栏。",
        expected_state_hint="back",
    )
    executor, driver, _ = _executor(
        candidate=visual_candidate,
        analysis=_analysis(visual_candidate),
    )
    request = _request(back_intent_candidate)
    request = replace(
        request,
        intent=replace(
            request.intent,
            action_type=InteractionOperation.BACK,
            semantic_target="系统 BACK",
            purpose="当前落在无根底栏的详情／付费遮罩；系统 BACK 返回后再巡检底栏。",
            parameters={
                "candidate_id": f"forced-system-back:{OBSERVATION_CAPTURE_ID}",
                "forced_detail_exit_back": True,
            },
        ),
    )

    result = await executor.execute(request)

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent.action_type is InteractionOperation.BACK


def test_home_top_seek_not_blocked_by_stale_unsatisfied_popup() -> None:
    """主页栏已露出：滞后 popup_satisfied=false 不得空拒「最新」。"""

    from types import SimpleNamespace

    from services.api.app.application.app_exploration_flow_contract import (
        update_app_flow_contract,
    )
    from services.api.app.application.app_interaction_runtime import (
        _home_top_tab_seek_blocked_by_unsatisfied_popup,
    )

    flow = update_app_flow_contract(
        None,
        page_state="popup",
        overlay_signature="float:national-day",
        surface_facts={"popup_visible": True},
        playbook_tabs=("主页", "短剧"),
    )
    candidate = _candidate(
        semantic_name="上方分类「最新」",
        expected_state_hint="home_top_tab:最新",
        purpose="点击主页上方分类「最新」并截图留证",
    )
    stale_flow_request = _request(
        candidate,
        parameters={
            "home_top_category_tab_seek": True,
            "coverage_metadata": {"app_flow_contract": flow},
        },
    )
    assert _home_top_tab_seek_blocked_by_unsatisfied_popup(stale_flow_request) is None

    chrome = SimpleNamespace(
        page_state="popup",
        visual_page_type="home",
        surface_facts={
            "popup_visible": True,
            "home_top_selected_label": "推荐",
            "pixel_selected_labels": ["主页"],
        },
        overlay_signature="float:national-day",
        candidates=(),
    )
    hard = SimpleNamespace(
        page_state="stable",
        visual_page_type="home",
        surface_facts={"popup_visible": True, "dialog_kind": "ad_popup"},
        overlay_signature="ad:grid",
        candidates=(),
    )

    def _analysis_request(analysis: object) -> SimpleNamespace:
        return SimpleNamespace(
            intent=SimpleNamespace(
                parameters={
                    "home_top_category_tab_seek": True,
                    "coverage_metadata": {"app_flow_contract": flow},
                    "visual_analysis": analysis,
                },
                task_id=TASK_ID,
                session_id=SESSION_ID,
                intent_id="intent-1",
            ),
            action_execution_id="action-1",
            source_capture_id=BEFORE_CAPTURE_ID,
        )

    assert _home_top_tab_seek_blocked_by_unsatisfied_popup(_analysis_request(chrome)) is None
    blocked = _home_top_tab_seek_blocked_by_unsatisfied_popup(_analysis_request(hard))
    assert blocked is not None
    assert blocked.error_code == "APP_POPUP_PHASE_BLOCKS_HOME_TOP_TABS"
