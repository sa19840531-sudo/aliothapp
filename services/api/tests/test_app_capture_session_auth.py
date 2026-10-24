"""App 登录事实只由同帧明确证据建立，缺账号与年龄门不得冒充游客态。"""

import pytest

from services.api.app.application.app_capture_evidence_fields import (
    normalize_session_auth,
    resolve_session_auth_from_facts,
)


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({}, "unknown"),
        ({"session_auth": "unknown"}, "unknown"),
        ({"session_auth": "authenticated"}, "authenticated"),
        ({"session_auth": "logged_out"}, "guest"),
        ({"authenticated": True}, "authenticated"),
        ({"logged_in": True}, "authenticated"),
        ({"logged_in": False}, "guest"),
        ({"authenticated": False}, "guest"),
        ({"login_form_visible": True}, "guest"),
        ({"login_required": True}, "guest"),
        ({"auth_gate_visible": True, "requires_auth": True}, "guest"),
        ({"auth_gate_visible": True}, "unknown"),
        (
            {"auth_gate_visible": True, "requires_auth": False, "dialog_kind": "age_verification"},
            "unknown",
        ),
        ({"guest_entry_visible": True, "login_form_visible": False}, "unknown"),
        ({"logged_in": "true"}, "unknown"),
        ({"logged_in": True, "login_form_visible": True}, "unknown"),
        ({"session_auth": "guest", "authenticated": True}, "unknown"),
    ],
)
def test_session_auth_uses_explicit_observed_facts(facts, expected):
    assert resolve_session_auth_from_facts(surface_facts=facts) == expected


@pytest.mark.parametrize("config", [{"no_test_account": True}, {"has_test_account": False}])
def test_missing_test_credentials_do_not_prove_logout(config):
    assert resolve_session_auth_from_facts(task_config=config) == "unknown"
    assert (
        resolve_session_auth_from_facts(surface_facts={"logged_in": True}, task_config=config)
        == "authenticated"
    )


def test_explicit_logout_alias_normalizes_without_collapsing_unknown():
    assert normalize_session_auth("logged_out") == "guest"
    assert normalize_session_auth("unknown") == "unknown"
