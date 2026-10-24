"""Web 首页的白名单直登支线；不生成账户，也不把秘密交给模型。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from services.api.app.application.web_login_allowlist import WebLoginAllowlist


@dataclass(frozen=True)
class WebLoginOutcome:
    status: Literal["skipped", "authenticated", "incomplete"]
    reason_code: str
    capture_id: str | None = None
    cross_origin_status: Literal["clear", "blocked"] = "clear"
    blocked_requests: tuple[tuple[str, str], ...] = ()
    account_status: str = "not_run"
    account_artifact_ids: tuple[str, ...] = ()
    account_storage_paths: tuple[str, ...] = ()
    account_page_type: str | None = None
    account_page_url: str | None = None
    account_avatar_opened: bool = False
    account_avatar_dom_artifact_id: str | None = None
    account_page_visits: tuple[tuple[str, str, str, str], ...] = ()


class WebLoginBranch:
    """在独立上下文中找入口、提交一次，并复核登录结果。"""

    def __init__(
        self, *, allowlist: WebLoginAllowlist, browser, visual, observer=None, inspector=None
    ):
        self._allowlist = allowlist
        self._browser = browser
        self._visual = visual
        self._observer = observer
        self._inspector = inspector

    async def run(
        self,
        *,
        session_id: str,
        target_url: str,
        task_id: str | None = None,
        source_capture_id: str | None = None,
        observer=None,
        attempt_count: int = 1,
        adult_declared: bool = False,
    ) -> WebLoginOutcome:
        profile = self._allowlist.resolve(target_url)
        http_candidate = False
        if profile is None:
            profile = self._allowlist.redirect_candidate(target_url)
            if profile is None:
                return WebLoginOutcome("skipped", "WEB_LOGIN_NOT_ALLOWLISTED")
            http_candidate = True
        if attempt_count != 1:
            return WebLoginOutcome("incomplete", "WEB_LOGIN_RETRY_REVIEW_REQUIRED")
        origin = profile.origin
        secret_window_opened = False
        branch_session_id = session_id
        opened_isolated = False

        async def authenticated(reason_code: str, capture_id: str | None) -> WebLoginOutcome:
            account_status = "not_run"
            artifact_ids: tuple[str, ...] = ()
            storage_paths: tuple[str, ...] = ()
            account_page_type: str | None = None
            account_page_url: str | None = None
            account_avatar_opened = False
            account_avatar_dom_artifact_id: str | None = None
            account_page_visits: tuple[tuple[str, str, str, str], ...] = ()
            if self._inspector is not None and task_id and source_capture_id and opened_isolated:
                try:
                    inspection = await self._inspector.inspect(
                        session_id=branch_session_id,
                        origin=origin,
                        task_id=task_id,
                        source_capture_id=source_capture_id,
                        adult_declared=adult_declared,
                    )
                    account_status = inspection.status
                    artifact_ids = tuple(inspection.artifact_ids)
                    storage_paths = tuple(inspection.storage_paths)
                    account_avatar_opened = bool(
                        getattr(inspection, "avatar_opened", False)
                    )
                    account_avatar_dom_artifact_id = getattr(
                        inspection, "avatar_dom_artifact_id", None
                    )
                    account_page_type = getattr(inspection, "page_type", None)
                    account_page_url = getattr(inspection, "page_url", None)
                    account_page_visits = tuple(
                        tuple(visit) for visit in getattr(inspection, "page_visits", ())
                    )
                except Exception as exc:  # noqa: BLE001 - 账户页面错误不得抹去认证事实
                    account_status = (
                        "artifact_index_failed"
                        if getattr(exc, "error_code", None)
                        == "WEB_LOGIN_ACCOUNT_ARTIFACT_INDEX_FAILED"
                        else "inspection_failed"
                    )
            blocked = self._blocked_cross_origin(branch_session_id)
            blocked_requests = self._blocked_requests(branch_session_id)
            account_failure_reason = {
                "guest_after_fresh_navigation": "WEB_LOGIN_SESSION_NOT_PERSISTED",
                "account_auth_unverified": "WEB_LOGIN_ACCOUNT_UI_UNVERIFIED",
                "avatar_missing": "WEB_LOGIN_AVATAR_NOT_FOUND",
                "secret_residue": "WEB_LOGIN_ACCOUNT_EVIDENCE_UNAVAILABLE",
                "obstructed": "WEB_LOGIN_ACCOUNT_EVIDENCE_UNAVAILABLE",
                "avatar_obstructed": "WEB_LOGIN_ACCOUNT_EVIDENCE_UNAVAILABLE",
                "inspection_failed": "WEB_LOGIN_ACCOUNT_EVIDENCE_UNAVAILABLE",
            }.get(account_status)
            if (
                account_status == "artifact_index_failed"
                and reason_code == "WEB_LOGIN_ACCOUNT_UI_CONFIRMED_ISOLATED"
            ):
                account_failure_reason = "WEB_LOGIN_ACCOUNT_EVIDENCE_UNAVAILABLE"
            if account_status in {
                "guest_after_fresh_navigation", "account_auth_unverified"
            }:
                failure_reader = getattr(self._browser, "login_server_failure", None)
                if callable(failure_reader):
                    server_failure = failure_reader(branch_session_id)
                    if server_failure == "WEB_LOGIN_CREDENTIALS_REJECTED":
                        account_failure_reason = server_failure
            return WebLoginOutcome(
                "incomplete" if account_failure_reason else "authenticated",
                account_failure_reason or reason_code,
                capture_id,
                "blocked" if blocked else "clear",
                blocked_requests,
                account_status,
                artifact_ids,
                storage_paths,
                account_page_type,
                account_page_url,
                account_avatar_opened,
                account_avatar_dom_artifact_id,
                account_page_visits,
            )

        try:
            open_isolated = getattr(self._browser, "open_isolated", None)
            if callable(open_isolated):
                branch_session_id = await open_isolated(session_id, origin)
                opened_isolated = True
            clear = getattr(self._browser, "clear_obstructions", None)
            if callable(clear):
                clearance = await clear(branch_session_id, origin, adult_declared=adult_declared)
                if clearance != "WEB_LOGIN_OBSTRUCTIONS_CLEAR":
                    return WebLoginOutcome("incomplete", clearance, source_capture_id)
            frame = await self._browser.capture(branch_session_id, origin)
            if await self._visual.already_authenticated(frame.png):
                return await authenticated("WEB_LOGIN_ALREADY_AUTHENTICATED", source_capture_id)
            last_capture_id = source_capture_id
            stages = ["avatar", "login"]
            for stage in stages:
                if await self._browser.form_ready(
                    branch_session_id, origin, profile.identifier_kind
                ):
                    break
                prior_url = frame.url
                recovery_name = "recover_entry" if stage == "avatar" else "recover_login"
                recovery = getattr(self._browser, recovery_name, None)
                box = await recovery(branch_session_id, origin) if callable(recovery) else None
                if box is None:
                    box = await self._visual.locate_entry(frame.png, stage)
                if box is None:
                    return WebLoginOutcome(
                        "incomplete", "WEB_LOGIN_ENTRY_UNCERTAIN", last_capture_id
                    )
                try:
                    await self._browser.click(branch_session_id, origin, frame, box)
                except Exception as exc:
                    if (
                        stage != "avatar"
                        or getattr(exc, "reason_code", None) != ("WEB_LOGIN_TARGET_UNPROVEN")
                        or not callable(clear)
                    ):
                        raise
                    clearance = await clear(
                        branch_session_id, origin, adult_declared=adult_declared
                    )
                    if clearance != "WEB_LOGIN_OBSTRUCTIONS_CLEAR":
                        return WebLoginOutcome("incomplete", clearance, last_capture_id)
                    frame = await self._browser.capture(branch_session_id, origin)
                    recovery = getattr(self._browser, "recover_entry", None)
                    fresh_box = (
                        await recovery(branch_session_id, origin) if callable(recovery) else None
                    )
                    if fresh_box is None:
                        return WebLoginOutcome(
                            "incomplete", "WEB_LOGIN_ENTRY_UNCERTAIN", last_capture_id
                        )
                    await self._browser.click(branch_session_id, origin, frame, fresh_box)
                wait = getattr(self._browser, "wait", None)
                for _ in range(3):
                    if await self._browser.form_ready(
                        branch_session_id, origin, profile.identifier_kind
                    ):
                        break
                    if callable(wait):
                        await wait(branch_session_id, 700)
                if callable(clear) and not await self._browser.form_ready(
                    branch_session_id, origin, profile.identifier_kind
                ):
                    clearance = await clear(
                        branch_session_id, origin, adult_declared=adult_declared
                    )
                    if clearance != "WEB_LOGIN_OBSTRUCTIONS_CLEAR":
                        return WebLoginOutcome("incomplete", clearance, last_capture_id)
                frame = await self._browser.capture(branch_session_id, origin)
                if stage == "login" and frame.url != prior_url:
                    # 账户菜单进入同源账户页后，允许在新页面再找一次人像入口。
                    stages.append("avatar")
                elif stage == "avatar" and len(stages) == 3:
                    # 新账户页的人像可能先展开菜单，再切换到登录表单。
                    stages.append("login")
            if not await self._browser.form_ready(
                branch_session_id, origin, profile.identifier_kind
            ):
                return WebLoginOutcome("incomplete", "WEB_LOGIN_FORM_UNPROVEN", last_capture_id)
            evidence_reader = getattr(self._browser, "session_evidence", None)
            before = (
                await evidence_reader(branch_session_id, origin)
                if callable(evidence_reader)
                else None
            )
            identifier, password = profile.reveal_for_runtime()
            secret_window_opened = True
            reason = await self._browser.submit(
                branch_session_id, origin, profile.identifier_kind, identifier, password
            )
            blocked_requests = self._blocked_requests(branch_session_id)
            cross_origin_status = (
                "blocked"
                if reason == "WEB_LOGIN_CROSS_ORIGIN_BLOCKED"
                or self._blocked_cross_origin(branch_session_id)
                else "clear"
            )
            if reason == "WEB_LOGIN_CROSS_ORIGIN_BLOCKED":
                after = (
                    await evidence_reader(branch_session_id, origin)
                    if callable(evidence_reader)
                    else None
                )
                cookie_changed = bool(
                    before is not None
                    and after is not None
                    and after.get("auth_cookie_signatures") - before.get("auth_cookie_signatures")
                )
                form_visible = await self._browser.form_ready(
                    branch_session_id, origin, profile.identifier_kind
                )
                if cookie_changed and not form_visible:
                    return await authenticated("WEB_LOGIN_COOKIE_CONFIRMED", last_capture_id)
                await self._quarantine(branch_session_id)
                return WebLoginOutcome(
                    "incomplete",
                    "WEB_LOGIN_CROSS_ORIGIN_BLOCKED",
                    last_capture_id,
                    "blocked",
                    blocked_requests,
                )
            if reason == "WEB_LOGIN_SECRET_RESIDUE":
                # 页面残留秘密时只允许本地只读复核，绝不生成截图或交给视觉模型。
                after = (
                    await evidence_reader(branch_session_id, origin)
                    if callable(evidence_reader)
                    else None
                )
                cookie_changed = bool(
                    before is not None
                    and after is not None
                    and after.get("auth_cookie_signatures") - before.get("auth_cookie_signatures")
                )
                form_visible = await self._browser.form_ready(
                    branch_session_id, origin, profile.identifier_kind
                )
                if cookie_changed and not form_visible:
                    return await authenticated(
                        "WEB_LOGIN_COOKIE_CONFIRMED_ISOLATED", last_capture_id
                    )
                if (
                    self._inspector is not None
                    and task_id
                    and source_capture_id
                    and opened_isolated
                ):
                    # 旧页仍有秘密时，只让账户复核器在新同源文档留证并判断登录态。
                    return await authenticated(
                        "WEB_LOGIN_ACCOUNT_UI_CONFIRMED_ISOLATED", last_capture_id
                    )
                return WebLoginOutcome(
                    "incomplete",
                    "WEB_LOGIN_FORM_STILL_VISIBLE"
                    if form_visible
                    else "WEB_LOGIN_AUTH_COOKIE_UNCHANGED",
                    last_capture_id,
                    cross_origin_status,
                    blocked_requests,
                )
            if reason != "WEB_LOGIN_SUBMITTED":
                await self._quarantine(branch_session_id)
                return WebLoginOutcome(
                    "incomplete",
                    reason,
                    last_capture_id,
                    cross_origin_status,
                    blocked_requests,
                )
            for _ in range(3):
                frame = await self._browser.capture(branch_session_id, origin)
                if await self._visual.verify_result(frame.png):
                    if self._blocked_cross_origin(branch_session_id):
                        after = (
                            await evidence_reader(branch_session_id, origin)
                            if callable(evidence_reader)
                            else None
                        )
                        if not (
                            before is not None
                            and after is not None
                            and after.get("auth_cookie_signatures")
                            - before.get("auth_cookie_signatures")
                            and not await self._browser.form_ready(
                                branch_session_id, origin, profile.identifier_kind
                            )
                        ):
                            await self._quarantine(branch_session_id)
                            return WebLoginOutcome(
                                "incomplete",
                                "WEB_LOGIN_CROSS_ORIGIN_BLOCKED",
                                last_capture_id,
                                "blocked",
                                self._blocked_requests(branch_session_id),
                            )
                    return await authenticated("WEB_LOGIN_AUTHENTICATED", last_capture_id)
                after = (
                    await evidence_reader(branch_session_id, origin)
                    if callable(evidence_reader)
                    else None
                )
                if (
                    before is not None
                    and after is not None
                    and after.get("auth_cookie_signatures") - before.get("auth_cookie_signatures")
                    and not await self._browser.form_ready(
                        branch_session_id, origin, profile.identifier_kind
                    )
                ):
                    return await authenticated("WEB_LOGIN_COOKIE_CONFIRMED", last_capture_id)
                wait = getattr(self._browser, "wait", None)
                if callable(wait):
                    await wait(branch_session_id, 1500)
            if self._inspector is not None and task_id and source_capture_id and opened_isolated:
                # 结果不明时先用隔离新文档复核，避免再次点击旧页的游客入口。
                return await authenticated(
                    "WEB_LOGIN_ACCOUNT_UI_CONFIRMED_ISOLATED", last_capture_id
                )
            recovery = getattr(self._browser, "recover_entry", None)
            if callable(recovery):
                box = await recovery(branch_session_id, origin)
            else:
                box = None
            if box is not None:
                await self._browser.click(branch_session_id, origin, frame, box)
                frame = await self._browser.capture(branch_session_id, origin)
            if await self._visual.verify_result(frame.png):
                if self._blocked_cross_origin(branch_session_id):
                    after = (
                        await evidence_reader(branch_session_id, origin)
                        if callable(evidence_reader)
                        else None
                    )
                    if not (
                        before is not None
                        and after is not None
                        and after.get("auth_cookie_signatures")
                        - before.get("auth_cookie_signatures")
                        and not await self._browser.form_ready(
                            branch_session_id, origin, profile.identifier_kind
                        )
                    ):
                        await self._quarantine(branch_session_id)
                        return WebLoginOutcome(
                            "incomplete",
                            "WEB_LOGIN_CROSS_ORIGIN_BLOCKED",
                            last_capture_id,
                            "blocked",
                            self._blocked_requests(branch_session_id),
                        )
                return await authenticated("WEB_LOGIN_AUTHENTICATED", last_capture_id)
            await self._quarantine(branch_session_id)
            return WebLoginOutcome(
                "incomplete",
                "WEB_LOGIN_UNVERIFIED",
                last_capture_id,
                "blocked" if self._blocked_cross_origin(branch_session_id) else "clear",
                self._blocked_requests(branch_session_id),
            )
        except Exception as exc:  # noqa: BLE001 - 外部异常可能携带页面或秘密内容
            if secret_window_opened:
                await self._quarantine(branch_session_id)
            if http_candidate and getattr(exc, "reason_code", None) == "WEB_LOGIN_ORIGIN_MISMATCH":
                return WebLoginOutcome("incomplete", "WEB_LOGIN_HTTPS_REQUIRED")
            return WebLoginOutcome("incomplete", _safe_failure_code(exc))
        finally:
            if opened_isolated:
                close = getattr(self._browser, "close_isolated", None)
                if callable(close):
                    await close(branch_session_id)

    async def _quarantine(self, session_id: str) -> None:
        quarantine = getattr(self._browser, "quarantine", None)
        if not callable(quarantine):
            raise RuntimeError("WEB_LOGIN_QUARANTINE_UNAVAILABLE")
        try:
            await quarantine(session_id)
        except Exception:  # noqa: BLE001 - 清理失败必须中止后续普通采集
            raise RuntimeError("WEB_LOGIN_QUARANTINE_FAILED") from None

    def _blocked_cross_origin(self, session_id: str) -> bool:
        checker = getattr(self._browser, "blocked_cross_origin", None)
        return bool(checker(session_id)) if callable(checker) else False

    def _blocked_requests(self, session_id: str) -> tuple[tuple[str, str], ...]:
        reader = getattr(self._browser, "blocked_request_summary", None)
        return tuple(reader(session_id)) if callable(reader) else ()


def _safe_failure_code(exc: Exception) -> str:
    code = getattr(exc, "reason_code", None)
    if isinstance(code, str) and re.fullmatch(r"WEB_LOGIN_[A-Z0-9_]{1,64}", code):
        return code
    return "WEB_LOGIN_EXECUTION_FAILED"
