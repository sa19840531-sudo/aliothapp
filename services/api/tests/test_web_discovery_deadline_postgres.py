"""冻结截止/收口预留约束目录与目录外的同一预算。"""

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
    WebMenuLedgerTerminalRecord,
)
from services.api.app.application.web_menu_exploration import WebMenuExplorer
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _CATALOG_SHA256,
    _CATALOG_VERSION,
    _command,
    _database,
    _site,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _upgrade_isolated_postgres as _upgrade_isolated_postgres,
)
from services.api.tests.test_web_runtime_discovery_postgres import _enabled_case

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="只允许显式隔离PG"
)


@pytest.mark.asyncio
async def test_frozen_closeout_reserve_also_blocks_catalog_and_detail_claims():
    async with _database() as (control, pool, _other, ids):
        policy = await _enabled_case(control, pool, ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        site, command = _site(), _command(ids)
        await repository.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        entry = site.entries[1]
        assert await repository.claim_menu(
            command=command, entry=entry, source_capture_id=ids["capture"]
        )
        near = replace(command, deadline=datetime.now(UTC) + timedelta(seconds=10))
        assert (
            await repository.claim_menu(
                command=near, entry=site.entries[2], source_capture_id=ids["capture"]
            )
            is None
        )
        with pytest.raises(WebExplorationBudgetLedgerConflictError, match="CLOSEOUT_RESERVED"):
            await repository.reserve_detail(
                command=near,
                entry=entry,
                source_capture_id=ids["capture"],
                candidate_id="detail",
                destination_url="https://example.test/article",
            )
        row = await control.fetchrow(
            "SELECT SUM(menu_action_count) AS menus,SUM(detail_action_count) AS details "
            "FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid",
            ids["run"],
        )
        assert (row["menus"], row["details"]) == (1, 0)


@pytest.mark.asyncio
async def test_catalog_closeout_settles_observed_discovery_and_restores_without_pending():
    async with _database() as (control, pool, other, ids):
        policy = await _enabled_case(control, pool, ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        site = _site()
        command = replace(_command(ids), deadline=datetime.now(UTC) + timedelta(seconds=10))
        await repository.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        invalid_home = WebMenuLedgerTerminalRecord(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            site_id=site.site_id,
            menu_entry_id="home",
            status=WebMenuTerminalStatus.HOME_BASELINE,
            menu_probe_capture_id=ids["capture"],
            menu_ready_capture_id=ids["capture"],
            menu_page_type="content_list",
        )
        # 只有 probe、没有正式分类的首页不能冒充成功；保留失败再验证零动作收口。
        with pytest.raises(WebExplorationBudgetLedgerConflictError, match="无效"):
            await repository.finalize(invalid_home)
        await repository.finalize(replace(
            invalid_home, status=WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED,
            menu_page_type=None,
            diagnostic={"error_code": "WEB_PAGE_TYPE_UNCLASSIFIED"},
        ))
        original = await control.fetch(
            "SELECT menu_entry_id,execution_slot,status,diagnostic FROM qc_web_menu_ledger_entry "
            "WHERE agent_run_id=$1::uuid ORDER BY catalog_position",
            ids["run"],
        )
        frozen = await control.fetchval(
            "SELECT payload_sha256 FROM qc_web_run_policy_snapshot WHERE agent_run_id=$1::uuid",
            ids["run"],
        )
        entry = site.entries[1]
        candidates = (
            WebNavigationCandidate(
                entry_id="matched-catalog",
                label=entry.menu_path[-1],
                menu_path=entry.menu_path,
                url=entry.canonical_url,
                dom_fingerprint="catalog",
                source_signal="semantic_nav",
                page_type_key="unknown",
                source_capture_id=ids["capture"],
            ),
            WebNavigationCandidate(
                entry_id="observed-news",
                label="新资讯",
                menu_path=("新资讯",),
                url="https://example.test/news",
                dom_fingerprint="news",
                source_signal="semantic_nav",
                page_type_key="unknown",
                source_capture_id=ids["capture"],
            ),
            WebNavigationCandidate(
                entry_id="observed-login",
                label="账号登录",
                menu_path=("账号登录",),
                url="https://example.test/login",
                dom_fingerprint="login",
                source_signal="semantic_nav",
                page_type_key="unknown",
                source_capture_id=ids["capture"],
            ),
        )

        class NoActionVisitor:
            async def visit(self, **kwargs):
                raise AssertionError("收口预留不足不得发菜单动作")

        def explorer(ledger):
            return WebMenuExplorer(
                navigation_context=SimpleNamespace(
                    resolve_site_context=lambda url: SimpleNamespace(site=replace(site, catalog_version=_CATALOG_VERSION), content_sha256=_CATALOG_SHA256),
                    version=_CATALOG_VERSION,
                    catalog_content_sha256=_CATALOG_SHA256,
                ),
                candidate_source=SimpleNamespace(navigation_candidates=lambda session: candidates),
                page_visitor=NoActionVisitor(),
                budget_ledger=ledger,
                discovery_policy=policy.discovery_policy,
                required_page_types=policy.required_page_types,
            )

        first = await explorer(repository).explore(command, initial_capture_id=ids["capture"])
        assert first.frontier_remaining == 0
        assert first.executed_same_origin_count == 0
        assert {row.menu_path[-1]: row.error_code for row in first.runtime_discovered_ledger} == {
            "新资讯": "WEB_DISCOVERY_CLOSEOUT_RESERVED",
            "账号登录": "WEB_DISCOVERY_LOGIN_BLOCKED",
        }
        rows = await control.fetch(
            "SELECT menu_entry_id,execution_slot,status,diagnostic,menu_action_count "
            "FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid ORDER BY catalog_position",
            ids["run"],
        )
        assert not any(
            row["status"] in {"pending", "home_pending", "menu_reserved", "detail_reserved"}
            for row in rows
        )
        assert all(row["menu_action_count"] == 0 for row in rows)
        assert not await control.fetchval(
            "SELECT count(*) FROM qc_action_execution WHERE task_id=$1::uuid", ids["task"]
        )
        for before in original:
            after = next(row for row in rows if row["menu_entry_id"] == before["menu_entry_id"])
            assert after["execution_slot"] == before["execution_slot"]
            if before["status"] == "pending":
                assert after["status"] == "navigation_failed"
                assert "WEB_MENU_CLOSEOUT_RESERVED" in after["diagnostic"]
            else:
                assert (after["status"], after["diagnostic"]) == (
                    before["status"],
                    before["diagnostic"],
                )
        recovered = PostgresWebExplorationBudgetLedgerRepository(pool=other, frozen_policy=policy)
        await recovered.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        cold = await explorer(recovered).explore(command, initial_capture_id=ids["capture"])
        assert cold.to_dict() == first.to_dict()
        assert (
            await control.fetchval(
                "SELECT payload_sha256 FROM qc_web_run_policy_snapshot WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            == frozen
        )
        assert set(policy.required_page_types) == {"content_list", "content_detail"}


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["reserved", "owner", "task_failed", "run_failed", "sealed"])
async def test_closeout_does_not_settle_unknown_reservation_or_lost_scope(boundary):
    async with _database() as (control, pool, _other, ids):
        policy = await _enabled_case(control, pool, ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        site, command = _site(), _command(ids)
        await repository.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        if boundary == "reserved":
            await repository.claim_menu(
                command=command, entry=site.entries[1], source_capture_id=ids["capture"]
            )
        elif boundary == "owner":
            await control.execute(
                "UPDATE qc_surface_session SET session_owner='h5-v2-workflow' WHERE session_id=$1",
                ids["session"],
            )
        elif boundary in {"task_failed", "run_failed"}:
            table, row_id = (
                ("qc_task", ids["task"])
                if boundary == "task_failed"
                else ("qc_agent_run", ids["run"])
            )
            await control.execute(f"UPDATE {table} SET status='failed' WHERE id=$1::uuid", row_id)
        else:
            await control.execute(
                "UPDATE qc_web_exploration_budget SET state='sealed',sealed_at=now() "
                "WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
        before = await control.fetch(
            "SELECT * FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid "
            "ORDER BY catalog_position",
            ids["run"],
        )
        near = replace(command, deadline=datetime.now(UTC) + timedelta(seconds=10))
        if boundary in {"owner", "task_failed", "run_failed"}:
            with pytest.raises(ValueError, match="SCOPE_INVALID"):
                await repository.claim_menu(
                    command=near, entry=site.entries[2], source_capture_id=ids["capture"]
                )
        else:
            assert (
                await repository.claim_menu(
                    command=near, entry=site.entries[2], source_capture_id=ids["capture"]
                )
                is None
            )
        after = await control.fetch(
            "SELECT * FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid "
            "ORDER BY catalog_position",
            ids["run"],
        )
        assert [dict(row) for row in after] == [dict(row) for row in before]
