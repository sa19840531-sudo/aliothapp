"""DevUI 独立调试工具测试：确认只代理 Alioth 现有 API，不直接执行 Worker 逻辑。"""

import json

import httpx
import pytest

from services.api.app.agents.devui_entities.alioth_worker_debug.tools import (
    AliothDevuiApiClient,
)


@pytest.mark.asyncio
async def test_devui_submit_url_task_posts_standard_task_payload() -> None:
    captured_request: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_request["method"] = request.method
        captured_request["path"] = request.url.path
        captured_request["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            status_code=202,
            json={
                "task_id": "task-001",
                "run_id": "run-001",
                "ruleset_id": "ruleset-001",
                "status": "queued",
            },
        )

    client = AliothDevuiApiClient(
        base_url="http://alioth-api:8000/api/v1",
        transport=httpx.MockTransport(handler),
    )

    result = await client.submit_url_task(
        target_url="https://example.com",
        ruleset_code="alioth_first_ruleset",
        ruleset_version=1,
    )

    assert captured_request["method"] == "POST"
    assert captured_request["path"] == "/api/v1/tasks"
    assert captured_request["payload"] == {
        "target_url": "https://example.com",
        "ruleset_code": "alioth_first_ruleset",
        "ruleset_version": 1,
        "channel": "api",
        "priority": 100,
        "task_config": {"source": "devui"},
        "page_sampling_policy": {},
        "loop_guard": {},
    }
    assert result["task_id"] == "task-001"
    assert result["run_id"] == "run-001"
    assert result["next_steps"]["run_status_tool"] == "get_run_status"


@pytest.mark.asyncio
async def test_devui_get_run_status_reads_worker_run_snapshot() -> None:
    captured_request: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_request["method"] = request.method
        captured_request["path"] = request.url.path
        return httpx.Response(
            status_code=200,
            json={
                "run_id": "run-001",
                "status": "completed",
                "submitted_at": "2026-07-08T10:00:00Z",
                "started_at": "2026-07-08T10:00:01Z",
                "finished_at": "2026-07-08T10:00:05Z",
                "termination_reason": "completed",
                "final_text": "done",
                "step_count": 1,
                "error_message": None,
                "error_category": None,
                "usage": None,
                "action_trace": [
                    {
                        "step": 1,
                        "tool_name": "candidate_rules_tool",
                        "arguments": "{}",
                        "status": "ok",
                        "duration_seconds": 0.1,
                        "error_message": None,
                        "retry_count": 0,
                    }
                ],
            },
        )

    client = AliothDevuiApiClient(
        base_url="http://alioth-api:8000/api/v1",
        transport=httpx.MockTransport(handler),
    )

    result = await client.get_run_status("run-001")

    assert captured_request["method"] == "GET"
    assert captured_request["path"] == "/api/v1/agents/runs/run-001"
    assert result["status"] == "completed"
    assert result["step_count"] == 1
    assert result["action_trace"][0]["tool_name"] == "candidate_rules_tool"
