"""页面类型适用性评估的运行时接入测试。"""

import json
from types import SimpleNamespace

from services.api.app.agents.instructions import MAIN_AGENT_INSTRUCTIONS
from services.api.app.agents.runtime.business_middlewares.page_inspection_memory_middleware import (
    PageInspectionMemoryMiddleware,
)
from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.agents.tools import exploration_tools  # noqa: F401
from services.api.app.agents.tools.registry import default_registry
from services.api.app.application.task_insight import _is_inactive_capture_node
from services.api.app.domain.page.catalog import load_default_page_type_catalog

_BUSINESS_PAGE_TYPES = tuple(
    page_type.code
    for page_type in load_default_page_type_catalog().page_types
    if page_type.coverage_required
)


async def test_exploration_closure_accepts_assessment_only_request() -> None:
    metadata = default_registry.get("exploration_closure_tool")
    assert metadata is not None

    result = await metadata.tool.invoke(
        arguments={
            "reason": "page_type_scope_assessed",
            "page_type_assessments": _assessment_payloads(),
            "assessment_scope_page_types": list(_BUSINESS_PAGE_TYPES),
            "assessment_scope_complete": True,
        }
    )

    payload = json.loads(result[0].text)
    assert payload["execution_status"] == "accepted"
    assert payload["assessment_scope_complete"] is True
    assert len(payload["page_type_assessments"]) == len(_BUSINESS_PAGE_TYPES)


async def test_exploration_closure_rejects_incomplete_assessment_scope() -> None:
    metadata = default_registry.get("exploration_closure_tool")
    assert metadata is not None

    result = await metadata.tool.invoke(
        arguments={
            "reason": "page_type_scope_assessed",
            "page_type_assessments": _assessment_payloads()[:-1],
            "assessment_scope_page_types": list(_BUSINESS_PAGE_TYPES),
            "assessment_scope_complete": True,
        }
    )

    payload = json.loads(result[0].text)
    assert payload["execution_status"] == "blocked"
    assert payload["error_code"] == "PAGE_TYPE_ASSESSMENT_SCOPE_INCOMPLETE"
    assert payload["missing_page_types"] == [_BUSINESS_PAGE_TYPES[-1]]


async def test_closure_middleware_persists_assessments_in_audit_snapshot() -> None:
    memory = WorkingMemory()
    middleware = PageInspectionMemoryMiddleware(memory)
    assessments = _assessment_payloads()
    context = SimpleNamespace(
        function=SimpleNamespace(name="exploration_closure_tool"),
        arguments={
            "reason": "page_type_scope_assessed",
            "page_type_assessments": assessments,
            "assessment_scope_page_types": list(_BUSINESS_PAGE_TYPES),
            "assessment_scope_complete": True,
        },
        result=None,
        metadata={},
    )

    async def _accepted_tool() -> None:
        context.result = {
            "execution_status": "accepted",
            "page_type_assessments": assessments,
            "assessment_scope_page_types": list(_BUSINESS_PAGE_TYPES),
            "assessment_scope_complete": True,
        }

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await middleware.process(context, _accepted_tool)

    snapshot = memory.audit_snapshot()
    assert snapshot["page_type_assessment_scope_complete"] is True
    assert snapshot["page_type_assessment_scope_page_types"] == list(_BUSINESS_PAGE_TYPES)
    stored = snapshot["page_type_assessments"]
    assert len(stored) == len(assessments)
    assert stored[0]["page_type"] == assessments[0]["page_type"]
    assert stored[0]["status"] == assessments[0]["status"]
    assert stored[0]["assessment_id"].startswith("page-type-assessment:")
    assert stored[0]["protocol_version"] == "page-type-assessment.v1"
    assert stored[0]["catalog_sha256"].startswith("sha256:")


def test_evidence_only_capture_is_inactive_for_rule_scope() -> None:
    assert _is_inactive_capture_node({"inspection_lifecycle_status": "evidence_only"}) is True


def test_main_agent_requires_complete_business_type_assessment_before_report() -> None:
    assert "page_type_assessments" in MAIN_AGENT_INSTRUCTIONS
    assert "assessment_scope_page_types" in MAIN_AGENT_INSTRUCTIONS
    assert "assessment_scope_complete=true" in MAIN_AGENT_INSTRUCTIONS
    assert "Catalog 全量页型" in MAIN_AGENT_INSTRUCTIONS


def _assessment_payloads() -> list[dict[str, object]]:
    return [
        {
            "page_type": page_type,
            "status": "evidence_insufficient",
            "reason_code": "no_auditable_entry",
            "page_capture_ids": [],
            "evidence_artifact_ids": [],
            "exploration_action_ids": [],
        }
        for page_type in _BUSINESS_PAGE_TYPES
    ]
