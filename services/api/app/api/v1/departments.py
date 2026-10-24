"""部门管理接口（2.1 原型「组织管理 → 部门管理」）。

组织层级是 分公司 → 部门 → 产品：运营中心可以管全部公司的部门，分公司管理员只管本公司的。
停用部门前要求部门下没有启用中的产品和账号，避免产品、任务失去归属。
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from services.api.app.api.v1.auth import AuthUserResponse, require_auth_user
from services.api.app.core.errors import AliothError
from services.api.app.infrastructure.db.department_repository import PostgresDepartmentRepository

router = APIRouter()
logger = logging.getLogger(__name__)


class DepartmentPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    owner_name: str | None = Field(default=None, max_length=128)
    status: Literal["enabled", "disabled"] = "enabled"
    monthly_token_budget: float | None = Field(default=None, ge=0)
    remark: str | None = Field(default=None, max_length=2000)
    # 缺省落在操作者自己的公司；只有总部可以指定别的公司。
    tenant_id: str | None = None


class DepartmentResponse(BaseModel):
    id: str
    tenant_id: str
    tenant_name: str = ""
    name: str
    owner_name: str | None = None
    status: str
    monthly_token_budget: float | None = None
    remark: str | None = None
    product_count: int = 0
    user_count: int = 0
    created_at: datetime
    updated_at: datetime


class DepartmentListResponse(BaseModel):
    items: list[DepartmentResponse]


@lru_cache
def _get_department_repository() -> PostgresDepartmentRepository:
    return PostgresDepartmentRepository()


async def _require_departments_permission(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    if "departments" not in current_user.permissions:
        raise AliothError(message="无权限访问该资源", status_code=403)
    return current_user


def _manages_any_tenant(current_user: AuthUserResponse) -> bool:
    return "users:any" in current_user.permissions


def _scoped_tenant_id(current_user: AuthUserResponse, requested: str | None) -> str:
    if _manages_any_tenant(current_user):
        return (requested or "").strip() or current_user.tenant_id
    return current_user.tenant_id


def _to_response(row: dict[str, object]) -> DepartmentResponse:
    return DepartmentResponse.model_validate(row)


@router.get("/departments", response_model=DepartmentListResponse, summary="查询部门列表")
async def list_departments(
    current_user: Annotated[AuthUserResponse, Depends(_require_departments_permission)],
    repository: Annotated[PostgresDepartmentRepository, Depends(_get_department_repository)],
    tenant_id: str | None = None,
    keyword: str | None = None,
    status: Literal["enabled", "disabled"] | None = None,
) -> DepartmentListResponse:
    if _manages_any_tenant(current_user):
        scoped_tenant_id = (tenant_id or "").strip() or None
    else:
        scoped_tenant_id = current_user.tenant_id
    rows = await repository.list_departments(
        tenant_id=scoped_tenant_id,
        keyword=(keyword or "").strip() or None,
        status=status,
    )
    return DepartmentListResponse(items=[_to_response(row) for row in rows])


@router.post("/departments", response_model=DepartmentResponse, status_code=201, summary="新增部门")
async def create_department(
    payload: DepartmentPayload,
    current_user: Annotated[AuthUserResponse, Depends(_require_departments_permission)],
    repository: Annotated[PostgresDepartmentRepository, Depends(_get_department_repository)],
) -> DepartmentResponse:
    name = payload.name.strip()
    if not name:
        raise AliothError(message="部门名称不能为空")
    tenant_id = _scoped_tenant_id(current_user, payload.tenant_id)
    if await repository.find_by_name(tenant_id=tenant_id, name=name) is not None:
        raise AliothError(message="该公司已有同名部门", status_code=409)
    try:
        row = await repository.create_department(
            {
                "tenant_id": tenant_id,
                "name": name,
                "owner_name": (payload.owner_name or "").strip() or None,
                "status": payload.status,
                "monthly_token_budget": payload.monthly_token_budget,
                "remark": (payload.remark or "").strip() or None,
            }
        )
    except asyncpg.UniqueViolationError as exc:
        raise AliothError(message="该公司已有同名部门", status_code=409) from exc
    return _to_response(row)


@router.put("/departments/{department_id}", response_model=DepartmentResponse, summary="更新部门")
async def update_department(
    department_id: str,
    payload: DepartmentPayload,
    current_user: Annotated[AuthUserResponse, Depends(_require_departments_permission)],
    repository: Annotated[PostgresDepartmentRepository, Depends(_get_department_repository)],
) -> DepartmentResponse:
    existing = await repository.find_by_id(department_id)
    if existing is None:
        raise AliothError(message=f"未找到 department_id={department_id} 对应的部门", status_code=404)
    if not _manages_any_tenant(current_user) and str(existing["tenant_id"]) != current_user.tenant_id:
        raise AliothError(message="只能维护本公司的部门", status_code=403)
    name = payload.name.strip()
    if not name:
        raise AliothError(message="部门名称不能为空")
    duplicate = await repository.find_by_name(tenant_id=str(existing["tenant_id"]), name=name)
    if duplicate is not None and str(duplicate["id"]) != str(existing["id"]):
        raise AliothError(message="该公司已有同名部门", status_code=409)
    if payload.status == "disabled" and existing.get("status") != "disabled":
        product_count = int(existing.get("product_count") or 0)
        user_count = int(existing.get("user_count") or 0)
        if product_count or user_count:
            raise AliothError(
                message=(
                    f"部门下还有 {product_count} 个启用产品、{user_count} 个启用账号，"
                    "先转移或停用后再停用部门"
                ),
                status_code=409,
            )
    try:
        row = await repository.update_department(
            department_id,
            {
                "name": name,
                "owner_name": (payload.owner_name or "").strip() or None,
                "status": payload.status,
                "monthly_token_budget": payload.monthly_token_budget,
                "remark": (payload.remark or "").strip() or None,
            },
        )
    except asyncpg.UniqueViolationError as exc:
        raise AliothError(message="该公司已有同名部门", status_code=409) from exc
    if row is None:
        raise AliothError(message=f"未找到 department_id={department_id} 对应的部门", status_code=404)
    return _to_response(row)


@router.delete("/departments/{department_id}", status_code=204, response_class=Response, summary="删除无关联部门")
async def delete_department(
    department_id: str,
    current_user: Annotated[AuthUserResponse, Depends(_require_departments_permission)],
    repository: Annotated[PostgresDepartmentRepository, Depends(_get_department_repository)],
) -> Response:
    existing = await repository.find_by_id(department_id)
    if existing is None:
        raise AliothError(message="部门不存在或已删除", status_code=404)
    if not _manages_any_tenant(current_user) and str(existing["tenant_id"]) != current_user.tenant_id:
        raise AliothError(message="只能维护本公司的部门", status_code=403)
    try:
        result = await repository.delete_department(
            department_id, tenant_id=None if _manages_any_tenant(current_user) else current_user.tenant_id,
        )
    except asyncpg.LockNotAvailableError as exc:
        raise AliothError(
            message="关联数据正在更新，请稍后重试删除", status_code=409,
        ) from exc
    except asyncpg.ForeignKeyViolationError as exc:
        raise AliothError(message="部门仍有业务关联，不能删除，请先处理关联数据", status_code=409) from exc
    if result.associations:
        raise AliothError(message=f"部门关联 {result.association_message()}（含停用或历史数据），不能删除；请先处理关联数据", status_code=409)
    if not result.deleted:
        raise AliothError(message="部门不存在或已删除", status_code=404)
    logger.info("组织删除 actor_id=%s department_id=%s tenant_id=%s department_name=%s", current_user.id, department_id, existing["tenant_id"], existing["name"])
    return Response(status_code=204)
