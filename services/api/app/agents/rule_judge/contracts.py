"""规则质检 Agent 的唯一输入与输出端口。"""

from __future__ import annotations

from typing import Protocol

from services.api.app.domain.inspection import InspectionHandoff, RuleJudgeOutcome


class RuleJudgeAgent(Protocol):
    """消费被领取的稳定工作项证据，返回判定或 Evidence Request。"""

    async def judge(self, handoff: InspectionHandoff) -> RuleJudgeOutcome: ...
