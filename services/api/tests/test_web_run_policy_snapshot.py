"""Web Run 完整策略的不可变正文与发布校验回归。"""

import hashlib
import json
from datetime import UTC, datetime

import pytest


def policy_payload():
    from services.api.app.application.web_run_policy import load_published_web_policy

    return load_published_web_policy(now=datetime(2026, 9, 9, tzinfo=UTC), max_duration_seconds=600)


def snapshot(payload=None):
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot

    return WebRunPolicySnapshot.create(
        task_id="task-a", agent_run_id="run-a", payload=payload or policy_payload()
    )


def test_snapshot_copies_and_deeply_freezes_full_json_body():
    body = policy_payload()
    frozen = snapshot(body)
    original = frozen.to_payload()
    body["page_catalog"]["page_types"][0]["purpose"] = "外部修改"
    assert frozen.to_payload() == original
    with pytest.raises(TypeError):
        frozen.payload["page_catalog"]["page_types"][0]["purpose"] = "内部修改"
    with pytest.raises((TypeError, AttributeError)):
        frozen.payload["page_catalog"]["page_types"].append({})
    canonical = json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert frozen.payload_sha256 == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert 10000 < len(canonical.encode("utf-8")) < 1048576


@pytest.mark.parametrize("field", ["workflow_skill", "page_catalog", "readiness_profile", "budget"])
def test_missing_full_body_cannot_be_replaced_by_version_label(field):
    body = policy_payload()
    del body[field]
    with pytest.raises(ValueError):
        snapshot(body)


def test_tampering_and_unsupported_semantics_fail_closed():
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot

    frozen = snapshot()
    body = frozen.to_payload()
    body["workflow_skill"] += "\n变更"
    with pytest.raises(ValueError, match="SHA256"):
        WebRunPolicySnapshot(
            task_id="task-a",
            agent_run_id="run-a",
            payload=body,
            payload_sha256=frozen.payload_sha256,
        )
    body["identity_policy_version"] = "future.v999"
    with pytest.raises(ValueError, match="VERSION"):
        snapshot(body)


@pytest.mark.parametrize("field", ["minimum_samples", "network_required", "maximum_capture_ms"])
def test_partial_readiness_body_does_not_fall_back_to_current_defaults(field):
    from services.api.app.application.web_run_policy import materialize_web_policy

    body = policy_payload()
    del body["readiness_profile"][field]
    with pytest.raises(ValueError, match="完整"):
        materialize_web_policy(snapshot(body))


@pytest.mark.parametrize("value", [None, [None], [""], "规则"])
def test_page_guide_rejects_coerced_or_empty_rule_identifiers(value):
    from services.api.app.application.web_run_policy import materialize_web_policy

    body = policy_payload()
    body["page_catalog"]["page_types"][0]["required_positive_signals"] = value
    with pytest.raises(ValueError):
        materialize_web_policy(snapshot(body))


@pytest.mark.parametrize("entries", [[None], ["corrupt"], {}])
def test_corrupt_page_catalog_shape_produces_controlled_policy_error(entries):
    from services.api.app.application.web_run_policy import materialize_web_policy
    body = policy_payload()
    body["page_catalog"]["page_types"] = entries
    with pytest.raises(ValueError, match="WEB_POLICY_BODY_INVALID"):
        materialize_web_policy(snapshot(body))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signals", None),
        ("signals", []),
        ("navigation_aliases", [None]),
        ("source", None),
        ("entry_hints", [None]),
        ("next_page_candidates", [None]),
    ],
)
def test_corrupt_nested_page_catalog_shape_produces_controlled_policy_error(field, value):
    from services.api.app.application.web_run_policy import materialize_web_policy
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicyError

    body = policy_payload()
    target = body["page_catalog"] if field == "source" else body["page_catalog"]["page_types"][0]
    target[field] = value
    with pytest.raises(WebRunPolicyError, match="WEB_POLICY_BODY_INVALID"):
        materialize_web_policy(snapshot(body))


