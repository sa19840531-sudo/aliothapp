"""任务提交幂等、原子事实与 Redis 可恢复投影合同。"""

from __future__ import annotations

import runpy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from services.api.app.application.app_workflow_dispatch import AppWorkflowDispatcher
from services.api.app.application.app_workflow_router import AppWorkflowRouter
from services.api.app.application.task_submission import (
    CreatedTask,
    CreateTaskSubmissionCommand,
    PersistedTaskSubmission,
    TaskSubmissionIdempotencyConflictError,
    TaskSubmissionService,
)
from services.api.app.application.task_submission_delivery import (
    TaskSubmissionProjectionService,
)
from services.api.app.core.config import Settings
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.domain.task import TaskTarget, TaskTargetPlatform, WebTaskConfig
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord, RedisAgentRunQueue

TASK_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "00000000-0000-0000-0000-000000000002"
RULESET_ID = "00000000-0000-0000-0000-000000000003"
SUBMISSION_ID = "00000000-0000-0000-0000-000000000004"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
ACCESS_SCOPE = AccessScope(
    actor_user_id="00000000-0000-0000-0000-000000000010",
    tenant_id="00000000-0000-0000-0000-000000000011",
    permissions=frozenset(),
)


class _AsyncContext:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _SubmissionConnection:
    def __init__(self, *, existing: dict[str, Any] | None = None) -> None:
        self.existing = existing
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.transaction_count = 0

    def transaction(self) -> _AsyncContext:
        self.transaction_count += 1
        return _AsyncContext(self)

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "FROM qc_task_submission AS submission" in normalized:
            return self.existing
        if "FROM qc_ruleset AS ruleset" in normalized and "INSERT INTO qc_task" not in normalized:
            assert "FOR SHARE OF ruleset" in normalized
            return {"ruleset_id": RULESET_ID, "ruleset_version": 1,
                    "ruleset_snapshot_hash": "", "rules": []}
        if "INSERT INTO qc_task" in normalized:
            return {
                "task_id": TASK_ID,
                "ruleset_id": RULESET_ID,
                "target_platform": "h5",
                "target_url": "https://example.test/h5",
                "status": "queued",
                "task_config": {"workflow_version": "v2"},
                "page_sampling_policy": {},
                "loop_guard": {},
            }
        raise AssertionError(normalized)

    async def fetchval(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchval", normalized, args))
        if "INSERT INTO qc_outbox_event" in normalized:
            return args[0]
        raise AssertionError(normalized)

    async def execute(self, query: str, *args: Any):
        self.calls.append(("execute", " ".join(query.split()), args))
        return "INSERT 0 1"


class _Pool:
    def __init__(self, connection: _SubmissionConnection) -> None:
        self.connection = connection

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(self.connection)


def _command(*, request_hash: str | None = None) -> CreateTaskSubmissionCommand:
    payload = {
        "protocol_version": "task-submission-request.v1",
        "target": {
            "platform": "h5",
            "target_url": "https://example.test/h5",
            "app": None,
        },
        "ruleset_code": "alioth_first_ruleset",
        "ruleset_version": 1,
        "channel": "workbench",
        "priority": 100,
        "task_config": {"workflow_version": "v2"},
        "page_sampling_policy": {},
        "loop_guard": {},
        "submission_kind": "v2_full_task",
    }
    return CreateTaskSubmissionCommand(
        access_scope=ACCESS_SCOPE,
        target=TaskTarget(
            platform=TaskTargetPlatform.H5,
            target_url="https://example.test/h5",
        ),
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={"workflow_version": "v2"},
        page_sampling_policy={},
        loop_guard={},
        submission_kind="v2_full_task",
        idempotency_key="request-1",
        request_hash=request_hash or stable_protocol_hash(payload),
        request_payload=payload,
    )


async def test_postgres_submission_commits_task_run_fact_and_outbox_in_one_transaction() -> None:
    connection = _SubmissionConnection()
    repository = PostgresTaskRepository(pool=_Pool(connection))

    result = await repository.create_submission(_command())

    assert result.task.task_id == TASK_ID
    assert result.replayed is False
    assert connection.transaction_count == 1
    queries = [query for _, query, _ in connection.calls]
    task_index = next(i for i, query in enumerate(queries) if "INSERT INTO qc_task" in query)
    run_index = next(i for i, query in enumerate(queries) if "INSERT INTO qc_agent_run" in query)
    submission_index = next(
        i for i, query in enumerate(queries) if "INSERT INTO qc_task_submission" in query
    )
    outbox_index = next(
        i for i, query in enumerate(queries) if "INSERT INTO qc_outbox_event" in query
    )
    assert task_index < run_index < submission_index < outbox_index


async def test_postgres_submission_replays_same_hash_and_rejects_payload_drift() -> None:
    command = _command()
    existing = {
        "submission_id": SUBMISSION_ID,
        "task_id": TASK_ID,
        "run_id": RUN_ID,
        "request_hash": command.request_hash,
        "ruleset_id": RULESET_ID,
        "target_platform": "h5",
        "target_url": "https://example.test/h5",
        "status": "queued",
        "task_config": {"workflow_version": "v2"},
        "page_sampling_policy": {},
        "loop_guard": {},
    }
    connection = _SubmissionConnection(existing=existing)
    repository = PostgresTaskRepository(pool=_Pool(connection))

    replay = await repository.create_submission(command)

    assert replay.run_id == RUN_ID
    assert replay.replayed is True
    assert not any("INSERT INTO qc_task " in query for _, query, _ in connection.calls)

    with pytest.raises(TaskSubmissionIdempotencyConflictError, match="不同任务请求"):
        await repository.create_submission(_command(request_hash="f" * 64))


class _ReliableRepository:
    def __init__(self) -> None:
        self.commands: list[CreateTaskSubmissionCommand] = []

    async def create_submission(
        self,
        command: CreateTaskSubmissionCommand,
    ) -> PersistedTaskSubmission:
        self.commands.append(command)
        return PersistedTaskSubmission(
            task=CreatedTask(
                task_id=TASK_ID,
                ruleset_id=RULESET_ID,
                target=command.target,
                status="queued",
                task_config=command.task_config,
                page_sampling_policy=command.page_sampling_policy,
                loop_guard=command.loop_guard,
            ),
            run_id=RUN_ID,
            replayed=False,
        )


class _ForbiddenDependency:
    def __getattr__(self, name: str):
        raise AssertionError(f"可靠提交不得调用提交后依赖：{name}")


async def test_service_returns_after_postgres_commit_without_waiting_for_redis() -> None:
    repository = _ReliableRepository()
    service = TaskSubmissionService(
        task_repository=repository,  # type: ignore[arg-type]
        agent_run_queue=_ForbiddenDependency(),  # type: ignore[arg-type]
        state_store=_ForbiddenDependency(),  # type: ignore[arg-type]
        default_workflow_version="v2",
    )

    result = await service.submit_task(
        access_scope=ACCESS_SCOPE,
        target_url="https://example.test/h5",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="workbench",
        priority=100,
        task_config={},
        page_sampling_policy={},
        loop_guard={},
        idempotency_key="request-1",
        submission_kind="v2_full_task",
    )

    assert result.task_id == TASK_ID
    assert result.run_id == RUN_ID
    assert repository.commands[0].idempotency_key == "request-1"
    assert "_task_target" not in repository.commands[0].task_config


async def test_reliable_web_submission_persists_target_for_notification_sql() -> None:
    """Web Task 必须在权威快照中冻结通知 SQL 依赖的目标身份。"""

    repository = _ReliableRepository()
    service = TaskSubmissionService(
        task_repository=repository,  # type: ignore[arg-type]
        agent_run_queue=_ForbiddenDependency(),  # type: ignore[arg-type]
        state_store=_ForbiddenDependency(),  # type: ignore[arg-type]
        default_workflow_version="v2",
    )

    await service.submit_task(
        access_scope=ACCESS_SCOPE,
        target=TaskTarget(
            platform=TaskTargetPlatform.WEB,
            target_url="https://example.test",
            web=WebTaskConfig(browser_profile_id="desktop_fhd_1920x1080_v1"),
        ),
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="api",
        priority=80,
        task_config={"execution_mode": "full_inspection"},
        page_sampling_policy={},
        loop_guard={},
        idempotency_key="web-request-1",
        submission_kind="v2_full_task",
    )

    assert repository.commands[0].task_config["_task_target"] == {
        "platform": "web",
        "target_url": "https://example.test",
        "app": None,
        "web": {
            "browser_profile_id": "desktop_fhd_1920x1080_v1",
            "adult_self_attestation": None,
        },
    }
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    queued = AgentRunRecord(
        run_id=RUN_ID,
        task_id=TASK_ID,
        message="Web 最小目标完整链路",
        submitted_at=NOW,
        task_config=repository.commands[0].task_config,
    )
    await queue.restore_queued(queued)
    restored = await queue.get(RUN_ID)
    assert restored is not None

    class WebHandler:
        async def run(self, *, record, settings, progress_recorder=None):
            del settings, progress_recorder
            return record.task_config["_task_target"]

    dispatcher = AppWorkflowDispatcher(
        router=AppWorkflowRouter(app_qc_enabled=False, web_v2_mode="on"),
        handler=None,
        web_handler=WebHandler(),
    )
    dispatched_target = await dispatcher.dispatch_web_v2(
        record=restored,
        settings=Settings(),
    )

    assert dispatched_target["web"]["adult_self_attestation"] is None


@pytest.mark.parametrize(
    ("default_workflow_version", "execution_mode", "submission_kind", "has_target_snapshot"),
    (
        ("v2", "exploration_only", "v2_exploration_task", True),
        ("v1", "full_inspection", "v1_task", False),
    ),
)
async def test_web_submission_freezes_v2_exploration_target_but_not_v1(
    default_workflow_version: str,
    execution_mode: str,
    submission_kind: str,
    has_target_snapshot: bool,
) -> None:
    """Web V2 仅探索也冻结完整运行目标，快照本身不授予正式质检资格。"""

    repository = _ReliableRepository()
    service = TaskSubmissionService(
        task_repository=repository,  # type: ignore[arg-type]
        agent_run_queue=_ForbiddenDependency(),  # type: ignore[arg-type]
        state_store=_ForbiddenDependency(),  # type: ignore[arg-type]
        default_workflow_version=default_workflow_version,
    )

    await service.submit_task(
        access_scope=ACCESS_SCOPE,
        target=TaskTarget(
            platform=TaskTargetPlatform.WEB,
            target_url="https://example.test",
            web=WebTaskConfig(browser_profile_id="desktop_fhd_1920x1080_v1"),
        ),
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
        channel="api",
        priority=80,
        task_config={"execution_mode": execution_mode},
        page_sampling_policy={},
        loop_guard={},
        idempotency_key=f"web-{default_workflow_version}-{execution_mode}",
        submission_kind=submission_kind,
    )

    command = repository.commands[0]
    assert command.task_config["workflow_version"] == default_workflow_version
    assert command.task_config["execution_mode"] == execution_mode
    assert command.request_payload["submission_kind"] == submission_kind
    if has_target_snapshot:
        assert command.task_config["_task_target"] == {
            "platform": "web",
            "target_url": "https://example.test",
            "app": None,
            "web": {
                "browser_profile_id": "desktop_fhd_1920x1080_v1",
                "adult_self_attestation": None,
            },
        }
        assert command.task_config["web_runtime_version"] == "web-runtime.v2"
    else:
        assert "_task_target" not in command.task_config
        assert "web_runtime_version" not in command.task_config


class _ProjectionRepository:
    def __init__(self, record: AgentRunRecord) -> None:
        self.record = record

    async def load_pending_run(self, **kwargs):
        assert kwargs == {"submission_id": SUBMISSION_ID, "task_id": TASK_ID}
        return self.record

    async def list_pending_runs(self, **kwargs):
        assert kwargs["minimum_age_seconds"] == 1
        return (self.record,)


async def test_projection_and_reconciler_restore_only_the_same_run_id() -> None:
    record = AgentRunRecord(
        run_id=RUN_ID,
        task_id=TASK_ID,
        submitted_by_user_id=ACCESS_SCOPE.actor_user_id,
        tenant_id=ACCESS_SCOPE.tenant_id,
        message="执行任务",
        submitted_at=NOW,
    )
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisAgentRunQueue(redis_client, ttl_seconds=3600)
    service = TaskSubmissionProjectionService(
        repository=_ProjectionRepository(record),  # type: ignore[arg-type]
        queue=queue,
    )

    assert await service.project(submission_id=SUBMISSION_ID, task_id=TASK_ID) is True
    assert await service.project(submission_id=SUBMISSION_ID, task_id=TASK_ID) is True
    assert await queue.dequeue(timeout=0.01) == RUN_ID
    assert await queue.dequeue(timeout=0.01) is None

    assert await service.reconcile(minimum_age_seconds=1) == 1
    assert await queue.dequeue(timeout=0.01) == RUN_ID
    restored = await queue.get(RUN_ID)
    assert restored is not None
    assert restored.task_id == TASK_ID


def test_task_submission_migration_is_single_successor_and_fail_closed() -> None:
    path = (
        Path(__file__).parents[1]
        / "migrations/versions/20260818_0068_task_submission_idempotency.py"
    )
    migration = runpy.run_path(str(path))
    source = path.read_text(encoding="utf-8")

    assert migration["revision"] == "20260818_0068"
    assert migration["down_revision"] == "20260818_0067"
    assert "qc_task_submission" in source
    assert "uq_qc_task_submission_idempotency" in source
    assert "cannot downgrade task submission idempotency while facts exist" in source
