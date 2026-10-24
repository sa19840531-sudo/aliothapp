"""持久化质检工作项中间件测试：常开发布与 Enforced 消费门禁。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

from services.api.app.agents.runtime.business_middlewares.base import (
    TaskRuntimeSnapshot,
)
from services.api.app.agents.runtime.business_middlewares.factory import (
    create_default_business_middlewares,
)
from services.api.app.agents.runtime.business_middlewares.inspection_work_item_middleware import (
    InspectionWorkItemMiddleware,
)
from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
)
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.application.inspection_work_items import (
    CompletedInspectionWorkItemResult,
    InspectionWorkItemExecutionLink,
    InspectionWorkItemLease,
    InspectionWorkItemRule,
    InspectionWorkItemStatus,
    PublishedInspectionWorkItem,
)

_SETTINGS_TARGET = (
    "services.api.app.agents.runtime.business_middlewares."
    "inspection_work_item_middleware.get_settings"
)


def _fake_settings():
    return SimpleNamespace(
        inspection_work_items=SimpleNamespace(
            max_attempts=3,
            lease_seconds=180,
            retry_base_seconds=2.0,
        )
    )


class FakeProvider:
    async def get_task_snapshot(self, task_id: str) -> TaskRuntimeSnapshot:
        return TaskRuntimeSnapshot(
            task_id=task_id,
            status="running",
            ruleset_version=3,
            ruleset_id="ruleset-1",
            ruleset_snapshot_hash="snapshot-3",
            priority=20,
        )


class FakeService:
    def __init__(self) -> None:
        self.commands = []
        self.publish_error: Exception | None = None
        self.claims = []
        self.lease = None
        self.completed_result = None
        self.nonterminal = False

    async def publish(self, command):
        if self.publish_error is not None:
            raise self.publish_error
        self.commands.append(command)
        return PublishedInspectionWorkItem(
            work_item_id=f"work-{len(self.commands)}",
            status=InspectionWorkItemStatus.PENDING,
            input_hash=f"hash-{len(self.commands)}",
            created=True,
        )

    async def claim(self, command):
        self.claims.append(command)
        return self.lease

    async def find_completed_result(self, command):
        self.completed_command = command
        return self.completed_result

    async def has_nonterminal_work(self, *, task_id):
        return self.nonterminal


def _context(tool_name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        function=SimpleNamespace(name=tool_name),
        arguments=arguments,
        result=None,
    )


def _candidate_result() -> dict:
    return {
        "task_id": "task-1",
        "page_capture_id": "capture-1",
        "page_type": "home",
        "rules": [
            {
                "rule_id": "rule-1",
                "rule_code": "alioth_1",
                "category": "visual",
                "executor_type": "hybrid",
                "severity": "medium",
                "check_point": "检查视觉内容",
                "input_contract": {"required_artifacts": ["screenshot"]},
                "judge_config": {},
                "manual_review_policy": {},
                "execution_route": {
                    "strategy": "hybrid",
                    "session_dependency": "snapshot",
                },
            },
            {
                "rule_id": "rule-2",
                "rule_code": "alioth_2",
                "category": "interaction",
                "executor_type": "hybrid",
                "severity": "high",
                "check_point": "检查点击旅程",
                "input_contract": {"required_artifacts": ["action_trace"]},
                "judge_config": {},
                "manual_review_policy": {},
                "execution_route": {
                    "strategy": "hybrid",
                    "session_dependency": "active_session",
                },
            },
        ],
        "execution_plan": {
            "batches": [
                {
                    "batch_id": "hybrid:visual:snapshot",
                    "strategy": "hybrid",
                    "category": "visual",
                    "session_dependency": "snapshot",
                    "rule_codes": ["alioth_1"],
                },
                {
                    "batch_id": "hybrid:interaction:active_session",
                    "strategy": "hybrid",
                    "category": "interaction",
                    "session_dependency": "active_session",
                    "rule_codes": ["alioth_2"],
                },
            ]
        },
    }


async def test_publishes_execution_plan_batches_as_enforced(monkeypatch) -> None:
    monkeypatch.setattr(_SETTINGS_TARGET, _fake_settings)
    service = FakeService()
    middleware = InspectionWorkItemMiddleware(FakeProvider(), service=service)
    context = _context(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "capture-1"},
    )

    async def _next() -> None:
        context.result = _candidate_result()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await middleware.process(context, _next)

    assert len(service.commands) == 2
    assert {command.control_mode for command in service.commands} == {"enforced"}
    assert service.commands[0].ruleset_version == 3
    assert service.commands[0].priority == 20
    assert service.commands[0].session_owner_agent_run_id is None
    assert service.commands[1].session_owner_agent_run_id == "run-1"
    assert context.result["inspection_work_items"]["published_count"] == 2


async def test_publish_failure_blocks_candidate_result(monkeypatch) -> None:
    monkeypatch.setattr(_SETTINGS_TARGET, _fake_settings)
    service = FakeService()
    service.publish_error = RuntimeError("database unavailable")
    middleware = InspectionWorkItemMiddleware(FakeProvider(), service=service)
    context = _context("candidate_rules_tool", {})

    async def _next() -> None:
        context.result = _candidate_result()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await middleware.process(context, _next)

    assert context.result["execution_status"] == "blocked"
    assert context.result["error_code"] == "INSPECTION_WORK_ITEM_PUBLISH_FAILED"
    assert context.result["retryable"] is True


async def test_large_execution_plan_is_chunked_like_runtime_dispatch(monkeypatch) -> None:
    monkeypatch.setattr(_SETTINGS_TARGET, _fake_settings)
    rules = [
        {
            "rule_id": f"rule-{index}",
            "rule_code": f"alioth_{index}",
            "category": "visual",
            "executor_type": "hybrid",
            "severity": "medium",
            "check_point": f"检查视觉内容 {index}",
            "input_contract": {"required_artifacts": ["screenshot"]},
            "judge_config": {},
            "manual_review_policy": {},
            "execution_route": {
                "strategy": "hybrid",
                "session_dependency": "snapshot",
            },
        }
        for index in range(21)
    ]
    service = FakeService()
    middleware = InspectionWorkItemMiddleware(FakeProvider(), service=service)
    context = _context("candidate_rules_tool", {})

    async def _next() -> None:
        context.result = {
            "page_capture_id": "capture-1",
            "page_type": "home",
            "rules": rules,
            "execution_plan": {
                "batches": [
                    {
                        "batch_id": "hybrid:visual:snapshot",
                        "strategy": "hybrid",
                        "category": "visual",
                        "session_dependency": "snapshot",
                        "rule_codes": [rule["rule_code"] for rule in rules],
                    }
                ]
            },
        }

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await middleware.process(context, _next)

    assert [len(command.rules) for command in service.commands] == [20, 1]
    assert [command.batch_key for command in service.commands] == [
        "hybrid:visual:snapshot:chunk:1",
        "hybrid:visual:snapshot:chunk:2",
    ]


def test_factory_publishes_before_page_memory_compacts_candidate_result() -> None:
    middlewares = create_default_business_middlewares(
        working_memory=WorkingMemory(),
        task_state_provider=FakeProvider(),
    )
    names = [type(middleware).__name__ for middleware in middlewares]

    assert names.index("DuplicateSubmissionDedupeMiddleware") < names.index(
        "PageInspectionMemoryMiddleware"
    )
    assert names.index("PageInspectionMemoryMiddleware") < names.index(
        "InspectionWorkItemMiddleware"
    )
    assert names.index("InspectionWorkItemMiddleware") < names.index("ScopedToolFailureMiddleware")


async def test_claims_exact_rule_batch_before_judge(monkeypatch) -> None:
    monkeypatch.setattr(_SETTINGS_TARGET, _fake_settings)
    events = []

    class FakeAttempt:
        def __init__(self, **kwargs):
            self.lease = kwargs["lease"]

        async def start(self):
            events.append("started")

        async def recover_formal_results(self):
            return None

        @asynccontextmanager
        async def lease_heartbeat(self):
            events.append("heartbeat")
            try:
                yield
            finally:
                events.append("heartbeat_exit")

        async def finalize_after_tool(self, result):
            events.append(("finalized", result["execution_id"]))
            return result

        async def fail_incomplete(self, **kwargs):
            raise AssertionError(kwargs)

    monkeypatch.setattr(
        "services.api.app.agents.runtime.business_middlewares."
        "inspection_work_item_middleware.InspectionWorkItemAttempt",
        FakeAttempt,
    )
    service = FakeService()
    service.lease = InspectionWorkItemLease(
        work_item_id="work-1",
        task_id="task-1",
        page_capture_id="capture-1",
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        input_hash="input-1",
        producer_agent_run_id="producer-run-1",
        consumer_agent_run_id="consumer-run-1",
        lease_token="lease-1",
        attempt_no=1,
        row_version=1,
        rules=(
            InspectionWorkItemRule(
                rule_id="rule-1",
                rule_code="alioth_1",
                ordinal=0,
                contract_hash="contract-1",
            ),
        ),
    )
    middleware = InspectionWorkItemMiddleware(FakeProvider(), service=service)
    context = _context(
        "rule_judge_tool",
        {"page_capture_id": "capture-1", "rule_code": "alioth_1"},
    )

    async def _next() -> None:
        events.append("judge")
        context.result = {
            "execution_id": "execution-1",
            "execution_status": "pass",
            "page_capture_id": "capture-1",
            "rule_code": "alioth_1",
        }

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await middleware.process(context, _next)

    assert service.claims[0].page_capture_id == "capture-1"
    assert service.claims[0].rule_codes == ("alioth_1",)
    assert events == [
        "started",
        "heartbeat",
        "judge",
        ("finalized", "execution-1"),
        "heartbeat_exit",
    ]
    assert context.result["inspection_work_item"]["status"] == "completed"


async def test_recovers_completed_exact_batch_without_reinvoking_judge(monkeypatch) -> None:
    monkeypatch.setattr(_SETTINGS_TARGET, _fake_settings)
    service = FakeService()
    service.completed_result = CompletedInspectionWorkItemResult(
        work_item_id="work-1",
        page_capture_id="capture-1",
        links=(
            InspectionWorkItemExecutionLink(
                rule_id="rule-1",
                rule_code="alioth_1",
                execution_status="pass",
                observation_packet_id="packet-1",
                rule_execution_id="execution-1",
            ),
        ),
    )
    middleware = InspectionWorkItemMiddleware(FakeProvider(), service=service)
    context = _context(
        "rule_judge_tool",
        {"page_capture_id": "capture-1", "rule_code": "alioth_1"},
    )
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await middleware.process(context, _next)

    assert called is False
    assert service.completed_command.page_capture_id == "capture-1"
    assert service.completed_command.rule_codes == ("alioth_1",)
    assert context.result["execution_id"] == "execution-1"
    assert context.result["recovered"] is True
    assert context.result["inspection_work_item"] == {
        "work_item_id": "work-1",
        "status": "recovered",
    }


async def test_blocks_report_while_enforced_work_is_nonterminal(monkeypatch) -> None:
    monkeypatch.setattr(_SETTINGS_TARGET, _fake_settings)
    service = FakeService()
    service.nonterminal = True
    middleware = InspectionWorkItemMiddleware(FakeProvider(), service=service)
    context = _context("report_generation_tool", {})
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await middleware.process(context, _next)

    assert called is False
    assert context.result["error_code"] == "INSPECTION_WORK_ITEMS_NOT_TERMINAL"
