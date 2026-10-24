"""独立登录支线失败或超时不得改变 Web 主线结果。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application import web_runtime_stage_login
from services.api.app.application.web_runtime_stage_runner import WebRuntimeStageRunner
from services.api.app.domain.exploration.web_run_stage_checkpoint import (
    WebRunStage,
    WebStageCursor,
)


def command(*, seconds=60):
    return SimpleNamespace(
        task_id="task", agent_run_id="run", session_id="web:run",
        target_url="https://example.test/",
        deadline=datetime.now(UTC) + timedelta(seconds=seconds),
    )


def cursor(*, attempt_count=1):
    return SimpleNamespace(
        attempt_count=attempt_count,
        cursor_payload={"login_source_capture_id": "ready-capture"},
    )


@pytest.mark.asyncio
async def test_frozen_adult_declaration_reaches_isolated_login():
    class Login:
        async def run(self, **kwargs):
            assert kwargs["source_capture_id"] == "ready-capture"
            assert kwargs["adult_declared"] is True
            return SimpleNamespace(status="authenticated", reason_code="WEB_LOGIN_COOKIE_CONFIRMED")

    result = await web_runtime_stage_login.run_web_login_branch(
        branch=Login(), command=command(), cursor=cursor(),
        adult_self_attestation=SimpleNamespace(declared=True),
    )
    assert result.status == "authenticated"


@pytest.mark.asyncio
async def test_login_exception_is_recorded_without_failing_mainline():
    class Login:
        async def run(self, **kwargs):
            raise RuntimeError("secret must not be logged")

    result = await web_runtime_stage_login.run_web_login_branch(
        branch=Login(), command=command(), cursor=cursor(),
        adult_self_attestation=None,
    )
    assert result.status == "incomplete"
    assert result.reason_code == "WEB_LOGIN_BRANCH_FAILED"


@pytest.mark.asyncio
async def test_login_timeout_is_bounded_and_never_replays_submit(monkeypatch):
    monkeypatch.setattr(web_runtime_stage_login, "WEB_LOGIN_BRANCH_TIMEOUT_SECONDS", 0.01)
    called = 0

    class Login:
        async def run(self, **kwargs):
            nonlocal called
            called += 1
            await asyncio.Event().wait()

    branch = Login()
    result = await web_runtime_stage_login.run_web_login_branch(
        branch=branch, command=command(), cursor=cursor(),
        adult_self_attestation=None,
    )
    assert result.status == "incomplete"
    assert result.reason_code == "WEB_LOGIN_BRANCH_TIMEOUT"
    recovered = await web_runtime_stage_login.run_web_login_branch(
        branch=branch, command=command(), cursor=cursor(attempt_count=2),
        adult_self_attestation=None,
    )
    assert recovered.reason_code == "WEB_LOGIN_RETRY_REVIEW_REQUIRED"
    assert called == 1


@pytest.mark.asyncio
async def test_expired_mainline_deadline_skips_login_submission():
    class Login:
        async def run(self, **kwargs):
            raise AssertionError("截止后不得提交")

    result = await web_runtime_stage_login.run_web_login_branch(
        branch=Login(), command=command(seconds=-1), cursor=cursor(),
        adult_self_attestation=None,
    )
    assert result.reason_code == "WEB_LOGIN_DEADLINE_EXHAUSTED"


@pytest.mark.asyncio
async def test_menu_cursor_runs_before_home_login_finalization():
    home = WebStageCursor.start(
        run_id="00000000-0000-0000-0000-000000000006",
        logical_page_key="home", stage=WebRunStage.REPORT_FINALIZATION,
        created_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    menu = WebStageCursor.start(
        run_id=home.run_id, logical_page_key="menu:latest",
        stage=WebRunStage.CLASSIFICATION,
        created_at=datetime.now(UTC),
    )

    class Checkpoints:
        async def list_for_run(self, *, run_id):
            assert run_id == home.run_id
            return (home, menu)

    runner = WebRuntimeStageRunner(checkpoints=Checkpoints())
    due = await runner.next_due(home.run_id)
    assert due.logical_page_key == "menu:latest"


def test_invalid_login_result_is_reduced_to_branch_incomplete():
    home = WebStageCursor.start(
        run_id="00000000-0000-0000-0000-000000000006",
        logical_page_key="home", stage=WebRunStage.REPORT_FINALIZATION,
        cursor_payload={"login_source_capture_id": "ready-capture"},
    )
    malformed = SimpleNamespace(
        status="authenticated", reason_code="WEB_LOGIN_COOKIE_CONFIRMED",
        account_status="unexpected_site_status",
    )
    detail = web_runtime_stage_login.web_login_cursor_detail(malformed, cursor=home)
    assert detail == {
        "login_branch_status": "incomplete",
        "login_branch_reason": "WEB_LOGIN_RESULT_INVALID",
    }
