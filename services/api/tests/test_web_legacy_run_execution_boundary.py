"""历史 Web 只读审计与执行隔离回归。"""

import json
from datetime import UTC, datetime
from pathlib import Path

import fakeredis
import pytest

from services.api.app.application import web_run_policy
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue


def test_new_runtime_context_has_no_site_registry_or_ad_flag():
    build = getattr(web_run_policy, "build_new_web_navigation_context", None)
    assert callable(build), "当前运行必须具有独立导航上下文"
    context = build(target_url="https://arbitrary.example/nested")
    assert context.target_origin == "https://arbitrary.example"
    assert context.discovery_policy.enabled is True
    assert not hasattr(context, "menu_registry")
    assert not hasattr(context, "direct_ad_page_enabled")


def test_legacy_audit_reads_registry_without_exposing_it_to_runtime():
    import importlib.util

    module_name = "services.api.app.application.web_legacy_run_audit"
    assert importlib.util.find_spec(module_name), "历史审计适配器必须独立于执行物化"
    from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
    from services.api.app.domain.page.web_site_menu_registry import WebSiteMenuRegistry

    body = web_run_policy.load_published_web_policy(now=datetime.now(UTC), max_duration_seconds=600)
    body["menu_priors"] = WebSiteMenuRegistry().to_payload()
    body["discovery_policy"]["direct_ad_page_enabled"] = False
    audit = LegacyWebRunAuditAdapter.load(body)
    assert audit.menu_registry_version == "web-menu-allowlist.v4"
    assert audit.direct_ad_page_enabled is False
    assert audit.execution_allowed is False
    with pytest.raises((AttributeError, TypeError)):
        audit.execution_allowed = True
    assert not hasattr(audit, "resolve_site_context")
    with pytest.raises(ValueError, match="WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"):
        web_run_policy.materialize_web_policy(
            web_run_policy.WebRunPolicySnapshot.create(
                task_id="old-task", agent_run_id="old-run", payload=body
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_worker_does_not_claim_non_terminal_legacy_run(status):
    queue = RedisAgentRunQueue(fakeredis.aioredis.FakeRedis(decode_responses=True), ttl_seconds=60)
    record = await queue.enqueue(
        "历史探索",
        task_id="old-task",
        task_config={"workflow_version": "v2", "_task_target": {"platform": "web"}},
    )
    record.status = type(record.status)(status)
    await queue.save(record)
    assert not await queue.claim_running(record, worker_instance_id="worker-current")
    persisted = await queue.get(record.run_id)
    assert persisted.status == "failed"
    assert persisted.evidence_gap == ["WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"]
    assert persisted.dispatch_attempt_count == 0
    assert persisted.started_at is None


@pytest.mark.asyncio
async def test_historical_rerun_has_new_identity_and_current_contract():
    from services.api.app.application.task_submission import TaskSubmissionService
    from services.api.app.application.workstation_tasks import WorkstationTaskService
    from services.api.app.domain.task import (
        AdultSelfAttestation,
        TaskExecutionMode,
        TaskTargetPlatform,
    )
    from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore
    from services.api.tests.test_task_submission import FakeTaskRepository
    from services.api.tests.test_workstation_task_service import (
        TEST_ACCESS_SCOPE,
        FakeLifecycleRepository,
        _task_detail,
    )

    source = _task_detail(
        status="done",
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
        target_platform=TaskTargetPlatform.WEB,
        browser_profile_id="desktop_fhd_1920x1080_v1",
        required_page_types=("video_list", "video_detail"),
        applicability_mode="catalog_declared",
        applicability_catalog_version="legacy-catalog",
        applicability_catalog_hash="legacy-hash",
        adult_self_attestation=AdultSelfAttestation(
            declared=True,
            statement_version="adult-v1",
            declared_by="test-user",
            declared_at=datetime.now(UTC),
        ),
    )
    queue = RedisAgentRunQueue(fakeredis.aioredis.FakeRedis(decode_responses=True), ttl_seconds=60)
    service = WorkstationTaskService(
        repository=FakeLifecycleRepository(source),
        submission_service=TaskSubmissionService(
            task_repository=FakeTaskRepository(),
            agent_run_queue=queue,
            web_agent_run_queue=queue,
            state_store=InMemoryAgentRunStateStore(),
        ),
    )
    result = await service.perform_operation(
        task_id=source.task_id,
        operation="rerun",
        access_scope=TEST_ACCESS_SCOPE,
    )
    run = await queue.get(result.run_id)
    assert result.task_id != source.task_id
    assert result.run_id != source.run_id
    assert run.task_config["source_task_id"] == source.task_id
    assert run.task_config["source_operation"] == "rerun"
    assert run.task_config["web_runtime_version"] == "web-runtime.v2"
    assert run.task_config["web_exploration_target"]["applicability_mode"] == "runtime_dynamic"
    assert run.task_config["web_exploration_target"]["catalog_version"] != "legacy-catalog"
    body = web_run_policy.load_published_web_policy(
        now=datetime.now(UTC),
        max_duration_seconds=600,
        exploration_target=run.task_config["web_exploration_target"],
    )
    assert body["runtime_version"] == "web-runtime.v2"
    assert body["discovery_policy"]["enabled"] is True
    assert await queue.claim_running(run, worker_instance_id="worker-current")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_worker_terminalizes_legacy_before_runtime_and_persists_history(status):
    from services.api.app import worker
    from services.api.app.core.config import Settings
    from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore

    queue = RedisAgentRunQueue(fakeredis.aioredis.FakeRedis(decode_responses=True), ttl_seconds=60)
    record = await queue.enqueue(
        "历史探索",
        task_id="legacy-task",
        task_config={
            "workflow_version": "v2",
            "execution_mode": "exploration_only",
            "_task_target": {"platform": "web"},
        },
    )
    record.status = type(record.status)(status)
    await queue.save(record)
    state = InMemoryAgentRunStateStore()
    await state.record_submitted(record)
    await worker._process_one(queue, record.run_id, settings=Settings(), state_store=state)
    assert (
        await queue.get(record.run_id)
    ).error_message == "WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"
    assert state.events[-1].event_type == "finished"
    assert state.events[-1].snapshot.status == "failed"


@pytest.mark.asyncio
async def test_legacy_without_snapshot_cannot_freeze_current_policy_in_provider():
    from services.api.app.application.web_runtime_composition import PostgresWebRuntimeProvider
    from services.api.app.core.config import Settings
    from services.api.tests.test_web_runtime_composition import _web_record

    class Repository:
        async def read(self, **kwargs):
            raise AssertionError("历史 Run 不得进入可冻结策略仓储")

    record = _web_record()
    record.task_config.pop("web_runtime_version", None)
    with pytest.raises(ValueError, match="WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"):
        await PostgresWebRuntimeProvider(policy_repository=Repository()).build(
            record=record, settings=Settings()
        )


def test_current_policy_rejects_disabled_discovery_even_with_current_version():
    body = web_run_policy.load_published_web_policy(now=datetime.now(UTC), max_duration_seconds=600)
    body["discovery_policy"]["enabled"] = False
    body["discovery_policy"]["max_unlisted_menus"] = 0
    snapshot = web_run_policy.WebRunPolicySnapshot.create(
        task_id="current-task", agent_run_id="current-run", payload=body
    )
    with pytest.raises(ValueError, match="WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED"):
        web_run_policy.materialize_web_policy(snapshot)


@pytest.mark.parametrize(
    "invalid", ["catalog", "menu_limit", "frontier", "supplemental", "combined"],
)
def test_current_marker_cannot_launder_incomplete_or_historical_policy(invalid):
    body = web_run_policy.load_published_web_policy(now=datetime.now(UTC), max_duration_seconds=600)
    if invalid in {"catalog", "combined"}:
        body["page_catalog"] = json.loads(
            (Path(__file__).parent / "fixtures/web_page_catalog_20260904_v3.json")
            .read_text(encoding="utf-8")
        )
        body["guide_version"] = "2026-09-04.web.v3"
    if invalid in {"menu_limit", "combined"}:
        body["budget"]["menu_limit"] = None
    if invalid in {"frontier", "combined"}:
        body.pop("frontier_policy")
    if invalid in {"supplemental", "combined"}:
        body.pop("full_page_supplemental_policy")
    with pytest.raises(ValueError, match="WEB_POLICY_BODY_INVALID"):
        web_run_policy.materialize_web_policy(web_run_policy.WebRunPolicySnapshot.create(
            task_id="task", agent_run_id="run", payload=body,
        ))


def test_menu_explorer_defaults_to_frozen_navigation_discovery():
    from services.api.app.application.web_menu_exploration import WebMenuExplorer

    context = web_run_policy.build_new_web_navigation_context(target_url="https://example.test")
    explorer = WebMenuExplorer(
        navigation_context=context, candidate_source=object(), page_visitor=object(),
    )
    assert explorer.discovery_policy is context.discovery_policy
    assert explorer.discovery_policy.enabled is True