def test_page_guide_positive_negative_conflict_rejected_before_freeze():
    from services.api.app.application.web_run_policy import materialize_web_policy

    body = policy_payload()
    guide = body["page_catalog"]["page_types"][0]
    guide["strong_negative_signals"].append(guide["required_positive_signals"][0])
    with pytest.raises(ValueError, match="冲突"):
        materialize_web_policy(snapshot(body))


def test_frozen_policy_materializes_same_deadline_and_workflow():
    from services.api.app.application.web_run_policy import materialize_web_policy

    policy = materialize_web_policy(snapshot())
    assert policy.deadline == datetime(2026, 9, 9, 0, 10, tzinfo=UTC)
    assert policy.readiness_profile.minimum_samples == 3
    assert "候选不是到达证明" in policy.workflow_skill
    assert policy.catalog.codes == (
        "home",
        "content_list",
        "content_detail",
        "video_list",
        "video_detail",
        "activity",
        "search_page",
        "collection_page",
        "age_gate",
        "ad_page",
        "recharge_page",
        "topup_page",
        "guide_page",
        "unknown",
    )
    assert policy.assessment_schema_version == "web-page-type-assessment.v4"


def test_new_web_policy_enables_full_page_supplemental_only_for_web_v2() -> None:
    from services.api.app.application.web_run_policy import materialize_web_policy

    body = policy_payload()

    assert body["full_page_supplemental_policy"] == {
        "protocol_version": "web-full-page-supplemental.v1",
        "enabled": True,
        "capture_role": "supplemental",
        "maximum_bytes": 16 * 1024 * 1024,
        "maximum_dimension": 32768,
        "maximum_pixels": 100_000_000,
        "stabilization": {
            "max_document_height": 3000, "scroll_offset_px": 1920,
            "max_scroll_steps": 80,
            "stable_rounds": 2, "lazy_load_timeout_ms": 8000,
        },
    }
    policy = materialize_web_policy(snapshot(body))
    assert policy.full_page_supplemental_policy.enabled is True


def test_runtime_without_full_page_section_is_rejected() -> None:
    from services.api.app.application.web_run_policy import materialize_web_policy

    body = policy_payload()
    del body["full_page_supplemental_policy"]

    with pytest.raises(ValueError, match="full_page_supplemental_policy"):
        materialize_web_policy(snapshot(body))


def test_historical_missing_full_page_section_remains_read_only() -> None:
    from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter

    body = policy_payload()
    del body["full_page_supplemental_policy"]
    audit = LegacyWebRunAuditAdapter.load(body)
    assert audit.execution_allowed is False
    assert "full_page_supplemental_policy" not in audit.frozen_fields


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", 1),
        ("capture_role", "primary"),
        ("maximum_bytes", 0),
        ("maximum_dimension", 32767),
        ("maximum_pixels", 100_000_001),
        ("protocol_version", "future.v2"),
    ],
)
def test_full_page_supplemental_policy_is_strictly_frozen(field, value) -> None:
    from services.api.app.application.web_run_policy import materialize_web_policy

    body = policy_payload()
    body["full_page_supplemental_policy"][field] = value

    with pytest.raises(ValueError, match="WEB_POLICY_BODY_INVALID"):
        materialize_web_policy(snapshot(body))


def test_new_policy_has_no_execution_allowlist_and_unbounded_runtime_navigation():
    """新运行仅按当前页面事实发现，不能依赖已发布站点或路径目录。"""
    from services.api.app.application.web_run_policy import materialize_web_policy
    body = policy_payload()
    assert body["menu_priors"] == {"schema_version": "web-runtime-navigation.v1"}
    assert body["budget"]["menu_limit"] == 10
    policy = materialize_web_policy(snapshot(body))
    assert policy.discovery_policy.enabled is True
    assert policy.discovery_policy.max_unlisted_menus is None
    assert policy.menu_limit == 10
    for url in ("https://heiliao.com/", "https://unknown.test/nested/menu"):
        from services.api.app.application.web_run_policy import build_new_web_navigation_context
        navigation = build_new_web_navigation_context(target_url=url, policy=policy)
        context = navigation.resolve_site_context(url)
        assert len(context.site.entries) == 1
        assert context.source_kind == "runtime"


