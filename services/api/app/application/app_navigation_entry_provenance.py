"""App 导航入口发现 provenance 合同。

每个可执行导航入口必须绑定发现当帧 ``source_capture_id``，并标注
``discovery_source``。禁止跨任务／跨包固定坐标当长期业务入口。

Android App 路径不消费 H5 Flutter ``flt-semantics``；结构优先仍靠 UIA／视觉。
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

APP_NAVIGATION_ENTRY_PROVENANCE_VERSION = "app-navigation-entry-provenance.v1"

DISCOVERY_SOURCE_VISION = "vision"
DISCOVERY_SOURCE_UIA_ALIGNED = "uia_aligned"
DISCOVERY_SOURCE_UIA_ISSUED = "uia_issued"
DISCOVERY_SOURCE_PROMOTED_COVERAGE = "promoted_coverage"
DISCOVERY_SOURCE_PROMOTED_BOTTOM_NAV = "promoted_bottom_nav"
DISCOVERY_SOURCE_PROMOTED_HOME_GRID = "promoted_home_grid"
DISCOVERY_SOURCE_PROMOTED_HOME_TOP_TAB = "promoted_home_top_tab"
DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC = "clearance_synthetic"

APP_NAVIGATION_DISCOVERY_SOURCES: frozenset[str] = frozenset(
    {
        DISCOVERY_SOURCE_VISION,
        DISCOVERY_SOURCE_UIA_ALIGNED,
        DISCOVERY_SOURCE_UIA_ISSUED,
        DISCOVERY_SOURCE_PROMOTED_COVERAGE,
        DISCOVERY_SOURCE_PROMOTED_BOTTOM_NAV,
        DISCOVERY_SOURCE_PROMOTED_HOME_GRID,
        DISCOVERY_SOURCE_PROMOTED_HOME_TOP_TAB,
        DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
    }
)

# 可进入业务导航／覆盖提升队列的来源（清障合成除外）。
APP_NAVIGATION_BUSINESS_DISCOVERY_SOURCES: frozenset[str] = frozenset(
    APP_NAVIGATION_DISCOVERY_SOURCES - {DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC}
)

__all__ = [
    "APP_NAVIGATION_BUSINESS_DISCOVERY_SOURCES",
    "APP_NAVIGATION_DISCOVERY_SOURCES",
    "APP_NAVIGATION_ENTRY_PROVENANCE_VERSION",
    "DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC",
    "DISCOVERY_SOURCE_PROMOTED_BOTTOM_NAV",
    "DISCOVERY_SOURCE_PROMOTED_COVERAGE",
    "DISCOVERY_SOURCE_PROMOTED_HOME_GRID",
    "DISCOVERY_SOURCE_PROMOTED_HOME_TOP_TAB",
    "DISCOVERY_SOURCE_UIA_ALIGNED",
    "DISCOVERY_SOURCE_UIA_ISSUED",
    "DISCOVERY_SOURCE_VISION",
    "is_business_navigation_entry",
    "is_clearance_only_discovery_source",
    "normalize_discovery_source",
    "tag_candidate_action_provenance",
    "tag_navigation_entry_provenance",
    "with_visual_candidate_discovery_source",
]


def normalize_discovery_source(raw: object) -> str | None:
    """归一 discovery_source；非法值返回 None。"""

    if raw is None:
        return None
    text = str(raw).strip().casefold()
    if not text:
        return None
    if text not in APP_NAVIGATION_DISCOVERY_SOURCES:
        return None
    return text


def is_clearance_only_discovery_source(raw: object) -> bool:
    return normalize_discovery_source(raw) == DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC


def is_business_navigation_entry(
    *,
    source_capture_id: object,
    discovery_source: object,
    allow_missing_source_for_clearance: bool = False,
) -> bool:
    """业务导航入口门禁：必须有来源 Capture，且来源属于业务发现集合。"""

    capture_id = str(source_capture_id or "").strip()
    source = normalize_discovery_source(discovery_source)
    if source == DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC:
        if allow_missing_source_for_clearance:
            return False
        return False
    if not capture_id or source is None:
        return False
    return source in APP_NAVIGATION_BUSINESS_DISCOVERY_SOURCES


def tag_navigation_entry_provenance(
    *,
    discovery_source: str,
    source_capture_id: str | None,
    screenshot_artifact_id: str | None = None,
    long_term_fixed_coordinates_forbidden: bool = True,
    requires_fresh_capture_after_action: bool = False,
) -> dict[str, Any]:
    """生成可并入 CandidateAction.metadata／surface_facts 的 provenance 块。"""

    source = normalize_discovery_source(discovery_source)
    if source is None:
        raise ValueError(f"unsupported_discovery_source={discovery_source!r}")
    capture_id = str(source_capture_id or "").strip() or None
    artifact_id = str(screenshot_artifact_id or "").strip() or None
    payload: dict[str, Any] = {
        "protocol_version": APP_NAVIGATION_ENTRY_PROVENANCE_VERSION,
        "discovery_source": source,
        "source_capture_id": capture_id,
        "long_term_fixed_coordinates_forbidden": bool(
            long_term_fixed_coordinates_forbidden
        ),
        "business_navigation_entry": is_business_navigation_entry(
            source_capture_id=capture_id,
            discovery_source=source,
        ),
        "clearance_only": source == DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
    }
    if artifact_id is not None:
        payload["screenshot_artifact_id"] = artifact_id
    if requires_fresh_capture_after_action or source == DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC:
        payload["requires_fresh_capture_after_action"] = True
        payload["cross_capture_coordinate_replay_forbidden"] = True
    return payload


def tag_candidate_action_provenance(
    metadata: MutableMapping[str, Any] | Mapping[str, Any] | None,
    *,
    discovery_source: str,
    source_capture_id: str | None = None,
    screenshot_artifact_id: str | None = None,
    requires_fresh_capture_after_action: bool = False,
) -> dict[str, Any]:
    """合并 provenance 到候选 metadata；保留既有键。"""

    base = dict(metadata) if isinstance(metadata, Mapping) else {}
    capture = (
        str(source_capture_id).strip()
        if source_capture_id is not None and str(source_capture_id).strip()
        else str(base.get("source_capture_id") or "").strip() or None
    )
    provenance = tag_navigation_entry_provenance(
        discovery_source=discovery_source,
        source_capture_id=capture,
        screenshot_artifact_id=screenshot_artifact_id
        or (
            str(base.get("screenshot_artifact_id")).strip()
            if isinstance(base.get("screenshot_artifact_id"), str)
            else None
        ),
        requires_fresh_capture_after_action=requires_fresh_capture_after_action,
    )
    base.update(provenance)
    # 顶层快捷字段，便于调度／门禁读取。
    base["discovery_source"] = provenance["discovery_source"]
    if capture is not None:
        base["source_capture_id"] = capture
    return base


def with_visual_candidate_discovery_source(
    candidate: Any,
    discovery_source: str,
) -> Any:
    """给 AppVisualCandidate 打 discovery_source；不改变 candidate_id。"""

    from dataclasses import replace

    source = normalize_discovery_source(discovery_source)
    if source is None:
        raise ValueError(f"unsupported_discovery_source={discovery_source!r}")
    current = getattr(candidate, "discovery_source", None)
    if current == source:
        return candidate
    return replace(candidate, discovery_source=source)
