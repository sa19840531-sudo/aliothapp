"""H5 V1/V2 只读决策影子记录测试。"""

from datetime import UTC, datetime
from importlib import import_module

import pytest

from services.api.app.application.pipeline_shadow import PipelineShadowService


def _shadow_module():
    return import_module("services.api.app.application.h5_workflow_shadow")


@pytest.mark.asyncio
async def test_shadow_records_sanitized_decision_projection_without_actions() -> None:
    module = _shadow_module()

    class Repository:
        comparison = None

        async def save_comparison(self, *, comparison, difference_type):
            self.comparison = comparison
            assert difference_type == "conclusion_mismatch"
            return "shadow-h5-1"

    repository = Repository()
    recorder = module.H5WorkflowShadowRecorder(
        service=PipelineShadowService(repository=repository),
        now=lambda: datetime(2026, 8, 11, tzinfo=UTC),
    )
    immutable_snapshot = {
        "current_page": {
            "page_capture_id": "capture-1",
            "inspection_status": "completed",
            "dom": "<html>敏感正文</html>",
            "screenshot_base64": "secret",
        },
        "exploration_frontier": {
            "pending_actions": [
                {
                    "exploration_action_id": "action-1",
                    "status": "candidate",
                    "semantic_target": "个人中心",
                }
            ],
            "explored_actions": [],
            "inactive_actions": [],
        },
        "page_type_assessments": [
            {"page_type": "profile", "status": "covered"}
        ],
        "page_type_assessment_scope_complete": True,
    }

    shadow_id = await recorder.record(
        task_id="task-h5",
        agent_run_id="run-h5",
        working_memory=immutable_snapshot,
        legacy_conclusion="completed",
        legacy_duration_ms=1200,
        legacy_input_tokens=80,
    )

    assert shadow_id == "shadow-h5-1"
    comparison = repository.comparison
    assert comparison.comparison_mode == "h5_v1_v2_decision_projection"
    assert comparison.legacy_conclusion == "completed"
    assert comparison.candidate_conclusion == "continue"
    assert comparison.summary["pending_action_count"] == 1
    serialized = str(comparison.summary)
    assert "敏感正文" not in serialized
    assert "screenshot_base64" not in serialized


def test_shadow_projects_requires_auth_as_a_terminal_boundary() -> None:
    module = _shadow_module()
    decision = module.project_h5_v2_decision(
        {
            "page_type_assessments": [
                {"page_type": "profile", "status": "requires_auth"}
            ],
            "page_type_assessment_scope_complete": True,
        }
    )

    assert decision.conclusion == "requires_auth"
    assert decision.executes_action is False
