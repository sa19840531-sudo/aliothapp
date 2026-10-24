"""隔离库复现超时 Probe 空 planned 动作，拒绝清理仍有执行权的反例。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from services.api.app.infrastructure.db.h5_probe_terminal_repository import (
    PostgresH5ProbeTerminalSettlementRepository,
)
from services.api.tests.test_h5_llm_budget_postgres import database as database
from services.api.tests.test_h5_llm_budget_postgres import seed
from services.api.tests.test_h5_probe_terminal_repository import _Connection, _Pool


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "platform",
        "epoch",
        "live_session",
        "live_work",
        "not_due",
        "executing",
        "capture",
        "other_run",
    ],
)
async def test_timeout_orphan_settlement_is_fenced_and_does_not_fake_evidence(database, fault):
    task, run, _ = await seed(database)
    fake = _Connection()
    await PostgresH5ProbeTerminalSettlementRepository(pool=_Pool(fake)).settle_ready(
        task_id=task, agent_run_id=run
    )
    query = next(
        query for _, (query, *_) in fake.queries if query.startswith("UPDATE qc_action_execution")
    )
    async with database.acquire() as connection:
        await connection.execute("""
            ALTER TABLE qc_task ADD finalization_state text DEFAULT 'open';
            CREATE TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid,
                action_execution_id text);
            CREATE TABLE qc_surface_session(task_id uuid,session_id text,surface_kind text,
                finalization_epoch int,command_sequence int,lease_token uuid,
                lease_expires_at timestamptz);
            CREATE TABLE qc_v2_work_item(task_id uuid,work_id text,work_type text,
                work_payload jsonb,
                finalization_epoch int,session_id text,source_capture_id uuid,
                deadline timestamptz,
                status text,lease_token uuid,lease_expires_at timestamptz);
            CREATE TABLE qc_action_execution(action_execution_id text,task_id uuid,session_id text,
                finalization_epoch int,command_sequence int,intent_payload jsonb,status text,
                started_at timestamptz,source_capture_id uuid,result_capture_id uuid,
                error_code text,
                error_message text,action_finished_at timestamptz,evidence_finished_at timestamptz,
                row_version int DEFAULT 0,updated_at timestamptz);
        """)
        now = datetime.now(UTC)
        capture, session, action, work = uuid4(), f"h5:{run}", "orphan", "expired-probe"
        await connection.execute(
            "INSERT INTO qc_capture VALUES($1,$2,$3,NULL)", capture, UUID(task), UUID(run)
        )
        await connection.execute(
            "INSERT INTO qc_surface_session VALUES($1,$2,'web',4,1,$3,$4)",
            UUID(task),
            session,
            uuid4() if fault == "live_session" else None,
            now + timedelta(seconds=120) if fault == "live_session" else now,
        )
        await connection.execute(
            "INSERT INTO qc_v2_work_item VALUES($1,$2,'probe',$3,4,$4,$5,$6,'failed',$7,$8)",
            UUID(task),
            work,
            json.dumps({"surface_kind": "web"}),
            session,
            capture,
            now + timedelta(seconds=60) if fault == "not_due" else now - timedelta(seconds=60),
            uuid4() if fault == "live_work" else None,
            now + timedelta(seconds=120),
        )
        await connection.execute(
            """INSERT INTO qc_action_execution
            (action_execution_id,task_id,session_id,finalization_epoch,command_sequence,intent_payload,status)
            VALUES($1,$2,$3,$4,1,$5,$6)""",
            action,
            UUID(task),
            session,
            5 if fault == "epoch" else 4,
            json.dumps({"parameters": {"exploration_work_id": work}}),
            "executing" if fault == "executing" else "planned",
        )
        if fault == "platform":
            await connection.execute("UPDATE qc_task SET target_platform='app'")
        if fault == "capture":
            await connection.execute(
                "INSERT INTO qc_capture VALUES($1,$2,$3,$4)", uuid4(), UUID(task), UUID(run), action
            )
        await connection.execute(query, task, str(uuid4()) if fault == "other_run" else run)
        row = await connection.fetchrow("SELECT * FROM qc_action_execution")
        assert row["status"] == (
            "failed" if fault is None else "executing" if fault == "executing" else "planned"
        )
        assert row["row_version"] == int(fault is None)
        assert row["result_capture_id"] is None
        if fault is None:
            assert row["error_code"] == "H5_ACTION_BEFORE_CAPTURE_ABORTED"
