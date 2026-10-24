"""H5“我的”页设置入口的确定性结构能力合同。"""

from __future__ import annotations

from collections.abc import Mapping

H5_PROFILE_SETTINGS_TARGET = "span.btn-edit:visible"
H5_PROFILE_OPTIONS_SETTINGS_TARGET = "个人中心设置图标"
H5_PROFILE_FLUTTER_SETTINGS_TARGET = "个人中心 Flutter 设置入口"
H5_PROFILE_CLASS_SETTINGS_TARGET = "个人中心类名设置图标"
H5_PROFILE_HEADER_SETTINGS_TARGET = "个人中心头部设置图标"
H5_PROFILE_TOP_RIGHT_SETTINGS_TARGET = "个人中心右上设置图标"
H5_PROFILE_ACTIONS_SETTINGS_TARGET = "个人中心操作区设置图标"
H5_PROFILE_SETTINGS_TARGET_MODE = "profile_settings_navigation"
H5_PROFILE_SETTINGS_CAPABILITY = "btn_edit"
H5_PROFILE_OPTIONS_SETTINGS_CAPABILITY = "options_right_image"
H5_PROFILE_FLUTTER_SETTINGS_CAPABILITY = "flutter_settings_arrow"
H5_PROFILE_CLASS_SETTINGS_CAPABILITY = "setting_image"
H5_PROFILE_HEADER_SETTINGS_CAPABILITY = "header_left_second_image"
H5_PROFILE_TOP_RIGHT_SETTINGS_CAPABILITY = "top_right_icon_setting"
H5_PROFILE_ACTIONS_SETTINGS_CAPABILITY = "mine_header_settings_image"
H5_PROFILE_SETTINGS_SOURCE_PAGE_TYPE = "profile"
H5_PROFILE_SETTINGS_EXPECTED_PAGE_TYPE = "settings"
H5_SETTINGS_PAGE_BACK_TARGET_MODE = "settings_page_back"
H5_SETTINGS_PAGE_BACK_CAPABILITY = "flutter_settings_page_back"
H5_SETTINGS_PAGE_BACK_SOURCE_PAGE_TYPE = "settings"
H5_PROFILE_FAVORITES_PAGE_BACK_TARGET_MODE = "profile_favorites_page_back"
H5_PROFILE_FAVORITES_PAGE_BACK_CAPABILITY = "flutter_profile_favorites_page_back"
H5_PROFILE_FAVORITES_PAGE_BACK_SOURCE_PAGE_TYPE = "video_list"
H5_PROFILE_FAVORITES_PAGE_BACK_SOURCE_PAGE_TYPES = frozenset(
    {H5_PROFILE_FAVORITES_PAGE_BACK_SOURCE_PAGE_TYPE, "content_list"}
)
H5_PROFILE_FAVORITES_PAGE_BACK_EXPECTED_PAGE_TYPE = "profile"
H5_PROFILE_FAVORITES_PAGE_LABEL = "我的收藏"
_CAPABILITY_BY_TARGET = {
    H5_PROFILE_SETTINGS_TARGET: H5_PROFILE_SETTINGS_CAPABILITY,
    H5_PROFILE_OPTIONS_SETTINGS_TARGET: H5_PROFILE_OPTIONS_SETTINGS_CAPABILITY,
    H5_PROFILE_FLUTTER_SETTINGS_TARGET: H5_PROFILE_FLUTTER_SETTINGS_CAPABILITY,
    H5_PROFILE_CLASS_SETTINGS_TARGET: H5_PROFILE_CLASS_SETTINGS_CAPABILITY,
    H5_PROFILE_HEADER_SETTINGS_TARGET: H5_PROFILE_HEADER_SETTINGS_CAPABILITY,
    H5_PROFILE_TOP_RIGHT_SETTINGS_TARGET: H5_PROFILE_TOP_RIGHT_SETTINGS_CAPABILITY,
    H5_PROFILE_ACTIONS_SETTINGS_TARGET: H5_PROFILE_ACTIONS_SETTINGS_CAPABILITY,
}


