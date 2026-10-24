"""判定前的规则结论复用规划（04 专项 §17.5：先判适用，再定目标，最后才问哪份结论可沿用）。

只做三件事：读规则合同里的复用声明、在同一任务里找可沿用的正式结论、把命中的规则从
本次 Judge 的规则清单里拿掉并生成带来源与失效条件的复用判定。任何读库失败都按
「不复用、照常判定」处理——复用是省成本的优化，永远不能挡住判定。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from services.api.app.domain.inspection.handoff import InspectionHandoff, InspectionRuleTarget
from services.api.app.domain.inspection.judgement import RuleDecision, RuleJudgeOutcome
from services.api.app.domain.inspection.rule_reuse import (
    LOOKUP_REUSE_SCOPES,
    REUSABLE_SOURCE_STATUSES,
    RuleReuseRecord,
    RuleReuseScope,
    build_reused_decision,
    parse_reuse_declaration,
)

logger = logging.getLogger(__name__)

# 常见跟踪参数不参与对象身份：同一个页面地址带不同 utm 仍是同一个对象。
_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "from",
        "nonce",
        "rand",
        "random",
        "ref",
        "spm",
        "t",
        "timestamp",
        "ts",
        "_t",
    }
)


@dataclass(frozen=True, kw_only=True)
class CurrentCaptureFacts:
    """当前待判定页面的身份事实，只读。"""

    page_capture_id: str
    url: str = ""
    page_type: str = ""
    page_state: str = ""
    surface_kind: str = ""
    surface_session_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class ReusableExecutionCandidate:
    """同一任务里已经形成的正式结论，连同它所在页面与规则版本事实。"""

    execution_id: str
    rule_id: str
    rule_code: str
    execution_status: str
    page_capture_id: str
    page_url: str = ""
    page_type: str = ""
    surface_kind: str = ""
    agent_run_id: str | None = None
    contract_hash: str | None = None
    result_summary: str | None = None
    problem_desc: str | None = None
    confidence: float | None = None
    judge_output: Mapping[str, Any] | None = None
    created_at: datetime | None = None


class RuleReuseRepository(Protocol):
    async def load_capture_facts(self, *, page_capture_id: str) -> CurrentCaptureFacts | None: ...

    async def load_rule_contracts(
        self, *, rule_ids: Sequence[str]
    ) -> Mapping[str, Mapping[str, Any]]: ...

    async def list_reusable_executions(
        self, *, task_id: str, rule_ids: Sequence[str]
    ) -> Sequence[ReusableExecutionCandidate]: ...


@dataclass(frozen=True)
class RuleReusePlan:
    """本次工作项里哪些规则沿用结论、哪些仍要 Judge，以及没复用的原因。"""

    reused_decisions: tuple[RuleDecision, ...] = ()
    records: tuple[RuleReuseRecord, ...] = ()
    judge_rules: tuple[InspectionRuleTarget, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def reused(self) -> bool:
        return bool(self.reused_decisions)

    def judge_handoff(self, handoff: InspectionHandoff) -> InspectionHandoff | None:
        """给 Judge 的缩减版 Handoff；全部规则都沿用时返回 None，跳过 Judge。"""

        if not self.reused:
            return handoff
        if not self.judge_rules:
            return None
        kept_codes = {rule.rule_code for rule in self.judge_rules}
        return replace(
            handoff,
            rules=tuple(self.judge_rules),
            rule_evidence={
                code: reference
                for code, reference in handoff.rule_evidence.items()
                if code in kept_codes
            },
        )


def no_reuse_plan(handoff: InspectionHandoff, *, reason: str = "") -> RuleReusePlan:
    return RuleReusePlan(
        judge_rules=tuple(handoff.rules),
        skipped=tuple((rule.rule_code, reason) for rule in handoff.rules) if reason else (),
    )


def normalize_object_key(url: str | None) -> str:
    """把页面地址归一成对象身份：去 fragment、去跟踪参数、参数排序、大小写与尾斜杠统一。"""

    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme and not parts.netloc:
        return text.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not (key.lower().startswith("utm_") or key.lower() in _TRACKING_QUERY_KEYS)
    ]
    query.sort()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query, doseq=True), "")
    )


def merge_reused_decisions(
    *,
    handoff: InspectionHandoff,
    outcome: RuleJudgeOutcome | None,
    decisions: Sequence[RuleDecision],
) -> RuleJudgeOutcome:
    """把复用判定并进 Judge 输出；Judge 被整体跳过时以复用判定单独成 Outcome。"""

    reused = tuple(decisions)
    if outcome is None:
        if not reused:
            raise ValueError("既没有 Judge 输出也没有复用判定")
        return RuleJudgeOutcome(
            work_item_id=handoff.work_item_id,
            attempt_id=handoff.attempt_id,
            task_id=handoff.task_id,
            decisions=reused,
        )
    if not reused:
        return outcome
    return RuleJudgeOutcome(
        work_item_id=outcome.work_item_id,
        attempt_id=outcome.attempt_id,
        task_id=outcome.task_id,
        decisions=(*outcome.decisions, *reused),
        evidence_requests=outcome.evidence_requests,
        deferred_decisions=outcome.deferred_decisions,
    )


class RuleReusePlanner:
    """按规则声明在同一任务里找可沿用的结论；找不到或读不到一律照常判定。"""

    def __init__(self, *, repository: RuleReuseRepository, enabled: bool = True) -> None:
        self._repository = repository
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def plan(self, *, handoff: InspectionHandoff) -> RuleReusePlan:
        if not self._enabled:
            return no_reuse_plan(handoff, reason="reuse_disabled")
        try:
            return await self._plan(handoff)
        except Exception as exc:  # noqa: BLE001 - 复用只是优化，读库失败不能挡判定
            logger.warning(
                "task_id=%s work_item_id=%s 规则复用规划失败，本次全部照常判定：%s",
                handoff.task_id,
                handoff.work_item_id,
                exc,
            )
            return no_reuse_plan(handoff, reason="reuse_lookup_failed")

    async def _plan(self, handoff: InspectionHandoff) -> RuleReusePlan:
        rule_ids = [rule.rule_id for rule in handoff.rules]
        contracts = await self._repository.load_rule_contracts(rule_ids=rule_ids)
        scopes = {
            rule.rule_id: parse_reuse_declaration(contracts.get(rule.rule_id))
            for rule in handoff.rules
        }
        skipped: list[tuple[str, str]] = []
        lookup_rules = []
        for rule in handoff.rules:
            scope = scopes[rule.rule_id]
            if scope in LOOKUP_REUSE_SCOPES:
                lookup_rules.append(rule)
            else:
                skipped.append((rule.rule_code, f"scope_{scope.value}"))
        if not lookup_rules:
            return RuleReusePlan(judge_rules=tuple(handoff.rules), skipped=tuple(skipped))

        facts = await self._repository.load_capture_facts(
            page_capture_id=handoff.evidence.page_capture_id
        )
        if facts is None:
            facts = CurrentCaptureFacts(page_capture_id=handoff.evidence.page_capture_id)
        candidates = await self._repository.list_reusable_executions(
            task_id=handoff.task_id,
            rule_ids=[rule.rule_id for rule in lookup_rules],
        )
        by_rule: dict[str, list[ReusableExecutionCandidate]] = {}
        for candidate in candidates:
            by_rule.setdefault(candidate.rule_id, []).append(candidate)

        reused_decisions: list[RuleDecision] = []
        records: list[RuleReuseRecord] = []
        judge_rules: list[InspectionRuleTarget] = []
        for rule in handoff.rules:
            scope = scopes[rule.rule_id]
            if scope not in LOOKUP_REUSE_SCOPES:
                judge_rules.append(rule)
                continue
            match = _pick_source(
                scope=scope,
                rule=rule,
                facts=facts,
                candidates=by_rule.get(rule.rule_id, ()),
            )
            if isinstance(match, str):
                skipped.append((rule.rule_code, match))
                judge_rules.append(rule)
                continue
            candidate, record = match
            records.append(record)
            reused_decisions.append(
                build_reused_decision(
                    handoff=handoff,
                    rule=rule,
                    record=record,
                    source_result_summary=candidate.result_summary,
                    source_problem_desc=candidate.problem_desc,
                    source_confidence=candidate.confidence,
                    source_judge_output=candidate.judge_output,
                )
            )
        return RuleReusePlan(
            reused_decisions=tuple(reused_decisions),
            records=tuple(records),
            judge_rules=tuple(judge_rules),
            skipped=tuple(skipped),
        )


def _pick_source(
    *,
    scope: RuleReuseScope,
    rule: InspectionRuleTarget,
    facts: CurrentCaptureFacts,
    candidates: Sequence[ReusableExecutionCandidate],
) -> tuple[ReusableExecutionCandidate, RuleReuseRecord] | str:
    """按范围挑第一条满足依据的来源；返回字符串表示没复用的原因代码。"""

    if scope is RuleReuseScope.SAME_MATERIAL:
        # 素材身份（广告素材版本指纹）目前没有稳定来源，宁可照常判定也不猜同一素材。
        return "material_identity_unavailable"
    current_key = normalize_object_key(facts.url) if scope is RuleReuseScope.SAME_OBJECT else ""
    if scope is RuleReuseScope.SAME_OBJECT and not current_key:
        return "object_identity_unavailable"
    reason_code = "no_prior_conclusion"
    for candidate in candidates:
        if candidate.execution_status not in REUSABLE_SOURCE_STATUSES:
            continue
        if candidate.page_capture_id == facts.page_capture_id:
            continue
        if not candidate.contract_hash:
            reason_code = "rule_version_unknown"
            continue
        if candidate.contract_hash != rule.contract_hash:
            reason_code = "rule_version_changed"
            continue
        if (
            facts.surface_kind
            and candidate.surface_kind
            and facts.surface_kind != candidate.surface_kind
        ):
            reason_code = "environment_changed"
            continue
        if scope is RuleReuseScope.SAME_OBJECT:
            candidate_key = normalize_object_key(candidate.page_url)
            if candidate_key != current_key or (
                facts.page_type and candidate.page_type and facts.page_type != candidate.page_type
            ):
                reason_code = "object_changed"
                continue
            reuse_key = current_key
            reason = "同一个页面对象已经判过，沿用第一次的结论"
        else:
            reuse_key = f"task:{_task_key(candidate)}"
            reason = "整个任务只判一次，沿用首个合格结论"
        record = RuleReuseRecord(
            scope=scope,
            reuse_key=reuse_key,
            source_execution_id=candidate.execution_id,
            source_page_capture_id=candidate.page_capture_id,
            source_execution_status=candidate.execution_status,
            source_agent_run_id=candidate.agent_run_id,
            source_page_type=candidate.page_type,
            source_page_url=candidate.page_url,
            reason=reason,
            basis={
                "rule_contract_hash": rule.contract_hash,
                "surface_kind": facts.surface_kind or candidate.surface_kind,
                "current_page_type": facts.page_type,
                "current_page_url": facts.url,
                "source_surface_session_id": None,
                "current_surface_session_id": facts.surface_session_id,
            },
        )
        return candidate, record
    return reason_code


def _task_key(candidate: ReusableExecutionCandidate) -> str:
    return candidate.rule_id


def rule_result_reuse_enabled(settings: Any) -> bool:
    """从配置对象读开关；假配置对象没有这一节时按关闭处理。"""

    section = getattr(settings, "rule_result_reuse", None)
    return bool(getattr(section, "enabled", False))


def build_current_rule_reuse_planner(*, enabled: bool) -> RuleReusePlanner | None:
    """生产装配：开关关闭时不装规划器，消费者照常全量判定。"""

    if not enabled:
        return None
    from services.api.app.infrastructure.db.rule_reuse_repository import (
        PostgresRuleReuseRepository,
    )

    return RuleReusePlanner(repository=PostgresRuleReuseRepository(), enabled=True)


__all__ = [
    "CurrentCaptureFacts",
    "ReusableExecutionCandidate",
    "RuleReusePlan",
    "RuleReusePlanner",
    "RuleReuseRepository",
    "build_current_rule_reuse_planner",
    "merge_reused_decisions",
    "no_reuse_plan",
    "normalize_object_key",
    "rule_result_reuse_enabled",
]
