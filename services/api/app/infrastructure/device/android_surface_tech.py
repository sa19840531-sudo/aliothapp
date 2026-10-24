"""有界 App surface_tech 推断：仅凭 UIA 证据，禁止无树瞎猜。"""

from __future__ import annotations

from collections.abc import Mapping

APP_SURFACE_TECH_VERSION = "app-surface-tech.v1"

_FLUTTER_MARKERS = (
    "io.flutter",
    "flutterview",
    "flutter.embedding",
    "flt-semantics",
)
_WEBVIEW_MARKERS = (
    "android.webkit.webview",
    "com.tencent.smtt",
    "com.uc.webview",
    "com.android.webview",
)


def infer_android_surface_tech(
    ui_hierarchy_xml: str | None,
    *,
    has_meaningful_content: bool | None = None,
) -> dict[str, object]:
    """从 UIA XML 推断 native／webview_shell／flutter／unknown。

    无树或证据不足 → ``unknown``；不写死产品画像。
    """

    text = str(ui_hierarchy_xml or "")
    lowered = text.casefold()
    reasons: list[str] = []
    if not text.strip() or "<hierarchy" not in lowered:
        return {
            "protocol_version": APP_SURFACE_TECH_VERSION,
            "surface_tech": "unknown",
            "confidence": "none",
            "authority": False,
            "reason_codes": ["ui_hierarchy_unavailable"],
        }

    flutter_hits = sum(1 for marker in _FLUTTER_MARKERS if marker in lowered)
    webview_hits = sum(1 for marker in _WEBVIEW_MARKERS if marker in lowered)
    node_count = lowered.count("<node")

    if flutter_hits > 0 and flutter_hits >= webview_hits:
        reasons.append("flutter_view_markers")
        return {
            "protocol_version": APP_SURFACE_TECH_VERSION,
            "surface_tech": "flutter",
            "confidence": "high" if flutter_hits >= 2 else "medium",
            "authority": False,
            "reason_codes": reasons,
            "marker_hits": {"flutter": flutter_hits, "webview": webview_hits},
            "node_count": node_count,
        }

    if webview_hits > 0:
        # 仅空壳 WebView、无业务子节点 → webview_shell
        meaningful = has_meaningful_content
        if meaningful is None:
            # 粗判：除 WebView 外几乎无其它可点击业务节点时视为壳
            meaningful = node_count > webview_hits + 3
        if not meaningful:
            reasons.append("webview_shell_sparse_tree")
            tech = "webview_shell"
            confidence = "medium"
        else:
            reasons.append("webview_present_with_nodes")
            tech = "webview_shell"
            confidence = "low"
        return {
            "protocol_version": APP_SURFACE_TECH_VERSION,
            "surface_tech": tech,
            "confidence": confidence,
            "authority": False,
            "reason_codes": reasons,
            "marker_hits": {"flutter": flutter_hits, "webview": webview_hits},
            "node_count": node_count,
        }

    if node_count >= 3:
        reasons.append("native_like_uia_nodes")
        return {
            "protocol_version": APP_SURFACE_TECH_VERSION,
            "surface_tech": "native",
            "confidence": "medium" if node_count >= 8 else "low",
            "authority": False,
            "reason_codes": reasons,
            "marker_hits": {"flutter": 0, "webview": 0},
            "node_count": node_count,
        }

    reasons.append("insufficient_structure_markers")
    return {
        "protocol_version": APP_SURFACE_TECH_VERSION,
        "surface_tech": "unknown",
        "confidence": "low",
        "authority": False,
        "reason_codes": reasons,
        "marker_hits": {"flutter": flutter_hits, "webview": webview_hits},
        "node_count": node_count,
    }


def surface_tech_code(payload: Mapping[str, object] | None) -> str:
    if not isinstance(payload, Mapping):
        return "unknown"
    code = str(payload.get("surface_tech") or "unknown").strip().casefold()
    return code or "unknown"
