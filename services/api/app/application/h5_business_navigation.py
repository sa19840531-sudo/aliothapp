"""从当前 H5 快照发现只读业务页链接，不推测按钮的副作用。"""

from collections.abc import Mapping
from urllib.parse import urljoin, urlsplit


def h5_navigation_snapshot_ready(snapshot: Mapping | None) -> bool:
    return bool(
        isinstance(snapshot, Mapping)
        and isinstance(snapshot.get("elements"), list)
        and snapshot.get("h5_modal_census_truncated") is False
        and isinstance(snapshot.get("h5_modal_census_selectors"), list)
        and not any(snapshot.get(key) for key in ("collection_error", "capture_degraded"))
    )


def discover_h5_business_links(
    *, snapshot: Mapping | None, page_url: str
) -> tuple[tuple[str, str], ...]:
    """只签发可命中、同源且具有明确入口语义的唯一链接。"""
    if not h5_navigation_snapshot_ready(snapshot) or snapshot.get("h5_modal_census_selectors"):
        return ()
    if any(snapshot.get(key) for key in ("budget_truncated", "element_limit_reached")):
        return ()
    labels = {
        "设置": "settings",
        "settings": "settings",
        "系统设置": "settings",
        "会员中心": "payment",
        "充值中心": "payment",
        "vip中心": "payment",
        "membership": "payment",
        "播放视频": "video_detail",
        "watch video": "video_detail",
    }
    grouped: dict[str, set[str]] = {}
    source = urlsplit(page_url)
    for element in snapshot["elements"]:
        if not isinstance(element, Mapping) or str(element.get("tag") or "").casefold() != "a":
            continue
        hit = element.get("hit_test")
        if (
            element.get("visible") is not True
            or element.get("disabled") is True
            or element.get("aria_disabled") in {True, "true"}
            or not isinstance(hit, Mapping)
            or hit.get("center_hits_target") is not True
        ):
            continue
        page_types = {
            labels[str(element.get(key) or "").strip().casefold()]
            for key in ("text", "aria_label", "title")
            if str(element.get(key) or "").strip().casefold() in labels
        }
        if len(page_types) != 1:
            continue
        href = element.get("href")
        if not isinstance(href, str) or not href.strip() or href == "#":
            continue
        target = urljoin(page_url, href)
        try:
            parsed = urlsplit(target)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username
                or parsed.password
                or (source.scheme, source.hostname, source.port)
                != (parsed.scheme, parsed.hostname, parsed.port)
                or target == page_url
                or any(
                    token in (parsed.path + "?" + parsed.query).casefold()
                    for token in (
                        "/api/",
                        "submit",
                        "purchase",
                        "checkout",
                        "order",
                        "delete",
                        "logout",
                        "pay=",
                    )
                )
            ):
                continue
        except ValueError:
            continue
        grouped.setdefault(next(iter(page_types)), set()).add(target)
    return tuple(
        (page_type, next(iter(targets)))
        for page_type, targets in grouped.items()
        if len(targets) == 1
    )