@pytest.mark.parametrize("menu_limit", [5, 10])
def test_new_run_freezes_configured_menu_limit(monkeypatch, menu_limit) -> None:
    from services.api.app.application.web_run_policy import materialize_web_policy

    monkeypatch.setenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", str(menu_limit))
    body = policy_payload()
    frozen = snapshot(body)
    monkeypatch.setenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", "25")

    assert frozen.payload["budget"]["menu_limit"] == menu_limit
    assert materialize_web_policy(frozen).menu_limit == menu_limit


def test_new_run_freezes_current_frontier_policy() -> None:
    from services.api.app.domain.exploration.web_frontier_policy import WebFrontierPolicy

    body = policy_payload()

    assert body["frontier_policy"] == WebFrontierPolicy.for_new_run(
        max_duration_seconds=600
    ).to_payload()


def test_discovery_required_types_exclude_lifecycle_and_unknown() -> None:
    from services.api.app.domain.exploration.web_discovery_policy import (
        validate_discovery_required_types,
    )

    assert validate_discovery_required_types(["activity", "search_page"]) == (
        "activity", "search_page",
    )
    for page_type in ("age_gate", "guide_page", "unknown"):
        with pytest.raises(ValueError, match="规范 required"):
            validate_discovery_required_types([page_type])


def test_historical_allowlist_policy_remains_readable_with_frozen_positive_limit():
    from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
    from services.api.app.domain.exploration.web_discovery_policy import LEGACY_WEB_DISCOVERY_POLICY
    from services.api.app.domain.page.web_site_menu_registry import WebSiteMenuRegistry
    body = policy_payload()
    body["menu_priors"] = WebSiteMenuRegistry().to_payload()
    body["budget"]["menu_limit"] = 10
    body["discovery_policy"] = dict(LEGACY_WEB_DISCOVERY_POLICY)
    audit = LegacyWebRunAuditAdapter.load(snapshot(body))
    assert audit.frozen_fields["budget"]["menu_limit"] == 10
    assert audit.menu_registry_version == "web-menu-allowlist.v4"
    assert audit.execution_allowed is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("platform", "h5"),
        ("navigation_signals", None),
        ("common_site_observations", {}),
    ),
)
def test_frozen_policy_revalidates_web_catalog_root_fields(field, value):
    from services.api.app.application.web_run_policy import materialize_web_policy

    body = policy_payload()
    body["page_catalog"][field] = value

    with pytest.raises(ValueError, match="WEB_POLICY_BODY_INVALID"):
        materialize_web_policy(snapshot(body))


@pytest.mark.parametrize(
    ("field_path", "value"),
    (
        (("status",), {"invalid": 1}),
        (("signals", "dom_hints"), {"invalid": 1}),
    ),
)
def test_frozen_policy_rejects_invalid_home_page_type_shape(field_path, value):
    from services.api.app.application.web_run_policy import materialize_web_policy

    body = policy_payload()
    home = next(
        item for item in body["page_catalog"]["page_types"] if item["code"] == "home"
    )
    target = home
    for field in field_path[:-1]:
        target = target[field]
    target[field_path[-1]] = value

    with pytest.raises(ValueError, match="WEB_POLICY_BODY_INVALID"):
        materialize_web_policy(snapshot(body))


