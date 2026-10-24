"""App 登录 detect-only 与 18+ 入口前置协议对齐。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

APP_LOGIN_DETECT_ONLY_VERSION = "app-login-detect-only.v1"
APP_AGE_GATE_PROTOCOL_VERSION = "app-age-gate-protocol.v1"

_LOGIN_STATES = frozenset(
    {"auth_gate", "auth_required", "login_required", "requires_auth"}
)
_FORBIDDEN_AUTO_ACTIONS = frozenset(
    {
        "type_password",
        "type_username",
        "submit_login",
        "oauth_authorize",
        "fill_otp",
        "fill_captcha",
    }
)


def classify_login_surface(
    *,
    page_state: object = None,
    surface_facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    state = str(page_state or "").strip().casefold()
    visible = facts.get("auth_gate_visible") is True or state in _LOGIN_STATES
    return {
        "protocol_version": APP_LOGIN_DETECT_ONLY_VERSION,
        "login_detected": visible,
        "page_state": state or None,
        "mode": "detect_only",
        "auto_credential_entry_forbidden": True,
        "forbidden_actions": tuple(sorted(_FORBIDDEN_AUTO_ACTIONS)),
    }


def login_action_allowed(
    action_name: object,
    *,
    task_config: Mapping[str, Any] | None = None,
) -> bool:
    text = str(action_name or "").strip().casefold()
    if text not in _FORBIDDEN_AUTO_ACTIONS:
        return True
    from services.api.app.application.app_schedule_gap_closure import (
        credentials_configured,
    )

    return credentials_configured(task_config)


def build_app_age_gate_protocol_snapshot(
    *,
    adult_declaration_frozen: bool,
    unique_enter_control: bool,
    confidence_ok: bool,
    requires_identity_or_payment: bool,
) -> dict[str, Any]:
    auto_enter = (
        adult_declaration_frozen
        and unique_enter_control
        and confidence_ok
        and not requires_identity_or_payment
    )
    return {
        "protocol_version": APP_AGE_GATE_PROTOCOL_VERSION,
        "adult_declaration_frozen": bool(adult_declaration_frozen),
        "unique_enter_control": bool(unique_enter_control),
        "confidence_ok": bool(confidence_ok),
        "requires_identity_or_payment": bool(requires_identity_or_payment),
        "auto_enter_allowed": auto_enter,
        "must_re_ready_and_reclassify_after_enter": True,
        "home_judgment_blocked_while_gate_present": True,
    }
