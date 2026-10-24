"""规则判定正式写回的应用层端口。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from services.api.app.domain.inspection import InspectionHandoff, RuleJudgeOutcome


class RuleDecisionWriter(Protocol):
    """只负责持久化已形成的 Rule Decision，不执行规则判定。"""

    async def write(
        self,
        *,
        handoff: InspectionHandoff,
        outcome: RuleJudgeOutcome,
    ) -> Mapping[str, Any]: ...
