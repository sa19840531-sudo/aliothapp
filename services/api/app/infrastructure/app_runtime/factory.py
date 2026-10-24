"""App Runtime Provider 的显式组合根与生产安全门禁。"""

from __future__ import annotations

from dataclasses import dataclass

from services.api.app.core.config import WorkflowSettings
from services.api.app.domain.interaction import AppBootstrapPort, SurfaceDriver
from services.api.app.infrastructure.app_runtime.fake import FakeAppRuntime, FakeAppScenario
from services.api.app.infrastructure.app_runtime.official import OfficialAppRuntime
from services.api.app.infrastructure.app_runtime.official_api import OfficialAppApiClient


class AppRuntimeConfigurationError(RuntimeError):
    """Provider 未实现、未注册或不允许在当前环境使用。"""


@dataclass(frozen=True, kw_only=True)
class AppRuntimeBundle:
    """保证安装、打开和设备动作来自同一个 Runtime 会话域。"""

    bootstrap_port: AppBootstrapPort
    driver: SurfaceDriver


class AppRuntimeFactory:
    """只显式注册 Fake 与 Official Provider，Demo 应用不是 Provider。"""

    def __init__(
        self,
        *,
        environment: str,
        workflow_settings: WorkflowSettings | None = None,
    ) -> None:
        if (
            not isinstance(environment, str)
            or not environment.strip()
            or environment != environment.strip()
        ):
            raise ValueError("environment 必须是无首尾空白的非空字符串")
        self._environment = environment.casefold()
        self._workflow_settings = workflow_settings

    def build(
        self,
        provider: str,
        *,
        fake_scenario: FakeAppScenario | None = None,
        official_client: OfficialAppApiClient | None = None,
    ) -> AppRuntimeBundle:
        if provider == "fake":
            if self._environment in {"prod", "production"}:
                raise AppRuntimeConfigurationError(
                    "production 环境禁止启用 Fake App Runtime Provider"
                )
            runtime = FakeAppRuntime(scenario=fake_scenario)
            return AppRuntimeBundle(bootstrap_port=runtime, driver=runtime)
        if provider == "official":
            return self._build_official(official_client=official_client)
        raise AppRuntimeConfigurationError(
            f"App Runtime Provider 未注册：{provider or '<empty>'}"
        )

    def _build_official(
        self,
        *,
        official_client: OfficialAppApiClient | None,
    ) -> AppRuntimeBundle:
        if self._workflow_settings is None:
            raise AppRuntimeConfigurationError(
                "Official Adapter 尚未完成配置：缺少 WorkflowSettings"
            )
        try:
            self._workflow_settings.validate_official_app_configuration()
        except ValueError as error:
            raise AppRuntimeConfigurationError(str(error)) from error
        if official_client is None:
            raise AppRuntimeConfigurationError(
                "Official App Runtime 必须显式注入 OfficialAppApiClient"
            )
        required_methods = (
            "start_session",
            "reattach_session",
            "install",
            "open",
            "capture_png",
            "execute",
            "close",
        )
        missing = [
            method_name
            for method_name in required_methods
            if not callable(getattr(official_client, method_name, None))
        ]
        if missing:
            raise AppRuntimeConfigurationError(
                "OfficialAppApiClient 不满足最小协议：" + ", ".join(missing)
            )
        runtime = OfficialAppRuntime(
            api_client=official_client,
            max_screenshot_bytes=self._workflow_settings.official_app_max_screenshot_bytes,
        )
        return AppRuntimeBundle(bootstrap_port=runtime, driver=runtime)
