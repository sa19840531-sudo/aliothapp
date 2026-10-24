"""Web 目标唯一占有、恢复与版本围栏的隔离 PostgreSQL 回归。"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import _database
from services.api.tests.test_web_run_policy_snapshot import policy_payload
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)
from services.api.tests.web_frozen_assessment_support import frozen_assessment_options

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="需显式隔离 PostgreSQL"
)


@asynccontextmanager
async def target_database():
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
    from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
        PostgresWebRunPolicySnapshotRepository,
    )

    async with _database() as (connection, first_pool, second_pool, ids):
        # 重建本用例的会话前，先解除共享样本的当前采集引用。
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=NULL WHERE task_id=$1::uuid",
            ids["task"],
        )
        await connection.execute("DELETE FROM qc_capture WHERE task_id=$1::uuid", ids["task"])
        await connection.execute(
            "DELETE FROM qc_surface_session WHERE task_id=$1::uuid", ids["task"]
        )
        ids["session"] = "web:" + ids["run"]
        await PostgresWebRunPolicySnapshotRepository(pool=first_pool).freeze_or_read(
            WebRunPolicySnapshot.create(
                task_id=ids["task"], agent_run_id=ids["run"], payload=policy_payload()
            )
        )
        await connection.execute(
            'UPDATE qc_task SET task_config=\'{"workflow_version":"v2"}\'::jsonb WHERE id=$1::uuid',
            ids["task"],
        )
        await connection.execute(
            "INSERT INTO "
            "qc_surface_session(session_id,task_id,surface_kind,session_owner,status,activated_at) "
            "VALUES($1,$2::uuid,'web','web-v2-workflow','active',now())",
            ids["session"],
            ids["task"],
        )
        await connection.execute(
            "INSERT INTO "
            "qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,capture_role,"
            "capture_status,captured_at,metadata) "
            "VALUES($1::uuid,$2::uuid,$3::uuid,'web',$4,'probe_before','captured',now(),"
            "'{\"finalization_epoch\":0}'::jsonb)",
            ids["capture"],
            ids["task"],
            ids["run"],
            ids["session"],
        )
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
            ids["capture"],
            ids["session"],
        )
        yield connection, first_pool, second_pool, ids


@pytest.mark.asyncio
async def test_cross_queue_two_connections_claim_once_and_crash_recovery_is_not_complete():
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )

    async with target_database() as (connection, first_pool, second_pool, ids):
        first = PostgresWebLogicalTargetRepository(pool=first_pool)
        second = PostgresWebLogicalTargetRepository(pool=second_pool)
        kwargs = dict(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            source_capture_id=ids["capture"],
            url="https://example.test/#/detail/1",
        )
        results = await asyncio.gather(first.claim(**kwargs), second.claim(**kwargs))
        assert sorted(row["created"] for row in results) == [False, True]
        assert all(row["state"] == "claimed" for row in results)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_web_logical_target WHERE agent_run_id=$1::uuid", ids["run"]
            )
            == 1
        )
        recovered = await PostgresWebLogicalTargetRepository(pool=second_pool).find(**kwargs)
        assert recovered["state"] == "claimed"
        assert recovered["assessment"] is None
        assert recovered["primary_capture_id"] is None
        assert recovered["reason_code"] == "WEB_LOGICAL_TARGET_INCOMPLETE"


@pytest.mark.asyncio
async def test_spa_targets_and_protocols_are_distinct_and_epoch_drift_rejected():
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )

    async with target_database() as (connection, pool, _other, ids):
        repo = PostgresWebLogicalTargetRepository(pool=pool)
        kwargs = dict(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            source_capture_id=ids["capture"],
        )
        for url in (
            "https://example.test/#/detail/1",
            "https://example.test/#/detail/2",
            "http://example.test/#/detail/1",
        ):
            assert (await repo.claim(url=url, **kwargs))["created"]
        assert not (await repo.claim(url="https://example.test/#/detail/1?tab=info", **kwargs))[
            "created"
        ]
        await connection.execute(
            "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1::uuid", ids["task"]
        )
        with pytest.raises(ValueError, match="WEB_LOGICAL_TARGET_SCOPE_INVALID"):
            await repo.find(url="https://example.test/#/detail/1", **kwargs)


async def seed_material(
    connection,
    pool,
    ids,
    *,
    url,
    sequence=1,
    requested_url=None,
    redirect_chain=None,
    page_type="content_detail",
    with_material=True,
    source_capture_id=None,
    intent_id=None,
):
    from services.api.app.application.web_page_classification import WebPageAssessment
    from services.api.app.application.web_page_type_assessment import WebPageTypeAssessmentRecorder
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        PostgresWebCaptureEvidenceRepository,
    )
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )
    from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
        _seed_completed_action_evidence,
    )
    from services.api.tests.test_web_page_type_assessment import _capture

    action_id = "t5-" + str(uuid4())
    before, after = await _seed_completed_action_evidence(
        connection,
        ids,
        action_execution_id=action_id,
        intent_id=intent_id or action_id,
        command_sequence=sequence,
        requested_source_capture_id=source_capture_id or ids["capture"],
        parent_capture_id=source_capture_id or ids["capture"],
    )
    await connection.execute(
        "UPDATE qc_capture SET metadata=$1::jsonb WHERE id=ANY($2::uuid[])",
        json.dumps({"finalization_epoch": 0}),
        [before, after],
    )
    if requested_url is not None:
        await connection.execute(
            "UPDATE qc_action_execution SET intent_payload=$1::jsonb WHERE action_execution_id=$2",
            json.dumps({"parameters": {"destination_url": requested_url}}),
            action_id,
        )
    if redirect_chain is not None:
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || $1::jsonb WHERE id=$2::uuid",
            json.dumps({"pipeline_metrics": {"web_natural_redirect_chain": redirect_chain}}),
            after,
        )
    await connection.execute(
        "INSERT INTO "
        "qc_page_capture(id,task_id,page_seq,page_slug,url,capture_status,captured_at,"
        "agent_run_id,surface_session_id,capture_role,session_command_sequence,metadata,"
        "action_execution_id) "
        "VALUES($1::uuid,$2::uuid,$3::integer*2-1,$4,$5,'captured',now(),$6::uuid,$7,"
        "'before_action',$3::integer::bigint,'{\"finalization_epoch\":0}'::jsonb,$4)",
        before,
        ids["task"],
        sequence,
        action_id,
        requested_url or url,
        ids["run"],
        ids["session"],
    )
    await connection.execute(
        "INSERT INTO "
        "qc_page_capture(id,task_id,page_seq,page_slug,url,capture_status,captured_at,"
        "agent_run_id,surface_session_id,capture_role,session_command_sequence,metadata,"
        "action_execution_id,parent_page_capture_id) "
        "VALUES($1::uuid,$2::uuid,$3::integer*2,$4,$5,'captured',now(),$6::uuid,$7,"
        "'after_action',$3::integer::bigint,$8::jsonb,$4,$9::uuid)",
        after,
        ids["task"],
        sequence,
        action_id,
        url,
        ids["run"],
        ids["session"],
        json.dumps(
            {
                "finalization_epoch": 0,
                "pipeline_metrics": {"web_natural_redirect_chain": redirect_chain},
            }
        ),
        before,
    )
    await connection.execute(
        "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
        after,
        ids["session"],
    )
    if not with_material:
        return after
    options = await frozen_assessment_options(connection, ids)
    capture = replace(_capture(), capture_id=after, url=url, page_role="detail")
    assessment = WebPageAssessment(page_type=page_type, confidence=0.95, evidence=("自然正文",))
    repo = PostgresWebLogicalTargetRepository(pool=pool)
    scope = dict(task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"])
    await repo.claim(**scope, source_capture_id=after, url=url)
    await WebPageTypeAssessmentRecorder(
        repository=PostgresWebCaptureEvidenceRepository(
            pool=pool, **options,
        ),
        **options,
    ).persist(**scope, capture=capture, assessment=assessment)
    await repo.materialize(**scope, capture_id=after, url=url, assessment=asdict(assessment))
    return after


@pytest.mark.asyncio
async def test_materialized_capture_primary_claim_and_cold_recovery_preserve_one_record():
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )

    async with target_database() as (connection, first_pool, second_pool, ids):
        url = "https://example.test/#/detail/1"
        capture_id = await seed_material(connection, first_pool, ids, url=url)
        first, second = (
            PostgresWebLogicalTargetRepository(pool=p) for p in (first_pool, second_pool)
        )
        kwargs = dict(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            source_capture_id=capture_id,
            url=url,
        )
        recovered = await second.find(**kwargs)
        assert recovered["assessment_capture_id"] == capture_id
        assert recovered["assessment"]["page_type"] == "content_detail"
        primary_claims = await asyncio.gather(
            first.claim_primary(**kwargs), second.claim_primary(**kwargs)
        )
        assert sorted(item["created"] for item in primary_claims) == [False, True]
        assert (await second.claim_primary(**kwargs))["primary_result"] is None
        await first.finish_primary(
            **kwargs, result={"evidence_capture_id": None, "status": "no_target"}
        )
        assert (await second.claim_primary(**kwargs))["primary_result"] == {
            "evidence_capture_id": None,
            "status": "no_target",
        }
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_h5_page_type_assessment WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_h5_inspection_publication WHERE "
                "source_agent_run_id=$1::uuid",
                ids["run"],
            )
            == 0
        )


@pytest.mark.asyncio
async def test_redirect_alias_race_conflict_and_plain_url_change_are_distinct():
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )

    async with target_database() as (connection, first_pool, second_pool, ids):
        requested, landed = "http://example.test/go", "https://example.test/#/detail/1"
        first_id = await seed_material(
            connection,
            first_pool,
            ids,
            url=landed,
            requested_url=requested,
            redirect_chain=[requested, landed],
        )
        first, second = (
            PostgresWebLogicalTargetRepository(pool=p) for p in (first_pool, second_pool)
        )
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"])
        await asyncio.gather(
            *(
                r.record_capture_redirect_aliases(**scope, source_capture_id=first_id, url=landed)
                for r in (first, second)
            )
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_web_logical_target_alias WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            == 1
        )
        found = await second.find(**scope, source_capture_id=first_id, url=requested)
        assert found["assessment_capture_id"] == first_id
        assert (
            await second.find(
                **scope, source_capture_id=first_id, url="https://example.test/source-list"
            )
            is None
        )
        other = "https://example.test/#/detail/2"
        other_id = await seed_material(
            connection,
            first_pool,
            ids,
            url=other,
            sequence=2,
            requested_url=requested,
            redirect_chain=[requested, other],
        )
        with pytest.raises(ValueError, match="WEB_LOGICAL_ALIAS_CONFLICT"):
            await second.record_capture_redirect_aliases(
                **scope, source_capture_id=other_id, url=other
            )
        plain_id = await seed_material(
            connection,
            first_pool,
            ids,
            url="https://example.test/#/detail/3",
            sequence=3,
            requested_url="https://example.test/unknown",
        )
        await second.record_capture_redirect_aliases(
            **scope, source_capture_id=plain_id, url="https://example.test/#/detail/3"
        )
        assert (
            await second.find(
                **scope, source_capture_id=plain_id, url="https://example.test/unknown"
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_table", ["qc_web_logical_target", "qc_web_logical_target_alias"])
async def test_trusted_identity_reader_checks_actual_classification_and_missing_migration(
    missing_table,
):
    from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicyError
    from services.api.app.infrastructure.db.web_capture_identity import read_web_capture_identities

    async with target_database() as (connection, pool, _other, ids):
        capture_id = await seed_material(
            connection, pool, ids, url="https://example.test/#/detail/1"
        )
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"])
        identities = await read_web_capture_identities(connection, **scope)
        assert identities[capture_id]["logical_page_identity_status"] == "verified"
        await connection.execute(
            "UPDATE qc_page_capture SET metadata=metadata || $1::jsonb WHERE id=$2::uuid",
            json.dumps({"logical_page_id": "web-logical:" + "f" * 64}),
            capture_id,
        )
        assert (await read_web_capture_identities(connection, **scope))[capture_id][
            "logical_page_identity_status"
        ] == "WEB_LOGICAL_ID_CONFLICT"
        transaction = connection.transaction()
        await transaction.start()
        try:
            # 只在隔离事务中模拟未安装 0101；回滚恢复表名，不影响已有审计数据。
            assert await connection.fetchval(
                "SELECT to_regclass('qc_web_logical_target_t5_missing') IS NULL"
            )
            await connection.execute(
                f"ALTER TABLE {missing_table} RENAME TO qc_web_logical_target_t5_missing"
            )
            with pytest.raises(WebRunPolicyError, match="WEB_LOGICAL_TARGET_SCHEMA_MISSING"):
                await read_web_capture_identities(connection, **scope)
        finally:
            await transaction.rollback()
        assert await connection.fetchval("SELECT to_regclass('qc_web_logical_target') IS NOT NULL")


@pytest.mark.asyncio
async def test_target_claim_rejects_stale_current_capture():
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )

    async with target_database() as (connection, pool, _other, ids):
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=NULL WHERE session_id=$1",
            ids["session"],
        )
        with pytest.raises(ValueError, match="WEB_LOGICAL_TARGET_SCOPE_INVALID"):
            await PostgresWebLogicalTargetRepository(pool=pool).claim(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                source_capture_id=ids["capture"],
                url="https://example.test/",
            )


@pytest.mark.asyncio
async def test_known_menu_reuse_is_zero_action_and_wrong_target_is_rejected():
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        PostgresWebExplorationBudgetLedgerRepository,
    )
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )
    from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
        _command,
        _site,
    )

    async with target_database() as (connection, pool, other, ids):
        command, site = _command(ids), _site()
        budget = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        await budget.bootstrap(command, site, catalog_version="t5", catalog_sha256="a" * 64)
        capture_id = await seed_material(connection, pool, ids, url=site.entries[1].canonical_url)
        repo = PostgresWebLogicalTargetRepository(pool=other)
        target = await repo.find(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            source_capture_id=capture_id,
            url=site.entries[1].canonical_url,
        )
        with pytest.raises(ValueError, match="WEB_LOGICAL_TARGET_REUSE_CONFLICT"):
            await repo.record_menu_reuse(
                command=command,
                site_id=site.site_id,
                entry=site.entries[2],
                source_capture_id=capture_id,
                target=target,
            )
        await repo.record_menu_reuse(
            command=command,
            site_id=site.site_id,
            entry=site.entries[1],
            source_capture_id=capture_id,
            target=target,
        )
        summary = await budget.read_summary(agent_run_id=ids["run"], site_id=site.site_id)
        reused = next(
            row for row in summary.menu_ledger if row.entry_id == site.entries[1].entry_id
        )
        assert reused.status.value == "logical_target_reused"
        assert reused.menu_action_count == reused.detail_action_count == 0
        assert reused.menu_ready_capture_id is None
        assert reused.reused_capture_id == capture_id
        await repo.record_menu_reuse(
            command=command,
            site_id=site.site_id,
            entry=site.entries[1],
            source_capture_id=capture_id,
            target=target,
        )
        await budget.bootstrap(command, site, catalog_version="t5", catalog_sha256="a" * 64)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_h5_page_type_assessment WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            == 1
        )
        await connection.execute(
            "DELETE FROM qc_h5_page_type_assessment WHERE page_capture_id=$1::uuid", capture_id
        )
        recovered = await budget.read_summary(agent_run_id=ids["run"], site_id=site.site_id)
        missing = next(
            row for row in recovered.menu_ledger if row.entry_id == site.entries[1].entry_id
        )
        assert missing.status.value == "navigation_failed"
        assert missing.error_code == "WEB_LOGICAL_TARGET_MATERIAL_MISSING"
        with pytest.raises(ValueError, match="WEB_LOGICAL_TARGET_MATERIAL_MISSING"):
            await repo.find(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                source_capture_id=capture_id,
                url=site.entries[1].canonical_url,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested, landed, expected_home",
    [
        ("https://example.test/", "https://example.test/landing", True),
        ("http://example.test/", "https://example.test/landing", True),
        ("https://example.test/", "https://example.test/#/detail/1", False),
    ],
)
async def test_only_trusted_root_redirect_can_restore_subpath_home_role(
    requested, landed, expected_home
):
    from services.api.app.infrastructure.db.web_capture_identity import read_web_capture_identities
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )

    async with target_database() as (connection, pool, _other, ids):
        # 请求入口来自真实动作，而不是落地页反推的协议。
        capture_id = await seed_material(
            connection,
            pool,
            ids,
            url=landed,
            requested_url=requested,
            redirect_chain=[requested, landed],
            page_type="content_list",
        )
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"])
        assert not (await read_web_capture_identities(connection, **scope))[capture_id][
            "home_role_verified"
        ]
        await PostgresWebLogicalTargetRepository(pool=pool).record_capture_redirect_aliases(
            **scope, session_id=ids["session"], source_capture_id=capture_id, url=landed
        )
        assert (await read_web_capture_identities(connection, **scope))[capture_id][
            "home_role_verified"
        ] is expected_home


@pytest.mark.asyncio
async def test_duplicate_discovered_after_action_keeps_fresh_pair_and_original_material():
    from services.api.app.application.web_exploration_budget_ledger import (
        WebExplorationBudgetLedgerConflictError,
        WebMenuLedgerTerminalRecord,
    )
    from services.api.app.application.web_logical_target import WebLogicalTargetClassifier
    from services.api.app.domain.exploration import WebMenuTerminalStatus
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        PostgresWebExplorationBudgetLedgerRepository,
    )
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )
    from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
        _command,
        _site,
    )
    from services.api.tests.test_web_page_type_assessment import _capture

    class NoRepeatClassifier:
        async def classify(self, capture):
            raise AssertionError("重复页面不得调用分类器或通知")

    async with target_database() as (connection, pool, other, ids):
        command, site = _command(ids), _site()
        budget = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        await budget.bootstrap(command, site, catalog_version="t5", catalog_sha256="a" * 64)
        url = "https://example.test/#/detail/1"
        original = await seed_material(connection, pool, ids, url=url)
        entry = site.entries[2]
        reservation = await budget.claim_menu(
            command=command, entry=entry, source_capture_id=original
        )
        probe = await seed_material(
            connection,
            pool,
            ids,
            url=url,
            sequence=2,
            with_material=False,
            source_capture_id=original,
            intent_id=reservation.intent_id,
        )
        ready = await seed_material(
            connection, pool, ids, url=url, sequence=3, with_material=False, source_capture_id=probe
        )
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"])
        result = await WebLogicalTargetClassifier(
            classifier=NoRepeatClassifier(),
            repository=PostgresWebLogicalTargetRepository(pool=other),
            **scope,
        ).classify(replace(_capture(), capture_id=ready, url=url))
        assert result.reused_capture_id == original and ready != original
        action_id = await connection.fetchval(
            "SELECT action_execution_id FROM qc_capture WHERE id=$1::uuid", probe
        )
        record = WebMenuLedgerTerminalRecord(
            **scope,
            site_id=site.site_id,
            menu_entry_id=entry.entry_id,
            status=WebMenuTerminalStatus.LOGICAL_TARGET_REUSED,
            menu_action_execution_id=action_id,
            menu_probe_capture_id=probe,
            menu_ready_capture_id=ready,
            menu_page_type="content_detail",
            diagnostic={
                "reused_capture_id": original,
                "reused_logical_page_id": result.reused_logical_page_id,
            },
        )
        with pytest.raises(WebExplorationBudgetLedgerConflictError):
            await budget.finalize(replace(record, menu_ready_capture_id=original))
        await budget.finalize(record)
        recovered = await PostgresWebExplorationBudgetLedgerRepository(pool=other).read_summary(
            agent_run_id=ids["run"], site_id=site.site_id
        )
        row = next(item for item in recovered.menu_ledger if item.entry_id == entry.entry_id)
        assert row.menu_action_count == 1 and row.detail_action_count == 0
        assert row.menu_ready_capture_id == ready and row.reused_capture_id == original
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_h5_page_type_assessment WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            == 1
        )


@pytest.mark.asyncio
async def test_readonly_find_does_not_wait_for_session_writer():
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )

    async with target_database() as (connection, pool, _other, ids):
        repo = PostgresWebLogicalTargetRepository(pool=pool)
        kwargs = dict(task_id=ids["task"], agent_run_id=ids["run"],
                      session_id=ids["session"], source_capture_id=ids["capture"],
                      url="https://example.test/")
        await repo.claim(**kwargs)
        async with connection.transaction():
            await connection.execute(
                "SELECT 1 FROM qc_surface_session WHERE session_id=$1 FOR UPDATE",
                ids["session"],
            )
            result = await asyncio.wait_for(repo.find(**kwargs), timeout=2)
            assert result["state"] == "claimed"
