"""H5 交互能力的共享结构合同。

页面采集、页面检测与执行验签必须使用同一套结构定义。这里仅描述可保留、
可验签的结构事实，不根据域名、坐标或站点名称授权点击。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

STARTUP_AD_DISMISSAL_SELECTOR = ".loading-page .down-time:visible"
GLOBAL_LAUNCH_DISMISSAL_SELECTOR = (
    '.globalLaunch .header-title > span:text-is("跳过"):visible'
)
COUNTDOWN_JUMP_DISMISSAL_SELECTOR = (
    '.countdown > .jump_over:text-is("跳过"):visible'
)
POPUP_SKIP_WRAP_DISMISSAL_SELECTOR = (
    '.van-popup:visible .skip-wrap:has(> div:text-is("0s(跳过)")):visible'
)
POPUP_COUNT_DOWN_DISMISSAL_SELECTOR = (
    '.van-popup:visible .count-down:text-is("跳过"):visible'
)
LAUNCH_PAGE_ENTRY_DISMISSAL_SELECTOR = (
    '.launch-page:visible .screen-ad > .button:has(> div:text-is("进入")):visible'
)


def h5_startup_dismissal_selector_from_snapshot_element(
    element: Mapping[str, Any],
) -> str | None:
    """从单个 UI Snapshot 元素识别已登记的开屏退出结构。

    返回选择器只表示结构合同命中；执行端仍须分别校验操作类型、唯一性、
    可见性与 hit-test，不能把本函数结果单独当成点击授权。
    """

    class_tokens = _normalized_tokens(element.get("class_tokens"))
    ancestor_tokens = _normalized_tokens(element.get("ancestor_class_tokens"))
    text = _normalized_text(element.get("text"))
    tag = str(element.get("tag") or "").strip().casefold()

    # down-time 结构由检测阶段的 loading-page DOM 合同签发；历史 Snapshot
    # 未必携带祖先 token，因此执行阶段继续以已签发能力 + 唯一命中验签。
    if "down-time" in class_tokens:
        return STARTUP_AD_DISMISSAL_SELECTOR
    if (
        tag == "span"
        and text == "跳过"
        and {"globallaunch", "header-title"}.issubset(ancestor_tokens)
    ):
        return GLOBAL_LAUNCH_DISMISSAL_SELECTOR
    if (
        "jump_over" in class_tokens
        and "countdown" in ancestor_tokens
        and text == "跳过"
    ):
        return COUNTDOWN_JUMP_DISMISSAL_SELECTOR
    if (
        "skip-wrap" in class_tokens
        and "van-popup" in ancestor_tokens
        and text == "0s(跳过)"
    ):
        return POPUP_SKIP_WRAP_DISMISSAL_SELECTOR
    if (
        "count-down" in class_tokens
        and "van-popup" in ancestor_tokens
        and text == "跳过"
    ):
        return POPUP_COUNT_DOWN_DISMISSAL_SELECTOR
    if (
        "button" in class_tokens
        and {"launch-page", "screen-ad"}.issubset(ancestor_tokens)
        and text == "进入"
    ):
        return LAUNCH_PAGE_ENTRY_DISMISSAL_SELECTOR
    return None


def _normalized_tokens(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(item).strip().casefold()
        for item in value
        if str(item).strip()
    }


def _normalized_text(value: object) -> str:
    return "".join(str(value or "").split()).casefold()
