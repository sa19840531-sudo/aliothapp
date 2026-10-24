"""worker.py 的单元测试：验证 `_process_one` 在各种场景下正确写回 Redis 记录状态。

不依赖真实 LLM/Redis：`build_default_main_agent`/`run_main_agent` 均通过 monkeypatch
替换为假实现，Redis 队列使用 fakeredis 支撑的真实 `RedisAgentRunQueue`（与
test_redis_queue.py/test_agents_async_endpoints.py 的既有模式一致）。`Settings()` 直接
以全默认值构造，不依赖 `get_settings()`/真实环境变量，避免跨测试的 lru_cache 缓存污染。
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any

import fakeredis
import pytest

from services.api.app import worker as worker_module
from services.api.app.agents.runtime.action_trace import ActionTraceEntry, InMemoryActionTraceSink
from services.api.app.agents.runtime.models import (
    ErrorCategory,
    ExecutionOutcome,
    RunResult,
    TerminationReason,
)
from services.api.app.agents.runtime.usage_meter import record_current_llm_usage
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.application.app_metrics import (
    APP_RUN_RECOVERY_OUTCOME_TOTAL,
    app_metric_value,
)
from services.api.app.application.app_run_recovery import (
    AppRunExecutionLease,
    AppRunExecutionLeaseLostError,
    AppRunRecoverySnapshot,
)
from services.api.app.application.rule_execution import (
    CandidatePageReference,
    RuleExecutionFinalizationResult,
    RuntimePageInspectionFailureFallback,
    RuntimeRuleExecutionFallback,
)
from services.api.app.core.config import Settings, WorkflowSettings
from services.api.app.domain.task import (
    TaskExecutionMode,
    TaskExecutionStatus,
    TaskFinalizationSnapshot,
    TaskQualityOutcome,
)
from services.api.app.domain.task.status import TaskStatusConflictError
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    AgentRunRecord,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import (
    AgentRunCancellationConflict,
    AgentRunTerminalStateConflict,
    InMemoryAgentRunStateStore,
    PostgresAgentRunStateStore,
)


class FakeRuleExecutionWriter:
    def __init__(self, *, unresolved_candidate_count: int = 0) -> None:
        self.calls: list[
            tuple[
                str,
                str,
                object,
                list[CandidatePageReference],
                list[RuntimeRuleExecutionFallback],
                list[RuntimePageInspectionFailureFallback],
            ]
        ] = []
        self.unresolved_candidate_count = unresolved_candidate_count

    async def finalize_missing_rule_executions(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finished_at,
        candidate_pages,
        runtime_rule_fallbacks,
        runtime_page_failure_fallbacks,
    ) -> RuleExecutionFinalizationResult:
        self.calls.append(
            (
                task_id,
                agent_run_id,
                finished_at,
                candidate_pages,
                runtime_rule_fallbacks,
                runtime_page_failure_fallbacks,
            )
        )
        return RuleExecutionFinalizationResult(
            inserted_count=2,
            unresolved_candidate_count=self.unresolved_candidate_count,
        )


class FailingRuleExecutionWriter:
    async def finalize_missing_rule_executions(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finished_at,
        candidate_pages,
        runtime_rule_fallbacks,
        runtime_page_failure_fallbacks,
    ) -> RuleExecutionFinalizationResult:
        raise RuntimeError("规则执行结果写入失败")


class FakeFinalReportGenerator:
    def __init__(
        self,
        *,
        unresolved_candidate_count: int = 0,
        overall_status: str | None = None,
        candidate_execution_complete: bool | None = None,
    ) -> None:
        self.calls: list[tuple[str, str | None, object, str | None]] = []
        self.page_explorations: list[dict] = []
        self.termination_reasons: list[str | None] = []
        self.unresolved_candidate_count = unresolved_candidate_count
        self.overall_status = overall_status
        self.candidate_execution_complete = candidate_execution_complete

    async def generate_task_report(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        finalized_at=None,
        terminal_task_status: str | None = None,
        termination_reason: str | None = None,
        page_exploration: dict | None = None,
    ) -> dict:
        self.calls.append((task_id, agent_run_id, finalized_at, terminal_task_status))
        self.page_explorations.append(dict(page_exploration or {}))
        self.termination_reasons.append(termination_reason)
        overall_status = self.overall_status
        if terminal_task_status == "failed":
            overall_status = "error"
        elif terminal_task_status == "review" or self.unresolved_candidate_count > 0:
            overall_status = "incomplete"
        report = {
            "report_version": "v1",
            "report_stage": "final",
            "is_final": True,
            "task": {"status": terminal_task_status or "done"},
            "summary": {
                "unresolved_candidate_rules": self.unresolved_candidate_count,
                "overall_status": overall_status or "pass",
            },
            "page_exploration": page_exploration,
        }
        if self.candidate_execution_complete is not None:
            report["summary"]["candidate_execution_complete"] = self.candidate_execution_complete
        return report


class FakeTaskFinalizationService:
    def __init__(self, *, gate_passed: bool, bind_error: bool = False) -> None:
        self.gate_passed = gate_passed
        self.bind_error = bind_error
        self.calls: list[str] = []
        self.bind_commands = []
        self.finalize_commands = []
        self.pending_task_ids: set[str] = set()
        self.recoverable_run_ids: dict[str, str] = {}
        self.quarantined_task_ids: list[str] = []

    async def list_session_draining(self, *, limit: int = 100):
        self.calls.append(f"list_session_draining:{limit}")
        return [
            SimpleNamespace(
                task_id=task_id,
                agent_run_id=self.recoverable_run_ids[task_id],
            )
            for task_id in self.pending_task_ids
        ]

    async def claim_recoverable_finalizations(self, *, limit: int = 100, **_kwargs):
        snapshots = await self.list_session_draining(limit=limit)
        return [
            SimpleNamespace(
                task_id=item.task_id,
                agent_run_id=item.agent_run_id,
                snapshot=item,
                lease_token="00000000-0000-0000-0000-000000000099",
                recovery_generation=0,
                expected_status="running",
            )
            for item in snapshots
        ]

    async def defer_finalization_retry(self, **_kwargs):
        return None

    async def get(self, *, task_id: str):
        self.calls.append("get")
        return TaskFinalizationSnapshot(
            task_id=task_id,
            state=worker_module.TaskFinalizationState.OPEN,
            agent_run_id=self.recoverable_run_ids[task_id],
            finalization_epoch=0,
            row_version=0,
        )

    async def begin_exploration_draining(self, command):
        self.calls.append("exploration_draining")
        return TaskFinalizationSnapshot(
            task_id=command.task_id,
            state=worker_module.TaskFinalizationState.EXPLORATION_DRAINING,
            agent_run_id=command.agent_run_id,
            finalization_epoch=0,
            row_version=1,
        )

    async def begin_session_draining(self, command):
        self.calls.append("session_draining")
        return TaskFinalizationSnapshot(
            task_id=command.task_id,
            state=worker_module.TaskFinalizationState.SESSION_DRAINING,
            agent_run_id=command.agent_run_id,
            finalization_epoch=0,
            row_version=2,
        )

    async def evaluate_and_seal(self, command):
        self.calls.append(f"evaluate:{command.execution_status.value}")
        return SimpleNamespace(
            snapshot=SimpleNamespace(
                gate_passed=self.gate_passed,
                gate_snapshot_id="gate-1",
                finalization_epoch=1 if self.gate_passed else 0,
                block_reasons=(
                    () if self.gate_passed else (SimpleNamespace(value="pending_action"),)
                ),
            )
        )

    async def finalize_task_with_report(self, command):
        self.calls.append(f"finalize:{command.execution_status.value}")
        self.finalize_commands.append(command)
        if self.bind_error:
            raise RuntimeError("报告绑定暂时失败")
        return SimpleNamespace(
            snapshot=SimpleNamespace(
                gate_passed=self.gate_passed,
                gate_snapshot_id="gate-1",
                finalization_epoch=1 if self.gate_passed else 0,
                block_reasons=(
                    () if self.gate_passed else (SimpleNamespace(value="pending_action"),)
                ),
            )
        )

    async def bind_report(self, command):
        self.calls.append("bind_report")
        self.bind_commands.append(command)
        if self.bind_error:
            raise RuntimeError("报告绑定暂时失败")
        return SimpleNamespace()

    async def quarantine_missing_sealed_report(self, *, task_id: str, **_kwargs):
        self.quarantined_task_ids.append(task_id)


class FakeAppPublicationRepository:
    """App Worker 单测使用的 Publication 完成屏障。"""

    def __init__(
        self,
        *,
        expected_entries: int = 2,
        linked_work_items: int = 2,
        needs_review_count: int = 0,
        failed_count: int = 0,
    ) -> None:
        self.expected_entries = expected_entries
        self.linked_work_items = linked_work_items
        self.needs_review_count = needs_review_count
        self.failed_count = failed_count
        self.calls: list[str] = []

    async def await_task_terminal(self, *, task_id: str):
        self.calls.append(task_id)
        return SimpleNamespace(
            total_count=1,
            nonterminal_count=0,
            expected_entry_count=self.expected_entries,
            terminal_entry_count=self.expected_entries,
            linked_work_item_count=self.linked_work_items,
            explicit_exclusion_count=0,
            needs_review_count=self.needs_review_count,
            failed_count=self.failed_count,
        )


class FakeH5PublicationRepository:
    """H5 Worker 单测使用的 Publication 完成屏障。"""

    def __init__(
        self,
        *,
        published_work_items: int = 2,
        excluded_active_session_rules: int = 0,
        needs_review_count: int = 0,
        failed_count: int = 0,
    ) -> None:
        self.published_work_items = published_work_items
        self.excluded_active_session_rules = excluded_active_session_rules
        self.needs_review_count = needs_review_count
        self.failed_count = failed_count
        self.calls: list[tuple[str, int]] = []

    async def await_task_terminal(self, *, task_id: str, minimum_total_count: int = 0):
        self.calls.append((task_id, minimum_total_count))
        return SimpleNamespace(
            total_count=1,
            nonterminal_count=0,
            published_work_item_count=self.published_work_items,
            excluded_active_session_rule_count=self.excluded_active_session_rules,
            needs_review_count=self.needs_review_count,
            failed_count=self.failed_count,
        )


class RecoverableTaskFinalizationService(FakeTaskFinalizationService):
    def __init__(
        self,
        *,
        gate_passed: bool,
        snapshot_state: worker_module.TaskFinalizationState,
        bind_error: bool = False,
    ) -> None:
        super().__init__(gate_passed=gate_passed, bind_error=bind_error)
        self.snapshot_state = snapshot_state

    async def list_recoverable_finalizations(self, *, limit: int = 100):
        self.calls.append(f"list_recoverable:{limit}")
        return [
            SimpleNamespace(
                task_id=task_id,
                agent_run_id=self.recoverable_run_ids[task_id],
            )
            for task_id in self.pending_task_ids
        ]

    async def get(self, *, task_id: str):
        self.calls.append("get")
        is_sealed = self.snapshot_state is worker_module.TaskFinalizationState.SEALED
        return TaskFinalizationSnapshot(
            task_id=task_id,
            state=self.snapshot_state,
            agent_run_id=self.recoverable_run_ids[task_id],
            finalization_epoch=7 if is_sealed else 0,
            row_version=0,
            gate_snapshot_id="gate-sealed" if is_sealed else None,
            execution_status=TaskExecutionStatus.COMPLETED if is_sealed else None,
            quality_outcome=TaskQualityOutcome.PASS if is_sealed else None,
        )


class AtomicOnlyTaskFinalizationService(FakeTaskFinalizationService):
    async def evaluate_and_seal(self, command):
        del command
        raise AssertionError("Worker 不得再调用会先封存任务的旧接口")


def test_app_qc_configuration_rejects_noop_state_store_or_missing_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)
    monkeypatch.setenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", "noop")
    settings = Settings(workflow=WorkflowSettings(app_qc_enabled=True))

    with pytest.raises(ValueError, match="Completion Gate"):
        worker_module.validate_completion_gate_configuration(
            settings,
            state_store=InMemoryAgentRunStateStore(),
            task_finalization_service=None,
        )


def test_h5_v2_configuration_rejects_noop_state_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H5 V2 开启时，取消线性化不能退化为内存状态。"""

    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)
    monkeypatch.setenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", "noop")

    with pytest.raises(ValueError, match="Completion Gate"):
        worker_module.validate_completion_gate_configuration(
            Settings(workflow=WorkflowSettings(h5_v2_mode="on")),
            state_store=InMemoryAgentRunStateStore(),
            task_finalization_service=object(),
        )


def test_h5_v2_configuration_accepts_postgres_finalization_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯 H5 V2 Worker 具备 durable 依赖时可启动。"""

    monkeypatch.setenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", "postgres")
    monkeypatch.setenv("ALIOTH_DATABASE_URL", "postgresql://example.test/alioth")

    worker_module.validate_completion_gate_configuration(
        Settings(workflow=WorkflowSettings(h5_v2_mode="on")),
        state_store=PostgresAgentRunStateStore(),
        task_finalization_service=object(),
    )


def test_worker_without_app_qc_or_h5_v2_keeps_noop_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完全关闭两条收口路径的普通 Worker 不要求 PostgreSQL。"""

    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)
    monkeypatch.setenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", "noop")

    worker_module.validate_completion_gate_configuration(
        Settings(workflow=WorkflowSettings()),
        state_store=InMemoryAgentRunStateStore(),
        task_finalization_service=None,
    )


def test_worker_rejects_noop_state_store_for_reliable_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)
    monkeypatch.setenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", "noop")

    with pytest.raises(ValueError, match="任务提交可靠投影"):
        worker_module.validate_task_submission_configuration(
            Settings(),
            state_store=InMemoryAgentRunStateStore(),
        )


def test_worker_accepts_postgres_state_store_for_reliable_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_DATABASE_URL", "postgresql://example.test/alioth")
    monkeypatch.setenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", "postgres")

    worker_module.validate_task_submission_configuration(
        Settings(),
        state_store=PostgresAgentRunStateStore(),
    )


@pytest.mark.parametrize(
    "settings",
    [
        Settings.model_validate(
            {
                "workflow": {"web_v2_mode": "exploration_only"},
                "worker": {"roles": ["explorer", "judge"]},
            }
        ),
        Settings.model_validate(
            {
                "workflow": {"web_v2_mode": "off"},
                "worker": {"roles": ["explorer", "judge", "web_explorer"]},
            }
        ),
        Settings.model_validate(
            {
                "workflow": {"web_v2_mode": "on"},
                "worker": {"roles": ["explorer", "web_explorer"]},
            }
        ),
    ],
)
def test_worker_rejects_mismatched_web_mode_and_role(settings: Settings) -> None:
    with pytest.raises(ValueError, match="Web V2|web_explorer|judge"):
        worker_module.validate_web_worker_configuration(settings)


def test_worker_accepts_enabled_web_mode_with_dedicated_role() -> None:
    settings = Settings.model_validate(
        {
            "workflow": {"web_v2_mode": "exploration_only"},
            "worker": {"roles": ["explorer", "judge", "web_explorer"]},
        }
    )

    worker_module.validate_web_worker_configuration(settings)


def test_worker_accepts_web_full_inspection_with_explorer_and_judge_roles() -> None:
    settings = Settings.model_validate(
        {
            "workflow": {"web_v2_mode": "on"},
            "worker": {"roles": ["explorer", "judge", "web_explorer"]},
        }
    )

    worker_module.validate_web_worker_configuration(settings)


def test_candidate_pages_keeps_only_complete_catalog_references() -> None:
    action_trace = [
        {
            "tool_name": "candidate_rules_tool",
            "status": "ok",
            "arguments": '{"page_capture_id":"capture-1","page_type":"home"}',
        },
        {
            "tool_name": "candidate_rules_tool",
            "status": "ok",
            "arguments": '{"page_capture_id":"capture-2","page_type":"ad_popup"}',
        },
        {"tool_name": "candidate_rules_tool", "status": "ok", "arguments": "{}"},
    ]

    assert worker_module._candidate_pages(action_trace) == [
        CandidatePageReference(page_capture_id="capture-1", page_type="home")
    ]


def test_candidate_pages_preserves_exact_returned_rule_codes() -> None:
    action_trace = [
        {
            "tool_name": "candidate_rules_tool",
            "status": "ok",
            "arguments": {"page_capture_id": "capture-1", "page_type": "video_list"},
            "result": {
                "rules": [
                    {"rule_code": "alioth_5"},
                    {"rule_code": "alioth_6"},
                    {"rule_code": "alioth_5"},
                ]
            },
        }
    ]

    assert worker_module._candidate_pages(action_trace) == [
        CandidatePageReference(
            page_capture_id="capture-1",
            page_type="video_list",
            candidate_rule_codes=("alioth_5", "alioth_6"),
        )
    ]


def test_candidate_pages_recovers_exact_codes_from_working_memory() -> None:
    action_trace = [
        {
            "tool_name": "candidate_rules_tool",
            "status": "ok",
            "arguments": {"page_capture_id": "capture-1", "page_type": "home"},
            "result": {"text": "截断的旧动作输出"},
        }
    ]
    working_memory = {
        "inspected_pages": [
            {
                "page_capture_id": "capture-1",
                "page_detection": {"page_type": "home"},
                "candidate_rules": [
                    {"rule_code": "alioth_2"},
                    {"rule_code": "alioth_7"},
                ],
            }
        ]
    }

    assert worker_module._candidate_pages(
        action_trace,
        working_memory=working_memory,
    ) == [
        CandidatePageReference(
            page_capture_id="capture-1",
            page_type="home",
            candidate_rule_codes=("alioth_2", "alioth_7"),
        )
    ]


def test_runtime_rule_fallbacks_reads_isolated_errors_once() -> None:
    fallback = {
        "page_capture_id": "capture-1",
        "result": {
            "rules": [
                {
                    "rule_id": "rule-1",
                    "rule_code": "alioth_1",
                    "execution_status": "error",
                    "result_summary": "页面已降级完成，规则未能完成判定。",
                    "problem_desc": "规则写回被拒绝",
                },
                {
                    "rule_id": "rule-2",
                    "rule_code": "alioth_2",
                    "execution_status": "pass",
                },
            ]
        },
    }

    assert worker_module._runtime_rule_fallbacks(
        {"inspected_pages": [fallback], "terminal_pages": [fallback]}
    ) == [
        RuntimeRuleExecutionFallback(
            page_capture_id="capture-1",
            rule_id="rule-1",
            rule_code="alioth_1",
            execution_status="error",
            result_summary="页面已降级完成，规则未能完成判定。",
            problem_desc="规则写回被拒绝",
        )
    ]


def test_runtime_rule_fallbacks_keeps_budget_terminated_rules_as_skipped() -> None:
    page = {
        "page_capture_id": "capture-2",
        "failure": {"code": "MAX_STEPS_REACHED"},
        "result": {
            "rules": [
                {
                    "rule_id": "rule-17",
                    "rule_code": "alioth_17",
                    "execution_status": "skipped",
                    "result_summary": "任务预算终止，规则未执行",
                    "problem_desc": "达到本次运行的动态步数上限",
                }
            ]
        },
    }

    assert worker_module._runtime_rule_fallbacks({"terminal_pages": [page]}) == [
        RuntimeRuleExecutionFallback(
            page_capture_id="capture-2",
            rule_id="rule-17",
            rule_code="alioth_17",
            execution_status="skipped",
            result_summary="任务预算终止，规则未执行",
            problem_desc="达到本次运行的动态步数上限",
            reason="run_budget_exhausted",
        )
    ]


def test_runtime_page_failure_fallbacks_keep_recognized_business_page_results() -> None:
    working_memory = {
        "terminal_pages": [
            {
                "page_capture_id": "capture-home",
                "page_type": "home",
                "inspection_status": "failed",
                "page_detection": {"rule_matching_allowed": True},
                "failure": {
                    "code": "PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED",
                    "message": "二维码登录遮罩无法关闭",
                },
            },
            {
                "page_capture_id": "capture-loading",
                "page_type": "line_loading",
                "inspection_status": "failed",
                "page_detection": {"rule_matching_allowed": False},
                "failure": {"code": "TRANSIENT_PAGE_RECOVERY_EXHAUSTED"},
            },
        ]
    }

    assert worker_module._runtime_page_failure_fallbacks(working_memory) == [
        RuntimePageInspectionFailureFallback(
            page_capture_id="capture-home",
            page_type="home",
            failure_code="PAGE_OBSTRUCTION_RECOVERY_EXHAUSTED",
            failure_message="二维码登录遮罩无法关闭",
        )
    ]


class LegacyH5TestQueue(RedisAgentRunQueue):
    """为通用 Worker 回归数据显式声明旧 H5 兼容路由。"""

    async def enqueue(
        self,
        message: str,
        *,
        task_id: str | None = None,
        task_config: dict | None = None,
        page_sampling_policy: dict | None = None,
        loop_guard: dict | None = None,
    ) -> AgentRunRecord:
        explicit_config = dict(task_config or {})
        if "_task_target" not in explicit_config:
            explicit_config.setdefault("legacy_h5_compatible", True)
        return await super().enqueue(
            message,
            task_id=task_id,
            task_config=explicit_config,
            page_sampling_policy=page_sampling_policy,
            loop_guard=loop_guard,
        )


@pytest.fixture
def queue() -> RedisAgentRunQueue:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return LegacyH5TestQueue(client, ttl_seconds=3600)


