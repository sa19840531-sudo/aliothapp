"""V2 Web 能力接口只暴露实际可用的探索模式与注册 Profile。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.app.api.v2.capabilities import router
from services.api.app.core.config import Settings, get_settings


def _client(mode: str) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v2")
    app.dependency_overrides[get_settings] = lambda: Settings.model_validate(
        {
            "workflow": {"web_v2_mode": mode},
            "worker": {"roles": ["explorer", "judge", "web_explorer"]},
        }
    )
    return TestClient(app)


def test_web_capability_is_disabled_by_default() -> None:
    response = _client("off").get("/api/v2/capabilities")

    assert response.status_code == 200
    assert response.json()["web"] == {
        "enabled": False,
        "execution_modes": [],
        "default_browser_profile_id": "desktop_fhd_1920x1080_v1",
        "browser_profiles": [
            {
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
        ],
    }


def test_web_capability_advertises_configured_execution_modes() -> None:
    for mode, expected in (
        ("exploration_only", ["exploration_only"]),
        ("on", ["full_inspection", "exploration_only"]),
    ):
        response = _client(mode).get("/api/v2/capabilities")

        assert response.status_code == 200
        assert response.json()["web"]["enabled"] is True
        assert response.json()["web"]["execution_modes"] == expected
