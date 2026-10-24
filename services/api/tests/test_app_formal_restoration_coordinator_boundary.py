"""正式 App 恢复必须经过 CaptureCoordinator 的边界测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.app_interaction_runtime import AppInteractionExecutor
from services.api.app.application.app_probe_hangback import (
    finalize_probe_equivalence_hangback,
)
from services.api.app.application.app_restoration_executor import (
    AppRestorationExecutor,
    probe_result_to_snapshot,
)
from services.api.app.application.app_v2_runtime import AppV2Runtime
from services.api.app.application.restoration import (
    RestorationExecutionError,
    RestorationExecutionRequest,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationStrategy,
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import (
    AppOpenRequest,
    SurfaceKind,
    SurfaceSessionRef,
)
from services.api.app.infrastructure.app_runtime.factory import AppRuntimeBundle
from services.api.app.infrastructure.app_runtime.official import OfficialAppRuntime

TASK_ID = "task-formal-restore"
SESSION_ID = "session-formal-restore"


def _request():
    return build_evidence_request(
        work_item_id="inspection-work-formal-restore",
        attempt_id="inspection-attempt-formal-restore",
        task_id=TASK_ID,
        rule_id="rule-formal-restore",
        rule_code="alioth_restore_1",
        target_capture_id="capture-before-formal-restore",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采正式 App 证据",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {"surface_kind": "android"},
            "business": {"package_name": "com.example.formal", "activity": ".Main"},
            "semantic": {"page_state": "stable"},
            "structure": {"screen_id": "screen:formal"},
        },
        minimum_source_grade=EvidenceSourceGrade.A1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _plan(request):
    return build_restoration_plan(
        request=request,
        source_session_id=SESSION_ID,
        target_session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        strategy=RestorationStrategy.SAME_APP_SESSION,
        deadline=datetime.now(UTC) + timedelta(minutes=10),
        replay_execution_plan_id="replay-plan:formal-restore",
    )


class _RecordingDriver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def warm_restore(self, _request: AppOpenRequest):
        self.calls.append("warm_restore")
        return SimpleNamespace(succeeded=True, error_code=None)

    async def reset_app(self, _request: AppOpenRequest):
        self.calls.append("reset_app")
        return SimpleNamespace(succeeded=True, error_code=None)

    async def execute(self, **_kwargs):
        self.calls.append("execute")
        return SimpleNamespace(succeeded=True, error_code=None)


@pytest.mark.asyncio
async def test_formal_restore_rejects_missing_coordinator_before_any_driver_call() -> None:
    request = _request()
    driver = _RecordingDriver()
    session = SurfaceSessionRef(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref=f"app-surface:{SESSION_ID}",
    )
    executor = AppRestorationExecutor(
        driver=driver,
        session=session,
        package_name="com.example.formal",
        activity=".Main",
        deep_link="app://home",
        probe_result_snapshot=probe_result_to_snapshot(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            capture_id="capture-after-formal-restore",
            surface_kind=SurfaceKind.ANDROID,
            package_name="com.example.formal",
            activity=".Main",
        ),
    )

    with pytest.raises(RestorationExecutionError) as error:
        await executor.restore(
            RestorationExecutionRequest(
                request=request,
                plan=_plan(request),
                restoration_attempt_id="attempt-formal-restore",
            )
        )

    assert error.value.error_code == "RESTORATION_COORDINATOR_REQUIRED"
    assert driver.calls == []


@pytest.mark.asyncio
async def test_hangback_missing_runtime_coordinator_returns_before_driver_or_store() -> None:
    driver = _RecordingDriver()

    class _Store:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get(self, **_kwargs):
            self.calls.append("get")
            return None

    request_store = _Store()
    await finalize_probe_equivalence_hangback(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        outcome=SimpleNamespace(
            claimed=True,
            work_type="probe",
            evidence_request_id="evidence-request-formal-restore",
            result_capture_id="capture-probe-formal-restore",
        ),
        runtime=SimpleNamespace(interaction_driver=driver),
        request_store=request_store,
        manifest_store=SimpleNamespace(),
    )

    assert driver.calls == []
    assert request_store.calls == []


def test_runtime_with_formal_snapshot_reader_requires_restoration_coordinator() -> None:
    shared_runtime = object()

    with pytest.raises(ValueError, match="正式恢复缺少 CaptureCoordinator"):
        AppV2Runtime(
            runtime_bundle=AppRuntimeBundle(
                bootstrap_port=shared_runtime,
                driver=shared_runtime,
            ),
            bootstrap_workflow=SimpleNamespace(),
            exploration_workflow=SimpleNamespace(),
            turn_runner=SimpleNamespace(),
            restoration_snapshot_reader=SimpleNamespace(read=lambda **_kwargs: None),
        )


def test_runtime_with_formal_interaction_executor_requires_restoration_coordinator() -> None:
    shared_runtime = object()
    interaction_executor = AppInteractionExecutor(
        driver=shared_runtime,
        surface_sessions=SimpleNamespace(),
        candidate_resolver=SimpleNamespace(),
    )

    with pytest.raises(ValueError, match="正式恢复缺少 CaptureCoordinator"):
        AppV2Runtime(
            runtime_bundle=AppRuntimeBundle(
                bootstrap_port=shared_runtime,
                driver=shared_runtime,
            ),
            bootstrap_workflow=SimpleNamespace(),
            exploration_workflow=SimpleNamespace(),
            turn_runner=SimpleNamespace(),
            interaction_executor=interaction_executor,
        )


def test_legacy_driver_fallback_rejects_unknown_driver_before_restore() -> None:
    driver = _RecordingDriver()

    with pytest.raises(ValueError, match="仅允许实际 FakeAppRuntime"):
        AppRestorationExecutor(
            driver=driver,
            session=None,
            package_name="com.example.formal",
            legacy_driver_fallback=True,
        )

    assert driver.calls == []


def test_legacy_driver_fallback_rejects_official_runtime_before_api_call() -> None:
    class _RecordingOfficialApi:
        def __init__(self) -> None:
            self.calls: list[str] = []

    api = _RecordingOfficialApi()
    driver = OfficialAppRuntime(api_client=api, max_screenshot_bytes=1024)

    with pytest.raises(ValueError, match="仅允许实际 FakeAppRuntime"):
        AppRestorationExecutor(
            driver=driver,
            session=None,
            package_name="com.example.formal",
            legacy_driver_fallback=True,
        )

    assert api.calls == []
