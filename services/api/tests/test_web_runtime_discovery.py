"""Web 任务级低风险发现配置、冻结与恢复链路回归。"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from services.api.app.api.v2.schemas import V2TaskSubmissionRequest
from services.api.app.application.v2_task_entries import ExplorationTaskV2EntryService
from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_runtime_composition import PostgresWebRuntimeProvider
from services.api.app.application.web_runtime_discovery import (
    classify_runtime_href,
    discovery_safety_reason,
)
from services.api.app.core.config import Settings
from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy
from services.api.tests.test_v2_task_entrypoints import (
    TEST_ACCESS_SCOPE,
    RecordingSubmissionService,
    _web_task_payload,
)
from services.api.tests.test_web_runtime_composition import _web_record


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("https://new.example/post/1", "actionable"),
        ("https://other.example/post/1", "cross_origin_blocked"),
        ("javascript:alert(1)", "unsafe_scheme"),
        ("data:text/html,x", "unsafe_scheme"),
    ],
)
def test_runtime_href_safety_is_origin_based(href: str, expected: str) -> None:
    assert classify_runtime_href("https://new.example/", href).code == expected


@pytest.mark.parametrize("href", ["", "   ", "#comments"])
def test_runtime_href_classifier_rejects_non_navigation_without_resolving_base(
    href: str,
) -> None:
    assert classify_runtime_href("https://new.example/current", href).code == "invalid_href"


def test_runtime_href_classifier_rejects_cross_origin_dom_source_even_for_site_target() -> None:
    classification = classify_runtime_href(
        "https://landing.example/redirected",
        "https://site.example/category",
        site_origin="https://site.example/",
    )

    assert classification.code == "source_origin_untrusted"


def test_runtime_href_classifier_does_not_reuse_root_http_upgrade_exception() -> None:
    """HTTP→HTTPS 只属于根导航建站点作用域，冻结后的普通导航必须严格同源。"""

    classification = classify_runtime_href(
        "http://site.example/current",
        "https://site.example/category",
        site_origin="http://site.example/",
    )

    assert classification.code == "cross_origin_blocked"


def test_persisted_authorization_uses_shared_source_origin_classification() -> None:
    candidate = WebNavigationCandidate(
        entry_id="cross-origin-source",
        label="返回分类",
        url="https://site.example/category",
        menu_path=("返回分类",),
        dom_fingerprint="cross-source",
        source_signal="semantic_nav",
        page_type_key="unknown",
        source_page_url="https://landing.example/redirected",
    )

    assert discovery_safety_reason(
        candidate,
        site_origin="https://site.example/",
    ) == "WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED"


@pytest.mark.asyncio
@pytest.mark.parametrize("target_url", ["http://91porna.com/", "https://unknown.example/news"])
async def test_current_discovery_survives_freeze_and_cold_restore(target_url, monkeypatch):
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        PostgresWebExplorationBudgetLedgerRepository,
    )

    ledger_reads = []

    async def read_absent_ledger(self, *, agent_run_id, site_id):
        ledger_reads.append((agent_run_id, site_id))
        return None

    # 本用例验证策略冻结；实际台账恢复由显式隔离 PG 用例覆盖，禁止访问默认库。
    monkeypatch.setenv("ALIOTH_DATABASE_URL", "postgresql://invalid@127.0.0.1:1/unused")
    monkeypatch.setattr(
        PostgresWebExplorationBudgetLedgerRepository, "read_summary", read_absent_ledger,
    )
    payload = _web_task_payload(url=target_url)
    payload["target"]["exploration_target"] = {
        "required_page_types": ["content_list", "content_detail"],
    }
    submission = RecordingSubmissionService()
    service = ExplorationTaskV2EntryService(submission, web_v2_mode="exploration_only")
    await service.submit(
        V2TaskSubmissionRequest.model_validate(payload).to_command(),
        access_scope=TEST_ACCESS_SCOPE,
    )
    config = submission.calls[0]["task_config"]
    assert "discovery_policy" not in config["web_exploration_target"]
    frozen = {}

    class Policies:
        async def read(self, *, task_id, agent_run_id):
            return frozen.get(agent_run_id)

        async def freeze_or_read(self, candidate):
            return frozen.setdefault(candidate.agent_run_id, candidate)

    def browser_builder(**kwargs):
        return SimpleNamespace(
            coordinator=SimpleNamespace(),
            surface_driver=SimpleNamespace(),
            surface_sessions=SimpleNamespace(),
            web_desktop_capture_policy=SimpleNamespace(),
            trusted_ui_snapshot_reader=SimpleNamespace(),
            web_ad_landing_capture_port=SimpleNamespace(),
            web_full_page_capture_browser=SimpleNamespace(),
            web_page_capture_persistence=SimpleNamespace(),
            session_command_gate=SimpleNamespace(),
        )

    record = _web_record()
    record.task_config.update(config)
    record.task_config["_task_target"]["target_url"] = target_url
    provider = PostgresWebRuntimeProvider(
        capture_runtime_builder=browser_builder,
        assessment_agent=SimpleNamespace(),
        policy_repository=Policies(),
    )
    first = await provider.build(record=record, settings=Settings())
    assert first.policy_snapshot.to_payload()["discovery_policy"]["enabled"] is True
    record.task_config["web_exploration_target"]["required_page_types"] = ["home"]
    restored = await PostgresWebRuntimeProvider(
        capture_runtime_builder=browser_builder,
        assessment_agent=SimpleNamespace(),
        policy_repository=Policies(),
    ).build(record=record, settings=Settings())
    assert restored.policy_snapshot == first.policy_snapshot
    assert restored.explorer.discovery_policy.enabled is True
    assert restored.explorer.discovery_policy.max_unlisted_menus is None
    # 首页仍用于入口导航，不擅自加入用户冻结的质检范围。
    assert restored.explorer.required_page_types == ("content_detail", "content_list")
    assert first.navigation_context.resolve_site_context(
        target_url
    ) == restored.navigation_context.resolve_site_context(target_url)
    assert len(ledger_reads) == 2
    assert ledger_reads[0] == ledger_reads[1]


@pytest.mark.parametrize(
    "policy",
    [
        {"enabled": "true", "max_unlisted_menus": 1, "same_origin_only": True},
        {"enabled": True, "max_unlisted_menus": True, "same_origin_only": True},
        {"enabled": True, "max_unlisted_menus": 0, "same_origin_only": True},
        {"enabled": True, "max_unlisted_menus": -1, "same_origin_only": True},
        {"enabled": True, "max_unlisted_menus": 1, "same_origin_only": False},
        {"enabled": True, "max_unlisted_menus": 1, "same_origin_only": True, "unsafe": True},
    ],
)
def test_web_request_rejects_unsafe_or_coerced_discovery_policy(policy):
    payload = _web_task_payload()
    payload["target"]["exploration_target"] = {"discovery_policy": policy}
    with pytest.raises(ValidationError):
        V2TaskSubmissionRequest.model_validate(payload)


def test_required_opportunities_precede_optional_catalog_with_explicit_historical_limit():
    from dataclasses import replace

    from services.api.app.application.web_menu_exploration import plan_web_menu_collection
    from services.api.app.domain.page import WebPageClassificationHint
    from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import _site

    site = _site()
    entries = list(site.entries)
    entries[11] = replace(
        entries[11],
        classification_hint=WebPageClassificationHint(
            expected_page_type="video_detail",
            confidence=0.6,
            source="catalog",
        ),
    )
    plan = plan_web_menu_collection(
        replace(site, entries=tuple(entries)),
        menu_limit=10,
        discovery_policy=WebDiscoveryPolicy(enabled=True, max_unlisted_menus=2),
        required_page_types=("video_detail", "content_list"),
    )
    assert plan.selected_entries[0].entry_id == "menu-11"
    assert len(plan.selected_entries) == 8
    assert len(plan.skipped_entries) == 3
    assert len({item.entry_id for item in plan.selected_entries}) == 8


def test_required_opportunities_never_expand_an_explicit_menu_limit():
    from dataclasses import replace

    from services.api.app.application.web_menu_exploration import plan_web_menu_collection
    from services.api.app.domain.page import WebPageClassificationHint
    from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import _site

    site = _site()
    entries = list(site.entries)
    entries[1] = replace(
        entries[1],
        classification_hint=WebPageClassificationHint(
            expected_page_type="video_detail",
            confidence=0.6,
            source="catalog",
        ),
    )
    entries[2] = replace(
        entries[2],
        classification_hint=WebPageClassificationHint(
            expected_page_type="content_list",
            confidence=0.6,
            source="catalog",
        ),
    )

    plan = plan_web_menu_collection(
        replace(site, entries=tuple(entries)),
        menu_limit=1,
        discovery_policy=WebDiscoveryPolicy(enabled=True, max_unlisted_menus=2),
        required_page_types=("video_detail", "content_list"),
    )

    assert [entry.entry_id for entry in plan.selected_entries] == ["menu-1"]
    assert len(plan.selected_entries) == 1
