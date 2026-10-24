"""缺句柄、伪造范围和写回失败不允许重建停止事实。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.infrastructure.browser.capture_runtime import WebCaptureRuntime
from services.api.app.infrastructure.db.h5_browser_stop_receipt import validate_browser_stop_proof
from services.api.tests.test_h5_draining_stop_postgres import proof


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": "h5:other"},
        {"driver_stop_ack": False},
        {"browser_pid": True},
        {"browser_pid": 0},
        {"resource_id": "unknown"},
        {"method": "disconnected"},
        {"observed_at": "2026-10-19T00:00:00"},
    ],
)
def test_receipt_rejects_incomplete_or_wrong_resource_facts(change):
    with pytest.raises((ValueError, TypeError)):
        validate_browser_stop_proof("h5:source", {**proof("h5:source"), **change})


async def test_missing_handle_and_no_durable_receipt_is_not_stop_proof():
    browser = SimpleNamespace(close_session=AsyncMock(return_value=None))
    sessions = SimpleNamespace(
        read_browser_stop=AsyncMock(return_value=None), record_browser_stop=AsyncMock()
    )
    runtime = WebCaptureRuntime(
        coordinator=None,
        session_command_gate=None,
        _browser=browser,
        _executor=None,
        _evidence_buffer=None,
        surface_sessions=sessions,
    )
    with pytest.raises(RuntimeError, match="UNCONFIRMED"):
        await runtime.close_browser_session("h5:source")
    sessions.record_browser_stop.assert_not_awaited()


async def test_failed_persistence_keeps_original_ack_without_second_close():
    original = proof("h5:source")
    browser = SimpleNamespace(close_session=AsyncMock(return_value=original))
    sessions = SimpleNamespace(
        record_browser_stop=AsyncMock(side_effect=[RuntimeError("database"), None])
    )
    runtime = WebCaptureRuntime(
        coordinator=None,
        session_command_gate=None,
        _browser=browser,
        _executor=None,
        _evidence_buffer=None,
        surface_sessions=sessions,
    )
    with pytest.raises(RuntimeError, match="database"):
        await runtime.close_browser_session("h5:source")
    assert runtime._pending_browser_stop_receipts["h5:source"] == original
    await runtime.close_browser_session("h5:source")
    browser.close_session.assert_awaited_once()
    assert runtime._pending_browser_stop_receipts == {}
