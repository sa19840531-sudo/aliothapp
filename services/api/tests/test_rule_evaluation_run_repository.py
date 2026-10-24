"""独立规则质检运行仓储的 task + owner/tenant 复合过滤测试。"""

from dataclasses import dataclass
from typing import Any

import pytest

from services.api.app.application.rule_evaluation_runs import (
    RuleEvaluationRun,
    RuleEvaluationRunStatus,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.rule_evaluation_run_repository import (
    PostgresRuleEvaluationPageSource,
    PostgresRuleEvaluationRunRepository,
    RuleEvaluationRunPersistenceError,
)

TASK_ID = "11111111-1111-1111-1111-111111111111"
OTHER_TASK_ID = "22222222-2222-2222-2222-222222222222"
OWNER_ID = "33333333-3333-3333-3333-333333333333"
TENANT_ID = "44444444-4444-4444-4444-444444444444"


@dataclass
class _Acquire:
    connection: "_Connection"

    async def __aenter__(self) -> "_Connection":
        return self.connection

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _Connection:
    def __init__(self, *, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.rows: list[dict[str, Any]] = [row] if row else []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: Any) -> str | None:
        self.calls.append((" ".join(query.split()), args))
        if "INSERT INTO qc_rule_evaluation_run" in query:
            can_write_any, tenant_id, actor_id = args[5:8]
            if not (
                can_write_any
                or (tenant_id == TENANT_ID and actor_id == OWNER_ID)
            ):
                return None
        return "rule-eval:test"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((" ".join(query.split()), args))
        if "FROM qc_rule_evaluation_run AS run" in query:
            run_id, task_id, can_read_any, tenant_id, can_read_tenant, actor_id, _department = args
            if self.row is None or self.row["run_id"] != run_id:
                return None
            if self.row["task_id"] != task_id:
                return None
            if not (
                can_read_any
                or (
                    tenant_id == TENANT_ID
                    and (can_read_tenant or actor_id == OWNER_ID)
                )
            ):
                return None
        return self.row

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((" ".join(query.split()), args))
        if "FROM qc_rule_evaluation_run AS run" in query:
            task_id, can_read_any, tenant_id, can_read_tenant, actor_id, _department = args
            if self.row is None or self.row["task_id"] != task_id:
                return []
            if not (
                can_read_any
                or (
                    tenant_id == TENANT_ID
                    and (can_read_tenant or actor_id == OWNER_ID)
                )
            ):
                return []
        return self.rows


class _Pool:
    def __init__(self, *, row: dict[str, Any] | None = None) -> None:
        self.connection = _Connection(row=row)

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


def _scope(
    *permissions: str,
    actor: str = OWNER_ID,
    tenant: str = TENANT_ID,
) -> AccessScope:
    return AccessScope(
        actor_user_id=actor,
        tenant_id=tenant,
        permissions=frozenset(permissions),
    )


def _run() -> RuleEvaluationRun:
    return RuleEvaluationRun(
        run_id="rule-eval:test",
        task_id=TASK_ID,
        status=RuleEvaluationRunStatus.PENDING,
        requested_by=OWNER_ID,
    )


def _row() -> dict[str, Any]:
    return {
        "run_id": "rule-eval:test",
        "task_id": TASK_ID,
        "status": "succeeded",
        "page_count": 1,
        "rule_count": 1,
        "requested_rule_codes": [],
        "result_payload": [],
        "error_message": None,
        "requested_by": OWNER_ID,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
    }


@pytest.mark.asyncio
async def test_insert_pending_binds_authenticated_actor_and_scoped_task() -> None:
    pool = _Pool()
    repository = PostgresRuleEvaluationRunRepository(pool=pool)

    await repository.insert_pending(
        _run(),
        access_scope=_scope("taskObjects:write:owner"),
    )

    query, args = pool.connection.calls[0]
    assert "FROM qc_task AS task" in query
    assert "task.tenant_id = $7::uuid" in query
    assert "task.submitter_id = $8::uuid" in query
    assert args[1] == TASK_ID
    assert args[3] == OWNER_ID
    assert args[6:] == (TENANT_ID, OWNER_ID)


@pytest.mark.asyncio
async def test_tenant_reviewer_cannot_create_rule_evaluation_run() -> None:
    pool = _Pool()
    repository = PostgresRuleEvaluationRunRepository(pool=pool)
    reviewer_scope = _scope(
        "taskObjects:read:tenant",
        "taskObjects:review:tenant",
        actor="55555555-5555-5555-5555-555555555555",
    )

    with pytest.raises(RuleEvaluationRunPersistenceError):
        await repository.insert_pending(_run(), access_scope=reviewer_scope)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "scope",
        "expected_any",
        "expected_tenant",
        "expected_owner",
        "expected_visible",
    ),
    [
        (_scope("taskObjects:read:owner"), False, TENANT_ID, OWNER_ID, True),
        (
            _scope(
                "taskObjects:read:tenant",
                actor="55555555-5555-5555-5555-555555555555",
            ),
            False,
            TENANT_ID,
            "55555555-5555-5555-5555-555555555555",
            True,
        ),
        (
            _scope(
                "taskObjects:read:tenant",
                actor="66666666-6666-6666-6666-666666666666",
                tenant="77777777-7777-7777-7777-777777777777",
            ),
            False,
            "77777777-7777-7777-7777-777777777777",
            "66666666-6666-6666-6666-666666666666",
            False,
        ),
        (
            _scope(
                "taskObjects:read:any",
                "taskObjects:write:any",
                actor="88888888-8888-8888-8888-888888888888",
                tenant="99999999-9999-9999-9999-999999999999",
            ),
            True,
            "99999999-9999-9999-9999-999999999999",
            "88888888-8888-8888-8888-888888888888",
            True,
        ),
    ],
)
async def test_list_uses_current_scope_for_owner_tenant_and_admin_matrix(
    scope: AccessScope,
    expected_any: bool,
    expected_tenant: str,
    expected_owner: str,
    expected_visible: bool,
) -> None:
    pool = _Pool(row=_row())
    repository = PostgresRuleEvaluationRunRepository(pool=pool)

    runs = await repository.list_for_task(task_id=TASK_ID, access_scope=scope)

    assert bool(runs) is expected_visible
    query, args = pool.connection.calls[0]
    assert "JOIN qc_task AS task ON task.id = run.task_id" in query
    assert "run.task_id = $1::uuid" in query
    assert "task.tenant_id = $3::uuid" in query
    assert args[1] is expected_any
    assert args[2] == expected_tenant
    assert args[4] == expected_owner


