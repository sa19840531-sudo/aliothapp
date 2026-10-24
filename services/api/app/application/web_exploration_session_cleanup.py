"""Web V2 纯探索会话的遗留动作收敛服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class WebExplorationSessionCleanupRepository(Protocol):
    async def fail_abandoned_evidence_pending(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> int: ...


@dataclass(frozen=True, kw_only=True)
class WebExplorationSessionCleanup:
    """仅收敛 Web V2 draining Session 中无法补齐的证据动作。"""

    repository: WebExplorationSessionCleanupRepository

    async def cleanup(self, *, task_id: str, session_id: str) -> int:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id 不能为空")
        if (
            not isinstance(session_id, str)
            or not session_id.startswith("web:")
            or not session_id.removeprefix("web:").strip()
        ):
            raise ValueError("Web V2 cleanup 只接受 web: Session")
        return await self.repository.fail_abandoned_evidence_pending(
            task_id=task_id.strip(),
            session_id=session_id,
        )


__all__ = [
    "WebExplorationSessionCleanup",
    "WebExplorationSessionCleanupRepository",
]
