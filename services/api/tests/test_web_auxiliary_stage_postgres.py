"""辅助阶段导航恢复与收尾的真实 PostgreSQL 归属、幂等和领取围栏。"""

import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.api.app.application.web_run_stage_orchestrator import WebRunStageOrchestrator
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor
from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
    PostgresWebAuxiliaryNavigationClaimRepository,
)
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
    WebStageCheckpointConflict,
)
from services.api.app.infrastructure.db.web_runtime_stage_evidence_repository import (
    PostgresWebRuntimeStageEvidenceRepository,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _command,
    _database,
    _seed_completed_action_evidence,
    _upgrade_isolated_postgres,  # noqa: F401 -- 注册隔离库迁移 fixture。
)
from services.api.tests.test_web_run_stage_checkpoint_postgres import seed_current
from services.api.tests.test_web_runtime_discovery_postgres import _enabled_case

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="只运行于显式双 DSN 隔离测试库",
)


async def _page(control, ids, capture_id, *, sequence=1, page_type="search_page"):
    await control.execute(
        "INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,url,"
        "capture_status,surface_session_id,capture_role,session_command_sequence,"
        "metadata,page_type) "
        "VALUES($1::uuid,$2::uuid,$3::uuid,$5::int,'aux','https://example.test/search',"
        "'captured',$4,'probe_before',$5::int,'{\"finalization_epoch\":0}'::jsonb,$6)",
        capture_id, ids["task"], ids["run"], ids["session"], sequence, page_type,
    )


async def _setup(control, pool, ids, *, claim=True, stage=WebRunStage.EVIDENCE_RECONCILIATION):
    await _enabled_case(control, pool, ids, menu_limit=5)
    await seed_current(control, ids)
    await _page(control, ids, ids["capture"])
    await control.execute(
        "UPDATE qc_surface_session SET command_sequence=1 WHERE session_id=$1", ids["session"],
    )
    claims = PostgresWebAuxiliaryNavigationClaimRepository(pool=pool)
    phase_id = "unclaimed-phase"
    if claim:
        phase_id = await claims.claim(
            task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"],
            phase_key="web_search_discovery", source_capture_id=ids["capture"],
            candidate_source_capture_id=ids["capture"], candidate_id="candidate-search",
        )
        assert phase_id is not None
    checkpoints = PostgresWebRunStageCheckpointRepository(pool=pool)
    initial = await checkpoints.create(WebStageCursor.start(
        run_id=ids["run"], logical_page_key="aux:search", stage=stage,
        cursor_payload={
            "session_id": ids["session"], "finalization_epoch": 0,
            "capture_id": ids["capture"], "parent_capture_id": ids["capture"],
            "idempotency_key": "candidate-search", "surface_phase_run_id": phase_id,
            "current_url": "https://example.test/search",
            "requested_url": "https://example.test/search",
        },
    ))
    cursor = await checkpoints.claim(
        run_id=ids["run"], logical_page_key=initial.logical_page_key,
        expected_row_version=initial.row_version, worker_id="aux-test",
    )
    return claims, checkpoints, cursor


async def _finish(repository, ids, cursor):
    method = getattr(repository, "finish_stage", None)
    assert callable(method), "辅助收尾必须具备独立阶段 fence 与幂等恢复接口"
    return await method(
        command=_command(ids), cursor=cursor, status="completed",
        reason_code="WEB_AUXILIARY_CLASSIFIED", result_capture_id=ids["capture"],
        page_type="search_page",
    )


@pytest.mark.parametrize(
    ("phase_key", "accepted"),
    [
        ("web_activity_navigation", True),
        ("web_search_discovery", False),
    ],
)
async def test_auxiliary_collection_page_finishes_only_activity_phase(
    phase_key, accepted,
):
    async with _database() as (control, pool, _, ids):
        await _enabled_case(control, pool, ids, menu_limit=5)
        await seed_current(control, ids)
        await _page(control, ids, ids["capture"], page_type="collection_page")
        await control.execute(
            "UPDATE qc_surface_session SET command_sequence=1 WHERE session_id=$1",
            ids["session"],
        )
        repository = PostgresWebAuxiliaryNavigationClaimRepository(pool=pool)
        phase_id = await repository.claim(
            task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"],
            phase_key=phase_key, source_capture_id=ids["capture"],
            candidate_source_capture_id=ids["capture"], candidate_id="candidate-collection",
        )
        assert phase_id is not None
        finish = repository.finish(
            phase_id=phase_id, status="completed", reason_code="WEB_AUXILIARY_CLASSIFIED",
            result_capture_id=ids["capture"], page_type="collection_page",
        )
        if accepted:
            await finish
        else:
            with pytest.raises(RuntimeError, match="WEB_AUXILIARY_FINISH_SCOPE_INVALID"):
                await finish
        row = await control.fetchrow(
            "SELECT status,context_payload->>'page_type' AS page_type "
            "FROM qc_surface_phase_run WHERE phase_run_id=$1",
            phase_id,
        )
        assert row["status"] == ("completed" if accepted else "running")
        assert row["page_type"] == ("collection_page" if accepted else None)


