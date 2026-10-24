"""真实 PostgreSQL 上走通 EvidenceRequest → RestorationAttempt → EquivalenceAssessment → ProbeWork。

审查里「ProbeWork 无法发布」的根因是路由先伪造 ``state-equivalence:pending:*`` 再发布，
而 ``qc_v2_work_item`` 的外键与 CHECK 要求发布前已经存在 status=equivalent 的
``qc_restoration_attempt``。这条用例按方案 03 的顺序在真库上把整条链走一遍：
请求受理 → 创建恢复尝试 → 用独立的新 Capture 做等价评估并持久化 →
``RestoredProbePublicationService`` 用真实 attempt / assessment 发布 Probe；再反向证明
伪造评估 ID 会被拒、证明字段对不上会被拒、没有请求建不了恢复尝试，且拒绝原因都是稳定码。

恢复执行器用桩：它只负责「交出一张新 Capture 的状态快照」——真实平台实现（App 已有，
Web 待接）替换的就是这一个端口，链路其余部分全走生产代码。
默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.exploration_work_queue import ExplorerWorkPersistenceError
from services.api.app.application.orchestration import QcOrchestrator
from services.api.app.application.restoration import (
    RestorationCoordinator,
    RestorationExecutionRequest,
    RestorationExecutionResult,
)
from services.api.app.application.restored_probe_publication import (
    RestoredProbePublicationService,
)
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.application.supplemental_evidence_policy import (
    build_lab_supplemental_evidence_policy,
)
from services.api.app.domain.exploration import ExplorationBudget, build_probe_work
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequest,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationAttemptStatus,
    RestorationStrategy,
    StateInvariantSnapshot,
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)
from services.api.app.infrastructure.db.explorer_work_repository import (
    PROBE_WORK_AVAILABLE,
    PUBLISH_REJECTION_EQUIVALENCE_PROOF_MISMATCH,
    PUBLISH_REJECTION_RESTORATION_ATTEMPT_MISSING,
    PostgresExplorerWorkRepository,
    PostgresProbeWorkPublisher,
)
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    ATTEMPT_REJECTION_EVIDENCE_REQUEST_MISSING,
    PostgresRestorationAttemptRepository,
    RestorationAttemptPersistenceError,
)

SESSION_ID = "session-web-chain"


class _ObserveOnlyExecutor:
    """原 Surface Pin 的最小恢复执行器：不做动作，只交出新 Capture 的状态快照。"""

    def __init__(self, restored_snapshot: StateInvariantSnapshot) -> None:
        self._restored = restored_snapshot
        self.requests: list[RestorationExecutionRequest] = []

    async def restore(self, request: RestorationExecutionRequest) -> RestorationExecutionResult:
        self.requests.append(request)
        return RestorationExecutionResult(restored_snapshot=self._restored)


def _snapshot(*, task_id: str, capture_id: str, captured_at: datetime) -> StateInvariantSnapshot:
    return StateInvariantSnapshot(
        task_id=task_id,
        capture_id=capture_id,
        session_id=SESSION_ID,
        surface_kind=SurfaceKind.WEB,
        environment={},
        business={"route": "/detail/42"},
        semantic={},
        structure={},
        captured_at=captured_at,
    )


def _budget() -> ExplorationBudget:
    return ExplorationBudget(
        max_actions=3,
        remaining_actions=3,
        max_depth=2,
        remaining_duration_ms=180_000,
    )


def _request(ids: dict[str, str], *, rule_code: str, expires_at: datetime) -> EvidenceRequest:
    return build_evidence_request(
        work_item_id=ids["work_item"],
        attempt_id=ids["attempt"],
        task_id=ids["task"],
        rule_id=ids["rule"],
        rule_code=rule_code,
        target_capture_id=ids["original"],
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采关闭弹窗后的详情页",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.SNAPSHOT,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/detail/42"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=expires_at,
    )


async def test_probe_is_published_only_from_equivalent_restoration_attempt() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in (
            "tenant",
            "user",
            "ruleset",
            "rule",
            "task",
            "run",
            "work_item",
            "attempt",
            "original",
            "restored",
        )
    }
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed(connection, ids)
        now = datetime.now(UTC)
        request = _request(ids, rule_code="alioth_chain_1", expires_at=now + timedelta(hours=1))
        requests = PostgresEvidenceRequestRepository(pool=pool)
        await requests.upsert(request=request)

        # 1. 原 Capture 仍是当前面：不导航，但恢复执行器仍要重新观察一张新 Capture
        plan = build_restoration_plan(
            request=request,
            source_session_id=SESSION_ID,
            target_session_id=SESSION_ID,
            surface_kind=SurfaceKind.WEB,
            strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
            deadline=now + timedelta(minutes=30),
        )
        captured_at = {
            row["id"]: row["captured_at"]
            for row in await connection.fetch(
                "SELECT id::text AS id, captured_at FROM qc_capture WHERE id = ANY($1::uuid[])",
                [ids["original"], ids["restored"]],
            )
        }
        original_snapshot = _snapshot(
            task_id=ids["task"],
            capture_id=ids["original"],
            captured_at=captured_at[ids["original"]],
        )
        restored_snapshot = _snapshot(
            task_id=ids["task"],
            capture_id=ids["restored"],
            captured_at=captured_at[ids["restored"]],
        )
        attempts = PostgresRestorationAttemptRepository(pool=pool)
        executor = _ObserveOnlyExecutor(restored_snapshot)
        coordinator = RestorationCoordinator(
            repository=attempts,
            executor=executor,
            equivalence_gate=StateEquivalenceGate(),
        )

        # 2. 恢复 → 等价评估 → 终态持久化
        attempt = await coordinator.restore(
            request=request,
            plan=plan,
            attempt_no=1,
            original_snapshot=original_snapshot,
        )

        assert len(executor.requests) == 1
        assert attempt.status is RestorationAttemptStatus.EQUIVALENT
        assert attempt.assessment is not None and attempt.assessment.passed
        assert attempt.restored_capture_id == ids["restored"]
        persisted = await connection.fetchrow(
            """
            SELECT status, restored_capture_id::text AS restored_capture_id,
                   equivalence_assessment_id, assessment_payload->>'passed' AS passed
            FROM qc_restoration_attempt WHERE attempt_id = $1
            """,
            attempt.attempt_id,
        )
        assert persisted["status"] == "equivalent"
        assert persisted["restored_capture_id"] == ids["restored"]
        assert persisted["equivalence_assessment_id"] == attempt.assessment.assessment_id
        assert persisted["passed"] == "true"

        # 3. 只有这时才能发布 Probe，而且带的是真实 attempt / assessment 身份
        policy = build_lab_supplemental_evidence_policy()
        work_repository = PostgresExplorerWorkRepository(
            pool=pool,
            supplemental_evidence_policy=policy,
        )
        publisher = PostgresProbeWorkPublisher(
            repository=work_repository,
            supplemental_evidence_policy=policy,
        )
        service = RestoredProbePublicationService(
            orchestrator=QcOrchestrator(probe_publisher=publisher),
        )

        probe = await service.publish(
            request=request,
            restoration_attempt=attempt,
            budget=_budget(),
        )

        work_row = await connection.fetchrow(
            """
            SELECT work_type, status, source_capture_id::text AS source_capture_id,
                   evidence_request_id, restoration_attempt_id, equivalence_assessment_id,
                   source_grade, work_payload->>'surface_kind' AS surface_kind
            FROM qc_v2_work_item WHERE work_id = $1
            """,
            probe.work_id,
        )
        assert work_row["work_type"] == "probe"
        assert work_row["status"] == "pending"
        assert work_row["source_capture_id"] == ids["restored"]
        assert work_row["evidence_request_id"] == request.request_id
        assert work_row["restoration_attempt_id"] == attempt.attempt_id
        assert work_row["equivalence_assessment_id"] == attempt.assessment.assessment_id
        assert work_row["source_grade"] == "A1"
        assert work_row["surface_kind"] == "web"
        request_status = await connection.fetchval(
            "SELECT status FROM qc_evidence_request WHERE request_id = $1",
            request.request_id,
        )
        assert request_status == "routed"
        wakeup = await connection.fetchval(
            """
            SELECT message_type FROM qc_outbox_event
            WHERE aggregate_type = 'explorer_work' AND aggregate_id = $1
            """,
            probe.work_id,
        )
        assert wakeup == PROBE_WORK_AVAILABLE

        # 反向一：伪造「待完成」评估 ID 的 Probe 被拒，原因是稳定码而不是笼统的归属无效
        fabricated = build_probe_work(
            session_id=SESSION_ID,
            branch_id=f"probe:{request.request_id}",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=ids["restored"],
            restoration_plan_hash=plan.plan_hash,
            restoration_plan=plan,
            restoration_attempt_id=f"restoration-attempt:fabricated:{request.request_id}",
            equivalence_assessment_id=f"state-equivalence:pending:{request.request_id}",
            source_grade=EvidenceSourceGrade.A1,
            evidence_request=request,
            budget=_budget(),
            deadline=plan.deadline,
        )
        with pytest.raises(ExplorerWorkPersistenceError) as fabricated_rejection:
            await work_repository.publish(fabricated)
        assert (
            fabricated_rejection.value.reason_code
            == PUBLISH_REJECTION_RESTORATION_ATTEMPT_MISSING
        )

        # 反向二：真实恢复尝试，但 Probe 带的恢复计划哈希与它对不上
        mismatched = build_probe_work(
            session_id=SESSION_ID,
            branch_id=f"probe:{request.request_id}",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=ids["restored"],
            restoration_plan_hash="restoration-plan-hash-drift",
            restoration_attempt_id=attempt.attempt_id,
            equivalence_assessment_id=attempt.assessment.assessment_id,
            source_grade=EvidenceSourceGrade.A1,
            evidence_request=request,
            budget=_budget(),
            deadline=now + timedelta(minutes=10),
        )
        with pytest.raises(ExplorerWorkPersistenceError) as mismatch_rejection:
            await work_repository.publish(mismatched)
        assert (
            mismatch_rejection.value.reason_code == PUBLISH_REJECTION_EQUIVALENCE_PROOF_MISMATCH
        )
        assert "恢复计划哈希" in str(mismatch_rejection.value)

        # 反向三：没有这条请求就建不了恢复尝试
        orphan = _request(ids, rule_code="alioth_chain_orphan", expires_at=now + timedelta(hours=1))
        orphan_plan = build_restoration_plan(
            request=orphan,
            source_session_id=SESSION_ID,
            target_session_id=SESSION_ID,
            surface_kind=SurfaceKind.WEB,
            strategy=RestorationStrategy.ORIGINAL_SURFACE_PIN,
            deadline=now + timedelta(minutes=30),
        )
        with pytest.raises(RestorationAttemptPersistenceError) as missing_request:
            await attempts.start(plan=orphan_plan, attempt_no=1)
        assert missing_request.value.reason_code == ATTEMPT_REJECTION_EVIDENCE_REQUEST_MISSING
    finally:
        await _cleanup(connection, ids)
        await pool.close()
        await connection.close()


async def _seed(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $2, 'Recapture Chain')
        """,
        ids["tenant"],
        f"recapture-chain-{ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (id, username, full_name, password_hash, role, tenant_id)
        VALUES ($1::uuid, $2, 'Recapture Chain', 'not-a-login-secret', '平台管理员', $3::uuid)
        """,
        ids["user"],
        f"recapture-chain-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        )
        VALUES ($1::uuid, $2, 'Recapture Chain', 1, 'enabled', 'snapshot-1', 1, 'manual')
        """,
        ids["ruleset"],
        f"recapture-chain-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_rule (
            id, ruleset_id, rule_code, name, category, executor_type,
            severity, status, check_point
        )
        VALUES (
            $1::uuid, $2::uuid, 'alioth_chain_1', 'Chain Rule',
            'visual', 'hybrid', 'high', 'enabled', '检查详情页'
        )
        """,
        ids["rule"],
        ids["ruleset"],
    )
    await connection.execute(
        """
        INSERT INTO qc_task (id, target_url, ruleset_id, status, submitter_id, tenant_id)
        VALUES ($1::uuid, 'https://example.com', $2::uuid, 'running', $3::uuid, $4::uuid)
        """,
        ids["task"],
        ids["ruleset"],
        ids["user"],
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
        VALUES ($1::uuid, $2::uuid, 'main', 'Recapture Chain', 'running')
        """,
        ids["run"],
        ids["task"],
    )
    # qc_page_capture 的插入触发器会同步一条同 id 的 qc_capture 公共行（surface_kind=web）；
    # 这条链不经过 Surface Session 表，快照里的 session_id 只在计划与快照之间比对
    for capture_key, page_seq, page_slug, age in (
        ("original", 1, "detail", "2 minutes"),
        ("restored", 2, "detail-reobserved", "0 seconds"),
    ):
        await connection.execute(
            f"""
            INSERT INTO qc_page_capture (
                id, task_id, agent_run_id, page_seq, page_slug, url, page_type,
                capture_status, captured_at
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4, $5, 'https://example.com/detail/42',
                'video_detail', 'captured', now() - interval '{age}'
            )
            """,
            ids[capture_key],
            ids["task"],
            ids["run"],
            page_seq,
            page_slug,
        )
    await connection.execute(
        """
        INSERT INTO qc_inspection_work_item (
            id, task_id, page_capture_id, producer_agent_run_id, consumer_agent_run_id,
            ruleset_id, ruleset_version, ruleset_snapshot_hash, batch_key, strategy,
            category, session_dependency, input_hash, status, attempt_count, max_attempts,
            control_mode, finalization_epoch
        )
        VALUES (
            $1::uuid, $2::uuid, $3::uuid, $4::uuid, $4::uuid,
            $5::uuid, 1, 'snapshot-1', 'hybrid:visual:snapshot', 'hybrid',
            'visual', 'snapshot', 'input-chain', 'waiting_evidence', 1, 3,
            'enforced', 0
        )
        """,
        ids["work_item"],
        ids["task"],
        ids["original"],
        ids["run"],
        ids["ruleset"],
    )
    await connection.execute(
        """
        INSERT INTO qc_inspection_work_item_rule (
            work_item_id, rule_id, rule_code, ordinal, contract_hash, status
        )
        VALUES ($1::uuid, $2::uuid, 'alioth_chain_1', 0, 'contract-1', 'pending')
        """,
        ids["work_item"],
        ids["rule"],
    )
    await connection.execute(
        """
        INSERT INTO qc_inspection_attempt (
            id, work_item_id, consumer_agent_run_id, attempt_no, lease_token, status, input_hash
        )
        VALUES ($1::uuid, $2::uuid, $3::uuid, 1, gen_random_uuid(), 'running', 'input-chain')
        """,
        ids["attempt"],
        ids["work_item"],
        ids["run"],
    )


async def _cleanup(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    # 带 RESTRICT 外键的表先删，别指望级联顺序
    for statement in (
        "DELETE FROM qc_outbox_event WHERE task_id = $1::uuid",
        "DELETE FROM qc_v2_work_item WHERE task_id = $1::uuid",
        "DELETE FROM qc_restoration_attempt WHERE task_id = $1::uuid",
        "DELETE FROM qc_evidence_request WHERE task_id = $1::uuid",
        "DELETE FROM qc_inspection_work_item WHERE task_id = $1::uuid",
        "DELETE FROM qc_capture WHERE task_id = $1::uuid",
        "DELETE FROM qc_agent_run WHERE task_id = $1::uuid",
        "DELETE FROM qc_task WHERE id = $1::uuid",
    ):
        await connection.execute(statement, ids["task"])
    await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
    await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
    await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
    await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
