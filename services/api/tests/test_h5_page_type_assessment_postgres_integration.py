"""H5 页面评估在真实 PostgreSQL 中的单调、并发与幂等验收。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from services.api.app.infrastructure.db.h5_page_type_assessment_repository import (
    H5PageTypeAssessmentRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)

_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _ROOT / "services" / "api" / "migrations" / "alembic.ini"
_DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
_HISTORY_PRINCIPAL_ID = "00000000-0000-0000-0000-000000000002"


@pytest.fixture(scope="session", autouse=True)
def _upgrade_isolated_postgres() -> None:
    database_url = _test_database_url()
    previous = os.environ.get("ALIOTH_DATABASE_URL")
    os.environ["ALIOTH_DATABASE_URL"] = database_url
    try:
        command.upgrade(AlembicConfig(str(_ALEMBIC_INI)), "head")
    finally:
        if previous is None:
            os.environ.pop("ALIOTH_DATABASE_URL", None)
        else:
            os.environ["ALIOTH_DATABASE_URL"] = previous


@pytest.mark.asyncio
async def test_postgres_cross_schema_monotonicity_and_concurrent_replay() -> None:
    """真实 PG 同时保留 schema 历史，并仅投影当前最强有效结论。"""
    database_url = _test_database_url()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    ids = {name: str(uuid4()) for name in ("ruleset", "task", "run", "capture")}
    ids["session"] = f"h5:{ids['run']}"
    try:
        await _seed_h5_context(connection, ids)
        repository = H5PageTypeAssessmentRepository(pool=pool)
        first = await _upsert(repository, ids, schema_version="h5-page-assessment.v1")
        weak_v2 = await _upsert(
            repository,
            ids,
            schema_version="h5-page-assessment.v2",
            status="evidence_insufficient",
        )

        [stored] = await repository.list_for_run(task_id=ids["task"], agent_run_id=ids["run"])
        assert stored.status == "covered"
        assert stored.schema_version == "h5-page-assessment.v1"

        strengthened = await _upsert(
            repository,
            ids,
            schema_version="h5-page-assessment.v2",
            status="covered",
            expected_row_version=weak_v2.row_version,
        )
        replayed = await asyncio.gather(
            _upsert(repository, ids, schema_version="h5-page-assessment.v2"),
            _upsert(repository, ids, schema_version="h5-page-assessment.v2"),
        )

        [stored] = await repository.list_for_run(task_id=ids["task"], agent_run_id=ids["run"])
        assert stored.status == "covered"
        assert stored.schema_version == "h5-page-assessment.v2"
        assert stored.row_version == strengthened.row_version == 1
        assert all(item.row_version == 1 for item in replayed)
        rows = await connection.fetch(
            """
            SELECT schema_version, status, row_version
            FROM qc_h5_page_type_assessment
            WHERE page_capture_id = $1::uuid
            ORDER BY schema_version
            """,
            ids["capture"],
        )
        assert [row["schema_version"] for row in rows] == [
            "h5-page-assessment.v1",
            "h5-page-assessment.v2",
        ]
        assert first.assessment_record_id != strengthened.assessment_record_id
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await pool.close()
        await connection.close()


async def _upsert(
    repository: H5PageTypeAssessmentRepository,
    ids: dict[str, str],
    *,
    schema_version: str,
    status: str = "covered",
    expected_row_version: int | None = None,
):
    reason_code = (
        "h5_page_detection_covered" if status == "covered" else "h5_page_detection_low_confidence"
    )
    return await repository.upsert(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        session_id=ids["session"],
        page_capture_id=ids["capture"],
        detection_result={"detection_status": "recognized", "page_state": "stable"},
        assessment_payload={
            "protocol_version": "h5-page-type-assessment-adapter.v1",
            "execution_status": "accepted",
            "retryable": False,
            "page_type_assessment": {
                "schema_version": schema_version,
                "page_type": "home",
                "status": status,
                "reason_code": reason_code,
                "page_capture_ids": [ids["capture"]],
                "evidence_artifact_ids": ["artifact-home"],
                "exploration_action_ids": [],
                "page_states": ["stable"],
            },
        },
        expected_row_version=expected_row_version,
    )


async def _seed_h5_context(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        ) VALUES ($1::uuid, $2, 'H5 Assessment PG', 1, 'enabled', 'h5-test', 0, 'manual')
        """,
        ids["ruleset"],
        f"h5-assessment-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, target_platform, tenant_id, submitter_id
        ) VALUES (
            $1::uuid, 'https://example.test', $2::uuid, 'running', 'h5',
            $3::uuid, $4::uuid
        )
        """,
        ids["task"],
        ids["ruleset"],
        _DEFAULT_TENANT_ID,
        _HISTORY_PRINCIPAL_ID,
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
        VALUES ($1::uuid, $2::uuid, 'main', 'H5 Assessment PG', 'running')
        """,
        ids["run"],
        ids["task"],
    )
    await connection.execute(
        """
        INSERT INTO qc_surface_session (
            session_id, task_id, surface_kind, session_owner, status,
            finalization_epoch, activated_at
        ) VALUES ($1, $2::uuid, 'web', 'h5-assessment-pg', 'active', 0, now())
        """,
        ids["session"],
        ids["task"],
    )
    await connection.execute(
        """
        INSERT INTO qc_page_capture (
            id, task_id, agent_run_id, page_seq, page_slug, url, capture_status,
            surface_session_id, capture_role, session_command_sequence
        ) VALUES (
            $1::uuid, $2::uuid, $3::uuid, 1, 'home', 'https://example.test', 'captured',
            $4, 'after_action', 1
        )
        """,
        ids["capture"],
        ids["task"],
        ids["run"],
        ids["session"],
    )


def _test_database_url() -> str:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    return require_isolated_test_database_url(
        database_url,
        os.getenv("ALIOTH_DATABASE_URL"),
    )
