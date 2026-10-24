from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import tasks as tasks_module
from services.api.app.application.task_query import (
    AgentRunDetail,
    AgentRunStep,
    CandidateRule,
    RuleExecutionSummary,
    TaskDetail,
    TaskListItem,
    TaskQueryService,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.main import create_app

TEST_ACCESS_SCOPE = AccessScope(
    actor_user_id="test-user",
    tenant_id="tenant-1",
    permissions=frozenset({"taskObjects:read:any"}),
)


class FakeTaskQueryRepository:
    def __init__(self) -> None:
        self.task_id = str(uuid4())
        self.detail = TaskDetail(
            task_id=self.task_id,
            target_url="https://example.com/h5",
            ruleset_id=str(uuid4()),
            ruleset_code="alioth_first_ruleset",
            ruleset_version=1,
            channel="workbench",
            status="queued",
            priority=100,
            run_id=str(uuid4()),
            created_at=datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
            started_at=None,
            finished_at=None,
            last_error=None,
        )
        self.executions = [
            RuleExecutionSummary(
                execution_id=str(uuid4()),
                rule_id=str(uuid4()),
                rule_code="alioth_2",
                rule_name="文案语言规范检查",
                page_capture_id="capture-1",
                evidence_input_set_id="input-set-1",
                observation_packet_id="packet-1",
                evidence_artifact_ids=("artifact-1", "artifact-2"),
                execution_status="pass",
                judge_strategy="llm",
                confidence=0.91,
                severity="low",
                result_summary="通过",
                problem_desc=None,
                review_status=None,
                started_at=datetime(2026, 7, 7, 12, 1, tzinfo=UTC),
                finished_at=datetime(2026, 7, 7, 12, 2, tzinfo=UTC),
            )
        ]
        self.list_items = [
            TaskListItem(
                task_id=self.task_id,
                target_url="https://example.com/h5",
                ruleset_code="alioth_first_ruleset",
                ruleset_version=1,
                channel="workbench",
                status="queued",
                priority=100,
                run_id=self.detail.run_id,
                created_at=datetime(2026, 7, 7, 12, 0, tzinfo=UTC),
                started_at=None,
                finished_at=None,
            )
        ]
        self.candidate_rules = [
            CandidateRule(
                rule_id=str(uuid4()),
                rule_code="alioth_21",
                name="首页弹窗蒙层点击行为检查",
                description="验证首页弹窗蒙层点击后的页面行为。",
                category="ad",
                severity="high",
                executor_type="hybrid",
                page_type="home",
                applicable_page_types=["home"],
                check_point="检查首页弹窗蒙层点击后是否符合预期行为。",
                input_contract={"required_artifact_types": ["screenshot"]},
                judge_config={
                    "tool_refs": ["visual_rule_judge_tool"],
                    "capability_config": {
                        "deterministic_tools": ["safe_hit_test_tool"],
                        "required_artifact_types": ["screenshot"],
                    },
                },
                manual_review_policy={"min_confidence": 0.8},
                sort_order=210,
                match_reason="applicable_page_types",
            )
        ]
        self.agent_run = AgentRunDetail(
            run_id=self.detail.run_id or str(uuid4()),
            status="done",
            stop_reason="completed",
            error_message=None,
            total_steps=7,
            total_ai_calls=3,
            total_tokens=60526,
            input_tokens=50000,
            output_tokens=10526,
            worker_rss_before_bytes=200_000_000,
            worker_rss_after_bytes=220_000_000,
            worker_rss_delta_bytes=20_000_000,
            working_memory_bytes=48_000,
            action_trace_count=1,
            final_text="任务已完成。入口页识别为 home，规则匹配结果为空。",
            started_at=datetime(2026, 7, 7, 12, 1, tzinfo=UTC),
            finished_at=datetime(2026, 7, 7, 12, 2, tzinfo=UTC),
            steps=[
                AgentRunStep(
                    step_seq=1,
                    phase="page-detection",
                    thought_summary="采集入口页并识别页面类型",
                    selected_capability="page_capture_tool",
                    capability_kind="tool",
                    status="success",
                    latency_ms=1200,
                    error_message=None,
                    capability_input={"mode": "goto", "url": "https://example.com/home"},
                    capability_output={"execution_status": "success", "title": "示例首页"},
                )
            ],
        )
        self.list_calls: list[dict] = []
        self.candidate_rule_calls: list[dict] = []

    async def get_task_detail(
        self, task_id: str, *, access_scope: AccessScope
    ) -> TaskDetail | None:
        return self.detail if task_id == self.task_id else None

    async def get_latest_agent_run(
        self, task_id: str, *, access_scope: AccessScope
    ) -> AgentRunDetail | None:
        return self.agent_run if task_id == self.task_id else None

    async def list_rule_executions(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope,
    ) -> list[RuleExecutionSummary]:
        return self.executions if task_id == self.task_id else []

    async def list_tasks(
        self,
        *,
        status: str | None,
        limit: int,
        access_scope: AccessScope,
    ) -> list[TaskListItem]:
        self.list_calls.append({"status": status, "limit": limit})
        return self.list_items

    async def list_candidate_rules(
        self,
        *,
        task_id: str,
        page_type: str,
        page_url: str | None = None,
        access_scope: AccessScope,
        auth_gate: str | None = None,
    ) -> list[CandidateRule]:
        del auth_gate
        self.candidate_rule_calls.append(
            {
                "task_id": task_id,
                "page_type": page_type,
                "page_url": page_url,
            }
        )
        return self.candidate_rules if task_id == self.task_id else []


async def test_task_query_service_returns_task_detail() -> None:
    repository = FakeTaskQueryRepository()
    service = TaskQueryService(repository=repository)

    detail = await service.get_task_detail(
        repository.task_id,
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert detail is not None
    assert detail.task_id == repository.task_id
    assert detail.ruleset_code == "alioth_first_ruleset"
    assert detail.run_id == repository.detail.run_id


async def test_task_query_service_lists_tasks_with_status_filter() -> None:
    repository = FakeTaskQueryRepository()
    service = TaskQueryService(repository=repository)

    items = await service.list_tasks(
        status="queued",
        limit=20,
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert len(items) == 1
    assert items[0].task_id == repository.task_id
    assert repository.list_calls == [{"status": "queued", "limit": 20}]


async def test_task_query_service_lists_candidate_rules_with_page_type() -> None:
    repository = FakeTaskQueryRepository()
    service = TaskQueryService(repository=repository)

    rules = await service.list_candidate_rules(
        task_id=repository.task_id,
        page_type="home",
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert len(rules) == 1
    assert rules[0].rule_code == "alioth_21"
    assert rules[0].page_type == "home"
    assert rules[0].applicable_page_types == ["home"]
    assert rules[0].description == "验证首页弹窗蒙层点击后的页面行为。"
    assert rules[0].input_contract == {"required_artifact_types": ["screenshot"]}
    assert rules[0].manual_review_policy == {"min_confidence": 0.8}
    assert repository.candidate_rule_calls == [
        {
            "task_id": repository.task_id,
            "page_type": "home",
            "page_url": None,
        }
    ]


async def test_task_query_service_rejects_non_catalog_page_type() -> None:
    repository = FakeTaskQueryRepository()
    service = TaskQueryService(repository=repository)

    with pytest.raises(ValueError, match="未知页面类型代码：global"):
        await service.list_candidate_rules(
            task_id=repository.task_id,
            page_type="global",
            access_scope=TEST_ACCESS_SCOPE,
        )

    assert repository.candidate_rule_calls == []


async def test_task_query_service_returns_latest_agent_run() -> None:
    repository = FakeTaskQueryRepository()
    service = TaskQueryService(repository=repository)

    run = await service.get_latest_agent_run(
        repository.task_id,
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert run is not None
    assert run.run_id == repository.agent_run.run_id
    assert run.status == "done"
    assert run.final_text is not None
    assert len(run.steps) == 1
    assert run.steps[0].selected_capability == "page_capture_tool"


@dataclass
class FakeTaskQueryService:
    repository: FakeTaskQueryRepository
    rule_execution_calls: list[tuple[str, str | None]] | None = None

    async def get_task_detail(
        self, task_id: str, *, access_scope: AccessScope
    ) -> TaskDetail | None:
        return await self.repository.get_task_detail(task_id, access_scope=access_scope)

    async def list_rule_executions(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        access_scope: AccessScope,
    ) -> list[RuleExecutionSummary]:
        if self.rule_execution_calls is not None:
            self.rule_execution_calls.append((task_id, agent_run_id))
        return await self.repository.list_rule_executions(
            task_id,
            agent_run_id=agent_run_id,
            access_scope=access_scope,
        )

    async def list_tasks(
        self,
        *,
        status: str | None,
        limit: int,
        access_scope: AccessScope,
    ) -> list[TaskListItem]:
        return await self.repository.list_tasks(
            status=status,
            limit=limit,
            access_scope=access_scope,
        )

    async def list_candidate_rules(
        self,
        *,
        task_id: str,
        page_type: str,
        page_url: str | None = None,
        access_scope: AccessScope,
        auth_gate: str | None = None,
    ) -> list[CandidateRule]:
        return await self.repository.list_candidate_rules(
            task_id=task_id,
            page_type=page_type,
            page_url=page_url,
            access_scope=access_scope,
            auth_gate=auth_gate,
        )

    async def get_latest_agent_run(
        self, task_id: str, *, access_scope: AccessScope
    ) -> AgentRunDetail | None:
        return await self.repository.get_latest_agent_run(
            task_id,
            access_scope=access_scope,
        )


class FakeTaskInsightRepository:
    """候选规则整页概览分支用：返回任务已识别的页面类型集合。"""

    def __init__(self, page_types: list[str] | None = None) -> None:
        self.page_types = page_types if page_types is not None else ["home"]

    async def list_task_page_types(self, *, task_id: str) -> list[str]:
        return self.page_types


@pytest.fixture
def client() -> tuple[TestClient, FakeTaskQueryRepository]:
    repository = FakeTaskQueryRepository()
    service = FakeTaskQueryService(repository=repository)
    app = create_app()
    app.dependency_overrides[tasks_module._get_task_query_service] = lambda: service
    app.dependency_overrides[tasks_module._get_task_insight_repository] = (
        lambda: FakeTaskInsightRepository()
    )
    return TestClient(app), repository


def test_get_task_endpoint_returns_task_detail(
    client: tuple[TestClient, FakeTaskQueryRepository],
) -> None:
    test_client, repository = client

    response = test_client.get(f"/api/v1/tasks/{repository.task_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == repository.task_id
    assert body["target_url"] == "https://example.com/h5"
    assert body["ruleset_code"] == "alioth_first_ruleset"
    assert body["run_id"] == repository.detail.run_id


def test_list_tasks_endpoint_returns_task_items(
    client: tuple[TestClient, FakeTaskQueryRepository],
) -> None:
    test_client, repository = client

    response = test_client.get("/api/v1/tasks?status=queued&limit=20")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["task_id"] == repository.task_id
    assert body["items"][0]["status"] == "queued"
    assert repository.list_calls == [{"status": "queued", "limit": 20}]


def test_get_task_endpoint_returns_404_for_missing_task(
    client: tuple[TestClient, FakeTaskQueryRepository],
) -> None:
    test_client, _repository = client

    response = test_client.get(f"/api/v1/tasks/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AliothError"


def test_list_rule_executions_endpoint_returns_execution_summaries(
    client: tuple[TestClient, FakeTaskQueryRepository],
) -> None:
    test_client, repository = client

    response = test_client.get(f"/api/v1/tasks/{repository.task_id}/rule-executions")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["rule_code"] == "alioth_2"
    assert body["items"][0]["execution_status"] == "pass"
    assert body["items"][0]["page_capture_id"] == "capture-1"
    assert body["items"][0]["evidence_input_set_id"] == "input-set-1"
    assert body["items"][0]["observation_packet_id"] == "packet-1"
    assert body["items"][0]["evidence_artifact_ids"] == ["artifact-1", "artifact-2"]


def test_list_rule_executions_endpoint_filters_by_agent_run(
    client: tuple[TestClient, FakeTaskQueryRepository],
) -> None:
    test_client, repository = client
    service = FakeTaskQueryService(repository=repository, rule_execution_calls=[])
    test_client.app.dependency_overrides[tasks_module._get_task_query_service] = lambda: service

    response = test_client.get(
        f"/api/v1/tasks/{repository.task_id}/rule-executions",
        params={"agent_run_id": repository.detail.run_id},
    )

    assert response.status_code == 200
    assert service.rule_execution_calls == [
        (repository.task_id, repository.detail.run_id)
    ]


def test_list_candidate_rules_endpoint_returns_rules(
    client: tuple[TestClient, FakeTaskQueryRepository],
) -> None:
    test_client, repository = client

    response = test_client.get(f"/api/v1/tasks/{repository.task_id}/candidate-rules?page_type=home")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["rule_code"] == "alioth_21"
    assert body["items"][0]["description"] == "验证首页弹窗蒙层点击后的页面行为。"
    assert body["items"][0]["input_contract"] == {
        "required_artifact_types": ["screenshot"]
    }
    assert body["items"][0]["manual_review_policy"] == {"min_confidence": 0.8}
    assert body["items"][0]["applicable_page_types"] == ["home"]
    assert body["items"][0]["match_reason"] == "applicable_page_types"
    assert body["items"][0]["execution_route"]["strategy"] == "hybrid"
    assert body["items"][0]["execution_route"]["ready"] is True
    assert body["items"][0]["execution_route"]["rule_engine_ready"] is False
    assert repository.candidate_rule_calls == [
        {
            "task_id": repository.task_id,
            "page_type": "home",
            "page_url": None,
        }
    ]


def test_list_candidate_rules_endpoint_validates_page_type_and_supports_overview(
    client: tuple[TestClient, FakeTaskQueryRepository],
) -> None:
    """不带 page_type 返回"已识别页面"候选并集（此前为 422，前端整页概览会直接报错）；
    显式传入非法 page_type 仍然是 400。"""
    test_client, repository = client

    missing_response = test_client.get(f"/api/v1/tasks/{repository.task_id}/candidate-rules")
    invalid_response = test_client.get(
        f"/api/v1/tasks/{repository.task_id}/candidate-rules?page_type=ad_popup"
    )

    assert missing_response.status_code == 200
    overview_items = missing_response.json()["items"]
    assert [item["rule_code"] for item in overview_items] == [
        rule.rule_code for rule in repository.candidate_rules
    ]
    assert {
        "task_id": repository.task_id,
        "page_type": "home",
        "page_url": None,
    } in repository.candidate_rule_calls
    assert invalid_response.status_code == 400
    assert invalid_response.json()["error"]["message"].startswith("未知页面类型代码")


def test_get_agent_run_endpoint_returns_run_with_steps(
    client: tuple[TestClient, FakeTaskQueryRepository],
    monkeypatch,
) -> None:
    test_client, repository = client
    monkeypatch.setattr(
        tasks_module,
        "get_settings",
        lambda: SimpleNamespace(
            llm=SimpleNamespace(
                provider="custom",
                model="grok-4.5",
                cost_currency="CNY",
                input_price_per_million_tokens=1.0,
                output_price_per_million_tokens=3.0,
                cache_hit_price_per_million_tokens=0.25,
                long_context_threshold_tokens=200000,
                long_context_input_price_per_million_tokens=2.0,
                long_context_output_price_per_million_tokens=6.0,
                long_context_cache_hit_price_per_million_tokens=0.5,
            )
        ),
    )

    response = test_client.get(f"/api/v1/tasks/{repository.task_id}/agent-run")

    assert response.status_code == 200
    body = response.json()
    assert body["run"] is not None
    assert body["run"]["run_id"] == repository.agent_run.run_id
    assert body["run"]["status"] == "done"
    assert body["run"]["stop_reason"] == "completed"
    assert body["run"]["total_steps"] == 7
    assert body["run"]["input_tokens"] == 50000
    assert body["run"]["output_tokens"] == 10526
    assert body["run"]["llm_cost_estimate"] == {
        "provider": "custom",
        "model": "grok-4.5",
        "currency": "CNY",
        "input_tokens": 50000,
        "output_tokens": 10526,
        "cache_hit_tokens": 0,
        "billable_input_tokens": 50000,
        "input_cost": 0.05,
        "output_cost": 0.031578,
        "cache_hit_cost": 0.0,
        "total_cost": 0.081578,
        "pricing_unit": "1m_tokens",
        "pricing_source": "runtime_config_aggregate",
        "pricing_configured": True,
        "applied_pricing_tiers": ["0-200000"],
        "pricing_tier": "0-200000",
        "input_price_per_million_tokens": 1.0,
        "output_price_per_million_tokens": 3.0,
        "cache_hit_price_per_million_tokens": 0.25,
        "calculation_formula": (
            "(billable_input_tokens * input_price_per_million_tokens "
            "+ output_tokens * output_price_per_million_tokens "
            "+ cache_hit_tokens * cache_hit_price_per_million_tokens) / 1000000"
        ),
    }
    assert body["run"]["worker_rss_delta_bytes"] == 20_000_000
    assert body["run"]["working_memory_bytes"] == 48_000
    assert body["run"]["action_trace_count"] == 1
    assert "规则匹配结果为空" in body["run"]["final_text"]
    assert len(body["run"]["steps"]) == 1
    assert body["run"]["steps"][0]["step_seq"] == 1
    assert body["run"]["steps"][0]["selected_capability"] == "page_capture_tool"
    assert body["run"]["steps"][0]["capability_input"]["mode"] == "goto"
    assert body["run"]["steps"][0]["capability_output"]["execution_status"] == "success"


def test_get_agent_run_endpoint_reestimates_unconfigured_zero_cost(
    client: tuple[TestClient, FakeTaskQueryRepository],
    monkeypatch,
) -> None:
    test_client, repository = client
    repository.agent_run = replace(
        repository.agent_run,
        input_cost=0,
        output_cost=0,
        cache_hit_cost=0,
        total_cost=0,
        cost_currency="CNY",
        pricing_source="runtime_config_per_call",
        pricing_configured=False,
    )
    monkeypatch.setattr(
        tasks_module,
        "get_settings",
        lambda: SimpleNamespace(
            llm=SimpleNamespace(
                provider="custom",
                model="grok-4.5",
                cost_currency="CNY",
                input_price_per_million_tokens=1.0,
                output_price_per_million_tokens=3.0,
                cache_hit_price_per_million_tokens=0.25,
                long_context_threshold_tokens=200000,
                long_context_input_price_per_million_tokens=2.0,
                long_context_output_price_per_million_tokens=6.0,
                long_context_cache_hit_price_per_million_tokens=0.5,
            )
        ),
    )

    response = test_client.get(f"/api/v1/tasks/{repository.task_id}/agent-run")

    assert response.status_code == 200
    assert response.json()["run"]["llm_cost_estimate"]["total_cost"] == 0.081578
    assert response.json()["run"]["llm_cost_estimate"]["pricing_source"] == (
        "runtime_config_aggregate"
    )
    assert response.json()["run"]["llm_cost_estimate"]["pricing_configured"] is True


def test_get_agent_run_endpoint_returns_null_when_no_run(
    client: tuple[TestClient, FakeTaskQueryRepository],
) -> None:
    test_client, _repository = client

    response = test_client.get(f"/api/v1/tasks/{uuid4()}/agent-run")

    assert response.status_code == 200
    assert response.json()["run"] is None
