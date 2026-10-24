"""Web 硬预算的真实 PostgreSQL 双连接并发与崩溃窗口验收。

测试只在显式 test/primary 双 DSN 通过身份比较后运行，未配置时禁止迁移或回退。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
    WebMenuLedgerTerminalRecord,
)
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.application.web_menu_exploration import (
    WebMenuExplorer,
)
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy
from services.api.app.domain.page import (
    WebMenuDecision,
    WebSiteMenuCatalog,
    WebSiteMenuContext,
    WebSiteMenuEntry,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="需要同时显式设置隔离的 ALIOTH_TEST_DATABASE_URL 与主库 ALIOTH_DATABASE_URL",
)

_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _ROOT / "services" / "api" / "migrations" / "alembic.ini"
_CATALOG_VERSION = "postgres-integration-v1"
_CATALOG_SHA256 = "a" * 64


class WebBudgetPostgresIntegrationConfigurationError(RuntimeError):
    """真实 Web 预算 PG 测试缺少双 DSN 隔离证据。"""


def _require_web_budget_test_database_url(environ: Mapping[str, str]) -> str:
    test_database_url = environ.get("ALIOTH_TEST_DATABASE_URL")
    primary_database_url = environ.get("ALIOTH_DATABASE_URL")
    if not test_database_url:
        raise WebBudgetPostgresIntegrationConfigurationError(
            "必须显式设置 ALIOTH_TEST_DATABASE_URL"
        )
    if not primary_database_url:
        raise WebBudgetPostgresIntegrationConfigurationError(
            "必须显式设置 ALIOTH_DATABASE_URL 以比较数据库身份"
        )
    return require_isolated_test_database_url(
        test_database_url,
        primary_database_url,
    )


@pytest.fixture(scope="session", autouse=True)
def _upgrade_isolated_postgres() -> None:
    database_url = _test_database_url()
    previous = os.environ.get("ALIOTH_DATABASE_URL")
    os.environ["ALIOTH_DATABASE_URL"] = database_url
    try:
        alembic_command.upgrade(AlembicConfig(str(_ALEMBIC_INI)), "head")
    finally:
        if previous is None:
            os.environ.pop("ALIOTH_DATABASE_URL", None)
        else:
            os.environ["ALIOTH_DATABASE_URL"] = previous


@pytest.mark.asyncio
async def test_postgres_two_connections_enforce_menu_detail_and_eleventh_limits() -> None:
    async with _database() as (control, first_pool, second_pool, ids):
        await _seed_historical_budget(control, ids, menu_limit=10)
        async with first_pool.acquire() as first_connection:
            first_pid = await first_connection.fetchval("SELECT pg_backend_pid()")
        async with second_pool.acquire() as second_connection:
            second_pid = await second_connection.fetchval("SELECT pg_backend_pid()")
        assert first_pid != second_pid

        site = _site()
        run_command = _command(ids)
        first_repository = PostgresWebExplorationBudgetLedgerRepository(pool=first_pool)
        second_repository = PostgresWebExplorationBudgetLedgerRepository(pool=second_pool)
        await first_repository.bootstrap(
            run_command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )

        first_entry = site.entries[1]
        competing_menu = await asyncio.gather(
            first_repository.claim_menu(
                command=run_command,
                entry=first_entry,
                source_capture_id=ids["capture"],
            ),
            second_repository.claim_menu(
                command=run_command,
                entry=first_entry,
                source_capture_id=ids["capture"],
            ),
        )
        assert all(item is not None for item in competing_menu)
        assert sum(not item.already_reserved for item in competing_menu if item is not None) == 1
        assert sum(item.already_reserved for item in competing_menu if item is not None) == 1
        assert {item.execution_slot for item in competing_menu if item is not None} == {1}
        assert len({item.intent_id for item in competing_menu if item is not None}) == 1

        competing_detail = await asyncio.gather(
            first_repository.reserve_detail(
                command=run_command,
                entry=first_entry,
                source_capture_id=ids["capture"],
                candidate_id="candidate-1",
                destination_url="https://example.test/detail/1",
            ),
            second_repository.reserve_detail(
                command=run_command,
                entry=first_entry,
                source_capture_id=ids["capture"],
                candidate_id="candidate-1",
                destination_url="https://example.test/detail/1",
            ),
        )
        assert sum(not item.already_reserved for item in competing_detail) == 1
        assert sum(item.already_reserved for item in competing_detail) == 1
        assert len({item.intent_id for item in competing_detail}) == 1

        first_row = await control.fetchrow(
            """
            SELECT execution_slot, status, row_version,
                   menu_action_count, menu_intent_id,
                   menu_source_capture_id::text AS menu_source_capture_id,
                   menu_action_execution_id,
                   detail_action_count, detail_intent_id,
                   detail_source_capture_id::text AS detail_source_capture_id,
                   detail_candidate_id, detail_destination_sha256,
                   detail_action_execution_id
            FROM qc_web_menu_ledger_entry
            WHERE agent_run_id = $1::uuid AND site_id = $2 AND menu_entry_id = $3
            """,
            ids["run"],
            site.site_id,
            first_entry.entry_id,
        )
        assert first_row is not None
        assert dict(first_row) == {
            "execution_slot": 1,
            "status": "detail_reserved",
            "row_version": 2,
            "menu_action_count": 1,
            "menu_intent_id": competing_menu[0].intent_id,
            "menu_source_capture_id": ids["capture"],
            "menu_action_execution_id": None,
            "detail_action_count": 1,
            "detail_intent_id": competing_detail[0].intent_id,
            "detail_source_capture_id": ids["capture"],
            "detail_candidate_id": "candidate-1",
            "detail_destination_sha256": sha256(b"https://example.test/detail/1").hexdigest(),
            "detail_action_execution_id": None,
        }

        await first_pool.close()
        for entry in site.entries[2:11]:
            reservation = await second_repository.claim_menu(
                command=run_command,
                entry=entry,
                source_capture_id=ids["capture"],
            )
            assert reservation is not None
            assert reservation.already_reserved is False

        over_limit_entry = site.entries[11]
        assert (
            await second_repository.claim_menu(
                command=run_command,
                entry=over_limit_entry,
                source_capture_id=ids["capture"],
            )
            is None
        )

        crash_retry = await second_repository.reserve_detail(
            command=run_command,
            entry=first_entry,
            source_capture_id=ids["capture"],
            candidate_id="candidate-1",
            destination_url="https://example.test/detail/1",
        )
        assert crash_retry.already_reserved is True

        counts = await control.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE menu_action_count = 1) AS menu_actions,
                count(*) FILTER (WHERE detail_action_count = 1) AS detail_actions
            FROM qc_web_menu_ledger_entry
            WHERE agent_run_id = $1::uuid AND site_id = $2
            """,
            ids["run"],
            site.site_id,
        )
        assert counts is not None
        assert counts["menu_actions"] == 10
        assert counts["detail_actions"] == 1
        slots = await control.fetch(
            """
            SELECT execution_slot
            FROM qc_web_menu_ledger_entry
            WHERE agent_run_id = $1::uuid AND site_id = $2 AND menu_action_count = 1
            ORDER BY execution_slot
            """,
            ids["run"],
            site.site_id,
        )
        assert [row["execution_slot"] for row in slots] == list(
            range(1, 11)
        )
        assert max(row["execution_slot"] for row in slots) == 10
        over_limit = await control.fetchrow(
            """
            SELECT execution_slot, status, row_version,
                   menu_action_count, menu_intent_id, menu_source_capture_id,
                   menu_action_execution_id, menu_probe_capture_id,
                   menu_ready_capture_id, menu_page_type,
                   detail_action_count, detail_intent_id, detail_source_capture_id,
                   detail_candidate_id, detail_destination_sha256,
                   detail_action_execution_id, detail_probe_capture_id,
                   detail_ready_capture_id, detail_page_type
            FROM qc_web_menu_ledger_entry
            WHERE agent_run_id = $1::uuid AND site_id = $2 AND menu_entry_id = $3
            """,
            ids["run"],
            site.site_id,
            over_limit_entry.entry_id,
        )
        assert over_limit is not None
        assert dict(over_limit) == {
            "execution_slot": None,
            "status": "menu_limit_skipped",
            "row_version": 0,
            "menu_action_count": 0,
            "menu_intent_id": None,
            "menu_source_capture_id": None,
            "menu_action_execution_id": None,
            "menu_probe_capture_id": None,
            "menu_ready_capture_id": None,
            "menu_page_type": None,
            "detail_action_count": 0,
            "detail_intent_id": None,
            "detail_source_capture_id": None,
            "detail_candidate_id": None,
            "detail_destination_sha256": None,
            "detail_action_execution_id": None,
            "detail_probe_capture_id": None,
            "detail_ready_capture_id": None,
            "detail_page_type": None,
        }


