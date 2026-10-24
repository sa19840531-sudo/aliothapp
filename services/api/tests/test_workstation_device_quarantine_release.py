"""设备人工解除隔离接口合同：安装失败等故障进入隔离后必须有退出通道。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.app.api.v1.workstation.devices import (
    _get_device_lease_repository,
)
from services.api.app.api.v1.workstation.devices import (
    router as devices_router,
)
from services.api.app.application.device_pool import DeviceInventoryItem
from services.api.app.core.errors import register_exception_handlers


def _item(device_ref: str, health_state: str) -> DeviceInventoryItem:
    return DeviceInventoryItem(
        device_ref=device_ref,
        backend="cloud_phone",
        profile_id=device_ref.split(":", 1)[-1],
        enabled=True,
        health_state=health_state,
        occupied=health_state not in {"available", "quarantined"},
        occupancy_status=health_state,
        morelogin_env_id="1721996994554962",
        queued_preferred_count=0,
    )


class _FakeRepo:
    def __init__(self, *, health_state: str, release_result: bool = True) -> None:
        self.health_state = health_state
        self.release_result = release_result
        self.released: list[str] = []

    async def list_inventory(self) -> list[DeviceInventoryItem]:
        return [_item("cloud_phone:qa-d", self.health_state)]

    async def release_quarantine(self, *, device_ref: str) -> bool:
        self.released.append(device_ref)
        if self.release_result:
            self.health_state = "available"
        return self.release_result


def _client(repo: _FakeRepo) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(devices_router)
    app.dependency_overrides[_get_device_lease_repository] = lambda: repo
    return TestClient(app)


def test_release_quarantine_returns_device_to_available() -> None:
    repo = _FakeRepo(health_state="quarantined")

    response = _client(repo).post(
        "/devices/cloud_phone:qa-d/release-quarantine"
    )

    assert response.status_code == 200
    assert response.json()["health_state"] == "available"
    assert repo.released == ["cloud_phone:qa-d"]


def test_release_quarantine_rejects_device_not_quarantined() -> None:
    repo = _FakeRepo(health_state="available")

    response = _client(repo).post(
        "/devices/cloud_phone:qa-d/release-quarantine"
    )

    assert response.status_code == 409
    assert repo.released == []


def test_release_quarantine_rejects_when_lease_still_active() -> None:
    """仍有在用租约时仓储拒绝，接口不能谎报解除成功。"""
    repo = _FakeRepo(health_state="quarantined", release_result=False)

    response = _client(repo).post(
        "/devices/cloud_phone:qa-d/release-quarantine"
    )

    assert response.status_code == 409
    assert repo.released == ["cloud_phone:qa-d"]


def test_release_quarantine_returns_404_for_unknown_device() -> None:
    repo = _FakeRepo(health_state="quarantined")

    response = _client(repo).post("/devices/cloud_phone:not-there/release-quarantine")

    assert response.status_code == 404
    assert repo.released == []
