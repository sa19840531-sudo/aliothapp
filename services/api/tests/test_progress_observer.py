"""旁路过程观察者的单元测试：Tool 事件 → 日志/节点/识别指标写入。

用假 Store 断言观察者产生的写入序列；观察者必须完全旁路——事件构造异常、写库
异常都不允许上抛。
"""

import asyncio
from types import SimpleNamespace
from typing import Any

from agent_framework import tool

from services.api.app.agents.runtime.agent_factory import build_main_agent, run_main_agent
from services.api.app.agents.runtime.progress_observer import (
    TaskProgressObserverMiddleware,
    TaskProgressRecorder,
    build_task_progress_recorder,
)
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.core.config import AgentRuntimeSettings
from services.api.tests.support.fake_chat_client import (
    ScriptedChatClient,
    make_function_call_response,
    make_text_response,
)


class FakeProgressStore:
    def __init__(self) -> None:
        self.logs: list[dict[str, Any]] = []
        self.journey_nodes: list[dict[str, Any]] = []
        self.attached: list[dict[str, Any]] = []
        self.candidate_totals: list[dict[str, Any]] = []
        self.current_rules: list[dict[str, Any]] = []
        self.detections: list[dict[str, Any]] = []
        self.element_counts: list[dict[str, Any]] = []
        self.exploration_checkpoints: list[dict[str, Any]] = []
        self.finalized: list[str] = []
        self.dom_path: str | None = None

    async def insert_log(self, **kwargs: Any) -> None:
        self.logs.append(kwargs)

    async def ensure_journey_nodes(self, **kwargs: Any) -> None:
        self.journey_nodes.append(kwargs)

    async def attach_capture(self, **kwargs: Any) -> None:
        self.attached.append(kwargs)

    async def set_candidate_total(self, **kwargs: Any) -> None:
        self.candidate_totals.append(kwargs)

    async def set_current_rule(self, **kwargs: Any) -> None:
        self.current_rules.append(kwargs)

    async def finalize_run_nodes(self, **kwargs: Any) -> None:
        self.finalized.append(kwargs["agent_run_id"])

    async def checkpoint_page_exploration(self, **kwargs: Any) -> None:
        self.exploration_checkpoints.append(kwargs)

    async def update_capture_detection(self, **kwargs: Any) -> None:
        self.detections.append(kwargs)

    async def update_capture_element_count(self, **kwargs: Any) -> None:
        self.element_counts.append(kwargs)

    async def fetch_capture_dom_path(self, **kwargs: Any) -> str | None:
        return self.dom_path


class FakeEvidenceStore:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def read(self, storage_path: str) -> bytes:
        return self.content


def _recorder(store: FakeProgressStore, **kwargs: Any) -> TaskProgressRecorder:
    return TaskProgressRecorder(
        store=store,
        task_id="task-1",
        agent_run_id="run-1",
        task_config=kwargs.pop("task_config", {}),
        evidence_store_factory=kwargs.pop("evidence_store_factory", None),
    )


async def test_run_started_materializes_journey_and_logs() -> None:
    store = FakeProgressStore()
    recorder = _recorder(store, task_config={"journey_steps": ["首页", "支付", "外星页面"]})
    await recorder.on_run_started()
    assert len(store.journey_nodes) == 1
    steps = store.journey_nodes[0]["steps"]
    assert [(step.step_name, step.page_type) for step in steps] == [
        ("首页", "home"),
        ("支付", "payment"),
        ("外星页面", None),
    ]
    messages = [entry["message"] for entry in store.logs]
    assert any("任务启动成功" in message for message in messages)
    assert any("首页 → 支付 → 外星页面" in message for message in messages)


