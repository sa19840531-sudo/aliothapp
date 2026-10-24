"""Web 菜单与详情最终 URL 的同站安全合同。"""

import pytest

from services.api.app.application.web_navigation_safety import (
    WebNavigationSafetyError,
    validate_web_final_url,
)


def test_http_default_port_may_upgrade_to_https_default_port() -> None:
    validate_web_final_url(
        site_url="http://example.test/",
        requested_url="http://example.test/menu",
        final_url="https://example.test/menu",
    )


@pytest.mark.parametrize(
    ("site_url", "requested_url", "final_url", "error_code"),
    [
        (
            "https://example.test/",
            "https://example.test/menu",
            "http://example.test/menu",
            "WEB_NAVIGATION_HTTPS_DOWNGRADE",
        ),
        (
            "https://example.test/",
            "https://example.test/menu",
            "https://other.test/menu",
            "WEB_NAVIGATION_CROSS_ORIGIN",
        ),
        (
            "https://example.test/",
            "https://example.test/menu",
            "https://example.test:8443/menu",
            "WEB_NAVIGATION_PORT_DRIFT",
        ),
        (
            "https://example.test/",
            "https://example.test/menu",
            "https://user:secret@example.test/menu",
            "WEB_NAVIGATION_URL_INVALID",
        ),
    ],
)
def test_unsafe_final_url_fails_closed(
    site_url: str,
    requested_url: str,
    final_url: str,
    error_code: str,
) -> None:
    with pytest.raises(WebNavigationSafetyError) as raised:
        validate_web_final_url(
            site_url=site_url,
            requested_url=requested_url,
            final_url=final_url,
        )

    assert raised.value.error_code == error_code
