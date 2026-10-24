"""正式 App Driver 受控异常与未知异常的结果合同。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.app_interaction_runtime import AppInteractionExecutor
from services.api.app.application.capture_coordination import ExecuteInteractionRequest
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSpec,
    SurfaceSessionStatus,
)
from services.api.app.infrastructure.app_runtime.official import OfficialAppRuntime
from services.api.app.infrastructure.app_runtime.official_api import (
    OfficialActionResponse,
    OfficialAppApiError,
    OfficialSessionResponse,
)

TASK_ID = "task-1"
SESSION_ID = "session-1"
SOURCE_CAPTURE_ID = "capture-before-1"
PACKAGE_NAME = "com.example.target"


def _intent() -> ActionIntent:
    return ActionIntent(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        intent_id="intent-1",
        idempotency_key="idem-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=SOURCE_CAPTURE_ID,
        action_type=InteractionOperation.TAP,
        semantic_target="继续",
        purpose="继续探索",
        capture_profile=CaptureProfilePolicy.for_surface(
            SurfaceKind.ANDROID,
            InteractionOperation.TAP,
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        parameters={"point": {"x": 10, "y": 20, "space": "device_px"}},
        risk_level=InteractionRiskLevel.LOW,
    )


def _request() -> ExecuteInteractionRequest:
    return ExecuteInteractionRequest(
        agent_run_id="run-1",
        intent=_intent(),
        source_capture_id=SOURCE_CAPTURE_ID,
        action_execution_id="action-1",
        finalization_epoch=0,
        command_sequence=1,
        lease_owner="worker-1",
        lease_token="11111111-1111-1111-1111-111111111111",
    )


class _CandidateResolver:
    async def resolve(self, _request: ExecuteInteractionRequest) -> object:
        return SimpleNamespace(
            candidate=SimpleNamespace(
                candidate_id="candidate-continue-1",
                semantic_name="继续",
                purpose="继续探索",
                expected_state_hint=None,
                control_role=None,
            ),
            before_capture=SimpleNamespace(
                capture_id=SOURCE_CAPTURE_ID,
                package_name=PACKAGE_NAME,
                activity=".MainActivity",
            ),
            resolved_target=None,
            is_dynamic_page_sampling=False,
        )


class _SessionRepository:
    def __init__(self, session_ref: SurfaceSessionRef) -> None:
        self._session_ref = session_ref

    async def get(self, *, session_id: str) -> object:
        return SimpleNamespace(
            task_id=TASK_ID,
            session_id=session_id,
            surface_kind=SurfaceKind.ANDROID,
            status=SurfaceSessionStatus.ACTIVE,
            current_surface_ref=self._session_ref.primary_surface_ref,
        )


class _StartSessionApi:
    async def start_session(self, _spec: SurfaceSessionSpec) -> OfficialSessionResponse:
        return OfficialSessionResponse(
            request_id="request-start",
            session_handle="private-session-handle",
        )


class _ContextUnsupportedApi(_StartSessionApi):
    async def execute(
        self,
        _session: SurfaceSessionRef,
        _intent: ActionIntent,
        _action_execution_id: str,
    ) -> OfficialActionResponse:
        # 故意保留旧签名；Official Runtime 应将 TypeError 转成受控错误。
        raise AssertionError("不应在缺少 execution_context 时进入 API 实现")


class _ActionIdentityMismatchApi(_StartSessionApi):
    async def execute(
        self,
        _session: SurfaceSessionRef,
        _intent: ActionIntent,
        _action_execution_id: str,
        *,
        execution_context: object,
    ) -> OfficialActionResponse:
        del execution_context
        return OfficialActionResponse(
            request_id="request-action",
            action_execution_id="different-action",
            succeeded=True,
            retryable=False,
        )


class _RetryableApi(_StartSessionApi):
    async def execute(
        self,
        _session: SurfaceSessionRef,
        _intent: ActionIntent,
        _action_execution_id: str,
        *,
        execution_context: object,
    ) -> OfficialActionResponse:
        del execution_context
        raise OfficialAppApiError(
            "内部 canary 不应进入 InteractionResult",
            error_code="ACTION_EXECUTION_FAILED",
            retryable=True,
        )


class _CountingOfficialRuntime(OfficialAppRuntime):
    def __init__(self, *, api_client: object) -> None:
        super().__init__(api_client=api_client, max_screenshot_bytes=1024)
        self.execute_calls = 0

    async def execute_with_context(self, **kwargs: object):  # noqa: ANN003
        self.execute_calls += 1
        return await super().execute_with_context(**kwargs)


async def _official_executor(
    *,
    api_client: object,
    stale_reference: bool = False,
) -> tuple[AppInteractionExecutor, _CountingOfficialRuntime]:
    runtime = _CountingOfficialRuntime(api_client=api_client)
    spec = SurfaceSessionSpec(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        platform_build_ref="build-1",
        device_ref="device-1",
    )
    session_ref = await runtime.start_session(spec)
    # 让实际 Official Runtime 进入 open 状态，避免测试只命中 OPEN_REQUIRED。
    await runtime.reattach_session(spec)
    repository_ref = (
        replace(session_ref, primary_surface_ref="stale-app-surface")
        if stale_reference
        else session_ref
    )
    return (
        AppInteractionExecutor(
            driver=runtime,
            surface_sessions=_SessionRepository(repository_ref),
            candidate_resolver=_CandidateResolver(),
            allowed_packages=(PACKAGE_NAME,),
            command_validator=InteractionCommandValidator(),
        ),
        runtime,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "api_client", "stale_reference"),
    [
        ("STALE_SESSION_LEASE", _StartSessionApi(), True),
        ("EXECUTION_CONTEXT_UNSUPPORTED", _ContextUnsupportedApi(), False),
        ("ACTION_BLOCKED", _ActionIdentityMismatchApi(), False),
    ],
)
async def test_controlled_official_errors_keep_code_and_retryability(
    error_code: str,
    api_client: object,
    stale_reference: bool,
) -> None:
    executor, runtime = await _official_executor(
        api_client=api_client,
        stale_reference=stale_reference,
    )

    result = await executor.execute(_request())

    assert runtime.execute_calls == 1
    assert (result.error_code, result.retryable) == (error_code, False)
    assert result.error_message == "App Driver 执行失败"
    assert "canary" not in repr(result)


@pytest.mark.asyncio
async def test_controlled_official_retryable_error_keeps_retryability() -> None:
    executor, runtime = await _official_executor(api_client=_RetryableApi())

    result = await executor.execute(_request())

    assert runtime.execute_calls == 1
    assert (result.error_code, result.retryable) == ("ACTION_EXECUTION_FAILED", True)
    assert result.error_message == "App Driver 执行失败"
    assert "canary" not in repr(result)


class _ForgedUnknownError(RuntimeError):
    error_code = "CANARY_FORGED_ERROR_CODE"
    retryable = False

    def __str__(self) -> str:
        return "canary-secret-message"


class _UnknownDriver:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_with_context(self, **_kwargs: object):  # noqa: ANN003
        self.calls += 1
        raise _ForgedUnknownError("canary-secret-message")


@pytest.mark.asyncio
async def test_unknown_driver_error_is_sanitized_without_extra_call() -> None:
    driver = _UnknownDriver()
    session_ref = SurfaceSessionRef(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref=f"app-surface:{SESSION_ID}",
    )
    executor = AppInteractionExecutor(
        driver=driver,
        surface_sessions=_SessionRepository(session_ref),
        candidate_resolver=_CandidateResolver(),
        allowed_packages=(PACKAGE_NAME,),
        command_validator=InteractionCommandValidator(),
    )

    result = await executor.execute(_request())

    assert driver.calls == 1
    assert (result.error_code, result.retryable) == ("APP_DRIVER_FAILED", True)
    assert result.error_message == "App Driver 执行失败"
    assert "CANARY_FORGED_ERROR_CODE" not in repr(result)
    assert "canary-secret-message" not in repr(result)


class _CancelingDriver:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_with_context(self, **_kwargs: object):  # noqa: ANN003
        self.calls += 1
        raise asyncio.CancelledError("cancel-canary")


@pytest.mark.asyncio
async def test_driver_cancellation_propagates_without_retry_or_second_call() -> None:
    driver = _CancelingDriver()
    session_ref = SurfaceSessionRef(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref=f"app-surface:{SESSION_ID}",
    )
    executor = AppInteractionExecutor(
        driver=driver,
        surface_sessions=_SessionRepository(session_ref),
        candidate_resolver=_CandidateResolver(),
        allowed_packages=(PACKAGE_NAME,),
        command_validator=InteractionCommandValidator(),
    )

    with pytest.raises(asyncio.CancelledError) as error:
        await executor.execute(_request())

    assert str(error.value) == "cancel-canary"
    assert driver.calls == 1
