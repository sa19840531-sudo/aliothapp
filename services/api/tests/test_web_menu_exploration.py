"""Web 单站有界菜单 Frontier 与完整终态账本合同。"""

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
    WebMenuActionReservation,
)
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.application.web_menu_exploration import (
    WebMenuExplorationSummary,
    WebMenuExplorer,
    WebMenuLedgerEntry,
    WebMenuVisitResult,
    _terminal_record,
    hydrate_web_menu_summary,
    plan_web_menu_collection,
    reconcile_site_menu,
)
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy
from services.api.app.domain.page import (
    WebMenuDecision,
    WebSiteMenuCatalog,
    WebSiteMenuContext,
    WebSiteMenuEntry,
    WebSiteMenuRegistry,
)


def test_terminal_record_preserves_independent_interaction_facts():
    from services.api.app.application.web_interaction_precision import WebInteractionOutcome

    site = WebSiteMenuRegistry().resolve_site("http://91porna.com/")
    outcome = WebInteractionOutcome(
        command_accepted=True,
        effect_observed=True,
        postcondition_satisfied=False,
        transition_arrived=False,
        after_capture_id="after",
        failure_reason="postcondition_not_satisfied",
    )
    record = _terminal_record(
        command=_command(),
        site_id=site.site_id,
        entry=site.entries[0],
        result=WebMenuVisitResult(
            final_capture_id="after",
            page_type=None,
            menu_interaction_outcome=outcome,
            status=WebMenuTerminalStatus.NAVIGATION_FAILED,
        ),
    )
    assert record.diagnostic["menu_interaction_outcome"]["command_accepted"] is True
    assert record.diagnostic["menu_interaction_outcome"]["effect_observed"] is True
    assert record.diagnostic["menu_interaction_outcome"]["transition_arrived"] is False


def _command(target_url: str = "http://91porna.com/") -> RunWebWorkflowCommand:
    return RunWebWorkflowCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        session_owner="web-v2-workflow",
        target_url=target_url,
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=30),
    )


def _runtime_candidates(site) -> tuple[WebNavigationCandidate, ...]:
    return tuple(
        WebNavigationCandidate(
            entry_id=f"runtime:{index}",
            label=entry.menu_path[-1],
            url=entry.canonical_url,
            menu_path=entry.menu_path,
            dom_fingerprint=f"dom:{index}",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime",
        )
        for index, entry in enumerate(site.entries)
        if entry.decision is not WebMenuDecision.BLOCKED_CROSS_ORIGIN
    )


class CandidateSource:
    def __init__(self, candidates) -> None:
        self.candidates = candidates

    def navigation_candidates(self, session_id):
        assert session_id == "web:run-1"
        return self.candidates


class Visitor:
    PAGE_TYPES = (
        "video_list",
        "video_detail",
        "content_list",
        "content_detail",
    )

    def __init__(self) -> None:
        self.entries = []
        self.source_capture_ids = {}

    async def visit(self, *, command, entry, candidate, source_capture_id):
        del command, candidate
        self.entries.append(entry.entry_id)
        self.source_capture_ids[entry.entry_id] = source_capture_id
        page_type = self.PAGE_TYPES[(len(self.entries) - 1) % len(self.PAGE_TYPES)]
        return WebMenuVisitResult(
            final_capture_id=f"capture:{entry.entry_id}",
            page_type=page_type,
            menu_probe_capture_id=f"probe:{entry.entry_id}",
            menu_ready_capture_id=f"capture:{entry.entry_id}",
            menu_page_type=page_type,
            menu_readiness_evidence_capture_ids=(
                f"probe:{entry.entry_id}",
                f"capture:{entry.entry_id}",
            ),
        )


def _site_with_unique_menus(menu_count: int) -> WebSiteMenuCatalog:
    origin = "https://menus.example.test"
    entries = (
        WebSiteMenuEntry(
            entry_id="home",
            menu_path=("首页",),
            href="/",
            canonical_url=f"{origin}/",
            decision=WebMenuDecision.HOME_BASELINE,
            is_same_origin=True,
        ),
        *(
            WebSiteMenuEntry(
                entry_id=f"menu-{index}",
                menu_path=("菜单", str(index)),
                href=f"/menu/{index}",
                canonical_url=f"{origin}/menu/{index}",
                decision=WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY,
                is_same_origin=True,
            )
            for index in range(menu_count)
        ),
    )
    return WebSiteMenuCatalog(
        site_id="unique-menus",
        origin=origin,
        navigation_evidence={"active_anchor_count": len(entries)},
        entries=entries,
    )


async def _explore_site_with_visible_menu_count(menu_count: int) -> WebMenuExplorationSummary:
    site = _site_with_unique_menus(menu_count)

    class StaticRegistry:
        version = "unique-menus-v1"
        catalog_content_sha256 = "a" * 64

        def resolve_site_context(self, target_url: str) -> WebSiteMenuContext:
            assert target_url == f"{site.origin}/"
            return WebSiteMenuContext(
                site=site,
                source_kind="catalog",
                content_sha256=self.catalog_content_sha256,
            )

    return await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=StaticRegistry(),
        candidate_source=CandidateSource(_runtime_candidates(site)),
        page_visitor=Visitor(),
    ).explore(
        _command(f"{site.origin}/"),
        initial_capture_id="capture-home",
        initial_page_type="home",
    )


@pytest.mark.asyncio
async def test_catalog_navigation_seals_runtime_parent_with_catalog_entry_identity() -> None:
    """目录菜单访问后的新 DOM 子项必须指向同作用域的真实 Catalog 父行。"""

    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")

    class ParentRecordingSource(CandidateSource):
        def __init__(self, candidates) -> None:
            super().__init__(candidates)
            self.parents: list[tuple[WebNavigationCandidate, str]] = []

        def set_navigation_parent(
            self,
            session_id: str,
            candidate: WebNavigationCandidate,
            *,
            parent_menu_entry_id: str,
        ) -> None:
            assert session_id == "web:run-1"
            self.parents.append((candidate, parent_menu_entry_id))

    candidates = list(_runtime_candidates(site))
    first_catalog_menu = next(
        entry
        for entry in site.entries
        if entry.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
        and entry.collection_eligibility.eligible
    )
    matched_index = next(
        index
        for index, candidate in enumerate(candidates)
        if candidate.url == first_catalog_menu.canonical_url
    )
    candidates[matched_index] = replace(
        candidates[matched_index],
        label="动态导航标签",
        menu_path=("动态导航标签",),
        exploration_path=("动态导航标签",),
    )
    source = ParentRecordingSource(tuple(candidates))
    await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=source,
        page_visitor=Visitor(),
    ).explore(
        _command("https://hl365.com/"),
        initial_capture_id="capture-home",
        initial_page_type="home",
    )

    assert source.parents
    entries_by_id = {entry.entry_id: entry for entry in site.entries}
    assert all(
        parent_entry_id in entries_by_id
        and entries_by_id[parent_entry_id].canonical_url == candidate.url
        for candidate, parent_entry_id in source.parents
    )
    assert all(
        candidate.menu_depth == 1
        and candidate.exploration_path == entries_by_id[parent_entry_id].menu_path
        for candidate, parent_entry_id in source.parents
    )
    assert all(
        not parent_entry_id.startswith("discovery:") for _, parent_entry_id in source.parents
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
async def test_navigation_handoff_overrides_stale_candidate_or_stops(blocked):
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("http://91porna.com/")
    sources = []
    finds = []

    class Targets:
        async def find(self, **kwargs):
            finds.append(kwargs["source_capture_id"])
            return None

    class HandoffVisitor:
        async def visit(self, **kwargs):
            sources.append(kwargs["source_capture_id"])
            return WebMenuVisitResult(
                final_capture_id="classification-baseline",
                page_type="content_detail",
                navigation_current_capture_id=None if blocked else f"actual-current:{len(sources)}",
                navigation_handoff_error="WEB_NAVIGATION_CURRENT_UNTRUSTED" if blocked else None,
            )

    explorer = WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(
            tuple(
                replace(c, source_capture_id="stale-candidate") for c in _runtime_candidates(site)
            )
        ),
        page_visitor=HandoffVisitor(),
        logical_target_repository=Targets(),
    )
    if blocked:
        with pytest.raises(RuntimeError, match="WEB_NAVIGATION_CURRENT_UNTRUSTED"):
            await explorer.explore(
                _command(),
                initial_capture_id="home-classified",
                initial_navigation_capture_id="home-current",
            )
        assert sources == finds == ["home-current"]
    else:
        summary = await explorer.explore(
            _command(),
            initial_capture_id="home-classified",
            initial_navigation_capture_id="home-current",
        )
        assert len(sources) > 1
        assert (
            sources
            == finds
            == ["home-current", *(f"actual-current:{i}" for i in range(1, len(sources)))]
        )
        assert summary.page_timeline[0] == "home-classified"


