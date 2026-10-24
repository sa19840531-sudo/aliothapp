"""菜单逻辑身份、跨 Capture 父链及不限层级回归。"""

from dataclasses import replace

import pytest

from services.api.app.application.web_runtime_discovery import (
    WebRuntimeMenuEntry,
    discovery_entry_id,
)
from services.api.tests.test_web_desktop_navigation import (
    _runtime_navigation_candidates as extract_navigation_candidates,
)
from services.api.tests.test_web_navigation_recognition import _runtime_navigation_snapshot


def _candidate(*, label="分类", href=False, **kwargs):
    control = f'<a href="/news">{label}</a>' if href else f"<button>{label}</button>"
    return extract_navigation_candidates(
        f'<nav id="main">{control}<a href="/other">其他</a></nav>',
        base_url="https://example.test/",
        **kwargs,
    )[0]


def test_capture_epoch_and_temporary_entry_ids_do_not_change_logical_identity():
    first = _candidate(source_capture_id="capture-1", source_epoch=1)
    second = replace(
        first,
        entry_id="temporary-other",
        dom_fingerprint="new-dom-order",
        source_capture_id="capture-2",
        discovery_capture_id="capture-2",
        source_epoch=2,
        menu_position=8,
    )
    assert discovery_entry_id(first) == discovery_entry_id(second)
    assert first.logical_target_identity == second.logical_target_identity


@pytest.mark.parametrize("href", [False, True])
def test_parent_logical_identity_separates_same_target_on_different_branches(href):
    left = _candidate(label="左侧")
    right = _candidate(label="右侧")
    one = _candidate(href=href, parent_candidate=left, parent_menu_entry_id="left")
    two = _candidate(href=href, parent_candidate=right, parent_menu_entry_id="right")
    assert discovery_entry_id(one) != discovery_entry_id(two)
    assert one.exploration_path == ("左侧", "分类")
    assert one.parent_menu_entry_id == "left"
    assert one.menu_depth == 2


def test_twenty_five_cross_capture_levels_preserve_full_lineage():
    parent = None
    for depth in range(1, 26):
        candidate = _candidate(
            label=f"层级{depth}",
            source_capture_id=f"capture-{depth}",
            parent_candidate=parent,
            parent_menu_entry_id=discovery_entry_id(parent) if parent else None,
        )
        entry = WebRuntimeMenuEntry.from_candidate(candidate)
        assert entry.menu_depth == depth
        assert entry.exploration_path == tuple(f"层级{i}" for i in range(1, depth + 1))
        assert entry.logical_target_identity == candidate.logical_target_identity
        parent = candidate


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_invalid_depth_is_rejected(invalid):
    with pytest.raises(ValueError, match="WEB_MENU_LINEAGE_INVALID"):
        replace(_candidate(), menu_depth=invalid)


def test_path_must_end_with_current_label():
    with pytest.raises(ValueError, match="WEB_MENU_LINEAGE_INVALID"):
        replace(_candidate(), exploration_path=("错误标签",))


def _capture(browser, *, capture_id, controls):
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureArtifact,
        PageCaptureMode,
        PageCaptureRequest,
        PageCaptureResult,
    )

    result = browser._apply_result_policy(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id="web:lineage",
            viewport_width=1920,
            viewport_height=1080,
            web_capture_stage="web_page_probe",
        ),
        PageCaptureResult(
            execution_status="success",
            mode="observe",
            reachable=True,
            final_url="https://example.test/",
            title="test",
            status_code=200,
            artifacts=[
                PageCaptureArtifact("dom", None, controls),
                PageCaptureArtifact("ui_snapshot", None, _runtime_navigation_snapshot(
                    controls, base_url="https://example.test/",
                )),
            ],
        ),
    )
    browser.bind_latest_navigation_capture("web:lineage", capture_id)
    return result


