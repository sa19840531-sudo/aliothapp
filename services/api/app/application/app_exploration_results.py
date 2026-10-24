"""App 探索结果的不可变持久化端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.api.app.domain.evidence import AppExplorationResult


@dataclass(frozen=True, kw_only=True)
class PersistedAppExplorationResult:
    """幂等写入后的探索结果。"""

    result: AppExplorationResult
    created: bool


class AppExplorationResultRepository(Protocol):
    """每个 App Agent Run 只允许一个不可变探索结果。"""

    async def create(
        self,
        *,
        result: AppExplorationResult,
    ) -> PersistedAppExplorationResult: ...

    async def get_by_run_id(self, *, run_id: str) -> AppExplorationResult | None: ...
