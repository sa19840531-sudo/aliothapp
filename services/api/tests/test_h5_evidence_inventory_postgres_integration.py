"""H5 证据库存的真实 asyncpg JSONB 往返测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

from services.api.app.application.h5_evidence_inventory import (
    H5EvidenceInventoryItem,
    H5EvidenceInventoryStatus,
)
from services.api.app.infrastructure.db.h5_evidence_inventory_repository import (
    PostgresH5EvidenceInventoryRepository,
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
async def test_inventory_upsert_round_trips_json_text_for_all_statuses() -> None:
    """默认 asyncpg codec 下三种状态都能以 JSON 文本写入并返回快照。"""
    database_url = _test_database_url()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    ids = {name: str(uuid4()) for name in ("ruleset", "task", "run", "capture")}
    ids["session"] = f"h5:{ids['run']}"
    statuses = (
        H5EvidenceInventoryStatus.PASS,
        H5EvidenceInventoryStatus.INSUFFICIENT,
        H5EvidenceInventoryStatus.FAILED,
    )
    action_ids = [f"h5-evidence-{uuid4()}" for _ in statuses]
    try:
        await _seed_context(connection, ids, action_ids)
        repository = PostgresH5EvidenceInventoryRepository(pool=pool)

        snapshots = [
            await repository.upsert_inventory_item(
                item=_item(ids, action_id, status)
            )
            for action_id, status in zip(action_ids, statuses, strict=True)
        ]
        rows = await connection.fetch(
            """
            SELECT action_execution_id, status, verification_summary
            FROM qc_h5_evidence_inventory_item
            WHERE task_id = $1::uuid
            ORDER BY action_execution_id
            """,
            ids["task"],
        )

        assert [snapshot.status for snapshot in snapshots] == list(statuses)
        assert {str(row["status"]) for row in rows} == {status.value for status in statuses}
        assert all(
            json.loads(row["verification_summary"])["wire_format"] == "json-text"
            for row in rows
        )
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await pool.close()
        await connection.close()


def _item(
    ids: dict[str, str], action_execution_id: str, status: H5EvidenceInventoryStatus
) -> H5EvidenceInventoryItem:
    return H5EvidenceInventoryItem(
        action_execution_id=action_execution_id,
        before_capture_id=None,
        after_capture_id=None,
        after_capture=None,
        status=status,
        reason_codes=(f"status:{status.value}",),
        verification_summary={"wire_format": "json-text", "status": status.value},
        task_id=ids["task"],
        agent_run_id=ids["run"],
        session_id=ids["session"],
        page_type="home",
        page_state="stable",
        required_artifacts=("before:dom", "after:dom"),
        found_artifacts=(),
    )


async def _seed_context(
    connection: asyncpg.Connection, ids: dict[str, str], action_ids: list[str]
) -> None:
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        ) VALUES ($1::uuid, $2, 'H5 Inventory PG', 1, 'enabled', 'h5-test', 0, 'manual')
        """,
        ids["ruleset"],
        f"h5-inventory-{ids['ruleset']}",
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
        VALUES ($1::uuid, $2::uuid, 'main', 'H5 Inventory PG', 'running')
        """,
        ids["run"],
        ids["task"],
    )
    await connection.execute(
        """
        INSERT INTO qc_surface_session (
            session_id, task_id, surface_kind, session_owner, status,
            finalization_epoch, activated_at
        ) VALUES ($1, $2::uuid, 'web', 'h5-inventory-pg', 'active', 0, now())
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
    for sequence, action_id in enumerate(action_ids, start=1):
        await connection.execute(
            """
            INSERT INTO qc_action_execution (
                action_execution_id, task_id, session_id, intent_id, idempotency_key,
                intent_hash, intent_protocol_version, intent_payload, command_sequence,
                finalization_epoch, requested_source_capture_id
            ) VALUES (
                $1, $2::uuid, $3, $4, $5, 'h5-intent-hash', 'h5/v1', '{}'::jsonb, $6,
                0, $7::uuid
            )
            """,
            action_id,
            ids["task"],
            ids["session"],
            f"intent-{sequence}",
            f"idempotency-{sequence}",
            sequence,
            ids["capture"],
        )


def _test_database_url() -> str:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    return require_isolated_test_database_url(
        database_url,
        os.getenv("ALIOTH_DATABASE_URL"),
    )
