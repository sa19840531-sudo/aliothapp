"""失败序列夹具必须像生产持久化一样对齐两张采集表，不能被状态更新冲掉身份。"""

import json
import os
from uuid import uuid4

import pytest

from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _upgrade_isolated_postgres as _upgrade_isolated_postgres,
)
from services.api.tests.test_web_logical_target_postgres import target_database
from services.api.tests.web_sequence_pg_support import seed_failure_frames

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="需同时显式提供隔离测试库与非测试库 PostgreSQL DSN",
)


async def read_pairs(connection, frames):
    return await connection.fetch(
        "SELECT c.id::text,c.metadata::text AS common_metadata,p.metadata::text AS page_metadata,"
        "c.captured_at AS common_time,p.captured_at AS page_time,"
        "c.surface_session_id AS common_session,p.surface_session_id AS page_session,"
        "c.parent_capture_id AS common_parent,p.parent_page_capture_id AS page_parent,"
        "c.action_execution_id AS common_action,p.action_execution_id AS page_action,"
        "c.capture_role AS common_role,p.capture_role AS page_role,"
        "c.session_command_sequence AS common_sequence,p.session_command_sequence AS page_sequence "
        "FROM qc_capture c JOIN qc_page_capture p ON p.id=c.id WHERE c.id=ANY($1::uuid[]) "
        "ORDER BY c.id",
        list(frames),
    )


@pytest.mark.asyncio
async def test_failure_seed_has_exact_canonical_identity_before_any_inspection_update():
    async with target_database() as (connection, pool, _, ids):
        frames, _ = await seed_failure_frames(connection, pool, ids)
        rows = await read_pairs(connection, frames)
        assert len(rows) == 4
        for row in rows:
            for field in ("metadata", "time", "session", "parent", "action", "role", "sequence"):
                assert row[f"common_{field}"] == row[f"page_{field}"], (field, dict(row))


@pytest.mark.asyncio
async def test_canonical_source_precedes_frames_in_both_sequence_and_time():
    async with target_database() as (connection, pool, _, ids):
        frames, _ = await seed_failure_frames(connection, pool, ids)
        ordered_ids = [ids["capture"], *frames]
        rows = await connection.fetch(
            "SELECT id::text,page_seq,captured_at FROM qc_page_capture "
            "WHERE task_id=$1::uuid ORDER BY page_seq",
            ids["task"],
        )
        assert [row["id"] for row in rows] == ordered_ids
        assert [row["page_seq"] for row in rows] == [1, 2, 3, 4, 5]
        assert [row["captured_at"] for row in rows] == sorted(row["captured_at"] for row in rows)


@pytest.mark.asyncio
async def test_inspection_only_page_update_keeps_original_failure_frame_identity():
    async with target_database() as (connection, pool, _, ids):
        frames, _ = await seed_failure_frames(connection, pool, ids)
        before = [dict(row) for row in await read_pairs(connection, frames)]
        await connection.execute(
            "UPDATE qc_page_capture SET inspection_status='completed' WHERE id=ANY($1::uuid[])",
            list(frames),
        )
        after = [dict(row) for row in await read_pairs(connection, frames)]
        for first, last in zip(before, after, strict=True):
            for field in ("metadata", "time", "session", "parent", "action", "role", "sequence"):
                assert last[f"common_{field}"] == first[f"common_{field}"], (field, first, last)
            metadata = json.loads(last["common_metadata"])
            assert metadata["finalization_epoch"] == 0
            assert metadata["pipeline_metrics"]["web_readiness"]["visible_loader"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [None, "epoch", "command", "expired_lease"])
async def test_real_repository_preserves_identity_and_rejects_stale_write(drift):
    from services.api.app.infrastructure.db.page_capture_repository import (
        PostgresPageCaptureRepository,
    )

    async with target_database() as (connection, pool, _, ids):
        frames, _ = await seed_failure_frames(connection, pool, ids)
        await connection.execute(
            "UPDATE qc_surface_session SET command_sequence=3,lease_token=$2::uuid,"
            "lease_owner='sequence-lineage-test',lease_expires_at=now()+interval '1 minute' "
            "WHERE session_id=$1",
            ids["session"],
            str(uuid4()),
        )
        source = await connection.fetchrow(
            "SELECT metadata::text,action_execution_id FROM qc_page_capture WHERE id=$1::uuid",
            frames[-1],
        )
        metadata = json.loads(source["metadata"])
        capture_id = str(uuid4())
        action_id = "sequence-write-" + str(uuid4())
        await connection.execute(
            "INSERT INTO qc_action_execution(action_execution_id,task_id,session_id,intent_id,"
            "idempotency_key,intent_hash,intent_protocol_version,intent_payload,command_sequence,"
            "finalization_epoch,requested_source_capture_id,source_capture_id,status,started_at) "
            "VALUES($1,$2::uuid,$3,$1,$1,repeat('a',64),'test/v1','{}'::jsonb,3,0,$4::uuid,"
            "$4::uuid,'executing',now())",
            action_id,
            ids["task"],
            ids["session"],
            frames[-1],
        )
        if drift == "expired_lease":
            await connection.execute(
                "UPDATE qc_surface_session SET lease_expires_at=now()-interval '1 second' "
                "WHERE session_id=$1",
                ids["session"],
            )
        repository = PostgresPageCaptureRepository(pool=pool)
        kwargs = dict(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            page_capture_id=capture_id,
            url="https://example.test/list",
            mode="diagnostic",
            reachable=True,
            title="加载未完成",
            status_code=200,
            action_log=[],
            artifacts=[],
            metrics=metadata["pipeline_metrics"],
            presentation=metadata["presentation"],
            parent_page_capture_id=frames[-1],
            capture_role="after_action",
            surface_session_id=ids["session"],
            action_execution_id=action_id,
            session_command_sequence=4 if drift == "command" else 3,
            finalization_epoch=1 if drift == "epoch" else 0,
        )
        # 此处只测仓储身份投影，不把无附件的隔离采集冒充合格输入。
        if drift is not None:
            with pytest.raises(RuntimeError, match="Page Capture 的父证据"):
                await repository.save_capture(**kwargs)
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_capture WHERE id=$1::uuid",
                    capture_id,
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_page_capture WHERE id=$1::uuid",
                    capture_id,
                )
                == 0
            )
            return
        await repository.save_capture(**kwargs)
        before = dict((await read_pairs(connection, [capture_id]))[0])
        await connection.execute(
            "UPDATE qc_page_capture SET inspection_status='completed' WHERE id=$1::uuid",
            capture_id,
        )
        after = dict((await read_pairs(connection, [capture_id]))[0])
        assert after == before
        for field in ("metadata", "time", "session", "parent", "action", "role", "sequence"):
            assert after[f"common_{field}"] == after[f"page_{field}"]
        persisted = json.loads(after["common_metadata"])
        assert persisted["finalization_epoch"] == 0
        assert persisted["web_evidence_admission"]["can_project_business_node"] is False