@pytest.fixture(autouse=True)
def disable_postgres_progress_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker 单测不得因本地 .env 配置连接真实任务进度库。"""
    monkeypatch.setattr(worker_module, "build_task_progress_recorder", lambda **_kwargs: None)


# 门禁专项测试需要绕过下方 autouse 旁路，模块导入期先捕获真实现。
_REAL_FINALIZE_PERSISTED_INSPECTION_WORK = worker_module._finalize_persisted_inspection_work
_REAL_GUARD_H5_RULE_BACKFILL = worker_module._guard_h5_rule_backfill


@pytest.fixture(autouse=True)
def bypass_persisted_inspection_finalization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker 单测不连接真实 PostgreSQL；工作项排空与门禁另有专门测试覆盖。"""

    async def _noop_drain(record, *, settings, worker_instance_id) -> None:
        del record, settings, worker_instance_id

    async def _noop_finalize(record, *, settings) -> None:
        del record, settings

    async def _noop_wait_inflight(service, *, task_id, **kwargs) -> float:
        del service, task_id, kwargs
        return 0.0

    monkeypatch.setattr(worker_module, "_drain_snapshot_inspection_work", _noop_drain)
    monkeypatch.setattr(worker_module, "_finalize_persisted_inspection_work", _noop_finalize)
    monkeypatch.setattr(worker_module, "_wait_for_inflight_inspection_work", _noop_wait_inflight)
    monkeypatch.setattr(worker_module, "_wait_for_aggregate_inspection_work", _noop_wait_inflight)
    async def _noop_h5_backfill_guard(record, service) -> None:
        del record, service

    monkeypatch.setattr(worker_module, "_guard_h5_rule_backfill", _noop_h5_backfill_guard)


def _app_task_config() -> dict:
    """构造带持久化目标快照的 App Run 配置，确保 Worker 走 App 路由。"""
    return {
        "workflow_version": "v2",
        "_task_target": {
            "platform": "app",
            "target_url": None,
            "app": {
                "platform_build_ref": "app-build:build-1",
                "package_name": "com.example.target",
                "device": {"backend": "fake", "profile_id": "pixel-8"},
            },
        },
    }


def _h5_v2_task_config() -> dict:
    return {
        "workflow_version": "v2",
        "_task_target": {
            "platform": "h5",
            "target_url": "https://m.example.test/home",
        },
    }


def _app_recovery_record(
    run_id: str,
    *,
    status: AgentRunQueueStatus,
    task_id: str | None = None,
) -> AgentRunRecord:
    return AgentRunRecord(
        run_id=run_id,
        task_id=task_id or f"task-{run_id}",
        message="恢复 App Run",
        status=status,
        submitted_at=datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
        started_at=datetime(2026, 8, 13, 10, 1, tzinfo=UTC),
        task_config=_app_task_config(),
    )


class FakeAppRunRecovery:
    """同时记录 Worker 恢复/租约调用，并可注入租约丢失。"""

    def __init__(
        self,
        *,
        order: list[str] | None = None,
        snapshots: list[AppRunRecoverySnapshot] | None = None,
        snapshot: AppRunRecoverySnapshot | None = None,
        acquire_lease: bool = True,
        fail_renew_after: int | None = None,
    ) -> None:
        self.order = order if order is not None else []
        self.snapshots = list(snapshots or [])
        self.snapshot = snapshot
        self.acquire_lease = acquire_lease
        self.fail_renew_after = fail_renew_after
        self.renew_count = 0
        self.get_calls: list[str] = []

    async def acquire(self, command: Any) -> AppRunExecutionLease | None:
        self.order.append("acquire")
        if not self.acquire_lease:
            return None
        return AppRunExecutionLease(
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
            lease_owner=command.lease_owner,
            lease_token=command.lease_token,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            row_version=1,
            attempt_count=1,
        )

    async def renew(self, command: Any) -> AppRunExecutionLease:
        self.order.append("renew")
        self.renew_count += 1
        if self.fail_renew_after is not None and self.renew_count > self.fail_renew_after:
            raise RuntimeError("App Run 执行租约已丢失")
        return AppRunExecutionLease(
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
            lease_owner=command.lease_owner,
            lease_token=command.lease_token,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            row_version=self.renew_count + 1,
            attempt_count=1,
        )

    async def release(self, command: Any) -> bool:
        del command
        self.order.append("release")
        return True

    async def list_recovery_snapshots(self, *, limit: int = 100) -> list[AppRunRecoverySnapshot]:
        self.order.append(f"list:{limit}")
        return list(self.snapshots)

    async def get_recovery_snapshot(
        self,
        *,
        agent_run_id: str,
    ) -> AppRunRecoverySnapshot | None:
        self.get_calls.append(agent_run_id)
        self.order.append("get")
        return self.snapshot


class TimelineStateStore(InMemoryAgentRunStateStore):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order

    async def record_started(self, record: AgentRunRecord) -> None:
        self.order.append("start")
        await super().record_started(record)

    async def record_finished(self, record: AgentRunRecord) -> None:
        self.order.append("finish")
        await super().record_finished(record)


def _recovery_snapshot(
    record: AgentRunRecord,
    *,
    ready: bool,
    reason: str,
    execution_lease_active: bool = False,
) -> AppRunRecoverySnapshot:
    return AppRunRecoverySnapshot(
        record=record,
        ready=ready,
        reason=reason,
        next_recovery_at=(
            datetime.now(UTC) + timedelta(minutes=5) if not ready else datetime.now(UTC)
        ),
        execution_lease_active=execution_lease_active,
    )


async def test_process_one_skips_unknown_run_id(queue: RedisAgentRunQueue) -> None:
    """队列里已找不到记录（如已过期）时应安静跳过，不抛异常。"""
    state_store = InMemoryAgentRunStateStore()

    await worker_module._process_one(queue, "does-not-exist", Settings(), state_store=state_store)

    assert state_store.events == []


@pytest.mark.parametrize(
    "status",
    [
        AgentRunQueueStatus.RUNNING,
        AgentRunQueueStatus.COMPLETED,
        AgentRunQueueStatus.FAILED,
        AgentRunQueueStatus.CANCELED,
    ],
)
async def test_process_one_skips_duplicate_wakeup_for_nonqueued_projection(
    queue: RedisAgentRunQueue,
    status: AgentRunQueueStatus,
) -> None:
    record = await queue.enqueue("重复唤醒")
    record.status = status
    await queue.save(record)
    state_store = InMemoryAgentRunStateStore()

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=state_store,
    )

    persisted = await queue.get(record.run_id)
    assert persisted is not None
    assert persisted.status is status
    assert state_store.events == []


async def test_concurrent_duplicate_start_does_not_requeue_run_after_pg_conflict(
    queue: RedisAgentRunQueue,
) -> None:
    """同一 Run 的重复唤醒不能把先启动者的 running 投影重排回 queued。"""

    class ConflictOnDuplicateStartStore(InMemoryAgentRunStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.first_start_entered = asyncio.Event()
            self.release_first_start = asyncio.Event()
            self.start_calls = 0

        async def record_started(self, record: AgentRunRecord) -> None:
            self.start_calls += 1
            if self.start_calls == 1:
                self.first_start_entered.set()
                await self.release_first_start.wait()
                await super().record_started(record)
                return
            raise TaskStatusConflictError("任务已由另一个重复唤醒启动")

    record = await queue.enqueue("重复启动竞争")
    assert await queue.dequeue(timeout=0.1) == record.run_id
    stale_projection = record.model_copy(deep=True)
    state_store = ConflictOnDuplicateStartStore()

    first_start = asyncio.create_task(
        worker_module._start_dequeued_record(
            queue=queue,
            record=record,
            state_store=state_store,
            worker_instance_id="worker-a",
        )
    )
    await state_store.first_start_entered.wait()
    duplicate_start = asyncio.create_task(
        worker_module._start_dequeued_record(
            queue=queue,
            record=stale_projection,
            state_store=state_store,
            worker_instance_id="worker-b",
        )
    )
    duplicate_result = await duplicate_start
    state_store.release_first_start.set()
    first_result = await first_start

    assert first_result is True
    assert duplicate_result is False
    persisted = await queue.get(record.run_id)
    assert persisted is not None
    assert persisted.status is AgentRunQueueStatus.RUNNING
    assert persisted.worker_instance_id == "worker-a"
    assert persisted.dispatch_attempt_count == 1
    assert await queue.queue_depth() == 0
    assert [event.event_type for event in state_store.events] == ["started"]


async def test_completion_gate_passes_before_report_becomes_final(
    queue: RedisAgentRunQueue,
) -> None:
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "质检任务",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    finalization.recoverable_run_ids[record.task_id] = record.run_id

    await worker_module._finish_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=None,
        final_report_generator=generator,
        task_finalization_service=finalization,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert [call[2] for call in generator.calls] == [None, record.finished_at]
    assert loaded.final_report["report_stage"] == "final"
    assert loaded.final_report["is_final"] is True
    assert loaded.final_report["completion_gate"] == {
        "gate_passed": True,
        "gate_snapshot_id": "gate-1",
        "finalization_epoch": 1,
        "block_reasons": [],
    }
    assert finalization.calls == [
        "get",
        "exploration_draining",
        "session_draining",
        "finalize:completed",
    ]
    assert finalization.bind_commands == []


async def test_worker_uses_atomic_task_finalization_for_passing_gate(
    queue: RedisAgentRunQueue,
) -> None:
    state_store = InMemoryAgentRunStateStore()
    finalization = AtomicOnlyTaskFinalizationService(gate_passed=True)
    record = await queue.enqueue(
        "原子收口任务",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    finalization.recoverable_run_ids[record.task_id] = record.run_id

    await worker_module._finish_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=None,
        final_report_generator=FakeFinalReportGenerator(),
        task_finalization_service=finalization,
    )

    assert "evaluate:completed" not in finalization.calls
    assert "finalize:completed" in finalization.calls
    assert len(state_store.events) == 2


async def test_blocked_completion_gate_keeps_report_as_preview(
    queue: RedisAgentRunQueue,
) -> None:
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=False)
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "质检任务",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.FAILED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.MAX_STEPS_REACHED.value
    finalization.recoverable_run_ids[record.task_id] = record.run_id

    await worker_module._finish_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=None,
        final_report_generator=generator,
        task_finalization_service=finalization,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert len(generator.calls) == 2
    assert generator.calls[0][2] is None
    assert loaded.final_report["report_stage"] == "preview"
    assert loaded.final_report["is_final"] is False
    assert loaded.final_report["completion_gate"] == {
        "gate_passed": False,
        "gate_snapshot_id": "gate-1",
        "finalization_epoch": 0,
        "block_reasons": ["pending_action"],
    }
    assert finalization.calls[-1] == "finalize:needs_review"
    assert finalization.bind_commands == []


@pytest.mark.parametrize("unchanged", [True, False])
@pytest.mark.parametrize("platform", ["h5", "web"])
async def test_completion_gate_retry_checks_facts_before_generating_report(
    queue: RedisAgentRunQueue,
    unchanged: bool,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    """稳定阻塞跳过模型与封存；事实变化后仍进入原有报告和 Gate 流程。"""
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "人工复核后的收口",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.task_config = {"workflow_version": "v2", "_task_target": {"platform": platform}}
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    record.final_report = {"summary": {}, "completion_gate": {"gate_passed": False}}
    finalization.pending_task_ids.add(record.task_id)
    finalization.recoverable_run_ids[record.task_id] = record.run_id
    await queue.save(record)
    commands = []
    released = []
    session_settlements = []

    class H5Settlement:
        async def settle_requests_after_session_close(self, *, task_id, agent_run_id):
            assert generator.calls == []
            assert commands == []
            session_settlements.append((task_id, agent_run_id))
            return 0

    monkeypatch.setattr(worker_module, "PostgresH5ProbeTerminalSettlementRepository", H5Settlement)

    async def get_snapshot(*, task_id):
        return TaskFinalizationSnapshot(
            task_id=task_id,
            state=worker_module.TaskFinalizationState.SESSION_DRAINING,
            agent_run_id=record.run_id,
            finalization_epoch=4,
            row_version=7,
            gate_snapshot_id="blocked-gate",
        )

    async def defer_unchanged(command):
        assert generator.calls == []
        commands.append(command)
        return unchanged

    async def clear_error(**kwargs):
        released.append(kwargs)

    class InspectionReader:
        async def inspection_work_items(self, *_args, **_kwargs):
            return {"summary": {}}

    async def exploration_for_retry_report(*_args, **_kwargs):
        return {"tracking_available": True, "complete": True, "page_results": []}

    monkeypatch.setattr(worker_module, "_page_exploration_for_report", exploration_for_retry_report)
    monkeypatch.setattr(worker_module, "TaskInsightService", lambda **_kwargs: InspectionReader())
    finalization.get = get_snapshot
    finalization.defer_unchanged_completion_gate = defer_unchanged
    finalization.record_finalization_retry_success = clear_error
    retried = await worker_module._retry_pending_task_finalizations(
        queue, state_store, finalization, final_report_generator=generator,
    )

    assert len(commands) == 1
    assert session_settlements == ([(record.task_id, record.run_id)] if platform == "h5" else [])
    assert commands[0].agent_run_id == record.run_id
    assert commands[0].expected_finalization_epoch == 4
    assert commands[0].recovery_lease_token == "00000000-0000-0000-0000-000000000099"
    assert len(released) == 1
    assert len(generator.calls) == (0 if unchanged else 1)
    assert len(finalization.finalize_commands) == (0 if unchanged else 1)
    assert retried == (0 if unchanged else 1)
    loaded = await queue.get(record.run_id)
    assert loaded.final_report["completion_gate"]["gate_passed"] is (not unchanged)


async def test_completion_gate_retry_uses_durable_session_draining_backlog(
    queue: RedisAgentRunQueue,
) -> None:
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    deferred: list[tuple[str, float]] = []

    async def defer_finalization_retry(*, task_id: str, retry_after_seconds: float) -> None:
        deferred.append((task_id, retry_after_seconds))

    finalization.defer_finalization_retry = defer_finalization_retry  # type: ignore[attr-defined]
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "待重试的收口任务",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    finalization.recoverable_run_ids[record.task_id] = record.run_id
    record.final_report = {
        "summary": {},
        "completion_gate": {"gate_passed": False},
    }
    finalization.pending_task_ids.add(record.task_id)
    finalization.recoverable_run_ids[record.task_id] = record.run_id
    await queue.save(record)

    retried = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        finalization,
        final_report_generator=generator,
    )

    loaded = await queue.get(record.run_id)
    assert retried == 1
    assert loaded is not None
    assert loaded.final_report["completion_gate"]["gate_passed"] is True
    assert len(generator.calls) == 1
    assert finalization.calls[0] == "list_session_draining:100"
    assert finalization.calls[-1] == "finalize:completed"
    assert deferred == []
    assert finalization.finalize_commands[-1].recovery_lease_token == (
        "00000000-0000-0000-0000-000000000099"
    )
    assert finalization.finalize_commands[-1].recovery_generation == 0


@pytest.mark.parametrize("target_platform", ["h5", "app"])
async def test_h5_app_crash_recovery_uses_legacy_backlog_when_web_claim_is_empty(
    queue: RedisAgentRunQueue,
    target_platform: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "H5 crash recovery",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
            "_task_target": {"platform": target_platform},
        },
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    finalization.pending_task_ids.add(record.task_id)
    finalization.recoverable_run_ids[record.task_id] = record.run_id
    await queue.save(record)

    async def empty_web_claim(**_kwargs):
        return []

    async def legacy_backlog(*, limit: int):
        assert limit == 100
        return [SimpleNamespace(task_id=record.task_id, agent_run_id=record.run_id)]

    finalization.claim_recoverable_finalizations = empty_web_claim  # type: ignore[method-assign]
    finalization.list_recoverable_finalizations = legacy_backlog  # type: ignore[attr-defined]

    async def generate_without_persistence(*args, **_kwargs):
        target_record = args[0]
        target_record.final_report = {
            "report_stage": "final",
            "is_final": True,
            "agent_run_id": target_record.run_id,
            "task": {"task_id": target_record.task_id, "status": "done"},
            "summary": {"overall_status": "pass", "pass": 1},
        }

    settled = []

    class H5Settlement:
        async def settle_requests_after_session_close(self, *, task_id, agent_run_id):
            settled.append((task_id, agent_run_id))
            return 0

    monkeypatch.setattr(worker_module, "PostgresH5ProbeTerminalSettlementRepository", H5Settlement)
    monkeypatch.setattr(
        worker_module,
        "_generate_final_report",
        generate_without_persistence,
    )

    retried = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        finalization,
        final_report_generator=generator,
    )

    assert retried == 1
    assert settled == ([(record.task_id, record.run_id)] if target_platform == "h5" else [])
    assert finalization.finalize_commands[-1].recovery_lease_token is None


async def test_completion_gate_retry_treats_terminal_conflict_as_idempotent(
    queue: RedisAgentRunQueue,
) -> None:
    class ConflictingRunFinishedStore(InMemoryAgentRunStateStore):
        async def record_run_finished(self, record) -> None:
            del record
            raise AgentRunTerminalStateConflict("authoritative terminal already exists")

    state_store = ConflictingRunFinishedStore()
    finalization = FakeTaskFinalizationService(gate_passed=False)
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "已有权威终态的收口任务",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    record.final_report = {
        "summary": {},
        "completion_gate": {"gate_passed": False},
    }
    finalization.pending_task_ids.add(record.task_id)
    finalization.recoverable_run_ids[record.task_id] = record.run_id
    await queue.save(record)

    retried = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        finalization,
        final_report_generator=generator,
    )

    loaded = await queue.get(record.run_id)
    assert retried == 1
    assert loaded is not None
    assert loaded.final_report["completion_gate"]["gate_passed"] is False
    assert finalization.calls[-1] == "finalize:completed"


async def test_completion_gate_retry_recovers_record_without_preview_report(
    queue: RedisAgentRunQueue,
) -> None:
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "缺少预览报告的收口任务",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    finalization.pending_task_ids.add(record.task_id)
    finalization.recoverable_run_ids[record.task_id] = record.run_id
    await queue.save(record)

    retried = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        finalization,
        final_report_generator=generator,
    )

    loaded = await queue.get(record.run_id)
    assert retried == 1
    assert loaded is not None
    assert loaded.final_report["completion_gate"]["gate_passed"] is True
    assert len(generator.calls) == 1
    assert finalization.calls[-1] == "finalize:completed"


async def test_completion_gate_continues_when_preview_report_generation_fails(
    queue: RedisAgentRunQueue,
) -> None:
    class PreviewFailingReportGenerator(FakeFinalReportGenerator):
        async def generate_task_report(self, task_id: str, **kwargs):
            if kwargs.get("finalized_at") is None:
                raise RuntimeError("预览报告暂时不可用")
            return await super().generate_task_report(task_id, **kwargs)

    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=False)
    record = await queue.enqueue(
        "预览报告失败的收口任务",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    finalization.recoverable_run_ids[record.task_id] = record.run_id

    await worker_module._finish_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=None,
        final_report_generator=PreviewFailingReportGenerator(),
        task_finalization_service=finalization,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.final_report is not None
    assert loaded.final_report["report_stage"] == "preview"
    assert finalization.calls[-1] == "finalize:completed"
    assert finalization.bind_commands == []


async def test_report_binding_failure_keeps_sealed_report_recoverable(
    queue: RedisAgentRunQueue,
) -> None:
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True, bind_error=True)
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "报告绑定失败的收口任务",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value

    await worker_module._finish_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=None,
        final_report_generator=generator,
        task_finalization_service=finalization,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.final_report["report_stage"] == "preview"
    assert "completion_gate" not in loaded.final_report
    assert len(state_store.events) >= 3


async def test_completion_gate_retry_quarantines_sealed_preview_without_regeneration(
    queue: RedisAgentRunQueue,
) -> None:
    state_store = InMemoryAgentRunStateStore()
    finalization = RecoverableTaskFinalizationService(
        gate_passed=True,
        snapshot_state=worker_module.TaskFinalizationState.SEALED,
    )
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "sealed 但只有预览报告的收口任务",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    record.final_report = {
        "report_stage": "preview",
        "is_final": False,
        "summary": {"overall_status": "pass"},
    }
    finalization.pending_task_ids.add(record.task_id)
    finalization.recoverable_run_ids[record.task_id] = record.run_id
    await queue.save(record)

    retried = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        finalization,
        final_report_generator=generator,
    )

    loaded = await queue.get(record.run_id)
    assert retried == 0
    assert loaded is not None
    assert len(generator.calls) == 0
    assert loaded.final_report["report_stage"] == "preview"
    assert loaded.final_report["is_final"] is False
    assert finalization.bind_commands == []
    assert finalization.quarantined_task_ids == [record.task_id]


