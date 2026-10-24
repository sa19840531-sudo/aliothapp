"""隔离登录会话中的账户入口与充值导航证据。"""

from dataclasses import dataclass
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid5


class WebLoginPageVisit(NamedTuple):
    page_type: str
    page_url: str
    screenshot_artifact_id: str
    dom_artifact_id: str


@dataclass(frozen=True)
class WebLoginAccountResult:
    status: str
    artifact_ids: tuple[str, ...] = ()
    storage_paths: tuple[str, ...] = ()
    page_type: str | None = None
    page_url: str | None = None
    avatar_opened: bool = False
    avatar_dom_artifact_id: str | None = None
    page_visits: tuple[WebLoginPageVisit, ...] = ()


class WebLoginAccountInspector:
    def __init__(self, *, browser, artifact_store, ocr=None):
        self._browser = browser
        self._artifact_store = artifact_store
        self._ocr = ocr

    async def inspect(
        self,
        *,
        session_id: str,
        origin: str,
        task_id: str,
        source_capture_id: str,
        adult_declared: bool = False,
    ) -> WebLoginAccountResult:
        if not await self._browser.prepare_authenticated_view(session_id, origin):
            return WebLoginAccountResult("secret_residue")
        if (
            await self._browser.clear_obstructions(
                session_id, origin, adult_declared=adult_declared
            )
            != "WEB_LOGIN_OBSTRUCTIONS_CLEAR"
        ):
            return WebLoginAccountResult("obstructed")
        saved = []
        page_visits: list[WebLoginPageVisit] = []

        async def capture_step(step: str):
            include_dom = step in {
                "authenticated", "account_opened", "account_cleared", "profile_opened",
                "wallet_opened", "topup_opened", "account_wallet_revealed"
            }
            frame = await self._browser.capture(
                session_id, origin, include_dom=include_dom
            )
            if not frame.png.startswith(b"\x89PNG\r\n\x1a\n") or len(frame.png) > 16 * 1024 * 1024:
                raise ValueError("WEB_LOGIN_ACCOUNT_SCREENSHOT_INVALID")
            if include_dom and (
                not isinstance(frame.dom, str) or not frame.dom.strip()
            ):
                raise ValueError("WEB_LOGIN_ACCOUNT_DOM_MISSING")
            artifact_id = str(
                uuid5(NAMESPACE_URL, f"web-login:{task_id}:{source_capture_id}:{step}")
            )
            artifact = await self._artifact_store.store(
                task_id=task_id,
                page_capture_id=source_capture_id,
                artifact_id=artifact_id,
                artifact_type="screenshot",
                content=frame.png,
                step=step,
                isolated_session_id=session_id,
            )
            saved.append((artifact.artifact_id, artifact.storage_path, step, "screenshot"))
            if include_dom:
                dom_id = str(
                    uuid5(NAMESPACE_URL, f"web-login:{task_id}:{source_capture_id}:{step}:dom")
                )
                dom_artifact = await self._artifact_store.store(
                    task_id=task_id,
                    page_capture_id=source_capture_id,
                    artifact_id=dom_id,
                    artifact_type="dom",
                    content=frame.dom,
                    step=step,
                    isolated_session_id=session_id,
                )
                saved.append((dom_artifact.artifact_id, dom_artifact.storage_path, step, "dom"))
            return frame

        def record_page(page_type: str, frame, step: str) -> None:
            parsed = urlsplit(frame.url)
            page_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            references = {
                item[3]: item[0] for item in saved if item[2] == step
            }
            page_visits.append(WebLoginPageVisit(
                page_type, page_url, references["screenshot"], references["dom"]
            ))

        def finish(status: str) -> WebLoginAccountResult:
            last = page_visits[-1] if page_visits else None
            return self._result(
                status, saved,
                page_type=last.page_type if last else None,
                page_url=last.page_url if last else None,
                page_visits=tuple(page_visits),
            )

        frame = await capture_step("authenticated")
        obstruction_present = getattr(self._browser, "account_obstruction_present", None)
        wait_account_loading = getattr(self._browser, "wait_account_loading", None)
        recover = getattr(self._browser, "recover_authenticated_entry", None)
        if recover is None:
            recover = self._browser.recover_entry
        avatar = await recover(session_id, origin)
        if avatar is None and callable(obstruction_present) and await obstruction_present(
            session_id, origin
        ):
            if (
                await self._browser.clear_obstructions(
                    session_id, origin, adult_declared=adult_declared
                )
                != "WEB_LOGIN_OBSTRUCTIONS_CLEAR"
            ):
                return self._result("avatar_obstructed", saved)
            frame = await capture_step("authenticated_recovered")
            avatar = await recover(session_id, origin)
        if avatar is None:
            return self._result("avatar_missing", saved)
        await self._browser.click(session_id, origin, frame, avatar)
        if (
            await self._browser.clear_obstructions(
                session_id, origin, adult_declared=adult_declared
            )
            != "WEB_LOGIN_OBSTRUCTIONS_CLEAR"
        ):
            return self._result("avatar_obstructed", saved)
        account_step = "account_opened"
        account_loaded = (
            not callable(wait_account_loading)
            or await wait_account_loading(session_id, origin)
        )
        frame = await capture_step(account_step)
        if not account_loaded:
            return self._result("account_auth_unverified", saved)
        if callable(obstruction_present) and await obstruction_present(session_id, origin):
            if (
                await self._browser.clear_obstructions(
                    session_id, origin, adult_declared=adult_declared
                )
                != "WEB_LOGIN_OBSTRUCTIONS_CLEAR"
            ):
                return self._result("avatar_obstructed", saved)
            account_step = "account_cleared"
            account_loaded = (
                not callable(wait_account_loading)
                or await wait_account_loading(session_id, origin)
            )
            frame = await capture_step(account_step)
            if not account_loaded:
                return self._result("account_auth_unverified", saved)
        auth_state = await self._browser.account_auth_state(
            session_id, origin, frame
        )
        if auth_state == "guest":
            return self._result("guest_after_fresh_navigation", saved)
        if auth_state != "authenticated":
            return self._result("account_auth_unverified", saved)

        direct_hits = await self._ocr.read(frame.png) if self._ocr is not None else ()
        if await self._browser.confirm_recharge_page(
            session_id, origin, frame, direct_hits
        ):
            actions = await self._browser.account_actions(
                session_id, origin, frame, direct_hits
            )
            wallets = [action for action in actions if action["kind"] == "wallet"]
            if len(wallets) > 1:
                return finish("entry_ambiguous")
            reveal = await self._browser.reveal_account_action(
                session_id, origin, frame, "wallet"
            )
            if reveal == "ambiguous":
                return finish("entry_ambiguous")
            if not wallets and reveal == "revealed":
                frame = await capture_step("account_wallet_revealed")
                account_step = "account_wallet_revealed"
                direct_hits = await self._ocr.read(frame.png) if self._ocr is not None else ()
                actions = await self._browser.account_actions(
                    session_id, origin, frame, direct_hits
                )
                wallets = [action for action in actions if action["kind"] == "wallet"]
                if len(wallets) > 1:
                    return finish("entry_ambiguous")
            if not wallets:
                if account_step == "account_wallet_revealed":
                    if not await self._browser.confirm_recharge_page(
                        session_id, origin, frame, direct_hits
                    ):
                        return finish("entry_unproven")
                record_page("topup_page", frame, account_step)
                return finish("recharge_opened")
        confirm_wallet = getattr(self._browser, "confirm_wallet_page", None)
        visited_kinds: set[str] = set()
        if callable(confirm_wallet) and await confirm_wallet(
            session_id, origin, frame, direct_hits
        ):
            record_page("recharge_page", frame, account_step)
            visited_kinds.add("wallet")

        for step in ("account", "profile", "wallet"):
            ocr_hits = await self._ocr.read(frame.png) if self._ocr is not None else ()
            actions = await self._browser.account_actions(
                session_id, origin, frame, ocr_hits
            )
            selected = None
            for kind in ("wallet", "recharge", "profile"):
                if kind in visited_kinds:
                    continue
                candidates = [action for action in actions if action["kind"] == kind]
                if len(candidates) > 1:
                    return finish("entry_ambiguous")
                reveal = await self._browser.reveal_account_action(
                    session_id, origin, frame, kind
                )
                if reveal == "ambiguous":
                    return finish("entry_ambiguous")
                if not candidates and reveal == "revealed":
                    frame = await capture_step(f"{step}_{kind}_revealed")
                    ocr_hits = await self._ocr.read(frame.png) if self._ocr is not None else ()
                    actions = await self._browser.account_actions(
                        session_id, origin, frame, ocr_hits
                    )
                    candidates = [
                        action for action in actions if action["kind"] == kind
                    ]
                    if len(candidates) > 1:
                        return finish("entry_ambiguous")
                if candidates:
                    selected = candidates[0]
                    break
            if selected is None:
                break
            opened = await self._browser.click_account_action(
                session_id, origin, frame, selected
            )
            if not opened:
                return finish("entry_unproven")
            kind = selected["kind"]
            visited_kinds.add(kind)
            if (
                await self._browser.clear_obstructions(
                    session_id, origin, adult_declared=adult_declared
                )
                != "WEB_LOGIN_OBSTRUCTIONS_CLEAR"
            ):
                return finish("entry_obstructed")
            capture_role = {
                "wallet": "wallet_opened",
                "recharge": "topup_opened",
                "profile": "profile_opened",
            }[kind]
            account_loaded = (
                not callable(wait_account_loading)
                or await wait_account_loading(session_id, origin)
            )
            frame = await capture_step(capture_role)
            if not account_loaded:
                return finish("entry_unproven")
            if kind == "profile":
                continue
            fresh_hits = await self._ocr.read(frame.png) if self._ocr is not None else ()
            if kind == "wallet":
                if not callable(confirm_wallet) or not await confirm_wallet(
                    session_id, origin, frame, fresh_hits
                ):
                    if await self._browser.confirm_recharge_page(
                        session_id, origin, frame, fresh_hits
                    ):
                        record_page("topup_page", frame, capture_role)
                        return finish("recharge_opened")
                    return finish("entry_unproven")
                record_page("recharge_page", frame, capture_role)
                continue
            if not await self._browser.confirm_recharge_page(
                session_id, origin, frame, fresh_hits
            ):
                return finish("entry_unproven")
            record_page("topup_page", frame, capture_role)
            return finish("recharge_opened")
        return finish("wallet_opened" if page_visits else "account_observed")

    @staticmethod
    def _result(
        status: str, saved, *, page_type: str | None = None, page_url: str | None = None,
        page_visits: tuple[WebLoginPageVisit, ...] = (),
    ) -> WebLoginAccountResult:
        return WebLoginAccountResult(
            status,
            tuple(item[0] for item in saved),
            tuple(item[1] for item in saved),
            page_type,
            page_url,
            any(
                item[2] in {"account_opened", "account_cleared"} and item[3] == "dom"
                for item in saved
            ),
            next(
                (item[0] for item in saved
                 if item[2] == "account_cleared" and item[3] == "dom"),
                next(
                    (item[0] for item in saved
                     if item[2] == "account_opened" and item[3] == "dom"),
                    None,
                ),
            ),
            page_visits,
        )