def h5_profile_settings_capability_for_target(target: str) -> str | None:
    """返回平台维护的设置入口能力，拒绝模型自造定位器。"""
    return _CAPABILITY_BY_TARGET.get(target)


def h5_profile_favorites_page_back_source_type_allowed(page_type: object) -> bool:
    """收藏页允许模型在两种列表分类间漂移，但不放宽强结构签名。"""
    return (
        str(page_type or "").strip().casefold()
        in H5_PROFILE_FAVORITES_PAGE_BACK_SOURCE_PAGE_TYPES
    )


def h5_profile_settings_snapshot_element_matches(
    element: Mapping[str, object],
    *,
    capability: str,
) -> bool:
    """按已签发能力复核当前 Snapshot 中的设置入口结构。"""
    raw_tokens = element.get("class_tokens")
    tokens = (
        {str(item).strip().casefold() for item in raw_tokens if str(item).strip()}
        if isinstance(raw_tokens, list)
        else set()
    )
    tag = str(element.get("tag") or "").casefold()
    if capability == H5_PROFILE_SETTINGS_CAPABILITY:
        return tag == "span" and "btn-edit" in tokens
    if capability == H5_PROFILE_CLASS_SETTINGS_CAPABILITY:
        ancestors = _normalized_tokens(element.get("ancestor_class_tokens"))
        hit_test = element.get("hit_test")
        return bool(
            tag == "img"
            and "setting" in tokens
            and {"left", "top", "header"}.issubset(ancestors)
            and isinstance(hit_test, Mapping)
            and hit_test.get("center_hits_target") is True
        )
    if capability == H5_PROFILE_HEADER_SETTINGS_CAPABILITY:
        ancestors = _normalized_tokens(element.get("ancestor_class_tokens"))
        hit_test = element.get("hit_test")
        return bool(
            tag == "img"
            and "wh-24" in tokens
            and "mr-15" not in tokens
            and {"header-left", "index-header"}.issubset(ancestors)
            and isinstance(hit_test, Mapping)
            and hit_test.get("center_hits_target") is True
        )
    if capability == H5_PROFILE_TOP_RIGHT_SETTINGS_CAPABILITY:
        ancestors = _normalized_tokens(element.get("ancestor_class_tokens"))
        hit_test = element.get("hit_test")
        return bool(
            tag == "img"
            and {"icon", "icon-setting"}.issubset(tokens)
            and {"top-right-menu", "top-wrap", "base-info"}.issubset(ancestors)
            and isinstance(hit_test, Mapping)
            and hit_test.get("center_hits_target") is True
        )
    if capability == H5_PROFILE_ACTIONS_SETTINGS_CAPABILITY:
        ancestors = _normalized_tokens(element.get("ancestor_class_tokens"))
        hit_test = element.get("hit_test")
        return bool(
            tag == "img"
            and "icon-button__img" in tokens
            and {"icon-button", "mine-header__actions"}.issubset(ancestors)
            and "".join(str(element.get("alt") or "").split()) == "设置"
            and isinstance(hit_test, Mapping)
            and hit_test.get("center_hits_target") is True
        )
    if capability == H5_PROFILE_FLUTTER_SETTINGS_CAPABILITY:
        rect = element.get("rect")
        parent_selector = str(element.get("parent_semantics_selector") or "").strip()
        parent_label = str(element.get("parent_semantics_aria_label") or "").strip()
        return bool(
            tag == "flt-semantics"
            and str(element.get("role") or "").casefold() == "img"
            and parent_selector
            and parent_label == "设置"
            and element.get("parent_semantics_tappable") is True
            and isinstance(rect, Mapping)
            and _bounded_number(rect.get("width"), minimum=12, maximum=32)
            and _bounded_number(rect.get("height"), minimum=12, maximum=32)
        )
    if capability != H5_PROFILE_OPTIONS_SETTINGS_CAPABILITY or tag != "img":
        return False
    raw_ancestors = element.get("ancestor_class_tokens")
    ancestors = (
        {
            str(item).strip().casefold()
            for item in raw_ancestors
            if str(item).strip()
        }
        if isinstance(raw_ancestors, list)
        else set()
    )
    hit_test = element.get("hit_test")
    return bool(
        {"options", "right"}.issubset(ancestors)
        and isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is True
    )