async def test_completion_gate_retry_does_not_bind_preview_when_final_regeneration_fails(
    queue: RedisAgentRunQueue,
) -> None:
    class FinalFailingReportGenerator(FakeFinalReportGenerator):
        async def generate_task_report(self, task_id: str, **kwargs):
            if kwargs.get("finalized_at") is not None:
                raise RuntimeError("正式报告暂时不可用")
            return await super().generate_task_report(task_id, **kwargs)

    state_store = InMemoryAgentRunStateStore()
    finalization = RecoverableTaskFinalizationService(
        gate_passed=True,
        snapshot_state=worker_module.TaskFinalizationState.SEALED,
    )
    retry_errors: list[tuple[str, str]] = []

    async def record_finalization_retry_error(
        *, task_id: str, error_message: str, **_kwargs
    ) -> None:
        retry_errors.append((task_id, error_message))

    finalization.record_finalization_retry_error = (  # type: ignore[attr-defined]
        record_finalization_retry_error
    )
    record = await queue.enqueue(
        "sealed 但正式报告生成失败的收口任务",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    record.final_report = {
        "report_stage": "preview",
        "is_final": False,
        "summary": {"overall_status": "pass"},
    }
    finalization.pending_task_ids.add(record.task_id)
    finalization.recoverable_run_ids[record.task_id] = record.run_id
    await queue.save(record)

    retried = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        finalization,
        final_report_generator=FinalFailingReportGenerator(),
    )

    loaded = await queue.get(record.run_id)
    assert retried == 0
    assert loaded is not None
    assert loaded.final_report["report_stage"] == "preview"
    assert loaded.final_report["is_final"] is False
    assert finalization.bind_commands == []
    assert retry_errors == []
    assert finalization.quarantined_task_ids == [record.task_id]


