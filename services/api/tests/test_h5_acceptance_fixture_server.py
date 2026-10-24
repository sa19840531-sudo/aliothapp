"""H5 页面行为验收夹具服务测试。"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from scripts.serve_h5_acceptance_fixtures import (
    CONTROLLED_DRIVER_SCENARIOS,
    FIXTURE_SCENARIOS,
    MANIFEST_SCHEMA_VERSION,
    FixtureHandler,
    H5AcceptanceFixtureServer,
    render_fixture,
)
from scripts.verify_h5_acceptance_scenarios import REQUIRED_SCENARIOS
from services.api.app.infrastructure.browser.page_capture import _goto_with_retry


@contextmanager
def _fixture_server() -> Iterator[str]:
    server = H5AcceptanceFixtureServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_fixture_manifest_partitions_every_required_scenario() -> None:
    fixture_ids = set(FIXTURE_SCENARIOS)
    controlled_ids = set(CONTROLLED_DRIVER_SCENARIOS)

    assert fixture_ids.isdisjoint(controlled_ids)
    assert fixture_ids | controlled_ids == REQUIRED_SCENARIOS


@pytest.mark.parametrize(
    ("path", "expected_marker"),
    [
        ("/h5/home", "H5 产品首页"),
        ("/h5/line-loading", "正在选择最佳线路"),
        ("/h5/startup-ad", "fixtureShowStartupAd"),
        ("/h5/popup-chain", "fixtureShowPopups(3)"),
        ("/h5/tabbar", 'aria-label="底部导航"'),
        ("/h5/detail-back", 'data-fixture-view="list"'),
        ("/h5/long-page", 'data-segment="6"'),
        ("/h5/js-navigation", "history.pushState"),
        ("/h5/external-navigation", 'data-external="true"'),
        ("/h5/cancel", "持续探索目录"),
        ("/h5/recapture", "recapture-overlay"),
        ("/h5/capture-retry", "H5 产品首页"),
        ("/h5/unstable", "setInterval"),
    ],
)
def test_fixture_pages_expose_expected_behavior_contract(
    path: str,
    expected_marker: str,
) -> None:
    html = render_fixture(path, external_origin="http://localhost:9080")

    assert html is not None
    assert expected_marker in html
    assert '<meta name="viewport"' in html
    assert 'name="alioth-fixture-scenario"' in html


def test_detail_and_high_risk_fixtures_expose_stable_page_structure() -> None:
    detail = render_fixture(
        "/h5/detail-back",
        external_origin="http://localhost:9080",
    )
    high_risk = render_fixture(
        "/h5/safe-action",
        external_origin="http://localhost:9080",
    )

    assert detail is not None
    assert 'class="content-list"' in detail
    assert "app.className = 'content-detail'" in detail
    assert high_risk is not None
    assert 'class="profile-page"' in high_risk


def test_fixture_server_exposes_health_manifest_and_no_store_pages() -> None:
    with _fixture_server() as base_url:
        with urllib.request.urlopen(f"{base_url}/healthz") as response:
            health = json.load(response)
        with urllib.request.urlopen(f"{base_url}/_manifest") as response:
            manifest = json.load(response)
        with urllib.request.urlopen(f"{base_url}/h5/home") as response:
            body = response.read().decode("utf-8")
            cache_control = response.headers["Cache-Control"]

    assert health == {"schema_version": MANIFEST_SCHEMA_VERSION, "status": "ok"}
    assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert manifest["fixtures"]["W46-H5-001"]["url"] == f"{base_url}/h5/home"
    assert manifest["fixtures"]["W46-H5-016"]["driver"] == ("task_cancellation_driver")
    assert manifest["fixtures"]["W46-H5-017"]["driver"] == "targeted_recapture_driver"
    assert manifest["fixtures"]["W46-H5-101"]["driver"] == "page_behavior"
    assert "H5 产品首页" in body
    assert "data-page-type" not in body
    assert cache_control == "no-store"


def test_targeted_recapture_fixture_releases_close_control_per_run() -> None:
    with _fixture_server() as base_url:
        run_key = "batch-017"
        query = urllib.parse.urlencode({"fixture_run": run_key})
        with urllib.request.urlopen(f"{base_url}/h5/recapture?{query}") as response:
            fixture_html = response.read().decode("utf-8")
        with urllib.request.urlopen(
            f"{base_url}/_drivers/targeted-recapture/state?{query}"
        ) as response:
            initial = json.load(response)

        request = urllib.request.Request(
            f"{base_url}/_drivers/targeted-recapture/release?{query}",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            released = json.load(response)
        with urllib.request.urlopen(
            f"{base_url}/_drivers/targeted-recapture/state?{query}"
        ) as response:
            current = json.load(response)

    assert initial == {
        "driver": "targeted_recapture_driver",
        "fixture_run": run_key,
        "released": False,
    }
    assert released == {**initial, "released": True}
    assert current == released
    assert 'class="detail-entry"' in fixture_html
    assert 'id="show-recapture-overlay"' in fixture_html
    assert '<div class="overlay" id="recapture-overlay"' not in fixture_html
    assert "document.body.appendChild(overlay)" in fixture_html


def test_fixture_server_returns_404_for_unknown_path() -> None:
    with _fixture_server() as base_url:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/h5/unknown")

    assert exc_info.value.code == 404


def test_capture_retry_fixture_drops_first_request_then_succeeds() -> None:
    with _fixture_server() as base_url:
        url = f"{base_url}/h5/capture-retry?fixture_run=batch-1"
        with pytest.raises(http.client.RemoteDisconnected):
            urllib.request.urlopen(url)
        with urllib.request.urlopen(url) as response:
            body = response.read().decode("utf-8")

    assert "H5 产品首页" in body


async def test_capture_retry_fixture_exercises_real_browser_retry() -> None:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    with _fixture_server() as base_url:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            audit: dict[str, object] = {}
            try:
                response = await _goto_with_retry(
                    page=page,
                    url=f"{base_url}/h5/capture-retry?fixture_run=browser-batch",
                    timeout_ms=10_000,
                    timeout_error_type=PlaywrightTimeoutError,
                    audit=audit,
                )
            finally:
                await browser.close()

    assert response is not None
    assert response.status == 200
    assert audit["attempt_count"] == 2
    assert audit["retry_count"] == 1
    assert audit["retry_reasons"] in (["err_empty_response"], ["browser_error_document"])
    assert audit["outcome"] == "succeeded_after_retry"
