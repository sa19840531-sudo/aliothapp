"""Web V2 证据包 Service/API 范围与 404 合同。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1.workstation import task_insight as task_insight_module
from services.api.app.application import task_insight as task_insight_service_module
from services.api.app.application.task_insight import (
    TaskInsightNotFoundError,
    TaskInsightService,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.main import create_app

_SCOPE = AccessScope(
    actor_user_id="00000000-0000-0000-0000-000000000101",
    tenant_id="00000000-0000-0000-0000-000000000201",
    permissions=frozenset({"taskObjects:read:tenant"}),
)


def _context(
    *,
    platform: str,
    target_platform: str | None = None,
    execution_mode: str = "exploration_only",
) -> dict[str, Any]:
    return {
        "task_id": "task-1",
        "run_id": "run-1",
        "status": "done",
        "target_platform": target_platform or platform,
        "task_config": {
            "workflow_version": "v2",
            "execution_mode": execution_mode,
            "_task_target": {
                "platform": platform,
                "web": (
                    {"browser_profile_id": "desktop_fhd_1920x1080_v1"}
                    if platform == "web"
                    else None
                ),
            },
        },
    }


def _exploration_context_without_legacy_target() -> dict[str, Any]:
    context = _context(platform="web")
    context["task_config"].pop("_task_target")
    context["task_config"]["web_exploration_target"] = {
        "browser_profile_id": "desktop_fhd_1920x1080_v1",
        "required_page_types": ["video_list", "video_detail"],
    }
    return context


class ContextRepository:
    def __init__(self, context: dict[str, Any]):
        self.context = context

    async def fetch_task_context(self, task_id: str, *, access_scope=None):
        assert task_id == "task-1"
        assert access_scope is _SCOPE
        return self.context


class EvidenceRepository:
    def __init__(self, snapshot: dict[str, object] | None):
        self.snapshot = snapshot
        self.calls: list[tuple[str, str, AccessScope]] = []

    async def list_for_task(self, *, task_id, agent_run_id, access_scope):
        self.calls.append((task_id, agent_run_id, access_scope))
        return self.snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        _context(platform="h5"),
        _context(platform="app"),
        _context(platform="h5", target_platform="web"),
    ],
)
async def test_service_rejects_h5_app_and_legacy_web_before_evidence_repository(
    context: dict[str, Any],
) -> None:
    evidence = EvidenceRepository(snapshot={})
    service = TaskInsightService(
        repository=ContextRepository(context),
        web_evidence_repository=evidence,
    )

    with pytest.raises(TaskInsightNotFoundError):
        await service.web_evidence("task-1", access_scope=_SCOPE)

    assert evidence.calls == []


@pytest.mark.asyncio
async def test_service_requires_current_run_to_have_unique_web_budget() -> None:
    evidence = EvidenceRepository(snapshot=None)
    service = TaskInsightService(
        repository=ContextRepository(_context(platform="web")),
        web_evidence_repository=evidence,
    )

    with pytest.raises(TaskInsightNotFoundError):
        await service.web_evidence("task-1", access_scope=_SCOPE)

    assert evidence.calls == [("task-1", "run-1", _SCOPE)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        _exploration_context_without_legacy_target(),
        _context(platform="web", execution_mode="full_inspection"),
    ],
)
async def test_service_accepts_exploration_and_full_web_v2_evidence_contexts(
    context: dict[str, Any],
) -> None:
    evidence = EvidenceRepository(snapshot=None)
    service = TaskInsightService(
        repository=ContextRepository(context),
        web_evidence_repository=evidence,
    )

    with pytest.raises(TaskInsightNotFoundError, match="唯一 Web V2 预算"):
        await service.web_evidence("task-1", access_scope=_SCOPE)

    assert evidence.calls == [("task-1", "run-1", _SCOPE)]


@pytest.mark.asyncio
async def test_service_summarizes_capabilities_from_formal_pages(monkeypatch) -> None:
    evidence = EvidenceRepository(snapshot={"task_id": "task-1", "agent_run_id": "run-1"})
    monkeypatch.setattr(
        task_insight_service_module,
        "build_web_evidence_bundle",
        lambda snapshot, **kwargs: {
            "task_id": snapshot["task_id"],
            "formal_pages": [
                {"product_capabilities": ["navigation", "content"]},
                {"product_capabilities": ["video", "content"]},
            ],
        },
    )

    class PolicyRepository:
        async def read(self, *, task_id, agent_run_id):
            from services.api.app.domain.exploration.web_run_policy_snapshot import (
                WebRunPolicySnapshot,
            )
            from services.api.tests.test_web_run_policy_snapshot import policy_payload

            return WebRunPolicySnapshot.create(
                task_id=task_id, agent_run_id=agent_run_id, payload=policy_payload()
            )

    service = TaskInsightService(
        repository=ContextRepository(_context(platform="web")),
        web_evidence_repository=evidence,
        web_policy_repository=PolicyRepository(),
    )

    result = await service.web_evidence("task-1", access_scope=_SCOPE)

    assert result["product_capability_summary"] == {
        "capabilities": ["content", "navigation", "video"],
        "formal_page_count": 2,
        "source": "formal_capture_assessments",
    }


class ApiService:
    def __init__(self) -> None:
        self.missing = False
        self.calls: list[tuple[str, Any]] = []

    async def web_evidence(self, task_id: str, *, access_scope=None) -> dict[str, object]:
        self.calls.append((task_id, access_scope))
        if self.missing:
            raise TaskInsightNotFoundError("不是当前 Web V2 任务")
        return {
            "task_id": task_id,
            "agent_run_id": "run-1",
            "site_id": "site-1",
            "status": "evidence_ready",
            "reason_codes": [],
            "summary": {"home_count": 1, "menu_count": 1, "detail_count": 1},
            "entries": [],
            "captures": [],
        }


def test_web_evidence_route_uses_task_read_scope_and_maps_not_found() -> None:
    service = ApiService()
    app = create_app()
    app.dependency_overrides[task_insight_module._get_task_insight_service] = lambda: service
    client = TestClient(app)

    response = client.get("/api/v1/workstation/tasks/task-1/web-evidence")

    assert response.status_code == 200
    assert response.json()["status"] == "evidence_ready"
    assert service.calls[0][0] == "task-1"
    assert isinstance(service.calls[0][1], AccessScope)

    service.missing = True
    missing = client.get("/api/v1/workstation/tasks/task-1/web-evidence")
    assert missing.status_code == 404
