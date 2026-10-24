"""任务列表真实 PostgreSQL 的 JIT 隔离、回滚与对象范围回归。"""

import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import fakeredis
import pytest

from services.api.app.application import task_insight
from services.api.app.application.task_insight import TaskInsightService
from services.api.app.application.task_progress import whole_task_progress_percent
from services.api.app.application.workstation_tasks import (
    WorkstationTaskListFilter,
    WorkstationTaskService,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import TaskExecutionMode, TaskTargetPlatform
from services.api.app.infrastructure.db import workstation_query_repository
from services.api.app.infrastructure.db.rule_completion_sql import completed_rule_execution_sql
from services.api.app.infrastructure.db.rule_result_scope_sql import superseded_placeholder_sql
from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)


class _ObservedConnection:
    """转发真实连接，仅在执行列表 SQL 前读取真实会话设置。"""

    def __init__(self, connection: asyncpg.Connection):
        self.connection = connection
        self.observed_settings: list[tuple[str, str]] = []
        self.fail_next_fetch = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def fetch(self, query, *args):
        self.observed_settings.append(
            (
                await self.connection.fetchval("SHOW jit"),
                await self.connection.fetchval("SHOW transaction_read_only"),
            )
        )
        if self.fail_next_fetch:
            self.fail_next_fetch = False
            # 在 PostgreSQL 内产生真实失败，验证事务回滚而不是模拟 Python 异常。
            return await self.connection.fetch("SELECT 1 / 0")
        return await self.connection.fetch(query, *args)


class _ConnectionPool:
    """重复归还同一真实连接，防止连接切换掩盖设置污染。"""

    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
async def pg_connection():
    url = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"],
        os.getenv("ALIOTH_DATABASE_URL"),
        allowed_roles=frozenset({"postgres", "alioth_test"}),
    )
    connection = await asyncpg.connect(url)
    try:
        # 本 fixture 不写业务对象；数据口径用例另开外层写事务并整体回滚。
        await connection.execute("SET jit = on")
        await connection.execute("SET statement_timeout = '10s'")
        yield connection
    finally:
        await connection.close()


def _scope(**overrides):
    values = {
        "actor_user_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "permissions": frozenset({"taskObjects:read:tenant"}),
    }
    return AccessScope(**(values | overrides))


@pytest.mark.asyncio
@pytest.mark.parametrize("report_status", [None, "ready,generating,failed"])
async def test_list_query_disables_jit_only_inside_read_transaction(
    pg_connection, report_status,
):
    observed = _ObservedConnection(pg_connection)
    repository = PostgresWorkstationQueryRepository(pool=_ConnectionPool(observed))

    # 无外层事务，与 API 获取池连接后直接调用列表仓储的生产路径一致。
    for _ in range(2):
        assert not pg_connection.is_in_transaction()
        assert await pg_connection.fetchval("SHOW jit") == "on"
        result = await repository.list_tasks(
            WorkstationTaskListFilter(task_name=f"无此任务-{uuid4()}", report_status=report_status),
            access_scope=_scope(),
        )
        assert (result.items, result.total, result.page, result.page_size) == ([], 0, 1, 20)
        assert observed.observed_settings[-1] == ("off", "on")
        assert not pg_connection.is_in_transaction()
        assert await pg_connection.fetchval("SHOW jit") == "on"


@pytest.mark.asyncio
@pytest.mark.parametrize("report_status", [None, "ready"])
async def test_failed_list_query_rolls_back_and_keeps_same_connection_usable(
    pg_connection, report_status,
):
    observed = _ObservedConnection(pg_connection)
    observed.fail_next_fetch = True
    repository = PostgresWorkstationQueryRepository(pool=_ConnectionPool(observed))
    filters = WorkstationTaskListFilter(
        task_name=f"无此任务-{uuid4()}", report_status=report_status,
    )

    with pytest.raises(asyncpg.DivisionByZeroError):
        await repository.list_tasks(filters, access_scope=_scope())

    assert observed.observed_settings == [("off", "on")]
    assert not pg_connection.is_in_transaction()
    assert await pg_connection.fetchval("SHOW jit") == "on"
    assert await pg_connection.fetchval("SELECT 42") == 42
    result = await repository.list_tasks(filters, access_scope=_scope())
    assert result.items == []
    assert observed.observed_settings[-1] == ("off", "on")
    assert await pg_connection.fetchval("SHOW jit") == "on"


