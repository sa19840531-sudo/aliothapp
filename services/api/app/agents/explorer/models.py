"""Explorer Agent 的严格结构化模型输出。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.api.app.domain.exploration import ExplorationTerminalStatus


class ExplorerDecisionEnvelopeModel(BaseModel):
    """供 Framework 解码 JSON 与基础字段类型的结构信封。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["explorer-decision.v1"] = "explorer-decision.v1"
    kind: Literal["action", "terminal"]
    candidate_id: str | None = Field(default=None, min_length=1, max_length=256)
    purpose: str | None = Field(default=None, min_length=1, max_length=1_000)
    terminal_status: ExplorationTerminalStatus | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=2_000)


class ExplorerDecisionModel(ExplorerDecisionEnvelopeModel):
    """模型只选择平台候选或声明终态，不得自行构造 ActionIntent。"""

    @model_validator(mode="after")
    def validate_variant(self) -> ExplorerDecisionModel:
        """动作与终态字段必须严格互斥。"""
        if self.kind == "action":
            if self.candidate_id is None or self.purpose is None:
                raise ValueError("action 必须包含 candidate_id 和 purpose")
            if self.terminal_status is not None or self.reason is not None:
                raise ValueError("action 不能包含终态字段")
            return self
        if self.terminal_status is None or self.reason is None:
            raise ValueError("terminal 必须包含 terminal_status 和 reason")
        if self.candidate_id is not None or self.purpose is not None:
            raise ValueError("terminal 不能包含动作字段")
        return self
