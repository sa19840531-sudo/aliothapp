"""菜单规划序真实落库、冷读回及历史并列位置只读兼容。"""

import os
from dataclasses import replace

import pytest

from services.api.app.application.web_run_policy import build_new_web_navigation_context
from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _command,
    _database,
    _upgrade_isolated_postgres,  # noqa: F401 -- 注册显式隔离测试库迁移夹具。
)
from services.api.tests.test_web_runtime_discovery_postgres import _enabled_case
from services.api.tests.test_web_runtime_stage_frontier import candidate

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="只允许显式双 DSN 隔离测试库",
)


async def _setup(control, pool, ids):
    policy = await _enabled_case(control, pool, ids, runtime_limit=None, menu_limit=5)
    command = _command(ids)
    context = build_new_web_navigation_context(
        target_url=command.target_url, policy=policy,
    ).resolve_site_context(command.target_url)
    ledger = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
    await ledger.bootstrap(
        command, context, catalog_version=context.site.catalog_version,
        catalog_sha256=context.content_sha256,
    )
    return command, context.site, ledger


def _frontier(ledger, site, candidates=()):
    class Source:
        def bind_latest_navigation_capture(self, session_id, capture_id):
            self.capture_id = capture_id

        def navigation_candidates(self, session_id):
            return candidates

    return WebRuntimeStageFrontier(
        ledger=ledger, candidate_source=Source(), navigator=None, site=site,
    )


async def _rows(control, ids):
    return [dict(row) for row in await control.fetch(
        "SELECT menu_entry_id,catalog_position,discovery_manifest,discovery_source_capture_id "
        "FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid "
        "AND source_kind='runtime_discovered' ORDER BY menu_entry_id", ids["run"],
    )]


async def test_ranked_plan_roundtrips_across_repository_and_replay_cannot_reorder():
    async with _database() as (control, pool, other, ids):
        command, site, ledger = await _setup(control, pool, ids)
        planned = tuple(
            replace(candidate(label), source_capture_id=ids["capture"], menu_position=position)
            for label, position in (("c", 3), ("a", 1), ("b", 2))
        )
        await _frontier(ledger, site, planned).discover(
            command, source_capture_id=ids["capture"], page_role="home",
        )
        before = await _rows(control, ids)
        restored_ledger = PostgresWebExplorationBudgetLedgerRepository(pool=other)
        summary = await restored_ledger.read_summary(agent_run_id=ids["run"], site_id=site.site_id)
        assert [
            row.restored_navigation_candidate().url for row in summary.runtime_discovered_ledger
        ] == [
            item.url for item in planned
        ]
        assert summary.menu_limit == 5 and summary.executed_same_origin_count == 0
        frontier = _frontier(restored_ledger, site)
        first = await frontier.next_candidate(command, excluded_entry_ids=())
        second = await frontier.next_candidate(command, excluded_entry_ids=(first[0],))
        assert (first[1].url, second[1].url) == (planned[0].url, planned[1].url)

        # 重放即使调用者次序不同，也不能改写第一次冻结的身份与规划位置。
        await _frontier(restored_ledger, site, tuple(reversed(planned))).discover(
            command, source_capture_id=ids["capture"], page_role="home",
        )
        assert await _rows(control, ids) == before
        summary = await restored_ledger.read_summary(agent_run_id=ids["run"], site_id=site.site_id)
        assert [
            row.restored_navigation_candidate().url for row in summary.runtime_discovered_ledger
        ] == [
            item.url for item in planned
        ]


async def test_historical_tied_positions_read_stably_without_repairing_old_rows():
    async with _database() as (control, pool, other, ids):
        command, site, ledger = await _setup(control, pool, ids)
        for label in ("c", "a", "b"):
            await ledger.record_discovery(
                command=command, site=site,
                candidate=replace(candidate(label), source_capture_id=ids["capture"]),
                source_capture_id=ids["capture"], error_code="WEB_DISCOVERY_PENDING",
            )
        before = await _rows(control, ids)
        assert len({row["catalog_position"] for row in before}) == 1
        restored = PostgresWebExplorationBudgetLedgerRepository(pool=other)
        for reader in (ledger, restored):
            summary = await reader.read_summary(agent_run_id=ids["run"], site_id=site.site_id)
            assert [
                row.restored_navigation_candidate().url for row in summary.runtime_discovered_ledger
            ] == [
                "https://example.test/a", "https://example.test/b", "https://example.test/c",
            ]
        assert await _rows(control, ids) == before
