"""星語 Official App API 掛載閘門：預設關閉，需顯式開關 + Token + loopback／隔離網。"""

from __future__ import annotations

import os

from services.api.app.core.bind_host import is_loopback_bind_host


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def resolve_xingyu_official_token() -> str:
    """Official 認證 Token（禁止寫入日誌全文）。"""
    return (os.getenv("ALIOTH_XINGYU_OFFICIAL_TOKEN") or "").strip()


def resolve_xingyu_auth_header_name() -> str:
    """與青雲 ``ALIOTH_OFFICIAL_APP_AUTH_HEADER_NAME`` 對齊，預設 Authorization。"""
    raw = (os.getenv("ALIOTH_XINGYU_OFFICIAL_AUTH_HEADER") or "").strip()
    return raw or "Authorization"


def xingyu_official_dry_run() -> bool:
    """乾跑：不碰真實裝置，供契約／單元測試。"""
    return _truthy(os.getenv("ALIOTH_XINGYU_OFFICIAL_DRY_RUN"))


def xingyu_official_docs_enabled() -> bool:
    """是否掛載公開 /v1/api-doc（只需顯式開關，文檔站不強制 Token）。"""
    return _truthy(os.getenv("ALIOTH_XINGYU_OFFICIAL_ENABLED"))


def xingyu_official_router_enabled() -> bool:
    """是否掛載 ``/v1/*`` Official 業務路由。

    必須同時滿足：
    1. ``ALIOTH_XINGYU_OFFICIAL_ENABLED=1``
    2. ``ALIOTH_XINGYU_OFFICIAL_TOKEN`` 非空
    3. 綁定位址為 loopback，或 ``ALIOTH_XINGYU_OFFICIAL_ALLOW_NON_LOOPBACK=1``
    """
    if not xingyu_official_docs_enabled():
        return False
    if not resolve_xingyu_official_token():
        return False
    if _truthy(os.getenv("ALIOTH_XINGYU_OFFICIAL_ALLOW_NON_LOOPBACK")):
        return True
    return is_loopback_bind_host()
