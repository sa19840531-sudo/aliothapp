"""H5 只将已绑定的同一次动作前后材料投影为时序观察。"""

import math
from datetime import datetime
from typing import Any

from services.api.app.application.observation_packet import ObservationSource


def project_h5_role_sequence(
    *,
    primary: ObservationSource,
    role_facts: dict[str, dict[str, Any]],
    current_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """不排序无关角色，不从截图相似度推断时间，不覆盖已有真实时序制品。"""
    if (
        primary.target_platform != "h5"
        or current_snapshot.get("sequence") is True
        or (
            primary.manifest_quality is not None
            and primary.manifest_quality.quality_status != "pass"
        )
    ):
        return current_snapshot
    pairs = {
        (before["page_capture_id"], after["page_capture_id"]): (before, after)
        for before in role_facts.values()
        for after in role_facts.values()
        if before.get("capture_role") in {"before_action", "probe_before"}
        and after.get("capture_role") in {"after_action", "probe_result_replay"}
        and before["page_capture_id"] == after.get("parent_capture_id")
        and all(
            fact.get("ui_snapshot")
            and (
                (fact.get("quality_status") == "pass" and fact.get("manifest_id"))
                or (
                    primary.manifest_quality is None
                    and fact.get("quality_status") is None
                    and fact.get("manifest_id") is None
                    and fact.get("artifact_ids")
                )
            )
            for fact in (before, after)
        )
    }
    if len(pairs) != 1 or not current_snapshot:
        return current_snapshot
    # 影子读取的附件已由仓储校验归属和校验和；投影真实时序不伪造清单裁决。
    before, after = next(iter(pairs.values()))
    if before["ui_snapshot"].get("sequence") is True:
        return current_snapshot
    if after["ui_snapshot"].get("sequence") is True:
        return after["ui_snapshot"]
    try:
        start = datetime.fromisoformat(str(before["captured_at"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(after["captured_at"]).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return current_snapshot
    if start.utcoffset() is None or end.utcoffset() is None or end < start:
        return current_snapshot
    return {
        "schema_version": "h5-action-observation-sequence.v1",
        "sequence": True,
        "source": "h5_bound_action_capture_roles",
        "interval_ms": int((end - start).total_seconds() * 1000),
        "frames": [
            {
                **before["ui_snapshot"],
                "index": 0,
                "capture_id": before["page_capture_id"],
                "captured_at": before["captured_at"],
            },
            {
                **after["ui_snapshot"],
                "index": 1,
                "capture_id": after["page_capture_id"],
                "captured_at": after["captured_at"],
            },
        ],
    }


def h5_modal_count(snapshot: dict[str, Any]) -> int | None:
    """完整现场普查的弹窗层数；缺证时返回未知，不能按 CSS 未命中算零。"""
    census = snapshot.get("h5_modal_census_selectors")
    if (
        snapshot.get("h5_modal_census_truncated") is not False
        or not isinstance(census, list)
        or any(not isinstance(selector, str) or not selector for selector in census)
        or any(
            snapshot.get(key)
            for key in (
                "capture_degraded",
                "collection_error",
            )
        )
    ):
        return None
    elements = {
        item.get("selector"): item
        for item in snapshot.get("elements", [])
        if isinstance(item, dict)
    }
    if not set(census).issubset(elements):
        return None
    viewport = snapshot.get("viewport") or {}
    try:
        area = float(viewport.get("width", 0)) * float(viewport.get("height", 0))
        if not math.isfinite(area) or area <= 0:
            return None
        layers = set()
        for selector in census:
            item = elements[selector]
            rect, style = item.get("rect") or {}, item.get("style") or {}
            width, height = float(rect.get("width", 0)), float(rect.get("height", 0))
            if not all(math.isfinite(value) and value >= 0 for value in (width, height)):
                return None
            coverage = width * height / area
            parents = item.get("h5_modal_ancestor_selectors")
            if not isinstance(parents, list) or any(
                not isinstance(parent, str) for parent in parents
            ):
                return None
            semantic = (
                item.get("aria_modal") is True
                or item.get("role") in {"dialog", "alertdialog"}
                or item.get("tag") == "dialog"
            )
            local_dismissal = any(
                control.get("dismiss_semantics") == "modal"
                and selector in (control.get("h5_modal_ancestor_selectors") or [])
                for control in elements.values()
            )
            if (
                semantic
                or (style.get("position") == "fixed" and coverage >= 0.02 and local_dismissal)
                or (
                    style.get("position") in {"fixed", "absolute"}
                    and coverage >= 0.8
                    and float(style.get("z_index") or 0) >= 100
                )
            ):
                layers.add(selector)
    except (ValueError, TypeError, AttributeError):
        return None
    # 一层弹窗的遮罩、面板可能嵌套，只计最内层；独立并列弹窗各自保留。
    ancestors = {
        parent
        for selector in layers
        for parent in elements[selector].get("h5_modal_ancestor_selectors", [])
    }
    return len(layers - ancestors)
