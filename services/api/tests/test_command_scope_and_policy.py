"""命令范围事实源与 Coordinator 前置安全校验。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
)
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidationError,
    TrustedCommandScope,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureKind,
    CaptureProfile,
    CaptureRef,
    CommandPolicy,
    CommandPolicyError,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.interaction.command_policy import (
    DEFAULT_ALLOWED_OPERATIONS,
    SERVER_MINIMUM_RISK_BY_OPERATION,
)
from services.api.app.infrastructure.db.command_scope_reader import (
    PostgresTaskCommandScopeReader,
)

TASK_ID = "123e4567-e89b-12d3-a456-426614174000"


class _Connection:
    def __init__(self, row: dict | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *args: object):
        self.calls.append((query, args))
        return self.row


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


def _scope_row(**overrides: object) -> dict:
    row = {
        "task_id": TASK_ID,
        "target_platform": "h5",
        "target_url": "https://m.example.test/home",
        "task_finalization_epoch": 4,
        "session_id": "h5-session-1",
        "surface_kind": "web",
        "session_status": "active",
        "session_finalization_epoch": 4,
        "platform_build_ref": None,
        "build_package_name": None,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_postgres_scope_reader_uses_task_session_and_target_facts() -> None:
    connection = _Connection(_scope_row())
    reader = PostgresTaskCommandScopeReader(pool=_Pool(connection))

    scope = await reader.read_command_scope(
        task_id=TASK_ID.upper(),
        session_id="h5-session-1",
        surface_kind=SurfaceKind.WEB,
    )

    assert scope is not None
    assert scope.task_id == TASK_ID
    assert scope.policy.allowed_hosts == ("m.example.test",)
    assert scope.policy.minimum_risk_by_operation[InteractionOperation.NETWORK_FAULT] is (
        InteractionRiskLevel.HIGH
    )
    assert connection.calls
    assert connection.calls[0][1] == (TASK_ID.upper(), "h5-session-1", "web")

    with pytest.raises(CommandPolicyError) as error:
        scope.policy.validate_runtime_location(
            surface_kind=SurfaceKind.WEB,
            url="https://outside.example.test/landing",
        )
    assert error.value.error_code == "COMMAND_HOST_BOUNDARY_VIOLATION"


@pytest.mark.asyncio
async def test_scope_reader_rejects_stale_epoch_or_platform_mismatch() -> None:
    stale = _Connection(_scope_row(session_finalization_epoch=3))
    reader = PostgresTaskCommandScopeReader(pool=_Pool(stale))
    assert (
        await reader.read_command_scope(
            task_id=TASK_ID,
            session_id="h5-session-1",
            surface_kind=SurfaceKind.WEB,
        )
        is None
    )

    missing_package = _Connection(
        _scope_row(
            target_platform="app",
            target_url=None,
            surface_kind="android",
            session_id="app-session-1",
            platform_build_ref="app-build:build-1",
            build_package_name=None,
        )
    )
    reader = PostgresTaskCommandScopeReader(pool=_Pool(missing_package))
    assert (
        await reader.read_command_scope(
            task_id=TASK_ID,
            session_id="app-session-1",
            surface_kind=SurfaceKind.ANDROID,
        )
        is None
    )

    mismatch = _Connection(_scope_row(target_platform="app", surface_kind="android"))
    reader = PostgresTaskCommandScopeReader(pool=_Pool(mismatch))
    assert (
        await reader.read_command_scope(
            task_id=TASK_ID,
            session_id="h5-session-1",
            surface_kind=SurfaceKind.WEB,
        )
        is None
    )


@pytest.mark.asyncio
async def test_server_scope_rejects_low_risk_reset_even_when_agent_self_reports_low() -> None:
    connection = _Connection(
        _scope_row(
            target_platform="app",
            target_url=None,
            surface_kind="android",
            session_id="app-session-1",
            build_package_name="com.example.target",
            platform_build_ref="app-build:build-1",
        )
    )
    reader = PostgresTaskCommandScopeReader(pool=_Pool(connection))
    scope = await reader.read_command_scope(
        task_id=TASK_ID,
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
    )
    assert scope is not None

    intent = _intent(
        task_id=TASK_ID,
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        operation=InteractionOperation.RESET_APP,
        risk_level=InteractionRiskLevel.LOW,
        parameters={
            "package_name": "com.example.target",
            "reset_policy": "force_stop",
        },
    )
    from services.api.app.application.interaction_command_validation import (
        InteractionCommandValidator,
    )

    with pytest.raises(InteractionCommandValidationError) as error:
        await InteractionCommandValidator(scope_reader=reader).validate_for(
            CaptureCoordinationCommand(agent_run_id="run-1", intent=intent)
        )
    assert error.value.error_code == "COMMAND_RISK_UNDERRATED"


@pytest.mark.asyncio
async def test_validator_rejects_scope_epoch_that_does_not_match_lease() -> None:
    class _Reader:
        async def read_command_scope(self, **_kwargs):
            return TrustedCommandScope(
                task_id=TASK_ID,
                session_id="h5-session-1",
                surface_kind=SurfaceKind.WEB,
                policy=CommandPolicy.default(),
                finalization_epoch=4,
            )

    from services.api.app.application.interaction_command_validation import (
        InteractionCommandValidator,
    )

    with pytest.raises(InteractionCommandValidationError) as error:
        await InteractionCommandValidator(scope_reader=_Reader()).validate_for(
            CaptureCoordinationCommand(
                agent_run_id="run-1",
                intent=_intent(
                    task_id=TASK_ID,
                    session_id="h5-session-1",
                    surface_kind=SurfaceKind.WEB,
                    operation=InteractionOperation.CLICK,
                    risk_level=InteractionRiskLevel.LOW,
                ),
            ),
            expected_finalization_epoch=5,
        )
    assert error.value.error_code == "COMMAND_SCOPE_EPOCH_MISMATCH"


@pytest.mark.asyncio
async def test_validator_rejects_scope_without_epoch_when_lease_epoch_is_expected() -> None:
    class _Reader:
        async def read_command_scope(self, **_kwargs):
            return TrustedCommandScope(
                task_id=TASK_ID,
                session_id="h5-session-1",
                surface_kind=SurfaceKind.WEB,
                policy=CommandPolicy.default(),
            )

    from services.api.app.application.interaction_command_validation import (
        InteractionCommandValidator,
    )

    with pytest.raises(InteractionCommandValidationError) as error:
        await InteractionCommandValidator(scope_reader=_Reader()).validate_for(
            CaptureCoordinationCommand(
                agent_run_id="run-1",
                intent=_intent(
                    task_id=TASK_ID,
                    session_id="h5-session-1",
                    surface_kind=SurfaceKind.WEB,
                    operation=InteractionOperation.CLICK,
                    risk_level=InteractionRiskLevel.LOW,
                ),
            ),
            expected_finalization_epoch=0,
        )
    assert error.value.error_code == "COMMAND_SCOPE_EPOCH_UNAVAILABLE"


@pytest.mark.asyncio
async def test_resolved_target_rechecks_fresh_policy_after_allow_is_revoked() -> None:
    class _Reader:
        def __init__(self) -> None:
            self.calls = 0

        async def read_command_scope(self, **_kwargs):
            self.calls += 1
            policy = (
                CommandPolicy.default()
                if self.calls == 1
                else CommandPolicy(
                    allowed_operations={
                        SurfaceKind.WEB: (InteractionOperation.OBSERVE,),
                    }
                )
            )
            return TrustedCommandScope(
                task_id=TASK_ID,
                session_id="h5-session-1",
                surface_kind=SurfaceKind.WEB,
                policy=policy,
            )

    from services.api.app.application.interaction_command_validation import (
        InteractionCommandValidator,
    )

    reader = _Reader()
    validator = InteractionCommandValidator(scope_reader=reader)
    command = CaptureCoordinationCommand(
        agent_run_id="run-1",
        intent=_intent(
            task_id=TASK_ID,
            session_id="h5-session-1",
            surface_kind=SurfaceKind.WEB,
            operation=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
        ),
    )
    await validator.validate_for(command)
    with pytest.raises(InteractionCommandValidationError) as error:
        await validator.validate_resolved_target_for(
            command,
            trusted_target_texts=("关闭弹窗",),
            target_trust="dom_snapshot",
        )
    assert error.value.error_code == "COMMAND_OPERATION_NOT_ALLOWED"
    assert reader.calls == 2


@pytest.mark.asyncio
async def test_runtime_location_rechecks_fresh_policy_after_allow_is_revoked() -> None:
    class _Reader:
        def __init__(self) -> None:
            self.calls = 0

        async def read_command_scope(self, **_kwargs):
            self.calls += 1
            policy = (
                CommandPolicy.default()
                if self.calls == 1
                else CommandPolicy(
                    allowed_operations={
                        SurfaceKind.WEB: (InteractionOperation.OBSERVE,),
                    }
                )
            )
            return TrustedCommandScope(
                task_id=TASK_ID,
                session_id="h5-session-1",
                surface_kind=SurfaceKind.WEB,
                policy=policy,
            )

    from services.api.app.application.interaction_command_validation import (
        InteractionCommandValidator,
    )

    reader = _Reader()
    validator = InteractionCommandValidator(scope_reader=reader)
    command = CaptureCoordinationCommand(
        agent_run_id="run-1",
        intent=_intent(
            task_id=TASK_ID,
            session_id="h5-session-1",
            surface_kind=SurfaceKind.WEB,
            operation=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
        ),
    )
    await validator.validate_for(command)
    with pytest.raises(InteractionCommandValidationError) as error:
        await validator.validate_runtime_location_for(
            command,
            url="https://m.example.test/home",
        )
    assert error.value.error_code == "COMMAND_OPERATION_NOT_ALLOWED"
    assert reader.calls == 2


@pytest.mark.asyncio
async def test_coordinator_rejects_scope_before_capture_or_driver() -> None:
    class _Reader:
        async def read_command_scope(self, **_kwargs):
            return None

    class _Writer:
        calls = 0

        async def capture(self, _request):
            self.calls += 1
            raise AssertionError("拒绝的命令不应进入 Capture Writer")

    class _Executor:
        calls = 0

        async def execute(self, _request):
            self.calls += 1
            raise AssertionError("拒绝的命令不应进入 Driver")

    writer = _Writer()
    executor = _Executor()
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=object(),
        command_scope_reader=_Reader(),
    )
    with pytest.raises(CaptureCoordinationError) as error:
        await coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id="run-1",
                intent=_intent(
                    task_id=TASK_ID,
                    session_id="h5-session-1",
                    surface_kind=SurfaceKind.WEB,
                    operation=InteractionOperation.CLICK,
                    risk_level=InteractionRiskLevel.LOW,
                ),
            )
        )
    assert error.value.error_code == "COMMAND_SCOPE_UNAVAILABLE"
    assert writer.calls == 0
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_coordinator_rechecks_scope_inside_lease_before_gate() -> None:
    class _Reader:
        def __init__(self) -> None:
            self.calls = 0

        async def read_command_scope(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return TrustedCommandScope(
                    task_id=TASK_ID,
                    session_id="h5-session-1",
                    surface_kind=SurfaceKind.WEB,
                    policy=CommandPolicy.default(),
                    finalization_epoch=0,
                )
            return None

    class _Gate:
        calls = 0

        async def reserve(self, **_kwargs):
            self.calls += 1
            raise AssertionError("范围过期时不应进入 Evidence Exit Gate")

    class _Writer:
        calls = 0

        async def capture(self, _request):
            self.calls += 1
            raise AssertionError("范围过期时不应进入 Capture Writer")

    class _Executor:
        calls = 0

        async def execute(self, _request):
            self.calls += 1
            raise AssertionError("范围过期时不应进入 Driver")

    reader = _Reader()
    gate = _Gate()
    writer = _Writer()
    executor = _Executor()
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=gate,
        command_scope_reader=reader,
    )

    with pytest.raises(CaptureCoordinationError) as error:
        await coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id="run-1",
                intent=_intent(
                    task_id=TASK_ID,
                    session_id="h5-session-1",
                    surface_kind=SurfaceKind.WEB,
                    operation=InteractionOperation.CLICK,
                    risk_level=InteractionRiskLevel.LOW,
                ),
            )
        )

    assert error.value.error_code == "COMMAND_SCOPE_UNAVAILABLE"
    assert reader.calls == 2
    assert gate.calls == 0
    assert writer.calls == 0
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_coordinator_rechecks_scope_immediately_before_driver() -> None:
    class _Reader:
        def __init__(self) -> None:
            self.calls = 0

        async def read_command_scope(self, **_kwargs):
            self.calls += 1
            if self.calls < 3:
                return TrustedCommandScope(
                    task_id=TASK_ID,
                    session_id="h5-session-1",
                    surface_kind=SurfaceKind.WEB,
                    policy=CommandPolicy.default(),
                    finalization_epoch=0,
                )
            return None

    class _Writer:
        def __init__(self) -> None:
            self.calls = 0

        async def capture(self, request):
            self.calls += 1
            return CaptureRef(
                task_id=TASK_ID,
                session_id="h5-session-1",
                capture_id=f"capture-{self.calls}",
                surface_kind=SurfaceKind.WEB,
                capture_kind=CaptureKind.PAGE,
                role=request.directive.role,
                artifact_ids=(f"artifact-{self.calls}",),
                parent_capture_id=request.parent_capture_id,
                action_execution_id=request.action_execution_id,
            )

    class _Gate:
        async def reserve(self, **_kwargs):
            return None

    class _Executor:
        calls = 0

        async def execute(self, _request):
            self.calls += 1
            raise AssertionError("范围换 Epoch 时不应调用 Driver")

    reader = _Reader()
    writer = _Writer()
    executor = _Executor()
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=_Gate(),
        command_scope_reader=reader,
    )

    with pytest.raises(CaptureCoordinationError) as error:
        await coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id="run-1",
                intent=_intent(
                    task_id=TASK_ID,
                    session_id="h5-session-1",
                    surface_kind=SurfaceKind.WEB,
                    operation=InteractionOperation.CLICK,
                    risk_level=InteractionRiskLevel.LOW,
                ),
            )
        )

    assert error.value.error_code == "COMMAND_SCOPE_UNAVAILABLE"
    assert error.value.stage.value == "action"
    assert error.value.before_capture is not None
    assert reader.calls == 3
    assert writer.calls == 1
    assert executor.calls == 0


@pytest.mark.parametrize(
    ("operation", "target_texts"),
    (
        (InteractionOperation.CLICK, ("立即支付", "支付订单")),
        (InteractionOperation.TAP, ("删除账号", "确认删除")),
        (InteractionOperation.INPUT, ("支付密码", "密码输入框")),
        (InteractionOperation.CLICK, ("确认授权", "允许权限")),
    ),
)
def test_policy_does_not_trust_low_risk_for_resolved_high_impact_target(
    operation: InteractionOperation,
    target_texts: tuple[str, ...],
) -> None:
    intent = _intent(
        task_id=TASK_ID,
        session_id="h5-session-1",
        surface_kind=SurfaceKind.WEB,
        operation=operation,
        risk_level=InteractionRiskLevel.LOW,
    )

    with pytest.raises(CommandPolicyError) as error:
        CommandPolicy.default().validate_resolved_target(
            intent,
            trusted_target_texts=target_texts,
            target_trust="dom_snapshot",
        )
    assert error.value.error_code == "COMMAND_TARGET_RISK_UNDERRATED"


def test_policy_rejects_dangerous_agent_label_when_snapshot_semantics_are_missing() -> None:
    intent = _intent(
        task_id=TASK_ID,
        session_id="h5-session-1",
        surface_kind=SurfaceKind.WEB,
        operation=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
    )
    intent = _replace_intent(intent, semantic_target="支付")

    with pytest.raises(CommandPolicyError) as error:
        CommandPolicy.default().validate_resolved_target(intent)
    assert error.value.error_code == "COMMAND_TARGET_SEMANTICS_UNTRUSTED"


def test_policy_allows_targetless_observe_even_when_purpose_mentions_sensitive_page() -> None:
    intent = _intent(
        task_id=TASK_ID,
        session_id="h5-session-1",
        surface_kind=SurfaceKind.WEB,
        operation=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
    )
    intent = _replace_intent(intent, semantic_target="观察支付授权页面")

    assert CommandPolicy.default().validate_resolved_target(intent) is None


@pytest.mark.parametrize(
    "texts",
    [
        ("暂不授权", "确认授权"),
        ("拒绝授权", "立即支付"),
        ("暂不授权并删除账号",),
    ],
)
def test_denial_text_does_not_hide_another_target_side_effect(texts):
    intent = _intent(
        task_id=TASK_ID,
        session_id="h5-session-1",
        surface_kind=SurfaceKind.WEB,
        operation=InteractionOperation.INPUT,
        risk_level=InteractionRiskLevel.LOW,
    )
    with pytest.raises(CommandPolicyError) as error:
        CommandPolicy.default().validate_resolved_target(
            intent,
            trusted_target_texts=texts,
            target_trust="dom_snapshot",
        )
    assert error.value.error_code == "COMMAND_TARGET_RISK_UNDERRATED"


def test_explicit_denial_alone_does_not_become_permission_grant():
    from services.api.app.domain.interaction.command_policy import infer_resolved_target_risk

    assert infer_resolved_target_risk(trusted_target_texts=("暂不授权",)) is None


def test_policy_does_not_misclassify_navigation_to_payment_as_high_risk() -> None:
    intent = _intent(
        task_id=TASK_ID,
        session_id="h5-session-1",
        surface_kind=SurfaceKind.WEB,
        operation=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.MEDIUM,
    )
    inferred = CommandPolicy.default().validate_resolved_target(
        intent,
        trusted_target_texts=("进入支付页面", "个人中心入口"),
        target_trust="logical_snapshot",
    )
    assert inferred is InteractionRiskLevel.MEDIUM


def test_policy_allows_platform_verified_dismiss_control_even_when_popup_mentions_payment() -> None:
    intent = _intent(
        task_id=TASK_ID,
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        operation=InteractionOperation.TAP,
        risk_level=InteractionRiskLevel.LOW,
    )
    assert (
        CommandPolicy.default().validate_resolved_target(
            intent,
            trusted_target_texts=("关闭支付弹窗", "立即支付"),
            target_trust="app_visual_candidate",
            target_role="dismiss",
        )
        is InteractionRiskLevel.LOW
    )


def test_policy_allows_top_left_back_even_when_purpose_mentions_payment() -> None:
    """左上角返回的 purpose 即使写「禁止点支付」也不得抬成 MEDIUM/HIGH。"""

    intent = _intent(
        task_id=TASK_ID,
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        operation=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
    )
    assert (
        CommandPolicy.default().validate_resolved_target(
            intent,
            trusted_target_texts=(
                "左上角返回",
                "付费／会员中心先点左上角「<」返回，禁止点底栏或支付。",
                "back",
            ),
            target_trust="app_visual_candidate",
        )
        is InteractionRiskLevel.LOW
    )


def test_policy_rejects_warm_restore_declared_below_server_minimum() -> None:
    """桌面热恢复最低 MEDIUM；自报 LOW 必须 COMMAND_RISK_UNDERRATED。"""

    policy = CommandPolicy(
        allowed_operations=DEFAULT_ALLOWED_OPERATIONS,
        allowed_risk_levels=(
            InteractionRiskLevel.LOW,
            InteractionRiskLevel.MEDIUM,
            InteractionRiskLevel.HIGH,
        ),
        minimum_risk_by_operation=SERVER_MINIMUM_RISK_BY_OPERATION,
    )
    low = _intent(
        task_id=TASK_ID,
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        operation=InteractionOperation.WARM_RESTORE,
        risk_level=InteractionRiskLevel.LOW,
        parameters={"package_name": "com.example.target"},
    )
    with pytest.raises(CommandPolicyError) as error:
        policy.validate(low)
    assert error.value.error_code == "COMMAND_RISK_UNDERRATED"

    medium = _replace_intent(low, risk_level=InteractionRiskLevel.MEDIUM)
    policy.validate(medium)


def test_policy_requires_high_risk_approval_for_clear_data_reset() -> None:
    low = _intent(
        task_id=TASK_ID,
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        operation=InteractionOperation.RESET_APP,
        risk_level=InteractionRiskLevel.MEDIUM,
        parameters={"reset_policy": "clear_data"},
    )
    with pytest.raises(CommandPolicyError) as error:
        CommandPolicy.default().validate(low)
    assert error.value.error_code == "COMMAND_RISK_UNDERRATED"

    high = _replace_intent(low, risk_level=InteractionRiskLevel.HIGH)
    policy = CommandPolicy(
        allowed_risk_levels=(
            InteractionRiskLevel.LOW,
            InteractionRiskLevel.MEDIUM,
            InteractionRiskLevel.HIGH,
        )
    )
    with pytest.raises(CommandPolicyError) as error:
        policy.validate(high)
    assert error.value.error_code == "COMMAND_HIGH_RISK_APPROVAL_REQUIRED"


def _replace_intent(intent: ActionIntent, **changes: object) -> ActionIntent:
    from dataclasses import replace

    return replace(intent, **changes)


def _intent(
    *,
    task_id: str,
    session_id: str,
    surface_kind: SurfaceKind,
    operation: InteractionOperation,
    risk_level: InteractionRiskLevel,
    parameters: dict | None = None,
) -> ActionIntent:
    targeted = operation in {
        InteractionOperation.NAVIGATE,
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.INPUT,
        InteractionOperation.HOVER,
        InteractionOperation.SEEK_MEDIA,
    }
    return ActionIntent(
        task_id=task_id,
        session_id=session_id,
        intent_id=f"intent-{operation.value}",
        idempotency_key=f"idem-{operation.value}",
        surface_kind=surface_kind,
        source_capture_id="capture-1",
        action_type=operation,
        semantic_target="关闭弹窗" if targeted else None,
        purpose="scope-test",
        capture_profile=CaptureProfile(
            profile_id="scope-test",
            before=CaptureDirective(role="before_action", artifact_types=("screenshot",)),
            after=CaptureDirective(role="after_action", artifact_types=("screenshot",)),
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        parameters=parameters or {},
        risk_level=risk_level,
    )
