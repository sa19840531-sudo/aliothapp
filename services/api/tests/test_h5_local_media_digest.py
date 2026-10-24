"""本地媒体摘要只提供身份事实，不扩大网络、内容或关闭权限。"""

import asyncio
import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.infrastructure.browser.h5_local_media_digest import (
    _MAX_BYTES,
    _digest_local_media,
    ensure_h5_local_media_digest,
)


@pytest.mark.parametrize(
    "data", [b"", b"local-asset", bytes(range(256)), b"x" * _MAX_BYTES],
    ids=["empty", "local-asset", "all-byte-values", "maximum-size"],
)
def test_local_media_digest_matches_standard_sha256(data, request):
    assert len(request.node.nodeid) < 256
    assert _digest_local_media(base64.b64encode(data).decode()) == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("encoded", [
    None, "not-base64!", base64.b64encode(b"x" * (_MAX_BYTES + 1)).decode(),
], ids=["missing", "invalid-base64", "oversized"])
def test_local_media_digest_rejects_invalid_or_oversized_input(encoded, request):
    assert len(request.node.nodeid) < 256
    with pytest.raises(ValueError):
        _digest_local_media(encoded)


@pytest.mark.asyncio
async def test_digest_binding_is_once_per_page_even_when_capture_and_modal_race():
    page = SimpleNamespace(expose_function=AsyncMock())
    await asyncio.gather(*(ensure_h5_local_media_digest(page) for _ in range(5)))
    page.expose_function.assert_awaited_once_with("__aliothH5LocalMediaDigest", _digest_local_media)


@pytest.mark.asyncio
async def test_digest_registration_failure_does_not_mark_page_bound():
    page = SimpleNamespace(expose_function=AsyncMock(
        side_effect=[RuntimeError("page closed"), None],
    ))
    with pytest.raises(RuntimeError, match="page closed"):
        await ensure_h5_local_media_digest(page)
    await ensure_h5_local_media_digest(page)
    assert page.expose_function.await_count == 2
