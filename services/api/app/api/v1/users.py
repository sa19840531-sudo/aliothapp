"""用户管理接口。

账号按公司隔离：总部（持有 ``users:any``）能查看和维护所有公司的账号，公司管理员
只能查看、新建、修改本公司的账号，并且不能创建或改动平台管理员。
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from services.api.app.api.v1.auth import AuthUserResponse, hash_password, require_auth_user
from services.api.app.core.errors import AliothError
from services.api.app.infrastructure.db.department_repository import PostgresDepartmentRepository
from services.api.app.infrastructure.db.tenant_repository import PostgresTenantRepository
from services.api.app.infrastructure.db.user_repository import PostgresUserRepository

router = APIRouter()

UserRole = Literal["平台管理员", "公司管理员", "审计员", "规则配置员"]
PLATFORM_ADMIN_ROLE = "平台管理员"
# 部门账号：数据范围按所属部门划分，必须挂在「部门管理」里的部门上
DEPARTMENT_ACCOUNT_ROLE = "规则配置员"


class UserPayload(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    full_name: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=6, max_length=128)
    role: UserRole = "规则配置员"
    status: Literal["enabled", "disabled"] = "enabled"
    # 缺省落在操作者自己的公司；只有总部可以指定别的公司。
    tenant_id: str | None = None
    department_name: str | None = Field(default=None, max_length=128)


class UserUpdatePayload(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=128)
    role: UserRole
    status: Literal["enabled", "disabled"]
    password: str | None = Field(default=None, min_length=6, max_length=128)
    tenant_id: str | None = None
    department_name: str | None = Field(default=None, max_length=128)


class UserResponse(BaseModel):
    id: str
    username: str
    full_name: str
    role: str
    status: str
    tenant_id: str = ""
    tenant_name: str = ""
    department_name: str | None = None
    last_login_at: datetime | None = None
    # 数据权限范围（原型「账号与权限」列）：由角色和部门推出来，不单独存。
    data_scope: str = ""
    created_at: datetime
    updated_at: datetime


class PagedUserResponse(BaseModel):
    items: list[UserResponse]
    total: int
    page: int
    page_size: int


@lru_cache
def _get_user_repository() -> PostgresUserRepository:
    return PostgresUserRepository()


def data_scope_label(role: str, department_name: str | None) -> str:
    """账号的数据权限范围说明：总部全看，公司管理员/审计员看本公司，部门账号看所属部门。"""
    if role == PLATFORM_ADMIN_ROLE:
        return "全部公司"
    if role == "公司管理员":
        return "本公司全部部门"
    if role == "审计员":
        return "本公司（只读）"
    department = (department_name or "").strip()
    # 没挂部门的老账号只能看本人发起的任务（与 AccessScope.department_scope 一致）
    return f"仅{department}数据" if department else "本人发起的任务"


def _to_user_response(item: dict[str, object]) -> UserResponse:
    data = dict(item)
    data["data_scope"] = data_scope_label(
        str(data.get("role") or ""), data.get("department_name")  # type: ignore[arg-type]
    )
    return UserResponse.model_validate(data)


@lru_cache
def _get_tenant_repository() -> PostgresTenantRepository:
    return PostgresTenantRepository()


@lru_cache
def _get_department_repository() -> PostgresDepartmentRepository:
    return PostgresDepartmentRepository()


async def _resolve_department(
    *,
    role: str,
    tenant_id: str,
    requested: str | None,
    department_repository: PostgresDepartmentRepository,
    current: str | None = None,
) -> str | None:
    """部门账号必须选本公司「部门管理」里的部门；其他角色看整个公司或全部公司，不带部门。

    ``current`` 是账号原来的部门（公司没变时才传）：原本就在已停用部门里的账号，改别的字段照常保存。
    """

    if role != DEPARTMENT_ACCOUNT_ROLE:
        return None
    name = (requested or "").strip()
    if not name:
        raise AliothError(message="部门账号必须选择所属部门", status_code=400)
    department = (
        await department_repository.find_by_name(tenant_id=tenant_id, name=name)
        if tenant_id
        else None
    )
    if department is None:
        raise AliothError(
            message="所选部门不在该公司的部门管理里，请先到「部门管理」新增部门",
            status_code=400,
        )
    resolved = str(department.get("name") or name)
    unchanged = resolved.casefold() == (current or "").strip().casefold()
    if str(department.get("status") or "") == "disabled" and not unchanged:
        raise AliothError(message="该部门已停用，不能再添加账号", status_code=409)
    return resolved


async def _require_users_permission(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    if "users" not in current_user.permissions:
        raise AliothError(message="无权限访问该资源", status_code=403)
    return current_user


def _manages_any_tenant(current_user: AuthUserResponse) -> bool:
    return "users:any" in current_user.permissions


def _resolve_target_tenant(current_user: AuthUserResponse, requested: str | None) -> str:
    """把请求里的公司归一到操作者有权管理的公司。"""

    requested = (requested or "").strip()
    if not requested:
        return current_user.tenant_id
    if requested != current_user.tenant_id and not _manages_any_tenant(current_user):
        raise AliothError(message="只能维护本公司的用户", status_code=403)
    return requested


def _ensure_role_allowed(current_user: AuthUserResponse, role: str) -> None:
    if role == PLATFORM_ADMIN_ROLE and not _manages_any_tenant(current_user):
        raise AliothError(message="只有总部可以设置平台管理员", status_code=403)


async def _ensure_tenant_usable(
    current_user: AuthUserResponse,
    tenant_id: str,
    tenant_repository: PostgresTenantRepository,
) -> None:
    """总部指定别的公司时，确认公司存在且未停用；本公司不再重复查库。"""

    if tenant_id == current_user.tenant_id:
        return
    tenant = await tenant_repository.find_by_id(tenant_id)
    if tenant is None:
        raise AliothError(message="公司不存在", status_code=404)
    if str(tenant.get("status")) == "disabled":
        raise AliothError(message="该公司已停用，不能再添加用户", status_code=409)


async def _load_manageable_user(
    current_user: AuthUserResponse,
    user_id: str,
    repository: PostgresUserRepository,
) -> dict[str, object]:
    existing = await repository.find_by_id(user_id)
    if existing is None:
        raise AliothError(message=f"未找到 user_id={user_id} 对应的用户", status_code=404)
    if not _manages_any_tenant(current_user):
        if str(existing.get("tenant_id") or "") != current_user.tenant_id:
            raise AliothError(message=f"未找到 user_id={user_id} 对应的用户", status_code=404)
        if str(existing.get("role") or "") == PLATFORM_ADMIN_ROLE:
            raise AliothError(message="只有总部可以维护平台管理员", status_code=403)
    return existing


@router.get("/users", response_model=PagedUserResponse, summary="分页查询用户")
async def list_users(
    current_user: Annotated[AuthUserResponse, Depends(_require_users_permission)],
    repository: Annotated[PostgresUserRepository, Depends(_get_user_repository)],
    keyword: str | None = None,
    status: Literal["enabled", "disabled"] | None = None,
    tenant_id: str | None = None,
    role: UserRole | None = None,
    department_name: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> PagedUserResponse:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    if _manages_any_tenant(current_user):
        scoped_tenant_id = (tenant_id or "").strip() or None
    else:
        scoped_tenant_id = current_user.tenant_id
    items, total = await repository.list_users(
        keyword=keyword,
        status=status,
        page=safe_page,
        page_size=safe_page_size,
        tenant_id=scoped_tenant_id,
        role=role,
        department_name=(department_name or "").strip() or None,
    )
    return PagedUserResponse(
        items=[_to_user_response(item) for item in items],
        total=total,
        page=safe_page,
        page_size=safe_page_size,
    )


@router.post("/users", response_model=UserResponse, status_code=201, summary="创建用户")
async def create_user(
    payload: UserPayload,
    current_user: Annotated[AuthUserResponse, Depends(_require_users_permission)],
    repository: Annotated[PostgresUserRepository, Depends(_get_user_repository)],
    tenant_repository: Annotated[PostgresTenantRepository, Depends(_get_tenant_repository)],
    department_repository: Annotated[
        PostgresDepartmentRepository, Depends(_get_department_repository)
    ],
) -> UserResponse:
    target_tenant_id = _resolve_target_tenant(current_user, payload.tenant_id)
    _ensure_role_allowed(current_user, payload.role)
    await _ensure_tenant_usable(current_user, target_tenant_id, tenant_repository)
    department_name = await _resolve_department(
        role=payload.role,
        tenant_id=target_tenant_id,
        requested=payload.department_name,
        department_repository=department_repository,
    )
    try:
        row = await repository.create_user(
            {
                "username": payload.username.strip(),
                "full_name": payload.full_name.strip(),
                "password_hash": hash_password(payload.password),
                "role": payload.role,
                "department_name": department_name,
                "status": payload.status,
                "tenant_id": target_tenant_id,
            }
        )
    except asyncpg.UniqueViolationError as exc:
        raise AliothError(message="用户名已存在", status_code=409) from exc
    except asyncpg.ForeignKeyViolationError as exc:
        raise AliothError(message="公司不存在", status_code=404) from exc
    return _to_user_response(row)


@router.put("/users/{user_id}", response_model=UserResponse, summary="更新用户")
async def update_user(
    user_id: str,
    payload: UserUpdatePayload,
    current_user: Annotated[AuthUserResponse, Depends(_require_users_permission)],
    repository: Annotated[PostgresUserRepository, Depends(_get_user_repository)],
    tenant_repository: Annotated[PostgresTenantRepository, Depends(_get_tenant_repository)],
    department_repository: Annotated[
        PostgresDepartmentRepository, Depends(_get_department_repository)
    ],
) -> UserResponse:
    existing = await _load_manageable_user(current_user, user_id, repository)
    _ensure_role_allowed(current_user, payload.role)
    next_tenant_id: str | None = None
    if payload.tenant_id and payload.tenant_id.strip() != str(existing.get("tenant_id") or ""):
        next_tenant_id = _resolve_target_tenant(current_user, payload.tenant_id)
        await _ensure_tenant_usable(current_user, next_tenant_id, tenant_repository)
    department_name = await _resolve_department(
        role=payload.role,
        tenant_id=next_tenant_id or str(existing.get("tenant_id") or ""),
        requested=payload.department_name,
        department_repository=department_repository,
        # 换了公司就按新公司的部门重新校验，不沿用原部门
        current=None if next_tenant_id else str(existing.get("department_name") or ""),
    )
    try:
        row = await repository.update_user(
            user_id,
            {
                "full_name": payload.full_name.strip(),
                "role": payload.role,
                "department_name": department_name,
                "status": payload.status,
                "password_hash": hash_password(payload.password) if payload.password else None,
                "tenant_id": next_tenant_id,
            },
        )
    except asyncpg.UniqueViolationError as exc:
        raise AliothError(message="用户名已存在", status_code=409) from exc
    except asyncpg.ForeignKeyViolationError as exc:
        raise AliothError(message="公司不存在", status_code=404) from exc
    if row is None:
        raise AliothError(message=f"未找到 user_id={user_id} 对应的用户", status_code=404)
    return _to_user_response(row)


@router.delete(
    "/users/{user_id}",
    status_code=204,
    response_model=None,
    summary="删除用户",
)
async def delete_user(
    user_id: str,
    current_user: Annotated[AuthUserResponse, Depends(_require_users_permission)],
    repository: Annotated[PostgresUserRepository, Depends(_get_user_repository)],
) -> None:
    await _load_manageable_user(current_user, user_id, repository)
    try:
        deleted = await repository.delete_user(user_id)
    except asyncpg.ForeignKeyViolationError as exc:
        raise AliothError(
            message="该用户名下已有任务或构建记录，只能停用不能删除", status_code=409
        ) from exc
    if not deleted:
        raise AliothError(message=f"未找到 user_id={user_id} 对应的用户", status_code=404)