async def test_detection_event_updates_capture_and_node() -> None:
    store = FakeProgressStore()
    recorder = _recorder(store)
    recorder.after_tool(
        "page_detection_tool",
        {"page_capture_id": "cap-1"},
        {
            "page_capture_id": "cap-1",
            "detection_status": "recognized",
            "page_type": "home",
            "confidence": 0.987,
            "confidence_source": "deterministic_structure",
        },
        1.234,
        None,
    )
    await recorder.drain()
    assert store.detections == [
        {
            "page_capture_id": "cap-1",
            "detection_status": "recognized",
            "page_type": "home",
            "confidence": 0.987,
            "duration_ms": 1234,
        }
    ]
    assert store.attached[0]["page_capture_id"] == "cap-1"
    assert store.attached[0]["recognition_status"] == "recognized"
    assert store.attached[0]["step_name"] == "首页"
    assert any(
        "页面识别成功" in entry["message"] and entry["level"] == "INFO"
        for entry in store.logs
    )
    assert any("结构规则兜底置信度 98.7%" in entry["message"] for entry in store.logs)


async def test_detection_failure_logs_warning_and_marks_failed() -> None:
    store = FakeProgressStore()
    recorder = _recorder(store)
    recorder.after_tool(
        "page_detection_tool",
        {},
        {
            "page_capture_id": "cap-2",
            "detection_status": "insufficient",
            "page_type": None,
            "confidence": 0,
            "reason": "证据不完整",
        },
        0.5,
        None,
    )
    await recorder.drain()
    assert store.attached[0]["recognition_status"] == "failed"
    assert any(entry["level"] == "WARN" for entry in store.logs)


async def test_candidate_rules_event_sets_total_and_attaches_node() -> None:
    store = FakeProgressStore()
    recorder = _recorder(store)
    recorder.after_tool(
        "candidate_rules_tool",
        {"page_type": "home", "page_capture_id": "cap-1"},
        {
            "page_capture_id": "cap-1",
            "page_type": "home",
            "rules": [{"rule_code": "alioth_2"}, {"rule_code": "alioth_4"}],
            "execution_plan": {"batches": [{"batch_id": "b1"}]},
        },
        0.2,
        None,
    )
    await recorder.drain()
    assert store.candidate_totals == [
        {"agent_run_id": "run-1", "page_capture_id": "cap-1", "total": 2}
    ]
    assert any("候选规则 2 条" in entry["message"] for entry in store.logs)


async def test_rule_result_log_levels_follow_severity() -> None:
    store = FakeProgressStore()
    recorder = _recorder(store)
    recorder.after_tool(
        "record_rule_execution_tool",
        {"severity": "critical", "problem_desc": "页面价格 299 接口价格 199"},
        {
            "execution_id": "exec-1",
            "rule_code": "alioth_13",
            "execution_status": "fail",
            "page_capture_id": "cap-1",
        },
        0.1,
        None,
    )
    recorder.after_tool(
        "record_rule_execution_tool",
        {},
        {
            "execution_id": "exec-2",
            "rule_code": "alioth_2",
            "execution_status": "pass",
            "page_capture_id": "cap-1",
        },
        0.1,
        None,
    )
    await recorder.drain()
    levels = {entry["message"].split(":")[0]: entry["level"] for entry in store.logs}
    assert levels.get("规则高风险") == "ERROR"
    assert levels.get("规则通过") == "INFO"
    assert store.logs[0]["rule_execution_id"] == "exec-1"


async def test_batch_record_event_emits_summary() -> None:
    store = FakeProgressStore()
    recorder = _recorder(store)
    recorder.after_tool(
        "record_rule_executions_batch_tool",
        {
            "executions": [
                {"rule_code": "alioth_2", "severity": "low"},
                {"rule_code": "alioth_4", "severity": "high", "problem_desc": "错别字"},
            ]
        },
        {
            "execution_status": "success",
            "page_capture_id": "cap-1",
            "succeeded_count": 2,
            "failed_count": 0,
            "items": [
                {
                    "status": "ok",
                    "execution_id": "exec-1",
                    "rule_code": "alioth_2",
                    "execution_status": "pass",
                },
                {
                    "status": "ok",
                    "execution_id": "exec-2",
                    "rule_code": "alioth_4",
                    "execution_status": "fail",
                },
            ],
        },
        0.3,
        None,
    )
    await recorder.drain()
    summary = [entry for entry in store.logs if "页面规则检查完成" in entry["message"]]
    assert len(summary) == 1
    assert "1/2 通过" in summary[0]["message"]
    # 批次完成后清空"正在执行规则"指针。
    assert store.current_rules[-1] == {
        "agent_run_id": "run-1",
        "page_capture_id": "cap-1",
        "rule_code": None,
    }


