"""公司（租户）管理接口。

只有总部（平台管理员）能新建、改名、停用和删除公司；无关联公司才允许删除，
有业务或审计关联时明确提示并保留数据。体验中心承接历史数据，不能停用或删除。
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import lru_cache
from secrets import token_hex
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from services.api.app.api.v1.auth import AuthUserResponse, hash_password, require_auth_user
from services.api.app.core.errors import AliothError
from services.api.app.infrastructure.db.tenant_repository import (
    DEFAULT_TENANT_ID,
    TENANT_KEY_UNIQUE_CONSTRAINT,
    TENANT_NAME_UNIQUE_INDEX,
    PostgresTenantRepository,
)
from services.api.app.infrastructure.db.user_repository import PostgresUserRepository

router = APIRouter()
logger = logging.getLogger(__name__)
_AUTOMATIC_KEY_ATTEMPTS = 5


class TenantPayload(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=128)
    tenant_key: str | None = Field(
        default=None,
        min_length=2,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    contact_name: str | None = Field(default=None, max_length=128)
    remark: str | None = Field(default=None, max_length=2000)
    # 原型「新增分公司」顺手建总管理员账号：填了账号就必须给初始密码。
    admin_username: str | None = Field(default=None, min_length=1, max_length=64)
    admin_full_name: str | None = Field(default=None, max_length=128)
    admin_password: str | None = Field(default=None, min_length=6, max_length=128)


class TenantUpdatePayload(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=128)
    status: Literal["enabled", "disabled"]
    contact_name: str | None = Field(default=None, max_length=128)
    remark: str | None = Field(default=None, max_length=2000)


class TenantResponse(BaseModel):
    id: str
    tenant_key: str
    display_name: str
    status: str
    contact_name: str | None = None
    remark: str | None = None
    admin_username: str | None = None
    user_count: int = 0
    department_count: int = 0
    product_count: int = 0
    is_headquarters: bool = False
    created_at: datetime
    updated_at: datetime


class TenantListResponse(BaseModel):
    items: list[TenantResponse]


@lru_cache
def _get_tenant_repository() -> PostgresTenantRepository:
    return PostgresTenantRepository()


@lru_cache
def _get_user_repository() -> PostgresUserRepository:
    return PostgresUserRepository()


async def _require_tenants_permission(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    if "tenants" not in current_user.permissions:
        raise AliothError(message="无权限访问该资源", status_code=403)
    return current_user


def _to_response(row: dict[str, object]) -> TenantResponse:
    return TenantResponse(
        id=str(row["id"]),
        tenant_key=str(row["tenant_key"]),
        display_name=str(row["display_name"]),
        status=str(row["status"]),
        contact_name=(str(row["contact_name"]) if row.get("contact_name") else None),
        remark=(str(row["remark"]) if row.get("remark") else None),
        admin_username=(str(row["admin_username"]) if row.get("admin_username") else None),
        user_count=int(row.get("user_count") or 0),
        department_count=int(row.get("department_count") or 0),
        product_count=int(row.get("product_count") or 0),
        is_headquarters=str(row["id"]) == DEFAULT_TENANT_ID,
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


@router.get("/tenants", response_model=TenantListResponse, summary="查询公司列表")
async def list_tenants(
    _: Annotated[AuthUserResponse, Depends(_require_tenants_permission)],
    repository: Annotated[PostgresTenantRepository, Depends(_get_tenant_repository)],
) -> TenantListResponse:
    rows = await repository.list_tenants()
    return TenantListResponse(items=[_to_response(row) for row in rows])


@router.post("/tenants", response_model=TenantResponse, status_code=201, summary="新建公司")
async def create_tenant(
    payload: TenantPayload,
    _: Annotated[AuthUserResponse, Depends(_require_tenants_permission)],
    repository: Annotated[PostgresTenantRepository, Depends(_get_tenant_repository)],
    user_repository: Annotated[PostgresUserRepository, Depends(_get_user_repository)],
) -> TenantResponse:
    display_name = payload.display_name.strip()
    if not display_name:
        raise AliothError(message="公司名称不能为空")
    if await repository.find_by_display_name(display_name) is not None:
        raise AliothError(message="公司名称已存在", status_code=409)
    admin_username = (payload.admin_username or "").strip()
    if admin_username:
        if not payload.admin_password:
            raise AliothError(message="请为总管理员账号设置初始密码")
        if await user_repository.find_by_username(admin_username) is not None:
            raise AliothError(message="总管理员账号已存在", status_code=409)
    # 自动编码碰撞时重试；数据库名称唯一索引兜住并发新增和改名。
    for attempt in range(_AUTOMATIC_KEY_ATTEMPTS):
        tenant_key = payload.tenant_key or f"co-{token_hex(4)}"
        try:
            row = await repository.create_tenant(
                {
                    "tenant_key": tenant_key,
                    "display_name": display_name,
                    "contact_name": (payload.contact_name or "").strip() or None,
                    "remark": (payload.remark or "").strip() or None,
                }
            )
            break
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == TENANT_NAME_UNIQUE_INDEX:
                raise AliothError(message="公司名称已存在", status_code=409) from exc
            if exc.constraint_name != TENANT_KEY_UNIQUE_CONSTRAINT:
                raise AliothError(message="公司信息与已有记录冲突", status_code=409) from exc
            if payload.tenant_key:
                raise AliothError(message="公司编码已存在", status_code=409) from exc
            if attempt == _AUTOMATIC_KEY_ATTEMPTS - 1:
                raise AliothError(message="公司编码生成失败，请稍后重试", status_code=503) from exc
    if admin_username:
        await user_repository.create_user(
            {
                "username": admin_username,
                "full_name": (payload.admin_full_name or "").strip() or admin_username,
                "password_hash": hash_password(payload.admin_password or ""),
                "role": "公司管理员",
                "status": "enabled",
                "tenant_id": str(row["id"]),
                "department_name": None,
            }
        )
        refreshed = await repository.find_by_id(str(row["id"]))
        if refreshed is not None:
            row = refreshed
    return _to_response(row)


@router.put("/tenants/{tenant_id}", response_model=TenantResponse, summary="更新公司")
async def update_tenant(
    tenant_id: str,
    payload: TenantUpdatePayload,
    _: Annotated[AuthUserResponse, Depends(_require_tenants_permission)],
    repository: Annotated[PostgresTenantRepository, Depends(_get_tenant_repository)],
) -> TenantResponse:
    existing = await repository.find_by_id(tenant_id)
    if existing is None:
        raise AliothError(message=f"未找到 tenant_id={tenant_id} 对应的公司", status_code=404)
    display_name = payload.display_name.strip()
    if not display_name:
        raise AliothError(message="公司名称不能为空")
    if str(existing["id"]) == DEFAULT_TENANT_ID and payload.status == "disabled":
        raise AliothError(message="体验中心是总部，不能停用", status_code=409)
    duplicate = await repository.find_by_display_name(display_name)
    if duplicate is not None and str(duplicate["id"]) != str(existing["id"]):
        raise AliothError(message="公司名称已存在", status_code=409)
    try:
        row = await repository.update_tenant(
            tenant_id,
            {
                "display_name": display_name,
                "status": payload.status,
                "contact_name": (payload.contact_name or "").strip() or None,
                "remark": (payload.remark or "").strip() or None,
            },
        )
    except asyncpg.UniqueViolationError as exc:
        message = (
            "公司名称已存在"
            if exc.constraint_name == TENANT_NAME_UNIQUE_INDEX
            else "公司信息与已有记录冲突"
        )
        raise AliothError(message=message, status_code=409) from exc
    if row is None:
        raise AliothError(message=f"未找到 tenant_id={tenant_id} 对应的公司", status_code=404)
    return _to_response(row)


@router.delete(
    "/tenants/{tenant_id}", status_code=204,
    response_class=Response, summary="删除无关联公司",
)
async def delete_tenant(
    tenant_id: str,
    current_user: Annotated[AuthUserResponse, Depends(_require_tenants_permission)],
    repository: Annotated[PostgresTenantRepository, Depends(_get_tenant_repository)],
) -> Response:
    existing = await repository.find_by_id(tenant_id)
    if existing is None:
        raise AliothError(message="公司不存在或已删除", status_code=404)
    if str(existing["id"]) == DEFAULT_TENANT_ID:
        raise AliothError(message="体验中心是总部，不能删除", status_code=409)
    try:
        result = await repository.delete_tenant(tenant_id)
    except asyncpg.LockNotAvailableError as exc:
        raise AliothError(
            message="关联数据正在更新，请稍后重试删除", status_code=409,
        ) from exc
    except asyncpg.ForeignKeyViolationError as exc:
        raise AliothError(
            message="公司仍有业务关联，不能删除，请先处理关联数据", status_code=409,
        ) from exc
    if result.associations:
        raise AliothError(
            message=(f"公司关联 {result.association_message()}，不能删除；"
                     "请先处理关联数据或停用公司"),
            status_code=409,
        )
    if not result.deleted:
        raise AliothError(message="公司不存在或已删除", status_code=404)
    logger.info(
        "组织删除 actor_id=%s tenant_id=%s tenant_name=%s",
        current_user.id, tenant_id, existing["display_name"],
    )
    return Response(status_code=204)
