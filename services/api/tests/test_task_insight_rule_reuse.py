"""任务详情「规则结论复用」只读接口：路由透传访问范围，服务层只汇总来源与失效条件。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from services.api.app.api.v1 import evidence as evidence_module
from services.api.app.api.v1.workstation import task_insight as task_insight_module
from services.api.app.application.task_insight import (
    TaskInsightNotFoundError,
    TaskInsightService,
)
from services.api.app.main import create_app


def _reuse(**overrides: Any) -> dict[str, Any]:
    payload = {
        "protocol_version": "rule-reuse.v1",
        "scope": "per_task",
        "scope_label": "整个任务一次",
        "reuse_key": "task:rule-1",
        "source_execution_id": "exec-source",
        "source_page_capture_id": "capture-0",
        "source_execution_status": "pass",
        "source_agent_run_id": "run-1",
        "source_page_type": "home",
        "source_page_url": "https://demo.example/",
        "reason": "整个任务只判一次，沿用首个合格结论",
        "basis": {"rule_contract_hash": "contract-1", "surface_kind": "web"},
        "invalidate_on": ["account_or_login_changed", "rule_version_changed"],
    }
    payload.update(overrides)
    return payload


class FakeInsightRepository:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, Any]] = []

    async def fetch_task_context(self, task_id: str, *, access_scope=None) -> dict | None:
        self.calls.append(("fetch_task_context", (task_id, access_scope)))
        if task_id == "missing":
            return None
        return {
            "task_id": task_id,
            "status": "review",
            "run_id": "run-1",
            "target_url": "https://demo.example",
            "task_config": {},
        }

    async def list_rule_reuse_rows(self, *, task_id, agent_run_id, access_scope=None):
        self.calls.append(("list_rule_reuse_rows", (task_id, agent_run_id, access_scope)))
        return list(self.rows)


def _row(**overrides: Any) -> dict[str, Any]:
    payload = {
        "execution_id": "exec-2",
        "rule_id": "rule-1",
        "rule_code": "alioth_ops_1",
        "rule_name": "产品域名可访问检查",
        "category": "interaction",
        "rule_severity": "critical",
        "execution_status": "pass",
        "result_summary": "沿用home上的结论：域名可以正常访问",
        "page_capture_id": "capture-2",
        "page_type": "video_list",
        "page_url": "https://demo.example/list",
        "reuse": _reuse(),
        "source_execution_status": "pass",
        "source_page_type": "home",
        "source_page_url": "https://demo.example/",
        "created_at": datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
    }
    payload.update(overrides)
    return payload


async def test_service_lists_reused_conclusions_with_source_and_invalidation() -> None:
    repository = FakeInsightRepository(
        [
            _row(),
            _row(
                execution_id="exec-3",
                rule_code="alioth_41",
                rule_name="二维码与网址有效性检查",
                execution_status="fail",
                reuse=json.dumps(
                    _reuse(
                        scope="same_object",
                        scope_label="同一对象复用",
                        reuse_key="https://demo.example/profile",
                        source_execution_status="fail",
                        invalidate_on=[],
                    ),
                    ensure_ascii=False,
                ),
                source_execution_status=None,
                source_page_type="",
            ),
            # 复用块缺失或损坏的行不进入台账，也不让接口报错。
            _row(execution_id="exec-4", reuse=None),
            _row(execution_id="exec-5", reuse="{not json"),
        ]
    )
    service = TaskInsightService(repository=repository)

    result = await service.rule_reuse("task-1")

    assert result["protocol_version"] == "rule-reuse-list.v1"
    assert result["task_status"] == "review"
    assert result["run_id"] == "run-1"
    assert result["presentation"] == "reused_only"
    assert result["total"] == 2
    assert result["scope_counts"] == {"per_task": 1, "same_object": 1}
    assert result["status_counts"] == {"pass": 1, "fail": 1}
    first, second = result["items"]
    assert first["rule_name"] == "产品域名可访问检查"
    assert first["scope_label"] == "整个任务一次"
    assert first["page_type"] == "video_list"
    assert first["source"] == {
        "execution_id": "exec-source",
        "page_capture_id": "capture-0",
        "page_type": "home",
        "page_url": "https://demo.example/",
        "execution_status": "pass",
    }
    assert first["invalidate_on"] == [
        {"code": "account_or_login_changed", "label": "账号或登录态变化"},
        {"code": "rule_version_changed", "label": "规则版本变化"},
    ]
    assert first["basis"]["surface_kind"] == "web"
    # 记录里没写失效条件时按范围补齐；来源页面缺失时退回复用记录里的值。
    assert second["scope_label"] == "同一对象复用"
    assert [item["code"] for item in second["invalidate_on"]] == [
        "object_changed",
        "object_content_changed",
        "rule_version_changed",
        "task_boundary",
    ]
    assert second["source"]["execution_status"] == "fail"
    assert second["source"]["page_type"] == "home"
    # 按质检规则统一口径：复用台账不分 Run
    assert ("list_rule_reuse_rows", ("task-1", None, None)) in repository.calls


async def test_service_raises_not_found_for_unknown_task() -> None:
    service = TaskInsightService(repository=FakeInsightRepository([]))

    try:
        await service.rule_reuse("missing")
    except TaskInsightNotFoundError as exc:
        assert "missing" in str(exc)
    else:  # pragma: no cover - 断言失败路径
        raise AssertionError("未知任务应抛出 TaskInsightNotFoundError")


class FakeInsightService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.missing = False

    async def rule_reuse(self, task_id: str, *, access_scope=None) -> dict:
        if self.missing:
            raise TaskInsightNotFoundError(f"未找到任务：{task_id}")
        self.calls.append(("rule_reuse", (task_id, access_scope)))
        return {
            "protocol_version": "rule-reuse-list.v1",
            "task_id": task_id,
            "total": 1,
            "presentation": "reused_only",
            "items": [{"scope": "per_task", "scope_label": "整个任务一次"}],
        }


def _client() -> tuple[TestClient, FakeInsightService]:
    service = FakeInsightService()
    app = create_app()
    app.dependency_overrides[task_insight_module._get_task_insight_service] = lambda: service
    app.dependency_overrides[evidence_module._get_task_insight_service] = lambda: service
    return TestClient(app), service


def test_rule_reuse_route_passes_access_scope_and_returns_ledger() -> None:
    test_client, service = _client()

    response = test_client.get("/api/v1/workstation/tasks/task-1/rule-reuse")

    assert response.status_code == 200
    body = response.json()
    assert body["presentation"] == "reused_only"
    assert body["items"][0]["scope_label"] == "整个任务一次"
    name, (task_id, access_scope) = service.calls[0]
    assert name == "rule_reuse"
    assert task_id == "task-1"
    assert access_scope is not None
    assert access_scope.actor_user_id == "test-user"


def test_rule_reuse_route_maps_unknown_task_to_404() -> None:
    test_client, service = _client()
    service.missing = True

    response = test_client.get("/api/v1/workstation/tasks/task-9/rule-reuse")

    assert response.status_code == 404
    assert "task-9" in response.json()["error"]["message"]
