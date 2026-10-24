"""登录后的账户截图必须来自隔离会话，并只执行有界充值导航。"""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from services.api.app.application.web_login_account_inspection import (
    WebLoginAccountInspector,
)


@dataclass(frozen=True)
class Frame:
    png: bytes = b"\x89PNG\r\n\x1a\nimage"
    url: str = "https://example.com/"
    identity: str = "document-1"
    dom: str | None = None


class Store:
    def __init__(self):
        self.calls = []

    async def store(self, **kwargs):
        self.calls.append(kwargs)
        extension = "html" if kwargs["artifact_type"] == "dom" else "png"
        return SimpleNamespace(
            artifact_id=kwargs["artifact_id"],
            storage_path=f"task/capture/{kwargs['artifact_id']}.{extension}",
        )


@pytest.mark.asyncio
async def test_account_artifacts_pass_step_identity_to_indexed_store():
    class IndexedStore(Store):
        async def store(self, **kwargs):
            assert kwargs["step"] in {"authenticated", "account_opened"}
            assert kwargs["isolated_session_id"] == "web:1:login"
            return await super().store(**kwargs)

    store = IndexedStore()
    result = await WebLoginAccountInspector(
        browser=Browser(actions=()), artifact_store=store
    ).inspect(
        session_id="web:1:login",
        origin="https://example.com",
        task_id="task",
        source_capture_id="capture",
    )
    assert result.status == "account_observed"
    assert result.artifact_ids == tuple(call["artifact_id"] for call in store.calls)


class Browser:
    def __init__(
        self, *, prepared=True, actions=(), recharge_confirmed=True,
        wallet_confirmed=True, auth_state="authenticated",
    ):
        self.prepared = prepared
        self.actions = actions
        self.recharge_confirmed = recharge_confirmed
        self.wallet_confirmed = wallet_confirmed
        self.auth_state = auth_state
        self.calls = []
        self.frame_number = 0
        self.page_url = "https://example.com/"
        self.reveal_result = "absent"

    async def prepare_authenticated_view(self, session_id, origin):
        self.calls.append(("prepare", session_id))
        return self.prepared

    async def clear_obstructions(self, session_id, origin, *, adult_declared=False):
        self.calls.append(("clear", session_id))
        return "WEB_LOGIN_OBSTRUCTIONS_CLEAR"

    async def capture(self, session_id, origin, *, include_dom=False):
        self.calls.append(("capture", session_id))
        self.frame_number += 1
        return Frame(
            png=Frame.png + str(self.frame_number).encode(),
            url=self.page_url,
            dom="<body><a href=\"/account\">我的账户</a></body>" if include_dom else None,
        )

    async def recover_entry(self, session_id, origin):
        return (0.8, 0.02, 0.9, 0.1)

    async def click(self, session_id, origin, frame, box):
        self.calls.append(("avatar", session_id))

    async def account_auth_state(self, session_id, origin, frame):
        self.calls.append(("auth_state", session_id))
        return self.auth_state

    async def account_actions(self, session_id, origin, frame, ocr_hits):
        self.calls.append(("actions", session_id))
        return self.actions

    async def reveal_account_action(self, session_id, origin, frame, kind):
        self.calls.append(("reveal", kind))
        return self.reveal_result

    async def click_account_action(self, session_id, origin, frame, action):
        self.calls.append(("action", action["kind"]))
        self.page_url = f"https://example.com/{action['kind']}?session=private"
        return True

    async def confirm_recharge_page(self, session_id, origin, frame, ocr_hits):
        self.calls.append(("confirm_recharge", session_id))
        return self.recharge_confirmed and any(
            marker in self.page_url for marker in ("recharge", "deposit")
        )

    async def confirm_wallet_page(self, session_id, origin, frame, ocr_hits):
        self.calls.append(("confirm_wallet", session_id))
        return self.wallet_confirmed and "/wallet" in self.page_url