@dataclass
class _BusinessCase:
    ids: dict[str, str]
    prefix: str
    created_at: datetime


async def _seed_business_case(
    connection, *, formal_status="done", legacy_status="review", run_status="done",
) -> _BusinessCase:
    names = (
        "tenant", "foreign_tenant", "owner", "colleague", "ruleset",
        "formal", "legacy", "other_department", "foreign", "run", "rule_a", "rule_b",
    )
    ids = {name: str(uuid4()) for name in names}
    prefix = f"任务列表回归-{uuid4()}"
    created_at = datetime(2026, 9, 30, tzinfo=UTC)
    for name in ("tenant", "foreign_tenant"):
        await connection.execute(
            "INSERT INTO qc_tenant(id,tenant_key,display_name) VALUES($1::uuid,$2,$3)",
            ids[name], f"task-list-{ids[name]}", f"列表公司-{ids[name]}",
        )
    for name in ("owner", "colleague"):
        await connection.execute(
            """INSERT INTO qc_user(id,username,full_name,password_hash,role,tenant_id)
               VALUES($1::uuid,$2,$3,'integration-only','平台管理员',$4::uuid)""",
            ids[name], f"task-list-{ids[name]}", f"列表用户-{name}", ids["tenant"],
        )
    await connection.execute(
        """INSERT INTO qc_ruleset(id,ruleset_code,name,version,status,source_type,rule_count)
           VALUES($1::uuid,$2,'列表回归规则集',1,'enabled','manual',2)""",
        ids["ruleset"], f"task-list-{ids['ruleset']}",
    )
    for index, (name, platform, status, owner, tenant, department) in enumerate(
        (
            ("formal", "h5", formal_status, "owner", "tenant", "其他部门"),
            ("legacy", "app", legacy_status, "colleague", "tenant", "质量部"),
            ("other_department", "app", "queued", "colleague", "tenant", "其他部门"),
            ("foreign", "app", "queued", "colleague", "foreign_tenant", "质量部"),
        )
    ):
        config = {
            "task_name": f"{prefix}-{name}",
            "execution_mode": "full_inspection",
            "workflow_version": "v2" if name == "formal" else "v1",
            "ruleset_rule_total": 2,
            "product": {
                "department_name": department,
                "product_code": "LIST-PG",
                "product_name": "列表回归产品",
            },
            "journey_steps": ["首页", "列表"],
        }
        if platform == "app":
            config["app"] = {"platform_build_ref": f"test-build-{ids[name]}"}
        await connection.execute(
            """INSERT INTO qc_task(id,target_url,ruleset_id,submitter_id,tenant_id,
                                   department_name,target_platform,status,task_config,
                                   created_at,started_at,finished_at)
               VALUES($1::uuid,CASE WHEN $6 = 'app' THEN NULL ELSE 'https://example.test/' END,
                      $2::uuid,$3::uuid,$4::uuid,
                      $5,$6,$7,$8::jsonb,$9,$10,$11)""",
            ids[name], ids["ruleset"], ids[owner], ids[tenant], department, platform, status,
            json.dumps(config), created_at + timedelta(seconds=index),
            created_at if status != "queued" else None,
            created_at + timedelta(minutes=2) if status != "queued" else None,
        )
    await connection.execute(
        """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
           VALUES($1::uuid,$2::uuid,'main','列表回归',$3)""",
        ids["run"], ids["legacy"], run_status,
    )
    for rule, status in (("rule_a", "pass"), ("rule_b", "evidence_insufficient")):
        await connection.execute(
            """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,check_point)
               VALUES($1::uuid,$2::uuid,$3,'列表规则','text','hybrid','列表回归')""",
            ids[rule], ids["ruleset"], f"LIST-{ids[rule]}",
        )
        await connection.execute(
            """INSERT INTO qc_rule_execution(task_id,rule_id,agent_run_id,execution_status,
                                             judge_strategy,started_at,finished_at)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4,'hybrid',now(),now())""",
            ids["legacy"], ids[rule], ids["run"], status,
        )
    return _BusinessCase(ids, prefix, created_at)