async def test_auxiliary_finish_rebuild_is_idempotent_without_rewriting_terminal(monkeypatch):
    async with _database() as (control, pool, other, ids):
        repository, _, cursor = await _setup(control, pool, ids)
        await _finish(repository, ids, cursor)
        before = await control.fetchrow(
            "SELECT * FROM qc_surface_phase_run WHERE phase_run_id=$1",
            cursor.cursor_payload["surface_phase_run_id"],
        )
        await _finish(PostgresWebAuxiliaryNavigationClaimRepository(pool=other), ids, cursor)
        after = await control.fetchrow(
            "SELECT * FROM qc_surface_phase_run WHERE phase_run_id=$1",
            cursor.cursor_payload["surface_phase_run_id"],
        )
        assert dict(after) == dict(before)
        assert after["status"] == "completed"


async def test_auxiliary_finish_without_claim_does_not_create_phase():
    async with _database() as (control, pool, _, ids):
        repository, _, cursor = await _setup(control, pool, ids, claim=False)
        await _finish(repository, ids, cursor)
        assert await control.fetchval(
            "SELECT count(*) FROM qc_surface_phase_run WHERE task_id=$1::uuid", ids["task"],
        ) == 0


@pytest.mark.parametrize(
    "invalid", ["task", "run", "session", "epoch", "token", "cancel", "expired"],
)
async def test_auxiliary_finish_rejects_foreign_or_lost_stage_without_mutation(invalid):
    async with _database() as (control, pool, _, ids):
        repository, _, cursor = await _setup(control, pool, ids)
        altered = dict(ids)
        if invalid in {"task", "run"}:
            altered[invalid] = str(uuid4())
        elif invalid == "session":
            altered[invalid] = "web:" + str(uuid4())
        elif invalid == "epoch":
            cursor = replace(
                cursor, cursor_payload={**cursor.cursor_payload, "finalization_epoch": 1},
            )
        elif invalid == "token":
            cursor = replace(cursor, claim_token=str(uuid4()))
        elif invalid == "cancel":
            await control.execute(
                "UPDATE qc_agent_run SET output_context='{\"cancel_requested\":true}'::jsonb "
                "WHERE id=$1::uuid", ids["run"],
            )
        else:
            await _expire(control, ids["run"])
        before = await control.fetchrow(
            "SELECT * FROM qc_surface_phase_run WHERE phase_run_id=$1",
            cursor.cursor_payload["surface_phase_run_id"],
        )
        with pytest.raises(WebStageCheckpointConflict):
            await _finish(repository, altered, cursor)
        after = await control.fetchrow(
            "SELECT * FROM qc_surface_phase_run WHERE phase_run_id=$1",
            cursor.cursor_payload["surface_phase_run_id"],
        )
        assert dict(after) == dict(before)


async def test_auxiliary_navigation_recovery_matches_exact_run_session_epoch_and_intent():
    async with _database() as (control, pool, _, ids):
        _, _, cursor = await _setup(control, pool, ids, stage=WebRunStage.BOOTSTRAP_NAVIGATION)
        _, after = await _seed_completed_action_evidence(
            control, ids, action_execution_id="action:" + ids["run"],
            intent_id=f"web-auxiliary:{ids['run']}:candidate-search", command_sequence=2,
            requested_source_capture_id=ids["capture"], parent_capture_id=ids["capture"],
        )
        await control.execute(
            "UPDATE qc_capture SET metadata='{\"finalization_epoch\":0}'::jsonb WHERE id=$1::uuid",
            after,
        )
        await _page(control, ids, after, sequence=2)
        await control.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
            after, ids["session"],
        )
        repository = PostgresWebRuntimeStageEvidenceRepository(
            task_id=ids["task"], run_id=ids["run"], session_id=ids["session"], pool=pool,
        )
        restored = await repository.find_navigation(cursor)
        assert restored.cursor_payload["capture_id"] == after
        for field, value in (("idempotency_key", "other"), ("finalization_epoch", 1)):
            invalid = replace(cursor, cursor_payload={**cursor.cursor_payload, field: value})
            assert await repository.find_navigation(invalid) is None
        for field, value in (("task_id", str(uuid4())), ("run_id", str(uuid4())),
                             ("session_id", "web:" + str(uuid4()))):
            args = {"task_id": ids["task"], "run_id": ids["run"], "session_id": ids["session"]}
            args[field] = value
            if field == "task_id":
                foreign = PostgresWebRuntimeStageEvidenceRepository(**args, pool=pool)
                assert await foreign.find_navigation(cursor) is None
            else:
                with pytest.raises(ValueError, match="Session"):
                    PostgresWebRuntimeStageEvidenceRepository(**args, pool=pool)


