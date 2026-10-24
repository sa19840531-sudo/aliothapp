"""产品管理接口。"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from services.api.app.api.v1.auth import AuthUserResponse, require_access_scope, require_auth_user
from services.api.app.application.spreadsheet import SpreadsheetError, build_xlsx, read_sheet_rows
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.product_repository import PostgresProductRepository
from services.api.app.infrastructure.db.tenant_repository import PostgresTenantRepository

router = APIRouter()


# iOS 只代表产品资料能力（资料录入、Excel 导入），自动质检仍待接入。
ProductPlatform = Literal["h5", "web", "app", "ios"]


class ProductPayload(BaseModel):
    project_code: str = Field(..., min_length=1, max_length=64)
    product_name: str = Field(..., min_length=1, max_length=255)
    h5_url: str | None = Field(default=None, max_length=4096)
    department_name: str | None = Field(default=None, max_length=128)
    owner_name: str | None = Field(default=None, max_length=128)
    platforms: list[ProductPlatform] = Field(default_factory=list)
    status: Literal["enabled", "disabled"] = "enabled"
    # 所属公司：只有总部能指定（留空 = 共享产品）；其它账号固定落在本公司。
    tenant_id: str | None = None


class ProductResponse(BaseModel):
    id: str
    project_code: str
    product_name: str
    h5_url: str | None
    department_name: str | None = None
    owner_name: str | None = None
    platforms: list[str] = Field(default_factory=list)
    status: str
    tenant_id: str | None = None
    tenant_name: str | None = None
    created_at: datetime
    updated_at: datetime


class ProductImportErrorItem(BaseModel):
    row: int
    message: str


class ProductImportResultResponse(BaseModel):
    id: str | None = None
    batch_no: str | None = None
    filename: str
    total_rows: int
    created_count: int
    updated_count: int
    failed_count: int
    errors: list[ProductImportErrorItem]


class ProductImportRecordResponse(BaseModel):
    id: str
    batch_no: str | None = None
    tenant_id: str | None = None
    tenant_name: str | None = None
    operator_name: str
    filename: str
    total_rows: int
    created_count: int
    updated_count: int
    failed_count: int
    errors: list[ProductImportErrorItem]
    created_at: datetime


class ProductImportRecordListResponse(BaseModel):
    items: list[ProductImportRecordResponse]


class PagedProductResponse(BaseModel):
    items: list[ProductResponse]
    total: int
    page: int
    page_size: int


@lru_cache
def _get_product_repository() -> PostgresProductRepository:
    return PostgresProductRepository()


@lru_cache
def _get_tenant_repository() -> PostgresTenantRepository:
    return PostgresTenantRepository()


def _resolve_product_tenant(scope: AccessScope, requested: str | None) -> str | None:
    """总部可指定公司（留空 = 共享）；其它账号的产品固定归本公司。"""
    if scope.can_read_any:
        value = (requested or "").strip()
        return value or None
    return scope.tenant_id


def _visible_department(scope: AccessScope) -> str | None:
    """部门账号只看本部门的产品；公司级以上不限部门。"""
    if scope.can_read_tenant:
        return None
    return scope.department_scope


def _pin_department(scope: AccessScope, payload: dict) -> dict:
    """部门账号登记 / 修改的产品固定归本部门。"""
    department = _visible_department(scope)
    if department:
        payload["department_name"] = department
    return payload


def _ensure_product_editable(scope: AccessScope, product: dict) -> None:
    owner = product.get("tenant_id")
    if scope.can_read_any:
        return
    if owner is None:
        raise AliothError(message="共享产品由运营中心维护，本公司账号不能修改", status_code=403)
    if str(owner) != scope.tenant_id:
        raise AliothError(message="只能维护本公司的产品", status_code=403)
    department = _visible_department(scope)
    if department and (product.get("department_name") or "") != department:
        raise AliothError(message="只能维护本部门的产品", status_code=403)


async def _require_products_permission(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    if "qcRules" not in current_user.permissions:
        raise AliothError(message="无权限访问该资源", status_code=403)
    return current_user


def _normalized_payload(payload: ProductPayload, *, tenant_id: str | None) -> dict:
    h5_url = payload.h5_url.strip() if payload.h5_url else ""
    return {
        "project_code": payload.project_code.strip(),
        "product_name": payload.product_name.strip(),
        "h5_url": h5_url or None,
        "department_name": (payload.department_name or "").strip() or None,
        "owner_name": (payload.owner_name or "").strip() or None,
        "platforms": _normalized_platforms(payload.platforms, h5_url),
        "status": payload.status,
        "tenant_id": tenant_id,
    }


def _normalized_platforms(platforms: list[str], h5_url: str | None) -> list[str]:
    """平台去重保序；填了 H5 地址但没勾平台时默认算 H5。"""
    result: list[str] = []
    for item in platforms:
        code = str(item).strip().lower()
        if code in _PLATFORM_CODES and code not in result:
            result.append(code)
    if not result and h5_url:
        result.append("h5")
    return result


_PLATFORM_CODES = {"h5", "web", "app", "ios"}
_PLATFORM_ALIASES = {
    "h5": "h5",
    "web": "web",
    "app": "app",
    "android": "app",
    "安卓": "app",
    "ios": "ios",
    "iphone": "ios",
    "苹果": "ios",
    "桌面": "web",
    "网页": "web",
}
_CSV_HEADER_ALIASES = {
    "project_code": {"project_code", "产品编码", "项目编号", "编码"},
    "product_name": {"product_name", "产品名称", "名称"},
    "tenant_key": {"tenant_key", "所属公司编码", "公司编码", "所属公司", "公司"},
    "department_name": {"department_name", "所属部门", "部门"},
    "owner_name": {"owner_name", "负责人"},
    "platforms": {"platforms", "支持平台", "平台", "平台类型", "目标平台"},
    "h5_url": {"h5_url", "h5地址", "h5 地址", "h5链接", "h5 链接", "地址", "访问地址"},
    "status": {"status", "状态"},
}
# 导入模板（xlsx）：表头与别名一致；「所属公司编码」只有运营中心导入时才需要填。
IMPORT_TEMPLATE_HEADER = ["产品编码", "产品名称", "所属公司编码", "所属部门", "平台类型", "负责人", "H5地址", "状态"]
IMPORT_TEMPLATE_ROWS = [
    ["MB-001", "手机银行 Android", "", "一部", "Android", "小明", "", "启用"],
    ["H5-001", "H5 活动页", "", "一部", "H5", "小红", "https://example.com/h5", "启用"],
    ["IOS-001", "iOS 客户端（资料）", "", "二部", "iOS", "小李", "", "启用"],
]


def _decode_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AliothError(message="CSV 文件编码无法识别，请用 UTF-8 或 GBK 保存", status_code=400)


def _header_key(name: str) -> str | None:
    normalized = name.strip().lower()
    for key, aliases in _CSV_HEADER_ALIASES.items():
        if normalized in aliases:
            return key
    return None


def _rows_from_upload(filename: str, raw: bytes) -> list[list[str]]:
    """CSV 或 xlsx 都转成「表头 + 数据行」的二维文本。"""
    lowered = filename.lower()
    if lowered.endswith(".xlsx"):
        try:
            return read_sheet_rows(raw)
        except SpreadsheetError as exc:
            raise AliothError(message=str(exc), status_code=400) from exc
    if lowered.endswith(".xls"):
        raise AliothError(message="暂不支持旧版 .xls，请在 Excel 里另存为 .xlsx 后再导入", status_code=400)
    if lowered.endswith(".csv"):
        return [list(cells) for cells in csv.reader(io.StringIO(_decode_csv(raw)))]
    raise AliothError(message="只支持 .xlsx 或 .csv 文件", status_code=400)


def _parse_product_rows(table: list[list[str]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """把表格（表头 + 数据行）解析为待入库行；表头按中英文别名识别。"""
    if not table:
        raise AliothError(message="文件是空的", status_code=400)
    header = table[0]
    columns = [_header_key(str(cell)) for cell in header]
    if "project_code" not in columns or "product_name" not in columns:
        raise AliothError(message="表格至少要有「产品编码」和「产品名称」两列", status_code=400)
    rows: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    for index, raw_cells in enumerate(table[1:], start=2):
        cells = [str(cell) for cell in raw_cells]
        if not any(cell.strip() for cell in cells):
            continue
        record: dict[str, str] = {}
        for key, cell in zip(columns, cells, strict=False):
            if key:
                record[key] = cell.strip()
        project_code = record.get("project_code", "")
        product_name = record.get("product_name", "")
        if not project_code or not product_name:
            errors.append({"row": index, "message": "产品编码或产品名称为空"})
            continue
        status_text = record.get("status", "").lower()
        status = "disabled" if status_text in {"disabled", "停用", "禁用"} else "enabled"
        platforms: list[str] = []
        for token in re.split(r"[;,，/\s|]+", record.get("platforms", "")):
            code = _PLATFORM_ALIASES.get(token.strip().lower())
            if code and code not in platforms:
                platforms.append(code)
        h5_url = record.get("h5_url") or None
        if h5_url and not h5_url.lower().startswith(("http://", "https://")):
            errors.append({"row": index, "message": f"H5 地址必须以 http(s):// 开头：{h5_url}"})
            continue
        if not platforms and h5_url:
            platforms.append("h5")
        rows.append(
            {
                "row": index,
                "project_code": project_code,
                "product_name": product_name,
                "h5_url": h5_url,
                "tenant_key": record.get("tenant_key") or None,
                "department_name": record.get("department_name") or None,
                "owner_name": record.get("owner_name") or None,
                "platforms": platforms,
                "status": status,
            }
        )
    return rows, errors


@router.get("/products", response_model=PagedProductResponse, summary="分页查询产品")
async def list_products(
    _: Annotated[AuthUserResponse, Depends(_require_products_permission)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresProductRepository, Depends(_get_product_repository)],
    keyword: str | None = None,
    status: Literal["enabled", "disabled"] | None = None,
    tenant_id: str | None = None,
    department_name: str | None = None,
    platform: ProductPlatform | None = None,
    page: int = 1,
    page_size: int = 10,
) -> PagedProductResponse:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    items, total = await repository.list_products(
        keyword=keyword,
        status=status,
        page=safe_page,
        page_size=safe_page_size,
        tenant_id=((tenant_id or "").strip() or None) if scope.can_read_any else None,
        visible_tenant_id=None if scope.can_read_any else scope.tenant_id,
        department_name=(department_name or "").strip() or None,
        platform=platform,
        visible_department=_visible_department(scope),
    )
    return PagedProductResponse(
        items=[ProductResponse.model_validate(item) for item in items],
        total=total,
        page=safe_page,
        page_size=safe_page_size,
    )


@router.post("/products", response_model=ProductResponse, status_code=201, summary="创建产品")
async def create_product(
    payload: ProductPayload,
    _: Annotated[AuthUserResponse, Depends(_require_products_permission)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresProductRepository, Depends(_get_product_repository)],
) -> ProductResponse:
    tenant_id = _resolve_product_tenant(scope, payload.tenant_id)
    try:
        row = await repository.create_product(
            _pin_department(scope, _normalized_payload(payload, tenant_id=tenant_id))
        )
    except asyncpg.UniqueViolationError as exc:
        raise AliothError(message="产品编码已存在", status_code=409) from exc
    return ProductResponse.model_validate(row)


@router.put("/products/{product_id}", response_model=ProductResponse, summary="更新产品")
async def update_product(
    product_id: str,
    payload: ProductPayload,
    _: Annotated[AuthUserResponse, Depends(_require_products_permission)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresProductRepository, Depends(_get_product_repository)],
) -> ProductResponse:
    existing = await repository.find_by_id(product_id)
    if existing is None:
        raise AliothError(message=f"未找到 product_id={product_id} 对应的产品", status_code=404)
    _ensure_product_editable(scope, existing)
    if scope.can_read_any:
        tenant_id = _resolve_product_tenant(scope, payload.tenant_id)
    else:
        tenant_id = str(existing.get("tenant_id") or scope.tenant_id)
    try:
        row = await repository.update_product(
            product_id, _pin_department(scope, _normalized_payload(payload, tenant_id=tenant_id))
        )
    except asyncpg.UniqueViolationError as exc:
        raise AliothError(message="产品编码已存在", status_code=409) from exc
    if row is None:
        raise AliothError(message=f"未找到 product_id={product_id} 对应的产品", status_code=404)
    return ProductResponse.model_validate(row)


@router.get(
    "/products/import-records",
    response_model=ProductImportRecordListResponse,
    summary="产品批量导入记录",
)
async def list_product_import_records(
    _: Annotated[AuthUserResponse, Depends(_require_products_permission)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresProductRepository, Depends(_get_product_repository)],
) -> ProductImportRecordListResponse:
    rows = await repository.list_import_records(
        tenant_id=None if scope.can_read_any else scope.tenant_id
    )
    return ProductImportRecordListResponse(
        items=[ProductImportRecordResponse.model_validate(row) for row in rows]
    )


@router.get("/products/import-template", summary="下载产品导入模板（xlsx）")
async def download_product_import_template(
    _: Annotated[AuthUserResponse, Depends(_require_products_permission)],
) -> Response:
    content = build_xlsx(
        [IMPORT_TEMPLATE_HEADER, *IMPORT_TEMPLATE_ROWS],
        sheet_name="产品导入",
        column_widths=[14, 24, 16, 12, 22, 10, 32, 8],
    )
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="product-import-template.xlsx"'},
    )


@router.get("/products/import-records/{record_id}/result", summary="下载某次导入的结果明细（CSV）")
async def download_product_import_result(
    record_id: str,
    _: Annotated[AuthUserResponse, Depends(_require_products_permission)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresProductRepository, Depends(_get_product_repository)],
) -> Response:
    record = await repository.find_import_record(record_id)
    if record is None:
        raise AliothError(message=f"未找到 record_id={record_id} 对应的导入记录", status_code=404)
    if not scope.can_read_any and str(record.get("tenant_id") or "") != scope.tenant_id:
        raise AliothError(message="只能下载本公司的导入结果", status_code=403)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["导入批次", record.get("batch_no") or record["id"]])
    writer.writerow(["文件名", record.get("filename") or ""])
    writer.writerow(["导入时间", str(record.get("created_at") or "")])
    writer.writerow(["总行数", record.get("total_rows") or 0])
    writer.writerow(["新增", record.get("created_count") or 0])
    writer.writerow(["更新", record.get("updated_count") or 0])
    writer.writerow(["失败", record.get("failed_count") or 0])
    writer.writerow([])
    writer.writerow(["行号", "错误原因"])
    for item in record.get("errors") or []:
        writer.writerow([item.get("row", ""), item.get("message", "")])
    filename = f"import-result-{record.get('batch_no') or record['id']}.csv"
    return Response(
        content=("\ufeff" + buffer.getvalue()).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _tenant_lookup(tenant_repository: PostgresTenantRepository) -> dict[str, dict]:
    """公司编码 / 公司名 → 公司行（大小写不敏感），运营中心导入时用来解析「所属公司编码」。"""
    lookup: dict[str, dict] = {}
    for tenant in await tenant_repository.list_tenants():
        lookup[str(tenant["tenant_key"]).lower()] = tenant
        lookup[str(tenant["display_name"]).lower()] = tenant
    return lookup


@router.post(
    "/products/import",
    response_model=ProductImportResultResponse,
    summary="批量导入产品（xlsx / CSV：产品编码/产品名称/所属公司编码/所属部门/平台类型/负责人/H5地址/状态）",
)
async def import_products(
    file: UploadFile,
    current_user: Annotated[AuthUserResponse, Depends(_require_products_permission)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresProductRepository, Depends(_get_product_repository)],
    tenant_repository: Annotated[PostgresTenantRepository, Depends(_get_tenant_repository)],
) -> ProductImportResultResponse:
    filename = file.filename or "products.xlsx"
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise AliothError(message="导入文件不能超过 5MB", status_code=400)
    rows, errors = _parse_product_rows(_rows_from_upload(filename, raw))
    tenant_lookup = await _tenant_lookup(tenant_repository) if scope.can_read_any else {}
    own_department = _visible_department(scope)
    created_count = 0
    updated_count = 0
    for row in rows:
        tenant_key = str(row.pop("tenant_key", "") or "").strip()
        if own_department:
            row_department = str(row.get("department_name") or "").strip()
            if row_department and row_department != own_department:
                errors.append({"row": row["row"], "message": f"只能导入本部门（{own_department}）的产品，所属部门「{row_department}」不是本部门"})
                continue
            row["department_name"] = own_department
        if scope.can_read_any:
            if tenant_key:
                tenant = tenant_lookup.get(tenant_key.lower())
                if tenant is None:
                    errors.append({"row": row["row"], "message": f"所属公司编码不存在：{tenant_key}"})
                    continue
                row["tenant_id"] = str(tenant["id"])
            else:
                row["tenant_id"] = None
        else:
            if tenant_key and tenant_key.lower() not in {
                current_user.tenant_name.lower(),
                (current_user.tenant_id or "").lower(),
            }:
                errors.append({"row": row["row"], "message": f"只能导入本公司产品，所属公司「{tenant_key}」不是本公司"})
                continue
            row["tenant_id"] = scope.tenant_id
        existing = await repository.find_by_code(str(row["project_code"]))
        if existing is not None:
            owner = existing.get("tenant_id")
            if owner is not None and row["tenant_id"] is not None and str(owner) != str(row["tenant_id"]):
                errors.append({"row": row["row"], "message": f"产品编码已被其它公司使用：{row['project_code']}"})
                continue
            if owner is None and not scope.can_read_any:
                errors.append({"row": row["row"], "message": f"产品编码属于运营中心共享产品，不能覆盖：{row['project_code']}"})
                continue
        try:
            _, inserted = await repository.upsert_product(row)
        except Exception as exc:  # noqa: BLE001 - 单行失败不影响其它行，错误进记录
            errors.append({"row": row["row"], "message": f"写入失败：{exc}"})
            continue
        if inserted:
            created_count += 1
        else:
            updated_count += 1
    errors.sort(key=lambda item: int(item["row"]))
    summary = {
        "tenant_id": scope.tenant_id,
        "operator_id": scope.actor_user_id,
        "operator_name": current_user.full_name or current_user.username,
        "filename": filename,
        "total_rows": len(rows) + len(errors),
        "created_count": created_count,
        "updated_count": updated_count,
        "failed_count": len(errors),
        "errors": errors,
    }
    record = await repository.record_import(summary)
    return ProductImportResultResponse(
        id=str(record.get("id")) if record.get("id") else None,
        batch_no=(str(record["batch_no"]) if record.get("batch_no") else None),
        filename=filename,
        total_rows=summary["total_rows"],
        created_count=created_count,
        updated_count=updated_count,
        failed_count=len(errors),
        errors=[ProductImportErrorItem(row=int(item["row"]), message=str(item["message"])) for item in errors],
    )


@router.delete(
    "/products/{product_id}",
    status_code=204,
    response_model=None,
    summary="删除产品",
)
async def delete_product(
    product_id: str,
    _: Annotated[AuthUserResponse, Depends(_require_products_permission)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[PostgresProductRepository, Depends(_get_product_repository)],
) -> None:
    # 删除与修改同一套归属校验：共享产品只有运营中心能删，其它账号只能删本公司 / 本部门的
    existing = await repository.find_by_id(product_id)
    if existing is None:
        raise AliothError(message=f"未找到 product_id={product_id} 对应的产品", status_code=404)
    _ensure_product_editable(scope, existing)
    deleted = await repository.delete_product(product_id)
    if not deleted:
        raise AliothError(message=f"未找到 product_id={product_id} 对应的产品", status_code=404)