@pytest.mark.parametrize("run_budget", [
    {"max_duration_seconds": 3600},
    {"timeout_minutes": 60},
    {"max_duration_seconds": "bad"},
    {},
])
async def test_loading_progress_matches_list_detail_and_stats_on_real_database(
    pg_connection, monkeypatch, run_budget,
) -> None:
    """三种读侧均使用冻结执行预算，规则缺证不再压低已停止任务的主进度。"""
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    for module in (workstation_query_repository, task_insight):
        monkeypatch.setattr(
            module, "whole_task_progress_percent",
            lambda status, **kwargs: whole_task_progress_percent(status, now=now, **kwargs),
        )
    transaction = pg_connection.transaction()
    await transaction.start()
    try:
        case = await _seed_business_case(
            pg_connection, legacy_status="running", run_status="running",
        )
        await pg_connection.execute(
            """UPDATE qc_task SET started_at=$2, finished_at=NULL,
                   loop_guard='{"max_duration_seconds":1800}'::jsonb WHERE id=$1::uuid""",
            case.ids["legacy"], now - timedelta(seconds=1800),
        )
        await pg_connection.execute(
            "UPDATE qc_agent_run SET loop_guard=$2::jsonb WHERE id=$1::uuid",
            case.ids["run"], json.dumps(run_budget),
        )
        scope = _scope(actor_user_id=case.ids["colleague"], tenant_id=case.ids["tenant"])
        pool = _ConnectionPool(pg_connection)
        repository = PostgresWorkstationQueryRepository(pool=pool)
        insight_repository = PostgresTaskInsightRepository(pool=pool)
        insight = TaskInsightService(repository=insight_repository)
        filters = WorkstationTaskListFilter(task_name=f"{case.prefix}-legacy")
        expected = whole_task_progress_percent(
            "running", started_at=now - timedelta(seconds=1800),
            loop_guard=run_budget or {"max_duration_seconds": 1800},
            run_status="running", now=now,
        )
        context = await insight_repository.fetch_task_context(
            case.ids["legacy"], access_scope=scope,
        )
        assert context["run_id"] == case.ids["run"]
        assert context["run_loop_guard"] == run_budget
        listed = await repository.list_tasks(filters, access_scope=scope)
        detail = await repository.get_task_detail(case.ids["legacy"], access_scope=scope)
        stats = await insight.stats(case.ids["legacy"], access_scope=scope)
        assert listed.items[0].progress == detail.progress == stats["progress_percent"] == expected
        assert (stats["finished"], stats["total"]) == (1, 2)
        await pg_connection.execute(
            "UPDATE qc_agent_run SET status='done' WHERE id=$1::uuid", case.ids["run"],
        )
        stats = await insight.stats(case.ids["legacy"], access_scope=scope)
        assert stats["progress_percent"] == 99
        assert (stats["finished"], stats["total"]) == (1, 2)
        await pg_connection.execute(
            "UPDATE qc_task SET status='review',finished_at=$2 WHERE id=$1::uuid",
            case.ids["legacy"], now,
        )
        listed = await repository.list_tasks(filters, access_scope=scope)
        detail = await repository.get_task_detail(case.ids["legacy"], access_scope=scope)
        stats = await insight.stats(case.ids["legacy"], access_scope=scope)
        assert listed.items[0].progress == detail.progress == stats["progress_percent"] == 100
        assert (stats["finished"], stats["total"]) == (1, 2)
        assert stats["status_counts"] == {"passed": 1, "evidence_insufficient": 1}
    finally:
        await transaction.rollback()


