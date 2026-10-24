"""登录依赖闸门：事实源解析与仓储参数归一。"""

import pytest

from services.api.app.application import rule_auth_gate


class _FakeH5Auth:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled


class _FakeSettings:
    def __init__(self, enabled: bool) -> None:
        self.h5_auth = _FakeH5Auth(enabled)


def test_resolver_blocks_authenticated_rules_when_login_machine_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登录机关闭 ⇒ 浏览器会话必为游客 ⇒ 拦「仅登录态可判」的规则。"""
    monkeypatch.setattr(
        "services.api.app.core.config.get_settings", lambda: _FakeSettings(enabled=False)
    )
    assert (
        rule_auth_gate.resolve_blocked_auth_requirement()
        == rule_auth_gate.AUTH_REQUIREMENT_AUTHENTICATED
    )


def test_resolver_does_not_gate_when_login_machine_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登录机启用后会话状态未知（二期接账本前），闸门不动作。"""
    monkeypatch.setattr(
        "services.api.app.core.config.get_settings", lambda: _FakeSettings(enabled=True)
    )
    assert rule_auth_gate.resolve_blocked_auth_requirement() is None


def test_normalize_auth_gate_passthrough_auto_and_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert rule_auth_gate.normalize_auth_gate(None) is None
    assert rule_auth_gate.normalize_auth_gate("guest") == "guest"
    assert rule_auth_gate.normalize_auth_gate("authenticated") == "authenticated"

    monkeypatch.setattr(
        "services.api.app.core.config.get_settings", lambda: _FakeSettings(enabled=False)
    )
    assert rule_auth_gate.normalize_auth_gate(rule_auth_gate.AUTH_GATE_AUTO) == "authenticated"

    with pytest.raises(ValueError):
        rule_auth_gate.normalize_auth_gate("vip")


def test_resolve_blocked_auth_requirement_from_session_auth() -> None:
    assert (
        rule_auth_gate.resolve_blocked_auth_requirement_from_session_auth("guest")
        == rule_auth_gate.AUTH_REQUIREMENT_AUTHENTICATED
    )
    assert (
        rule_auth_gate.resolve_blocked_auth_requirement_from_session_auth("authenticated")
        == rule_auth_gate.AUTH_REQUIREMENT_GUEST
    )
    assert rule_auth_gate.resolve_blocked_auth_requirement_from_session_auth("unknown") is None
    assert (
        rule_auth_gate.resolve_blocked_auth_requirement_from_session_auth(
            None, no_test_account=True
        )
        == rule_auth_gate.AUTH_REQUIREMENT_AUTHENTICATED
    )


@pytest.mark.parametrize("no_test_account", [True, False])
@pytest.mark.parametrize(
    ("session_auth", "expected"),
    [("authenticated", "guest"), ("guest", "authenticated"), ("logged_out", "authenticated")],
)
def test_explicit_session_auth_precedes_credential_availability(
    no_test_account, session_auth, expected
):
    assert (
        rule_auth_gate.resolve_blocked_auth_requirement_from_session_auth(
            session_auth, no_test_account=no_test_account
        )
        == expected
    )