def test_capture_parent_applies_only_to_new_controls_and_repeated_capture_refreshes_source():
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)
    html = '<nav id="main"><button>分类</button><a href="/other">其他</a></nav>'
    _capture(browser, capture_id="one", controls=html)
    parent = next(c for c in browser.navigation_candidates("web:lineage") if c.label == "分类")
    browser.set_navigation_parent(
        "web:lineage", parent, parent_menu_entry_id=discovery_entry_id(parent)
    )
    expanded = html.replace("</nav>", "<button>子分类</button></nav>")
    _capture(browser, capture_id="two", controls=expanded)
    result = _capture(browser, capture_id="three", controls=expanded)
    candidates = browser.navigation_candidates("web:lineage")
    assert len(candidates) == 3
    root = next(c for c in candidates if c.label == "分类")
    child = next(c for c in candidates if c.label == "子分类")
    assert root.logical_target_identity == parent.logical_target_identity
    assert root.menu_depth == 1
    assert child.menu_depth == 2
    assert child.parent_menu_entry_id == discovery_entry_id(parent)
    assert child.exploration_path == ("分类", "子分类")
    assert child.discovery_capture_id == "two"
    assert child.source_capture_id == "three"
    snapshot = next(a.content for a in result.artifacts if a.artifact_type == "ui_snapshot")
    sealed = next(c for c in snapshot["web_navigation"]["candidates"] if c["label"] == "子分类")
    assert sealed["menu_depth"] == 2
    assert sealed["logical_target_identity"] == child.logical_target_identity


def test_capture_policy_keeps_same_named_children_in_their_current_parent_branch():
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)
    html = '<nav id="main"><button>Left</button><button>Right</button></nav>'
    _capture(browser, capture_id="root", controls=html)
    roots = {item.label: item for item in browser.navigation_candidates("web:lineage")}
    children = html.replace("</nav>", "<button>Child</button></nav>")
    for label in ("Left", "Right"):
        parent = roots[label]
        browser.set_navigation_parent(
            "web:lineage", parent, parent_menu_entry_id=discovery_entry_id(parent)
        )
        _capture(browser, capture_id=label.lower(), controls=children)
    collected = [
        item for item in browser.navigation_candidates("web:lineage") if item.label == "Child"
    ]
    assert len(collected) == 2
    assert {item.exploration_path for item in collected} == {("Left", "Child"), ("Right", "Child")}
    assert {item.source_capture_id for item in collected} == {"left", "right"}
    assert len({item.logical_target_identity for item in collected}) == 2
    assert all(item.menu_depth == 2 for item in collected)


def test_original_controls_reappear_after_loading_without_self_parent_or_new_identity():
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)
    html = '<nav id="main"><button>Left</button><button>Right</button></nav>'
    _capture(browser, capture_id="root", controls=html)
    roots = browser.navigation_candidates("web:lineage")
    parent = next(item for item in roots if item.label == "Left")
    browser.set_navigation_parent(
        "web:lineage", parent, parent_menu_entry_id=discovery_entry_id(parent)
    )
    _capture(browser, capture_id="loading", controls="<main>Loading</main>")
    _capture(browser, capture_id="restored", controls=html)
    candidates = browser.navigation_candidates("web:lineage")
    assert len(candidates) == 2
    assert {item.logical_target_identity for item in candidates} == {
        item.logical_target_identity for item in roots
    }
    assert all(item.menu_depth == 1 and item.parent_menu_entry_id is None for item in candidates)
    assert all(item.source_capture_id == "restored" for item in candidates)


def test_unique_url_root_keeps_lineage_when_navigation_container_changes_across_pages():
    """全局根导航的语义身份唯一时，页面模板变化不能把它错误挂成子菜单。"""
    from services.api.app.application.web_navigation_lineage import (
        reconcile_navigation_lineages,
    )

    roots = extract_navigation_candidates(
        '<nav id="primary"><a href="/news">资讯</a><a href="/other">其他</a></nav>',
        base_url="https://example.test/",
        source_capture_id="root",
    )
    parent = extract_navigation_candidates(
        '<nav id="primary"><a href="/section">分区</a><a href="/other">其他</a></nav>',
        base_url="https://example.test/",
        source_capture_id="parent",
    )[0]
    drifted = extract_navigation_candidates(
        '<header><nav class="global-links"><a href="/news">资讯</a>'
        '<a href="/other">其他</a></nav></header>',
        base_url="https://example.test/section",
        source_capture_id="child-page",
        parent_candidate=parent,
        parent_menu_entry_id=discovery_entry_id(parent),
    )

    assert roots[0].navigation_container_id != drifted[0].navigation_container_id
    assert drifted[0].menu_depth == 2

    reconciled = next(
        item
        for item in reconcile_navigation_lineages(
            drifted,
            history=roots,
            parent_candidate=parent,
        )
        if item.label == "资讯"
    )

    assert reconciled.logical_target_identity == roots[0].logical_target_identity
    assert reconciled.parent_menu_entry_id is None
    assert reconciled.parent_logical_target_identity is None
    assert reconciled.menu_depth == 1
    assert reconciled.exploration_path == ("资讯",)
    assert reconciled.actionability == "actionable"


