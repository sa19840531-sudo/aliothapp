"""只接受浏览器自然重定向链，不能把普通 URL 变化当别名。"""

from types import SimpleNamespace


def test_redirect_chain_requires_natural_document_request_lineage():
    from services.api.app.infrastructure.browser.web_redirect_identity import (
        natural_web_redirect_chain,
    )

    first = SimpleNamespace(
        url="http://example.test/item/1",
        method="GET",
        resource_type="document",
        redirected_from=None,
    )
    last = SimpleNamespace(
        url="https://example.test/item/1",
        method="GET",
        resource_type="document",
        redirected_from=first,
    )
    assert natural_web_redirect_chain(SimpleNamespace(request=last)) == (
        "http://example.test/item/1",
        "https://example.test/item/1",
    )
    assert natural_web_redirect_chain(SimpleNamespace(request=first)) == ()
    assert natural_web_redirect_chain(None) == ()
    last.method = "POST"
    assert natural_web_redirect_chain(SimpleNamespace(request=last)) == ()


def test_access_upgrade_permission_is_not_page_identity_proof():
    from services.api.app.application.web_desktop_navigation import catalog_navigation_url_matches

    assert not catalog_navigation_url_matches("http://example.test/1", "https://example.test/1")
    assert catalog_navigation_url_matches(
        "http://example.test:80/1", "https://example.test/1", allow_http_upgrade=True
    )
    assert not catalog_navigation_url_matches(
        "http://example.test:8080/1", "https://example.test/1", allow_http_upgrade=True
    )
