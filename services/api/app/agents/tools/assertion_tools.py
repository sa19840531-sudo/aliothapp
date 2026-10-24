"""确定性断言 Tool：对页面采集结果做可复现、可审计的规则引擎判断。"""

import re
from typing import Any, Literal

from services.api.app.agents.tools.registry import PermissionLevel, tool

DeterministicAssertionType = Literal[
    "regex",
    "regex_match",
    "regex_not_match",
    "text_contains",
    "number_compare",
    "dom_exists",
]


def evaluate_deterministic_assertion(
    *,
    assertion_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """执行一条确定性断言，返回统一的 pass/fail 结构。"""
    if assertion_type in {"regex", "regex_match"}:
        return _regex_match(payload)
    if assertion_type == "regex_not_match":
        return _regex_match({**payload, "should_match": False})
    if assertion_type == "text_contains":
        return _text_contains(payload)
    if assertion_type == "number_compare":
        return _number_compare(payload)
    if assertion_type == "dom_exists":
        return _dom_exists(payload)
    raise ValueError(f"unsupported assertion_type: {assertion_type}")


@tool(
    name="deterministic_assertion_tool",
    description="对页面采集结果执行正则、文本包含、数值比较、DOM 存在性等确定性断言。",
    category="校验",
    permission_level=PermissionLevel.SAFE,
)
def deterministic_assertion_tool(
    assertion_type: DeterministicAssertionType, payload: dict[str, Any]
) -> dict[str, Any]:
    """Agent 可调用的确定性断言入口。"""
    return evaluate_deterministic_assertion(assertion_type=assertion_type, payload=payload)


def _regex_match(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text", ""))
    pattern = str(payload["pattern"])
    should_match = bool(payload.get("should_match", True))
    matched = re.search(pattern, text) is not None
    passed = matched is should_match
    return _result(
        assertion_type="regex_match",
        passed=passed,
        summary="正则断言通过" if passed else "正则断言未通过",
        details={
            "pattern": pattern,
            "matched": matched,
            "should_match": should_match,
        },
    )


def _text_contains(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text", ""))
    expected = str(payload["expected"])
    should_contain = bool(payload.get("should_contain", True))
    contained = expected in text
    passed = contained is should_contain
    return _result(
        assertion_type="text_contains",
        passed=passed,
        summary="文本包含断言通过" if passed else "文本包含断言未通过",
        details={
            "expected": expected,
            "contained": contained,
            "should_contain": should_contain,
        },
    )


def _number_compare(payload: dict[str, Any]) -> dict[str, Any]:
    left = float(payload["left"])
    right = float(payload["right"])
    operator = str(payload["operator"])
    comparators = {
        "lt": left < right,
        "lte": left <= right,
        "eq": left == right,
        "gte": left >= right,
        "gt": left > right,
    }
    if operator not in comparators:
        raise ValueError(f"unsupported number operator: {operator}")
    passed = comparators[operator]
    return _result(
        assertion_type="number_compare",
        passed=passed,
        summary="数值比较断言通过" if passed else "数值比较断言未通过",
        details={
            "left": left,
            "operator": operator,
            "right": right,
        },
    )


def _dom_exists(payload: dict[str, Any]) -> dict[str, Any]:
    elements = payload.get("elements", [])
    if not isinstance(elements, list):
        raise ValueError("elements must be a list")
    selector = payload.get("selector")
    text = payload.get("text")
    should_exist = bool(payload.get("should_exist", True))
    matched_element = _find_element(elements=elements, selector=selector, text=text)
    exists = matched_element is not None
    passed = exists is should_exist
    return _result(
        assertion_type="dom_exists",
        passed=passed,
        summary="DOM 存在性断言通过" if passed else "DOM 存在性断言未通过",
        details={
            "selector": selector,
            "text": text,
            "exists": exists,
            "should_exist": should_exist,
            "matched_element": matched_element,
        },
    )


def _find_element(
    *,
    elements: list[Any],
    selector: Any,
    text: Any,
) -> dict[str, Any] | None:
    for item in elements:
        if not isinstance(item, dict):
            continue
        selector_matched = selector is None or item.get("selector") == selector
        text_matched = text is None or str(text) in str(item.get("text", ""))
        if selector_matched and text_matched:
            return item
    return None


def _result(
    *,
    assertion_type: str,
    passed: bool,
    summary: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "assertion_type": assertion_type,
        "passed": passed,
        "execution_status": "pass" if passed else "fail",
        "summary": summary,
        "details": details,
    }
