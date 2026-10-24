"""重复详情遇菜单主体补证换帧时的持久血缘围栏。"""

import json
from dataclasses import asdict, replace

import pytest

from services.api.tests.test_web_logical_target_postgres import seed_material, target_database


@pytest.mark.asyncio
async def test_reused_detail_uses_only_current_same_menu_primary_capture():
    from services.api.app.application.web_exploration_budget_ledger import (
        WebExplorationBudgetLedgerConflictError,
        WebMenuLedgerTerminalRecord,
    )
    from services.api.app.application.web_page_classification import WebPageAssessment
    from services.api.app.application.web_page_type_assessment import (
        WebPageTypeAssessmentRecorder,
    )
    from services.api.app.domain.exploration import WebMenuTerminalStatus
    from services.api.app.infrastructure.db.web_capture_evidence_repository import (
        PostgresWebCaptureEvidenceRepository,
    )
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        PostgresWebExplorationBudgetLedgerRepository,
    )
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )
    from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
        WebStageCheckpointConflict,
    )
    from services.api.app.infrastructure.db.web_runtime_stage_evidence_repository import (
        PostgresWebRuntimeStageEvidenceRepository,
    )
    from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
        _command,
        _site,
    )
    from services.api.tests.test_web_page_type_assessment import _capture, _catalog_identity

    async with target_database() as (connection, pool, other, ids):
        command, site = _command(ids), _site()
        budget = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        catalog_identity = _catalog_identity()
        await budget.bootstrap(
            command, site, catalog_version=catalog_identity.catalog_version,
            catalog_sha256=catalog_identity.catalog_sha256.removeprefix("sha256:"),
        )
        recorder = WebPageTypeAssessmentRecorder(
            repository=PostgresWebCaptureEvidenceRepository(
                pool=pool, assessment_schema_version="web-page-type-assessment.v4",
                catalog_identity=catalog_identity,
            ),
            catalog_identity=catalog_identity,
        )
        detail_url = "https://example.test/video/7219/"
        original = await seed_material(
            connection, pool, ids, url=detail_url, with_material=False
        )
        detail_assessment = WebPageAssessment(
            page_type="video_detail", confidence=0.95, evidence=("视频详情",)
        )
        await recorder.persist(
            task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"],
            capture=replace(_capture(), capture_id=original, url=detail_url, page_role="detail"),
            assessment=detail_assessment,
        )
        logical = PostgresWebLogicalTargetRepository(pool=pool)
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"])
        await logical.claim(**scope, source_capture_id=original, url=detail_url)
        await logical.materialize(
            **scope, capture_id=original, url=detail_url, assessment=asdict(detail_assessment)
        )
        entry = site.entries[1]
        menu_url = entry.canonical_url
        reservation = await budget.claim_menu(
            command=command, entry=entry, source_capture_id=original
        )
        probe = await seed_material(
            connection, pool, ids, url=menu_url, sequence=2, with_material=False,
            source_capture_id=original, intent_id=reservation.intent_id,
        )
        ready = await seed_material(
            connection, pool, ids, url=menu_url, sequence=3, with_material=False,
            source_capture_id=probe, intent_id="web-readiness:observe:menu-one",
        )
        await recorder.persist(
            task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"],
            capture=replace(_capture(), capture_id=ready, url=menu_url, page_role="menu"),
            assessment=WebPageAssessment(
                page_type="video_list", confidence=0.95, evidence=("列表卡",)
            ),
        )
        scrolled = await seed_material(
            connection, pool, ids, url=menu_url, sequence=4, with_material=False,
            source_capture_id=ready, intent_id=f"web-primary-content:{ready}:1",
        )
        target = await PostgresWebLogicalTargetRepository(pool=other).find(
            task_id=ids["task"], agent_run_id=ids["run"],
            session_id=ids["session"], source_capture_id=scrolled, url=detail_url,
        )
        assert target["assessment_capture_id"] == original

        action_id = await connection.fetchval(
            "SELECT action_execution_id FROM qc_capture WHERE id=$1::uuid", probe
        )
        validator = PostgresWebRuntimeStageEvidenceRepository(
            task_id=ids["task"], run_id=ids["run"], session_id=ids["session"], pool=other
        )
        kwargs = dict(
            site_id=site.site_id, menu_entry_id=entry.entry_id,
            menu_action_execution_id=action_id,
            menu_probe_capture_id=probe, menu_ready_capture_id=ready,
        )

        # 旧 ready 在主体滚动后不是当前帧，持久账本仍必须拒绝旧引用。
        record = WebMenuLedgerTerminalRecord(
            task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"],
            site_id=site.site_id, menu_entry_id=entry.entry_id,
            status=WebMenuTerminalStatus.LOGICAL_TARGET_REUSED,
            menu_action_execution_id=action_id, menu_probe_capture_id=probe,
            menu_ready_capture_id=ready, menu_page_type="video_list",
            diagnostic={
                "reused_capture_id": original,
                "reused_logical_page_id": target["logical_page_id"],
            },
        )
        with pytest.raises(WebExplorationBudgetLedgerConflictError):
            await budget.finalize(record)

        # 非当前帧、别的菜单以及伪装的主体动作都不可提升为菜单终态。
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
            ready, ids["session"],
        )
        assert await validator.current_menu_capture_after_primary(**kwargs) == ready
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
            original, ids["session"],
        )
        with pytest.raises(WebStageCheckpointConflict):
            await validator.current_menu_capture_after_primary(**kwargs)
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
            scrolled, ids["session"],
        )
        with pytest.raises(WebStageCheckpointConflict):
            await validator.current_menu_capture_after_primary(
                **{**kwargs, "menu_entry_id": site.entries[2].entry_id}
            )
        other_entry = site.entries[2]
        other_reservation = await budget.claim_menu(
            command=command, entry=other_entry, source_capture_id=scrolled
        )
        other_probe = await seed_material(
            connection, pool, ids, url=menu_url, sequence=5, with_material=False,
            source_capture_id=scrolled, intent_id=other_reservation.intent_id,
        )
        await seed_material(
            connection, pool, ids, url=menu_url, sequence=6, with_material=False,
            source_capture_id=other_probe, intent_id="web-readiness:observe:menu-two",
        )
        with pytest.raises(WebStageCheckpointConflict):
            await validator.current_menu_capture_after_primary(**kwargs)
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
            scrolled, ids["session"],
        )
        with pytest.raises(WebStageCheckpointConflict):
            await validator.current_menu_capture_after_primary(
                **{**kwargs, "menu_action_execution_id": "wrong-menu-action"}
            )
        primary_action = await connection.fetchval(
            "SELECT action_execution_id FROM qc_capture WHERE id=$1::uuid", scrolled
        )
        await connection.execute(
            "UPDATE qc_action_execution SET intent_id='web-readiness:observe:wrong' "
            "WHERE action_execution_id=$1", primary_action,
        )
        with pytest.raises(WebStageCheckpointConflict):
            await validator.current_menu_capture_after_primary(**kwargs)
        await connection.execute(
            "UPDATE qc_action_execution SET intent_id=$1 WHERE action_execution_id=$2",
            f"web-primary-content:{ready}:1", primary_action,
        )

        current = await validator.current_menu_capture_after_primary(**kwargs)
        assert current == scrolled
        await budget.finalize(replace(
            record, menu_ready_capture_id=current,
            diagnostic={
                **record.diagnostic, "menu_classification_capture_id": ready,
            },
        ))
        row = await connection.fetchrow(
            "SELECT status,menu_ready_capture_id,diagnostic "
            "FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid "
            "AND site_id=$2 AND menu_entry_id=$3",
            ids["run"], site.site_id, entry.entry_id,
        )
        assert row["status"] == "logical_target_reused"
        assert str(row["menu_ready_capture_id"]) == scrolled
        assert json.loads(row["diagnostic"])["menu_classification_capture_id"] == ready
