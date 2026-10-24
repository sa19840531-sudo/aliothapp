"""正式 App 质检 API 文档站：/api/v1/app-qc/api-doc。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services.api.app.api.v1.app_qc_openapi import (
    APP_QC_DOC_VERSION,
    build_app_qc_openapi,
    render_app_qc_swagger_html,
)

router = APIRouter(prefix="/app-qc", tags=["app-qc-docs"])


@router.get("/openapi.json")
def app_qc_openapi_json(request: Request) -> JSONResponse:
    del request
    return JSONResponse(
        build_app_qc_openapi(version=APP_QC_DOC_VERSION),
        media_type="application/json; charset=utf-8",
    )


@router.get("/api-doc", response_class=HTMLResponse)
@router.get("/api-doc/", response_class=HTMLResponse)
def app_qc_api_doc() -> HTMLResponse:
    """Swagger UI 风格正式 App 质检对接站（对齐 mobile-demo /api-doc）。"""
    return HTMLResponse(
        render_app_qc_swagger_html(openapi_url="/api/v1/app-qc/openapi.json")
    )