@pytest.mark.parametrize("platform", list(TaskTargetPlatform))
async def test_queued_detail_uses_actual_fifo_not_cross_platform_pg_priority(
    pg_connection, platform,
) -> None:
    transaction = pg_connection.transaction()
    await transaction.start()
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600, queue_key="test:pg-default")
    web_queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600, queue_key="test:pg-web")
    repository = PostgresWorkstationQueryRepository(pool=_ConnectionPool(pg_connection))
    try:
        case = await _seed_business_case(pg_connection, formal_status="queued")
        run_id = str(uuid4())
        await pg_connection.execute(
            """UPDATE qc_task SET target_platform=$2::text,
                   target_url=CASE WHEN $2::text='app' THEN NULL ELSE target_url END,
                   task_config=CASE WHEN $2::text='app' THEN task_config ||
                     '{"app":{"platform_build_ref":"queue-test-build"}}'::jsonb
                     ELSE task_config END,
                   priority=999 WHERE id=$1::uuid""",
            case.ids["formal"], platform.value,
        )
        await pg_connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
               VALUES($1::uuid,$2::uuid,'main','排队观测回归','pending')""",
            run_id, case.ids["formal"],
        )
        scope = _scope(actor_user_id=case.ids["owner"], tenant_id=case.ids["tenant"])
        durable_detail = await repository.get_task_detail(case.ids["formal"], access_scope=scope)
        assert durable_detail is not None
        assert durable_detail.queue_position is None
        assert durable_detail.tasks_ahead is None
        assert durable_detail.available_worker_count is None
        assert "Web" not in durable_detail.start_condition
        await redis_client.rpush("test:pg-default", "shared-app-ahead", run_id)
        await redis_client.rpush("test:pg-web", run_id)
        await queue.record_worker_heartbeat("explorer", roles={"explorer"})
        await queue.record_worker_heartbeat("web", roles={"web_explorer"})
        await queue.record_worker_heartbeat("judge", roles={"judge"})
        service = WorkstationTaskService(
            repository=repository, queue_status_reader=queue, web_queue_status_reader=web_queue
        )

        detail = await service.get_task_detail(case.ids["formal"], access_scope=scope)

        assert detail.status == "queued"
        assert detail.run_status == "pending"
        assert detail.queue_position == (1 if platform is TaskTargetPlatform.WEB else 2)
        assert detail.tasks_ahead == (0 if platform is TaskTargetPlatform.WEB else 1)
        assert detail.available_worker_count == 1
        selected_queue = web_queue if platform is TaskTargetPlatform.WEB else queue
        if platform is not TaskTargetPlatform.WEB:
            assert await selected_queue.dequeue(timeout=0.1) == "shared-app-ahead"
        assert await selected_queue.dequeue(timeout=0.1) == run_id

        detail = await service.get_task_detail(case.ids["formal"], access_scope=scope)

        assert detail.status == "queued"
        assert detail.queue_position is None
        assert detail.tasks_ahead is None
        assert detail.start_condition == "尚未确认排队位置，等待调度状态同步"
    finally:
        await redis_client.aclose()
        await transaction.rollback()


@pytest.mark.asyncio
async def test_list_preserves_scope_pagination_fields_and_report_business_status(pg_connection):
    transaction = pg_connection.transaction()
    await transaction.start()
    observed = _ObservedConnection(pg_connection)
    repository = PostgresWorkstationQueryRepository(pool=_ConnectionPool(observed))
    try:
        case = await _seed_business_case(pg_connection)
        own_scope = _scope(
            actor_user_id=case.ids["owner"], tenant_id=case.ids["tenant"],
            permissions=frozenset({"taskObjects:read:own"}),
        )
        tenant_scope = _scope(
            actor_user_id=case.ids["owner"], tenant_id=case.ids["tenant"],
        )
        department_scope = _scope(
            actor_user_id=case.ids["owner"], tenant_id=case.ids["tenant"],
            permissions=frozenset({"taskObjects:read:department"}), department_name="质量部",
        )
        filters = WorkstationTaskListFilter(task_name=case.prefix)
        own = await repository.list_tasks(filters, access_scope=own_scope)
        assert [item.task_id for item in own.items] == [case.ids["formal"]]
        formal = own.items[0]
        assert formal.target_platform is TaskTargetPlatform.H5
        assert formal.execution_mode is TaskExecutionMode.FULL_INSPECTION
        assert formal.report_availability == "none"
        assert formal.report_status == "generating"
        assert "report" not in formal.available_actions
        assert formal.progress == 100

        tenant = await repository.list_tasks(filters, access_scope=tenant_scope)
        assert tenant.total == 3
        assert case.ids["foreign"] not in {item.task_id for item in tenant.items}
        department = await repository.list_tasks(filters, access_scope=department_scope)
        assert {item.task_id for item in department.items} == {
            case.ids["formal"], case.ids["legacy"],
        }
        page = await repository.list_tasks(
            WorkstationTaskListFilter(task_name=case.prefix, page_size=1),
            access_scope=tenant_scope,
        )
        assert (page.total, page.page, page.page_size) == (3, 1, 1)
        assert page.items[0].task_id == case.ids["other_department"]

        ready = await repository.list_tasks(
            WorkstationTaskListFilter(task_name=case.prefix, report_status="ready"),
            access_scope=tenant_scope,
        )
        assert (ready.total, len(ready.items)) == (1, 1)
        legacy = ready.items[0]
        assert legacy.task_id == case.ids["legacy"]
        assert legacy.status == "review"
        assert legacy.progress == 100
        assert legacy.product.product_name == "列表回归产品"
        assert legacy.product.department_name == "质量部"
        assert legacy.journey_steps == ["首页", "列表"]
        assert legacy.tenant_name == f"列表公司-{case.ids['tenant']}"
        assert legacy.submitter_name == "列表用户-colleague"
        assert legacy.execution_started_at == case.created_at
        assert legacy.execution_finished_at == case.created_at + timedelta(minutes=2)
        generating = await repository.list_tasks(
            WorkstationTaskListFilter(task_name=case.prefix, report_status="generating"),
            access_scope=tenant_scope,
        )
        assert [item.task_id for item in generating.items] == [case.ids["formal"]]
        assert all(jit == "off" for jit, _ in observed.observed_settings)
    finally:
        # 种数据的外层写事务使仓储事务成为 savepoint；LOCAL 到最外层回滚才恢复。
        await transaction.rollback()
    assert await pg_connection.fetchval("SHOW jit") == "on"
    assert await pg_connection.fetchval(
        "SELECT count(*) FROM qc_task WHERE task_config->>'task_name' LIKE $1", f"{case.prefix}%",
    ) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["h5", "web"])
@pytest.mark.parametrize(
    "status,without_report", [("canceled", "not_started"), ("failed", "failed")],
)
async def test_terminal_task_report_projection_and_filters_follow_actual_availability(
    pg_connection, platform, status, without_report,
):
    transaction = pg_connection.transaction()
    await transaction.start()
    repository = PostgresWorkstationQueryRepository(pool=_ConnectionPool(pg_connection))
    try:
        case = await _seed_business_case(pg_connection, formal_status=status)
        run_id = str(uuid4())
        await pg_connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,output_context)
               VALUES($1::uuid,$2::uuid,'main','终态报告回归',$3,'{}'::jsonb)""",
            run_id, case.ids["formal"], status,
        )
        await pg_connection.execute(
            """UPDATE qc_task SET status=$2,target_platform=$3,finalization_agent_run_id=$4::uuid,
                   task_config=task_config
                       || '{"web_inspection_notification_mode":"enforced"}'::jsonb
               WHERE id=$1::uuid""",
            case.ids["formal"], status, platform, run_id,
        )
        scope = _scope(actor_user_id=case.ids["owner"], tenant_id=case.ids["tenant"])
        name = f"{case.prefix}-formal"

        async def assert_projection(expected_status, availability):
            unfiltered = await repository.list_tasks(
                WorkstationTaskListFilter(task_name=name), access_scope=scope,
            )
            assert (unfiltered.total, len(unfiltered.items)) == (1, 1)
            item = unfiltered.items[0]
            expected = (status, expected_status, availability)
            assert (item.status, item.report_status, item.report_availability) == expected
            detail = await repository.get_task_detail(case.ids["formal"], access_scope=scope)
            assert (detail.status, detail.report_status, detail.report_availability) == expected
            assert ("report" in item.available_actions) is (availability == "preview")
            assert ("report" in detail.available_actions) is (availability == "preview")
            for report_filter in ("not_started", "generating", "ready", "failed", "not_applicable"):
                filtered = await repository.list_tasks(
                    WorkstationTaskListFilter(task_name=name, report_status=report_filter),
                    access_scope=scope,
                )
                assert filtered.total == int(report_filter == expected_status)
                assert len(filtered.items) == int(report_filter == expected_status)

        # 没有持久化报告时，取消是未生成，执行失败仍保留生成失败的现有口径。
        await assert_projection(without_report, "none")
        await pg_connection.execute(
            """UPDATE qc_agent_run SET output_context='{"final_report":{"rules":[]}}'::jsonb
               WHERE id=$1::uuid""",
            run_id,
        )
        await pg_connection.execute(
            """INSERT INTO qc_rule_execution(task_id,rule_id,agent_run_id,execution_status,
                judge_strategy,started_at,finished_at)
               VALUES($1::uuid,$2::uuid,$3::uuid,'pass','hybrid',now(),now())""",
            case.ids["formal"], case.ids["rule_a"], run_id,
        )
        # 真实预览优先于任务取消或失败；列表、详情与筛选都必须保持可查看。
        await assert_projection("ready", "preview")
    finally:
        await transaction.rollback()


