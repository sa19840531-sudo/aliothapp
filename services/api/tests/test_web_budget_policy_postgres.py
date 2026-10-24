"""冻结菜单额度与实际执行账本一致性的真实数据库回归。"""

import asyncio
import os
from dataclasses import replace

import pytest

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
)
from services.api.app.application.web_run_policy import materialize_web_policy
from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    PostgresWebRunPolicySnapshotRepository,
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
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="需要显式双 DSN 和隔离数据库守卫",
)


async def _bootstrap(repository, command, site):
    await repository.bootstrap(
        command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
    )


async def _candidate(repository, command, site, ids, index):
    candidate = WebNavigationCandidate(
        entry_id=f"budget-{index}",
        label=f"资讯{index}",
        url=f"https://example.test/news/{index}",
        menu_path=(f"资讯{index}",),
        dom_fingerprint=f"budget-{index}",
        source_signal="semantic_nav",
        page_type_key="unknown",
        source_capture_id=ids["capture"],
    )
    await repository.record_discovery(
        command=command,
        site=site,
        candidate=candidate,
        source_capture_id=ids["capture"],
        error_code="WEB_DISCOVERY_PENDING",
    )
    return WebRuntimeMenuEntry.from_candidate(candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize("menu_limit,selected", [(2, 2), (5, 3), (10, 8), (17, 11)])
async def test_policy_budget_initialization_and_cold_resume_ignore_environment(
    menu_limit, selected, monkeypatch
):
    async with _database() as (control, pool, other, ids):
        policy = await _enabled_case(control, pool, ids, menu_limit=menu_limit)
        site, command = _site(), _command(ids)
        await _bootstrap(
            PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy),
            command,
            site,
        )
        row = await control.fetchrow(
            "SELECT menu_limit,(SELECT count(*) FROM qc_web_menu_ledger_entry "
            "WHERE agent_run_id=$1::uuid AND execution_slot IS NOT NULL) AS selected "
            "FROM qc_web_exploration_budget WHERE agent_run_id=$1::uuid",
            ids["run"],
        )
        assert dict(row) == {"menu_limit": menu_limit, "selected": selected}
        monkeypatch.setenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", "23")
        snapshot = await PostgresWebRunPolicySnapshotRepository(pool=other).read(
            task_id=ids["task"],
            agent_run_id=ids["run"],
        )
        frozen = materialize_web_policy(snapshot)
        assert frozen.menu_limit == menu_limit
        await _bootstrap(
            PostgresWebExplorationBudgetLedgerRepository(pool=other, frozen_policy=frozen),
            command,
            site,
        )


@pytest.mark.asyncio
async def test_five_menu_limit_rejects_sixth_and_keeps_fifth_detail_available():
    async with _database() as (control, pool, _, ids):
        policy = await _enabled_case(control, pool, ids, menu_limit=5, runtime_limit=None)
        site, command = _site(), _command(ids)
        site = replace(site, entries=site.entries[:1])
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        await _bootstrap(repository, command, site)
        candidates = [
            await _candidate(repository, command, site, ids, index)
            for index in range(1, 7)
        ]
        reservations = []
        for index, entry in enumerate(candidates):
            reservation = await repository.claim_menu(
                command=command, entry=entry, source_capture_id=ids["capture"]
            )
            reservations.append(reservation)
            if index < 4:
                # 模拟前四个菜单串行完成，不把菜单总额度测试混成并发额度测试。
                await control.execute(
                    "UPDATE qc_web_menu_ledger_entry SET status='navigation_failed' "
                    "WHERE agent_run_id=$1::uuid AND menu_entry_id=$2",
                    ids["run"], entry.entry_id,
                )
        assert [item.execution_slot for item in reservations[:5]] == [1, 2, 3, 4, 5]
        assert reservations[5] is None
        detail = await repository.reserve_detail(
            command=command, entry=candidates[4], source_capture_id=ids["capture"],
            candidate_id="fifth-detail", destination_url="https://example.test/detail/5",
        )
        assert not detail.already_reserved
        count = await control.fetchval(
            "SELECT sum(menu_action_count) FROM qc_web_menu_ledger_entry "
            "WHERE agent_run_id=$1::uuid", ids["run"],
        )
        assert count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_limit", [None, 17])
