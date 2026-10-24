"""星語 Official Auth：Header Token 比對（對齊青雲 HttpOfficialAppApiClient）。"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from services.api.app.api.v1.xingyu_official.gate import (
    resolve_xingyu_auth_header_name,
    resolve_xingyu_official_token,
)


async def require_xingyu_official_auth(request: Request) -> None:
    """未帶正確 Token 一律 401；Token 未配置則 503（不應掛載，雙重保險）。"""
    expected = resolve_xingyu_official_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error_code": "OFFICIAL_TOKEN_UNSET", "retryable": False},
        )
    header_name = resolve_xingyu_auth_header_name()
    got = (request.headers.get(header_name) or "").strip()
    if got == expected or got == f"Bearer {expected}":
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error_code": "OFFICIAL_UNAUTHORIZED", "retryable": False},
    )
