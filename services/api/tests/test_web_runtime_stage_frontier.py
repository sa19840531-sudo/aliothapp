"""持久候选恢复与阶段化菜单导航，不调用整页 visitor。"""

from hashlib import sha256
from types import SimpleNamespace

import pytest

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_menu_exploration import WebMenuVisitResult
from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.tests.test_web_runtime_stage_effects import CAPTURE, command, cursor


@pytest.mark.parametrize("status,raw,expected", [
    (WebMenuTerminalStatus.NAVIGATION_FAILED, "before_capture_failed",
     "WEB_MENU_NAVIGATION_BEFORE_CAPTURE_FAILED"),
    (WebMenuTerminalStatus.NAVIGATION_FAILED, "after_capture_failed",
     "WEB_MENU_NAVIGATION_AFTER_CAPTURE_FAILED"),
    (WebMenuTerminalStatus.UNSAFE_NAVIGATION, "unsafe_navigation",
     "WEB_MENU_NAVIGATION_COORDINATION_FAILED"),
    (WebMenuTerminalStatus.NAVIGATION_FAILED, "WEB_CAPTURE_TIMEOUT", "WEB_CAPTURE_TIMEOUT"),
    (WebMenuTerminalStatus.NAVIGATION_FAILED, "bad https://host/?token=secret",
     "WEB_MENU_NAVIGATION_COORDINATION_FAILED"),
])
async def test_navigation_error_can_be_persisted_as_stage_gap(status, raw, expected):
    """底层错误必须保留诊断且不能使阶段缺口保存再次失败。"""
    class Navigator:
        async def visit(self, **kwargs):
            return WebMenuVisitResult(final_capture_id=None, page_type=None,
                                      status=status, error_code=raw)

    frontier = WebRuntimeStageFrontier(
        ledger=Ledger(), candidate_source=None, navigator=Navigator(),
        site=SimpleNamespace(site_id="runtime", origin="https://example.test/"),
    )
    with pytest.raises(WebStageFailure) as caught:
        await frontier.navigate(command(), candidate=candidate("broken"), source_capture_id=CAPTURE)
    failure = caught.value
    assert failure.code == expected
    assert failure.transient is (status is WebMenuTerminalStatus.NAVIGATION_FAILED)
    assert "secret" not in str(failure.detail)
    assert failure.detail["raw_error_code"] == (raw if " " not in raw else
        "sha256_" + sha256(raw.encode()).hexdigest())
    saved = cursor(WebRunStage.BOOTSTRAP_NAVIGATION).record_gap_and_advance(
        error_code=failure.code, detail=failure.detail,
    )
    assert saved.last_error_code == expected


def candidate(label):
    return WebNavigationCandidate(
        entry_id=label,
        label=label,
        url=f"https://example.test/{label}",
        menu_path=(label,),
        dom_fingerprint=f"dom:{label}",
        source_signal="href",
        page_type_key="content_list",
        source_capture_id=CAPTURE,
        source_page_url="https://example.test/",
    )


class Ledger:
    def __init__(self):
        self.candidates = [candidate("broken"), candidate("healthy")]

    async def read_summary(self, **_kwargs):
        return SimpleNamespace(
            runtime_discovered_ledger=tuple(
                SimpleNamespace(
                    entry_id="discovery:" + item.logical_target_identity,
                    status=WebMenuTerminalStatus.RUNTIME_DISCOVERED_UNREVIEWED,
                    execution=None,
                    restored_navigation_candidate=lambda item=item: item,
                )
                for item in self.candidates
            )
        )

    async def claim_menu(self, *, command, entry, source_capture_id):
        return SimpleNamespace(intent_id=entry.entry_id, already_reserved=False)

    async def record_discovery(self, **kwargs):
        self.recorded = kwargs


async def test_frontier_next_candidate_skips_already_checkpointed_url_after_tls_gap():
    ledger = Ledger()
    frontier = WebRuntimeStageFrontier(
        ledger=ledger,
        candidate_source=None,
        navigator=None,
        site=SimpleNamespace(site_id="runtime", origin="https://example.test/"),
    )
    first = await frontier.next_candidate(command(), excluded_entry_ids=())
    second = await frontier.next_candidate(command(), excluded_entry_ids=(first[0],))
    assert first[1].url == "https://example.test/broken"
    assert second[1].url == "https://example.test/healthy"


async def test_frontier_stops_scheduling_when_persisted_menu_limit_is_reached():
    class Budget(Ledger):
        closeout_called = False

        async def read_summary(self, **kwargs):
            summary = await super().read_summary(**kwargs)
            return SimpleNamespace(
                **vars(summary), menu_limit=2, executed_same_origin_count=2
            )

        async def closeout_unexecuted(self, *, command, site_id):
            self.closeout_called = True

    ledger = Budget()
    frontier = WebRuntimeStageFrontier(
        ledger=ledger,
        candidate_source=None,
        navigator=None,
        site=SimpleNamespace(site_id="runtime", origin="https://example.test/"),
    )
    assert await frontier.next_candidate(command(), excluded_entry_ids=()) is None
    assert ledger.closeout_called


