"""发布 Probe / 创建恢复尝试被拒时给出稳定原因码，而不是笼统的「归属无效」。

审查里「ProbeWork 无法发布」的根因是路由伪造了 ``state-equivalence:pending:*`` 的评估
ID，落库时 ``qc_restoration_attempt`` 里根本没有这条恢复尝试。以前只报「Explorer Work
归属无效、任务不可运行或幂等身份发生漂移」，看不出是任务、Capture、请求、恢复尝试还是
等价证明出了问题；现在仓储在被拒后做一次只读诊断，按 INSERT 的前置条件顺序报第一条
不满足的。这里用假连接把诊断行喂进去，逐条验证映射；真库整链见
``test_targeted_recapture_chain_postgres``。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.exploration_work_queue import ExplorerWorkPersistenceError
from services.api.app.application.supplemental_evidence_policy import (
    build_lab_supplemental_evidence_policy,
)
from services.api.app.domain.exploration import ExplorationBudget, ProbeWork, build_probe_work
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequest,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    RestorationPlan,
    RestorationStrategy,
    build_evidence_request,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.explorer_work_repository import (
    PUBLISH_REJECTION_EQUIVALENCE_PROOF_MISMATCH,
    PUBLISH_REJECTION_EVIDENCE_REQUEST_INVALID,
    PUBLISH_REJECTION_RESTORATION_ATTEMPT_MISSING,
    PUBLISH_REJECTION_RESTORATION_NOT_EQUIVALENT,
    PUBLISH_REJECTION_SOURCE_CAPTURE_INVALID,
    PUBLISH_REJECTION_TASK_NOT_RUNNABLE,
    PUBLISH_REJECTION_WORK_IDENTITY_DRIFT,
    PostgresExplorerWorkRepository,
)
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    ATTEMPT_REJECTION_ATTEMPT_IDENTITY_DRIFT,
    ATTEMPT_REJECTION_EVIDENCE_REQUEST_EXPIRED,
    ATTEMPT_REJECTION_EVIDENCE_REQUEST_INVALID,
    ATTEMPT_REJECTION_EVIDENCE_REQUEST_MISSING,
    ATTEMPT_REJECTION_ORIGINAL_CAPTURE_INVALID,
    ATTEMPT_REJECTION_REPLAY_PLAN_MISSING,
    ATTEMPT_REJECTION_TASK_NOT_RUNNABLE,
    PostgresRestorationAttemptRepository,
    RestorationAttemptPersistenceError,
)
from services.api.tests.test_restoration_persistence import FakeConnection, FakePool

TASK_ID = "00000000-0000-0000-0000-00000000a001"
WORK_ITEM_ID = "00000000-0000-0000-0000-00000000a002"
ATTEMPT_ID = "00000000-0000-0000-0000-00000000a003"
RULE_ID = "00000000-0000-0000-0000-00000000a004"
ORIGINAL_CAPTURE_ID = "00000000-0000-0000-0000-00000000a005"
RESTORED_CAPTURE_ID = "00000000-0000-0000-0000-00000000a006"
SESSION_ID = "session-web-1"


def _now() -> datetime:
    return datetime.now(UTC)


def _request(
    *,
    session_dependency: EvidenceSessionDependency = EvidenceSessionDependency.SNAPSHOT,
) -> EvidenceRequest:
    return build_evidence_request(
        work_item_id=WORK_ITEM_ID,
        attempt_id=ATTEMPT_ID,
        task_id=TASK_ID,
        rule_id=RULE_ID,
        rule_code="alioth_1",
        target_capture_id=ORIGINAL_CAPTURE_ID,
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采遮挡关闭后的详情页",
        required_artifact_types=("screenshot",),
        session_dependency=session_dependency,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"route": "/detail/42"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=_now() + timedelta(hours=1),
    )


def _plan(
    request: EvidenceRequest,
    *,
    strategy: RestorationStrategy = RestorationStrategy.ORIGINAL_SURFACE_PIN,
    replay_execution_plan_id: str | None = None,
) -> RestorationPlan:
    return build_restoration_plan(
        request=request,
        source_session_id=SESSION_ID,
        target_session_id=SESSION_ID,
        surface_kind=SurfaceKind.WEB,
        strategy=strategy,
        deadline=_now() + timedelta(minutes=30),
        replay_execution_plan_id=replay_execution_plan_id,
    )


def _probe(
    request: EvidenceRequest,
    plan: RestorationPlan,
    *,
    attempt_id: str = "restoration-attempt:fabricated",
    assessment_id: str = "state-equivalence:pending:fabricated",
) -> ProbeWork:
    return build_probe_work(
        session_id=SESSION_ID,
        branch_id=f"probe:{request.request_id}",
        surface_kind=SurfaceKind.WEB,
        source_capture_id=RESTORED_CAPTURE_ID,
        restoration_plan_hash=plan.plan_hash,
        restoration_plan=plan,
        restoration_attempt_id=attempt_id,
        equivalence_assessment_id=assessment_id,
        source_grade=EvidenceSourceGrade.A1,
        evidence_request=request,
        budget=ExplorationBudget(
            max_actions=3,
            remaining_actions=3,
            max_depth=2,
            remaining_duration_ms=180_000,
        ),
        # ProbeWork 会校验工作上下文与恢复计划一致，截止时间要和计划同一个
        deadline=plan.deadline,
    )


def _publish_facts(**overrides: Any) -> dict[str, Any]:
    """诊断查询的一行事实；默认「除了恢复尝试不存在，其余都满足」。"""
    facts: dict[str, Any] = {
        "task_exists": True,
        "task_status": "running",
        "task_target_platform": "h5",
        "task_finalization_state": "open",
        "capture_exists": True,
        "capture_task_id": TASK_ID,
        "capture_status": "captured",
        "request_exists": True,
        "request_task_id": TASK_ID,
        "request_status": "pending",
        "attempt_exists": False,
        "attempt_request_id": None,
        "attempt_task_id": None,
        "attempt_status": None,
        "attempt_passed": None,
        "attempt_assessment_id": None,
        "attempt_source_grade": None,
        "attempt_plan_hash": None,
        "attempt_original_capture_id": None,
        "attempt_restored_capture_id": None,
        "work_exists": False,
        "existing_work_type": None,
        "existing_payload_hash": None,
        "existing_attempt_id": None,
        "existing_assessment_id": None,
    }
    facts.update(overrides)
    return facts


def _equivalent_attempt_facts(work: ProbeWork, plan: RestorationPlan) -> dict[str, Any]:
    """与 Probe 完全对得上的等价恢复尝试事实。"""
    return _publish_facts(
        attempt_exists=True,
        attempt_request_id=work.evidence_request.request_id,
        attempt_task_id=TASK_ID,
        attempt_status="equivalent",
        attempt_passed="true",
        attempt_assessment_id=work.equivalence_assessment_id,
        attempt_source_grade="A1",
        attempt_plan_hash=plan.plan_hash,
        attempt_original_capture_id=ORIGINAL_CAPTURE_ID,
        attempt_restored_capture_id=RESTORED_CAPTURE_ID,
    )


async def _publish_rejection(
    work: ProbeWork,
    facts: dict[str, Any] | None,
) -> ExplorerWorkPersistenceError:
    # fetchrow 依次是：请求 upsert 回写 → 工作项 INSERT（拒）→ 只读诊断
    results: list[Any] = [{"status": "pending"}, None]
    if facts is not None:
        results.append(facts)
    repository = PostgresExplorerWorkRepository(
        pool=FakePool(FakeConnection(fetchrow_results=results)),
        supplemental_evidence_policy=build_lab_supplemental_evidence_policy(),
    )
    with pytest.raises(ExplorerWorkPersistenceError) as rejected:
        await repository.publish(work)
    return rejected.value


async def test_fabricated_pending_assessment_is_rejected_as_attempt_missing() -> None:
    request = _request()
    error = await _publish_rejection(_probe(request, _plan(request)), _publish_facts())

    assert error.reason_code == PUBLISH_REJECTION_RESTORATION_ATTEMPT_MISSING
    assert "先发 Probe 再补评估" in str(error)


async def test_divergent_attempt_is_rejected_as_not_equivalent() -> None:
    request = _request()
    plan = _plan(request)
    work = _probe(request, plan)
    facts = _equivalent_attempt_facts(work, plan)
    facts.update(attempt_status="divergent", attempt_passed="false")

    error = await _publish_rejection(work, facts)

    assert error.reason_code == PUBLISH_REJECTION_RESTORATION_NOT_EQUIVALENT
    assert "divergent" in str(error)


async def test_proof_field_drift_is_rejected_with_named_fields() -> None:
    request = _request()
    plan = _plan(request)
    work = _probe(request, plan)
    facts = _equivalent_attempt_facts(work, plan)
    facts.update(attempt_assessment_id="state-equivalence:other", attempt_source_grade="B")

    error = await _publish_rejection(work, facts)

    assert error.reason_code == PUBLISH_REJECTION_EQUIVALENCE_PROOF_MISMATCH
    assert "等价评估 ID" in str(error)
    assert "来源等级" in str(error)


async def test_attempt_of_other_request_is_rejected_as_proof_mismatch() -> None:
    request = _request()
    plan = _plan(request)
    work = _probe(request, plan)
    facts = _equivalent_attempt_facts(work, plan)
    facts.update(attempt_request_id="request-other")

    error = await _publish_rejection(work, facts)

    assert error.reason_code == PUBLISH_REJECTION_EQUIVALENCE_PROOF_MISMATCH
    assert "其他请求" in str(error)


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"task_exists": False, "task_status": None}, PUBLISH_REJECTION_TASK_NOT_RUNNABLE),
        ({"task_status": "cancelled"}, PUBLISH_REJECTION_TASK_NOT_RUNNABLE),
        # 非 H5 的 snapshot 依赖在排空阶段仍不受理。
        (
            {"task_finalization_state": "session_draining", "task_target_platform": "web"},
            PUBLISH_REJECTION_TASK_NOT_RUNNABLE,
        ),
        ({"capture_exists": False}, PUBLISH_REJECTION_SOURCE_CAPTURE_INVALID),
        ({"capture_status": "failed"}, PUBLISH_REJECTION_SOURCE_CAPTURE_INVALID),
        ({"request_exists": False}, PUBLISH_REJECTION_EVIDENCE_REQUEST_INVALID),
        ({"request_status": "unfulfillable"}, PUBLISH_REJECTION_EVIDENCE_REQUEST_INVALID),
    ],
)
async def test_publish_preconditions_map_to_stable_reason_codes(
    overrides: dict[str, Any],
    reason_code: str,
) -> None:
    request = _request()
    error = await _publish_rejection(
        _probe(request, _plan(request)),
        _publish_facts(**overrides),
    )

    assert error.reason_code == reason_code


@pytest.mark.parametrize(
    "dependency",
    [
        EvidenceSessionDependency.ACTIVE_SESSION,
        EvidenceSessionDependency.SNAPSHOT,
    ],
)
async def test_h5_probe_accepts_review_session_draining(dependency) -> None:
    request = _request(session_dependency=dependency)
    plan = _plan(request)
    work = _probe(request, plan)
    facts = _equivalent_attempt_facts(work, plan)
    facts.update(
        task_status="review",
        task_target_platform="h5",
        task_finalization_state="session_draining",
    )

    error = await _publish_rejection(work, facts)

    assert error.reason_code is None


@pytest.mark.parametrize("target_platform", ["web", "app"])
@pytest.mark.parametrize(
    "dependency",
    [
        EvidenceSessionDependency.ACTIVE_SESSION,
        EvidenceSessionDependency.SNAPSHOT,
    ],
)
async def test_non_h5_probe_rejects_review_session_draining(
    target_platform: str,
    dependency,
) -> None:
    request = _request(session_dependency=dependency)
    plan = _plan(request)
    work = _probe(request, plan)
    facts = _equivalent_attempt_facts(work, plan)
    facts.update(
        task_status="review",
        task_target_platform=target_platform,
        task_finalization_state="session_draining",
    )

    error = await _publish_rejection(work, facts)

    assert error.reason_code == PUBLISH_REJECTION_TASK_NOT_RUNNABLE


async def test_existing_work_with_other_payload_is_identity_drift() -> None:
    request = _request()
    plan = _plan(request)
    work = _probe(request, plan)
    facts = _equivalent_attempt_facts(work, plan)
    facts.update(
        work_exists=True,
        existing_work_type="probe",
        existing_payload_hash="payload-hash-of-another-version",
        existing_attempt_id=work.restoration_attempt_id,
        existing_assessment_id=work.equivalence_assessment_id,
    )

    error = await _publish_rejection(work, facts)

    assert error.reason_code == PUBLISH_REJECTION_WORK_IDENTITY_DRIFT


async def test_publish_rejection_without_diagnosis_keeps_generic_message() -> None:
    request = _request()
    error = await _publish_rejection(_probe(request, _plan(request)), None)

    assert error.reason_code is None
    assert "归属无效" in str(error)


def _start_facts(**overrides: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "request_exists": True,
        "request_task_id": TASK_ID,
        "request_target_capture_id": ORIGINAL_CAPTURE_ID,
        "request_status": "pending",
        "request_session_dependency": "snapshot",
        "request_expired": False,
        "plan_deadline_beyond_request": False,
        "request_epoch": 0,
        "task_exists": True,
        "task_status": "running",
        "task_target_platform": "h5",
        "task_finalization_state": "open",
        "task_epoch": 0,
        "capture_exists": True,
        "capture_task_id": TASK_ID,
        "capture_status": "captured",
        "capture_captured": True,
        "replay_plan_exists": False,
        "replay_plan_task_id": None,
        "attempt_exists": False,
        "existing_plan_id": None,
        "existing_plan_hash": None,
        "existing_request_id": None,
        "existing_attempt_no": None,
    }
    facts.update(overrides)
    return facts


async def _start_rejection(
    plan: RestorationPlan,
    facts: dict[str, Any] | None,
) -> RestorationAttemptPersistenceError:
    # fetchrow 依次是：恢复尝试 INSERT（拒）→ 只读诊断
    results: list[Any] = [None]
    if facts is not None:
        results.append(facts)
    repository = PostgresRestorationAttemptRepository(
        pool=FakePool(FakeConnection(fetchrow_results=results))
    )
    with pytest.raises(RestorationAttemptPersistenceError) as rejected:
        await repository.start(plan=plan, attempt_no=1)
    return rejected.value


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"request_exists": False}, ATTEMPT_REJECTION_EVIDENCE_REQUEST_MISSING),
        ({"request_task_id": "other-task"}, ATTEMPT_REJECTION_EVIDENCE_REQUEST_INVALID),
        ({"request_status": "satisfied"}, ATTEMPT_REJECTION_EVIDENCE_REQUEST_INVALID),
        ({"request_expired": True}, ATTEMPT_REJECTION_EVIDENCE_REQUEST_EXPIRED),
        ({"plan_deadline_beyond_request": True}, ATTEMPT_REJECTION_EVIDENCE_REQUEST_EXPIRED),
        ({"task_status": "finished"}, ATTEMPT_REJECTION_TASK_NOT_RUNNABLE),
        ({"task_epoch": 1}, ATTEMPT_REJECTION_TASK_NOT_RUNNABLE),
        ({"capture_captured": False}, ATTEMPT_REJECTION_ORIGINAL_CAPTURE_INVALID),
        ({"capture_task_id": "other-task"}, ATTEMPT_REJECTION_ORIGINAL_CAPTURE_INVALID),
        (
            {
                "attempt_exists": True,
                "existing_plan_id": "restoration-plan:other",
                "existing_plan_hash": "other",
                "existing_attempt_no": 1,
            },
            ATTEMPT_REJECTION_ATTEMPT_IDENTITY_DRIFT,
        ),
    ],
)
async def test_start_preconditions_map_to_stable_reason_codes(
    overrides: dict[str, Any],
    reason_code: str,
) -> None:
    request = _request()
    facts = _start_facts(**overrides)
    if facts["attempt_exists"] and facts["existing_request_id"] is None:
        facts["existing_request_id"] = request.request_id

    error = await _start_rejection(_plan(request), facts)

    assert error.reason_code == reason_code


async def test_start_reports_missing_replay_plan_for_route_strategies() -> None:
    request = _request()
    plan = _plan(
        request,
        strategy=RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
        replay_execution_plan_id="page-graph-route:route-1",
    )

    error = await _start_rejection(plan, _start_facts())

    assert error.reason_code == ATTEMPT_REJECTION_REPLAY_PLAN_MISSING
    assert "page-graph-route:route-1" in str(error)


async def test_h5_active_session_restoration_accepts_review_session_draining() -> None:
    request = _request(session_dependency=EvidenceSessionDependency.ACTIVE_SESSION)
    facts = _start_facts(
        request_session_dependency="active_session",
        task_status="review",
        task_target_platform="h5",
        task_finalization_state="session_draining",
    )

    error = await _start_rejection(_plan(request), facts)

    assert error.reason_code is None


@pytest.mark.parametrize("target_platform", ["web", "app"])
async def test_non_h5_restoration_rejects_review_session_draining(
    target_platform: str,
) -> None:
    request = _request(session_dependency=EvidenceSessionDependency.ACTIVE_SESSION)
    facts = _start_facts(
        request_session_dependency="active_session",
        task_status="review",
        task_target_platform=target_platform,
        task_finalization_state="session_draining",
    )

    error = await _start_rejection(_plan(request), facts)

    assert error.reason_code == ATTEMPT_REJECTION_TASK_NOT_RUNNABLE


async def test_start_rejection_without_diagnosis_keeps_generic_message() -> None:
    request = _request()
    error = await _start_rejection(_plan(request), None)

    assert error.reason_code is None
    assert "归属无效" in str(error)