def test_untrusted_detail_selection_keeps_bounded_audit_facts_without_action() -> None:
    """低置信选择未发动作时仍须保留候选 ID、置信度和原因。"""

    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://www.91jav1.com/")
    entry = next(item for item in site.entries if item.entry_id == "91jav.theme")

    record = _terminal_record(
        command=_command("https://www.91jav1.com/"),
        site_id=site.site_id,
        entry=entry,
        result=WebMenuVisitResult(
            final_capture_id="capture:menu",
            page_type="video_list",
            status=WebMenuTerminalStatus.DETAIL_SELECTION_UNTRUSTED,
            error_code="WEB_DETAIL_SELECTION_UNTRUSTED",
            menu_action_count=1,
            menu_intent_id="web-menu:run-1:www_91jav1_com:91jav.theme",
            menu_action_execution_id="action:menu",
            menu_probe_capture_id="capture:probe",
            menu_ready_capture_id="capture:menu",
            menu_page_type="video_list",
            detail_candidate_ids=("candidate:1", "candidate:2"),
            selected_detail_candidate_id="candidate:2",
            detail_selection_confidence=0.61,
            detail_selection_reason="卡片语义不足，禁止发起详情动作",
            detail_action_count=0,
        ),
    )

    assert record.detail_action_execution_id is None
    assert record.detail_selection == {
        "candidate_id": "candidate:2",
        "confidence": 0.61,
        "reason": "卡片语义不足，禁止发起详情动作",
    }


def _frozen_persisted_summary(
    registry: WebSiteMenuRegistry,
    *,
    target_url: str = "https://hl365.com/",
) -> WebMenuExplorationSummary:
    site = registry.resolve_site(target_url)
    return WebMenuExplorationSummary(
        site_id=site.site_id,
        catalog_version=registry.version,
        catalog_sha256=registry.catalog_content_sha256,
        menu_total=len(site.entries),
        menu_ledger=tuple(
            WebMenuLedgerEntry(
                entry_id=entry.entry_id,
                menu_path=entry.menu_path,
                canonical_url=("sha256:" + sha256(entry.canonical_url.encode("utf-8")).hexdigest()),
                status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                source_capture_id="",
                final_capture_id=None,
                page_type=None,
                detail_capture_id=None,
                error_code="WEB_BUDGET_RESERVED_NEEDS_REVIEW",
            )
            for entry in site.entries
        ),
        page_timeline=(),
        page_type_keys=(),
    )


@pytest.mark.parametrize(
    "drift",
    (
        "site_id",
        "catalog_version",
        "catalog_sha256",
        "missing_entry",
        "extra_entry",
        "duplicate_entry",
        "entry_order",
        "menu_path",
        "placeholder_format",
        "placeholder_digest",
        "placeholder_uppercase",
    ),
)
def test_strict_hydration_rejects_every_frozen_catalog_drift(drift: str) -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    summary = _frozen_persisted_summary(registry)
    ledger = summary.menu_ledger
    if drift == "site_id":
        summary = replace(summary, site_id="other-site")
    elif drift == "catalog_version":
        summary = replace(summary, catalog_version="other-version")
    elif drift == "catalog_sha256":
        summary = replace(summary, catalog_sha256="0" * 64)
    elif drift == "missing_entry":
        summary = replace(summary, menu_ledger=ledger[:-1])
    elif drift == "extra_entry":
        summary = replace(
            summary,
            menu_ledger=(
                *ledger,
                replace(ledger[-1], entry_id="unpublished-entry"),
            ),
        )
    elif drift == "duplicate_entry":
        summary = replace(summary, menu_ledger=(*ledger[:-1], ledger[0]))
    elif drift == "entry_order":
        summary = replace(summary, menu_ledger=(ledger[1], ledger[0], *ledger[2:]))
    elif drift == "menu_path":
        summary = replace(
            summary,
            menu_ledger=(replace(ledger[0], menu_path=("漂移菜单",)), *ledger[1:]),
        )
    elif drift == "placeholder_format":
        summary = replace(
            summary,
            menu_ledger=(
                replace(ledger[0], canonical_url=site.entries[0].canonical_url),
                *ledger[1:],
            ),
        )
    elif drift == "placeholder_digest":
        summary = replace(
            summary,
            menu_ledger=(replace(ledger[0], canonical_url=f"sha256:{'f' * 64}"), *ledger[1:]),
        )
    else:
        summary = replace(
            summary,
            menu_ledger=(
                replace(ledger[0], canonical_url=ledger[0].canonical_url.upper()),
                *ledger[1:],
            ),
        )

    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        hydrate_web_menu_summary(
            summary,
            site=site,
            catalog_version=registry.version,
            catalog_sha256=registry.catalog_content_sha256,
        )


def test_strict_hydration_returns_registry_urls_only_after_exact_match() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")

    hydrated = hydrate_web_menu_summary(
        _frozen_persisted_summary(registry),
        site=site,
        catalog_version=registry.version,
        catalog_sha256=registry.catalog_content_sha256,
    )

    assert tuple(item.entry_id for item in hydrated.menu_ledger) == tuple(
        entry.entry_id for entry in site.entries
    )
    assert tuple(item.canonical_url for item in hydrated.menu_ledger) == tuple(
        entry.canonical_url for entry in site.entries
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("menu_limit", [5, 10, 25])
async def test_persistent_budget_retry_obeys_frozen_menu_limit(menu_limit: int) -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("http://91porna.com/")

    class Ledger:
        def __init__(self) -> None:
            self.reservations: dict[str, WebMenuActionReservation] = {}
            self.entries = {}
            self.records = {}

        async def claim_menu(self, *, command, entry, source_capture_id):
            del source_capture_id
            reserved = self.reservations.get(entry.entry_id)
            if reserved is not None:
                return WebMenuActionReservation(
                    site_id=reserved.site_id,
                    menu_entry_id=reserved.menu_entry_id,
                    execution_slot=reserved.execution_slot,
                    intent_id=reserved.intent_id,
                    already_reserved=True,
                )
            if len(self.reservations) >= menu_limit:
                return None
            reserved = WebMenuActionReservation(
                site_id=site.site_id,
                menu_entry_id=entry.entry_id,
                execution_slot=len(self.reservations) + 1,
                intent_id=f"web-menu:{command.agent_run_id}:{site.site_id}:{entry.entry_id}",
                already_reserved=False,
            )
            self.reservations[entry.entry_id] = reserved
            self.entries[entry.entry_id] = entry
            return reserved

        async def finalize(self, record):
            self.records[record.menu_entry_id] = record

        async def read_summary(self, *, agent_run_id, site_id):
            del agent_run_id
            base = _frozen_persisted_summary(
                registry,
                target_url="http://91porna.com/",
            )
            return replace(
                base,
                menu_limit=menu_limit,
                executed_same_origin_count=len(self.reservations),
                menu_ledger=tuple(
                    (
                        replace(
                            item,
                            status=self.records[item.entry_id].status,
                            source_capture_id="capture-home",
                            final_capture_id=(self.records[item.entry_id].menu_ready_capture_id),
                            page_type=self.records[item.entry_id].menu_page_type,
                            error_code=None,
                            menu_action_count=1,
                            menu_intent_id=self.reservations[item.entry_id].intent_id,
                            menu_action_execution_id=(
                                self.records[item.entry_id].menu_action_execution_id
                            ),
                        )
                        if item.entry_id in self.reservations
                        else item
                    )
                    for item in base.menu_ledger
                ),
                page_timeline=("capture-home",),
                page_type_keys=("content_list",),
            )

    class CountingVisitor:
        def __init__(self) -> None:
            self.visit_count = 0

        async def visit(self, *, entry, menu_intent_id, **kwargs):
            del kwargs
            self.visit_count += 1
            return WebMenuVisitResult(
                final_capture_id=f"capture:{entry.entry_id}",
                page_type="content_list",
                menu_probe_capture_id=f"probe:{entry.entry_id}",
                menu_ready_capture_id=f"capture:{entry.entry_id}",
                menu_page_type="content_list",
                menu_intent_id=menu_intent_id,
                menu_action_execution_id=f"actual-action:{entry.entry_id}",
            )

    ledger = Ledger()
    visitor = CountingVisitor()
    explorer = WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(_runtime_candidates(site)),
        page_visitor=visitor,
        budget_ledger=ledger,
    )

    await explorer.explore(_command(), initial_capture_id="capture-home")
    persisted = (await explorer.explore(_command(), initial_capture_id="capture-home")).menu_ledger

    assert sum(entry.menu_action_count for entry in persisted) <= menu_limit
    assert len({entry.menu_intent_id for entry in persisted if entry.menu_intent_id}) <= menu_limit
    assert visitor.visit_count <= menu_limit
    assert visitor.visit_count == len(ledger.reservations) == menu_limit


