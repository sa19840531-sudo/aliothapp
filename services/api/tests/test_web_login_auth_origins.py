"""Web 登录认证源站配置只允许凭据白名单内的站点声明精确 HTTPS 源站。"""

import json

import pytest

from services.api.app.application.web_login_allowlist import parse_web_login_allowlist
from services.api.app.application.web_login_auth_origins import (
    DEFAULT_WEB_LOGIN_AUTH_ORIGINS_PATH,
    WebLoginAuthOriginsError,
    load_web_login_auth_origins,
    parse_web_login_auth_origins,
)


def test_exact_auth_origins_are_scoped_to_credential_site():
    allowlist = parse_web_login_allowlist(
        "网址：https://chigua.com\n账号：user\n密码：secret\n\n"
        "网址：https://hl365.com\n账号：user\n密码：secret\n"
    )
    config = json.dumps({
        "version": 1,
        "sites": {
            "https://chigua.com": [
                "https://52.221.236.23:15212",
                "https://bpi5.azmzyrwn.cc",
            ],
        },
    })

    policy = parse_web_login_auth_origins(config, allowlist=allowlist)

    assert policy.for_site("https://chigua.com") == frozenset({
        "https://52.221.236.23:15212", "https://bpi5.azmzyrwn.cc"
    })
    assert policy.for_site("https://hl365.com") == frozenset()
    assert "secret" not in repr(policy)


def test_checked_in_config_contains_only_four_approved_origins():
    sites = (
        "https://chigua.com", "https://hl365.com", "https://heiliao.com",
        "https://www.91jav1.com", "https://91porna.com",
        "https://huangguoai.com", "https://avhub360.com",
    )
    allowlist = parse_web_login_allowlist("\n\n".join(
        f"网址：{site}\n账号：test-user\n密码：test-secret" for site in sites
    ))
    policy = load_web_login_auth_origins(
        DEFAULT_WEB_LOGIN_AUTH_ORIGINS_PATH, allowlist=allowlist
    )

    assert policy.for_site("https://chigua.com") == frozenset({
        "https://52.221.236.23:15212", "https://bpi5.azmzyrwn.cc"
    })
    assert policy.for_site("https://hl365.com") == frozenset({
        "https://54.151.247.122:15212", "https://api1.gxmufvxq.cc"
    })
    assert all(not policy.for_site(site) for site in sites[2:])


@pytest.mark.parametrize("site,auth", [
    ("https://unknown.test", "https://auth.test"),
    ("https://chigua.com", "http://auth.test"),
    ("https://chigua.com", "https://*.auth.test"),
    ("https://chigua.com", "https://127.0.0.1:15212"),
    ("https://chigua.com", "https://auth.test/path"),
])
def test_unknown_site_or_nonexact_auth_origin_is_rejected(site, auth):
    allowlist = parse_web_login_allowlist(
        "网址：https://chigua.com\n账号：user\n密码：secret\n"
    )
    config = json.dumps({"version": 1, "sites": {site: [auth]}})

    with pytest.raises(WebLoginAuthOriginsError):
        parse_web_login_auth_origins(config, allowlist=allowlist)