async def test_element_count_reads_dom_and_updates_capture() -> None:
    store = FakeProgressStore()
    store.dom_path = "task/cap-1/dom.html"
    dom = b"<html><a href=1></a><BUTTON></BUTTON><img src=x><div></div></html>"
    recorder = _recorder(store, evidence_store_factory=lambda: FakeEvidenceStore(dom))
    recorder.after_tool(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com"},
        {
            "execution_status": "success",
            "page_capture_id": "cap-1",
            "title": "示例站",
            "mode": "goto",
        },
        0.8,
        None,
    )
    await recorder.drain()
    assert store.element_counts == [{"page_capture_id": "cap-1", "element_count": 3}]
    assert any("页面采集完成" in entry["message"] for entry in store.logs)


async def test_tool_result_checkpoints_dynamic_frontier() -> None:
    store = FakeProgressStore()
    recorder = _recorder(store)
    working_memory = {
        "schema_version": 2,
        "current_page": None,
        "exploration_frontier": {
            "pending_actions": [
                {
                    "exploration_action_id": "exploration-2",
                    "action": "click",
                    "status": "candidate",
                }
            ]
        },
    }

    recorder.after_tool(
        "page_detection_tool",
        {"page_capture_id": "cap-1"},
        {
            "page_capture_id": "cap-1",
            "detection_status": "recognized",
            "page_type": "home",
            "working_memory": working_memory,
        },
        0.2,
        None,
    )
    await recorder.drain()

    assert store.exploration_checkpoints == [
        {
            "task_id": "task-1",
            "agent_run_id": "run-1",
            "working_memory": working_memory,
        }
    ]


async def test_run_finish_checkpoints_supervisor_terminalization() -> None:
    store = FakeProgressStore()
    recorder = _recorder(store)
    working_memory = {
        "schema_version": 2,
        "terminal_pages": [
            {
                "page_capture_id": "cap-1",
                "inspection_status": "degraded",
                "failure": {"code": "CONTINUATION_LIMIT_REACHED"},
            }
        ],
        "exploration_frontier": {"inactive_actions": []},
    }

    await recorder.on_run_finished(
        status="failed",
        termination_reason="incomplete",
        error_message=None,
        working_memory=working_memory,
    )

    assert store.exploration_checkpoints[-1]["working_memory"] == working_memory
    assert store.finalized == ["run-1"]


async def test_final_checkpoint_failure_does_not_block_node_finalization() -> None:
    class CheckpointFailureStore(FakeProgressStore):
        async def checkpoint_page_exploration(self, **kwargs: Any) -> None:
            raise RuntimeError("checkpoint unavailable")

    store = CheckpointFailureStore()
    recorder = _recorder(store)

    await recorder.on_run_finished(
        status="failed",
        termination_reason="incomplete",
        error_message=None,
        working_memory={"schema_version": 2},
    )

    assert store.finalized == ["run-1"]


def test_checkpoint_construction_failure_does_not_escape_after_tool() -> None:
    store = FakeProgressStore()
    store.checkpoint_page_exploration = None  # type: ignore[method-assign]
    recorder = _recorder(store)

    recorder.after_tool(
        "echo_tool",
        {},
        {"working_memory": {"schema_version": 2}},
        0.1,
        None,
    )


async def test_store_failures_never_propagate() -> None:
    class ExplodingStore(FakeProgressStore):
        async def insert_log(self, **kwargs: Any) -> None:
            raise RuntimeError("db down")

        async def finalize_run_nodes(self, **kwargs: Any) -> None:
            raise RuntimeError("db down")

    recorder = TaskProgressRecorder(
        store=ExplodingStore(), task_id="task-1", agent_run_id="run-1"
    )
    recorder.before_tool("rule_judge_tool", {"rule_code": "alioth_6"})
    recorder.after_tool("report_generation_tool", {}, {"ok": True}, 0.1, None)
    await recorder.on_run_finished(
        status="failed", termination_reason="error", error_message="boom"
    )