@pytest.mark.asyncio
async def test_get_requires_both_path_task_id_and_run_id() -> None:
    pool = _Pool(row=_row())
    repository = PostgresRuleEvaluationRunRepository(pool=pool)

    run = await repository.get(
        run_id="rule-eval:test",
        task_id=TASK_ID,
        access_scope=_scope("taskObjects:read:owner"),
    )
    assert run is not None
    query, args = pool.connection.calls[0]
    assert "run.run_id = $1" in query
    assert "run.task_id = $2::uuid" in query
    assert args[:2] == ("rule-eval:test", TASK_ID)

    pool.connection.calls.clear()
    assert (
        await repository.get(
            run_id="rule-eval:test",
            task_id=OTHER_TASK_ID,
            access_scope=_scope("taskObjects:read:owner"),
        )
    ) is None
    # SQL 必须把 path task_id 作为复合条件传入；真实数据库中该条件会把
    # run 属于其他任务的情况过滤为空，API 再统一返回 404。
    assert pool.connection.calls[0][1][:2] == ("rule-eval:test", OTHER_TASK_ID)


@pytest.mark.asyncio
async def test_invalid_path_task_id_is_hidden_without_database_query() -> None:
    pool = _Pool(row=_row())
    repository = PostgresRuleEvaluationRunRepository(pool=pool)

    assert (
        await repository.get(
            run_id="rule-eval:test",
            task_id="not-a-uuid",
            access_scope=_scope("taskObjects:read:any"),
        )
    ) is None
    assert pool.connection.calls == []


@pytest.mark.asyncio
async def test_app_judgeable_page_falls_back_to_latest_visual_page_type() -> None:
    pool = _Pool(
        row={
            "page_capture_id": "55555555-5555-5555-5555-555555555555",
            "page_type": "content_detail",
            "ad_navigation_pair_complete": False,
        }
    )
    source = PostgresRuleEvaluationPageSource(pool=pool)

    pages = await source.list_judgeable_pages(
        task_id=TASK_ID,
        access_scope=_scope("taskObjects:read:owner"),
    )

    assert [(item.page_capture_id, item.page_type) for item in pages] == [
        ("55555555-5555-5555-5555-555555555555", "content_detail")
    ]
    query, _args = pool.connection.calls[0]
    assert "LEFT JOIN LATERAL" in query
    assert "FROM qc_app_visual_observation AS observation" in query
    assert "app_visual.visual_page_type" in query
    assert "ad_navigation_pair_complete" in query
    assert "common_capture.capture_role = 'after_action'" in query


@pytest.mark.asyncio
async def test_app_judgeable_page_marks_complete_ad_navigation_pair() -> None:
    pool = _Pool(
        row={
            "page_capture_id": "66666666-6666-5666-8666-666666666666",
            "page_type": "startup_ad",
            "ad_navigation_pair_complete": True,
        }
    )
    source = PostgresRuleEvaluationPageSource(pool=pool)

    pages = await source.list_judgeable_pages(
        task_id=TASK_ID,
        access_scope=_scope("taskObjects:read:owner"),
    )

    assert pages[0].evidence_tags == frozenset({"ad_navigation_pair_complete"})
