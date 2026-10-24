"""MoreLogin HTTP 传输：Local API 与官方 Open API。

Open API 文档：https://guide.morelogin.com/api-reference/cloud-phone
认证：https://guide.morelogin.com/api-reference/getting-started/authentication

环境变量：
- ``ALIOTH_MORELOGIN_API_MODE``：``auto``（默认）／``open``／``local``
- ``ALIOTH_MORELOGIN_API_ID``／``ALIOTH_MORELOGIN_API_KEY``：Open API OAuth2 凭证
- ``ALIOTH_MORELOGIN_OPEN_API``：默认 ``https://api.morelogin.com``
- ``ALIOTH_MORELOGIN_LOCAL_API``：默认 ``http://127.0.0.1:40000``
- ``ALIOTH_MORELOGIN_HTTP_USER_AGENT``：覆盖 Open API User-Agent
  （避免 Python-urllib 被 Cloudflare 1010 拦截）
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any


class MoreLoginAdbError(RuntimeError):
    """MoreLogin ADB／开机／Open API 操作失败。"""


_DEFAULT_OPEN_API = "https://api.morelogin.com"
_DEFAULT_LOCAL_API = "http://127.0.0.1:40000"
_TOKEN_SKEW_SECONDS = 60.0
# urllib 默认 Python-urllib/* 会被 Cloudflare 1010 拦。
_DEFAULT_OPEN_API_USER_AGENT = "Alioth-MoreLogin-Client/1.0"

_token_lock = threading.Lock()
_cached_token: str = ""
_cached_token_expires_at: float = 0.0


def _truthy_mode(raw: str) -> str:
    return raw.strip().lower()


def resolve_morelogin_http_user_agent() -> str:
    """Open API 请求 User-Agent；可用环境变量覆盖。"""
    return (
        os.getenv("ALIOTH_MORELOGIN_HTTP_USER_AGENT") or _DEFAULT_OPEN_API_USER_AGENT
    ).strip()


def _json_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": resolve_morelogin_http_user_agent(),
    }


def resolve_morelogin_api_id() -> str:
    return (
        os.getenv("ALIOTH_MORELOGIN_API_ID") or os.getenv("ML_API_ID") or ""
    ).strip()


def resolve_morelogin_api_key() -> str:
    return (
        os.getenv("ALIOTH_MORELOGIN_API_KEY") or os.getenv("ML_API_KEY") or ""
    ).strip()


def resolve_morelogin_api_mode() -> str:
    """解析传输模式。auto：同时具备 API ID/Key 则走 Open API。"""
    raw = _truthy_mode(os.getenv("ALIOTH_MORELOGIN_API_MODE") or "auto")
    if raw in ("open", "openapi", "remote"):
        return "open"
    if raw in ("local", "client"):
        return "local"
    if resolve_morelogin_api_id() and resolve_morelogin_api_key():
        return "open"
    return "local"


def resolve_morelogin_open_api_base() -> str:
    return (os.getenv("ALIOTH_MORELOGIN_OPEN_API") or _DEFAULT_OPEN_API).strip().rstrip(
        "/"
    )


def resolve_morelogin_local_api_base() -> str:
    return (os.getenv("ALIOTH_MORELOGIN_LOCAL_API") or _DEFAULT_LOCAL_API).strip().rstrip(
        "/"
    )


def resolve_morelogin_api_base() -> str:
    if resolve_morelogin_api_mode() == "open":
        return resolve_morelogin_open_api_base()
    return resolve_morelogin_local_api_base()


def morelogin_transport_kind(api_base: str | None = None) -> str:
    """根据基址判断路径前缀与是否带 OAuth。显式 localhost 仍走 Local。"""
    base = (api_base or resolve_morelogin_api_base()).rstrip("/").lower()
    if "127.0.0.1" in base or "localhost" in base or ":40000" in base:
        return "local"
    if "api.morelogin.com" in base:
        return "open"
    return resolve_morelogin_api_mode()


def morelogin_cloudphone_url(action: str, *, api_base: str | None = None) -> str:
    base = (api_base or resolve_morelogin_api_base()).rstrip("/")
    prefix = "/cloudphone" if morelogin_transport_kind(base) == "open" else "/api/cloudphone"
    return f"{base}{prefix}/{action.lstrip('/')}"


def reset_morelogin_open_token_cache() -> None:
    """测试用：清空 Open API token 缓存。"""
    global _cached_token, _cached_token_expires_at
    with _token_lock:
        _cached_token = ""
        _cached_token_expires_at = 0.0


def _parse_json_object(raw: str, *, context: str) -> dict[str, Any]:
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise MoreLoginAdbError(f"{context} 回傳非 JSON：{raw[:200]}") from exc
    if not isinstance(data, dict):
        raise MoreLoginAdbError(f"{context} 回傳格式異常：{type(data).__name__}")
    return data


def _http_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout_seconds: float,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return 200, _parse_json_object(raw, context=url)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code in (400, 401, 403) and detail.strip().startswith("{"):
            parsed = _parse_json_object(detail, context=url)
            return exc.code, parsed
        raise MoreLoginAdbError(
            f"MoreLogin API HTTP {exc.code}：{url} {detail[:300]}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise MoreLoginAdbError(f"MoreLogin API 連線失敗：{url}（{exc}）") from exc


def _fetch_open_access_token(*, timeout_seconds: float = 20.0) -> tuple[str, float]:
    api_id = resolve_morelogin_api_id()
    api_key = resolve_morelogin_api_key()
    if not api_id or not api_key:
        raise MoreLoginAdbError(
            "Open API 模式需要 ALIOTH_MORELOGIN_API_ID 与 ALIOTH_MORELOGIN_API_KEY"
            "（MoreLogin 客户端「设置 → API & MCP」，服务器无需安装客户端）"
        )
    token_url = f"{resolve_morelogin_open_api_base()}/oauth2/token"
    status, data = _http_json(
        token_url,
        {
            "client_id": api_id,
            "client_secret": api_key,
            "grant_type": "client_credentials",
        },
        headers=_json_headers(),
        timeout_seconds=timeout_seconds,
    )
    if status != 200 or int(data.get("code") or 0) != 0:
        msg = str(data.get("msg") or data.get("error") or data)
        if data.get("cloudflare_error") or data.get("error_code") == 1010:
            raise MoreLoginAdbError(
                "MoreLogin Open API 被 Cloudflare 拦截（1010）。"
                "请确认出口 IP／User-Agent 未被站点封禁，"
                "或设置 ALIOTH_MORELOGIN_HTTP_USER_AGENT 后重试"
            )
        raise MoreLoginAdbError(f"MoreLogin OAuth2 换票失败：HTTP {status} {msg}")
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    token = str(inner.get("access_token") or "").strip()
    if not token:
        raise MoreLoginAdbError("MoreLogin OAuth2 回传缺少 access_token")
    try:
        expires_in = float(inner.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600.0
    return token, max(60.0, expires_in)


def _open_api_bearer(*, force_refresh: bool = False, timeout_seconds: float = 20.0) -> str:
    global _cached_token, _cached_token_expires_at
    now = time.time()
    with _token_lock:
        if (
            not force_refresh
            and _cached_token
            and now < (_cached_token_expires_at - _TOKEN_SKEW_SECONDS)
        ):
            return _cached_token
        token, expires_in = _fetch_open_access_token(timeout_seconds=timeout_seconds)
        _cached_token = token
        _cached_token_expires_at = time.time() + expires_in
        return token


def post_morelogin_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """POST JSON。Open API 自动带 Bearer，401 清缓存后重试一次。"""
    headers = _json_headers()
    use_open = "api.morelogin.com" in url.lower() and "/oauth2/" not in url.lower()
    if not use_open:
        status, data = _http_json(url, payload, headers=headers, timeout_seconds=timeout_seconds)
        if status != 200:
            raise MoreLoginAdbError(f"MoreLogin API HTTP {status}：{url} {data}")
        return data

    for attempt, force in enumerate((False, True)):
        token = _open_api_bearer(force_refresh=force, timeout_seconds=timeout_seconds)
        auth_headers = {**headers, "Authorization": f"Bearer {token}"}
        status, data = _http_json(
            url, payload, headers=auth_headers, timeout_seconds=timeout_seconds
        )
        if status in (401, 403) and attempt == 0:
            reset_morelogin_open_token_cache()
            continue
        if status != 200:
            raise MoreLoginAdbError(f"MoreLogin Open API HTTP {status}：{url} {data}")
        return data
    raise MoreLoginAdbError(f"MoreLogin Open API 认证失败：{url}")
