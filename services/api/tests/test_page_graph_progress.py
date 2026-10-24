"""树进度只使用实际证据和规则完成事实，不把文件通过或已发工作当完成。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.page_graph import PageGraphService
from services.api.app.infrastructure.db.page_graph_progress_repository import _progress_fact


@pytest.mark.parametrize(
    "updates,expected",
    [
        ({"quality_status": "pass"}, ("unknown", "unknown")),
        ({"quality_status": "pass", "inspection_status": "completed"}, ("complete", "completed")),
        (
            {"quality_status": "pass", "inspection_status": "no_rules_applicable"},
            ("complete", "completed"),
        ),
        ({"quality_status": "fail"}, ("failed", "unknown")),
        (
            {"quality_status": "pass", "inventory_status": "insufficient"},
            ("insufficient", "unknown"),
        ),
        (
            {"quality_status": "pass", "unresolved_evidence_requests": True},
            ("insufficient", "unknown"),
        ),
        (
            {"quality_status": "pass", "inspection_status": "completed", "unfinished_rules": True},
            ("complete", "evaluating"),
        ),
        (
            {
                "quality_status": "pass",
                "inspection_status": "published",
                "published_count": 2,
                "completed_rule_count": 1,
            },
            ("unknown", "published"),
        ),
        (
            {
                "quality_status": "pass",
                "inspection_status": "published",
                "published_count": 2,
                "completed_rule_count": 2,
            },
            ("complete", "completed"),
        ),
        (
            {
                "quality_status": "pass",
                "inspection_status": "published",
                "published_count": 2,
                "completed_rule_count": 2,
                "rule_evidence_gaps": True,
            },
            ("insufficient", "completed"),
        ),
    ],
)
def test_progress_separates_material_quality_rule_gaps_and_evaluation_completion(updates, expected):
    result = _progress_fact({"capture_id": "capture", **updates})
    assert (result["evidence_status"], result["inspection_status"]) == expected
    assert result["exploration_finished"] is False


async def test_service_assembles_tree_from_the_same_repository_snapshot():
    scope = {"task_id": "task", "agent_run_id": "run", "finalization_epoch": 1}
    repository = SimpleNamespace(
        get_graph=AsyncMock(
            return_value={
                **scope,
                "status": "available",
                "states": [
                    {
                        **scope,
                        "state_id": "capture",
                        "logical_page_key": "home",
                        "state_fingerprint": "stable",
                        "identity_status": "confirmed",
                        "counts_as_business_page": True,
                    }
                ],
                "transitions": [],
                "candidates": [],
                "progress_facts": [
                    {
                        "capture_id": "capture",
                        "evidence_status": "complete",
                        "inspection_status": "completed",
                        "exploration_finished": True,
                    }
                ],
            }
        )
    )
    result = await PageGraphService(repository=repository).get_graph(**scope)
    repository.get_graph.assert_awaited_once_with(**scope)
    assert result["exploration_tree"]["nodes"][0]["status"] == "completed"
    assert "progress_facts" not in result


async def test_incomplete_graph_cannot_provide_formal_recovery_routes():
    repository = SimpleNamespace(get_graph=AsyncMock(return_value={"status": "legacy_unprojected"}))
    result = await PageGraphService(repository=repository).get_recovery_route(
        task_id="task",
        agent_run_id="run",
        finalization_epoch=1,
    )
    assert result["status"] == "unavailable"
    assert result["routes"] == []
