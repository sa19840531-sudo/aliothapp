"""在完整迁移后的隔离 PG 验证首页共用聚合和任务详情计数，结束后整体回滚。"""

import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.app.infrastructure.db.workstation_dashboard_repository import (
    PostgresWorkstationDashboardRepository,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)


class _Pool:
    def __init__(self, connection):
        self.connection = connection
        self.detail_row = None
        self.fail_execution_read = False

    @asynccontextmanager
    async def acquire(self):
        yield _Connection(self)


class _Connection:
    def __init__(self, pool):
        self.pool = pool

    def __getattr__(self, name):
        return getattr(self.pool.connection, name)

    async def fetchrow(self, query, *args):
        row = await self.pool.connection.fetchrow(query, *args)
        if "execution_stats.total_rules" in query:
            self.pool.detail_row = dict(row) if row else None
        return row

    async def fetch(self, query, *args):
        if self.pool.fail_execution_read and "WITH scoped_execution" in query:
            raise RuntimeError("模拟首页聚合读取失败")
        return await self.pool.connection.fetch(query, *args)


@dataclass
class _Case:
    connection: asyncpg.Connection
    pool: _Pool
    tenant: str
    other_tenant: str
    actor: str
    peer: str
    other_actor: str
    ruleset: str
    rules: list[str]

    def scope(self, permissions=()):
        return AccessScope(
            actor_user_id=self.actor,
            tenant_id=self.tenant,
            department_name="质检部",
            permissions=frozenset(permissions),
        )


@pytest.fixture
async def query_case():
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式设置完整迁移后的隔离 ALIOTH_TEST_DATABASE_URL")
    url = require_isolated_test_database_url(url, os.getenv("ALIOTH_DATABASE_URL") or None)
    connection = await asyncpg.connect(url)
    transaction = connection.transaction()
    await transaction.start()
    try:
        ids = {
            key: str(uuid4())
            for key in ("tenant", "other_tenant", "actor", "peer", "other_actor", "ruleset")
        }
        for name in ("tenant", "other_tenant"):
            await connection.execute(
                "INSERT INTO qc_tenant(id,tenant_key,display_name) "
                "VALUES($1::uuid,$1::text,$1::text)",
                ids[name],
            )
        for name, tenant in (
            ("actor", "tenant"),
            ("peer", "tenant"),
            ("other_actor", "other_tenant"),
        ):
            await connection.execute(
                """INSERT INTO qc_user(id,username,full_name,password_hash,role,tenant_id)
                   VALUES($1::uuid,$1::text,'查询回归用户','仅用于隔离测试','审计员',$2::uuid)""",
                ids[name],
                ids[tenant],
            )
        await connection.execute(
            """INSERT INTO qc_ruleset(id,ruleset_code,name,version,status,source_type,rule_count)
               VALUES($1::uuid,$1::text,'查询回归规则集',1,'enabled','manual',5)""",
            ids["ruleset"],
        )
        rules = [str(uuid4()) for _ in range(5)]
        for rule in rules:
            await connection.execute(
                """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,
                                      executor_type,check_point)
                   VALUES($1::uuid,$2::uuid,$1::text,'查询回归规则','visual','hybrid','检查页面')""",
                rule,
                ids["ruleset"],
            )
        yield _Case(connection, _Pool(connection), rules=rules, **ids)
    finally:
        await transaction.rollback()
        await connection.close()


async def _task(case, *, actor=None, tenant=None, department="质检部", status="running"):
    task, run = str(uuid4()), str(uuid4())
    await case.connection.execute(
        """INSERT INTO qc_task(id,target_url,ruleset_id,submitter_id,tenant_id,target_platform,
                               status,department_name,task_config)
           VALUES($1::uuid,'https://example.test/',$2::uuid,$3::uuid,$4::uuid,'h5',$5,$6,$7::jsonb)""",
        task,
        case.ruleset,
        actor or case.actor,
        tenant or case.tenant,
        status,
        department,
        json.dumps(
            {
                "workflow_version": "v1",
                "execution_mode": "full_inspection",
                "task_name": "查询回归任务",
                "ruleset_rule_total": 5,
            }
        ),
    )
    await case.connection.execute(
        """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,output_context)
           VALUES($1::uuid,$2::uuid,'main','查询回归运行','running',$3::jsonb)""",
        run,
        task,
        json.dumps({"unrelated_trace": [str(uuid4()) for _ in range(3000)]}),
    )
    return task, run


