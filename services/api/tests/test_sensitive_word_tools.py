"""测试 sensitive_word_detection_tool：禁止内容库多分类穷举扫描。"""

import pytest

from services.api.app.agents.tools import sensitive_word_tools  # noqa: F401  # 触发 Tool 注册
from services.api.app.agents.tools.registry import PermissionLevel, default_registry
from services.api.app.agents.tools.sensitive_word_tools import detect_sensitive_words
from services.api.app.domain.rule.forbidden_content import (
    REQUIRED_FORBIDDEN_CONTENT_CATEGORIES,
    scan_sensitive_words,
)


def test_scan_sensitive_words_returns_multi_category_hits_with_positions() -> None:
    result = scan_sensitive_words("评论里出现赌博广告，也出现钓鱼链接诱导。")

    hits = result["hits"]

    assert result["execution_status"] == "success"
    assert result["hit_count"] == 2
    assert result["effective_hit_count"] == 2
    assert [hit["category"] for hit in hits] == ["gambling", "phishing"]
    assert hits[0]["term"] == "赌博"
    assert hits[0]["start"] == 5
    assert hits[0]["end"] == 7
    assert hits[0]["is_whitelisted"] is False


def test_scan_sensitive_words_keeps_whitelisted_hits_auditable() -> None:
    result = scan_sensitive_words("页面展示反恐怖宣传专题。")

    assert result["hit_count"] == 1
    assert result["effective_hit_count"] == 0
    assert result["hits"][0]["category"] == "terrorism"
    assert result["hits"][0]["is_whitelisted"] is True


def test_scan_sensitive_words_returns_empty_result_for_empty_text() -> None:
    result = scan_sensitive_words("")

    assert result["hit_count"] == 0
    assert result["effective_hit_count"] == 0
    assert result["hits"] == []


def test_forbidden_content_catalog_has_required_categories() -> None:
    assert REQUIRED_FORBIDDEN_CONTENT_CATEGORIES == (
        "political",
        "terrorism",
        "personal_attack",
        "china_related",
        "adult",
        "gambling",
        "private_ad",
        "phishing",
        "uk_politics",
        "minor_redline",
    )


def test_minor_redline_scope_flags_any_hit_without_tiering() -> None:
    result = scan_sensitive_words(
        "首页推荐位出现未成年相关内容。",
        categories=["minor_redline"],
    )

    assert result["scanned_categories"] == ["minor_redline"]
    assert result["hit_count"] >= 1
    assert {hit["category"] for hit in result["hits"]} == {"minor_redline"}
    assert any(hit["term"] == "未成年" for hit in result["hits"])


def test_scan_sensitive_words_respects_category_scope() -> None:
    result = scan_sensitive_words("评论提到赌博，还聊到脱欧公投。", categories=["uk_politics"])

    assert result["scanned_categories"] == ["uk_politics"]
    assert {hit["category"] for hit in result["hits"]} == {"uk_politics"}
    assert any(hit["term"] == "脱欧" for hit in result["hits"])


def test_scan_sensitive_words_rejects_unknown_category_scope() -> None:
    with pytest.raises(ValueError, match="uk_weather"):
        scan_sensitive_words("任意文本", categories=["uk_weather"])


def test_ascii_terms_match_whole_words_case_insensitively() -> None:
    result = scan_sensitive_words(
        "The desirable outcome was praised, but the ira claimed responsibility.",
        categories=["uk_politics"],
    )

    ira_hits = [hit for hit in result["hits"] if hit["term"] == "IRA"]
    assert len(ira_hits) == 1
    assert ira_hits[0]["match_type"] == "word"
    assert result["text_length"] > ira_hits[0]["end"]


def test_ascii_terms_do_not_match_inside_longer_words() -> None:
    result = scan_sensitive_words("desirable snapshot dupont", categories=["uk_politics"])

    assert result["hit_count"] == 0


def test_detect_sensitive_words_tool_function_returns_scanner_result() -> None:
    result = detect_sensitive_words("请警惕私人广告和加微信引流。")

    assert result["hit_count"] == 2
    assert {hit["category"] for hit in result["hits"]} == {"private_ad"}


def test_sensitive_word_detection_tool_is_registered_as_safe_text_tool() -> None:
    metadata = default_registry.get("sensitive_word_detection_tool")

    assert metadata is not None
    assert metadata.category == "text"
    assert metadata.permission_level == PermissionLevel.SAFE

