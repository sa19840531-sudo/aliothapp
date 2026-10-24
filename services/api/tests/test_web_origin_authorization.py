"""浏览器规范 origin 与单向首跳升级授权。"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from services.api.app.application.web_page_classification_hints import (
    resolve_web_page_classification_hint,
)
from services.api.app.application.web_runtime_discovery import (
    WebRuntimeMenuEntry,
    discovery_safety_reason,
)
from services.api.app.domain.page.web_site_menu_registry import WebSiteMenuRegistry
from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY
from services.api.tests.test_web_desktop_navigation import (
    _runtime_navigation_candidates as extract_navigation_candidates,
)
from services.api.tests.test_web_menu_exploration import _command


@pytest.mark.parametrize(
    "input_url,browser_url",
    [
        ("https://例子.测试/", "https://xn--fsqu00a.xn--0zwm56d/"),
        ("https://faß.de/", "https://xn--fa-hia.de/"),
        ("https://[2001:0db8:0:0:0:0:0:1]:443/", "https://[2001:db8::1]/"),
        ("https://EXAMPLE.net:443/", "https://example.net/"),
    ],
)
def test_browser_normalized_origin_keeps_context_identity_dom_and_authorization(
    input_url, browser_url
):
    registry = WebSiteMenuRegistry()
    context = registry.resolve_site_context(input_url)
    assert context == registry.resolve_site_context(browser_url)
    assert DEFAULT_WEB_URL_IDENTITY_POLICY.canonical(input_url) == browser_url
    candidates = extract_navigation_candidates(
        f'<nav><a href="{browser_url}news">资讯</a><a href="/categories">分类</a></nav>',
        base_url=input_url,
        source_capture_id="sealed",
    )
    assert len(candidates) == 2
    assert all(candidate.risk_level == "low" for candidate in candidates)
    assert discovery_safety_reason(candidates[0], site_origin=input_url) is None
    assert resolve_web_page_classification_hint(
        site=context.site, url=browser_url + "news"
    ).eligibility.eligible


def test_nontransitional_idn_does_not_merge_different_browser_hosts():
    registry = WebSiteMenuRegistry()
    sharp_s = registry.resolve_site_context("https://faß.de/")
    double_s = registry.resolve_site_context("https://fass.de/")
    assert sharp_s.site.site_id != double_s.site.site_id
    assert not sharp_s.site.contains_url_origin("https://fass.de/news")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_url,browser_url",
    [
        ("https://例子.测试/", "https://xn--fsqu00a.xn--0zwm56d/"),
        ("https://faß.de/", "https://xn--fa-hia.de/"),
        ("https://[2001:0db8:0:0:0:0:0:1]:443/", "https://[2001:db8::1]/"),
    ],
)
async def test_actual_browser_normalized_capture_keeps_unknown_site_boundary(
    input_url, browser_url
):
    from playwright.async_api import async_playwright

    from services.api.app.application.web_desktop_navigation import (
        extract_navigation_candidates as extract_from_rendered_snapshot,
    )
    from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
    from services.api.tests.web_browser_test_support import (
        WebBrowserUnavailable,
        launch_web_test_browser,
    )

    async with async_playwright() as playwright:
        try:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
        except WebBrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = await browser.new_page()
            await page.route(
                "**/*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body='<nav><a href="/news">资讯</a><a href="/categories">分类</a></nav>',
                ),
            )
            await page.goto(input_url)
            assert page.url == browser_url
            context = WebSiteMenuRegistry().resolve_site_context(input_url)
            candidates = extract_from_rendered_snapshot(
                await page.content(), base_url=page.url, source_capture_id="browser-capture",
                snapshot=await _collect_ui_snapshot(page),
            )
            assert len(candidates) == 2
            assert all(
                discovery_safety_reason(candidate, site_origin=context.site.origin) is None
                for candidate in candidates
            )
        finally:
            await browser.close()


@pytest.mark.parametrize(
    "target_url,captured_url,allowed",
    [
        ("http://example.net/", "https://example.net/", True),
        ("http://example.net:80/", "https://EXAMPLE.net:443/", True),
        ("http://example.net:8080/", "https://example.net/", False),
        ("http://example.net/", "https://example.net:8443/", False),
        ("https://example.net/", "http://example.net/", False),
        ("http://example.net/", "https://outside.test/", False),
    ],
)
def test_upgrade_requires_captured_source_and_preserves_origin_boundaries(
    target_url, captured_url, allowed
):
    candidate = extract_navigation_candidates(
        '<nav><a href="/news">资讯</a><a href="/categories">分类</a></nav>',
        base_url=captured_url,
        source_capture_id="sealed",
    )[0]
    assert discovery_safety_reason(candidate, site_origin=target_url) is not None
    if allowed:
        from services.api.app.application.web_runtime_discovery import CanonicalSiteScope
        scope = CanonicalSiteScope.bind(target_url, (captured_url,))
        candidate = replace(candidate, frontier_site_origin=scope.canonical_site_origin)
        assert discovery_safety_reason(candidate, site_origin=target_url) is None
        assert (
            discovery_safety_reason(
                replace(candidate, source_page_url=None, frontier_site_origin=None),
                site_origin=target_url,
            )
            is not None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actual_source,allowed",
    [
        ("https://example.net/", True),
        ("https://example.net:443/", True),
        ("http://example.net/", False),
        ("https://example.net:8443/", False),
        ("https://outside.test/", False),
        (None, False),
    ],
)
async def test_transaction_authorization_rechecks_actual_capture_url(actual_source, allowed):
    from services.api.app.application.web_run_policy import load_published_web_policy
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.app.infrastructure.db.web_runtime_discovery_repository import (
        authorize_discovery_slot,
    )

    command = _command("http://example.net/")
    payload = load_published_web_policy(
        now=datetime.now(UTC),
        max_duration_seconds=300,
        exploration_target={
            "required_page_types": ["home", "content_list"],
            "discovery_policy": {
                "enabled": True,
                "max_unlisted_menus": 10,
                "same_origin_only": True,
            },
        },
    )
    snapshot = WebRunPolicySnapshot.create(
        task_id=command.task_id, agent_run_id=command.agent_run_id, payload=payload
    )
    candidate = extract_navigation_candidates(
        '<nav><a href="/news">资讯</a><a href="/categories">分类</a></nav>',
        base_url="https://example.net/",
        source_capture_id="sealed",
    )[0]
    entry = WebRuntimeMenuEntry.from_candidate(candidate)

    class Connection:
        def __init__(self):
            self.updates = []
            self.source_reads = []

        async def fetchrow(self, query, *args):
            if "SELECT task.finalization_epoch" in query:
                return {"finalization_epoch": 0}
            if "FROM qc_web_run_policy_snapshot" in query:
                return {
                    "task_id": command.task_id,
                    "agent_run_id": command.agent_run_id,
                    "payload": snapshot.to_payload(),
                    "payload_sha256": snapshot.payload_sha256,
                }
            if "SELECT budget.*" in query:
                return {"site_id": "site", "menu_limit": 10}
            if "SELECT * FROM qc_web_menu_ledger_entry" in query:
                return {
                    "menu_action_count": 0,
                    "discovery_source_capture_id": "sealed",
                    "discovery_manifest": {
                        "version": "web-discovery-manifest.v1",
                        "entry_id": entry.entry_id,
                        "candidate": candidate.to_dict(),
                    },
                }
            raise AssertionError(query)

        async def fetchval(self, query, *args):
            if "MAX(execution_slot)" in query:
                return 1
            self.source_reads.append((query, args))
            return actual_source

        async def fetch(self, query, *args):
            return []

        async def execute(self, query, *args):
            self.updates.append((query, args))

    connection = Connection()
    assert (
        await authorize_discovery_slot(
            connection,
            command=command,
            entry=entry,
            source_capture_id="sealed",
        )
        is False
    )
    assert connection.source_reads[0][1] == ("sealed", command.task_id, command.agent_run_id)
    assert connection.updates[0][1][-1] == "WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED"


@pytest.mark.asyncio
async def test_transaction_authorization_accepts_frozen_same_run_discovery_capture():
    """当前动作源变化时，原始发现证据仍须按同一 Web Run 的历史 Capture 校验。"""

    from services.api.app.application.web_run_policy import load_published_web_policy
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.app.infrastructure.db.web_runtime_discovery_repository import (
        authorize_discovery_slot,
    )

    command = _command("https://example.net/")
    payload = load_published_web_policy(
        now=datetime.now(UTC),
        max_duration_seconds=300,
        exploration_target={
            "required_page_types": ["home", "content_list"],
            "discovery_policy": {
                "enabled": True,
                "max_unlisted_menus": None,
                "same_origin_only": True,
            },
        },
    )
    snapshot = WebRunPolicySnapshot.create(
        task_id=command.task_id,
        agent_run_id=command.agent_run_id,
        payload=payload,
    )
    candidate = extract_navigation_candidates(
        '<nav><a href="/news">资讯</a><a href="/categories">分类</a></nav>',
        base_url="https://example.net/",
        source_capture_id="discovery-sealed",
    )[0]
    refreshed_candidate = replace(
        candidate,
        source_capture_id="current-sealed",
        discovery_capture_id="discovery-sealed",
    )
    entry = WebRuntimeMenuEntry.from_candidate(refreshed_candidate)

    class Connection:
        def __init__(self):
            self.updates = []
            self.scope_capture_ids = []

        async def fetchrow(self, query, *args):
            if "SELECT task.finalization_epoch" in query:
                self.scope_capture_ids.append(args[3])
                return {"finalization_epoch": 0}
            if "FROM qc_web_run_policy_snapshot" in query:
                return {
                    "task_id": command.task_id,
                    "agent_run_id": command.agent_run_id,
                    "payload": snapshot.to_payload(),
                    "payload_sha256": snapshot.payload_sha256,
                }
            if "SELECT budget.*" in query:
                return {"site_id": "site", "menu_limit": 10}
            if "SELECT * FROM qc_web_menu_ledger_entry" in query:
                return {
                    "menu_action_count": 0,
                    "discovery_source_capture_id": "discovery-sealed",
                    "discovery_manifest": {
                        "version": "web-discovery-manifest.v1",
                        "entry_id": entry.entry_id,
                        "candidate": candidate.to_dict(),
                    },
                }
            raise AssertionError(query)

        async def fetchval(self, query, *_args):
            if "MAX(execution_slot)" in query:
                return 1
            raise AssertionError(query)

        async def fetch(self, query, *_args):
            assert "execution_slot IS NOT NULL" in query
            return []

        async def execute(self, query, *args):
            self.updates.append((query, args))

    connection = Connection()
    allowed = await authorize_discovery_slot(
        connection,
        command=command,
        entry=entry,
        source_capture_id="current-sealed",
    )

    assert allowed is True
    assert connection.scope_capture_ids == ["current-sealed", "discovery-sealed"]
    assert "execution_slot=$4" in connection.updates[0][0]