async def _expire(control, run_id):
    await control.execute(
        "UPDATE qc_web_run_stage_checkpoint SET claimed_at=now()-interval '11 minutes',"
        "lease_expires_at=now()-interval '1 minute' "
        "WHERE run_id=$1::uuid AND stage_status='running'", run_id,
    )


@pytest.mark.parametrize("persisted_classification", [False, True])
async def test_auxiliary_child_survives_expired_parent_around_classification(
    persisted_classification,
):
    async with _database() as (control, pool, other, ids):
        _, checkpoints, child = await _setup(
            control, pool, ids, stage=WebRunStage.CLASSIFICATION,
        )
        await control.execute(
            "UPDATE qc_page_capture SET page_type=NULL WHERE id=$1::uuid", ids["capture"],
        )
        parent = await checkpoints.create(WebStageCursor.start(
            run_id=ids["run"], logical_page_key="home", stage=WebRunStage.FRONTIER_DRAIN,
            cursor_payload={"session_id": ids["session"], "finalization_epoch": 0},
        ))
        await checkpoints.claim(
            run_id=ids["run"], logical_page_key="home",
            expected_row_version=parent.row_version, worker_id="stopped-parent",
        )

        class Stopped(BaseException):
            """模拟分类写库前后进程结束，保留真实领取等待接管。"""

        class Classifier:
            calls = 0

            async def classify(self, ready):
                self.calls += 1
                if persisted_classification:
                    await control.execute(
                        "UPDATE qc_page_capture SET page_type='content_list' WHERE id=$1::uuid",
                        ready.capture_id,
                    )
                raise Stopped()

        class ReadyReader:
            async def read(self, *, capture_id, page_role):
                return SimpleNamespace(capture_id=capture_id, url="https://example.test/search")

        class Snapshots:
            async def read_snapshot(self, *, capture_id):
                return {"elements": [{"class_name": "banner", "href": "https://ad.test/one"}]}

        evidence = PostgresWebRuntimeStageEvidenceRepository(
            task_id=ids["task"], run_id=ids["run"], session_id=ids["session"], pool=pool,
        )
        classifier = Classifier()
        runtime = SimpleNamespace(
            page_type_classifier=classifier, stage_ready_capture_reader=ReadyReader(),
            ad_snapshot_reader=Snapshots(), ad_registered_domain="example.test",
            stage_ad_capture_port=object(),
        )
        effects = WebRuntimeStageEffects(
            runtime=runtime, evidence=evidence, command=_command(ids), checkpoints=checkpoints,
        )
        with pytest.raises(Stopped):
            await effects.execute(child)
        await _expire(control, ids["run"])
        recovered = PostgresWebRunStageCheckpointRepository(pool=other)
        old_parent = await recovered.get(run_id=ids["run"], logical_page_key="home")
        gapped_parent = await recovered.claim(
            run_id=ids["run"], logical_page_key="home",
            expected_row_version=old_parent.row_version, worker_id="recovery",
        )
        assert gapped_parent.status.value == "gapped"
        orchestrator = WebRunStageOrchestrator(
            checkpoints=recovered, effects=effects, worker_id="recovery",
        )
        expired = await orchestrator.advance_once(ids["run"], "aux:search")
        assert expired.cursor.status.value == "gapped"
        assert expired.cursor.last_error_code == "WEB_STAGE_LEASE_EXPIRED"
        from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
            WebStageCheckpointConflict,
        )

        with pytest.raises(WebStageCheckpointConflict):
            await checkpoints.mark_succeeded(child)
        resumed = await orchestrator.advance_once(ids["run"], "aux:search")
        # 分类额度耗尽只能进入排空；已有页型可供独立广告审计，不补算分类成功。
        assert resumed.cursor.current_stage is WebRunStage.FRONTIER_DRAIN
        assert resumed.cursor.status.value == "succeeded"
        assert resumed.cursor.gaps == expired.cursor.gaps
        assert classifier.calls == 1
        ads = [
            row for row in await recovered.list_for_run(run_id=ids["run"])
            if row.logical_page_key.startswith("ad:")
        ]
        assert len(ads) == (1 if persisted_classification else 0)
        if ads:
            assert ads[0].cursor_payload["capture_id"] == ids["capture"]
            assert ads[0].cursor_payload["requested_url"] == "https://ad.test/one"
