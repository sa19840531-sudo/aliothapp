"""App 导航执行器合同：冻结设备／UI Tree epoch，并结算 after 证据四件套。

对齐统一设计报告：
- 候选必须绑定 Package／Activity／Window／方向（及可选 density／insets）与当前 Capture epoch；
- 真实 Tap／Click 导航的 after 必须具备 Screenshot + UI Tree + Package/Activity/Window + 导航态；
- Deep Link 不走本执行器的「控件点击通过」结算（见 app_deep_link_contract）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

APP_NAVIGATION_EXECUTOR_CONTRACT_VERSION = "app-navigation-executor.v1"

REQUIRED_AFTER_GAPS = frozenset(
    {
        "screenshot_missing",
        "ui_tree_missing",
        "package_missing",
        "activity_missing",
        "window_missing",
        "navigation_state_missing",
        "epoch_missing",
        "epoch_mismatch",
    }
)

_NAV_OPS = frozenset({"tap", "click", "navigate"})


def is_native_navigation_operation(action_type: object) -> bool:
    text = str(getattr(action_type, "value", action_type) or "").strip().casefold()
    return text in _NAV_OPS


def extract_window_label(surface_facts: Mapping[str, Any] | None) -> str | None:
    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    for key in ("current_window", "window", "window_title", "window_id"):
        value = facts.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            for inner in ("id", "title", "name", "token"):
                raw = value.get(inner)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
    return None


def extract_ui_tree_presence(
    *,
    surface_facts: Mapping[str, Any] | None,
    artifact_ids: Sequence[str] | None = None,
    ui_tree_artifact_id: object = None,
    has_ui_tree: object = None,
) -> bool:
    if has_ui_tree is True:
        return True
    if isinstance(ui_tree_artifact_id, str) and ui_tree_artifact_id.strip():
        return True
    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    if facts.get("has_ui_tree") is True:
        return True
    for key in ("ui_tree_artifact_id", "hierarchy_artifact_id"):
        value = facts.get(key)
        if isinstance(value, str) and value.strip():
            return True
    del artifact_ids
    return False


def bind_navigation_candidate_epoch(
    *,
    capture_epoch: object,
    package_name: object,
    activity: object,
    orientation: object,
    window: object = None,
    density: object = None,
    insets: object = None,
    source_capture_id: object = None,
) -> dict[str, Any]:
    epoch = str(capture_epoch or "").strip() or None
    pkg = str(package_name or "").strip() or None
    act = str(activity or "").strip() or None
    orient = str(orientation or "").strip() or None
    win = str(window or "").strip() or None
    dens = str(density or "").strip() or None
    inset_text = str(insets or "").strip() or None
    source = str(source_capture_id or "").strip() or None
    gaps: list[str] = []
    if not epoch:
        gaps.append("epoch_missing")
    if not pkg:
        gaps.append("package_missing")
    if not act:
        gaps.append("activity_missing")
    if not orient:
        gaps.append("navigation_state_missing")
    if not win:
        gaps.append("window_missing")
    return {
        "protocol_version": APP_NAVIGATION_EXECUTOR_CONTRACT_VERSION,
        "capture_epoch": epoch,
        "source_capture_id": source,
        "package_name": pkg,
        "activity": act,
        "window": win,
        "orientation": orient,
        "density": dens,
        "insets": inset_text,
        "bind_gaps": tuple(gaps),
        "bind_complete": not gaps,
    }


def settle_navigation_after_evidence(
    *,
    screenshot_artifact_id: object,
    package_name: object,
    activity: object,
    orientation: object,
    page_state: object = None,
    surface_facts: Mapping[str, Any] | None = None,
    bound_epoch: object = None,
    after_epoch: object = None,
    ui_tree_present: bool | None = None,
    artifact_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    gaps: list[str] = []
    shot = str(screenshot_artifact_id or "").strip()
    pkg = str(package_name or "").strip()
    act = str(activity or "").strip()
    orient = str(orientation or "").strip()
    state = str(page_state or "").strip()
    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    window = extract_window_label(facts)
    has_tree = (
        bool(ui_tree_present)
        if ui_tree_present is not None
        else extract_ui_tree_presence(surface_facts=facts, artifact_ids=artifact_ids)
    )
    bound = str(bound_epoch or "").strip()
    after = str(after_epoch or "").strip()

    if not shot:
        gaps.append("screenshot_missing")
    if not has_tree:
        gaps.append("ui_tree_missing")
    if not pkg:
        gaps.append("package_missing")
    if not act:
        gaps.append("activity_missing")
    if not window:
        gaps.append("window_missing")
    if not orient and not state:
        gaps.append("navigation_state_missing")
    if not bound:
        gaps.append("epoch_missing")
    elif after and bound != after:
        gaps.append("epoch_mismatch")
    elif not after:
        gaps.append("epoch_missing")

    unique_gaps = tuple(dict.fromkeys(gaps))
    return {
        "protocol_version": APP_NAVIGATION_EXECUTOR_CONTRACT_VERSION,
        "settled": not unique_gaps,
        "gaps": unique_gaps,
        "after_evidence": {
            "screenshot_artifact_id": shot or None,
            "ui_tree_present": has_tree,
            "package_name": pkg or None,
            "activity": act or None,
            "window": window,
            "orientation": orient or None,
            "page_state": state or None,
            "bound_epoch": bound or None,
            "after_epoch": after or None,
        },
        "proves_native_navigation_click": not unique_gaps,
    }


def settlement_blocks_business_arrival(settlement: Mapping[str, Any] | None) -> bool:
    if not isinstance(settlement, Mapping):
        return True
    if settlement.get("settled") is True:
        return False
    gaps = settlement.get("gaps")
    if not isinstance(gaps, (list, tuple)):
        return True
    return any(str(item) in REQUIRED_AFTER_GAPS for item in gaps)
