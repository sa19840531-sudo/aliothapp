"""质检任务查询用例。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from services.api.app.application.rule_auth_gate import AUTH_GATE_AUTO
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.page.catalog import PageTypeCatalog
from services.api.app.domain.page.registry import get_default_page_type_registry


@dataclass(frozen=True)
class TaskDetail:
    """质检任务详情。"""

    task_id: str
    target_url: str | None
    ruleset_id: str
    ruleset_code: str
    ruleset_version: int
    channel: str
    status: str
    priority: int
    run_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None
    ruleset_snapshot_hash: str | None = None
    target_platform: str = "h5"
    evidence_index_id: str | None = None
    exploration_status: str | None = None
    evidence_gap: tuple[str, ...] = ()
    page_timeline: tuple[str, ...] = ()
    remark: str | None = None
    inspection_scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskListItem:
    """质检任务列表项。"""

    task_id: str
    target_url: str | None
    ruleset_code: str
    ruleset_version: int
    channel: str
    status: str
    priority: int
    run_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    target_platform: str = "h5"


@dataclass(frozen=True)
class RuleExecutionSummary:
    """规则执行结果摘要。"""

    execution_id: str
    rule_id: str
    rule_code: str
    rule_name: str
    page_capture_id: str | None
    execution_status: str
    judge_strategy: str
    confidence: float | None
    severity: str | None
    result_summary: str | None
    problem_desc: str | None
    review_status: str | None
    started_at: datetime | None
    finished_at: datetime | None
    source: str = "agent"
    """判定来源：agent=Agent 真实写回；bootstrap=任务收尾兜底补记（非判定结论）。"""
    source_idempotency_key: str | None = None
    """无 Capture 正式结果的不可变来源身份。"""
    judge_input: dict[str, Any] = field(default_factory=dict)
    """用于验证特殊结果来源的结构化判定输入。"""
    diagnostic_codes: tuple[str, ...] = ()
    """规则执行的结构化诊断码；只保存安全码，不包含供应商响应正文。"""
    missing_evidence: tuple[str, ...] = ()
    """Judge 明确声明的缺失证据；空值表示证据缺口尚未结构化。"""
    evidence_input_set_id: str | None = None
    """任务级聚合规则使用的冻结证据输入集。"""
    observation_packet_id: str | None = None
    """任务级聚合规则使用的观察包。"""
    evidence_artifact_ids: tuple[str, ...] = ()
    """支撑该规则结论的证据附件 ID。"""


@dataclass(frozen=True)
class UnresolvedCandidateRule:
    """当前 Run 中尚未写回的页面级候选规则。"""

    page_capture_id: str
    rule_code: str


@dataclass(frozen=True)
class RuleExecutionCompleteness:
    """当前 Agent Run 的页面级候选规则执行完整性。"""

    candidate_query_count: int
    candidate_rule_count: int
    completed_candidate_count: int
    unresolved_rule_codes: tuple[str, ...]
    unresolved_candidate_items: tuple[UnresolvedCandidateRule, ...] = ()

    @property
    def unresolved_candidate_count(self) -> int:
        """返回页面级缺口数量，兼容旧调用只提供规则编码的场景。"""
        if self.unresolved_candidate_items:
            return len(self.unresolved_candidate_items)
        return len(self.unresolved_rule_codes)

    @property
    def is_complete(self) -> bool:
        return self.candidate_query_count > 0 and self.unresolved_candidate_count == 0


@dataclass(frozen=True)
class TaskTargetPageCoverage:
    """任务配置目标页面在当前 Run 中的只读覆盖事实。"""

    page_type: str
    capture_count: int
    explored_action_count: int
    failed_action_count: int
    blocked_action_count: int
    skipped_action_count: int
    pending_action_count: int
    failure_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceArtifactSummary:
    """任务证据附件摘要，不包含附件正文。"""

    artifact_id: str
    page_capture_id: str | None
    rule_execution_id: str | None
    artifact_type: str
    source: str
    storage_path: str
    content_type: str | None
    checksum: str | None
    size_bytes: int | None
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class CandidateRule:
    """任务当前候选规则。"""

    rule_id: str
    rule_code: str
    name: str
    category: str
    severity: str
    executor_type: str
    page_type: str | None
    applicable_page_types: list[str]
    check_point: str
    judge_config: dict[str, Any]
    sort_order: int
    match_reason: str
    description: str | None = None
    scope: str | None = None
    url_pattern: str | None = None
    steps: list[Any] = field(default_factory=list)
    input_contract: dict[str, Any] = field(default_factory=dict)
    manual_review_policy: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRunStep:
    """Agent 运行单步轨迹。"""

    step_seq: int
    phase: str | None
    thought_summary: str | None
    selected_capability: str | None
    capability_kind: str | None
    status: str
    latency_ms: int | None
    error_message: str | None
    capability_input: dict[str, Any]
    capability_output: dict[str, Any]
    error_code: str | None = None


@dataclass(frozen=True)
class AgentRunDetail:
    """任务最近一次 Agent 运行概要与轨迹。"""

    run_id: str
    status: str
    stop_reason: str | None
    error_message: str | None
    total_steps: int
    total_ai_calls: int
    total_tokens: int | None
    final_text: str | None
    started_at: datetime | None
    finished_at: datetime | None
    steps: list[AgentRunStep]
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit_tokens: int | None = None
    input_cost: float | None = None
    output_cost: float | None = None
    cache_hit_cost: float | None = None
    total_cost: float | None = None
    cost_currency: str | None = None
    pricing_source: str | None = None
    pricing_configured: bool | None = None
    applied_pricing_tiers: tuple[str, ...] = ()
    worker_rss_before_bytes: int | None = None
    worker_rss_after_bytes: int | None = None
    worker_rss_delta_bytes: int | None = None
    working_memory_bytes: int | None = None
    action_trace_count: int = 0
    evidence_index_id: str | None = None
    exploration_status: str | None = None
    evidence_gap: tuple[str, ...] = ()
    page_timeline: tuple[str, ...] = ()


class TaskQueryRepository(Protocol):
    """质检任务查询仓储契约。"""

    async def get_task_detail(
        self, task_id: str, *, access_scope: AccessScope | None = None
    ) -> TaskDetail | None:
        """按任务 ID 查询任务详情。"""

    async def list_tasks(
        self, *, status: str | None, limit: int, access_scope: AccessScope
    ) -> list[TaskListItem]:
        """查询任务列表。"""

    async def list_rule_executions(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[RuleExecutionSummary]:
        """按任务 ID 查询规则执行结果。"""

    async def list_evidence_artifacts(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[EvidenceArtifactSummary]:
        """按任务 ID 查询证据附件摘要。"""

    async def list_candidate_rules(
        self,
        *,
        task_id: str,
        page_type: str,
        page_url: str | None = None,
        access_scope: AccessScope | None = None,
        auth_gate: str | None = AUTH_GATE_AUTO,
    ) -> list[CandidateRule]:
        """查询任务当前候选规则。"""

    async def get_page_capture_url(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> str | None:
        """读取页面采集 URL。"""

    async def get_rule_execution_completeness(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        access_scope: AccessScope | None = None,
    ) -> RuleExecutionCompleteness:
        """查询当前 Run 的候选规则执行完整性。"""

    async def list_task_target_page_coverage(
        self,
        *,
        task_id: str,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[TaskTargetPageCoverage]:
        """查询任务配置目标页面的采集与动作覆盖事实。"""

    async def get_latest_agent_run(
        self, task_id: str, *, access_scope: AccessScope
    ) -> AgentRunDetail | None:
        """查询任务最近一次 Agent 运行概要与轨迹。"""


class TaskQueryService:
    """质检任务查询服务。"""

    def __init__(
        self,
        *,
        repository: TaskQueryRepository,
        page_type_catalog: PageTypeCatalog | None = None,
    ) -> None:
        self._repository = repository
        self._page_type_catalog = page_type_catalog or get_default_page_type_registry().catalog

    async def get_task_detail(
        self, task_id: str, *, access_scope: AccessScope | None = None
    ) -> TaskDetail | None:
        return await self._repository.get_task_detail(
            task_id,
            **_scope_kwargs(access_scope),
        )

    async def list_tasks(
        self, *, status: str | None, limit: int, access_scope: AccessScope
    ) -> list[TaskListItem]:
        return await self._repository.list_tasks(
            status=status,
            limit=limit,
            access_scope=access_scope,
        )

    async def list_rule_executions(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[RuleExecutionSummary]:
        return await self._repository.list_rule_executions(
            task_id,
            agent_run_id=agent_run_id,
            **_scope_kwargs(access_scope),
        )

    async def list_evidence_artifacts(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[EvidenceArtifactSummary]:
        return await self._repository.list_evidence_artifacts(
            task_id,
            agent_run_id=agent_run_id,
            **_scope_kwargs(access_scope),
        )

    async def list_candidate_rules(
        self,
        *,
        task_id: str,
        page_type: str,
        page_url: str | None = None,
        access_scope: AccessScope | None = None,
        auth_gate: str | None = AUTH_GATE_AUTO,
    ) -> list[CandidateRule]:
        page_type_code = self._page_type_catalog.require_code(page_type).code
        return await self._repository.list_candidate_rules(
            task_id=task_id,
            page_type=page_type_code,
            page_url=page_url,
            auth_gate=auth_gate,
            **_scope_kwargs(access_scope),
        )

    async def get_page_capture_url(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> str | None:
        return await self._repository.get_page_capture_url(
            task_id=task_id,
            page_capture_id=page_capture_id,
        )

    async def get_rule_execution_completeness(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        access_scope: AccessScope | None = None,
    ) -> RuleExecutionCompleteness:
        return await self._repository.get_rule_execution_completeness(
            task_id=task_id,
            agent_run_id=agent_run_id,
            **_scope_kwargs(access_scope),
        )

    async def list_task_target_page_coverage(
        self,
        *,
        task_id: str,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> list[TaskTargetPageCoverage]:
        return await self._repository.list_task_target_page_coverage(
            task_id=task_id,
            agent_run_id=agent_run_id,
            **_scope_kwargs(access_scope),
        )

    async def get_latest_agent_run(
        self, task_id: str, *, access_scope: AccessScope
    ) -> AgentRunDetail | None:
        return await self._repository.get_latest_agent_run(
            task_id,
            access_scope=access_scope,
        )


def _scope_kwargs(access_scope: AccessScope | None) -> dict[str, AccessScope]:
    """HTTP 查询传主体；Worker 内部任务链保持显式的无主体可信读路径。"""

    return {"access_scope": access_scope} if access_scope is not None else {}
