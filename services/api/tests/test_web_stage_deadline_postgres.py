"""真实 PG 验证期限缺口不能把未领取菜单伪造成动作，也不能遗漏已领取槽位。"""

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.web_runtime_discovery import (
    WebRuntimeMenuEntry,
    discovery_entry_id,
)
from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
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
from services.api.tests.test_web_run_stage_checkpoint_postgres import seed_current
from services.api.tests.test_web_runtime_discovery_postgres import _enabled_case
from services.api.tests.test_web_runtime_stage_frontier import candidate

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须使用显式隔离测试库"
)


@pytest.mark.parametrize("reserved", [False, True])
async def test_real_deadline_reconciliation_seals_reserved_and_unexecuted(reserved):
    async with _database() as (control, pool, _other, ids):
        policy = await _enabled_case(control, pool, ids)
        await seed_current(control, ids)
        command = _command(ids)
        site = replace(_site(), entries=(_site().entries[0],))
        ledger = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        assert not await ledger.has_budget(command=command, site_id=site.site_id)
        await ledger.bootstrap(
            command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )
        observed = replace(candidate("deadline"), source_capture_id=ids["capture"])
        await ledger.record_discovery(
            command=command,
            site=site,
            candidate=observed,
            source_capture_id=ids["capture"],
            error_code="WEB_DISCOVERY_PENDING",
        )
        if reserved:
            assert await ledger.claim_menu(
                command=command,
                entry=WebRuntimeMenuEntry.from_candidate(observed),
                source_capture_id=ids["capture"],
            )
        checkpoints = PostgresWebRunStageCheckpointRepository(pool=pool)
        frontier = WebRuntimeStageFrontier(
            ledger=ledger, candidate_source=None, navigator=None, site=site
        )
        expired = replace(command, deadline=datetime.now(UTC) - timedelta(seconds=1))
        for logical_key in (discovery_entry_id(observed), "home"):
            payload = {
                "session_id": ids["session"],
                "finalization_epoch": 0,
                "capture_id": ids["capture"],
            }
            if logical_key != "home":
                payload["menu_ledger_id"] = logical_key
            initial = await checkpoints.create(
                WebStageCursor.start(
                    run_id=ids["run"],
                    logical_page_key=logical_key,
                    stage=WebRunStage.READINESS,
                    cursor_payload=payload,
                )
            )
            active = await checkpoints.claim(
                run_id=ids["run"],
                logical_page_key=logical_key,
                expected_row_version=initial.row_version,
                worker_id="deadline-test",
                now=datetime.now(UTC),
            )
            gapped = await checkpoints.record_gap_and_advance(
                active, error_code="WEB_EXPLORATION_DEADLINE_EXHAUSTED", now=datetime.now(UTC)
            )
            await frontier.reconcile(expired, gapped)
        row = await control.fetchrow(
            "SELECT status,menu_action_count FROM qc_web_menu_ledger_entry "
            "WHERE agent_run_id=$1::uuid AND menu_entry_id=$2",
            ids["run"],
            discovery_entry_id(observed),
        )
        assert row["status"] == (
            "navigation_failed" if reserved else "runtime_discovered_unreviewed"
        )
        assert row["menu_action_count"] == int(reserved)
        assert (
            await control.fetchval(
                "SELECT state FROM qc_web_exploration_budget WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            == "sealed"
        )