@pytest.mark.asyncio
async def test_persistent_budget_retry_after_coordinate_before_finalize_never_resends() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = next(
        item for item in _runtime_candidates(site) if item.menu_path == entry.menu_path
    )

    class Ledger:
        claimed = False

        async def claim_menu(self, **kwargs):
            del kwargs
            already_reserved = self.claimed
            self.claimed = True
            return WebMenuActionReservation(
                site_id=site.site_id,
                menu_entry_id=entry.entry_id,
                execution_slot=1,
                intent_id="web-menu:run-1:hl365:entry-1",
                already_reserved=already_reserved,
            )

        async def finalize(self, record):
            del record
            raise RuntimeError("process stopped before ledger finalize")

        async def read_summary(self, *, agent_run_id, site_id):
            del agent_run_id
            assert site_id == site.site_id
            base = _frozen_persisted_summary(registry)
            return replace(
                base,
                executed_same_origin_count=1,
                menu_ledger=tuple(
                    (
                        replace(
                            item,
                            menu_action_count=1,
                            menu_intent_id="web-menu:run-1:hl365:entry-1",
                        )
                        if item.entry_id == entry.entry_id
                        else item
                    )
                    for item in base.menu_ledger
                ),
            )

    class VisitorAfterCoordinate:
        visit_count = 0

        async def visit(self, *, menu_intent_id, **kwargs):
            del kwargs
            self.visit_count += 1
            return WebMenuVisitResult(
                final_capture_id="capture-menu-ready",
                page_type="content_list",
                menu_probe_capture_id="capture-menu-probe",
                menu_ready_capture_id="capture-menu-ready",
                menu_page_type="content_list",
                menu_action_count=1,
                menu_intent_id=menu_intent_id,
                menu_action_execution_id="actual-menu-action-lost-before-finalize",
            )

    visitor = VisitorAfterCoordinate()
    explorer = WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource((candidate,)),
        page_visitor=visitor,
        budget_ledger=Ledger(),
    )
    command = _command("https://hl365.com/")

    with pytest.raises(RuntimeError, match="before ledger finalize"):
        await explorer.explore(command, initial_capture_id="capture-home")
    recovered = await explorer.explore(command, initial_capture_id="capture-home")

    assert visitor.visit_count == 1
    recovered_entry = next(
        item for item in recovered.menu_ledger if item.entry_id == entry.entry_id
    )
    assert recovered_entry.status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert recovered_entry.error_code == "WEB_BUDGET_RESERVED_NEEDS_REVIEW"
    assert recovered_entry.menu_action_count == 1
    assert recovered_entry.menu_action_execution_id is None
    assert recovered_entry.canonical_url == entry.canonical_url


@pytest.mark.asyncio
async def test_persistent_budget_drift_without_runtime_candidate_returns_ledger_only() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )

    class Ledger:
        def __init__(self) -> None:
            self.claim_count = 0
            self.records = {}

        async def claim_menu(self, **kwargs):
            self.claim_count += 1
            raise AssertionError(kwargs)

        async def finalize(self, record):
            assert record.status is WebMenuTerminalStatus.ALLOWLIST_DRIFTED
            assert record.menu_probe_capture_id == "capture-home"
            assert record.menu_action_execution_id is None
            assert record.detail_action_execution_id is None
            self.records[record.menu_entry_id] = record

        async def read_summary(self, *, agent_run_id, site_id):
            assert agent_run_id == "run-1"
            assert site_id == site.site_id
            base = _frozen_persisted_summary(registry)
            return replace(
                base,
                menu_ledger=tuple(
                    (
                        replace(
                            item,
                            status=self.records[item.entry_id].status,
                            source_capture_id=(
                                self.records[item.entry_id].menu_probe_capture_id or ""
                            ),
                            error_code="WEB_MENU_ALLOWLIST_DRIFTED",
                            menu_probe_capture_id=(
                                self.records[item.entry_id].menu_probe_capture_id
                            ),
                        )
                        if item.entry_id in self.records
                        else item
                    )
                    for item in base.menu_ledger
                ),
            )

    class NeverVisitor:
        def __init__(self) -> None:
            self.visit_count = 0

        async def visit(self, **kwargs):
            self.visit_count += 1
            raise AssertionError(kwargs)

    ledger = Ledger()
    visitor = NeverVisitor()
    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(()),
        page_visitor=visitor,
        budget_ledger=ledger,
    ).explore(_command("https://hl365.com/"), initial_capture_id="capture-home")

    recovered_entry = next(item for item in summary.menu_ledger if item.entry_id == entry.entry_id)
    assert ledger.claim_count == 0
    assert visitor.visit_count == 0
    assert recovered_entry.canonical_url == entry.canonical_url
    assert recovered_entry.status is WebMenuTerminalStatus.ALLOWLIST_DRIFTED
    assert recovered_entry.source_capture_id == "capture-home"
    assert recovered_entry.menu_probe_capture_id == "capture-home"
    assert recovered_entry.menu_action_count == 0
    assert recovered_entry.menu_intent_id is None
    assert recovered_entry.menu_action_execution_id is None
    assert recovered_entry.detail_action_count == 0
    assert recovered_entry.detail_intent_id is None
    assert recovered_entry.detail_action_execution_id is None


@pytest.mark.asyncio
async def test_persistent_no_match_closes_pending_entries_as_zero_action_drift() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    executable = plan_web_menu_collection(site).selected_entries

    class Ledger:
        def __init__(self) -> None:
            self.records = {}

        async def claim_menu(self, **kwargs):
            raise AssertionError(kwargs)

        async def finalize(self, record):
            self.records[record.menu_entry_id] = record

        async def read_summary(self, *, agent_run_id, site_id):
            assert agent_run_id == "run-1"
            assert site_id == site.site_id
            base = _frozen_persisted_summary(registry)
            return replace(
                base,
                menu_ledger=tuple(
                    (
                        replace(
                            item,
                            status=self.records[item.entry_id].status,
                            source_capture_id=(
                                self.records[item.entry_id].menu_probe_capture_id or ""
                            ),
                            error_code="WEB_MENU_ALLOWLIST_DRIFTED",
                            menu_probe_capture_id=(
                                self.records[item.entry_id].menu_probe_capture_id
                            ),
                        )
                        if item.entry_id in self.records
                        else item
                    )
                    for item in base.menu_ledger
                ),
            )

    ledger = Ledger()
    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(()),
        page_visitor=object(),
        budget_ledger=ledger,
    ).explore(_command("https://hl365.com/"), initial_capture_id="capture-home")

    assert tuple(ledger.records) == tuple(item.entry_id for item in executable)
    assert all(
        record.status is WebMenuTerminalStatus.ALLOWLIST_DRIFTED
        and record.menu_probe_capture_id == "capture-home"
        and record.menu_action_execution_id is None
        and record.detail_action_execution_id is None
        for record in ledger.records.values()
    )
    recovered = tuple(item for item in summary.menu_ledger if item.entry_id in ledger.records)
    assert len(recovered) == len(executable)
    assert all(item.status is WebMenuTerminalStatus.ALLOWLIST_DRIFTED for item in recovered)
    assert all(item.menu_action_count == 0 for item in recovered)
    assert all(item.menu_intent_id is None for item in recovered)
    assert all(item.menu_action_execution_id is None for item in recovered)
    assert all(item.detail_action_count == 0 for item in recovered)
    assert all(item.detail_intent_id is None for item in recovered)
    assert all(item.detail_action_execution_id is None for item in recovered)


