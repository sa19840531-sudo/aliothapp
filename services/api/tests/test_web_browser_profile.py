"""Web 桌面浏览器配置的稳定合同测试。"""

import pytest

from services.api.app.application.web_browser_profile import (
    DEFAULT_WEB_BROWSER_PROFILE_ID,
    WebBrowserProfileRegistry,
    WebBrowserProfileUnsupportedError,
)


def test_default_web_browser_profile_is_fixed_desktop_fhd() -> None:
    profile = WebBrowserProfileRegistry().resolve(DEFAULT_WEB_BROWSER_PROFILE_ID)

    assert profile.to_dict() == {
        "profile_id": "desktop_fhd_1920x1080_v1",
        "viewport_width": 1920,
        "viewport_height": 1080,
        "screen_width": 1920,
        "screen_height": 1080,
        "device_scale_factor": 1.0,
        "is_mobile": False,
        "has_touch": False,
        "color_scheme": "light",
    }
    assert profile.to_action_parameters() == {
        "viewport_width": 1920,
        "viewport_height": 1080,
        "screen_width": 1920,
        "screen_height": 1080,
        "device_scale_factor": 1.0,
        "is_mobile": False,
        "has_touch": False,
        "color_scheme": "light",
        "browser_profile_id": "desktop_fhd_1920x1080_v1",
    }


def test_unknown_web_browser_profile_fails_closed() -> None:
    with pytest.raises(
        WebBrowserProfileUnsupportedError,
        match="WEB_PROFILE_UNSUPPORTED",
    ):
        WebBrowserProfileRegistry().resolve("desktop_unknown")
