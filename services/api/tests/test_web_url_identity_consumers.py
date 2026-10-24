"""同一 URL 矩阵贯穿 Web 菜单、详情、聚合和冻结版本。"""

import pytest

from services.api.app.application.web_desktop_navigation import (
    canonical_navigation_identity,
)
from services.api.app.application.web_logical_page import canonical_web_logical_page_url
from services.api.app.domain.page.web_site_menu_registry import canonical_menu_url
from services.api.tests.test_web_desktop_navigation import (
    _runtime_navigation_candidates as extract_navigation_candidates,
)


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://example.test/#/detail/1?tab=info&id=%FF",
            "https://example.test/#/detail/1?id=%FF",
        ),
        ("https://example.test/#!/detail/2?id=42&page=2", "https://example.test/#!/detail/2?id=42"),
        ("https://example.test/article/1#comments", "https://example.test/article/1"),
        ("http://example.test/?page=detail&id=2", "http://example.test/?page=detail&id=2"),
    ],
)
def test_catalog_and_logical_identity_share_url_matrix(url, expected):
    assert canonical_web_logical_page_url(url) == expected
    assert canonical_menu_url(url, site_origin="http://example.test") == expected


def test_navigation_keeps_spa_targets_separate():
    candidates = extract_navigation_candidates(
        '<nav><a href="/#/list">列表</a><a href="/#/detail/1">详情</a></nav>',
        base_url="https://example.test/",
    )
    assert [item.url for item in candidates] == [
        "https://example.test/#/list",
        "https://example.test/#/detail/1",
    ]
    assert canonical_navigation_identity(candidates[0].url) != canonical_navigation_identity(
        candidates[1].url
    )
    assert canonical_navigation_identity(
        "http://example.test/list"
    ) != canonical_navigation_identity("https://example.test/list")


def test_legacy_frozen_identity_fails_closed_without_rewriting_body():
    from services.api.tests.test_web_run_policy_snapshot import policy_payload, snapshot

    body = policy_payload()
    body["identity_policy_version"] = "web-url-identity.v1"
    with pytest.raises(ValueError, match="WEB_POLICY_VERSION_UNSUPPORTED"):
        snapshot(body)
    assert body["identity_policy_version"] == "web-url-identity.v1"
