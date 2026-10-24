"""任务详情「页型判断与改判」只读接口：服务层按页面裁定并标出与正式识别的差异，路由透传访问范围。"""

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

    async def list_page_type_judgement_rows(self, *, task_id, agent_run_id, access_scope=None):
        self.calls.append(("list_page_type_judgement_rows", (task_id, agent_run_id, access_scope)))
        return list(self.rows)


def _row(**overrides: Any) -> dict[str, Any]:
    payload = {
        "judgement_id": "j-1",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "page_capture_id": "capture-1",
        "sequence_no": 1,
        "source": "structure_rules",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.9,
        "catalog_version": "v1",
        "kind": "judgement",
        "revises_judgement_id": None,
        "basis": json.dumps({"detection_status": "recognized"}),
        "task_finalization_state": "open",
        "created_at": datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
        "formal_page_type": "home",
        "page_seq": 1,
        "page_slug": "home",
        "page_url": "https://demo.example/",
        "logical_page_key": "web:demo.example/",
    }
    payload.update(overrides)
    return payload


async def test_service_resolves_pages_and_flags_differences_from_formal_type() -> None:
    repository = FakeInsightRepository(
        [
            _row(),
            _row(judgement_id="j-2", sequence_no=2, source="model", page_type="home"),
            # 第二页：结构规则与模型打架 → 未知，与正式识别不一致
            _row(
                judgement_id="j-3",
                page_capture_id="capture-2",
                sequence_no=1,
                source="structure_rules",
                page_type="video_list",
                formal_page_type="video_list",
                page_seq=2,
                page_slug="list",
                created_at=datetime(2026, 9, 9, 12, 1, tzinfo=UTC),
            ),
            _row(
                judgement_id="j-4",
                page_capture_id="capture-2",
                sequence_no=2,
                source="model",
                page_type="content_list",
                formal_page_type="video_list",
                page_seq=2,
                created_at=datetime(2026, 9, 9, 12, 1, 1, tzinfo=UTC),
            ),
            # 坏行不进台账也不报错
            _row(judgement_id="", page_capture_id="capture-3", sequence_no=1),
        ]
    )
    service = TaskInsightService(repository=repository)

    result = await service.page_type_judgements("task-1")

    assert result["protocol_version"] == "page-type-judgement-list.v1"
    assert result["resolution_policy"] == "recorded_only"
    assert result["total"] == 2
    assert result["status_counts"] == {"confirmed": 1, "conflicting": 1}
    assert result["conflicting_count"] == 1
    assert result["revised_count"] == 0
    assert result["differs_count"] == 1
    first, second = result["items"]
    assert first["page_capture_id"] == "capture-1"
    assert first["formal_page_type"] == "home"
    assert first["resolution"]["status"] == "confirmed"
    assert first["resolution"]["status_label"] == "判断一致"
    assert first["differs_from_formal"] is False
    assert [item["source_label"] for item in first["judgements"]] == ["结构规则", "模型判断"]
    assert first["judgements"][0]["basis"] == {"detection_status": "recognized"}
    assert second["page_capture_id"] == "capture-2"
    assert second["page_seq"] == 2
    assert second["resolution"]["status"] == "conflicting"
    assert second["resolution"]["page_type"] is None
    assert second["resolution"]["conflicting_page_types"] == ["content_list", "video_list"]
    assert second["differs_from_formal"] is True
    assert ("list_page_type_judgement_rows", ("task-1", "run-1", None)) in repository.calls


async def test_service_raises_not_found_for_unknown_task() -> None:
    service = TaskInsightService(repository=FakeInsightRepository([]))

    try:
        await service.page_type_judgements("missing")
    except TaskInsightNotFoundError as exc:
        assert "missing" in str(exc)
    else:  # pragma: no cover - 断言失败路径
        raise AssertionError("未知任务应抛出 TaskInsightNotFoundError")


class FakeInsightService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.missing = False

    async def page_type_judgements(self, task_id: str, *, access_scope=None) -> dict:
        if self.missing:
            raise TaskInsightNotFoundError(f"未找到任务：{task_id}")
        self.calls.append(("page_type_judgements", (task_id, access_scope)))
        return {
            "protocol_version": "page-type-judgement-list.v1",
            "task_id": task_id,
            "total": 1,
            "resolution_policy": "recorded_only",
            "items": [{"resolution": {"status": "conflicting"}}],
        }


def _client() -> tuple[TestClient, FakeInsightService]:
    service = FakeInsightService()
    app = create_app()
    app.dependency_overrides[task_insight_module._get_task_insight_service] = lambda: service
    app.dependency_overrides[evidence_module._get_task_insight_service] = lambda: service
    return TestClient(app), service


def test_page_type_judgements_route_passes_access_scope_and_returns_ledger() -> None:
    test_client, service = _client()

    response = test_client.get("/api/v1/workstation/tasks/task-1/page-type-judgements")

    assert response.status_code == 200
    body = response.json()
    assert body["resolution_policy"] == "recorded_only"
    assert body["items"][0]["resolution"]["status"] == "conflicting"
    name, (task_id, access_scope) = service.calls[0]
    assert name == "page_type_judgements"
    assert task_id == "task-1"
    assert access_scope is not None
    assert access_scope.actor_user_id == "test-user"


def test_page_type_judgements_route_maps_unknown_task_to_404() -> None:
    test_client, service = _client()
    service.missing = True

    response = test_client.get("/api/v1/workstation/tasks/task-9/page-type-judgements")

    assert response.status_code == 404
    assert "task-9" in response.json()["error"]["message"]
