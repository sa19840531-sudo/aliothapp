"""任务详情「证据缺口」只读接口：路由透传访问范围，服务层只汇总不执行补采。"""

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


def _gap(**overrides: Any) -> dict[str, Any]:
    payload = {
        "protocol_version": "evidence-gap.v1",
        "gap_id": "evidence-gap:abc",
        "task_id": "task-1",
        "rule_id": "rule-1",
        "rule_code": "alioth_5",
        "overlay_phase": "after_close",
        "recapture_policy": "manual",
        "disposition": "manual_review_required",
        "target": {"page_capture_id": "capture-1", "page_type": "home"},
        "missing": {"presentation": {"code": "BASE_PAGE_OCCLUDED"}},
        "evidence_goal": "home 的「页面可见内容」：底层被遮挡，需要弹窗关闭后的证据。",
        "requested_request": None,
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
            "target_url": "https://example.com",
            "task_config": {},
        }

    async def list_evidence_gap_rows(self, *, task_id, agent_run_id, access_scope=None):
        self.calls.append(("list_evidence_gap_rows", (task_id, agent_run_id, access_scope)))
        return list(self.rows)


def _row(**overrides: Any) -> dict[str, Any]:
    payload = {
        "execution_id": "exec-1",
        "rule_id": "rule-1",
        "rule_code": "alioth_5",
        "rule_name": "页面模块空白检查",
        "category": "visual",
        "rule_severity": "medium",
        "execution_status": "evidence_insufficient",
        "page_capture_id": "capture-1",
        "agent_run_id": "run-1",
        "evidence_gap": _gap(),
        "logical_page_key": "home",
        "state_fingerprint": "fp-1",
        "created_at": datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        "finished_at": None,
    }
    payload.update(overrides)
    return payload


async def test_service_summarizes_gaps_by_phase_policy_and_disposition() -> None:
    repository = FakeInsightRepository(
        [
            _row(),
            _row(
                execution_id="exec-2",
                rule_code="alioth_8",
                evidence_gap=json.dumps(
                    _gap(
                        gap_id="evidence-gap:def",
                        rule_code="alioth_8",
                        overlay_phase="not_applicable",
                        recapture_policy="equivalent",
                        disposition="recorded_only",
                        requested_request={"request_id": "evidence-request:1"},
                    ),
                    ensure_ascii=False,
                ),
            ),
            # 缺口字段缺失或损坏的行不进入台账，也不让接口报错。
            _row(execution_id="exec-3", evidence_gap=None),
            _row(execution_id="exec-4", evidence_gap="{not json"),
        ]
    )
    service = TaskInsightService(repository=repository)

    result = await service.evidence_gaps("task-1")

    assert result["protocol_version"] == "evidence-gap-list.v1"
    assert result["task_status"] == "review"
    assert result["run_id"] == "run-1"
    assert result["execution_policy"] == "recorded_only"
    assert result["total"] == 2
    assert result["phase_counts"] == {"after_close": 1, "not_applicable": 1}
    assert result["policy_counts"] == {"manual": 1, "equivalent": 1}
    assert result["disposition_counts"] == {"manual_review_required": 1, "recorded_only": 1}
    first, second = result["items"]
    assert first["rule_name"] == "页面模块空白检查"
    assert first["logical_page_key"] == "home"
    assert first["gap"]["overlay_phase"] == "after_close"
    assert second["gap"]["requested_request"] == {"request_id": "evidence-request:1"}
    # 按质检规则统一口径不分 Run，访问范围原样传给仓储。
    assert ("list_evidence_gap_rows", ("task-1", None, None)) in repository.calls


async def test_service_explains_why_a_gap_got_no_recapture_route() -> None:
    """没规划出补采路线的缺口，台账要给出原因的人话而不是原因码。"""

    repository = FakeInsightRepository(
        [
            _row(
                request_status="unfulfillable",
                request_resolution_reason="TRUSTED_RECOVERY_ROUTE_UNAVAILABLE",
            ),
            _row(
                execution_id="exec-2",
                request_status="running",
                request_resolution_reason=None,
            ),
            # 压根儿没提补采请求的缺口，不算「没规划出路线」。
            _row(execution_id="exec-3"),
        ]
    )
    service = TaskInsightService(repository=repository)

    result = await service.evidence_gaps("task-1")

    blocked, routed, untouched = result["items"]
    assert blocked["route_plan"]["state"] == "没规划出路线"
    assert blocked["route_plan"]["reason_label"] == "页面图里没有可信的低风险历史路线"
    assert "转场证明" in blocked["route_plan"]["advice"]
    assert routed["route_plan"]["state"] == "已规划出路线"
    assert untouched["route_plan"]["state"] == "未提补采请求"
    assert result["route_plan_counts"] == {
        "没规划出路线": 1,
        "已规划出路线": 1,
        "未提补采请求": 1,
    }
    assert result["route_blocked_reasons"] == {"页面图里没有可信的低风险历史路线": 1}


async def test_service_raises_not_found_for_unknown_task() -> None:
    service = TaskInsightService(repository=FakeInsightRepository([]))

    try:
        await service.evidence_gaps("missing")
    except TaskInsightNotFoundError as exc:
        assert "missing" in str(exc)
    else:  # pragma: no cover - 断言失败路径
        raise AssertionError("未知任务应抛出 TaskInsightNotFoundError")


class FakeInsightService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.missing = False

    async def evidence_gaps(self, task_id: str, *, access_scope=None) -> dict:
        if self.missing:
            raise TaskInsightNotFoundError(f"未找到任务：{task_id}")
        self.calls.append(("evidence_gaps", (task_id, access_scope)))
        return {
            "protocol_version": "evidence-gap-list.v1",
            "task_id": task_id,
            "total": 1,
            "execution_policy": "recorded_only",
            "items": [{"gap": _gap()}],
        }


def _client() -> tuple[TestClient, FakeInsightService]:
    service = FakeInsightService()
    app = create_app()
    app.dependency_overrides[task_insight_module._get_task_insight_service] = lambda: service
    app.dependency_overrides[evidence_module._get_task_insight_service] = lambda: service
    return TestClient(app), service


def test_evidence_gaps_route_passes_access_scope_and_returns_ledger() -> None:
    test_client, service = _client()

    response = test_client.get("/api/v1/workstation/tasks/task-1/evidence-gaps")

    assert response.status_code == 200
    body = response.json()
    assert body["execution_policy"] == "recorded_only"
    assert body["items"][0]["gap"]["overlay_phase"] == "after_close"
    name, (task_id, access_scope) = service.calls[0]
    assert name == "evidence_gaps"
    assert task_id == "task-1"
    assert access_scope is not None
    assert access_scope.actor_user_id == "test-user"


def test_evidence_gaps_route_maps_unknown_task_to_404() -> None:
    test_client, service = _client()
    service.missing = True

    response = test_client.get("/api/v1/workstation/tasks/task-9/evidence-gaps")

    assert response.status_code == 404
    assert "task-9" in response.json()["error"]["message"]
