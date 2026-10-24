"""Judge 产出 Evidence Request 后的公共受理与选路。

这里只做四件事：补采闸门、恢复策略、页面图选路、确定性不可补采的结算。**不发布
ProbeWork。** ProbeWork 只有一个合法产出点：``restored_probe_publication`` 在
RestorationAttempt 已完成且通过状态等价门禁之后，用真实的 attempt / assessment 身份
发布——数据库外键 ``fk_qc_v2_work_item_restoration_assessment`` 与
``ck_qc_v2_work_item_probe_proof`` 锁死了这一点，路由层伪造一个「待完成」的评估 ID
只会被拒。

受理通过的请求保持 ``pending``，由本端的恢复执行器领取：先恢复（就地观察或按路线
重放，产出新 Capture）、再等价评估并持久化、再发布 Probe（方案 03 §4「编排层不伪造
已满足条件」、§16「restored 必须来自独立实际状态」）。App 端参考
``app_targeted_recapture_consumer``；H5/Web 端的恢复执行端口由各自 Runtime 提供。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from services.api.app.application.evidence_recovery_route import (
    EvidenceRecoveryRoutePlan,
    EvidenceRecoveryRouteValidationError,
    plan_evidence_recovery_route,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceDecision,
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequest,
    EvidenceSessionDependency,
    InspectionHandoff,
    RestorationPlan,
    RestorationStrategy,
    RuleJudgeOutcome,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.pool import get_postgres_pool

logger = logging.getLogger(__name__)

# 本轮请求已可判定「补不回来」的原因码；其余路由失败（含 Page Graph 短暂异常）保持 pending 可重试。
_DETERMINISTIC_ROUTE_UNFULFILLABLE_REASON_CODES: frozenset[str] = frozenset(
    {
        "RECAPTURE_FORBIDDEN_BY_REQUEST",
        "ORIGINAL_SURFACE_NO_LONGER_CURRENT",
        "ORIGINAL_ATOMIC_RECAPTURE_FORBIDDEN",
        "TRUSTED_RECOVERY_ROUTE_UNAVAILABLE",
        "RECOVERY_ROUTE_TARGET_NOT_REACHED",
        "RECOVERY_ROUTE_NOT_REDRIVABLE",
        "EVIDENCE_REQUEST_EXPIRED",
    }
)


def is_deterministic_route_unfulfillable(reason_code: str | None) -> bool:
    """仅白名单原因码可落 unfulfillable；临时技术异常不得终态化。"""

    return (reason_code or "").strip() in _DETERMINISTIC_ROUTE_UNFULFILLABLE_REASON_CODES


@dataclass(frozen=True, slots=True)
class EvidenceRoutingResult:
    """Evidence Request 受理摘要，显式区分无请求、已规划、被策略抑制。

    ``planned_request_ids`` 是已通过闸门、规划出恢复计划、保持 pending 等待本端恢复
    执行器领取的请求。这里不会有 ProbeWork ID——Probe 只在恢复通过等价门禁后才存在。
    """

    requested_count: int
    planned_request_ids: tuple[str, ...] = ()
    suppressed_request_ids: tuple[str, ...] = ()
    gate_decision: SupplementalEvidenceDecision | None = None


async def route_outcome_evidence_requests(
    *,
    handoff: InspectionHandoff,
    outcome: RuleJudgeOutcome,
    request_store: Any | None = None,
    supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
    page_graph_service: Any | None = None,
) -> EvidenceRoutingResult:
    """受理 Outcome 中的 Evidence Request：规划恢复或写回确定性失败，不发布 Probe。"""
    if not outcome.evidence_requests:
        return EvidenceRoutingResult(requested_count=0)
    policy = (
        supplemental_evidence_policy
        if supplemental_evidence_policy is not None
        else SupplementalEvidencePolicy()
    )
    gate = policy.decide(SupplementalEvidenceOperation.ROUTE_REQUEST)
    if gate.denied:
        suppressed_request_ids = tuple(
            request.request_id for request in outcome.evidence_requests
        )
        logger.info(
            "task_id=%s work_item_id=%s 补采路由已按服务端策略抑制，"
            "request_ids=%s reason_code=%s policy_version=%s",
            outcome.task_id,
            outcome.work_item_id,
            ",".join(suppressed_request_ids),
            gate.reason_code,
            gate.policy_version,
        )
        return EvidenceRoutingResult(
            requested_count=len(outcome.evidence_requests),
            suppressed_request_ids=suppressed_request_ids,
            gate_decision=gate,
        )
    if request_store is None:
        try:
            from services.api.app.infrastructure.db.evidence_request_repository import (
                PostgresEvidenceRequestRepository,
            )

            request_store = PostgresEvidenceRequestRepository()
        except Exception:  # noqa: BLE001 - 无仓储时只规划、不写回失败态
            request_store = None
    planned: list[str] = []
    for request in outcome.evidence_requests:
        try:
            plan, reason_code = await _route_one(
                handoff=handoff,
                request=request,
                page_graph_service=page_graph_service,
            )
            if plan is not None:
                planned.append(request.request_id)
            elif request_store is not None:
                await _record_evidence_request_route_failure(
                    request_store=request_store,
                    request_id=request.request_id,
                    reason=reason_code or "restoration_route_unavailable",
                    deterministic_unfulfillable=is_deterministic_route_unfulfillable(
                        reason_code
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - 单条补采失败不得阻断规则写回
            logger.warning(
                "work_item_id=%s evidence_request=%s 受理补采请求失败",
                handoff.work_item_id,
                request.request_id,
                exc_info=True,
            )
            if request_store is not None:
                try:
                    await _record_evidence_request_route_failure(
                        request_store=request_store,
                        request_id=request.request_id,
                        reason=str(exc)[:500],
                        deterministic_unfulfillable=False,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "evidence_request=%s 失败态写回异常",
                        request.request_id,
                        exc_info=True,
                    )
    return EvidenceRoutingResult(
        requested_count=len(outcome.evidence_requests),
        planned_request_ids=tuple(planned),
        gate_decision=gate,
    )


async def _route_one(
    *,
    handoff: InspectionHandoff,
    request: EvidenceRequest,
    page_graph_service: Any | None = None,
) -> tuple[RestorationPlan | None, str | None]:
    """受理一条补采请求：给出恢复计划，或给出确定的原因码，二者必有其一。

    到「计划」为止。执行恢复、拍新 Capture、做等价评估、发布 Probe 都是恢复执行器的
    事，这里不碰设备也不碰浏览器，更不替它们伪造评估结果。
    """
    context = await _load_capture_context(
        task_id=handoff.task_id,
        capture_id=request.target_capture_id or handoff.evidence.page_capture_id,
        producer_agent_run_id=handoff.evidence.producer_agent_run_id,
    )
    if context is None:
        raise RuntimeError("Evidence Request 目标 Capture 不存在")
    surface_kind = context["surface_kind"]
    session_id = context["session_id"]
    strategy = _select_strategy(request=request, surface_kind=surface_kind)
    if strategy is RestorationStrategy.UNFULFILLABLE:
        logger.info(
            "evidence_request=%s 标记为不可补采（strategy=unfulfillable）",
            request.request_id,
        )
        return None, "RECAPTURE_FORBIDDEN_BY_REQUEST"
    target_capture_id = str(request.target_capture_id)
    current_capture_id = context.get("current_capture_id")
    route_id: str | None = None
    normalization_step_ids: tuple[str, ...] = ()
    if current_capture_id == target_capture_id:
        strategy = RestorationStrategy.ORIGINAL_SURFACE_PIN
    elif strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN:
        logger.info(
            "evidence_request=%s 原 Surface 已离开目标 Capture，禁止导航恢复",
            request.request_id,
        )
        return None, "ORIGINAL_SURFACE_NO_LONGER_CURRENT"
    else:
        plan_result = await _plan_page_graph_route(
            context=context,
            task_id=handoff.task_id,
            page_graph_service=page_graph_service,
        )
        if plan_result.route is None:
            logger.info(
                "evidence_request=%s 没能规划出可执行的补采路线：%s",
                request.request_id,
                plan_result.explain(),
            )
            return None, plan_result.reason_code
        route_id = plan_result.route.route_id
        normalization_step_ids = plan_result.route.transition_ids
    deadline = min(request.expires_at, datetime.now(UTC) + timedelta(minutes=10))
    requires_replay = strategy in {
        RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
        RestorationStrategy.SAME_APP_SESSION,
        RestorationStrategy.NEW_SESSION_REPLAY,
    }
    target_session_id = session_id
    if strategy is RestorationStrategy.NEW_SESSION_REPLAY:
        # 只声明新 Session 的目标身份；实际开 Session 由恢复执行器完成
        target_session_id = f"session-replay:{request.request_id}"
    plan = build_restoration_plan(
        request=request,
        source_session_id=session_id,
        target_session_id=target_session_id,
        surface_kind=surface_kind,
        strategy=strategy,
        deadline=deadline,
        replay_execution_plan_id=(
            route_id if requires_replay else None
        ),
        normalization_step_ids=normalization_step_ids,
    )
    logger.info(
        "evidence_request=%s 已受理：strategy=%s route=%s steps=%d，保持 pending 等待 %s 端"
        "恢复执行器领取（先恢复→等价评估→再发布 Probe）",
        request.request_id,
        strategy.value,
        route_id,
        len(normalization_step_ids),
        surface_kind.value,
    )
    return plan, None


def select_restoration_strategy(
    *,
    request: EvidenceRequest,
    surface_kind: SurfaceKind,
) -> RestorationStrategy:
    """按 Evidence Request 约束选择恢复策略（路由与 Probe 挂回共用）。"""
    from services.api.app.domain.inspection import EvidenceRequiredEquivalence

    if request.replay_policy is EvidenceReplayPolicy.FORBIDDEN:
        return RestorationStrategy.UNFULFILLABLE
    if request.required_equivalence is EvidenceRequiredEquivalence.ORIGINAL_ATOMIC:
        return RestorationStrategy.UNFULFILLABLE
    # App 会话事件优先：权限框保同会话；进程死亡走 warm/reset（SAME_APP_SESSION）
    if surface_kind is SurfaceKind.ANDROID:
        env = dict((request.target_invariants or {}).get("environment") or {})
        event = str(env.get("session_event") or "").strip()
        if event == "permission_dialog":
            return RestorationStrategy.SAME_APP_SESSION
        if event in {"process_death", "process_rebuild"}:
            if request.replay_policy is EvidenceReplayPolicy.NEW_SESSION_ALLOWED:
                return RestorationStrategy.NEW_SESSION_REPLAY
            return RestorationStrategy.SAME_APP_SESSION
    if (
        request.session_dependency is EvidenceSessionDependency.ACTIVE_SESSION
        or request.required_equivalence
        is EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION
        or request.replay_policy is EvidenceReplayPolicy.SAME_SURFACE_ONLY
    ):
        return RestorationStrategy.ORIGINAL_SURFACE_PIN
    if surface_kind is SurfaceKind.WEB:
        if request.replay_policy is EvidenceReplayPolicy.NEW_SESSION_ALLOWED:
            return RestorationStrategy.NEW_SESSION_REPLAY
        return RestorationStrategy.SAME_CONTEXT_NEW_SURFACE
    if request.replay_policy is EvidenceReplayPolicy.NEW_SESSION_ALLOWED:
        return RestorationStrategy.NEW_SESSION_REPLAY
    return RestorationStrategy.SAME_APP_SESSION


def _select_strategy(
    *,
    request: EvidenceRequest,
    surface_kind: SurfaceKind,
) -> RestorationStrategy:
    return select_restoration_strategy(request=request, surface_kind=surface_kind)


async def _plan_page_graph_route(
    *,
    context: dict[str, Any],
    task_id: str,
    page_graph_service: Any | None,
) -> EvidenceRecoveryRoutePlan:
    required = (
        "agent_run_id",
        "finalization_epoch",
        "current_capture_id",
        "target_state_id",
        "target_identity",
    )
    if any(context.get(field_name) in {None, ""} for field_name in required):
        return EvidenceRecoveryRoutePlan(
            route=None,
            reason_code="TARGET_GRAPH_STATE_UNAVAILABLE",
        )
    if page_graph_service is None:
        from services.api.app.application.page_graph import PageGraphService
        from services.api.app.infrastructure.db.page_graph_repository import (
            PostgresPageGraphRepository,
        )

        page_graph_service = PageGraphService(repository=PostgresPageGraphRepository())
    try:
        response = await page_graph_service.get_recovery_route(
            task_id=task_id,
            agent_run_id=context["agent_run_id"],
            finalization_epoch=context["finalization_epoch"],
            source_state_id=context["current_capture_id"],
            target_identity=context["target_identity"],
            limit=20,
        )
        return plan_evidence_recovery_route(
            response,
            task_id=task_id,
            agent_run_id=context["agent_run_id"],
            finalization_epoch=context["finalization_epoch"],
            source_state_id=context["current_capture_id"],
            target_state_id=context["target_state_id"],
            target_identity=context["target_identity"],
        )
    except EvidenceRecoveryRouteValidationError:
        logger.warning("页面图恢复路线合同校验失败", exc_info=True)
        return EvidenceRecoveryRoutePlan(
            route=None,
            reason_code="PAGE_GRAPH_ROUTE_CONTRACT_INVALID",
        )
    except Exception:  # noqa: BLE001 - Page Graph 短暂故障不得冒充确定性不可补采
        logger.warning("页面图恢复路线临时失败", exc_info=True)
        return EvidenceRecoveryRoutePlan(
            route=None,
            reason_code="PAGE_GRAPH_ROUTE_TRANSIENT_FAILURE",
        )


async def _load_capture_context(
    *,
    task_id: str,
    capture_id: str,
    producer_agent_run_id: str,
) -> dict[str, Any] | None:
    pool = await get_postgres_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT
                capture.surface_session_id AS session_id,
                capture.surface_kind::text AS surface_kind,
                capture.agent_run_id::text AS capture_agent_run_id,
                session.current_capture_id::text AS current_capture_id,
                state.state_id::text AS target_state_id,
                state.agent_run_id::text AS agent_run_id,
                state.finalization_epoch,
                state.logical_page_key AS target_identity
            FROM qc_capture AS capture
            LEFT JOIN qc_surface_session AS session
              ON session.task_id = capture.task_id
             AND session.session_id = capture.surface_session_id
            LEFT JOIN qc_page_graph_state AS state
              ON state.state_id = capture.id
             AND state.task_id = capture.task_id
             AND state.agent_run_id = capture.agent_run_id
             AND state.surface_session_id = capture.surface_session_id
             AND state.finalization_epoch IS NOT DISTINCT FROM session.finalization_epoch
            WHERE capture.id = $1::uuid
              AND capture.task_id = $2::uuid
              AND capture.capture_status = 'captured'
            """,
            capture_id,
            task_id,
        )
    if row is None or not row["session_id"]:
        return None
    if str(row["capture_agent_run_id"] or "") != producer_agent_run_id:
        return None
    try:
        surface = SurfaceKind(str(row["surface_kind"]))
    except ValueError:
        return None
    return {
        "session_id": str(row["session_id"]),
        "surface_kind": surface,
        "agent_run_id": (
            str(row["agent_run_id"]) if row["agent_run_id"] is not None else None
        ),
        "finalization_epoch": row["finalization_epoch"],
        "current_capture_id": (
            str(row["current_capture_id"]) if row["current_capture_id"] is not None else None
        ),
        "target_state_id": (
            str(row["target_state_id"]) if row["target_state_id"] is not None else None
        ),
        "target_identity": row["target_identity"],
    }


async def _record_evidence_request_route_failure(
    *,
    request_store: Any,
    request_id: str,
    reason: str,
    deterministic_unfulfillable: bool,
) -> None:
    """写回路由失败：确定性不可补采才进 unfulfillable；临时异常保持可重试。"""

    from services.api.app.application.evidence_requests import (
        EvidenceRequestStatus,
        TransitionEvidenceRequestCommand,
    )

    if not deterministic_unfulfillable:
        logger.info(
            "evidence_request=%s 路由临时失败，保持 pending 可重试 reason=%s",
            request_id,
            reason,
        )
        return
    try:
        stored = await request_store.get(request_id=request_id)
        if stored is None or stored.status.is_terminal:
            return
        await request_store.transition(
            command=TransitionEvidenceRequestCommand(
                request_id=request_id,
                expected_status=stored.status,
                target_status=EvidenceRequestStatus.UNFULFILLABLE,
                row_version=stored.row_version,
                resolution_reason=reason or "evidence_request_route_failed",
            )
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "evidence_request=%s 路由失败态写回失败",
            request_id,
            exc_info=True,
        )
