"""视口必要媒体的只读终态证据，不以网络空闲推断媒体成像。"""

import hashlib
from collections import Counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from services.api.app.application.web_assessment_evidence import _safe_attribute_value


async def read_viewport_media_readiness(page: Any) -> dict[str, Any]:
    """保存解码事实与脱敏资源引用；缺失采样能力始终返回未知。"""
    raw = await page.evaluate(
        """() => [...document.querySelectorAll("img,video")].flatMap((item, index) => {
          const style = getComputedStyle(item), r = item.getBoundingClientRect();
          const w = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
          const h = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
          if (style.display === "none" || style.visibility === "hidden"
              || Number(style.opacity || 1) <= .05 || w <= 0 || h <= 0) return [];
          const image = item instanceof HTMLImageElement;
          const source = item.currentSrc || item.src || "";
          const loaded = image ? item.complete && item.naturalWidth > 0 : item.readyState >= 2;
          const failed = image ? item.complete && item.naturalWidth === 0 && Boolean(source)
            : Boolean(item.error);
          return [{index, tag: item.tagName.toLowerCase(),
            rect: {x:r.x,y:r.y,width:r.width,height:r.height},
            viewport_coverage: w*h/(innerWidth*innerHeight),
            critical: w*h/(innerWidth*innerHeight) >= .05,
            semantic_required: item.getAttribute("aria-required") === "true",
            complete: image ? item.complete : null,
            natural_width: image ? item.naturalWidth : null,
            natural_height: image ? item.naturalHeight : null,
            ready_state: image ? null : item.readyState,
            error_code: !image && item.error ? item.error.code : null,
            current_src: source,
            resource_state: loaded ? "loaded" : failed ? "failed" : source ? "pending" : "unknown",
            resource_state_basis: image ? "image_decode_state" : "media_decode_state"}];
        })"""
    )
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        return {"schema_version": "viewport-media.v2", "state": "unknown", "items": []}
    items = []
    for raw_item in raw[:100]:
        item = dict(raw_item)
        source = str(item.get("current_src") or "")
        item["source_present"] = bool(source)
        item["current_src_sha256"] = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
        try:
            parts = urlsplit(source)
            item["current_src"] = (
                urlunsplit(
                    (
                        parts.scheme,
                        parts.hostname or "",
                        _safe_attribute_value("href", source),
                        "",
                        "",
                    )
                )
                if parts.scheme in {"http", "https"}
                else "[non-http-resource]"
            )
        except ValueError:
            item["current_src"] = "[invalid-resource]"
        items.append(item)
    # critical 仅表示几何关键性，aria-required 单独保留为明确的语义必要性。
    necessary = [
        item
        for item in items
        if item.get("critical") is True or item.get("semantic_required") is True
    ]
    noncritical = [
        item
        for item in items
        if item.get("critical") is False and item.get("semantic_required") is False
    ]
    known_states = {"loaded", "failed", "pending", "unknown"}
    invalid = any(
        type(item.get("critical")) is not bool
        or type(item.get("semantic_required")) is not bool
        or not isinstance(item.get("resource_state"), str)
        or item.get("resource_state") not in known_states
        for item in items
    )
    if invalid:
        return {
            "schema_version": "viewport-media.v2",
            "state": "unknown",
            "items": items,
            "sample_truncated": len(raw) > 100,
            "warnings": ["media_sampling_invalid"],
        }
    necessary_counts = Counter(item["resource_state"] for item in necessary)
    noncritical_counts = Counter(item["resource_state"] for item in noncritical)
    states = set(necessary_counts)
    if len(raw) > 100 or "unknown" in states:
        state = "unknown"
    elif "pending" in states:
        state = "pending"
    elif "failed" in states:
        state = "critical_failed"
    else:
        state = "ready"
    return {
        # v2 的 state 仅描述必要集合；非关键异常绝不伪装为必要媒体未就绪。
        "schema_version": "viewport-media.v2",
        "state": state,
        "items": items,
        "sample_truncated": len(raw) > 100,
        "necessary_count": len(necessary),
        "necessary_ready_count": necessary_counts["loaded"],
        "necessary_resource_counts": {key: necessary_counts[key] for key in sorted(known_states)},
        "noncritical_resource_counts": {
            key: noncritical_counts[key] for key in sorted(known_states)
        },
        "warnings": [
            "noncritical_media_" + key for key in sorted(noncritical_counts) if key != "loaded"
        ],
    }