@pytest.mark.parametrize(
    "action", ["bootstrap", "catalog_menu", "runtime_menu", "runtime_retry", "detail"]
)
async def test_drift_rejects_before_action_or_idempotent_return_without_mutation(
    execution_limit, action
):
    async with _database() as (control, pool, other, ids):
        policy = await _enabled_case(control, pool, ids)
        site, command = _site(), _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        await _bootstrap(repository, command, site)
        entry = await _candidate(repository, command, site, ids, 1)
        if action in {"runtime_retry", "detail"}:
            assert await repository.claim_menu(
                command=command, entry=entry, source_capture_id=ids["capture"]
            )
        await control.execute(
            "UPDATE qc_web_exploration_budget SET menu_limit=$2 WHERE agent_run_id=$1::uuid",
            ids["run"],
            execution_limit,
        )
        before = await control.fetch(
            "SELECT * FROM qc_web_menu_ledger_entry "
            "WHERE agent_run_id=$1::uuid ORDER BY menu_entry_id",
            ids["run"],
        )
        cold = PostgresWebExplorationBudgetLedgerRepository(
            pool=other,
            frozen_policy=policy if action in {"bootstrap", "catalog_menu"} else None,
        )
        with pytest.raises(
            WebExplorationBudgetLedgerConflictError, match="WEB_BUDGET_POLICY_MISMATCH"
        ):
            if action == "bootstrap":
                await _bootstrap(cold, command, site)
            elif action == "detail":
                await cold.reserve_detail(
                    command=command,
                    entry=entry,
                    source_capture_id=ids["capture"],
                    candidate_id="detail",
                    destination_url="https://example.test/detail/1",
                )
            else:
                await cold.claim_menu(
                    command=command,
                    entry=site.entries[1] if action == "catalog_menu" else entry,
                    source_capture_id=ids["capture"],
                )
        after = await control.fetch(
            "SELECT * FROM qc_web_menu_ledger_entry "
            "WHERE agent_run_id=$1::uuid ORDER BY menu_entry_id",
            ids["run"],
        )
        assert after == before
        assert (
            await control.fetchval(
                "SELECT menu_limit FROM qc_web_exploration_budget WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            == execution_limit
        )
        assert (
            await cold.read_summary(agent_run_id=ids["run"], site_id=site.site_id)
        ).menu_limit == execution_limit
        await cold.closeout_unexecuted(command=command, site_id=site.site_id)


@pytest.mark.asyncio
async def test_two_workers_share_last_slot_and_full_budget_still_allows_its_detail():
    async with _database() as (control, pool, other, ids):
        policy = await _enabled_case(control, pool, ids, menu_limit=2)
        site, command = _site(), _command(ids)
        site = replace(site, entries=site.entries[:2])
        first = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        second = PostgresWebExplorationBudgetLedgerRepository(pool=other, frozen_policy=policy)
        await _bootstrap(first, command, site)
        assert await first.claim_menu(
            command=command, entry=site.entries[1], source_capture_id=ids["capture"]
        )
        candidates = [await _candidate(first, command, site, ids, index) for index in (1, 2)]
        reservations = await asyncio.gather(
            *(
                repo.claim_menu(command=command, entry=entry, source_capture_id=ids["capture"])
                for repo, entry in zip((first, second), candidates, strict=True)
            )
        )
        assert sum(item is not None for item in reservations) == 1
        winner = next(index for index, item in enumerate(reservations) if item is not None)
        assert reservations[winner].execution_slot == 2
        detail = await second.reserve_detail(
            command=command,
            entry=candidates[winner],
            source_capture_id=ids["capture"],
            candidate_id="detail",
            destination_url="https://example.test/detail/1",
        )
        assert not detail.already_reserved
        rows = await control.fetch(
            "SELECT execution_slot,menu_action_count,detail_action_count "
            "FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid",
            ids["run"],
        )
        assert sum(row["menu_action_count"] for row in rows) == 2
        assert sum(row["detail_action_count"] for row in rows) == 1
        assert max(row["execution_slot"] or 0 for row in rows) == 2