def test_container_drift_does_not_guess_between_duplicate_semantic_roots():
    """同 URL、标签和路径存在多个历史根时必须保持歧义阻断。"""
    from services.api.app.application.web_navigation_lineage import (
        reconcile_navigation_lineages,
    )

    first = extract_navigation_candidates(
        '<nav id="one"><a href="/news">资讯</a><a href="/other">其他</a></nav>',
        base_url="https://example.test/",
    )[0]
    second = extract_navigation_candidates(
        '<nav id="two"><a href="/news">资讯</a><a href="/other">其他</a></nav>',
        base_url="https://example.test/",
    )[0]
    parent = _candidate(label="分区", href=True)
    candidate = extract_navigation_candidates(
        '<nav id="three"><a href="/news">资讯</a><a href="/other">其他</a></nav>',
        base_url="https://example.test/section",
        parent_candidate=parent,
        parent_menu_entry_id=discovery_entry_id(parent),
    )

    reconciled = next(
        item
        for item in reconcile_navigation_lineages(
            candidate,
            history=(first, second),
            parent_candidate=parent,
        )
        if item.label == "资讯"
    )

    assert reconciled.actionability == "blocked"
    assert reconciled.identity_review_reason == "WEB_MENU_LINEAGE_AMBIGUOUS"


def test_partial_root_navigation_reappearance_is_review_only_when_global_identity_is_unproven():
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)
    _capture(
        browser,
        capture_id="root",
        controls=('<nav id="main"><button>Left</button><button>Right</button></nav>'),
    )
    roots = {item.label: item for item in browser.navigation_candidates("web:lineage")}
    browser.set_navigation_parent(
        "web:lineage", roots["Left"], parent_menu_entry_id=discovery_entry_id(roots["Left"])
    )
    _capture(browser, capture_id="loading", controls="<main>Loading</main>")
    result = _capture(
        browser,
        capture_id="partial",
        controls=('<nav id="main"><button>Right</button><button>New</button></nav>'),
    )
    snapshot = next(a.content for a in result.artifacts if a.artifact_type == "ui_snapshot")
    right = next(c for c in snapshot["web_navigation"]["candidates"] if c["label"] == "Right")
    assert right["actionability"] == "blocked"
    assert right["identity_review_reason"] == "WEB_MENU_LINEAGE_UNPROVEN"
    assert right["logical_target_identity"] == roots["Right"].logical_target_identity


@pytest.mark.parametrize("prefix", ["", '<nav><a href="/x">X</a><a href="/y">Y</a></nav>'])
def test_anonymous_navigation_identity_is_stable_when_unrelated_navigation_is_inserted(prefix):
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)
    target = '<nav><button>分类</button><a href="/other">其他</a></nav>'
    _capture(browser, capture_id="one", controls=target)
    first = next(c for c in browser.navigation_candidates("web:lineage") if c.label == "分类")
    result = _capture(browser, capture_id="two", controls=prefix + target)
    snapshot = next(a.content for a in result.artifacts if a.artifact_type == "ui_snapshot")
    latest = next(c for c in snapshot["web_navigation"]["candidates"] if c["label"] == "分类")
    assert latest["navigation_container_id"] == first.navigation_container_id
    assert latest["logical_target_identity"] == first.logical_target_identity
    if prefix:
        other = next(c for c in snapshot["web_navigation"]["candidates"] if c["label"] == "X")
        assert other["navigation_container_id"] != first.navigation_container_id
    assert len([c for c in browser.navigation_candidates("web:lineage") if c.label == "分类"]) == 1


def test_duplicate_anonymous_navigation_containers_are_audited_but_not_executable():
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)
    target = '<nav><button>分类</button><a href="/other">其他</a></nav>'
    result = _capture(browser, capture_id="ambiguous", controls=target + target)
    snapshot = next(a.content for a in result.artifacts if a.artifact_type == "ui_snapshot")
    candidates = snapshot["web_navigation"]["candidates"]
    assert len(candidates) == 4
    assert all(c["actionability"] == "blocked" for c in candidates)
    assert all(
        c["identity_review_reason"] == "WEB_NAVIGATION_CONTAINER_IDENTITY_AMBIGUOUS"
        for c in candidates
    )


