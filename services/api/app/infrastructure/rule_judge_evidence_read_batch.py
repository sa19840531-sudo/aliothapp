"""单个确定性判定批次内复用已校验原文，不缓存规则或授权结论。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

ArtifactReadKey = tuple[str, str, int | None]


@dataclass
class _ReadBatch:
    payloads: dict[ArtifactReadKey, bytes] = field(default_factory=dict)
    size_bytes: int = 0
    active: bool = True


class EvidenceReadBatchCache:
    """只在显式作用域内缓存成功校验的字节，并限制内存占用。"""

    def __init__(self, *, max_bytes: int = 16 * 1024 * 1024) -> None:
        if max_bytes < 0:
            raise ValueError("批次证据缓存大小不能为负数")
        self._max_bytes = max_bytes
        self._current: ContextVar[_ReadBatch | None] = ContextVar(
            "rule_judge_evidence_read_batch", default=None
        )

    @contextmanager
    def scope(self) -> Iterator[None]:
        batch = _ReadBatch()
        token = self._current.set(batch)
        try:
            yield
        finally:
            batch.active = False
            batch.payloads.clear()
            batch.size_bytes = 0
            self._current.reset(token)

    async def read(
        self,
        *,
        key: ArtifactReadKey,
        load: Callable[[], Awaitable[bytes]],
        validate: Callable[[bytes], None],
    ) -> bytes:
        batch = self._current.get()
        payload = batch.payloads.get(key) if batch is not None else None
        cached = payload is not None
        if payload is None:
            payload = await load()
        try:
            validate(payload)
        except Exception:
            if batch is not None and cached:
                batch.size_bytes -= len(batch.payloads.pop(key))
            raise
        if batch is not None and not cached and batch.size_bytes + len(payload) <= self._max_bytes:
            batch.payloads[key] = payload
            batch.size_bytes += len(payload)
        return payload
