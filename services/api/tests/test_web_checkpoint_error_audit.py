"""0115 仅增加安全协调诊断，恢复游标本身的引用白名单不变。"""

import importlib
from dataclasses import replace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from services.api.app.domain.exploration.web_run_stage_checkpoint import WebStageCursor
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
)
from services.api.app.infrastructure.db.web_run_stage_checkpoint_row import (
    parse_web_run_stage_checkpoint_row,
    web_run_stage_checkpoint_values,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import _database
from services.api.tests.test_web_run_stage_checkpoint import NOW, RUN_ID
from services.api.tests.test_web_run_stage_checkpoint_postgres import (
    _upgrade_isolated_postgres as _upgrade_isolated_postgres,
)
from services.api.tests.test_web_run_stage_checkpoint_postgres import pg, seed_current
from services.api.tests.test_web_run_stage_checkpoint_repository import ClockedCheckpointConnection

DETAIL = {
    "raw_error_code": "after_capture_failed",
    "coordination_stage": "after_capture",
    "cause_type": "CaptureTransportError",
    "retryable": True,
}


def test_error_audit_survives_domain_row_roundtrip_but_never_enters_cursor_payload():
    cursor = WebStageCursor.start(run_id=RUN_ID, logical_page_key="home").claim(
        worker_id="worker", now=NOW
    )
    failed = cursor.schedule_retry(
        error_code="WEB_BOOTSTRAP_AFTER_CAPTURE_FAILED", now=NOW, detail=DETAIL
    )
    assert (
        dict(
            parse_web_run_stage_checkpoint_row(
                web_run_stage_checkpoint_values(failed)
            ).last_error_detail
        )
        == DETAIL
    )
    with pytest.raises(ValueError):
        replace(cursor, cursor_payload=DETAIL)


@pytest.mark.parametrize(
    "changes",
    [
        {"raw_error_code": "https://example.test/?token=x"},
        {"raw_error_code": "x" * 129},
        {"coordination_stage": "unknown"},
        {"cause_type": "Bearer abc"},
        {"retryable": "true"},
        {"message": "sensitive error"},
    ],
)
def test_error_audit_rejects_unsafe_types_and_content(changes):
    with pytest.raises(ValueError):
        WebStageCursor(
            run_id=RUN_ID, logical_page_key="home", last_error_detail={**DETAIL, **changes}
        )


def test_0115_upgrade_downgrade_emit_strict_audit_constraints(monkeypatch):
    module = importlib.import_module(
        "services.api.migrations.versions.20260916_0121_web_checkpoint_error_audit"
    )
    assert module.down_revision == "20260916_0120"
    statements = []
    monkeypatch.setattr(module.op, "execute", lambda sql: statements.append(str(sql)))
    module.upgrade()
    upgrade = " ".join(" ".join(statements).split())
    assert "last_error_detail - ARRAY" in upgrade
    assert "jsonb_typeof(last_error_detail->'retryable') = 'boolean'" in upgrade
    assert "before_capture" in upgrade and "after_capture" in upgrade
    assert "{0,127}" in upgrade
    statements.clear()
    module.downgrade()
    downgrade = " ".join(" ".join(statements).split())
    assert "DROP CONSTRAINT ck_qc_web_checkpoint_error_audit" in downgrade
    assert "WEB_CHECKPOINT_ERROR_AUDIT_DOWNGRADE_BLOCKED" in downgrade
    assert "UPDATE qc_web_run_stage_checkpoint" not in downgrade
    assert "ADD CONSTRAINT ck_qc_web_checkpoint_reference_only" in downgrade
    graph = ScriptDirectory.from_config(Config("services/api/migrations/alembic.ini"))
    heads = graph.get_heads()
    assert len(heads) == 1
    assert "20260916_0121" in {
        script.revision for script in graph.walk_revisions(head=heads[0])
    }


async def test_repository_retry_persists_structured_coordination_audit():
    cursor = WebStageCursor.start(run_id=RUN_ID, logical_page_key="home").claim(
        worker_id="worker", now=NOW
    )
    connection = ClockedCheckpointConnection(cursor, locked_at=NOW)
    result = await PostgresWebRunStageCheckpointRepository(pool=connection).schedule_retry(
        cursor, error_code="WEB_BOOTSTRAP_AFTER_CAPTURE_FAILED", detail=DETAIL
    )
    assert dict(result.last_error_detail) == DETAIL


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
async def test_postgres_error_audit_roundtrip_and_guarded_downgrade_preserve_data(monkeypatch):
    import json

    import asyncpg

    async with _database() as (control, pool, _, ids):
        await seed_current(control, ids)
        repo = PostgresWebRunStageCheckpointRepository(pool=pool)
        await repo.create(WebStageCursor.start(run_id=ids["run"], logical_page_key="audit"))
        cursor = await repo.claim(
            run_id=ids["run"], logical_page_key="audit", expected_row_version=0, worker_id="worker"
        )
        await repo.schedule_retry(
            cursor, error_code="WEB_BOOTSTRAP_AFTER_CAPTURE_FAILED", detail=DETAIL
        )
        restored = await repo.get(run_id=ids["run"], logical_page_key="audit")
        assert dict(restored.last_error_detail) == DETAIL
        for invalid in (
            {"retryable": "true"},
            {"raw_error_code": "https://bad/?token=x"},
            {"coordination_stage": "other"},
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await control.execute(
                    "UPDATE qc_web_run_stage_checkpoint SET last_error_detail=$2::jsonb "
                    "WHERE run_id=$1::uuid",
                    ids["run"],
                    json.dumps(invalid),
                )
        module = importlib.import_module(
            "services.api.migrations.versions.20260916_0121_web_checkpoint_error_audit"
        )
        sql = []
        monkeypatch.setattr(module.op, "execute", lambda statement: sql.append(str(statement)))
        module.downgrade()
        with pytest.raises(
            asyncpg.RaiseError, match="WEB_CHECKPOINT_ERROR_AUDIT_DOWNGRADE_BLOCKED"
        ):
            await control.execute(";".join(sql))
        assert (
            dict((await repo.get(run_id=ids["run"], logical_page_key="audit")).last_error_detail)
            == DETAIL
        )


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
async def test_postgres_empty_audit_downgrade_upgrade_restores_original_constraint(monkeypatch):
    async with _database() as (control, _pool, _, _ids):
        async def checkpoint_constraints():
            rows = await control.fetch(
                "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                "WHERE conrelid='qc_web_run_stage_checkpoint'::regclass ORDER BY conname"
            )
            return [(row["conname"], row["definition"]) for row in rows]

        original_constraints = await checkpoint_constraints()
        module = importlib.import_module(
            "services.api.migrations.versions.20260916_0121_web_checkpoint_error_audit"
        )
        sql = []
        monkeypatch.setattr(module.op, "execute", lambda statement: sql.append(str(statement)))
        module.downgrade()
        # 旧迁移的约束往返只在事务内验证，回滚保留当前迁移头新增的全部字段。
        transaction = control.transaction()
        await transaction.start()
        try:
            await control.execute(";".join(sql))
            constraint = await control.fetchval(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_qc_web_checkpoint_reference_only'"
            )
            assert "raw_error_code" not in constraint
            sql.clear()
            module.upgrade()
            await control.execute(";".join(sql))
            constraint = await control.fetchval(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_qc_web_checkpoint_reference_only'"
            )
            assert "raw_error_code" in constraint
        finally:
            await transaction.rollback()
        assert await checkpoint_constraints() == original_constraints
