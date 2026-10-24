"""隔离 PostgreSQL 验证 Snapshot 逐请求前沿，绝不读写本轮运行库。"""

import json
from uuid import UUID, uuid4

import pytest

from services.api.app.infrastructure.db.h5_targeted_recapture_repository import (
    PostgresH5TargetedRecaptureRepository,
)
from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore
from services.api.tests.test_h5_llm_budget_postgres import database as database
from services.api.tests.test_h5_llm_budget_postgres import seed
from services.api.tests.test_h5_snapshot_checkpoint_coordination import snapshot_request_with


@pytest.mark.parametrize("fault", [
    None, "leased", "routed", "probe", "unpublished_probe", "published_probe",
    "platform", "epoch", "other_run", "closed_task", "terminal_request",
])
async def test_checkpoint_inventory_includes_each_owned_request_and_fences_old_scope(
    database, fault,
):
    task, run, _ = await seed(database)
    capture = uuid4()
    request_ids = []
    async with database.acquire() as connection:
        await connection.execute("""
            ALTER TABLE qc_task ADD finalization_state text DEFAULT 'open';
            CREATE TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid);
            CREATE TABLE qc_evidence_request(request_id text,task_id uuid,target_capture_id uuid,
                finalization_epoch int,session_dependency text,status text,lease_token uuid,
                request_payload jsonb);
            CREATE TABLE qc_v2_work_item(task_id uuid,finalization_epoch int,
                evidence_request_id text,work_type text,status text,work_id text);
            CREATE TABLE qc_evidence_supplement(probe_work_id text);
        """)
        await connection.execute(
            "INSERT INTO qc_capture VALUES($1,$2,$3)", capture, UUID(task), UUID(run),
        )
        for index, goal in enumerate(("连续滚动查看底部间距", "连续滚动与点击反馈")):
            request = snapshot_request_with(
                task_id=task,
                target_capture_id=str(capture), evidence_goal=goal,
            )
            request_ids.append(request.request_id)
            await connection.execute(
                "INSERT INTO qc_evidence_request VALUES($1,$2,$3,4,'snapshot',$4,$5,$6)",
                request.request_id, UUID(task), capture,
                "routed" if index == 1 and fault == "routed" else "pending",
                uuid4() if index == 1 and fault == "leased" else None,
                json.dumps(request.to_dict()),
            )
        if fault in {"probe", "unpublished_probe", "published_probe"}:
            await connection.execute(
                "INSERT INTO qc_v2_work_item VALUES($1,4,$2,'probe',$3,'probe-1')",
                UUID(task), request_ids[1], "running" if fault == "probe" else "succeeded",
            )
            if fault == "published_probe":
                await connection.execute("INSERT INTO qc_evidence_supplement VALUES('probe-1')")
        if fault == "platform":
            await connection.execute("UPDATE qc_task SET target_platform='web'")
        elif fault == "epoch":
            await connection.execute("UPDATE qc_task SET finalization_epoch=5")
        elif fault == "closed_task":
            await connection.execute("UPDATE qc_task SET finalization_state='sealed'")
        elif fault == "terminal_request":
            await connection.execute(
                "UPDATE qc_evidence_request SET status='satisfied' WHERE request_id=$1",
                request_ids[1],
            )

    repository = PostgresH5TargetedRecaptureRepository(pool=database)
    items = await repository.list_snapshot_checkpoint_requests(
        task_id=task, agent_run_id=str(uuid4()) if fault == "other_run" else run,
    )
    if fault in {"platform", "epoch", "closed_task", "other_run"}:
        assert items == ()
    elif fault == "terminal_request":
        assert len(items) == 1 and items[0].request.request_id == request_ids[0]
    else:
        assert {item.request.request_id for item in items} == set(request_ids)
        flags = {item.request.request_id: item.blocks_current_surface for item in items}
        assert flags[request_ids[0]] is False
        assert flags[request_ids[1]] is (
            fault in {"leased", "routed", "probe", "unpublished_probe"}
        )


@pytest.mark.parametrize("status,flag,platform,expected", [
    ("running", False, "h5", False), ("running", True, "h5", True),
    ("canceled", False, "h5", True), ("running", False, "web", None),
])
async def test_h5_durable_cancel_sql_reads_boolean_or_fails_closed(
    database, status, flag, platform, expected,
):
    task, run, _ = await seed(database)
    async with database.acquire() as connection:
        await connection.execute("ALTER TABLE qc_agent_run ADD output_context jsonb")
        await connection.execute(
            "UPDATE qc_agent_run SET status=$1,output_context=$2 WHERE id=$3", status,
            json.dumps({"cancel_requested": flag}), UUID(run),
        )
        await connection.execute("UPDATE qc_task SET target_platform=$1", platform)
    store = PostgresAgentRunStateStore(pool=database)
    if expected is None:
        with pytest.raises(RuntimeError, match="取消事实缺失"):
            await store.is_h5_cancel_requested(run)
    else:
        assert await store.is_h5_cancel_requested(run) is expected
