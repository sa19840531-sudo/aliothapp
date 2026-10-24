"""验证 App 取消清理失败时的可重试状态与日志脱敏边界。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.api.app import worker as worker_module
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    AgentRunRecord,
)


class _RecordingQueue:
    """只记录取消投影，避免测试连接 Redis。"""

    def __init__(self) -> None:
        self.saved: list[AgentRunRecord] = []

    async def save(self, record: AgentRunRecord) -> None:
        self.saved.append(record.model_copy(deep=True))


class _SuccessfulWorkRepository:
    async def cancel_nonterminal(self, **kwargs: object) -> int:
        del kwargs
        return 0


class _SuccessfulActionRepository:
    async def fail_nonterminal_for_task(self, **kwargs: object) -> int:
        del kwargs
        return 0


class _FailingWorkRepository:
    def __init__(self, message: str) -> None:
        self._message = message

    async def cancel_nonterminal(self, **kwargs: object) -> int:
        del kwargs
        raise RuntimeError(self._message)


class _FailingActionRepository:
    def __init__(self, message: str) -> None:
        self._message = message

    async def fail_nonterminal_for_task(self, **kwargs: object) -> int:
        del kwargs
        raise RuntimeError(self._message)


def _app_record() -> AgentRunRecord:
    return AgentRunRecord(
        run_id=str(uuid4()),
        task_id=str(uuid4()),
        message="取消清理日志边界测试",
        submitted_at=datetime.now(UTC),
        status=AgentRunQueueStatus.RUNNING,
        task_config={"_task_target": {"platform": "app"}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_stage", ("work", "action"))
async def test_cancel_cleanup_failure_is_retryable_and_redacts_exception(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    failing_stage: str,
) -> None:
    secret = f"secret-canary-{failing_stage}-token"
    if failing_stage == "work":
        monkeypatch.setattr(
            worker_module,
            "PostgresExplorerWorkRepository",
            lambda: _FailingWorkRepository(secret),
        )
        monkeypatch.setattr(
            worker_module,
            "PostgresActionExecutionRepository",
            _SuccessfulActionRepository,
        )
    else:
        monkeypatch.setattr(
            worker_module,
            "PostgresExplorerWorkRepository",
            _SuccessfulWorkRepository,
        )
        monkeypatch.setattr(
            worker_module,
            "PostgresActionExecutionRepository",
            lambda: _FailingActionRepository(secret),
        )

    queue = _RecordingQueue()
    record = _app_record()

    with caplog.at_level(logging.WARNING, logger=worker_module.__name__):
        cleaned = await worker_module._project_run_canceled_and_cleanup(
            queue=queue,
            record=record,
        )

    assert cleaned is False
    assert record.status is AgentRunQueueStatus.CANCELED
    assert record.cancellation_cleanup_completed is False
    assert queue.saved
    assert queue.saved[-1].status is AgentRunQueueStatus.CANCELED
    assert queue.saved[-1].cancellation_cleanup_completed is False

    records = [entry for entry in caplog.records if entry.name == worker_module.__name__]
    assert any(
        f"task_id={record.task_id}" in entry.getMessage()
        and f"run_id={record.run_id}" in entry.getMessage()
        and "error_type=RuntimeError" in entry.getMessage()
        for entry in records
    )
    assert secret not in caplog.text
    assert all(entry.exc_info is None for entry in records)
