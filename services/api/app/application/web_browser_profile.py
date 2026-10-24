"""Web V2 桌面浏览器配置注册表。"""

from __future__ import annotations

from dataclasses import asdict, dataclass

DEFAULT_WEB_BROWSER_PROFILE_ID = "desktop_fhd_1920x1080_v1"


class WebBrowserProfileUnsupportedError(ValueError):
    """请求的 Web Browser Profile 未在服务端注册。"""


@dataclass(frozen=True, slots=True)
class WebBrowserProfile:
    """一次 Web Run 锁定的不可变桌面浏览器参数。"""

    profile_id: str
    viewport_width: int
    viewport_height: int
    screen_width: int
    screen_height: int
    device_scale_factor: float
    is_mobile: bool
    has_touch: bool
    color_scheme: str

    def to_dict(self) -> dict[str, object]:
        """返回可持久化到任务与 Run 快照的配置。"""

        return asdict(self)

    def to_action_parameters(self) -> dict[str, object]:
        """返回只允许 Runtime 注入的浏览器动作参数。"""

        return {
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "device_scale_factor": self.device_scale_factor,
            "is_mobile": self.is_mobile,
            "has_touch": self.has_touch,
            "color_scheme": self.color_scheme,
            "browser_profile_id": self.profile_id,
        }


class WebBrowserProfileRegistry:
    """解析受服务端控制的 Web Browser Profile。"""

    def __init__(self) -> None:
        profile = WebBrowserProfile(
            profile_id=DEFAULT_WEB_BROWSER_PROFILE_ID,
            viewport_width=1920,
            viewport_height=1080,
            screen_width=1920,
            screen_height=1080,
            device_scale_factor=1.0,
            is_mobile=False,
            has_touch=False,
            color_scheme="light",
        )
        self._profiles = {profile.profile_id: profile}

    def resolve(self, profile_id: str) -> WebBrowserProfile:
        """解析 profile；未知值必须失败，禁止回退 Playwright 默认配置。"""

        normalized = profile_id.strip() if isinstance(profile_id, str) else ""
        try:
            return self._profiles[normalized]
        except KeyError as exc:
            raise WebBrowserProfileUnsupportedError(
                f"WEB_PROFILE_UNSUPPORTED: {normalized or '<empty>'}"
            ) from exc

    def list_profiles(self) -> tuple[WebBrowserProfile, ...]:
        """按稳定 ID 顺序返回所有服务器注册 Profile。"""

        return tuple(self._profiles[key] for key in sorted(self._profiles))


__all__ = [
    "DEFAULT_WEB_BROWSER_PROFILE_ID",
    "WebBrowserProfile",
    "WebBrowserProfileRegistry",
    "WebBrowserProfileUnsupportedError",
]
