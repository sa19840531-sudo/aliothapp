"""真实账本终态经过冻结身份 reader 到 bundle，覆盖三个复用阶段。"""

import json
import os
from dataclasses import replace

import pytest

from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
    WebMenuLedgerTerminalRecord,
)
from services.api.app.application.web_logical_target import WebLogicalTargetClassifier
from services.api.app.application.web_menu_exploration import WebMenuVisitResult, _terminal_record
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.app.infrastructure.db.web_logical_target_repository import (
    PostgresWebLogicalTargetRepository,
)
from services.api.tests.test_web_evidence_bundle import _valid_snapshot
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _command,
    _site,
)
from services.api.tests.test_web_logical_target_postgres import seed_material, target_database
from services.api.tests.test_web_page_type_assessment import _capture
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="需显式隔离 PostgreSQL"
)


async def bundle_facts(connection, ids):
    # 支持产物与动作事实来自受控夹具；绝不填充 fresh 的分类或逻辑 ID。
    existing_edges = await connection.fetch(
        "SELECT * FROM qc_page_graph_transition WHERE task_id=$1::uuid ORDER BY transition_id",
        ids["task"],
    )
    metrics = _valid_snapshot()["captures"][0]
    await connection.execute(
        "UPDATE qc_agent_run SET input_context=$1::jsonb WHERE id=$2::uuid",
        json.dumps({"task_config": {"_task_target": {"platform": "web"}}}),
        ids["run"],
    )
    await connection.execute(
        "INSERT INTO qc_page_capture(id,task_id,page_seq,page_slug,url,capture_status,captured_at,"
        "agent_run_id,surface_session_id,capture_role,session_command_sequence,metadata) "
        "VALUES($1::uuid,$2::uuid,1000,'bootstrap','https://example.test/','captured',now(),"
        "$3::uuid,$4,'probe_before',1,'{\"finalization_epoch\":0}'::jsonb)",
        ids["capture"],
        ids["task"],
        ids["run"],
        ids["session"],
    )
    await connection.execute(
        "UPDATE qc_page_capture page SET parent_page_capture_id=action.requested_source_capture_id "
        "FROM qc_action_execution action WHERE page.task_id=$1::uuid "
        "AND page.id=action.source_capture_id",
        ids["task"],
    )
    await connection.execute(
        "UPDATE qc_page_capture SET metadata=metadata || $1::jsonb WHERE task_id=$2::uuid",
        json.dumps(
            {
                key: metrics[key]
                for key in (
                    "pipeline_metrics",
                    "web_evidence_admission",
                    "web_product_capability_assessment",
                )
            }
        ),
        ids["task"],
    )
    captures = await connection.fetch(
        "SELECT id,capture_role FROM qc_page_capture WHERE task_id=$1::uuid", ids["task"]
    )
    for capture in captures:
        for kind, mime in (
            ("screenshot", "image/png"),
            ("dom", "text/html"),
            ("action_trace", "application/json"),
        ):
            if kind == "action_trace" and capture["capture_role"] != "after_action":
                continue
            await connection.execute(
                "INSERT INTO qc_evidence_artifact(task_id,page_capture_id,artifact_type,source,"
                "storage_path,content_type,checksum,size_bytes)"
                " VALUES($1::uuid,$2::uuid,$3,'baseline','isolated/t5',$4,$5,1)",
                ids["task"],
                capture["id"],
                kind,
                mime,
                "sha256:" + "a" * 64,
            )
    # 仅补缺失的受控替身边；既有 T6 边的证明、到达与审计字段一律保留。
    await connection.execute(
        "INSERT INTO qc_page_graph_transition(transition_id,task_id,agent_run_id,surface_kind,"
        "surface_session_id,finalization_epoch,command_sequence,action_execution_id,"
        "source_capture_id,target_capture_id,status,proof,counts_as_business_arrival,terminal) "
        "SELECT action_execution_id,task_id,$2::uuid,'web',session_id,0,command_sequence,"
        "action_execution_id,source_capture_id,result_capture_id,'completed','trusted',true,true "
        "FROM qc_action_execution WHERE task_id=$1::uuid "
        "ON CONFLICT (transition_id) DO NOTHING",
        ids["task"],
        ids["run"],
    )
    for previous in existing_edges:
        current = await connection.fetchrow(
            "SELECT * FROM qc_page_graph_transition WHERE transition_id=$1",
            previous["transition_id"],
        )
        assert dict(current) == dict(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["menu", "detail_before_action", "detail_after_action"])