async def _execution(
    case,
    task,
    run,
    rule,
    *,
    status="pass",
    severity="high",
    bootstrap=False,
    reason=None,
    human=None,
    capture=None,
):
    execution = str(uuid4())
    await case.connection.execute(
        """INSERT INTO qc_rule_execution(id,task_id,agent_run_id,rule_id,execution_status,
                    severity,judge_strategy,judge_input,judge_output,human_verdict,page_capture_id,
                    created_at,finished_at)
           VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,'hybrid',$7::jsonb,$8::jsonb,
                  $9,$10::uuid,now(),now())""",
        execution,
        task,
        run,
        rule,
        status,
        severity,
        json.dumps({"bootstrap": bootstrap}),
        json.dumps({"reason": reason}),
        human,
        capture,
    )
    return execution


@pytest.mark.parametrize(
    ("permissions", "task_count", "execution_count"),
    [
        ((), 1, 2),
        (("taskObjects:read:department",), 2, 3),
        (("taskObjects:read:tenant",), 3, 4),
        (("taskObjects:read:any",), 4, 5),
    ],
)
async def test_dashboard_preserves_owner_department_tenant_and_global_aggregation(
    query_case, permissions, task_count, execution_count
):
    case = query_case
    # 全局权限必须包含其他事务留下的合法记录；不删除它们来假定测试库永远为空。
    unrelated, unrelated_run = await _task(
        case, actor=case.other_actor, tenant=case.other_tenant, department="外部部门"
    )
    await _execution(case, unrelated, unrelated_run, case.rules[3], status="fail")
    repository = PostgresWorkstationDashboardRepository(pool=case.pool)
    baseline = await repository.get_dashboard_overview(access_scope=case.scope(permissions))
    baseline_statuses = {
        row["execution_status"]: row["count"] for row in baseline["execution_status_distribution"]
    }
    if "taskObjects:read:any" in permissions:
        assert baseline["summary_cards"][0]["value"] >= 1
    else:
        assert baseline["summary_cards"][0]["value"] == 0
    own, own_run = await _task(case)
    await _execution(case, own, own_run, case.rules[0], status="skipped", bootstrap=True)
    await _execution(case, own, own_run, case.rules[0], status="pass")
    await _execution(
        case,
        own,
        own_run,
        case.rules[1],
        status="error",
        severity="info",
        reason="candidate_not_executed",
        human="ignored",
    )
    for actor, tenant, department in (
        (case.peer, case.tenant, "质检部"),
        (case.peer, case.tenant, "产品部"),
        (case.other_actor, case.other_tenant, "质检部"),
    ):
        task, run = await _task(case, actor=actor, tenant=tenant, department=department)
        await _execution(case, task, run, case.rules[2], status="fail")
    overview = await PostgresWorkstationDashboardRepository(pool=case.pool).get_dashboard_overview(
        access_scope=case.scope(permissions)
    )
    assert (
        overview["summary_cards"][0]["value"] == baseline["summary_cards"][0]["value"] + task_count
    )
    assert len(overview["recent_tasks"]) == min(
        10, baseline["summary_cards"][0]["value"] + task_count
    )
    assert sum(row["count"] for row in overview["risk_distribution"]) == (
        sum(row["count"] for row in baseline["risk_distribution"]) + execution_count
    )
    expected_statuses = dict(baseline_statuses)
    for status, count in {"fail": execution_count - 2, "ignored": 1, "pass": 1}.items():
        if count:
            expected_statuses[status] = expected_statuses.get(status, 0) + count
    assert {
        row["execution_status"]: row["count"] for row in overview["execution_status_distribution"]
    } == expected_statuses
    assert (
        overview["today_metrics"]["ignored_rules_today"]
        == baseline["today_metrics"]["ignored_rules_today"] + 1
    )
    assert (
        overview["today_metrics"]["abnormal_rules_today"]
        == baseline["today_metrics"]["abnormal_rules_today"]
    )
    assert overview["today_metrics"]["qc_success_rate_today"] == 100.0
    assert len(overview["latest_rule_executions"]) == min(
        10, sum(baseline_statuses.values()) + execution_count
    )


