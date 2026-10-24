"""App 探索技术失败事实的持久化端口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.api.app.domain.evidence.app_failure import (
    AppExplorationFailureFact,
    AppExplorationFailureRecoveryState,
)


@dataclass(frozen=True, kw_only=True)
class PersistedAppExplorationFailure:
    """幂等写入后的技术失败事实。"""

    failure: AppExplorationFailureFact
    created: bool


class AppExplorationFailureRepository(Protocol):
    """按 Run 唯一保存失败，并为独立恢复机提供待对账事实。"""

    async def record(
        self,
        *,
        failure: AppExplorationFailureFact,
    ) -> PersistedAppExplorationFailure: ...

    async def get_by_run_id(
        self,
        *,
        run_id: str,
    ) -> AppExplorationFailureFact | None: ...

    async def list_pending(
        self,
        *,
        limit: int = 100,
    ) -> tuple[AppExplorationFailureFact, ...]: ...

    async def mark_reconciled(
        self,
        *,
        failure_id: str,
        failure_sha256: str,
        recovery_state: AppExplorationFailureRecoveryState,
        reconciled_at: datetime,
        sealed_index_id: str | None = None,
        result_id: str | None = None,
        session_status: str | None = None,
    ) -> AppExplorationFailureFact: ...