async def test_middleware_is_transparent_and_reports_events() -> None:
    store = FakeProgressStore()
    recorder = _recorder(store)
    middleware = TaskProgressObserverMiddleware(recorder)

    class FakeFunction:
        name = "report_generation_tool"

    class FakeContext:
        function = FakeFunction()
        arguments = {"foo": "bar"}
        result = '{"report_stage": "preview"}'

    called = False

    async def call_next() -> None:
        nonlocal called
        called = True

    await middleware.process(FakeContext(), call_next)
    await recorder.drain()
    assert called is True
    assert any("质检报告已生成" in entry["message"] for entry in store.logs)




async def test_bound_working_memory_checkpoint_keeps_full_frontier_when_result_is_compacted(
) -> None:
    store = FakeProgressStore()
    recorder = _recorder(store)
    middleware = TaskProgressObserverMiddleware(recorder)
    memory = WorkingMemory()
    action_id = memory.seed_entry_exploration_action(
        action="goto",
        target="https://example.com",
        navigation_depth=0,
    )
    memory.remember_page_capture(
        page_capture_id="cap-1",
        page_url="https://example.com",
        page_title="首页",
        page_seq=1,
        artifact_ids=["artifact-1"],
        exploration_action_id=action_id,
    )
    middleware.bind_working_memory(memory)

    class FakeFunction:
        name = "page_capture_tool"

    class FakeContext:
        function = FakeFunction()
        arguments = {"mode": "goto"}
        result = None

    context = FakeContext()

    async def call_next() -> None:
        context.result = {
            "execution_status": "success",
            "page_capture_id": "cap-1",
            "working_memory": {
                "schema_version": 2,
                "exploration_frontier": {
                    "pending_actions": [],
                    "pending_action_count": 0,
                    "explored_action_count": 1,
                    "inactive_action_count": 0,
                },
                "compacted": True,
            },
        }

    await middleware.process(context, call_next)
    await recorder.drain()

    checkpoint = store.exploration_checkpoints[0]["working_memory"]
    explored = checkpoint["exploration_frontier"]["explored_actions"]
    assert explored[0]["exploration_action_id"] == action_id
    assert explored[0]["status"] == "explored"

async def test_real_agent_middleware_receives_page_working_memory_checkpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.api.app.agents.runtime.agent_factory.get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(pipeline_mode="off")
        ),
    )
    @tool(name="page_capture_tool", approval_mode="never_require")
    def fake_page_capture_tool(
        mode: str,
        url: str | None = None,
        exploration_action_id: str | None = None,
        navigation_depth: int | None = None,
    ) -> dict:
        """真实 Agent 中间件链测试用页面采集。"""
        return {
            "execution_status": "success",
            "mode": mode,
            "page_capture_id": "cap-1",
            "final_url": "https://example.com",
            "title": "首页",
            "page_seq": 1,
            "artifact_ids": ["artifact-1"],
        }

    store = FakeProgressStore()
    recorder = _recorder(store)
    settings = AgentRuntimeSettings(
        max_steps=10,
        max_iterations=10,
        max_duration_seconds=5,
        max_continuation_attempts=0,
    )
    client = ScriptedChatClient(
        [
            make_function_call_response("1", "page_capture_tool", '{"mode":"goto","url":"https://example.com"}'),
            make_text_response("done"),
        ]
    )
    agent = build_main_agent(
        client=client,
        instructions="test",
        runtime_settings=settings,
        tools=[fake_page_capture_tool],
    )

    await run_main_agent(
        agent,
        "目标URL：https://example.com",
        runtime_settings=settings,
        extra_middleware=[TaskProgressObserverMiddleware(recorder)],
    )
    await recorder.drain()

    live_snapshot = store.exploration_checkpoints[0]["working_memory"]
    assert live_snapshot["schema_version"] == 2
    assert live_snapshot["current_page"]["page_capture_id"] == "cap-1"
    assert live_snapshot["exploration_frontier"]["explored_action_count"] == 1


async def test_middleware_propagates_tool_exception_after_logging() -> None:
    store = FakeProgressStore()
    recorder = _recorder(store)
    middleware = TaskProgressObserverMiddleware(recorder)

    class FakeFunction:
        name = "page_capture_tool"

    class FakeContext:
        function = FakeFunction()
        arguments = {}
        result = None

    async def call_next() -> None:
        raise ValueError("browser crashed")

    try:
        await middleware.process(FakeContext(), call_next)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("工具异常必须原样上抛")
    await recorder.drain()
    assert any(entry["level"] == "ERROR" for entry in store.logs)


