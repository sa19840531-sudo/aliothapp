"""V2 设备库存只读接口。"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from services.api.app.application.device_pool import DeviceInventoryItem
from services.api.app.infrastructure.db.device_lease_repository import (
    PostgresDeviceLeaseRepository,
)

router = APIRouter(prefix="/devices")


class DeviceInventoryItemResponse(BaseModel):
    """设备库存项：占用与偏好排队观测。"""

    device_ref: str
    backend: str
    profile_id: str
    enabled: bool
    health_state: str
    occupied: bool
    occupancy_status: str = Field(
        description="available|occupied|recovering|quarantined|disabled"
    )
    lease_task_id: str | None = None
    lease_session_id: str | None = None
    lease_state: str | None = None
    lease_expires_at: str | None = None
    morelogin_env_id: str = ""
    queued_preferred_count: int = 0


class DeviceInventoryListResponse(BaseModel):
    """设备库存列表。"""

    items: list[DeviceInventoryItemResponse]


@lru_cache
def _get_device_lease_repository() -> PostgresDeviceLeaseRepository:
    return PostgresDeviceLeaseRepository()


def _to_response_item(item: DeviceInventoryItem) -> DeviceInventoryItemResponse:
    return DeviceInventoryItemResponse(
        device_ref=item.device_ref,
        backend=item.backend,
        profile_id=item.profile_id,
        enabled=item.enabled,
        health_state=item.health_state,
        occupied=item.occupied,
        occupancy_status=item.occupancy_status,
        lease_task_id=item.lease_task_id,
        lease_session_id=item.lease_session_id,
        lease_state=item.lease_state,
        lease_expires_at=(
            item.lease_expires_at.isoformat() if item.lease_expires_at is not None else None
        ),
        morelogin_env_id=item.morelogin_env_id,
        queued_preferred_count=item.queued_preferred_count,
    )


@router.get(
    "",
    response_model=DeviceInventoryListResponse,
    summary="查询设备库存与占用状态",
)
async def list_devices(
    repository: Annotated[
        PostgresDeviceLeaseRepository, Depends(_get_device_lease_repository)
    ],
) -> DeviceInventoryListResponse:
    """列出 `qc_device_resource` 库存，并附带活跃租约与偏好排队观测。"""
    items = await repository.list_inventory()
    return DeviceInventoryListResponse(items=[_to_response_item(item) for item in items])
