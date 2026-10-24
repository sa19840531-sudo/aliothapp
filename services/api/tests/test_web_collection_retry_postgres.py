"""观察换帧失败经真实阶段与 PG 重试，再由重建的执行器继续。"""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.api.app.application.web_run_stage_orchestrator import WebRunStageOrchestrator
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    PostgresWebRunStageCheckpointRepository,
)
from services.api.app.infrastructure.db.web_runtime_stage_evidence_repository import (
    PostgresWebRuntimeStageEvidenceRepository,
)
from services.api.tests.test_web_auxiliary_stage_postgres import _page
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _command,
    _database,
    _upgrade_isolated_postgres,  # noqa: F401 -- 使用隔离库迁移夹具。
)
from services.api.tests.test_web_run_stage_checkpoint_postgres import seed_current
from services.api.tests.test_web_runtime_discovery_postgres import _enabled_case

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="只运行于显式隔离PG库",
)


async def test_collection_retry_restarts_from_current_capture_in_real_database():
    async with _database() as (control, pool, other, ids):
        await _enabled_case(control, pool, ids, menu_limit=5)
        await seed_current(control, ids)
        await _page(control, ids, ids["capture"])
        observed = str(uuid4())
        sources = []

        class Readiness:
            async def prepare(self, *, source_capture_id, page_role):
                sources.append(source_capture_id)
                if len(sources) == 1:
                    await control.execute(
                        "INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,"
                        "surface_session_id,capture_role,capture_status,captured_at,metadata) "
                        "VALUES($1::uuid,$2::uuid,$3::uuid,'web',$4,'probe_before','captured',"
                        "now(),'{\"finalization_epoch\":0}')",
                        observed,
                        ids["task"],
                        ids["run"],
                        ids["session"],
                    )
                    await _page(control, ids, observed, sequence=2)
                    await control.execute(
                        "UPDATE qc_surface_session SET current_capture_id=$1::uuid "
                        "WHERE session_id=$2",
                        observed,
                        ids["session"],
                    )
                    return SimpleNamespace(
                        status="timeout", ready_capture_id=None, blocking_reasons=()
                    )
                return SimpleNamespace(status="stable", ready_capture_id=observed)

        runtime = SimpleNamespace(page_readiness=Readiness())

        def orchestrator(selected_pool, worker):
            checkpoints = PostgresWebRunStageCheckpointRepository(pool=selected_pool)
            evidence = PostgresWebRuntimeStageEvidenceRepository(
                task_id=ids["task"],
                run_id=ids["run"],
                session_id=ids["session"],
                pool=selected_pool,
            )
            effects = WebRuntimeStageEffects(
                runtime=runtime,
                evidence=evidence,
                command=_command(ids),
                checkpoints=checkpoints,
            )
            return WebRunStageOrchestrator(
                checkpoints=checkpoints, effects=effects, worker_id=worker
            )

        key = "collection-detail:collection:menu:one"
        await PostgresWebRunStageCheckpointRepository(pool=pool).create(
            WebStageCursor.start(
                run_id=ids["run"],
                logical_page_key=key,
                stage=WebRunStage.READINESS,
                cursor_payload={
                    "session_id": ids["session"],
                    "finalization_epoch": 0,
                    "capture_id": ids["capture"],
                    "current_url": "https://example.test/search",
                },
            )
        )
        first = await orchestrator(pool, "before-restart").advance_once(ids["run"], key)
        assert first.cursor.cursor_payload["capture_id"] == observed
        await control.execute(
            "UPDATE qc_web_run_stage_checkpoint "
            "SET next_attempt_at=clock_timestamp()-interval '1 second' "
            "WHERE run_id=$1::uuid AND logical_page_key=$2",
            ids["run"],
            key,
        )
        second = await orchestrator(other, "after-restart").advance_once(ids["run"], key)
        assert sources == [ids["capture"], observed]
        assert second.cursor.status.value == "succeeded"
        assert second.cursor.cursor_payload["capture_id"] == observed