async def test_v2_finalization_commits_postgres_when_redis_save_fails(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    record = await queue.enqueue(
        "Redis 断连时仍应完成 PG 收口",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    finalization.recoverable_run_ids[record.task_id] = record.run_id
    call_order: list[str] = []
    original_record_run_finished = state_store.record_run_finished

    async def track_postgres(saved_record) -> None:
        call_order.append("postgres")
        await original_record_run_finished(saved_record)

    async def fail_save(_record) -> None:
        call_order.append("redis")
        raise ConnectionError("redis connection reset")

    monkeypatch.setattr(state_store, "record_run_finished", track_postgres)
    monkeypatch.setattr(queue, "save", fail_save)

    await worker_module._finish_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=None,
        final_report_generator=FakeFinalReportGenerator(),
        task_finalization_service=finalization,
    )

    assert finalization.calls[-1] == "finalize:completed"
    assert [event.event_type for event in state_store.events] == ["finished", "finished"]
    assert call_order == ["postgres", "postgres", "redis"]


async def test_completion_gate_retry_uses_postgres_when_redis_list_fails(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    record = await queue.enqueue(
        "Redis 断连时从 PG 恢复",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    await state_store.record_run_finished(record)
    finalization.pending_task_ids.add(record.task_id)
    finalization.recoverable_run_ids[record.task_id] = record.run_id
    call_order: list[str] = []
    original_list_finished = state_store.list_finished_records

    async def track_list_finished(**kwargs):
        call_order.append("postgres")
        return await original_list_finished(**kwargs)

    async def fail_list_records():
        call_order.append("redis")
        raise ConnectionError("redis connection reset")

    async def fail_save(_record) -> None:
        raise ConnectionError("redis connection reset")

    monkeypatch.setattr(state_store, "list_finished_records", track_list_finished)
    monkeypatch.setattr(queue, "list_records", fail_list_records)
    monkeypatch.setattr(queue, "save", fail_save)

    retried = await worker_module._retry_pending_task_finalizations(
        queue,
        state_store,
        finalization,
        final_report_generator=FakeFinalReportGenerator(),
    )

    assert retried == 1
    assert finalization.calls[-1] == "finalize:completed"
    assert call_order[:2] == ["postgres", "redis"]


@pytest.mark.parametrize(
    ("termination_reason", "expected_finalize"),
    [
        (TerminationReason.COMPLETED, "finalize:completed"),
        (TerminationReason.INCOMPLETE, "finalize:needs_review"),
    ],
)
async def test_app_worker_routes_terminal_state_through_completion_gate(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
    termination_reason: TerminationReason,
    expected_finalize: str,
) -> None:
    """App 业务终态必须先消费 sealed Index，再经 Completion Gate 收口。"""
    close_events: list[str] = []

    class Dispatcher:
        async def dispatch(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            record_current_llm_usage(
                {
                    "prompt_tokens": 80,
                    "completion_tokens": 20,
                    "total_tokens": 100,
                }
            )
            return RunResult(
                termination_reason=termination_reason,
                working_memory=WorkingMemory(step_count=1),
                trace_sink=InMemoryActionTraceSink(),
            )

    class SealedInspectionPublisher:
        async def publish(self, *, task_id: str, agent_run_id: str):
            assert task_id == "11111111-1111-1111-1111-111111111111"
            assert agent_run_id == record.run_id
            close_events.append("publish_sealed_inspection")
            return SimpleNamespace(
                to_dict=lambda: {
                    "evidence_index_id": "index-1",
                    "outcome": "exploration_completed",
                    "indexed_capture_count": 2,
                    "published_capture_count": 2,
                    "skipped_capture_count": 0,
                    "published_work_item_count": 2,
                    "created_work_item_count": 2,
                }
            )

    async def _record_drain(record, *, settings, worker_instance_id) -> None:
        del record, settings, worker_instance_id
        close_events.append("drain_inspection")

    monkeypatch.setattr(worker_module, "_drain_snapshot_inspection_work", _record_drain)

    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "App 质检任务",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    finalization.recoverable_run_ids[record.task_id] = record.run_id

    await worker_module._process_app_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=Dispatcher(),
        rule_execution_writer=FakeRuleExecutionWriter(),
        final_report_generator=generator,
        task_finalization_service=finalization,
        sealed_inspection_publisher=SealedInspectionPublisher(),
        app_publication_repository=FakeAppPublicationRepository(),
    )

    assert close_events == ["publish_sealed_inspection", "drain_inspection"]
    assert record.usage["app_evidence_index_id"] == "index-1"
    assert record.usage["app_inspection_work_item_count"] == 2
    assert record.usage["total_tokens"] == 100
    assert record.usage["ai_calls"] == 1
    assert finalization.calls[-1] == expected_finalize
    assert len(generator.calls) == 2
    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.final_report["completion_gate"]["gate_passed"] is True


async def test_publication_snapshot_mismatch_continues_instead_of_closing(
    queue: RedisAgentRunQueue,
) -> None:
    """来源、封存 Index 或任务快照不一致时，不得把 Run 标失败并直接收口。"""

    from services.api.app.application.app_inspection_publication import (
        AppPublicationSnapshotMismatch,
    )

    class Dispatcher:
        async def dispatch(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(step_count=1),
                trace_sink=InMemoryActionTraceSink(),
            )

    class MismatchPublisher:
        async def publish(self, *, task_id: str, agent_run_id: str):
            del task_id, agent_run_id
            raise AppPublicationSnapshotMismatch()

    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    record = await queue.enqueue(
        "App 快照不一致仍继续",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    finalization.recoverable_run_ids[record.task_id] = record.run_id

    await worker_module._process_app_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=Dispatcher(),
        task_finalization_service=finalization,
        sealed_inspection_publisher=MismatchPublisher(),
        worker_instance_id="worker-a",
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.QUEUED
    assert loaded.termination_reason is None
    assert loaded.error_message is None
    assert loaded.finished_at is None
    assert finalization.calls == []
    assert await queue.dequeue(timeout=0.01) == record.run_id


async def test_app_exploration_only_stops_after_runtime_closure(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App 仅探索不得触发正式质检发布、规则执行、报告或 Completion Gate。"""

    class Dispatcher:
        async def dispatch(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(step_count=2),
                trace_sink=InMemoryActionTraceSink(),
                final_text="App 页面探索已完成",
                execution_outcome=ExecutionOutcome.SUCCESS,
                evidence_index_id="index-exploration-only",
                exploration_status="exploration_completed",
                evidence_gap=(),
            )

    class ForbiddenSealedInspectionPublisher:
        async def publish(self, *, task_id: str, agent_run_id: str):
            del task_id, agent_run_id
            raise AssertionError("exploration_only 不得发布 sealed Inspection Work")

    async def forbidden_drain(record, *, settings, worker_instance_id) -> None:
        del record, settings, worker_instance_id
        raise AssertionError("exploration_only 不得排空 Inspection Work")

    async def forbidden_rules(record, writer) -> None:
        del record, writer
        raise AssertionError("exploration_only 不得写入 qc_rule_execution")

    async def forbidden_finalize(record, *, settings) -> None:
        del record, settings
        raise AssertionError("exploration_only 不得执行 Inspection Work 最终门禁")

    monkeypatch.setattr(worker_module, "_drain_snapshot_inspection_work", forbidden_drain)
    monkeypatch.setattr(worker_module, "_finalize_rule_executions", forbidden_rules)
    monkeypatch.setattr(worker_module, "_finalize_persisted_inspection_work", forbidden_finalize)

    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "App 仅页面探索",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={
            **_app_task_config(),
            "execution_mode": "exploration_only",
        },
    )

    await worker_module._process_app_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=Dispatcher(),
        rule_execution_writer=FakeRuleExecutionWriter(),
        final_report_generator=generator,
        task_finalization_service=finalization,
        sealed_inspection_publisher=ForbiddenSealedInspectionPublisher(),
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.COMPLETED
    assert loaded.evidence_index_id == "index-exploration-only"
    assert loaded.exploration_status == "exploration_completed"
    assert loaded.final_report is None
    assert generator.calls == []
    assert finalization.calls == []


async def test_app_exploration_only_persists_visual_llm_usage(
    queue: RedisAgentRunQueue,
) -> None:
    """仅探索入口必须与正式入口一样保存视觉模型 token 账本。"""

    class Dispatcher:
        async def dispatch(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            record_current_llm_usage(
                {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                }
            )
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(step_count=1),
                trace_sink=InMemoryActionTraceSink(),
                execution_outcome=ExecutionOutcome.SUCCESS,
                evidence_index_id="index-metered-app",
                exploration_status="exploration_completed",
            )

    record = await queue.enqueue(
        "App token 计量 exploration_only",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={
            **_app_task_config(),
            "execution_mode": "exploration_only",
        },
    )
    await worker_module._process_app_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=Dispatcher(),
    )
    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.usage is not None
    assert loaded.usage["input_tokens"] == 120
    assert loaded.usage["output_tokens"] == 30
    assert loaded.usage["total_tokens"] == 150
    assert loaded.usage["ai_calls"] == 1


async def test_process_one_app_defers_rule_guard_to_post_exploration(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App 路由不得在共享截图 Runtime 之前执行正式规则门禁。"""

    class Guard:
        def __init__(self) -> None:
            self.calls: list[TaskExecutionMode] = []

        async def ensure_ready_for(self, execution_mode: TaskExecutionMode) -> None:
            self.calls.append(execution_mode)

    guard = Guard()
    captured: list[object] = []

    async def capture_app_route(**kwargs) -> None:
        captured.append(kwargs["rule_engine_configuration_guard"])

    monkeypatch.setattr(
        worker_module,
        "build_default_task_finalization_service",
        lambda: None,
    )
    monkeypatch.setattr(
        worker_module,
        "_process_app_one_with_run_lease",
        capture_app_route,
    )
    record = await queue.enqueue(
        "App 正式质检",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={**_app_task_config(), "execution_mode": "full_inspection"},
    )

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings.model_validate({"workflow": {"app_qc_enabled": True}}),
        state_store=InMemoryAgentRunStateStore(),
        app_workflow_handler=object(),
        rule_engine_configuration_guard=guard,
    )

    assert guard.calls == []
    assert captured == [guard]


async def test_app_full_inspection_checks_rule_guard_after_dispatch(
    queue: RedisAgentRunQueue,
) -> None:
    """规则配置阻断可终止质检，但必须保留已经完成的页面探索。"""

    order: list[str] = []

    class Dispatcher:
        async def dispatch(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            order.append("dispatch")
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(step_count=3),
                trace_sink=InMemoryActionTraceSink(),
                evidence_index_id="index-before-rule-guard",
                exploration_status="exploration_completed",
            )

    class Guard:
        async def ensure_ready_for(self, execution_mode: TaskExecutionMode) -> None:
            assert execution_mode is TaskExecutionMode.FULL_INSPECTION
            order.append("rule_guard")
            raise RuntimeError("规则配置不可执行")

    record = await queue.enqueue(
        "App 正式质检规则阻断",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={**_app_task_config(), "execution_mode": "full_inspection"},
    )
    await worker_module._process_app_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=Dispatcher(),
        rule_engine_configuration_guard=Guard(),
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert order == ["dispatch", "rule_guard"]
    assert loaded.status is AgentRunQueueStatus.FAILED
    assert loaded.evidence_index_id == "index-before-rule-guard"
    assert loaded.exploration_status == "exploration_completed"
    assert loaded.error_message == "规则配置不可执行"


async def test_standard_canceled_finish_disables_report_and_completion_gate(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任何 Workflow 的取消终态都不得继续生成报告或进入 Completion Gate。"""

    captured: dict[str, object] = {}

    async def capture_finish(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(worker_module, "_finish_terminal_run", capture_finish)
    record = await queue.enqueue(
        "统一取消收口",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.CANCELED
    record.termination_reason = TerminationReason.CANCELED.value

    await worker_module._finish_standard_terminal_run(
        queue=queue,
        record=record,
        state_store=InMemoryAgentRunStateStore(),
        progress_recorder=None,
        final_report_generator=object(),
        task_finalization_service=object(),
        inspection_work_items_enabled=True,
        legacy_report_enabled=True,
    )

    assert captured["task_finalization_service"] is None
    assert captured["inspection_work_items_enabled"] is False
    assert captured["legacy_report_enabled"] is False


async def test_app_worker_projects_durable_cancel_conflict_to_canceled(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享 PostgreSQL 守卫判定取消胜出时，App 不得遗留 running Run。"""

    class Dispatcher:
        async def dispatch(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(step_count=1),
                trace_sink=InMemoryActionTraceSink(),
            )

    class SealedInspectionPublisher:
        async def publish(self, *, task_id: str, agent_run_id: str):
            del task_id, agent_run_id
            return SimpleNamespace(to_dict=lambda: {})

    class CancelWinningStore(InMemoryAgentRunStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.finish_calls = 0

        async def record_run_finished(self, record) -> None:
            self.finish_calls += 1
            if self.finish_calls == 1:
                raise AgentRunCancellationConflict("cancel won")
            await super().record_run_finished(record)

    async def no_op_drain(record, *, settings, worker_instance_id) -> None:
        del record, settings, worker_instance_id

    monkeypatch.setattr(worker_module, "_drain_snapshot_inspection_work", no_op_drain)
    canceled_tasks: list[str] = []

    async def cancel_app_work(*, task_id: str) -> int:
        canceled_tasks.append(task_id)
        return 1

    monkeypatch.setattr(worker_module, "_cancel_app_nonterminal_work", cancel_app_work)
    state_store = CancelWinningStore()
    record = await queue.enqueue(
        "App 取消竞争任务",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={
            "workflow_version": "v2",
            "_task_target": {"platform": "app", "target_url": "app://demo"},
        },
    )

    await worker_module._process_app_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=Dispatcher(),
        rule_execution_writer=FakeRuleExecutionWriter(),
        task_finalization_service=FakeTaskFinalizationService(gate_passed=True),
        sealed_inspection_publisher=SealedInspectionPublisher(),
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.CANCELED
    assert loaded.termination_reason == TerminationReason.CANCELED.value
    assert loaded.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value
    assert canceled_tasks == [record.task_id]


async def test_app_durable_cancel_blocks_sealed_inspection_when_redis_signal_is_missing(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App Runtime 返回后仍须读取长期取消事实，不能继续发布 sealed Inspection。"""

    state_store = InMemoryAgentRunStateStore()
    close_events: list[str] = []

    class Dispatcher:
        async def dispatch(self, *, record, settings, progress_recorder=None):
            del settings, progress_recorder
            await state_store.record_cancel_requested(record.run_id)
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(step_count=1),
                trace_sink=InMemoryActionTraceSink(),
            )

    class SealedInspectionPublisher:
        async def publish(self, *, task_id: str, agent_run_id: str):
            del task_id, agent_run_id
            close_events.append("sealed")
            return SimpleNamespace(to_dict=lambda: {})

    async def cancel_app_work(*, task_id: str) -> int:
        assert task_id == "11111111-1111-1111-1111-111111111111"
        close_events.append("cancel_work")
        if close_events.count("cancel_work") == 1:
            raise ConnectionError("postgres cleanup unavailable")
        return 1

    monkeypatch.setattr(worker_module, "_cancel_app_nonterminal_work", cancel_app_work)
    record = await queue.enqueue(
        "App 长期取消任务",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={
            "workflow_version": "v2",
            "_task_target": {"platform": "app", "target_url": "app://demo"},
        },
    )

    await worker_module._process_app_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=Dispatcher(),
        task_finalization_service=FakeTaskFinalizationService(gate_passed=True),
        sealed_inspection_publisher=SealedInspectionPublisher(),
    )

    assert await queue.is_cancel_requested(record.run_id) is False
    assert record.status is AgentRunQueueStatus.CANCELED
    assert close_events == ["cancel_work", "cancel_work"]
    assert record.cancellation_cleanup_completed is True
    assert record.cancellation_finalized is True


@pytest.mark.parametrize(
    ("canceling_event", "expected_prefix"),
    [
        ("publish", ("publish",)),
        ("drain", ("publish", "drain")),
        ("rules", ("publish", "drain", "rules")),
        ("finalize", ("publish", "drain", "rules", "finalize")),
    ],
)
async def test_app_cancel_between_formal_closure_steps_blocks_later_side_effects(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
    canceling_event: str,
    expected_prefix: tuple[str, ...],
) -> None:
    events: list[str] = []

    class GuardedStateStore(InMemoryAgentRunStateStore):
        async def record_run_finished(self, record) -> None:
            if (
                await self.is_cancel_requested(record.run_id)
                and record.status is not AgentRunQueueStatus.CANCELED
            ):
                raise AgentRunCancellationConflict("cancel won")
            await super().record_run_finished(record)

    class Dispatcher:
        async def dispatch(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(step_count=1),
                trace_sink=InMemoryActionTraceSink(),
            )

    state_store = GuardedStateStore()

    async def mark_cancel(event: str) -> None:
        events.append(event)
        if canceling_event == event:
            await state_store.record_cancel_requested(record.run_id)

    class SealedInspectionPublisher:
        async def publish(self, *, task_id: str, agent_run_id: str):
            assert task_id == record.task_id
            assert agent_run_id == record.run_id
            await mark_cancel("publish")
            return SimpleNamespace(to_dict=lambda: {})

    async def drain(record, *, settings, worker_instance_id) -> None:
        del record, settings, worker_instance_id
        await mark_cancel("drain")

    async def finalize_rules(record, writer) -> None:
        del record, writer
        await mark_cancel("rules")

    async def finalize_work(record, *, settings) -> None:
        del record, settings
        await mark_cancel("finalize")

    async def cancel_app_work(*, task_id: str) -> int:
        assert task_id == record.task_id
        events.append("cancel_work")
        return 1

    monkeypatch.setattr(worker_module, "_drain_snapshot_inspection_work", drain)
    monkeypatch.setattr(worker_module, "_finalize_rule_executions", finalize_rules)
    monkeypatch.setattr(worker_module, "_finalize_persisted_inspection_work", finalize_work)
    monkeypatch.setattr(worker_module, "_cancel_app_nonterminal_work", cancel_app_work)
    generator = FakeFinalReportGenerator()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    record = await queue.enqueue(
        "App 取消边界任务",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={
            "workflow_version": "v2",
            "_task_target": {"platform": "app", "target_url": "app://demo"},
        },
    )
    await worker_module._process_app_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=Dispatcher(),
        rule_execution_writer=FakeRuleExecutionWriter(),
        final_report_generator=generator,
        task_finalization_service=finalization,
        sealed_inspection_publisher=SealedInspectionPublisher(),
        app_publication_repository=FakeAppPublicationRepository(),
    )

    assert record.status is AgentRunQueueStatus.CANCELED
    assert events == [*expected_prefix, "cancel_work"]
    assert generator.calls == []
    assert finalization.calls == []


async def test_app_worker_visual_gap_preserves_incomplete_review_semantics(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dispatcher:
        async def dispatch(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(step_count=2),
                trace_sink=InMemoryActionTraceSink(),
                final_text="App 探索完成",
                execution_outcome=ExecutionOutcome.SUCCESS,
            )

    class SealedInspectionPublisher:
        async def publish(self, *, task_id: str, agent_run_id: str):
            del task_id, agent_run_id
            return SimpleNamespace(
                to_dict=lambda: {
                    "evidence_index_id": "index-1",
                    "evidence_index_outcome": "exploration_completed",
                    "outcome": "evidence_insufficient",
                    "indexed_capture_count": 2,
                    "published_capture_count": 1,
                    "skipped_capture_count": 1,
                    "published_work_item_count": 1,
                    "created_work_item_count": 1,
                    "capture_results": [
                        {"capture_id": "capture-final", "status": "visual_fact_missing"}
                    ],
                }
            )

    async def _noop_drain(record, *, settings, worker_instance_id) -> None:
        del record, settings, worker_instance_id

    monkeypatch.setattr(worker_module, "_drain_snapshot_inspection_work", _noop_drain)
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    record = await queue.enqueue(
        "App 最终视觉事实不足",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    finalization.recoverable_run_ids[record.task_id] = record.run_id

    await worker_module._process_app_one(
        queue,
        record,
        settings=Settings(),
        state_store=state_store,
        dispatcher=Dispatcher(),
        rule_execution_writer=FakeRuleExecutionWriter(),
        final_report_generator=FakeFinalReportGenerator(),
        task_finalization_service=finalization,
        sealed_inspection_publisher=SealedInspectionPublisher(),
        app_publication_repository=FakeAppPublicationRepository(
            linked_work_items=1,
        ),
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.FAILED
    assert loaded.termination_reason == TerminationReason.INCOMPLETE.value
    assert loaded.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value
    assert loaded.error_category is None
    assert loaded.usage["app_evidence_index_outcome"] == "exploration_completed"
    assert loaded.usage["app_exploration_outcome"] == "evidence_insufficient"
    assert loaded.usage["app_inspection_skipped_capture_count"] == 1
    assert finalization.calls[-1] == "finalize:needs_review"
    assert loaded.final_report["task"]["status"] == "review"


async def test_h5_failed_run_with_salvaged_inspection_enters_completion_gate(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H5 探索失败但证据交接完成时，不能绕过 V2 任务收口。"""

    events: list[str] = []

    class Dispatcher:
        async def dispatch_h5_v2(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            return RunResult(
                termination_reason=TerminationReason.ERROR,
                working_memory=WorkingMemory(step_count=20),
                trace_sink=InMemoryActionTraceSink(),
                error_message="App Explorer Work 租约已失效",
                error_category=ErrorCategory.INFRASTRUCTURE,
                execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
            )

    class H5EvidenceHandoff:
        async def prepare(
            self,
            *,
            task_id: str,
            agent_run_id: str,
            execution_mode,
            notification_mode,
        ):
            del task_id, agent_run_id, notification_mode
            assert execution_mode is worker_module.TaskExecutionMode.FULL_INSPECTION
            events.append("handoff")
            return worker_module.H5EvidenceHandoffSummary(
                evidence_ready=True,
                judgeable_capture_count=2,
                page_types=("startup_ad", "home"),
                published_work_item_count=2,
                total_count=2,
                pass_count=2,
            )

    async def drain(record, *, settings, worker_instance_id) -> None:
        del record, settings, worker_instance_id
        events.append("drain")

    async def finalize_rules(record, writer) -> None:
        del record, writer
        events.append("rules")

    async def finalize_work(record, *, settings) -> None:
        del record, settings
        events.append("finalize_work")

    async def page_exploration_for_report(record, *, reader=None) -> dict:
        del record, reader
        return {}

    monkeypatch.setattr(worker_module, "_drain_snapshot_inspection_work", drain)
    monkeypatch.setattr(worker_module, "_finalize_rule_executions", finalize_rules)
    monkeypatch.setattr(worker_module, "_finalize_persisted_inspection_work", finalize_work)
    monkeypatch.setattr(worker_module, "_page_exploration_for_report", page_exploration_for_report)
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    generator = FakeFinalReportGenerator()
    record = await queue.enqueue(
        "H5 失败证据可交接任务",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config=_h5_v2_task_config(),
    )
    finalization.recoverable_run_ids[record.task_id] = record.run_id

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings.model_validate({"workflow": {"h5_v2_mode": "on"}}),
        state_store=state_store,
        dispatcher=Dispatcher(),
        rule_execution_writer=FakeRuleExecutionWriter(),
        final_report_generator=generator,
        task_finalization_service=finalization,
        h5_evidence_handoff=H5EvidenceHandoff(),
        h5_publication_repository=FakeH5PublicationRepository(),
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert events == ["handoff", "drain", "rules", "finalize_work"]
    assert loaded.status is AgentRunQueueStatus.FAILED
    assert loaded.termination_reason == TerminationReason.ERROR.value
    assert loaded.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value
    assert finalization.calls[-1] == "finalize:needs_review"
    assert len(generator.calls) == 2
    assert loaded.final_report["completion_gate"]["gate_passed"] is True


def test_h5_v2_exploration_work_trace_entries_summarize_persisted_work() -> None:
    started_at = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
    finished_at = datetime(2026, 8, 18, 10, 0, 3, 250000, tzinfo=UTC)

    entries = worker_module._h5_v2_exploration_work_trace_entries_from_rows(
        [
            {
                "work_id": "exploration-work:1",
                "work_type": "exploration",
                "source_capture_id": "capture-1",
                "work_payload": {
                    "budget": {"remaining_actions": 3},
                    "frontier_snapshot_id": "frontier-1",
                    "frontier": {
                        "candidates": [
                            {
                                "candidate_id": "candidate-1",
                                "semantic_target": "关闭广告",
                                "metadata": {
                                    "purpose": "关闭当前遮挡弹窗并重新采集",
                                    "target_keywords": ["popup_dismissal"],
                                },
                            }
                        ]
                    },
                },
                "status": "succeeded",
                "attempt_count": 1,
                "max_attempts": 3,
                "started_at": started_at,
                "finished_at": finished_at,
                "deadline": datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
                "last_error_code": None,
                "last_error_message": None,
            },
            {
                "work_id": "exploration-work:2",
                "work_type": "exploration",
                "source_capture_id": "capture-2",
                "work_payload": {},
                "status": "failed",
                "attempt_count": 2,
                "max_attempts": 3,
                "started_at": started_at,
                "finished_at": finished_at,
                "deadline": datetime(2026, 8, 18, 10, 1, tzinfo=UTC),
                "last_error_code": "H5_EXPLORATION_TURN_FAILED",
                "last_error_message": "不能提交已超过 deadline 的后继 Explorer Work",
            },
        ]
    )

    assert len(entries) == 2
    assert entries[0]["tool_name"] == "h5_exploration_work"
    assert entries[0]["status"] == "ok"
    assert entries[0]["duration_seconds"] == 3.25
    assert entries[0]["retry_count"] == 0
    assert json.loads(entries[0]["arguments"]) == {
        "work_id": "exploration-work:1",
        "work_type": "exploration",
        "source_capture_id": "capture-1",
        "deadline": "2026-08-18T10:01:00+00:00",
        "budget": {"remaining_actions": 3},
        "frontier_snapshot_id": "frontier-1",
        "selected_candidate_id": "candidate-1",
        "semantic_target": "关闭广告",
        "purpose": "关闭当前遮挡弹窗并重新采集",
        "target_keywords": ["popup_dismissal"],
    }
    assert entries[1]["status"] == "error"
    assert entries[1]["retry_count"] == 1
    assert json.loads(entries[1]["result"])["error_code"] == "H5_EXPLORATION_TURN_FAILED"


async def test_h5_v2_failed_run_attaches_persisted_work_trace(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dispatcher:
        async def dispatch_h5_v2(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            return RunResult(
                termination_reason=TerminationReason.ERROR,
                working_memory=WorkingMemory(step_count=1),
                trace_sink=InMemoryActionTraceSink(),
                error_message="H5_EXPLORATION_TURN_FAILED",
                error_category=ErrorCategory.INFRASTRUCTURE,
                execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
            )

    async def load_trace_entries(*, task_id: str) -> list[dict[str, Any]]:
        del task_id
        return [
            {
                "step": 1,
                "tool_name": "h5_exploration_work",
                "arguments": '{"work_id":"work-1"}',
                "status": "error",
                "duration_seconds": 1.5,
                "error_message": "H5_EXPLORATION_TURN_FAILED",
                "retry_count": 0,
                "result": '{"work_status":"failed"}',
            }
        ]

    monkeypatch.setattr(
        worker_module,
        "_load_h5_v2_exploration_work_trace_entries",
        load_trace_entries,
    )
    state_store = InMemoryAgentRunStateStore()
    record = await queue.enqueue(
        "H5 失败留痕任务",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config=_h5_v2_task_config(),
    )

    await worker_module._process_h5_v2_one(
        queue,
        record,
        settings=Settings.model_validate({"workflow": {"h5_v2_mode": "on"}}),
        state_store=state_store,
        dispatcher=Dispatcher(),
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.FAILED
    assert loaded.action_trace == [
        {
            "step": 1,
            "tool_name": "h5_exploration_work",
            "arguments": '{"work_id":"work-1"}',
            "status": "error",
            "duration_seconds": 1.5,
            "error_message": "H5_EXPLORATION_TURN_FAILED",
            "retry_count": 0,
            "result": '{"work_status":"failed"}',
        }
    ]
    assert loaded.step_count == 1


async def test_h5_v1_projects_durable_cancel_conflict_to_canceled(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H5 V1/Shadow 也必须使用统一取消感知收口，不能遗留 RUNNING。"""

    class CancelWinningStore(InMemoryAgentRunStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.finish_calls = 0

        async def record_finished(self, record) -> None:
            self.finish_calls += 1
            if record.status is not AgentRunQueueStatus.CANCELED:
                raise AgentRunCancellationConflict("cancel won")
            await super().record_finished(record)

        async def record_run_finished(self, record) -> None:
            if record.status is not AgentRunQueueStatus.CANCELED:
                raise AgentRunCancellationConflict("cancel won")
            await super().record_run_finished(record)

    state_store = CancelWinningStore()
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())

    async def run_completed(*args, **kwargs):
        del args, kwargs
        await state_store.record_cancel_requested(record.run_id)
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(step_count=1),
            trace_sink=InMemoryActionTraceSink(),
        )

    close_events: list[str] = []

    async def record_inspection(name: str, *args, **kwargs):
        del args, kwargs
        close_events.append(name)

    monkeypatch.setattr(worker_module, "run_main_agent", run_completed)
    monkeypatch.setattr(
        worker_module,
        "_drain_snapshot_inspection_work",
        partial(record_inspection, "drain"),
    )
    monkeypatch.setattr(
        worker_module,
        "_finalize_rule_executions",
        partial(record_inspection, "rules"),
    )
    monkeypatch.setattr(
        worker_module,
        "_finalize_persisted_inspection_work",
        partial(record_inspection, "inspection"),
    )
    canceled_tasks: list[str] = []

    async def cancel_work(*, task_id: str) -> int:
        canceled_tasks.append(task_id)
        return 1

    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", cancel_work)
    monkeypatch.setattr(worker_module, "_cancel_app_nonterminal_work", cancel_work)
    record = await queue.enqueue(
        "H5 V1 取消竞争任务",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={"legacy_h5_compatible": True, "workflow_version": "v1"},
    )

    generator = FakeFinalReportGenerator()
    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=state_store,
        final_report_generator=generator,
    )
    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.CANCELED
    assert loaded.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value
    assert loaded.final_report is None
    assert canceled_tasks == [record.task_id]
    assert close_events == []
    assert generator.calls == []


async def test_h5_cancel_cleanup_cancels_nonterminal_inspection_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class CancellationSettlementRepository:
        async def settle_canceled(self, *, task_id, reason):
            assert task_id == "11111111-1111-1111-1111-111111111111"
            assert reason == "H5_TASK_CANCELED"
            calls.append(("settlement", task_id))
            return SimpleNamespace(total_count=0)

    class ExplorerRepository:
        async def cancel_nonterminal(self, *, task_id, reason, work_types, h5_only):
            assert task_id == "11111111-1111-1111-1111-111111111111"
            assert h5_only is True
            assert {item.value for item in work_types} == {"exploration", "probe"}
            assert "取消" in reason
            calls.append(("explorer", task_id))
            return 2

    class ActionRepository:
        async def fail_nonterminal_for_task(self, *, task_id, error_code, error_message, h5_only):
            assert task_id == "11111111-1111-1111-1111-111111111111"
            assert error_code == "WORKFLOW_CANCELED"
            assert h5_only is True
            assert "取消" in error_message
            calls.append(("action", task_id))
            return 3

    class InspectionRepository:
        async def cancel_nonterminal(self, *, task_id, reason):
            assert task_id == "11111111-1111-1111-1111-111111111111"
            assert "取消" in reason
            calls.append(("inspection", task_id))
            return 5

    monkeypatch.setattr(worker_module, "PostgresExplorerWorkRepository", ExplorerRepository)
    monkeypatch.setattr(worker_module, "PostgresActionExecutionRepository", ActionRepository)
    monkeypatch.setattr(worker_module, "PostgresInspectionWorkItemRepository", InspectionRepository)
    monkeypatch.setattr(
        worker_module,
        "PostgresH5CancellationSettlementRepository",
        CancellationSettlementRepository,
    )

    count = await worker_module._cancel_h5_nonterminal_work(
        task_id="11111111-1111-1111-1111-111111111111"
    )

    assert count == 10
    assert calls == [
        ("settlement", "11111111-1111-1111-1111-111111111111"),
        ("explorer", "11111111-1111-1111-1111-111111111111"),
        ("action", "11111111-1111-1111-1111-111111111111"),
        ("inspection", "11111111-1111-1111-1111-111111111111"),
    ]


async def test_app_worker_publisher_infrastructure_failure_remains_error(
    queue: RedisAgentRunQueue,
) -> None:
    class Dispatcher:
        async def dispatch(self, *, record, settings, progress_recorder=None):
            del record, settings, progress_recorder
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(step_count=1),
                trace_sink=InMemoryActionTraceSink(),
                execution_outcome=ExecutionOutcome.SUCCESS,
            )

    class FailingPublisher:
        async def publish(self, *, task_id: str, agent_run_id: str):
            del task_id, agent_run_id
            raise ConnectionError("visual repository unavailable")

    record = await queue.enqueue(
        "App 发布基础设施失败",
        task_id="11111111-1111-1111-1111-111111111111",
    )

    await worker_module._process_app_one(
        queue,
        record,
        settings=Settings(),
        state_store=InMemoryAgentRunStateStore(),
        dispatcher=Dispatcher(),
        sealed_inspection_publisher=FailingPublisher(),
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.FAILED
    assert loaded.termination_reason == TerminationReason.ERROR.value
    assert loaded.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value
    assert loaded.error_category == ErrorCategory.UNKNOWN.value
    assert loaded.error_message == "visual repository unavailable"


async def test_process_one_app_acquire_failure_does_not_start_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    queue: RedisAgentRunQueue,
) -> None:
    order: list[str] = []
    repository = FakeAppRunRecovery(order=order, acquire_lease=False)
    state_store = TimelineStateStore(order)

    class UnexpectedAppHandler:
        async def run(self, **_kwargs):
            raise AssertionError("租约领取失败时不得 dispatch App Runtime")

    monkeypatch.setattr(worker_module, "build_default_task_finalization_service", lambda: None)
    record = await queue.enqueue(
        "App 租约被其他 Worker 持有",
        task_id="task-app-lease-conflict",
        task_config=_app_task_config(),
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings.model_validate({"workflow": {"app_qc_enabled": True}}),
        state_store=state_store,
        app_workflow_handler=UnexpectedAppHandler(),
        app_run_recovery=repository,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.QUEUED
    assert state_store.events == []
    assert order == ["acquire"]


async def test_cancel_conflict_survives_h5_work_cleanup_failure(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancelWinningStore(InMemoryAgentRunStateStore):
        async def record_finished(self, record) -> None:
            if record.status is not AgentRunQueueStatus.CANCELED:
                raise AgentRunCancellationConflict("cancel won")
            await super().record_finished(record)

    cleanup_calls: list[str] = []

    async def flaky_cleanup(*, task_id: str) -> int:
        del task_id
        cleanup_calls.append(record.run_id)
        if len(cleanup_calls) == 1:
            raise ConnectionError("postgres cleanup unavailable")
        return 2

    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", flaky_cleanup)
    monkeypatch.setattr(worker_module, "_cancel_app_nonterminal_work", flaky_cleanup)
    record = await queue.enqueue(
        "H5 cancel cleanup failure",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config={"legacy_h5_compatible": True, "workflow_version": "v1"},
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    record.final_report = {
        "report_stage": "final",
        "completion_gate": {"gate_passed": True},
    }
    await queue.save(record)

    state_store = CancelWinningStore()
    await state_store.record_cancel_requested(record.run_id)
    await worker_module._finish_standard_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=None,
        final_report_generator=None,
        task_finalization_service=None,
        inspection_work_items_enabled=False,
        legacy_report_enabled=False,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.CANCELED
    assert loaded.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value
    assert loaded.final_report is None
    assert loaded.worker_instance_id is None
    assert loaded.cancellation_cleanup_completed is False
    assert loaded.cancellation_finalized is True
    assert cleanup_calls == [record.run_id]

    await worker_module._reconcile_orphaned_runs(queue, state_store)

    reconciled = await queue.get(record.run_id)
    assert reconciled is not None
    assert reconciled.cancellation_cleanup_completed is True
    assert reconciled.cancellation_finalized is True
    assert cleanup_calls == [record.run_id, record.run_id]
    assert await state_store.list_cancellation_recovery_records() == []


async def test_canceled_record_terminal_conflict_finalizes_reconciliation(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消竞争落败后 PG 权威终态不可逆转，取消对账必须幂等收口而非每轮无限重试。"""

    class StaleTerminalStore(InMemoryAgentRunStateStore):
        async def record_finished(self, record) -> None:
            del record
            raise AgentRunTerminalStateConflict("authoritative terminal already exists")

    async def noop_cleanup(*, task_id: str) -> int:
        del task_id
        return 0

    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", noop_cleanup)
    monkeypatch.setattr(worker_module, "_cancel_app_nonterminal_work", noop_cleanup)
    record = await queue.enqueue(
        "canceled record lost terminal race",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.CANCELED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.CANCELED.value
    await queue.save(record)

    await worker_module._finish_standard_terminal_run(
        queue=queue,
        record=record,
        state_store=StaleTerminalStore(),
        progress_recorder=None,
        final_report_generator=None,
        task_finalization_service=None,
        inspection_work_items_enabled=False,
        legacy_report_enabled=False,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.CANCELED
    assert loaded.cancellation_finalized is True


async def test_stale_terminal_conflict_never_projects_run_as_canceled(
    queue: RedisAgentRunQueue,
) -> None:
    class StaleTerminalStore(InMemoryAgentRunStateStore):
        async def record_finished(self, record) -> None:
            del record
            raise AgentRunTerminalStateConflict("authoritative terminal already exists")

    record = await queue.enqueue(
        "stale completed terminal",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    await queue.save(record)

    with pytest.raises(AgentRunTerminalStateConflict):
        await worker_module._finish_standard_terminal_run(
            queue=queue,
            record=record,
            state_store=StaleTerminalStore(),
            progress_recorder=None,
            final_report_generator=None,
            task_finalization_service=None,
            inspection_work_items_enabled=False,
            legacy_report_enabled=False,
        )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.COMPLETED
    assert loaded.cancellation_cleanup_completed is False
    assert loaded.cancellation_finalized is False


async def test_orphan_reconcile_refreshes_redis_from_authoritative_terminal_on_conflict(
    queue: RedisAgentRunQueue,
) -> None:
    terminal_record: AgentRunRecord | None = None

    class StaleTerminalStore(InMemoryAgentRunStateStore):
        async def record_finished(self, record) -> None:
            del record
            raise AgentRunTerminalStateConflict("authoritative terminal already exists")

        async def get_finished_record(self, *, run_id: str) -> AgentRunRecord | None:
            if terminal_record is not None and terminal_record.run_id == run_id:
                return terminal_record.model_copy(deep=True)
            return None

    record = await queue.enqueue("已有 PG 终态的孤儿运行")
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    terminal_record = record.model_copy(
        update={
            "status": AgentRunQueueStatus.COMPLETED,
            "finished_at": datetime.now(UTC),
            "termination_reason": TerminationReason.COMPLETED.value,
            "worker_instance_id": None,
            "worker_missing_since": None,
        },
        deep=True,
    )

    await worker_module._reconcile_orphaned_runs(queue, StaleTerminalStore())

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.COMPLETED
    assert loaded.termination_reason == TerminationReason.COMPLETED.value
    assert loaded.worker_instance_id is None
    assert loaded.worker_missing_since is None


async def test_process_one_app_success_orders_lease_start_dispatch_finish_release(
    monkeypatch: pytest.MonkeyPatch,
    queue: RedisAgentRunQueue,
) -> None:
    order: list[str] = []
    repository = FakeAppRunRecovery(order=order)
    state_store = TimelineStateStore(order)

    class AppHandler:
        async def run(self, **_kwargs):
            order.append("dispatch")
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(step_count=1),
                trace_sink=InMemoryActionTraceSink(),
                final_text="App 完成",
            )

    class SealedPublisher:
        async def publish(self, *, task_id: str, agent_run_id: str):
            del task_id, agent_run_id
            return SimpleNamespace(
                to_dict=lambda: {
                    "evidence_index_id": "index-app-1",
                    "outcome": "exploration_completed",
                    "indexed_capture_count": 1,
                    "published_capture_count": 1,
                    "skipped_capture_count": 0,
                    "published_work_item_count": 0,
                    "created_work_item_count": 0,
                }
            )

    async def persisted_app_page_exploration(record, *, reader):
        del reader
        assert record.task_id == "task-app-lease-success"
        return {
            "tracking_available": True,
            "complete": True,
            "quality_status": "complete",
            "requires_review": False,
            "app_coverage": {
                "coverage_complete": True,
                "visual_quality_satisfied": True,
            },
            "page_results": [],
            "action_results": [],
        }

    monkeypatch.setattr(worker_module, "build_default_task_finalization_service", lambda: None)
    monkeypatch.setattr(
        worker_module,
        "_page_exploration_for_report",
        persisted_app_page_exploration,
    )
    record = await queue.enqueue(
        "App 租约完整执行",
        task_id="task-app-lease-success",
        task_config=_app_task_config(),
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings.model_validate({"workflow": {"app_qc_enabled": True}}),
        state_store=state_store,
        app_workflow_handler=AppHandler(),
        app_run_recovery=repository,
        final_report_generator=FakeFinalReportGenerator(),
        rule_execution_writer=FakeRuleExecutionWriter(),
        sealed_inspection_publisher=SealedPublisher(),
        app_publication_repository=FakeAppPublicationRepository(
            expected_entries=1,
            linked_work_items=0,
        ),
        worker_instance_id="app-worker-1",
    )

    assert order == ["acquire", "renew", "start", "dispatch", "finish", "release"]
    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.COMPLETED
    assert [event.event_type for event in state_store.events] == ["started", "finished"]


async def test_process_one_app_lease_renew_loss_cancels_dispatch_without_pseudo_terminal(
    monkeypatch: pytest.MonkeyPatch,
    queue: RedisAgentRunQueue,
) -> None:
    order: list[str] = []
    repository = FakeAppRunRecovery(order=order, fail_renew_after=5)
    state_store = TimelineStateStore(order)

    real_guard = worker_module.AppRunExecutionLeaseGuard

    class FastLeaseGuard(real_guard):
        def __init__(self, **kwargs):
            kwargs["renew_interval_seconds"] = 0.01
            super().__init__(**kwargs)

    class SlowAppHandler:
        async def run(self, **_kwargs):
            order.append("dispatch")
            await asyncio.sleep(0.2)
            order.append("dispatch_done")
            return RunResult(
                termination_reason=TerminationReason.COMPLETED,
                working_memory=WorkingMemory(step_count=1),
                trace_sink=InMemoryActionTraceSink(),
            )

    monkeypatch.setattr(worker_module, "AppRunExecutionLeaseGuard", FastLeaseGuard)
    monkeypatch.setattr(worker_module, "build_default_task_finalization_service", lambda: None)
    record = await queue.enqueue(
        "App 续租失效",
        task_id="task-app-lease-lost",
        task_config=_app_task_config(),
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id

    with pytest.raises(AppRunExecutionLeaseLostError):
        await worker_module._process_one(
            queue,
            record.run_id,
            Settings.model_validate({"workflow": {"app_qc_enabled": True}}),
            state_store=state_store,
            app_workflow_handler=SlowAppHandler(),
            app_run_recovery=repository,
            worker_instance_id="app-worker-1",
        )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.RUNNING
    assert loaded.finished_at is None
    assert [event.event_type for event in state_store.events] == ["started"]
    assert "dispatch" in order
    assert "dispatch_done" not in order
    assert order[-1] == "release"


async def test_process_one_h5_v2_never_calls_app_recovery_repository(
    monkeypatch: pytest.MonkeyPatch,
    queue: RedisAgentRunQueue,
) -> None:
    called: list[str] = []

    class NeverCalledAppRecovery(FakeAppRunRecovery):
        async def acquire(self, command: Any):
            raise AssertionError(f"H5 不得领取 App 租约：{command}")

        async def get_recovery_snapshot(self, *, agent_run_id: str):
            raise AssertionError(f"H5 不得读取 App 恢复快照：{agent_run_id}")

    async def fake_process_h5_v2_one(queue_arg, record_arg, **kwargs) -> None:
        del queue_arg, kwargs
        called.append(record_arg.run_id)

    monkeypatch.setattr(worker_module, "_process_h5_v2_one", fake_process_h5_v2_one)
    record = await queue.enqueue(
        "H5 页面探索",
        task_id="task-h5-no-app-recovery",
        task_config=_h5_v2_task_config(),
    )
    state_store = InMemoryAgentRunStateStore()

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings.model_validate({"workflow": {"h5_v2_mode": "on"}}),
        state_store=state_store,
        h5_workflow_handler=object(),
        h5_evidence_handoff=object(),
        task_finalization_service=object(),
        app_run_recovery=NeverCalledAppRecovery(),
    )

    assert called == [record.run_id]
    assert [event.event_type for event in state_store.events] == ["started"]


async def test_process_one_requeues_when_started_state_cannot_be_persisted(
    queue: RedisAgentRunQueue,
) -> None:
    """Run 已出队但启动状态写库失败时必须放回队列，不能遗留假 running。"""

    class FailingStartedStateStore(InMemoryAgentRunStateStore):
        async def record_started(self, record) -> None:
            raise RuntimeError("sorry, too many clients already")

    record = await queue.enqueue("质检任务")
    assert await queue.dequeue(timeout=0.1) == record.run_id

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=FailingStartedStateStore(),
        worker_instance_id="worker-1",
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.QUEUED
    assert loaded.dispatch_attempt_count == 1
    assert loaded.worker_instance_id is None
    assert await queue.dequeue(timeout=0.1) == record.run_id


async def test_process_one_start_failure_honors_durable_cancel_before_requeue(
    queue: RedisAgentRunQueue,
) -> None:
    """启动状态写入失败时，长期取消事实必须阻止 Run 重新入队。"""

    class CancelDuringStartStore(InMemoryAgentRunStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.finish_calls = 0

        async def record_started(self, record) -> None:
            await self.record_cancel_requested(record.run_id)
            raise RuntimeError("task was canceled while starting")

        async def record_finished(self, record) -> None:
            self.finish_calls += 1
            if self.finish_calls == 1:
                raise ConnectionError("postgres still unavailable")
            await super().record_finished(record)

    record = await queue.enqueue("启动取消任务")
    assert await queue.dequeue(timeout=0.1) == record.run_id
    state_store = CancelDuringStartStore()

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=state_store,
        worker_instance_id="worker-1",
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.CANCELED
    assert loaded.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value
    assert loaded.worker_instance_id is None
    assert loaded.cancellation_finalized is False
    assert await queue.dequeue(timeout=0.1) is None

    await worker_module._reconcile_orphaned_runs(queue, state_store)

    reconciled = await queue.get(record.run_id)
    assert reconciled is not None
    assert reconciled.status is AgentRunQueueStatus.CANCELED
    assert reconciled.cancellation_finalized is True
    assert state_store.finish_calls == 2


async def test_startup_reconcile_restores_only_ready_app_runs(
    queue: RedisAgentRunQueue,
) -> None:
    due = _app_recovery_record("run-app-due", status=AgentRunQueueStatus.RUNNING)
    future = _app_recovery_record("run-app-future", status=AgentRunQueueStatus.RUNNING)
    active = _app_recovery_record("run-app-active", status=AgentRunQueueStatus.RUNNING)
    repository = FakeAppRunRecovery(
        snapshots=[
            _recovery_snapshot(
                due,
                ready=True,
                reason="bootstrap_not_started",
            ),
            _recovery_snapshot(
                future,
                ready=False,
                reason="exploration_work_waiting",
            ),
            _recovery_snapshot(
                active,
                ready=False,
                reason="run_execution_lease_active",
                execution_lease_active=True,
            ),
        ]
    )

    metric_before = app_metric_value(APP_RUN_RECOVERY_OUTCOME_TOTAL)
    restored = await worker_module._reconcile_recoverable_app_runs(queue, repository)

    assert restored == 1
    assert await queue.queue_depth() == 1
    assert await queue.dequeue(timeout=0.1) == due.run_id
    assert await queue.get(future.run_id) is None
    assert await queue.get(active.run_id) is None
    assert repository.order == ["list:100"]
    assert app_metric_value(APP_RUN_RECOVERY_OUTCOME_TOTAL) == metric_before + 1


async def test_periodic_app_reconcile_retries_pg_ready_snapshot_without_future_enqueue(
    queue: RedisAgentRunQueue,
) -> None:
    due = _app_recovery_record("run-app-periodic", status=AgentRunQueueStatus.QUEUED)
    future = _app_recovery_record("run-app-periodic-future", status=AgentRunQueueStatus.RUNNING)
    repository = FakeAppRunRecovery(
        snapshots=[
            _recovery_snapshot(due, ready=True, reason="pending_run_not_dispatched"),
            _recovery_snapshot(
                future,
                ready=False,
                reason="exploration_work_waiting",
            ),
        ]
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        worker_module._app_run_recovery_loop(
            queue,
            repository,
            stop_event,
            interval_seconds=0.01,
        )
    )
    try:
        for _ in range(20):
            if await queue.get(due.run_id) is not None:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("周期恢复未补回 ready App Run")
    finally:
        stop_event.set()
        await task

    assert await queue.queue_depth() == 1
    assert await queue.dequeue(timeout=0.1) == due.run_id
    assert await queue.get(future.run_id) is None
    assert len([item for item in repository.order if item.startswith("list:")]) >= 1


async def test_orphan_app_with_active_execution_lease_is_not_terminalized(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue(
        "App 活跃租约",
        task_id="task-app-active-lease",
        task_config=_app_task_config(),
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-app-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    state_store = InMemoryAgentRunStateStore()
    repository = FakeAppRunRecovery(
        snapshot=_recovery_snapshot(
            _app_recovery_record(
                record.run_id,
                status=AgentRunQueueStatus.RUNNING,
                task_id=record.task_id,
            ),
            ready=True,
            reason="run_execution_lease_active",
            execution_lease_active=True,
        )
    )

    await worker_module._reconcile_orphaned_runs(
        queue,
        state_store,
        app_run_recovery=repository,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.RUNNING
    assert loaded.worker_missing_since == record.worker_missing_since
    assert state_store.events == []
    assert repository.get_calls == [record.run_id]


async def test_orphan_canceled_app_with_active_lease_waits_for_current_owner(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue(
        "App 活租约期间取消",
        task_id="task-app-cancel-active-lease",
        task_config=_app_task_config(),
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-app-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    assert await queue.request_cancel(record.run_id) is True
    repository = FakeAppRunRecovery(
        snapshot=_recovery_snapshot(
            _app_recovery_record(
                record.run_id,
                status=AgentRunQueueStatus.RUNNING,
                task_id=record.task_id,
            ),
            ready=False,
            reason="run_execution_lease_active",
            execution_lease_active=True,
        )
    )
    state_store = InMemoryAgentRunStateStore()

    await worker_module._reconcile_orphaned_runs(
        queue,
        state_store,
        app_run_recovery=repository,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.RUNNING
    assert state_store.events == []
    assert repository.get_calls == [record.run_id]


async def test_orphan_app_ready_snapshot_requeues_the_original_run_id(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue(
        "App 可恢复租约",
        task_id="task-app-recoverable",
        task_config=_app_task_config(),
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-app-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    recovered = _app_recovery_record(
        record.run_id,
        status=AgentRunQueueStatus.RUNNING,
        task_id=record.task_id,
    )
    repository = FakeAppRunRecovery(
        snapshot=_recovery_snapshot(
            recovered,
            ready=True,
            reason="app_ready_without_exploration_work",
        )
    )
    state_store = InMemoryAgentRunStateStore()

    await worker_module._reconcile_orphaned_runs(
        queue,
        state_store,
        app_run_recovery=repository,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.QUEUED
    assert loaded.run_id == record.run_id
    assert loaded.worker_instance_id is None
    assert loaded.finished_at is None
    assert state_store.events == []
    assert await queue.dequeue(timeout=0.1) == record.run_id


async def test_orphan_app_restores_terminal_postgres_projection_when_snapshot_is_absent(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue(
        "App PG 已终态但 Redis 仍运行中",
        task_id="task-app-terminal-projection",
        task_config=_app_task_config(),
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-app-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    terminal = record.model_copy(
        update={
            "status": AgentRunQueueStatus.COMPLETED,
            "finished_at": datetime.now(UTC),
            "termination_reason": TerminationReason.COMPLETED.value,
            "worker_instance_id": None,
            "worker_missing_since": None,
        }
    )
    state_store = InMemoryAgentRunStateStore()
    await state_store.record_run_finished(terminal)
    repository = FakeAppRunRecovery(snapshot=None)

    await worker_module._reconcile_orphaned_runs(
        queue,
        state_store,
        app_run_recovery=repository,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.COMPLETED
    assert loaded.termination_reason == TerminationReason.COMPLETED.value
    assert loaded.worker_instance_id is None
    assert repository.get_calls == [record.run_id]


async def test_orphan_h5_does_not_query_app_recovery_repository(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await queue.enqueue(
        "H5 孤儿 Run",
        task_id="task-h5-orphan",
        task_config=_h5_v2_task_config(),
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-h5-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    state_store = InMemoryAgentRunStateStore()

    class NeverCalledAppRecovery(FakeAppRunRecovery):
        async def get_recovery_snapshot(self, *, agent_run_id: str):
            raise AssertionError(f"H5 孤儿 Run 不得读取 App 恢复快照：{agent_run_id}")

    async def cleanup_runtime(*, task_id: str) -> int:
        assert task_id == record.task_id
        return 0

    monkeypatch.setattr(
        worker_module,
        "_fail_h5_orphaned_nonterminal_runtime",
        cleanup_runtime,
    )

    await worker_module._reconcile_orphaned_runs(
        queue,
        state_store,
        app_run_recovery=NeverCalledAppRecovery(),
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.FAILED
    assert [event.event_type for event in state_store.events] == ["finished"]


async def test_orphan_h5_cleans_nonterminal_runtime_before_finishing_run(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """孤儿 H5 Run 必须先收束 Work 与 Action，避免 Completion Gate 永久卡住。"""

    record = await queue.enqueue(
        "H5 孤儿 Run 收束",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config=_h5_v2_task_config(),
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-h5-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    order: list[str] = []

    async def cleanup_runtime(*, task_id: str) -> int:
        assert task_id == record.task_id
        order.append("cleanup")
        return 2

    class CleanupAwareStore(InMemoryAgentRunStateStore):
        async def record_finished(self, finished) -> None:
            assert order == ["cleanup"]
            order.append("finish")
            await super().record_finished(finished)

    monkeypatch.setattr(
        worker_module,
        "_fail_h5_orphaned_nonterminal_runtime",
        cleanup_runtime,
        raising=False,
    )

    await worker_module._reconcile_orphaned_runs(queue, CleanupAwareStore())

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.FAILED
    assert order == ["cleanup", "finish"]


async def test_orphan_h5_cleanup_failure_keeps_run_recoverable(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL 收束短断时不得先把 Redis Run 终态化。"""

    record = await queue.enqueue(
        "H5 孤儿 Run 收束短断",
        task_id="11111111-1111-1111-1111-111111111111",
        task_config=_h5_v2_task_config(),
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-h5-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)

    async def cleanup_runtime(*, task_id: str) -> int:
        assert task_id == record.task_id
        raise ConnectionError("postgres cleanup unavailable")

    monkeypatch.setattr(
        worker_module,
        "_fail_h5_orphaned_nonterminal_runtime",
        cleanup_runtime,
    )
    state_store = InMemoryAgentRunStateStore()

    await worker_module._reconcile_orphaned_runs(queue, state_store)

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.RUNNING
    assert loaded.worker_instance_id == "dead-h5-worker"
    assert state_store.events == []


async def test_orphan_web_full_uses_durable_reconciliation_and_audited_report_generator(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web full 孤儿恢复必须沿用正式对账和审计报告边界。"""

    task_id = "11111111-1111-1111-1111-111111111111"
    record = await queue.enqueue(
        "Web full gate-ready 孤儿 Run",
        task_id=task_id,
        task_config={
            "workflow_version": "v2",
            "web_runtime_version": "web-runtime.v2",
            "execution_mode": "full_inspection",
            "web_inspection_notification_mode": "enforced",
            "_task_target": {
                "platform": "web",
                "target_url": "https://example.test",
            },
        },
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-web-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    authoritative = record.model_copy(
        update={
            "status": AgentRunQueueStatus.COMPLETED,
            "finished_at": datetime.now(UTC),
            "termination_reason": TerminationReason.COMPLETED.value,
            "worker_instance_id": None,
            "worker_missing_since": None,
        },
        deep=True,
    )

    class DurableStore(InMemoryAgentRunStateStore):
        async def get_finished_record(self, *, run_id: str):
            assert run_id == record.run_id
            return authoritative.model_copy(deep=True)

    class Finalization:
        async def get(self, *, task_id: str):
            assert task_id == record.task_id
            return SimpleNamespace(
                agent_run_id=record.run_id,
                finalization_epoch=0,
            )

    reconciliation_calls: list[object] = []

    class Reconciliation:
        async def reconcile(self, command):
            reconciliation_calls.append(command)
            return SimpleNamespace(status="gate_ready", execution_status="completed")

    audited_generator = object()
    finish_calls: list[tuple[AgentRunRecord, object]] = []

    async def finish_standard_terminal_run(**kwargs) -> None:
        finish_calls.append((kwargs["record"], kwargs["final_report_generator"]))

    async def forbidden_h5_cleanup(*, task_id: str) -> int:
        raise AssertionError(f"Web full 不得走 H5 孤儿清理：{task_id}")

    monkeypatch.setattr(
        worker_module,
        "_finish_standard_terminal_run",
        finish_standard_terminal_run,
    )
    monkeypatch.setattr(
        worker_module,
        "_fail_h5_orphaned_nonterminal_runtime",
        forbidden_h5_cleanup,
    )

    await worker_module._reconcile_orphaned_runs(
        queue,
        DurableStore(),
        task_finalization_service=Finalization(),
        web_inspection_reconciliation=Reconciliation(),
        web_final_report_generator=audited_generator,
        web_formal_session_closer=object(),
    )

    assert len(reconciliation_calls) == 1
    assert len(finish_calls) == 1
    finished_record, generator = finish_calls[0]
    assert finished_record.status is AgentRunQueueStatus.COMPLETED
    assert generator is audited_generator


async def test_orphan_web_full_sealed_replay_only_projects_authoritative_run(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已封存 Web full 孤儿只能刷新 Redis 投影，不得重复进入完成门。"""

    task_id = "11111111-1111-1111-1111-111111111111"
    record = await queue.enqueue(
        "Web full 已封存孤儿 Run",
        task_id=task_id,
        task_config={
            "workflow_version": "v2",
            "web_runtime_version": "web-runtime.v2",
            "execution_mode": "full_inspection",
            "web_inspection_notification_mode": "enforced",
            "_task_target": {
                "platform": "web",
                "target_url": "https://example.test",
            },
        },
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-web-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    authoritative = record.model_copy(
        update={
            "status": AgentRunQueueStatus.COMPLETED,
            "finished_at": datetime.now(UTC),
            "termination_reason": TerminationReason.COMPLETED.value,
            "worker_instance_id": None,
            "worker_missing_since": None,
        },
        deep=True,
    )

    class DurableStore(InMemoryAgentRunStateStore):
        async def get_finished_record(self, *, run_id: str):
            assert run_id == record.run_id
            return authoritative.model_copy(deep=True)

    class Finalization:
        async def get(self, *, task_id: str):
            assert task_id == record.task_id
            return SimpleNamespace(
                agent_run_id=record.run_id,
                finalization_epoch=0,
            )

    reconciliation_calls: list[object] = []

    class Reconciliation:
        async def reconcile(self, command):
            reconciliation_calls.append(command)
            return SimpleNamespace(status="already_terminal", execution_status="needs_review")

    async def forbidden_finish(**_kwargs) -> None:
        raise AssertionError("已封存任务不得重复进入完成门")

    monkeypatch.setattr(
        worker_module,
        "_finish_standard_terminal_run",
        forbidden_finish,
    )

    await worker_module._reconcile_orphaned_runs(
        queue,
        DurableStore(),
        task_finalization_service=Finalization(),
        web_inspection_reconciliation=Reconciliation(),
        web_final_report_generator=object(),
        web_formal_session_closer=object(),
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.COMPLETED
    assert loaded.worker_instance_id is None
    assert loaded.worker_missing_since is None
    assert len(reconciliation_calls) == 1


async def test_orphan_web_full_closes_frontier_before_persisting_failed_run(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探索中失联时必须先收束 Frontier，再允许正式对账读取失败 Run。"""

    task_id = "11111111-1111-1111-1111-111111111111"
    record = await queue.enqueue(
        "Web full 探索中孤儿 Run",
        task_id=task_id,
        task_config={
            "workflow_version": "v2",
            "web_runtime_version": "web-runtime.v2",
            "execution_mode": "full_inspection",
            "web_inspection_notification_mode": "enforced",
            "_task_target": {"platform": "web"},
        },
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-web-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    order: list[str] = []

    class DurableStore(InMemoryAgentRunStateStore):
        async def record_run_finished(self, finished) -> None:
            assert order == ["cleanup"]
            order.append("run_terminal")
            await super().record_run_finished(finished)

    async def cleanup(*, task_id: str) -> int:
        assert task_id == record.task_id
        order.append("cleanup")
        return 2

    class Finalization:
        async def get(self, *, task_id: str):
            assert task_id == record.task_id
            return SimpleNamespace(agent_run_id=record.run_id, finalization_epoch=0)

    class Reconciliation:
        async def reconcile(self, command):
            assert order == ["cleanup", "run_terminal"]
            order.append("reconcile")
            return SimpleNamespace(status="failed", execution_status="failed")

    monkeypatch.setattr(
        worker_module,
        "_fail_h5_orphaned_nonterminal_runtime",
        cleanup,
    )

    await worker_module._reconcile_orphaned_runs(
        queue,
        DurableStore(),
        task_finalization_service=Finalization(),
        web_inspection_reconciliation=Reconciliation(),
        web_final_report_generator=object(),
        web_formal_session_closer=object(),
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.FAILED
    assert order == ["cleanup", "run_terminal", "reconcile"]


async def test_orphan_web_full_cancel_race_switches_to_durable_cancel_recovery(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写 Run 终态时取消胜出，启动扫描不得抛错或进入 Gate。"""

    task_id = "11111111-1111-1111-1111-111111111111"
    record = await queue.enqueue(
        "Web full 孤儿取消竞态",
        task_id=task_id,
        task_config={
            "workflow_version": "v2",
            "web_runtime_version": "web-runtime.v2",
            "execution_mode": "full_inspection",
            "web_inspection_notification_mode": "enforced",
            "_task_target": {"platform": "web"},
        },
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.worker_instance_id = "dead-web-worker"
    record.worker_missing_since = datetime.now(UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)

    class CancelWinningStore(InMemoryAgentRunStateStore):
        async def record_run_finished(self, _record) -> None:
            raise AgentRunCancellationConflict("cancel won")

    async def cleanup(*, task_id: str) -> int:
        assert task_id == record.task_id
        return 1

    cancel_recovery_calls: list[tuple[AgentRunRecord, object]] = []

    async def reconcile_canceled_run(
        _queue,
        _state_store,
        canceled_record,
        *,
        web_formal_session_closer=None,
        web_formal_terminal_ledger_finalizer=None,
    ) -> None:
        assert web_formal_terminal_ledger_finalizer is None
        cancel_recovery_calls.append(
            (canceled_record, web_formal_session_closer)
        )

    class Finalization:
        async def get(self, *, task_id: str):
            raise AssertionError(f"取消胜出后不得读取 Gate：{task_id}")

    class Reconciliation:
        async def reconcile(self, command):
            raise AssertionError(f"取消胜出后不得调和：{command}")

    closer = object()
    monkeypatch.setattr(
        worker_module,
        "_fail_h5_orphaned_nonterminal_runtime",
        cleanup,
    )
    monkeypatch.setattr(
        worker_module,
        "_reconcile_canceled_run",
        reconcile_canceled_run,
    )

    await worker_module._reconcile_orphaned_runs(
        queue,
        CancelWinningStore(),
        task_finalization_service=Finalization(),
        web_inspection_reconciliation=Reconciliation(),
        web_final_report_generator=object(),
        web_formal_session_closer=closer,
    )

    assert cancel_recovery_calls == [(record, closer)]


async def test_worker_marks_missing_worker_before_terminalizing_orphaned_run(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue("质检任务")
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.started_at = worker_module.datetime.now(worker_module.UTC)
    record.worker_instance_id = "dead-worker"
    await queue.save(record)
    state_store = InMemoryAgentRunStateStore()

    await worker_module._reconcile_orphaned_runs(queue, state_store)

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.RUNNING
    assert loaded.worker_missing_since is not None
    assert state_store.events == []


async def test_worker_periodically_terminalizes_orphan_after_heartbeat_expires(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue("质检任务")
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.started_at = worker_module.datetime.now(worker_module.UTC)
    record.worker_instance_id = "expired-worker"
    record.worker_missing_since = worker_module.datetime.now(worker_module.UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    state_store = InMemoryAgentRunStateStore()
    stop_event = asyncio.Event()

    task = asyncio.create_task(
        worker_module._orphan_reconciliation_loop(
            queue,
            state_store,
            stop_event,
            interval_seconds=0.01,
        )
    )
    for _ in range(20):
        loaded = await queue.get(record.run_id)
        if loaded is not None and loaded.status == AgentRunQueueStatus.FAILED:
            break
        await asyncio.sleep(0.01)
    stop_event.set()
    await task

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.FAILED
    assert [event.event_type for event in state_store.events] == ["finished"]


async def test_orphan_reconciliation_uses_durable_cancel_when_redis_key_is_missing(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue(
        "H5 孤儿取消对账",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.started_at = worker_module.datetime.now(worker_module.UTC)
    record.worker_instance_id = "expired-worker"
    record.worker_missing_since = worker_module.datetime.now(worker_module.UTC) - timedelta(
        seconds=worker_module.ORPHAN_RECONCILIATION_GRACE_SECONDS + 1
    )
    await queue.save(record)
    state_store = InMemoryAgentRunStateStore()
    await state_store.record_cancel_requested(record.run_id)

    await worker_module._reconcile_orphaned_runs(queue, state_store)

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status is AgentRunQueueStatus.CANCELED
    assert loaded.termination_reason == TerminationReason.CANCELED.value
    assert loaded.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value


async def test_worker_clears_missing_marker_when_heartbeat_recovers(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue("质检任务")
    assert await queue.dequeue(timeout=0.1) == record.run_id
    record.status = AgentRunQueueStatus.RUNNING
    record.started_at = worker_module.datetime.now(worker_module.UTC)
    record.worker_instance_id = "recovering-worker"
    await queue.save(record)
    state_store = InMemoryAgentRunStateStore()

    await worker_module._reconcile_orphaned_runs(queue, state_store)
    missing = await queue.get(record.run_id)
    assert missing is not None
    assert missing.worker_missing_since is not None

    await queue.record_worker_heartbeat("recovering-worker")
    await worker_module._reconcile_orphaned_runs(queue, state_store)

    recovered = await queue.get(record.run_id)
    assert recovered is not None
    assert recovered.status == AgentRunQueueStatus.RUNNING
    assert recovered.worker_missing_since is None
    assert state_store.events == []


async def test_cancel_check_fails_open_on_transient_redis_error() -> None:
    class FailingQueue:
        async def is_cancel_requested(self, run_id: str) -> bool:
            del run_id
            raise ConnectionError("redis connection reset")

    assert (
        await worker_module._is_cancel_requested_safely(
            FailingQueue(),
            "run-1",
        )
        is False
    )


async def test_worker_loop_survives_transient_dequeue_error() -> None:
    stop_event = asyncio.Event()

    class FailingQueue:
        async def dequeue(self, *, timeout: float) -> str | None:
            del timeout
            stop_event.set()
            raise ConnectionError("redis connection reset")

    await worker_module._worker_loop(
        0,
        FailingQueue(),
        Settings(),
        stop_event,
        InMemoryAgentRunStateStore(),
    )


def test_worker_instance_id_is_unique_across_container_restarts() -> None:
    first = worker_module._new_worker_instance_id()
    second = worker_module._new_worker_instance_id()

    assert first != second
    assert first.startswith(f"{worker_module.socket.gethostname()}:{worker_module.os.getpid()}:")


async def test_process_one_marks_failed_when_agent_construction_raises(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """`build_default_main_agent()` 失败（多为 LLM 配置缺失/错误）必须写回 FAILED 且
    携带错误信息，而不是让记录永远停留在 RUNNING（此前的真实缺陷：该调用发生在
    标记 RUNNING 之后、try 块之外，异常会被 `_worker_loop` 吞掉但记录不会再更新）。
    """

    def _boom() -> None:
        raise RuntimeError("ALIOTH_LLM_API_KEY 未配置")

    monkeypatch.setattr(worker_module, "build_default_main_agent", _boom)

    state_store = InMemoryAgentRunStateStore()
    record = await queue.enqueue("质检任务")
    await worker_module._process_one(queue, record.run_id, Settings(), state_store=state_store)

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.FAILED
    assert loaded.finished_at is not None
    assert loaded.termination_reason == TerminationReason.ERROR.value
    assert loaded.error_message == "ALIOTH_LLM_API_KEY 未配置"
    assert loaded.error_category == ErrorCategory.LLM_PROVIDER.value
    assert [event.event_type for event in state_store.events] == ["started", "finished"]
    assert state_store.events[0].snapshot is not None
    assert state_store.events[0].snapshot.status == AgentRunQueueStatus.RUNNING
    assert state_store.events[1].snapshot is not None
    assert state_store.events[1].snapshot.status == AgentRunQueueStatus.FAILED


async def test_process_one_applies_successful_run_result(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """agent 构建成功、`run_main_agent` 正常返回时，结果字段应正确写回记录
    （覆盖此前完全没有测试覆盖的 `_process_one` 正常路径与 `_apply_result`）。
    """
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())

    prepared_sink = InMemoryActionTraceSink()
    prepared_sink.record(
        ActionTraceEntry(
            step=1,
            tool_name="echo_tool",
            arguments='{"message": "hello"}',
            status="ok",
            duration_seconds=0.01,
        )
    )
    working_memory = WorkingMemory(step_count=2)

    async def _fake_run_main_agent(
        agent, message, *, runtime_settings, is_cancelled=None, sink=None, extra_middleware=None
    ):
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=working_memory,
            trace_sink=prepared_sink,
            final_text="质检完成，未发现异常",
        )

    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)

    state_store = InMemoryAgentRunStateStore()
    record = await queue.enqueue("质检任务")
    await worker_module._process_one(queue, record.run_id, Settings(), state_store=state_store)

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.COMPLETED
    assert loaded.final_text == "质检完成，未发现异常"
    assert loaded.step_count == 2
    assert loaded.termination_reason == TerminationReason.COMPLETED.value
    assert loaded.action_trace[0]["tool_name"] == "echo_tool"
    assert [event.event_type for event in state_store.events] == ["started", "finished"]
    assert state_store.events[1].snapshot is not None
    assert state_store.events[1].snapshot.status == AgentRunQueueStatus.COMPLETED
    assert state_store.events[1].snapshot.action_trace[0]["tool_name"] == "echo_tool"


async def test_process_one_keeps_completed_run_and_routes_local_page_failure_to_review(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """局部页面失败不改变 Run 技术完成态，但业务任务和报告必须进入待复核。"""
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())
    working_memory = WorkingMemory(step_count=3)
    working_memory.remember_page_capture(
        page_capture_id="capture-failed",
        page_url="https://example.com/failed",
        page_title="异常页面",
        page_seq=1,
        artifact_ids=[],
    )
    assert working_memory.fail_page_inspection(
        page_capture_id="capture-failed",
        failure_stage="rule_persistence",
        failure_code="RULE_EXECUTION_WRITE_FAILED",
        failure_message="规则结果写入失败",
        retryable=True,
    )

    async def _fake_run_main_agent(
        agent, message, *, runtime_settings, is_cancelled=None, sink=None, extra_middleware=None
    ):
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=working_memory,
            trace_sink=InMemoryActionTraceSink(),
            final_text="动态页面探索已完成，存在局部异常",
            execution_outcome=ExecutionOutcome.NEEDS_REVIEW,
        )

    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)
    state_store = InMemoryAgentRunStateStore()
    report_generator = FakeFinalReportGenerator()
    record = await queue.enqueue("质检任务", task_id="11111111-1111-1111-1111-111111111111")

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=state_store,
        rule_execution_writer=FakeRuleExecutionWriter(),
        final_report_generator=report_generator,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.COMPLETED
    assert loaded.termination_reason == TerminationReason.COMPLETED.value
    assert loaded.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value
    assert report_generator.calls[0][3] == "review"
    assert report_generator.termination_reasons == ["completed"]
    assert report_generator.page_explorations[0]["complete"] is True
    assert report_generator.page_explorations[0]["page_total"] == 1
    assert report_generator.page_explorations[0]["failed_page_count"] == 1
    assert report_generator.page_explorations[0]["pending_work_count"] == 0
    assert loaded.final_report is not None
    observability = loaded.final_report["run_observability"]
    assert observability["rss_scope"] == "worker_process"
    assert observability["working_memory_bytes"] > 0
    assert observability["action_trace_count"] == 0
    assert "worker_rss_before_bytes" in observability
    assert "worker_rss_after_bytes" in observability


async def test_process_one_closes_page_capture_session(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())
    closed_sessions: list[str] = []

    async def _fake_run_main_agent(
        agent, message, *, runtime_settings, is_cancelled=None, sink=None, extra_middleware=None
    ):
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(step_count=1),
            trace_sink=InMemoryActionTraceSink(),
            final_text="质检完成",
        )

    async def _close_session(run_id: str) -> None:
        closed_sessions.append(run_id)

    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)
    monkeypatch.setattr(worker_module, "close_page_capture_session", _close_session)

    state_store = InMemoryAgentRunStateStore()
    record = await queue.enqueue("质检任务")
    await worker_module._process_one(queue, record.run_id, Settings(), state_store=state_store)

    assert closed_sessions == [record.run_id]


async def test_process_one_marks_failed_when_agent_returns_error(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """Agent 编排已判定失败时，队列状态不得继续伪装成 completed。"""
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())

    async def _fake_run_main_agent(
        agent, message, *, runtime_settings, is_cancelled=None, sink=None, extra_middleware=None
    ):
        return RunResult(
            termination_reason=TerminationReason.ERROR,
            working_memory=WorkingMemory(step_count=0),
            trace_sink=InMemoryActionTraceSink(),
            error_message="Agent 未返回文本且未执行任何工具，不能判定为完成。",
            error_category=ErrorCategory.UNKNOWN,
        )

    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)

    state_store = InMemoryAgentRunStateStore()
    record = await queue.enqueue("质检任务")
    await worker_module._process_one(queue, record.run_id, Settings(), state_store=state_store)

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.FAILED
    assert loaded.termination_reason == TerminationReason.ERROR.value
    assert loaded.step_count == 0
    assert loaded.error_category == ErrorCategory.UNKNOWN.value
    assert state_store.events[-1].snapshot is not None
    assert state_store.events[-1].snapshot.status == AgentRunQueueStatus.FAILED


@pytest.mark.parametrize(
    "termination_reason",
    [
        TerminationReason.TIMEOUT,
        TerminationReason.MAX_STEPS_REACHED,
        TerminationReason.STUCK_DETECTED,
        TerminationReason.INCOMPLETE,
    ],
)
async def test_process_one_routes_unfinished_termination_to_review_task(
    monkeypatch: pytest.MonkeyPatch,
    queue: RedisAgentRunQueue,
    termination_reason: TerminationReason,
) -> None:
    """未完成或命中运行护栏时不得落成成功终态。"""
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())

    async def _fake_run_main_agent(
        agent, message, *, runtime_settings, is_cancelled=None, sink=None, extra_middleware=None
    ):
        return RunResult(
            termination_reason=termination_reason,
            working_memory=WorkingMemory(step_count=3),
            trace_sink=InMemoryActionTraceSink(),
        )

    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)
    state_store = InMemoryAgentRunStateStore()
    writer = FakeRuleExecutionWriter()
    report_generator = FakeFinalReportGenerator()
    record = await queue.enqueue("质检任务", task_id="11111111-1111-1111-1111-111111111111")

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=state_store,
        rule_execution_writer=writer,
        final_report_generator=report_generator,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.FAILED
    assert loaded.termination_reason == termination_reason.value
    assert loaded.step_count == 3
    assert state_store.events[-1].snapshot is not None
    assert state_store.events[-1].snapshot.status == AgentRunQueueStatus.FAILED
    assert len(writer.calls) == 1
    assert report_generator.calls[0][3] == "review"
    assert report_generator.termination_reasons == [termination_reason.value]
    assert loaded.final_report is not None
    assert loaded.final_report["task"]["status"] == "review"
    assert loaded.final_report["summary"]["overall_status"] == "incomplete"


async def test_process_one_initializes_rule_executions_for_bound_task(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """绑定业务任务的 Run 成功结束后，应初始化该任务的规则执行记录。"""
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())

    prepared_sink = InMemoryActionTraceSink()
    prepared_sink.record(
        ActionTraceEntry(
            step=1,
            tool_name="candidate_rules_tool",
            arguments='{"page_capture_id": "capture-1", "page_type": "home"}',
            status="ok",
            duration_seconds=0.01,
        )
    )

    async def _fake_run_main_agent(
        agent, message, *, runtime_settings, is_cancelled=None, sink=None, extra_middleware=None
    ):
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(step_count=1),
            trace_sink=prepared_sink,
            final_text="质检完成",
        )

    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)

    state_store = InMemoryAgentRunStateStore()
    writer = FakeRuleExecutionWriter()
    record = await queue.enqueue("质检任务", task_id="11111111-1111-1111-1111-111111111111")
    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=state_store,
        rule_execution_writer=writer,
        final_report_generator=FakeFinalReportGenerator(),
    )

    assert len(writer.calls) == 1
    (
        task_id,
        agent_run_id,
        finished_at,
        candidate_pages,
        runtime_rule_fallbacks,
        runtime_page_failure_fallbacks,
    ) = writer.calls[0]
    assert task_id == "11111111-1111-1111-1111-111111111111"
    assert agent_run_id == record.run_id
    assert finished_at is not None
    assert candidate_pages == [
        CandidatePageReference(page_capture_id="capture-1", page_type="home")
    ]


async def test_process_one_exposes_unresolved_candidate_rules(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """Agent 完成但漏判候选规则时，最终结果必须显式提示任务转人工复核。"""
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())

    async def _fake_run_main_agent(
        agent, message, *, runtime_settings, is_cancelled=None, sink=None, extra_middleware=None
    ):
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(step_count=1),
            trace_sink=InMemoryActionTraceSink(),
            final_text="质检完成",
        )

    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)

    state_store = InMemoryAgentRunStateStore()
    writer = FakeRuleExecutionWriter(unresolved_candidate_count=3)
    report_generator = FakeFinalReportGenerator(unresolved_candidate_count=3)
    record = await queue.enqueue("质检任务", task_id="11111111-1111-1111-1111-111111111111")
    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=state_store,
        rule_execution_writer=writer,
        final_report_generator=report_generator,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.FAILED
    assert loaded.termination_reason == TerminationReason.INCOMPLETE.value
    assert loaded.final_text == (
        "质检完成\n\n系统完整性校验发现 3 条候选规则未完成判定，任务已转人工复核。"
    )
    assert len(report_generator.calls) == 2
    report_task_id, report_run_id, finalized_at, terminal_task_status = report_generator.calls[-1]
    assert report_task_id == "11111111-1111-1111-1111-111111111111"
    assert report_run_id == record.run_id
    assert finalized_at is not None
    assert terminal_task_status == "review"
    assert loaded.final_report is not None
    assert loaded.final_report["task"]["status"] == "review"
    assert loaded.final_report["summary"]["unresolved_candidate_rules"] == 3


async def test_process_one_rejects_completed_run_with_incomplete_final_report(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """没有形成候选规则执行的任务不得以 completed/done 假完成。"""
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())

    async def _fake_run_main_agent(
        agent, message, *, runtime_settings, is_cancelled=None, sink=None, extra_middleware=None
    ):
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(step_count=8),
            trace_sink=InMemoryActionTraceSink(),
            final_text="质检完成",
        )

    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)
    report_generator = FakeFinalReportGenerator(
        overall_status="incomplete",
        candidate_execution_complete=False,
    )
    state_store = InMemoryAgentRunStateStore()
    record = await queue.enqueue("质检任务", task_id="11111111-1111-1111-1111-111111111111")

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=state_store,
        rule_execution_writer=FakeRuleExecutionWriter(),
        final_report_generator=report_generator,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.FAILED
    assert loaded.termination_reason == TerminationReason.INCOMPLETE.value
    assert loaded.final_text == ("质检完成\n\n最终报告完整性校验未通过，任务已转人工复核。")
    assert len(report_generator.calls) == 2
    assert report_generator.calls[-1][3] == "review"
    assert loaded.final_report is not None
    assert loaded.final_report["task"]["status"] == "review"
    assert loaded.final_report["summary"]["overall_status"] == "incomplete"
    assert state_store.events[-1].snapshot is not None
    assert state_store.events[-1].snapshot.status == AgentRunQueueStatus.FAILED


def test_reconcile_incomplete_final_report_keeps_budget_limited_run_completed() -> None:
    """采样预算已耗尽且探索无待办时，候选缺口应转 review，而不是撤销 completed。"""
    record = AgentRunRecord(
        run_id="run-1",
        task_id="11111111-1111-1111-1111-111111111111",
        message="质检任务",
        status=AgentRunQueueStatus.COMPLETED,
        submitted_at=datetime.now(UTC),
        termination_reason=TerminationReason.COMPLETED.value,
        final_text="质检完成",
        action_trace=[
            {
                "step": 1,
                "tool_name": "page_capture_tool",
                "status": "denied",
                "arguments": '{"mode": "single_trigger"}',
                "duration_seconds": 0.01,
                "result": '{"error_code": "PAGE_SAMPLING_LIMIT_REACHED"}',
            }
        ],
        final_report={
            "summary": {
                "candidate_execution_complete": False,
                "unresolved_candidate_rules": 2,
            },
            "page_exploration": {
                "complete": True,
                "pending_work_count": 0,
                "pending_action_count": 0,
            },
        },
    )

    assert worker_module._reconcile_incomplete_final_report(record) is True
    assert record.status == AgentRunQueueStatus.COMPLETED
    assert record.termination_reason == TerminationReason.COMPLETED.value
    assert record.execution_outcome == "needs_review"
    assert record.final_text == "质检完成"
    assert worker_module._reconcile_incomplete_final_report(record) is False


def test_reconcile_incomplete_final_report_keeps_review_without_unresolved_rules() -> None:
    """候选规则均有终态时，业务复核不能反向改写 Run 技术终态。"""
    record = AgentRunRecord(
        run_id="run-1",
        task_id="11111111-1111-1111-1111-111111111111",
        message="质检任务",
        status=AgentRunQueueStatus.COMPLETED,
        submitted_at=datetime.now(UTC),
        termination_reason=TerminationReason.COMPLETED.value,
        execution_outcome="needs_review",
        final_text="质检完成",
        final_report={
            "summary": {
                "candidate_execution_complete": False,
                "candidate_rule_count": 46,
                "completed_candidate_rules": 46,
                "unresolved_candidate_rules": 0,
                "evidence_gap_rules": 3,
            },
            "page_exploration": {
                "complete": True,
                "pending_work_count": 0,
                "pending_action_count": 0,
            },
        },
    )

    assert worker_module._reconcile_incomplete_final_report(record) is False
    assert record.status == AgentRunQueueStatus.COMPLETED
    assert record.termination_reason == TerminationReason.COMPLETED.value
    assert record.execution_outcome == "needs_review"
    assert record.final_text == "质检完成"


async def test_formal_work_preview_does_not_rewrite_completed_run_as_failed(
    queue: RedisAgentRunQueue,
) -> None:
    """异步 Work 尚未收口时由 Completion Gate 阻断，不得反向改写 Main Run 终态。"""

    class PendingInspectionReader:
        async def inspection_work_items(self, task_id: str, *, control_mode: str):
            assert task_id == "task-1"
            assert control_mode == "enforced"
            return {
                "summary": {
                    "work_item_count": 2,
                    "nonterminal_count": 2,
                    "status_counts": {"pending": 2},
                }
            }

    record = await queue.enqueue("H5 正式质检", task_id="task-1")
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    generator = FakeFinalReportGenerator(
        unresolved_candidate_count=2,
        candidate_execution_complete=False,
    )

    await worker_module._generate_final_report(
        record,
        generator,
        finalized=False,
        inspection_work_items_enabled=True,
        inspection_work_item_reader=PendingInspectionReader(),
        legacy_candidate_reconciliation_enabled=False,
    )

    assert record.status is AgentRunQueueStatus.COMPLETED
    assert record.termination_reason == TerminationReason.COMPLETED.value
    assert len(generator.calls) == 1
    assert record.final_report is not None
    assert record.final_report["report_stage"] == "preview"
    assert (
        record.final_report["run_observability"]["inspection_work_items"]["snapshot"][
            "nonterminal_count"
        ]
        == 2
    )


async def test_formal_work_finalization_disables_legacy_candidate_reconciliation(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正式异步 Work 的预览与最终报告都不得制造 Main Run 跨终态写入。"""

    class PendingTaskInsightService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def inspection_work_items(self, task_id: str, *, control_mode: str):
            assert task_id == "11111111-1111-1111-1111-111111111111"
            assert control_mode == "enforced"
            return {
                "summary": {
                    "work_item_count": 2,
                    "nonterminal_count": 2,
                    "status_counts": {"pending": 2},
                }
            }

    monkeypatch.setattr(worker_module, "TaskInsightService", PendingTaskInsightService)
    state_store = InMemoryAgentRunStateStore()
    finalization = FakeTaskFinalizationService(gate_passed=True)
    record = await queue.enqueue(
        "H5 正式质检收口",
        task_id="11111111-1111-1111-1111-111111111111",
    )
    record.status = AgentRunQueueStatus.COMPLETED
    record.finished_at = datetime.now(UTC)
    record.termination_reason = TerminationReason.COMPLETED.value
    finalization.recoverable_run_ids[record.task_id] = record.run_id
    generator = FakeFinalReportGenerator(
        unresolved_candidate_count=2,
        candidate_execution_complete=False,
    )

    await worker_module._finish_terminal_run(
        queue=queue,
        record=record,
        state_store=state_store,
        progress_recorder=None,
        final_report_generator=generator,
        task_finalization_service=finalization,
        inspection_work_items_enabled=True,
        legacy_report_enabled=True,
    )

    assert record.status is AgentRunQueueStatus.COMPLETED
    assert record.termination_reason == TerminationReason.COMPLETED.value
    assert len(generator.calls) == 2
    assert finalization.calls[-1] == "finalize:completed_with_gaps"


async def test_process_one_finalizes_failed_bound_run_and_generates_failure_report(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """绑定任务的 Agent 失败时，也必须补齐规则执行并保存失败终态报告。"""
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())
    prepared_sink = InMemoryActionTraceSink()
    prepared_sink.record(
        ActionTraceEntry(
            step=1,
            tool_name="candidate_rules_tool",
            arguments='{"page_capture_id": "capture-1", "page_type": "home"}',
            status="ok",
            duration_seconds=0.01,
        )
    )

    async def _fake_run_main_agent(
        agent, message, *, runtime_settings, is_cancelled=None, sink=None, extra_middleware=None
    ):
        return RunResult(
            termination_reason=TerminationReason.ERROR,
            working_memory=WorkingMemory(step_count=1),
            trace_sink=prepared_sink,
            error_message="LLM 请求超时",
            error_category=ErrorCategory.LLM_PROVIDER,
        )

    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)
    writer = FakeRuleExecutionWriter(unresolved_candidate_count=2)
    report_generator = FakeFinalReportGenerator(unresolved_candidate_count=2)
    record = await queue.enqueue("质检任务", task_id="11111111-1111-1111-1111-111111111111")

    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=InMemoryAgentRunStateStore(),
        rule_execution_writer=writer,
        final_report_generator=report_generator,
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.FAILED
    assert len(writer.calls) == 1
    assert writer.calls[0][3] == [
        CandidatePageReference(page_capture_id="capture-1", page_type="home")
    ]
    assert len(report_generator.calls) == 1
    assert report_generator.calls[0][3] == "failed"
    assert loaded.final_report is not None
    assert loaded.final_report["is_final"] is True
    assert loaded.final_report["task"]["status"] == "failed"
    assert loaded.final_report["summary"]["overall_status"] == "error"


async def test_process_one_marks_failed_when_rule_execution_initialization_fails(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """规则执行初始化失败时，应写回 FAILED，避免任务长期停留在 RUNNING。"""
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())

    async def _fake_run_main_agent(
        agent, message, *, runtime_settings, is_cancelled=None, sink=None, extra_middleware=None
    ):
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(step_count=1),
            trace_sink=InMemoryActionTraceSink(),
            final_text="质检完成",
        )

    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)

    state_store = InMemoryAgentRunStateStore()
    record = await queue.enqueue("质检任务", task_id="11111111-1111-1111-1111-111111111111")
    await worker_module._process_one(
        queue,
        record.run_id,
        Settings(),
        state_store=state_store,
        rule_execution_writer=FailingRuleExecutionWriter(),
    )

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.FAILED
    assert loaded.termination_reason == TerminationReason.ERROR.value
    assert loaded.error_message == "规则执行结果写入失败"
    assert loaded.error_category == ErrorCategory.UNKNOWN.value


async def test_process_one_marks_failed_on_infrastructure_error_during_run(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """`run_main_agent` 本身抛出意料之外异常（基础设施级）时，记录应写回 FAILED。"""
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())

    async def _raise(agent, message, *, runtime_settings, is_cancelled=None):
        raise RuntimeError("Redis 写入失败")

    monkeypatch.setattr(worker_module, "run_main_agent", _raise)

    state_store = InMemoryAgentRunStateStore()
    record = await queue.enqueue("质检任务")
    await worker_module._process_one(queue, record.run_id, Settings(), state_store=state_store)

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.FAILED
    assert loaded.finished_at is not None
    assert [event.event_type for event in state_store.events] == ["started", "finished"]
    assert state_store.events[1].snapshot is not None
    assert state_store.events[1].snapshot.status == AgentRunQueueStatus.FAILED


async def test_process_one_records_canceled_result_to_state_store(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """Agent Loop 返回 canceled 时，Redis 记录和长期状态 Store 都应进入取消终态。"""
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())

    async def _fake_run_main_agent(
        agent, message, *, runtime_settings, is_cancelled=None, sink=None, extra_middleware=None
    ):
        return RunResult(
            termination_reason=TerminationReason.CANCELED,
            working_memory=WorkingMemory(step_count=1),
            trace_sink=InMemoryActionTraceSink(),
            final_text=None,
        )

    monkeypatch.setattr(worker_module, "run_main_agent", _fake_run_main_agent)

    state_store = InMemoryAgentRunStateStore()
    record = await queue.enqueue("质检任务")
    await worker_module._process_one(queue, record.run_id, Settings(), state_store=state_store)

    loaded = await queue.get(record.run_id)
    assert loaded is not None
    assert loaded.status == AgentRunQueueStatus.CANCELED
    assert [event.event_type for event in state_store.events] == ["started", "finished"]
    assert state_store.events[1].snapshot is not None
    assert state_store.events[1].snapshot.status == AgentRunQueueStatus.CANCELED


async def test_live_action_trace_sink_streams_entries_and_drains() -> None:
    """LiveActionTraceSink 每步立即调度落库写入，drain 等待全部完成。"""

    class RecordingStore:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def insert_action_trace_entry(
            self, *, run_id: str, task_id: str, entry: dict
        ) -> None:
            self.calls.append({"run_id": run_id, "task_id": task_id, "entry": entry})

    store = RecordingStore()
    sink = worker_module.LiveActionTraceSink(
        state_store=store, run_id="run-live", task_id="task-live"
    )

    sink.record(
        ActionTraceEntry(
            step=1,
            tool_name="page_capture_tool",
            arguments='{"mode": "goto"}',
            status="ok",
            duration_seconds=0.5,
            result='{"execution_status": "success"}',
        )
    )
    await sink.drain()

    assert len(sink.entries) == 1
    assert len(store.calls) == 1
    assert store.calls[0]["run_id"] == "run-live"
    assert store.calls[0]["task_id"] == "task-live"
    assert store.calls[0]["entry"]["step"] == 1
    assert store.calls[0]["entry"]["result"] == '{"execution_status": "success"}'


async def test_live_action_trace_sink_survives_insert_failure() -> None:
    """单步写入失败不影响后续记录与 drain（终局批量写入兜底）。"""

    class FailingStore:
        async def insert_action_trace_entry(self, **kwargs) -> None:
            raise RuntimeError("db down")

    sink = worker_module.LiveActionTraceSink(
        state_store=FailingStore(), run_id="run-live", task_id="task-live"
    )

    sink.record(
        ActionTraceEntry(
            step=1,
            tool_name="echo_tool",
            arguments="{}",
            status="ok",
            duration_seconds=0.1,
        )
    )
    await sink.drain()

    assert len(sink.entries) == 1


class FakeProgressRecorder:
    """过程观察者接线断言用的假记录器。"""

    def __init__(self) -> None:
        self.started_count = 0
        self.finished: list[tuple[str, str | None, str | None]] = []
        self.finished_working_memories: list[dict] = []

    async def on_run_started(self) -> None:
        self.started_count += 1

    async def on_run_finished(
        self,
        *,
        status: str,
        termination_reason: str | None,
        error_message: str | None,
        working_memory: dict | None = None,
    ) -> None:
        self.finished.append((status, termination_reason, error_message))
        self.finished_working_memories.append(dict(working_memory or {}))


async def test_process_one_wires_progress_observer(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """启用过程观察者时：启动/终态钩子各触发一次，观察中间件进入 extra_middleware。"""
    from services.api.app.agents.runtime.progress_observer import (
        TaskProgressObserverMiddleware,
    )

    recorder = FakeProgressRecorder()
    captured: dict[str, object] = {}
    monkeypatch.setattr(worker_module, "build_task_progress_recorder", lambda **kwargs: recorder)
    monkeypatch.setattr(worker_module, "build_default_main_agent", lambda: object())

    async def fake_run_main_agent(
        agent,
        message,
        *,
        runtime_settings,
        is_cancelled=None,
        sink=None,
        extra_middleware=None,
        **kwargs,
    ) -> RunResult:
        captured["extra_middleware"] = list(extra_middleware or [])
        return RunResult(
            termination_reason=TerminationReason.COMPLETED,
            working_memory=WorkingMemory(),
            trace_sink=sink if sink is not None else InMemoryActionTraceSink(),
            final_text="完成",
        )

    monkeypatch.setattr(worker_module, "run_main_agent", fake_run_main_agent)

    record = await queue.enqueue("质检任务")
    await worker_module._process_one(
        queue, record.run_id, Settings(), state_store=InMemoryAgentRunStateStore()
    )

    assert recorder.started_count == 1
    assert recorder.finished == [("completed", TerminationReason.COMPLETED.value, None)]
    assert recorder.finished_working_memories[0]["schema_version"] == 2
    middlewares = captured["extra_middleware"]
    assert any(isinstance(middleware, TaskProgressObserverMiddleware) for middleware in middlewares)


async def test_process_one_notifies_progress_observer_on_agent_build_failure(
    monkeypatch: pytest.MonkeyPatch, queue: RedisAgentRunQueue
) -> None:
    """Agent 构建失败的早退路径也必须给观察者补终态（否则页面节点永远停在待执行）。"""
    recorder = FakeProgressRecorder()
    monkeypatch.setattr(worker_module, "build_task_progress_recorder", lambda **kwargs: recorder)

    def _boom() -> None:
        raise RuntimeError("ALIOTH_LLM_API_KEY 未配置")

    monkeypatch.setattr(worker_module, "build_default_main_agent", _boom)

    record = await queue.enqueue("质检任务")
    await worker_module._process_one(
        queue, record.run_id, Settings(), state_store=InMemoryAgentRunStateStore()
    )

    assert recorder.started_count == 1
    assert len(recorder.finished) == 1
    assert recorder.finished[0][0] == "failed"


async def test_worker_resource_usage_is_auditable_without_claiming_task_ownership(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue("质检任务")
    record.usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "ai_calls": 2,
    }
    record.working_memory = {"schema_version": 2, "current_page": None}
    record.action_trace = [{"step": 1}]

    worker_module._record_worker_resource_usage(
        record,
        rss_before_bytes=1_000,
        rss_after_bytes=1_400,
    )
    observability = worker_module._run_observability(record)

    assert record.usage["worker_rss_delta_bytes"] == 400
    assert observability["input_tokens"] == 100
    assert observability["worker_rss_before_bytes"] == 1_000
    assert observability["worker_rss_after_bytes"] == 1_400
    assert observability["worker_rss_delta_bytes"] == 400
    assert observability["working_memory_bytes"] > 0
    assert observability["action_trace_count"] == 1
    assert observability["rss_scope"] == "worker_process"
    assert observability["inspection_work_items"] == {
        "claimed": 0,
        "completed": 0,
        "recovered": 0,
        "deferred": 0,
        "finalized_from_formal": 0,
        "failed_at_gate": 0,
        "active_session_failed_at_gate": 0,
        "attempts_abandoned_at_gate": 0,
    }


async def test_worker_observability_merges_persisted_work_item_snapshot(
    queue: RedisAgentRunQueue,
) -> None:
    record = await queue.enqueue("质检任务")

    observability = worker_module._run_observability(
        record,
        inspection_work_item_summary={
            "work_item_count": 4,
            "nonterminal_count": 0,
            "status_counts": {"succeeded": 2, "partial": 1, "failed": 1},
            "rule_status_counts": {"written": 5, "error": 1},
            "attempt_status_counts": {"written": 3, "abandoned": 1},
        },
    )

    inspection = observability["inspection_work_items"]
    assert inspection["claimed"] == 4
    assert inspection["completed"] == 3
    assert inspection["snapshot"]["work_item_count"] == 4
    assert inspection["snapshot"]["nonterminal_count"] == 0


async def test_final_report_reads_enforced_work_item_snapshot(
    queue: RedisAgentRunQueue,
) -> None:
    class FakeInspectionReader:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def inspection_work_items(self, task_id: str, *, control_mode: str):
            self.calls.append((task_id, control_mode))
            return {
                "summary": {
                    "work_item_count": 2,
                    "nonterminal_count": 0,
                    "status_counts": {"succeeded": 2},
                    "rule_status_counts": {"written": 3},
                    "attempt_status_counts": {"written": 2},
                }
            }

    record = await queue.enqueue("质检任务", task_id="task-1")
    record.status = AgentRunQueueStatus.FAILED
    reader = FakeInspectionReader()

    await worker_module._generate_final_report(
        record,
        FakeFinalReportGenerator(),
        inspection_work_items_enabled=True,
        inspection_work_item_reader=reader,
    )

    assert reader.calls == [("task-1", "enforced")]
    assert record.final_report is not None
    inspection = record.final_report["run_observability"]["inspection_work_items"]
    assert inspection["claimed"] == 2
    assert inspection["completed"] == 2
    assert inspection["snapshot"]["rule_status_counts"] == {"written": 3}


async def test_h5_v2_final_report_reads_persisted_page_exploration(
    queue: RedisAgentRunQueue,
) -> None:
    from services.api.tests.test_h5_report_coverage import coverage_payload

    class FakePageExplorationReader:
        async def fetch_h5_coverage_snapshot(self, *, task_id, agent_run_id):
            return coverage_payload(task_id=task_id, agent_run_id=agent_run_id)

        async def page_timeline(self, task_id: str):
            assert task_id == "task-1"
            return {
                "run_id": record.run_id,
                "exploration_summary": {
                    "tracking_available": True,
                    "complete": False,
                    "quality_status": "needs_review",
                    "page_total": 26,
                    "page_results": [{"page_capture_id": "capture-1"}],
                    "action_results": [{"exploration_action_id": "action-1"}],
                }
            }

    record = await queue.enqueue(
        "H5 V2 质检任务",
        task_id="task-1",
        task_config={
            "workflow_version": "v2",
            "_task_target": {"platform": "h5", "target_url": "https://example.test"},
        },
    )
    record.status = AgentRunQueueStatus.FAILED
    generator = FakeFinalReportGenerator()

    await worker_module._generate_final_report(
        record,
        generator,
        page_exploration_reader=FakePageExplorationReader(),
    )

    exploration = generator.page_explorations[0]
    assert exploration["complete"] is False
    assert exploration["quality_status"] == "needs_review"
    assert exploration["page_total"] == 26
    assert exploration["page_results"] == [{"page_capture_id": "capture-1"}]
    assert exploration["action_results"] == [{"exploration_action_id": "action-1"}]
    assert exploration["h5_coverage"]["coverage_snapshot_id"] == "coverage-1"
    assert exploration["h5_coverage"]["agent_run_id"] == record.run_id
    assert exploration["h5_coverage"]["payload_hash"] == coverage_payload()["payload_hash"]


async def test_app_v2_final_report_reads_persisted_target_and_visual_quality(
    queue: RedisAgentRunQueue,
) -> None:
    class FakePageExplorationReader:
        async def app_stage(self, task_id: str):
            assert task_id == "task-app"
            return {
                "app_exploration": {
                    "outcome": "evidence_insufficient",
                    "coverage_summary": {
                        "coverage_complete": True,
                        "visual_quality_satisfied": False,
                        "app_target_snapshot": {"target_hash": "sha256:target"},
                        "degraded_provider_facts": [
                            {
                                "status": "degraded",
                                "provider_id": "llm-app-vision",
                            }
                        ],
                    },
                }
            }

    record = await queue.enqueue(
        "App V2 质检任务",
        task_id="task-app",
        task_config={
            "workflow_version": "v2",
            "_task_target": {"platform": "app"},
        },
    )
    record.status = AgentRunQueueStatus.FAILED
    generator = FakeFinalReportGenerator()

    await worker_module._generate_final_report(
        record,
        generator,
        page_exploration_reader=FakePageExplorationReader(),
    )

    summary = generator.page_explorations[0]
    assert summary["tracking_available"] is True
    assert summary["complete"] is False
    assert summary["quality_status"] == "needs_review"
    assert summary["requires_review"] is True
    assert summary["app_coverage"]["app_target_snapshot"]["target_hash"] == ("sha256:target")


async def test_persisted_work_item_gate_turns_missing_results_into_review(
    monkeypatch: pytest.MonkeyPatch,
    queue: RedisAgentRunQueue,
) -> None:
    async def forbidden_second_wait(*args, **kwargs):
        del args, kwargs
        raise AssertionError("规则补记已完成等待后，持久化最终门禁不得再次等待")

    monkeypatch.setattr(
        worker_module,
        "_wait_for_inflight_inspection_work",
        forbidden_second_wait,
    )

    class FakeInspectionService:
        async def finalize_task_work(self, **kwargs):
            assert kwargs["task_id"] == "task-1"
            return SimpleNamespace(
                recovered_work_item_count=2,
                failed_work_item_count=1,
                active_session_failed_count=1,
                abandoned_attempt_count=1,
            )

        async def has_nonterminal_work(self, *, task_id):
            assert task_id == "task-1"
            return False

        async def count_inflight_leased_work(self, *, task_id):
            assert task_id == "task-1"
            return 0

        async def find_active_consumer_run(self, *, task_id):
            assert task_id == "task-1"
            return None

        async def count_claimable_pending_work(self, *, task_id):
            assert task_id == "task-1"
            return 0

    class FakeAggregateInspectionService:
        async def finalize_task_work(self, **kwargs):
            assert kwargs["task_id"] == "task-1"
            return SimpleNamespace(
                recovered_work_item_count=1,
                failed_work_item_count=2,
                abandoned_attempt_count=1,
            )

        async def has_nonterminal_work(self, *, task_id):
            assert task_id == "task-1"
            return False

    monkeypatch.setattr(
        worker_module,
        "PostgresInspectionWorkItemRepository",
        lambda: object(),
    )
    monkeypatch.setattr(
        worker_module,
        "InspectionWorkItemService",
        lambda repository: FakeInspectionService(),
    )
    monkeypatch.setattr(
        worker_module,
        "PostgresAggregateInspectionWorkItemRepository",
        lambda: object(),
    )
    monkeypatch.setattr(
        worker_module,
        "AggregateInspectionWorkItemService",
        lambda repository: FakeAggregateInspectionService(),
    )
    record = await queue.enqueue("质检任务", task_id="task-1")
    record.status = AgentRunQueueStatus.COMPLETED
    record.termination_reason = TerminationReason.COMPLETED.value
    record.execution_outcome = ExecutionOutcome.SUCCESS.value

    await _REAL_FINALIZE_PERSISTED_INSPECTION_WORK(
        record,
        settings=Settings(),
    )

    assert record.status == AgentRunQueueStatus.FAILED
    assert record.termination_reason == TerminationReason.INCOMPLETE.value
    assert record.execution_outcome == ExecutionOutcome.NEEDS_REVIEW.value
    assert record.usage["inspection_work_items_failed_at_gate"] == 1
    assert record.usage["inspection_active_session_failed_at_gate"] == 1
    assert record.usage["aggregate_inspection_work_items_recovered_at_gate"] == 1
    assert record.usage["aggregate_inspection_work_items_failed_at_gate"] == 2
    assert record.usage["aggregate_inspection_attempts_abandoned_at_gate"] == 1
    assert "3 个持久化质检工作项" in record.final_text
    assert "任务已转人工复核" in record.final_text


async def test_h5_persisted_gate_settles_late_snapshot_work_before_finalization(
    monkeypatch: pytest.MonkeyPatch,
    queue: RedisAgentRunQueue,
) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeInspectionService:
        async def settle_stalled_work_for_closure(self, **kwargs):
            calls.append(("settle", kwargs))
            return SimpleNamespace(
                settled_work_item_count=2,
                settled_rule_count=5,
            )

        async def finalize_task_work(self, **kwargs):
            calls.append(("finalize", kwargs))
            return SimpleNamespace(
                recovered_work_item_count=2,
                failed_work_item_count=0,
                active_session_failed_count=0,
                abandoned_attempt_count=0,
                deferred_retry_count=0,
            )

        async def has_nonterminal_work(self, *, task_id):
            assert task_id == "task-h5"
            return False

    class FakeAggregateInspectionService:
        async def finalize_task_work(self, **kwargs):
            return SimpleNamespace(
                recovered_work_item_count=0,
                failed_work_item_count=0,
                abandoned_attempt_count=0,
            )

        async def has_nonterminal_work(self, *, task_id):
            assert task_id == "task-h5"
            return False

    monkeypatch.setattr(
        worker_module,
        "PostgresInspectionWorkItemRepository",
        lambda: object(),
    )
    monkeypatch.setattr(
        worker_module,
        "InspectionWorkItemService",
        lambda repository: FakeInspectionService(),
    )
    monkeypatch.setattr(
        worker_module,
        "PostgresAggregateInspectionWorkItemRepository",
        lambda: object(),
    )
    monkeypatch.setattr(
        worker_module,
        "AggregateInspectionWorkItemService",
        lambda repository: FakeAggregateInspectionService(),
    )
    record = await queue.enqueue(
        "H5 质检任务",
        task_id="task-h5",
        task_config={
            "workflow_version": "v2",
            "_task_target": {"platform": "h5"},
        },
    )
    record.status = AgentRunQueueStatus.COMPLETED

    await _REAL_FINALIZE_PERSISTED_INSPECTION_WORK(record, settings=Settings())

    assert calls == [
        (
            "settle",
            {
                "task_id": "task-h5",
                "snapshot_generation_closed": True,
            },
        ),
        (
            "finalize",
            {
                "task_id": "task-h5",
                "error_code": "INSPECTION_WORK_ITEM_FINALIZATION_INCOMPLETE",
                "error_message": "Worker 收口时工作项仍缺少正式规则执行结果",
            },
        ),
    ]
    assert record.usage["h5_late_snapshot_settled_work_count"] == 2
    assert record.usage["h5_late_snapshot_settled_rule_count"] == 5
    assert record.status is AgentRunQueueStatus.COMPLETED


@pytest.mark.parametrize("deadline_expired", [False, True])
async def test_h5_late_publication_deferral_respects_original_deadline(
    monkeypatch, deadline_expired,
):
    from unittest.mock import AsyncMock

    from services.api.app.application.inspection_work_items import (
        InspectionWorkItemClosureSettlement,
        InspectionWorkItemFinalization,
    )

    results = [
        InspectionWorkItemFinalization("task-h5", 0, 0, 0, 0, deferred_retry_count=1),
        InspectionWorkItemFinalization("task-h5", 1, 0, 0, 0),
    ]
    page = SimpleNamespace(
        settle_stalled_work_for_closure=AsyncMock(
            return_value=InspectionWorkItemClosureSettlement("task-h5"),
        ),
        finalize_task_work=AsyncMock(side_effect=results),
        has_nonterminal_work=AsyncMock(return_value=False),
    )
    aggregate = SimpleNamespace(
        finalize_task_work=AsyncMock(return_value=SimpleNamespace(
            recovered_work_item_count=0, failed_work_item_count=0, abandoned_attempt_count=0,
        )),
        has_nonterminal_work=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(worker_module, "InspectionWorkItemService", lambda repository: page)
    monkeypatch.setattr(
        worker_module, "AggregateInspectionWorkItemService", lambda repository: aggregate,
    )
    monkeypatch.setattr(worker_module, "_is_locked_native_h5_v2_run_record", lambda record: True)
    deadline = datetime.now(UTC) + timedelta(seconds=-1 if deadline_expired else 30)
    monkeypatch.setattr(worker_module, "_h5_inspection_deadline", lambda record: deadline)
    sleep = AsyncMock()
    monkeypatch.setattr(worker_module.asyncio, "sleep", sleep)
    record = SimpleNamespace(
        task_id="task-h5", status=AgentRunQueueStatus.COMPLETED, usage={}, final_text="",
    )
    if deadline_expired:
        with pytest.raises(RuntimeError, match="非终态工作"):
            await _REAL_FINALIZE_PERSISTED_INSPECTION_WORK(record, settings=Settings())
        sleep.assert_not_called()
        assert page.finalize_task_work.await_count == 1
    else:
        await _REAL_FINALIZE_PERSISTED_INSPECTION_WORK(record, settings=Settings())
        assert page.finalize_task_work.await_count == 2
        assert sleep.await_count == 1
        assert 0 < sleep.call_args.args[0] <= 30
    assert record.status is AgentRunQueueStatus.COMPLETED


async def test_persisted_work_item_gate_rejects_nonterminal_aggregate_work(
    monkeypatch: pytest.MonkeyPatch,
    queue: RedisAgentRunQueue,
) -> None:
    class FakePageInspectionService:
        async def finalize_task_work(self, **kwargs):
            return SimpleNamespace(
                recovered_work_item_count=0,
                failed_work_item_count=0,
                active_session_failed_count=0,
                abandoned_attempt_count=0,
            )

        async def has_nonterminal_work(self, *, task_id):
            return False

        async def count_inflight_leased_work(self, *, task_id):
            return 0

        async def find_active_consumer_run(self, *, task_id):
            return None

        async def count_claimable_pending_work(self, *, task_id):
            return 0

    class FakeAggregateInspectionService:
        async def finalize_task_work(self, **kwargs):
            return SimpleNamespace(
                recovered_work_item_count=0,
                failed_work_item_count=0,
                abandoned_attempt_count=0,
            )

        async def has_nonterminal_work(self, *, task_id):
            return True

    monkeypatch.setattr(
        worker_module,
        "PostgresInspectionWorkItemRepository",
        lambda: object(),
    )
    monkeypatch.setattr(
        worker_module,
        "InspectionWorkItemService",
        lambda repository: FakePageInspectionService(),
    )
    monkeypatch.setattr(
        worker_module,
        "PostgresAggregateInspectionWorkItemRepository",
        lambda: object(),
    )
    monkeypatch.setattr(
        worker_module,
        "AggregateInspectionWorkItemService",
        lambda repository: FakeAggregateInspectionService(),
    )
    record = await queue.enqueue("质检任务", task_id="task-1")
    record.status = AgentRunQueueStatus.COMPLETED

    with pytest.raises(RuntimeError, match="未能关闭全部非终态工作"):
        await _REAL_FINALIZE_PERSISTED_INSPECTION_WORK(
            record,
            settings=Settings(),
        )


@pytest.mark.asyncio
async def test_worker_main_injects_one_rule_engine_guard_only_into_worker_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate({"worker": {"roles": ["explorer"]}})
    repository = object()
    guard = object()
    guard_repositories: list[object] = []
    injected_guards: list[object] = []
    injected_app_handlers: list[object | None] = []
    heartbeat_calls: list[tuple[str, tuple[str, ...]]] = []

    class FakeRedisClient:
        async def aclose(self) -> None:
            return None

    class FakeQueue:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def record_worker_heartbeat(self, instance_id: str, *, roles=()) -> None:
            heartbeat_calls.append((instance_id, tuple(roles)))

    class FakeDeliveryRuntime:
        async def close(self) -> None:
            return None

    class FakeEvidenceExitSweeper:
        def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    def _build_guard(repository_arg):
        guard_repositories.append(repository_arg)
        return guard

    async def _reconcile_orphaned_runs(*_args, **_kwargs) -> None:
        return None

    async def _reconciliation_loop(
        _queue,
        _state_store,
        _stop_event,
        *,
        task_finalization_service=None,
        app_run_recovery=None,
        web_formal_session_closer=None,
        web_inspection_reconciliation=None,
        web_final_report_generator=None,
    ) -> None:
        del (
            task_finalization_service,
            app_run_recovery,
            web_formal_session_closer,
            web_inspection_reconciliation,
            web_final_report_generator,
        )

    async def _background_loop(*_args, **_kwargs) -> None:
        return None

    def _capture_worker_loops(
        *,
        stop_event,
        rule_engine_configuration_guard,
        app_workflow_handler,
        **_kwargs,
    ):
        injected_guards.append(rule_engine_configuration_guard)
        injected_app_handlers.append(app_workflow_handler)
        stop_event.set()
        return []

    def _unexpected_app_workflow_handler(*_args, **_kwargs):
        raise AssertionError("未启用 App 质检时不得构建 App Workflow handler")

    redis_client = FakeRedisClient()
    delivery_runtime = FakeDeliveryRuntime()
    evidence_exit_sweeper = FakeEvidenceExitSweeper()
    monkeypatch.setattr(worker_module, "configure_logging", lambda: None)
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "PostgresTaskRepository", lambda: repository)
    monkeypatch.setattr(worker_module, "RuleEngineConfigurationGuard", _build_guard)
    monkeypatch.setattr(worker_module, "build_redis_client", lambda _settings: redis_client)
    monkeypatch.setattr(worker_module, "RedisAgentRunQueue", FakeQueue)
    monkeypatch.setattr(worker_module, "build_agent_run_state_store", object)
    monkeypatch.setattr(
        worker_module,
        "validate_completion_gate_configuration",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        worker_module,
        "validate_task_submission_configuration",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(worker_module, "_new_worker_instance_id", lambda: "worker-1")
    monkeypatch.setattr(
        worker_module,
        "build_app_workflow_handler",
        _unexpected_app_workflow_handler,
    )
    monkeypatch.setattr(
        worker_module,
        "build_default_app_sealed_inspection_publisher",
        object,
    )
    monkeypatch.setattr(
        worker_module,
        "build_default_reliable_delivery_runtime",
        lambda **_kwargs: delivery_runtime,
    )
    monkeypatch.setattr(worker_module, "_reconcile_orphaned_runs", _reconcile_orphaned_runs)
    monkeypatch.setattr(worker_module, "_worker_heartbeat_loop", _background_loop)
    monkeypatch.setattr(worker_module, "_orphan_reconciliation_loop", _reconciliation_loop)
    monkeypatch.setattr(worker_module, "_v2_reliable_delivery_loop", _background_loop)
    monkeypatch.setattr(worker_module, "build_web_evidence_exit_gate", object)
    monkeypatch.setattr(
        worker_module,
        "EvidenceExitReservationSweeper",
        lambda **_kwargs: evidence_exit_sweeper,
    )
    monkeypatch.setattr(worker_module, "_start_worker_loops", _capture_worker_loops)

    await worker_module.main()

    assert guard_repositories == [repository]
    assert injected_guards == [guard]
    assert injected_app_handlers == [None]
    # 首次心跳必须携带角色：系统状态接口在心跳循环首跳前读取时，
    # 不能把刚启动的 Worker 误判成无角色实例。
    assert heartbeat_calls
    assert set(heartbeat_calls[0][1]) == {"explorer"}


@pytest.mark.asyncio
async def test_worker_main_rejects_enabled_but_unready_supplemental_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.model_validate({"supplemental_evidence": {"enabled": True}})
    downstream_called = False

    def _unexpected_rule_configuration_guard(*_args, **_kwargs):
        nonlocal downstream_called
        downstream_called = True
        raise AssertionError("补充证据自检失败后不得继续装配规则门禁")

    monkeypatch.setattr(worker_module, "configure_logging", lambda: None)
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        worker_module,
        "RuleEngineConfigurationGuard",
        _unexpected_rule_configuration_guard,
    )

    with pytest.raises(RuntimeError, match="运行时闭环尚未就绪"):
        await worker_module.main()

    assert downstream_called is False


async def test_cancel_reconciler_recovers_durable_record_after_redis_expiry(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = AgentRunRecord(
        run_id="22222222-2222-2222-2222-222222222222",
        task_id="11111111-1111-1111-1111-111111111111",
        message="H5 durable cancel recovery",
        task_config={
            "workflow_version": "v2",
            "_task_target": {"platform": "h5", "target_url": "https://m.example.test"},
        },
        submitted_at=datetime.now(UTC),
        status=AgentRunQueueStatus.CANCELED,
        termination_reason=TerminationReason.CANCELED.value,
        cancellation_cleanup_completed=False,
        cancellation_finalized=False,
    )

    class DurableRecoveryStore(InMemoryAgentRunStateStore):
        async def list_cancellation_recovery_records(self, *, limit: int = 100):
            assert limit == 100
            return [durable.model_copy(deep=True)]

    cleanup_calls: list[str] = []

    async def cancel_h5_work(*, task_id: str) -> int:
        cleanup_calls.append(task_id)
        return 2

    monkeypatch.setattr(worker_module, "_cancel_h5_nonterminal_work", cancel_h5_work)
    await worker_module._reconcile_orphaned_runs(queue, DurableRecoveryStore())

    recovered = await queue.get(durable.run_id)
    assert recovered is not None
    assert recovered.status is AgentRunQueueStatus.CANCELED
    assert recovered.cancellation_cleanup_completed is True
    assert recovered.cancellation_finalized is True
    assert cleanup_calls == [durable.task_id]


async def test_process_one_cancel_read_outage_stays_recoverable_past_max_attempts(
    queue: RedisAgentRunQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OutageStore(InMemoryAgentRunStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_read_available = False

        async def record_started(self, record) -> None:
            raise ConnectionError("postgres start unavailable")

        async def is_cancel_requested(self, run_id: str) -> bool:
            if not self.cancel_read_available:
                raise ConnectionError("postgres cancel read unavailable")
            return True

    handler_called = False

    async def fail_if_executed(*args, **kwargs):
        nonlocal handler_called
        handler_called = True
        raise AssertionError("取消事实未知时不得执行 Agent")

    monkeypatch.setattr(worker_module, "run_main_agent", fail_if_executed)
    record = await queue.enqueue("启动取消事实未知任务")
    record.dispatch_attempt_count = 3
    await queue.save(record)
    assert await queue.dequeue(timeout=0.1) == record.run_id
    store = OutageStore()

    await worker_module._process_one(queue, record.run_id, Settings(), state_store=store)

    waiting = await queue.get(record.run_id)
    assert waiting is not None
    assert waiting.status is AgentRunQueueStatus.QUEUED
    assert waiting.worker_instance_id is None
    assert handler_called is False
    assert await queue.dequeue(timeout=0.1) == record.run_id

    store.cancel_read_available = True
    await worker_module._process_one(queue, record.run_id, Settings(), state_store=store)

    canceled = await queue.get(record.run_id)
    assert canceled is not None
    assert canceled.status is AgentRunQueueStatus.CANCELED
    assert handler_called is False


async def test_finalize_rule_executions_waits_for_inflight_before_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收尾补记与确定性补判都以已落库判定去重，必须先等在途判定排空再补记。"""
    order: list[str] = []
    inflight_wait_kwargs: dict[str, Any] = {}

    async def fake_wait(service: Any, *, task_id: str, **kwargs: Any) -> float:
        del service, task_id
        inflight_wait_kwargs.update(kwargs)
        order.append("wait_inflight")
        return 3.0

    async def fake_aggregate_wait(service: Any, *, task_id: str, **kwargs: Any) -> float:
        del service, task_id, kwargs
        order.append("wait_aggregate")
        return 2.0

    async def fake_posthoc(record: Any) -> None:
        del record
        order.append("posthoc")

    async def fake_reconcile(record: Any) -> None:
        del record
        order.append("reconcile")

    monkeypatch.setattr(worker_module, "_wait_for_inflight_inspection_work", fake_wait)
    monkeypatch.setattr(worker_module, "_wait_for_aggregate_inspection_work", fake_aggregate_wait)
    monkeypatch.setattr(worker_module, "_run_h5_posthoc_engine_judgement", fake_posthoc)
    monkeypatch.setattr(worker_module, "_reconcile_run_llm_usage_from_events", fake_reconcile)

    class OrderedRuleExecutionWriter(FakeRuleExecutionWriter):
        async def finalize_missing_rule_executions(self, **kwargs: Any):
            order.append("backfill")
            return await super().finalize_missing_rule_executions(**kwargs)

    record = AgentRunRecord(
        run_id="run-finalize-order",
        task_id="task-finalize-order",
        message="收口顺序守卫",
        status=AgentRunQueueStatus.COMPLETED,
        submitted_at=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
        started_at=datetime(2026, 8, 24, 10, 1, tzinfo=UTC),
        finished_at=datetime(2026, 8, 24, 10, 2, tzinfo=UTC),
        task_config=_h5_v2_task_config(),
    )

    await worker_module._finalize_rule_executions(record, OrderedRuleExecutionWriter())

    assert order == [
        "wait_inflight",
        "wait_aggregate",
        "posthoc",
        "backfill",
        "reconcile",
    ]
    assert record.usage["inspection_finalize_grace_wait_seconds"] == 5.0
    assert inflight_wait_kwargs["max_wait_seconds"] == (
        worker_module.H5_INSPECTION_FINALIZE_MAX_WAIT_SECONDS
    )


@pytest.mark.parametrize("h5,inflight,pending", [
    (True, 1, False), (True, 0, True), (True, 0, False), (False, 1, True),
])
async def test_h5_rule_backfill_settles_late_snapshot_and_rejects_undrained_resources(
    monkeypatch, h5, inflight, pending,
):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(worker_module, "_guard_h5_rule_backfill", _REAL_GUARD_H5_RULE_BACKFILL)
    order = []

    async def settle(**kwargs):
        assert kwargs == {"task_id": "task", "snapshot_generation_closed": True}
        order.append("settle")

    async def backfill(**kwargs):
        order.append("backfill")
        return SimpleNamespace(unresolved_candidate_count=0)

    async def posthoc(record):
        order.append("posthoc")

    service = SimpleNamespace(
        settle_stalled_work_for_closure=AsyncMock(side_effect=settle),
        count_inflight_leased_work=AsyncMock(return_value=inflight),
        has_nonterminal_work=AsyncMock(return_value=pending),
    )
    monkeypatch.setattr(worker_module, "InspectionWorkItemService", lambda repository: service)
    monkeypatch.setattr(worker_module, "_is_locked_native_h5_v2_run_record", lambda record: h5)
    monkeypatch.setattr(worker_module, "_h5_inspection_deadline", lambda record: None)
    monkeypatch.setattr(
        worker_module, "_run_h5_posthoc_engine_judgement", AsyncMock(side_effect=posthoc),
    )
    monkeypatch.setattr(worker_module, "_reconcile_run_llm_usage_from_events", AsyncMock())
    writer = SimpleNamespace(finalize_missing_rule_executions=AsyncMock(side_effect=backfill))
    record = SimpleNamespace(
        task_id="task", run_id="run", status=AgentRunQueueStatus.COMPLETED,
        finished_at=datetime.now(UTC), action_trace=[], working_memory={}, usage={},
    )
    if h5 and (inflight or pending):
        with pytest.raises(RuntimeError, match="H5.*补记"):
            await worker_module._finalize_rule_executions(record, writer)
        writer.finalize_missing_rule_executions.assert_not_awaited()
        assert order == ["posthoc", "settle"]
    else:
        await worker_module._finalize_rule_executions(record, writer)
        assert order == (["posthoc", "settle", "backfill"] if h5 else ["posthoc", "backfill"])


@pytest.mark.asyncio
async def test_finalization_heartbeat_failure_cancels_long_side_effect_immediately() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_side_effect() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def lost_heartbeat() -> None:
        await started.wait()
        raise RuntimeError("lease reclaimed")

    heartbeat = asyncio.create_task(lost_heartbeat())

    with pytest.raises(
        worker_module.FinalizationRecoveryFenceError,
        match="heartbeat",
    ):
        await worker_module._await_with_recovery_heartbeat(
            long_side_effect(),
            heartbeat,
        )

    assert cancelled.is_set()


def test_finalization_fence_failure_is_not_eligible_for_projection_writeback() -> None:
    error = worker_module.TaskFinalizationPersistenceError(
        "收口恢复租约已过期或 fencing 身份已变化"
    )

    assert worker_module._is_recovery_fence_failure(error, None) is True