@pytest.mark.asyncio
async def test_provider_restores_full_policy_before_browser_build_and_new_run_refreshes(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from services.api.app.application import web_run_policy as policy_module
    from services.api.app.application.web_runtime_composition import PostgresWebRuntimeProvider
    from services.api.app.core.config import Settings
    from services.api.tests.test_web_runtime_composition import _web_record

    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)

    frozen = {}

    class Repository:
        async def read(self, *, task_id, agent_run_id):
            return frozen.get(agent_run_id)

        async def freeze_or_read(self, candidate):
            return frozen.setdefault(candidate.agent_run_id, candidate)

    builds = []
    browser = SimpleNamespace()
    persistence = SimpleNamespace()
    command_gate = SimpleNamespace()

    def build(**kwargs):
        assert frozen
        builds.append(kwargs)
        return SimpleNamespace(
            coordinator=SimpleNamespace(),
            surface_driver=SimpleNamespace(),
            surface_sessions=SimpleNamespace(),
            web_desktop_capture_policy=SimpleNamespace(),
            trusted_ui_snapshot_reader=SimpleNamespace(),
            web_ad_landing_capture_port=SimpleNamespace(),
            web_full_page_capture_browser=browser,
            web_page_capture_persistence=persistence,
            session_command_gate=command_gate,
        )

    catalog_file = tmp_path / "pages.json"
    body = policy_payload()["page_catalog"]
    catalog_file.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(policy_module, "_PAGE_CATALOG_PATH", catalog_file)
    provider = PostgresWebRuntimeProvider(
        capture_runtime_builder=build,
        assessment_agent=SimpleNamespace(),
        policy_repository=Repository(),
    )
    record = _web_record()
    record.task_config["_task_target"]["target_url"] = "http://91porna.com/"
    first = await provider.build(record=record, settings=Settings())
    body["notes"].append("发布正文已刷新")
    catalog_file.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    restored = await provider.build(record=record, settings=Settings())
    second = await provider.build(
        record=record.model_copy(update={"run_id": "run-b"}), settings=Settings()
    )
    assert first.policy_snapshot == restored.policy_snapshot
    assert restored.deadline == first.deadline
    assert second.policy_snapshot.payload["page_catalog"]["notes"][-1] == "发布正文已刷新"
    assert "发布正文已刷新" not in first.policy_snapshot.payload["page_catalog"]["notes"]
    assert builds[0]["web_run_policy"] == first.policy_snapshot
    assert (
        first.home_ready_capture_reader.source.readiness_profile
        == builds[0]["web_readiness_profile"]
    )


def test_assessment_agent_accepts_only_valid_frozen_workflow(monkeypatch):
    from services.api.app.agents.explorer import web_assessment_agent as module

    body = policy_payload()["workflow_skill"] + "\n冻结解释内容"
    monkeypatch.setattr(
        module,
        "load_web_explorer_workflow_skill_content",
        lambda: (_ for _ in ()).throw(AssertionError("恢复不得重读磁盘")),
    )

    class Agent:
        def __init__(self, **kwargs):
            self.instructions = kwargs["instructions"]

    monkeypatch.setattr(module, "Agent", Agent)
    built = module._build_agent(object(), workflow_skill_content=body)
    assert built.instructions.endswith(body)
    with pytest.raises(
        module.WebExplorerWorkflowSkillConfigurationError
        if hasattr(module, "WebExplorerWorkflowSkillConfigurationError")
        else RuntimeError
    ):
        module._build_agent(object(), workflow_skill_content="伪造正文")


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
async def test_web_browser_injects_bound_profile_over_request_metadata(timeout):
    from dataclasses import replace

    from services.api.app.application.web_readiness_contracts import DEFAULT_WEB_READINESS_PROFILE
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
    )
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    trusted = replace(DEFAULT_WEB_READINESS_PROFILE, minimum_samples=5)

    class Browser:
        async def capture(self, request):
            assert request.web_readiness_profile == trusted
            raise LookupError("已到真实采集边界")

    browser = WebDesktopCapturePolicyBrowser(browser=Browser(), readiness_profile=trusted)
    request = PageCaptureRequest(mode=PageCaptureMode.GOTO, url="https://example.test")
    with pytest.raises(LookupError):
        if timeout:
            await browser.capture_with_timeout(request, timeout_seconds=1)
        else:
            await browser.capture(request)


