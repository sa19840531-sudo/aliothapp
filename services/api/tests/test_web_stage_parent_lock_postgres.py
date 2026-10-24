"""阶段父记录锁须兼容外键取证写入，同时保留取消与 epoch 隔离。"""

import asyncio

import pytest

from services.api.app.application.web_run_stage_orchestrator import WebStageEffectResult
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor
from services.api.app.infrastructure.db.web_exploration_orphan_repository import (
    PostgresWebExplorationOrphanRepository,
)
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
    WebStageCheckpointConflict,
)
from services.api.app.infrastructure.db.web_runtime_stage_evidence_repository import (
    PostgresWebRuntimeStageEvidenceRepository,
)
from services.api.tests import test_web_run_stage_checkpoint_postgres as helpers

pytestmark = [helpers.pg, pytest.mark.usefixtures("_upgrade_isolated_postgres")]


@pytest.fixture(scope="module")
def _upgrade_isolated_postgres():
    helpers._upgrade_isolated_postgres.__wrapped__()


async def _operation(control, pool, ids, operation):
    await helpers.seed_current(control, ids)
    session = f"web:{ids['run']}"
    await control.execute(
        "UPDATE qc_agent_run SET input_context=jsonb_set(input_context, "
        "'{task_config,execution_mode}', '\"exploration_only\"') WHERE id=$1::uuid",
        ids["run"],
    )
    await control.execute(
        "INSERT INTO qc_surface_session "
        "(session_id,task_id,surface_kind,session_owner,status,activated_at) "
        "VALUES ($1,$2::uuid,'web','web-v2-workflow','active',now())", session, ids["task"],
    )
    await control.execute(
        "UPDATE qc_capture SET surface_session_id=$1,metadata='{\"finalization_epoch\":0}' "
        "WHERE id=$2::uuid", session, ids["capture"],
    )
    repository = PostgresWebRunStageCheckpointRepository(pool=pool)
    await repository.create(WebStageCursor.start(
        run_id=ids["run"], logical_page_key="home", stage=WebRunStage.CLASSIFICATION,
        cursor_payload={"session_id": session, "finalization_epoch": 0,
                        "capture_id": ids["capture"]},
    ))
    cursor = await repository.claim(
        run_id=ids["run"], logical_page_key="home", expected_row_version=0, worker_id="worker",
    )
    if operation == "renew":
        return lambda: repository.renew_lease(cursor)
    if operation == "evidence":
        evidence = PostgresWebRuntimeStageEvidenceRepository(
            task_id=ids["task"], run_id=ids["run"], session_id=session, pool=pool,
        )
        return lambda: evidence.save_completed(cursor, WebStageEffectResult())
    await helpers.expire_lease(control, ids["run"])
    orphan = PostgresWebExplorationOrphanRepository(pool=pool)
    return lambda: orphan.settle(task_id=ids["task"], run_id=ids["run"])


@pytest.mark.parametrize("operation", ["renew", "evidence", "orphan"])
async def test_parent_identity_lock_does_not_block_evidence_foreign_keys(operation):
    async with helpers._database() as (control, pool, _, ids):
        execute = await _operation(control, pool, ids, operation)
        async with control.transaction():
            # 模拟转场 INSERT 的外键检查：事务持有 Task/Run 的 KEY SHARE。
            await control.fetchrow(
                "SELECT id FROM qc_task WHERE id=$1::uuid FOR KEY SHARE", ids["task"],
            )
            await control.fetchrow(
                "SELECT id FROM qc_agent_run WHERE id=$1::uuid FOR KEY SHARE", ids["run"],
            )
            result = await asyncio.wait_for(execute(), timeout=2)
        if operation == "renew":
            assert result.status.value == "running" and result.claim_token
        elif operation == "evidence":
            assert await control.fetchval(
                "SELECT count(*) FROM qc_surface_phase_run WHERE task_id=$1::uuid "
                "AND status='completed'", ids["task"],
            ) == 1
        else:
            assert result is True
            assert await control.fetchval(
                "SELECT status FROM qc_surface_session WHERE session_id=$1", f"web:{ids['run']}",
            ) == "lost"


@pytest.mark.parametrize("operation", ["renew", "evidence", "orphan"])
@pytest.mark.parametrize("mutation", ["cancel", "epoch"])
async def test_parent_identity_lock_still_fences_concurrent_cancellation_and_epoch(
    operation, mutation,
):
    async with helpers._database() as (control, pool, _, ids):
        execute = await _operation(control, pool, ids, operation)
        async with pool.acquire() as connection:
            pid = await connection.fetchval("SELECT pg_backend_pid()")
        async with control.transaction():
            if mutation == "cancel":
                await control.execute(
                    "UPDATE qc_agent_run SET output_context='{\"cancel_requested\":true}' "
                    "WHERE id=$1::uuid", ids["run"],
                )
            else:
                await control.execute(
                    "UPDATE qc_task SET finalization_epoch=finalization_epoch+1 WHERE id=$1::uuid",
                    ids["task"],
                )
            work = asyncio.create_task(execute())
            try:
                async with asyncio.timeout(2):
                    while not await control.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND NOT granted)", pid,
                    ):
                        if work.done():
                            raise AssertionError("父记录更新尚未提交，阶段操作不得提前返回")
                        await asyncio.sleep(0.01)
            except BaseException:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                raise
        if operation == "orphan":
            # orphan 读取当前 epoch 处理失联会话；取消必须拒绝收束。
            assert await work is (mutation == "epoch")
        else:
            with pytest.raises(WebStageCheckpointConflict):
                await work
