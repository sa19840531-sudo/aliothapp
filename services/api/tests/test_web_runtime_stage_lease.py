"""长阶段续租保持同一授权身份；过期、取消和接管必须拒绝旧 Worker。"""

import asyncio
from datetime import timedelta

import pytest

from services.api.app.application.web_run_stage_orchestrator import (
    WebRunStageOrchestrator,
    WebStageEffectResult,
)
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebStageCursor
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
    WebStageCheckpointConflict,
)
from services.api.tests.test_web_run_stage_checkpoint import NOW, RUN_ID
from services.api.tests.test_web_run_stage_checkpoint_repository import ClockedCheckpointConnection


async def test_renewal_cas_extends_only_same_live_claim_without_changing_stage_identity():
    claimed = WebStageCursor.start(run_id=RUN_ID, logical_page_key="long").claim(
        worker_id="worker", now=NOW
    )
    connection = ClockedCheckpointConnection(claimed, locked_at=NOW + timedelta(seconds=50))
    repo = PostgresWebRunStageCheckpointRepository(pool=connection)
    renewed = await repo.renew_lease(claimed)
    assert renewed.lease_expires_at == NOW + timedelta(seconds=650)
    assert renewed.row_version == claimed.row_version
    assert renewed.claim_token == claimed.claim_token
    assert renewed.idempotency_key == claimed.idempotency_key
    connection.locked_at = connection.persisted_at = NOW + timedelta(seconds=90)
    result = await repo.mark_succeeded(claimed)
    assert result.status.value == "succeeded"


@pytest.mark.parametrize("invalid", ["expired", "replaced", "cancelled"])
async def test_renewal_rejects_expired_replaced_and_cancelled_claims(invalid):
    claimed = WebStageCursor.start(run_id=RUN_ID, logical_page_key="long").claim(
        worker_id="worker", now=NOW
    )
    connection = ClockedCheckpointConnection(
        claimed, locked_at=NOW + timedelta(seconds=61 if invalid == "expired" else 20)
    )
    if invalid == "replaced":
        connection.row["claim_token"] = "00000000-0000-0000-0000-000000000099"
    if invalid == "cancelled":
        original = connection.fetchrow

        async def fetchrow(sql, *args):
            return None if "SELECT task.finalization_epoch" in sql else await original(sql, *args)

        connection.fetchrow = fetchrow
    with pytest.raises(WebStageCheckpointConflict):
        await PostgresWebRunStageCheckpointRepository(pool=connection).renew_lease(claimed)


async def test_long_stage_over_sixty_seconds_finishes_under_production_repository_heartbeat():
    pending = WebStageCursor.start(run_id=RUN_ID, logical_page_key="long")
    connection = ClockedCheckpointConnection(pending, locked_at=NOW)
    progressed = asyncio.Event()

    class Repository(PostgresWebRunStageCheckpointRepository):
        renewals = 0

        async def renew_lease(self, cursor):
            self.renewals += 1
            connection.locked_at = connection.persisted_at = NOW + timedelta(
                seconds=20 * self.renewals
            )
            result = await super().renew_lease(cursor)
            if self.renewals == 4:
                progressed.set()
            return result

    repo = Repository(pool=connection)

    class Effects:
        async def find_completed(self, cursor):
            return None

        async def execute(self, cursor):
            await asyncio.wait_for(progressed.wait(), 1)
            return WebStageEffectResult()

    result = await WebRunStageOrchestrator(
        checkpoints=repo, effects=Effects(), worker_id="worker", heartbeat_interval_seconds=0.001
    ).advance_once(RUN_ID, "long")
    assert result.cursor.status.value == "succeeded"
    assert connection.locked_at >= NOW + timedelta(seconds=80)
    assert repo.renewals >= 4


async def test_cancelled_stage_cancels_and_joins_heartbeat_before_returning():
    pending = WebStageCursor.start(run_id=RUN_ID, logical_page_key="long")
    connection = ClockedCheckpointConnection(pending, locked_at=NOW)
    running, cancelled = asyncio.Event(), asyncio.Event()

    class Repository(PostgresWebRunStageCheckpointRepository):
        renewals = 0

        async def renew_lease(self, cursor):
            self.renewals += 1
            return await super().renew_lease(cursor)

    repo = Repository(pool=connection)

    class Effects:
        async def find_completed(self, cursor):
            return None

        async def execute(self, cursor):
            running.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    task = asyncio.create_task(
        WebRunStageOrchestrator(
            checkpoints=repo,
            effects=Effects(),
            worker_id="worker",
            heartbeat_interval_seconds=0.001,
        ).advance_once(RUN_ID, "long")
    )
    await asyncio.wait_for(running.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    count = repo.renewals
    await asyncio.sleep(0.01)
    assert repo.renewals == count
    assert cancelled.is_set()
    assert connection.row["stage_status"] == "running"


async def test_heartbeat_database_timeout_cancels_effect_without_scheduling_page_retry():
    pending = WebStageCursor.start(run_id=RUN_ID, logical_page_key="long")
    connection = ClockedCheckpointConnection(pending, locked_at=NOW)
    cancelled = asyncio.Event()

    class Repository(PostgresWebRunStageCheckpointRepository):
        async def renew_lease(self, cursor):
            raise TimeoutError("database timeout")

    class Effects:
        async def find_completed(self, cursor):
            return None

        async def execute(self, cursor):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    with pytest.raises(RuntimeError, match="WEB_STAGE_LEASE_LOST"):
        await WebRunStageOrchestrator(
            checkpoints=Repository(pool=connection),
            effects=Effects(),
            worker_id="worker",
            heartbeat_interval_seconds=0.001,
        ).advance_once(RUN_ID, "long")
    assert cancelled.is_set()
    assert connection.row["stage_status"] == "running"
