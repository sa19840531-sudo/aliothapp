"""规则质检 Agent 输出与证据补采请求的稳定领域合同。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from services.api.app.domain.inspection.evidence_request import EvidenceRequest

RULE_JUDGE_OUTCOME_PROTOCOL_VERSION = "rule-judge-outcome.v1"
RULE_DECISION_PROTOCOL_VERSION = "rule-decision.v1"

_WRITER_FIELDS = (
    "execution_status",
    "judge_strategy",
    "confidence",
    "severity",
    "result_summary",
    "problem_desc",
    "evidence_artifact_ids",
    "judge_input",
    "judge_output",
    "review_status",
    "review_reason",
    "error_code",
    "error_message",
)


class RuleJudgeOutcomeValidationError(ValueError):
    """规则判定输出缺少稳定身份或与工作项目标不一致。"""


@dataclass(frozen=True, kw_only=True)
class RuleDecision:
    """Rule Judge 对单条锁定规则形成的只读判定。"""

    work_item_id: str
    attempt_id: str
    rule_id: str
    rule_code: str
    execution_status: str
    observation_packet_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = RULE_DECISION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "work_item_id",
            "attempt_id",
            "rule_id",
            "rule_code",
            "execution_status",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.observation_packet_id is not None:
            _require_text(self.observation_packet_id, "observation_packet_id")
        if self.protocol_version != RULE_DECISION_PROTOCOL_VERSION:
            raise RuleJudgeOutcomeValidationError("RuleDecision protocol_version 不受支持")
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "payload"))

    @classmethod
    def from_payload(
        cls,
        *,
        work_item_id: str,
        attempt_id: str,
        rule_id: str,
        rule_code: str,
        payload: Mapping[str, Any],
    ) -> RuleDecision:
        """把当前判定实现的输出锁定到工作项、Attempt 与规则身份。"""
        raw = dict(payload)
        _validate_optional_identity(raw.get("rule_id"), rule_id, "rule_id")
        _validate_optional_identity(raw.get("rule_code"), rule_code, "rule_code")
        execution_status = _require_text(raw.get("execution_status"), "execution_status")
        observation_packet_id = _optional_text(raw.get("observation_packet_id"))
        error = raw.get("error")
        if isinstance(error, Mapping):
            raw.setdefault("error_code", _optional_text(error.get("code")))
            raw.setdefault("error_message", _optional_text(error.get("message")))
        return cls(
            work_item_id=work_item_id,
            attempt_id=attempt_id,
            rule_id=rule_id,
            rule_code=rule_code,
            execution_status=execution_status,
            observation_packet_id=observation_packet_id,
            payload=raw,
        )

    def to_writer_payload(self) -> dict[str, Any]:
        """返回 Result Writer 可写入的白名单字段。"""
        raw = _thaw_mapping(self.payload)
        payload = {key: raw[key] for key in _WRITER_FIELDS if key in raw}
        payload.update(
            {
                "rule_id": self.rule_id,
                "rule_code": self.rule_code,
                "execution_status": self.execution_status,
                "observation_packet_id": self.observation_packet_id,
            }
        )
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "work_item_id": self.work_item_id,
            "attempt_id": self.attempt_id,
            **self.to_writer_payload(),
        }


@dataclass(frozen=True, kw_only=True)
class RuleJudgeOutcome:
    """一次 Handoff 的完整 Judge 输出，不承担正式结果写回。"""

    work_item_id: str
    attempt_id: str
    task_id: str
    decisions: tuple[RuleDecision, ...] = ()
    evidence_requests: tuple[EvidenceRequest, ...] = ()
    # 随补采请求一起暂缓的证据不足判定：请求被门禁抑制时，写回层用它把原始
    # 证据缺口（judge_output.evidence_gap）原样落库，缺口台账不因链路关闭而丢失。
    deferred_decisions: tuple[RuleDecision, ...] = ()
    protocol_version: str = RULE_JUDGE_OUTCOME_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_text(self.work_item_id, "work_item_id")
        _require_text(self.attempt_id, "attempt_id")
        _require_text(self.task_id, "task_id")
        decisions = tuple(self.decisions)
        requests = tuple(self.evidence_requests)
        deferred = tuple(self.deferred_decisions)
        if self.protocol_version != RULE_JUDGE_OUTCOME_PROTOCOL_VERSION:
            raise RuleJudgeOutcomeValidationError("RuleJudgeOutcome protocol_version 不受支持")
        if not decisions and not requests:
            raise RuleJudgeOutcomeValidationError("Judge 输出不能同时缺少判定与证据请求")
        if any(not isinstance(item, RuleDecision) for item in decisions):
            raise RuleJudgeOutcomeValidationError("decisions 必须由 RuleDecision 组成")
        if any(not isinstance(item, EvidenceRequest) for item in requests):
            raise RuleJudgeOutcomeValidationError("evidence_requests 必须由 EvidenceRequest 组成")
        identities: list[tuple[str, str]] = []
        for item in (*decisions, *requests):
            if item.work_item_id != self.work_item_id or item.attempt_id != self.attempt_id:
                raise RuleJudgeOutcomeValidationError("Judge 子项身份与 Outcome 不一致")
            identities.append((item.rule_id, item.rule_code))
        if any(item.task_id != self.task_id for item in requests):
            raise RuleJudgeOutcomeValidationError("Evidence Request 任务身份与 Outcome 不一致")
        if len(set(identities)) != len(identities):
            raise RuleJudgeOutcomeValidationError("同一规则不能重复输出判定或证据请求")
        if any(not isinstance(item, RuleDecision) for item in deferred):
            raise RuleJudgeOutcomeValidationError("deferred_decisions 必须由 RuleDecision 组成")
        requested_rules = {item.rule_id for item in requests}
        for item in deferred:
            if item.work_item_id != self.work_item_id or item.attempt_id != self.attempt_id:
                raise RuleJudgeOutcomeValidationError("暂缓判定身份与 Outcome 不一致")
            if item.rule_id not in requested_rules:
                raise RuleJudgeOutcomeValidationError("暂缓判定只能对应已发出证据请求的规则")
        if len({item.rule_id for item in deferred}) != len(deferred):
            raise RuleJudgeOutcomeValidationError("同一规则不能重复暂缓判定")
        object.__setattr__(self, "decisions", decisions)
        object.__setattr__(self, "evidence_requests", requests)
        object.__setattr__(self, "deferred_decisions", deferred)

    def deferred_decision_for(self, rule_id: str) -> RuleDecision | None:
        """返回某条已发请求规则的暂缓判定；没有则为 None。"""
        for item in self.deferred_decisions:
            if item.rule_id == rule_id:
                return item
        return None

    @property
    def retryable(self) -> bool:
        return any(item.retryable for item in self.evidence_requests)

    def to_dict(self) -> dict[str, Any]:
        status = (
            "success"
            if self.decisions and not self.evidence_requests
            else ("partial_success" if self.decisions else "evidence_requested")
        )
        return {
            "protocol_version": self.protocol_version,
            "work_item_id": self.work_item_id,
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "execution_status": status,
            "retryable": self.retryable,
            "items": [item.to_writer_payload() for item in self.decisions],
            "decisions": [item.to_dict() for item in self.decisions],
            "evidence_requests": [item.to_dict() for item in self.evidence_requests],
            "deferred_decisions": [item.to_dict() for item in self.deferred_decisions],
        }


def _validate_optional_identity(value: Any, expected: str, field_name: str) -> None:
    actual = _optional_text(value)
    if actual is not None and actual != expected:
        raise RuleJudgeOutcomeValidationError(f"Judge 输出的 {field_name} 与工作项不一致")


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RuleJudgeOutcomeValidationError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _require_text(value, "可选文本")


def _freeze_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuleJudgeOutcomeValidationError(f"{field_name} 必须为对象")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise RuleJudgeOutcomeValidationError(f"{field_name} 包含非法字段名")
        frozen[key] = _freeze_json(item, field_name)
    return MappingProxyType(frozen)


def _freeze_json(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value, field_name)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field_name) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise RuleJudgeOutcomeValidationError(f"{field_name} 不能包含 NaN 或无穷大")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise RuleJudgeOutcomeValidationError(f"{field_name} 只能包含 JSON 兼容值")


def _thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _thaw_json(item) for key, item in value.items()}


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _thaw_mapping(value)
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