def h5_settings_page_back_snapshot_element_matches(
    element: Mapping[str, object],
    *,
    snapshot: Mapping[str, object],
) -> bool:
    """复核 Flutter 设置页左上角空文本返回控件及其全屏父语义。"""
    return _h5_fullscreen_labeled_back_snapshot_element_matches(
        element,
        snapshot=snapshot,
        parent_label="设置",
    )


def h5_profile_favorites_page_back_snapshot_element_matches(
    element: Mapping[str, object],
    *,
    snapshot: Mapping[str, object],
) -> bool:
    """复核 Flutter“我的收藏”全屏页左上角的唯一返回控件。"""
    return _h5_fullscreen_labeled_back_snapshot_element_matches(
        element,
        snapshot=snapshot,
        parent_label=H5_PROFILE_FAVORITES_PAGE_LABEL,
    )


def _h5_fullscreen_labeled_back_snapshot_element_matches(
    element: Mapping[str, object],
    *,
    snapshot: Mapping[str, object],
    parent_label: str,
) -> bool:
    """按固定全屏语义标题复核左上空文本 Flutter 返回控件。"""
    if (
        str(element.get("tag") or "").casefold() != "flt-semantics"
        or str(element.get("role") or "").strip()
        or str(element.get("text") or "").strip()
        or str(element.get("aria_label") or "").strip()
        or element.get("interactive") is not True
        or element.get("parent_semantics_tappable") is not True
        or str(element.get("parent_semantics_aria_label") or "").strip() != parent_label
    ):
        return False
    parent_selector = str(element.get("parent_semantics_selector") or "").strip()
    rect = element.get("rect")
    hit_test = element.get("hit_test")
    viewport = snapshot.get("viewport")
    raw_elements = snapshot.get("elements")
    if (
        not parent_selector
        or not isinstance(rect, Mapping)
        or not isinstance(hit_test, Mapping)
        or hit_test.get("center_hits_target") is not True
        or not isinstance(viewport, Mapping)
        or not isinstance(raw_elements, list)
        or not _bounded_number(rect.get("x"), minimum=-1, maximum=12)
        or not _bounded_number(rect.get("y"), minimum=0, maximum=20)
        or not _bounded_number(rect.get("width"), minimum=48, maximum=90)
        or not _bounded_number(rect.get("height"), minimum=24, maximum=48)
    ):
        return False
    viewport_width = viewport.get("width")
    viewport_height = viewport.get("height")
    if not (
        _bounded_number(viewport_width, minimum=320, maximum=600)
        and _bounded_number(viewport_height, minimum=600, maximum=1_200)
    ):
        return False
    parents = [
        item
        for item in raw_elements
        if isinstance(item, Mapping)
        and str(item.get("selector") or "").strip() == parent_selector
    ]
    if len(parents) != 1:
        return False
    parent = parents[0]
    parent_rect = parent.get("rect")
    return bool(
        str(parent.get("tag") or "").casefold() == "flt-semantics"
        and str(parent.get("role") or "").casefold() == "group"
        and str(parent.get("aria_label") or "").strip() == parent_label
        and parent.get("interactive") is True
        and isinstance(parent_rect, Mapping)
        and _bounded_number(parent_rect.get("x"), minimum=-2, maximum=2)
        and _bounded_number(parent_rect.get("y"), minimum=-2, maximum=2)
        and _bounded_number(
            parent_rect.get("width"),
            minimum=float(viewport_width) * 0.95,
            maximum=float(viewport_width) * 1.05,
        )
        and _bounded_number(
            parent_rect.get("height"),
            minimum=float(viewport_height) * 0.95,
            maximum=float(viewport_height) * 1.05,
        )
    )


def _bounded_number(value: object, *, minimum: float, maximum: float) -> bool:
    return bool(
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and minimum <= float(value) <= maximum
    )


def _normalized_tokens(value: object) -> set[str]:
    return (
        {str(item).strip().casefold() for item in value if str(item).strip()}
        if isinstance(value, list)
        else set()
    )
