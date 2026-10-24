"""V2 设备库存列表接口合同。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.app.api.v2.devices import (
    _get_device_lease_repository,
)
from services.api.app.api.v2.devices import (
    router as devices_router,
)
from services.api.app.application.device_pool import DeviceInventoryItem


class _FakeRepo:
    async def list_inventory(self) -> list[DeviceInventoryItem]:
        return [
            DeviceInventoryItem(
                device_ref="cloud_phone:qa-c",
                backend="cloud_phone",
                profile_id="qa-c",
                enabled=True,
                health_state="available",
                occupied=False,
                occupancy_status="available",
                morelogin_env_id="1721455845687143",
                queued_preferred_count=0,
            ),
            DeviceInventoryItem(
                device_ref="cloud_phone:qa-a",
                backend="cloud_phone",
                profile_id="qa-a",
                enabled=True,
                health_state="available",
                occupied=True,
                occupancy_status="occupied",
                lease_task_id="11111111-1111-1111-1111-111111111111",
                lease_session_id="session-a",
                lease_state="active",
                lease_expires_at=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
                morelogin_env_id="1716966721435348",
                queued_preferred_count=2,
            ),
        ]


def test_list_devices_returns_inventory() -> None:
    app = FastAPI()
    app.include_router(devices_router, prefix="/api/v2")
    _get_device_lease_repository.cache_clear()
    app.dependency_overrides[_get_device_lease_repository] = lambda: _FakeRepo()
    client = TestClient(app)

    response = client.get("/api/v2/devices")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 2
    by_profile = {item["profile_id"]: item for item in payload["items"]}
    assert by_profile["qa-c"]["occupied"] is False
    assert by_profile["qa-c"]["occupancy_status"] == "available"
    assert by_profile["qa-a"]["occupied"] is True
    assert by_profile["qa-a"]["queued_preferred_count"] == 2
    assert by_profile["qa-a"]["morelogin_env_id"] == "1716966721435348"