@pytest.mark.asyncio
async def test_web_admission_uses_frozen_profile_at_persistence_boundary():
    from contextlib import asynccontextmanager

    from services.api.app.infrastructure.db.page_capture_repository import (
        PostgresPageCaptureRepository,
    )
    from services.api.tests.test_web_capture_readiness_metrics import _stable_capture_metrics

    body = policy_payload()
    body["readiness_profile"]["minimum_samples"] = 8
    saved = {}

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield

        async def execute(self, *args):
            pass

        async def fetchval(self, query, *args):
            if "INSERT INTO qc_page_capture" in query:
                saved.update(json.loads(args[13]))
                raise LookupError("已到持久化边界")
            return 1

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    repository = PostgresPageCaptureRepository(
        pool=Pool(), page_graph_repository=None, web_run_policy=snapshot(body)
    )
    with pytest.raises(LookupError):
        await repository.save_capture(
            task_id="task-a",
            agent_run_id="run-a",
            page_capture_id="capture",
            url="https://example.test",
            mode="observe",
            reachable=True,
            title="测试",
            status_code=200,
            action_log=[],
            artifacts=[],
            metrics={"web_readiness": _stable_capture_metrics()},
        )
    assert saved["web_evidence_admission"]["can_enter_formal_bundle"] is False


def test_missing_policy_retains_preview_but_never_formal_interpretation():
    from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
    from services.api.tests.test_web_evidence_bundle import _valid_snapshot

    result = build_web_evidence_bundle(
        _valid_snapshot(), readiness_profile=None, policy_error="WEB_POLICY_HISTORY_MISSING"
    )
    assert result["status"] == "needs_review"
    assert "WEB_POLICY_HISTORY_MISSING" in result["reason_codes"]
    assert result["captures"]
    assert result["formal_pages"] == []


def test_real_factory_rejects_profile_that_disagrees_with_frozen_body():
    from dataclasses import replace

    from services.api.app.application.web_readiness_contracts import DEFAULT_WEB_READINESS_PROFILE
    from services.api.app.infrastructure.browser.capture_runtime import (
        build_default_web_capture_runtime,
    )

    with pytest.raises(ValueError, match="profile"):
        build_default_web_capture_runtime(
            browser=object(),
            web_desktop_policy_enabled=True,
            web_run_policy=snapshot(),
            web_readiness_profile=replace(DEFAULT_WEB_READINESS_PROFILE, minimum_samples=9),
        )


@pytest.mark.asyncio
async def test_missing_policy_table_is_a_diagnostic_not_an_unhandled_database_error():
    import asyncpg

    from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
        read_web_policy_on_connection,
    )

    class Connection:
        async def fetchrow(self, *args):
            raise asyncpg.UndefinedTableError("qc_web_run_policy_snapshot")

    with pytest.raises(ValueError, match="WEB_POLICY_STORAGE_UNAVAILABLE"):
        await read_web_policy_on_connection(Connection(), "task", "run")


@pytest.mark.asyncio
async def test_real_factory_carries_frozen_profile_to_browser_and_persistence(monkeypatch):
    from functools import partial
    from types import SimpleNamespace

    from services.api.app.application.web_runtime_composition import PostgresWebRuntimeProvider
    from services.api.app.core.config import Settings
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.app.infrastructure.browser.capture_runtime import (
        build_default_web_capture_runtime,
    )
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
    )
    from services.api.tests.test_web_runtime_composition import _web_record

    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)

    body = policy_payload()
    body["readiness_profile"]["minimum_samples"] = 8
    trusted = WebRunPolicySnapshot.create(task_id="task-web", agent_run_id="run-web", payload=body)

    class Repository:
        async def read(self, **kwargs):
            return trusted

    class Browser:
        async def capture(self, request):
            assert request.web_readiness_profile.minimum_samples == 8
            raise LookupError("采集边界")

    record = _web_record()
    record.task_config["_task_target"]["target_url"] = "http://91porna.com/"
    runtime = await PostgresWebRuntimeProvider(
        policy_repository=Repository(),
        assessment_agent=SimpleNamespace(),
        capture_runtime_builder=partial(build_default_web_capture_runtime, browser=Browser()),
    ).build(record=record, settings=Settings())
    with pytest.raises(LookupError):
        await runtime.capture_runtime.borrow_browser_for_legacy_agent_tool().capture(
            PageCaptureRequest(mode=PageCaptureMode.GOTO, url="https://example.test")
        )
    persistence = runtime.capture_runtime.coordinator._capture_writer._persistence
    assert persistence._repository._web_run_policy is trusted


