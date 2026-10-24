"""上一页也发生跳转时，仅从真实后帧有界恢复到已授权入口。"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.web_desktop_exploration import WebDesktopMenuPageVisitor
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand


async def _recover(*, previous="https://example.test/previous", urls=(), error=None):
    calls = []

    class Coordinator:
        async def coordinate(self, command):
            calls.append(command)
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id=f"after-{len(calls)}"))

    class Reader:
        async def read_url(self, *, capture_id):
            if error is not None:
                raise error
            return urls[int(capture_id.split("-")[1]) - 1]

    command = RunWebWorkflowCommand(
        task_id="task", agent_run_id="run", session_id="web:run", session_owner="web-v2",
        target_url="https://example.test/", browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    visitor = WebDesktopMenuPageVisitor(coordinator=Coordinator(), capture_url_reader=Reader())
    result = await visitor._recover_from_unsafe_navigation(
        command=command, unsafe_capture_id="unsafe", recovery_url=previous,
        menu_intent_id="menu", action_parameters={},
    )
    return result, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [
    "https://elsewhere.test/", "http://example.test/previous", "https://example.test/other",
])
async def test_unstable_previous_page_falls_back_to_authorized_entry_from_actual_capture(first):
    result, calls = await _recover(urls=(first, "https://example.test/"))
    assert result == "after-2"
    assert [item.intent.source_capture_id for item in calls] == ["unsafe", "after-1"]
    assert [item.intent.parameters["destination_url"] for item in calls] == [
        "https://example.test/previous", "https://example.test/",
    ]
    assert len({item.intent.idempotency_key for item in calls}) == 2
    assert all(item.intent.parameters.get("full_page") is False for item in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("final", ["https://elsewhere.test/", "http://example.test/",
                                   "https://example.test/other"])
async def test_entry_fallback_cannot_publish_cross_origin_downgrade_or_wrong_page(final):
    result, calls = await _recover(urls=("https://elsewhere.test/", final))
    assert result is None
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_successful_previous_page_does_not_navigate_entry_again():
    result, calls = await _recover(urls=("https://example.test/previous",))
    assert result == "after-1"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_same_entry_recovery_is_not_repeated():
    result, calls = await _recover(previous="https://example.test/", urls=("https://elsewhere.test/",))
    assert result is None
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    RuntimeError("WEB_CAPTURE_CHECKSUM_MISMATCH"), ConnectionError(),
])
async def test_unverified_recovery_capture_does_not_authorize_second_action(error):
    result, calls = await _recover(error=error)
    assert result is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_for_fallback():
    with pytest.raises(asyncio.CancelledError):
        await _recover(error=asyncio.CancelledError())
