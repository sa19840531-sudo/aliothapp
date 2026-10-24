"""App 业务会话事件：权限框 / 进程重建 / 前后台 → 一等不变量字段。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_PERMISSION_PACKAGES = (
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.packageinstaller",
    "com.google.android.packageinstaller",
    "com.samsung.android.permissioncontroller",
)
_PERMISSION_UI_TOKENS = (
    "permission",
    "权限",
    "允許",
    "允许",
    "拒絕",
    "拒绝",
    "allow",
    "deny",
    "while using the app",
    "仅在使用该应用时",
)
_PROCESS_DEATH_PATTERNS = (
    re.compile(r"Process\s+\S+\s+\(.*\)\s+has\s+died", re.I),
    re.compile(r"Force\s+finishing\s+activity", re.I),
    re.compile(r"Fatal\s+signal", re.I),
    re.compile(r"FATAL\s+EXCEPTION", re.I),
    re.compile(r"ANR\s+in\s+", re.I),
)
_PROCESS_REBUILD_PATTERNS = (
    re.compile(r"Start\s+proc\s+", re.I),
    re.compile(r"Process\s+\S+\s+\(.*\)\s+created\s+for", re.I),
)
_FOCUS_COMPONENT_RE = re.compile(
    r"(?:u\d+)\s+([\w.$]+)/([\w.$]+)|([\w.$]+)/([\w.$]+)"
)


@dataclass(frozen=True, kw_only=True)
class AppSessionEventObservation:
    """有界会话事件观测，供不变量与等价格局使用。"""

    lifecycle_phase: str
    session_event: str | None
    page_state_hint: str | None
    package_name: str | None
    activity: str | None
    process_alive: bool | None
    evidence: Mapping[str, Any]

    def as_session_options(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "lifecycle_phase": self.lifecycle_phase,
            "process_alive": self.process_alive,
        }
        if self.session_event:
            payload["session_event"] = self.session_event
        if self.page_state_hint:
            payload["page_state_hint"] = self.page_state_hint
        if self.package_name:
            payload["observed_package_name"] = self.package_name
        if self.activity:
            payload["observed_activity"] = self.activity
        payload["session_event_evidence"] = dict(self.evidence)
        return payload


def detect_app_session_events(
    *,
    expected_package: str | None = None,
    focus_evidence_lines: Sequence[str] | None = None,
    ui_hierarchy_xml: str | None = None,
    logcat_text: str | None = None,
    previous_lifecycle_phase: str | None = None,
) -> AppSessionEventObservation:
    """从 dumpsys / hierarchy / logcat 推导一等会话事件。"""
    expected = (expected_package or "").strip() or None
    focus_lines = [str(item).strip() for item in (focus_evidence_lines or ()) if str(item).strip()]
    focus_pkg, focus_act = parse_focus_component(focus_lines)
    ui_blob = (ui_hierarchy_xml or "")[:50_000]
    log_blob = (logcat_text or "")[-12_000:]

    permission = _is_permission_dialog(
        focus_package=focus_pkg,
        ui_hierarchy_xml=ui_blob,
    )
    death = _log_matches(log_blob, _PROCESS_DEATH_PATTERNS, package=expected)
    rebuild = _log_matches(log_blob, _PROCESS_REBUILD_PATTERNS, package=expected)

    lifecycle = "unknown"
    if focus_pkg:
        if expected and expected == focus_pkg:
            lifecycle = "foreground"
        elif focus_pkg in _PERMISSION_PACKAGES or permission:
            lifecycle = "foreground"
        elif expected and expected != focus_pkg:
            lifecycle = "background"
        else:
            lifecycle = "foreground"
    elif expected and death and not rebuild:
        lifecycle = "background"

    session_event: str | None = None
    page_hint: str | None = None
    if permission:
        session_event = "permission_dialog"
        page_hint = "system_dialog"
    elif death and rebuild:
        session_event = "process_rebuild"
    elif death:
        session_event = "process_death"
        lifecycle = "background"
    elif (
        previous_lifecycle_phase
        and previous_lifecycle_phase != lifecycle
        and lifecycle in {"foreground", "background"}
        and previous_lifecycle_phase in {"foreground", "background"}
    ):
        session_event = "fg_bg_transition"

    process_alive: bool | None = None
    if death and not rebuild:
        process_alive = False
    elif rebuild or lifecycle == "foreground":
        process_alive = True

    return AppSessionEventObservation(
        lifecycle_phase=lifecycle,
        session_event=session_event,
        page_state_hint=page_hint,
        package_name=focus_pkg or expected,
        activity=focus_act,
        process_alive=process_alive,
        evidence={
            "focus_lines": focus_lines[:8],
            "permission_dialog": permission,
            "process_death_signal": death,
            "process_rebuild_signal": rebuild,
            "previous_lifecycle_phase": previous_lifecycle_phase,
        },
    )


def parse_focus_component(
    focus_evidence_lines: Sequence[str],
) -> tuple[str | None, str | None]:
    """从前景证据行解析 package / activity。"""
    for line in focus_evidence_lines:
        match = _FOCUS_COMPONENT_RE.search(line)
        if not match:
            continue
        pkg = match.group(1) or match.group(3)
        act = match.group(2) or match.group(4)
        if pkg and pkg not in {"null", "None"}:
            return pkg.strip(), (act.strip() if act else None)
    return None, None


def build_display_safe_area(*, width: int, height: int) -> dict[str, Any]:
    """由显示边界构造有界 safe_area（无 insets 时退回全屏）。"""
    w = max(0, int(width or 0))
    h = max(0, int(height or 0))
    return {
        "top": 0,
        "right": 0,
        "bottom": 0,
        "left": 0,
        "width": w,
        "height": h,
        "source": "display_bounds",
    }


def _is_permission_dialog(*, focus_package: str | None, ui_hierarchy_xml: str) -> bool:
    if focus_package and focus_package in _PERMISSION_PACKAGES:
        return True
    blob = ui_hierarchy_xml.casefold()
    if not blob:
        return False
    if any(pkg in blob for pkg in _PERMISSION_PACKAGES):
        return True
    return any(token.casefold() in blob for token in _PERMISSION_UI_TOKENS)


def _log_matches(
    text: str,
    patterns: Sequence[re.Pattern[str]],
    *,
    package: str | None,
) -> bool:
    if not text:
        return False
    lines = text.splitlines()
    scoped = lines
    if package:
        filtered = [line for line in lines if package in line]
        if filtered:
            scoped = filtered
    joined = "\n".join(scoped[-200:])
    return any(pattern.search(joined) for pattern in patterns)