@pytest.mark.asyncio
async def test_expired_frozen_run_never_reallocates_time_or_opens_session():
    from services.api.app.application.web_runtime_composition import ProductionWebV2RuntimeExecutor
    from services.api.app.core.config import Settings
    from services.api.tests.test_web_runtime_composition import _web_record

    calls = []

    class Runtime:
        deadline = datetime(2026, 9, 9, 0, 10, tzinfo=UTC)

        async def prepare_home(self, *args, **kwargs):
            calls.append("opened")
            raise AssertionError("期限已过不得开启浏览器")

        async def close(self, **kwargs):
            pass

    result = await ProductionWebV2RuntimeExecutor(
        worker_instance_id="test", now=lambda: datetime(2026, 9, 9, 1, tzinfo=UTC)
    ).run(Runtime(), record=_web_record(), settings=Settings())
    assert calls == []
    assert "WEB_POLICY_DEADLINE_EXPIRED" in result.error_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("signals", None), ("signals", []), ("navigation_aliases", [None])],
)
async def test_task_insight_corrupt_nested_policy_retains_only_diagnostic_preview(field, value):
    from services.api.app.application.task_insight import TaskInsightService
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.tests.test_web_evidence_bundle import _valid_snapshot
    from services.api.tests.test_web_evidence_bundle_api import _SCOPE, EvidenceRepository, _context

    class ContextRepository:
        async def fetch_task_context(self, task_id, **kwargs):
            return {**_context(platform="web"), "task_id": "task-web", "run_id": "run-web"}

    body = policy_payload()
    body["page_catalog"]["page_types"][0][field] = value
    frozen = WebRunPolicySnapshot.create(task_id="task-web", agent_run_id="run-web", payload=body)

    class PolicyRepository:
        async def read(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == ("task-web", "run-web")
            return frozen

    service = TaskInsightService(
        repository=ContextRepository(),
        web_evidence_repository=EvidenceRepository(_valid_snapshot()),
        web_policy_repository=PolicyRepository(),
    )
    result = await service.web_evidence("task-web", access_scope=_SCOPE)
    assert result["status"] == "needs_review"
    assert "WEB_POLICY_BODY_INVALID" in result["reason_codes"]
    assert result["captures"]
    assert result["formal_pages"] == []
    assert result["policy"] == {"status": "unavailable"}
    assert "workflow_skill" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("minimum_samples", [3, 9, None])
@pytest.mark.parametrize("legacy", [False, True])
async def test_task_insight_uses_trusted_policy_and_redacts_full_guide(minimum_samples, legacy):
    from services.api.app.application.task_insight import TaskInsightService
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.tests.test_web_evidence_bundle import _valid_snapshot
    from services.api.tests.test_web_evidence_bundle_api import _SCOPE, EvidenceRepository, _context

    data = _valid_snapshot()

    class ContextRepository:
        async def fetch_task_context(self, task_id, **kwargs):
            return {**_context(platform="web"), "task_id": "task-web", "run_id": "run-web"}

    body = policy_payload()
    body["readiness_profile"]["minimum_samples"] = minimum_samples or 3
    if legacy:
        from services.api.app.domain.page.web_site_menu_registry import WebSiteMenuRegistry
        body["menu_priors"] = WebSiteMenuRegistry().to_payload()
        body.pop("runtime_version")

    class PolicyRepository:
        async def read(self, **kwargs):
            return (
                None
                if minimum_samples is None
                else WebRunPolicySnapshot.create(
                    task_id="task-web", agent_run_id="run-web", payload=body
                )
            )

    service = TaskInsightService(
        repository=ContextRepository(),
        web_evidence_repository=EvidenceRepository(data),
        web_policy_repository=PolicyRepository(),
    )
    result = await service.web_evidence("task-web", access_scope=_SCOPE)
    assert result["status"] == ("evidence_ready" if minimum_samples == 3 else "needs_review")
    assert "workflow_skill" not in json.dumps(result)
    if minimum_samples is None:
        assert "WEB_POLICY_HISTORY_MISSING" in result["reason_codes"]
        assert result["formal_pages"] == []
    else:
        assert result["policy"]["status"] == "verified"
        assert ("evidence_incomplete" in result["reason_codes"]) == (minimum_samples == 9)
