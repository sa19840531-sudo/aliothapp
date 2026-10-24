"""只读审计权限、同任务跨 Run 物理调用、排队预算与缺用量。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.h5_call_audit_repository import (
    PostgresH5CallAuditRepository,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database,
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)


def scope(ids, *, authorized=True):
    return AccessScope(
        actor_user_id=ids["user"] if authorized else str(uuid4()),
        tenant_id=ids["tenant"] if authorized else str(uuid4()),
        permissions=frozenset({"taskObjects:read:own"}),
    )


async def test_audit_queued_budget_and_access_scope_without_writing():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        reader = PostgresH5CallAuditRepository(pool=pool)
        assert (
            await reader.read(task_id=ids["task"], access_scope=scope(ids, authorized=False))
            is None
        )
        result = await reader.read(task_id=ids["task"], access_scope=scope(ids))
        assert result["budget_status"] == "not_initialized"
        assert result["usage_events"] == []
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_h5_llm_call_budget WHERE task_id=$1::uuid", ids["task"]
            )
            == 0
        )


async def test_audit_preserves_child_run_and_missing_usage_with_frozen_ledger():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        child = str(uuid4())
        await connection.execute(
            "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name) "
            "VALUES($1::uuid,$2::uuid,'rule_judge','audit-child')",
            child,
            ids["task"],
        )
        await connection.execute(
            "INSERT INTO qc_h5_llm_call_budget(task_id,agent_run_id,"
            "finalization_epoch,max_calls,admitted_calls,deadline) "
            "VALUES($1::uuid,$2::uuid,0,200,2,$3)",
            ids["task"],
            ids["run"],
            datetime.now(UTC) + timedelta(hours=3),
        )
        for run, source, usage in [
            (ids["run"], "h5_shortflow_agent", True),
            (child, "semantic_rule_judge", False),
        ]:
            await connection.execute(
                "INSERT INTO qc_llm_usage_event(task_id,agent_run_id,scope_type,"
                "source,provider,model,api_style,usage_available,ai_calls,"
                "total_tokens,total_cost,cost_currency,pricing_configured,pricing_source,metadata) "
                "VALUES($1::uuid,$2::uuid,'task_run',$3,'controlled','controlled',"
                "'chat', $4,1,120,0.25,'CNY',true,'controlled',"
                "'{\"physical_request\":true}'::jsonb)",
                ids["task"],
                run,
                source,
                usage,
            )
        result = await PostgresH5CallAuditRepository(pool=pool).read(
            task_id=ids["task"], access_scope=scope(ids)
        )
        assert result["recorded_physical_calls"] == 2
        assert result["usage_unavailable_calls"] == 1
        assert result["total_cost_cny"] is None
        assert result["known_cost_cny"] == "0.25000000"
        assert {row["agent_run_id"] for row in result["usage_events"]} == {ids["run"], child}


async def test_retry_diagnostics_export_only_registered_safe_metadata_fields():
    async with _database(surface_kind="web", target_platform="h5") as (connection, pool, ids):
        from services.api.app.infrastructure.db.llm_usage_event_repository import (
            PostgresLLMUsageEventRepository,
        )

        await PostgresLLMUsageEventRepository(pool=pool).record(
            {
                "task_id": ids["task"],
                "agent_run_id": ids["run"],
                "scope_type": "task_run",
                "source": "semantic_rule_judge",
                "ai_calls": 1,
                "metadata": {
                    "http_status_code": 402,
                    "request_status": "failed",
                    "physical_request": True,
                    "request_path": "/private-canary",
                    "error_message": "secret-canary",
                },
            }
        )
        result = await PostgresH5CallAuditRepository(pool=pool).read(
            task_id=ids["task"], access_scope=scope(ids)
        )
        row = result["usage_events"][0]
        assert row["http_status_code"] == 402
        assert row["request_status"] == "failed"
        assert row["physical_request"] is True
        assert "secret-canary" not in str(result)
        assert "private-canary" not in str(result)
