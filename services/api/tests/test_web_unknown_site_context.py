"""未登记站点的首页、预算与静态提示边界。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.web_page_classification_hints import (
    resolve_web_page_classification_hint,
)
from services.api.app.application.web_runtime_composition import WebV2Runtime
from services.api.app.domain.page.web_site_menu_registry import (
    WebMenuDecision,
    WebSiteMenuCatalogError,
    WebSiteMenuRegistry,
)
from services.api.tests.test_web_menu_exploration import _command


def test_unknown_site_context_normalizes_origin_and_keeps_only_home_baseline():
    registry = WebSiteMenuRegistry()
    first = registry.resolve_site_context("https://EXAMPLE.net:443/path?q=1#section")
    restored = WebSiteMenuRegistry().resolve_site_context("https://example.net/other")
    assert first == restored
    assert first.source_kind == "runtime"
    assert first.site.origin == "https://example.net"
    assert len(first.site.site_id) == 64
    assert len(first.content_sha256) == 64
    assert len(first.site.entries) == 1
    entry = first.site.entries[0]
    assert entry.decision is WebMenuDecision.HOME_BASELINE
    assert entry.canonical_url == "https://example.net/"
    assert entry.classification_hint is None
    assert entry.collection_eligibility.eligible
    assert first.content_sha256 != registry.catalog_content_sha256
    assert (
        first.site.site_id
        != registry.resolve_site_context("https://example.net:8443/").site.site_id
    )


def test_catalog_context_preserves_published_identity_and_order():
    registry = WebSiteMenuRegistry()
    context = registry.resolve_site_context("https://heiliao.com/anything")
    assert context.source_kind == "catalog"
    assert context.site is registry.resolve_site("https://heiliao.com/")
    assert context.content_sha256 == registry.catalog_content_sha256


@pytest.mark.parametrize(
    "url", ["file:///tmp/a", "https://name:secret@example.net/", "https://example.net:wrong/"]
)
def test_runtime_context_rejects_non_web_or_credential_urls(url):
    with pytest.raises(WebSiteMenuCatalogError):
        WebSiteMenuRegistry().resolve_site_context(url)


def test_missing_static_hint_keeps_same_origin_page_classifiable():
    site = WebSiteMenuRegistry().resolve_site("https://heiliao.com/")
    hint = resolve_web_page_classification_hint(
        site=site, url="https://heiliao.com/unlisted-category"
    )
    assert hint.hint is None
    assert hint.entry_ids == ()
    assert hint.eligibility.eligible


def test_unlisted_cross_origin_hint_does_not_authorize_collection():
    site = WebSiteMenuRegistry().resolve_site("https://heiliao.com/")
    hint = resolve_web_page_classification_hint(site=site, url="https://outside.test/news")
    assert hint.hint is None
    assert not hint.eligibility.eligible


def test_ipv6_runtime_home_is_valid_and_default_port_stable():
    registry = WebSiteMenuRegistry()
    context = registry.resolve_site_context("https://[::1]:443/news")
    assert context.site.origin == "https://[::1]"
    assert context.site.entries[0].canonical_url == "https://[::1]/"
    assert context == registry.resolve_site_context("https://[::1]/")


@pytest.mark.asyncio
async def test_unknown_runtime_bootstraps_origin_context_before_navigating_home():
    events = []
    registry = WebSiteMenuRegistry()
    command = _command("https://example.net/start")

    class Bootstrapper:
        async def open_session(self, command):
            events.append("session")

        async def navigate_home(self, command):
            events.append("home")
            return "arrived"

    class Ledger:
        async def bootstrap(self, command, context, **kwargs):
            events.append("budget")
            assert context.source_kind == "runtime"
            assert context.site.origin == "https://example.net"
            assert kwargs["catalog_sha256"] == context.content_sha256

    runtime = WebV2Runtime(
        bootstrapper=Bootstrapper(),
        capture_runtime=SimpleNamespace(),
        navigation_context=registry,
        budget_ledger=Ledger(),
    )
    assert await runtime.start(command) == "arrived"
    assert events == ["session", "budget", "home"]


@pytest.mark.asyncio
async def test_unknown_context_materializes_only_pending_home_in_budget():
    import json

    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        PostgresWebExplorationBudgetLedgerRepository,
    )
    from services.api.tests.test_web_exploration_budget_ledger_repository import (
        FakeConnection,
        FakePool,
    )

    context = WebSiteMenuRegistry().resolve_site_context("https://example.net/")
    command = _command("https://example.net/")
    entry = context.site.entries[0]
    from hashlib import sha256

    row = {
        "menu_entry_id": entry.entry_id,
        "menu_path": ["首页"],
        "canonical_url_sha256": sha256(b"https://example.net/").hexdigest(),
        "decision": "home_baseline",
        "catalog_position": 0,
        "execution_slot": None,
        "status": "home_pending",
        "diagnostic": {},
        "logical_alias_of": None,
    }
    connection = FakeConnection(
        fetchrow_results=[
            {"task_id": command.task_id, "session_id": command.session_id},
            None,
            {"site_id": context.site.site_id},
        ],
        fetch_results=[[row], [row]],
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))
    await repository.bootstrap(
        command,
        context,
        catalog_version=context.site.catalog_version,
        catalog_sha256=context.content_sha256,
    )
    call = next(call for call in connection.calls if "jsonb_to_recordset" in call[1])
    payload = json.loads(call[2][-1])
    assert len(payload) == 1
    assert payload[0]["status"] == "home_pending"
    assert payload[0]["execution_slot"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_url", ["https://example.net/", "http://example.net/"])
async def test_unknown_site_explores_dom_menus_without_static_hint_and_blocks_risk(target_url):
    from hashlib import sha256

    from services.api.app.application.web_exploration_budget_ledger import WebMenuActionReservation
    from services.api.app.application.web_menu_exploration import (
        WebMenuExplorationSummary,
        WebMenuExplorer,
        WebMenuVisitResult,
        _ledger,
    )
    from services.api.app.domain.exploration import WebMenuTerminalStatus
    from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy
    from services.api.tests.test_web_desktop_navigation import (
        _runtime_navigation_candidates as extract_navigation_candidates,
    )

    registry = WebSiteMenuRegistry()
    context = registry.resolve_site_context(target_url)
    candidates = extract_navigation_candidates(
        '<nav><a href="/news">资讯</a><a href="/videos">视频分类</a>'
        '<a href="/login">登录</a><a href="/pay">支付</a>'
        '<a href="https://outside.test/">外站</a></nav>',
        base_url="https://example.net/",
        source_capture_id="capture-home",
        include_blocked=True,
    )
    from services.api.app.application.web_runtime_discovery import CanonicalSiteScope
    scope = CanonicalSiteScope.bind(target_url, ("https://example.net/",))
    candidates = tuple(
        replace(candidate, frontier_site_origin=scope.canonical_site_origin)
        for candidate in candidates
    )
    visited, claimed, reasons = [], [], {}
    home = context.site.entries[0]
    home_row = replace(
        _ledger(
            home,
            status=WebMenuTerminalStatus.HOME_BASELINE,
            source_capture_id="capture-home",
            page_type="home",
        ),
        canonical_url="sha256:" + sha256(home.canonical_url.encode()).hexdigest(),
    )
    summary = WebMenuExplorationSummary(
        site_id=context.site.site_id,
        catalog_version=context.site.catalog_version,
        catalog_sha256=context.content_sha256,
        page_timeline=("capture-home",),
        page_type_keys=("home",),
        menu_ledger=(home_row,),
    )

    class Ledger:
        async def read_summary(self, **kwargs):
            return summary

        async def record_discovery(self, **kwargs):
            reasons[kwargs["candidate"].label] = kwargs["error_code"]

        async def claim_menu(self, **kwargs):
            entry = kwargs["entry"]
            claimed.append(entry)
            return WebMenuActionReservation(
                site_id=context.site.site_id,
                menu_entry_id=entry.entry_id,
                execution_slot=len(claimed),
                intent_id=f"intent-{len(claimed)}",
                already_reserved=False,
            )

        async def finalize(self, record):
            pass

    class Visitor:
        async def visit(self, **kwargs):
            visited.append(kwargs["entry"])
            return WebMenuVisitResult(
                final_capture_id=None,
                page_type=None,
                status=WebMenuTerminalStatus.NAVIGATION_FAILED,
            )

    explorer = WebMenuExplorer(
        navigation_context=registry,
        candidate_source=SimpleNamespace(navigation_candidates=lambda session: candidates),
        page_visitor=Visitor(),
        budget_ledger=Ledger(),
        discovery_policy=WebDiscoveryPolicy(enabled=True, max_unlisted_menus=10),
    )
    result = await explorer.explore(
        _command(target_url),
        initial_capture_id="capture-home",
        initial_page_type="home",
    )
    assert [entry.canonical_url for entry in visited] == [
        "https://example.net/news",
        "https://example.net/videos",
    ]
    assert all(entry.classification_hint is None for entry in claimed)
    assert result.catalog_sha256 == context.content_sha256
    assert reasons["登录"] == "WEB_DISCOVERY_LOGIN_BLOCKED"
    assert reasons["支付"] == "WEB_DISCOVERY_PAYMENT_BLOCKED"
    assert reasons["外站"] == "WEB_DISCOVERY_CROSS_ORIGIN_BLOCKED"
