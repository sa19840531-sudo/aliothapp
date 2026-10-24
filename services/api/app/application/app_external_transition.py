"""App ExternalTransition：外跳事实入图，不进入业务 Coverage。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.application.app_package_boundary import (
    ExternalActivityKind,
    classify_external_activity,
)

APP_EXTERNAL_TRANSITION_VERSION = "app-external-transition.v1"


def build_external_transition(
    *,
    source_capture_id: object,
    source_package: object,
    source_activity: object | None = None,
    destination_package: object,
    destination_activity: object | None = None,
    trigger_action_id: object = None,
    reason: object = None,
    recoverable: bool = True,
) -> dict[str, Any]:
    dest_pkg = str(destination_package or "").strip()
    kind = classify_external_activity(
        package_name=dest_pkg,
        activity=str(destination_activity or "") or None,
    )
    return {
        "protocol_version": APP_EXTERNAL_TRANSITION_VERSION,
        "entity": "ExternalTransition",
        "source_capture_id": str(source_capture_id or "").strip() or None,
        "source_package": str(source_package or "").strip() or None,
        "source_activity": str(source_activity or "").strip() or None,
        "destination_package": dest_pkg or None,
        "destination_activity": str(destination_activity or "").strip() or None,
        "external_activity_kind": kind.value,
        "trigger_action_id": str(trigger_action_id or "").strip() or None,
        "reason": str(reason or "").strip() or None,
        "recoverable": bool(recoverable),
        "counts_as_business_arrival": False,
        "enters_business_coverage": False,
        "hard_blocked": kind
        in {
            ExternalActivityKind.STORE,
            ExternalActivityKind.PAYMENT,
        },
    }


def project_external_transitions_for_shadow(
    transitions: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """供 page_graph shadow 挂载的只读外跳列表。"""

    out: list[dict[str, Any]] = []
    for item in transitions or ():
        if not isinstance(item, Mapping):
            continue
        if item.get("entity") != "ExternalTransition":
            continue
        if item.get("protocol_version") != APP_EXTERNAL_TRANSITION_VERSION:
            continue
        out.append(dict(item))
    return out


def external_transition_from_package_hop(
    *,
    source_capture_id: str,
    before_package: str | None,
    before_activity: str | None,
    after_package: str | None,
    after_activity: str | None,
    allowed_hosts: Sequence[str],
    trigger_action_id: str | None = None,
) -> dict[str, Any] | None:
    """前后包不在 allowlist 内时生成 ExternalTransition。"""

    allowed = {h.casefold() for h in allowed_hosts if h and str(h).strip()}
    after_pkg = str(after_package or "").strip().casefold()
    if not after_pkg:
        return None
    if after_pkg in allowed:
        return None
    return build_external_transition(
        source_capture_id=source_capture_id,
        source_package=before_package,
        source_activity=before_activity,
        destination_package=after_package,
        destination_activity=after_activity,
        trigger_action_id=trigger_action_id,
        reason="foreground_left_target_package",
    )
