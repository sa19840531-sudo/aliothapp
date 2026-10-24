"""工作台设备库存接口（占用与排队观测，以及人工解除隔离）。"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from services.api.app.application.device_pool import DeviceInventoryItem
from services.api.app.core.errors import AliothError
from services.api.app.infrastructure.db.device_lease_repository import (
    PostgresDeviceLeaseRepository,
)

router = APIRouter(prefix="/devices")


class WorkstationDeviceItem(BaseModel):
    """设备下拉与状态条展示项。"""

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


class WorkstationDeviceListResponse(BaseModel):
    """设备库存列表响应。"""

    items: list[WorkstationDeviceItem]


@lru_cache
def _get_device_lease_repository() -> PostgresDeviceLeaseRepository:
    return PostgresDeviceLeaseRepository()


def _to_response_item(item: DeviceInventoryItem) -> WorkstationDeviceItem:
    return WorkstationDeviceItem(
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


@router.get("", response_model=WorkstationDeviceListResponse, summary="查询设备库存与占用状态")
async def list_workstation_devices(
    repository: Annotated[
        PostgresDeviceLeaseRepository, Depends(_get_device_lease_repository)
    ],
) -> WorkstationDeviceListResponse:
    items = await repository.list_inventory()
    return WorkstationDeviceListResponse(
        items=[_to_response_item(item) for item in items]
    )


@router.post(
    "/{device_ref:path}/release-quarantine",
    response_model=WorkstationDeviceItem,
    summary="人工解除设备隔离",
)
async def release_device_quarantine(
    device_ref: str,
    repository: Annotated[
        PostgresDeviceLeaseRepository, Depends(_get_device_lease_repository)
    ],
) -> WorkstationDeviceItem:
    """设备恢复健康后放回可用池。

    安装失败等故障会把设备标记为隔离，此前只有进入隔离的路径、没有退出路径，
    设备会一直占位。解除前须自行确认设备已恢复；仍有在用租约时拒绝解除。
    """
    items = {item.device_ref: item for item in await repository.list_inventory()}
    current = items.get(device_ref)
    if current is None:
        raise AliothError(message=f"设备不存在：{device_ref}", status_code=404)
    if current.health_state != "quarantined":
        raise AliothError(
            message=f"设备未处于隔离状态（当前 {current.health_state}）",
            status_code=409,
        )
    if not await repository.release_quarantine(device_ref=device_ref):
        raise AliothError(message="设备仍有在用租约，无法解除隔离", status_code=409)
    refreshed = {item.device_ref: item for item in await repository.list_inventory()}
    return _to_response_item(refreshed[device_ref])