@pytest.mark.parametrize(
    "state_class",
    [
        "",
        "is-open",
        "menu--expanded",
        "nav-opened",
        "has-active-child",
        "is_active",
        "menu__expanded",
        "nav-active",
    ],
)
def test_href_less_parent_expansion_does_not_prove_clickable_submenu_child(state_class):
    from services.api.app.application.web_submenu_expansion import proven_submenu_children
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)
    before_html = (
        '<nav class="main-nav"><button aria-expanded="false">Parent</button>'
        '<a href="/other">Other</a></nav>'
    )
    before = _capture(browser, capture_id="collapsed", controls=before_html)
    parent = next(c for c in browser.navigation_candidates("web:lineage") if c.label == "Parent")
    browser.set_navigation_parent(
        "web:lineage", parent, parent_menu_entry_id=discovery_entry_id(parent)
    )
    after_html = before_html.replace('expanded="false"', 'expanded="true"').replace(
        "</button>", '</button><button aria-expanded="false">Child</button>', 1
    )
    after_html = after_html.replace('class="main-nav"', f'class="main-nav {state_class}"')
    after = _capture(browser, capture_id="expanded", controls=after_html)
    candidates = browser.navigation_candidates("web:lineage")
    parents = [c for c in candidates if c.label == "Parent"]
    child = next(c for c in candidates if c.label == "Child")
    assert len(parents) == 1
    assert parents[0].menu_depth == 1
    assert parents[0].logical_target_identity == parent.logical_target_identity
    assert child.navigation_container_id == parent.navigation_container_id
    assert child.menu_depth == 2
    assert child.parent_menu_entry_id == discovery_entry_id(parent)
    assert child.exploration_path == ("Parent", "Child")
    before_snapshot = next(a.content for a in before.artifacts if a.artifact_type == "ui_snapshot")
    after_snapshot = next(a.content for a in after.artifacts if a.artifact_type == "ui_snapshot")
    assert proven_submenu_children(
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        candidate=parent,
        entry_id=discovery_entry_id(parent),
        site_origin="https://example.test/",
    ) == ()
    _capture(browser, capture_id="collapsed-again", controls=before_html)
    latest_parent = next(
        c for c in browser.navigation_candidates("web:lineage") if c.label == "Parent"
    )
    assert latest_parent.logical_target_identity == parent.logical_target_identity
    assert latest_parent.navigation_container_id == parent.navigation_container_id
    assert latest_parent.menu_depth == 1


@pytest.mark.asyncio
async def test_new_explorer_restores_evolving_anonymous_container_before_first_capture() -> None:
    from datetime import UTC, datetime, timedelta
    from hashlib import sha256
    from types import SimpleNamespace

    from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
    from services.api.app.application.web_menu_exploration import (
        WebMenuExplorationSummary,
        WebMenuExplorer,
        WebMenuLedgerEntry,
        WebRuntimeDiscoveredLedgerEntry,
        _ledger,
        restore_persisted_navigation_state,
    )
    from services.api.app.domain.exploration import WebMenuTerminalStatus
    from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy
    from services.api.app.domain.page import WebSiteMenuRegistry
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    collapsed = (
        '<nav class="main-nav"><button aria-expanded="false">Parent</button>'
        '<a href="/other">Other</a></nav>'
    )
    expanded = collapsed.replace('expanded="false"', 'expanded="true"').replace(
        "</button>", '</button><button aria-expanded="false">Child</button>', 1
    )
    first = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)
    _capture(first, capture_id="before-restart", controls=collapsed)
    parent = next(c for c in first.navigation_candidates("web:lineage") if c.label == "Parent")
    collapsed_manifests = {
        candidate.label: candidate.to_dict()
        for candidate in first.navigation_candidates("web:lineage")
    }
    first.set_navigation_parent(
        "web:lineage", parent, parent_menu_entry_id=discovery_entry_id(parent)
    )
    _capture(first, capture_id="persisted-expanded", controls=expanded)
    persisted = first.navigation_candidates("web:lineage")
    child = next(c for c in persisted if c.label == "Child")

    restarted = WebDesktopCapturePolicyBrowser(
        browser=object(),
        include_blocked_navigation=True,
    )
    parent_entry_id = discovery_entry_id(parent)
    parent_execution = WebMenuLedgerEntry(
        entry_id=parent_entry_id,
        menu_path=parent.menu_path,
        canonical_url="sha256:" + "a" * 64,
        status=WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
        source_capture_id="before-restart",
        final_capture_id="persisted-expanded",
        page_type=None,
        detail_capture_id=None,
        error_code=None,
        menu_action_count=1,
        menu_intent_id="intent-parent",
        menu_action_execution_id="action-parent",
        menu_probe_capture_id="persisted-expanded",
        menu_ready_capture_id="persisted-expanded",
        menu_page_type=None,
        detail_page_type=None,
        diagnostic_type="WEB_SUBMENU_EXPANDED",
        menu_interaction_outcome={
            "command_accepted": True,
            "effect_observed": True,
            "postcondition_satisfied": True,
            "transition_arrived": True,
            "after_capture_id": "persisted-expanded",
            "failure_reason": None,
            "evidence_mode": "natural",
        },
        parent_menu_entry_id=parent.parent_menu_entry_id,
        parent_logical_target_identity=parent.parent_logical_target_identity,
        menu_depth=parent.menu_depth,
        exploration_path=parent.exploration_path,
        logical_target_identity=parent.logical_target_identity,
        submenu_relations_verified=True,
    )
    rows = tuple(
        WebRuntimeDiscoveredLedgerEntry(
            entry_id=discovery_entry_id(candidate),
            menu_path=candidate.menu_path,
            canonical_url=None,
            menu_position=candidate.menu_position,
            source_capture_id="persisted-expanded",
            status=(
                WebMenuTerminalStatus.MENU_PAGE_CAPTURED
                if candidate.label == "Parent"
                else WebMenuTerminalStatus.RUNTIME_DISCOVERED_UNREVIEWED
            ),
            error_code=(
                None if candidate.label == "Parent" else "WEB_DISCOVERY_PENDING"
            ),
            execution=(parent_execution if candidate.label == "Parent" else None),
            parent_menu_entry_id=candidate.parent_menu_entry_id,
            parent_logical_target_identity=candidate.parent_logical_target_identity,
            menu_depth=candidate.menu_depth,
            exploration_path=candidate.exploration_path,
            logical_target_identity=candidate.logical_target_identity,
            candidate_manifest=(
                collapsed_manifests.get(candidate.label, candidate.to_dict())
            ),
        )
        for candidate in persisted
    )

    registry = WebSiteMenuRegistry()
    context = registry.resolve_site_context("https://example.test/")
    home_rows = tuple(
        replace(
            _ledger(
                entry,
                status=WebMenuTerminalStatus.HOME_BASELINE,
                source_capture_id="persisted-expanded",
                final_capture_id="persisted-expanded",
                page_type="home",
            ),
            canonical_url="sha256:" + sha256(entry.canonical_url.encode()).hexdigest(),
        )
        for entry in context.site.entries
    )
    summary = WebMenuExplorationSummary(
        site_id=context.site.site_id,
        catalog_version=context.site.catalog_version,
        catalog_sha256=context.content_sha256,
        menu_total=len(context.site.entries),
        menu_ledger=home_rows,
        runtime_discovered_ledger=rows,
        page_timeline=("persisted-expanded",),
        page_type_keys=("home",),
    )

    class CaptureOnFirstRead:
        def __init__(self) -> None:
            self.capture_started = False
            self.restore_before_capture = False

        def restore_navigation_state(self, session_id, **kwargs):
            self.restore_before_capture = not self.capture_started
            return restarted.restore_navigation_state(session_id, **kwargs)

        def reject_navigation_restore(self, session_id, *, reason):
            restarted.reject_navigation_restore(session_id, reason=reason)

        def navigation_candidates(self, session_id):
            if not self.capture_started:
                self.capture_started = True
                _capture(restarted, capture_id="after-restart", controls=expanded)
            return restarted.navigation_candidates(session_id)

        def set_navigation_parent(self, session_id, candidate, *, parent_menu_entry_id):
            restarted.set_navigation_parent(
                session_id,
                candidate,
                parent_menu_entry_id=parent_menu_entry_id,
            )

    class Ledger:
        def __init__(self) -> None:
            self.recorded_entry_ids = set()
            self.claim_count = 0

        async def read_summary(self, **_kwargs):
            return summary

        async def record_discovery(self, *, candidate, **_kwargs):
            self.recorded_entry_ids.add(discovery_entry_id(candidate))

        async def claim_menu(self, **_kwargs):
            self.claim_count += 1
            raise AssertionError("冷恢复不应为既有身份新增预算")

    class Visitor:
        async def visit(self, **_kwargs):
            raise AssertionError("closeout 冷恢复不应执行新动作")

    source = CaptureOnFirstRead()
    ledger = Ledger()
    await WebMenuExplorer(
        navigation_context=registry,
        candidate_source=source,
        page_visitor=Visitor(),
        budget_ledger=ledger,
        discovery_policy=WebDiscoveryPolicy(enabled=True, max_unlisted_menus=10),
    ).explore(
        RunWebWorkflowCommand(
            task_id="task-lineage",
            agent_run_id="run-lineage",
            session_id="web:lineage",
            session_owner="web-v2-workflow",
            target_url="https://example.test/",
            browser_profile_id="desktop_fhd_1920x1080_v1",
            deadline=datetime.now(UTC) + timedelta(seconds=10),
        ),
        initial_capture_id="persisted-expanded",
        initial_page_type="home",
    )
    restored = {item.label: item for item in restarted.navigation_candidates("web:lineage")}

    assert source.restore_before_capture is True
    assert ledger.claim_count == 0
    assert ledger.recorded_entry_ids == {row.entry_id for row in rows}
    assert len(restored) == len({item.label for item in persisted})
    assert {item.logical_target_identity for item in restored.values()} == {
        item.logical_target_identity for item in persisted
    }
    assert restored["Parent"].logical_target_identity == parent.logical_target_identity
    assert restored["Parent"].menu_depth == 1
    assert restored["Child"].logical_target_identity == child.logical_target_identity
    assert restored["Child"].parent_menu_entry_id == parent_entry_id
    assert restored["Child"].parent_logical_target_identity == parent.logical_target_identity
    assert restored["Child"].exploration_path == ("Parent", "Child")

    unavailable = WebDesktopCapturePolicyBrowser(
        browser=object(),
        include_blocked_navigation=True,
    )
    rows_without_seed = tuple(
        replace(
            row,
            candidate_manifest={
                **row.candidate_manifest,
                "navigation_container_restore_seed": None,
            },
        )
        for row in rows
    )
    restore_persisted_navigation_state(
        unavailable,
        SimpleNamespace(runtime_discovered_ledger=rows_without_seed),
        session_id="web:lineage",
    )
    unavailable_result = _capture(
        unavailable,
        capture_id="unavailable-restart",
        controls=expanded,
    )
    unavailable_snapshot = next(
        artifact.content
        for artifact in unavailable_result.artifacts
        if artifact.artifact_type == "ui_snapshot"
    )
    assert all(
        candidate["identity_review_reason"] == "WEB_MENU_LINEAGE_UNAVAILABLE"
        and candidate["actionability"] == "blocked"
        for candidate in unavailable_snapshot["web_navigation"]["candidates"]
    )


def test_anonymous_container_restore_rejects_duplicate_persisted_identity_seed() -> None:
    from services.api.app.application.web_navigation_container_identity import (
        WebNavigationContainerRegistry,
    )

    seed = {
        "version": "web-nav-container-seed.v1",
        "identity": "web-nav-container:" + "a" * 24,
        "kind": "anonymous",
        "semantic_key": '{"id": "", "tag": "nav"}',
        "observations": [["parent", "", "button", []]],
    }
    registry = WebNavigationContainerRegistry()
    assert registry.restore(seed) is None
    assert registry.restore(
        {**seed, "identity": "web-nav-container:" + "b" * 24}
    ) == "WEB_MENU_LINEAGE_AMBIGUOUS"


@pytest.mark.parametrize(
    "order",
    [
        ("a-small", "a-big", "b-small"),
        ("a-small", "b-small", "a-big"),
    ],
)
def test_container_restore_conflict_is_permanent_and_seed_order_independent(order) -> None:
    from services.api.app.application.web_navigation_container_identity import (
        WebNavigationContainerRegistry,
    )

    semantic_key = '{"id": "", "tag": "nav"}'
    small = [["parent", "", "button", []], ["other", "/other", "a", []]]
    big = [*small, ["child", "", "button", []]]
    seeds = {
        "a-small": {
            "version": "web-nav-container-seed.v1",
            "identity": "web-nav-container:" + "a" * 24,
            "kind": "anonymous",
            "semantic_key": semantic_key,
            "observations": small,
        },
        "a-big": {
            "version": "web-nav-container-seed.v1",
            "identity": "web-nav-container:" + "a" * 24,
            "kind": "anonymous",
            "semantic_key": semantic_key,
            "observations": big,
        },
        "b-small": {
            "version": "web-nav-container-seed.v1",
            "identity": "web-nav-container:" + "b" * 24,
            "kind": "anonymous",
            "semantic_key": semantic_key,
            "observations": small,
        },
    }
    registry = WebNavigationContainerRegistry()
    results = {name: registry.restore(seeds[name]) for name in order}

    assert results["b-small"] == "WEB_MENU_LINEAGE_AMBIGUOUS"
    descriptors = {"temporary-dom-handle": {"id": "", "tag": "nav"}}
    collapsed_elements = [
        {
            "navigation_container_id": "temporary-dom-handle",
            "visible": True,
            "text": label,
            "href": href,
            "tag": tag,
            "navigation_menu_path": [],
        }
        for label, href, tag, _path in small
    ]
    expanded_elements = [
        *collapsed_elements,
        {
            "navigation_container_id": "temporary-dom-handle",
            "visible": True,
            "text": "child",
            "href": "",
            "tag": "button",
            "navigation_menu_path": [],
        },
    ]
    collapsed, collapsed_ambiguous = registry.resolve(descriptors, collapsed_elements)
    expanded, expanded_ambiguous = registry.resolve(descriptors, expanded_elements)

    expected_identity = "web-nav-container:" + "a" * 24
    assert collapsed == {"temporary-dom-handle": expected_identity}
    assert expanded == {"temporary-dom-handle": expected_identity}
    assert collapsed_ambiguous == {expected_identity}
    assert expanded_ambiguous == {expected_identity}


@pytest.mark.parametrize(
    "structural_class",
    ["open-source-nav", "active-users-nav", "hashtag-nav", "isotope-nav", "nav-expanded-content"],
)
def test_structural_classes_are_not_removed_as_transient_states(structural_class):
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)
    entries = '<button>Parent</button><a href="/other">Other</a>'
    result = _capture(
        browser,
        capture_id="structural",
        controls=f'<nav class="{structural_class}">{entries}</nav><nav>{entries}</nav>',
    )
    snapshot = next(a.content for a in result.artifacts if a.artifact_type == "ui_snapshot")
    candidates = snapshot["web_navigation"]["candidates"]
    assert len({c["navigation_container_id"] for c in candidates}) == 2
    assert all(c["actionability"] != "blocked" for c in candidates)


def test_state_classes_do_not_disambiguate_multiple_historical_anonymous_containers():
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)

    def container(extra_class, child):
        return (
            f'<nav class="main-nav {extra_class}"><button>Parent</button>'
            f'<a href="/{child}">{child}</a></nav>'
        )

    _capture(browser, capture_id="before", controls=container("", "Left") + container("", "Right"))
    result = _capture(
        browser,
        capture_id="after",
        controls=container("is-open", "A") + container("menu--expanded", "B"),
    )
    snapshot = next(a.content for a in result.artifacts if a.artifact_type == "ui_snapshot")
    candidates = snapshot["web_navigation"]["candidates"]
    assert len(candidates) == 4
    assert all(c["actionability"] == "blocked" for c in candidates)
    assert all(
        c["identity_review_reason"] == "WEB_NAVIGATION_CONTAINER_IDENTITY_AMBIGUOUS"
        for c in candidates
    )


