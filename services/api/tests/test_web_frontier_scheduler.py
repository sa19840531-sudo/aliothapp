"""服务端冻结策略驱动的通用 Web frontier 调度。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from services.api.app.application.web_frontier_scheduler import (
    RuntimeAnchor,
    WebFrontierScheduler,
)
from services.api.app.application.web_runtime_discovery import (
    CanonicalSiteScope,
    TrustedWebCaptureAuthority,
    WebCapture,
)
from services.api.app.domain.exploration.web_frontier_policy import WebFrontierPolicy

FRONTIER_POLICY = WebFrontierPolicy.for_new_run()


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def anchor(
    href: str,
    *,
    dom_role: str = "menu",
    structure_fingerprint: str = "navigation",
    priority: int = 0,
) -> RuntimeAnchor:
    return RuntimeAnchor(
        href=href,
        dom_role=dom_role,
        structure_fingerprint=structure_fingerprint,
        priority=priority,
    )


def capture(
    *,
    url: str = "https://site.example/",
    capture_id: str = "capture-home",
    fingerprint: str = "state-home",
    depth: int = 0,
    anchors: tuple[RuntimeAnchor, ...] = (),
) -> WebCapture:
    return WebCapture(
        capture_id=capture_id,
        session_id="web:run-1",
        finalization_epoch=2,
        requested_url=url,
        final_url=url,
        redirect_chain=(),
        current=True,
        ready=True,
        capture_state_fingerprint=fingerprint,
        depth=depth,
        anchors=anchors,
    )


def scheduler(
    *,
    policy: WebFrontierPolicy = FRONTIER_POLICY,
    menu_limit: int = 10,
    clock: Clock | None = None,
) -> WebFrontierScheduler:
    site_scope = CanonicalSiteScope.bind("https://site.example/", ())
    return WebFrontierScheduler(
        policy=policy,
        capture_authority=TrustedWebCaptureAuthority(
            site_scope=site_scope,
            session_id="web:run-1",
            finalization_epoch=2,
        ),
        menu_limit=menu_limit,
        clock=clock,
        started_at=0.0 if clock is not None else None,
    )


@pytest.mark.parametrize(
    "href",
    [
        "",
        "#comments",
        "javascript:void(0)",
        "data:text/html,x",
        "https://site.example:bad/path",
    ],
)
def test_non_navigable_anchor_is_aggregated_without_frontier_row(href: str) -> None:
    result = scheduler().ingest(capture(), [anchor(href)])

    assert result.inserted_candidates == ()
    assert result.rejected_count == 1


@pytest.mark.parametrize(
    "href",
    ["/account/login", "/billing/checkout", "/files/upload", "/archive.zip"],
)
def test_high_risk_or_download_anchor_is_rejected_without_frontier_row(href: str) -> None:
    result = scheduler().ingest(capture(), [anchor(href)])

    assert result.inserted_candidates == ()
    assert result.rejected_count == 1


def test_tracking_fragment_and_trailing_slash_variants_collapse_to_one_candidate() -> None:
    result = scheduler().ingest(
        capture(),
        [
            anchor("/post/1/?utm_source=a#top"),
            anchor("https://site.example/post/1?utm_source=b"),
            anchor("/post/1"),
        ],
    )

    assert [item.canonical_url for item in result.inserted_candidates] == [
        "https://site.example/post/1"
    ]
    assert result.alias_count == 2


def test_candidate_keeps_generic_route_shape_and_source_lineage() -> None:
    frontier = scheduler()
    first = frontier.ingest(
        capture(capture_id="capture-a", fingerprint="state-a"),
        [anchor("/post/123", dom_role="detail", structure_fingerprint="article")],
    )
    frontier.ingest(
        capture(capture_id="capture-b", fingerprint="state-b"),
        [anchor("/post/123", dom_role="detail", structure_fingerprint="article")],
    )

    assert first.inserted_candidates[0].route_shape == "/post/:id"
    assert frontier.source_lineage_for("https://site.example/post/123") == (
        "capture-a",
        "capture-b",
    )


def test_query_variants_per_path_are_bounded_by_frozen_policy() -> None:
    policy = replace(FRONTIER_POLICY, max_query_variants_per_path=3)
    result = scheduler(policy=policy).ingest(
        capture(),
        [anchor(f"/search?q={value}", structure_fingerprint=f"query-{value}") for value in "abcd"],
    )

    assert [item.canonical_url for item in result.actionable_candidates] == [
        "https://site.example/search?q=a",
        "https://site.example/search?q=b",
        "https://site.example/search?q=c",
    ]
    assert [item.canonical_url for item in result.budget_not_executed] == [
        "https://site.example/search?q=d"
    ]


def test_structure_cluster_sampling_limits_duplicate_templates() -> None:
    result = scheduler().ingest(
        capture(),
        [
            anchor(
                f"/post/{index}",
                dom_role="detail",
                structure_fingerprint="article-detail",
            )
            for index in range(16)
        ],
    )

    assert len(result.actionable_candidates) == FRONTIER_POLICY.max_samples_per_structure
    assert len(result.budget_not_executed) == (16 - FRONTIER_POLICY.max_samples_per_structure)


def test_structure_budget_uses_route_role_and_structure_composite_key() -> None:
    frontier = scheduler(policy=replace(FRONTIER_POLICY, max_samples_per_structure=1))

    result = frontier.ingest(
        capture(),
        [
            anchor("/post/1", dom_role="detail", structure_fingerprint="shared"),
            anchor("/category", dom_role="menu", structure_fingerprint="shared"),
        ],
    )

    assert len(result.actionable_candidates) == 2
    assert (
        result.actionable_candidates[0].structure_cluster_key
        != result.actionable_candidates[1].structure_cluster_key
    )


def test_candidate_beyond_frozen_depth_is_budget_not_executed() -> None:
    result = scheduler().ingest(
        capture(depth=FRONTIER_POLICY.max_depth),
        [anchor("/too-deep", structure_fingerprint="deep")],
    )

    assert result.actionable_candidates == ()
    assert [item.canonical_url for item in result.budget_not_executed] == [
        "https://site.example/too-deep"
    ]


def test_scheduler_factory_consumes_frozen_run_policy_values() -> None:
    frozen = type(
        "FrozenPolicy",
        (),
        {
            "frontier_policy": replace(FRONTIER_POLICY, max_depth=1),
            "menu_limit": 2,
        },
    )()
    authority = TrustedWebCaptureAuthority(
        site_scope=CanonicalSiteScope.bind("https://site.example/", ()),
        session_id="web:run-1",
        finalization_epoch=2,
    )
    frontier = WebFrontierScheduler.from_frozen_policy(
        frozen,
        capture_authority=authority,
    )

    result = frontier.ingest(
        capture(depth=1),
        [anchor("/depth-two", structure_fingerprint="depth-two")],
    )

    assert result.actionable_candidates == ()
    assert len(result.budget_not_executed) == 1


def test_elapsed_duration_stops_new_candidates_without_relabeling_them_rejected() -> None:
    clock = Clock(11.0)
    policy = replace(FRONTIER_POLICY, max_duration_seconds=10.0)
    result = scheduler(policy=policy, clock=clock).ingest(
        capture(),
        [anchor("/late", structure_fingerprint="late")],
    )

    assert result.rejected_count == 0
    assert len(result.budget_not_executed) == 1


@pytest.mark.asyncio
async def test_each_trusted_page_discovers_once_and_adds_nested_menu() -> None:
    frontier = scheduler()
    await frontier.discover_once(
        capture(anchors=(anchor("/category/", structure_fingerprint="category"),))
    )
    await frontier.mark_visited("https://site.example/category/")
    nested = await frontier.discover_once(
        capture(
            url="https://site.example/category/",
            capture_id="capture-category",
            fingerprint="state-category",
            depth=1,
            anchors=(
                anchor("sub/", structure_fingerprint="subcategory"),
                anchor("/category/", structure_fingerprint="category"),
            ),
        )
    )

    assert "https://site.example/category/sub" in nested.frontier_urls
    assert nested.logical_candidates_for("https://site.example/category/") == 1


@pytest.mark.asyncio
async def test_redirect_alias_merges_candidate_and_releases_original_inflight() -> None:
    frontier = scheduler()
    frontier.ingest(
        capture(),
        [
            anchor("/old", structure_fingerprint="old", priority=10),
            anchor("/second", structure_fingerprint="second"),
        ],
    )
    reserved = await frontier.claim_next()
    assert reserved is not None and reserved.canonical_url.endswith("/old")

    redirected = replace(
        capture(
            url="https://site.example/new",
            capture_id="capture-new",
            fingerprint="state-new",
            depth=1,
            anchors=(anchor("/new", structure_fingerprint="self"),),
        ),
        requested_url="https://site.example/old",
        redirect_chain=("https://site.example/old", "https://site.example/new"),
    )
    discovered = await frontier.discover_once(redirected)
    await frontier.mark_visited("https://site.example/new")

    assert discovered.logical_candidates_for("https://site.example/old") == 1
    assert discovered.logical_candidates_for("https://site.example/new") == 1
    assert frontier.source_lineage_for("https://site.example/new") == (
        "capture-home",
        "capture-new",
    )
    assert (await frontier.claim_next()).canonical_url.endswith("/second")


@pytest.mark.asyncio
async def test_redirect_alias_merges_duplicate_reservations_and_landing_lineage() -> None:
    policy = replace(FRONTIER_POLICY, max_concurrency_per_origin=2)
    frontier = scheduler(policy=policy)
    frontier.ingest(
        capture(),
        [
            anchor("/old", structure_fingerprint="old", priority=40),
            anchor("/new", structure_fingerprint="new", priority=30),
            anchor("/third", structure_fingerprint="third", priority=20),
            anchor("/fourth", structure_fingerprint="fourth", priority=10),
        ],
    )
    old = await frontier.claim_next()
    new = await frontier.claim_next()
    assert old is not None and old.canonical_url.endswith("/old")
    assert new is not None and new.canonical_url.endswith("/new")

    landing = replace(
        capture(
            url="https://site.example/new",
            capture_id="capture-landing",
            fingerprint="state-landing",
            depth=1,
            anchors=(),
        ),
        requested_url="https://site.example/old",
        redirect_chain=("https://site.example/old", "https://site.example/new"),
    )
    await frontier.discover_once(landing)
    await frontier.mark_visited(old.canonical_url)
    await frontier.mark_visited(new.canonical_url)

    assert frontier.source_lineage_for("https://site.example/new") == (
        "capture-home",
        "capture-landing",
    )
    assert frontier.execution_summary.executed_page_count == 1
    assert frontier.execution_summary.executed_unique_menu_count == 1
    assert await frontier.claim_next() is not None
    assert await frontier.claim_next() is not None


@pytest.mark.asyncio
async def test_source_page_self_link_is_visited_baseline_without_menu_budget() -> None:
    frontier = scheduler(menu_limit=1)

    result = await frontier.discover_once(
        capture(anchors=(anchor("/", structure_fingerprint="home"),))
    )

    assert result.inserted_candidates == ()
    assert result.rejected_count == 0
    assert result.alias_count == 1
    assert await frontier.claim_next() is None
    assert frontier.execution_summary.executed_unique_menu_count == 0


@pytest.mark.asyncio
async def test_same_capture_state_is_discovered_idempotently() -> None:
    calls = 0

    def read_anchors(source_capture: WebCapture):
        nonlocal calls
        calls += 1
        return source_capture.anchors

    frontier = scheduler()
    frontier.anchor_reader = read_anchors
    source = capture(anchors=(anchor("/category", structure_fingerprint="category"),))

    first = await frontier.discover_once(source)
    repeated = await frontier.discover_once(source)

    assert repeated == first
    assert calls == 1


@pytest.mark.asyncio
async def test_frozen_page_limit_stops_claims_and_records_budget_terminal() -> None:
    policy = replace(FRONTIER_POLICY, max_pages=2)
    frontier = scheduler(policy=policy)
    frontier.ingest(
        capture(),
        [anchor(f"/menu-{index}", structure_fingerprint=f"menu-{index}") for index in range(3)],
    )

    first = await frontier.claim_next()
    assert first is not None
    await frontier.mark_visited(first.canonical_url)
    second = await frontier.claim_next()
    assert second is not None
    await frontier.mark_visited(second.canonical_url)

    assert await frontier.claim_next() is None
    assert frontier.execution_summary.page_budget_skipped_count == 1


@pytest.mark.asyncio
async def test_frozen_concurrency_allows_one_inflight_candidate_per_origin() -> None:
    policy = replace(FRONTIER_POLICY, max_concurrency_per_origin=1)
    frontier = scheduler(policy=policy)
    frontier.ingest(
        capture(),
        [
            anchor("/first", structure_fingerprint="first", priority=2),
            anchor("/second", structure_fingerprint="second", priority=1),
        ],
    )

    first = await frontier.claim_next()

    assert first is not None
    assert await frontier.claim_next() is None
    await frontier.mark_visited(first.canonical_url)
    assert await frontier.claim_next() is not None


@pytest.mark.asyncio
async def test_frozen_menu_limit_counts_only_unique_executed_menu_pages() -> None:
    frontier = scheduler(menu_limit=10)
    menu_candidates = [
        anchor(f"/menu-{index}", structure_fingerprint=f"menu-{index}", priority=10)
        for index in range(12)
    ]
    duplicate_global_nav = menu_candidates[:5]
    detail_candidates = [
        anchor(
            f"/detail-{index}",
            dom_role="detail",
            structure_fingerprint=f"detail-{index}",
        )
        for index in range(6)
    ]
    frontier.ingest(
        capture(),
        [*menu_candidates, *duplicate_global_nav, *detail_candidates],
    )

    while candidate := await frontier.claim_next():
        await frontier.mark_visited(candidate.canonical_url)

    result = frontier.execution_summary
    assert result.executed_unique_menu_count == 10
    assert result.menu_limit_skipped_count == 2
    assert result.detail_candidate_count == 6


@pytest.mark.asyncio
async def test_restored_menu_beyond_limit_does_not_increment_executed_pages() -> None:
    frontier = scheduler(menu_limit=1)
    frontier.ingest(
        capture(),
        [
            anchor("/first", structure_fingerprint="first"),
            anchor("/overflow", structure_fingerprint="overflow"),
        ],
    )

    await frontier.mark_visited("https://site.example/first")
    await frontier.mark_visited("https://site.example/overflow")

    assert frontier.execution_summary.executed_page_count == 1
    assert frontier.execution_summary.menu_limit_skipped_count == 1
