"""Web 登录白名单只解析站点与秘密引用，不暴露账号密码。"""

import pytest

from services.api.app.application.web_login_allowlist import (
    WebLoginAllowlistError,
    parse_web_login_allowlist,
)


def test_parse_exact_https_site_and_keep_secret_out_of_safe_view():
    config = parse_web_login_allowlist(
        "网址：https://example.com\n账号：13800138000\n密码：test-secret\n"
    )

    profile = config.resolve("https://example.com/path")
    assert profile is not None
    assert profile.origin == "https://example.com"
    assert profile.identifier_kind == "phone"
    assert config.resolve("https://example.com/video?id=12#play") is profile
    assert config.resolve("https://www.example.com/") is None
    assert "test-secret" not in repr(config)
    assert "13800138000" not in repr(config)


def test_http_attachment_site_requires_https_before_credential_release():
    config = parse_web_login_allowlist(
        "网址：http://example.com\n账号：13800138000\n密码：test-secret\n"
    )

    assert config.resolve("http://example.com/") is None
    profile = config.resolve("https://example.com/")
    assert profile is not None
    assert profile.origin == "https://example.com"


def test_duplicate_site_and_unexpected_lines_are_rejected():
    with pytest.raises(WebLoginAllowlistError):
        parse_web_login_allowlist(
            "网址：https://example.com\n账号：13800138000\n密码：one\n\n"
            "网址：http://example.com\n账号：13800138000\n密码：two\n"
        )
    with pytest.raises(WebLoginAllowlistError):
        parse_web_login_allowlist(
            "网址：https://example.com\n账号：13800138000\n密码：one\n执行命令：x\n"
        )
