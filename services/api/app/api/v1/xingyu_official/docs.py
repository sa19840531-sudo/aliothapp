"""星語 Official 公开文档路由（无需 Token）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services.api.app.api.v1.xingyu_official.openapi import (
    _XINGYU_VERSION,
    build_xingyu_official_openapi,
    render_xingyu_swagger_html,
)

docs_router = APIRouter(prefix="/v1", tags=["xingyu-official-docs"])


@docs_router.get("/openapi.json")
def xingyu_openapi_json(request: Request) -> JSONResponse:
    """Official App OpenAPI 3 规格（供 Swagger／青云导入）。"""
    del request
    return JSONResponse(
        build_xingyu_official_openapi(version=_XINGYU_VERSION),
        headers={"Cache-Control": "no-store"},
    )


@docs_router.get("/api-doc", response_class=HTMLResponse)
@docs_router.get("/api-doc/", response_class=HTMLResponse)
def xingyu_api_doc() -> HTMLResponse:
    """Swagger UI 风格 Official API 对接站（对齐 mobile-demo /api-doc）。"""
    html = render_xingyu_swagger_html(openapi_url="/v1/openapi.json")
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )
