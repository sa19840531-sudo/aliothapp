"""Web 登录支线仅在精确白名单命中时执行，结果必须经独立观察。"""

from dataclasses import dataclass

import pytest

from services.api.app.application.web_login_allowlist import parse_web_login_allowlist
from services.api.app.application.web_login_branch import WebLoginBranch, WebLoginOutcome


@dataclass(frozen=True)
class Frame:
    png: bytes = b"png"
    url: str = "https://example.com/"
    identity: str = "document-1"


class Browser:
    def __init__(self, *, form_after_click=True, secure=True):
        self.clicks = []
        self.submissions = []
        self.quarantined = []
        self.form_after_click = form_after_click
        self.secure = secure

    async def capture(self, session_id, origin):
        if not self.secure:
            error = RuntimeError("origin mismatch")
            error.reason_code = "WEB_LOGIN_ORIGIN_MISMATCH"
            raise error
        return Frame()

    async def form_ready(self, session_id, origin, identifier_kind):
        return bool(self.clicks) and self.form_after_click

    async def click(self, session_id, origin, frame, box):
        self.clicks.append(box)

    async def submit(self, session_id, origin, identifier_kind, identifier, password):
        self.submissions.append((identifier_kind, identifier, password))
        return "WEB_LOGIN_SUBMITTED"

    async def quarantine(self, session_id):
        self.quarantined.append(session_id)


class Visual:
    def __init__(self, *, authenticated=True):
        self.authenticated = authenticated

    async def already_authenticated(self, png):
        return False

    async def locate_entry(self, png, stage):
        return (0.7, 0.1, 0.8, 0.2)

    async def verify_result(self, png):
        return self.authenticated


def _branch(browser, visual):
    allowlist = parse_web_login_allowlist(
        "网址：https://example.com\n账号：13800138000\n密码：test-secret\n"
    )
    return WebLoginBranch(allowlist=allowlist, browser=browser, visual=visual)


@pytest.mark.asyncio
async def test_non_allowlisted_site_does_not_touch_browser():
    browser = Browser()
    outcome = await _branch(browser, Visual()).run(
        session_id="web:1", target_url="https://elsewhere.example/"
    )
    assert outcome == WebLoginOutcome(status="skipped", reason_code="WEB_LOGIN_NOT_ALLOWLISTED")
    assert browser.clicks == []
    assert browser.submissions == []


@pytest.mark.asyncio
async def test_http_allowlisted_site_reports_tls_gap_before_secret_use():
    browser = Browser(secure=False)
    outcome = await _branch(browser, Visual()).run(
        session_id="web:1", target_url="http://example.com/"
    )
    assert outcome == WebLoginOutcome(status="incomplete", reason_code="WEB_LOGIN_HTTPS_REQUIRED")
    assert browser.submissions == []


@pytest.mark.asyncio
async def test_http_entry_that_redirected_to_https_can_log_in():
    browser = Browser()
    outcome = await _branch(browser, Visual()).run(
        session_id="web:1", target_url="http://example.com/"
    )
    assert outcome.status == "authenticated"
    assert len(browser.submissions) == 1
    assert browser.quarantined == []


@pytest.mark.asyncio
async def test_reclaimed_stage_never_replays_login_submission():
    browser = Browser()
    outcome = await _branch(browser, Visual()).run(
        session_id="web:1", target_url="https://example.com/", attempt_count=2
    )
    assert outcome.reason_code == "WEB_LOGIN_RETRY_REVIEW_REQUIRED"
    assert browser.clicks == []
    assert browser.submissions == []


@pytest.mark.asyncio
async def test_login_uses_same_session_and_one_submission():
    browser = Browser()
    outcome = await _branch(browser, Visual()).run(
        session_id="web:1", target_url="https://example.com/video?id=1"
    )
    assert outcome.status == "authenticated"
    assert len(browser.clicks) == 1
    assert browser.submissions == [("phone", "13800138000", "test-secret")]


@pytest.mark.asyncio
async def test_uncertain_result_never_claims_authentication():
    browser = Browser()
    outcome = await _branch(browser, Visual(authenticated=False)).run(
        session_id="web:1", target_url="https://example.com/"
    )
    assert outcome.status == "incomplete"
    assert outcome.reason_code == "WEB_LOGIN_UNVERIFIED"
    assert len(browser.submissions) == 1
    assert browser.quarantined == ["web:1"]


@pytest.mark.asyncio
async def test_blocked_unknown_cross_origin_keeps_branch_incomplete():
    class BlockedBrowser(Browser):
        async def submit(self, session_id, origin, identifier_kind, identifier, password):
            self.submissions.append((identifier_kind, identifier, password))
            return "WEB_LOGIN_CROSS_ORIGIN_BLOCKED"

    browser = BlockedBrowser()
    outcome = await _branch(browser, Visual(authenticated=True)).run(
        session_id="web:1", target_url="https://example.com/"
    )
    assert outcome == WebLoginOutcome(
        "incomplete", "WEB_LOGIN_CROSS_ORIGIN_BLOCKED", cross_origin_status="blocked"
    )
    assert len(browser.submissions) == 1


@pytest.mark.asyncio
async def test_cookie_and_form_proof_survives_blocked_background_request():
    class ConfirmedBrowser(Browser):
        def __init__(self):
            super().__init__()
            self.submitted = False

        async def session_evidence(self, session_id, origin):
            return {"auth_cookie_signatures": frozenset({"new"}) if self.submitted else frozenset()}

        async def form_ready(self, session_id, origin, identifier_kind):
            return bool(self.clicks) and not self.submitted

        async def submit(self, session_id, origin, identifier_kind, identifier, password):
            self.submitted = True
            return "WEB_LOGIN_CROSS_ORIGIN_BLOCKED"

        def blocked_cross_origin(self, session_id):
            return self.submitted

        def blocked_request_summary(self, session_id):
            return (("https://mc.yandex.ru", "GET"),) if self.submitted else ()

    browser = ConfirmedBrowser()
    outcome = await _branch(browser, Visual(authenticated=False)).run(
        session_id="web:1", target_url="https://example.com/"
    )
    assert outcome.status == "authenticated"
    assert outcome.reason_code == "WEB_LOGIN_COOKIE_CONFIRMED"
    assert outcome.cross_origin_status == "blocked"
    assert outcome.blocked_requests == (("https://mc.yandex.ru", "GET"),)


@pytest.mark.asyncio
async def test_confirmed_cookie_survives_account_artifact_index_failure():
    class ConfirmedBrowser(Browser):
        def __init__(self):
            super().__init__()
            self.submitted = False

        async def open_isolated(self, session_id, origin):
            return session_id + ":login"

        async def close_isolated(self, session_id):
            return None

        async def session_evidence(self, session_id, origin):
            return {"auth_cookie_signatures": frozenset({"new"}) if self.submitted else frozenset()}

        async def form_ready(self, session_id, origin, identifier_kind):
            return bool(self.clicks) and not self.submitted

        async def submit(self, session_id, origin, identifier_kind, identifier, password):
            self.submitted = True
            return "WEB_LOGIN_SUBMITTED"

    class IndexFailure(RuntimeError):
        error_code = "WEB_LOGIN_ACCOUNT_ARTIFACT_INDEX_FAILED"

    class Inspector:
        async def inspect(self, **kwargs):
            raise IndexFailure()

    branch = _branch(ConfirmedBrowser(), Visual(authenticated=False))
    branch._inspector = Inspector()
    outcome = await branch.run(
        task_id="task", session_id="web:1", target_url="https://example.com/",
        source_capture_id="capture",
    )
    assert outcome.status == "authenticated"
    assert outcome.reason_code == "WEB_LOGIN_COOKIE_CONFIRMED"
    assert outcome.account_status == "artifact_index_failed"
    assert outcome.account_artifact_ids == ()
    assert outcome.account_storage_paths == ()


@pytest.mark.asyncio
async def test_unknown_post_without_cookie_and_form_proof_stays_incomplete():
    class UnprovenBrowser(Browser):
        async def submit(self, session_id, origin, identifier_kind, identifier, password):
            return "WEB_LOGIN_CROSS_ORIGIN_BLOCKED"

        def blocked_cross_origin(self, session_id):
            return True

        def blocked_request_summary(self, session_id):
            return (("https://auth-unknown.example", "POST"),)

    outcome = await _branch(UnprovenBrowser(), Visual()).run(
        session_id="web:1", target_url="https://example.com/"
    )
    assert outcome.status == "incomplete"
    assert outcome.cross_origin_status == "blocked"
    assert outcome.blocked_requests == (("https://auth-unknown.example", "POST"),)


@pytest.mark.asyncio
async def test_confirmed_login_inspects_only_isolated_session_and_keeps_main_session():
    class ConfirmedBrowser(Browser):
        def __init__(self):
            super().__init__()
            self.submitted = False
            self.closed = []
            self.blocked = False

        def blocked_cross_origin(self, session_id):
            return self.blocked

        def blocked_request_summary(self, session_id):
            return (("https://unknown.example", "GET"),) if self.blocked else ()

        async def open_isolated(self, session_id, origin):
            return session_id + ":login"

        async def close_isolated(self, session_id):
            self.closed.append(session_id)

        async def session_evidence(self, session_id, origin):
            return {"auth_cookie_signatures": frozenset({"new"}) if self.submitted else frozenset()}

        async def form_ready(self, session_id, origin, identifier_kind):
            return bool(self.clicks) and not self.submitted

        async def submit(self, session_id, origin, identifier_kind, identifier, password):
            self.submitted = True
            return "WEB_LOGIN_SUBMITTED"

    class Inspector:
        def __init__(self, browser):
            self.calls = []
            self.browser = browser

        async def inspect(self, **kwargs):
            self.calls.append(kwargs)
            self.browser.blocked = True
            return SimpleNamespace(
                status="recharge_opened",
                artifact_ids=("artifact-1",),
                storage_paths=("task/capture/artifact-1.png",),
                page_type="topup_page",
                page_url="https://example.com/recharge",
                page_visits=(
                    ("recharge_page", "https://example.com/wallet",
                     "wallet-shot", "wallet-dom"),
                    ("topup_page", "https://example.com/recharge",
                     "topup-shot", "topup-dom"),
                ),
                avatar_opened=True,
                avatar_dom_artifact_id="artifact-dom",
            )

    from types import SimpleNamespace

    browser = ConfirmedBrowser()
    inspector = Inspector(browser)
    branch = _branch(browser, Visual(authenticated=False))
    branch._inspector = inspector
    outcome = await branch.run(
        task_id="task",
        session_id="web:1",
        target_url="https://example.com/",
        source_capture_id="capture",
        adult_declared=True,
    )
    assert outcome.status == "authenticated"
    assert outcome.account_status == "recharge_opened"
    assert outcome.cross_origin_status == "blocked"
    assert outcome.blocked_requests == (("https://unknown.example", "GET"),)
    assert outcome.account_artifact_ids == ("artifact-1",)
    assert outcome.account_page_type == "topup_page"
    assert outcome.account_page_url == "https://example.com/recharge"
    assert outcome.account_page_visits == (
        ("recharge_page", "https://example.com/wallet", "wallet-shot", "wallet-dom"),
        ("topup_page", "https://example.com/recharge", "topup-shot", "topup-dom"),
    )
    assert outcome.account_avatar_opened is True
    assert outcome.account_avatar_dom_artifact_id == "artifact-dom"
    assert inspector.calls[0]["session_id"] == "web:1:login"
    assert browser.closed == ["web:1:login"]


@pytest.mark.parametrize(("inspection_status", "reason_code"), [
    ("guest_after_fresh_navigation", "WEB_LOGIN_SESSION_NOT_PERSISTED"),
    ("account_auth_unverified", "WEB_LOGIN_ACCOUNT_UI_UNVERIFIED"),
    ("avatar_missing", "WEB_LOGIN_AVATAR_NOT_FOUND"),
    ("secret_residue", "WEB_LOGIN_ACCOUNT_EVIDENCE_UNAVAILABLE"),
    ("obstructed", "WEB_LOGIN_ACCOUNT_EVIDENCE_UNAVAILABLE"),
    ("avatar_obstructed", "WEB_LOGIN_ACCOUNT_EVIDENCE_UNAVAILABLE"),
    ("inspection_failed", "WEB_LOGIN_ACCOUNT_EVIDENCE_UNAVAILABLE"),
])
@pytest.mark.asyncio
async def test_fresh_account_ui_downgrades_cookie_proof_and_keeps_evidence(
    inspection_status, reason_code
):
    from types import SimpleNamespace

    class CookieBrowser(Browser):
        def __init__(self):
            super().__init__()
            self.submitted = False

        async def open_isolated(self, session_id, origin):
            return session_id + ":login"

        async def close_isolated(self, session_id):
            pass

        async def session_evidence(self, session_id, origin):
            values = {"new"} if self.submitted else set()
            return {"auth_cookie_signatures": frozenset(values)}

        async def form_ready(self, session_id, origin, identifier_kind):
            return bool(self.clicks) and not self.submitted

        async def submit(self, session_id, origin, identifier_kind, identifier, password):
            self.submitted = True
            return "WEB_LOGIN_SUBMITTED"

    class GuestInspector:
        async def inspect(self, **kwargs):
            avatar_opened = inspection_status not in {
                "avatar_missing", "secret_residue", "obstructed",
                "avatar_obstructed", "inspection_failed",
            }
            return SimpleNamespace(
                status=inspection_status,
                artifact_ids=("shot", "dom") if avatar_opened else ("shot",),
                storage_paths=(
                    ("task/capture/shot.png", "task/capture/dom.html")
                    if avatar_opened else ("task/capture/shot.png",)
                ),
                avatar_opened=avatar_opened,
                avatar_dom_artifact_id="dom" if avatar_opened else None,
            )

    branch = _branch(CookieBrowser(), Visual(authenticated=False))
    branch._inspector = GuestInspector()
    outcome = await branch.run(
        task_id="task", session_id="web:1", target_url="https://example.com/",
        source_capture_id="capture",
    )
    assert outcome.status == "incomplete"
    assert outcome.reason_code == reason_code
    assert outcome.account_status == inspection_status
    avatar_opened = inspection_status not in {
        "avatar_missing", "secret_residue", "obstructed",
        "avatar_obstructed", "inspection_failed",
    }
    assert outcome.account_avatar_opened is avatar_opened
    assert outcome.account_avatar_dom_artifact_id == (
        "dom" if avatar_opened else None
    )
    assert outcome.account_artifact_ids == (
        ("shot", "dom") if avatar_opened else ("shot",)
    )


@pytest.mark.asyncio
async def test_same_origin_account_page_can_open_avatar_then_login_menu():
    class AccountPageBrowser(Browser):
        async def capture(self, session_id, origin):
            return Frame(
                url=(
                    "https://example.com/account/"
                    if len(self.clicks) >= 2
                    else "https://example.com/"
                )
            )

        async def form_ready(self, session_id, origin, identifier_kind):
            return len(self.clicks) >= 4

    browser = AccountPageBrowser()
    outcome = await _branch(browser, Visual()).run(
        session_id="web:1", target_url="https://example.com/"
    )
    assert outcome.status == "authenticated"
    assert len(browser.clicks) == 4
    assert len(browser.submissions) == 1


@pytest.mark.asyncio
async def test_secret_residue_uses_cookie_proof_without_post_submit_screenshot():
    class ResidueBrowser(Browser):
        def __init__(self):
            super().__init__()
            self.submitted = False
            self.captures = 0
            self.closed = []

        async def open_isolated(self, session_id, origin):
            return f"{session_id}:login"

        async def close_isolated(self, session_id):
            self.closed.append(session_id)

        async def capture(self, session_id, origin):
            self.captures += 1
            if self.submitted:
                raise AssertionError("残留秘密后不得截图")
            return Frame()

        async def session_evidence(self, session_id, origin):
            return {"auth_cookie_signatures": frozenset({"new"}) if self.submitted else frozenset()}

        async def form_ready(self, session_id, origin, identifier_kind):
            return bool(self.clicks) and not self.submitted

        async def submit(self, session_id, origin, identifier_kind, identifier, password):
            self.submitted = True
            return "WEB_LOGIN_SECRET_RESIDUE"

    class NoPostSubmitVisual(Visual):
        async def verify_result(self, png):
            raise AssertionError("残留秘密后不得调用视觉模型")

    browser = ResidueBrowser()
    outcome = await _branch(browser, NoPostSubmitVisual()).run(
        session_id="web:1", target_url="https://example.com/"
    )
    assert outcome.status == "authenticated"
    assert outcome.reason_code == "WEB_LOGIN_COOKIE_CONFIRMED_ISOLATED"
    assert browser.captures == 2
    assert browser.closed == ["web:1:login"]


@pytest.mark.parametrize(("account_status", "expected_status", "expected_reason"), [
    ("account_observed", "authenticated", "WEB_LOGIN_ACCOUNT_UI_CONFIRMED_ISOLATED"),
    ("account_auth_unverified", "incomplete", "WEB_LOGIN_ACCOUNT_UI_UNVERIFIED"),
])
@pytest.mark.asyncio
async def test_secret_residue_uses_only_fresh_isolated_account_evidence(
    account_status, expected_status, expected_reason
):
    from types import SimpleNamespace

    class ResidueBrowser(Browser):
        def __init__(self):
            super().__init__()
            self.submitted = False
            self.closed = []

        async def open_isolated(self, session_id, origin):
            return session_id + ":login"

        async def close_isolated(self, session_id):
            self.closed.append(session_id)

        async def capture(self, session_id, origin):
            if self.submitted:
                raise AssertionError("残留秘密的旧文档不得截图")
            return Frame()

        async def session_evidence(self, session_id, origin):
            return {"auth_cookie_signatures": frozenset()}

        async def form_ready(self, session_id, origin, identifier_kind):
            return bool(self.clicks)

        async def submit(self, session_id, origin, identifier_kind, identifier, password):
            self.submitted = True
            return "WEB_LOGIN_SECRET_RESIDUE"

    class Inspector:
        def __init__(self):
            self.calls = 0

        async def inspect(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                status=account_status, artifact_ids=("shot", "dom"),
                storage_paths=("task/shot.png", "task/dom.html"),
                avatar_opened=True, avatar_dom_artifact_id="dom",
            )

    browser = ResidueBrowser()
    inspector = Inspector()
    branch = _branch(browser, Visual(authenticated=False))
    branch._inspector = inspector
    outcome = await branch.run(
        task_id="task", session_id="web:1", target_url="https://example.com/",
        source_capture_id="capture",
    )
    assert outcome.status == expected_status
    assert outcome.reason_code == expected_reason
    assert outcome.account_status == account_status
    assert inspector.calls == 1
    assert browser.closed == ["web:1:login"]


@pytest.mark.parametrize(("server_failure", "expected_reason"), [
    (None, "WEB_LOGIN_SESSION_NOT_PERSISTED"),
    ("WEB_LOGIN_CREDENTIALS_REJECTED", "WEB_LOGIN_CREDENTIALS_REJECTED"),
])
@pytest.mark.asyncio
async def test_unconfirmed_submit_checks_fresh_account_before_retrying_avatar(
    server_failure, expected_reason
):
    from types import SimpleNamespace

    class UnconfirmedBrowser(Browser):
        def __init__(self):
            super().__init__()
            self.submitted = False

        async def open_isolated(self, session_id, origin):
            return session_id + ":login"

        async def close_isolated(self, session_id):
            pass

        async def session_evidence(self, session_id, origin):
            return {"auth_cookie_signatures": frozenset()}

        async def form_ready(self, session_id, origin, identifier_kind):
            return bool(self.clicks)

        async def submit(self, session_id, origin, identifier_kind, identifier, password):
            self.submitted = True
            return "WEB_LOGIN_SUBMITTED"

        async def recover_entry(self, session_id, origin):
            if self.submitted:
                raise AssertionError("结果不明时不得再点游客入口")
            return None

        def login_server_failure(self, session_id):
            return server_failure

    class GuestInspector:
        async def inspect(self, **kwargs):
            return SimpleNamespace(
                status="guest_after_fresh_navigation", artifact_ids=("shot", "dom"),
                storage_paths=("task/shot.png", "task/dom.html"),
                avatar_opened=True, avatar_dom_artifact_id="dom",
            )

    branch = _branch(UnconfirmedBrowser(), Visual(authenticated=False))
    branch._inspector = GuestInspector()
    outcome = await branch.run(
        task_id="task", session_id="web:1", target_url="https://example.com/",
        source_capture_id="capture",
    )
    assert outcome.status == "incomplete"
    assert outcome.reason_code == expected_reason


@pytest.mark.asyncio
async def test_prelogin_blocked_analytics_does_not_override_unverified_submit():
    class BackgroundBlockedBrowser(Browser):
        def blocked_cross_origin(self, session_id):
            return True

        def blocked_request_summary(self, session_id):
            return (("https://mc.yandex.ru", "GET"),)

    browser = BackgroundBlockedBrowser()
    outcome = await _branch(browser, Visual(authenticated=False)).run(
        session_id="web:1", target_url="https://example.com/"
    )
    assert outcome.status == "incomplete"
    assert outcome.reason_code == "WEB_LOGIN_UNVERIFIED"
    assert outcome.cross_origin_status == "blocked"
    assert outcome.blocked_requests == (("https://mc.yandex.ru", "GET"),)


@pytest.mark.asyncio
async def test_isolated_login_does_not_observe_main_session_as_login_evidence():
    observed = []

    async def observer(*, source_capture_id, stage):
        observed.append((source_capture_id, stage))
        return "main-session-capture"

    browser = Browser()
    outcome = await _branch(browser, Visual()).run(
        session_id="web:main",
        target_url="https://example.com/",
        source_capture_id="source-capture",
        observer=observer,
    )
    assert outcome.status == "authenticated"
    assert outcome.capture_id == "source-capture"
    assert observed == []