async def test_real_terminal_summary_reader_and_bundle_preserve_reused_material(stage):
    class NoRepeatClassifier:
        async def classify(self, capture):
            raise AssertionError("重复观察不得重新分类或通知")

    async with target_database() as (connection, pool, other, ids):
        command = _command(ids)
        full_site = _site()
        site = replace(full_site, entries=full_site.entries[:3])
        budget = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        await budget.bootstrap(command, site, catalog_version="t5-review", catalog_sha256="b" * 64)
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"])

        async def seed(url, seq, source, *, material=False, kind="content_detail", intent=None):
            return await seed_material(
                connection,
                pool,
                ids,
                url=url,
                sequence=seq,
                source_capture_id=source,
                with_material=material,
                page_type=kind,
                intent_id=intent,
            )

        home_probe = await seed("https://example.test/", 1, ids["capture"])
        home = await seed(
            "https://example.test/", 2, home_probe, material=True, kind="home"
        )
        home_record = WebMenuLedgerTerminalRecord(
                **scope,
                site_id=site.site_id,
                menu_entry_id="home",
                status=WebMenuTerminalStatus.HOME_BASELINE,
                menu_probe_capture_id=home_probe,
                menu_ready_capture_id=home,
                menu_page_type="home",
        )
        # 新冻结目录不能借列表分类冒充首页；复用测试先建立合法首页前提。
        with pytest.raises(WebExplorationBudgetLedgerConflictError):
            await budget.finalize(replace(home_record, menu_page_type="content_list"))
        await budget.finalize(home_record)
        original_url = "https://example.test/#/detail/1"
        reservation = await budget.claim_menu(
            command=command, entry=site.entries[1], source_capture_id=home
        )
        old_probe = await seed(original_url, 3, home, intent=reservation.intent_id)
        original = await seed(original_url, 4, old_probe, material=True)

        async def action_id(capture_id):
            return await connection.fetchval(
                "SELECT action_execution_id FROM qc_capture WHERE id=$1::uuid", capture_id
            )

        await budget.finalize(
            WebMenuLedgerTerminalRecord(
                **scope,
                site_id=site.site_id,
                menu_entry_id=site.entries[1].entry_id,
                status=WebMenuTerminalStatus.DETAIL_CAPTURED,
                menu_action_execution_id=await action_id(old_probe),
                menu_probe_capture_id=old_probe,
                menu_ready_capture_id=original,
                menu_page_type="content_detail",
            )
        )
        entry = site.entries[2]
        reservation = await budget.claim_menu(
            command=command, entry=entry, source_capture_id=original
        )
        new_list = stage != "menu"
        menu_url = "https://example.test/list" if new_list else original_url
        menu_probe = await seed(menu_url, 5, original, intent=reservation.intent_id)
        menu_ready = await seed(menu_url, 6, menu_probe, material=new_list, kind="content_list")
        detail_probe = detail_ready = detail_action = None
        count = 0
        if stage == "detail_after_action":
            detail = await budget.reserve_detail(
                command=command,
                entry=entry,
                source_capture_id=menu_ready,
                candidate_id="detail-candidate",
                destination_url=original_url,
            )
            detail_probe = await seed(original_url, 7, menu_ready, intent=detail.intent_id)
            detail_ready = await seed(original_url, 8, detail_probe)
            detail_action, count = await action_id(detail_probe), 1
        repo = PostgresWebLogicalTargetRepository(pool=other)
        if stage == "detail_before_action":
            reused = await repo.find(**scope, source_capture_id=menu_ready, url=original_url)
            assert reused["assessment_capture_id"] == original
            reused_id = reused["logical_page_id"]
        else:
            classified = await WebLogicalTargetClassifier(
                classifier=NoRepeatClassifier(),
                repository=repo,
                **scope,
            ).classify(replace(_capture(), capture_id=detail_ready or menu_ready, url=original_url))
            assert classified.reused_capture_id == original
            reused_id = classified.reused_logical_page_id
        record = _terminal_record(
            command=command,
            site_id=site.site_id,
            entry=entry,
            result=WebMenuVisitResult(
                final_capture_id=menu_ready,
                page_type="content_list" if new_list else "content_detail",
                status=WebMenuTerminalStatus.LOGICAL_TARGET_REUSED,
                menu_action_count=1,
                menu_action_execution_id=await action_id(menu_probe),
                menu_probe_capture_id=menu_probe,
                menu_ready_capture_id=menu_ready,
                menu_page_type="content_list" if new_list else "content_detail",
                detail_page_type="content_detail" if new_list else None,
                detail_action_count=count,
                detail_action_execution_id=detail_action,
                detail_probe_capture_id=detail_probe,
                detail_ready_capture_id=detail_ready,
                reused_capture_id=original,
                reused_logical_page_id=reused_id,
            ),
        )
        assert record.diagnostic["reused_target_stage"] == stage
        if stage == "detail_before_action":
            assert record.detail_page_type is None and record.detail_action_execution_id is None
        await budget.finalize(record)
        recovered = await PostgresWebExplorationBudgetLedgerRepository(pool=other).read_summary(
            agent_run_id=ids["run"],
            site_id=site.site_id,
        )
        terminal = next(row for row in recovered.menu_ledger if row.entry_id == entry.entry_id)
        assert terminal.menu_action_count == 1 and terminal.detail_action_count == count
        await bundle_facts(connection, ids)
        snapshot = await PostgresWebCaptureEvidenceRepository(pool=other).list_for_task(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            access_scope=AccessScope(
                actor_user_id=ids["user"],
                tenant_id=ids["tenant"],
                permissions=frozenset({"taskObjects:read:tenant"}),
            ),
        )
        assert snapshot is not None
        bundle = build_web_evidence_bundle(snapshot)
        assert bundle["status"] == "evidence_ready", bundle["reason_codes"]
        assert len(bundle["formal_pages"]) == (3 if new_list else 2)
        assert {row["capture_id"] for row in bundle["captures"]} == {
            row["capture_id"] for row in snapshot["captures"]
        }
        assert bundle["entries"][-1]["menu_action_count"] == 1
        assert bundle["entries"][-1]["detail_action_count"] == count
        if new_list:
            assert menu_ready in {row["primary_capture_id"] for row in bundle["formal_pages"]}
        fresh_duplicate = detail_ready or (menu_ready if stage == "menu" else None)
        if fresh_duplicate:
            row = next(row for row in snapshot["captures"] if row["capture_id"] == fresh_duplicate)
            assert row["page_type"] is None and row["logical_page_id"] is None
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_h5_page_type_assessment WHERE agent_run_id=$1::uuid",
            ids["run"],
        ) == (3 if new_list else 2)
