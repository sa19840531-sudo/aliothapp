"""动态 Frontier 与页面失败补偿持久化测试。"""

from typing import Any
from uuid import uuid4

from services.api.app.infrastructure.db.page_exploration_repository import (
    PostgresPageExplorationRepository,
)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((" ".join(query.split()), args))
        return "UPDATE 1"


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.acquire_count = 0

    def acquire(self) -> FakeAcquire:
        self.acquire_count += 1
        return FakeAcquire(self.connection)


async def test_checkpoint_upserts_frontier_and_compensates_failed_page() -> None:
    task_id = str(uuid4())
    run_id = str(uuid4())
    source_capture_id = str(uuid4())
    result_capture_id = str(uuid4())
    pool = FakePool()
    repository = PostgresPageExplorationRepository(pool=pool)
    snapshot = {
        "schema_version": 2,
        "current_page": None,
        "terminal_pages": [
            {
                "page_capture_id": source_capture_id,
                "supersedes_page_capture_id": None,
                "lifecycle_status": "active",
                "inspection_status": "degraded",
                "page_detection": {"detection_status": "recognized"},
                "abandon_reason": None,
                "failure": {
                    "stage": "rule_persistence",
                    "code": "RULE_EXECUTION_WRITE_FAILED",
                    "message": "database unavailable",
                    "retryable": True,
                },
            }
        ],
        "inactive_pages": [
            {
                "page_capture_id": result_capture_id,
                "supersedes_page_capture_id": source_capture_id,
                "lifecycle_status": "abandoned",
                "inspection_status": "matching",
                "abandon_reason": "page_type_excluded",
                "failure": None,
            }
        ],
        "exploration_frontier": {
            "pending_actions": [],
            "explored_actions": [
                {
                    "exploration_action_id": "exploration-1",
                    "source_page_capture_id": None,
                    "source_page_type": None,
                    "action": "goto",
                    "origin": "entry",
                    "target": "https://example.com",
                    "target_keywords": [],
                    "risk_level": None,
                    "purpose": None,
                    "expected_next_page_type": None,
                    "navigation_depth": 0,
                    "status": "explored",
                    "result_page_capture_id": source_capture_id,
                    "attempt_count": 1,
                }
            ],
            "inactive_actions": [
                {
                    "exploration_action_id": "exploration-2",
                    "source_page_capture_id": source_capture_id,
                    "source_page_type": "home",
                    "action": "click",
                    "origin": "discovered",
                    "target": "settings",
                    "target_keywords": ["设置"],
                    "status": "failed",
                    "result_page_capture_id": None,
                    "failure_reason": "page_capture_failed",
                    "failure_code": "PAGE_CAPTURE_FAILED",
                    "failure_message": "browser unavailable",
                    "failure_retryable": True,
                    "attempt_count": 2,
                }
            ],
        },
    }

    await repository.checkpoint(
        task_id=task_id,
        agent_run_id=run_id,
        working_memory=snapshot,
    )

    calls = pool.connection.calls
    action_calls = [call for call in calls if "INSERT INTO qc_page_exploration_action" in call[0]]
    capture_calls = [call for call in calls if "UPDATE qc_page_capture" in call[0]]
    node_calls = [call for call in calls if "UPDATE qc_task_page_node" in call[0]]
    assert pool.acquire_count == 1
    assert len(action_calls) == 2
    assert len(capture_calls) == 2
    assert len(node_calls) == 2
    assert "ON CONFLICT (agent_run_id, exploration_action_id)" in action_calls[0][0]
    assert action_calls[1][1][2] == "exploration-2"
    assert action_calls[1][1][13] == "failed"
    assert action_calls[1][1][16] == "PAGE_CAPTURE_FAILED"
    assert capture_calls[0][1][5] == "degraded"
    assert capture_calls[0][1][7] == "RULE_EXECUTION_WRITE_FAILED"
    assert capture_calls[1][1][3] == source_capture_id
    assert capture_calls[1][1][4] == "abandoned"
    assert capture_calls[1][1][10] == "page_type_excluded"
    assert node_calls[0][1][3] == "recognized"
    assert "THEN 'recognized'" in node_calls[0][0]


async def test_empty_checkpoint_does_not_open_database_connection() -> None:
    pool = FakePool()
    repository = PostgresPageExplorationRepository(pool=pool)

    await repository.checkpoint(
        task_id=str(uuid4()),
        agent_run_id=str(uuid4()),
        working_memory={"schema_version": 2},
    )

    assert pool.acquire_count == 0
    assert pool.connection.calls == []


async def test_checkpoint_preserves_recovery_action_origin() -> None:
    pool = FakePool()
    repository = PostgresPageExplorationRepository(pool=pool)

    await repository.checkpoint(
        task_id=str(uuid4()),
        agent_run_id=str(uuid4()),
        working_memory={
            "exploration_frontier": {
                "explored_actions": [
                    {
                        "exploration_action_id": "exploration-recovery",
                        "action": "goto",
                        "origin": "recovery",
                        "target": "https://example.com/source",
                        "target_keywords": ["restore_source_action:exploration-2"],
                        "status": "explored",
                        "attempt_count": 1,
                    }
                ]
            }
        },
    )

    action_call = next(
        call
        for call in pool.connection.calls
        if "INSERT INTO qc_page_exploration_action" in call[0]
    )
    assert action_call[1][6] == "recovery"


async def test_checkpoint_rebinds_page_node_to_latest_supplemental_capture() -> None:
    task_id = str(uuid4())
    run_id = str(uuid4())
    source_capture_id = str(uuid4())
    result_capture_id = str(uuid4())
    pool = FakePool()
    repository = PostgresPageExplorationRepository(pool=pool)
    snapshot = {
        "terminal_pages": [
            {
                "page_capture_id": result_capture_id,
                "supersedes_page_capture_id": source_capture_id,
                "exploration_action_id": "scroll-1",
                "lifecycle_status": "active",
                "inspection_status": "completed",
            }
        ],
        "exploration_frontier": {
            "explored_actions": [
                {
                    "exploration_action_id": "scroll-1",
                    "source_page_capture_id": source_capture_id,
                    "source_page_type": "content_list",
                    "action": "scroll",
                    "origin": "capture_plan",
                    "status": "explored",
                    "result_page_capture_id": result_capture_id,
                }
            ]
        },
    }

    await repository.checkpoint(
        task_id=task_id,
        agent_run_id=run_id,
        working_memory=snapshot,
    )

    node_calls = [
        call
        for call in pool.connection.calls
        if "UPDATE qc_task_page_node" in call[0]
    ]
    rebind_query, rebind_args = next(
        call for call in node_calls if "page_capture_id = $2::uuid" in call[0]
    )
    assert "page_capture_id = $3::uuid" in rebind_query
    assert rebind_args == (run_id, result_capture_id, source_capture_id)
