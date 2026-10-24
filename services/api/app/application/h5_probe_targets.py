"""H5 配对补证的展示目标合同；不接收规则或模型传入的 selector。"""

import math
import re
from collections.abc import Mapping

from services.api.app.domain.protocol import stable_protocol_hash

PROBE_TARGET_MODE = "h5_probe_display_target"
PROBE_TARGET_LABELS = {
    "media_fullscreen_pair": "放大当前帖子媒体",
    "payment_plan_selection": "选择当前会员档位",
    "native_video_fullscreen": "进入当前视频原生全屏",
    "scroll_button_feedback": "验证滚动后只读按钮反馈",
    "scroll_back_to_top": "点击当前列表返回顶部按钮",
}


def payment_plan_structure(element: Mapping) -> bool:
    """结构登记同时用于有界快照保留；登记本身不授予点击。"""
    classes = {str(item).casefold() for item in element.get("class_tokens") or ()}
    ancestors = {str(item).casefold() for item in element.get("ancestor_class_tokens") or ()}
    return bool(
        (
            element.get("role") in {"radio", "option"}
            and {"vip-plans", "membership-plans", "plan-list", "package-list"}.intersection(
                ancestors
            )
            and {"plan-option", "package-item", "vip-option"}.intersection(classes)
        )
        or (
            "swiper-slide" in classes
            and {"member", "card-box", "swiper-wrapper"}.issubset(ancestors)
        )
    )


def back_to_top_structure(element: Mapping) -> bool:
    """只认专用回顶控件，不把“返回”导航或支付文案视为只读反馈。"""
    label = str(element.get("aria_label") or element.get("text") or "").strip().casefold()
    return bool(
        label in {"返回顶部", "回到顶部", "回顶部", "back to top"}
        and (element.get("tag") == "button" or element.get("role") == "button")
        and element.get("interactive") is True
    )


def disclosure_structure(element: Mapping) -> bool:
    classes = set(element.get("class_tokens") or [])
    return bool(
        "van-collapse-item__title" in classes
        and element.get("role") == "button"
        and element.get("aria_expanded") in {"true", "false"}
        and element.get("aria_controls")
        and (element.get("h5_probe_controlled_region") or {}).get("unique") is True
        and element.get("interactive") is True
    )


def probe_subject_key(element: Mapping) -> str:
    """对象内容与结构一起绑定；不依赖易漂移的列表位置或屏幕坐标。"""
    identity = {
        key: element.get(key)
        for key in (
            "selector",
            "tag",
            "role",
            "text",
            "src",
            "href",
            "aria_label",
            "alt",
            "class_tokens",
            "parent_class_tokens",
            "ancestor_class_tokens",
            "h5_media_instance_id",
            "h5_probe_scroll_context",
            "aria_expanded",
            "aria_controls",
        )
    }
    if element.get("tag") == "img":
        for key in ("h5_probe_preview_controlled_root", "h5_probe_image_resources"):
            identity[key] = element.get(key)
        if element.get("h5_probe_gallery_binding") is not None:
            identity["h5_probe_gallery_binding"] = element["h5_probe_gallery_binding"]
    if element.get("h5_probe_plan_label"):
        identity["text"] = element["h5_probe_plan_label"]
    if payment_plan_structure(element):
        transient = {
            "selected",
            "active",
            "checked",
            "swiper-slide-active",
            "swiper-slide-prev",
            "swiper-slide-next",
            "swiper-slide-visible",
            "swiper-slide-fully-visible",
        }
        for key in ("class_tokens", "parent_class_tokens", "ancestor_class_tokens"):
            identity[key] = sorted(
                token for token in element.get(key) or [] if str(token).casefold() not in transient
            )
    return stable_protocol_hash(identity)


