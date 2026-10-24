"""业务中间件工厂：按固定顺序构建默认业务中间件链。"""

from agent_framework import FunctionMiddleware

from services.api.app.agents.runtime.business_middlewares import (
    duplicate_submission_dedupe_middleware as dedupe_middleware,
)
from services.api.app.agents.runtime.business_middlewares.audit_log_middleware import (
    AuditLogMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.base import (
    DefaultTaskRuntimeStateProvider,
    TaskRuntimeStateProvider,
)
from services.api.app.agents.runtime.business_middlewares.evidence_completeness_middleware import (
    EvidenceCompletenessMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.inspection_work_item_middleware import (
    InspectionWorkItemMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.page_inspection_memory_middleware import (
    PageInspectionMemoryMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.publish_condition_middleware import (
    PublishConditionMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.rule_execution_context_middleware import (
    RuleExecutionContextMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.rule_judge_handoff_middleware import (
    RuleJudgeHandoffMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.ruleset_version_lock_middleware import (
    RulesetVersionLockMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.scoped_tool_failure_middleware import (
    ScopedToolFailureMiddleware,
)
from services.api.app.agents.runtime.business_middlewares.task_status_gate_middleware import (
    TaskStatusGateMiddleware,
)
from services.api.app.agents.runtime.rule_execution_context import RuleExecutionContext
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.agents.tools.registry import ToolRegistry, default_registry


def create_default_business_middlewares(
    *,
    working_memory: WorkingMemory,
    task_state_provider: TaskRuntimeStateProvider | None = None,
    registry: ToolRegistry | None = None,
) -> list[FunctionMiddleware]:
    """按固定顺序构建默认业务中间件链。"""
    provider = (
        task_state_provider
        if task_state_provider is not None
        else DefaultTaskRuntimeStateProvider()
    )
    target_registry = registry if registry is not None else default_registry
    return [
        AuditLogMiddleware(),
        RulesetVersionLockMiddleware(provider),
        TaskStatusGateMiddleware(provider, registry=target_registry),
        RuleExecutionContextMiddleware(RuleExecutionContext()),
        RuleJudgeHandoffMiddleware(),
        EvidenceCompletenessMiddleware(working_memory=working_memory),
        dedupe_middleware.DuplicateSubmissionDedupeMiddleware(registry=target_registry),
        PageInspectionMemoryMiddleware(working_memory),
        InspectionWorkItemMiddleware(provider),
        ScopedToolFailureMiddleware(),
        PublishConditionMiddleware(working_memory),
    ]