@pytest.mark.asyncio
async def test_persistent_site_human_block_closes_remaining_pending_without_actions() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    executable = plan_web_menu_collection(site).selected_entries
    first = executable[0]
    candidate = next(
        item for item in _runtime_candidates(site) if item.menu_path == first.menu_path
    )

    class Ledger:
        def __init__(self) -> None:
            self.records = {}

        async def claim_menu(self, *, command, entry, source_capture_id):
            del source_capture_id
            assert entry.entry_id == first.entry_id
            return WebMenuActionReservation(
                site_id=site.site_id,
                menu_entry_id=entry.entry_id,
                execution_slot=1,
                intent_id=f"web-menu:{command.agent_run_id}:{site.site_id}:{entry.entry_id}",
                already_reserved=False,
            )

        async def finalize(self, record):
            self.records[record.menu_entry_id] = record

        async def read_summary(self, *, agent_run_id, site_id):
            assert agent_run_id == "run-1"
            assert site_id == site.site_id
            base = _frozen_persisted_summary(registry)
            return replace(
                base,
                menu_ledger=tuple(
                    (
                        replace(
                            item,
                            status=self.records[item.entry_id].status,
                            source_capture_id="capture-human-block",
                            final_capture_id=(
                                "capture-human-block" if item.entry_id == first.entry_id else None
                            ),
                            error_code="WEB_SITE_HUMAN_VERIFICATION_BLOCKED",
                            menu_probe_capture_id="capture-human-block",
                            menu_action_count=int(item.entry_id == first.entry_id),
                            menu_intent_id=(
                                f"web-menu:run-1:{site.site_id}:{first.entry_id}"
                                if item.entry_id == first.entry_id
                                else None
                            ),
                            menu_action_execution_id=(
                                "actual-human-block-action"
                                if item.entry_id == first.entry_id
                                else None
                            ),
                        )
                        if item.entry_id in self.records
                        else item
                    )
                    for item in base.menu_ledger
                ),
            )

    class Visitor:
        async def visit(self, *, menu_intent_id, **kwargs):
            del kwargs
            return WebMenuVisitResult(
                final_capture_id="capture-human-block",
                page_type=None,
                status=WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
                error_code="WEB_MENU_HUMAN_VERIFICATION_BLOCKED",
                menu_probe_capture_id="capture-human-block",
                menu_action_count=1,
                menu_intent_id=menu_intent_id,
                menu_action_execution_id="actual-human-block-action",
            )

    ledger = Ledger()
    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource((candidate,)),
        page_visitor=Visitor(),
        budget_ledger=ledger,
    ).explore(_command("https://hl365.com/"), initial_capture_id="capture-home")

    assert tuple(ledger.records) == tuple(item.entry_id for item in executable)
    remaining_records = tuple(ledger.records[item.entry_id] for item in executable[1:])
    assert all(
        record.status is WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED
        and record.menu_probe_capture_id == "capture-human-block"
        and record.menu_action_execution_id is None
        and record.detail_action_execution_id is None
        for record in remaining_records
    )
    remaining = tuple(
        item
        for item in summary.menu_ledger
        if item.entry_id in {e.entry_id for e in executable[1:]}
    )
    assert all(
        item.status is WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED for item in remaining
    )
    assert all(item.menu_action_count == 0 for item in remaining)
    assert all(item.menu_intent_id is None for item in remaining)
    assert all(item.menu_action_execution_id is None for item in remaining)


@pytest.mark.parametrize("invalid_count", (True, -1, 2))
def test_menu_result_and_ledger_reject_invalid_detail_action_count(
    invalid_count: object,
) -> None:
    with pytest.raises(ValueError, match="detail_action_count"):
        WebMenuVisitResult(
            final_capture_id=None,
            page_type=None,
            detail_action_count=invalid_count,
        )
    with pytest.raises(ValueError, match="detail_action_count"):
        WebMenuLedgerEntry(
            entry_id="entry-1",
            menu_path=("菜单",),
            canonical_url="https://example.test/menu",
            status=WebMenuTerminalStatus.NAVIGATION_FAILED,
            source_capture_id="capture-source",
            final_capture_id=None,
            page_type=None,
            detail_capture_id=None,
            error_code="WEB_DETAIL_POST_NAVIGATION_FAILED",
            detail_action_count=invalid_count,
        )


@pytest.mark.asyncio
async def test_menu_ledger_preserves_single_detail_action_count() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("http://91porna.com/")

    class FailedDetailVisitor:
        async def visit(self, **kwargs):
            del kwargs
            return WebMenuVisitResult(
                final_capture_id="capture-menu-ready",
                page_type="video_list",
                status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                error_code="WEB_DETAIL_POST_NAVIGATION_FAILED",
                detail_action_count=1,
                ad_landing_evidence={
                    "status": "no_candidate",
                    "reason_code": "WEB_AD_NO_CANDIDATE",
                },
            )

    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(_runtime_candidates(site)),
        page_visitor=FailedDetailVisitor(),
    ).explore(_command(), initial_capture_id="capture-home")

    attempted = [
        entry
        for entry in summary.menu_ledger
        if entry.status is WebMenuTerminalStatus.NAVIGATION_FAILED
    ]
    assert attempted
    assert all(entry.detail_action_count == 1 for entry in attempted)
    assert all(
        entry.ad_landing_evidence
        == {"status": "no_candidate", "reason_code": "WEB_AD_NO_CANDIDATE"}
        for entry in attempted
    )


@pytest.mark.asyncio
async def test_91porna_collects_every_unique_eligible_menu_and_closes_every_catalog_entry() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("http://91porna.com/")
    visitor = Visitor()
    explorer = WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(_runtime_candidates(site)),
        page_visitor=visitor,
    )

    summary = await explorer.explore(
        _command(),
        initial_capture_id="capture-home",
        initial_page_type="content_list",
    )

    assert summary.menu_total == 65
    executable = plan_web_menu_collection(site).selected_entries
    assert summary.menu_limit is None
    assert summary.status_counts["home_baseline"] == 1
    assert summary.status_counts["cross_origin_blocked"] == 2
    assert summary.menu_limit_skipped_count == 0
    assert not [item for item in summary.menu_ledger if item.error_code == "WEB_MENU_LIMIT_SKIPPED"]
    assert summary.executed_same_origin_count == len(executable)
    assert summary.frontier_remaining == 0
    assert len(summary.menu_ledger) == 65
    assert len(visitor.entries) == len(executable)
    first_executed = next(
        item
        for item in summary.menu_ledger
        if item.status is WebMenuTerminalStatus.MENU_PAGE_CAPTURED
    )
    payload = first_executed.to_dict()
    assert str(payload["menu_probe_capture_id"]).startswith("probe:")
    assert len(payload["menu_readiness_evidence_capture_ids"]) == 2


@pytest.mark.asyncio
async def test_five_page_types_do_not_stop_remaining_menus() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("http://91porna.com/")
    visitor = Visitor()
    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(_runtime_candidates(site)),
        page_visitor=visitor,
    ).explore(
        _command(),
        initial_capture_id="capture-home",
        initial_page_type="content_list",
    )

    assert set(summary.page_type_keys) == {
        "video_list",
        "video_detail",
        "content_list",
        "content_detail",
    }
    assert len(visitor.entries) == len(plan_web_menu_collection(site).selected_entries)
    assert len(summary.menu_ledger) == len(site.entries)


