"""任务详情过程可视化接口与证据回读接口的路由测试（假服务注入）。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import evidence as evidence_module
from services.api.app.api.v1 import object_access as object_access_module
from services.api.app.api.v1.workstation import task_insight as task_insight_module
from services.api.app.application.task_insight import TaskInsightNotFoundError
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_object_access


class FakeInsightService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.missing = False

    def _guard(self, task_id: str) -> None:
        if self.missing:
            raise TaskInsightNotFoundError(f"未找到任务：{task_id}")

    async def page_timeline(self, task_id: str, *, access_scope=None) -> dict:
        self._guard(task_id)
        self.calls.append(("page_timeline", task_id))
        return {
            "task_id": task_id,
            "nodes": [],
            "frontier": {"pending_count": 0},
            "h5_assessments": [{"status": "covered"}],
            "h5_evidence_inventory": {"insufficient_count": 0},
        }

    async def page_graph(self, task_id: str, *, access_scope=None) -> dict:
        self._guard(task_id)
        self.calls.append(("page_graph", task_id))
        return {
            "task_id": task_id,
            "status": "available",
            "states": [],
            "transitions": [],
        }

    async def page_recovery_route(
        self,
        task_id: str,
        *,
        source_state_id=None,
        target_identity=None,
        limit=20,
        access_scope=None,
    ) -> dict:
        self._guard(task_id)
        self.calls.append(
            (
                "page_recovery_route",
                (task_id, source_state_id, target_identity, limit),
            )
        )
        return {"task_id": task_id, "status": "available", "routes": []}

    async def evidence_index(self, task_id: str, *, access_scope=None) -> dict:
        self._guard(task_id)
        self.calls.append(("evidence_index", task_id))
        return {
            "index_id": "evidence-index:1",
            "status": "exploration_completed",
            "page_timeline": [],
        }

    async def rule_results(
        self,
        task_id: str,
        *,
        display_status: str | None = None,
        page_type: str | None = None,
        access_scope=None,
    ) -> dict:
        self._guard(task_id)
        self.calls.append(("rule_results", (task_id, display_status, page_type)))
        return {"task_id": task_id, "items": [], "total": 0, "status_counts": {}}

    async def stats(
        self,
        task_id: str,
        *,
        recent_limit: int = 10,
        access_scope=None,
    ) -> dict:
        self._guard(task_id)
        self.calls.append(("stats", (task_id, recent_limit)))
        return {"task_id": task_id, "status_counts": {}, "recent_anomalies": []}

    async def inspection_work_items(
        self,
        task_id: str,
        *,
        control_mode: str | None = None,
        access_scope=None,
    ) -> dict:
        self._guard(task_id)
        self.calls.append(("inspection_work_items", (task_id, control_mode)))
        return {
            "task_id": task_id,
            "control_mode": control_mode,
            "summary": {"work_item_count": 0},
            "items": [],
        }

    async def logs(
        self,
        task_id: str,
        *,
        level: str | None = None,
        page: int = 1,
        page_size: int = 100,
        access_scope=None,
    ) -> dict:
        self._guard(task_id)
        self.calls.append(("logs", (task_id, level, page, page_size)))
        return {"task_id": task_id, "items": [], "total": 0, "page": page, "page_size": page_size}

    async def logs_export_text(self, task_id: str, *, access_scope=None) -> str:
        self._guard(task_id)
        return "[2026-07-17 10:00:00] INFO  任务启动成功\n"

    async def screenshots(self, task_id: str, *, access_scope=None) -> dict:
        self._guard(task_id)
        return {"task_id": task_id, "groups": []}

    async def screenshots_archive(
        self,
        task_id: str,
        *,
        access_scope=None,
    ) -> tuple[str, bytes]:
        self._guard(task_id)
        return (f"task_{task_id}_screenshots.zip", b"PK\x03\x04fake")
    async def platform_progress_export(
        self,
        task_id: str,
        *,
        access_scope=None,
    ) -> tuple[str, bytes]:
        self._guard(task_id)
        return (f"alioth_platform_progress_{task_id}.xlsx", b"PK\x03\x04xlsx")

    async def evidence_file(self, artifact_id: str) -> dict | None:
        if artifact_id == "missing":
            return None
        return {
            "artifact_id": artifact_id,
            "artifact_type": "screenshot",
            "content_type": "image/png",
            "content": b"png-bytes",
        }


class FailingInsightService(FakeInsightService):
    async def page_timeline(self, task_id: str, *, access_scope=None) -> dict:
        del task_id, access_scope
        raise RuntimeError("postgres unavailable")


@pytest.fixture()
def client() -> tuple[TestClient, FakeInsightService]:
    service = FakeInsightService()
    app = create_app()
    app.dependency_overrides[task_insight_module._get_task_insight_service] = lambda: service
    app.dependency_overrides[evidence_module._get_task_insight_service] = lambda: service
    return TestClient(app), service


def test_evidence_index_route(client: tuple[TestClient, FakeInsightService]) -> None:
    test_client, service = client
    response = test_client.get("/api/v1/workstation/tasks/task-1/evidence-index")
    assert response.status_code == 200
    assert response.json()["status"] == "exploration_completed"
    assert ("evidence_index", "task-1") in service.calls


def test_page_timeline_route(client: tuple[TestClient, FakeInsightService]) -> None:
    test_client, service = client
    response = test_client.get("/api/v1/workstation/tasks/task-1/page-timeline")
    assert response.status_code == 200
    assert response.json()["task_id"] == "task-1"
    assert response.json()["h5_assessments"][0]["status"] == "covered"
    assert response.json()["h5_evidence_inventory"]["insufficient_count"] == 0
    assert ("page_timeline", "task-1") in service.calls


def test_page_timeline_backend_failure_returns_auditable_error_id() -> None:
    app = create_app()
    app.dependency_overrides[task_insight_module._get_task_insight_service] = (
        lambda: FailingInsightService()
    )
    api = TestClient(app, raise_server_exceptions=False)

    response = api.get("/api/v1/workstation/tasks/task-1/page-timeline")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "WorkstationReadError"
    assert error["error_id"].startswith("workstation-")
    assert error["error_id"] in error["message"]
    assert "postgres unavailable" not in error["message"]


def test_page_graph_routes_pass_scope_and_clamp_recovery_limit(
    client: tuple[TestClient, FakeInsightService],
) -> None:
    test_client, service = client
    graph = test_client.get("/api/v1/workstation/tasks/task-1/page-graph")
    assert graph.status_code == 200
    assert graph.json()["status"] == "available"
    assert ("page_graph", "task-1") in service.calls
    route = test_client.get(
        "/api/v1/workstation/tasks/task-1/page-graph/recovery-route",
        params={
            "source_state_id": "state-1",
            "target_identity": "identity-1",
            "limit": 999,
        },
    )
    assert route.status_code == 200
    assert (
        "page_recovery_route",
        ("task-1", "state-1", "identity-1", 100),
    ) in service.calls


def test_rule_results_route_passes_filters(
    client: tuple[TestClient, FakeInsightService],
) -> None:
    test_client, service = client
    response = test_client.get(
        "/api/v1/workstation/tasks/task-1/rule-results",
        params={"display_status": "pending", "page_type": "home"},
    )
    assert response.status_code == 200
    assert ("rule_results", ("task-1", "pending", "home")) in service.calls
    invalid = test_client.get(
        "/api/v1/workstation/tasks/task-1/rule-results",
        params={"display_status": "nonsense"},
    )
    assert invalid.status_code == 422


def test_stats_route_clamps_recent_limit(
    client: tuple[TestClient, FakeInsightService],
) -> None:
    test_client, service = client
    response = test_client.get(
        "/api/v1/workstation/tasks/task-1/stats", params={"recent_limit": 999}
    )
    assert response.status_code == 200
    assert ("stats", ("task-1", 50)) in service.calls


def test_inspection_work_items_route_passes_control_mode(
    client: tuple[TestClient, FakeInsightService],
) -> None:
    test_client, service = client
    response = test_client.get(
        "/api/v1/workstation/tasks/task-1/inspection-work-items",
        params={"control_mode": "enforced"},
    )
    assert response.status_code == 200
    assert response.json()["summary"]["work_item_count"] == 0
    assert (
        "inspection_work_items",
        ("task-1", "enforced"),
    ) in service.calls
    invalid = test_client.get(
        "/api/v1/workstation/tasks/task-1/inspection-work-items",
        params={"control_mode": "invalid"},
    )
    assert invalid.status_code == 422


def test_logs_route_and_export_headers(
    client: tuple[TestClient, FakeInsightService],
) -> None:
    test_client, service = client
    response = test_client.get(
        "/api/v1/workstation/tasks/task-1/logs",
        params={"level": "ERROR", "page": 2, "page_size": 10},
    )
    assert response.status_code == 200
    assert ("logs", ("task-1", "ERROR", 2, 10)) in service.calls
    export = test_client.get("/api/v1/workstation/tasks/task-1/logs/export")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/plain")
    assert "attachment" in export.headers["content-disposition"]
    assert "任务启动成功" in export.text


def test_screenshots_routes(client: tuple[TestClient, FakeInsightService]) -> None:
    test_client, _ = client
    listing = test_client.get("/api/v1/workstation/tasks/task-1/screenshots")
    assert listing.status_code == 200
    assert listing.json()["groups"] == []
    archive = test_client.get("/api/v1/workstation/tasks/task-1/screenshots/archive")
    assert archive.status_code == 200
    assert archive.headers["content-type"] == "application/zip"
    assert archive.content.startswith(b"PK")


def test_platform_progress_export_route(client: tuple[TestClient, FakeInsightService]) -> None:
    test_client, _ = client
    export = test_client.get(
        "/api/v1/workstation/tasks/task-1/platform-progress/export"
    )
    assert export.status_code == 200
    assert export.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment" in export.headers["content-disposition"]
    assert ".xlsx" in export.headers["content-disposition"]
    assert export.content.startswith(b"PK")


def test_missing_task_maps_to_404(client: tuple[TestClient, FakeInsightService]) -> None:
    test_client, service = client
    service.missing = True
    for path in (
        "/api/v1/workstation/tasks/nope/page-timeline",
        "/api/v1/workstation/tasks/nope/page-graph",
        "/api/v1/workstation/tasks/nope/page-graph/recovery-route",
        "/api/v1/workstation/tasks/nope/rule-results",
        "/api/v1/workstation/tasks/nope/stats",
        "/api/v1/workstation/tasks/nope/inspection-work-items",
        "/api/v1/workstation/tasks/nope/logs",
        "/api/v1/workstation/tasks/nope/logs/export",
        "/api/v1/workstation/tasks/nope/screenshots",
        "/api/v1/workstation/tasks/nope/screenshots/archive",
        "/api/v1/workstation/tasks/nope/platform-progress/export",
    ):
        assert test_client.get(path).status_code == 404, path


def test_evidence_file_route(client: tuple[TestClient, FakeInsightService]) -> None:
    test_client, _ = client
    issued = test_client.post("/api/v1/evidence/art-1/ticket")
    assert issued.status_code == 200
    ticket_url = f"/api/v1{issued.json()['url']}"
    assert "access_token=" not in ticket_url
    response = test_client.get(ticket_url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"png-bytes"
    assert "content-disposition" not in response.headers
    download = test_client.get(f"{ticket_url}&download=true")
    assert "attachment" in download.headers["content-disposition"]
    # 下载的文件名要带扩展名，否则存下来的截图没有文件类型
    assert download.headers["content-disposition"].endswith("art-1.png")
    missing_ticket = test_client.post("/api/v1/evidence/missing/ticket").json()["url"]
    missing = test_client.get(f"/api/v1{missing_ticket}")
    assert missing.status_code == 404
    assert test_client.get("/api/v1/evidence/art-1/file").status_code == 404
    mismatched = test_client.get(
        f"/api/v1/evidence/art-2/file?{ticket_url.split('?', 1)[1]}"
    )
    assert mismatched.status_code == 404


def test_task_and_artifact_cross_owner_are_hidden_as_not_found(
    client: tuple[TestClient, FakeInsightService],
) -> None:
    test_client, service = client

    class DenyObjectAccess:
        async def can_access_task(self, **_kwargs) -> bool:
            return False

        async def can_access_artifact(self, **_kwargs) -> bool:
            return False

    pop_test_object_access(test_client.app)
    test_client.app.dependency_overrides[
        object_access_module._get_object_access_repository
    ] = DenyObjectAccess

    task_response = test_client.get(
        "/api/v1/workstation/tasks/task-from-another-owner/page-timeline"
    )
    ticket_response = test_client.post(
        "/api/v1/evidence/artifact-from-another-owner/ticket"
    )

    assert task_response.status_code == 404
    assert ticket_response.status_code == 404
    assert service.calls == []
