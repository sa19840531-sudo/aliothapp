"""单次 Run 的规则执行上下文：统一关联页面、证据与候选规则。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from services.api.app.agents.runtime.run_context import get_agent_run_context


@dataclass
class PageRuleExecutionContext:
    """一个页面采集对应的规则执行事实。"""

    page_capture_id: str
    artifact_ids: list[str] = field(default_factory=list)
    page_type: str | None = None
    candidate_rules: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class RuleExecutionContext:
    """维护当前 Run 内可供写回 Tool 复用的可信上下文。"""

    _pages: dict[str, PageRuleExecutionContext] = field(default_factory=dict, repr=False)

    def remember_page_capture(self, result: Mapping[str, Any]) -> None:
        page_capture_id = _text(result.get("page_capture_id"))
        if page_capture_id is None:
            return
        page = self._pages.setdefault(
            page_capture_id,
            PageRuleExecutionContext(page_capture_id=page_capture_id),
        )
        page.artifact_ids = _string_list(result.get("artifact_ids"))

    def remember_candidate_rules(
        self,
        *,
        page_capture_id: str | None,
        page_type: str | None,
        rules: object,
    ) -> dict[str, Any] | None:
        if page_capture_id is None or not isinstance(rules, list):
            return None
        page = self._pages.get(page_capture_id)
        if page is None:
            return None
        page.page_type = page_type
        page.candidate_rules.clear()
        for raw_rule in rules:
            if not isinstance(raw_rule, Mapping):
                continue
            rule = dict(raw_rule)
            for key in _rule_keys(rule):
                page.candidate_rules[key] = rule
        return self.page_payload(page_capture_id)

    def enrich_single(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        enriched = dict(arguments)
        page_capture_id = self._resolve_page_capture_id(enriched)
        if page_capture_id is None:
            return enriched
        page = self._pages.get(page_capture_id)
        rule = self._candidate_rule(page, enriched)
        if page is None or rule is None or _identity_conflicts(enriched, rule):
            return enriched

        enriched["page_capture_id"] = page_capture_id
        _set_if_missing(enriched, "rule_id", rule.get("rule_id"))
        _set_if_missing(enriched, "rule_code", rule.get("rule_code"))
        _set_if_missing(enriched, "judge_strategy", rule.get("executor_type"))
        _set_if_missing(enriched, "severity", rule.get("severity"))
        if not _string_list(enriched.get("evidence_artifact_ids")) and page.artifact_ids:
            enriched["evidence_artifact_ids"] = list(page.artifact_ids)
        enriched["judge_input"] = _merge_judge_input(
            enriched.get("judge_input"),
            page=page,
        )
        return enriched

    def enrich_batch(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        enriched = dict(arguments)
        raw_items = arguments.get("executions")
        if not isinstance(raw_items, list):
            return enriched
        page_capture_id = _text(arguments.get("page_capture_id"))
        if page_capture_id is None:
            inferred = {
                candidate
                for item in raw_items
                if isinstance(item, Mapping)
                for candidate in [self._resolve_page_capture_id(item)]
                if candidate is not None
            }
            if len(inferred) == 1:
                page_capture_id = inferred.pop()
        if page_capture_id is None:
            return enriched
        enriched["page_capture_id"] = page_capture_id
        enriched["executions"] = [
            self.enrich_single({**dict(item), "page_capture_id": page_capture_id})
            if isinstance(item, Mapping)
            else item
            for item in raw_items
        ]
        return enriched

    def enrich_judge(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """为统一 Judge 注入当前页面采集的可信制品与执行上下文。"""
        enriched = dict(arguments)
        page_capture_id = _text(arguments.get("page_capture_id"))
        if page_capture_id is None:
            return enriched
        page = self._pages.get(page_capture_id)
        if page is None:
            return enriched
        if page.artifact_ids and not _string_list(enriched.get("evidence_artifact_ids")):
            enriched["evidence_artifact_ids"] = list(page.artifact_ids)
        raw_rules = enriched.get("rules")
        if isinstance(raw_rules, list):
            enriched["rules"] = [
                {
                    **dict(item),
                    "judge_input": _merge_judge_input(item.get("judge_input"), page=page),
                }
                if isinstance(item, Mapping)
                else item
                for item in raw_rules
            ]
        else:
            enriched["judge_input"] = _merge_judge_input(
                enriched.get("judge_input"),
                page=page,
            )
        return enriched
    def validation_error(self, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        """阻断已建立候选集页面上的非候选规则或身份冲突。"""
        page_capture_id = self._resolve_page_capture_id(arguments)
        if page_capture_id is None:
            return None
        page = self._pages.get(page_capture_id)
        if page is None or not page.candidate_rules:
            return None
        rule = self._candidate_rule(page, arguments)
        if rule is None:
            return _validation_error(
                code="RULE_NOT_IN_PAGE_CANDIDATES",
                message="规则不属于该页面采集的候选规则集合，禁止写回。",
                page_capture_id=page_capture_id,
                arguments=arguments,
            )
        if _identity_conflicts(arguments, rule):
            return _validation_error(
                code="RULE_EXECUTION_IDENTITY_CONFLICT",
                message="规则写回身份与页面候选规则不一致，禁止自动补齐。",
                page_capture_id=page_capture_id,
                arguments=arguments,
            )
        return None

    def page_payload(self, page_capture_id: str) -> dict[str, Any] | None:
        page = self._pages.get(page_capture_id)
        if page is None:
            return None
        return {
            "page_capture_id": page.page_capture_id,
            "page_type": page.page_type,
            "evidence_artifact_ids": list(page.artifact_ids),
            "candidate_rule_count": len(
                {
                    _text(rule.get("rule_id")) or _text(rule.get("rule_code"))
                    for rule in page.candidate_rules.values()
                }
                - {None}
            ),
        }

    def _resolve_page_capture_id(self, arguments: Mapping[str, Any]) -> str | None:
        explicit = _text(arguments.get("page_capture_id"))
        if explicit is not None:
            return explicit
        keys = _rule_keys(arguments)
        matches = {
            page.page_capture_id
            for page in self._pages.values()
            if any(key in page.candidate_rules for key in keys)
        }
        return matches.pop() if len(matches) == 1 else None

    @staticmethod
    def _candidate_rule(
        page: PageRuleExecutionContext | None,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if page is None:
            return None
        for key in _rule_keys(arguments):
            if rule := page.candidate_rules.get(key):
                return rule
        return None


def _merge_judge_input(value: object, *, page: PageRuleExecutionContext) -> dict[str, Any]:
    judge_input = dict(value) if isinstance(value, Mapping) else {}
    execution_context: dict[str, Any] = {
        "page_capture_id": page.page_capture_id,
        "page_type": page.page_type,
        "evidence_artifact_ids": list(page.artifact_ids),
    }
    run_context = get_agent_run_context()
    if run_context is not None:
        execution_context.update(
            {
                "task_id": run_context.task_id,
                "agent_run_id": run_context.agent_run_id,
            }
        )
    judge_input["execution_context"] = execution_context
    return judge_input


def _identity_conflicts(arguments: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    return any(
        actual is not None and expected is not None and actual != expected
        for actual, expected in (
            (_text(arguments.get("rule_id")), _text(rule.get("rule_id"))),
            (_text(arguments.get("rule_code")), _text(rule.get("rule_code"))),
        )
    )


def _validation_error(
    *,
    code: str,
    message: str,
    page_capture_id: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "execution_status": "blocked",
        "error_code": code,
        "retryable": True,
        "page_capture_id": page_capture_id,
        "rule_id": _text(arguments.get("rule_id")),
        "rule_code": _text(arguments.get("rule_code")),
        "message": message,
    }


def _set_if_missing(target: dict[str, Any], key: str, value: object) -> None:
    if _text(target.get(key)) is None and value is not None:
        target[key] = value


def _rule_keys(value: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    if rule_id := _text(value.get("rule_id")):
        keys.append(f"id:{rule_id}")
    if rule_code := _text(value.get("rule_code")):
        keys.append(f"code:{rule_code}")
    return keys


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [text for item in value if (text := _text(item)) is not None]


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