async def test_navigation_transient_result_is_retried_without_entering_readiness():
    class Navigator:
        async def visit(self, **_kwargs):
            return WebMenuVisitResult(
                final_capture_id=None,
                page_type=None,
                status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                error_code="WEB_GOTO_TLS_FAILED",
            )

    ledger = Ledger()
    frontier = WebRuntimeStageFrontier(
        ledger=ledger,
        candidate_source=None,
        navigator=Navigator(),
        site=SimpleNamespace(site_id="runtime", origin="https://example.test/"),
    )
    item = ledger.candidates[0]
    with pytest.raises(WebStageFailure) as error:
        await frontier.navigate(command(), candidate=item, source_capture_id=CAPTURE)
    assert error.value.transient
    assert error.value.code == "WEB_GOTO_TLS_FAILED"


async def test_navigation_claim_uses_current_capture_after_discovery_source_scroll():
    class RecordingLedger(Ledger):
        async def claim_menu(self, *, command, entry, source_capture_id):
            self.claim_source = source_capture_id
            return SimpleNamespace(intent_id=entry.entry_id, already_reserved=False)

    class Evidence:
        async def current_capture_reference(self):
            return {"capture_id": "capture-after-scroll"}

    class Navigator:
        async def visit(self, **kwargs):
            self.source = kwargs["source_capture_id"]
            return WebMenuVisitResult(
                final_capture_id="menu-capture",
                page_type="content_list",
                status=WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
            )

    ledger = RecordingLedger()
    navigator = Navigator()
    frontier = WebRuntimeStageFrontier(
        ledger=ledger,
        candidate_source=None,
        navigator=navigator,
        site=SimpleNamespace(site_id="runtime", origin="https://example.test/"),
        evidence=Evidence(),
    )
    result = await frontier.navigate(
        command(), candidate=ledger.candidates[0], source_capture_id=CAPTURE
    )
    assert result.final_capture_id == "menu-capture"
    assert ledger.claim_source == "capture-after-scroll"
    assert navigator.source == "capture-after-scroll"


async def test_frontier_restores_exact_candidate_from_persisted_manifest():
    ledger = Ledger()
    frontier = WebRuntimeStageFrontier(
        ledger=ledger,
        candidate_source=None,
        navigator=None,
        site=SimpleNamespace(site_id="runtime", origin="https://example.test/"),
    )
    result = await frontier.get_candidate(
        command(), entry_id="discovery:" + ledger.candidates[1].logical_target_identity
    )
    assert result.url == "https://example.test/healthy"


async def test_discovery_binds_current_capture_before_reading_trusted_candidates():
    ledger = Ledger()

    class Source:
        def bind_latest_navigation_capture(self, session_id, capture_id):
            self.bound = capture_id

        async def navigation_candidates(self, session_id):
            assert self.bound == CAPTURE
            return (ledger.candidates[0],)

    frontier = WebRuntimeStageFrontier(
        ledger=ledger,
        candidate_source=Source(),
        navigator=None,
        site=SimpleNamespace(site_id="runtime", origin="https://example.test/"),
    )
    await frontier.discover(command(), source_capture_id=CAPTURE)
    assert ledger.recorded["candidate"].url == "https://example.test/broken"
    assert ledger.recorded["source_capture_id"] == CAPTURE


async def test_reconcile_navigation_gap_closes_only_failed_menu_ledger():
    from dataclasses import replace

    records = []

    class Budget:
        async def finalize(self, record):
            records.append(record)

        async def read_summary(self, **kwargs):
            return SimpleNamespace(runtime_discovered_ledger=())

    frontier = WebRuntimeStageFrontier(
        ledger=Budget(),
        candidate_source=None,
        navigator=None,
        site=SimpleNamespace(site_id="runtime", origin="https://example.test/"),
    )
    state = cursor(WebRunStage.BOOTSTRAP_NAVIGATION).record_gap_and_advance(
        error_code="WEB_GOTO_TLS_FAILED", gap_code="WEB_NAVIGATION_RETRY_EXHAUSTED"
    )
    state = replace(
        state,
        logical_page_key="child",
        cursor_payload={
            **state.cursor_payload,
            "menu_ledger_id": "failed-menu",
        },
    )
    await frontier.reconcile(command(), state)
    assert records[0].menu_entry_id == "failed-menu"
    assert records[0].status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert records[0].diagnostic["error_code"] == "WEB_NAVIGATION_RETRY_EXHAUSTED"


