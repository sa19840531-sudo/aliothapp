"""复现 20261011-130130 YC003：动作已 timed_out，恢复不得拒绝或重放旧动作。"""

import os
from uuid import uuid4

import pytest

from services.api.app.domain.interaction import ActionExecutionStatus
from services.api.app.infrastructure.db.action_execution_repository import (
    ActionExecutionPersistenceError,
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_h5_rule_inventory_postgres import seeded

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="只允许显式隔离数据库"
)


@pytest.mark.parametrize(
    "fault",
    [None, "completed", "error", "run", "owner", "epoch", "platform", "active", "after_capture"],
)
async def test_persisted_action_timeout_can_resume_only_its_lost_h5_session(fault):
    require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"], os.getenv("ALIOTH_DATABASE_URL")
    )
    async with seeded() as (connection, pool, ids, _):
        session = f"h5:{ids['run']}"
        action = f"action-execution:{uuid4().hex}"
        await connection.execute(
            """INSERT INTO qc_surface_session
            (session_id,task_id,surface_kind,session_owner,status,finalization_epoch,
             activated_at,lost_at,failure_reason)
            VALUES($1,$2::uuid,'web',$3,$4::text,$5,now(),
                   CASE WHEN $4::text='lost' THEN now() END,'browser_session_lost')""",
            session,
            ids["task"],
            "other-owner" if fault == "owner" else "h5-workflow",
            "active" if fault == "active" else "lost",
            0,
        )
        await connection.execute(
            "UPDATE qc_capture SET surface_session_id=$1 WHERE id=$2::uuid",
            session,
            ids["original"],
        )
        await connection.execute(
            """INSERT INTO qc_action_execution
            (action_execution_id,task_id,session_id,intent_id,idempotency_key,intent_hash,
             intent_protocol_version,intent_payload,command_sequence,finalization_epoch,
             source_capture_id,status,error_code,started_at,action_finished_at,
             result_capture_id,evidence_finished_at)
            VALUES($1,$2::uuid,$3,$1,$1,'hash','test-v1','{}',1,0,$4::uuid,$5::text,$6,now(),now(),
                   CASE WHEN $5::text='completed' THEN $4::uuid END,
                   CASE WHEN $5::text='completed' THEN now() END)""",
            action,
            ids["task"],
            session,
            ids["original"],
            "completed" if fault == "completed" else "timed_out",
            None
            if fault == "completed"
            else "unrelated_timeout"
            if fault == "error"
            else "ACTION_TIMED_OUT",
        )
        if fault == "epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1::uuid", ids["task"]
            )
        if fault == "platform":
            await connection.execute(
                "UPDATE qc_task SET target_platform='web' WHERE id=$1::uuid", ids["task"]
            )
        before = dict(
            await connection.fetchrow(
                "SELECT * FROM qc_action_execution WHERE action_execution_id=$1", action
            )
        )
        repository = PostgresActionExecutionRepository(pool=pool, page_graph_repository=None)
        kwargs = dict(
            task_id=ids["task"],
            agent_run_id=str(uuid4()) if fault == "run" else ids["run"],
            session_id=session,
            action_execution_id=action,
            source_capture_id=ids["original"],
            failure_stage="after_capture" if fault == "after_capture" else "action",
        )
        if fault is not None:
            with pytest.raises(ActionExecutionPersistenceError):
                await repository.fail_h5_browser_loss_unknown(**kwargs)
        else:
            for _ in range(2):
                result = await repository.fail_h5_browser_loss_unknown(**kwargs)
                assert result.status is ActionExecutionStatus.TIMED_OUT
                assert result.result_capture_id is None
                assert result.error_code == "ACTION_TIMED_OUT"
        after = dict(
            await connection.fetchrow(
                "SELECT * FROM qc_action_execution WHERE action_execution_id=$1", action
            )
        )
        assert after == before
