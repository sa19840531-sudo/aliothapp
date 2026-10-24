"""App Capture 证据清单字段：每份采集落 session_auth／is_obstructed。

星语配套字段要求：capture_role、page_identity 等之外，还必须带
session_auth（guest／authenticated／unknown）与 is_obstructed，供 Judge／
登录闸／报表直接过滤。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SESSION_AUTH_GUEST = "guest"
SESSION_AUTH_AUTHENTICATED = "authenticated"
SESSION_AUTH_UNKNOWN = "unknown"
_VALID_SESSION_AUTH = frozenset(
    {SESSION_AUTH_GUEST, SESSION_AUTH_AUTHENTICATED, SESSION_AUTH_UNKNOWN}
)


def normalize_session_auth(raw: object | None) -> str:
    text = str(raw or "").strip().casefold()
    if text in _VALID_SESSION_AUTH:
        return text
    if text in {"logged_in", "login", "authed", "user"}:
        return SESSION_AUTH_AUTHENTICATED
    if text in {"anonymous", "visitor", "游客", "logged_out"}:
        return SESSION_AUTH_GUEST
    return SESSION_AUTH_UNKNOWN


def resolve_session_auth_from_facts(
    *,
    surface_facts: Mapping[str, Any] | None = None,
    task_config: Mapping[str, Any] | None = None,
    coverage_metadata: Mapping[str, Any] | None = None,
) -> str:
    """从明确登录事实解析状态；无账号和年龄门都不能证明已退出登录。"""

    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    observed_states: set[str] = set()
    explicit = normalize_session_auth(facts.get("session_auth"))
    if explicit != SESSION_AUTH_UNKNOWN:
        observed_states.add(explicit)
    for key in ("authenticated", "logged_in"):
        if facts.get(key) is True:
            observed_states.add(SESSION_AUTH_AUTHENTICATED)
        elif facts.get(key) is False:
            observed_states.add(SESSION_AUTH_GUEST)
    if (
        facts.get("login_form_visible") is True
        or facts.get("login_required") is True
        or (
            facts.get("auth_gate_visible") is True
            and facts.get("requires_auth") is True
        )
    ):
        observed_states.add(SESSION_AUTH_GUEST)
    if observed_states:
        return next(iter(observed_states)) if len(observed_states) == 1 else SESSION_AUTH_UNKNOWN

    for bag in (coverage_metadata, task_config):
        if not isinstance(bag, Mapping):
            continue
        for key in ("session_auth", "app_session_auth", "auth_state"):
            if key in bag:
                return normalize_session_auth(bag.get(key))
        product = bag.get("product") if isinstance(bag.get("product"), Mapping) else {}
        if isinstance(product, Mapping) and "session_auth" in product:
            return normalize_session_auth(product.get("session_auth"))
    return SESSION_AUTH_UNKNOWN


def resolve_is_obstructed_from_analysis(analysis: object | None) -> bool:
    """与运行时 is_obstructed_surface 对齐，写入采集 metadata。"""

    if analysis is None:
        return False
    from services.api.app.application.app_obstruction_controls import (
        is_obstructed_surface,
    )

    page_state = getattr(analysis, "page_state", None)
    page_type = getattr(analysis, "page_type", None) or getattr(
        analysis, "visual_page_type", None
    )
    overlay_signature = getattr(analysis, "overlay_signature", None)
    surface_facts = getattr(analysis, "surface_facts", None)
    return bool(
        is_obstructed_surface(
            page_state=str(page_state) if page_state is not None else None,
            page_type=str(page_type) if page_type is not None else None,
            overlay_signature=(
                str(overlay_signature) if overlay_signature is not None else None
            ),
            surface_facts=surface_facts if isinstance(surface_facts, Mapping) else None,
        )
    )


def enrich_capture_evidence_fields(
    metadata: Mapping[str, Any] | None,
    *,
    analysis: object | None = None,
    surface_facts: Mapping[str, Any] | None = None,
    task_config: Mapping[str, Any] | None = None,
    coverage_metadata: Mapping[str, Any] | None = None,
    requirement_role: str | None = None,
) -> dict[str, Any]:
    """合并证据清单字段；不覆盖调用方已显式写入的同名键。"""

    out = dict(metadata) if isinstance(metadata, Mapping) else {}
    if "session_auth" not in out:
        out["session_auth"] = resolve_session_auth_from_facts(
            surface_facts=surface_facts
            or (
                getattr(analysis, "surface_facts", None)
                if analysis is not None
                else None
            ),
            task_config=task_config,
            coverage_metadata=coverage_metadata,
        )
    if "is_obstructed" not in out:
        if analysis is not None:
            out["is_obstructed"] = resolve_is_obstructed_from_analysis(analysis)
        elif isinstance(surface_facts, Mapping):
            from services.api.app.application.app_obstruction_controls import (
                is_obstructed_surface,
            )

            out["is_obstructed"] = bool(
                is_obstructed_surface(
                    page_state=None,
                    page_type=None,
                    overlay_signature=None,
                    surface_facts=surface_facts,
                )
            )
        else:
            out["is_obstructed"] = False
    role = str(requirement_role or "").strip()
    if role and "requirement_role" not in out:
        out["requirement_role"] = role
    if role and "capture_role" not in out:
        out["capture_role"] = role
    page_state = getattr(analysis, "page_state", None) if analysis is not None else None
    page_type = None
    if analysis is not None:
        page_type = getattr(analysis, "page_type", None) or getattr(
            analysis, "visual_page_type", None
        )
    if page_state and "page_state" not in out:
        out["page_state"] = str(page_state)
    if page_type and "page_type" not in out:
        out["page_type"] = str(page_type)
    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    if not facts and analysis is not None:
        raw_facts = getattr(analysis, "surface_facts", None)
        if isinstance(raw_facts, Mapping):
            facts = raw_facts
    page_url = facts.get("page_url") or facts.get("url")
    if page_url and "page_url" not in out:
        out["page_url"] = str(page_url)[:500]
    outbound = facts.get("outbound_target") or facts.get("outbound_landing")
    if outbound and "outbound_target" not in out:
        out["outbound_target"] = str(outbound)[:500]
    if "evidence_role" not in out:
        from services.api.app.application.app_schedule_gap_closure import (
            evidence_role_for_surface,
        )

        out["evidence_role"] = evidence_role_for_surface(
            page_type=str(page_type) if page_type else out.get("page_type"),
            page_state=str(page_state) if page_state else out.get("page_state"),
            surface_facts=facts,
        )
    width = out.get("width")
    height = out.get("height")
    display = out.get("device_snapshot")
    if (
        "fullscreen" not in out
        and isinstance(width, int)
        and isinstance(height, int)
        and isinstance(display, Mapping)
    ):
        display_width = display.get("width")
        display_height = display.get("height")
        if isinstance(display_width, int) and isinstance(display_height, int):
            out["fullscreen"] = width == display_width and height == display_height
    return out
