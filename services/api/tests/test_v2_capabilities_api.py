"""V2 平台能力声明接口测试。"""

from fastapi.testclient import TestClient

from services.api.app.core.config import Settings, get_settings
from services.api.app.main import create_app


def _web_settings(*, mode: str, roles: list[str]) -> Settings:
    """构造 Web mode 和 Worker role 的最小能力配置。"""

    return Settings.model_validate(
        {
            "workflow": {"web_v2_mode": mode},
            "worker": {"roles": roles},
        }
    )


def test_web_capability_requires_mode_and_worker_role() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: _web_settings(
        mode="exploration_only",
        roles=["explorer", "judge", "web_explorer"],
    )

    response = TestClient(app).get("/api/v2/capabilities")

    assert response.status_code == 200
    payload = response.json()
    web = next(item for item in payload["platforms"] if item["platform"] == "web")
    assert web["enabled"] is True
    assert web["execution_modes"] == ["exploration_only"]
    assert web["unavailable_reasons"] == []
    assert web["browser_profiles"] == [
        {
            "profile_id": "desktop_fhd_1920x1080_v1",
            "viewport_width": 1920,
            "viewport_height": 1080,
            "device_scale_factor": 1.0,
            "is_mobile": False,
            "has_touch": False,
        }
    ]


def test_web_capability_fails_closed_without_dedicated_worker_role() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: _web_settings(
        mode="on",
        roles=["explorer", "judge"],
    )

    response = TestClient(app).get("/api/v2/capabilities")

    assert response.status_code == 200
    web = next(item for item in response.json()["platforms"] if item["platform"] == "web")
    assert web["enabled"] is False
    assert web["execution_modes"] == []
    assert web["unavailable_reasons"] == ["ALIOTH_WORKER_ROLES 未包含 web_explorer"]


def test_web_on_capability_exposes_full_inspection_and_exploration() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: _web_settings(
        mode="on",
        roles=["explorer", "judge", "web_explorer"],
    )

    response = TestClient(app).get("/api/v2/capabilities")

    assert response.status_code == 200
    web = next(item for item in response.json()["platforms"] if item["platform"] == "web")
    assert web["enabled"] is True
    assert web["configured_mode"] == "on"
    assert web["execution_modes"] == ["full_inspection", "exploration_only"]


def test_web_capability_reports_off_mode_without_publishing_execution_modes() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: _web_settings(
        mode="off",
        roles=["explorer", "judge", "web_explorer"],
    )

    response = TestClient(app).get("/api/v2/capabilities")

    web = next(item for item in response.json()["platforms"] if item["platform"] == "web")
    assert web["enabled"] is False
    assert web["execution_modes"] == []
    assert web["unavailable_reasons"] == ["ALIOTH_WEB_V2_MODE 未开启"]


def test_web_capability_withholds_full_inspection_without_judge() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings.model_validate(
        {
            "workflow": {"web_v2_mode": "on"},
            "worker": {"roles": ["explorer", "web_explorer"]},
        }
    )

    response = TestClient(app).get("/api/v2/capabilities")

    web = next(item for item in response.json()["platforms"] if item["platform"] == "web")
    assert web["enabled"] is True
    assert web["execution_modes"] == ["exploration_only"]
    assert web["unavailable_reasons"] == [
        "Web 完整质检要求 ALIOTH_WORKER_ROLES 包含 judge"
    ]


def test_h5_and_app_capability_projections_remain_unchanged_when_web_is_ready() -> None:
    app = create_app()
    settings = _web_settings(
        mode="on",
        roles=["explorer", "judge", "web_explorer"],
    )
    settings.workflow.h5_v2_mode = "on"
    settings.workflow.app_qc_enabled = True
    app.dependency_overrides[get_settings] = lambda: settings

    response = TestClient(app).get("/api/v2/capabilities")

    platforms = {item["platform"]: item for item in response.json()["platforms"]}
    assert platforms["h5"] == {
        "platform": "h5",
        "enabled": True,
        "configured_mode": "on",
        "execution_modes": ["full_inspection", "exploration_only"],
        "unavailable_reasons": [],
        "browser_profiles": [],
    }
    assert platforms["app"] == {
        "platform": "app",
        "enabled": True,
        "configured_mode": "on",
        "execution_modes": ["full_inspection", "exploration_only"],
        "unavailable_reasons": [],
        "browser_profiles": [],
    }