@pytest.mark.asyncio
async def test_postgres_restart_after_menu_reservation_does_not_visit_again() -> None:
    async with _database() as (control, first_pool, second_pool, ids):
        site = _site()
        run_command = _command(ids)
        first_repository = PostgresWebExplorationBudgetLedgerRepository(pool=first_pool)
        await first_repository.bootstrap(
            run_command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )
        entry = site.entries[1]
        reservation = await first_repository.claim_menu(
            command=run_command,
            entry=entry,
            source_capture_id=ids["capture"],
        )
        assert reservation is not None and reservation.already_reserved is False

        await first_pool.close()
        visitor = _CountingVisitor()
        restarted_repository = PostgresWebExplorationBudgetLedgerRepository(pool=second_pool)
        summary = await WebMenuExplorer(
            navigation_context=_FrozenRegistry(site),
            candidate_source=_CandidateSource(_candidate(entry, source_capture_id=ids["capture"])),
            page_visitor=visitor,
            budget_ledger=restarted_repository,
        ).explore(run_command, initial_capture_id=ids["capture"])

        recovered = next(item for item in summary.menu_ledger if item.entry_id == entry.entry_id)
        assert visitor.visit_count == 0
        assert recovered.status is WebMenuTerminalStatus.NAVIGATION_FAILED
        assert recovered.error_code == "WEB_BUDGET_RESERVED_NEEDS_REVIEW"
        assert recovered.menu_action_count == 1
        assert recovered.menu_intent_id == reservation.intent_id
        assert recovered.menu_action_execution_id is None
        row = await control.fetchrow(
            """
            SELECT status, row_version, menu_action_count, detail_action_count
            FROM qc_web_menu_ledger_entry
            WHERE agent_run_id = $1::uuid AND site_id = $2 AND menu_entry_id = $3
            """,
            ids["run"],
            site.site_id,
            entry.entry_id,
        )
        assert dict(row) == {
            "status": "menu_reserved",
            "row_version": 1,
            "menu_action_count": 1,
            "detail_action_count": 0,
        }


