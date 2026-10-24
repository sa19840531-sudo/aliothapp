"""人工审核仓储必须同步规则终审与任务业务状态。"""

from typing import Any

from services.api.app.application.workstation_reviews import WorkstationReviewListFilter
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.workstation_review_repository import (
    PostgresWorkstationReviewRepository,
)

OWNER_ID = "11111111-1111-1111-1111-111111111111"
REVIEWER_ID = "22222222-2222-2222-2222-222222222222"
TENANT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_TENANT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _scope(
    *,
    actor_user_id: str = OWNER_ID,
    tenant_id: str = TENANT_ID,
    permissions: frozenset[str] = frozenset(),
) -> AccessScope:
    return AccessScope(
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        permissions=permissions,
    )


class _Context:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Connection:
    def __init__(
        self,
        *,
        status: str,
        pending_count: int,
        deny_update: bool = False,
    ) -> None:
        self.status = status
        self.pending_count = pending_count
        self.deny_update = deny_update
        self.task_status = "done" if status == "needs_recheck" else "review"
        self.task_row_version = 3
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_entered = False

    def transaction(self) -> _Context:
        self.transaction_entered = True
        return _Context(None)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        if compact.startswith("WITH eligible_review") and self.deny_update:
            return None
        if compact.startswith("SELECT") and "FROM qc_task" in compact:
            return {
                "task_id": str(args[0]),
                "status": self.task_status,
                "row_version": self.task_row_version,
                "finalization_state": "open",
            }
        if compact.startswith("UPDATE qc_task"):
            self.task_status = str(args[3])
            self.task_row_version += 1
            return {
                "task_id": str(args[0]),
                "status": self.task_status,
                "row_version": self.task_row_version,
                "finalization_state": "open",
            }
        return {
            "review_id": "review-1",
            "task_id": "task-1",
            "status": self.status,
            "agent_run_id": "run-1",
        }

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        return []

    async def fetchval(self, query: str, *args: Any) -> int | bool:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        if "SELECT EXISTS" in compact:
            return True
        return self.pending_count

    async def execute(self, query: str, *args: Any) -> None:
        self.calls.append((" ".join(query.split()), args))


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Context:
        return _Context(self.connection)


async def test_approve_review_syncs_human_verdict_and_closes_review_task() -> None:
    connection = _Connection(status="approved", pending_count=0)
    repository = PostgresWorkstationReviewRepository(pool=_Pool(connection))

    result = await repository.update_review_status(
        review_id="review-1",
        status="approved",
        reason="人工确认通过",
        access_scope=_scope(),
    )

    queries = [query for query, _args in connection.calls]
    assert connection.transaction_entered is True
    assert "WHEN 'approved' THEN 'pass'" in queries[0]
    assert "reviewer = $4::uuid" in queries[0]
    assert "task.deleted_at IS NULL" in queries[0]
    assert "FOR UPDATE OF task, execution, review" in queries[0]
    assert "execution.execution_status = 'manual_review'" in queries[0]
    action_args = next(args for query, args in connection.calls if query.startswith("WITH"))
    assert action_args[3] == OWNER_ID
    assert action_args[6] == TENANT_ID
    assert action_args[7] == OWNER_ID
    # 复核后仅重算有效业务转人工，未执行、占位和平台故障均不计入待确认数。
    pending_query = next(query for query in queries if "SELECT COUNT(*)" in query)
    assert "execution.execution_status = 'manual_review'" in pending_query
    assert "execution.human_verdict IS NULL" in pending_query
    assert "judge_input->>'bootstrap', 'false') <> 'true'" in pending_query
    assert "candidate_not_executed" in pending_query
    assert "run_budget_exhausted" in pending_query
    assert "semantic_provider_" in pending_query
    assert "semantic_judge_unavailable" in pending_query
    assert "jsonb_array_elements_text" in pending_query
    assert "judge_output->>'reason', ''))) NOT IN" in pending_query
    assert "!~ '^(capability_" in pending_query
    assert "AND NOT EXISTS" in pending_query
    # 任务级复核保留跨判定 Run 口径，仅占位替换的 H5 结论排除旧主 Run。
    assert "execution.agent_run_id" not in pending_query
    assert "current_h5_task.target_platform = 'h5'" in pending_query
    assert "FROM qc_rule_execution AS concluded" in pending_query
    task_update = next(query for query in queries if query.startswith("UPDATE qc_task"))
    assert "status = $4" in task_update
    assert "row_version = $3" in task_update
    assert "finalization_state <> 'sealed'" in task_update
    assert result == {
        "review_id": "review-1",
        "status": "approved",
        "message": "审核已更新",
    }


