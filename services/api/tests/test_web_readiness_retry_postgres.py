"""真实数据库验证就绪换帧引用和重试结算原子持久化。"""

import pytest

from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
    WebStageCheckpointConflict,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import _database
from services.api.tests.test_web_run_stage_checkpoint_postgres import (
    NOW,
    _upgrade_isolated_postgres,
    pg,
    seed_current,
)

__all__ = ["_upgrade_isolated_postgres"]


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
async def test_readiness_retry_reference_survives_restart_and_stale_writer():
    async with _database() as (control, first_pool, second_pool, ids):
        await seed_current(control, ids)
        repo = PostgresWebRunStageCheckpointRepository(pool=first_pool)
        restarted = PostgresWebRunStageCheckpointRepository(pool=second_pool)
        original = {
            "session_id": ids["session"], "finalization_epoch": 0,
            "capture_id": ids["capture"], "parent_capture_id": ids["capture"],
            "current_url": "https://example.test/",
        }
        await repo.create(WebStageCursor.start(
            run_id=ids["run"], logical_page_key="home", stage=WebRunStage.READINESS,
            cursor_payload=original,
        ))
        claimed = await repo.claim(
            run_id=ids["run"], logical_page_key="home", expected_row_version=0,
            worker_id="first", now=NOW,
        )
        reference = {
            "capture_id": "00000000-0000-0000-0000-000000000091",
            "current_url": "https://example.test/ready",
        }
        retry = await repo.schedule_retry(
            claimed, error_code="WEB_READINESS_LOADING", detail=reference, now=NOW,
        )
        persisted = (await restarted.list_for_run(run_id=ids["run"]))[0]
        assert persisted == retry
        assert persisted.cursor_payload == {**original, **reference}
        assert persisted.last_error_detail == reference
        with pytest.raises(WebStageCheckpointConflict):
            await restarted.schedule_retry(
                claimed, error_code="WEB_READINESS_LOADING", now=NOW,
                detail={**reference, "current_url": "https://example.test/stale"},
            )
        await control.execute(
            "UPDATE qc_web_run_stage_checkpoint SET next_attempt_at=clock_timestamp() "
            "- interval '1 second' WHERE run_id=$1::uuid", ids["run"],
        )
        next_claim = await restarted.claim(
            run_id=ids["run"], logical_page_key="home", expected_row_version=retry.row_version,
            worker_id="second", now=NOW,
        )
        assert next_claim.cursor_payload["capture_id"] == reference["capture_id"]
        final_reference = {**reference, "capture_id": "00000000-0000-0000-0000-000000000092"}
        settled = await restarted.schedule_retry(
            next_claim, error_code="WEB_READINESS_LOADING", detail=final_reference, now=NOW,
        )
        persisted = (await repo.list_for_run(run_id=ids["run"]))[0]
        assert persisted == settled
        assert persisted.status.value == "gapped"
        assert persisted.cursor_payload == {**original, **final_reference}
        assert persisted.last_error_detail == final_reference
