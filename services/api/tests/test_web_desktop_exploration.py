"""Web 白名单菜单导航只生成 page_probe，不再隐式清障。"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest

from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationStage,
)
from services.api.app.application.web_desktop_exploration import (
    WebDesktopMenuPageVisitor,
    WebDesktopNavigationExplorer,
    _read_recovery_url,
)
from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_budget_ledger import (
    WebMenuActionReservation,
)
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.application.web_menu_exploration import (
    WebMenuExplorationSummary,
    WebMenuLedgerEntry,
)
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy
from services.api.app.domain.page import WebMenuDecision, WebSiteMenuRegistry


@pytest.mark.asyncio
async def test_allowlisted_menu_action_execution_uses_reservation_intent() -> None:
    coordinated = []

    class Coordinator:
        async def coordinate(self, command):
            coordinated.append(command)
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="capture-menu"),
                interaction=SimpleNamespace(
                    execution_outcome="succeeded",
                    status="succeeded",
                    action_execution_id="actual-menu-action-42",
                ),
            )

    class UrlReader:
        async def read_url(self, *, capture_id):
            if capture_id == "capture-home":
                return "https://hl365.com/"
            assert capture_id == "capture-menu"
            return entry.canonical_url

    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = WebNavigationCandidate(
        entry_id="runtime-1",
        label=entry.menu_path[-1],
        url=entry.canonical_url,
        menu_path=entry.menu_path,
        dom_fingerprint="dom-1",
        source_signal="semantic_nav",
        page_type_key="nav-class:test",
    )
    command = RunWebWorkflowCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        session_owner="web-v2-workflow",
        target_url="https://hl365.com/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    result = await WebDesktopMenuPageVisitor(
        coordinator=Coordinator(),
        capture_url_reader=UrlReader(),
    ).visit(
        command=command,
        entry=entry,
        candidate=candidate,
        source_capture_id="capture-home",
        menu_intent_id="web-menu:reserved-entry",
    )

    assert result.final_capture_id == "capture-menu"
    assert result.menu_intent_id == "web-menu:reserved-entry"
    assert result.menu_action_execution_id == "actual-menu-action-42"
    assert result.menu_interaction_outcome.command_accepted is True
    assert result.menu_interaction_outcome.transition_arrived is False
    intent = coordinated[0].intent
    assert intent.intent_id == "web-menu:reserved-entry"
    assert intent.parameters["destination_url"] == entry.canonical_url
    assert intent.parameters["clear_obstructions"] is False
    assert intent.parameters["web_capture_stage"] == "web_page_probe"
    assert intent.parameters["viewport_width"] == 1920


@pytest.mark.asyncio
async def test_allowlisted_href_less_menu_never_coordinates_click() -> None:
    coordinated = []

    class Coordinator:
        async def coordinate(self, command):
            coordinated.append(command)
            raise AssertionError("无 href 菜单不得进入动作协调器")

    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = WebNavigationCandidate(
        entry_id="runtime-button-1",
        label=entry.menu_path[-1],
        url=None,
        menu_path=entry.menu_path,
        dom_fingerprint="dom-button-1",
        source_signal="semantic_nav",
        page_type_key="nav-class:test",
        source_capture_id="capture-home",
        source_kind="button",
        target_hint=entry.menu_path[-1],
        source_epoch=2,
        actionability="requires_runtime_validation",
        allowlist_match=True,
    )

    with pytest.raises(ValueError, match="WEB_MENU_DIRECT_URL_REQUIRED"):
        await WebDesktopMenuPageVisitor(
            coordinator=Coordinator(),
            capture_url_reader=object(),
        ).visit(
            command=RunWebWorkflowCommand(
                task_id="task-1",
                agent_run_id="run-1",
                session_id="web:run-1",
                session_owner="web-v2-workflow",
                target_url="https://hl365.com/",
                browser_profile_id="desktop_fhd_1920x1080_v1",
                deadline=datetime.now(UTC) + timedelta(minutes=5),
            ),
            entry=entry,
            candidate=candidate,
            source_capture_id="capture-home",
            menu_intent_id="web-menu:runtime-button",
        )

    assert coordinated == []


@pytest.mark.asyncio
async def test_menu_action_execution_survives_capture_coordination_error() -> None:
    interaction = SimpleNamespace(action_execution_id="actual-menu-action-error")

    class Coordinator:
        async def coordinate(self, command):
            del command
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.AFTER_CAPTURE,
                error_code="CAPTURE_AFTER_FAILED",
                message="动作已执行但后采集失败",
                interaction=interaction,
            )

    class UrlReader:
        async def read_url(self, *, capture_id):
            if capture_id == "capture-home":
                return "https://hl365.com/"
            raise AssertionError(capture_id)

    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = WebNavigationCandidate(
        entry_id="runtime-1",
        label=entry.menu_path[-1],
        url=entry.canonical_url,
        menu_path=entry.menu_path,
        dom_fingerprint="dom-1",
        source_signal="semantic_nav",
        page_type_key="nav-class:test",
    )

    result = await WebDesktopMenuPageVisitor(
        coordinator=Coordinator(),
        capture_url_reader=UrlReader(),
    ).visit(
        command=RunWebWorkflowCommand(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            session_owner="web-v2-workflow",
            target_url="https://hl365.com/",
            browser_profile_id="desktop_fhd_1920x1080_v1",
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        entry=entry,
        candidate=candidate,
        source_capture_id="capture-home",
        menu_intent_id="web-menu:reserved-entry",
    )

    assert result.status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert result.menu_intent_id == "web-menu:reserved-entry"
    assert result.menu_action_execution_id == "actual-menu-action-error"


@pytest.mark.parametrize("failure", ("missing_after_capture", "read_url"))
@pytest.mark.asyncio
async def test_menu_action_execution_survives_post_coordinate_failure(failure: str) -> None:
    class Coordinator:
        async def coordinate(self, command):
            del command
            return SimpleNamespace(
                after_capture=(
                    None
                    if failure == "missing_after_capture"
                    else SimpleNamespace(capture_id="capture-menu-after")
                ),
                interaction=SimpleNamespace(
                    execution_outcome="succeeded",
                    status="succeeded",
                    action_execution_id="actual-menu-action-post",
                ),
            )

    class UrlReader:
        async def read_url(self, *, capture_id):
            if capture_id == "capture-home":
                return "https://hl365.com/"
            assert capture_id == "capture-menu-after"
            raise RuntimeError("url read failed")

    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = WebNavigationCandidate(
        entry_id="runtime-post-coordinate",
        label=entry.menu_path[-1],
        url=entry.canonical_url,
        menu_path=entry.menu_path,
        dom_fingerprint="dom-post-coordinate",
        source_signal="semantic_nav",
        page_type_key="nav-class:test",
    )

    result = await WebDesktopMenuPageVisitor(
        coordinator=Coordinator(),
        capture_url_reader=UrlReader(),
    ).visit(
        command=RunWebWorkflowCommand(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            session_owner="web-v2-workflow",
            target_url="https://hl365.com/",
            browser_profile_id="desktop_fhd_1920x1080_v1",
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        entry=entry,
        candidate=candidate,
        source_capture_id="capture-home",
        menu_intent_id="web-menu:reservation-not-action-id",
    )

    assert result.status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert result.menu_intent_id == "web-menu:reservation-not-action-id"
    assert result.menu_action_execution_id == "actual-menu-action-post"
    assert result.final_capture_id == (
        None if failure == "missing_after_capture" else "capture-menu-after"
    )


@pytest.mark.parametrize("failure", (None, "coordination_error", "read_url"))
@pytest.mark.asyncio
async def test_desktop_explorer_persists_probe_only_action_and_capture_lineage(
    failure: str | None,
) -> None:
    registry = WebSiteMenuRegistry()
    site = registry.resolve_site("https://hl365.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = WebNavigationCandidate(
        entry_id="runtime-wrapper",
        label=entry.menu_path[-1],
        url=entry.canonical_url,
        menu_path=entry.menu_path,
        dom_fingerprint="dom-wrapper",
        source_signal="semantic_nav",
        page_type_key="nav-class:test",
    )
    coordinated: list[object] = []

    class Coordinator:
        async def coordinate(self, command):
            coordinated.append(command)
            if failure == "coordination_error":
                raise CaptureCoordinationError(
                    stage=CaptureCoordinationStage.AFTER_CAPTURE,
                    error_code="CAPTURE_AFTER_FAILED",
                    message="动作已执行但证据附着失败",
                    interaction=SimpleNamespace(
                        execution_outcome="succeeded",
                        status="succeeded",
                        action_execution_id="actual-wrapper-action",
                    ),
                    after_capture=SimpleNamespace(capture_id="capture-wrapper"),
                )
            return SimpleNamespace(
                after_capture=SimpleNamespace(capture_id="capture-wrapper"),
                interaction=SimpleNamespace(
                    execution_outcome="succeeded",
                    status="succeeded",
                    action_execution_id="actual-wrapper-action",
                ),
            )

    class CandidateSource:
        def navigation_candidates(self, session_id):
            assert session_id == "web:run-1"
            return (candidate,)

    class UrlReader:
        async def read_url(self, *, capture_id):
            if capture_id == "capture-home":
                return "https://hl365.com/"
            assert capture_id == "capture-wrapper"
            if failure == "read_url":
                raise RuntimeError("capture url read failed")
            return entry.canonical_url

    class Ledger:
        record = None

        async def claim_menu(self, *, command, entry, source_capture_id):
            del source_capture_id
            return WebMenuActionReservation(
                site_id=site.site_id,
                menu_entry_id=entry.entry_id,
                execution_slot=1,
                intent_id=f"web-menu:{command.agent_run_id}:{site.site_id}:{entry.entry_id}",
                already_reserved=False,
            )

        async def finalize(self, record):
            if record.menu_entry_id != entry.entry_id:
                assert record.status is WebMenuTerminalStatus.ALLOWLIST_DRIFTED
                assert record.menu_action_execution_id is None
                return
            assert record.status is WebMenuTerminalStatus.NAVIGATION_FAILED
            assert (
                record.diagnostic["error_code"]
                == {
                    None: "WEB_MENU_PIPELINE_INCOMPLETE",
                    "coordination_error": "CAPTURE_AFTER_FAILED",
                    "read_url": "WEB_MENU_POST_NAVIGATION_FAILED",
                }[failure]
            )
            assert record.menu_probe_capture_id == "capture-wrapper"
            assert record.menu_ready_capture_id is None
            assert record.menu_page_type is None
            assert record.menu_action_execution_id == "actual-wrapper-action"
            self.record = record

        async def read_summary(self, *, agent_run_id, site_id):
            assert agent_run_id == "run-1"
            assert site_id == site.site_id
            return WebMenuExplorationSummary(
                site_id=site.site_id,
                catalog_version=registry.version,
                catalog_sha256=registry.catalog_content_sha256,
                menu_total=len(site.entries),
                menu_ledger=tuple(
                    WebMenuLedgerEntry(
                        entry_id=item.entry_id,
                        menu_path=item.menu_path,
                        canonical_url=(
                            "sha256:" + sha256(item.canonical_url.encode("utf-8")).hexdigest()
                        ),
                        status=(
                            self.record.status
                            if item.entry_id == entry.entry_id and self.record is not None
                            else WebMenuTerminalStatus.NAVIGATION_FAILED
                        ),
                        source_capture_id="capture-home",
                        final_capture_id=(
                            self.record.menu_ready_capture_id
                            if item.entry_id == entry.entry_id and self.record is not None
                            else None
                        ),
                        page_type=(
                            self.record.menu_page_type
                            if item.entry_id == entry.entry_id and self.record is not None
                            else None
                        ),
                        detail_capture_id=None,
                        error_code=None,
                        menu_action_count=int(item.entry_id == entry.entry_id),
                        menu_intent_id=(
                            f"web-menu:run-1:{site.site_id}:{entry.entry_id}"
                            if item.entry_id == entry.entry_id
                            else None
                        ),
                        menu_action_execution_id=(
                            self.record.menu_action_execution_id
                            if item.entry_id == entry.entry_id and self.record is not None
                            else None
                        ),
                    )
                    for item in site.entries
                ),
                page_timeline=("capture-wrapper",),
                page_type_keys=(),
            )

    ledger = Ledger()
    summary = await WebDesktopNavigationExplorer(
        coordinator=Coordinator(),
        candidate_source=CandidateSource(),
        capture_url_reader=UrlReader(),
        navigation_context=SimpleNamespace(
            discovery_policy=WebDiscoveryPolicy(),
            version=registry.version,
            catalog_content_sha256=registry.catalog_content_sha256,
            resolve_site_context=registry.resolve_site_context,
        ),
        budget_ledger=ledger,
    ).explore(
        RunWebWorkflowCommand(
            task_id="task-1",
            agent_run_id="run-1",
            session_id="web:run-1",
            session_owner="web-v2-workflow",
            target_url="https://hl365.com/",
            browser_profile_id="desktop_fhd_1920x1080_v1",
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        initial_capture_id="capture-home",
    )

    persisted = next(item for item in summary.menu_ledger if item.entry_id == entry.entry_id)
    assert len(coordinated) == 1
    assert coordinated[0].intent.intent_id.startswith("web-menu:run-1:")
    assert ledger.record.status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert ledger.record.menu_probe_capture_id == "capture-wrapper"
    assert ledger.record.menu_action_execution_id == "actual-wrapper-action"
    assert persisted.menu_action_execution_id == "actual-wrapper-action"


@pytest.mark.asyncio
async def test_http_catalog_uses_reconciled_https_runtime_candidate() -> None:
    coordinated = []

    class Coordinator:
        async def coordinate(self, command):
            coordinated.append(command)
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="capture-menu"))

    class UrlReader:
        async def read_url(self, *, capture_id):
            del capture_id
            return candidate.url

    site = WebSiteMenuRegistry().resolve_site("http://91porna.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = WebNavigationCandidate(
        entry_id="runtime-upgraded",
        label=entry.menu_path[-1],
        url=entry.canonical_url.replace("http://", "https://", 1),
        menu_path=entry.menu_path,
        dom_fingerprint="dom-upgraded",
        source_signal="semantic_nav",
        page_type_key="nav-class:test",
    )
    command = RunWebWorkflowCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        session_owner="web-v2-workflow",
        target_url="http://91porna.com/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    result = await WebDesktopMenuPageVisitor(
        coordinator=Coordinator(),
        capture_url_reader=UrlReader(),
    ).visit(
        command=command,
        entry=entry,
        candidate=candidate,
        source_capture_id="capture-home",
        menu_intent_id="web-menu:run-1:91porna:entry",
    )

    assert coordinated[0].intent.parameters["destination_url"] == candidate.url
    assert result.status is WebMenuTerminalStatus.MENU_PAGE_CAPTURED


@pytest.mark.asyncio
async def test_cross_origin_redirect_is_terminal_before_readiness() -> None:
    coordinated = []

    class Coordinator:
        async def coordinate(self, command):
            coordinated.append(command)
            capture_id = "capture-cross-origin" if len(coordinated) == 1 else "capture-recovered"
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id=capture_id))

    class UrlReader:
        async def read_url(self, *, capture_id):
            return {
                "capture-home": "https://hl365.com/",
                "capture-cross-origin": "https://ads.example/landing",
                "capture-recovered": "https://hl365.com/",
            }[capture_id]

    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = WebNavigationCandidate(
        entry_id="runtime-1",
        label=entry.menu_path[-1],
        url=entry.canonical_url,
        menu_path=entry.menu_path,
        dom_fingerprint="dom-1",
        source_signal="semantic_nav",
        page_type_key="nav-class:test",
    )
    command = RunWebWorkflowCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        session_owner="web-v2-workflow",
        target_url="https://hl365.com/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    result = await WebDesktopMenuPageVisitor(
        coordinator=Coordinator(),
        capture_url_reader=UrlReader(),
    ).visit(
        command=command,
        entry=entry,
        candidate=candidate,
        source_capture_id="capture-home",
        menu_intent_id="web-menu:run-1:hl365:entry",
    )

    assert result.status is WebMenuTerminalStatus.UNSAFE_NAVIGATION
    assert result.error_code == "WEB_NAVIGATION_CROSS_ORIGIN"
    assert result.final_capture_id == "capture-cross-origin"
    assert result.navigation_current_capture_id == "capture-recovered"
    assert result.navigation_handoff_error is None
    assert len(coordinated) == 2
    recovery_intent = coordinated[1].intent
    assert recovery_intent.source_capture_id == "capture-cross-origin"
    assert recovery_intent.parameters["destination_url"] == "https://hl365.com/"


@pytest.mark.asyncio
async def test_cross_origin_recovery_retries_transient_capture_evidence_visibility() -> None:
    coordinated = []
    recovery_reads = 0

    class Coordinator:
        async def coordinate(self, command):
            coordinated.append(command)
            capture_id = "capture-cross-origin" if len(coordinated) == 1 else "capture-recovered"
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id=capture_id))

    class UrlReader:
        async def read_url(self, *, capture_id):
            nonlocal recovery_reads
            if capture_id == "capture-home":
                return "https://hl365.com/"
            if capture_id == "capture-cross-origin":
                return "https://ads.example/landing"
            recovery_reads += 1
            if recovery_reads == 1:
                raise RuntimeError("WEB_CAPTURE_EVIDENCE_MISSING")
            return "https://hl365.com/"

    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = WebNavigationCandidate(
        entry_id="runtime-transient-recovery",
        label=entry.menu_path[-1],
        url=entry.canonical_url,
        menu_path=entry.menu_path,
        dom_fingerprint="dom-transient-recovery",
        source_signal="semantic_nav",
        page_type_key="nav-class:test",
    )
    command = RunWebWorkflowCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        session_owner="web-v2-workflow",
        target_url="https://hl365.com/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    result = await WebDesktopMenuPageVisitor(
        coordinator=Coordinator(),
        capture_url_reader=UrlReader(),
    ).visit(
        command=command,
        entry=entry,
        candidate=candidate,
        source_capture_id="capture-home",
        menu_intent_id="web-menu:run-1:hl365:transient-recovery",
    )

    assert result.navigation_current_capture_id == "capture-recovered"
    assert result.navigation_handoff_error is None
    assert recovery_reads == 2


@pytest.mark.asyncio
async def test_recovery_url_read_fails_closed_after_three_missing_evidence_reads() -> None:
    read_count = 0

    class UrlReader:
        async def read_url(self, *, capture_id):
            nonlocal read_count
            assert capture_id == "capture-recovered"
            read_count += 1
            raise RuntimeError("WEB_CAPTURE_EVIDENCE_MISSING")

    with pytest.raises(RuntimeError, match="^WEB_CAPTURE_EVIDENCE_MISSING$"):
        await _read_recovery_url(UrlReader(), capture_id="capture-recovered")

    assert read_count == 3


@pytest.mark.asyncio
async def test_recovery_url_read_does_not_retry_other_runtime_error() -> None:
    read_count = 0

    class UrlReader:
        async def read_url(self, *, capture_id):
            nonlocal read_count
            assert capture_id == "capture-recovered"
            read_count += 1
            raise RuntimeError("WEB_CAPTURE_CHECKSUM_MISMATCH")

    with pytest.raises(RuntimeError, match="^WEB_CAPTURE_CHECKSUM_MISMATCH$"):
        await _read_recovery_url(UrlReader(), capture_id="capture-recovered")

    assert read_count == 1


@pytest.mark.asyncio
async def test_cross_origin_recovery_keeps_current_untrusted_when_restore_is_unsafe() -> None:
    class Coordinator:
        call_count = 0

        async def coordinate(self, command):
            del command
            self.call_count += 1
            capture_id = (
                "capture-cross-origin" if self.call_count == 1 else "capture-recovery-cross-origin"
            )
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id=capture_id))

    class UrlReader:
        async def read_url(self, *, capture_id):
            return {
                "capture-home": "https://hl365.com/",
                "capture-cross-origin": "https://ads.example/landing",
                "capture-recovery-cross-origin": "https://ads.example/still-away",
            }[capture_id]

    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")
    entry = next(
        item for item in site.entries if item.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
    )
    candidate = WebNavigationCandidate(
        entry_id="runtime-unsafe-recovery",
        label=entry.menu_path[-1],
        url=entry.canonical_url,
        menu_path=entry.menu_path,
        dom_fingerprint="dom-unsafe-recovery",
        source_signal="semantic_nav",
        page_type_key="nav-class:test",
    )
    command = RunWebWorkflowCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        session_owner="web-v2-workflow",
        target_url="https://hl365.com/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    result = await WebDesktopMenuPageVisitor(
        coordinator=Coordinator(),
        capture_url_reader=UrlReader(),
    ).visit(
        command=command,
        entry=entry,
        candidate=candidate,
        source_capture_id="capture-home",
        menu_intent_id="web-menu:run-1:hl365:unsafe-recovery",
    )

    assert result.status is WebMenuTerminalStatus.UNSAFE_NAVIGATION
    assert result.error_code == "WEB_NAVIGATION_CROSS_ORIGIN"
    assert result.final_capture_id == "capture-cross-origin"
    assert result.navigation_current_capture_id is None
    assert result.navigation_handoff_error == "WEB_NAVIGATION_CURRENT_UNTRUSTED"
