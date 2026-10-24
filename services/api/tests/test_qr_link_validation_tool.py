"""测试二维码链接校验 Tool：截图解码、URL 可达性与跳转异常识别。"""

import httpx
import pytest

from services.api.app.agents.tools import qr_link_validation_tool as qr_tool_module


@pytest.mark.asyncio
async def test_qr_link_validation_tool_decodes_image_and_validates_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.com/promo"
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)

    monkeypatch.setattr(qr_tool_module, "_decode_qr_texts", lambda **_: ["https://example.com/promo"])
    monkeypatch.setattr(
        qr_tool_module,
        "_create_http_client",
        lambda **_: httpx.AsyncClient(transport=transport, follow_redirects=True),
    )

    result = await qr_tool_module.qr_link_validation_tool(image_base64="aWdub3JlZA==")

    assert result["is_valid"] is True
    assert result["decoded_texts"] == ["https://example.com/promo"]
    assert result["decoded_urls"] == ["https://example.com/promo"]
    assert result["url_checks"][0]["reachable"] is True
    assert result["url_checks"][0]["browser_accessible"] is True


@pytest.mark.asyncio
async def test_qr_link_validation_tool_flags_cross_domain_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com" and request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://evil.example/login"})
        if request.url.host == "evil.example" and request.url.path == "/login":
            return httpx.Response(200, text="login")
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        qr_tool_module,
        "_create_http_client",
        lambda **_: httpx.AsyncClient(transport=transport, follow_redirects=True),
    )

    result = await qr_tool_module.qr_link_validation_tool(url="https://example.com/start")

    assert result["is_valid"] is False
    assert result["url_checks"][0]["reachable"] is True
    assert result["url_checks"][0]["redirect_anomaly"] is True
    assert result["url_checks"][0]["final_url"] == "https://evil.example/login"