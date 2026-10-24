"""Web 逻辑页面身份与 Capture 版本聚合测试。"""

from services.api.app.application.web_logical_page import (
    build_web_logical_pages,
    canonical_web_logical_page_url,
    web_logical_page_id,
)


def test_hash_routes_are_distinct_and_anchors_are_not() -> None:
    key = canonical_web_logical_page_url
    assert key("https://example.test/#/list") != key("https://example.test/#/detail/1")
    assert key("https://example.test/#!/detail/1") != key("https://example.test/#!/detail/2")
    assert key("https://example.test/article/1#comments") == key("https://example.test/article/1")


def test_home_menu_cannot_override_business_route() -> None:
    from services.api.app.application.web_logical_page import resolve_web_logical_page_role

    assert resolve_web_logical_page_role(
        canonical_url="https://example.test/#/detail/1",
        menu_entry_ids=("site.home",),
        asserted_roles=("home", "detail"),
    ) == "detail"


def test_canonical_url_removes_tracking_noise_but_preserves_business_query() -> None:
    canonical = canonical_web_logical_page_url(
        "HTTP://Example.COM:80/path?id=42&utm_source=feed&spm=abc#player"
    )

    assert canonical == "http://example.com/path?id=42"
    assert canonical_web_logical_page_url("https://example.com?id=42") == (
        "https://example.com/?id=42"
    )
    assert canonical_web_logical_page_url("http://example.com/path?id=42") == canonical
    assert canonical_web_logical_page_url("https://example.com/path?id=43") != canonical


def test_capture_versions_share_one_logical_page_and_select_content_evidence() -> None:
    captures = [
        {
            "page_capture_id": "capture-ready",
            "url": "https://example.com/watch?id=42&utm_campaign=x",
            "capture_purpose": "web_page_ready_baseline",
            "page_seq": 1,
        },
        {
            "page_capture_id": "capture-cleared",
            "url": "https://EXAMPLE.com/watch?id=42#after-popup",
            "capture_purpose": "web_popup_cleared_recapture",
            "page_seq": 2,
        },
        {
            "page_capture_id": "capture-content",
            "url": "https://example.com/watch?id=42&ref=timeline",
            "capture_purpose": "web_primary_content_evidence",
            "page_seq": 3,
        },
        {
            "page_capture_id": "capture-other",
            "url": "https://example.com/watch?id=43",
            "capture_purpose": "web_page_ready_baseline",
            "page_seq": 4,
        },
    ]

    pages = build_web_logical_pages(captures)

    assert len(pages) == 2
    first = pages[0]
    assert first.logical_page_id == web_logical_page_id(
        "https://example.com/watch?id=42"
    )
    assert first.capture_ids == (
        "capture-ready",
        "capture-cleared",
        "capture-content",
    )
    assert first.primary_capture_id == "capture-content"


def test_local_state_is_removed_without_losing_unknown_encoded_business_values() -> None:
    key = canonical_web_logical_page_url
    assert key("https://example.test/#/detail?id=%FF&tab=info&page=2") == (
        "https://example.test/#/detail?id=%FF"
    )
    assert key("https://example.test/#/detail?id=%FF") != key("https://example.test/#/detail?id=%FE")
    assert key("https://example.test/?page=detail&unknown=1") != key("https://example.test/?page=detail&unknown=2")
    assert key("http://example.test/item/1") != key("https://example.test/item/1")


def test_unresolved_hash_requires_review_instead_of_merging() -> None:
    import pytest

    with pytest.raises(ValueError, match="WEB_URL_HASH_NEEDS_REVIEW"):
        canonical_web_logical_page_url("https://example.test/#view=detail&id=1")