@pytest.mark.asyncio
async def test_postgres_restart_preserves_durable_action_capture_and_finalizes_once() -> None:
    async with _database() as (control, first_pool, second_pool, ids):
        site = _site()
        run_command = _command(ids)
        first_repository = PostgresWebExplorationBudgetLedgerRepository(pool=first_pool)
        await first_repository.bootstrap(
            run_command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )
        entry = site.entries[1]
        menu_reservation = await first_repository.claim_menu(
            command=run_command,
            entry=entry,
            source_capture_id=ids["capture"],
        )
        assert menu_reservation is not None
        menu_evidence = await _seed_completed_action_evidence(
            control,
            ids,
            action_execution_id="pg-menu-action-1",
            intent_id=menu_reservation.intent_id,
            command_sequence=1,
            requested_source_capture_id=ids["capture"],
            parent_capture_id=ids["capture"],
        )
        detail_reservation = await first_repository.reserve_detail(
            command=run_command,
            entry=entry,
            source_capture_id=menu_evidence[1],
            candidate_id="candidate-1",
            destination_url="https://example.test/detail/1",
        )
        detail_evidence = await _seed_completed_action_evidence(
            control,
            ids,
            action_execution_id="pg-detail-action-1",
            intent_id=detail_reservation.intent_id,
            command_sequence=2,
            requested_source_capture_id=menu_evidence[1],
            parent_capture_id=menu_evidence[1],
        )

        await first_pool.close()
        visitor = _CountingVisitor()
        restarted_repository = PostgresWebExplorationBudgetLedgerRepository(pool=second_pool)
        recovered_summary = await WebMenuExplorer(
            navigation_context=_FrozenRegistry(site),
            candidate_source=_CandidateSource(_candidate(entry, source_capture_id=ids["capture"])),
            page_visitor=visitor,
            budget_ledger=restarted_repository,
        ).explore(run_command, initial_capture_id=ids["capture"])
        recovered = next(
            item for item in recovered_summary.menu_ledger if item.entry_id == entry.entry_id
        )
        assert visitor.visit_count == 0
        assert recovered.status is WebMenuTerminalStatus.NAVIGATION_FAILED
        assert recovered.error_code == "WEB_BUDGET_RESERVED_NEEDS_REVIEW"
        durable = await control.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM qc_action_execution
                 WHERE task_id = $1::uuid AND session_id = $2) AS action_count,
                (SELECT count(*) FROM qc_capture
                 WHERE task_id = $1::uuid AND surface_session_id = $2
                   AND action_execution_id IS NOT NULL) AS action_capture_count
            """,
            ids["task"],
            ids["session"],
        )
        assert dict(durable) == {"action_count": 2, "action_capture_count": 4}

        terminal = WebMenuLedgerTerminalRecord(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            site_id=site.site_id,
            menu_entry_id=entry.entry_id,
            status=WebMenuTerminalStatus.DETAIL_CAPTURED,
            menu_action_execution_id="pg-menu-action-1",
            menu_probe_capture_id=menu_evidence[0],
            menu_ready_capture_id=menu_evidence[1],
            menu_page_type="content_list",
            detail_action_execution_id="pg-detail-action-1",
            detail_probe_capture_id=detail_evidence[0],
            detail_ready_capture_id=detail_evidence[1],
            detail_page_type="content_detail",
            detail_selection={"candidate_id": "candidate-1"},
        )
        await restarted_repository.finalize(terminal)
        finalized = await restarted_repository.read_summary(
            agent_run_id=ids["run"],
            site_id=site.site_id,
        )
        finalized_entry = next(
            item for item in finalized.menu_ledger if item.entry_id == entry.entry_id
        )
        assert finalized_entry.status is WebMenuTerminalStatus.DETAIL_CAPTURED
        assert finalized_entry.menu_action_execution_id == "pg-menu-action-1"
        assert finalized_entry.menu_probe_capture_id == menu_evidence[0]
        assert finalized_entry.menu_ready_capture_id == menu_evidence[1]
        assert finalized_entry.detail_action_execution_id == "pg-detail-action-1"
        assert finalized_entry.detail_probe_capture_id == detail_evidence[0]
        assert finalized_entry.detail_ready_capture_id == detail_evidence[1]
        row = await control.fetchrow(
            """
            SELECT status, row_version, menu_action_count, detail_action_count,
                   menu_action_execution_id, detail_action_execution_id,
                   menu_page_type, detail_page_type
            FROM qc_web_menu_ledger_entry
            WHERE agent_run_id = $1::uuid AND site_id = $2 AND menu_entry_id = $3
            """,
            ids["run"],
            site.site_id,
            entry.entry_id,
        )
        assert dict(row) == {
            "status": "detail_captured",
            "row_version": 3,
            "menu_action_count": 1,
            "detail_action_count": 1,
            "menu_action_execution_id": "pg-menu-action-1",
            "detail_action_execution_id": "pg-detail-action-1",
            "menu_page_type": "content_list",
            "detail_page_type": "content_detail",
        }
        with pytest.raises(WebExplorationBudgetLedgerConflictError):
            await restarted_repository.finalize(terminal)
        with pytest.raises(WebExplorationBudgetLedgerConflictError):
            await restarted_repository.finalize(
                _zero_action_record(
                    ids,
                    site,
                    entry,
                    WebMenuTerminalStatus.ALLOWLIST_DRIFTED,
                )
            )
        assert await _row_version(control, ids, site, entry) == 3


@pytest.mark.asyncio
async def test_postgres_zero_action_terminal_is_pending_only_and_irreversible() -> None:
    async with _database() as (control, first_pool, second_pool, ids):
        site = _site()
        run_command = _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=first_pool)
        await repository.bootstrap(
            run_command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )
        allowlist_entry, human_entry, reserved_entry = site.entries[1:4]
        with pytest.raises(WebExplorationBudgetLedgerConflictError):
            await repository.finalize(
                WebMenuLedgerTerminalRecord(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    session_id=ids["session"],
                    site_id=site.site_id,
                    menu_entry_id=allowlist_entry.entry_id,
                    status=WebMenuTerminalStatus.ALLOWLIST_DRIFTED,
                )
            )
        assert await _row_version(control, ids, site, allowlist_entry) == 0
        await repository.finalize(
            _zero_action_record(
                ids,
                site,
                allowlist_entry,
                WebMenuTerminalStatus.ALLOWLIST_DRIFTED,
            )
        )
        await repository.finalize(
            _zero_action_record(
                ids,
                site,
                human_entry,
                WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
            )
        )
        reserved = await repository.claim_menu(
            command=run_command,
            entry=reserved_entry,
            source_capture_id=ids["capture"],
        )
        assert reserved is not None
        for status in (
            WebMenuTerminalStatus.ALLOWLIST_DRIFTED,
            WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
        ):
            with pytest.raises(WebExplorationBudgetLedgerConflictError):
                await repository.finalize(_zero_action_record(ids, site, reserved_entry, status))
        with pytest.raises(WebExplorationBudgetLedgerConflictError):
            await repository.finalize(
                _zero_action_record(
                    ids,
                    site,
                    allowlist_entry,
                    WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
                )
            )
        assert await _row_version(control, ids, site, allowlist_entry) == 1

        rows = await control.fetch(
            """
            SELECT menu_entry_id, status, row_version,
                   menu_action_count, menu_intent_id, menu_source_capture_id,
                   menu_action_execution_id, menu_probe_capture_id::text,
                   detail_action_count, detail_intent_id, detail_source_capture_id,
                   detail_action_execution_id
            FROM qc_web_menu_ledger_entry
            WHERE agent_run_id = $1::uuid AND site_id = $2
              AND menu_entry_id = ANY($3::varchar[])
            ORDER BY catalog_position
            """,
            ids["run"],
            site.site_id,
            [allowlist_entry.entry_id, human_entry.entry_id],
        )
        assert [row["status"] for row in rows] == [
            "allowlist_drifted",
            "human_verification_blocked",
        ]
        assert all(row["row_version"] == 1 for row in rows)
        assert all(row["menu_action_count"] == 0 for row in rows)
        assert all(row["detail_action_count"] == 0 for row in rows)
        assert all(row["menu_probe_capture_id"] == ids["capture"] for row in rows)
        assert all(
            row[field] is None
            for row in rows
            for field in (
                "menu_intent_id",
                "menu_source_capture_id",
                "menu_action_execution_id",
                "detail_intent_id",
                "detail_source_capture_id",
                "detail_action_execution_id",
            )
        )


@pytest.mark.asyncio
async def test_postgres_zero_action_terminal_uses_frozen_menu_limit_for_slots_six_and_ten() -> None:
    """新任务允许第 6/10 菜单收口，历史冻结为 5 时仍拒绝第 6 个。"""

    async with _database() as (control, first_pool, _second_pool, ids):
        site = _site()
        command = _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=first_pool)
        await repository.bootstrap(
            command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )

        slot_six, slot_ten = site.entries[6], site.entries[10]
        await repository.finalize(
            _zero_action_record(
                ids,
                site,
                slot_six,
                WebMenuTerminalStatus.ALLOWLIST_DRIFTED,
            )
        )
        await repository.finalize(
            _zero_action_record(
                ids,
                site,
                slot_ten,
                WebMenuTerminalStatus.HUMAN_VERIFICATION_BLOCKED,
            )
        )
        rows = await control.fetch(
            """
            SELECT execution_slot, status
            FROM qc_web_menu_ledger_entry
            WHERE agent_run_id = $1::uuid
              AND site_id = $2
              AND menu_entry_id = ANY($3::varchar[])
            ORDER BY execution_slot
            """,
            ids["run"],
            site.site_id,
            [slot_six.entry_id, slot_ten.entry_id],
        )
        assert [dict(row) for row in rows] == [
            {"execution_slot": 6, "status": "allowlist_drifted"},
            {"execution_slot": 10, "status": "human_verification_blocked"},
        ]

        await control.execute(
            """
            UPDATE qc_web_exploration_budget
            SET menu_limit = 5
            WHERE agent_run_id = $1::uuid AND site_id = $2
            """,
            ids["run"],
            site.site_id,
        )
        with pytest.raises(WebExplorationBudgetLedgerConflictError):
            await repository.finalize(
                _zero_action_record(
                    ids,
                    site,
                    site.entries[7],
                    WebMenuTerminalStatus.ALLOWLIST_DRIFTED,
                )
            )


@pytest.mark.asyncio
async def test_postgres_unclassified_home_terminal_keeps_menu_budget_claimable() -> None:
    """首页待复核必须闭合自身账本，但不能吞掉后续十菜单预算。"""

    async with _database() as (control, first_pool, _second_pool, ids):
        site = _site()
        run_command = _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=first_pool)
        await repository.bootstrap(
            run_command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )

        await repository.finalize(
            WebMenuLedgerTerminalRecord(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                site_id=site.site_id,
                menu_entry_id=site.entries[0].entry_id,
                status=WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED,
                menu_probe_capture_id=ids["capture"],
                menu_ready_capture_id=ids["capture"],
                diagnostic={"error_code": "WEB_PAGE_TYPE_UNCLASSIFIED"},
            )
        )

        home = await control.fetchrow(
            """
            SELECT status, menu_action_count, menu_intent_id,
                   menu_page_type, diagnostic
            FROM qc_web_menu_ledger_entry
            WHERE agent_run_id = $1::uuid
              AND site_id = $2
              AND menu_entry_id = $3
            """,
            ids["run"],
            site.site_id,
            site.entries[0].entry_id,
        )
        assert {**dict(home), "diagnostic": json.loads(home["diagnostic"])} == {
            "status": "page_type_unclassified",
            "menu_action_count": 0,
            "menu_intent_id": None,
            "menu_page_type": None,
            "diagnostic": {"error_code": "WEB_PAGE_TYPE_UNCLASSIFIED"},
        }

        reservation = await repository.claim_menu(
            command=run_command,
            entry=site.entries[1],
            source_capture_id=ids["capture"],
        )
        assert reservation is not None
        assert reservation.already_reserved is False
        assert reservation.execution_slot == 1


@pytest.mark.asyncio
async def test_postgres_unclassified_home_terminal_requires_fixed_diagnostic() -> None:
    """首页待复核例外必须携带固定诊断码，禁止扩成任意无类型终态。"""

    async with _database() as (_control, first_pool, _second_pool, ids):
        site = _site()
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=first_pool)
        await repository.bootstrap(
            _command(ids),
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )

        with pytest.raises(WebExplorationBudgetLedgerConflictError):
            await repository.finalize(
                WebMenuLedgerTerminalRecord(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    session_id=ids["session"],
                    site_id=site.site_id,
                    menu_entry_id=site.entries[0].entry_id,
                    status=WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED,
                    menu_probe_capture_id=ids["capture"],
                    menu_ready_capture_id=ids["capture"],
                    diagnostic={},
                )
            )


class _FrozenRegistry:
    version = _CATALOG_VERSION
    catalog_content_sha256 = _CATALOG_SHA256
    discovery_policy = WebDiscoveryPolicy.for_new_run()

    def __init__(self, site: WebSiteMenuCatalog) -> None:
        self._site = replace(site, catalog_version=self.version)

    def resolve_site(self, target_url: str) -> WebSiteMenuCatalog:
        assert target_url == "https://example.test/"
        return self._site

    def resolve_site_context(self, target_url: str) -> WebSiteMenuContext:
        return WebSiteMenuContext(
            site=self.resolve_site(target_url),
            source_kind="catalog",
            content_sha256=self.catalog_content_sha256,
        )


class _CandidateSource:
    def __init__(self, candidate: WebNavigationCandidate) -> None:
        self._candidate = candidate

    def navigation_candidates(self, session_id: str) -> tuple[WebNavigationCandidate, ...]:
        assert session_id
        return (self._candidate,)


class _CountingVisitor:
    def __init__(self) -> None:
        self.visit_count = 0

    async def visit(self, **kwargs: object) -> None:
        del kwargs
        self.visit_count += 1
        raise AssertionError("已提交 reservation 的重启路径不得再次执行 visitor")


def _candidate(
    entry: WebSiteMenuEntry,
    *,
    source_capture_id: str,
) -> WebNavigationCandidate:
    return WebNavigationCandidate(
        entry_id=f"runtime-{entry.entry_id}",
        label=entry.menu_path[-1],
        url=entry.canonical_url,
        menu_path=entry.menu_path,
        dom_fingerprint=f"dom-{entry.entry_id}",
        source_signal="semantic_nav",
        page_type_key="nav-class:postgres-integration",
        source_capture_id=source_capture_id,
    )


def _zero_action_record(
    ids: Mapping[str, str],
    site: WebSiteMenuCatalog,
    entry: WebSiteMenuEntry,
    status: WebMenuTerminalStatus,
) -> WebMenuLedgerTerminalRecord:
    return WebMenuLedgerTerminalRecord(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        session_id=ids["session"],
        site_id=site.site_id,
        menu_entry_id=entry.entry_id,
        status=status,
        menu_probe_capture_id=ids["capture"],
    )


async def _row_version(
    connection: asyncpg.Connection,
    ids: Mapping[str, str],
    site: WebSiteMenuCatalog,
    entry: WebSiteMenuEntry,
) -> int:
    value = await connection.fetchval(
        """
        SELECT row_version
        FROM qc_web_menu_ledger_entry
        WHERE agent_run_id = $1::uuid AND site_id = $2 AND menu_entry_id = $3
        """,
        ids["run"],
        site.site_id,
        entry.entry_id,
    )
    return int(value)


async def _seed_completed_action_evidence(
    connection: asyncpg.Connection,
    ids: Mapping[str, str],
    *,
    action_execution_id: str,
    intent_id: str,
    command_sequence: int,
    requested_source_capture_id: str,
    parent_capture_id: str,
) -> tuple[str, str]:
    probe_capture_id = str(uuid4())
    ready_capture_id = str(uuid4())
    async with connection.transaction():
        await connection.executemany(
            """
            INSERT INTO qc_capture (
                id, task_id, agent_run_id, surface_kind, surface_session_id,
                parent_capture_id, action_execution_id, capture_role,
                session_command_sequence, capture_status, captured_at
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, 'web', $4,
                $5::uuid, $6, $7, $8, 'captured', now()
            )
            """,
            (
                (
                    probe_capture_id,
                    ids["task"],
                    ids["run"],
                    ids["session"],
                    parent_capture_id,
                    action_execution_id,
                    "before_action",
                    command_sequence,
                ),
                (
                    ready_capture_id,
                    ids["task"],
                    ids["run"],
                    ids["session"],
                    probe_capture_id,
                    action_execution_id,
                    "after_action",
                    command_sequence,
                ),
            ),
        )
        await connection.execute(
            """
            INSERT INTO qc_action_execution (
                action_execution_id, task_id, session_id, intent_id,
                idempotency_key, intent_hash, intent_protocol_version,
                intent_payload, command_sequence, finalization_epoch,
                requested_source_capture_id, source_capture_id, result_capture_id,
                status, result_payload, started_at, action_finished_at,
                evidence_finished_at
            ) VALUES (
                $1, $2::uuid, $3, $4,
                $4, $5, 'web-budget-integration/v1',
                '{}'::jsonb, $6, 0,
                $7::uuid, $8::uuid, $9::uuid,
                'completed', '{}'::jsonb, now(), now(), now()
            )
            """,
            action_execution_id,
            ids["task"],
            ids["session"],
            intent_id,
            sha256(intent_id.encode("utf-8")).hexdigest(),
            command_sequence,
            requested_source_capture_id,
            probe_capture_id,
            ready_capture_id,
        )
    return probe_capture_id, ready_capture_id


async def _cleanup_web_budget_run(
    connection: asyncpg.Connection,
    ids: Mapping[str, str],
) -> None:
    await connection.execute(
        "DELETE FROM qc_web_exploration_budget WHERE agent_run_id = $1::uuid",
        ids["run"],
    )
    await connection.execute(
        "DELETE FROM qc_task WHERE id = $1::uuid",
        ids["task"],
    )
    await connection.execute(
        "DELETE FROM qc_ruleset WHERE id = $1::uuid",
        ids["ruleset"],
    )
    await connection.execute(
        "DELETE FROM qc_user WHERE id = $1::uuid",
        ids["user"],
    )
    await connection.execute(
        "DELETE FROM qc_tenant WHERE id = $1::uuid",
        ids["tenant"],
    )


@asynccontextmanager
async def _database() -> AsyncIterator[
    tuple[asyncpg.Connection, asyncpg.Pool, asyncpg.Pool, dict[str, str]]
]:
    database_url = _test_database_url()
    ids = {
        "tenant": str(uuid4()),
        "user": str(uuid4()),
        "ruleset": str(uuid4()),
        "task": str(uuid4()),
        "run": str(uuid4()),
        "session": f"web:{uuid4()}",
        "capture": str(uuid4()),
    }
    control = await asyncpg.connect(database_url)
    first_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=1)
    second_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=1)
    try:
        await _seed_web_run(control, ids)
        yield control, first_pool, second_pool, ids
    finally:
        try:
            await _cleanup_web_budget_run(control, ids)
        finally:
            try:
                await first_pool.close()
            finally:
                try:
                    await second_pool.close()
                finally:
                    await control.close()


def _test_database_url() -> str:
    try:
        database_url = _require_web_budget_test_database_url(os.environ)
    except WebBudgetPostgresIntegrationConfigurationError as exc:
        pytest.skip(str(exc))
    # Alembic 接受 SQLAlchemy 的 asyncpg URL；asyncpg 直连只接受 PostgreSQL URL。
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _command(ids: dict[str, str]) -> RunWebWorkflowCommand:
    return RunWebWorkflowCommand(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        session_id=ids["session"],
        session_owner="web-v2-workflow",
        target_url="https://example.test/",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=10),
    )


async def _seed_historical_budget(connection, ids, *, menu_limit: int) -> None:
    await connection.execute(
        """
        INSERT INTO qc_web_exploration_budget (
            agent_run_id, site_id, task_id, session_id, surface_kind, session_owner,
            target_origin_sha256, catalog_version, catalog_sha256, menu_limit, state
        ) VALUES ($1::uuid,$2,$3::uuid,$4,'web','web-v2-workflow',$5,$6,$7,$8,'open')
        """,
        ids["run"], _site().site_id, ids["task"], ids["session"],
        sha256(b"https://example.test").hexdigest(), _CATALOG_VERSION, _CATALOG_SHA256,
        menu_limit,
    )
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        _BOOTSTRAP_ENTRIES_SQL,
        _catalog_payload,
    )
    await connection.fetch(
        _BOOTSTRAP_ENTRIES_SQL, ids["run"], _site().site_id,
        json.dumps(_catalog_payload(_site(), menu_limit=menu_limit)),
    )


@pytest.mark.asyncio
async def test_postgres_unbounded_budget_persists_twelve_slots_and_cold_resume() -> None:
    async with _database() as (control, pool, other, ids):
        site = _site()
        site = replace(site, entries=site.entries + (replace(
            site.entries[-1], entry_id="menu-12", menu_path=("菜单", "12"),
            href="/menu/12", canonical_url="https://example.test/menu/12",
        ),))
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        await repository.bootstrap(_command(ids), site, catalog_version=_CATALOG_VERSION,
                                   catalog_sha256=_CATALOG_SHA256)
        for entry in site.entries[1:]:
            assert await repository.claim_menu(command=_command(ids), entry=entry,
                                               source_capture_id=ids["capture"])
        row = await control.fetchrow(
            "SELECT menu_limit FROM qc_web_exploration_budget WHERE agent_run_id=$1::uuid",
            ids["run"],
        )
        assert row["menu_limit"] is None
        slots = await control.fetch(
            "SELECT execution_slot FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid "
            "AND execution_slot IS NOT NULL ORDER BY execution_slot", ids["run"],
        )
        assert [item["execution_slot"] for item in slots] == list(range(1, 13))
        restored = PostgresWebExplorationBudgetLedgerRepository(pool=other)
        await restored.bootstrap(_command(ids), site, catalog_version=_CATALOG_VERSION,
                                 catalog_sha256=_CATALOG_SHA256)
        summary = await restored.read_summary(agent_run_id=ids["run"], site_id=site.site_id)
        assert summary.menu_limit is None
        assert summary.executed_same_origin_count == 12


@pytest.mark.asyncio
async def test_postgres_historical_five_budget_rejects_slot_six() -> None:
    async with _database() as (control, pool, _other, ids):
        await _seed_historical_budget(control, ids, menu_limit=5)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        await repository.bootstrap(_command(ids), _site(), catalog_version=_CATALOG_VERSION,
                                   catalog_sha256=_CATALOG_SHA256)
        assert await repository.claim_menu(command=_command(ids), entry=_site().entries[6],
                                           source_capture_id=ids["capture"]) is None
        with pytest.raises(asyncpg.CheckViolationError):
            await control.execute(
                "UPDATE qc_web_menu_ledger_entry SET execution_slot=6, status='pending' "
                "WHERE agent_run_id=$1::uuid AND menu_entry_id='menu-6'", ids["run"],
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("menu_limit,slot", [(None, 1), (7, 1), (10, 11), (None, 40000)])
async def test_unbounded_migration_downgrade_rejects_incompatible_data(menu_limit, slot):
    from services.api.tests.test_web_exploration_budget_ledger_migration import (
        MIGRATION_PATH,
        RecordingOperations,
        _load_migration,
    )
    migration = _load_migration(MIGRATION_PATH.with_name("20260914_0117_web_unbounded_menu_budget.py"))
    operations = RecordingOperations()
    migration.op = operations
    migration.downgrade()
    async with _database() as (control, _pool, _other, ids):
        await _seed_historical_budget(control, ids, menu_limit=5)
        await control.execute(
            "UPDATE qc_web_exploration_budget SET menu_limit=NULL "
            "WHERE agent_run_id=$1::uuid",
            ids["run"],
        )
        await control.execute(
            "UPDATE qc_web_menu_ledger_entry SET execution_slot=$2 "
            "WHERE agent_run_id=$1::uuid AND menu_entry_id='menu-1'",
            ids["run"],
            slot,
        )
        await control.execute(
            "UPDATE qc_web_exploration_budget SET menu_limit=$2 "
            "WHERE agent_run_id=$1::uuid",
            ids["run"],
            menu_limit,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            async with control.transaction():
                for statement in operations.statements:
                    await control.execute(statement)
        assert (
            await control.fetchval(
                "SELECT menu_limit FROM qc_web_exploration_budget "
                "WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            == menu_limit
        )
        assert (
            await control.fetchval(
                "SELECT execution_slot FROM qc_web_menu_ledger_entry "
                "WHERE agent_run_id=$1::uuid AND menu_entry_id='menu-1'",
                ids["run"],
            )
            == slot
        )


@pytest.mark.asyncio
async def test_unbounded_migration_roundtrip_preserves_historical_budget_and_constraints():
    from services.api.tests.test_web_exploration_budget_ledger_migration import (
        MIGRATION_PATH,
        RecordingOperations,
        _load_migration,
    )
    migration = _load_migration(MIGRATION_PATH.with_name("20260914_0117_web_unbounded_menu_budget.py"))
    async with _database() as (control, _pool, _other, ids):
        await _seed_historical_budget(control, ids, menu_limit=5)
        transaction = control.transaction()
        await transaction.start()
        try:
            operations = RecordingOperations()
            migration.op = operations
            migration.downgrade()
            for statement in operations.statements:
                await control.execute(statement)
            assert await control.fetchval(
                "SELECT data_type FROM information_schema.columns WHERE table_name="
                "'qc_web_menu_ledger_entry' AND column_name='execution_slot'"
            ) == "smallint"
            for limit in (None, 7):
                with pytest.raises(asyncpg.IntegrityConstraintViolationError):
                    async with control.transaction():
                        await control.execute(
                            "UPDATE qc_web_exploration_budget SET menu_limit=$2 "
                            "WHERE agent_run_id=$1::uuid",
                            ids["run"],
                            limit,
                        )
            operations.statements.clear()
            migration.upgrade()
            for statement in operations.statements:
                await control.execute(statement)
            assert (
                await control.fetchval(
                    "SELECT menu_limit FROM qc_web_exploration_budget "
                    "WHERE agent_run_id=$1::uuid",
                    ids["run"],
                )
                == 5
            )
            with pytest.raises(asyncpg.CheckViolationError):
                async with control.transaction():
                    await control.execute(
                        "UPDATE qc_web_menu_ledger_entry SET execution_slot=6 "
                        "WHERE agent_run_id=$1::uuid AND menu_entry_id='menu-1'",
                        ids["run"],
                    )
            await control.execute(
                "UPDATE qc_web_exploration_budget SET menu_limit=NULL "
                "WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            await control.execute(
                "UPDATE qc_web_menu_ledger_entry SET execution_slot=40000 "
                "WHERE agent_run_id=$1::uuid AND menu_entry_id='menu-1'",
                ids["run"],
            )
            for slot in (0, -1):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with control.transaction():
                        await control.execute(
                            "UPDATE qc_web_menu_ledger_entry SET execution_slot=$2 "
                            "WHERE agent_run_id=$1::uuid AND menu_entry_id='menu-1'",
                            ids["run"],
                            slot,
                        )
            with pytest.raises(asyncpg.UniqueViolationError):
                async with control.transaction():
                    await control.execute(
                        "UPDATE qc_web_menu_ledger_entry SET execution_slot=40000 "
                        "WHERE agent_run_id=$1::uuid AND menu_entry_id='menu-2'",
                        ids["run"],
                    )
        finally:
            await transaction.rollback()


def _site() -> WebSiteMenuCatalog:
    entries = (
        WebSiteMenuEntry(
            entry_id="home",
            menu_path=("首页",),
            href="/",
            canonical_url="https://example.test/",
            decision=WebMenuDecision.HOME_BASELINE,
            is_same_origin=True,
        ),
        *(
            WebSiteMenuEntry(
                entry_id=f"menu-{index}",
                menu_path=("菜单", str(index)),
                href=f"/menu/{index}",
                canonical_url=f"https://example.test/menu/{index}",
                decision=WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY,
                is_same_origin=True,
            )
            for index in range(1, 12)
        ),
    )
    return WebSiteMenuCatalog(
        site_id="postgres-integration",
        origin="https://example.test",
        navigation_evidence={"active_anchor_count": len(entries)},
        entries=entries,
    )


async def _seed_web_run(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $2, $3)
        """,
        ids["tenant"],
        f"web-budget-{ids['tenant']}",
        f"Web Budget Integration {ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (
            id, username, full_name, password_hash, role, tenant_id
        )
        VALUES (
            $1::uuid, $2, 'Web Budget Integration', 'not-a-login-secret',
            '平台管理员', $3::uuid
        )
        """,
        ids["user"],
        f"web-budget-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        )
        VALUES ($1::uuid, $2, 'Web Budget Integration', 1,
                'enabled', 'web-budget-integration', 0, 'manual')
        """,
        ids["ruleset"],
        f"web-budget-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, target_platform, task_config,
            tenant_id, submitter_id
        )
        VALUES (
            $1::uuid, 'https://example.test/', $2::uuid, 'running', 'web', '{}'::jsonb,
            $3::uuid, $4::uuid
        )
        """,
        ids["task"],
        ids["ruleset"],
        ids["tenant"],
        ids["user"],
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
        VALUES ($1::uuid, $2::uuid, 'main', 'Web Budget Integration', 'running')
        """,
        ids["run"],
        ids["task"],
    )
    await connection.execute(
        """
        INSERT INTO qc_surface_session (
            session_id, task_id, surface_kind, session_owner, status, activated_at
        )
        VALUES ($1, $2::uuid, 'web', 'web-v2-workflow', 'active', now())
        """,
        ids["session"],
        ids["task"],
    )
    await connection.execute(
        """
        INSERT INTO qc_capture (
            id, task_id, agent_run_id, surface_kind, surface_session_id,
            capture_role, capture_status, captured_at
        )
        VALUES ($1::uuid, $2::uuid, $3::uuid, 'web', $4,
                'probe_before', 'captured', now())
        """,
        ids["capture"],
        ids["task"],
        ids["run"],
        ids["session"],
    )
    # 当前页面指针必须引用同一任务、运行和会话的真实采集记录。
    await connection.execute(
        "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
        ids["capture"],
        ids["session"],
    )