async def test_home_reconciliation_uses_persisted_classification_and_seals_budget():
    records = []

    class Budget:
        async def finalize(self, record):
            records.append(record)

        async def seal(self, **_kwargs):
            records.append("sealed")

    class Evidence:
        async def capture_outcome(self, *, capture_id):
            return {"page_type": "home", "action_execution_id": None}

    from services.api.app.domain.page import WebMenuDecision

    site = SimpleNamespace(
        site_id="runtime",
        origin="https://example.test/",
        entries=(SimpleNamespace(entry_id="home", decision=WebMenuDecision.HOME_BASELINE),),
    )
    frontier = WebRuntimeStageFrontier(
        ledger=Budget(), candidate_source=None, navigator=None, site=site, evidence=Evidence()
    )
    await frontier.reconcile(command(), cursor(WebRunStage.EVIDENCE_RECONCILIATION))
    assert records[0].status is WebMenuTerminalStatus.HOME_BASELINE
    assert records[0].menu_ready_capture_id == CAPTURE
    assert records[-1] == "sealed"


@pytest.mark.parametrize(
    "root,page_type,want",
    [
        (True, "home", "home_baseline"),
        (True, "content_list", "page_type_unclassified"),
        (True, None, "page_type_unclassified"),
        (False, "content_detail", "detail_captured"),
        (False, "video_detail", "detail_captured"),
        (False, "content_list", "menu_page_captured"),
        (False, None, "page_type_unclassified"),
    ],
)
async def test_reconciliation_preserves_home_and_direct_menu_terminal_contract(
    root, page_type, want
):
    from dataclasses import replace

    from services.api.app.domain.page import WebMenuDecision

    records = []

    class Budget:
        async def read_summary(self, **kwargs):
            return SimpleNamespace(runtime_discovered_ledger=())

        async def finalize(self, record):
            records.append(record)

        async def seal(self, **kwargs):
            pass

    class Evidence:
        async def capture_outcome(self, *, capture_id):
            return {
                "page_type": page_type,
                "action_execution_id": "00000000-0000-0000-0000-000000000099",
            }

    site = SimpleNamespace(
        site_id="runtime",
        origin="https://example.test/",
        entries=(SimpleNamespace(entry_id="home", decision=WebMenuDecision.HOME_BASELINE),),
    )
    state = cursor(WebRunStage.EVIDENCE_RECONCILIATION)
    if not root:
        state = replace(
            state,
            logical_page_key="child",
            cursor_payload={**state.cursor_payload, "menu_ledger_id": "child"},
        )
    await WebRuntimeStageFrontier(
        ledger=Budget(), candidate_source=None, navigator=None, site=site, evidence=Evidence()
    ).reconcile(command(), state)
    record = records[0]
    assert record.status.value == want
    assert record.menu_action_execution_id == (
        None if root else "00000000-0000-0000-0000-000000000099"
    )
    assert record.menu_page_type == (None if want == "page_type_unclassified" else page_type)
    if want == "page_type_unclassified":
        assert record.diagnostic["error_code"] == "WEB_PAGE_TYPE_UNCLASSIFIED"


@pytest.mark.parametrize("failed", [False, True])
async def test_stage_home_terminal_reaches_real_ledger_sql_with_zero_action_contract(failed):
    from services.api.app.domain.page import WebMenuDecision
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        PostgresWebExplorationBudgetLedgerRepository,
    )
    from services.api.tests.test_web_exploration_budget_ledger_repository import (
        FakeConnection,
        FakePool,
    )

    connection = FakeConnection(fetchrow_results=[{"status": "home_baseline"}])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    class Budget:
        async def finalize(self, record):
            await repository.finalize(record)

        async def seal(self, **kwargs):
            pass

    class Evidence:
        async def capture_outcome(self, *, capture_id):
            return {
                "page_type": "home",
                "action_execution_id": "00000000-0000-0000-0000-000000000099",
            }

    site = SimpleNamespace(
        site_id="runtime",
        entries=(SimpleNamespace(entry_id="home", decision=WebMenuDecision.HOME_BASELINE),),
    )
    current = cursor(WebRunStage.EVIDENCE_RECONCILIATION)
    if failed:
        current = cursor(WebRunStage.BOOTSTRAP_NAVIGATION).record_gap_and_advance(
            error_code="WEB_GOTO_TLS_FAILED"
        )
    await WebRuntimeStageFrontier(
        ledger=Budget(), candidate_source=None, navigator=None, site=site, evidence=Evidence()
    ).reconcile(command(), current)
    _, sql, args = connection.calls[0]
    assert args[5] == ("navigation_failed" if failed else "home_baseline")
    assert args[6] is None
    assert "$7::varchar IS NULL" in sql
    assert "locked.menu_action_count = 0" in sql
    assert args[14] == "{}"
    if failed:
        assert "checkpoint.logical_page_key = 'home'" in sql
        assert "gap->>'code' = $16::jsonb->>'error_code'" in sql
