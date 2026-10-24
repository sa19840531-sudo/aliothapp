"""H5 完整底栏布局上下文映射测试。"""

from services.api.app.application.h5_navigation_layout_contract import (
    h5_contextual_bottom_navigation_page_type,
)


def test_four_tab_content_label_maps_only_with_complete_layout() -> None:
    labels = ("首页", "内容", "视频", "我的")

    assert h5_contextual_bottom_navigation_page_type(labels=labels, index=1) == "home"
    assert (
        h5_contextual_bottom_navigation_page_type(labels=labels, index=2)
        == "content_list"
    )
    assert (
        h5_contextual_bottom_navigation_page_type(labels=labels, index=3)
        == "video_list"
    )
    assert h5_contextual_bottom_navigation_page_type(labels=labels, index=4) == "profile"


def test_ambiguous_content_label_is_not_mapped_without_complete_layout() -> None:
    assert (
        h5_contextual_bottom_navigation_page_type(
            labels=("首页", "内容", "我的"),
            index=2,
        )
        is None
    )


def test_contextual_layout_rejects_out_of_range_index() -> None:
    assert (
        h5_contextual_bottom_navigation_page_type(
            labels=("首页", "内容", "视频", "我的"),
            index=5,
        )
        is None
    )
