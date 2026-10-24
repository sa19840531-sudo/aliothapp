"""规则 url_pattern 候选过滤回归。"""

import pytest

from services.api.app.domain.rule.url_scope import (
    InvalidRuleUrlPatternError,
    candidate_rule_matches_page_url,
    compile_rule_url_pattern,
    normalize_rule_scope_url,
    page_url_matches_pattern,
)


def test_normalize_rule_scope_url_lowercases_scheme_and_host() -> None:
    assert normalize_rule_scope_url("HTTPS://Example.COM/path?q=1") == (
        "https://example.com/path?q=1"
    )


def test_candidate_rule_without_pattern_matches_any_url() -> None:
    matches, reason = candidate_rule_matches_page_url(page_url=None, url_pattern=None)
    assert matches is True
    assert reason is None


def test_candidate_rule_with_pattern_fails_closed_without_page_url() -> None:
    matches, _ = candidate_rule_matches_page_url(
        page_url=None,
        url_pattern=r"example\.com/home",
    )
    assert matches is False


def test_candidate_rule_url_pattern_hit_and_miss() -> None:
    page_url = "https://example.com/home/index"
    hit, reason = candidate_rule_matches_page_url(
        page_url=page_url,
        url_pattern=r"example\.com/home",
    )
    miss, _ = candidate_rule_matches_page_url(
        page_url=page_url,
        url_pattern=r"example\.com/profile",
    )
    assert hit is True
    assert reason == "url_pattern"
    assert miss is False


def test_invalid_url_pattern_is_fail_closed() -> None:
    with pytest.raises(InvalidRuleUrlPatternError):
        compile_rule_url_pattern("(")
    matches, _ = candidate_rule_matches_page_url(
        page_url="https://example.com",
        url_pattern=".*",
    )
    assert matches is False


def test_page_url_matches_pattern_uses_search() -> None:
    assert page_url_matches_pattern(
        "https://example.com/app/home",
        r"/home$",
    )
