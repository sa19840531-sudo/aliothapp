"""V2 客户端只读能力发现接口。"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from services.api.app.application.web_browser_profile import (
    DEFAULT_WEB_BROWSER_PROFILE_ID,
    WebBrowserProfileRegistry,
)
from services.api.app.core.config import Settings, get_settings

router = APIRouter()


class BrowserProfileCapability(BaseModel):
    """控制面公开的最小可复现浏览器画像。"""

    profile_id: str
    viewport_width: int
    viewport_height: int
    device_scale_factor: float
    is_mobile: bool
    has_touch: bool


class WebBrowserProfileCapability(BaseModel):
    profile_id: str
    viewport_width: int
    viewport_height: int
    screen_width: int
    screen_height: int
    device_scale_factor: float
    is_mobile: bool
    has_touch: bool
    color_scheme: str


class WebV2Capability(BaseModel):
    enabled: bool
    execution_modes: list[Literal["exploration_only", "full_inspection"]]
    default_browser_profile_id: str
    browser_profiles: list[WebBrowserProfileCapability]


class PlatformCapability(BaseModel):
    """单个平台当前可提交的运行能力。"""

    platform: Literal["h5", "web", "app"]
    enabled: bool
    configured_mode: str
    execution_modes: list[Literal["full_inspection", "exploration_only"]]
    unavailable_reasons: list[str]
    browser_profiles: list[BrowserProfileCapability]


class V2CapabilitiesResponse(BaseModel):
    schema_version: Literal["v2-capabilities.1"] = "v2-capabilities.1"
    platforms: list[PlatformCapability]
    # 兼容已发布的 Web V2 表单；新消费者优先读取 platforms。
    web: WebV2Capability


@router.get(
    "/capabilities",
    response_model=V2CapabilitiesResponse,
    summary="读取 V2 平台能力和服务器注册 Profile",
)
async def get_v2_capabilities(
    settings: Annotated[Settings, Depends(get_settings)],
) -> V2CapabilitiesResponse:
    registry = WebBrowserProfileRegistry()
    profiles = registry.list_profiles()
    web_reasons: list[str] = []
    if settings.workflow.web_v2_mode == "off":
        web_reasons.append("ALIOTH_WEB_V2_MODE 未开启")
    if "web_explorer" not in settings.worker.roles:
        web_reasons.append("ALIOTH_WORKER_ROLES 未包含 web_explorer")
    web_enabled = (
        settings.workflow.web_v2_mode != "off"
        and "web_explorer" in settings.worker.roles
    )
    execution_modes: list[Literal["full_inspection", "exploration_only"]] = []
    if web_enabled:
        if settings.workflow.web_v2_mode == "on" and "judge" in settings.worker.roles:
            execution_modes.append("full_inspection")
        execution_modes.append("exploration_only")
    if settings.workflow.web_v2_mode == "on" and "judge" not in settings.worker.roles:
        web_reasons.append("Web 完整质检要求 ALIOTH_WORKER_ROLES 包含 judge")
    compact_profiles = [
        BrowserProfileCapability(
            profile_id=profile.profile_id,
            viewport_width=profile.viewport_width,
            viewport_height=profile.viewport_height,
            device_scale_factor=profile.device_scale_factor,
            is_mobile=profile.is_mobile,
            has_touch=profile.has_touch,
        )
        for profile in profiles
    ]
    h5_enabled = settings.workflow.h5_v2_mode == "on"
    app_enabled = settings.workflow.app_qc_enabled
    return V2CapabilitiesResponse(
        platforms=[
            PlatformCapability(
                platform="h5",
                enabled=h5_enabled,
                configured_mode=settings.workflow.h5_v2_mode,
                execution_modes=(
                    ["full_inspection", "exploration_only"] if h5_enabled else []
                ),
                unavailable_reasons=(
                    [] if h5_enabled else ["ALIOTH_H5_V2_MODE 未开启"]
                ),
                browser_profiles=[],
            ),
            PlatformCapability(
                platform="web",
                enabled=web_enabled,
                configured_mode=settings.workflow.web_v2_mode,
                execution_modes=execution_modes,
                unavailable_reasons=web_reasons,
                browser_profiles=compact_profiles,
            ),
            PlatformCapability(
                platform="app",
                enabled=app_enabled,
                configured_mode="on" if app_enabled else "off",
                execution_modes=(
                    ["full_inspection", "exploration_only"] if app_enabled else []
                ),
                unavailable_reasons=(
                    [] if app_enabled else ["ALIOTH_APP_QC_ENABLED 未开启"]
                ),
                browser_profiles=[],
            ),
        ],
        web=WebV2Capability(
            enabled=web_enabled,
            execution_modes=execution_modes,
            default_browser_profile_id=DEFAULT_WEB_BROWSER_PROFILE_ID,
            browser_profiles=[
                WebBrowserProfileCapability.model_validate(profile.to_dict())
                for profile in profiles
            ],
        )
    )


__all__ = ["V2CapabilitiesResponse", "get_v2_capabilities", "router"]