@pytest.mark.asyncio
async def test_unknown_menu_is_skipped_without_stopping_remaining_menus() -> None:
    """单个菜单判为 unknown 时应闭合该项，并继续访问剩余 href。"""

    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("http://91porna.com/")
    selected = plan_web_menu_collection(site).selected_entries

    class Ledger:
        def __init__(self) -> None:
            self.reservations = {}
            self.records = {}
            self.claim_sources = []

        async def read_summary(self, *, agent_run_id, site_id):
            del agent_run_id
            assert site_id == site.site_id
            base = _frozen_persisted_summary(
                registry,
                target_url="http://91porna.com/",
            )
            if not self.records:
                return base
            menu_ledger = tuple(
                (
                    replace(
                        item,
                        status=self.records[item.entry_id].status,
                        source_capture_id=self.claim_sources[
                            list(self.reservations).index(item.entry_id)
                        ],
                        final_capture_id=self.records[item.entry_id].menu_ready_capture_id,
                        page_type=self.records[item.entry_id].menu_page_type,
                        error_code=self.records[item.entry_id].diagnostic.get("error_code"),
                        menu_probe_capture_id=(
                            self.records[item.entry_id].menu_probe_capture_id
                        ),
                        menu_ready_capture_id=(
                            self.records[item.entry_id].menu_ready_capture_id
                        ),
                        menu_page_type=self.records[item.entry_id].menu_page_type,
                        menu_action_count=1,
                        menu_intent_id=self.reservations[item.entry_id].intent_id,
                        menu_action_execution_id=(
                            self.records[item.entry_id].menu_action_execution_id
                        ),
                    )
                    if item.entry_id in self.records
                    else item
                )
                for item in base.menu_ledger
            )
            return replace(
                base,
                executed_same_origin_count=len(self.records),
                menu_ledger=menu_ledger,
                status_counts={
                    "page_type_unclassified": sum(
                        record.status is WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
                        for record in self.records.values()
                    ),
                    "menu_page_captured": sum(
                        record.status is WebMenuTerminalStatus.MENU_PAGE_CAPTURED
                        for record in self.records.values()
                    ),
                },
            )

        async def claim_menu(self, *, command, entry, source_capture_id):
            self.claim_sources.append(source_capture_id)
            reservation = WebMenuActionReservation(
                site_id=site.site_id,
                menu_entry_id=entry.entry_id,
                execution_slot=len(self.reservations) + 1,
                intent_id=f"web-menu:{command.agent_run_id}:{entry.entry_id}",
                already_reserved=False,
            )
            self.reservations[entry.entry_id] = reservation
            return reservation

        async def finalize(self, record):
            self.records[record.menu_entry_id] = record

    class UnknownThenContinueVisitor:
        def __init__(self) -> None:
            self.entries = []

        async def visit(self, *, entry, menu_intent_id, **kwargs):
            del kwargs
            self.entries.append(entry)
            capture_id = f"capture:{entry.entry_id}"
            if len(self.entries) == 1:
                return WebMenuVisitResult(
                    final_capture_id=capture_id,
                    page_type="unknown",
                    status=WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED,
                    error_code="WEB_PAGE_TYPE_UNCLASSIFIED",
                    menu_probe_capture_id=capture_id,
                    menu_ready_capture_id=capture_id,
                    menu_page_type="unknown",
                    menu_action_count=1,
                    menu_intent_id=menu_intent_id,
                    menu_action_execution_id=f"action:{entry.entry_id}",
                    navigation_current_capture_id=capture_id,
                )
            return WebMenuVisitResult(
                final_capture_id=capture_id,
                page_type="video_list",
                status=WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
                menu_probe_capture_id=capture_id,
                menu_ready_capture_id=capture_id,
                menu_page_type="video_list",
                menu_action_count=1,
                menu_intent_id=menu_intent_id,
                menu_action_execution_id=f"action:{entry.entry_id}",
                navigation_current_capture_id=capture_id,
            )

    ledger = Ledger()
    visitor = UnknownThenContinueVisitor()
    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(_runtime_candidates(site)),
        page_visitor=visitor,
        budget_ledger=ledger,
    ).explore(
        _command(),
        initial_capture_id="capture-home",
        initial_page_type="home",
    )

    assert len(visitor.entries) == len(selected)
    assert summary.frontier_remaining == 0
    assert summary.status_counts["page_type_unclassified"] == 1
    assert summary.status_counts["menu_page_captured"] == len(selected) - 1
    assert len(ledger.records) == len(selected)
    assert ledger.claim_sources[1] == f"capture:{selected[0].entry_id}"
    unknown_entry = next(
        item
        for item in summary.menu_ledger
        if item.status is WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
    )
    assert unknown_entry.page_type == "unknown"
    assert unknown_entry.error_code == "WEB_PAGE_TYPE_UNCLASSIFIED"
    unknown_record = next(
        record
        for record in ledger.records.values()
        if record.status is WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED
    )
    assert unknown_record.menu_page_type == "unknown"


@pytest.mark.asyncio
async def test_menu_ledger_retains_detail_candidate_selection_evidence() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://www.91jav1.com/")

    class EvidenceVisitor:
        async def visit(self, *, entry, **kwargs):
            del kwargs
            return WebMenuVisitResult(
                final_capture_id=f"capture:{entry.entry_id}",
                page_type="video_list",
                status=WebMenuTerminalStatus.DETAIL_CAPTURED,
                detail_capture_id=f"detail:{entry.entry_id}",
                detail_candidate_ids=("candidate:1", "candidate:2"),
                selected_detail_candidate_id="candidate:1",
                detail_selection_confidence=0.96,
                detail_selection_reason="首个可信重复视频卡片",
            )

    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(_runtime_candidates(site)),
        page_visitor=EvidenceVisitor(),
    ).explore(
        _command("https://www.91jav1.com/"),
        initial_capture_id="capture-home",
    )

    payload = next(
        item.to_dict()
        for item in summary.menu_ledger
        if item.status is WebMenuTerminalStatus.DETAIL_CAPTURED
    )
    assert payload["detail_candidate_ids"] == ["candidate:1", "candidate:2"]
    assert payload["selected_detail_candidate_id"] == "candidate:1"
    assert payload["detail_selection_confidence"] == 0.96
    assert payload["detail_selection_reason"] == "首个可信重复视频卡片"


@pytest.mark.asyncio
async def test_duplicate_url_menu_paths_keep_one_execution_and_an_alias_terminal() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("http://91porna.com/")
    visitor = Visitor()
    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(_runtime_candidates(site)),
        page_visitor=visitor,
    ).explore(_command(), initial_capture_id="capture-home")

    moviesets = [item for item in summary.menu_ledger if item.canonical_url.endswith("/moviesets")]
    assert len(moviesets) == 2
    primary = next(item for item in moviesets if item.entry_id == "91porna.sets")
    assert primary.status is WebMenuTerminalStatus.MENU_PAGE_CAPTURED
    assert primary.entry_id in visitor.entries
    alias = next(item for item in moviesets if item.entry_id == "91porna.sets.latest")
    assert alias.status is WebMenuTerminalStatus.MENU_LIMIT_SKIPPED
    assert alias.entry_id not in visitor.entries
    assert alias.error_code == "WEB_MENU_LOGICAL_ALIAS"
    assert alias.alias_of == "91porna.sets"


def test_menu_collection_plan_keeps_aliases_and_collects_unique_eligible_urls() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("http://91porna.com/")

    plan = plan_web_menu_collection(site)

    selected_urls = [item.canonical_url for item in plan.selected_entries]
    assert len(selected_urls) == len(set(selected_urls))
    assert all(item.collection_eligibility.eligible for item in plan.selected_entries)
    assert plan.alias_of["91porna.sets.latest"] == "91porna.sets"
    assert "91porna.sets" in {item.entry_id for item in plan.selected_entries}
    assert "91porna.sets.latest" in {item.entry_id for item in plan.skipped_entries}


def test_default_menu_collection_keeps_every_unique_eligible_menu() -> None:
    site = _site_with_unique_menus(12)

    plan = plan_web_menu_collection(site)

    assert len(plan.selected_entries) == 12
    assert not [entry for entry in plan.skipped_entries if entry.collection_eligibility.eligible]


@pytest.mark.asyncio
async def test_unbounded_explorer_executes_more_than_ten_visible_menus() -> None:
    summary = await _explore_site_with_visible_menu_count(12)

    assert summary.menu_limit is None
    assert summary.executed_same_origin_count == 12
    assert summary.menu_limit_skipped_count == 0


def test_menu_collection_plan_never_enqueues_deprecated_catalog_entries() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")

    plan = plan_web_menu_collection(site)

    selected_ids = {entry.entry_id for entry in plan.selected_entries}
    deprecated_ids = {
        entry.entry_id for entry in site.entries if entry.status.value == "deprecated"
    }
    assert deprecated_ids == {
        "hl365.sjb_hl",
        "hl365.sjb_ttt",
        "hl365.sjb_rs",
        "hl365.sjb_bc",
    }
    assert selected_ids.isdisjoint(deprecated_ids)


@pytest.mark.asyncio
async def test_in_memory_ledger_uses_stable_deprecated_catalog_diagnostic() -> None:
    registry = WebSiteMenuRegistry()

    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(()),
        page_visitor=object(),
    ).explore(
        _command("https://hl365.com/"),
        initial_capture_id="capture-home",
    )

    deprecated = {
        item.entry_id: item
        for item in summary.menu_ledger
        if item.entry_id in {"hl365.sjb_hl", "hl365.sjb_ttt", "hl365.sjb_rs", "hl365.sjb_bc"}
    }
    assert set(deprecated) == {
        "hl365.sjb_hl",
        "hl365.sjb_ttt",
        "hl365.sjb_rs",
        "hl365.sjb_bc",
    }
    assert all(item.error_code == "WEB_MENU_CATALOG_DEPRECATED" for item in deprecated.values())


def test_menu_collection_plan_skips_root_alias_already_captured_as_home() -> None:
    site = WebSiteMenuRegistry().resolve_site("https://heiliao.com/")
    home = next(entry for entry in site.entries if entry.decision is WebMenuDecision.HOME_BASELINE)
    root_alias = replace(
        home,
        entry_id="heiliao.home.alias",
        menu_path=("首页别名",),
        decision=WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY,
    )

    plan = plan_web_menu_collection(replace(site, entries=(home, root_alias)))

    assert plan.selected_entries == ()
    assert plan.skipped_entries == (root_alias,)
    assert plan.alias_of == {root_alias.entry_id: home.entry_id}


@pytest.mark.asyncio
async def test_site_with_fewer_than_ten_eligible_menus_collects_all() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://www.91jav1.com/")
    visitor = Visitor()

    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(_runtime_candidates(site)),
        page_visitor=visitor,
    ).explore(
        _command("https://www.91jav1.com/"),
        initial_capture_id="capture-home",
    )

    expected = len(plan_web_menu_collection(site).selected_entries)
    assert len(visitor.entries) == expected
    assert summary.executed_same_origin_count == expected
    assert summary.menu_limit_skipped_count == 0


