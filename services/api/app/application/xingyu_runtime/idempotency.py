"""星語 Official 冪等鍵記憶體表：同 key 同 payload 回快取；異 payload → 衝突。"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

ResponseKind = Literal["json", "png"]


@dataclass(frozen=True)
class IdempotentRecord:
    """快取的成功／業務回應。"""

    payload_hash: str
    kind: ResponseKind
    status_code: int
    json_body: dict[str, Any] | None = None
    png_bytes: bytes | None = None
    headers: dict[str, str] | None = None


class IdempotencyConflict(Exception):
    """同一 Idempotency-Key 對應不同請求體。"""


class XingyuIdempotencyStore:
    """執行緒安全的冪等快取。"""

    def __init__(
        self,
        *,
        png_ttl_seconds: float = 3600,
        max_png_bytes: int = 128 * 1024 * 1024,
        max_png_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(png_ttl_seconds) or png_ttl_seconds <= 0:
            raise ValueError("截图快取期限必须为有限正数")
        if max_png_bytes <= 0 or max_png_entries <= 0:
            raise ValueError("截图快取容量必须为正数")
        self._lock = threading.Lock()
        self._items: dict[str, IdempotentRecord] = {}
        # 图片独立限制，不能因截图淘汰动作的幂等结果。
        self._png_order: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._png_bytes = 0
        self._png_ttl = png_ttl_seconds
        self._max_png_bytes = max_png_bytes
        self._max_png_entries = max_png_entries
        self._clock = clock

    def _remove_png(self, key: str) -> None:
        """调用方持锁；同步释放图片及容量记账。"""
        _, size = self._png_order.pop(key)
        self._png_bytes -= size
        self._items.pop(key, None)

    def _prune_png(self) -> None:
        """按写入顺序惰性回收，读取不延长重试窗口。"""
        now = self._clock()
        while self._png_order:
            key = next(iter(self._png_order))
            expires, _ = self._png_order[key]
            if (
                expires > now
                and self._png_bytes <= self._max_png_bytes
                and len(self._png_order) <= self._max_png_entries
            ):
                break
            self._remove_png(key)

    @staticmethod
    def hash_payload(payload: Any) -> str:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def lookup(self, key: str, *, payload: Any) -> IdempotentRecord | None:
        payload_hash = self.hash_payload(payload)
        with self._lock:
            self._prune_png()
            existing = self._items.get(key)
            if existing is None:
                return None
            if existing.payload_hash != payload_hash:
                raise IdempotencyConflict(key)
            return existing

    def store_json(
        self,
        key: str,
        *,
        payload: Any,
        status_code: int,
        body: dict[str, Any],
    ) -> IdempotentRecord:
        record = IdempotentRecord(
            payload_hash=self.hash_payload(payload),
            kind="json",
            status_code=status_code,
            json_body=dict(body),
        )
        with self._lock:
            self._prune_png()
            if key in self._png_order:
                self._remove_png(key)
            self._items[key] = record
        return record

    def store_png(
        self,
        key: str,
        *,
        payload: Any,
        status_code: int,
        png_bytes: bytes,
        headers: dict[str, str],
    ) -> IdempotentRecord:
        record = IdempotentRecord(
            payload_hash=self.hash_payload(payload),
            kind="png",
            status_code=status_code,
            png_bytes=png_bytes,
            headers=dict(headers),
        )
        with self._lock:
            self._prune_png()
            if key in self._png_order:
                self._remove_png(key)
            # 超大图片仍可返回给本次调用，但不挤占整个重试快取。
            if len(png_bytes) > self._max_png_bytes:
                return record
            self._items[key] = record
            self._png_order[key] = (self._clock() + self._png_ttl, len(png_bytes))
            self._png_bytes += len(png_bytes)
            self._prune_png()
        return record

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._png_order.clear()
            self._png_bytes = 0
