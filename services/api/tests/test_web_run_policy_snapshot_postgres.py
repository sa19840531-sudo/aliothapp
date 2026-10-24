"""完整冻结正文的真实 JSONB 往返与双连接竞争。"""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import _database
from services.api.tests.test_web_run_policy_snapshot import policy_payload

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="需同时显式提供隔离测试库与非测试库 PostgreSQL DSN",
)


@pytest.fixture(scope="module", autouse=True)
def policy_database_schema():
    dsn = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"], os.environ["ALIOTH_DATABASE_URL"]
    )

    async def check_identity():
        connection = await asyncpg.connect(dsn)
        try:
            row = await connection.fetchrow("SELECT current_database(), current_user")
            assert tuple(row) == ("alioth_test_web_v2", "alioth_web_gap")
        finally:
            await connection.close()

    asyncio.run(check_identity())
    old = os.environ["ALIOTH_DATABASE_URL"]
    config = Config(str(Path(__file__).resolve().parents[1] / "migrations/alembic.ini"))
    try:
        os.environ["ALIOTH_DATABASE_URL"] = dsn
        command.upgrade(config, "head")
    finally:
        os.environ["ALIOTH_DATABASE_URL"] = old


@pytest.mark.asyncio
async def test_real_jsonb_freeze_race_read_only_recovery_and_task_ownership():
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
        PostgresWebRunPolicySnapshotRepository,
    )

    async with _database() as (connection, first_pool, second_pool, ids):
        # 既有预算测试种入了 Capture，本项先清除该测试种子以模拟第一动作之前。
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=NULL WHERE task_id=$1::uuid",
            ids["task"],
        )
        await connection.execute("DELETE FROM qc_capture WHERE task_id=$1::uuid", ids["task"])
        first = PostgresWebRunPolicySnapshotRepository(pool=first_pool)
        second = PostgresWebRunPolicySnapshotRepository(pool=second_pool)
        async with first_pool.acquire() as first_connection:
            first_pid = await first_connection.fetchval("SELECT pg_backend_pid()")
        async with second_pool.acquire() as second_connection:
            second_pid = await second_connection.fetchval("SELECT pg_backend_pid()")
        assert first_pid != second_pid
        body = policy_payload()
        changed = policy_payload()
        changed["page_catalog"]["page_types"][0]["purpose"] += " 新发布"
        candidates = [
            WebRunPolicySnapshot.create(task_id=ids["task"], agent_run_id=ids["run"], payload=p)
            for p in (body, changed)
        ]
        results = await asyncio.gather(
            first.freeze_or_read(candidates[0]), second.freeze_or_read(candidates[1])
        )
        assert results[0] == results[1]
        assert results[0].payload_sha256 in {c.payload_sha256 for c in candidates}
        row = await connection.fetchrow(
            "SELECT payload, payload_sha256 FROM qc_web_run_policy_snapshot "
            "WHERE agent_run_id=$1::uuid",
            ids["run"],
        )
        assert isinstance(row["payload"], str)
        assert row["payload_sha256"] == results[0].payload_sha256
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_web_run_policy_snapshot WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            == 1
        )
        await connection.execute(
            'UPDATE qc_agent_run SET working_memory=\'{"guide_version":"fake-v2"}\'::jsonb '
            "WHERE id=$1::uuid",
            ids["run"],
        )
        await connection.execute(
            "UPDATE qc_surface_session SET status='closed', draining_at=now(), closed_at=now() "
            "WHERE session_id=$1",
            ids["session"],
        )
        recovered = await second.read(task_id=ids["task"], agent_run_id=ids["run"])
        assert recovered == results[0]
        with pytest.raises(ValueError, match="OWNERSHIP"):
            await first.read(task_id=str(uuid4()), agent_run_id=ids["run"])
        with pytest.raises(Exception, match="immutable"):
            await connection.execute(
                "UPDATE qc_web_run_policy_snapshot SET payload_sha256=repeat('0',64) "
                "WHERE agent_run_id=$1::uuid",
                ids["run"],
            )


