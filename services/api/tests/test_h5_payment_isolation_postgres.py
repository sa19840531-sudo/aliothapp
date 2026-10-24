"""显式隔离数据库验证展示恢复租约；不连接本地验收库。"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_targeted_recapture import H5TargetedRecapturePlanService
from services.api.app.domain.inspection import EvidenceInteractionRequirement, RestorationStrategy
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.db.h5_closed_session_context import (
    PostgresH5ClosedSessionContextReader,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)
from services.api.tests.test_h5_closed_session_recapture_postgres import _closed_request

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="必须显式提供隔离测试数据库",
)


@pytest.mark.parametrize("invalid", [None, "unleased", "expired", "terminal", "epoch", "platform"])
async def test_active_isolated_display_context_requires_real_current_lease(invalid):
    require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"],
        os.getenv("ALIOTH_DATABASE_URL"),
    )
    requirement = EvidenceInteractionRequirement(
        operation=InteractionOperation.CLICK,
        semantic_target="选择当前会员档位",
        purpose="只读展示补证",
        parameters={"goal_capability": "payment_plan_selection"},
    )
    async with _closed_request(
        task_status="running",
        request_changes={"required_interaction": requirement},
        check_constraint=False,
    ) as fixture:
        connection, pool, ids, request, _, _, _, _ = fixture
        await connection.execute(
            "UPDATE qc_surface_session SET status='active',draining_at=NULL,closed_at=NULL,"
            "current_capture_id=$2"
            " WHERE session_id=$1",
            f"h5:{ids['run']}",
            ids["original"],
        )
        await connection.execute(
            "UPDATE qc_task SET finalization_state='open',exploration_draining_at=NULL,"
            "session_draining_at=NULL WHERE id=$1",
            ids["task"],
        )
        if invalid in {"unleased", "expired", "terminal"}:
            assignment = {
                "unleased": "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL",
                "expired": "lease_expires_at=now()-interval '1 second'",
                "terminal": "status='unfulfillable',completed_at=now(),lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL",
            }[invalid]
            await connection.execute(
                "UPDATE qc_evidence_request SET " + assignment + " WHERE request_id=$1",
                request.request_id,
            )
        elif invalid == "epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1", ids["task"]
            )
        elif invalid == "platform":
            await connection.execute(
                "UPDATE qc_task SET target_platform='web' WHERE id=$1", ids["task"]
            )
        before = await connection.fetchrow(
            "SELECT * FROM qc_evidence_request WHERE request_id=$1",
            request.request_id,
        )
        reader = PostgresH5ClosedSessionContextReader(pool=pool)
        context = await reader.read_isolated_display_context(request=request)
        assert (context is not None) is (invalid is None)
        if invalid is None:
            assert context.source_session_closed is False
            assert context.replay_session_id != context.session_id
            runtime = SimpleNamespace(remember_new_session_plan=AsyncMock())
            result = await H5TargetedRecapturePlanService(
                contexts=reader,
                new_session_runtime=runtime,
            ).resolve(request=request, agent_run_id=ids["run"], finalization_epoch=0)
            assert result.executable
            assert result.plan.strategy is RestorationStrategy.NEW_SESSION_REPLAY
            assert runtime.remember_new_session_plan.call_args.kwargs["context"].isolated_display
        assert (
            await connection.fetchrow(
                "SELECT * FROM qc_evidence_request WHERE request_id=$1",
                request.request_id,
            )
            == before
        )
