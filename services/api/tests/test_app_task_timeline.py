"""APP 时间线沿当前页面证据读取独立 Judge Run 的真实结论。"""

import pytest

from services.api.tests.test_task_insight_service import (
    FakeInsightRepository,
    _execution,
    _node,
    _service,
)


async def test_app_timeline_counts_judge_run_results_only_on_current_capture():
    class RunScopedRepository(FakeInsightRepository):
        async def list_rule_execution_rows(self, *, task_id, agent_run_id):
            assert task_id == "task-1"
            return [
                row for row in self.execution_rows
                if agent_run_id is None or row.get("agent_run_id") == agent_run_id
            ]

    repository = RunScopedRepository()
    repository.context.update(target_platform="app", target_url=None)
    repository.nodes = [_node(
        1, source="app_runtime", page_type="post_list", status="captured",
        page_capture_id="current-capture", candidate_rule_total=None,
    )]
    repository.expected_counts = {"post_list": 3}
    repository.execution_rows = [
        _execution("current-rule", "pass", agent_run_id="judge-run",
                   page_capture_id="current-capture", capture_page_type="post_list"),
        _execution("old-rule", "fail", agent_run_id="old-judge-run",
                   page_capture_id="old-capture", capture_page_type="post_list"),
    ]

    timeline = await _service(repository).page_timeline("task-1")

    assert len(timeline["nodes"]) == 1
    assert timeline["nodes"][0]["rule_progress"] == {"executed": 1, "total": None}
    assert timeline["nodes"][0]["result_rollup"] == "passed"


@pytest.mark.parametrize("selected", [["home", "content_list"], []])
async def test_app_timeline_distinguishes_explicit_scope_from_all_pages(selected):
    repository = FakeInsightRepository()
    repository.context.update(target_platform="app", target_url=None)
    repository.context["task_config"] = {
        "execution_mode": "full_inspection", "inspection_scope": {"page_types": selected},
    }
    repository.nodes = [_node(
        1, source="app_runtime", page_type="post_list", status="captured",
        page_capture_id="current-capture", candidate_rule_total=None,
        page_state="stable",
    )]
    repository.expected_counts = {"post_list": 3}

    item = (await _service(repository).page_timeline("task-1"))["nodes"][0]

    assert item["rule_progress"] == {"executed": 0, "total": 0 if selected else None}
    if selected:
        assert item["inspection"]["status_label"] == "本次质检范围外"
        assert item["inspection"]["failure_code"] == "PAGE_TYPE_OUTSIDE_INSPECTION_SCOPE"
    else:
        assert item["inspection"]["failure_code"] is None


async def test_app_timeline_keeps_persisted_candidates_without_querying_current_rules():
    class HistoricalRepository(FakeInsightRepository):
        async def count_rules_by_page_type(self, *, task_id):
            raise AssertionError("APP 历史候选不能使用现行规则")

    repository = HistoricalRepository()
    repository.context.update(target_platform="app", target_url=None)
    repository.nodes = [_node(
        1, source="app_runtime", page_type="post_list", status="captured",
        page_capture_id="current-capture", candidate_rule_total=7,
    )]

    item = (await _service(repository).page_timeline("task-1"))["nodes"][0]

    assert item["rule_progress"] == {"executed": 0, "total": 7}


@pytest.mark.parametrize("mode", ["unknown", "stored_failure", "failed_status", "degraded_status"])
async def test_scope_projection_does_not_guess_unknown_pages_or_hide_real_failures(mode):
    repository = FakeInsightRepository()
    repository.context.update(target_platform="app", target_url=None)
    repository.context["task_config"] = {
        "execution_mode": "full_inspection", "inspection_scope": {"page_types": ["home"]},
    }
    repository.nodes = [_node(
        1, source="app_runtime", page_type="app" if mode == "unknown" else "post_list",
        status="captured", page_capture_id="current-capture", candidate_rule_total=None,
        inspection_failure_code="VISUAL_ERROR" if mode == "stored_failure" else None,
        inspection_failure_message="真实视觉故障" if mode == "stored_failure" else None,
        inspection_status=(
            "failed" if mode == "failed_status"
            else "degraded" if mode == "degraded_status" else None
        ),
    )]

    item = (await _service(repository).page_timeline("task-1"))["nodes"][0]

    if mode == "unknown":
        assert item["inspection"]["failure_code"] is None
        assert item["rule_progress"]["total"] is None
    elif mode == "stored_failure":
        assert item["inspection"]["failure_code"] == "VISUAL_ERROR"
        assert item["inspection"]["failure_message"] == "真实视觉故障"
    else:
        assert item["inspection"]["status"] == mode.removesuffix("_status")
        assert item["inspection"]["failure_code"] is None
