"""Web checkpoint 迁移图及显式双 DSN 下的真实事务验证。"""

import asyncio
import importlib.util
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
    WebStageCheckpointConflict,
)
from services.api.app.infrastructure.db.web_run_stage_checkpoint_row import (
    parse_web_run_stage_checkpoint_row,
    web_run_stage_checkpoint_values,
)
from services.api.tests import test_web_exploration_budget_ledger_postgres_integration as pg_helpers
from services.api.tests.test_web_discovery_migration_postgres import migrate
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import _database

pg = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="需要显式隔离 ALIOTH_TEST_DATABASE_URL 和 ALIOTH_DATABASE_URL 双 DSN",
)
NOW = datetime.now(UTC)


@pytest.fixture(scope="module")
def _upgrade_isolated_postgres():
    pg_helpers._upgrade_isolated_postgres.__wrapped__()


def test_checkpoint_migration_extends_single_head():
    graph = ScriptDirectory.from_config(Config("services/api/migrations/alembic.ini"))
    heads = graph.get_heads()
    assert len(heads) == 1
    assert "20260916_0121" in {
        item.revision for item in graph.walk_revisions(base="base", head=heads[0])
    }
    assert graph.get_revision("20260916_0121").down_revision == "20260916_0120"
    assert graph.get_revision("20260916_0120").down_revision == "20260914_0119"


