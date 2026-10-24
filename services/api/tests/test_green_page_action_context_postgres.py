"""在独立PG验证页面共享判定补读的真实字段、动作血缘及只读边界。"""

from dataclasses import replace
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.green_page_action_context import (
    read_green_page_action_context,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_checkpoint_evidence_postgres import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
    case,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_green_process_evidence_postgres import counts, prepare


async def test_page_context_reads_real_action_without_changing_owned_records(tmp_path):
    async with case() as (connection, repo, ids, work, context, decision):
        decision = replace(decision, parameters={"target_url": "https://outside.example.test/"})
        _, turn = await prepare(
            connection,
            repo,
            ids,
            work,
            context,
            decision,
            LocalEvidenceArtifactStore(root=tmp_path),
        )
        before = await counts(connection, ids["task"])
        roles = {
            item.role: item.capture_id
            for item in (
                turn.coordination.before_capture,
                turn.coordination.after_capture,
            )
        }
        platform, actions, issues = await read_green_page_action_context(
            repo._pool,
            task_id=ids["task"],
            capture_roles=roles,
            source_agent_run_id=ids["run"],
        )
        assert platform == "h5" and not issues and len(actions) == 1
        action = next(iter(actions.values()))
        assert action["_source_capture_id"] == roles["before_action"]
        assert action["_result_capture_id"] == roles["after_action"]
        assert action["_session_id"] == ids["session"]
        assert action["_action_type"] == "click"
        assert action["_command_sequence"] == 2
        assert action["_started_at"] and action["_finished_at"]
        assert action["parameters"] == decision.parameters
        assert await counts(connection, ids["task"]) == before


@pytest.mark.parametrize("fault", ["run", "capture", "role", "sequence", "session", "epoch"])
async def test_page_context_never_accepts_mismatched_action_lineage(tmp_path, fault):
    async with case() as (connection, repo, ids, work, context, decision):
        _, turn = await prepare(
            connection,
            repo,
            ids,
            work,
            context,
            decision,
            LocalEvidenceArtifactStore(root=tmp_path),
        )
        roles = {
            item.role: item.capture_id
            for item in (
                turn.coordination.before_capture,
                turn.coordination.after_capture,
            )
        }
        run = ids["run"]
        if fault == "run":
            run = str(uuid4())
        elif fault == "capture":
            roles["after_action"] = str(uuid4())
        elif fault == "role":
            await connection.execute(
                "UPDATE qc_capture SET capture_role='primary' WHERE id=$1::uuid",
                roles["after_action"],
            )
        elif fault == "sequence":
            await connection.execute(
                "UPDATE qc_capture SET session_command_sequence=999 WHERE id=$1::uuid",
                roles["after_action"],
            )
        elif fault == "session":
            await connection.execute(
                "UPDATE qc_capture SET surface_session_id='foreign-session' WHERE id=$1::uuid",
                roles["after_action"],
            )
        else:
            # 正式数据库自身禁止把完成动作换到另一个周期。
            with pytest.raises(asyncpg.RaiseError):
                await connection.execute(
                    "UPDATE qc_action_execution SET finalization_epoch=99 WHERE task_id=$1::uuid",
                    ids["task"],
                )
            return
        before = await counts(connection, ids["task"])
        _, actions, issues = await read_green_page_action_context(
            repo._pool,
            task_id=ids["task"],
            capture_roles=roles,
            source_agent_run_id=run,
        )
        assert actions == {} and issues
        assert await counts(connection, ids["task"]) == before
