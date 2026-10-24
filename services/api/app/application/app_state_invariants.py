"""从 App Capture / 视觉事实构造 StateInvariantSnapshot。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.domain.inspection import StateInvariantSnapshot
from services.api.app.domain.interaction import SurfaceKind


def build_app_state_invariant_snapshot(
    *,
    task_id: str,
    capture_id: str,
    session_id: str,
    surface_kind: SurfaceKind,
    package_name: str | None = None,
    activity: str | None = None,
    page_type: str | None = None,
    page_state: str | None = None,
    image_signature: str | None = None,
    device_ref: str | None = None,
    platform_build_ref: str | None = None,
    account_environment_ref: str | None = None,
    session_options: Mapping[str, Any] | None = None,
    captured_at: datetime | None = None,
) -> StateInvariantSnapshot:
    """把 App 业务会话身份写入等价格局，避免只认设备。"""
    options = dict(session_options or {})
    lifecycle = options.get("lifecycle_phase")
    if not lifecycle:
        lifecycle = "foreground"
    environment = {
        "surface_kind": surface_kind.value,
        "device_ref": device_ref,
        "platform_build_ref": platform_build_ref,
        "account_environment_ref": account_environment_ref
        or options.get("account_environment_ref"),
        "language": options.get("language"),
        "region": options.get("region"),
        "experiment": options.get("experiment"),
        "process_identity": options.get("process_identity"),
        "app_data_container_ref": options.get("app_data_container_ref"),
        "deep_link": options.get("deep_link"),
        "lifecycle_phase": lifecycle,
        "session_event": options.get("session_event"),
        "process_alive": options.get("process_alive"),
    }
    business = {
        "package_name": package_name,
        "activity": activity,
        "route": f"app://{package_name or 'unknown'}/{activity or 'unknown'}",
        "page_type": page_type,
    }
    resolved_page_state = page_state
    if not resolved_page_state and options.get("page_state_hint"):
        resolved_page_state = str(options.get("page_state_hint"))
    semantic = {
        "page_state": resolved_page_state or "stable",
        "overlay": options.get("overlay_signature") or "none",
        "session_event": options.get("session_event"),
    }
    structure = {
        "image_signature": image_signature,
        "state_version": image_signature,
        "has_ui_tree": bool(options.get("has_ui_tree")),
        "screen_id": options.get("screen_id"),
        "structure_signature": options.get("structure_signature"),
        "safe_area": options.get("safe_area"),
    }
    return StateInvariantSnapshot(
        task_id=task_id,
        capture_id=capture_id,
        session_id=session_id,
        surface_kind=surface_kind,
        environment={k: v for k, v in environment.items() if v is not None},
        business={k: v for k, v in business.items() if v is not None},
        semantic={k: v for k, v in semantic.items() if v is not None},
        structure={k: v for k, v in structure.items() if v is not None},
        captured_at=captured_at or datetime.now(UTC),
    )


def derive_app_page_state(
    *,
    visual_page_type: str | None,
    visual_summary: str,
    key_elements: tuple[str, ...],
    candidate_count: int,
) -> str:
    """由视觉事实推导有界 page_state，避免一律写死 stable。"""
    summary = (visual_summary or "").casefold()
    page_type = (visual_page_type or "").casefold()
    joined = " ".join(key_elements).casefold()
    blob = f"{summary} {page_type} {joined}"
    if any(token in blob for token in ("loading", "加载", "splash", "启动")):
        return "loading"
    if any(token in blob for token in ("permission", "权限", "允许", "拒绝")):
        return "system_dialog"
    if any(token in blob for token in ("popup", "弹窗", "广告", "ad_", "关闭广告")):
        return "popup"
    if candidate_count == 0 and page_type in {"", "unknown", "none"}:
        return "needs_more_observation"
    return "stable"
