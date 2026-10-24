"""App Runtime Provider 组合根与生产门禁测试。"""

import pytest

from services.api.app.infrastructure.app_runtime.factory import (
    AppRuntimeConfigurationError,
    AppRuntimeFactory,
)
from services.api.app.infrastructure.app_runtime.fake import FakeAppRuntime


def test_fake_provider_builds_one_adapter_for_bootstrap_and_driver() -> None:
    bundle = AppRuntimeFactory(environment="local").build("fake")

    assert isinstance(bundle.bootstrap_port, FakeAppRuntime)
    assert bundle.bootstrap_port is bundle.driver


def test_fake_provider_is_rejected_in_production() -> None:
    with pytest.raises(AppRuntimeConfigurationError, match="production"):
        AppRuntimeFactory(environment="production").build("fake")


def test_official_provider_requires_explicit_configuration() -> None:
    with pytest.raises(AppRuntimeConfigurationError, match="WorkflowSettings"):
        AppRuntimeFactory(environment="local").build("official")


@pytest.mark.parametrize("provider", ["mobile_demo", "unknown", ""])
def test_demo_and_unknown_values_never_become_runtime_provider(provider: str) -> None:
    with pytest.raises(AppRuntimeConfigurationError, match="Provider"):
        AppRuntimeFactory(environment="local").build(provider)


def test_environment_must_be_explicit_nonempty_text() -> None:
    with pytest.raises(ValueError, match="environment"):
        AppRuntimeFactory(environment="").build("fake")
