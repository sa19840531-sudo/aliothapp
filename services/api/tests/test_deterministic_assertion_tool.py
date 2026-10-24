"""测试 deterministic_assertion_tool：确定性断言的最小可用能力。"""

import pytest

from services.api.app.agents.tools.assertion_tools import evaluate_deterministic_assertion
from services.api.app.agents.tools.registry import PermissionLevel, default_registry


def test_regex_match_assertion_passes_when_pattern_matches() -> None:
    result = evaluate_deterministic_assertion(
        assertion_type="regex_match",
        payload={"text": "接口返回 500 错误", "pattern": r"\b500\b"},
    )

    assert result["passed"] is True
    assert result["execution_status"] == "pass"
    assert result["details"]["matched"] is True


def test_text_contains_assertion_fails_when_expected_text_missing() -> None:
    result = evaluate_deterministic_assertion(
        assertion_type="text_contains",
        payload={"text": "个人中心", "expected": "退出登录"},
    )

    assert result["passed"] is False
    assert result["execution_status"] == "fail"
    assert result["details"]["expected"] == "退出登录"


def test_number_compare_assertion_supports_lte_operator() -> None:
    result = evaluate_deterministic_assertion(
        assertion_type="number_compare",
        payload={"left": 3, "operator": "lte", "right": 3},
    )

    assert result["passed"] is True
    assert result["details"]["operator"] == "lte"


def test_dom_exists_assertion_checks_selector_and_text() -> None:
    result = evaluate_deterministic_assertion(
        assertion_type="dom_exists",
        payload={
            "elements": [
                {"selector": "#logout", "text": "退出登录"},
                {"selector": ".profile", "text": "我的"},
            ],
            "selector": "#logout",
            "text": "退出登录",
        },
    )

    assert result["passed"] is True
    assert result["details"]["matched_element"]["selector"] == "#logout"


def test_unsupported_assertion_type_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        evaluate_deterministic_assertion(assertion_type="unsupported", payload={})


def test_deterministic_assertion_tool_is_registered_as_safe() -> None:
    metadata = default_registry.get("deterministic_assertion_tool")

    assert metadata is not None
    assert metadata.permission_level == PermissionLevel.SAFE