def visible_probe_element(element: Mapping, snapshot: Mapping) -> bool:
    rect, viewport, hit = (element.get("rect"), snapshot.get("viewport"), element.get("hit_test"))
    if not all(isinstance(item, Mapping) for item in (rect, viewport, hit)):
        return False
    if (
        hit.get("center_hits_target") is not True
        or element.get("visible") is False
        or element.get("disabled") is True
        or str(element.get("aria_disabled") or "").casefold() == "true"
        or not element.get("selector")
        or element.get("is_advertisement") is True
        or element.get("within_web_navigation") is True
    ):
        return False
    try:
        x, y, width, height = (float(rect[key]) for key in ("x", "y", "width", "height"))
        vw, vh = (float(viewport[key]) for key in ("width", "height"))
    except (KeyError, ValueError, TypeError):
        return False
    return (
        all(math.isfinite(value) for value in (x, y, width, height, vw, vh))
        and min(width, height, vw, vh) > 0
        and 0 < x + width / 2 < vw
        and 0 < y + height / 2 < vh
    )


def select_probe_target(
    snapshot: Mapping,
    *,
    goal: str,
    page_type: str,
    expected_key: str | None = None,
    excluded_keys=(),
) -> Mapping | None:
    """只接受唯一可见帖子媒体或无提交副作用的标准档位控件。"""
    if snapshot.get("capture_degraded") or snapshot.get("collection_error"):
        return None
    elements = snapshot.get("elements")
    if not isinstance(elements, (list, tuple)):
        return None
    matches = []
    for element in elements:
        if not isinstance(element, Mapping) or not visible_probe_element(element, snapshot):
            continue
        if (
            element.get("href")
            or element.get("form_associated") is True
            or element.get("h5_probe_local_display") is not True
        ):
            continue
        ancestors = {str(item).casefold() for item in element.get("ancestor_class_tokens") or ()}
        text = str(element.get("text") or element.get("aria_label") or "").strip()
        if goal == "media_fullscreen_pair" and page_type in {
            "post_list",
            "content_detail",
            "post_detail",
        }:
            image = element.get("image")
            image = image if isinstance(image, Mapping) else {}
            if (
                str(element.get("tag") or "").casefold() == "img"
                and element.get("src")
                and image.get("complete") is True
                and isinstance(image.get("natural_width"), int | float)
                and isinstance(image.get("natural_height"), int | float)
                and image["natural_width"] > 0
                and image["natural_height"] > 0
                and {
                    "post-content",
                    "post-images",
                    "content-images-wrap",
                    "dynamicdata-content",
                }.intersection(ancestors)
                and not {"ad", "advertisement", "banner"}.intersection(ancestors)
            ):
                matches.append(element)
        elif goal == "native_video_fullscreen" and page_type in {
            "post_list",
            "post_detail",
            "content_detail",
            "video_detail",
        }:
            media = element.get("media") or {}
            media = media if isinstance(media, Mapping) else {}
            if (
                element.get("tag") == "video"
                and element.get("src")
                and element.get("h5_media_instance_id")
                and media.get("controls") is True
                and media.get("fullscreen_enabled") is True
                and {
                    "post-content",
                    "post-images",
                    "content-images-wrap",
                    "dynamicdata-content",
                    "video-player-box",
                    "player-surface",
                }.intersection(ancestors)
                and isinstance(media.get("ready_state"), int)
                and media["ready_state"] >= 2
                and all(
                    isinstance(media.get(key), int | float) and media[key] > 0
                    for key in ("video_width", "video_height")
                )
                and not {"ad", "advertisement", "banner"}.intersection(ancestors)
            ):
                matches.append(element)
        elif goal in {"scroll_button_feedback", "scroll_back_to_top"} and page_type in {
            "home",
            "content_list",
            "video_list",
            "post_list",
        }:
            from services.api.app.application.h5_probe_journey import probe_scroll_context

            context = probe_scroll_context(snapshot)
            at_bottom = bool(
                context
                and context["scroll_top"] > context["client_height"]
                and context["scroll_top"] + context["client_height"] >= context["scroll_height"] - 2
            )
            if context and (
                (back_to_top_structure(element) and at_bottom)
                or (goal == "scroll_button_feedback" and disclosure_structure(element))
            ):
                matches.append({**element, "h5_probe_scroll_context": context})
        elif goal == "payment_plan_selection" and page_type == "payment":
            label = str(element.get("h5_probe_plan_label") or text)
            if (
                payment_plan_structure(element)
                and (
                    element.get("role") in {"radio", "option"} or element.get("interactive") is True
                )
                and element.get("aria_checked") != "true"
                and element.get("aria_selected") != "true"
                and not {"selected", "checked"}.intersection(element.get("class_tokens") or [])
                and re.search(r"(?:¥|￥|\$|元)\s*\d|\d\s*元", label)
                and not re.search(
                    r"支付|充值|购买|提交|pay|submit|purchase|order", text + label, re.I
                )
            ):
                matches.append(element)
    matches = [
        item
        for item in matches
        if probe_subject_key(item) not in excluded_keys
        and (expected_key is None or probe_subject_key(item) == expected_key)
    ]
    if goal == "payment_plan_selection" and matches:
        # 每个 Probe 只选一个不同档位；重复文案不能凭位置猜对象，也不补算全部选档。
        labels = {str(item.get("text") or item.get("aria_label") or "") for item in matches}
        if len(labels) == len(matches):
            return min(
                matches, key=lambda item: (item["rect"]["y"], item["rect"]["x"], item["selector"])
            )
    return matches[0] if len(matches) == 1 else None


