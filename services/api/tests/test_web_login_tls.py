"""登录前 TLS 校验保持证书验证，并容忍一次短暂网络超时。"""

import httpx
import pytest

from services.api.app.infrastructure.browser.web_login_tls import verify_web_login_tls


@pytest.mark.asyncio
async def test_proxy_tls_check_retries_one_timeout_without_disabling_verification(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, origin):
            if len(calls) == 1:
                raise httpx.ReadTimeout("temporary timeout")
            return object()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    assert await verify_web_login_tls(
        "https://example.test", proxy_server="http://127.0.0.1:7897"
    )
    assert len(calls) == 2
    assert all(call["verify"] is True for call in calls)