@pytest.mark.asyncio
async def test_catalog_drift_and_runtime_new_menu_are_record_only() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    candidates = list(_runtime_candidates(site))
    missing = candidates.pop(1)
    candidates.append(
        WebNavigationCandidate(
            entry_id="runtime:new",
            label="新增菜单",
            url="https://hl365.com/new-runtime-menu",
            menu_path=("新增菜单",),
            dom_fingerprint="dom:new",
            source_signal="semantic_nav",
            page_type_key="nav-class:new",
            source_capture_id="capture-runtime-new-source",
        )
    )
    visitor = Visitor()
    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(tuple(candidates)),
        page_visitor=visitor,
    ).explore(
        _command("https://hl365.com/"),
        initial_capture_id="capture-home",
        initial_page_type="content_list",
    )

    drifted = [item for item in summary.menu_ledger if item.status == "allowlist_drifted"]
    discovered = list(summary.runtime_discovered_ledger)
    assert [item.canonical_url for item in drifted] == [missing.url]
    assert [item.canonical_url for item in discovered] == ["https://hl365.com/new-runtime-menu"]
    assert discovered[0].source_capture_id == "capture-runtime-new-source"
    assert "runtime:new" not in visitor.entries
    assert len(summary.menu_ledger) == len(site.entries)
    assert summary.runtime_discovered_count == 1
    assert summary.status_counts["runtime_discovered_unreviewed"] == 0


def test_unique_href_matches_when_runtime_parent_label_drifts() -> None:
    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")
    target = next(entry for entry in site.entries if entry.entry_id == "hl365.sjb_qm")
    candidates = list(_runtime_candidates(site))
    index = next(
        index for index, candidate in enumerate(candidates) if candidate.url == target.canonical_url
    )
    original = candidates[index]
    candidates[index] = WebNavigationCandidate(
        entry_id=original.entry_id,
        label=original.label,
        url=original.url,
        menu_path=("优先投放", original.menu_path[-1]),
        dom_fingerprint=original.dom_fingerprint,
        source_signal=original.source_signal,
        page_type_key=original.page_type_key,
        menu_position=original.menu_position,
    )

    reconciliation = reconcile_site_menu(site, tuple(candidates))

    assert reconciliation.matched_candidates[target.entry_id].url == target.canonical_url
    assert reconciliation.matched_candidates[target.entry_id].menu_path == (
        "优先投放",
        original.menu_path[-1],
    )
    assert reconciliation.matched_candidates[target.entry_id].allowlist_match is True


def test_href_less_candidate_is_audited_but_never_matched_as_arrival() -> None:
    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")
    candidate = WebNavigationCandidate(
        entry_id="runtime:no-target",
        label="动态菜单",
        url=None,
        menu_path=("动态菜单",),
        dom_fingerprint="dom:no-target",
        source_signal="semantic_nav",
        page_type_key="nav-class:dynamic",
        source_kind="button",
        target_hint="动态菜单",
        actionability="requires_runtime_validation",
    )

    reconciliation = reconcile_site_menu(site, (candidate,))

    assert reconciliation.matched_candidates == {}
    assert reconciliation.runtime_unreviewed == (candidate,)


def test_catalog_entries_never_synthesize_runtime_navigation_candidates() -> None:
    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")

    reconciliation = reconcile_site_menu(site, ())

    assert reconciliation.matched_candidates == {}
    assert reconciliation.runtime_unreviewed == ()


