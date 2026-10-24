"""H5 主动登录配置的域名作用域与秘密边界回归测试。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from services.api.app.core import config as config_module
from services.api.app.core.h5_auth_config import (
    FileH5CredentialConfigResolver,
    H5AuthConfigError,
    H5CredentialProfileRef,
)

_PHONE_CANARY = "AUTH_CANARY_PHONE_8917"
_PASSWORD_CANARY = "AUTH_CANARY_PASSWORD_6241"


def _write_config(tmp_path, payload: dict[str, object]):
    path = tmp_path / "h5-auth.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _valid_payload() -> dict[str, object]:
    return {
        "schema_version": "h5-auth-config.v1",
        "global": {
            "phone": _PHONE_CANARY,
            "password": _PASSWORD_CANARY,
        },
        "domains": {
            "h5.example.com": {
                "phone": "domain-phone",
                "password": "domain-password",
            }
        },
    }


def test_exact_hostname_profile_overrides_global(tmp_path) -> None:
    resolver = FileH5CredentialConfigResolver.from_path(
        _write_config(tmp_path, _valid_payload()),
        authorized_hostnames=("h5.example.com",),
    )

    ref = resolver.resolve_ref("H5.EXAMPLE.COM.")

    assert ref == H5CredentialProfileRef(scope="domain", hostname="h5.example.com")
    assert resolver.read_secret(ref).reveal_for_runtime() == (
        "domain-phone",
        "domain-password",
    )


def test_global_profile_is_used_only_for_an_authorized_exact_hostname(tmp_path) -> None:
    resolver = FileH5CredentialConfigResolver.from_path(
        _write_config(tmp_path, _valid_payload()),
        authorized_hostnames=("authorized.example.com",),
    )

    assert resolver.resolve_ref("sub.h5.example.com") is None
    ref = resolver.resolve_ref("authorized.example.com")

    assert ref == H5CredentialProfileRef(
        scope="global",
        hostname="authorized.example.com",
    )
    assert resolver.read_secret(ref).reveal_for_runtime() == (
        _PHONE_CANARY,
        _PASSWORD_CANARY,
    )


def test_secret_repr_and_safe_projection_never_disclose_canaries(tmp_path) -> None:
    resolver = FileH5CredentialConfigResolver.from_path(
        _write_config(tmp_path, _valid_payload()),
        authorized_hostnames=("authorized.example.com",),
    )
    ref = resolver.resolve_ref("authorized.example.com")
    assert ref is not None

    secret = resolver.read_secret(ref)
    rendered = " ".join((repr(secret), str(secret), json.dumps(secret.to_safe_dict())))

    assert _PHONE_CANARY not in rendered
    assert _PASSWORD_CANARY not in rendered
    assert rendered.count("redacted") >= 1


def test_synthetic_phone_strategy_generates_valid_phone_without_safe_projection(tmp_path) -> None:
    payload = {
        "schema_version": "h5-auth-config.v1",
        "domains": {
            "p1.51pj1.com": {
                "phone_strategy": "synthetic_cn_mobile",
                "password": "123456",
            }
        },
    }
    resolver = FileH5CredentialConfigResolver.from_path(
        _write_config(tmp_path, payload),
        authorized_hostnames=("p1.51pj1.com",),
    )
    ref = resolver.resolve_ref("p1.51pj1.com")
    assert ref is not None

    phone, password = resolver.read_secret(ref).reveal_for_runtime()

    assert len(phone) == 11
    assert phone[:3] in {
        "138",
        "139",
        "150",
        "151",
        "152",
        "157",
        "158",
        "159",
        "182",
        "183",
        "187",
        "188",
    }
    assert phone.isdigit()
    assert password == "123456"
    assert phone not in json.dumps(ref.to_safe_dict(), ensure_ascii=False)


def test_registration_submission_mode_is_explicit_and_safe(tmp_path) -> None:
    payload = {
        "schema_version": "h5-auth-config.v1",
        "domains": {
            "p1.51pj1.com": {
                "phone_strategy": "synthetic_cn_mobile",
                "password": "123456",
                "submission_mode": "register_if_available",
                "allowed_request_hostnames": ["api.uszim.com"],
            }
        },
    }
    resolver = FileH5CredentialConfigResolver.from_path(
        _write_config(tmp_path, payload),
        authorized_hostnames=("p1.51pj1.com",),
    )

    ref = resolver.resolve_ref("p1.51pj1.com")

    assert ref is not None
    assert ref.submission_mode == "register_if_available"
    assert ref.uses_synthetic_phone is True
    assert ref.allowed_request_hostnames == ("api.uszim.com",)
    assert ref.to_safe_dict() == {
        "scope": "domain",
        "hostname": "p1.51pj1.com",
        "submission_mode": "register_if_available",
        "allowed_request_hostnames": ["api.uszim.com"],
    }


def test_auth_request_allowlist_rejects_non_exact_hostname(tmp_path) -> None:
    payload = _valid_payload()
    payload["domains"]["h5.example.com"]["allowed_request_hostnames"] = [
        "*.api.example.com"
    ]

    with pytest.raises(H5AuthConfigError):
        FileH5CredentialConfigResolver.from_path(
            _write_config(tmp_path, payload),
            authorized_hostnames=("h5.example.com",),
        )


@pytest.mark.parametrize(
    "invalid_hostname",
    (
        "*.example.com",
        ".example.com",
        "https://example.com",
        "example.com:443",
        "example.com/path",
        "user@example.com",
    ),
)
def test_config_rejects_non_exact_hostname_keys(tmp_path, invalid_hostname: str) -> None:
    payload = _valid_payload()
    payload["domains"] = {
        invalid_hostname: {
            "phone": "domain-phone",
            "password": "domain-password",
        }
    }

    with pytest.raises(H5AuthConfigError, match="hostname"):
        FileH5CredentialConfigResolver.from_path(
            _write_config(tmp_path, payload),
            authorized_hostnames=("example.com",),
        )


def test_config_rejects_missing_password(tmp_path) -> None:
    payload = _valid_payload()
    payload["global"] = {"phone": _PHONE_CANARY}

    with pytest.raises(H5AuthConfigError, match="配置合同无效"):
        FileH5CredentialConfigResolver.from_path(
            _write_config(tmp_path, payload),
            authorized_hostnames=("example.com",),
        )


def test_h5_auth_settings_are_disabled_without_reading_a_file(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_H5_AUTH_ENABLED", "false")
    monkeypatch.setenv("ALIOTH_H5_AUTH_CONFIG_PATH", "missing-h5-auth.json")

    settings = config_module._build_h5_auth_settings()

    assert settings.enabled is False
    assert str(settings.config_path).endswith("missing-h5-auth.json")


def test_h5_auth_settings_require_existing_file_when_enabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ALIOTH_H5_AUTH_ENABLED", "true")
    monkeypatch.setenv(
        "ALIOTH_H5_AUTH_CONFIG_PATH",
        str(tmp_path / "missing-h5-auth.json"),
    )

    with pytest.raises(ValidationError, match="配置文件不存在"):
        config_module._build_h5_auth_settings()


def test_h5_auth_settings_load_enabled_path(monkeypatch, tmp_path) -> None:
    path = _write_config(tmp_path, _valid_payload())
    monkeypatch.setenv("ALIOTH_H5_AUTH_ENABLED", "true")
    monkeypatch.setenv("ALIOTH_H5_AUTH_CONFIG_PATH", str(path))

    settings = config_module._build_h5_auth_settings()

    assert settings.enabled is True
    assert settings.config_path == path
