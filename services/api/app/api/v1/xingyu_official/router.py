"""星語 Official App HTTP 路由：對齊青雲 HttpOfficialAppApiClient 預設路徑。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse

from services.api.app.api.v1.xingyu_official.auth import require_xingyu_official_auth
from services.api.app.application.xingyu_runtime.idempotency import IdempotentRecord
from services.api.app.application.xingyu_runtime.service import (
    XingyuOfficialError,
    XingyuOfficialService,
    get_xingyu_official_service,
)

router = APIRouter(
    prefix="/v1",
    tags=["xingyu-official"],
    dependencies=[Depends(require_xingyu_official_auth)],
)


def _error_response(exc: XingyuOfficialError) -> JSONResponse:
    # 显式打出业务 error_code，便于对照 Worker 侧 APP_DRIVER_FAILED。
    import logging
    from pathlib import Path

    logging.getLogger(__name__).warning(
        "xingyu_official_error status=%s error_code=%s retryable=%s message=%s request_id=%s",
        exc.status_code,
        exc.error_code,
        exc.retryable,
        str(exc)[:200],
        exc.request_id,
    )
    try:
        Path("/workspace/.tmp_xingyu_official_error.log").open("a", encoding="utf-8").write(
            f"status={exc.status_code} error_code={exc.error_code} "
            f"retryable={exc.retryable} message={str(exc)[:200]} "
            f"request_id={exc.request_id}\n"
        )
    except Exception:  # noqa: BLE001 - 诊断旁路不得影响主链
        pass
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "request_id": exc.request_id,
            "error_code": exc.error_code,
            "retryable": exc.retryable,
            "message": str(exc),
        },
        headers={"x-request-id": exc.request_id or ""},
    )


def _cached_response(record: IdempotentRecord) -> Response:
    if record.kind == "json":
        return JSONResponse(
            status_code=record.status_code,
            content=record.json_body or {},
        )
    headers = dict(record.headers or {})
    return Response(
        content=record.png_bytes or b"",
        status_code=record.status_code,
        media_type="image/png",
        headers=headers,
    )


async def _with_idempotency(
    request: Request,
    idempotency_key: str | None,
    handler: Callable[[XingyuOfficialService, dict[str, Any]], Awaitable[Response] | Response],
) -> Response:
    """执行 Official handler；同步 ADB/设备调用必须离开事件循环，避免整站假死。"""
    service = get_xingyu_official_service()
    try:
        raw = await request.json()
        payload = raw if isinstance(raw, dict) else {}
        cached = service.begin_idempotent(idempotency_key, payload)
        if cached is not None:
            return _cached_response(cached)
        # 路由层 handler 目前是同步闭包（内部跑 subprocess ADB）。
        # 若直接在 async 路由里调用，会堵死 uvicorn 事件循环，表现为提交任务后 health/工作台卡死。
        if inspect.iscoroutinefunction(handler):
            return await handler(service, payload)
        result = await asyncio.to_thread(handler, service, payload)
        if isinstance(result, Awaitable):
            return await result
        return result
    except XingyuOfficialError as exc:
        return _error_response(exc)


@router.post("/sessions")
async def start_session(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    def handler(service: XingyuOfficialService, payload: dict[str, Any]) -> Response:
        body = service.start_session(payload)
        if idempotency_key:
            service.idempotency.store_json(
                idempotency_key, payload=payload, status_code=200, body=body
            )
        return JSONResponse(body)

    return await _with_idempotency(request, idempotency_key, handler)


@router.post("/sessions/reattach")
async def reattach_session(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    def handler(service: XingyuOfficialService, payload: dict[str, Any]) -> Response:
        body = service.reattach_session(payload)
        if idempotency_key:
            service.idempotency.store_json(
                idempotency_key, payload=payload, status_code=200, body=body
            )
        return JSONResponse(body)

    return await _with_idempotency(request, idempotency_key, handler)


@router.post("/apps/install")
async def install_app(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    def handler(service: XingyuOfficialService, payload: dict[str, Any]) -> Response:
        body = service.install(payload)
        if idempotency_key:
            service.idempotency.store_json(
                idempotency_key, payload=payload, status_code=200, body=body
            )
        return JSONResponse(body)

    return await _with_idempotency(request, idempotency_key, handler)


@router.post("/apps/open")
async def open_app(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    def handler(service: XingyuOfficialService, payload: dict[str, Any]) -> Response:
        body = service.open_app(payload)
        if idempotency_key:
            service.idempotency.store_json(
                idempotency_key, payload=payload, status_code=200, body=body
            )
        return JSONResponse(body)

    return await _with_idempotency(request, idempotency_key, handler)


@router.post("/apps/capture")
async def capture_app(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    def handler(service: XingyuOfficialService, payload: dict[str, Any]) -> Response:
        capture_payload = dict(payload)
        if request.headers.get("X-Alioth-Force-Adb-Refresh") == "1":
            capture_payload["force_adb_refresh"] = True
        shot, request_id = service.capture(capture_payload)
        headers: dict[str, str] = {
            "x-alioth-width": str(shot.width),
            "x-alioth-height": str(shot.height),
            "x-alioth-orientation": shot.orientation,
            "x-alioth-sha256": shot.sha256,
            "x-request-id": request_id,
        }
        if shot.package_name:
            headers["x-alioth-package-name"] = shot.package_name
        if shot.activity:
            headers["x-alioth-activity"] = shot.activity
        if idempotency_key:
            service.idempotency.store_png(
                idempotency_key,
                payload=payload,
                status_code=200,
                png_bytes=shot.png_bytes,
                headers=headers,
            )
        return Response(
            content=shot.png_bytes,
            media_type="image/png",
            headers=headers,
        )

    return await _with_idempotency(request, idempotency_key, handler)


@router.post("/apps/capture-ui-tree")
async def capture_ui_tree(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    def handler(service: XingyuOfficialService, payload: dict[str, Any]) -> Response:
        capture_payload = dict(payload)
        if request.headers.get("X-Alioth-Force-Adb-Refresh") == "1":
            capture_payload["force_adb_refresh"] = True
        xml, request_id = service.capture_ui_tree(capture_payload)
        headers = {"x-request-id": request_id}
        body = xml.encode("utf-8")
        if idempotency_key:
            service.idempotency.store_png(
                idempotency_key,
                payload=payload,
                status_code=200,
                png_bytes=body,
                headers={**headers, "content-type": "application/xml; charset=utf-8"},
            )
        return Response(
            content=body,
            media_type="application/xml; charset=utf-8",
            headers=headers,
        )

    return await _with_idempotency(request, idempotency_key, handler)


@router.post("/apps/capture-aux")
async def capture_aux(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    def handler(service: XingyuOfficialService, payload: dict[str, Any]) -> Response:
        body = service.capture_aux(payload)
        if idempotency_key:
            service.idempotency.store_json(
                idempotency_key, payload=payload, status_code=200, body=body
            )
        return JSONResponse(body)

    return await _with_idempotency(request, idempotency_key, handler)


@router.post("/apps/execute")
async def execute_action(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    async def handler(service: XingyuOfficialService, payload: dict[str, Any]) -> Response:
        body = await service.execute(payload)
        if idempotency_key:
            service.idempotency.store_json(
                idempotency_key, payload=payload, status_code=200, body=body
            )
        return JSONResponse(body)

    return await _with_idempotency(request, idempotency_key, handler)


@router.post("/sessions/close")
async def close_session(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    def handler(service: XingyuOfficialService, payload: dict[str, Any]) -> Response:
        body = service.close(payload)
        if idempotency_key:
            service.idempotency.store_json(
                idempotency_key, payload=payload, status_code=200, body=body
            )
        return JSONResponse(body)

    return await _with_idempotency(request, idempotency_key, handler)