@pytest.mark.parametrize("platform", ["app", "h5", "web"])
@pytest.mark.parametrize("owner_bound", [False, True])
async def test_batched_list_counts_match_detail_scope_with_null_and_occluded_placeholders(
    pg_connection, platform, owner_bound,
):
    """真实库对账逐条门禁，覆盖空值、同页型替代、旧主 Run 和独立判定。"""
    transaction = pg_connection.transaction()
    await transaction.start()

    class ResultConnection(_ObservedConnection):
        async def fetch(self, query, *args):
            self.rows = await super().fetch(query, *args)
            return self.rows

    observed = ResultConnection(pg_connection)
    repository = PostgresWorkstationQueryRepository(pool=_ConnectionPool(observed))
    try:
        case = await _seed_business_case(pg_connection)
        ids = case.ids
        old, judge = str(uuid4()), str(uuid4())
        await pg_connection.execute(
            "DELETE FROM qc_rule_execution WHERE task_id = $1::uuid", ids["legacy"],
        )
        await pg_connection.execute(
            """UPDATE qc_task SET target_platform=$2::text,
                   target_url=CASE WHEN $2::text='app' THEN NULL ELSE 'https://example.test/' END,
                   finalization_agent_run_id=$3::uuid,
                   task_config=task_config || '{"workflow_version":"v2"}'::jsonb
               WHERE id=$1::uuid""",
            ids["legacy"], platform, ids["run"] if owner_bound else None,
        )
        await pg_connection.executemany(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,created_at)
               VALUES($1::uuid,$2::uuid,$3,'批量列表回归','done',$4)""",
            [
                (old, ids["legacy"], "main", case.created_at - timedelta(days=1)),
                (judge, ids["legacy"], "rule_judge", datetime.now(UTC) + timedelta(days=1)),
            ],
        )
        captures = {}
        for name, page_type in (
            ("clean", "home"), ("occluded_home", "home"), ("occluded_list", "video_list"),
            ("independent", "home"),
        ):
            captures[name] = str(uuid4())
            await pg_connection.execute(
                """INSERT INTO qc_capture(
                       id,task_id,agent_run_id,surface_kind,capture_role,metadata
                   ) VALUES($1::uuid,$2::uuid,$3::uuid,'web','observation',$4::jsonb)""",
                captures[name], ids["legacy"], ids["run"], json.dumps({"page_type": page_type}),
            )
        occluded = {"presentation_rejection": {"reason_code": "BASE_PAGE_OCCLUDED"}}
        for rule, run, capture, status, output in (
            ("rule_a", ids["run"], captures["clean"], "pass", {}),
            ("rule_a", ids["run"], None, "evidence_insufficient", {}),
            ("rule_a", ids["run"], captures["occluded_home"], "evidence_insufficient", occluded),
            ("rule_a", ids["run"], captures["occluded_list"], "evidence_insufficient", occluded),
            ("rule_a", judge, captures["independent"], "warning", {}),
            ("rule_b", ids["run"], None, "skipped", {"reason": "candidate_not_executed"}),
            ("rule_b", old, None, "pass", {}),
        ):
            await pg_connection.execute(
                """INSERT INTO qc_rule_execution(
                       task_id,rule_id,agent_run_id,page_capture_id,execution_status,
                       judge_strategy,judge_output
                   ) VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,'hybrid',$6::jsonb)""",
                ids["legacy"], ids[rule], run, capture, status, json.dumps(output),
            )
        expected = await pg_connection.fetchrow(
            f"""SELECT COUNT(*)::int AS total_rules, COUNT(*) FILTER (
                    WHERE {completed_rule_execution_sql('execution')}
                )::int AS finished_rules
                FROM qc_rule_execution AS execution
                WHERE execution.task_id=$1::uuid
                  AND (execution.agent_run_id=$2::uuid OR EXISTS (
                      SELECT 1 FROM qc_capture AS capture
                      WHERE capture.id=execution.page_capture_id AND capture.agent_run_id=$2::uuid
                  ))
                  AND NOT {superseded_placeholder_sql('execution')}""",
            ids["legacy"], ids["run"],
        )
        assert dict(expected) == {
            "total_rules": 5 if platform == "h5" else 4, "finished_rules": 2,
        }
        scope = _scope(actor_user_id=ids["owner"], tenant_id=ids["tenant"])
        for report_status in (None, "ready,generating,failed"):
            result = await repository.list_tasks(
                WorkstationTaskListFilter(
                    task_name=f"{case.prefix}-legacy", report_status=report_status,
                ),
                access_scope=scope,
            )
            assert result.total == 1
            assert result.items[0].task_id == ids["legacy"]
            assert {key: observed.rows[0][key] for key in expected.keys()} == dict(expected)
    finally:
        await transaction.rollback()
