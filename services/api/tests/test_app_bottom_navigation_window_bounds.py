"""App UIA 底栏在全屏与裁剪应用窗口下的几何回归。"""

from __future__ import annotations

import pytest

from services.api.app.application.app_surface_evidence import (
    inspect_trusted_app_accessibility_controls,
)

_PACKAGE = "uerma.zqeluk.cwsxx"
_NAVIGATION = (
    ("主页", 0, 268),
    ("短剧", 268, 494),
    ("暗网", 494, 720),
    ("AI", 720, 946),
    ("社区", 946, 1172),
    ("我的", 1172, 1398),
)


def _hierarchy(*, root_height: int, nav_top: int, nav_bottom: int, foreign: bool = False) -> str:
    package = "com.other.foreign" if foreign else _PACKAGE
    navigation = "\n".join(
        f'<node package="{package}" content-desc="{label}" '
        f'clickable="true" enabled="true" '
        f'bounds="[{left},{nav_top}][{right},{nav_bottom}]"/>'
        for label, left, right in _NAVIGATION
    )
    return f'''<hierarchy rotation="0">
      <node package="{_PACKAGE}" bounds="[0,0][1440,{root_height}]">
        {navigation}
      </node>
    </hierarchy>'''


@pytest.mark.parametrize(
    ("root_height", "nav_top", "nav_bottom"),
    [
        (3040, 2979, 3040),
        (2730, 2669, 2730),
    ],
    ids=["fullscreen-window", "cropped-app-window"],
)
def test_bottom_navigation_uses_app_window_bottom_without_rescaling_raw_bounds(
    root_height: int,
    nav_top: int,
    nav_bottom: int,
) -> None:
    controls = inspect_trusted_app_accessibility_controls(
        _hierarchy(root_height=root_height, nav_top=nav_top, nav_bottom=nav_bottom),
        package_name=_PACKAGE,
        screen_width=1440,
        screen_height=3040,
    )

    assert controls is not None
    navigation = controls["bottom_navigation"]
    assert [item["label"] for item in navigation] == [item[0] for item in _NAVIGATION]
    assert navigation[3]["bounds"] == {
        "left": 720,
        "top": nav_top,
        "right": 946,
        "bottom": nav_bottom,
        "width": 226,
        "height": nav_bottom - nav_top,
        "signature": f"720,{nav_top},946,{nav_bottom}",
    }


def test_cropped_app_window_rejects_body_chip_row_below_navigation_contract() -> None:
    body_chips = "\n".join(
        f'<node package="{_PACKAGE}" content-desc="{label}" '
        f'clickable="true" enabled="true" bounds="[{left},2450][{right},2520]"/>'
        for label, left, right in (
            ("推荐", 0, 240),
            ("最新", 240, 480),
            ("热门", 480, 720),
            ("综合", 720, 960),
            ("关注", 960, 1200),
            ("活动", 1200, 1440),
        )
    )
    xml = _hierarchy(root_height=2730, nav_top=2669, nav_bottom=2730).replace(
        "      </node>", f"        {body_chips}\n      </node>", 1
    )

    controls = inspect_trusted_app_accessibility_controls(
        xml,
        package_name=_PACKAGE,
        screen_width=1440,
        screen_height=3040,
    )

    assert controls is not None
    assert [item["label"] for item in controls["bottom_navigation"]] == [
        item[0] for item in _NAVIGATION
    ]


def test_small_app_window_does_not_promote_lower_body_chip_row_to_navigation() -> None:
    chips = "\n".join(
        f'<node package="{_PACKAGE}" content-desc="{label}" '
        f'clickable="true" enabled="true" bounds="[{left},2150][{right},2220]"/>'
        for label, left, right in (
            ("推荐", 0, 240),
            ("最新", 240, 480),
            ("热门", 480, 720),
            ("综合", 720, 960),
            ("关注", 960, 1200),
            ("活动", 1200, 1440),
        )
    )
    xml = f'''<hierarchy rotation="0">
      <node package="{_PACKAGE}" bounds="[0,0][1440,2300]">
        {chips}
      </node>
    </hierarchy>'''

    controls = inspect_trusted_app_accessibility_controls(
        xml,
        package_name=_PACKAGE,
        screen_width=1440,
        screen_height=3040,
    )

    assert controls is not None
    assert controls["bottom_navigation"] == ()


def test_foreign_package_navigation_is_ignored_even_with_target_window_root() -> None:
    controls = inspect_trusted_app_accessibility_controls(
        _hierarchy(root_height=2730, nav_top=2669, nav_bottom=2730, foreign=True),
        package_name=_PACKAGE,
        screen_width=1440,
        screen_height=3040,
    )

    assert controls is not None
    assert controls["bottom_navigation"] == ()
