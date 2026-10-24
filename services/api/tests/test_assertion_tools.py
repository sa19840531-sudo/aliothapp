"""确定性断言 Tool 契约测试。"""

from services.api.app.agents.tools.assertion_tools import (
    deterministic_assertion_tool,
    evaluate_deterministic_assertion,
)


def test_regex_not_match_assertion_uses_negative_match_semantics() -> None:
    result = evaluate_deterministic_assertion(
        assertion_type="regex_not_match",
        payload={"text": "页面加载完成", "pattern": "TypeError|HTTP 500"},
    )

    assert result["passed"] is True
    assert result["details"]["matched"] is False
    assert result["details"]["should_match"] is False


def test_regex_alias_uses_regex_match_semantics() -> None:
    result = evaluate_deterministic_assertion(
        assertion_type="regex",
        payload={"text": "Alioth 2026", "pattern": r"Alioth\s+\d+"},
    )

    assert result["passed"] is True


def test_assertion_tool_schema_exposes_supported_types_as_enum() -> None:
    assertion_type = deterministic_assertion_tool.parameters()["properties"]["assertion_type"]

    assert set(assertion_type["enum"]) == {
        "regex",
        "regex_match",
        "regex_not_match",
        "text_contains",
        "number_compare",
        "dom_exists",
    }
