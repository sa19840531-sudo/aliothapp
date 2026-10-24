"""真实 PostgreSQL 中 App 终态动作结算的持久化与恢复回归。"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from services.api.app.application.app_capture_persistence import (
    AppCapturePersistenceService,
)
from services.api.app.application.exploration_work_queue import (
    ClaimExplorerWorkCommand,
    ExplorerWorkStatus,
    ExplorerWorkType,
    TransitionExplorerWorkCommand,
)
from services.api.app.domain.exploration import (
    ExplorationTerminal,
    ExplorationTerminalStatus,
)
from services.api.app.infrastructure.db.app_bootstrap_repository import (
    PostgresAppBootstrapRepository,
)
from services.api.app.infrastructure.db.app_capture_repository import (
    PostgresAppCaptureRepository,
)
from services.api.app.infrastructure.db.app_exploration_runtime_recovery_repository import (
    PostgresAppExplorationRuntimeRecoveryRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.explorer_work_repository import (
    PostgresExplorerWorkRepository,
)
from services.api.app.infrastructure.storage.local_evidence_store import (
    LocalEvidenceArtifactStore,
)
from services.api.tests.test_app_postgres_integration import (
    _advance_to_bootstrap_capturing,
    _app_database,
    _app_exploration_work,
    _screenshot_artifacts,
    _start_and_claim,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)


@pytest.mark.asyncio
async def test_postgres_terminal_action_count_survives_recovery_reader(tmp_path) -> None:
    """真实 Work terminal_payload 写入后，重启读侧仍恢复最后一轮动作计数。"""

    async with _app_database() as (connection, pool, ids):
        bootstrap = PostgresAppBootstrapRepository(pool=pool)
        captures = AppCapturePersistenceService(
            repository=PostgresAppCaptureRepository(pool=pool),
            artifact_store=LocalEvidenceArtifactStore(root=tmp_path / "evidence"),
            manifest_repository=PostgresEvidenceManifestRepository(pool=pool),
        )
        snapshot = await _start_and_claim(connection, pool, ids, owner="worker-a")
        snapshot = await _advance_to_bootstrap_capturing(
            bootstrap,
            ids,
            snapshot,
            "worker-a",
        )
        persisted = await captures.persist_bootstrap(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            artifacts=_screenshot_artifacts(ids),
            expected_bootstrap_row_version=snapshot.row_version,
            workflow_lease_owner="worker-a",
            workflow_lease_token=ids["token_a"],
        )
        work = _app_exploration_work(ids, capture=persisted.capture)
        works = PostgresExplorerWorkRepository(pool=pool)
        await works.publish(work)

        owner = "terminal-counter-worker"
        lease = await works.claim(
            command=ClaimExplorerWorkCommand(
                work_type=ExplorerWorkType.EXPLORATION,
                lease_owner=owner,
                task_id=ids["task"],
            ),
            lease_token=str(uuid4()),
        )
        assert lease is not None
        running_version = await works.transition(
            command=TransitionExplorerWorkCommand(
                work_id=work.work_id,
                expected_status=ExplorerWorkStatus.LEASED,
                target_status=ExplorerWorkStatus.RUNNING,
                lease_token=lease.lease_token,
                row_version=lease.row_version,
            )
        )
        terminal = ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
            reason="deadline 已到，停止后继提交",
            outcome="evidence_insufficient",
            reason_code="deadline_reached",
            coverage_summary={
                "consumed_action_count": 1,
                "result_capture_id": persisted.capture.capture_id,
            },
        )
        await works.transition(
            command=TransitionExplorerWorkCommand(
                work_id=work.work_id,
                expected_status=ExplorerWorkStatus.RUNNING,
                target_status=ExplorerWorkStatus.BUDGET_EXHAUSTED,
                lease_token=lease.lease_token,
                row_version=running_version,
                terminal=terminal,
            )
        )

        recovery = await PostgresAppExplorationRuntimeRecoveryRepository(
            pool=pool
        ).get_work_recovery(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            surface_session_id=ids["session"],
        )

        assert recovery.consumed_action_count == 1
        assert recovery.terminal is not None
        assert recovery.terminal.coverage_summary["consumed_action_count"] == 1
        assert recovery.terminal.coverage_summary["result_capture_id"] == (
            persisted.capture.capture_id
        )
        stored = await connection.fetchrow(
            "SELECT terminal_payload FROM qc_v2_work_item WHERE work_id = $1",
            work.work_id,
        )
        assert stored is not None
        raw_payload = stored["terminal_payload"]
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        assert payload["coverage_summary"]["consumed_action_count"] == 1
