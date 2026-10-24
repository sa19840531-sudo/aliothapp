"""Surface 阶段机与页面识别结果之间的观测桥接。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.api.app.domain.exploration import (
    AUTH_GATE_PHASE_PROTOCOL_VERSION,
    POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
    STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION,
    AuthGateObservation,
    AuthGatePhaseContext,
    PopupChainObservation,
    PopupChainPhaseContext,
    StartupAdCountdownObservation,
    StartupAdCountdownPhaseContext,
    SurfacePhaseRunSnapshot,
    SystemDialogActionPolicy,
    SystemDialogKind,
    SystemDialogObservation,
    SystemDialogPhaseContext,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.protocol import require_mapping

SurfacePhaseBridgeContext = (
    StartupAdCountdownPhaseContext
    | PopupChainPhaseContext
    | AuthGatePhaseContext
    | SystemDialogPhaseContext
)
SurfacePhaseBridgeObservation = (
    StartupAdCountdownObservation
    | PopupChainObservation
    | AuthGateObservation
    | SystemDialogObservation
)

_PHASE_PROTOCOLS = {
    "startup_ad_countdown": STARTUP_AD_COUNTDOWN_PHASE_PROTOCOL_VERSION,
    "popup_chain": POPUP_CHAIN_PHASE_PROTOCOL_VERSION,
    "auth_gate": AUTH_GATE_PHASE_PROTOCOL_VERSION,
    "system_dialog": SYSTEM_DIALOG_PHASE_PROTOCOL_VERSION,
}
_STABLE_PAGE_STATES = frozenset({"stable", "normal", "loaded", "feed", "content_available"})
_POPUP_STATES = frozenset({"popup", "modal", "dialog", "overlay"})
_AUTH_STATES = frozenset(
    {
        "auth_gate",
        "auth_required",
        "authentication_required",
        "login",
        "login_blocked",
        "login_required",
        "login_wall",
        "logged_out",
        "requires_auth",
    }
)
_AUTH_FLAGS = frozenset({"authentication_required", "login_required"})
_SYSTEM_DIALOG_STATES = frozenset(
    {"system_dialog", "permission_dialog", "browser_permission"}
)


@dataclass(frozen=True, kw_only=True)
class SurfacePhaseObservationBridgeResult:
    """阶段机本轮上下文与观测。"""

    phase_key: str
    context: SurfacePhaseBridgeContext
    observation: SurfacePhaseBridgeObservation


def build_surface_phase_observation(
    *,
    run: SurfacePhaseRunSnapshot,
    page_detection_result: Mapping[str, Any],
    observed_at: datetime | None = None,
) -> SurfacePhaseObservationBridgeResult:
    """从当前阶段运行和页面识别结果构造 W17-W20 领域观测。"""
    _validate_run_protocol(run)
    capture_id = _required_text(page_detection_result.get("page_capture_id"), "page_capture_id")
    evidence_ids = tuple(_string_list(page_detection_result.get("evidence_artifact_ids")))
    page_type = _text(page_detection_result.get("page_type"))
    page_state = _text(page_detection_result.get("page_state"))
    detected_at = observed_at or datetime.now(UTC)
    context_payload = require_mapping(run.context_payload, "context_payload")

    if run.phase_key == "startup_ad_countdown":
        context = StartupAdCountdownPhaseContext(
            task_id=run.task_id,
            session_id=run.session_id,
            phase_run_id=run.phase_run_id,
            countdown_observation_count=_int(
                context_payload.get("countdown_observation_count")
            ),
            close_attempt_count=_int(context_payload.get("close_attempt_count")),
            max_countdown_observations=_positive_int(
                context_payload.get("max_countdown_observations"),
                default=10,
            ),
            max_close_attempts=_positive_int(
                context_payload.get("max_close_attempts"),
                default=3,
            ),
        )
        return SurfacePhaseObservationBridgeResult(
            phase_key=run.phase_key,
            context=context,
            observation=StartupAdCountdownObservation(
                task_id=run.task_id,
                session_id=run.session_id,
                source_capture_id=capture_id,
                surface_kind=run.surface_kind,
                observed_at=detected_at,
                page_type=page_type,
                page_state=page_state,
                ad_visible=_startup_ad_visible(page_type=page_type, page_state=page_state),
                countdown_remaining_ms=_optional_int(
                    page_detection_result.get("countdown_remaining_ms")
                ),
                close_control_visible=_bool(page_detection_result.get("close_control_visible")),
                close_control_enabled=_bool(page_detection_result.get("close_control_enabled")),
                close_semantic_target=_semantic_target(
                    page_detection_result,
                    explicit_key="close_semantic_target",
                ),
                stable_page_reached=_stable_page_reached(
                    page_type=page_type,
                    page_state=page_state,
                    obstructed=_startup_ad_visible(page_type=page_type, page_state=page_state),
                ),
                evidence_artifact_ids=evidence_ids,
            ),
        )

    if run.phase_key == "popup_chain":
        context = PopupChainPhaseContext(
            task_id=run.task_id,
            session_id=run.session_id,
            phase_run_id=run.phase_run_id,
            closed_popup_count=_int(context_payload.get("closed_popup_count")),
            reobserve_count=_int(context_payload.get("reobserve_count")),
            same_popup_attempt_count=_int(
                context_payload.get("same_popup_attempt_count")
            ),
            max_popup_layers=_positive_int(
                context_payload.get("max_popup_layers"),
                default=30,
            ),
            max_reobservations=_positive_int(
                context_payload.get("max_reobservations"),
                default=6,
            ),
            max_same_popup_attempts=_positive_int(
                context_payload.get("max_same_popup_attempts"),
                default=2,
            ),
        )
        popup_visible = _popup_visible(page_state=page_state, value=page_detection_result)
        top_popup_signature = _text(page_detection_result.get("top_popup_signature"))
        return SurfacePhaseObservationBridgeResult(
            phase_key=run.phase_key,
            context=context,
            observation=PopupChainObservation(
                task_id=run.task_id,
                session_id=run.session_id,
                source_capture_id=capture_id,
                surface_kind=run.surface_kind,
                observed_at=detected_at,
                page_type=page_type,
                page_state=page_state,
                popup_visible=popup_visible,
                top_popup_signature=top_popup_signature,
                close_control_visible=_bool(page_detection_result.get("close_control_visible")),
                close_control_enabled=_bool(page_detection_result.get("close_control_enabled")),
                close_semantic_target=_semantic_target(
                    page_detection_result,
                    explicit_key="close_semantic_target",
                ),
                stable_page_reached=_stable_page_reached(
                    page_type=page_type,
                    page_state=page_state,
                    obstructed=popup_visible or top_popup_signature is not None,
                ),
                evidence_artifact_ids=evidence_ids,
            ),
        )

    if run.phase_key == "auth_gate":
        context = AuthGatePhaseContext(
            task_id=run.task_id,
            session_id=run.session_id,
            phase_run_id=run.phase_run_id,
            dismiss_attempt_count=_int(context_payload.get("dismiss_attempt_count")),
            reobserve_count=_int(context_payload.get("reobserve_count")),
            max_dismiss_attempts=_positive_int(
                context_payload.get("max_dismiss_attempts"),
                default=2,
            ),
            max_reobservations=_positive_int(
                context_payload.get("max_reobservations"),
                default=4,
            ),
        )
        return SurfacePhaseObservationBridgeResult(
            phase_key=run.phase_key,
            context=context,
            observation=AuthGateObservation(
                task_id=run.task_id,
                session_id=run.session_id,
                source_capture_id=capture_id,
                surface_kind=run.surface_kind,
                observed_at=detected_at,
                page_type=page_type,
                page_state=page_state,
                auth_gate_visible=_auth_gate_visible(
                    page_state=page_state,
                    value=page_detection_result,
                ),
                login_form_visible=_bool(page_detection_result.get("login_form_visible")),
                requires_auth=_requires_auth(page_state=page_state, value=page_detection_result),
                dismiss_control_visible=_bool(
                    page_detection_result.get("dismiss_control_visible")
                ),
                dismiss_control_enabled=_bool(
                    page_detection_result.get("dismiss_control_enabled")
                ),
                dismiss_semantic_target=_semantic_target(
                    page_detection_result,
                    explicit_key="dismiss_semantic_target",
                ),
                guest_entry_visible=_bool(page_detection_result.get("guest_entry_visible")),
                guest_entry_enabled=_bool(page_detection_result.get("guest_entry_enabled")),
                guest_entry_semantic_target=_semantic_target(
                    page_detection_result,
                    explicit_key="guest_entry_semantic_target",
                ),
                stable_page_reached=_stable_page_reached(
                    page_type=page_type,
                    page_state=page_state,
                    obstructed=_auth_gate_visible(
                        page_state=page_state,
                        value=page_detection_result,
                    )
                    or _bool(page_detection_result.get("login_form_visible"))
                    or _requires_auth(page_state=page_state, value=page_detection_result),
                ),
                evidence_artifact_ids=evidence_ids,
            ),
        )

    if run.phase_key == "system_dialog":
        context = SystemDialogPhaseContext(
            task_id=run.task_id,
            session_id=run.session_id,
            phase_run_id=run.phase_run_id,
            handled_dialog_count=_int(context_payload.get("handled_dialog_count")),
            reobserve_count=_int(context_payload.get("reobserve_count")),
            max_system_dialogs=_positive_int(
                context_payload.get("max_system_dialogs"),
                default=6,
            ),
            max_reobservations=_positive_int(
                context_payload.get("max_reobservations"),
                default=4,
            ),
        )
        return SurfacePhaseObservationBridgeResult(
            phase_key=run.phase_key,
            context=context,
            observation=SystemDialogObservation(
                task_id=run.task_id,
                session_id=run.session_id,
                source_capture_id=capture_id,
                surface_kind=run.surface_kind,
                observed_at=detected_at,
                dialog_visible=_system_dialog_visible(
                    page_state=page_state,
                    value=page_detection_result,
                ),
                dialog_kind=_system_dialog_kind(page_detection_result.get("dialog_kind")),
                action_policy=_system_dialog_policy(
                    page_detection_result.get("action_policy")
                ),
                action_control_visible=_bool(
                    page_detection_result.get("action_control_visible")
                ),
                action_control_enabled=_bool(
                    page_detection_result.get("action_control_enabled")
                ),
                action_semantic_target=_semantic_target(
                    page_detection_result,
                    explicit_key="action_semantic_target",
                ),
                stable_page_reached=_stable_page_reached(
                    page_type=page_type,
                    page_state=page_state,
                    obstructed=_system_dialog_visible(
                        page_state=page_state,
                        value=page_detection_result,
                    ),
                ),
                page_state=page_state,
                evidence_artifact_ids=evidence_ids,
            ),
        )

    raise ValueError(f"不支持的 Surface 阶段：{run.phase_key}")


def _validate_run_protocol(run: SurfacePhaseRunSnapshot) -> None:
    expected_protocol = _PHASE_PROTOCOLS.get(run.phase_key)
    if expected_protocol is None:
        raise ValueError(f"不支持的 Surface 阶段：{run.phase_key}")
    if run.phase_protocol_version != expected_protocol:
        raise ValueError("Surface 阶段运行协议版本与阶段类型不一致")


def _stable_page_reached(
    *,
    page_type: str | None,
    page_state: str | None,
    obstructed: bool = False,
) -> bool:
    """业务页型 + 稳定态才算到达；有遮挡时一律不算稳定。"""

    if obstructed:
        return False
    if page_type is None:
        return False
    if page_type not in get_default_page_type_registry().coverage_required_codes:
        return False
    return _normalized(page_state) in _STABLE_PAGE_STATES


def _startup_ad_visible(*, page_type: str | None, page_state: str | None) -> bool:
    return page_type == "startup_ad" or _normalized(page_state) in {
        "countdown",
        "startup_ad",
    }


def _popup_visible(*, page_state: str | None, value: Mapping[str, Any]) -> bool:
    return _bool(value.get("popup_visible")) or _normalized(page_state) in _POPUP_STATES


def _auth_gate_visible(*, page_state: str | None, value: Mapping[str, Any]) -> bool:
    return _bool(value.get("auth_gate_visible")) or _normalized(page_state) in _AUTH_STATES


def _requires_auth(*, page_state: str | None, value: Mapping[str, Any]) -> bool:
    risk_flags = {_normalized(flag) for flag in _string_list(value.get("risk_flags"))}
    return (
        _bool(value.get("requires_auth"))
        or _normalized(page_state) in _AUTH_STATES
        or bool(risk_flags & _AUTH_FLAGS)
    )


def _system_dialog_visible(*, page_state: str | None, value: Mapping[str, Any]) -> bool:
    return _bool(value.get("dialog_visible")) or _normalized(page_state) in _SYSTEM_DIALOG_STATES


def _system_dialog_kind(value: Any) -> SystemDialogKind:
    normalized = _text(value) or SystemDialogKind.UNKNOWN.value
    try:
        return SystemDialogKind(normalized)
    except ValueError:
        return SystemDialogKind.UNKNOWN


def _system_dialog_policy(value: Any) -> SystemDialogActionPolicy:
    normalized = _text(value) or SystemDialogActionPolicy.MANUAL_REVIEW.value
    try:
        return SystemDialogActionPolicy(normalized)
    except ValueError:
        return SystemDialogActionPolicy.MANUAL_REVIEW


def _semantic_target(value: Mapping[str, Any], *, explicit_key: str) -> str | None:
    explicit = _text(value.get(explicit_key))
    if explicit is not None:
        return explicit
    for action in _mapping_list(value.get("candidate_actions")):
        action_type = _normalized(action.get("action"))
        if action_type not in {"click", "tap"}:
            continue
        target = _text(action.get("target"))
        if target is None:
            continue
        purpose = _normalized(action.get("purpose"))
        keywords = {_normalized(keyword) for keyword in _string_list(action.get("target_keywords"))}
        if "关闭" in target or "close" in target.casefold() or "close" in keywords:
            return target
        if "关闭" in purpose or "dismiss" in purpose or "close" in purpose:
            return target
    return None


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    items: list[str] = []
    for item in value:
        normalized = _text(item)
        if normalized is not None and normalized not in items:
            items.append(normalized)
    return items


def _required_text(value: Any, field_name: str) -> str:
    normalized = _text(value)
    if normalized is None:
        raise ValueError(f"{field_name} 不能为空")
    return normalized


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def _normalized(value: Any) -> str:
    text = _text(value)
    return text.casefold().replace("-", "_") if text is not None else ""


def _bool(value: Any) -> bool:
    return value is True


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _positive_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    return default


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None
