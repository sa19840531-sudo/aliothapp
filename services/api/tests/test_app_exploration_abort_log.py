"""App 探索因错误未跑完的审计日志。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from services.api.app.application.app_runtime_composition import _record_technical_failure
from services.api.app.infrastructure.device.app_exploration_abort_log import (
    log_app_exploration_abort,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


def test_log_app_exploration_abort_emits_stable_prefix(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="alioth.app.exploration_abort"):
        log_app_exploration_abort(
            "bootstrap",
            task_id="task-1",
            reason_code="OFFICIAL_API_TIMEOUT",
            retryable=True,
            consumed_turns=0,
        )
    messages = " | ".join(record.message for record in caplog.records)
    assert "app_exploration_abort" in messages
    assert "stage=bootstrap" in messages
    assert "reason_code=OFFICIAL_API_TIMEOUT" in messages
    assert "retryable=true" in messages
    assert "consumed_turns=0" in messages


@pytest.mark.asyncio
async def test_record_technical_failure_emits_abort_audit_and_progress_log(
    caplog,
) -> None:
    captured: list[object] = []
    progress_events: list[dict[str, object]] = []

    class Runtime:
        async def record_exploration_failure(self, *, failure):
            captured.append(failure)

    class Progress:
        def on_app_exploration_aborted(self, **kwargs):
            progress_events.append(kwargs)

    record = AgentRunRecord(
        run_id="22222222-2222-2222-2222-222222222222",
        task_id="11111111-1111-1111-1111-111111111111",
        message="test",
        submitted_at=datetime(2026, 10, 24, 2, tzinfo=UTC),
    )
    with caplog.at_level(logging.INFO, logger="alioth.app.exploration_abort"):
        await _record_technical_failure(
            Runtime(),  # type: ignore[arg-type]
            record=record,
            session_id="app:11111111-1111-1111-1111-111111111111",
            phase="bootstrap",
            reason_code="OFFICIAL_API_TIMEOUT",
            error_type="AppWorkflowFailure",
            retryable=True,
            observed_at=datetime(2026, 10, 24, 2, 5, tzinfo=UTC),
            progress_recorder=Progress(),
            error_message="Official App 截图调用失败",
            consumed_turns=0,
            consumed_actions=0,
        )

    assert len(captured) == 1
    assert progress_events == [
        {
            "phase": "bootstrap",
            "reason_code": "OFFICIAL_API_TIMEOUT",
            "error_type": "AppWorkflowFailure",
            "error_message": "Official App 截图调用失败",
            "retryable": True,
            "consumed_turns": 0,
            "consumed_actions": 0,
        }
    ]
    messages = " | ".join(record.message for record in caplog.records)
    assert "app_exploration_abort" in messages
    assert "stage=bootstrap" in messages
    assert "reason_code=OFFICIAL_API_TIMEOUT" in messages
