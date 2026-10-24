"""桌面 Web 任务使用的固定浏览器画像。"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_WEB_BROWSER_PROFILE_ID = "desktop_fhd_1920x1080_v1"


class WebBrowserProfileNotFoundError(ValueError):
    """任务引用了平台未注册的 Web 浏览器画像。"""


@dataclass(frozen=True)
class WebBrowserProfile:
    """Web 探索可审计、可复现的浏览器上下文参数。"""

    profile_id: str
    viewport_width: int
    viewport_height: int
    screen_width: int
    screen_height: int
    device_scale_factor: float
    is_mobile: bool
    has_touch: bool

    def page_capture_parameters(self) -> dict[str, object]:
        """返回浏览器采集层可直接消费的固定参数。"""
        return {
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "device_scale_factor": self.device_scale_factor,
            "is_mobile": self.is_mobile,
            "has_touch": self.has_touch,
        }


_WEB_BROWSER_PROFILES = {
    DEFAULT_WEB_BROWSER_PROFILE_ID: WebBrowserProfile(
        profile_id=DEFAULT_WEB_BROWSER_PROFILE_ID,
        viewport_width=1920,
        viewport_height=1080,
        screen_width=1920,
        screen_height=1080,
        device_scale_factor=1.0,
        is_mobile=False,
        has_touch=False,
    )
}


def get_web_browser_profile(profile_id: str) -> WebBrowserProfile:
    """按稳定 ID 返回浏览器画像，未知画像必须失败关闭。"""
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise WebBrowserProfileNotFoundError("browser_profile_id 不能为空")
    normalized = profile_id.strip()
    try:
        return _WEB_BROWSER_PROFILES[normalized]
    except KeyError as exc:
        raise WebBrowserProfileNotFoundError(
            f"Web browser_profile_id 未注册：{normalized}"
        ) from exc
