"""使用完整行与可控数据库时钟复现等待锁跨越租约的仓储行为。"""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from services.api.app.domain.exploration.web_run_stage_checkpoint import WebStageCursor
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
    WebStageCheckpointConflict,
)
from services.api.app.infrastructure.db.web_run_stage_checkpoint_row import (
    web_run_stage_checkpoint_values,
)
from services.api.tests.test_web_run_stage_checkpoint import NOW, RUN_ID


class ClockedCheckpointConnection:
    """仅替代数据库锁等待；领域转换和仓储查询构造保持真实。"""

    def __init__(self, cursor, *, locked_at, persisted_at=None):
        self.row = web_run_stage_checkpoint_values(cursor)
        self.locked_at = locked_at
        self.persisted_at = persisted_at or locked_at
        self.has_row_lock = False

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchval(self, sql):
        if sql.strip() == "SELECT clock_timestamp()":
            if not self.has_row_lock:
                raise AssertionError("数据库时间必须在取得 checkpoint 行锁后读取")
            return self.locked_at
        raise AssertionError(f"未建模数据库读取：{sql}")

    async def fetchrow(self, sql, *values):
        if "SELECT task.finalization_epoch" in sql:
            return {"finalization_epoch": 0}
        if "SELECT * FROM qc_web_run_stage_checkpoint" in sql:
            self.has_row_lock = "FOR UPDATE" in sql
            return dict(self.row)
        if sql.startswith("UPDATE qc_web_run_stage_checkpoint"):
            if "lease_expires_at > clock_timestamp()" in sql:
                if self.row["lease_expires_at"] <= self.persisted_at:
                    return None
            assignments = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            for assignment in assignments.split(","):
                column, argument = assignment.split("=")
                self.row[column.strip()] = values[int(argument.removeprefix("$")) - 1]
            return dict(self.row)
        raise AssertionError(f"未建模数据库查询：{sql}")


async def test_list_for_run_preserves_terminal_and_retry_cursor_for_resume_scheduler():
    cursor = WebStageCursor.start(run_id=RUN_ID, logical_page_key="url:1")
    connection = ClockedCheckpointConnection(cursor, locked_at=NOW)

    async def fetch(sql, run_id):
        assert run_id == RUN_ID
        return [connection.row]

    connection.fetch = fetch
    result = await PostgresWebRunStageCheckpointRepository(pool=connection).list_for_run(
        run_id=RUN_ID
    )
    assert len(result) == 1
    assert result[0].logical_page_key == "url:1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["mark_succeeded", "schedule_retry", "record_gap_and_advance"]
)
async def test_settlement_rejects_lease_that_expired_while_waiting_for_locks(operation):
    cursor = WebStageCursor.start(run_id=RUN_ID, logical_page_key="url:1").claim(
        worker_id="worker", now=NOW, lease_seconds=60
    )
    connection = ClockedCheckpointConnection(cursor, locked_at=NOW + timedelta(seconds=61))
    repo = PostgresWebRunStageCheckpointRepository(pool=connection)
    kwargs = {} if operation == "mark_succeeded" else {"error_code": "WEB_GOTO_TLS_FAILED"}
    with pytest.raises(WebStageCheckpointConflict):
        await getattr(repo, operation)(cursor, now=NOW, **kwargs)
    assert connection.row["stage_status"] == "running"
    assert connection.row["row_version"] == 0


@pytest.mark.asyncio
async def test_claim_lease_starts_at_database_time_after_all_locks():
    cursor = WebStageCursor.start(run_id=RUN_ID, logical_page_key="url:1")
    connection = ClockedCheckpointConnection(cursor, locked_at=NOW + timedelta(seconds=61))
    result = await PostgresWebRunStageCheckpointRepository(pool=connection).claim(
        run_id=RUN_ID,
        logical_page_key="url:1",
        expected_row_version=0,
        worker_id="worker",
        now=NOW,
        lease_seconds=60,
    )
    assert result.claimed_at == NOW + timedelta(seconds=61)
    assert result.lease_expires_at == NOW + timedelta(seconds=121)


@pytest.mark.asyncio
async def test_settlement_cas_rechecks_expiry_after_clock_read():
    cursor = WebStageCursor.start(run_id=RUN_ID, logical_page_key="url:1").claim(
        worker_id="worker", now=NOW, lease_seconds=60
    )
    connection = ClockedCheckpointConnection(
        cursor, locked_at=NOW + timedelta(seconds=59), persisted_at=NOW + timedelta(seconds=61)
    )
    with pytest.raises(WebStageCheckpointConflict):
        await PostgresWebRunStageCheckpointRepository(pool=connection).mark_succeeded(
            cursor, now=NOW
        )
    assert connection.row["stage_status"] == "running"
