"""工作台产品下拉接口（只读，供创建任务时选择产品与目标链接）。"""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.product_repository import PostgresProductRepository

router = APIRouter(prefix="/products")


class WorkstationProductOption(BaseModel):
    """产品清单项（2.0 原型产品管理页 + 创建任务下拉共用）。"""

    id: str | None = None
    project_code: str
    product_name: str
    h5_url: str | None = None
    department_name: str | None = None
    owner_name: str | None = None
    platforms: list[str] = []
    status: str = "enabled"
    tenant_id: str | None = None
    tenant_name: str | None = None


class WorkstationProductListResponse(BaseModel):
    """产品下拉列表响应。"""

    items: list[WorkstationProductOption]


@lru_cache
def _get_product_repository() -> PostgresProductRepository:
    return PostgresProductRepository()


@router.get("", response_model=WorkstationProductListResponse, summary="查询可选产品（下拉）")
async def list_workstation_products(
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresProductRepository, Depends(_get_product_repository)],
) -> WorkstationProductListResponse:
    # 总部看全部；其它公司看本公司的 + 运营中心共享的。
    products = await repository.list_enabled_products(
        visible_tenant_id=None if scope.can_read_any else scope.tenant_id,
        visible_department=None if scope.can_read_tenant else scope.department_scope,
    )
    return WorkstationProductListResponse(
        items=[
            WorkstationProductOption(
                id=(str(product["id"]) if product.get("id") else None),
                project_code=str(product["project_code"]),
                product_name=str(product["product_name"]),
                h5_url=product.get("h5_url"),
                department_name=product.get("department_name"),
                owner_name=product.get("owner_name"),
                platforms=list(product.get("platforms") or []),
                status=str(product.get("status") or "enabled"),
                tenant_id=(str(product["tenant_id"]) if product.get("tenant_id") else None),
                tenant_name=(str(product["tenant_name"]) if product.get("tenant_name") else None),
            )
            for product in products
        ]
    )
