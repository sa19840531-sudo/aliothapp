"""App 恢复与 Official/星语动作上下文的统一执行边界。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.app_interaction_runtime import AppInteractionExecutor
from services.api.app.application.app_restoration_executor import (
    AppRestorationExecutor,
    AppRestorationInteractionExecutor,
)
from services.api.app.application.capture_coordination import ExecuteInteractionRequest
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
    TrustedCommandScope,
)
from services.api.app.application.restoration import RestorationExecutionError
from services.api.app.application.xingyu_runtime.device_bridge import DryRunDeviceBridge
from services.api.app.application.xingyu_runtime.service import (
    XingyuOfficialError,
    XingyuOfficialService,
)
from services.api.app.application.xingyu_runtime.session_store import XingyuSessionStore
from services.api.app.domain.exploration.app_visual import (
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    AppOpenRequest,
    CommandPolicy,
    InteractionOperation,
    InteractionRiskLevel,
    RawActionResult,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSpec,
    SurfaceSessionStatus,
)
from services.api.app.infrastructure.app_runtime.official import OfficialAppRuntime
from services.api.app.infrastructure.app_runtime.official_api import (
    OfficialActionResponse,
    OfficialOpenResponse,
    OfficialSessionResponse,
)

TASK_ID = "task-1"
SESSION_ID = "session-1"
SOURCE_CAPTURE_ID = "capture-before-1"
LEASE_CONTEXT = {
    "task_id": TASK_ID,
    "session_id": SESSION_ID,
    "surface_kind": "android",
    "action_execution_id": "action-1",
    "source_capture_id": SOURCE_CAPTURE_ID,
    "finalization_epoch": 0,
    "command_sequence": 1,
    "lease_owner": "worker-1",
    "lease_token": "11111111-1111-1111-1111-111111111111",
}


def test_execution_request_repr_does_not_expose_command_lease() -> None:
    request = ExecuteInteractionRequest(
        agent_run_id="run-1",
        intent=_intent(),
        source_capture_id=SOURCE_CAPTURE_ID,
        action_execution_id="action-1",
        finalization_epoch=0,
        command_sequence=1,
        lease_owner="private-worker-canary",
        lease_token="private-token-canary",
    )
    assert "private-worker-canary" not in repr(request)
    assert "private-token-canary" not in repr(request)


def _intent(
    *,
    operation: InteractionOperation = InteractionOperation.TAP,
    semantic_target: str | None = "继续",
    risk_level: InteractionRiskLevel = InteractionRiskLevel.LOW,
    source_capture_id: str = SOURCE_CAPTURE_ID,
) -> ActionIntent:
    return ActionIntent(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        intent_id="intent-1",
        idempotency_key="idem-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=source_capture_id,
        action_type=operation,
        semantic_target=semantic_target,
        purpose="继续探索",
        capture_profile=CaptureProfilePolicy.for_surface(SurfaceKind.ANDROID, operation),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        parameters=(
            {"point": {"x": 10, "y": 20, "space": "device_px"}}
            if operation is InteractionOperation.TAP
            else {}
        ),
        risk_level=risk_level,
    )


def _scope(*, operation: InteractionOperation = InteractionOperation.TAP) -> TrustedCommandScope:
    return TrustedCommandScope(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        policy=CommandPolicy(
            allowed_operations={SurfaceKind.ANDROID: (operation,)},
            allowed_packages=("com.example.target",),
        ),
        finalization_epoch=0,
    )


def _resolved_candidate(
    *,
    source_capture_id: str = SOURCE_CAPTURE_ID,
    semantic_name: str = "我已保存",
    purpose: str = "保存凭证",
    control_role: str | None = "dismiss",
    risk_level: InteractionRiskLevel = InteractionRiskLevel.LOW,
    source_width: int = 1000,
    source_height: int = 2000,
) -> AppVisualCandidate:
    return AppVisualCandidate.build(
        source_capture_id=source_capture_id,
        source_image_signature="sha256:" + "a" * 64,
        source_width=source_width,
        source_height=source_height,
        source_orientation="portrait",
        semantic_name=semantic_name,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(
            left=0.1,
            top=0.1,
            right=0.3,
            bottom=0.2,
        ),
        confidence=0.99,
        risk_level=risk_level,
        purpose=purpose,
        control_role=control_role,
    )


def _resolved_target_intent(
    candidate: AppVisualCandidate,
    *,
    source_capture_id: str = SOURCE_CAPTURE_ID,
    target_x: int = 200,
    target_y: int = 300,
    base_width: int = 1000,
    base_height: int = 2000,
) -> ActionIntent:
    return ActionIntent(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        intent_id="intent-resolved-target",
        idempotency_key="idem-resolved-target",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=source_capture_id,
        action_type=InteractionOperation.CLICK,
        semantic_target=candidate.semantic_name,
        purpose=candidate.purpose,
        capture_profile=CaptureProfilePolicy.for_surface(
            SurfaceKind.ANDROID,
            InteractionOperation.CLICK,
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        parameters={
            "candidate_id": candidate.candidate_id,
            "resolved_target": {
                "x": target_x,
                "y": target_y,
                "space": "capture_px",
                "base_width": base_width,
                "base_height": base_height,
            },
        },
        risk_level=InteractionRiskLevel.LOW,
    )


class _SequencedScopeReader:
    def __init__(self, *results: object | None) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    async def verify_execution_context(self, **kwargs: object) -> object | None:
        self.calls.append(kwargs)
        return self.results.pop(0) if self.results else None


class _SequencedResolvedTargetReader:
    """按调用顺序返回数据库中的当前视觉候选。"""

    def __init__(self, *results: AppVisualCandidate | None) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    async def read_resolved_candidate(self, **kwargs: object) -> AppVisualCandidate | None:
        self.calls.append(kwargs)
        return self.results.pop(0) if self.results else None


class _RecordingBridge(DryRunDeviceBridge):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def execute(self, device, *, action_type, parameters, semantic_target):  # noqa: ANN001
        self.calls.append(
            {
                "device": device,
                "action_type": action_type,
                "parameters": parameters,
                "semantic_target": semantic_target,
            }
        )
        return super().execute(
            device,
            action_type=action_type,
            parameters=parameters,
            semantic_target=semantic_target,
        )


_MISSING_RESOLVED_TARGET_READER = object()


def _service_with_reader(
    reader: _SequencedScopeReader,
    bridge: _RecordingBridge,
    *,
    resolved_target_reader: object = _MISSING_RESOLVED_TARGET_READER,
):
    sessions = XingyuSessionStore()
    record = sessions.create(
        session_id=SESSION_ID,
        task_id=TASK_ID,
        backend="fake",
        device=object(),
    )
    sessions.update(SESSION_ID, package_name="com.example.target", activity=".MainActivity")
    kwargs: dict[str, object] = {
        "sessions": sessions,
        "bridge": bridge,
        "command_scope_reader": reader,
        "require_execution_context": True,
    }
    if resolved_target_reader is not _MISSING_RESOLVED_TARGET_READER:
        kwargs["resolved_target_reader"] = resolved_target_reader
    service = XingyuOfficialService(
        **kwargs,
    )
    return service, record


def _xingyu_payload(
    record,
    intent: ActionIntent,
    *,
    resolved_candidate_id: str | None = None,
    context_overrides: dict[str, object] | None = None,
) -> dict[str, object]:  # noqa: ANN001
    context = dict(LEASE_CONTEXT)
    if resolved_candidate_id is not None:
        context["resolved_candidate_id"] = resolved_candidate_id
    if context_overrides:
        context.update(context_overrides)
    return {
        "session_handle": record.session_handle,
        "surface_ref": f"app-surface:{SESSION_ID}",
        "action_execution_id": LEASE_CONTEXT["action_execution_id"],
        "intent": intent.to_dict(),
        "execution_context": context,
    }


@pytest.mark.asyncio
async def test_xingyu_rechecks_scope_before_dispatch_and_validates_full_intent() -> None:
    bridge = _RecordingBridge()
    reader = _SequencedScopeReader(_scope(), _scope())
    service, record = _service_with_reader(reader, bridge)

    result = await service.execute(_xingyu_payload(record, _intent()))

    assert result["succeeded"] is True
    assert len(reader.calls) == 2
    assert len(bridge.calls) == 1
    assert reader.calls[0]["lease_token"] == LEASE_CONTEXT["lease_token"]


@pytest.mark.asyncio
async def test_xingyu_dispatches_formal_back_with_boundary_recovery_purpose() -> None:
    bridge = _RecordingBridge()
    reader = _SequencedScopeReader(
        _scope(operation=InteractionOperation.BACK),
        _scope(operation=InteractionOperation.BACK),
    )
    service, record = _service_with_reader(reader, bridge)
    intent = replace(
        _intent(operation=InteractionOperation.BACK, semantic_target="系统返回"),
        purpose="当前前台已离开目标 App 包边界，只允许恢复目标包后再执行 sibling。",
    )

    result = await service.execute(_xingyu_payload(record, intent))

    assert result["succeeded"] is True
    assert len(reader.calls) == 2
    assert len(bridge.calls) == 1
    assert bridge.calls[0]["action_type"] == InteractionOperation.BACK.value
    assert bridge.calls[0]["parameters"] == {}
    assert bridge.calls[0]["semantic_target"] == "系统返回"


@pytest.mark.asyncio
async def test_xingyu_rejects_back_when_server_scope_excludes_operation() -> None:
    bridge = _RecordingBridge()
    reader = _SequencedScopeReader(_scope(operation=InteractionOperation.TAP))
    service, record = _service_with_reader(reader, bridge)
    intent = replace(
        _intent(operation=InteractionOperation.BACK, semantic_target="系统返回"),
        purpose="当前前台已离开目标 App 包边界，只允许恢复目标包后再执行 sibling。",
    )

    with pytest.raises(XingyuOfficialError) as error:
        await service.execute(_xingyu_payload(record, intent))

    assert error.value.error_code == "COMMAND_OPERATION_NOT_ALLOWED"
    assert len(reader.calls) == 1
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_xingyu_rejects_stale_scope_between_validation_and_dispatch() -> None:
    bridge = _RecordingBridge()
    reader = _SequencedScopeReader(_scope(), None)
    service, record = _service_with_reader(reader, bridge)

    with pytest.raises(XingyuOfficialError) as error:
        await service.execute(_xingyu_payload(record, _intent()))

    assert error.value.error_code == "STALE_SESSION_LEASE"
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_xingyu_rejects_low_risk_payment_intent_at_official_boundary() -> None:
    bridge = _RecordingBridge()
    reader = _SequencedScopeReader(_scope(), _scope())
    service, record = _service_with_reader(reader, bridge)
    intent = _intent(semantic_target="确认支付")

    with pytest.raises(XingyuOfficialError) as error:
        await service.execute(_xingyu_payload(record, intent))

    assert error.value.error_code == "COMMAND_TARGET_SEMANTICS_UNTRUSTED"
    assert len(reader.calls) == 1
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_xingyu_accepts_low_risk_db_dismiss_candidate_inside_current_bounds() -> None:
    candidate = _resolved_candidate()
    bridge = _RecordingBridge()
    scope_reader = _SequencedScopeReader(
        _scope(operation=InteractionOperation.CLICK),
        _scope(operation=InteractionOperation.CLICK),
    )
    target_reader = _SequencedResolvedTargetReader(candidate, candidate)
    service, record = _service_with_reader(
        scope_reader,
        bridge,
        resolved_target_reader=target_reader,
    )
    intent = _resolved_target_intent(candidate)

    result = await service.execute(
        _xingyu_payload(
            record,
            intent,
            resolved_candidate_id=candidate.candidate_id,
        )
    )

    assert result["succeeded"] is True
    assert len(scope_reader.calls) == 2
    assert len(target_reader.calls) == 2
    assert len(bridge.calls) == 1
    assert all(
        call == {
            "task_id": TASK_ID,
            "session_id": SESSION_ID,
            "capture_id": SOURCE_CAPTURE_ID,
            "candidate_id": candidate.candidate_id,
            "package_name": "com.example.target",
        }
        for call in target_reader.calls
    )
    assert bridge.calls[0]["action_type"] == "tap"
    assert bridge.calls[0]["parameters"]["point"] == {
        "x": 200,
        "y": 300,
        "space": "capture_px",
    }


@pytest.mark.asyncio
async def test_xingyu_does_not_trust_client_target_metadata_over_db_candidate() -> None:
    # 数据库候选没有 dismiss 角色；请求上下文中的同名字段不能伪造可信语义。
    candidate = _resolved_candidate(control_role=None)
    bridge = _RecordingBridge()
    scope_reader = _SequencedScopeReader(_scope(operation=InteractionOperation.CLICK))
    target_reader = _SequencedResolvedTargetReader(candidate)
    service, record = _service_with_reader(
        scope_reader,
        bridge,
        resolved_target_reader=target_reader,
    )

    with pytest.raises(XingyuOfficialError) as error:
        await service.execute(
            _xingyu_payload(
                record,
                _resolved_target_intent(candidate),
                resolved_candidate_id=candidate.candidate_id,
                context_overrides={
                    "target_role": "dismiss",
                    "target_trust": "app_visual_candidate",
                    "trusted_target_texts": ["关闭支付弹窗"],
                },
            )
        )

    assert error.value.error_code == "COMMAND_TARGET_RISK_UNDERRATED"
    assert len(scope_reader.calls) == 1
    assert len(target_reader.calls) == 1
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_xingyu_rejects_resolved_target_without_db_candidate() -> None:
    candidate = _resolved_candidate()
    bridge = _RecordingBridge()
    scope_reader = _SequencedScopeReader(_scope(operation=InteractionOperation.CLICK))
    target_reader = _SequencedResolvedTargetReader(None)
    service, record = _service_with_reader(
        scope_reader,
        bridge,
        resolved_target_reader=target_reader,
    )

    with pytest.raises(XingyuOfficialError) as error:
        await service.execute(
            _xingyu_payload(
                record,
                _resolved_target_intent(candidate),
                resolved_candidate_id=candidate.candidate_id,
            )
        )

    assert error.value.error_code == "COMMAND_TARGET_SEMANTICS_UNTRUSTED"
    assert len(scope_reader.calls) == 1
    assert len(target_reader.calls) == 1
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_xingyu_rejects_when_db_candidate_semantic_name_differs() -> None:
    candidate = _resolved_candidate()
    bridge = _RecordingBridge()
    scope_reader = _SequencedScopeReader(_scope(operation=InteractionOperation.CLICK))
    target_reader = _SequencedResolvedTargetReader(candidate)
    service, record = _service_with_reader(
        scope_reader,
        bridge,
        resolved_target_reader=target_reader,
    )
    intent = replace(
        _resolved_target_intent(candidate),
        semantic_target="我已保存确认",
    )

    with pytest.raises(XingyuOfficialError) as error:
        await service.execute(
            _xingyu_payload(
                record,
                intent,
                resolved_candidate_id=candidate.candidate_id,
            )
        )

    assert error.value.error_code == "COMMAND_TARGET_SEMANTICS_UNTRUSTED"
    assert len(scope_reader.calls) == 1
    assert len(target_reader.calls) == 1
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_xingyu_does_not_trust_resolved_target_when_point_parameter_is_present() -> None:
    candidate = _resolved_candidate()
    bridge = _RecordingBridge()
    scope_reader = _SequencedScopeReader(_scope(operation=InteractionOperation.CLICK))
    target_reader = _SequencedResolvedTargetReader(candidate)
    service, record = _service_with_reader(
        scope_reader,
        bridge,
        resolved_target_reader=target_reader,
    )
    original = _resolved_target_intent(candidate)
    intent = replace(
        original,
        parameters={
            **original.parameters,
            "point": {"x": 200, "y": 300, "space": "device_px"},
        },
    )

    with pytest.raises(XingyuOfficialError) as error:
        await service.execute(
            _xingyu_payload(
                record,
                intent,
                resolved_candidate_id=candidate.candidate_id,
            )
        )

    assert error.value.error_code == "COMMAND_TARGET_SEMANTICS_UNTRUSTED"
    assert len(scope_reader.calls) == 1
    assert target_reader.calls == []
    assert bridge.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_x", "base_width", "base_height", "candidate_source_capture_id"),
    [
        (400, 1000, 2000, SOURCE_CAPTURE_ID),
        (200, 900, 2000, SOURCE_CAPTURE_ID),
        (200, 1000, 2000, "capture-other"),
    ],
)
async def test_xingyu_does_not_trust_candidate_after_geometry_or_capture_drift(
    target_x: int,
    base_width: int,
    base_height: int,
    candidate_source_capture_id: str,
) -> None:
    candidate = _resolved_candidate(source_capture_id=candidate_source_capture_id)
    bridge = _RecordingBridge()
    scope_reader = _SequencedScopeReader(_scope(operation=InteractionOperation.CLICK))
    target_reader = _SequencedResolvedTargetReader(candidate)
    service, record = _service_with_reader(
        scope_reader,
        bridge,
        resolved_target_reader=target_reader,
    )

    with pytest.raises(XingyuOfficialError) as error:
        await service.execute(
            _xingyu_payload(
                record,
                _resolved_target_intent(
                    candidate,
                    target_x=target_x,
                    base_width=base_width,
                    base_height=base_height,
                ),
                resolved_candidate_id=candidate.candidate_id,
            )
        )

    assert error.value.error_code == "COMMAND_TARGET_SEMANTICS_UNTRUSTED"
    assert len(scope_reader.calls) == 1
    assert len(target_reader.calls) == 1
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_xingyu_does_not_reuse_first_candidate_when_second_read_is_missing() -> None:
    candidate = _resolved_candidate()
    bridge = _RecordingBridge()
    scope_reader = _SequencedScopeReader(
        _scope(operation=InteractionOperation.CLICK),
        _scope(operation=InteractionOperation.CLICK),
    )
    target_reader = _SequencedResolvedTargetReader(candidate, None)
    service, record = _service_with_reader(
        scope_reader,
        bridge,
        resolved_target_reader=target_reader,
    )

    with pytest.raises(XingyuOfficialError) as error:
        await service.execute(
            _xingyu_payload(
                record,
                _resolved_target_intent(candidate),
                resolved_candidate_id=candidate.candidate_id,
            )
        )

    assert error.value.error_code == "COMMAND_TARGET_SEMANTICS_UNTRUSTED"
    assert len(scope_reader.calls) == 2
    assert len(target_reader.calls) == 2
    assert bridge.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate_risk",
    [InteractionRiskLevel.MEDIUM, InteractionRiskLevel.HIGH],
)
async def test_xingyu_rejects_low_risk_when_db_candidate_risk_is_higher(
    candidate_risk: InteractionRiskLevel,
) -> None:
    candidate = _resolved_candidate(risk_level=candidate_risk)
    bridge = _RecordingBridge()
    scope_reader = _SequencedScopeReader(_scope(operation=InteractionOperation.CLICK))
    target_reader = _SequencedResolvedTargetReader(candidate)
    service, record = _service_with_reader(
        scope_reader,
        bridge,
        resolved_target_reader=target_reader,
    )

    with pytest.raises(XingyuOfficialError) as error:
        await service.execute(
            _xingyu_payload(
                record,
                _resolved_target_intent(candidate),
                resolved_candidate_id=candidate.candidate_id,
            )
        )

    assert error.value.error_code == "COMMAND_TARGET_RISK_UNDERRATED"
    assert len(scope_reader.calls) == 1
    assert len(target_reader.calls) == 1
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_xingyu_rejects_without_a_resolved_target_reader() -> None:
    candidate = _resolved_candidate()
    bridge = _RecordingBridge()
    scope_reader = _SequencedScopeReader(_scope(operation=InteractionOperation.CLICK))
    service, record = _service_with_reader(
        scope_reader,
        bridge,
        resolved_target_reader=None,
    )

    with pytest.raises(XingyuOfficialError) as error:
        await service.execute(
            _xingyu_payload(
                record,
                _resolved_target_intent(candidate),
                resolved_candidate_id=candidate.candidate_id,
            )
        )

    assert error.value.error_code == "COMMAND_TARGET_SEMANTICS_UNTRUSTED"
    assert len(scope_reader.calls) == 1
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_restoration_executor_uses_context_port_for_warm_restore() -> None:
    calls: list[str] = []
    context_intents: list[ActionIntent] = []
    contexts: list[dict[str, object]] = []

    class _Driver:
        async def execute_with_context(self, **kwargs):  # noqa: ANN003
            calls.append("execute_with_context")
            context_intents.append(kwargs["intent"])
            contexts.append(dict(kwargs["execution_context"]))
            return RawActionResult(
                action_execution_id="action-1",
                succeeded=True,
                retryable=False,
            )

        async def warm_restore(self, _request):
            calls.append("warm_restore")
            return SimpleNamespace(succeeded=True)

    session = SurfaceSessionRef(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref=f"app-surface:{SESSION_ID}",
    )
    request = ExecuteInteractionRequest(
        agent_run_id="run-1",
        intent=_intent(
            operation=InteractionOperation.WARM_RESTORE,
            semantic_target=None,
            source_capture_id="stale-plan-source",
        ),
        source_capture_id=SOURCE_CAPTURE_ID,
        action_execution_id="action-1",
        finalization_epoch=0,
        command_sequence=1,
        lease_owner="worker-1",
        lease_token="11111111-1111-1111-1111-111111111111",
    )
    executor = AppRestorationInteractionExecutor(
        driver=_Driver(),
        surface_sessions=SimpleNamespace(),
        package_name="com.example.target",
        session=session,
        command_validator=InteractionCommandValidator(),
    )

    result = await executor.execute(request)

    assert result.status.value == "evidence_pending"
    assert calls == ["execute_with_context"]
    assert context_intents[0].source_capture_id == SOURCE_CAPTURE_ID
    assert contexts == [LEASE_CONTEXT]
    receipt = result.state_summary["app_command_broker_receipt"]
    assert receipt["task_id"] == TASK_ID
    assert receipt["session_id"] == SESSION_ID
    assert receipt["agent_run_id"] == "run-1"
    assert receipt["source_capture_id"] == SOURCE_CAPTURE_ID
    assert receipt["finalization_epoch"] == 0
    assert receipt["command_sequence"] == 1


@pytest.mark.asyncio
async def test_app_interaction_rejects_formal_request_before_bare_driver() -> None:
    class _Resolver:
        async def resolve(self, _request):  # noqa: ANN001
            return SimpleNamespace(
                candidate=SimpleNamespace(
                    candidate_id="candidate-1",
                    semantic_name="继续",
                    purpose="继续探索",
                    expected_state_hint=None,
                    control_role=None,
                ),
                before_capture=SimpleNamespace(
                    package_name="com.example.target",
                    activity=".MainActivity",
                ),
                resolved_target=None,
            )

    class _Sessions:
        async def get(self, *, session_id):  # noqa: ANN001
            return SimpleNamespace(
                task_id=TASK_ID,
                session_id=session_id,
                surface_kind=SurfaceKind.ANDROID,
                status=SurfaceSessionStatus.ACTIVE,
                current_surface_ref=f"app-surface:{session_id}",
            )

    class _BareDriver:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, **_kwargs):  # noqa: ANN003
            self.calls += 1
            return SimpleNamespace(succeeded=True, error_code=None, retryable=False)

    driver = _BareDriver()
    executor = AppInteractionExecutor(
        driver=driver,
        surface_sessions=_Sessions(),
        candidate_resolver=_Resolver(),
        allowed_packages=("com.example.target",),
        command_validator=InteractionCommandValidator(),
    )
    request = ExecuteInteractionRequest(
        agent_run_id="run-1",
        intent=_intent(),
        source_capture_id=SOURCE_CAPTURE_ID,
        action_execution_id="action-1",
        finalization_epoch=0,
        command_sequence=1,
        lease_owner="worker-1",
        lease_token="11111111-1111-1111-1111-111111111111",
    )

    result = await executor.execute(request)

    assert result.status.value == "failed"
    assert result.error_code == "EXECUTION_CONTEXT_UNSUPPORTED"
    assert result.retryable is False
    receipt = result.state_summary["app_command_broker_receipt"]
    assert receipt["status"] == "rejected_runtime_driver"
    assert receipt["result_summary"]["driver_invoked"] is False
    assert receipt["result_summary"]["error_code"] == "EXECUTION_CONTEXT_UNSUPPORTED"
    assert driver.calls == 0


@pytest.mark.asyncio
async def test_app_interaction_generates_resolved_candidate_id_in_execution_context() -> None:
    class _Resolver:
        async def resolve(self, _request):  # noqa: ANN001
            return SimpleNamespace(
                candidate=SimpleNamespace(
                    candidate_id="candidate-generated-by-runtime",
                    semantic_name="继续",
                    purpose="继续探索",
                    expected_state_hint=None,
                    control_role=None,
                ),
                before_capture=SimpleNamespace(
                    package_name="com.example.target",
                    activity=".MainActivity",
                ),
                resolved_target=None,
            )

    class _Sessions:
        async def get(self, *, session_id):  # noqa: ANN001
            return SimpleNamespace(
                task_id=TASK_ID,
                session_id=session_id,
                surface_kind=SurfaceKind.ANDROID,
                status=SurfaceSessionStatus.ACTIVE,
                current_surface_ref=f"app-surface:{session_id}",
            )

    class _ContextDriver:
        def __init__(self) -> None:
            self.contexts: list[dict[str, object]] = []

        async def execute_with_context(self, **kwargs):  # noqa: ANN003
            self.contexts.append(dict(kwargs["execution_context"]))
            return RawActionResult(
                action_execution_id=kwargs["action_execution_id"],
                succeeded=True,
                retryable=False,
            )

    driver = _ContextDriver()
    executor = AppInteractionExecutor(
        driver=driver,
        surface_sessions=_Sessions(),
        candidate_resolver=_Resolver(),
        allowed_packages=("com.example.target",),
        command_validator=InteractionCommandValidator(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id="run-1",
            intent=_intent(),
            source_capture_id=SOURCE_CAPTURE_ID,
            action_execution_id="action-1",
            finalization_epoch=0,
            command_sequence=1,
            lease_owner="worker-1",
            lease_token="11111111-1111-1111-1111-111111111111",
        )
    )

    assert result.status.value == "evidence_pending"
    assert len(driver.contexts) == 1
    assert driver.contexts[0]["resolved_candidate_id"] == (
        "candidate-generated-by-runtime"
    )


@pytest.mark.asyncio
async def test_restoration_interaction_rejects_formal_request_before_bare_driver() -> None:
    calls: list[str] = []

    class _BareDriver:
        async def execute(self, **_kwargs):  # noqa: ANN003
            calls.append("execute")
            return SimpleNamespace(succeeded=True, error_code=None, retryable=False)

        async def warm_restore(self, _request):
            calls.append("warm_restore")
            return SimpleNamespace(succeeded=True)

    session = SurfaceSessionRef(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref=f"app-surface:{SESSION_ID}",
    )
    request = ExecuteInteractionRequest(
        agent_run_id="run-1",
        intent=_intent(
            operation=InteractionOperation.WARM_RESTORE,
            semantic_target=None,
        ),
        source_capture_id=SOURCE_CAPTURE_ID,
        action_execution_id="action-1",
        finalization_epoch=0,
        command_sequence=1,
        lease_owner="worker-1",
        lease_token="11111111-1111-1111-1111-111111111111",
    )
    executor = AppRestorationInteractionExecutor(
        driver=_BareDriver(),
        surface_sessions=SimpleNamespace(),
        package_name="com.example.target",
        session=session,
        command_validator=InteractionCommandValidator(),
    )

    result = await executor.execute(request)

    assert result.status.value == "failed"
    assert result.error_code == "EXECUTION_CONTEXT_UNSUPPORTED"
    assert result.retryable is False
    receipt = result.state_summary["app_command_broker_receipt"]
    assert receipt["status"] == "rejected_runtime_driver"
    assert receipt["result_summary"]["driver_invoked"] is False
    assert receipt["result_summary"]["error_code"] == "EXECUTION_CONTEXT_UNSUPPORTED"
    assert calls == []


@pytest.mark.asyncio
async def test_formal_restoration_requires_validator_before_context_driver() -> None:
    calls: list[str] = []

    class _ContextDriver:
        async def execute_with_context(self, **_kwargs):  # noqa: ANN003
            calls.append("execute_with_context")
            return RawActionResult(
                action_execution_id="action-1",
                succeeded=True,
                retryable=False,
            )

    session = SurfaceSessionRef(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref=f"app-surface:{SESSION_ID}",
    )
    request = ExecuteInteractionRequest(
        agent_run_id="run-1",
        intent=_intent(),
        source_capture_id=SOURCE_CAPTURE_ID,
        action_execution_id="action-1",
        finalization_epoch=0,
        command_sequence=1,
        lease_owner="worker-1",
        lease_token="11111111-1111-1111-1111-111111111111",
    )
    executor = AppRestorationInteractionExecutor(
        driver=_ContextDriver(),
        surface_sessions=SimpleNamespace(),
        package_name="com.example.target",
        session=session,
    )

    result = await executor.execute(request)

    assert result.status.value == "failed"
    assert result.error_code == "COMMAND_VALIDATOR_REQUIRED"
    assert calls == []


@pytest.mark.asyncio
async def test_restoration_interaction_keeps_legacy_bare_driver_without_context() -> None:
    calls: list[str] = []

    class _BareDriver:
        async def execute(self, **_kwargs):  # noqa: ANN003
            calls.append("execute")
            return SimpleNamespace(succeeded=True, error_code=None, retryable=False)

        async def warm_restore(self, _request):
            calls.append("warm_restore")
            return SimpleNamespace(succeeded=True)

    session = SurfaceSessionRef(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref=f"app-surface:{SESSION_ID}",
    )
    request = ExecuteInteractionRequest(
        agent_run_id="run-1",
        intent=_intent(
            operation=InteractionOperation.WARM_RESTORE,
            semantic_target=None,
        ),
        source_capture_id=SOURCE_CAPTURE_ID,
        action_execution_id="action-legacy",
    )
    executor = AppRestorationInteractionExecutor(
        driver=_BareDriver(),
        surface_sessions=SimpleNamespace(),
        package_name="com.example.target",
        session=session,
    )

    result = await executor.execute(request)

    assert result.status.value == "evidence_pending"
    assert calls == ["warm_restore"]


@pytest.mark.asyncio
async def test_new_session_replay_fails_before_start_without_target_capture_seed() -> None:
    started: list[str] = []

    class _Driver:
        async def start_session(self, spec):  # noqa: ANN001
            started.append(spec.session_id)
            return SimpleNamespace(primary_surface_ref=f"app-surface:{spec.session_id}")

    session = SurfaceSessionRef(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.ANDROID,
        primary_surface_ref=f"app-surface:{SESSION_ID}",
    )
    executor = AppRestorationExecutor(
        driver=_Driver(),
        session=session,
        package_name="com.example.target",
        platform_build_ref="build-1",
        capture_coordinator=object(),
    )

    with pytest.raises(RestorationExecutionError) as error:
        await executor._start_new_replay_session(
            target_session_id="session-replay:1",
            attempt_id="attempt-1",
        )

    assert error.value.error_code == "REPLAY_SESSION_CAPTURE_SEED_REQUIRED"
    assert started == []


@pytest.mark.asyncio
async def test_official_runtime_passes_complete_execution_context_to_api() -> None:
    class _Api:
        def __init__(self) -> None:
            self.contexts: list[dict[str, object] | None] = []

        async def start_session(self, _spec):
            return OfficialSessionResponse(request_id="start", session_handle="private")

        async def open(self, request):
            return OfficialOpenResponse(
                request_id="open",
                succeeded=True,
                retryable=False,
                package_name=request.package_name,
                foreground_verified=True,
                activity=".MainActivity",
            )

        async def execute(
            self,
            _session,
            intent,
            action_execution_id,
            execution_context=None,
        ):
            self.contexts.append(dict(execution_context) if execution_context else None)
            return OfficialActionResponse(
                request_id="execute",
                action_execution_id=action_execution_id,
                succeeded=True,
                retryable=False,
                action_trace=(),
            )

    api = _Api()
    runtime = OfficialAppRuntime(api_client=api, max_screenshot_bytes=1024)
    session = await runtime.start_session(
        SurfaceSessionSpec(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref="build-1",
        )
    )
    await runtime.open_app(
        AppOpenRequest(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            package_name="com.example.target",
        )
    )

    await runtime.execute_with_context(
        session=session,
        intent=_intent(),
        action_execution_id="action-1",
        execution_context=LEASE_CONTEXT,
    )

    assert api.contexts == [LEASE_CONTEXT]