def test_builder_disabled_without_postgres_backend(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", raising=False)
    monkeypatch.setenv("ALIOTH_DATABASE_URL", "postgresql://localhost/db")
    assert (
        build_task_progress_recorder(task_id="t", agent_run_id="r", task_config={})
        is None
    )
    monkeypatch.setenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", "postgres")
    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)
    assert (
        build_task_progress_recorder(task_id="t", agent_run_id="r", task_config={})
        is None
    )
    monkeypatch.setenv("ALIOTH_DATABASE_URL", "postgresql://localhost/db")
    recorder = build_task_progress_recorder(
        task_id="t", agent_run_id="r", task_config={}
    )
    assert recorder is not None
    assert (
        build_task_progress_recorder(task_id=None, agent_run_id="r", task_config={})
        is None
    )


async def test_no_running_loop_is_safe() -> None:
    """事件在无事件循环环境下静默丢弃（同 LiveActionTraceSink 语义）。"""
    store = FakeProgressStore()
    recorder = _recorder(store)

    def sync_call() -> None:
        recorder.before_tool("rule_judge_tool", {"rule_code": "alioth_6"})

    await asyncio.to_thread(sync_call)
    await recorder.drain()
    assert store.logs == []


async def test_app_exploration_aborted_log_is_written() -> None:
    """App 因错误未跑完探索时，执行日志必须留下可检索中断记录。"""
    store = FakeProgressStore()
    recorder = _recorder(store)

    recorder.on_app_exploration_aborted(
        phase="bootstrap",
        reason_code="OFFICIAL_API_TIMEOUT",
        error_type="AppWorkflowFailure",
        error_message="Official App 截图调用失败",
        retryable=True,
        consumed_turns=0,
        consumed_actions=0,
    )
    await recorder.drain()

    assert any(
        entry["level"] == "ERROR"
        and "App 探索未跑完" in entry["message"]
        and "OFFICIAL_API_TIMEOUT" in entry["message"]
        and entry.get("context", {}).get("event") == "app_exploration_aborted"
        for entry in store.logs
    )


async def test_app_exploration_turn_logs_are_written() -> None:
    """正式 App 质检探索轮次必须写入 qc_task_log 可读事件。"""
    store = FakeProgressStore()
    recorder = _recorder(store)

    recorder.on_app_exploration_published(
        objective="按 Playbook 探索首页与底部 Tab",
        max_actions=28,
        deadline_seconds=900,
    )
    recorder.on_app_exploration_turn_started(turn_index=1, work_type="exploration")
    recorder.on_app_exploration_turn_finished(
        turn_index=1,
        work_type="exploration",
        claimed=True,
        work_id="work-1",
        status="succeeded",
        action_type="click",
        purpose="点击底部首页",
        semantic_target="tab:home",
        result_capture_id="cap-after-1",
        successor_work_id="work-2",
    )
    recorder.on_app_exploration_turn_finished(
        turn_index=2,
        work_type="exploration",
        claimed=True,
        work_id="work-2",
        status="failed",
        error_code="APP_EXPLORATION_TURN_FAILED",
    )
    recorder.on_app_exploration_turn_finished(
        turn_index=3,
        work_type="probe",
        claimed=False,
    )
    await recorder.drain()

    messages = [entry["message"] for entry in store.logs]
    assert any("已发布 App 截图探索" in message for message in messages)
    assert any("开始探索第 1 轮" in message for message in messages)
    assert any("探索第 1 轮结束" in message and "click" in message for message in messages)
    assert any(
        entry["level"] == "ERROR" and "APP_EXPLORATION_TURN_FAILED" in entry["message"]
        for entry in store.logs
    )
    assert any("补采 Probe" in message and "队列已空" in message for message in messages)
    finished = next(
        entry
        for entry in store.logs
        if entry.get("context", {}).get("event") == "app_exploration_turn_finished"
        and entry["context"].get("work_id") == "work-1"
    )
    assert finished["page_capture_id"] == "cap-after-1"
    assert finished["context"]["successor_work_id"] == "work-2"
