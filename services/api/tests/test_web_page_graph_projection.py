"""Web 图专属站点范围沿用既有冻结 URL 和菜单领域边界。"""

from types import SimpleNamespace

import pytest

from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY
from services.api.app.infrastructure.db.web_page_graph_projection import _same_web_site


@pytest.mark.parametrize(
    "source, target, expected",
    [
        ("http://example.test/", "https://example.test/#/list", True),
        ("http://example.test:80/", "https://example.test:443/#/list", True),
        ("https://example.test/", "https://example.test:443/#/list", True),
        ("https://example.test/", "http://example.test/#/list", False),
        ("https://example.test/", "https://foreign.test/#/list", False),
        ("https://example.test/", "https://example.test:8443/#/list", False),
        ("http://example.test:8080/", "https://example.test/#/list", False),
        ("https://example.test/", "https://user:password@example.test/", False),
        ("https://example.test/", "javascript:alert(1)", False),
        (None, "https://example.test/", False),
    ],
)
def test_graph_uses_existing_site_origin_policy_without_inventing_permission(
    source, target, expected
):
    policy = SimpleNamespace(identity_policy=DEFAULT_WEB_URL_IDENTITY_POLICY)
    assert _same_web_site(target, source, policy) is expected