def select_cover_subject(snapshot: Mapping, *, page_type: str, selector: str | None = None):
    """封面补证沿用列表卡片登记结构，只选择可见首卡并保留对象身份。"""
    from services.api.app.application.h5_list_detail_navigation import (
        h5_list_detail_selectors,
        h5_list_detail_snapshot_element_matches,
    )

    selectors = (selector,) if selector else h5_list_detail_selectors(page_type)
    matches = [
        element
        for element in snapshot.get("elements") or ()
        if isinstance(element, Mapping)
        and visible_probe_element(element, snapshot)
        and any(
            h5_list_detail_snapshot_element_matches(
                element=element, source_page_type=page_type, selector=item
            )
            for item in selectors
        )
    ]
    return (
        min(matches, key=lambda item: (item["rect"]["y"], item["rect"]["x"], item["selector"]))
        if matches
        else None
    )


def payment_plan_reveal_needed(snapshot: Mapping, *, excluded_keys=()) -> bool:
    """仅真实横向屏外档位触发原有有界横向浏览；遮挡不作为屏外处理。"""
    view = snapshot.get("viewport") or {}
    if not all(
        isinstance(view.get(key), int | float) and view[key] > 0 for key in ("width", "height")
    ):
        return False
    for item in snapshot.get("elements") or []:
        if not isinstance(item, Mapping) or not payment_plan_structure(item):
            continue
        label = str(item.get("h5_probe_plan_label") or item.get("text") or "")
        rect = item.get("rect") or {}
        if (
            item.get("h5_probe_local_display") is not True
            or item.get("disabled") is True
            or item.get("aria_disabled") == "true"
            or item.get("href")
            or item.get("aria_selected") == "true"
            or item.get("aria_checked") == "true"
            or probe_subject_key(item) in excluded_keys
            or not re.search(r"(?:¥|￥|\$|元)\s*\d|\d\s*元", label)
            or re.search(r"支付|购买|提交|pay|submit|purchase|order", label, re.I)
            or not all(
                isinstance(rect.get(key), int | float) and math.isfinite(rect[key])
                for key in ("x", "y", "width", "height")
            )
        ):
            continue
        if (
            rect["width"] > 0
            and rect["height"] > 0
            and 0 < rect["y"] + rect["height"] / 2 < view["height"]
            and not 0 < rect["x"] + rect["width"] / 2 < view["width"]
        ):
            return True
    return False
