"""首页被同源登录页拦住时，只改诊断，不伪造主线首页。"""

from types import SimpleNamespace

import pytest

from services.api.app.application.web_login_branch import WebLoginOutcome
from services.api.app.application.web_runtime_evidence import WebReadyCaptureReaderAdapter
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld


def _login_snapshot(*, password_visible: bool = True, submit_visible: bool = True):
    form = "main > section > form"
    return {
        "url": "https://example.test/login?redirect=/",
        "elements": [
            {"selector": form, "tag": "form", "visible": True},
            {
                "selector": 'input[name="password"]',
                "tag": "input",
                "type": "password",
                "visible": password_visible,
                "interactive": True,
                "web_ancestor_structure": [{"selector": form, "tag": "form"}],
                "hit_test": {"center_hits_target": True, "visual_occlusion_detected": False},
            },
            {
                "selector": "main > section > form > button",
                "tag": "button",
                "type": "submit",
                "text": "登录",
                "visible": submit_visible,
                "interactive": True,
                "web_ancestor_structure": [{"selector": form, "tag": "form"}],
                "hit_test": {"center_hits_target": True, "visual_occlusion_detected": False},
            },
        ],
    }


def _redirected_world(monkeypatch, *, snapshot):
    world = RecoveryWorld(monkeypatch, with_candidates=True)

    async def start(request):
        world.actions.append("bootstrap")
        assert request.target_url == "https://example.test/"
        return SimpleNamespace(
            after_capture=SimpleNamespace(
                capture_id=world.capture("https://example.test/login", "probe")
            )
        )

    async def classify(ready):
        world.classifications.append(ready.url)
        world.captures[ready.capture_id]["page_type"] = "unknown"
        return SimpleNamespace(page_type="unknown")

    async def read_snapshot(*, capture_id):
        assert capture_id == world.current
        return snapshot

    world.runtime.start = start
    world.runtime.page_type_classifier.classify = classify
    world.runtime.stage_ready_capture_reader.read_snapshot = read_snapshot
    return world


async def test_root_redirected_to_visible_login_form_has_distinct_terminal_reason(
    monkeypatch,
):
    world = _redirected_world(monkeypatch, snapshot=_login_snapshot())
    result = await world.run()

    assert world.closed and world.rows["home"].is_terminal
    assert "WEB_HOME_LOGIN_GATE" in result.gap_codes
    assert "WEB_HOME_PAGE_TYPE_UNCLASSIFIED" not in result.gap_codes
    assert not world.discoveries and set(world.rows) == {"home"}
    assert world.records["home"].status is WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
    assert world.captures[world.current]["page_type"] == "unknown"


@pytest.mark.parametrize("missing", ["password", "submit"])
async def test_login_url_without_complete_visible_form_remains_generic_unknown(
    monkeypatch, missing,
):
    world = _redirected_world(
        monkeypatch,
        snapshot=_login_snapshot(
            password_visible=missing != "password",
            submit_visible=missing != "submit",
        ),
    )
    result = await world.run()
    assert "WEB_HOME_PAGE_TYPE_UNCLASSIFIED" in result.gap_codes
    assert "WEB_HOME_LOGIN_GATE" not in result.gap_codes


async def test_login_diagnostic_snapshot_failure_remains_generic_unknown(monkeypatch):
    world = _redirected_world(monkeypatch, snapshot=_login_snapshot())

    async def read_snapshot(*, capture_id):
        raise RuntimeError("snapshot unavailable")

    world.runtime.stage_ready_capture_reader.read_snapshot = read_snapshot
    result = await world.run()
    assert "WEB_HOME_PAGE_TYPE_UNCLASSIFIED" in result.gap_codes
    assert "WEB_HOME_LOGIN_GATE" not in result.gap_codes


@pytest.mark.parametrize("snapshot_url", [
    "https://other.test/login",
    "https://example.test/account",
])
async def test_login_diagnostic_requires_same_capture_page_identity(
    monkeypatch, snapshot_url,
):
    snapshot = _login_snapshot()
    snapshot["url"] = snapshot_url
    world = _redirected_world(monkeypatch, snapshot=snapshot)
    result = await world.run()
    assert "WEB_HOME_PAGE_TYPE_UNCLASSIFIED" in result.gap_codes
    assert "WEB_HOME_LOGIN_GATE" not in result.gap_codes


async def test_password_and_submit_must_belong_to_same_visible_form(monkeypatch):
    snapshot = _login_snapshot()
    snapshot["elements"][2]["web_ancestor_structure"][0]["selector"] = "other > form"
    world = _redirected_world(monkeypatch, snapshot=snapshot)
    result = await world.run()
    assert "WEB_HOME_PAGE_TYPE_UNCLASSIFIED" in result.gap_codes
    assert "WEB_HOME_LOGIN_GATE" not in result.gap_codes


async def test_ready_snapshot_reader_rechecks_current_capture_identity():
    checked = []

    class Source:
        async def _read(self, capture_id, *, current_only):
            checked.append((capture_id, current_only))
            return object(), {"capture_id": capture_id, "elements": []}

    snapshot = await WebReadyCaptureReaderAdapter(source=Source()).read_snapshot(
        capture_id="ready-capture"
    )
    assert checked == [("ready-capture", True)]
    assert snapshot["capture_id"] == "ready-capture"


async def test_authenticated_home_classification_is_unchanged(monkeypatch):
    world = RecoveryWorld(monkeypatch)

    async def read_snapshot(*, capture_id):
        raise AssertionError("真实首页不得读取登录门诊断快照")

    world.runtime.stage_ready_capture_reader.read_snapshot = read_snapshot
    result = await world.run()
    assert "WEB_HOME_LOGIN_GATE" not in result.gap_codes
    assert "WEB_HOME_PAGE_TYPE_UNCLASSIFIED" not in result.gap_codes
    assert world.records["home"].status is WebMenuTerminalStatus.HOME_BASELINE


async def test_isolated_login_success_does_not_authorize_mainline_menus(monkeypatch):
    world = _redirected_world(monkeypatch, snapshot=_login_snapshot())
    branch_calls = []

    class LoginBranch:
        async def run(self, **kwargs):
            branch_calls.append(kwargs)
            return WebLoginOutcome("authenticated", "WEB_LOGIN_COOKIE_CONFIRMED")

    world.runtime.web_login_branch = LoginBranch()
    result = await world.run()

    assert "WEB_HOME_LOGIN_GATE" in result.gap_codes
    assert len(branch_calls) == 1
    assert branch_calls[0]["session_id"] == world.rows["home"].cursor_payload["session_id"]
    assert world.rows["home"].cursor_payload["login_branch_status"] == "authenticated"
    assert not world.discoveries and set(world.rows) == {"home"}