@pytest.mark.asyncio
async def test_avatar_and_recharge_screenshots_are_saved_from_isolated_session():
    browser = Browser(
        actions=(
            {"kind": "wallet", "box": (0.1, 0.2, 0.2, 0.3)},
            {"kind": "recharge", "box": (0.4, 0.2, 0.5, 0.3)},
        )
    )
    store = Store()
    inspector = WebLoginAccountInspector(browser=browser, artifact_store=store)
    result = await inspector.inspect(
        session_id="web:1:login",
        origin="https://example.com",
        task_id="task",
        source_capture_id="capture",
        adult_declared=True,
    )
    assert result.status == "recharge_opened"
    assert result.page_type == "topup_page"
    assert result.page_url == "https://example.com/recharge"
    assert ("confirm_recharge", "web:1:login") in browser.calls
    assert ("action", "recharge") in browser.calls
    assert ("action", "wallet") in browser.calls
    assert len(store.calls) == 8
    assert [item["artifact_type"] for item in store.calls] == [
        "screenshot", "dom", "screenshot", "dom", "screenshot", "dom",
        "screenshot", "dom",
    ]
    assert all(
        item["content"].startswith(b"\x89PNG")
        for item in store.calls if item["artifact_type"] == "screenshot"
    )
    assert result.avatar_opened is True
    assert result.avatar_dom_artifact_id == store.calls[3]["artifact_id"]
    assert all(
        session_id == "web:1:login"
        for name, session_id in browser.calls
        if name in {"prepare", "clear", "capture", "avatar", "actions"}
    )


@pytest.mark.asyncio
async def test_secret_residue_without_clean_new_document_never_screenshots():
    browser = Browser(prepared=False)
    store = Store()
    result = await WebLoginAccountInspector(browser=browser, artifact_store=store).inspect(
        session_id="web:1:login",
        origin="https://example.com",
        task_id="task",
        source_capture_id="capture",
    )
    assert result.status == "secret_residue"
    assert store.calls == []
    assert not any(name == "capture" for name, _ in browser.calls)


@pytest.mark.asyncio
async def test_ambiguous_recharge_links_are_not_clicked():
    browser = Browser(
        actions=(
            {"kind": "recharge", "box": (0.1, 0.2, 0.2, 0.3)},
            {"kind": "recharge", "box": (0.4, 0.2, 0.5, 0.3)},
        )
    )
    result = await WebLoginAccountInspector(browser=browser, artifact_store=Store()).inspect(
        session_id="web:1:login",
        origin="https://example.com",
        task_id="task",
        source_capture_id="capture",
    )
    assert result.status == "entry_ambiguous"
    assert not any(name == "action" for name, _ in browser.calls)


@pytest.mark.asyncio
async def test_recharge_without_independent_page_evidence_keeps_capture_but_no_type():
    browser = Browser(
        actions=({"kind": "recharge", "box": (0.4, 0.2, 0.5, 0.3)},),
        recharge_confirmed=False,
    )
    store = Store()
    result = await WebLoginAccountInspector(browser=browser, artifact_store=store).inspect(
        session_id="web:1:login",
        origin="https://example.com",
        task_id="task",
        source_capture_id="capture",
    )
    assert result.status == "entry_unproven"
    assert result.page_type is None
    assert result.page_url is None
    assert len(store.calls) == 6


