"""把 UI Snapshot 的 DOM / Semantics 事实适配为统一 H5 逻辑页面。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.application.h5_obstruction_facts import is_h5_nonblocking_inline_state
from services.api.app.application.h5_structural_navigation import (
    H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT,
)
from services.api.app.domain.exploration.h5_logical_surface import (
    build_h5_logical_surface_contract,
    h5_logical_route_hash,
    stable_h5_surface_hash,
)
from services.api.app.domain.exploration.h5_visual_navigation import (
    classify_flutter_semantics_coverage,
)

_DYNAMIC_TEXT_PATTERN = re.compile(
    r"(?:\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|https?://\S+|\b\d{1,4}(?:[-/:.]\d{1,4})+\b|"
    r"(?:¥|￥|\$)\s*\d+(?:\.\d+)?|\b\d+(?:\.\d+)?\b)",
    re.IGNORECASE,
)
_DYNAMIC_CLASS_PATTERN = re.compile(
    r"(?:^flt-|^semantics-|[0-9a-f]{8,}|\d{3,})",
    re.IGNORECASE,
)
_CONTENT_CONTEXT_PATTERN = re.compile(
    r"(?:^|[-_])(?:announcement|article|banner|card|carousel|comment|feed|item|"
    r"marquee|masonry|movie|notice|poi|post|promo|swiper|ticker|video)"
    r"(?:$|[-_])",
    re.IGNORECASE,
)
_STATE_MARKER_PATTERN = re.compile(
    r"dialog|modal|overlay|popup|toast|loading|spinner|countdown|count-down|遮罩|弹窗|加载",
    re.IGNORECASE,
)
_ACTIVE_MARKERS = frozenset({"active", "selected", "current", "checked"})
_NAVIGATION_ROLES = frozenset({"tab", "menuitem", "navigation"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_STABLE_CONTROL_ROLES = frozenset(
    {"button", "link", "tab", "menuitem", "checkbox", "radio", "switch"}
)


def build_h5_logical_surface(
    *,
    ui_snapshot: Mapping[str, Any],
    page_url: str,
    surface_session_id: str | None,
    document_token: str | None,
) -> dict[str, Any]:
    """在渲染器差异之上构造统一逻辑页面快照。"""
    if not isinstance(ui_snapshot, Mapping):
        raise TypeError("ui_snapshot 必须为对象")
    elements = _elements(ui_snapshot.get("elements"))
    render_profile = detect_h5_render_profile(
        ui_snapshot=ui_snapshot,
        elements=elements,
    )
    semantics_coverage = classify_flutter_semantics_coverage(
        elements=elements,
        ocr_text=ui_snapshot.get("ocr_text"),
    )
    state_elements = _surface_state_elements(
        ui_snapshot=ui_snapshot,
        elements=elements,
    )
    state_element_ids = {id(item) for item in state_elements}
    identity_elements = [item for item in elements if id(item) not in state_element_ids]
    identity_signals = _identity_signals(
        ui_snapshot=ui_snapshot,
        elements=identity_elements,
        render_profile_kind=str(render_profile["kind"]),
        render_profile=render_profile,
        semantics_coverage=semantics_coverage,
    )
    route_hash = h5_logical_route_hash(page_url)
    if route_hash is not None:
        identity_signals["route"] = route_hash
    state_signals = _state_signals(
        ui_snapshot=ui_snapshot,
        elements=elements,
        state_elements=state_elements,
    )
    state_signals["semantics_coverage"] = semantics_coverage
    visual_layout = _trusted_visual_layout_evidence(
        ui_snapshot=ui_snapshot,
        render_profile=render_profile,
        semantics_coverage=semantics_coverage,
    )
    if visual_layout is not None:
        state_signals["visual_layout_audit"] = visual_layout["state_audit"]
    degraded_reasons = _degraded_reasons(
        ui_snapshot=ui_snapshot,
        elements=elements,
        render_profile=render_profile,
    )
    surface = build_h5_logical_surface_contract(
        render_profile=render_profile,
        document_identity_material=(
            str(surface_session_id or ""),
            str(document_token or ""),
        ),
        identity_signals=identity_signals,
        state_signals=state_signals,
        evidence_sources=[
            *_evidence_sources(elements=elements, ui_snapshot=ui_snapshot),
            *(("visual_layout",) if visual_layout is not None else ()),
        ],
        evidence_quality=_evidence_quality(
            render_profile=render_profile,
            identity_element_count=len(identity_elements),
            semantics_coverage=semantics_coverage,
        ),
        degraded_reasons=degraded_reasons,
    )
    surface["semantics_coverage"] = semantics_coverage
    if render_profile.get("kind") == "dom_spa":
        native_structure = _native_main_shell_structure(ui_snapshot)
        if native_structure:
            surface["native_main_structure"] = native_structure
    return surface


def _evidence_quality(
    *,
    render_profile: Mapping[str, Any],
    identity_element_count: int,
    semantics_coverage: Mapping[str, object],
) -> str:
    """把 Flutter Semantics 覆盖档位纳入证据质量，绝不写入身份材料。"""
    base_quality = (
        "high"
        if render_profile.get("status") == "confirmed" and identity_element_count >= 2
        else "medium"
        if identity_element_count
        else "insufficient"
    )
    if render_profile.get("kind") not in {"flutter_web", "hybrid"}:
        return base_quality
    if semantics_coverage.get("level") == "semantics_sufficient":
        return base_quality
    return "medium" if base_quality == "high" else base_quality


def detect_h5_render_profile(
    *,
    ui_snapshot: Mapping[str, Any],
    elements: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """使用确定性渲染证据识别 DOM SPA、Flutter Web 与 Hybrid。"""
    flutter_elements = [item for item in elements if _is_flutter_semantics(item)]
    dom_elements = [item for item in elements if not _is_flutter_semantics(item)]
    document = ui_snapshot.get("document")
    document_facts = document if isinstance(document, Mapping) else {}
    flutter_root_present = document_facts.get("flutter_root_present") is True
    flutter_signals: list[str] = []
    if flutter_root_present:
        flutter_signals.append("flutter_render_root")
    if len(flutter_elements) >= 2:
        flutter_signals.append("flutter_semantics_tree")
    flutter_confirmed = bool(flutter_signals)
    spa_confirmed = bool(
        document_facts.get("history_api_observed") is True
        and document_facts.get("spa_root_present") is True
    )

    if flutter_confirmed and any(item.get("interactive") is True for item in dom_elements):
        return {
            "kind": "hybrid",
            "status": "confirmed",
            "confidence": 0.97,
            "signals": [*flutter_signals, "dom_interactive_elements"],
        }
    if flutter_confirmed:
        return {
            "kind": "flutter_web",
            "status": "confirmed",
            "confidence": 0.96,
            "signals": flutter_signals,
        }
    if spa_confirmed:
        return {
            "kind": "dom_spa",
            "status": "confirmed",
            "confidence": 0.94,
            "signals": ["history_api_observed", "spa_root_present"],
        }
    if flutter_elements:
        return {
            "kind": "flutter_web",
            "status": "suspected",
            "confidence": 0.62,
            "signals": ["flutter_semantics_partial"],
        }
    if (
        len(dom_elements) >= 2
        and ui_snapshot.get("schema_version") == "ui-snapshot.v2"
        and ui_snapshot.get("ready_state") in {"interactive", "complete"}
        and ui_snapshot.get("flutter_semantics_activation") in {None, "", "not_applicable"}
        and document_facts.get("time_origin_ms")
        and not ui_snapshot.get("collection_error")
        and not ui_snapshot.get("capture_degraded")
        and not ui_snapshot.get("budget_truncated")
    ):
        # 普通 DOM 不必先使用 SPA 框架；可信身份仍由独立的结构与状态指纹构造。
        return {
            "kind": "dom_document",
            "status": "confirmed",
            "confidence": 0.9,
            "signals": ["ready_document", "dom_elements_present"],
        }
    if dom_elements:
        return {
            "kind": "dom_unclassified",
            "status": "observed",
            "confidence": 0.5,
            "signals": ["dom_elements_present"],
        }
    return {
        "kind": "unknown",
        "status": "insufficient",
        "confidence": 0.0,
        "signals": [],
    }


def _identity_signals(
    *,
    ui_snapshot: Mapping[str, Any],
    elements: Sequence[Mapping[str, Any]],
    render_profile_kind: str,
    render_profile: Mapping[str, Any],
    semantics_coverage: Mapping[str, object],
) -> dict[str, Any]:
    navigation = [item for item in elements if _is_navigation_element(item)]
    structural_navigation = [
        item for item in navigation if not _is_flutter_semantics(item)
    ]
    structural_headings = [
        item
        for item in elements
        if _is_heading(item) and not _is_flutter_semantics(item)
    ]
    structural_controls = [
        item
        for item in elements
        if (
            _is_stable_control(item)
            and not _is_navigation_element(item)
            and not _is_flutter_semantics(item)
        )
    ]
    active_navigation = _sorted_identity_items([
        _element_identity(item, identity_only=True)
        for item in navigation
        if _element_active(item) and not _is_page_tab_element(item)
    ])
    headings = _sorted_unique_identity_items([
        _element_identity(item, identity_only=True)
        for item in elements
        if _is_heading(item)
    ])[:12]
    if not headings and render_profile_kind == "dom_spa":
        headings = _native_page_chrome_headings(ui_snapshot)
    controls = _sorted_identity_items([
        _element_identity(item, identity_only=True)
        for item in structural_controls
    ])[:40]
    semantics = [
        _element_identity(item, identity_only=True)
        for item in elements
        if _is_flutter_semantics_identity_anchor(item)
    ]
    # 列表重复节点的抽样数量属于当前状态，不得挤掉结构类型后改变页面身份。
    main_structure = _sorted_unique_identity_items([
        {
            "tag": str(item.get("tag") or ""),
            "role": str(item.get("role") or ""),
            "classes": _stable_classes(item),
            "rect_bucket": None,
        }
        for item in elements
        if (
            (
                _is_heading(item)
                or _is_stable_control(item)
                or _is_registered_dom_bottom_navigation(item)
            )
            and not _is_flutter_semantics(item)
        )
    ])[:60]
    if not main_structure and render_profile_kind == "dom_spa":
        main_structure = _native_main_shell_structure(ui_snapshot)
    result = {
        "active_navigation": active_navigation,
        "page_heading": headings,
        "main_structure": main_structure,
        "stable_controls": controls,
        "semantics_root": (
            {
                "role_sequence": sorted(
                    {
                        str(item.get("role") or "")
                        for item in semantics
                        if str(item.get("role") or "")
                    }
                ),
            }
            if render_profile_kind in {"flutter_web", "hybrid"}
            else None
        ),
        "cardinality": {
            "navigation": _count_bucket(len(structural_navigation)),
            "controls": _count_bucket(len(controls)),
            "headings": _count_bucket(len(structural_headings)),
        },
        # document.title 经常拼入用户标题或动态内容，只保留在原始 Snapshot，
        # 不进入稳定业务页面身份。
        "document_title": None,
    }
    visual_layout = _trusted_visual_layout_evidence(
        ui_snapshot=ui_snapshot,
        render_profile=render_profile,
        semantics_coverage=semantics_coverage,
    )
    if visual_layout is not None:
        result["visual_layout"] = visual_layout["identity"]
    return result


def _native_main_shell_structure(ui_snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """仅补全缺失的原生主体壳层，不替代既有语义结构或作为单独转场锚点。"""
    proof = _native_shell_proof(ui_snapshot)
    if proof is None or not proof["roots"]:
        return []
    rows = []
    for root in proof["roots"]:
        if not (
            isinstance(root, Mapping)
            and (root.get("tag") == "main" or root.get("role") == "main"
                 or (root.get("tag") in {"div", "section"}
                     and root.get("root_basis") in {
                         "viewport_scroll_region", "viewport_page_container",
                     }
                     and (root.get("root_basis") != "viewport_page_container"
                          or "main" in (root.get("class_tokens") or []))
                     and root.get("visible") is True
                     and root.get("center_hits_root") is True
                     and _positive_number(root.get("viewport_coverage")) is not None
                     and .21 <= root["viewport_coverage"] <= 1))
            and isinstance(root.get("children"), list) and len(root["children"]) <= 32
        ):
            return []
        for depth, item in [(0, root), *((1, child) for child in root["children"])]:
            if not (
                isinstance(item, Mapping) and isinstance(item.get("tag"), str)
                and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", item["tag"])
                and isinstance(item.get("role"), str) and len(item["role"]) <= 64
                and isinstance(item.get("class_tokens"), list)
                and len(item["class_tokens"]) <= 32
                and all(isinstance(token, str) and 0 < len(token) <= 128
                        for token in item["class_tokens"])
            ):
                return []
            if depth and (
                _has_content_context(item)
                or any(_STATE_MARKER_PATTERN.search(token) for token in item["class_tokens"])
                or item["role"] in {"dialog", "timer", "alert", "status"}
            ):
                continue
            classes = sorted({token.strip().casefold() for token in item["class_tokens"]
                              if token.strip().casefold() not in _ACTIVE_MARKERS
                              and not re.search(r"[-_](active|selected|current|checked)$",
                                                token.strip().casefold())})
            rows.append({
                "basis": "native_main_shell", "depth": depth,
                "tag": item["tag"], "role": item["role"],
                "classes": _stable_classes({**item, "class_tokens": classes}),
                "rect_bucket": None,
            })
    return _sorted_unique_identity_items(rows)


def _native_shell_proof(ui_snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """旧快照缺证或跨文档普查不能取得新的页面身份材料。"""
    proof, document = ui_snapshot.get("h5_main_structure"), ui_snapshot.get("document")
    if not (
        isinstance(proof, Mapping) and isinstance(document, Mapping)
        and proof.get("schema_version") == "h5-native-main-shell.v1"
        and proof.get("complete") is True
        and isinstance(proof.get("document_time_origin_ms"), (int, float))
        and not isinstance(proof.get("document_time_origin_ms"), bool)
        and math.isfinite(proof["document_time_origin_ms"])
        and proof["document_time_origin_ms"] == document.get("time_origin_ms")
        and isinstance(proof.get("roots"), list) and len(proof["roots"]) <= 4
    ):
        return None
    return proof


def _native_page_chrome_headings(ui_snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """顶部栏标题补足锚点，但不赋予控件点击能力或单独证明转场。"""
    proof = _native_shell_proof(ui_snapshot)
    if proof is None:
        return []
    headings = proof.get("headings")
    if not isinstance(headings, list) or len(headings) > 12:
        return []
    rows = []
    for item in headings:
        if not (
            isinstance(item, Mapping) and item.get("tag") in {"div", "span"}
            and item.get("role") == ""
            and item.get("heading_basis") == "visible_page_chrome"
            and item.get("visible") is True and item.get("center_hits_heading") is True
            and isinstance(item.get("text"), str) and 0 < len(item["text"]) <= 80
            and isinstance(item.get("class_tokens"), list) and len(item["class_tokens"]) <= 32
            and all(isinstance(token, str) and 0 < len(token) <= 128
                    for token in item["class_tokens"])
            and not _has_content_context(item) and not _is_surface_state_element(item)
        ):
            return []
        rows.append(_element_identity(item, identity_only=True))
    return _sorted_unique_identity_items(rows)


def _trusted_visual_layout_evidence(
    *,
    ui_snapshot: Mapping[str, Any],
    render_profile: Mapping[str, Any],
    semantics_coverage: Mapping[str, object],
) -> dict[str, dict[str, Any]] | None:
    """只让已确认稀疏 Flutter/Hybrid 的专用帧生成视觉布局身份。"""
    if not (
        render_profile.get("kind") in {"flutter_web", "hybrid"}
        and render_profile.get("status") == "confirmed"
        and semantics_coverage.get("level") == "semantics_sparse"
    ):
        return None
    capture = ui_snapshot.get("visual_navigation_capture")
    evidence = ui_snapshot.get("visual_layout_evidence")
    if not (
        isinstance(capture, Mapping)
        and capture.get("protocol_version") == "visual_navigation_capture.v1"
        and capture.get("purpose") == "visual_navigation_viewport"
        and capture.get("coordinate_space") == "visual_viewport_css_pixels"
        and isinstance(evidence, Mapping)
        and evidence.get("protocol_version") == "visual_layout_evidence.v1"
    ):
        return None
    identity = evidence.get("identity")
    state_audit = evidence.get("state_audit")
    if not isinstance(identity, Mapping) or not isinstance(state_audit, Mapping):
        return None
    top = identity.get("top_structure")
    bottom = identity.get("bottom_navigation")
    main = str(identity.get("main_layout_category") or "")
    if not (
        isinstance(top, Mapping)
        and set(top) == {"category", "segment_count"}
        and top.get("category") in {"app_bar", "segmented_header", "sparse_header"}
        and isinstance(top.get("segment_count"), int)
        and not isinstance(top.get("segment_count"), bool)
        and 0 <= top["segment_count"] <= 4
        and isinstance(bottom, Mapping)
        and set(bottom) == {"slot_count", "active_slot"}
        and isinstance(bottom.get("slot_count"), int)
        and not isinstance(bottom.get("slot_count"), bool)
        and 0 <= bottom["slot_count"] <= 8
        and main
        in {
            "sparse",
            "grid",
            "list",
            "detail",
            "profile_menu",
            "settings_form",
            "payment_panel",
            "activity_dashboard",
            "dashboard",
        }
    ):
        return None
    active_slot = bottom.get("active_slot")
    if active_slot is not None and (
        isinstance(active_slot, bool)
        or not isinstance(active_slot, int)
        or active_slot < 0
        or active_slot >= bottom["slot_count"]
    ):
        return None
    allowed_audit_keys = {
        "main_density_bucket",
        "main_component_count_bucket",
        "obstruction_bucket",
        "destination_facts",
        "animation_bucket",
        "full_frame_change_bucket",
        "color_change_bucket",
        "node_set_bucket",
    }
    destination_facts = state_audit.get("destination_facts")
    scalar_audit = {
        key: value
        for key, value in state_audit.items()
        if key != "destination_facts"
    }
    if (
        not set(state_audit).issubset(allowed_audit_keys)
        or not all(
            isinstance(value, str) and 0 < len(value) <= 40
            for value in scalar_audit.values()
        )
        or (
            destination_facts is not None
            and not _valid_visual_destination_facts(destination_facts)
        )
    ):
        return None
    safe_state_audit: dict[str, Any] = {
        str(key): str(value)
        for key, value in sorted(scalar_audit.items())
    }
    if isinstance(destination_facts, Mapping):
        safe_state_audit["destination_facts"] = {
            str(key): value
            for key, value in sorted(destination_facts.items())
        }
    return {
        "identity": {
            "top_structure": {
                "category": str(top["category"]),
                "segment_count": int(top["segment_count"]),
            },
            "bottom_navigation": {
                "slot_count": int(bottom["slot_count"]),
                "active_slot": active_slot,
            },
            "main_layout_category": main,
        },
        "state_audit": safe_state_audit,
    }


def _valid_visual_destination_facts(value: object) -> bool:
    """只接收固定页面结构计数与 chrome 类别，不接收文本或几何细节。"""
    if not isinstance(value, Mapping):
        return False
    count_keys = {
        "large_region_count",
        "body_band_count",
        "wide_row_count",
        "medium_card_count",
        "profile_marker_count",
        "bottom_action_count",
        "dashboard_header_count",
    }
    if set(value) != {
        *count_keys,
        "page_chrome",
        "page_chrome_texture_bucket",
    }:
        return False
    if value.get("page_chrome") not in {
        "app_bar",
        "segmented_header",
        "sparse_header",
    }:
        return False
    if value.get("page_chrome_texture_bucket") not in {
        "sparse",
        "medium",
        "dense",
    }:
        return False
    return all(
        isinstance(value.get(key), int)
        and not isinstance(value.get(key), bool)
        and 0 <= value[key] <= 8
        for key in count_keys
    )


def _state_signals(
    *,
    ui_snapshot: Mapping[str, Any],
    elements: Sequence[Mapping[str, Any]],
    state_elements: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    viewport = ui_snapshot.get("viewport")
    viewport_facts = viewport if isinstance(viewport, Mapping) else {}
    timer_nodes = [
        item for item in elements
        if _is_nonblocking_floating_timer(item, ui_snapshot=ui_snapshot)
    ]
    image_nodes = [
        item for item in elements
        if _is_nonblocking_floating_image(item, ui_snapshot=ui_snapshot)
    ]
    # 屏外/隐藏弹窗仍从身份材料中排除，但不能证明当前视口仍被遮挡。
    visible_state_elements = [
        item for item in state_elements
        if _state_element_in_viewport(item, viewport_facts)
        and not _is_nonblocking_floating_timer(
            item, ui_snapshot=ui_snapshot, timer_nodes=timer_nodes,
        )
        and not _is_nonblocking_floating_image(
            item, ui_snapshot=ui_snapshot, image_nodes=image_nodes,
        )
        and not is_h5_nonblocking_inline_state(item, snapshot=ui_snapshot)
    ]
    selected_page_tabs = sorted(
        (
            {
                "label": _normalize_label(item.get("aria_label") or item.get("text")),
                "selector": _normalize_selector(item.get("selector")),
            }
            for item in elements
            if _is_page_tab_element(item) and _element_active(item)
        ),
        key=lambda item: (item["selector"], item["label"]),
    )
    selected_page_tab = selected_page_tabs[0] if selected_page_tabs else None
    scroll_regions = []
    raw_regions = ui_snapshot.get("h5_scroll_regions")
    for region in raw_regions[:16] if isinstance(raw_regions, list) else ():
        if not isinstance(region, Mapping):
            continue
        try:
            scroll_top = float(region.get("scroll_top") or 0)
            scroll_height = float(region.get("scroll_height") or 0)
            client_height = float(region.get("client_height") or 0)
        except (TypeError, ValueError):
            continue
        if client_height <= 0 or scroll_height <= client_height:
            continue
        scroll_regions.append({
            "selector": _normalize_selector(region.get("selector")),
            "scroll_bucket": _scroll_bucket(scroll_top),
            "at_bottom": scroll_top + client_height >= scroll_height - 2,
        })
    return {
        **({"scroll_regions": scroll_regions} if scroll_regions else {}),
        "obstructions": [_element_state(item) for item in visible_state_elements[:30]],
        "obstruction_count": _count_bucket(len(visible_state_elements)),
        "loading": any(_is_visible_loading_indicator(item, viewport_facts) for item in elements),
        "scroll_bucket": _scroll_bucket(viewport_facts.get("scroll_y")),
        "active_element": _normalize_selector(
            (ui_snapshot.get("document") or {}).get("active_element")
            if isinstance(ui_snapshot.get("document"), Mapping)
            else None
        ),
        "selected_tab": selected_page_tab["label"] if selected_page_tab else None,
        "tab_key": selected_page_tab["selector"] if selected_page_tab else None,
    }


def _element_identity(item: Mapping[str, Any], *, identity_only: bool = False) -> dict[str, Any]:
    return {
        "tag": str(item.get("tag") or ""),
        "role": str(item.get("role") or ""),
        "label": _stable_identity_label(item),
        "classes": _stable_classes(item),
        "navigation_kind": _structural_navigation_kind(item),
        # 临时坐标属于本帧状态和命中证据，不参与稳定业务页面身份。
        "rect_bucket": (
            None if identity_only or _is_page_tab_element(item)
            else _rect_bucket(item.get("rect"))
        ),
    }


def _state_element_in_viewport(item: Mapping[str, Any], viewport: Mapping[str, Any]) -> bool:
    if item.get("visible") is False:
        return False
    style = item.get("style")
    if isinstance(style, Mapping) and (
        style.get("display") == "none"
        or style.get("visibility") in {"hidden", "collapse"}
        or str(style.get("opacity", "1")) == "0"
    ):
        return False
    rect = item.get("rect")
    geometry = _numeric_rect(rect) if isinstance(rect, Mapping) else None
    width = _positive_number(viewport.get("width"))
    height = _positive_number(viewport.get("height"))
    if geometry is None or width is None or height is None:
        # 没有完整几何不能声称遮挡已离开，保留旧快照的保守状态。
        return True
    x, y, w, h = geometry
    return x < width and y < height and x + w > 0 and y + h > 0


def _sorted_identity_items(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """兄弟节点重建可能改变抓取顺序，身份材料只保留无序稳定集合。"""
    return sorted(
        items,
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )


def _sorted_unique_identity_items(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """重建产生的同一 Flutter 兄弟节点只保留一条稳定身份记录。"""
    unique_items = {
        json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in items
    }
    return _sorted_identity_items(list(unique_items.values()))


def _element_state(item: Mapping[str, Any]) -> dict[str, Any]:
    media = item.get("h5_modal_media_identity")
    media_identity = (
        sorted({value for value in media[:4] if isinstance(value, str) and value})
        if isinstance(media, list) else []
    )
    return {
        **_element_identity(item),
        "disabled": item.get("disabled") is True,
        "checked": item.get("checked") is True,
        **({"modal_media_hash": stable_h5_surface_hash(media_identity)} if media_identity else {}),
    }


def _elements(raw: Any) -> list[Mapping[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw[:300] if isinstance(item, Mapping)]


def _is_flutter_semantics(item: Mapping[str, Any]) -> bool:
    return bool(
        str(item.get("tag") or "").casefold() == "flt-semantics"
        or str(item.get("selector") or "").casefold().startswith("flt-semantics")
        or item.get("structural_navigation_kind") == "flutter_bottom_navigation"
    )


def _is_flutter_semantics_identity_anchor(item: Mapping[str, Any]) -> bool:
    """Semantics 根身份只保留结构角色，排除正文与动态空节点。"""
    if not _is_flutter_semantics(item):
        return False
    role = str(item.get("role") or "").strip().casefold()
    return bool(role and role != "text")


def _is_navigation_element(item: Mapping[str, Any]) -> bool:
    return bool(
        _structural_navigation_kind(item)
        or str(item.get("role") or "").casefold() in _NAVIGATION_ROLES
    )


def _is_page_tab_element(item: Mapping[str, Any]) -> bool:
    class_tokens = {
        str(token).strip().casefold()
        for token in item.get("class_tokens") or []
        if str(token).strip()
    }
    return "van-tab" in class_tokens and "van-tabbar-item" not in class_tokens


def _structural_navigation_kind(item: Mapping[str, Any]) -> str:
    declared = str(item.get("structural_navigation_kind") or "").strip()
    if declared:
        return declared
    if _is_registered_dom_bottom_navigation(item):
        return H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT
    return ""


def _is_registered_dom_bottom_navigation(item: Mapping[str, Any]) -> bool:
    """仅把平台登记的 pages-foot 直属 li 投影为 DOM 底栏身份。"""
    parent_classes = {
        str(token).strip().casefold()
        for token in item.get("parent_class_tokens") or []
        if str(token).strip()
    }
    return bool(
        str(item.get("tag") or "").casefold() == "li"
        and "pages-foot-fix" in parent_classes
    )


def _is_heading(item: Mapping[str, Any]) -> bool:
    return bool(
        str(item.get("tag") or "").casefold() in _HEADING_TAGS
        or str(item.get("role") or "").casefold() == "heading"
    )


def _is_stable_control(item: Mapping[str, Any]) -> bool:
    if _is_incidental_floating_control(item):
        return False
    role = str(item.get("role") or "").casefold()
    tag = str(item.get("tag") or "").casefold()
    return bool(
        item.get("interactive") is True
        and (role in _STABLE_CONTROL_ROLES or tag in {"button", "a", "input", "select"})
        and _normalize_label(item.get("aria_label") or item.get("text"))
    )


def _is_incidental_floating_control(item: Mapping[str, Any]) -> bool:
    """排除不遮挡业务内容的小型悬浮入口，避免同页身份随运营挂件抖动。"""
    coverage = item.get("viewport_coverage")
    return bool(
        item.get("fixed_or_sticky_ancestor") is True
        and item.get("within_navigation") is not True
        and isinstance(coverage, (int, float))
        and not isinstance(coverage, bool)
        and 0 <= float(coverage) <= 0.08
    )


def _is_nonblocking_floating_timer(
    item: Mapping[str, Any], *, ui_snapshot: Mapping[str, Any],
    timer_nodes: Sequence[Mapping[str, Any]] = (),
) -> bool:
    """完整弹窗普查排除的纯展示小计时器，不因数字排版变化制造遮挡状态。"""
    if not (
        _has_nonmodal_floating_proof(item, ui_snapshot=ui_snapshot)
        or _has_nonmodal_inline_timer_proof(item, ui_snapshot=ui_snapshot)
    ):
        return False
    role = str(item.get("role") or "").casefold()
    if role == "timer":
        return True
    rect = item.get("rect")
    geometry = _numeric_rect(rect) if isinstance(rect, Mapping) else None
    text = re.sub(r"\s+", "", str(item.get("text") or ""))
    if role or geometry is None or not text:
        return False
    x, y, width, height = geometry
    # 只合并已证明计时器区域内的同文包装层和数字片段，不按产品 CSS 扩大过滤。
    for timer in timer_nodes:
        rect = timer.get("rect")
        timer_rect = _numeric_rect(rect) if isinstance(rect, Mapping) else None
        timer_text = re.sub(r"\s+", "", str(timer.get("text") or ""))
        if timer_rect is None or not re.fullmatch(r"\d+(?::\d+)+", timer_text):
            continue
        if text not in {timer_text, ":", *timer_text.split(":")}:
            continue
        tx, ty, tw, th = timer_rect
        # 小于百万分之一像素的容差只消除浮点加法误差，不扩大实际区域。
        if (
            x >= tx - 1e-6 and y >= ty - 1e-6
            and x + width <= tx + tw + 1e-6 and y + height <= ty + th + 1e-6
        ):
            return True
    return False


def _has_nonmodal_inline_timer_proof(
    item: Mapping[str, Any], *, ui_snapshot: Mapping[str, Any],
) -> bool:
    """内联计时器同样需完整非模态事实及小区域几何，不靠 CSS 推断可放行。"""
    census = ui_snapshot.get("h5_modal_census_selectors")
    rect = item.get("rect")
    geometry = _numeric_rect(rect) if isinstance(rect, Mapping) else None
    viewport = ui_snapshot.get("viewport")
    width = _positive_number(viewport.get("width")) if isinstance(viewport, Mapping) else None
    height = _positive_number(viewport.get("height")) if isinstance(viewport, Mapping) else None
    return bool(
        item.get("fixed_or_sticky_ancestor") is False
        and item.get("interactive") is False
        and item.get("aria_modal") is False
        and item.get("h5_modal_ancestor_selectors") == []
        and ui_snapshot.get("h5_modal_census_truncated") is False
        and isinstance(census, list) and item.get("selector") not in census
        and geometry is not None and width is not None and height is not None
        and geometry[2] * geometry[3] <= width * height * 0.08
    )


def _has_nonmodal_floating_proof(
    item: Mapping[str, Any], *, ui_snapshot: Mapping[str, Any],
) -> bool:
    """完整同帧普查与节点事实同时排除模态归属，缺字段不得推断非遮挡。"""
    census = ui_snapshot.get("h5_modal_census_selectors")
    return bool(
        item.get("interactive") is False
        and item.get("aria_modal") is False
        and item.get("h5_modal_ancestor_selectors") == []
        and _is_incidental_floating_control(item)
        and ui_snapshot.get("h5_modal_census_truncated") is False
        and isinstance(census, list)
        and item.get("selector") not in census
    )


def _is_nonblocking_floating_image(
    item: Mapping[str, Any], *, ui_snapshot: Mapping[str, Any],
    image_nodes: Sequence[Mapping[str, Any]] = (),
) -> bool:
    """小型非模态图片及同框空包装不构成整页遮挡，实际点击仍须验签。"""
    if not _has_nonmodal_floating_proof(item, ui_snapshot=ui_snapshot):
        return False
    role = str(item.get("role") or "").casefold()
    rect = item.get("rect")
    geometry = _numeric_rect(rect) if isinstance(rect, Mapping) else None
    viewport = ui_snapshot.get("viewport")
    if geometry is None or not isinstance(viewport, Mapping):
        return False
    viewport_rect = _numeric_rect({
        "x": 0, "y": 0, "width": viewport.get("width"), "height": viewport.get("height"),
    })
    if (
        viewport_rect is None
        or not all(math.isfinite(value) for value in (*geometry, *viewport_rect))
        or geometry[2] * geometry[3] > viewport_rect[2] * viewport_rect[3] * 0.02
    ):
        return False
    if str(item.get("tag") or "").casefold() == "img" and role in {"", "img"}:
        image = item.get("image")
        return bool(
            isinstance(image, Mapping) and image.get("complete") is True
            and isinstance(image.get("natural_width"), (int, float))
            and not isinstance(image.get("natural_width"), bool) and image["natural_width"] > 0
            and math.isfinite(image["natural_width"])
            and isinstance(image.get("natural_height"), (int, float))
            and not isinstance(image.get("natural_height"), bool) and image["natural_height"] > 0
            and math.isfinite(image["natural_height"])
        )
    if role or str(item.get("text") or "").strip() or item.get("tag") not in {"div", "span"}:
        return False
    # 包装只认已证明图片的同一几何框，不把相邻控件或整层容器一并排除。
    return any(
        isinstance(image.get("rect"), Mapping)
        and (image_rect := _numeric_rect(image["rect"])) is not None
        and all(abs(a - b) <= 1e-6 for a, b in zip(geometry, image_rect, strict=True))
        for image in image_nodes
    )


def _is_surface_state_element(item: Mapping[str, Any]) -> bool:
    if str(item.get("role") or "").casefold() in {"dialog", "alertdialog"}:
        return True
    values = [
        str(item.get("selector") or ""),
        *(str(token) for token in item.get("class_tokens") or []),
        *(str(token) for token in item.get("ancestor_class_tokens") or []),
    ]
    # 文档滚动锁只是全局状态，不能证明其所有后代都是弹窗内容。
    return any(
        _STATE_MARKER_PATTERN.search(value)
        for value in values
        if value.strip().casefold() != "modal-open"
    )


def _surface_state_elements(
    *,
    ui_snapshot: Mapping[str, Any],
    elements: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    viewport = ui_snapshot.get("viewport")
    viewport_facts = viewport if isinstance(viewport, Mapping) else {}
    census = ui_snapshot.get("h5_modal_census_selectors")
    modal_selectors = (
        {value for value in census if isinstance(value, str) and value}
        if ui_snapshot.get("h5_modal_census_truncated") is False and isinstance(census, list)
        else set()
    )
    return [
        item
        for item in elements
        if _is_surface_state_element(item)
        or item.get("selector") in modal_selectors
        or any(
            parent in modal_selectors
            for parent in item.get("h5_modal_ancestor_selectors") or []
            if isinstance(parent, str)
        )
        or _is_flutter_overlay_geometry(item, viewport=viewport_facts)
    ]


def _is_flutter_overlay_geometry(
    item: Mapping[str, Any],
    *,
    viewport: Mapping[str, Any],
) -> bool:
    """识别无 dialog 角色的 Flutter 中心遮挡，只把几何状态写入 state。"""
    if (
        not _is_flutter_semantics(item)
        or item.get("interactive") is not True
        or _is_navigation_element(item)
        or _normalize_label(item.get("aria_label") or item.get("text"))
    ):
        return False
    rect = item.get("rect")
    if not isinstance(rect, Mapping):
        return False
    numeric = _numeric_rect(rect)
    viewport_width = _positive_number(viewport.get("width"))
    viewport_height = _positive_number(viewport.get("height"))
    if numeric is None or viewport_width is None or viewport_height is None:
        return False
    x, y, width, height = numeric
    center_x = x + width / 2
    horizontally_centered = abs(center_x - viewport_width / 2) <= viewport_width * 0.12
    full_viewport_overlay = bool(
        width >= viewport_width * 0.9
        and height >= viewport_height * 0.85
        and x <= viewport_width * 0.06
        and y <= viewport_height * 0.08
    )
    centered_panel = bool(
        horizontally_centered
        and viewport_width * 0.55 <= width <= viewport_width * 0.95
        and viewport_height * 0.25 <= height <= viewport_height * 0.75
        and viewport_height * 0.1 <= y <= viewport_height * 0.65
    )
    centered_close = bool(
        horizontally_centered
        and 20 <= width <= 64
        and 20 <= height <= 64
        and 0.7 <= width / height <= 1.3
        and viewport_height * 0.45 <= y <= viewport_height * 0.85
    )
    return full_viewport_overlay or centered_panel or centered_close


def _numeric_rect(raw: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    values: list[float] = []
    for key in ("x", "y", "width", "height"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        values.append(float(value))
    if values[2] <= 0 or values[3] <= 0:
        return None
    return values[0], values[1], values[2], values[3]


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if numeric > 0 else None


def _element_active(item: Mapping[str, Any]) -> bool:
    classes = {
        str(token).casefold()
        for token in item.get("class_tokens") or []
        if str(token).strip()
    }
    return bool(
        classes.intersection(_ACTIVE_MARKERS)
        or any(token.endswith(("--active", "-active", "_active")) for token in classes)
        or item.get("checked") is True
        or item.get("aria_selected") is True
        or item.get("aria_selected") == "true"
    )


def _stable_classes(item: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    page_tab = _is_page_tab_element(item)
    for raw in item.get("class_tokens") or []:
        token = str(raw).strip().casefold()
        if (
            not token
            or _DYNAMIC_CLASS_PATTERN.search(token)
            or (
                page_tab
                and (
                    token in _ACTIVE_MARKERS
                    or token.endswith(("--active", "-active", "_active"))
                )
            )
        ):
            continue
        result.append(token[:64])
    return list(dict.fromkeys(result))[:8]


def _stable_identity_label(item: Mapping[str, Any]) -> str:
    """排除列表正文和 Flutter 内容节点，仅保留稳定结构标签。"""
    if _is_navigation_element(item):
        group = item.get("h5_adaptive_navigation_group_selector")
        selector = str(item.get("selector") or "")
        if (
            isinstance(group, str) and group
            and item.get("parent_selector") == group
            and selector.startswith(f"{group} > ")
            and not _is_page_tab_element(item)
            and not _is_flutter_semantics(item)
        ):
            # 同一底栏槽位可随滚动改文案，身份绑定实际结构位置而非展示文字。
            return f"slot:{_normalize_selector(selector)}"
        return _normalize_label(item.get("aria_label") or item.get("text"))
    if _is_flutter_semantics(item) or _has_content_context(item):
        return ""
    return _normalize_label(item.get("aria_label") or item.get("text"))


def _has_content_context(item: Mapping[str, Any]) -> bool:
    values = [
        *(str(token) for token in item.get("class_tokens") or []),
        *(str(token) for token in item.get("ancestor_class_tokens") or []),
    ]
    return any(_CONTENT_CONTEXT_PATTERN.search(value) for value in values)


def _normalize_label(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return _DYNAMIC_TEXT_PATTERN.sub("#", text)[:120]


def _normalize_selector(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = re.split(r"(:nth-(?:of-type|child)\(\d+\))", text)
    return "".join(
        part if re.fullmatch(r":nth-(?:of-type|child)\(\d+\)", part)
        else _DYNAMIC_TEXT_PATTERN.sub("#", part)
        for part in parts
    )[:160]


def _rect_bucket(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, Mapping):
        return None
    result: dict[str, int] = {}
    for key in ("x", "y", "width", "height"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        result[key] = int(round(float(value) / 24.0))
    return result


def _count_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 5:
        return "2-5"
    if value <= 20:
        return "6-20"
    return "20+"


def _scroll_bucket(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    numeric = max(float(value), 0.0)
    if numeric < 120:
        return "top"
    if numeric < 720:
        return "middle"
    return "deep"


def _element_has_state_marker(item: Mapping[str, Any], *markers: str) -> bool:
    values = " ".join(
        [
            str(item.get("selector") or ""),
            *(str(token) for token in item.get("class_tokens") or []),
            *(str(token) for token in item.get("ancestor_class_tokens") or []),
        ]
    ).casefold()
    return any(marker.casefold() in values for marker in markers)


def _is_visible_loading_indicator(item: Mapping[str, Any], viewport: Mapping[str, Any]) -> bool:
    """CSS 名字不能单独否定已渲染正文；只保留可见加载语义或无文本指示器。"""
    if not _state_element_in_viewport(item, viewport):
        return False
    if item.get("aria_busy") is True or item.get("aria_busy") == "true":
        return True
    if not _element_has_state_marker(item, "loading", "spinner"):
        return False
    text = str(item.get("aria_label") or item.get("text") or "").strip()
    return bool(
        not text
        or str(item.get("role") or "").casefold() == "progressbar"
        or re.fullmatch(
            r"(?:loading|加载中|正在加载|请稍候|请稍等)[\s.…。!！]*", text, re.IGNORECASE
        )
    )


def _evidence_sources(
    *, elements: Sequence[Mapping[str, Any]], ui_snapshot: Mapping[str, Any]
) -> list[str]:
    sources: list[str] = []
    if any(not _is_flutter_semantics(item) for item in elements):
        sources.append("dom")
    if any(_is_flutter_semantics(item) for item in elements):
        sources.append("flutter_semantics")
    if isinstance(ui_snapshot.get("document"), Mapping):
        sources.append("document")
    return sources


def _degraded_reasons(
    *,
    ui_snapshot: Mapping[str, Any],
    elements: Sequence[Mapping[str, Any]],
    render_profile: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    document = ui_snapshot.get("document")
    document_facts = document if isinstance(document, Mapping) else {}
    activation = str(ui_snapshot.get("flutter_semantics_activation") or "")
    if activation in {"failed", "ambiguous"}:
        reasons.append("flutter_semantics_unavailable")
    if ui_snapshot.get("capture_degraded"):
        reasons.append(str(ui_snapshot["capture_degraded"])[:120])
    if ui_snapshot.get("collection_error"):
        reasons.append("ui_snapshot_collection_error")
    if not elements:
        reasons.append("logical_elements_missing")
    if render_profile.get("status") == "suspected":
        reasons.append("render_profile_unconfirmed")
    profile_kind = str(render_profile.get("kind") or "")
    has_flutter_evidence = bool(
        document_facts.get("flutter_root_present") is True
        or any(_is_flutter_semantics(item) for item in elements)
    )
    has_interactive_semantics = any(
        _is_flutter_semantics(item) and item.get("interactive") is True
        for item in elements
    )
    if profile_kind in {"flutter_web", "hybrid"} and (
        activation in {"failed", "ambiguous", "unavailable", "not_applicable"}
        or not any(_is_flutter_semantics(item) for item in elements)
    ):
        reasons.append("flutter_semantics_unavailable")
    if (
        profile_kind == "flutter_web"
        and has_flutter_evidence
        and not has_interactive_semantics
    ):
        reasons.append("interactive_exploration_blocked")
    return list(dict.fromkeys(reasons))