async def test_recheck_clears_human_verdict_and_reopens_done_task() -> None:
    connection = _Connection(status="needs_recheck", pending_count=1)
    repository = PostgresWorkstationReviewRepository(pool=_Pool(connection))

    await repository.update_review_status(
        review_id="review-1",
        status="needs_recheck",
        reason="需要重新采集证据",
        access_scope=_scope(
            actor_user_id=REVIEWER_ID,
            permissions=frozenset({"taskObjects:review:tenant"}),
        ),
    )

    queries = [query for query, _args in connection.calls]
    assert "WHEN 'needs_recheck' THEN NULL" in queries[0]
    task_update = next(query for query in queries if query.startswith("UPDATE qc_task"))
    assert "status = $4" in task_update
    task_update_args = next(
        args for query, args in connection.calls if query.startswith("UPDATE qc_task")
    )
    assert task_update_args[:4] == ("task-1", "done", 3, "review")


async def test_review_queue_passes_owner_scope_to_sql() -> None:
    connection = _Connection(status="pending", pending_count=1)
    repository = PostgresWorkstationReviewRepository(pool=_Pool(connection))

    await repository.list_reviews(
        WorkstationReviewListFilter(status="pending"),
        access_scope=_scope(),
    )

    query, args = connection.calls[0]
    assert "task.tenant_id = $7::uuid" in query
    assert "task.submitter_id = $8::uuid" in query
    assert "task.deleted_at IS NULL" in query
    assert "execution.execution_status = 'manual_review'" in query
    assert "human_verdict IS NULL" not in query
    assert args[4:10] == (False, False, TENANT_ID, OWNER_ID, False, False)


async def test_same_tenant_reviewer_scope_is_allowed_and_records_reviewer() -> None:
    connection = _Connection(status="approved", pending_count=0)
    repository = PostgresWorkstationReviewRepository(pool=_Pool(connection))
    reviewer_scope = _scope(
        actor_user_id=REVIEWER_ID,
        permissions=frozenset({"taskObjects:review:tenant"}),
    )

    result = await repository.update_review_status(
        review_id="review-1",
        status="approved",
        reason="同租户复核",
        access_scope=reviewer_scope,
    )

    action_query, action_args = next(
        (query, args) for query, args in connection.calls if query.startswith("WITH")
    )
    assert "FROM qc_task AS task" in action_query
    assert action_args[3] == REVIEWER_ID
    assert action_args[8] is True
    assert result is not None


async def test_cross_tenant_review_is_treated_as_not_found() -> None:
    connection = _Connection(status="approved", pending_count=0, deny_update=True)
    repository = PostgresWorkstationReviewRepository(pool=_Pool(connection))

    result = await repository.update_review_status(
        review_id="review-1",
        status="approved",
        reason="跨租户不应写入",
        access_scope=_scope(
            actor_user_id=REVIEWER_ID,
            tenant_id=OTHER_TENANT_ID,
            permissions=frozenset({"taskObjects:review:tenant"}),
        ),
    )

    assert result is None


async def test_platform_admin_scope_can_update_review_across_tenant() -> None:
    connection = _Connection(status="approved", pending_count=0)
    repository = PostgresWorkstationReviewRepository(pool=_Pool(connection))

    result = await repository.update_review_status(
        review_id="review-1",
        status="approved",
        reason="平台管理员复核",
        access_scope=_scope(
            actor_user_id=REVIEWER_ID,
            tenant_id=OTHER_TENANT_ID,
            permissions=frozenset({"taskObjects:write:any"}),
        ),
    )

    action_query, action_args = next(
        (query, args) for query, args in connection.calls if query.startswith("WITH")
    )
    assert "$5::boolean" in action_query
    assert action_args[4] is True
    assert result is not None