@pytest.mark.asyncio
async def test_loading_topup_keeps_screenshot_without_claiming_page_type():
    class LoadingBrowser(Browser):
        async def wait_account_loading(self, session_id, origin):
            return "/recharge" not in self.page_url

    browser = LoadingBrowser(actions=(
        {"kind": "recharge", "box": (0.4, 0.2, 0.5, 0.3)},
    ))
    store = Store()
    result = await WebLoginAccountInspector(
        browser=browser, artifact_store=store
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "entry_unproven"
    assert result.page_type is None
    assert result.page_visits == ()
    assert [item["artifact_type"] for item in store.calls] == [
        "screenshot", "dom", "screenshot", "dom", "screenshot", "dom"
    ]


@pytest.mark.asyncio
async def test_offscreen_wallet_is_scrolled_and_recaptured_before_topup():
    class ScrollingBrowser(Browser):
        async def account_actions(self, session_id, origin, frame, ocr_hits):
            self.calls.append(("actions", session_id))
            if "/wallet" in self.page_url:
                return ({"kind": "recharge", "box": (0.4, 0.2, 0.5, 0.3)},)
            if self.frame_number >= 3:
                return ({"kind": "wallet", "box": (0.1, 0.2, 0.2, 0.3)},)
            return ({"kind": "recharge", "box": (0.4, 0.2, 0.5, 0.3)},)

        async def reveal_account_action(self, session_id, origin, frame, kind):
            self.calls.append(("reveal", kind))
            return "revealed" if kind == "wallet" else "absent"

    browser = ScrollingBrowser()
    store = Store()
    result = await WebLoginAccountInspector(browser=browser, artifact_store=store).inspect(
        session_id="web:1:login",
        origin="https://example.com",
        task_id="task",
        source_capture_id="capture",
    )
    assert result.status == "recharge_opened"
    assert [value for name, value in browser.calls if name == "action"] == [
        "wallet", "recharge"
    ]
    assert ("action", "recharge") in browser.calls
    assert len(store.calls) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("route_changes", [False, True])
async def test_revealed_topup_visit_uses_confirmed_frame_and_its_artifacts(route_changes):
    """展开入口后的充值页必须用当前帧复核，并引用同帧截图和 DOM。"""
    class RevealingBrowser(Browser):
        async def click(self, session_id, origin, frame, box):
            await super().click(session_id, origin, frame, box)
            self.page_url = "https://example.com/recharge"

        async def account_actions(self, session_id, origin, frame, ocr_hits):
            return ()

        async def reveal_account_action(self, session_id, origin, frame, kind):
            if kind == "wallet":
                if route_changes:
                    self.page_url = "https://example.com/profile"
                return "revealed"
            return "absent"

        async def confirm_recharge_page(self, session_id, origin, frame, ocr_hits):
            return frame.url == "https://example.com/recharge"

    store = Store()
    result = await WebLoginAccountInspector(
        browser=RevealingBrowser(), artifact_store=store
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )

    if route_changes:
        assert result.status == "entry_unproven"
        assert result.page_visits == ()
    else:
        assert result.status == "recharge_opened"
        assert len(result.page_visits) == 1
        visit = result.page_visits[0]
        assert visit.page_url == "https://example.com/recharge"
        current_artifacts = {
            item["artifact_type"]: item["artifact_id"]
            for item in store.calls if item["step"] == "account_wallet_revealed"
        }
        assert visit.screenshot_artifact_id == current_artifacts["screenshot"]
        assert visit.dom_artifact_id == current_artifacts["dom"]


@pytest.mark.asyncio
async def test_no_wallet_or_recharge_skips_without_changing_login_result():
    store = Store()
    result = await WebLoginAccountInspector(
        browser=Browser(actions=()), artifact_store=store
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "account_observed"
    assert result.page_type is None
    assert len(store.calls) == 4
    assert result.avatar_opened is True
    assert result.avatar_dom_artifact_id == store.calls[3]["artifact_id"]


@pytest.mark.asyncio
async def test_wallet_page_can_reveal_recharge_and_save_both_pages():
    class WalletBrowser(Browser):
        async def account_actions(self, session_id, origin, frame, ocr_hits):
            self.calls.append(("actions", session_id))
            kind = "recharge" if "/wallet" in self.page_url else "wallet"
            return ({"kind": kind, "box": (0.2, 0.2, 0.3, 0.3)},)

    browser = WalletBrowser()
    store = Store()
    result = await WebLoginAccountInspector(browser=browser, artifact_store=store).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "recharge_opened"
    assert result.page_type == "topup_page"
    assert ("action", "wallet") in browser.calls
    assert ("action", "recharge") in browser.calls
    assert len(store.calls) == 8


@pytest.mark.asyncio
async def test_wallet_is_opened_before_direct_topup_and_both_page_types_are_saved():
    class WalletFirstBrowser(Browser):
        async def account_actions(self, session_id, origin, frame, ocr_hits):
            self.calls.append(("actions", session_id))
            if "/wallet" in self.page_url:
                kinds = ("recharge",)
            else:
                kinds = ("wallet", "recharge")
            return tuple(
                {"kind": kind, "box": (0.2, 0.2, 0.3, 0.3)}
                for kind in kinds
            )

    browser = WalletFirstBrowser()
    store = Store()
    result = await WebLoginAccountInspector(
        browser=browser, artifact_store=store
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert [value for name, value in browser.calls if name == "action"] == [
        "wallet", "recharge"
    ]
    assert result.status == "recharge_opened"
    assert result.page_type == "topup_page"
    assert [(visit.page_type, visit.page_url) for visit in result.page_visits] == [
        ("recharge_page", "https://example.com/wallet"),
        ("topup_page", "https://example.com/recharge"),
    ]
    assert result.page_visits[0].screenshot_artifact_id == store.calls[4]["artifact_id"]
    assert result.page_visits[0].dom_artifact_id == store.calls[5]["artifact_id"]
    assert result.page_visits[1].screenshot_artifact_id == store.calls[6]["artifact_id"]
    assert result.page_visits[1].dom_artifact_id == store.calls[7]["artifact_id"]


@pytest.mark.asyncio
async def test_wallet_precedes_topup_form_already_visible_in_account_popup():
    class MixedPopupBrowser(Browser):
        async def account_actions(self, session_id, origin, frame, ocr_hits):
            self.calls.append(("actions", session_id))
            if "/wallet" in self.page_url:
                kinds = ("recharge",)
            else:
                kinds = ("wallet", "recharge")
            return tuple(
                {"kind": kind, "box": (0.2, 0.2, 0.3, 0.3)}
                for kind in kinds
            )

        async def confirm_recharge_page(self, session_id, origin, frame, ocr_hits):
            self.calls.append(("confirm_recharge", session_id))
            return "/wallet" not in self.page_url

    browser = MixedPopupBrowser()
    result = await WebLoginAccountInspector(
        browser=browser, artifact_store=Store()
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert [value for name, value in browser.calls if name == "action"] == [
        "wallet", "recharge"
    ]
    assert [visit.page_type for visit in result.page_visits] == [
        "recharge_page", "topup_page"
    ]


@pytest.mark.asyncio
async def test_wallet_without_topup_keeps_wallet_page_type():
    class WalletOnlyBrowser(Browser):
        async def account_actions(self, session_id, origin, frame, ocr_hits):
            self.calls.append(("actions", session_id))
            if "/wallet" in self.page_url:
                return ()
            return ({"kind": "wallet", "box": (0.2, 0.2, 0.3, 0.3)},)

    browser = WalletOnlyBrowser()
    result = await WebLoginAccountInspector(
        browser=browser, artifact_store=Store()
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "wallet_opened"
    assert result.page_type == "recharge_page"
    assert len(result.page_visits) == 1
    assert result.page_visits[0].page_url == "https://example.com/wallet"


@pytest.mark.asyncio
async def test_profile_link_is_followed_and_captured_before_recharge_search():
    class ProfileBrowser(Browser):
        async def account_actions(self, session_id, origin, frame, ocr_hits):
            self.calls.append(("actions", session_id))
            kind = "recharge" if "/profile" in self.page_url else "profile"
            return ({"kind": kind, "box": (0.2, 0.2, 0.3, 0.3)},)

    browser = ProfileBrowser()
    store = Store()
    result = await WebLoginAccountInspector(browser=browser, artifact_store=store).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "recharge_opened"
    assert [(name, value) for name, value in browser.calls if name == "action"] == [
        ("action", "profile"), ("action", "recharge")
    ]
    assert [item["artifact_type"] for item in store.calls] == [
        "screenshot", "dom", "screenshot", "dom", "screenshot", "dom",
        "screenshot", "dom",
    ]
    assert store.calls[5]["content"]


@pytest.mark.asyncio
async def test_profile_without_recharge_keeps_authenticated_account_result():
    class ProfileBrowser(Browser):
        async def account_actions(self, session_id, origin, frame, ocr_hits):
            self.calls.append(("actions", session_id))
            if "/profile" in self.page_url:
                return ()
            return ({"kind": "profile", "box": (0.2, 0.2, 0.3, 0.3)},)

    browser = ProfileBrowser()
    store = Store()
    result = await WebLoginAccountInspector(browser=browser, artifact_store=store).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "account_observed"
    assert result.page_type is None
    assert ("action", "profile") in browser.calls
    assert [item["artifact_type"] for item in store.calls] == [
        "screenshot", "dom", "screenshot", "dom", "screenshot", "dom"
    ]


@pytest.mark.asyncio
async def test_visible_recharge_is_not_clicked_if_hidden_dom_duplicate_exists():
    browser = Browser(
        actions=({"kind": "recharge", "box": (0.4, 0.2, 0.5, 0.3)},)
    )
    browser.reveal_result = "ambiguous"
    result = await WebLoginAccountInspector(browser=browser, artifact_store=Store()).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "entry_ambiguous"
    assert not any(name == "action" for name, _ in browser.calls)


@pytest.mark.asyncio
async def test_missing_avatar_does_not_claim_avatar_evidence():
    class MissingAvatarBrowser(Browser):
        async def recover_entry(self, session_id, origin):
            return None

    store = Store()
    result = await WebLoginAccountInspector(
        browser=MissingAvatarBrowser(), artifact_store=store
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "avatar_missing"
    assert result.avatar_opened is False
    assert result.avatar_dom_artifact_id is None
    assert [item["artifact_type"] for item in store.calls] == [
        "screenshot", "dom"
    ]
    assert store.calls[1]["content"] == (
        '<body><a href="/account">我的账户</a></body>'
    )
    assert result.artifact_ids == tuple(item["artifact_id"] for item in store.calls)


@pytest.mark.asyncio
async def test_late_overlay_is_cleared_and_recaptured_before_avatar_click():
    class LateOverlayBrowser(Browser):
        async def account_obstruction_present(self, session_id, origin):
            return self.frame_number == 1

        async def recover_entry(self, session_id, origin):
            return None if self.frame_number == 1 else (0.8, 0.02, 0.9, 0.1)

    browser = LateOverlayBrowser()
    store = Store()
    result = await WebLoginAccountInspector(
        browser=browser, artifact_store=store
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "account_observed"
    assert result.avatar_opened is True
    assert [item["artifact_type"] for item in store.calls] == [
        "screenshot", "dom", "screenshot", "screenshot", "dom"
    ]
    assert browser.calls.count(("clear", "web:1:login")) == 3


@pytest.mark.asyncio
async def test_late_account_overlay_keeps_clean_avatar_dom_evidence():
    class LateAccountOverlayBrowser(Browser):
        async def account_obstruction_present(self, session_id, origin):
            return self.frame_number == 2

    browser = LateAccountOverlayBrowser()
    store = Store()
    result = await WebLoginAccountInspector(
        browser=browser, artifact_store=store
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "account_observed"
    assert [item["artifact_type"] for item in store.calls] == [
        "screenshot", "dom", "screenshot", "dom", "screenshot", "dom"
    ]
    assert result.avatar_dom_artifact_id == store.calls[5]["artifact_id"]


@pytest.mark.asyncio
async def test_account_inspection_uses_hydrated_post_login_entry():
    class HydratedBrowser(Browser):
        async def recover_entry(self, session_id, origin):
            raise AssertionError("登录后不应复用游客入口")

        async def recover_authenticated_entry(self, session_id, origin):
            self.calls.append(("hydrated_entry", session_id))
            return (0.8, 0.02, 0.9, 0.1)

    browser = HydratedBrowser()
    result = await WebLoginAccountInspector(
        browser=browser, artifact_store=Store()
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "account_observed"
    assert ("hydrated_entry", "web:1:login") in browser.calls


@pytest.mark.asyncio
async def test_avatar_directly_opening_recharge_page_saves_and_classifies_it():
    class DirectRechargeBrowser(Browser):
        async def click(self, session_id, origin, frame, box):
            self.calls.append(("avatar", session_id))
            self.page_url = "https://example.com/ai/my-recharge/?source=private"

    browser = DirectRechargeBrowser()
    store = Store()
    result = await WebLoginAccountInspector(
        browser=browser, artifact_store=store
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "recharge_opened"
    assert result.page_type == "topup_page"
    assert result.page_url == "https://example.com/ai/my-recharge/"
    assert result.avatar_opened is True
    assert [item["artifact_type"] for item in store.calls] == [
        "screenshot", "dom", "screenshot", "dom",
    ]
    assert not any(name == "action" for name, _ in browser.calls)


@pytest.mark.asyncio
async def test_guest_controls_after_fresh_avatar_invalidate_account_confirmation():
    browser = Browser(auth_state="guest")
    store = Store()
    result = await WebLoginAccountInspector(
        browser=browser, artifact_store=store
    ).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "guest_after_fresh_navigation"
    assert result.avatar_opened is True
    assert result.avatar_dom_artifact_id is not None
    assert [item["artifact_type"] for item in store.calls] == [
        "screenshot", "dom", "screenshot", "dom"
    ]
    assert not any(name in {"actions", "action"} for name, _ in browser.calls)


@pytest.mark.asyncio
async def test_unknown_account_state_keeps_avatar_evidence_but_stops_account_navigation():
    browser = Browser(auth_state="unknown", actions=(
        {"kind": "recharge", "box": (0.4, 0.2, 0.5, 0.3)},
    ))
    store = Store()
    result = await WebLoginAccountInspector(browser=browser, artifact_store=store).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "account_auth_unverified"
    assert result.avatar_opened is True
    assert result.avatar_dom_artifact_id == store.calls[3]["artifact_id"]
    assert [item["artifact_type"] for item in store.calls] == [
        "screenshot", "dom", "screenshot", "dom"
    ]
    assert not any(name == "action" for name, _ in browser.calls)


@pytest.mark.asyncio
async def test_wallet_found_on_profile_is_followed_to_recharge():
    class ProfileWalletBrowser(Browser):
        async def account_actions(self, session_id, origin, frame, ocr_hits):
            self.calls.append(("actions", session_id))
            if "/wallet" in self.page_url:
                kind = "recharge"
            elif "/profile" in self.page_url:
                kind = "wallet"
            else:
                kind = "profile"
            return ({"kind": kind, "box": (0.2, 0.2, 0.3, 0.3)},)

    browser = ProfileWalletBrowser()
    store = Store()
    result = await WebLoginAccountInspector(browser=browser, artifact_store=store).inspect(
        session_id="web:1:login", origin="https://example.com",
        task_id="task", source_capture_id="capture",
    )
    assert result.status == "recharge_opened"
    assert [value for name, value in browser.calls if name == "action"] == [
        "profile", "wallet", "recharge"
    ]
    assert [item["artifact_type"] for item in store.calls] == [
        "screenshot", "dom", "screenshot", "dom", "screenshot", "dom",
        "screenshot", "dom", "screenshot", "dom",
    ]
