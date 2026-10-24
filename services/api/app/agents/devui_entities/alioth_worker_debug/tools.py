"""Alioth DevUI 调试工具：作为客户端调用现有 API，保持 Worker 执行链路不变。"""

import os
from dataclasses import dataclass
from typing import Any

import httpx

_DEFAULT_API_BASE_URL = "http://alioth-api:8000/api/v1"
_DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class AliothDevuiApiClient:
    """DevUI 调试客户端：只代理 Alioth API，不直接执行任务或写状态。"""

    base_url: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    transport: httpx.AsyncBaseTransport | None = None

    async def submit_url_task(
        self,
        *,
        target_url: str,
        ruleset_code: str = "alioth_first_ruleset",
        ruleset_version: int = 1,
        priority: int = 100,
    ) -> dict[str, Any]:
        """提交 URL 质检任务，由 alioth-worker 按正式链路异步执行。"""
        payload = {
            "target_url": target_url,
            "ruleset_code": ruleset_code,
            "ruleset_version": ruleset_version,
            "channel": "api",
            "priority": priority,
            "task_config": {"source": "devui"},
            "page_sampling_policy": {},
            "loop_guard": {},
        }
        result = await self._request_json("POST", "tasks", json=payload)
        return {
            **result,
            "next_steps": {
                "run_status_tool": "get_run_status",
                "task_detail_tool": "get_task_detail",
                "rule_executions_tool": "get_rule_executions",
            },
        }

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        """查询 alioth-worker 写回的 Agent Run 快照和 action_trace。"""
        return await self._request_json("GET", f"agents/runs/{run_id}")

    async def get_task_detail(self, task_id: str) -> dict[str, Any]:
        """查询业务质检任务详情。"""
        return await self._request_json("GET", f"tasks/{task_id}")

    async def get_rule_executions(self, task_id: str) -> dict[str, Any]:
        """查询业务质检任务下的规则执行结果。"""
        return await self._request_json("GET", f"tasks/{task_id}/rule-executions")

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_url = self.base_url.rstrip("/") + "/"
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            response = await client.request(method, path.lstrip("/"), json=json)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Alioth API 请求失败：{method} {base_url}{path.lstrip('/')} "
                f"status={response.status_code} body={response.text}"
            ) from exc
        payload = response.json()
        return payload if isinstance(payload, dict) else {"value": payload}


def _build_client() -> AliothDevuiApiClient:
    """按容器环境构建 DevUI API 客户端。"""
    return AliothDevuiApiClient(
        base_url=os.getenv("ALIOTH_API_BASE_URL", _DEFAULT_API_BASE_URL),
        timeout_seconds=float(os.getenv("ALIOTH_DEVUI_API_TIMEOUT_SECONDS", "30")),
    )


async def submit_url_task(
    target_url: str,
    ruleset_code: str = "alioth_first_ruleset",
    ruleset_version: int = 1,
    priority: int = 100,
) -> dict[str, Any]:
    """提交 URL 质检任务；返回 task_id/run_id 后继续用 get_run_status 查询执行步骤。"""
    return await _build_client().submit_url_task(
        target_url=target_url,
        ruleset_code=ruleset_code,
        ruleset_version=ruleset_version,
        priority=priority,
    )


async def get_run_status(run_id: str) -> dict[str, Any]:
    """查询 Worker Run 状态、终止原因、错误信息与 action_trace。"""
    return await _build_client().get_run_status(run_id)


async def get_task_detail(task_id: str) -> dict[str, Any]:
    """查询质检任务详情，包含任务状态与关联 run_id。"""
    return await _build_client().get_task_detail(task_id)


async def get_rule_executions(task_id: str) -> dict[str, Any]:
    """查询质检任务规则执行结果列表。"""
    return await _build_client().get_rule_executions(task_id)
