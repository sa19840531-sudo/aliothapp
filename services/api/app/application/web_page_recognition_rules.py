"""纯函数计算 Web 结构门槛，模型和静态先验不能改变结果。"""

from dataclasses import asdict, dataclass
from typing import Any

from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.domain.page.web_page_knowledge import (
    WebFactPredicate,
    WebPageRecognitionRules,
)


@dataclass(frozen=True)
class WebPageRuleEvaluation:
    """每个页型独立、可序列化的规则审计。"""

    page_type: str
    required_satisfied: bool
    positive_hits: tuple[str, ...]
    supporting_hits: tuple[str, ...]
    strong_conflicts: tuple[str, ...]
    missing_facts: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


def evaluate_web_page_rules(
    *, page_type: str, facts: Any, rules: WebPageRecognitionRules
) -> WebPageRuleEvaluation:
    """一次评估全部条件；即使正向缺失也保留强冲突。"""

    def matches(predicate: WebFactPredicate) -> bool:
        regions = getattr(facts, "subject_region_facts", None)
        actual = (
            getattr(regions, predicate.fact, None)
            if predicate.fact in WebSubjectRegionFacts.__dataclass_fields__
            else getattr(facts, predicate.fact, None)
        )
        if actual is None:
            return False
        if predicate.operator == "contains_exact":
            return predicate.value in actual
        if predicate.operator == "eq":
            return type(actual) is type(predicate.value) and actual == predicate.value
        if predicate.operator == "gte":
            return type(actual) is int and actual >= predicate.value
        return type(actual) is int and actual <= predicate.value

    def label(predicate: WebFactPredicate) -> str:
        return f"{predicate.fact}:{predicate.operator}:{predicate.value}"

    all_hits = tuple(matches(item) for item in rules.required_all)
    any_hits = tuple(matches(item) for item in rules.required_any)
    return WebPageRuleEvaluation(
        page_type=page_type,
        required_satisfied=all(all_hits) and any(any_hits),
        positive_hits=tuple(
            label(item) for item in (*rules.required_all, *rules.required_any) if matches(item)
        ),
        supporting_hits=tuple(label(item) for item in rules.supporting if matches(item)),
        strong_conflicts=tuple(
            group.reason_code
            for group in rules.strong_negative_any
            if all(matches(item) for item in group.all_of)
        ),
        missing_facts=tuple(label(item) for item in rules.required_all if not matches(item))
        + (() if any(any_hits) else tuple(label(item) for item in rules.required_any)),
    )