def test_allowlisted_href_less_menu_is_not_scheduled_for_click() -> None:
    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")
    target = next(
        entry
        for entry in site.entries
        if entry.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = WebNavigationCandidate(
        entry_id="runtime:button",
        label=target.menu_path[-1],
        url=None,
        menu_path=target.menu_path,
        dom_fingerprint="dom:button",
        source_signal="semantic_nav",
        page_type_key="nav-class:dynamic",
        source_capture_id="capture-home",
        source_kind="button",
        target_hint=target.menu_path[-1],
        actionability="requires_runtime_validation",
    )

    reconciliation = reconcile_site_menu(site, (candidate,))

    assert target.entry_id not in reconciliation.matched_candidates
    assert reconciliation.runtime_unreviewed == (candidate,)


def test_hidden_href_catalog_menu_is_matched_for_direct_navigation() -> None:
    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")
    target = next(
        entry
        for entry in site.entries
        if entry.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = next(
        item for item in _runtime_candidates(site) if item.url == target.canonical_url
    )
    hidden_candidate = replace(candidate, visibility="hidden")

    reconciliation = reconcile_site_menu(site, (hidden_candidate,))

    assert reconciliation.matched_candidates[target.entry_id] == replace(
        hidden_candidate,
        allowlist_match=True,
    )
    assert reconciliation.runtime_unreviewed == ()


@pytest.mark.asyncio
async def test_discovered_href_frontier_survives_current_capture_advancing() -> None:
    """首页冻结的同源 href 应在当前页变化后继续通过 GOTO 逐个执行。"""

    registry = WebSiteMenuRegistry()
    context = registry.resolve_site_context("https://example.test/")
    home = context.site.entries[0]
    candidates = tuple(
        WebNavigationCandidate(
            entry_id=f"runtime:frozen:{index}",
            label=f"频道 {index}",
            url=f"https://example.test/channel/{index}",
            menu_path=(f"频道 {index}",),
            dom_fingerprint=f"dom:frozen:{index}",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime",
            source_capture_id="capture-home",
            discovery_capture_id="capture-home",
        )
        for index in range(1, 4)
    )
    persisted = WebMenuExplorationSummary(
        site_id=context.site.site_id,
        catalog_version=context.site.catalog_version,
        catalog_sha256=context.content_sha256,
        menu_total=1,
        menu_ledger=(
            replace(
                WebMenuLedgerEntry(
                    entry_id=home.entry_id,
                    menu_path=home.menu_path,
                    canonical_url=home.canonical_url,
                    status=WebMenuTerminalStatus.HOME_BASELINE,
                    source_capture_id="capture-home",
                    final_capture_id="capture-home",
                    page_type="home",
                    detail_capture_id=None,
                    error_code=None,
                ),
                canonical_url="sha256:"
                + sha256(home.canonical_url.encode("utf-8")).hexdigest(),
            ),
        ),
        page_timeline=("capture-home",),
        page_type_keys=("home",),
    )

    class Ledger:
        def __init__(self) -> None:
            self.claim_sources: list[str] = []

        async def read_summary(self, **_kwargs):
            return persisted

        async def record_discovery(self, **_kwargs):
            return None

        async def claim_menu(self, *, command, entry, source_capture_id):
            self.claim_sources.append(source_capture_id)
            return WebMenuActionReservation(
                site_id=context.site.site_id,
                menu_entry_id=entry.entry_id,
                execution_slot=len(self.claim_sources),
                intent_id=f"web-menu:{command.agent_run_id}:{entry.entry_id}",
                already_reserved=False,
            )

        async def finalize(self, _record):
            return None

    class AdvancingVisitor:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def visit(self, *, entry, menu_intent_id, **_kwargs):
            self.urls.append(entry.canonical_url)
            capture_id = f"capture-current-{len(self.urls)}"
            return WebMenuVisitResult(
                final_capture_id=capture_id,
                page_type="content_list",
                navigation_current_capture_id=capture_id,
                menu_probe_capture_id=capture_id,
                menu_ready_capture_id=capture_id,
                menu_page_type="content_list",
                menu_action_count=1,
                menu_intent_id=menu_intent_id,
                menu_action_execution_id=f"action-{len(self.urls)}",
            )

    ledger = Ledger()
    visitor = AdvancingVisitor()
    await WebMenuExplorer(
        navigation_context=registry,
        candidate_source=CandidateSource(candidates),
        page_visitor=visitor,
        budget_ledger=ledger,
        discovery_policy=WebDiscoveryPolicy(enabled=True, max_unlisted_menus=None),
    ).explore(
        _command("https://example.test/"),
        initial_capture_id="capture-home",
        initial_page_type="home",
        initial_navigation_capture_id="capture-home",
    )

    assert visitor.urls == [candidate.url for candidate in candidates]
    assert ledger.claim_sources == [
        "capture-home",
        "capture-current-1",
        "capture-current-2",
    ]


@pytest.mark.asyncio
async def test_runtime_menu_finalizes_the_entry_identity_returned_by_reservation() -> None:
    """动态候选被账本归并后，终态必须引用实际领取的账本行身份。"""

    registry = WebSiteMenuRegistry()
    context = registry.resolve_site_context("https://example.test/")
    home = context.site.entries[0]
    candidate = WebNavigationCandidate(
        entry_id="runtime:rediscovered",
        label="重发现频道",
        url="https://example.test/channel",
        menu_path=("重发现频道",),
        dom_fingerprint="dom:rediscovered",
        source_signal="semantic_nav",
        page_type_key="nav-class:runtime",
        source_capture_id="capture-home",
        discovery_capture_id="capture-home",
    )
    persisted = WebMenuExplorationSummary(
        site_id=context.site.site_id,
        catalog_version=context.site.catalog_version,
        catalog_sha256=context.content_sha256,
        menu_total=1,
        menu_ledger=(
            replace(
                WebMenuLedgerEntry(
                    entry_id=home.entry_id,
                    menu_path=home.menu_path,
                    canonical_url=home.canonical_url,
                    status=WebMenuTerminalStatus.HOME_BASELINE,
                    source_capture_id="capture-home",
                    final_capture_id="capture-home",
                    page_type="home",
                    detail_capture_id=None,
                    error_code=None,
                ),
                canonical_url="sha256:"
                + sha256(home.canonical_url.encode("utf-8")).hexdigest(),
            ),
        ),
        page_timeline=("capture-home",),
        page_type_keys=("home",),
    )

    class Ledger:
        def __init__(self) -> None:
            self.records = []

        async def read_summary(self, **_kwargs):
            return persisted

        async def record_discovery(self, **_kwargs):
            return None

        async def claim_menu(self, **_kwargs):
            return WebMenuActionReservation(
                site_id=context.site.site_id,
                menu_entry_id="discovery:persisted-ledger-entry",
                execution_slot=1,
                intent_id="web-discovery-menu:run-1:persisted",
                already_reserved=False,
            )

        async def finalize(self, record):
            self.records.append(record)

    class PageVisitor:
        async def visit(self, *, menu_intent_id, **_kwargs):
            return WebMenuVisitResult(
                final_capture_id="capture-menu-ready",
                page_type="content_list",
                menu_probe_capture_id="capture-menu-probe",
                menu_ready_capture_id="capture-menu-ready",
                menu_page_type="content_list",
                menu_action_count=1,
                menu_intent_id=menu_intent_id,
                menu_action_execution_id="action-menu",
            )

    ledger = Ledger()
    await WebMenuExplorer(
        navigation_context=registry,
        candidate_source=CandidateSource((candidate,)),
        page_visitor=PageVisitor(),
        budget_ledger=ledger,
        discovery_policy=WebDiscoveryPolicy(enabled=True, max_unlisted_menus=None),
    ).explore(
        _command("https://example.test/"),
        initial_capture_id="capture-home",
        initial_page_type="home",
        initial_navigation_capture_id="capture-home",
    )

    assert len(ledger.records) == 1
    assert ledger.records[0].menu_entry_id == "discovery:persisted-ledger-entry"


def test_ambiguous_same_href_with_label_drift_stays_unreviewed() -> None:
    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")
    target = next(entry for entry in site.entries if entry.entry_id == "hl365.sjb_hl")
    candidates = [
        candidate
        for candidate in _runtime_candidates(site)
        if candidate.url != target.canonical_url
    ]
    candidates.extend(
        (
            WebNavigationCandidate(
                entry_id="runtime:drift:1",
                label=target.menu_path[-1],
                url=target.canonical_url,
                menu_path=("优先投放", target.menu_path[-1]),
                dom_fingerprint="dom:drift:1",
                source_signal="semantic_nav",
                page_type_key="nav-class:drift",
                menu_position=1,
            ),
            WebNavigationCandidate(
                entry_id="runtime:drift:2",
                label=target.menu_path[-1],
                url=target.canonical_url,
                menu_path=("轮播导航", target.menu_path[-1]),
                dom_fingerprint="dom:drift:2",
                source_signal="semantic_nav",
                page_type_key="nav-class:drift",
                menu_position=2,
            ),
        )
    )

    reconciliation = reconcile_site_menu(site, tuple(candidates))

    assert target.entry_id not in reconciliation.matched_candidates
    assert [
        item.entry_id
        for item in reconciliation.runtime_unreviewed
        if item.url == target.canonical_url
    ] == ["runtime:drift:1", "runtime:drift:2"]


@pytest.mark.asyncio
async def test_same_logical_menu_positions_merge_without_extra_discovery_actions() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    target = next(
        entry
        for entry in site.entries
        if entry.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    extras = (
        WebNavigationCandidate(
            entry_id="runtime:position:2:first",
            label=target.menu_path[-1],
            url=target.canonical_url,
            menu_path=target.menu_path,
            dom_fingerprint="dom:position:2:first",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime-position",
            menu_position=2,
            source_capture_id="capture-position-2-first",
        ),
        WebNavigationCandidate(
            entry_id="runtime:position:2:duplicate",
            label=target.menu_path[-1],
            url=target.canonical_url,
            menu_path=target.menu_path,
            dom_fingerprint="dom:position:2:duplicate",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime-position",
            menu_position=2,
            source_capture_id="capture-position-2-later",
        ),
        WebNavigationCandidate(
            entry_id="runtime:position:3",
            label=target.menu_path[-1],
            url=target.canonical_url,
            menu_path=target.menu_path,
            dom_fingerprint="dom:position:3",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime-position",
            menu_position=3,
            source_capture_id="capture-position-3",
        ),
    )

    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource((*_runtime_candidates(site), *extras)),
        page_visitor=Visitor(),
    ).explore(
        _command("https://hl365.com/"),
        initial_capture_id="capture-home",
        initial_page_type="content_list",
    )

    discovered = [
        item
        for item in summary.runtime_discovered_ledger
        if item.canonical_url == target.canonical_url and item.menu_path == target.menu_path
    ]
    assert discovered == []
    payload_positions = [
        item["menu_position"]
        for item in summary.to_dict()["runtime_discovered_ledger"]
        if item["canonical_url"] == target.canonical_url
        and tuple(item["menu_path"]) == target.menu_path
    ]
    assert payload_positions == []
    assert sum(item.entry_id == target.entry_id for item in summary.menu_ledger) == 1


@pytest.mark.asyncio
async def test_catalog_menu_ledger_uses_actual_discovery_capture() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    candidates = list(_runtime_candidates(site))
    target = candidates[1]
    candidates[1] = WebNavigationCandidate(
        entry_id=target.entry_id,
        label=target.label,
        url=target.url,
        menu_path=target.menu_path,
        dom_fingerprint=target.dom_fingerprint,
        source_signal=target.source_signal,
        page_type_key=target.page_type_key,
        source_capture_id="capture-menu-discovered",
    )
    visitor = Visitor()

    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(tuple(candidates)),
        page_visitor=visitor,
    ).explore(
        _command("https://hl365.com/"),
        initial_capture_id="capture-home",
    )

    catalog_entry = next(
        item
        for item in site.entries
        if item.canonical_url == target.url and item.menu_path == target.menu_path
    )
    ledger = next(item for item in summary.menu_ledger if item.entry_id == catalog_entry.entry_id)
    assert ledger.source_capture_id == "capture-menu-discovered"
    assert visitor.source_capture_ids[catalog_entry.entry_id] == "capture-menu-discovered"


@pytest.mark.asyncio
async def test_catalog_menu_uses_latest_rediscovered_capture_for_action_source() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    candidates = list(_runtime_candidates(site))
    target_entry = plan_web_menu_collection(site).selected_entries[5]
    target = next(
        candidate
        for candidate in candidates
        if candidate.url == target_entry.canonical_url
        and candidate.menu_path == target_entry.menu_path
    )

    class RefreshingCandidateSource:
        def __init__(self) -> None:
            self.refresh = False

        def navigation_candidates(self, session_id):
            assert session_id == "web:run-1"
            refreshed = []
            for candidate in candidates:
                if self.refresh and candidate.entry_id == target.entry_id:
                    refreshed.append(
                        WebNavigationCandidate(
                            entry_id=candidate.entry_id,
                            label=candidate.label,
                            url=candidate.url,
                            menu_path=candidate.menu_path,
                            dom_fingerprint=candidate.dom_fingerprint,
                            source_signal=candidate.source_signal,
                            page_type_key=candidate.page_type_key,
                            menu_position=candidate.menu_position,
                            source_capture_id="capture-action-latest",
                            discovery_capture_id="capture-discovery",
                        )
                    )
                else:
                    refreshed.append(candidate)
            return tuple(refreshed)

    source = RefreshingCandidateSource()

    class RefreshingVisitor(Visitor):
        async def visit(self, **kwargs):
            result = await super().visit(**kwargs)
            source.refresh = True
            return result

    visitor = RefreshingVisitor()
    await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=source,
        page_visitor=visitor,
    ).explore(
        _command("https://hl365.com/"),
        initial_capture_id="capture-home",
    )

    assert visitor.source_capture_ids[target_entry.entry_id] == "capture-action-latest"


