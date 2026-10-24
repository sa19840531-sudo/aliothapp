"""H5 本地媒体身份使用运行端摘要，不依赖网站的安全上下文。"""

import asyncio
import base64
import hashlib
from typing import Any

_MAX_BYTES = 5 * 1024 * 1024
_BINDING = "__aliothH5LocalMediaDigest"


def _digest_local_media(encoded: str) -> str:
    if not isinstance(encoded, str) or len(encoded) > 4 * ((_MAX_BYTES + 2) // 3):
        raise ValueError("H5 本地媒体超过摘要大小边界")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > _MAX_BYTES:
        raise ValueError("H5 本地媒体超过摘要大小边界")
    return hashlib.sha256(data).hexdigest()


async def ensure_h5_local_media_digest(page: Any) -> None:
    """只向 H5 Page 暴露有界纯摘要，不替换 crypto 或输出原始媒体证据。"""
    expose = getattr(page, "expose_function", None)
    if not callable(expose):
        return
    lock = getattr(page, "_alioth_h5_digest_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        page._alioth_h5_digest_lock = lock
    async with lock:
        if not getattr(page, "_alioth_h5_digest_bound", False):
            await expose(_BINDING, _digest_local_media)
            page._alioth_h5_digest_bound = True


H5_LOCAL_MEDIA_DIGEST_JS = """
    const h5LocalMediaDigest = async (bytes) => {
      if (globalThis.crypto?.subtle?.digest) {
        try {
          const digest = await crypto.subtle.digest('SHA-256', bytes);
          return [...new Uint8Array(digest)]
            .map(value => value.toString(16).padStart(2, '0')).join('');
        } catch {}
      }
      if (bytes.byteLength > 5 * 1024 * 1024) throw new Error('local_media_size_limit');
      const input = new Uint8Array(bytes);
      const chunks = [];
      for (let i = 0; i < input.length; i += 4096) {
        chunks.push(String.fromCharCode(...input.subarray(i, i + 4096)));
      }
      return await window.__aliothH5LocalMediaDigest(btoa(chunks.join('')));
    };
"""
