"""Web 主线结束后独立结算登录支线，失败只进入支线游标。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime

from services.api.app.application.web_login_branch import WebLoginOutcome

_LOGGER = logging.getLogger(__name__)
WEB_LOGIN_BRANCH_TIMEOUT_SECONDS = 180


async def run_web_login_branch(*, branch, command, cursor, adult_self_attestation):
    """只允许首次收口执行提交；阶段接管后不重放凭据。"""

    if cursor.attempt_count != 1:
        return WebLoginOutcome("incomplete", "WEB_LOGIN_RETRY_REVIEW_REQUIRED")
    remaining = (command.deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return WebLoginOutcome("incomplete", "WEB_LOGIN_DEADLINE_EXHAUSTED")
    try:
        return await asyncio.wait_for(
            branch.run(
                task_id=command.task_id,
                session_id=command.session_id,
                target_url=command.target_url,
                source_capture_id=cursor.cursor_payload["login_source_capture_id"],
                attempt_count=cursor.attempt_count,
                adult_declared=bool(getattr(adult_self_attestation, "declared", False)),
            ),
            timeout=min(WEB_LOGIN_BRANCH_TIMEOUT_SECONDS, remaining),
        )
    except TimeoutError:
        return WebLoginOutcome("incomplete", "WEB_LOGIN_BRANCH_TIMEOUT")
    except Exception as exc:  # noqa: BLE001 - 不记录可能携带凭据的异常文本
        _LOGGER.warning(
            "web_login_branch_failed run_id=%s type=%s",
            command.agent_run_id, type(exc).__name__,
        )
        return WebLoginOutcome("incomplete", "WEB_LOGIN_BRANCH_FAILED")


def web_login_cursor_detail(login, *, cursor) -> dict[str, object]:
    try:
        detail = {
            "login_branch_status": login.status,
            "login_branch_reason": login.reason_code,
            "login_cross_origin_status": getattr(login, "cross_origin_status", "clear"),
            "login_blocked_requests": tuple(getattr(login, "blocked_requests", ()))[:20],
            "login_account_status": getattr(login, "account_status", "not_run"),
            "login_account_artifact_ids": tuple(
                getattr(login, "account_artifact_ids", ())
            )[:24],
            "login_account_storage_paths": tuple(
                getattr(login, "account_storage_paths", ())
            )[:24],
            "login_account_avatar_opened": bool(
                getattr(login, "account_avatar_opened", False)
            ),
        }
        avatar_dom_id = getattr(login, "account_avatar_dom_artifact_id", None)
        if avatar_dom_id:
            detail["login_account_avatar_dom_artifact_id"] = avatar_dom_id
        account_page_type = getattr(login, "account_page_type", None)
        account_page_url = getattr(login, "account_page_url", None)
        if account_page_type in {"recharge_page", "topup_page"} and account_page_url:
            detail["login_account_page_type"] = account_page_type
            detail["login_account_page_url"] = account_page_url
        visits = tuple(getattr(login, "account_page_visits", ()))[:2]
        if visits:
            detail["login_account_page_visits"] = visits
        replace(cursor, cursor_payload={**cursor.cursor_payload, **detail})
        return detail
    except Exception as exc:  # noqa: BLE001 - 支线结果不允许使主线游标失败
        _LOGGER.warning("web_login_result_invalid type=%s", type(exc).__name__)
        return {
            "login_branch_status": "incomplete",
            "login_branch_reason": "WEB_LOGIN_RESULT_INVALID",
        }
