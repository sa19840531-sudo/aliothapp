"""UIA 坐标窗口与截图坐标空间的回归。"""

from __future__ import annotations

import pytest

from services.api.app.application.app_surface_evidence import (
    find_unique_trusted_navigation_target_by_label,
    inspect_trusted_app_accessibility_controls,
    resolve_ui_tree_screen_size,
)


@pytest.mark.parametrize(
    ("hierarchy_size", "capture_size", "expected"),
    [
        # 根窗口裁掉底部系统栏，但 Capture 保留完整屏幕。
        ((1440, 2730), (1440, 3040), (1440, 3040)),
        # 同一帧的等比例缩略图仍应恢复到完整 Capture 坐标空间。
        ((1440, 2730), (720, 1520), (1440, 3040)),
        # 横屏根窗口裁剪和缩略图走同一套统一缩放。
        ((2730, 1440), (1520, 720), (3040, 1440)),
        # 根窗口已覆盖 Capture 时不应缩小 UIA 坐标空间。
        ((1920, 1080), (1280, 720), (1920, 1080)),
        # 没有根尺寸时保留 Capture 自身坐标空间。
        (None, (720, 1520), (720, 1520)),
    ],
)
def test_resolve_ui_tree_screen_size_preserves_uniform_coordinate_scale(
    hierarchy_size: tuple[int, int] | None,
    capture_size: tuple[int, int],
    expected: tuple[int, int],
) -> None:
    assert (
        resolve_ui_tree_screen_size(
            hierarchy_size,
            capture_width=capture_size[0],
            capture_height=capture_size[1],
        )
        == expected
    )


@pytest.mark.parametrize(
    ("hierarchy_size", "capture_size"),
    [
        ((0, 2730), (1440, 3040)),
        ((1440, 2730), (0, 3040)),
        ((1440, 2730), (1440, True)),
        ((1440, 2730), (1440.0, 3040)),
        ((1440, 2730), (1440, 3040)),
    ],
)
def test_resolve_ui_tree_screen_size_rejects_invalid_dimensions(
    hierarchy_size: object,
    capture_size: tuple[object, object],
) -> None:
    if hierarchy_size == (1440, 2730) and capture_size == (1440, 3040):
        hierarchy_size = [1440, 2730]
    with pytest.raises(ValueError):
        resolve_ui_tree_screen_size(
            hierarchy_size,  # type: ignore[arg-type]
            capture_width=capture_size[0],  # type: ignore[arg-type]
            capture_height=capture_size[1],  # type: ignore[arg-type]
        )


def test_09ff_like_navigation_uses_full_capture_height_for_normalized_bounds() -> None:
    xml = """<hierarchy rotation="0">
      <node bounds="[0,0][1440,2730]" package="uerma.zqeluk.cwsxx">
        <node package="uerma.zqeluk.cwsxx" content-desc="主页" clickable="true"
              enabled="true" bounds="[0,2669][268,2730]"/>
        <node package="uerma.zqeluk.cwsxx" content-desc="短剧" clickable="true"
              enabled="true" bounds="[268,2669][494,2730]"/>
        <node package="uerma.zqeluk.cwsxx" content-desc="暗网" clickable="true"
              enabled="true" bounds="[494,2669][720,2730]"/>
        <node package="uerma.zqeluk.cwsxx" content-desc="AI" clickable="true"
              enabled="true" bounds="[720,2669][946,2730]"/>
        <node package="uerma.zqeluk.cwsxx" content-desc="社区" clickable="true"
              enabled="true" bounds="[946,2669][1172,2730]"/>
        <node package="uerma.zqeluk.cwsxx" content-desc="我的" clickable="true"
              enabled="true" bounds="[1172,2669][1398,2730]"/>
      </node>
    </hierarchy>"""

    screen_width, screen_height = resolve_ui_tree_screen_size(
        (1440, 2730),
        capture_width=1440,
        capture_height=3040,
    )
    target = find_unique_trusted_navigation_target_by_label(
        xml,
        package_name="uerma.zqeluk.cwsxx",
        semantic_name="底栏「主页」",
        expected_page_type="home",
        screen_width=screen_width,
        screen_height=screen_height,
    )

    assert target is not None
    bounds = target["bounds"]
    normalized_top = int(bounds["top"]) / screen_height
    normalized_bottom = int(bounds["bottom"]) / screen_height
    assert normalized_top == pytest.approx(2669 / 3040)
    assert normalized_bottom == pytest.approx(2730 / 3040)
    assert normalized_bottom < 1


def test_bottom_navigation_control_threshold_remains_fail_closed_for_short_raw_nav() -> None:
    xml = """<hierarchy rotation="0">
      <node package="uerma.zqeluk.cwsxx" content-desc="主页" clickable="true"
            enabled="true" bounds="[0,2669][268,2730]"/>
      <node package="uerma.zqeluk.cwsxx" content-desc="短剧" clickable="true"
            enabled="true" bounds="[268,2669][494,2730]"/>
      <node package="uerma.zqeluk.cwsxx" content-desc="暗网" clickable="true"
            enabled="true" bounds="[494,2669][720,2730]"/>
      <node package="uerma.zqeluk.cwsxx" content-desc="AI" clickable="true"
            enabled="true" bounds="[720,2669][946,2730]"/>
      <node package="uerma.zqeluk.cwsxx" content-desc="社区" clickable="true"
            enabled="true" bounds="[946,2669][1172,2730]"/>
      <node package="uerma.zqeluk.cwsxx" content-desc="我的" clickable="true"
            enabled="true" bounds="[1172,2669][1398,2730]"/>
    </hierarchy>"""

    controls = inspect_trusted_app_accessibility_controls(
        xml,
        package_name="uerma.zqeluk.cwsxx",
        screen_width=1440,
        screen_height=3040,
    )

    assert controls is not None
    # 61 px / 3040 低于既有 3% 底栏高度门槛；坐标修复不放宽控件合同。
    assert controls["bottom_navigation"] == ()
