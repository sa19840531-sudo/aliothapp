"""Web V2 持久化硬预算仓储测试。"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import ModuleType
from typing import Any

import pytest

from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
    WebMenuLedgerTerminalRecord,
)
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.application.web_logical_page import canonical_web_logical_page_url
from services.api.app.application.web_menu_exploration import (
    WEB_MENU_COLLECTION_LIMIT,
)
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.page import (
    WebCatalogStatus,
    WebMenuDecision,
    WebSiteMenuCatalog,
    WebSiteMenuEntry,
    WebSiteMenuRegistry,
)
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
    _catalog_payload,
)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeConnection:
    """用排队结果模拟 asyncpg，并保留真实 SQL 与参数供行为断言。"""

    def __init__(
        self,
        *,
        fetchrow_results: list[dict[str, Any] | None] | None = None,
        fetch_results: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self.fetchrow_results = list(fetchrow_results or [])
        self.fetch_results = list(fetch_results or [])
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchrow(self, query: str, *args: Any):
        self.calls.append(("fetchrow", " ".join(query.split()), args))
        if not self.fetchrow_results:
            raise AssertionError("fetchrow 没有预设结果")
        return self.fetchrow_results.pop(0)

    async def fetch(self, query: str, *args: Any):
        self.calls.append(("fetch", " ".join(query.split()), args))
        if not self.fetch_results:
            raise AssertionError("fetch 没有预设结果")
        return self.fetch_results.pop(0)


class StatefulBootstrapTransaction:
    def __init__(self, connection: StatefulBootstrapConnection) -> None:
        self.connection = connection
        self.original_rows: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> None:
        self.original_rows = {
            key: dict(value) for key, value in self.connection.persisted_rows.items()
        }

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            self.connection.persisted_rows = self.original_rows


class StatefulBootstrapConnection:
    """模拟 PostgreSQL upsert 对既有账本集合的真实写入副作用。"""

    def __init__(
        self,
        *,
        budget: dict[str, Any],
        persisted_rows: list[dict[str, Any]],
    ) -> None:
        self.budget = dict(budget)
        self.persisted_rows = {str(row["menu_entry_id"]): dict(row) for row in persisted_rows}
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.entry_mutation_count = 0

    def transaction(self) -> StatefulBootstrapTransaction:
        return StatefulBootstrapTransaction(self)

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "INSERT INTO qc_web_exploration_budget" in normalized:
            return dict(self.budget)
        if "FROM qc_surface_session AS session" in normalized:
            return {"task_id": self.budget["task_id"], "session_id": self.budget["session_id"]}
        if "FROM qc_web_exploration_budget AS budget" in normalized:
            return dict(self.budget)
        raise AssertionError(f"未建模的 fetchrow SQL: {normalized}")

    async def fetch(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetch", normalized, args))
        if "INSERT INTO qc_web_menu_ledger_entry" in normalized:
            self.entry_mutation_count += 1
            for item in json.loads(args[2]):
                self.persisted_rows.setdefault(str(item["menu_entry_id"]), dict(item))
            return list(self.persisted_rows.values())
        if "FROM qc_web_menu_ledger_entry AS entry" in normalized:
            return list(self.persisted_rows.values())
        raise AssertionError(f"未建模的 fetch SQL: {normalized}")


def _expected_materialized_rows(
    site: WebSiteMenuCatalog,
    *,
    menu_limit: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    canonical_primary = {
        canonical_web_logical_page_url(entry.canonical_url): entry.entry_id
        for entry in site.entries
        if entry.decision is WebMenuDecision.HOME_BASELINE
    }
    selectable: list[str] = []
    alias_of: dict[str, str] = {}
    for entry in site.entries:
        if (
            entry.decision is not WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
            or not entry.is_same_origin
            or not entry.collection_eligibility.eligible
            or entry.status is WebCatalogStatus.DEPRECATED
        ):
            continue
        canonical = canonical_web_logical_page_url(entry.canonical_url)
        primary = canonical_primary.get(canonical)
        if primary is not None:
            alias_of[entry.entry_id] = primary
            continue
        canonical_primary[canonical] = entry.entry_id
        selectable.append(entry.entry_id)
    selected_slots = {
        entry_id: slot
        for slot, entry_id in enumerate(selectable[:menu_limit], start=1)
    }

    for position, entry in enumerate(site.entries):
        execution_slot = selected_slots.get(entry.entry_id)
        diagnostic: dict[str, object] = {}
        if execution_slot is not None:
            status = "pending"
        elif entry.decision is WebMenuDecision.HOME_BASELINE:
            status = "home_pending"
        elif entry.decision is WebMenuDecision.BLOCKED_CROSS_ORIGIN:
            status = WebMenuTerminalStatus.CROSS_ORIGIN_BLOCKED.value
            diagnostic = {"error_code": "WEB_MENU_CROSS_ORIGIN_BLOCKED"}
        else:
            status = WebMenuTerminalStatus.MENU_LIMIT_SKIPPED.value
            if entry.entry_id in alias_of:
                diagnostic = {
                    "error_code": "WEB_MENU_LOGICAL_ALIAS",
                    "alias_of": alias_of[entry.entry_id],
                }
            elif entry.status is WebCatalogStatus.DEPRECATED:
                diagnostic = {
                    "error_code": "WEB_MENU_CATALOG_DEPRECATED",
                    "catalog_status": entry.status.value,
                }
            elif not entry.collection_eligibility.eligible:
                diagnostic = {"error_code": "WEB_MENU_COLLECTION_INELIGIBLE"}
            else:
                diagnostic = {"error_code": "WEB_MENU_LIMIT_SKIPPED"}
        rows.append(
            {
                "menu_entry_id": entry.entry_id,
                "catalog_position": position,
                "decision": entry.decision.value,
                "menu_path": list(entry.menu_path),
                "canonical_url_sha256": sha256(entry.canonical_url.encode("utf-8")).hexdigest(),
                "execution_slot": execution_slot,
                "status": status,
                "diagnostic": diagnostic,
            }
        )
    return rows


def _command(
    *,
    session_id: str = "web:run-1",
    session_owner: str = "web-v2-workflow",
) -> RunWebWorkflowCommand:
    return RunWebWorkflowCommand(
        task_id="00000000-0000-0000-0000-000000000001",
        agent_run_id="00000000-0000-0000-0000-000000000002",
        session_id=session_id,
        session_owner=session_owner,
        target_url="https://example.test/",
        browser_profile_id="web-desktop-chromium-v1",
        deadline=datetime.now(UTC) + timedelta(minutes=10),
    )


def _entry(
    index: int,
    *,
    decision: WebMenuDecision = WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY,
    is_same_origin: bool = True,
) -> WebSiteMenuEntry:
    return WebSiteMenuEntry(
        entry_id=f"entry-{index}",
        menu_path=("一级", f"菜单 {index}"),
        href=f"/menu/{index}",
        canonical_url=(
            f"https://example.test/menu/{index}"
            if is_same_origin
            else f"https://outside.test/menu/{index}"
        ),
        decision=decision,
        is_same_origin=is_same_origin,
    )


def _site() -> WebSiteMenuCatalog:
    return WebSiteMenuCatalog(
        site_id="example",
        origin="https://example.test",
        navigation_evidence={"active_anchor_count": 13},
        entries=(
            _entry(0, decision=WebMenuDecision.HOME_BASELINE),
            *(_entry(index) for index in range(1, 12)),
            _entry(
                12,
                decision=WebMenuDecision.BLOCKED_CROSS_ORIGIN,
                is_same_origin=False,
            ),
        ),
    )


def _site_with_entries(entries: tuple[WebSiteMenuEntry, ...]) -> WebSiteMenuCatalog:
    return WebSiteMenuCatalog(
        site_id="example",
        origin="https://example.test",
        navigation_evidence={"active_anchor_count": len(entries)},
        entries=entries,
    )


def _budget_row() -> dict[str, Any]:
    return {
        "task_id": _command().task_id,
        "agent_run_id": _command().agent_run_id,
        "session_id": _command().session_id,
        "site_id": "example",
        "surface_kind": "web",
        "session_owner": "web-v2-workflow",
        "target_origin_sha256": sha256(b"https://example.test").hexdigest(),
        "catalog_version": "catalog-v1",
        "catalog_sha256": "a" * 64,
        "menu_limit": None,
        "state": "open",
    }


def _frozen_budget_policy(menu_limit=10):
    from services.api.app.application.web_run_policy import (
        load_published_web_policy,
        materialize_web_policy,
    )
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot

    body = load_published_web_policy(
        now=datetime.now(UTC),
        max_duration_seconds=600,
        exploration_target={
            "required_page_types": ["content_list", "content_detail"],
            "discovery_policy": {
                "enabled": True,
                "max_unlisted_menus": 2,
                "same_origin_only": True,
            },
        },
    )
    body["budget"]["menu_limit"] = menu_limit
    body["discovery_policy"]["max_unlisted_menus"] = 2
    return materialize_web_policy(
        WebRunPolicySnapshot.create(
            task_id=_command().task_id,
            agent_run_id=_command().agent_run_id,
            payload=body,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("menu_limit,selected_count", [(2, 2), (10, 8), (17, 15)])
async def test_bootstrap_persists_frozen_limit_and_bounds_catalog(menu_limit, selected_count):
    site = _site_with_entries(tuple(_entry(index) for index in range(20)))
    expected = _expected_materialized_rows(site, menu_limit=selected_count)
    connection = FakeConnection(
        fetchrow_results=[_session_row(), None, {**_budget_row(), "menu_limit": menu_limit}],
        fetch_results=[expected, expected],
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(
        pool=FakePool(connection),
        frozen_policy=_frozen_budget_policy(menu_limit),
    )
    await repository.bootstrap(
        _command(), site, catalog_version="catalog-v1", catalog_sha256="a" * 64
    )
    insertion = next(
        call for call in connection.calls if "INSERT INTO qc_web_exploration_budget" in call[1]
    )
    assert insertion[2][-1] == menu_limit
    materialization = next(
        call for call in connection.calls if "INSERT INTO qc_web_menu_ledger_entry" in call[1]
    )
    payload = json.loads(materialization[2][-1])
    assert sum(row["execution_slot"] is not None for row in payload) == selected_count


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_limit", [None, 2, 17])
async def test_cold_bootstrap_rejects_policy_limit_drift_before_catalog_reads(execution_limit):
    connection = StatefulBootstrapConnection(
        budget={**_budget_row(), "menu_limit": execution_limit},
        persisted_rows=_expected_materialized_rows(_site(), menu_limit=execution_limit),
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(
        pool=FakePool(connection),
        frozen_policy=_frozen_budget_policy(),
    )
    with pytest.raises(WebExplorationBudgetLedgerConflictError, match="WEB_BUDGET_POLICY_MISMATCH"):
        await repository.bootstrap(
            _command(), _site(), catalog_version="catalog-v1", catalog_sha256="a" * 64
        )
    assert connection.entry_mutation_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["menu", "detail"])
@pytest.mark.parametrize("execution_limit", [None, 2, 17])
async def test_new_actions_reject_execution_limit_drift(action, execution_limit):
    connection = FakeConnection(fetchrow_results=[{"id": "capture", "menu_limit": execution_limit}])
    repository = PostgresWebExplorationBudgetLedgerRepository(
        pool=FakePool(connection),
        frozen_policy=_frozen_budget_policy(),
    )
    with pytest.raises(WebExplorationBudgetLedgerConflictError, match="WEB_BUDGET_POLICY_MISMATCH"):
        if action == "menu":
            await repository.claim_menu(
                command=_command(), entry=_entry(1), source_capture_id="capture"
            )
        else:
            await repository.reserve_detail(
                command=_command(),
                entry=_entry(1),
                source_capture_id="capture",
                candidate_id="detail",
                destination_url="https://example.test/detail/1",
            )


@pytest.mark.asyncio
async def test_seal_requires_all_entries_terminal_and_is_idempotent() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {"state": "sealed", "row_version": 4},
            {"state": "sealed", "row_version": 4},
        ]
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(
        pool=FakePool(connection)
    )

    await repository.seal(command=_command(), site_id="example")
    await repository.seal(command=_command(), site_id="example")

    sql = connection.calls[0][1]
    assert "FOR UPDATE OF budget" in sql
    assert "budget.state IN ('open', 'sealed')" in sql
    assert "entry.status IN" in sql
    assert "'pending', 'home_pending', 'menu_reserved', 'detail_reserved'" in sql
    assert "run.agent_type = 'main'" in sql
    assert "SELECT latest.id" in sql
    assert "task.finalization_state = 'open'" in sql
    assert "task.finalization_state = 'exploration_draining'" in sql
    assert "task.finalization_agent_run_id = budget.agent_run_id" in sql
    assert "SET state = 'sealed'" in sql
    assert connection.calls[0][2][:4] == (
        _command().task_id,
        _command().agent_run_id,
        _command().session_id,
        "example",
    )


@pytest.mark.asyncio
async def test_seal_fails_closed_when_budget_or_terminal_facts_do_not_match() -> None:
    repository = PostgresWebExplorationBudgetLedgerRepository(
        pool=FakePool(FakeConnection(fetchrow_results=[None]))
    )

    with pytest.raises(
        WebExplorationBudgetLedgerConflictError,
        match="尚未全部终态",
    ):
        await repository.seal(command=_command(), site_id="example")


def _session_row() -> dict[str, Any]:
    return {"task_id": _command().task_id, "session_id": _command().session_id}


def _materialized_rows(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in payload]


@pytest.mark.asyncio
async def test_bootstrap_materializes_full_catalog_without_fixed_limit() -> None:
    site = _site()
    connection = FakeConnection(
        fetchrow_results=[_session_row(), None, _budget_row()],
        fetch_results=[
            _expected_materialized_rows(site),
            _expected_materialized_rows(site),
        ],
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    await repository.bootstrap(
        _command(),
        site,
        catalog_version="catalog-v1",
        catalog_sha256="a" * 64,
    )

    entry_call = next(call for call in connection.calls if "qc_web_menu_ledger_entry" in call[1])
    payload = json.loads(
        next(arg for arg in entry_call[2] if isinstance(arg, str) and arg.startswith("["))
    )
    assert len(payload) == 13
    assert [item["execution_slot"] for item in payload[1:11]] == list(range(1, 11))
    assert payload[0]["execution_slot"] is None
    assert payload[0]["status"] == "home_pending"
    assert payload[11]["execution_slot"] == 11
    assert payload[12]["execution_slot"] is None
    assert all(
        item["status"] == "pending"
        for item in payload[11:12]
    )
    assert payload[12]["status"] == WebMenuTerminalStatus.CROSS_ORIGIN_BLOCKED.value
    assert all("canonical_url" not in item and "href" not in item for item in payload)


def test_catalog_rows_persist_logical_alias_without_assigning_queue_slot() -> None:
    home = _entry(0, decision=WebMenuDecision.HOME_BASELINE)
    primary = _entry(1)
    alias = WebSiteMenuEntry(
        entry_id="entry-alias",
        menu_path=("一级", "同 URL 别名"),
        href=primary.href,
        canonical_url=primary.canonical_url,
        decision=WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY,
        is_same_origin=True,
    )
    site = _site_with_entries((home, primary, alias))

    payload = _catalog_payload(site)

    assert payload[1]["execution_slot"] == 1
    assert payload[2]["execution_slot"] is None
    assert payload[2]["status"] == WebMenuTerminalStatus.MENU_LIMIT_SKIPPED.value
    assert payload[2]["diagnostic"] == {
        "error_code": "WEB_MENU_LOGICAL_ALIAS",
        "alias_of": primary.entry_id,
    }


def test_catalog_rows_persist_stable_deprecated_diagnostic() -> None:
    site = WebSiteMenuRegistry().resolve_site("https://hl365.com/")

    payload = {
        row["menu_entry_id"]: row
        for row in _catalog_payload(site)
    }

    deprecated_ids = {
        "hl365.sjb_hl",
        "hl365.sjb_ttt",
        "hl365.sjb_rs",
        "hl365.sjb_bc",
    }
    assert all(
        payload[entry_id]["diagnostic"]
        == {
            "error_code": "WEB_MENU_CATALOG_DEPRECATED",
            "catalog_status": "deprecated",
        }
        for entry_id in deprecated_ids
    )


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent_but_rejects_catalog_origin_or_session_drift() -> None:
    site = _site()
    connection = FakeConnection(
        fetchrow_results=[
            _session_row(),
            None,
            _budget_row(),
            _session_row(),
            _budget_row(),
            _session_row(),
            _budget_row(),
            _session_row(),
            _budget_row(),
            None,
        ],
        fetch_results=[
            _expected_materialized_rows(site),
            _expected_materialized_rows(site),
            _expected_materialized_rows(site),
        ],
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    for _ in range(2):
        await repository.bootstrap(
            _command(),
            site,
            catalog_version="catalog-v1",
            catalog_sha256="a" * 64,
        )

    drifted_site = WebSiteMenuCatalog(
        site_id=site.site_id,
        origin="https://drifted.test",
        navigation_evidence=site.navigation_evidence,
        entries=site.entries,
    )
    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        await repository.bootstrap(
            _command(),
            drifted_site,
            catalog_version="catalog-v1",
            catalog_sha256="a" * 64,
        )
    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        await repository.bootstrap(
            _command(),
            site,
            catalog_version="catalog-v2",
            catalog_sha256="b" * 64,
        )
    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        await repository.bootstrap(
            _command(session_id="web:other-run"),
            site,
            catalog_version="catalog-v1",
            catalog_sha256="a" * 64,
        )


@pytest.mark.parametrize(
    "runtime_kind", ["unclassified_home", "unclassified_home_details", "visited_menu"],
)
def test_cold_catalog_validation_preserves_runtime_diagnostics_without_accepting_identity_drift(
    runtime_kind,
):
    from copy import deepcopy

    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        _same_materialized_catalog,
    )

    wanted = _catalog_payload(_site())
    rows = deepcopy(wanted)
    index = 0 if runtime_kind.startswith("unclassified_home") else 1
    rows[index].update(
        status="page_type_unclassified", menu_action_count=0 if index == 0 else 1,
        detail_action_count=0, diagnostic={"error_code": "WEB_PAGE_TYPE_UNCLASSIFIED"},
    )
    if runtime_kind == "unclassified_home_details":
        # 正式终态工厂还会保留类型、说明等运行审计，不能要求诊断对象只有一个键。
        rows[index]["diagnostic"].update(type="TimeoutError", message="原分类失败诊断")
    before = deepcopy(rows)
    assert _same_materialized_catalog(wanted, rows)
    assert rows == before
    for field, value in (
        ("catalog_position", 999), ("canonical_url_sha256", "0" * 64),
        ("execution_slot", 999), ("menu_path", ["错误入口"]),
        ("decision", "runtime_observe_only"),
    ):
        invalid = deepcopy(rows)
        invalid[index][field] = value
        assert not _same_materialized_catalog(wanted, invalid), field
    invalid = deepcopy(wanted)
    invalid[1]["diagnostic"] = {"error_code": "WEB_PAGE_TYPE_UNCLASSIFIED"}
    assert not _same_materialized_catalog(wanted, invalid)
    invalid = deepcopy(rows)
    invalid[index]["menu_action_count"] = 0
    invalid[index]["diagnostic"] = {"error_code": "FORGED_CLOSEOUT"}
    assert not _same_materialized_catalog(wanted, invalid)
    for status in (
        "pending", "menu_reserved", "detail_reserved", "menu_limit_skipped",
        "cross_origin_blocked", "runtime_discovered_unreviewed", "home_baseline",
    ):
        invalid = deepcopy(wanted)
        invalid[1].update(
            status=status, menu_action_count=1,
            diagnostic={"error_code": "FORGED_CLOSEOUT"},
        )
        assert not _same_materialized_catalog(wanted, invalid), status


@pytest.mark.asyncio
@pytest.mark.parametrize("menu_limit", [5, 7, 10])
async def test_bootstrap_accepts_existing_positive_budget_without_rewriting(menu_limit) -> None:
    site = _site()
    legacy_budget = {**_budget_row(), "menu_limit": menu_limit}
    legacy_rows = _expected_materialized_rows(site, menu_limit=menu_limit)
    connection = FakeConnection(
        fetchrow_results=[_session_row(), legacy_budget],
        fetch_results=[legacy_rows],
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    await repository.bootstrap(
        _command(),
        site,
        catalog_version="catalog-v1",
        catalog_sha256="a" * 64,
    )

    assert all("INSERT INTO qc_web_exploration_budget" not in call[1] for call in connection.calls)
    assert all("INSERT INTO qc_web_menu_ledger_entry" not in call[1] for call in connection.calls)


@pytest.mark.asyncio
async def test_bootstrap_fails_closed_when_full_catalog_is_not_materialized() -> None:
    connection = FakeConnection(
        fetchrow_results=[_session_row(), None, _budget_row()],
        fetch_results=[[], []],
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        await repository.bootstrap(
            _command(),
            _site(),
            catalog_version="catalog-v1",
            catalog_sha256="a" * 64,
        )


@pytest.mark.asyncio
async def test_bootstrap_does_not_depend_on_insert_returning_order() -> None:
    site = _site()
    connection = FakeConnection(
        fetchrow_results=[_session_row(), None, _budget_row()],
        fetch_results=[
            list(reversed(_expected_materialized_rows(site))),
            list(reversed(_expected_materialized_rows(site))),
        ],
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    await repository.bootstrap(
        _command(),
        site,
        catalog_version="catalog-v1",
        catalog_sha256="a" * 64,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("drift_kind", ["added", "deleted", "replaced"])
async def test_existing_bootstrap_compares_catalog_before_any_entry_mutation(
    drift_kind: str,
) -> None:
    home = _entry(0, decision=WebMenuDecision.HOME_BASELINE)
    original_menu = _entry(1)
    original_site = _site_with_entries((home, original_menu))
    if drift_kind == "added":
        incoming_site = _site_with_entries((home, original_menu, _entry(2)))
    elif drift_kind == "deleted":
        incoming_site = _site_with_entries((home,))
    else:
        replacement = WebSiteMenuEntry(
            entry_id=original_menu.entry_id,
            menu_path=original_menu.menu_path,
            href="/replacement",
            canonical_url="https://example.test/replacement",
            decision=original_menu.decision,
            is_same_origin=True,
        )
        incoming_site = _site_with_entries((home, replacement))
    original_rows = _expected_materialized_rows(original_site)
    connection = StatefulBootstrapConnection(
        budget=_budget_row(),
        persisted_rows=original_rows,
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        await repository.bootstrap(
            _command(),
            incoming_site,
            catalog_version="catalog-v1",
            catalog_sha256="a" * 64,
        )

    assert connection.entry_mutation_count == 0
    assert list(connection.persisted_rows.values()) == original_rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface_kind", "session_owner"),
    [("android", "web-v2-workflow"), ("web", "h5-workflow")],
)
async def test_bootstrap_rejects_non_web_v2_session(
    surface_kind: str,
    session_owner: str,
) -> None:
    connection = FakeConnection(fetchrow_results=[None])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        await repository.bootstrap(
            _command(session_owner=session_owner),
            _site(),
            catalog_version="catalog-v1",
            catalog_sha256="a" * 64,
        )

    sql = connection.calls[0][1]
    assert "session.surface_kind = 'web'" in sql
    assert "session.session_owner = 'web-v2-workflow'" in sql
    assert surface_kind in {"android", "web"}


@pytest.mark.asyncio
async def test_claim_menu_returns_none_for_eleventh_entry_without_intent() -> None:
    connection = FakeConnection(fetchrow_results=[{"id": "capture-source"}, None])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    reservation = await repository.claim_menu(
        command=_command(),
        entry=_entry(11),
        source_capture_id="00000000-0000-0000-0000-000000000011",
    )

    assert reservation is None
    sql = next(call[1] for call in connection.calls if "qc_web_menu_ledger_entry" in call[1])
    assert "FOR UPDATE" in sql
    assert "execution_slot IS NOT NULL" in sql


@pytest.mark.asyncio
async def test_budget_closeout_persists_terminal_skips_without_reopening_candidates() -> None:
    class CloseoutConnection(FakeConnection):
        async def execute(self, query, *args):
            self.calls.append(("execute", " ".join(query.split()), args))
            return "UPDATE 2"

    connection = CloseoutConnection()
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))
    await repository.closeout_unexecuted(command=_command(), site_id="example")
    sql = connection.calls[0][1]
    assert "status='menu_limit_skipped'" in sql
    assert "execution_slot IS NULL" in sql
    assert "menu_action_count=0" in sql
    assert "menu_reserved','detail_reserved" in sql
    assert "WEB_DISCOVERY_BUDGET_EXHAUSTED" in sql
    assert "budget.state='open'" in sql
    assert "task.status='running'" in sql
    assert "run.status='running'" in sql
    assert "session.finalization_epoch=task.finalization_epoch" in sql


@pytest.mark.asyncio
async def test_claim_menu_returns_stable_new_and_existing_reservations() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {"id": "capture-source"},
            {
                "site_id": "example",
                "menu_entry_id": "entry-3",
                "execution_slot": 3,
                "menu_intent_id": f"web-menu:{_command().agent_run_id}:example:entry-3",
                "already_reserved": False,
            },
            {"id": "capture-source"},
            {
                "site_id": "example",
                "menu_entry_id": "entry-3",
                "execution_slot": 3,
                "menu_intent_id": f"web-menu:{_command().agent_run_id}:example:entry-3",
                "already_reserved": True,
            },
        ]
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))
    kwargs = {
        "command": _command(),
        "entry": _entry(3),
        "source_capture_id": "00000000-0000-0000-0000-000000000013",
    }

    first = await repository.claim_menu(**kwargs)
    second = await repository.claim_menu(**kwargs)

    expected_intent = f"web-menu:{_command().agent_run_id}:example:entry-3"
    assert first is not None and first.intent_id == expected_intent
    assert first.execution_slot == 3 and first.already_reserved is False
    assert second is not None and second.intent_id == expected_intent
    assert second.execution_slot == 3 and second.already_reserved is True


@pytest.mark.asyncio
async def test_claim_menu_proves_source_capture_web_lineage_in_sql() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {"id": "capture-source"},
            {
                "site_id": "example",
                "menu_entry_id": "entry-3",
                "execution_slot": 3,
                "menu_intent_id": f"web-menu:{_command().agent_run_id}:example:entry-3",
                "already_reserved": False,
            },
        ]
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    await repository.claim_menu(
        command=_command(),
        entry=_entry(3),
        source_capture_id="00000000-0000-0000-0000-000000000013",
    )

    sql = next(call[1] for call in connection.calls if "qc_web_menu_ledger_entry" in call[1])
    assert "INNER JOIN qc_capture AS source" in sql
    assert "source.task_id = budget.task_id" in sql
    assert "source.agent_run_id = budget.agent_run_id" in sql
    assert "source.surface_session_id = budget.session_id" in sql
    assert "source.surface_kind = 'web'" in sql
    assert "source.capture_status = 'captured'" in sql


@pytest.mark.asyncio
async def test_second_detail_reservation_reuses_intent_without_second_action() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {"id": "capture-source"},
            {
                "site_id": "example",
                "menu_entry_id": "entry-4",
                "detail_intent_id": f"web-detail:{_command().agent_run_id}:example:entry-4",
                "already_reserved": False,
            },
            {"id": "capture-source"},
            {
                "site_id": "example",
                "menu_entry_id": "entry-4",
                "detail_intent_id": f"web-detail:{_command().agent_run_id}:example:entry-4",
                "already_reserved": True,
            },
        ]
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))
    kwargs = {
        "command": _command(),
        "entry": _entry(4),
        "source_capture_id": "00000000-0000-0000-0000-000000000014",
        "candidate_id": "web-candidate:detail-4",
        "destination_url": "https://example.test/detail/4",
    }

    first = await repository.reserve_detail(**kwargs)
    second = await repository.reserve_detail(**kwargs)

    expected_intent = f"web-detail:{_command().agent_run_id}:example:entry-4"
    assert first.intent_id == expected_intent and first.already_reserved is False
    assert second.intent_id == expected_intent and second.already_reserved is True
    assert all(
        "FOR UPDATE" in call[1]
        for call in connection.calls
        if "qc_web_menu_ledger_entry" in call[1]
    )


@pytest.mark.asyncio
async def test_claim_menu_rejects_invalid_source_capture_as_conflict() -> None:
    connection = FakeConnection(fetchrow_results=[None])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        await repository.claim_menu(
            command=_command(),
            entry=_entry(3),
            source_capture_id="00000000-0000-0000-0000-000000000099",
        )

    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_reserve_detail_proves_source_capture_web_lineage_in_sql() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {"id": "capture-source"},
            {
                "site_id": "example",
                "menu_entry_id": "entry-4",
                "detail_intent_id": f"web-detail:{_command().agent_run_id}:example:entry-4",
                "already_reserved": False,
            },
        ]
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    await repository.reserve_detail(
        command=_command(),
        entry=_entry(4),
        source_capture_id="00000000-0000-0000-0000-000000000014",
        candidate_id="web-candidate:detail-4",
        destination_url="https://example.test/detail/4",
    )

    sql = next(call[1] for call in connection.calls if "qc_web_menu_ledger_entry" in call[1])
    assert "INNER JOIN qc_capture AS source" in sql
    assert "source.task_id = budget.task_id" in sql
    assert "source.agent_run_id = budget.agent_run_id" in sql
    assert "source.surface_session_id = budget.session_id" in sql
    assert "source.surface_kind = 'web'" in sql
    assert "source.capture_status = 'captured'" in sql


@pytest.mark.asyncio
async def test_finalize_records_only_existing_evidence_and_action_ids() -> None:
    connection = FakeConnection(fetchrow_results=[{"menu_entry_id": "entry-2"}])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))
    record = WebMenuLedgerTerminalRecord(
        task_id=_command().task_id,
        agent_run_id=_command().agent_run_id,
        session_id=_command().session_id,
        site_id="example",
        menu_entry_id="entry-2",
        status=WebMenuTerminalStatus.DETAIL_CAPTURED,
        menu_action_execution_id="action-execution:menu-2",
        menu_probe_capture_id="00000000-0000-0000-0000-000000000021",
        menu_ready_capture_id="00000000-0000-0000-0000-000000000022",
        menu_page_type="content_list",
        detail_action_execution_id="action-execution:detail-2",
        detail_probe_capture_id="00000000-0000-0000-0000-000000000023",
        detail_ready_capture_id="00000000-0000-0000-0000-000000000024",
        detail_page_type="content_detail",
        detail_selection={"candidate_id": "web-candidate:detail-2"},
        diagnostic={},
    )

    await repository.finalize(record)

    sql = connection.calls[0][1]
    assert "qc_action_execution" in sql
    assert "qc_capture" in sql
    assert "locked.menu_source_capture_id" in sql
    assert "locked.detail_source_capture_id" in sql
    assert "screenshot" not in sql.casefold()
    assert "dom" not in sql.casefold()


@pytest.mark.asyncio
async def test_finalize_ordinary_menu_requires_empty_detail_selection() -> None:
    connection = FakeConnection(fetchrow_results=[{"menu_entry_id": "entry-2"}])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    await repository.finalize(
        WebMenuLedgerTerminalRecord(
            task_id=_command().task_id,
            agent_run_id=_command().agent_run_id,
            session_id=_command().session_id,
            site_id="example",
            menu_entry_id="entry-2",
            status=WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
            menu_action_execution_id="action-execution:menu-2",
            menu_probe_capture_id="00000000-0000-0000-0000-000000000021",
            menu_ready_capture_id="00000000-0000-0000-0000-000000000022",
            menu_page_type="content_list",
        )
    )

    sql = connection.calls[0][1]
    ordinary_branch = sql.split("FROM locked_entry AS locked", 1)[1].split(
        "$6 = 'menu_page_captured'",
        2,
    )[1]
    assert "$15::jsonb = '{}'::jsonb" in ordinary_branch


def test_collection_menu_terminal_accepts_recognized_frozen_catalog_assessment():
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        _FINALIZE_SQL,
    )

    branch = _FINALIZE_SQL.split("FROM locked_entry AS locked", 1)[1].split(
        "$6 = 'menu_page_captured'",
        2,
    )[1]
    assert "$10 = 'collection_page'" in branch
    assert "collection_assessment.page_capture_id = $9::uuid" in branch
    assert "collection_assessment.detection_status = 'recognized'" in branch
    assert "collection_assessment.schema_version = 'web-page-type-assessment.v4'" in branch
    assert "2026-09-19.web.v11" not in branch


@pytest.mark.asyncio
async def test_finalize_home_binds_exact_assessment_normalization_version() -> None:
    connection = FakeConnection(fetchrow_results=[{"menu_entry_id": "entry-0"}])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    await repository.finalize(
        WebMenuLedgerTerminalRecord(
            task_id=_command().task_id,
            agent_run_id=_command().agent_run_id,
            session_id=_command().session_id,
            site_id="example",
            menu_entry_id="entry-0",
            status=WebMenuTerminalStatus.HOME_BASELINE,
            menu_probe_capture_id="00000000-0000-0000-0000-000000000031",
            menu_ready_capture_id="00000000-0000-0000-0000-000000000032",
            menu_page_type="home",
        )
    )

    sql = connection.calls[0][1]
    assert "normalization_version" in sql
    assert "qc_h5_page_type_assessment AS home_assessment" in sql
    assert "home_assessment.page_capture_id = $9::uuid" in sql
    assert "home_assessment.schema_version IN (" in sql
    assert "'web-page-type-assessment.v3'" in sql
    assert "'web-page-type-assessment.v4'" in sql
    assert "$10 = 'home'" in sql
    assert "home_assessment.schema_version IN (" in sql
    assert "'web-page-type-normalization.v1'" in sql
    assert "'web-page-type-assessment.v1'" in sql
    assert "$10 IN (" in sql
    for page_type in ("content_list", "video_list", "content_detail", "video_detail"):
        assert f"'{page_type}'" in sql


@pytest.mark.asyncio
async def test_finalize_submenu_binds_completed_action_and_after_capture_lineage() -> None:
    connection = FakeConnection(fetchrow_results=[{"menu_entry_id": "entry-2"}])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))
    await repository.finalize(
        WebMenuLedgerTerminalRecord(
            task_id=_command().task_id,
            agent_run_id=_command().agent_run_id,
            session_id=_command().session_id,
            site_id="example",
            menu_entry_id="entry-2",
            status=WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
            menu_action_execution_id="action-execution:menu-2",
            menu_probe_capture_id="00000000-0000-0000-0000-000000000022",
            menu_ready_capture_id="00000000-0000-0000-0000-000000000022",
            diagnostic={
                "type": "WEB_SUBMENU_EXPANDED",
                "menu_interaction_outcome": {
                    "command_accepted": True,
                    "effect_observed": True,
                    "postcondition_satisfied": True,
                    "transition_arrived": True,
                    "after_capture_id": "00000000-0000-0000-0000-000000000022",
                    "failure_reason": None,
                    "evidence_mode": "natural",
                },
            },
        )
    )

    sql = connection.calls[0][1]
    assert "$8::uuid = $9::uuid" in sql
    assert "action.status = 'completed'" in sql
    assert "action.finalization_epoch = locked.session_epoch" in sql
    assert "action.requested_source_capture_id = locked.menu_source_capture_id" in sql
    assert "after_capture.parent_capture_id = before_capture.id" in sql
    assert "after_capture.action_execution_id = action.action_execution_id" in sql
    assert "before_capture.capture_role = 'before_action'" in sql
    assert "before_capture.action_execution_id = action.action_execution_id" in sql
    assert "before_capture.parent_capture_id = action.requested_source_capture_id" in sql
    assert "before_capture.session_command_sequence = action.command_sequence" in sql
    assert "after_capture.capture_role = 'after_action'" in sql
    assert "after_capture.session_command_sequence = action.command_sequence" in sql
    assert "action.result_capture_id = $8::uuid" in sql
    assert "$16::jsonb->>'type' IS DISTINCT FROM 'WEB_SUBMENU_EXPANDED'" in sql
    for field_name in (
        "detail_intent_id",
        "detail_source_capture_id",
        "detail_candidate_id",
        "detail_destination_sha256",
    ):
        assert f"locked.{field_name} IS NULL" in sql
    assert "locked.detail_selection = '{}'::jsonb" in sql


@pytest.mark.asyncio
async def test_finalize_sql_is_irreversible_and_requires_proven_success_shape() -> None:
    connection = FakeConnection(fetchrow_results=[{"menu_entry_id": "entry-2"}])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))
    await repository.finalize(
        WebMenuLedgerTerminalRecord(
            task_id=_command().task_id,
            agent_run_id=_command().agent_run_id,
            session_id=_command().session_id,
            site_id="example",
            menu_entry_id="entry-2",
            status=WebMenuTerminalStatus.DETAIL_CAPTURED,
            menu_action_execution_id="action-execution:menu-2",
            menu_probe_capture_id="00000000-0000-0000-0000-000000000021",
            menu_ready_capture_id="00000000-0000-0000-0000-000000000022",
            menu_page_type="content_list",
            detail_action_execution_id="action-execution:detail-2",
            detail_probe_capture_id="00000000-0000-0000-0000-000000000023",
            detail_ready_capture_id="00000000-0000-0000-0000-000000000024",
            detail_page_type="content_detail",
        )
    )

    sql = connection.calls[0][1]
    assert "locked.status IN ('menu_reserved', 'detail_reserved')" in sql
    assert "locked.status = 'menu_reserved'" in sql
    assert "locked.status = 'detail_reserved'" in sql
    assert "locked.menu_action_count = 1" in sql
    assert "locked.detail_action_count = 1" in sql
    assert "content_list" in sql and "video_list" in sql
    assert "content_detail" in sql and "video_detail" in sql


@pytest.mark.asyncio
async def test_finalize_rejects_bootstrap_only_terminal_status() -> None:
    connection = FakeConnection(fetchrow_results=[{"menu_entry_id": "entry-11"}])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        await repository.finalize(
            WebMenuLedgerTerminalRecord(
                task_id=_command().task_id,
                agent_run_id=_command().agent_run_id,
                session_id=_command().session_id,
                site_id="example",
                menu_entry_id="entry-11",
                status=WebMenuTerminalStatus.MENU_LIMIT_SKIPPED,
            )
        )

    assert connection.calls == []


@pytest.mark.parametrize(
    ("status", "error_code"),
    (
        (WebMenuTerminalStatus.ALLOWLIST_DRIFTED, "WEB_MENU_ALLOWLIST_DRIFTED"),
        (
            WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
            "WEB_SITE_HUMAN_VERIFICATION_BLOCKED",
        ),
    ),
)
@pytest.mark.asyncio
async def test_finalize_allows_only_zero_action_pending_site_terminal(
    status: WebMenuTerminalStatus,
    error_code: str,
) -> None:
    connection = FakeConnection(fetchrow_results=[{"menu_entry_id": "entry-1"}])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    await repository.finalize(
        WebMenuLedgerTerminalRecord(
            task_id=_command().task_id,
            agent_run_id=_command().agent_run_id,
            session_id=_command().session_id,
            site_id="example",
            menu_entry_id="entry-1",
            status=status,
            menu_probe_capture_id="00000000-0000-0000-0000-000000000041",
            diagnostic={"error_code": error_code},
        )
    )

    sql = connection.calls[0][1]
    assert "entry.status IN ('home_pending', 'pending', 'menu_reserved', 'detail_reserved')" in sql
    assert "$6 IN ('allowlist_drifted', 'human_verification_blocked')" in sql
    assert "locked.status = 'pending'" in sql
    assert "locked.execution_slot >= 1" in sql
    assert "locked.menu_limit IS NULL OR locked.execution_slot <= locked.menu_limit" in sql
    assert "locked.menu_action_count = 0" in sql
    assert "locked.detail_action_count = 0" in sql
    assert "$8::uuid IS NOT NULL" in sql


@pytest.mark.asyncio
async def test_zero_action_site_terminal_cannot_fall_through_reserved_failure_branch() -> None:
    connection = FakeConnection(fetchrow_results=[{"menu_entry_id": "entry-1"}])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    await repository.finalize(
        WebMenuLedgerTerminalRecord(
            task_id=_command().task_id,
            agent_run_id=_command().agent_run_id,
            session_id=_command().session_id,
            site_id="example",
            menu_entry_id="entry-1",
            status=WebMenuTerminalStatus.ALLOWLIST_DRIFTED,
            menu_probe_capture_id="00000000-0000-0000-0000-000000000041",
        )
    )

    sql = connection.calls[0][1]
    reserved_failure_branch = sql.split("$6 IN (", 2)[2]
    assert "'allowlist_drifted'" not in reserved_failure_branch
    assert "locked.status IN ('menu_reserved', 'detail_reserved')" in reserved_failure_branch
    assert "$6 <> 'human_verification_blocked' OR $7::varchar IS NOT NULL" in sql


@pytest.mark.asyncio
async def test_finalize_closes_home_pending_with_captured_web_evidence_only() -> None:
    connection = FakeConnection(fetchrow_results=[{"menu_entry_id": "entry-0"}])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    await repository.finalize(
        WebMenuLedgerTerminalRecord(
            task_id=_command().task_id,
            agent_run_id=_command().agent_run_id,
            session_id=_command().session_id,
            site_id="example",
            menu_entry_id="entry-0",
            status=WebMenuTerminalStatus.HOME_BASELINE,
            menu_probe_capture_id="00000000-0000-0000-0000-000000000031",
            menu_ready_capture_id="00000000-0000-0000-0000-000000000032",
            menu_page_type="home",
        )
    )

    sql = connection.calls[0][1]
    assert "entry.status IN ('home_pending'" in sql
    assert "locked.decision = 'home_baseline'" in sql
    assert "locked.execution_slot IS NULL" in sql
    assert "locked.menu_action_count = 0" in sql
    assert "locked.detail_action_count = 0" in sql
    assert "locked.menu_intent_id IS NULL" in sql
    assert "locked.menu_source_capture_id IS NULL" in sql
    assert "$6 = 'home_baseline'" in sql
    assert "$8::uuid IS NOT NULL" in sql
    assert "$9::uuid IS NOT NULL" in sql
    assert "$10 = 'home'" in sql
    assert "$15::jsonb = '{}'::jsonb" in sql
    assert "capture.surface_kind = 'web'" in sql
    assert "capture.capture_status = 'captured'" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_capture_id", "ready_capture_id", "page_type"),
    [
        (None, "00000000-0000-0000-0000-000000000032", "content_list"),
        (
            "00000000-0000-0000-0000-000000000031",
            "00000000-0000-0000-0000-000000000032",
            "content_list",
        ),
    ],
)
async def test_finalize_home_rejects_missing_or_non_home_evidence(
    probe_capture_id: str | None,
    ready_capture_id: str,
    page_type: str,
) -> None:
    connection = FakeConnection(fetchrow_results=[None])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        await repository.finalize(
            WebMenuLedgerTerminalRecord(
                task_id=_command().task_id,
                agent_run_id=_command().agent_run_id,
                session_id=_command().session_id,
                site_id="example",
                menu_entry_id="entry-0",
                status=WebMenuTerminalStatus.HOME_BASELINE,
                menu_probe_capture_id=probe_capture_id,
                menu_ready_capture_id=ready_capture_id,
                menu_page_type=page_type,
            )
        )

    assert len(connection.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_reason", ["cross_lineage", "non_home_entry"])
async def test_finalize_home_rejects_invalid_capture_lineage_or_entry(
    invalid_reason: str,
) -> None:
    connection = FakeConnection(fetchrow_results=[None])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        await repository.finalize(
            WebMenuLedgerTerminalRecord(
                task_id=_command().task_id,
                agent_run_id=_command().agent_run_id,
                session_id=_command().session_id,
                site_id="example",
                menu_entry_id="entry-0" if invalid_reason == "cross_lineage" else "entry-1",
                status=WebMenuTerminalStatus.HOME_BASELINE,
                menu_probe_capture_id="00000000-0000-0000-0000-000000000031",
                menu_ready_capture_id="00000000-0000-0000-0000-000000000032",
                menu_page_type="content_list",
            )
        )

    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_finalize_home_is_irreversible() -> None:
    connection = FakeConnection(fetchrow_results=[{"menu_entry_id": "entry-0"}, None])
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))
    record = WebMenuLedgerTerminalRecord(
        task_id=_command().task_id,
        agent_run_id=_command().agent_run_id,
        session_id=_command().session_id,
        site_id="example",
        menu_entry_id="entry-0",
        status=WebMenuTerminalStatus.HOME_BASELINE,
        menu_probe_capture_id="00000000-0000-0000-0000-000000000031",
        menu_ready_capture_id="00000000-0000-0000-0000-000000000032",
        menu_page_type="content_list",
    )

    await repository.finalize(record)
    with pytest.raises(WebExplorationBudgetLedgerConflictError):
        await repository.finalize(record)

    assert len(connection.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("normalization,page_type,expected_status", [
    ("web-page-type-assessment.v2", "content_list", WebMenuTerminalStatus.HOME_BASELINE),
    ("web-page-type-normalization.v1", "video_list", WebMenuTerminalStatus.HOME_BASELINE),
    ("web-page-type-assessment.v3", "home", WebMenuTerminalStatus.HOME_BASELINE),
    ("web-page-type-assessment.v4", "home", WebMenuTerminalStatus.HOME_BASELINE),
    ("web-page-type-assessment.v3", "content_list", WebMenuTerminalStatus.NAVIGATION_FAILED),
])
async def test_read_summary_projects_valid_home_baseline_without_actions(
    normalization, page_type, expected_status
) -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {
                "site_id": "example",
                "catalog_version": "catalog-v1",
                "normalization_version": normalization,
                "catalog_sha256": "a" * 64,
                "menu_limit": WEB_MENU_COLLECTION_LIMIT,
                "menu_total": 1,
            }
        ],
        fetch_results=[
            [
                {
                    "menu_entry_id": "entry-0",
                    "decision": "home_baseline",
                    "menu_path": '["首页"]',
                    "canonical_url_sha256": "e" * 64,
                    "status": WebMenuTerminalStatus.HOME_BASELINE.value,
                    "execution_slot": None,
                    "menu_action_count": 0,
                    "menu_intent_id": None,
                    "menu_source_capture_id": None,
                    "menu_action_execution_id": None,
                    "menu_probe_capture_id": "00000000-0000-0000-0000-000000000031",
                    "menu_ready_capture_id": "00000000-0000-0000-0000-000000000032",
                    "menu_page_type": "content_list",
                    "detail_action_count": 0,
                    "detail_intent_id": None,
                    "detail_source_capture_id": None,
                    "detail_action_execution_id": None,
                    "detail_probe_capture_id": None,
                    "detail_ready_capture_id": None,
                    "detail_page_type": None,
                    "detail_selection": "{}",
                    "diagnostic": "{}",
                }
            ]
        ],
    )
    connection.fetch_results[0][0]["menu_page_type"] = page_type
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    summary = await repository.read_summary(
        agent_run_id=_command().agent_run_id,
        site_id="example",
    )

    assert summary.menu_ledger[0].status is expected_status
    assert summary.normalization_version == normalization
    assert summary.executed_same_origin_count == 0
    assert summary.menu_ledger[0].detail_action_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "normalization_version",
    [None, "", "web-page-type-assessment.v999"],
)
async def test_read_summary_fails_closed_when_home_normalization_version_is_untrusted(
    normalization_version: str | None,
) -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {
                "site_id": "example",
                "catalog_version": "catalog-v1",
                "normalization_version": normalization_version,
                "catalog_sha256": "a" * 64,
                "menu_limit": WEB_MENU_COLLECTION_LIMIT,
                "menu_total": 1,
            }
        ],
        fetch_results=[
            [
                {
                    "menu_entry_id": "entry-0",
                    "decision": "home_baseline",
                    "menu_path": '["首页"]',
                    "canonical_url_sha256": "e" * 64,
                    "status": WebMenuTerminalStatus.HOME_BASELINE.value,
                    "execution_slot": None,
                    "menu_action_count": 0,
                    "menu_intent_id": None,
                    "menu_source_capture_id": None,
                    "menu_action_execution_id": None,
                    "menu_probe_capture_id": "00000000-0000-0000-0000-000000000031",
                    "menu_ready_capture_id": "00000000-0000-0000-0000-000000000032",
                    "menu_page_type": "content_list",
                    "detail_action_count": 0,
                    "detail_intent_id": None,
                    "detail_source_capture_id": None,
                    "detail_candidate_id": None,
                    "detail_destination_sha256": None,
                    "detail_action_execution_id": None,
                    "detail_probe_capture_id": None,
                    "detail_ready_capture_id": None,
                    "detail_page_type": None,
                    "detail_selection": "{}",
                    "diagnostic": "{}",
                }
            ]
        ],
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    summary = await repository.read_summary(
        agent_run_id=_command().agent_run_id,
        site_id="example",
    )

    assert summary.normalization_version == (normalization_version or "")
    assert summary.menu_ledger[0].status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert (
        summary.menu_ledger[0].error_code
        == "WEB_BUDGET_TERMINAL_EVIDENCE_INVALID"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["missing_capture", "detail_selection"])
async def test_read_summary_fails_closed_for_invalid_home_evidence(
    invalid_kind: str,
) -> None:
    has_capture = invalid_kind == "detail_selection"
    connection = FakeConnection(
        fetchrow_results=[
            {
                "site_id": "example",
                "catalog_version": "catalog-v1",
                "catalog_sha256": "a" * 64,
                "menu_limit": WEB_MENU_COLLECTION_LIMIT,
                "menu_total": 1,
            }
        ],
        fetch_results=[
            [
                {
                    "menu_entry_id": "entry-0",
                    "decision": "home_baseline",
                    "menu_path": '["首页"]',
                    "canonical_url_sha256": "e" * 64,
                    "status": WebMenuTerminalStatus.HOME_BASELINE.value,
                    "execution_slot": None,
                    "menu_action_count": 0,
                    "menu_probe_capture_id": (
                        "00000000-0000-0000-0000-000000000031" if has_capture else None
                    ),
                    "menu_ready_capture_id": (
                        "00000000-0000-0000-0000-000000000032" if has_capture else None
                    ),
                    "menu_page_type": "content_list" if has_capture else None,
                    "detail_action_count": 0,
                    "detail_probe_capture_id": None,
                    "detail_ready_capture_id": None,
                    "detail_page_type": None,
                    "detail_selection": (
                        '{"candidate_id":"web-candidate:forged"}'
                        if invalid_kind == "detail_selection"
                        else "{}"
                    ),
                    "diagnostic": "{}",
                }
            ]
        ],
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    summary = await repository.read_summary(
        agent_run_id=_command().agent_run_id,
        site_id="example",
    )

    assert summary.menu_ledger[0].status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert summary.menu_ledger[0].error_code == "WEB_BUDGET_TERMINAL_EVIDENCE_INVALID"


@pytest.mark.asyncio
async def test_read_summary_projects_persisted_action_execution_and_intents() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {
                "site_id": "example",
                "catalog_version": "catalog-v1",
                "catalog_sha256": "a" * 64,
                "menu_limit": WEB_MENU_COLLECTION_LIMIT,
                "menu_total": 1,
            }
        ],
        fetch_results=[
            [
                {
                    "menu_entry_id": "entry-1",
                    "menu_path": '["一级", "菜单 1"]',
                    "canonical_url_sha256": "c" * 64,
                    "status": WebMenuTerminalStatus.MENU_PAGE_CAPTURED.value,
                    "menu_source_capture_id": "00000000-0000-0000-0000-000000000011",
                    "menu_probe_capture_id": "00000000-0000-0000-0000-000000000012",
                    "menu_ready_capture_id": "00000000-0000-0000-0000-000000000013",
                    "menu_page_type": "content_list",
                    "menu_action_execution_id": "action-execution:menu-1",
                    "menu_intent_id": "web-menu:run-1:example:entry-1",
                    "detail_probe_capture_id": None,
                    "detail_ready_capture_id": None,
                    "detail_page_type": None,
                    "detail_selection": "{}",
                    "detail_intent_id": None,
                    "detail_action_execution_id": None,
                    "diagnostic": json.dumps(
                        {
                            "ad_landing_evidence": {
                                "status": "observed",
                                "source_url": "https://example.test/menu",
                                "landing_url": "https://ads.example.net/campaign",
                                "candidate_kind": "popup",
                                "artifact_ids": ["artifact-source", "artifact-landing"],
                            }
                        }
                    ),
                    "menu_action_count": 1,
                    "detail_action_count": 0,
                    "execution_slot": 1,
                }
            ]
        ],
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    summary = await repository.read_summary(
        agent_run_id=_command().agent_run_id,
        site_id="example",
    )

    assert summary.site_id == "example"
    assert summary.catalog_version == "catalog-v1"
    assert summary.executed_same_origin_count == 1
    assert summary.menu_ledger[0].entry_id == "entry-1"
    assert summary.menu_ledger[0].canonical_url == f"sha256:{'c' * 64}"
    assert summary.menu_ledger[0].menu_action_count == 1
    assert summary.menu_ledger[0].menu_intent_id == "web-menu:run-1:example:entry-1"
    assert summary.menu_ledger[0].menu_action_execution_id == "action-execution:menu-1"
    assert summary.menu_ledger[0].detail_intent_id is None
    assert summary.menu_ledger[0].detail_action_execution_id is None
    assert summary.menu_ledger[0].ad_landing_evidence == {
        "status": "observed",
        "source_url": "https://example.test/menu",
        "landing_url": "https://ads.example.net/campaign",
        "candidate_kind": "popup",
        "artifact_ids": ["artifact-source", "artifact-landing"],
    }
    assert summary.page_timeline == ("00000000-0000-0000-0000-000000000013",)
    read_sql = next(sql for operation, sql, _args in connection.calls if operation == "fetch")
    assert "before_capture.capture_role = 'before_action'" in read_sql
    assert "before_capture.action_execution_id = action.action_execution_id" in read_sql
    assert "before_capture.parent_capture_id = action.requested_source_capture_id" in read_sql
    assert "before_capture.session_command_sequence = action.command_sequence" in read_sql
    assert "after_capture.capture_role = 'after_action'" in read_sql
    assert "after_capture.parent_capture_id = before_capture.id" in read_sql
    assert "after_capture.session_command_sequence = action.command_sequence" in read_sql


@pytest.mark.asyncio
async def test_read_summary_projects_catalog_digest_and_position_total() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {
                "site_id": "example",
                "catalog_version": "catalog-v1",
                "catalog_sha256": "c" * 64,
                "menu_limit": WEB_MENU_COLLECTION_LIMIT,
                "menu_total": 13,
            }
        ],
        fetch_results=[
            [
                {
                    "menu_entry_id": "entry-12",
                    "catalog_position": 12,
                    "decision": "blocked_cross_origin",
                    "menu_path": ["外链"],
                    "canonical_url_sha256": "d" * 64,
                    "execution_slot": None,
                    "status": "cross_origin_blocked",
                    "menu_action_count": 0,
                    "detail_action_count": 0,
                }
            ]
        ],
    )

    summary = await PostgresWebExplorationBudgetLedgerRepository(
        pool=FakePool(connection)
    ).read_summary(agent_run_id=_command().agent_run_id, site_id="example")

    assert summary.catalog_sha256 == "c" * 64
    assert summary.menu_total == 13
    budget_sql = connection.calls[0][1]
    assert "catalog_sha256" in budget_sql
    assert "menu_total" in budget_sql


@pytest.mark.asyncio
async def test_summary_home_cross_origin_and_over_limit_have_no_budget_action_ids() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {
                "site_id": "example",
                "catalog_version": "catalog-v1",
                "catalog_sha256": "a" * 64,
                "menu_limit": WEB_MENU_COLLECTION_LIMIT,
                "menu_total": 3,
            }
        ],
        fetch_results=[
            [
                {
                    "menu_entry_id": "home",
                    "catalog_position": 0,
                    "decision": "home_baseline",
                    "menu_path": ["首页"],
                    "canonical_url_sha256": "a" * 64,
                    "execution_slot": None,
                    "status": "home_baseline",
                    "menu_action_count": 0,
                    "menu_intent_id": None,
                    "menu_source_capture_id": None,
                    "menu_action_execution_id": None,
                    "menu_probe_capture_id": "00000000-0000-0000-0000-000000000051",
                    "menu_ready_capture_id": "00000000-0000-0000-0000-000000000052",
                    "menu_page_type": "content_list",
                    "detail_action_count": 0,
                    "detail_intent_id": None,
                    "detail_source_capture_id": None,
                    "detail_candidate_id": None,
                    "detail_destination_sha256": None,
                    "detail_action_execution_id": None,
                    "detail_probe_capture_id": None,
                    "detail_ready_capture_id": None,
                    "detail_page_type": None,
                    "detail_selection": {},
                    "diagnostic": {},
                },
                {
                    "menu_entry_id": "cross-origin",
                    "catalog_position": 1,
                    "decision": "blocked_cross_origin",
                    "menu_path": ["外链"],
                    "canonical_url_sha256": "b" * 64,
                    "execution_slot": None,
                    "status": "cross_origin_blocked",
                    "menu_action_count": 0,
                    "detail_action_count": 0,
                },
                {
                    "menu_entry_id": "over-limit",
                    "catalog_position": 2,
                    "decision": "allowlisted_observe_only",
                    "menu_path": ["超限"],
                    "canonical_url_sha256": "c" * 64,
                    "execution_slot": None,
                    "status": "menu_limit_skipped",
                    "menu_action_count": 0,
                    "detail_action_count": 0,
                },
            ]
        ],
    )

    summary = await PostgresWebExplorationBudgetLedgerRepository(
        pool=FakePool(connection)
    ).read_summary(agent_run_id=_command().agent_run_id, site_id="example")

    assert summary.executed_same_origin_count == 0
    assert summary.menu_limit_skipped_count == 1
    for item in summary.menu_ledger:
        assert item.menu_action_count == 0
        assert item.menu_intent_id is None
        assert item.menu_action_execution_id is None
        assert item.detail_action_count == 0
        assert item.detail_intent_id is None
        assert item.detail_action_execution_id is None


@pytest.mark.asyncio
async def test_action_execution_direct_detail_projects_menu_ready_without_detail_action() -> None:
    menu_ready = "00000000-0000-0000-0000-000000000023"
    connection = FakeConnection(
        fetchrow_results=[
            {
                "site_id": "example",
                "catalog_version": "catalog-v1",
                "catalog_sha256": "a" * 64,
                "menu_limit": WEB_MENU_COLLECTION_LIMIT,
                "menu_total": 1,
            }
        ],
        fetch_results=[
            [
                {
                    "menu_entry_id": "entry-direct",
                    "menu_path": '["详情"]',
                    "canonical_url_sha256": "e" * 64,
                    "status": WebMenuTerminalStatus.DETAIL_CAPTURED.value,
                    "menu_source_capture_id": "00000000-0000-0000-0000-000000000021",
                    "menu_probe_capture_id": "00000000-0000-0000-0000-000000000022",
                    "menu_ready_capture_id": menu_ready,
                    "menu_page_type": "content_detail",
                    "menu_action_count": 1,
                    "menu_intent_id": "web-menu:run-1:example:entry-direct",
                    "menu_action_execution_id": "actual-menu-action-direct",
                    "detail_action_count": 0,
                    "detail_intent_id": None,
                    "detail_action_execution_id": None,
                    "detail_probe_capture_id": None,
                    "detail_ready_capture_id": None,
                    "detail_page_type": None,
                    "detail_selection": "{}",
                    "diagnostic": "{}",
                    "execution_slot": 1,
                }
            ]
        ],
    )

    summary = await PostgresWebExplorationBudgetLedgerRepository(
        pool=FakePool(connection)
    ).read_summary(agent_run_id=_command().agent_run_id, site_id="example")
    direct = summary.menu_ledger[0]

    assert direct.status is WebMenuTerminalStatus.DETAIL_CAPTURED
    assert direct.menu_action_count == 1
    assert direct.menu_action_execution_id == "actual-menu-action-direct"
    assert direct.detail_capture_id == menu_ready
    assert direct.detail_ready_capture_id == menu_ready
    assert direct.detail_page_type == "content_detail"
    assert direct.detail_action_count == 0
    assert direct.detail_intent_id is None
    assert direct.detail_action_execution_id is None


@pytest.mark.asyncio
async def test_read_summary_fails_closed_for_invalid_success_record() -> None:
    connection = FakeConnection(
        fetchrow_results=[
            {
                "site_id": "example",
                "catalog_version": "catalog-v1",
                "catalog_sha256": "a" * 64,
                "menu_limit": WEB_MENU_COLLECTION_LIMIT,
                "menu_total": 1,
            }
        ],
        fetch_results=[
            [
                {
                    "menu_entry_id": "entry-11",
                    "menu_path": '["一级", "菜单 11"]',
                    "canonical_url_sha256": "d" * 64,
                    "status": WebMenuTerminalStatus.DETAIL_CAPTURED.value,
                    "menu_source_capture_id": None,
                    "menu_action_execution_id": None,
                    "menu_probe_capture_id": None,
                    "menu_ready_capture_id": None,
                    "menu_page_type": None,
                    "detail_action_execution_id": None,
                    "detail_probe_capture_id": None,
                    "detail_ready_capture_id": None,
                    "detail_page_type": None,
                    "detail_selection": "{}",
                    "diagnostic": "{}",
                    "menu_action_count": 0,
                    "detail_action_count": 0,
                    "execution_slot": None,
                }
            ]
        ],
    )
    repository = PostgresWebExplorationBudgetLedgerRepository(pool=FakePool(connection))

    summary = await repository.read_summary(
        agent_run_id=_command().agent_run_id,
        site_id="example",
    )

    assert summary.menu_ledger[0].status is WebMenuTerminalStatus.NAVIGATION_FAILED
    assert summary.menu_ledger[0].error_code == "WEB_BUDGET_TERMINAL_EVIDENCE_INVALID"


def test_ledger_contract_avoids_runtime_import_of_web_menu_exploration() -> None:
    ledger_name = "services.api.app.application.web_exploration_budget_ledger"
    explorer_name = "services.api.app.application.web_menu_exploration"
    saved_ledger = sys.modules.pop(ledger_name)
    saved_explorer = sys.modules[explorer_name]
    sys.modules[explorer_name] = ModuleType(explorer_name)
    try:
        imported = importlib.import_module(ledger_name)
        assert hasattr(imported, "WebExplorationBudgetLedgerPort")
    finally:
        sys.modules[ledger_name] = saved_ledger
        sys.modules[explorer_name] = saved_explorer