def test_checkpoint_migration_uses_one_statement_per_driver_call():
    path = (
        Path(__file__).parents[1]
        / "migrations/versions/20260916_0120_web_run_stage_checkpoint.py"
    )
    spec = importlib.util.spec_from_file_location("web_checkpoint_0114", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []

    class Operations:
        @staticmethod
        def execute(statement: str) -> None:
            sql = statement.strip().rstrip(";").strip()
            assert ";" not in sql
            statements.append(sql)

    module.op = Operations()
    module.upgrade()
    assert len(statements) == 5
    assert statements[1].startswith("CREATE TABLE qc_web_run_stage_checkpoint")

    statements.clear()
    module.downgrade()
    assert len(statements) == 5
    assert statements[-1].startswith("DROP FUNCTION qc_web_checkpoint_gap_audit_valid")


def test_checkpoint_row_roundtrip_preserves_claim_gap_and_references():
    cursor = WebStageCursor.start(
        run_id="50000000-0000-0000-0000-000000000114", logical_page_key="url:1"
    )
    cursor = cursor.claim(worker_id="worker", now=NOW).schedule_retry(
        error_code="WEB_GOTO_TLS_FAILED",
        now=NOW,
        transient=False,
        detail={"final_url": "https://example.test/"},
    )
    restored = parse_web_run_stage_checkpoint_row(web_run_stage_checkpoint_values(cursor))
    assert restored == cursor
    assert restored.gaps[0].error_code == "WEB_GOTO_TLS_FAILED"


@pytest.mark.parametrize(
    "changes",
    [
        {"gaps": "[]", "gap_codes": "[]"},
        {"attempt_count": 0},
        {"stage_generation": 5},
        {"current_stage": "readiness"},
        {"gap_codes": '["WEB_FAKE"]'},
    ],
)
def test_row_parser_rejects_gapped_without_matching_audit(changes):
    cursor = (
        WebStageCursor.start(
            run_id="50000000-0000-0000-0000-000000000114", logical_page_key="url:1"
        )
        .claim(worker_id="worker", now=NOW)
        .record_gap_and_advance(error_code="WEB_GOTO_TLS_FAILED")
    )
    row = web_run_stage_checkpoint_values(cursor)
    with pytest.raises(ValueError):
        parse_web_run_stage_checkpoint_row({**row, **changes})


def test_popup_budget_and_audit_survive_advance_and_full_row_roundtrip():
    cursor = WebStageCursor.start(
        run_id="50000000-0000-0000-0000-000000000114",
        logical_page_key="url:1",
        stage=WebRunStage.OBSTRUCTION_CLEAR,
        cursor_payload={"obstruction_fingerprint": "popup-a"},
    )
    for attempt in range(2):
        cursor = cursor.claim(worker_id="worker", now=NOW + timedelta(seconds=attempt * 10))
        cursor = cursor.schedule_retry(error_code="WEB_OBSTRUCTION_CLOSE_FAILED", now=NOW)
    restored = parse_web_run_stage_checkpoint_row(
        web_run_stage_checkpoint_values(cursor.advance(WebRunStage.READINESS))
    )
    assert dict(restored.obstruction_attempts) == {"popup-a": 2}
    assert restored.gap_codes == ("WEB_OBSTRUCTION_UNRESOLVED",)
    assert restored.gaps[0].stage is WebRunStage.OBSTRUCTION_CLEAR
    assert restored.attempt_count == 0


@pytest.mark.parametrize("field", ["stage_generation", "attempt_count"])
def test_row_parser_rejects_negative_gap_numbers(field):
    cursor = (
        WebStageCursor.start(
            run_id="50000000-0000-0000-0000-000000000114", logical_page_key="url:1"
        )
        .claim(worker_id="worker", now=NOW)
        .record_gap_and_advance(error_code="WEB_GOTO_TLS_FAILED")
    )
    row = web_run_stage_checkpoint_values(cursor)
    gaps = json.loads(row["gaps"])
    gaps[0][field] = -1
    row["gaps"] = json.dumps(gaps)
    with pytest.raises(ValueError):
        parse_web_run_stage_checkpoint_row(row)


async def seed_current(control, ids):
    await control.execute(
        "UPDATE qc_agent_run SET input_context=jsonb_build_object('task_config', "
        "jsonb_build_object('web_runtime_version', 'web-runtime.v2')) WHERE id=$1::uuid",
        ids["run"],
    )


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
@pytest.mark.asyncio
async def test_menu_navigation_reference_survives_real_database_completion():
    async with _database() as (control, pool, _, ids):
        await seed_current(control, ids)
        repo = PostgresWebRunStageCheckpointRepository(pool=pool)
        await repo.create(WebStageCursor.start(run_id=ids["run"], logical_page_key="menu:1"))
        claimed = await repo.claim(
            run_id=ids["run"], logical_page_key="menu:1",
            expected_row_version=0, worker_id="worker",
        )
        saved = await repo.mark_succeeded(
            claimed, cursor_payload={"menu_navigation_capture_id": ids["capture"]},
        )
        restored = await repo.get(run_id=ids["run"], logical_page_key="menu:1")
        assert restored == saved
        assert restored.cursor_payload["menu_navigation_capture_id"] == ids["capture"]
        # 扩展一个恢复引用不能放开正文或任意字段持久化。
        for field in ("body_text", "unexpected_reference"):
            with pytest.raises(asyncpg.CheckViolationError):
                await control.execute(
                    "UPDATE qc_web_run_stage_checkpoint SET cursor_payload=cursor_payload || "
                    "jsonb_build_object($2::text, 'forbidden') WHERE run_id=$1::uuid",
                    ids["run"], field,
                )


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
@pytest.mark.asyncio
async def test_login_checkpoint_references_survive_real_database_completion():
    async with _database() as (control, pool, _, ids):
        await seed_current(control, ids)
        repo = PostgresWebRunStageCheckpointRepository(pool=pool)
        await repo.create(WebStageCursor.start(
            run_id=ids["run"], logical_page_key="home:login-source",
            stage=WebRunStage.READINESS,
            cursor_payload={"capture_id": ids["capture"]},
        ))
        claimed = await repo.claim(
            run_id=ids["run"], logical_page_key="home:login-source",
            expected_row_version=0, worker_id="worker",
        )
        saved = await repo.mark_succeeded(
            claimed, cursor_payload={"login_source_capture_id": ids["capture"]},
        )
        assert saved.cursor_payload["login_source_capture_id"] == ids["capture"]

        await repo.create(WebStageCursor.start(
            run_id=ids["run"], logical_page_key="home:login-result",
            stage=WebRunStage.REPORT_FINALIZATION,
            cursor_payload={"login_source_capture_id": ids["capture"]},
        ))
        claimed = await repo.claim(
            run_id=ids["run"], logical_page_key="home:login-result",
            expected_row_version=0, worker_id="worker",
        )
        saved = await repo.mark_succeeded(claimed, cursor_payload={
            "login_branch_status": "authenticated",
            "login_branch_reason": "WEB_LOGIN_COOKIE_CONFIRMED",
            "login_cross_origin_status": "blocked",
            "login_blocked_requests": (("https://stats.example", "GET"),),
            "login_account_status": "recharge_opened",
            "login_account_artifact_ids": ("artifact-1",),
            "login_account_storage_paths": ("task/capture/artifact-1.png",),
            "login_account_avatar_opened": True,
            "login_account_avatar_dom_artifact_id": "artifact-dom",
            "login_account_page_type": "recharge_page",
            "login_account_page_url": "https://example.test/recharge",
            "login_account_page_visits": ((
                "recharge_page", "https://example.test/recharge",
                "artifact-1", "artifact-dom",
            ),),
        })
        restored = await repo.get(
            run_id=ids["run"], logical_page_key="home:login-result",
        )
        assert restored == saved
        with pytest.raises(asyncpg.CheckViolationError):
            await control.execute(
                "UPDATE qc_web_run_stage_checkpoint SET cursor_payload=cursor_payload || "
                "jsonb_build_object('raw_dom', '<html>') WHERE run_id=$1::uuid "
                "AND logical_page_key='home:login-result'",
                ids["run"],
            )


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
async def test_ad_reservation_is_per_business_page_and_same_page_restart_is_idempotent():
    async with _database() as (control, first_pool, second_pool, ids):
        await seed_current(control, ids)
        first = PostgresWebRunStageCheckpointRepository(pool=first_pool)
        second = PostgresWebRunStageCheckpointRepository(pool=second_pool)

        def candidate(key, url):
            return WebStageCursor.start(
                run_id=ids["run"], logical_page_key="ad:" + key,
                stage=WebRunStage.AD_ISOLATED_NAVIGATION,
                cursor_payload={"session_id": ids["session"], "finalization_epoch": 0,
                    "capture_id": ids["capture"], "current_url": "https://example.test/",
                    "requested_url": url},
            )

        a, b = await asyncio.gather(
            first.create(candidate("home", "https://ADS.example:443/")),
            second.create(candidate("menu", "https://ads.example/")),
        )
        assert a.logical_page_key != b.logical_page_key
        assert len(await first.list_for_run(run_id=ids["run"])) == 2
        restarted = PostgresWebRunStageCheckpointRepository(pool=second_pool)
        repeated = await restarted.create(candidate("menu", "https://ads.example/"))
        assert repeated == b
        distinct = await restarted.create(candidate("video", "https://ads.example/?campaign=2"))
        assert distinct.logical_page_key == "ad:video"
        assert len(await restarted.list_for_run(run_id=ids["run"])) == 3

        # 旧版本接收过的非法 IDN 不能使当前整轮广告调度失败。
        await control.execute(
            "UPDATE qc_web_run_stage_checkpoint "
            "SET cursor_payload=jsonb_set(cursor_payload, '{requested_url}', $3::jsonb) "
            "WHERE run_id=$1::uuid AND logical_page_key=$2",
            ids["run"], a.logical_page_key, json.dumps("https://bad_host.example/ad"),
        )
        fresh = await restarted.create(candidate("new", "https://ads.example/new"))
        assert fresh.logical_page_key == "ad:new"


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
async def test_web_orphan_settlement_waits_for_leases_and_closes_only_own_run():
    from services.api.app.infrastructure.db.web_exploration_orphan_repository import (
        PostgresWebExplorationOrphanRepository,
    )

    async with _database() as (control, pool, _other, ids):
        await seed_current(control, ids)
        await control.execute(
            "UPDATE qc_agent_run SET input_context=jsonb_set(input_context, "
            "'{task_config,execution_mode}', '\"exploration_only\"') WHERE id=$1::uuid",
            ids["run"],
        )
        session = f"web:{ids['run']}"
        await control.execute(
            "INSERT INTO qc_surface_session "
            "(session_id,task_id,surface_kind,session_owner,status,activated_at) "
            "VALUES ($1,$2::uuid,'web','web-v2-workflow','active',now())", session, ids["task"],
        )
        checkpoints = PostgresWebRunStageCheckpointRepository(pool=pool)
        await control.execute(
            "UPDATE qc_capture SET surface_session_id=$1 WHERE id=$2::uuid",
            session, ids["capture"],
        )
        for sequence, action_session, status in (
            (1, session, "planned"), (2, session, "evidence_pending"),
            (3, ids["session"], "planned"),
        ):
            await control.execute(
                "INSERT INTO qc_action_execution (action_execution_id,task_id,session_id,"
                "intent_id,idempotency_key,intent_hash,intent_protocol_version,intent_payload,"
                "command_sequence,finalization_epoch,status,source_capture_id,started_at,"
                "action_finished_at) "
                "VALUES ($1,$2::uuid,$3,$1,$1,'hash','test-v1','{}',$4,0,$5::varchar,"
                "CASE WHEN $5='evidence_pending' THEN $6::uuid ELSE NULL END,"
                "CASE WHEN $5='evidence_pending' THEN now() ELSE NULL END,"
                "CASE WHEN $5='evidence_pending' THEN now() ELSE NULL END)",
                f"orphan:{ids['run']}:{sequence}", ids["task"], action_session, sequence, status,
                ids["capture"],
            )
        initial = await checkpoints.create(WebStageCursor.start(
            run_id=ids["run"], logical_page_key="home",
            cursor_payload={"session_id": session, "finalization_epoch": 0},
        ))
        await checkpoints.claim(run_id=ids["run"], logical_page_key="home",
                                expected_row_version=initial.row_version, worker_id="dead")
        repository = PostgresWebExplorationOrphanRepository(pool=pool)
        assert not await repository.settle(task_id=ids["task"], run_id=ids["run"])
        await control.execute(
            "UPDATE qc_web_run_stage_checkpoint SET claimed_at=now()-interval '11 minutes', "
            "lease_expires_at=now()-interval '1 minute' WHERE run_id=$1::uuid", ids["run"],
        )
        await control.execute(
            "UPDATE qc_surface_session SET lease_owner='live-command', "
            "lease_token='00000000-0000-0000-0000-000000000012'::uuid, "
            "lease_expires_at=now()+interval '1 minute' WHERE session_id=$1", session,
        )
        assert not await repository.settle(task_id=ids["task"], run_id=ids["run"])
        await control.execute(
            "UPDATE qc_surface_session SET lease_expires_at=now()-interval '1 minute' "
            "WHERE session_id=$1", session,
        )
        for mutation, restoration in (
            ("output_context='{" + '\"cancel_requested\":true' + "}'::jsonb",
             "output_context='{}'::jsonb"),
            ("input_context=jsonb_set(input_context,'{task_config,web_runtime_version}', "
             "'\"web-runtime.old\"')",
             "input_context=jsonb_set(input_context,'{task_config,web_runtime_version}', "
             "'\"web-runtime.v2\"')"),
        ):
            await control.execute(
                f"UPDATE qc_agent_run SET {mutation} WHERE id=$1::uuid", ids["run"]
            )
            assert not await repository.settle(task_id=ids["task"], run_id=ids["run"])
            await control.execute(
                f"UPDATE qc_agent_run SET {restoration} WHERE id=$1::uuid", ids["run"]
            )
        assert await repository.settle(task_id=ids["task"], run_id=ids["run"])
        assert await repository.settle(task_id=ids["task"], run_id=ids["run"])
        result = await checkpoints.get(run_id=ids["run"], logical_page_key="home")
        assert result.is_terminal and result.last_error_code == "WEB_WORKER_ORPHANED"
        assert await control.fetchval(
            "SELECT status FROM qc_surface_session WHERE session_id=$1", session
        ) == "lost"
        assert await control.fetchval(
            "SELECT status FROM qc_surface_session WHERE session_id=$1", ids["session"]
        ) == "active"
        assert await control.fetchval(
            "SELECT count(*) FROM qc_capture WHERE id=$1::uuid", ids["capture"]
        ) == 1
        actions = await control.fetch(
            "SELECT status,error_code FROM qc_action_execution WHERE task_id=$1::uuid "
            "ORDER BY command_sequence", ids["task"],
        )
        assert [(row["status"], row["error_code"]) for row in actions] == [
            ("failed", "WEB_WORKER_ORPHANED"),
            ("evidence_failed", "WEB_WORKER_ORPHANED"), ("planned", None),
        ]


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
@pytest.mark.asyncio
async def test_two_workers_cas_unique_identity_and_stale_completion():
    async with _database() as (control, first_pool, second_pool, ids):
        await seed_current(control, ids)
        first = PostgresWebRunStageCheckpointRepository(pool=first_pool)
        second = PostgresWebRunStageCheckpointRepository(pool=second_pool)
        cursor = WebStageCursor.start(run_id=ids["run"], logical_page_key="url:1")
        a, b = await asyncio.gather(first.create(cursor), second.create(cursor))
        assert a == b
        outcomes = await asyncio.gather(
            first.claim(
                run_id=ids["run"],
                logical_page_key="url:1",
                expected_row_version=0,
                worker_id="first",
                now=NOW,
            ),
            second.claim(
                run_id=ids["run"],
                logical_page_key="url:1",
                expected_row_version=0,
                worker_id="second",
                now=NOW,
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, WebStageCheckpointConflict) for item in outcomes) == 1
        winner = next(item for item in outcomes if isinstance(item, WebStageCursor))
        assert winner.row_version == 1
        await expire_lease(control, ids["run"])
        recovered = await second.claim(
            run_id=ids["run"],
            logical_page_key="url:1",
            expected_row_version=1,
            worker_id="recovery",
            now=NOW + timedelta(seconds=61),
        )
        with pytest.raises(WebStageCheckpointConflict):
            await first.mark_succeeded(winner, now=NOW + timedelta(seconds=62))
        result = await second.mark_succeeded(recovered, now=NOW + timedelta(seconds=62))
        assert result.row_version == 3
        assert result.next_stage.value == "obstruction_clear"
        assert (
            await control.fetchval(
                "SELECT count(*) FROM qc_web_run_stage_checkpoint WHERE run_id=$1::uuid", ids["run"]
            )
            == 1
        )


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
@pytest.mark.asyncio
async def test_due_retry_gap_advance_and_run_cancellation_are_durable():
    async with _database() as (control, pool, _, ids):
        await seed_current(control, ids)
        repo = PostgresWebRunStageCheckpointRepository(pool=pool)
        await repo.create(WebStageCursor.start(run_id=ids["run"], logical_page_key="url:1"))
        current = await repo.claim(
            run_id=ids["run"],
            logical_page_key="url:1",
            expected_row_version=0,
            worker_id="worker",
            now=NOW,
        )
        retry = await repo.schedule_retry(current, error_code="WEB_GOTO_TLS_FAILED", now=NOW)
        assert await repo.list_due(now=retry.updated_at, run_id=ids["run"]) == ()
        assert (await repo.list_due(now=retry.next_attempt_at, run_id=ids["run"]))[0] == retry
        await control.execute(
            "UPDATE qc_web_run_stage_checkpoint SET next_attempt_at=clock_timestamp() "
            "- interval '1 second' WHERE run_id=$1::uuid",
            ids["run"],
        )
        claimed = await repo.claim(
            run_id=ids["run"],
            logical_page_key="url:1",
            expected_row_version=2,
            worker_id="worker",
            now=NOW + timedelta(seconds=1),
        )
        gapped = await repo.record_gap_and_advance(
            claimed, error_code="WEB_GOTO_TLS_FAILED", now=NOW + timedelta(seconds=1)
        )
        assert gapped.gap_codes == ("WEB_NAVIGATION_RETRY_EXHAUSTED",)
        advanced = await repo.claim(
            run_id=ids["run"],
            logical_page_key="url:1",
            expected_row_version=4,
            worker_id="worker",
            now=NOW + timedelta(seconds=2),
        )
        assert advanced.current_stage.value == "frontier_drain"
        await control.execute(
            "UPDATE qc_agent_run SET output_context='{"
            + '"cancel_requested":true'
            + "}'::jsonb WHERE id=$1::uuid",
            ids["run"],
        )
        assert await repo.list_due(now=NOW + timedelta(seconds=90), run_id=ids["run"]) == ()
        with pytest.raises(WebStageCheckpointConflict):
            await repo.mark_succeeded(advanced, now=NOW + timedelta(seconds=3))


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
@pytest.mark.asyncio
async def test_legacy_terminal_and_identity_drift_are_not_claimable():
    async with _database() as (control, pool, _, ids):
        repo = PostgresWebRunStageCheckpointRepository(pool=pool)
        cursor = WebStageCursor.start(run_id=ids["run"], logical_page_key="url:1")
        with pytest.raises(WebStageCheckpointConflict):
            await repo.create(cursor)
        await seed_current(control, ids)
        await repo.create(cursor)
        with pytest.raises(WebStageCheckpointConflict):
            await repo.create(
                replace(cursor, cursor_payload={"current_url": "https://different.test/"})
            )
        await control.execute(
            "UPDATE qc_agent_run SET status='failed' WHERE id=$1::uuid", ids["run"]
        )
        with pytest.raises(WebStageCheckpointConflict):
            await repo.claim(
                run_id=ids["run"],
                logical_page_key="url:1",
                expected_row_version=0,
                worker_id="worker",
                now=NOW,
            )


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
@pytest.mark.asyncio
async def test_database_constraints_reject_bad_stage_negative_counter_and_duplicate():
    async with _database() as (control, pool, _, ids):
        await seed_current(control, ids)
        repo = PostgresWebRunStageCheckpointRepository(pool=pool)
        await repo.create(WebStageCursor.start(run_id=ids["run"], logical_page_key="url:1"))
        for assignment in ("current_stage='bogus'", "attempt_count=-1", "row_version=-1"):
            with pytest.raises(asyncpg.CheckViolationError):
                await control.execute(
                    f"UPDATE qc_web_run_stage_checkpoint SET {assignment} WHERE run_id=$1::uuid",
                    ids["run"],
                )
        with pytest.raises(asyncpg.UniqueViolationError):
            await control.execute(
                "INSERT INTO qc_web_run_stage_checkpoint "
                "SELECT * FROM qc_web_run_stage_checkpoint WHERE run_id=$1::uuid",
                ids["run"],
            )
        indexes = await control.fetch(
            "SELECT indexdef FROM pg_indexes WHERE tablename='qc_web_run_stage_checkpoint'"
        )
        assert any("next_attempt_at" in row["indexdef"] for row in indexes)


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
@pytest.mark.asyncio
async def test_migration_upgrade_downgrade_roundtrip_preserves_prior_head():
    result = await asyncio.to_thread(migrate, "downgrade", "20260914_0119")
    assert result.returncode == 0, result.stderr
    try:
        async with _database() as (control, _, _other, _ids):
            assert (
                await control.fetchval("SELECT to_regclass('qc_web_run_stage_checkpoint')") is None
            )
            assert (
                await control.fetchval("SELECT version_num FROM alembic_version") == "20260914_0119"
            )
    finally:
        result = await asyncio.to_thread(migrate, "upgrade", "head")
        assert result.returncode == 0, result.stderr


async def expire_lease(connection, run_id):
    await connection.execute(
        "UPDATE qc_web_run_stage_checkpoint SET claimed_at=clock_timestamp()-interval '2 seconds', "
        "lease_expires_at=clock_timestamp()-interval '1 second' WHERE run_id=$1::uuid",
        run_id,
    )


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
@pytest.mark.asyncio
async def test_postgres_repeated_expired_claims_exhaust_without_fourth_attempt():
    async with _database() as (control, pool, _, ids):
        await seed_current(control, ids)
        repo = PostgresWebRunStageCheckpointRepository(pool=pool)
        cursor = await repo.create(
            WebStageCursor.start(run_id=ids["run"], logical_page_key="url:1")
        )
        for attempt in range(3):
            cursor = await repo.claim(
                run_id=ids["run"],
                logical_page_key="url:1",
                expected_row_version=cursor.row_version,
                worker_id="worker",
            )
            assert cursor.attempt_count == attempt + 1
            await expire_lease(control, ids["run"])
        exhausted = await repo.claim(
            run_id=ids["run"],
            logical_page_key="url:1",
            expected_row_version=cursor.row_version,
            worker_id="worker",
        )
        assert exhausted.status.value == "gapped"
        assert exhausted.attempt_count == 3
        assert exhausted.gap_codes == ("WEB_NAVIGATION_RETRY_EXHAUSTED",)


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["mark_succeeded", "schedule_retry", "record_gap_and_advance", "claim"]
)
async def test_postgres_authoritative_clock_after_contended_run_lock(operation):
    async with _database() as (control, pool, _, ids):
        await seed_current(control, ids)
        repo = PostgresWebRunStageCheckpointRepository(pool=pool)
        cursor = await repo.create(
            WebStageCursor.start(run_id=ids["run"], logical_page_key="url:1")
        )
        if operation != "claim":
            cursor = await repo.claim(
                run_id=ids["run"],
                logical_page_key="url:1",
                expected_row_version=0,
                worker_id="worker",
                lease_seconds=1,
            )
        async with pool.acquire() as connection:
            worker_pid = await connection.fetchval("SELECT pg_backend_pid()")
        async with control.transaction():
            await control.fetchrow(
                "SELECT id FROM qc_agent_run WHERE id=$1::uuid FOR UPDATE", ids["run"]
            )
            if operation == "claim":
                work = asyncio.create_task(
                    repo.claim(
                        run_id=ids["run"],
                        logical_page_key="url:1",
                        expected_row_version=0,
                        worker_id="worker",
                        now=NOW,
                        lease_seconds=60,
                    )
                )
            else:
                kwargs = (
                    {} if operation == "mark_succeeded" else {"error_code": "WEB_GOTO_TLS_FAILED"}
                )
                work = asyncio.create_task(getattr(repo, operation)(cursor, now=NOW, **kwargs))
            for _ in range(100):
                if await control.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=$1 AND NOT granted)",
                    worker_pid,
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                work.cancel()
                raise AssertionError("Worker 未进入真实 PostgreSQL 锁等待")
            await asyncio.sleep(1.05)
            released_at = await control.fetchval("SELECT clock_timestamp()")
        if operation == "claim":
            claimed = await work
            assert claimed.claimed_at >= released_at
            assert claimed.lease_expires_at == claimed.claimed_at + timedelta(seconds=60)
        else:
            with pytest.raises(WebStageCheckpointConflict):
                await work


@pg
@pytest.mark.usefixtures("_upgrade_isolated_postgres")
@pytest.mark.asyncio
async def test_postgres_gapped_requires_matching_stage_generation_and_codes():
    async with _database() as (control, pool, _, ids):
        await seed_current(control, ids)
        repo = PostgresWebRunStageCheckpointRepository(pool=pool)
        await repo.create(WebStageCursor.start(run_id=ids["run"], logical_page_key="url:1"))
        with pytest.raises(asyncpg.CheckViolationError):
            await control.execute(
                "UPDATE qc_web_run_stage_checkpoint SET stage_status='gapped' "
                "WHERE run_id=$1::uuid",
                ids["run"],
            )
        cursor = await repo.claim(
            run_id=ids["run"], logical_page_key="url:1", expected_row_version=0, worker_id="worker"
        )
        await repo.record_gap_and_advance(cursor, error_code="WEB_GOTO_TLS_FAILED")
        for assignment in ("gaps='[]'", "gap_codes='[]'", "stage_generation=1", "attempt_count=0"):
            with pytest.raises(asyncpg.CheckViolationError):
                await control.execute(
                    f"UPDATE qc_web_run_stage_checkpoint SET {assignment} WHERE run_id=$1::uuid",
                    ids["run"],
                )
