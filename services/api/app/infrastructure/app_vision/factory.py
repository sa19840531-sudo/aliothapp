"""按运行配置解析 App Screenshot Understanding Provider。"""

from __future__ import annotations

from services.api.app.application.app_screenshot_understanding import (
    ScreenshotUnderstandingProvider,
)
from services.api.app.application.app_workflow_dispatch import AppWorkflowNotConfiguredError
from services.api.app.core.config import Settings
from services.api.app.infrastructure.app_vision.fake import FakeAppVisionProvider
from services.api.app.infrastructure.app_vision.llm import LlmAppVisionProvider
from services.api.app.infrastructure.llm_gateway.client_factory import LLMConfigurationError


def resolve_app_vision_provider(settings: Settings) -> ScreenshotUnderstandingProvider:
    """根据 workflow.app_vision_provider 与 runtime 选择 Fake 或 LLM Provider。"""
    mode = settings.workflow.app_vision_provider
    environment = settings.environment.casefold()
    is_prod = environment in {"prod", "production"}
    runtime = settings.workflow.app_runtime_provider

    if mode == "auto":
        mode = "llm" if runtime == "official" else "fake"

    if mode == "fake":
        if is_prod and runtime == "official":
            raise AppWorkflowNotConfiguredError(
                "production Official App Workflow 禁止使用 Fake Screenshot Understanding Provider"
            )
        return FakeAppVisionProvider()

    if mode == "llm":
        try:
            return LlmAppVisionProvider(llm_settings=settings.llm)
        except (LLMConfigurationError, ValueError) as exc:
            raise AppWorkflowNotConfiguredError(
                f"App Screenshot Understanding LLM Provider 无法初始化：{exc}"
            ) from exc

    raise AppWorkflowNotConfiguredError(
        f"不受支持的 ALIOTH_APP_VISION_PROVIDER={mode!r}"
    )