@pytest.mark.asyncio
async def test_old_run_history_cannot_be_backfilled_from_current_publication():
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
        PostgresWebRunPolicySnapshotRepository,
    )

    async with _database() as (connection, pool, _other, ids):
        repository = PostgresWebRunPolicySnapshotRepository(pool=pool)
        candidate = WebRunPolicySnapshot.create(
            task_id=ids["task"], agent_run_id=ids["run"], payload=policy_payload()
        )
        with pytest.raises(ValueError, match="HISTORY_MISSING"):
            await repository.freeze_or_read(candidate)
        with pytest.raises(ValueError, match="HISTORY_MISSING"):
            await repository.read(task_id=ids["task"], agent_run_id=ids["run"])
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_web_run_policy_snapshot WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["digest", "version", "body"])
async def test_pg_read_revalidates_digest_and_actual_run_owner(corruption):
    from hashlib import sha256

    from services.api.app.domain.exploration.web_run_policy_snapshot import canonical_policy_json
    from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
        PostgresWebRunPolicySnapshotRepository,
    )

    async with _database() as (connection, pool, _other, ids):
        body = policy_payload()
        if corruption == "version":
            body["identity_policy_version"] = "future.v999"
        elif corruption == "body":
            del body["readiness_profile"]["minimum_samples"]
        canonical = canonical_policy_json(body)
        digest = (
            "0" * 64 if corruption == "digest" else sha256(canonical.encode("utf-8")).hexdigest()
        )
        await connection.execute(
            "INSERT INTO qc_web_run_policy_snapshot(task_id,agent_run_id,payload,payload_sha256) "
            "VALUES($1::uuid,$2::uuid,$3::jsonb,$4)",
            ids["task"],
            ids["run"],
            canonical,
            digest,
        )
        error = {"digest": "SHA256", "version": "VERSION", "body": "BODY_INVALID"}[corruption]
        with pytest.raises(ValueError, match=error):
            await PostgresWebRunPolicySnapshotRepository(pool=pool).read(
                task_id=ids["task"], agent_run_id=ids["run"]
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await connection.execute(
                "INSERT INTO qc_web_run_policy_snapshot"
                "(task_id,agent_run_id,payload,payload_sha256) "
                "VALUES($1::uuid,$2::uuid,'{}'::jsonb,$3)",
                str(uuid4()),
                str(uuid4()),
                "0" * 64,
            )


@pytest.mark.asyncio
async def test_pg_cold_provider_restores_v1_and_same_process_new_run_reads_v2(
    monkeypatch, tmp_path
):
    import json
    from functools import partial
    from types import SimpleNamespace

    from services.api.app.agents.explorer import web_assessment_agent as agent_module
    from services.api.app.application import web_run_policy as policy_module
    from services.api.app.application import web_runtime_composition as runtime_module
    from services.api.app.core.config import Settings
    from services.api.app.infrastructure.browser.capture_runtime import (
        build_default_web_capture_runtime,
    )
    from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
        PostgresWebRunPolicySnapshotRepository,
    )
    from services.api.tests.test_web_runtime_composition import _web_record

    published = policy_payload()
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(published["page_catalog"]), encoding="utf-8")
    monkeypatch.setattr(policy_module, "_PAGE_CATALOG_PATH", catalog)
    monkeypatch.setattr(
        policy_module,
        "load_web_explorer_workflow_skill_content",
        lambda: published["workflow_skill"],
    )
    monkeypatch.setattr(
        runtime_module, "build_chat_client", lambda *args, **kwargs: SimpleNamespace()
    )

    class Agent:
        def __init__(self, **kwargs):
            self.instructions = kwargs["instructions"]

    monkeypatch.setattr(agent_module, "Agent", Agent)
    async with _database() as (connection, pool, second_pool, ids):
        # 冷组合新增预算读取也必须显式接隔离池，默认数据库仍保持不可达。
        monkeypatch.setattr(
            runtime_module,
            "PostgresWebExplorationBudgetLedgerRepository",
            partial(runtime_module.PostgresWebExplorationBudgetLedgerRepository, pool=pool),
        )
        # 种子采集已有合法当前页面引用，重建前按外键顺序解除。
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=NULL WHERE task_id=$1::uuid",
            ids["task"],
        )
        await connection.execute("DELETE FROM qc_capture WHERE task_id=$1::uuid", ids["task"])
        second_id = str(uuid4())
        await connection.execute(
            "INSERT INTO qc_agent_run (id,task_id,agent_type,agent_name,status) "
            "VALUES($1::uuid,$2::uuid,'main','Web Policy Test','running')",
            second_id,
            ids["task"],
        )
        try:
            record = _web_record().model_copy(update={"run_id": ids["run"], "task_id": ids["task"]})
            record.task_config["_task_target"]["target_url"] = "https://example.test/"
            provider = runtime_module.PostgresWebRuntimeProvider(
                policy_repository=PostgresWebRunPolicySnapshotRepository(pool=pool),
                capture_runtime_builder=partial(
                    build_default_web_capture_runtime, browser=object()
                ),
            )
            first = await provider.build(record=record, settings=Settings())
            original = first.policy_snapshot
            # 当前目录协议固定版本；发布正文修订以完整哈希区分，不伪造受支持新版本。
            from services.api.app.domain.page.web_page_catalog import (
                WebPageTypeCatalogError,
                parse_web_page_type_catalog,
            )

            unsupported = {**published["page_catalog"], "version": "published-v2"}
            with pytest.raises(WebPageTypeCatalogError, match="目录版本"):
                parse_web_page_type_catalog(unsupported)
            published["page_catalog"]["page_types"][0]["purpose"] += "，发布正文修订"
            catalog.write_text(json.dumps(published["page_catalog"]), encoding="utf-8")
            published["workflow_skill"] += "\n新发布的解释正文"
            restored = await runtime_module.PostgresWebRuntimeProvider(
                policy_repository=PostgresWebRunPolicySnapshotRepository(pool=second_pool),
                capture_runtime_builder=partial(
                    build_default_web_capture_runtime, browser=object()
                ),
            ).build(record=record, settings=Settings())
            second = await provider.build(
                record=record.model_copy(update={"run_id": second_id}), settings=Settings()
            )
            assert restored.policy_snapshot == original
            assert restored.deadline == first.deadline
            assert second.policy_snapshot.payload_sha256 != original.payload_sha256
            assert (
                second.policy_snapshot.payload["page_catalog"]["page_types"][0]["purpose"]
                == (published["page_catalog"]["page_types"][0]["purpose"])
            )
            assert (
                restored.policy_snapshot.payload["guide_version"]
                == original.payload["guide_version"]
            )
            assert restored.policy_snapshot.payload["menu_priors"] == {
                "schema_version": "web-runtime-navigation.v1"
            }
            assert first.navigation_context is not restored.navigation_context
            assert restored.navigation_context.resolve_site_context(
                "https://example.test/"
            ).site.origin == ("https://example.test")
            assert restored.age_gate_handler._assessor.framework_agent.instructions.endswith(
                original.payload["workflow_skill"]
            )
            assert second.age_gate_handler._assessor.framework_agent.instructions.endswith(
                "新发布的解释正文"
            )
        finally:
            await connection.execute("DELETE FROM qc_agent_run WHERE id=$1::uuid", second_id)


def test_policy_migration_downgrade_upgrade_only_in_verified_isolated_database():
    dsn = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"], os.environ["ALIOTH_DATABASE_URL"]
    )
    old = os.environ["ALIOTH_DATABASE_URL"]
    config = Config(str(Path(__file__).resolve().parents[1] / "migrations/alembic.ini"))

    async def check(expected):
        connection = await asyncpg.connect(dsn)
        try:
            assert tuple(await connection.fetchrow("SELECT current_database(),current_user")) == (
                "alioth_test_web_v2",
                "alioth_web_gap",
            )
            assert (
                await connection.fetchval("SELECT to_regclass('qc_web_run_policy_snapshot')")
                is not None
            ) == expected
        finally:
            await connection.close()

    asyncio.run(check(True))
    try:
        os.environ["ALIOTH_DATABASE_URL"] = dsn
        command.downgrade(config, "20260908_0099")
        asyncio.run(check(False))
        command.upgrade(config, "head")
        asyncio.run(check(True))
    finally:
        os.environ["ALIOTH_DATABASE_URL"] = old
