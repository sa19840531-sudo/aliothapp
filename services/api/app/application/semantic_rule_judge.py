"""独立语义规则 Judge 的结构化输出契约。"""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.api.app.application.observation_packet import ObservationPacket

SemanticConclusion = Literal[
    "pass",
    "fail",
    "warning",
    "manual_review",
    "evidence_insufficient",
]
MAX_SEMANTIC_JUDGE_OUTPUT_BYTES = 8_192


class SemanticJudgeFinding(BaseModel):
    """语义 Judge 的单条证据化发现。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=600)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    element_refs: list[str] = Field(default_factory=list, max_length=20)


class SemanticJudgeModelOutput(BaseModel):
    """模型必须返回的严格判定结构。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["semantic-rule-judge.v1"] = "semantic-rule-judge.v1"
    conclusion: SemanticConclusion
    confidence: float = Field(ge=0, le=1)
    reasoning_summary: str = Field(min_length=1, max_length=2_000)
    findings: list[SemanticJudgeFinding] = Field(default_factory=list, max_length=50)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    missing_evidence: list[str] = Field(default_factory=list, max_length=50)
    conflicts: list[str] = Field(default_factory=list, max_length=50)
    review_reason: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_conclusion_evidence(self) -> SemanticJudgeModelOutput:
        """风险结论必须指向证据，证据不足结论必须说明缺口。"""
        if self.conclusion in {"fail", "warning"} and not (
            self.evidence_refs
            or any(finding.evidence_refs for finding in self.findings)
        ):
            raise ValueError("fail/warning 结论必须引用证据 artifact")
        if self.conclusion == "evidence_insufficient" and not self.missing_evidence:
            raise ValueError("evidence_insufficient 必须说明缺失证据")
        serialized = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(serialized) > MAX_SEMANTIC_JUDGE_OUTPUT_BYTES:
            raise ValueError(
                "语义 Judge 输出超过审计写回上限："
                f"{len(serialized)} > {MAX_SEMANTIC_JUDGE_OUTPUT_BYTES} 字节"
            )
        return self


class SemanticJudgeBatchItem(BaseModel):
    """语义批次内按规则编码隔离的一条结构化输出。"""

    model_config = ConfigDict(extra="forbid")

    rule_code: str = Field(min_length=1, max_length=128)
    result: SemanticJudgeModelOutput


class SemanticJudgeBatchOutput(BaseModel):
    """同页、同类别语义规则的小批次输出。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["semantic-rule-judge-batch.v1"] = (
        "semantic-rule-judge-batch.v1"
    )
    items: list[SemanticJudgeBatchItem] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_unique_rule_codes(self) -> SemanticJudgeBatchOutput:
        codes = [item.rule_code for item in self.items]
        if len(codes) != len(set(codes)):
            raise ValueError("语义批次 rule_code 不得重复")
        return self


class SemanticRuleJudge(Protocol):
    """独立语义判定器契约。"""

    async def judge(
        self,
        *,
        packet: ObservationPacket,
        tool_results: list[dict[str, Any]],
    ) -> SemanticJudgeModelOutput:
        """只根据规则配置、可信证据包和确定性 Tool 结果输出结论。"""

    async def judge_batch(
        self,
        *,
        items: list[tuple[ObservationPacket, list[dict[str, Any]]]],
    ) -> dict[str, SemanticJudgeModelOutput]:
        """按 rule_code 独立返回同页小批次结果。"""
