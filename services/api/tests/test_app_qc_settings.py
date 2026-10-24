import pytest

from services.api.app.core.config import WorkflowSettings, get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_workflow_settings_read_app_qc_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_WORKFLOW_VERSION", "v2")
    monkeypatch.setenv("ALIOTH_APP_QC_ENABLED", "true")
    monkeypatch.setenv("ALIOTH_APP_RUNTIME_PROVIDER", "official")
    monkeypatch.setenv("ALIOTH_APP_VISION_PROVIDER", "llm")
    monkeypatch.setenv("ALIOTH_APP_DEVICE_LEASE_BACKEND", "postgres")
    monkeypatch.setenv("ALIOTH_MOBILE_DEMO_SMOKE_ENABLED", "true")
    monkeypatch.setenv("ALIOTH_H5_V2_MODE", "shadow")
    monkeypatch.setenv("ALIOTH_WEB_V2_MODE", "exploration_only")

    workflow = get_settings().workflow

    assert workflow.version == "v2"
    assert workflow.app_qc_enabled is True
    assert workflow.app_runtime_provider == "official"
    assert workflow.app_vision_provider == "llm"
    assert workflow.app_device_lease_backend == "postgres"
    assert workflow.mobile_demo_smoke_enabled is True
    assert workflow.h5_v2_mode == "shadow"
    assert workflow.web_v2_mode == "exploration_only"


def test_workflow_settings_fallback_unknown_h5_v2_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_H5_V2_MODE", "future")

    assert get_settings().workflow.h5_v2_mode == "off"


def test_workflow_settings_fallback_unknown_web_v2_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_WEB_V2_MODE", "future")

    assert get_settings().workflow.web_v2_mode == "off"


def test_workflow_settings_fallback_unknown_vision_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_APP_VISION_PROVIDER", "magic")

    assert get_settings().workflow.app_vision_provider == "auto"


def test_workflow_settings_fallback_unknown_device_lease_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_APP_DEVICE_LEASE_BACKEND", "magic")

    assert get_settings().workflow.app_device_lease_backend == "in_process"


def test_workflow_settings_read_official_app_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # API_BASE_URL 别名优先；清掉以免被本地 .env 抢占 BASE_URL 断言
    monkeypatch.delenv("ALIOTH_OFFICIAL_APP_API_BASE_URL", raising=False)
    monkeypatch.setenv("ALIOTH_OFFICIAL_APP_BASE_URL", " https://app.example.test/ ")
    monkeypatch.setenv("ALIOTH_OFFICIAL_APP_TIMEOUT_SECONDS", "45.5")
    monkeypatch.setenv("ALIOTH_OFFICIAL_APP_CAPTURE_TIMEOUT_SECONDS", "180.0")
    monkeypatch.setenv("ALIOTH_OFFICIAL_APP_MAX_SCREENSHOT_BYTES", "1048576")

    workflow = get_settings().workflow

    assert workflow.official_app_base_url == "https://app.example.test/"
    assert workflow.official_app_timeout_seconds == 45.5
    assert workflow.official_app_capture_timeout_seconds == 180.0
    assert workflow.official_app_max_screenshot_bytes == 1_048_576


def test_workflow_settings_accepts_star_api_base_url_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_OFFICIAL_APP_API_BASE_URL", " https://star.example.test/ ")

    workflow = get_settings().workflow

    assert workflow.official_app_base_url == "https://star.example.test/"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ALIOTH_OFFICIAL_APP_TIMEOUT_SECONDS", "0"),
        ("ALIOTH_OFFICIAL_APP_TIMEOUT_SECONDS", "inf"),
        ("ALIOTH_OFFICIAL_APP_TIMEOUT_SECONDS", "nan"),
        ("ALIOTH_OFFICIAL_APP_MAX_SCREENSHOT_BYTES", "0"),
    ],
)
def test_workflow_settings_reject_invalid_official_app_limits(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        get_settings()


def test_workflow_settings_reject_mobile_demo_as_runtime_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_WORKFLOW_VERSION", "future")
    monkeypatch.setenv("ALIOTH_APP_RUNTIME_PROVIDER", "mobile_demo")

    workflow = get_settings().workflow

    assert workflow.version == "v1"
    assert workflow.app_runtime_provider == "fake"


def test_workflow_settings_reject_enabled_fake_provider_in_production() -> None:
    workflow = WorkflowSettings(
        version="v2",
        app_qc_enabled=True,
        app_runtime_provider="fake",
    )

    with pytest.raises(ValueError, match="production"):
        workflow.validate_runtime_environment("production")


def test_disabled_app_keeps_safe_defaults_in_production() -> None:
    workflow = WorkflowSettings()

    workflow.validate_runtime_environment("production")


def test_enabled_app_rejects_in_process_device_lease_in_production() -> None:
    workflow = WorkflowSettings(
        version="v2",
        app_qc_enabled=True,
        app_runtime_provider="official",
        app_device_lease_backend="in_process",
    )

    with pytest.raises(ValueError, match="PostgreSQL App 设备租约"):
        workflow.validate_runtime_environment("production")
