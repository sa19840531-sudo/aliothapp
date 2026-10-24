"""AppNavigationOccurrence：统一候选、分端执行的 App 导航来源 occurrence。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

APP_NAVIGATION_OCCURRENCE_VERSION = "app-navigation-occurrence.v1"


def build_app_navigation_occurrence(
    *,
    occurrence_id: object,
    logical_target_id: object,
    label: object,
    source_capture_id: object,
    package_name: object,
    activity: object | None = None,
    window: object | None = None,
    discovery_source: object = None,
    capture_epoch: object = None,
) -> dict[str, Any]:
    return {
        "protocol_version": APP_NAVIGATION_OCCURRENCE_VERSION,
        "entity": "AppNavigationOccurrence",
        "occurrence_id": str(occurrence_id or "").strip() or None,
        "logical_target_id": str(logical_target_id or "").strip() or None,
        "label": str(label or "").strip() or None,
        "source_capture_id": str(source_capture_id or "").strip() or None,
        "package_name": str(package_name or "").strip() or None,
        "activity": str(activity or "").strip() or None,
        "window": str(window or "").strip() or None,
        "discovery_source": str(discovery_source or "").strip() or None,
        "capture_epoch": str(capture_epoch or "").strip() or None,
        "dedupe_merges_logical_target_only": True,
    }


def merge_occurrences_keep_each(
    occurrences: Sequence[Mapping[str, Any]] | None,
) -> dict[str, list[dict[str, Any]]]:
    """按 logical_target_id 分组，但保留每个 occurrence。"""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in occurrences or ():
        if not isinstance(item, Mapping):
            continue
        if item.get("entity") != "AppNavigationOccurrence":
            continue
        key = str(item.get("logical_target_id") or item.get("label") or "").strip() or "_unknown"
        grouped.setdefault(key, []).append(dict(item))
    return grouped