@pytest.mark.asyncio
async def test_https_catalog_does_not_match_runtime_http_downgrade() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    candidates = list(_runtime_candidates(site))
    target_entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    target_index = next(
        index
        for index, item in enumerate(candidates)
        if item.url == target_entry.canonical_url and item.menu_path == target_entry.menu_path
    )
    target = candidates[target_index]
    candidates[target_index] = WebNavigationCandidate(
        entry_id=target.entry_id,
        label=target.label,
        url=target.url.replace("https://", "http://", 1),
        menu_path=target.menu_path,
        dom_fingerprint=target.dom_fingerprint,
        source_signal=target.source_signal,
        page_type_key=target.page_type_key,
    )
    visitor = Visitor()

    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(tuple(candidates)),
        page_visitor=visitor,
    ).explore(
        _command("https://hl365.com/"),
        initial_capture_id="capture-home",
    )

    assert any(
        item.status == "allowlist_drifted" and item.canonical_url == target.url
        for item in summary.menu_ledger
    )
    assert target.entry_id not in visitor.entries


@pytest.mark.asyncio
async def test_human_verification_stops_current_site_and_marks_remaining_frontier() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")

    class BlockingVisitor:
        def __init__(self) -> None:
            self.entries: list[str] = []

        async def visit(self, *, command, entry, candidate, source_capture_id):
            del command, candidate, source_capture_id
            self.entries.append(entry.entry_id)
            if len(self.entries) == 2:
                return WebMenuVisitResult(
                    final_capture_id="capture-human-verification",
                    page_type=None,
                    status=WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
                    error_code="WEB_MENU_HUMAN_VERIFICATION_BLOCKED",
                )
            return WebMenuVisitResult(
                final_capture_id=f"capture:{entry.entry_id}",
                page_type="content_list",
            )

    visitor = BlockingVisitor()
    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(_runtime_candidates(site)),
        page_visitor=visitor,
    ).explore(
        _command("https://hl365.com/"),
        initial_capture_id="capture-home",
        initial_page_type="content_list",
    )

    executable = plan_web_menu_collection(site).selected_entries
    assert len(visitor.entries) == 2
    assert summary.frontier_remaining == len(executable) - 2
    assert summary.status_counts["human_verification_blocked"] == (len(executable) - 1)
    assert all(
        item.status
        in {
            WebMenuTerminalStatus.HOME_BASELINE,
            WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
            WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
            WebMenuTerminalStatus.MENU_LIMIT_SKIPPED,
        }
        for item in summary.menu_ledger
    )


@pytest.mark.asyncio
async def test_later_page_catalog_menu_is_executed_and_new_menu_is_record_only() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    all_candidates = list(_runtime_candidates(site))
    delayed = all_candidates.pop(1)
    runtime_new = WebNavigationCandidate(
        entry_id="runtime:later-new",
        label="运行时新增",
        url="https://hl365.com/runtime-later-new",
        menu_path=("运行时新增",),
        dom_fingerprint="dom:later-new",
        source_signal="semantic_nav",
        page_type_key="nav-class:new",
    )

    class DynamicCandidateSource:
        def __init__(self) -> None:
            self.page_visited = False
            self.read_count = 0

        def navigation_candidates(self, session_id):
            assert session_id == "web:run-1"
            self.read_count += 1
            if self.page_visited:
                return (*all_candidates, delayed, runtime_new)
            return tuple(all_candidates)

    source = DynamicCandidateSource()

    class DynamicVisitor(Visitor):
        async def visit(self, **kwargs):
            result = await super().visit(**kwargs)
            source.page_visited = True
            return result

    visitor = DynamicVisitor()
    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=source,
        page_visitor=visitor,
    ).explore(
        _command("https://hl365.com/"),
        initial_capture_id="capture-home",
    )

    delayed_catalog_entry = next(
        entry.entry_id
        for entry in site.entries
        if entry.canonical_url == delayed.url and entry.menu_path == delayed.menu_path
    )
    assert delayed_catalog_entry in visitor.entries
    assert any(
        item.entry_id == runtime_new.entry_id
        and item.status is WebMenuTerminalStatus.RUNTIME_DISCOVERED_UNREVIEWED
        for item in summary.runtime_discovered_ledger
    )
    assert source.read_count > 1


@pytest.mark.asyncio
async def test_catalog_duplicate_and_cross_origin_anchor_are_not_runtime_new() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("http://91porna.com/")
    candidates = list(_runtime_candidates(site))
    allowlisted = next(
        entry
        for entry in site.entries
        if entry.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    cross_origin = next(
        entry for entry in site.entries if entry.decision is WebMenuDecision.BLOCKED_CROSS_ORIGIN
    )
    candidates.extend(
        [
            WebNavigationCandidate(
                entry_id="runtime:catalog-duplicate",
                label=allowlisted.menu_path[-1],
                url=allowlisted.canonical_url,
                menu_path=allowlisted.menu_path,
                dom_fingerprint="dom:catalog-duplicate",
                source_signal="semantic_nav",
                page_type_key="nav-class:duplicate",
            ),
            WebNavigationCandidate(
                entry_id="runtime:cross-origin",
                label=cross_origin.menu_path[-1],
                url=cross_origin.canonical_url,
                menu_path=cross_origin.menu_path,
                dom_fingerprint="dom:cross-origin",
                source_signal="semantic_nav",
                page_type_key="nav-class:cross-origin",
            ),
        ]
    )

    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(tuple(candidates)),
        page_visitor=Visitor(),
    ).explore(_command(), initial_capture_id="capture-home")

    assert summary.status_counts["runtime_discovered_unreviewed"] == 0
    assert len(summary.menu_ledger) == len(site.entries)
    assert summary.runtime_discovered_ledger == ()


@pytest.mark.asyncio
async def test_runtime_discovered_ledger_never_changes_catalog_totals() -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    runtime_new = tuple(
        WebNavigationCandidate(
            entry_id=f"runtime:new:{index}",
            label=f"新增菜单 {index}",
            url=f"https://hl365.com/runtime-new-{index}",
            menu_path=(f"新增菜单 {index}",),
            dom_fingerprint=f"dom:new:{index}",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime-new",
            source_capture_id=f"capture-runtime-{index}",
        )
        for index in range(46)
    )

    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource((*_runtime_candidates(site), *runtime_new)),
        page_visitor=Visitor(),
    ).explore(
        _command("https://hl365.com/"),
        initial_capture_id="capture-home",
    )

    assert len(summary.menu_ledger) == summary.menu_total == len(site.entries)
    assert len(summary.runtime_discovered_ledger) == 46
    assert summary.runtime_discovered_count == 46
    assert summary.executed_same_origin_count == len(
        plan_web_menu_collection(site).selected_entries
    )
    assert summary.status_counts["runtime_discovered_unreviewed"] == 0
    assert len(summary.to_dict()["runtime_discovered_ledger"]) == 46


class CaptureCoordinationError(RuntimeError):
    pass


class StringDataRightTruncationError(RuntimeError):
    pass


@pytest.mark.parametrize(
    "exception_type",
    (CaptureCoordinationError, StringDataRightTruncationError),
)
@pytest.mark.asyncio
async def test_menu_failure_uses_fixed_error_code_and_bounded_diagnostic(
    exception_type: type[RuntimeError],
) -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://www.91jav1.com/")

    class FailingVisitor:
        async def visit(self, **kwargs):
            del kwargs
            raise exception_type("外部异常细节不得进入错误码")

    summary = await WebMenuExplorer(
        discovery_policy=WebDiscoveryPolicy(),
        navigation_context=registry,
        candidate_source=CandidateSource(_runtime_candidates(site)),
        page_visitor=FailingVisitor(),
    ).explore(
        _command("https://www.91jav1.com/"),
        initial_capture_id="capture-home",
    )

    failed = next(
        item
        for item in summary.menu_ledger
        if item.status is WebMenuTerminalStatus.NAVIGATION_FAILED
    )
    assert failed.error_code == "WEB_MENU_NAVIGATION_FAILED"
    assert re.fullmatch(r"WEB_[A-Z0-9_]+", failed.error_code)
    assert failed.diagnostic_type == exception_type.__name__
    assert failed.diagnostic_message == "菜单访问失败，已闭合当前 Catalog 账本"
    assert "外部异常细节" not in str(failed.to_dict())