async def test_detail_counts_current_run_and_capture_judges_without_wide_context_grouping(
    query_case,
):
    case = query_case
    task, run = await _task(case)
    other_run = str(uuid4())
    await case.connection.execute(
        """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
           VALUES($1::uuid,$2::uuid,'rule_judge','独立判定','done')""",
        other_run,
        task,
    )
    capture = str(uuid4())
    await case.connection.execute(
        """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,capture_role,capture_status)
           VALUES($1::uuid,$2::uuid,$3::uuid,'web','after_action','captured')""",
        capture,
        task,
        run,
    )
    await case.connection.execute(
        """INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,
                                      page_slug,url,capture_status)
           VALUES($1::uuid,$2::uuid,$3::uuid,1,$1::text,'https://example.test/','captured')""",
        capture,
        task,
        run,
    )
    await _execution(case, task, run, case.rules[0], status="pass")
    await _execution(case, task, run, case.rules[1], status="skipped", bootstrap=True)
    await _execution(case, task, other_run, case.rules[1], capture=capture)
    await _execution(case, task, run, case.rules[2], status="fail")
    await _execution(case, task, other_run, case.rules[3], status="warning", capture=capture)
    await _execution(case, task, other_run, case.rules[4], status="pass")
    detail = await PostgresWorkstationQueryRepository(pool=case.pool).get_task_detail(
        task, access_scope=case.scope()
    )
    assert detail.task_id == task
    assert detail.run_id == run
    assert detail.status == "running"
    assert case.pool.detail_row["total_rules"] == 4
    assert case.pool.detail_row["finished_rules"] == 4
    denied = AccessScope(
        actor_user_id=case.other_actor, tenant_id=case.other_tenant, permissions=frozenset()
    )
    assert (
        await PostgresWorkstationQueryRepository(pool=case.pool).get_task_detail(
            task, access_scope=denied
        )
        is None
    )


async def test_empty_detail_and_dashboard_keep_zero_and_unknown_distinct(query_case):
    case = query_case
    task, run = await _task(case)
    detail = await PostgresWorkstationQueryRepository(pool=case.pool).get_task_detail(
        task, access_scope=case.scope()
    )
    assert detail.run_id == run
    assert case.pool.detail_row["total_rules"] == case.pool.detail_row["finished_rules"] == 0
    overview = await PostgresWorkstationDashboardRepository(pool=case.pool).get_dashboard_overview(
        access_scope=case.scope()
    )
    assert overview["risk_distribution"] == overview["execution_status_distribution"] == []
    assert overview["today_metrics"]["qc_success_rate_today"] is None


@pytest.mark.parametrize("fail_execution_read", [False, True])
async def test_dashboard_returns_connection_jit_setting_after_success_or_failure(
    query_case, fail_execution_read
):
    case = query_case
    # 独立连接模拟 API 正常借用池连接，避免测试回滚外层事务改变 SET LOCAL 的边界。
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    try:
        pool = _Pool(connection)
        pool.fail_execution_read = fail_execution_read
        initial_jit = await connection.fetchval("SHOW jit")
        repository = PostgresWorkstationDashboardRepository(pool=pool)
        if fail_execution_read:
            with pytest.raises(RuntimeError, match="模拟首页聚合读取失败"):
                await repository.get_dashboard_overview(access_scope=case.scope())
        else:
            await repository.get_dashboard_overview(access_scope=case.scope())
        assert await connection.fetchval("SHOW jit") == initial_jit
        assert not connection.is_in_transaction()
    finally:
        await connection.close()
