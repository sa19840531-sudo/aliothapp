"""SurfaceDriver 的统一 Broker 出口。

本模块校验动作身份、调用唯一的 Driver 端口并生成执行回执。ADB M4 的命令
授权仍由 app_command_broker 负责；这里的 mode 只作为审计元数据，不会
因为 M4 为 off 就阻断正式 App 动作。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.application.app_bounded_exploration_milestones import (
    build_app_bounded_exploration_milestone_policy,
)
from services.api.app.application.app_command_broker import (
    AppCommandBrokerMode,
    build_app_command_receipt,
    resolve_app_command_broker_mode,
)
from services.api.app.application.capture_coordination import CaptureCoordinationCommand
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidationError,
    InteractionCommandValidator,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    RawActionResult,
    SurfaceDriverError,
    SurfaceSessionRef,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.app_runtime.fake import FakeAppRuntime

_CONTEXT_FIELDS = (
    "task_id",
    "session_id",
    "surface_kind",
    "action_execution_id",
    "source_capture_id",
    "finalization_epoch",
    "command_sequence",
    "lease_owner",
    "lease_token",
)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _command(intent: ActionIntent | None) -> str:
    if not isinstance(intent, ActionIntent):
        return "unknown"
    operation = intent.action_type.value
    return {"click": "tap", "tap": "tap", "swipe": "swipe", "scroll": "swipe", "back": "back"}.get(
        operation, f"driver_{operation}"
    )


def _parameter_summary(intent: ActionIntent | None) -> dict[str, Any]:
    parameters = intent.parameters if isinstance(intent, ActionIntent) else {}
    keys = sorted(str(key) for key in parameters)
    return {
        "parameter_keys": keys,
        "parameter_count": len(keys),
        "parameter_sha256": stable_protocol_hash(parameters),
    }


def _mode(policy: Mapping[str, Any] | None) -> AppCommandBrokerMode:
    source = (
        dict(policy)
        if isinstance(policy, Mapping)
        else build_app_bounded_exploration_milestone_policy()
    )
    try:
        return resolve_app_command_broker_mode(source)
    except Exception:  # noqa: BLE001 - mode 只用于审计，异常时保守记录
        return AppCommandBrokerMode.OFF


def _failure(action_id: str, code: str, retryable: bool) -> RawActionResult:
    return RawActionResult(
        action_execution_id=action_id,
        succeeded=False,
        retryable=bool(retryable),
        error_code=code,
    )


def _receipt(
    *,
    intent: ActionIntent | None,
    session: SurfaceSessionRef | None,
    action_execution_id: str,
    source_capture_id: str | None,
    agent_run_id: str | None,
    mode: AppCommandBrokerMode,
    status: str,
    reason_codes: Sequence[str],
    driver_invoked: bool,
    foreground_package: str | None,
    package_allowlist: Sequence[str] | None,
    serial: str | None,
    capture_epoch: str | None,
    context: Mapping[str, Any] | None,
    error_code: str | None = None,
    retryable: bool | None = None,
    succeeded: bool | None = None,
) -> dict[str, Any]:
    summary = _parameter_summary(intent)
    summary["driver_invoked"] = driver_invoked
    if _text(foreground_package):
        summary["foreground_package"] = _text(foreground_package)
    for key, value in (
        ("error_code", error_code),
        ("retryable", retryable),
        ("succeeded", succeeded),
    ):
        if value is not None:
            summary[key] = value
    context = context or {}
    identity = {
        "task_id": session.task_id if session else getattr(intent, "task_id", None),
        "session_id": session.session_id if session else getattr(intent, "session_id", None),
        "agent_run_id": agent_run_id,
        "intent_id": getattr(intent, "intent_id", None),
        "action_execution_id": action_execution_id,
        "source_capture_id": source_capture_id or getattr(intent, "source_capture_id", None),
        "surface_kind": (
            session.surface_kind.value
            if session
            else getattr(getattr(intent, "surface_kind", None), "value", None)
        ),
        "finalization_epoch": context.get("finalization_epoch"),
        "command_sequence": context.get("command_sequence"),
    }
    return build_app_command_receipt(
        command=_command(intent),
        status=status,
        mode=mode,
        reason_codes=reason_codes,
        package_allowlist=package_allowlist,
        serial=serial,
        capture_epoch=capture_epoch or source_capture_id,
        result_summary=summary,
        **identity,
    )


def _reject(
    common: dict[str, Any],
    error: tuple[str, str, bool],
) -> tuple[RawActionResult, dict[str, Any]]:
    code, reason, retryable = error
    return (
        _failure(common["action_execution_id"], code, retryable),
        _receipt(
            **common,
            status="rejected_runtime_driver",
            reason_codes=("broker_validation_failed", reason),
            driver_invoked=False,
            error_code=code,
            retryable=retryable,
            succeeded=False,
        ),
    )


def _context_error(
    context: Mapping[str, Any],
    *,
    session: SurfaceSessionRef,
    action_execution_id: str,
    source_capture_id: str,
) -> str | None:
    expected = {
        "task_id": session.task_id,
        "session_id": session.session_id,
        "surface_kind": session.surface_kind.value,
        "action_execution_id": action_execution_id,
        "source_capture_id": source_capture_id,
    }
    if any(context.get(key) != value for key, value in expected.items()):
        return "EXECUTION_CONTEXT_MISMATCH"
    if any(key not in context for key in _CONTEXT_FIELDS):
        return "EXECUTION_CONTEXT_INVALID"
    epoch, sequence = context["finalization_epoch"], context["command_sequence"]
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 0
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= 0
    ):
        return "EXECUTION_CONTEXT_INVALID"
    if any(
        not isinstance(context[key], str)
        or not context[key].strip()
        or context[key] != context[key].strip()
        for key in ("lease_owner", "lease_token")
    ):
        return "EXECUTION_CONTEXT_INVALID"
    return None


def _basic_error(
    *,
    session: Any,
    intent: Any,
    action_id: str,
    requested_action_id: str,
    source_capture_id: str | None,
    execution_context: Any,
) -> tuple[str, str, bool] | None:
    if not isinstance(session, SurfaceSessionRef):
        return "SESSION_INVALID", "session_invalid", False
    if not isinstance(intent, ActionIntent):
        return "INTENT_INVALID", "intent_invalid", False
    if not _text(requested_action_id):
        return "ACTION_EXECUTION_REQUIRED", "action_execution_id_required", False
    if (
        intent.task_id != session.task_id
        or intent.session_id != session.session_id
        or intent.surface_kind is not session.surface_kind
    ):
        return "SESSION_INTENT_MISMATCH", "session_intent_mismatch", False
    source = _text(source_capture_id) or intent.source_capture_id
    if source != intent.source_capture_id:
        return "SOURCE_CAPTURE_MISMATCH", "source_capture_mismatch", False
    if execution_context is None:
        return None
    if not isinstance(execution_context, Mapping):
        return "EXECUTION_CONTEXT_INVALID", "execution_context_invalid", False
    context_error = _context_error(
        execution_context, session=session, action_execution_id=action_id, source_capture_id=source
    )
    return (context_error, "execution_context_invalid", False) if context_error else None


async def _validator_error(
    *,
    command_validator: Any,
    driver: Any,
    formal: bool,
    agent_run_id: str | None,
    intent: ActionIntent,
    context: Mapping[str, Any] | None,
) -> tuple[str, str, bool] | None:
    if command_validator is None:
        if formal or not isinstance(driver, FakeAppRuntime):
            return "COMMAND_VALIDATOR_REQUIRED", "command_validator_required", False
        return None
    if not isinstance(command_validator, InteractionCommandValidator):
        return "COMMAND_VALIDATOR_INVALID", "command_validator_invalid", False
    if not _text(agent_run_id):
        return "AGENT_RUN_ID_REQUIRED", "agent_run_id_required", False
    try:
        await command_validator.validate_for(
            CaptureCoordinationCommand(agent_run_id=agent_run_id, intent=intent),
            expected_finalization_epoch=(context or {}).get("finalization_epoch"),
        )
    except InteractionCommandValidationError as exc:
        code = _text(exc.error_code) or "COMMAND_VALIDATION_FAILED"
        return code, code, bool(exc.retryable)
    except Exception:  # noqa: BLE001 - 校验器异常必须失败关闭
        return "COMMAND_VALIDATION_FAILED", "validator_exception", True
    return None


async def execute_app_driver_action_via_broker(
    *,
    driver: Any,
    session: Any,
    intent: Any,
    action_execution_id: str,
    execution_context: Any | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
    package_allowlist: Sequence[str] | None = None,
    serial: str | None = None,
    capture_epoch: str | None = None,
    foreground_package: str | None = None,
    agent_run_id: str | None = None,
    source_capture_id: str | None = None,
    command_validator: InteractionCommandValidator | None = None,
) -> tuple[RawActionResult, dict[str, Any]]:
    """校验并执行一次动作；所有 Driver 结果都转换为安全 RawActionResult。"""

    mode = _mode(milestone_policy)
    requested_action_id = action_execution_id
    action_id = _text(action_execution_id) or "invalid-action-execution"
    context = execution_context if isinstance(execution_context, Mapping) else None
    common: dict[str, Any] = {
        "intent": intent if isinstance(intent, ActionIntent) else None,
        "session": session if isinstance(session, SurfaceSessionRef) else None,
        "action_execution_id": action_id,
        "source_capture_id": source_capture_id,
        "agent_run_id": agent_run_id,
        "mode": mode,
        "foreground_package": foreground_package,
        "package_allowlist": package_allowlist,
        "serial": serial,
        "capture_epoch": capture_epoch,
        "context": context,
    }
    error = _basic_error(
        session=session,
        intent=intent,
        action_id=action_id,
        requested_action_id=requested_action_id,
        source_capture_id=source_capture_id,
        execution_context=execution_context,
    )
    if error:
        return _reject(common, error)
    source = _text(source_capture_id) or intent.source_capture_id
    common["source_capture_id"] = source
    formal = execution_context is not None
    error = await _validator_error(
        command_validator=command_validator,
        driver=driver,
        formal=formal,
        agent_run_id=agent_run_id,
        intent=intent,
        context=context,
    )
    if error:
        return _reject(common, error)
    execute_with_context = getattr(driver, "execute_with_context", None)
    if formal and not callable(execute_with_context):
        return _reject(
            common, ("EXECUTION_CONTEXT_UNSUPPORTED", "execution_context_unsupported", False)
        )
    try:
        raw = await (
            execute_with_context(
                session=session,
                intent=intent,
                action_execution_id=action_id,
                execution_context=context,
            )
            if formal
            else driver.execute(session=session, intent=intent, action_execution_id=action_id)
        )
        status, reasons = None, ()
    except SurfaceDriverError as exc:
        code = _text(getattr(exc, "error_code", None)) or "SURFACE_DRIVER_FAILED"
        raw, status, reasons = (
            _failure(action_id, code, bool(getattr(exc, "retryable", False))),
            "surface_driver_error",
            ("runtime_sole_driver_entry", "driver_failed"),
        )
    except Exception:  # noqa: BLE001 - 未知 Driver 异常不得穿透结果边界
        raw, status, reasons = (
            _failure(action_id, "APP_DRIVER_FAILED", True),
            "driver_exception",
            ("runtime_sole_driver_entry", "driver_exception"),
        )
    if not isinstance(raw, RawActionResult):
        raw, status, reasons = (
            _failure(action_id, "INVALID_DRIVER_RESULT", False),
            "invalid_runtime_result",
            ("runtime_sole_driver_entry", "invalid_driver_result"),
        )
    elif raw.action_execution_id != action_id:
        raw, status, reasons = (
            _failure(action_id, "ACTION_EXECUTION_MISMATCH", False),
            "invalid_runtime_result",
            ("runtime_sole_driver_entry", "action_execution_mismatch"),
        )
    elif status is None:
        status = "completed_runtime_driver" if raw.succeeded else "failed_runtime_driver"
        reasons = (
            "runtime_sole_driver_entry",
            "arrival_not_implied",
            "driver_succeeded" if raw.succeeded else "driver_failed",
        )
    return raw, _receipt(
        **common,
        status=status,
        reason_codes=reasons,
        driver_invoked=True,
        error_code=raw.error_code,
        retryable=raw.retryable,
        succeeded=raw.succeeded,
    )


__all__ = ["execute_app_driver_action_via_broker"]
