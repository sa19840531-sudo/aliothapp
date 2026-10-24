"""H5 完整导航布局的上下文页型合同。"""

from __future__ import annotations

_BOTTOM_NAVIGATION_LAYOUTS = {
    (
        "精选",
        "二次元",
        "禁区",
        "ai生成",
        "社区",
        "我的",
    ): (
        "home",
        "content_list",
        "video_list",
        "content_list",
        "post_list",
        "profile",
    ),
    (
        "首页",
        "内容",
        "视频",
        "我的",
    ): (
        "home",
        "content_list",
        "video_list",
        "profile",
    ),
}


def h5_contextual_bottom_navigation_page_type(
    *,
    labels: tuple[str, ...],
    index: int,
) -> str | None:
    """完整底栏结构匹配时覆盖单标签全局别名。"""
    normalized_labels = tuple("".join(label.split()).casefold() for label in labels)
    page_types = _BOTTOM_NAVIGATION_LAYOUTS.get(normalized_labels)
    if page_types is None:
        return None
    if index < 1 or index > len(page_types):
        return None
    return page_types[index - 1]
