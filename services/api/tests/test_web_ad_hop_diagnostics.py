"""广告 DNS 拒绝必须保留逐跳证据，且不能触发被拒绝地址连接。"""

import pytest

from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied
from services.api.app.infrastructure.browser import ad_safe_transport as transport_module


@pytest.mark.parametrize(
    "url",
    [
        'javascript:fetch("https://example.test/?token=private")',
        "data:text/html,<p>private</p>",
        "/private",
    ],
)
async def test_rejected_non_http_target_keeps_only_digest(url):
    events = []
    transport = transport_module.AdSafeTransport(timeout_seconds=10, diagnostic_events=events)
    with pytest.raises(AdOutboundDenied):
        await transport.fetch(url)
    assert events[0]["url"] == ""
    assert "private" not in str(events)
    assert len(events[0]["url_sha256"]) == 64


@pytest.mark.asyncio
async def test_nonpublic_subresource_records_dns_and_rejection_without_connect(monkeypatch):
    async def resolve(host, port):
        return ["0.0.0.0", "::"]

    def connect(*args):
        pytest.fail("非公网地址不得连接")

    monkeypatch.setattr(transport_module, "_resolve_addresses", resolve)
    monkeypatch.setattr(transport_module, "_fetch_pinned", connect)
    events = []
    transport = transport_module.AdSafeTransport(timeout_seconds=10, diagnostic_events=events)
    with pytest.raises(AdOutboundDenied, match="NONPUBLIC_ADDRESS"):
        await transport.fetch("https://ads.example/image?token=private", resource_kind="image")
    assert len(events) == 1
    assert events[0]["resolved_addresses"] == ["0.0.0.0", "::"]
    assert events[0]["denial_reason"] == "WEB_AD_OUTBOUND_NONPUBLIC_ADDRESS"
    assert events[0]["resource_type"] == "image"
    assert events[0]["url"] == "https://ads.example/image"
    assert len(events[0]["url_sha256"]) == 64
    assert "private" not in str(events)


@pytest.mark.asyncio
async def test_http_redirect_rejection_belongs_to_rejected_target(monkeypatch):
    async def resolve(host, port):
        assert host == "ads.example"
        return ["8.8.8.8"]

    def connect(url, *args):
        return transport_module.AdResponse(url, 302, {"location": "https://127.0.0.1/private"}, b"")

    monkeypatch.setattr(transport_module, "_resolve_addresses", resolve)
    monkeypatch.setattr(transport_module, "_fetch_pinned", connect)
    events = []
    transport = transport_module.AdSafeTransport(timeout_seconds=10, diagnostic_events=events)
    with pytest.raises(AdOutboundDenied):
        await transport.fetch(
            "https://ads.example/start", resource_kind="document", request_scope="main_document"
        )
    assert len(events) == 2
    assert events[0]["status"] == 302
    assert events[0]["denial_reason"] is None
    assert events[1]["url"] == "https://127.0.0.1/private"
    assert events[1]["denial_reason"]
    assert events[1]["resolved_addresses"] == []
