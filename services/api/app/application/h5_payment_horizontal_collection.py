"""H5 支付页会员套餐横向只读采集合同。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

H5_PAYMENT_HORIZONTAL_TARGET = "浏览全部会员卡"
H5_PAYMENT_HORIZONTAL_TARGET_MODE = "h5_payment_horizontal_collection"
H5_PAYMENT_HORIZONTAL_SOURCE_PAGE_TYPE = "payment"
H5_PAYMENT_HORIZONTAL_SOURCE_PAGE_TYPES = frozenset({"payment", "video_detail"})
H5_PAYMENT_HORIZONTAL_MAX_SEGMENTS = 6
H5_PAYMENT_HORIZONTAL_PURPOSE = "横向浏览当前支付页全部会员套餐"
H5_ENTITLEMENT_HORIZONTAL_PURPOSE = "横向浏览当前详情页权益层全部会员套餐"
H5_PAYMENT_HORIZONTAL_KEYWORDS = (
    "payment_horizontal_collection",
    "read_only_collection",
    "page_type:payment",
)

_MEMBERSHIP_TEXT_MARKERS = (
    "vip",
    "会员",
    "月卡",
    "季卡",
    "年卡",
    "周卡",
    "永久",
    "终身",
    "套餐",
)
_MEMBERSHIP_CLASS_MARKERS = (
    "vip",
    "member",
    "membership",
    "plan",
    "package",
)


def h5_payment_horizontal_container_selector(
    ui_snapshot: Mapping[str, Any] | None,
    *,
    source_page_type: str | None = None,
) -> str | None:
    """返回当前快照中唯一可信的会员套餐横向容器。"""
    if not isinstance(ui_snapshot, Mapping):
        return None
    viewport = ui_snapshot.get("viewport")
    elements = ui_snapshot.get("elements")
    if not isinstance(viewport, Mapping) or not isinstance(elements, list):
        return None
    viewport_width = _positive_number(viewport.get("width"))
    viewport_height = _positive_number(viewport.get("height"))
    if viewport_width is None or viewport_height is None:
        return None

    matches: list[tuple[float, float, str]] = []
    for raw in elements:
        if not isinstance(raw, Mapping) or raw.get("visible") is False:
            continue
        selector = str(raw.get("selector") or "").strip()
        rect = raw.get("rect")
        style = raw.get("style")
        metrics = raw.get("text_metrics")
        hit_test = raw.get("hit_test")
        if (
            not selector
            or len(selector) > 512
            or not isinstance(rect, Mapping)
            or not isinstance(style, Mapping)
            or not isinstance(metrics, Mapping)
            or not isinstance(hit_test, Mapping)
            or hit_test.get("center_hits_target") is not True
        ):
            continue
        width = _positive_number(rect.get("width"))
        height = _positive_number(rect.get("height"))
        scroll_width = _positive_number(metrics.get("scroll_width"))
        client_width = _positive_number(metrics.get("client_width"))
        if None in {width, height, scroll_width, client_width}:
            continue
        assert width is not None
        assert height is not None
        assert scroll_width is not None
        assert client_width is not None
        native_scroll_container = str(style.get("overflow_x") or "").casefold() in {
            "auto",
            "scroll",
        }
        if (
            width < viewport_width * 0.55
            or height < 72
            or height > viewport_height * 0.8
            or metrics.get("horizontal_overflow") is not True
            or not (native_scroll_container or _trusted_horizontal_swiper(raw))
            or scroll_width <= client_width * 1.1
            or not (
                _membership_semantics_present(raw)
                or (
                    (source_page_type or "").strip().casefold() == "video_detail"
                    and _trusted_entitlement_offer_swiper(raw)
                )
            )
        ):
            continue
        matches.append((scroll_width - client_width, width * height, selector))
    if not matches:
        return None
    matches.sort(reverse=True)
    best = matches[0]
    if len(matches) > 1 and best[:2] == matches[1][:2]:
        return None
    return best[2]


def h5_payment_horizontal_context_supported(
    *,
    page_type: str | None,
    page_state: str,
) -> bool:
    """限制会员横滑只用于支付页或详情页权益层。"""
    normalized_type = (page_type or "").strip().casefold()
    normalized_state = page_state.strip().casefold()
    return (
        normalized_type == "payment"
        and normalized_state in {"stable", "normal", "loaded"}
    ) or (
        normalized_type == "video_detail"
        and normalized_state == "entitlement_locked"
    )


def h5_payment_horizontal_keywords(source_page_type: str) -> tuple[str, ...]:
    """返回绑定来源页型的只读横滑关键词。"""
    normalized_type = source_page_type.strip().casefold()
    return (
        "payment_horizontal_collection",
        "read_only_collection",
        f"page_type:{normalized_type}",
    )


def h5_payment_horizontal_purpose(source_page_type: str) -> str:
    """返回与来源页型一致的采集目的。"""
    if source_page_type.strip().casefold() == "video_detail":
        return H5_ENTITLEMENT_HORIZONTAL_PURPOSE
    return H5_PAYMENT_HORIZONTAL_PURPOSE


def h5_payment_horizontal_action_parameters(
    *,
    source_page_type: str = H5_PAYMENT_HORIZONTAL_SOURCE_PAGE_TYPE,
) -> dict[str, object]:
    """返回执行端唯一允许的横向只读采集参数。"""
    normalized_type = source_page_type.strip().casefold()
    if normalized_type not in H5_PAYMENT_HORIZONTAL_SOURCE_PAGE_TYPES:
        raise ValueError("H5 会员横向采集来源页型不受支持")
    return {
        "target_mode": H5_PAYMENT_HORIZONTAL_TARGET_MODE,
        "horizontal_scroll_max_segments": H5_PAYMENT_HORIZONTAL_MAX_SEGMENTS,
        "split_scroll_screenshots": True,
        "source_page_type": normalized_type,
    }


def _membership_semantics_present(element: Mapping[str, Any]) -> bool:
    text = "".join(str(element.get("text") or "").split()).casefold()
    raw_tokens = [
        element.get("class_tokens"),
        element.get("parent_class_tokens"),
        element.get("ancestor_class_tokens"),
    ]
    tokens = " ".join(
        str(item).casefold()
        for values in raw_tokens
        if isinstance(values, list)
        for item in values
    )
    return bool(
        any(marker in text for marker in _MEMBERSHIP_TEXT_MARKERS)
        and (
            any(marker in tokens for marker in _MEMBERSHIP_CLASS_MARKERS)
            or sum(marker in text for marker in _MEMBERSHIP_TEXT_MARKERS) >= 2
        )
    )


def _trusted_horizontal_swiper(element: Mapping[str, Any]) -> bool:
    """仅识别快照中已初始化的横向 Swiper 容器。"""
    class_tokens = element.get("class_tokens")
    if not isinstance(class_tokens, list):
        return False
    normalized = {str(item).strip().casefold() for item in class_tokens}
    return "swiper-horizontal" in normalized and bool(
        {"swiper", "swiper-initialized"} & normalized
    )


def _trusted_entitlement_offer_swiper(element: Mapping[str, Any]) -> bool:
    """识别文字绘制在图片内、但结构明确的详情权益套餐轮播。"""
    if not _trusted_horizontal_swiper(element):
        return False
    ancestor_tokens = element.get("ancestor_class_tokens")
    if not isinstance(ancestor_tokens, list):
        return False
    normalized = {str(item).strip().casefold() for item in ancestor_tokens}
    text = "".join(str(element.get("text") or "").split()).casefold()
    return (
        any("offer" in item for item in normalized)
        and any("popup" in item for item in normalized)
        and any(marker in text for marker in ("特惠", "限时", "会员", "vip", "卡"))
    )


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if number > 0 else None
