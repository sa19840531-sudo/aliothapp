"""Web 动态发现只消费 canonical scope 内的当前可信 Capture。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
)
from services.api.app.application.web_frontier_scheduler import TrustedWebFrontierCandidateSource
from services.api.app.application.web_runtime_discovery import (
    CanonicalSiteScope,
    TrustedWebCaptureAuthority,
    UntrustedWebCaptureError,
    WebCapture,
)
from services.api.app.domain.exploration.web_frontier_policy import WebFrontierPolicy
from services.api.app.infrastructure.db.web_runtime_discovery_repository import (
    frozen_frontier_rejection,
    record_discovery,
)


def _scope() -> CanonicalSiteScope:
    return CanonicalSiteScope.bind(
        "http://site.example/",
        ("http://site.example/", "https://www.site.example/home"),
    )


def _capture(**changes: object) -> WebCapture:
    values: dict[str, object] = {
        "capture_id": "capture-1",
        "session_id": "web:run-1",
        "finalization_epoch": 4,
        "requested_url": "https://www.site.example/category/",
        "final_url": "https://www.site.example/category/",
        "redirect_chain": (),
        "current": True,
        "ready": True,
        "capture_state_fingerprint": "state-1",
        "depth": 1,
        "anchors": (),
    }
    values.update(changes)
    return WebCapture(**values)


def _authority() -> TrustedWebCaptureAuthority:
    return TrustedWebCaptureAuthority(
        site_scope=_scope(),
        session_id="web:run-1",
        finalization_epoch=4,
    )


def test_top_menu_priority_uses_observation_order_not_duplicate_occurrence():
    observed = tuple(
        WebNavigationCandidate(
            entry_id=str(index), label='分类', url=url, menu_path=('分类',),
            dom_fingerprint=str(index), source_signal='href', page_type_key='content_list',
            menu_position=1, visibility='hidden' if index == 0 else 'visible',
        )
        for index, url in enumerate((
            'https://www.site.example/category/news/',
            'https://www.site.example/123.html',
        ))
    )
    source = object.__new__(TrustedWebFrontierCandidateSource)
    source._active_capture = capture = _capture()
    source._active_page_role = 'home'
    source._originals_by_url = {}
    source._delegate = SimpleNamespace(navigation_candidates=lambda _session: observed)
    anchors = source._read_anchors(capture)
    assert anchors[0].priority > anchors[1].priority
    assert [item.href for item in anchors] == [item.url for item in observed]


@pytest.mark.asyncio
async def test_trusted_source_authorizes_secondary_menu_hrefs_from_current_menu_capture():
    root = _capture(
        capture_id="capture-root",
        requested_url="https://www.site.example/",
        final_url="https://www.site.example/",
        capture_state_fingerprint="state-root",
        depth=0,
    )
    menu = _capture(capture_id="capture-menu", capture_state_fingerprint="state-menu")

    class Metadata:
        async def read(self, *, capture_id, depth):
            return root if capture_id == "capture-root" else menu

    class Ready:
        async def read(self, *, capture_id, page_role):
            self.roles.append(page_role)
            return SimpleNamespace(capture_id=capture_id, page_role=page_role)

        roles = []

    class Delegate:
        def bind_latest_navigation_capture(self, session_id, capture_id):
            pass

        def authorized_navigation_candidates(self, session_id, *, capture_id, page_role):
            self.roles.append(page_role)
            if capture_id == "capture-root":
                return ()
            return (
                WebNavigationCandidate(
                    entry_id="secondary",
                    label="二级入口",
                    url="https://www.site.example/category/secondary/",
                    menu_path=("二级入口",),
                    dom_fingerprint="secondary-dom",
                    source_signal="href",
                    page_type_key="content_list",
                    source_capture_id=capture_id,
                    source_page_url=menu.final_url,
                ),
            )

        roles = []

    ready = Ready()
    delegate = Delegate()
    source = TrustedWebFrontierCandidateSource(
        delegate=delegate,
        capture_metadata_reader=Metadata(),
        ready_capture_reader=ready,
        frozen_policy=SimpleNamespace(
            frontier_policy=WebFrontierPolicy.for_new_run(), menu_limit=10
        ),
        target_url="https://www.site.example/",
        session_id="web:run-1",
    )
    source.bind_latest_navigation_capture("web:run-1", "capture-root")
    assert await source.navigation_candidates("web:run-1") == ()
    source.bind_latest_navigation_capture("web:run-1", "capture-menu")
    source.bind_navigation_page_role("web:run-1", "capture-menu", "menu")
    candidates = await source.navigation_candidates("web:run-1")
    assert any(item.url.rstrip("/").endswith("/category/secondary") for item in candidates)
    assert ready.roles == ["home", "menu"]
    assert delegate.roles == ["home", "menu"]


def test_root_binding_accepts_only_http_upgrade_and_apex_www_equivalence() -> None:
    scope = _scope()

    assert scope.canonical_site_origin == "https://www.site.example"


@pytest.mark.parametrize(
    "redirect_chain",
    [
        ("https://site.example/", "http://site.example/"),
        ("https://site.example/", "https://cdn.site.example/"),
        ("https://site.example/", "https://unrelated.example/"),
    ],
)
def test_root_binding_rejects_downgrade_subdomain_and_unrelated_origin(
    redirect_chain: tuple[str, ...],
) -> None:
    with pytest.raises(
        UntrustedWebCaptureError,
        match="WEB_CANONICAL_SITE_SCOPE_INVALID",
    ):
        CanonicalSiteScope.bind("https://site.example/", redirect_chain)


def test_cross_origin_arrival_dom_is_not_discoverable() -> None:
    capture = _capture(
        requested_url="https://www.site.example/github.html",
        final_url="https://gitlab.example/project",
    )

    with pytest.raises(
        UntrustedWebCaptureError,
        match="WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED",
    ):
        _authority().assert_discoverable(capture)


def test_http_upgrade_is_rejected_after_canonical_scope_is_frozen() -> None:
    with pytest.raises(
        UntrustedWebCaptureError,
        match="WEB_DISCOVERY_REQUEST_ORIGIN_UNTRUSTED",
    ):
        _authority().assert_discoverable(
            _capture(
                requested_url="http://www.site.example/category/",
                final_url="https://www.site.example/category/",
                redirect_chain=(
                    "http://www.site.example/category/",
                    "https://www.site.example/category/",
                ),
            )
        )


def test_root_redirect_proof_cannot_be_replayed_by_later_capture() -> None:
    authority = TrustedWebCaptureAuthority(
        site_scope=_scope(),
        session_id="web:run-1",
        finalization_epoch=4,
        root_capture_id="capture-root",
    )
    root_capture = _capture(
        capture_id="capture-root",
        requested_url="http://site.example/",
        final_url="https://www.site.example/home",
        redirect_chain=(
            "http://site.example/",
            "https://www.site.example/home",
        ),
        depth=0,
    )

    authority.assert_discoverable(root_capture)
    with pytest.raises(
        UntrustedWebCaptureError,
        match="WEB_DISCOVERY_REQUEST_ORIGIN_UNTRUSTED",
    ):
        authority.assert_discoverable(
            replace(
                root_capture,
                capture_id="capture-later",
                capture_state_fingerprint="state-later",
                depth=1,
            )
        )


@pytest.mark.parametrize(
    ("changes", "error_code"),
    [
        ({"session_id": "web:run-2"}, "WEB_DISCOVERY_SOURCE_SESSION_UNTRUSTED"),
        ({"finalization_epoch": 3}, "WEB_DISCOVERY_SOURCE_EPOCH_UNTRUSTED"),
        ({"current": False}, "WEB_DISCOVERY_SOURCE_NOT_CURRENT"),
        ({"current": 1}, "WEB_DISCOVERY_SOURCE_NOT_CURRENT"),
        ({"ready": False}, "WEB_DISCOVERY_SOURCE_NOT_READY"),
        ({"ready": 1}, "WEB_DISCOVERY_SOURCE_NOT_READY"),
    ],
)
def test_capture_authority_rejects_each_stale_or_unready_state(
    changes: dict[str, object],
    error_code: str,
) -> None:
    with pytest.raises(UntrustedWebCaptureError, match=error_code):
        _authority().assert_discoverable(_capture(**changes))


@pytest.mark.asyncio
async def test_cross_origin_capture_is_rejected_before_anchor_reader_runs() -> None:
    from services.api.app.application.web_frontier_scheduler import WebFrontierScheduler

    def forbidden_reader(_capture: WebCapture):
        raise AssertionError("跨域落地不得读取 DOM anchors")

    scheduler = WebFrontierScheduler(
        policy=WebFrontierPolicy.for_new_run(),
        capture_authority=_authority(),
        menu_limit=10,
        anchor_reader=forbidden_reader,
    )
    capture = replace(_capture(), final_url="https://unrelated.example/")

    with pytest.raises(
        UntrustedWebCaptureError,
        match="WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED",
    ):
        await scheduler.discover_once(capture)


@pytest.mark.asyncio
async def test_repository_rejects_cross_origin_dom_lineage_before_database_write() -> None:
    class ForbiddenConnection:
        async def fetchrow(self, *_args: object, **_kwargs: object):
            raise AssertionError("不可信来源不得进入发现账本 SQL")

    candidate = WebNavigationCandidate(
        entry_id="cross-origin-source",
        label="返回站内",
        url="https://www.site.example/category",
        menu_path=("返回站内",),
        dom_fingerprint="cross-source",
        source_signal="semantic_nav",
        page_type_key="unknown",
        source_page_url="https://landing.example/redirected",
    )

    with pytest.raises(
        WebExplorationBudgetLedgerConflictError,
        match="WEB_DISCOVERY_SOURCE_ORIGIN_UNTRUSTED",
    ):
        await record_discovery(
            ForbiddenConnection(),
            command=SimpleNamespace(
                task_id="task",
                agent_run_id="run",
                session_id="web:run",
            ),
            site=SimpleNamespace(site_id="site", origin="https://www.site.example/", entries=()),
            candidate=candidate,
            source_capture_id="capture",
            error_code="WEB_DISCOVERY_PENDING",
        )


def _ledger_row(
    *,
    url: str,
    structure: str,
    status: str = "menu_page_captured",
    execution_slot: int = 1,
) -> dict[str, object]:
    return {
        "status": status,
        "execution_slot": execution_slot,
        "discovery_manifest": {
            "candidate": {
                "url": url,
                "frontier_structure_cluster_key": structure,
            }
        },
    }


@pytest.mark.parametrize(
    ("candidate", "rows", "policy", "expected"),
    [
        (
            SimpleNamespace(
                menu_depth=5,
                url="https://site.example/deep",
                dom_fingerprint="deep",
                frontier_structure_cluster_key="deep",
            ),
            (),
            WebFrontierPolicy(max_depth=4),
            "WEB_FRONTIER_DEPTH_EXHAUSTED",
        ),
        (
            SimpleNamespace(
                menu_depth=1,
                url="https://site.example/article/4",
                dom_fingerprint="article",
                frontier_structure_cluster_key="article",
            ),
            tuple(
                _ledger_row(
                    url=f"https://site.example/article/{index}",
                    structure="article",
                    execution_slot=index,
                )
                for index in range(1, 4)
            ),
            WebFrontierPolicy(max_samples_per_structure=3),
            "WEB_FRONTIER_STRUCTURE_BUDGET_EXHAUSTED",
        ),
        (
            SimpleNamespace(
                menu_depth=1,
                url="https://site.example/search?q=d",
                dom_fingerprint="query-d",
                frontier_structure_cluster_key="query-d",
            ),
            tuple(
                _ledger_row(
                    url=f"https://site.example/search?q={value}",
                    structure=f"query-{value}",
                    execution_slot=index,
                )
                for index, value in enumerate("abc", start=1)
            ),
            WebFrontierPolicy(max_query_variants_per_path=3),
            "WEB_FRONTIER_QUERY_BUDGET_EXHAUSTED",
        ),
        (
            SimpleNamespace(
                menu_depth=1,
                url="https://site.example/next",
                dom_fingerprint="next",
                frontier_structure_cluster_key="next",
            ),
            (
                _ledger_row(
                    url="https://site.example/inflight",
                    structure="inflight",
                    status="menu_reserved",
                ),
            ),
            WebFrontierPolicy(max_concurrency_per_origin=1),
            "WEB_FRONTIER_ORIGIN_CONCURRENCY_BUSY",
        ),
        (
            SimpleNamespace(
                menu_depth=1,
                url="https://site.example/page-3",
                dom_fingerprint="page-3",
                frontier_structure_cluster_key="page-3",
            ),
            (
                _ledger_row(url="https://site.example/page-1", structure="one"),
                _ledger_row(
                    url="https://site.example/page-2",
                    structure="two",
                    execution_slot=2,
                ),
            ),
            WebFrontierPolicy(max_pages=2),
            "WEB_FRONTIER_PAGE_BUDGET_EXHAUSTED",
        ),
    ],
)
def test_repository_rechecks_each_frozen_frontier_execution_constraint(
    candidate: SimpleNamespace,
    rows: tuple[dict[str, object], ...],
    policy: WebFrontierPolicy,
    expected: str,
) -> None:
    assert (
        frozen_frontier_rejection(
            policy=policy,
            candidate=candidate,
            ledger_rows=rows,
            site_origin="https://site.example/",
        )
        == expected
    )


def test_repository_structure_budget_does_not_fall_back_to_dom_fingerprint() -> None:
    candidate = SimpleNamespace(
        menu_depth=1,
        url="https://site.example/article/2",
        dom_fingerprint="same-dom-order-sensitive-value",
        frontier_structure_cluster_key="route-b|detail|article",
    )
    row = _ledger_row(
        url="https://site.example/article/1",
        structure="route-a|detail|article",
    )
    row["discovery_manifest"]["candidate"]["dom_fingerprint"] = (
        "same-dom-order-sensitive-value"
    )

    assert (
        frozen_frontier_rejection(
            policy=WebFrontierPolicy(max_samples_per_structure=1),
            candidate=candidate,
            ledger_rows=(row,),
            site_origin="https://site.example/",
        )
        is None
    )


def test_frontier_structure_identity_is_persisted_in_existing_candidate_manifest() -> None:
    candidate = WebNavigationCandidate(
        entry_id="frontier-news",
        label="资讯",
        url="https://site.example/news",
        menu_path=("资讯",),
        dom_fingerprint="order-sensitive-dom",
        source_signal="semantic_nav",
        page_type_key="unknown",
        frontier_route_shape="/news",
        frontier_dom_role="menu",
        frontier_structure_fingerprint="nav-anchor",
        frontier_structure_cluster_key="web-frontier-structure.v1:abc",
    )

    payload = candidate.to_dict()

    assert payload["frontier_route_shape"] == "/news"
    assert payload["frontier_dom_role"] == "menu"
    assert payload["frontier_structure_fingerprint"] == "nav-anchor"
    assert payload["frontier_structure_cluster_key"] == "web-frontier-structure.v1:abc"
