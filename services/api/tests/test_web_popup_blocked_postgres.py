"""仅在连接私有临时表验证 Web 阻断写入，禁止访问业务表。"""

import json
import os

import pytest

from services.api.app.infrastructure.db.web_popup_blocked_repository import (
    PostgresWebPopupBlockedRepository,
    WebPopupBlockedPersistenceError,
)
from services.api.tests.test_web_capture_evidence_repository import Pool

_TASK = "00000000-0000-0000-0000-000000000001"
_RUN = "00000000-0000-0000-0000-000000000002"
_CAPTURE = "00000000-0000-0000-0000-000000000003"
_SESSION = f"web:{_RUN}"

_SCHEMA = """
CREATE TEMP TABLE qc_task (
    id uuid, target_platform text, task_config jsonb, execution_status text,
    finalization_state text, finalization_epoch integer
) ON COMMIT DROP;
CREATE TEMP TABLE qc_agent_run (
    id uuid, task_id uuid, agent_type text, created_at timestamptz
) ON COMMIT DROP;
CREATE TEMP TABLE qc_surface_session (
    task_id uuid, session_id text, surface_kind text, session_owner text,
    status text, finalization_epoch integer, current_capture_id uuid
) ON COMMIT DROP;
CREATE TEMP TABLE qc_capture (
    id uuid, task_id uuid, agent_run_id uuid, surface_kind text,
    surface_session_id text, capture_status text, metadata jsonb
) ON COMMIT DROP;
CREATE TEMP TABLE qc_page_capture (
    id uuid, task_id uuid, agent_run_id uuid, surface_session_id text,
    capture_status text, metadata jsonb, page_type text, page_state text, url text
) ON COMMIT DROP;
CREATE TEMP TABLE qc_h5_page_type_assessment (
    page_capture_id uuid, task_id uuid
) ON COMMIT DROP;
"""

_RESET = f"""
TRUNCATE pg_temp.qc_task, pg_temp.qc_agent_run, pg_temp.qc_surface_session,
    pg_temp.qc_capture, pg_temp.qc_page_capture, pg_temp.qc_h5_page_type_assessment;
INSERT INTO pg_temp.qc_task VALUES (
    '{_TASK}', 'web', '{{"workflow_version":"v2"}}', 'running', 'open', 2
);
INSERT INTO pg_temp.qc_agent_run VALUES ('{_RUN}', '{_TASK}', 'main', now());
INSERT INTO pg_temp.qc_surface_session VALUES (
    '{_TASK}', '{_SESSION}', 'web', 'web-v2-workflow', 'active', 2, '{_CAPTURE}'
);
INSERT INTO pg_temp.qc_capture VALUES (
    '{_CAPTURE}', '{_TASK}', '{_RUN}', 'web', '{_SESSION}', 'captured',
    '{{"finalization_epoch":2,"untouched":true}}'
);
INSERT INTO pg_temp.qc_page_capture VALUES (
    '{_CAPTURE}', '{_TASK}', '{_RUN}', '{_SESSION}', 'captured',
    '{{"finalization_epoch":2,"untouched":true}}', NULL, NULL, 'https://example.test/'
);
"""


@pytest.mark.skipif(
    not os.getenv("ALIOTH_WEB_POPUP_TEST_DATABASE_URL"),
    reason="需显式提供临时表验证数据库地址",
)
@pytest.mark.asyncio
async def test_popup_sql_write_idempotence_and_fences_in_private_temp_tables():
    import asyncpg

    connection = await asyncpg.connect(
        os.environ["ALIOTH_WEB_POPUP_TEST_DATABASE_URL"], timeout=10,
        server_settings={"search_path": "pg_temp", "statement_timeout": "15000"},
    )
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute(_SCHEMA)
        repository = PostgresWebPopupBlockedRepository(pool=Pool(connection))

        async def record():
            return await repository.record(
                task_id=_TASK, agent_run_id=_RUN, session_id=_SESSION, capture_id=_CAPTURE,
                blocking_reasons=("popup_blocked",),
            )

        await connection.execute(_RESET)
        assert await record() is True
        assert await record() is True
        page = await connection.fetchrow("SELECT * FROM pg_temp.qc_page_capture")
        common = await connection.fetchrow("SELECT * FROM pg_temp.qc_capture")
        assert page["page_type"] == "popup_ad"
        assert page["page_state"] == "popup_blocked"
        assert json.loads(page["metadata"]) == json.loads(common["metadata"])
        assert json.loads(page["metadata"])["untouched"] is True
        assert await connection.fetchval(
            "SELECT count(*) FROM pg_temp.qc_h5_page_type_assessment"
        ) == 0

        for page_type in ("age_gate", "home", "unknown"):
            await connection.execute(_RESET)
            await connection.execute(
                "UPDATE pg_temp.qc_page_capture SET page_type=$1", page_type,
            )
            assert await record() is False
            assert await connection.fetchval(
                "SELECT page_type FROM pg_temp.qc_page_capture"
            ) == page_type
        await connection.execute(_RESET)
        await connection.execute(
            "INSERT INTO pg_temp.qc_h5_page_type_assessment VALUES ($1,$2)", _CAPTURE, _TASK,
        )
        assert await record() is False

        invalid_cases = (
            "UPDATE pg_temp.qc_task SET target_platform='h5'",
            "UPDATE pg_temp.qc_task SET target_platform='app'",
            "UPDATE pg_temp.qc_task SET execution_status='canceled'",
            "UPDATE pg_temp.qc_task SET finalization_state='sealed'",
            "UPDATE pg_temp.qc_task SET finalization_epoch=3",
            "UPDATE pg_temp.qc_surface_session SET status='closed'",
            "UPDATE pg_temp.qc_surface_session SET current_capture_id=NULL",
            "UPDATE pg_temp.qc_surface_session SET session_owner='foreign'",
            "UPDATE pg_temp.qc_capture SET agent_run_id=NULL",
            "UPDATE pg_temp.qc_page_capture SET task_id=NULL",
            "UPDATE pg_temp.qc_page_capture SET metadata='{}'",
            "UPDATE pg_temp.qc_page_capture SET url='about:blank'",
            "INSERT INTO pg_temp.qc_agent_run SELECT "
            "'00000000-0000-0000-0000-000000000004', task_id, agent_type, "
            "created_at + interval '1 second' FROM pg_temp.qc_agent_run",
        )
        for mutation in invalid_cases:
            await connection.execute(_RESET)
            await connection.execute(mutation)
            with pytest.raises(WebPopupBlockedPersistenceError):
                await record()
            assert await connection.fetchval(
                "SELECT page_type FROM pg_temp.qc_page_capture"
            ) is None
            assert "web_popup_blocked" not in json.loads(await connection.fetchval(
                "SELECT metadata FROM pg_temp.qc_capture"
            ))
    finally:
        await transaction.rollback()
        await connection.close()