def test_two_anonymous_containers_match_independently_when_one_expands_and_order_changes():
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)
    target = '<nav><button>Parent</button><a href="/other">Other</a></nav>'
    sibling = '<nav><a href="/x">X</a><a href="/y">Y</a></nav>'
    _capture(browser, capture_id="before", controls=target + sibling)
    roots = {c.label: c for c in browser.navigation_candidates("web:lineage")}
    browser.set_navigation_parent(
        "web:lineage", roots["Parent"], parent_menu_entry_id=discovery_entry_id(roots["Parent"])
    )
    expanded = target.replace("</nav>", "<button>Child</button></nav>")
    result = _capture(browser, capture_id="after", controls=sibling + expanded)
    snapshot = next(a.content for a in result.artifacts if a.artifact_type == "ui_snapshot")
    latest = {c["label"]: c for c in snapshot["web_navigation"]["candidates"]}
    assert latest["Parent"]["logical_target_identity"] == roots["Parent"].logical_target_identity
    assert latest["X"]["logical_target_identity"] == roots["X"].logical_target_identity
    assert latest["Parent"]["navigation_container_id"] != latest["X"]["navigation_container_id"]
    assert latest["Child"]["navigation_container_id"] == roots["Parent"].navigation_container_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expansion_evidence", ["proven", "no_state_change", "no_new_child", "failed_receipt"]
)
@pytest.mark.parametrize("container_attribute", [' id="main"', "", ' class="main-nav"'])
async def test_href_less_frontier_is_audit_only_without_clicking_parent_chain(
    expansion_evidence,
    container_attribute,
):
    from types import SimpleNamespace

    from services.api.app.application.web_desktop_exploration import WebDesktopMenuPageVisitor
    from services.api.app.application.web_exploration_budget_ledger import WebMenuActionReservation
    from services.api.app.application.web_menu_exploration import (
        WebMenuExplorationSummary,
        WebMenuExplorer,
        _ledger,
    )
    from services.api.app.application.web_runtime_composition import WebOrchestratedMenuPageVisitor
    from services.api.app.domain.exploration import WebMenuTerminalStatus
    from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy
    from services.api.app.domain.page import WebSiteMenuRegistry
    from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
        WebDesktopCapturePolicyBrowser,
    )
    from services.api.tests.test_web_menu_exploration import _command

    browser = WebDesktopCapturePolicyBrowser(browser=object(), include_blocked_navigation=True)

    def controls(depth):
        count = 1 if expansion_evidence == "no_new_child" else depth
        state_changes = expansion_evidence != "no_state_change"
        buttons = "".join(
            f'<button aria-expanded="{str(i < depth and state_changes).lower()}">层级{i}</button>'
            for i in range(1, count + 1)
        )
        attributes = container_attribute
        if depth > 1 and 'class="main-nav"' in attributes:
            attributes = attributes.replace('class="main-nav"', 'class="main-nav is-open"')
        return f'<nav{attributes}>{buttons}<a href="/">首页</a></nav>'

    snapshots = {}

    def capture(depth):
        result = _capture(browser, capture_id=f"capture-{depth}", controls=controls(depth))
        snapshots[f"capture-{depth}"] = next(
            a.content for a in result.artifacts if a.artifact_type == "ui_snapshot"
        )

    capture(1)
    registry = WebSiteMenuRegistry()
    context = registry.resolve_site_context("https://example.test/")
    from hashlib import sha256

    home = context.site.entries[0]
    home_row = replace(
        _ledger(
            home,
            status=WebMenuTerminalStatus.HOME_BASELINE,
            source_capture_id="capture-1",
            page_type="home",
        ),
        canonical_url="sha256:" + sha256(home.canonical_url.encode()).hexdigest(),
    )
    summary = WebMenuExplorationSummary(
        site_id=context.site.site_id,
        catalog_version=context.site.catalog_version,
        catalog_sha256=context.content_sha256,
        menu_ledger=(home_row,),
        menu_limit=10,
        page_timeline=("capture-1",),
        page_type_keys=("home",),
    )
    visited, reasons = [], {}

    class Ledger:
        async def read_summary(self, **kwargs):
            return summary

        async def record_discovery(self, **kwargs):
            reasons[kwargs["candidate"].label] = kwargs["error_code"]

        async def claim_menu(self, **kwargs):
            if len(visited) == 10:
                return None
            return WebMenuActionReservation(
                site_id=context.site.site_id,
                menu_entry_id=kwargs["entry"].entry_id,
                execution_slot=len(visited) + 1,
                intent_id=f"intent-{len(visited) + 1}",
                already_reserved=False,
            )

        async def finalize(self, record):
            pass

    class Coordinator:
        async def coordinate(self, request):
            raw = request.intent.parameters["web_discovery_candidate"]
            visited.append(
                next(
                    c
                    for c in browser.navigation_candidates("web:lineage")
                    if c.entry_id == raw["entry_id"]
                )
            )
            depth = len(visited) + 1
            if depth <= 12:
                capture(depth)
            receipt = "failed" if expansion_evidence == "failed_receipt" else "succeeded"
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id=f"capture-{depth}"),
                interaction=SimpleNamespace(
                    execution_outcome=receipt, status=receipt, action_execution_id=f"action-{depth}"
                ),
            )

    class Reader:
        async def read_url(self, *, capture_id):
            return "https://example.test/"

        async def read_snapshot(self, *, capture_id):
            return snapshots[capture_id]

    visitor = WebOrchestratedMenuPageVisitor(
        navigator=WebDesktopMenuPageVisitor(coordinator=Coordinator(), capture_url_reader=Reader()),
        readiness=object(),
        ready_capture_reader=object(),
        classifier=object(),
        evidence_source=object(),
        detail_navigator=object(),
        navigation_candidate_source=browser,
    )

    await WebMenuExplorer(
        navigation_context=registry,
        candidate_source=browser,
        page_visitor=visitor,
        budget_ledger=Ledger(),
        discovery_policy=WebDiscoveryPolicy(enabled=True, max_unlisted_menus=10),
    ).explore(
        replace(_command("https://example.test/"), session_id="web:lineage"),
        initial_capture_id="capture-1",
        initial_page_type="home",
    )
    assert visited == []
    assert reasons["层级1"] == "WEB_DISCOVERY_DIRECT_URL_REQUIRED"
