"""任务级与跨页面证据输入集合的应用层持久化端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.api.app.domain.evidence import EvidenceInputSet


@dataclass(frozen=True, kw_only=True)
class PersistedEvidenceInputSet:
    """一次幂等封存的持久化结果。"""

    input_set: EvidenceInputSet
    created: bool


class EvidenceInputSetRepository(Protocol):
    """保存并读取不可变 EvidenceInputSet 的端口。"""

    async def add(
        self,
        *,
        input_set: EvidenceInputSet,
    ) -> PersistedEvidenceInputSet: ...

    async def get(self, *, input_set_id: str) -> EvidenceInputSet | None: ...
