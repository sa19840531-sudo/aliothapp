"""Probe 后置：RestorationCoordinator + StateEquivalenceGate 完整挂回闭环。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from services.api.app.application.app_restoration_executor import (
    AppRestorationExecutor,
    probe_result_to_snapshot,
    snapshot_from_target_invariants,
)
from services.api.app.application.evidence_requests import (
    EvidenceRequestStatus,
    StoredEvidenceRequest,
    TransitionEvidenceRequestCommand,
)
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
    TrustedTaskCommandScopeReader,
)
from services.api.app.application.restoration import (
    RestorationAttemptRepository,
    RestorationCoordinator,
    new_running_restoration_attempt,
)
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.domain.inspection import (
    EvidenceRequest,
    RestorationAttempt,
    RestorationAttemptStatus,
    RestorationPlan,
    RestorationStrategy,
    build_restoration_plan,
)
from services.api.app.domain.interaction import SurfaceKind, SurfaceSessionRef
from services.api.app.domain.interaction.surface_driver import app_primary_surface_ref
from services.api.app.infrastructure.db.command_scope_reader import (
    PostgresTaskCommandScopeReader,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.evidence_request_repository import (
    PostgresEvidenceRequestRepository,
)

logger = logging.getLogger(__name__)


@dataclass
class _MemoryRestorationAttemptRepository:
    """单测 / 无库时的内存恢复尝试仓储。"""

    attempts: dict[str, RestorationAttempt]

    def __init__(self) -> None:
        self.attempts = {}

    async def start(self, *, plan: RestorationPlan, attempt_no: int) -> RestorationAttempt:
        attempt = new_running_restoration_attempt(
            plan=plan,
            attempt_no=attempt_no,
            started_at=datetime.now(UTC),
        )
        self.attempts[attempt.attempt_id] = attempt
        return attempt

    async def complete(
        self,
        *,
        attempt: RestorationAttempt,
        expected_row_version: int,
    ) -> RestorationAttempt:
        del expected_row_version
        finished = replace(attempt, row_version=attempt.row_version + 1)
        self.attempts[finished.attempt_id] = finished
        return finished


async def finalize_probe_equivalence_hangback(
    *,
    task_id: str,
    session_id: str,
    outcome: Any,
    runtime: Any | None = None,
    request_store: Any | None = None,
    manifest_store: Any | None = None,
    restoration_repository: RestorationAttemptRepository | None = None,
    package_name: str | None = None,
    activity: str | None = None,
    deep_link: str | None = None,
    surface_kind: SurfaceKind = SurfaceKind.ANDROID,
    command_scope_reader: TrustedTaskCommandScopeReader | None = None,
) -> None:
    """Probe 成功后：恢复策略执行 + 状态等价门禁，再推进 Evidence Request。"""
    try:
        if not getattr(outcome, "claimed", False):
            return
        if str(getattr(outcome, "work_type", "") or "").casefold() != "probe":
            return
        request_id = getattr(outcome, "evidence_request_id", None)
        result_capture_id = getattr(outcome, "result_capture_id", None)
        if not request_id or not result_capture_id:
            return

        # 正式 Runtime 的恢复必须由组合根提供统一 Coordinator。不能把
        # runtime 上缺失的属性降级成 None，再让恢复器直驱真实设备。
        restoration_coordinator = _resolve_formal_restoration_coordinator(runtime)
        if runtime is not None and restoration_coordinator is None:
            logger.warning(
                "task_id=%s evidence_request=%s 缺少正式 App 恢复 Coordinator，拒绝恢复动作",
                task_id,
                request_id,
            )
            return

        if request_store is None:
            request_store = PostgresEvidenceRequestRepository()
        if manifest_store is None:
            manifest_store = PostgresEvidenceManifestRepository()

        stored = await request_store.get(request_id=str(request_id))
        if stored is None:
            return
        stored = await _ensure_running(request_store=request_store, stored=stored)
        if stored is None or stored.status is not EvidenceRequestStatus.RUNNING:
            return

        pass_manifest = await manifest_store.get_latest_for_capture(
            task_id=task_id,
            capture_id=str(result_capture_id),
            require_quality_pass=True,
        )
        if pass_manifest is None:
            return

        request = stored.request
        plan = _rebuild_plan(
            request=request,
            source_session_id=session_id,
            surface_kind=surface_kind,
            outcome=outcome,
        )
        original = snapshot_from_target_invariants(
            request=request,
            session_id=plan.source_session_id,
            surface_kind=surface_kind,
            captured_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        # 恢复快照只能来自 Probe／独立观察字段，禁止从 target_invariants 填空或 merge
        # （否则等价门禁可用目标证明目标）。
        probe_snapshot = probe_result_to_snapshot(
            task_id=task_id,
            session_id=session_id,
            capture_id=str(result_capture_id),
            surface_kind=surface_kind,
            package_name=package_name,
            activity=activity,
            screen_id=_outcome_text(outcome, "screen_id"),
            structure_signature=_outcome_text(outcome, "structure_signature"),
            image_signature=_outcome_text(outcome, "image_signature", "state_version"),
            deep_link=deep_link,
            page_state=_outcome_text(outcome, "page_state"),
            lifecycle_phase=_outcome_text(outcome, "lifecycle_phase"),
            session_event=_outcome_text(outcome, "session_event"),
            captured_at=datetime.now(UTC),
        )

        driver = getattr(runtime, "interaction_driver", None) if runtime is not None else None
        session_ref = _resolve_session_ref(
            runtime=runtime,
            task_id=task_id,
            session_id=session_id,
            surface_kind=surface_kind,
        )
        # session_options／Driver 目标仍可读 target（恢复意图），与 restored 观察分离。
        session_options = {
            "platform_build_ref": _invariant_text(request, "environment", "platform_build_ref")
            or getattr(runtime, "platform_build_ref", None),
            "device_ref": _invariant_text(request, "environment", "device_ref")
            or getattr(runtime, "device_ref", None),
            "account_environment_ref": _invariant_text(
                request, "environment", "account_environment_ref"
            ),
            "language": _invariant_text(request, "environment", "language"),
            "region": _invariant_text(request, "environment", "region"),
            "experiment": _invariant_text(request, "environment", "experiment"),
            "lifecycle_phase": _invariant_text(request, "environment", "lifecycle_phase"),
            "session_event": _invariant_text(request, "environment", "session_event"),
            "deep_link": deep_link or _invariant_text(request, "environment", "deep_link"),
            "safe_area": (request.target_invariants or {}).get("structure", {}).get("safe_area")
            if isinstance((request.target_invariants or {}).get("structure"), dict)
            else None,
            "app_data_container_ref": _invariant_text(
                request, "environment", "app_data_container_ref"
            ),
        }
        executor = AppRestorationExecutor(
            driver=driver,
            session=session_ref,
            package_name=package_name
            or _invariant_text(request, "business", "package_name"),
            activity=activity or _invariant_text(request, "business", "activity"),
            deep_link=deep_link or _invariant_text(request, "environment", "deep_link"),
            probe_result_snapshot=probe_snapshot,
            platform_build_ref=session_options.get("platform_build_ref"),
            device_ref=session_options.get("device_ref"),
            account_environment_ref=session_options.get("account_environment_ref"),
            session_options=session_options,
            command_validator=InteractionCommandValidator(
                scope_reader=(command_scope_reader or PostgresTaskCommandScopeReader())
            ),
            capture_coordinator=restoration_coordinator,
            restored_snapshot_reader=(
                getattr(runtime, "restoration_snapshot_reader", None)
                if runtime is not None
                else None
            ),
            surface_sessions=(
                getattr(runtime, "surface_sessions", None)
                if runtime is not None
                else None
            ),
        )
        if restoration_repository is None:
            try:
                from services.api.app.infrastructure.db.restoration_attempt_repository import (
                    PostgresRestorationAttemptRepository,
                )

                restoration_repository = PostgresRestorationAttemptRepository()
            except Exception:  # noqa: BLE001
                restoration_repository = _MemoryRestorationAttemptRepository()

        coordinator = RestorationCoordinator(
            repository=restoration_repository,
            executor=executor,
            equivalence_gate=StateEquivalenceGate(),
        )
        attempt = await coordinator.restore(
            request=request,
            plan=plan,
            attempt_no=1,
            original_snapshot=original,
        )
        await _apply_request_terminal(
            request_store=request_store,
            stored=stored,
            attempt=attempt,
            pass_manifest_id=pass_manifest.manifest_id,
        )
    except Exception:  # noqa: BLE001 - Probe 挂回失败不得阻断下一轮
        logger.warning(
            "task_id=%s evidence_request Probe 恢复/等价挂回失败",
            task_id,
            exc_info=True,
        )


async def _ensure_running(
    *,
    request_store: Any,
    stored: StoredEvidenceRequest,
) -> StoredEvidenceRequest | None:
    if stored.status is EvidenceRequestStatus.ROUTED:
        await request_store.transition(
            command=TransitionEvidenceRequestCommand(
                request_id=stored.request.request_id,
                expected_status=EvidenceRequestStatus.ROUTED,
                target_status=EvidenceRequestStatus.RUNNING,
                row_version=stored.row_version,
            )
        )
        return await request_store.get(request_id=stored.request.request_id)
    return stored


def _rebuild_plan(
    *,
    request: EvidenceRequest,
    source_session_id: str,
    surface_kind: SurfaceKind,
    outcome: Any,
) -> RestorationPlan:
    routed_plan = getattr(outcome, "restoration_plan", None)
    if routed_plan is not None:
        if not isinstance(routed_plan, RestorationPlan):
            raise ValueError("Probe 挂回携带的 RestorationPlan 类型无效")
        if (
            routed_plan.request_id != request.request_id
            or routed_plan.task_id != request.task_id
            or routed_plan.source_session_id != source_session_id
            or routed_plan.surface_kind is not surface_kind
        ):
            raise ValueError("Probe 挂回携带的 RestorationPlan 与请求上下文不一致")
        return routed_plan
    from services.api.app.application.evidence_request_routing import (
        select_restoration_strategy,
    )

    strategy = select_restoration_strategy(request=request, surface_kind=surface_kind)
    target_session_id = source_session_id
    if strategy is RestorationStrategy.NEW_SESSION_REPLAY:
        target_session_id = f"session-replay:{request.request_id}"
    requires_replay = strategy in {
        RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
        RestorationStrategy.SAME_APP_SESSION,
        RestorationStrategy.NEW_SESSION_REPLAY,
    }
    deadline = min(request.expires_at, datetime.now(UTC) + timedelta(minutes=10))
    # 若 Probe 工作项已带 plan hash，仍以确定性重建保证身份一致
    del outcome
    return build_restoration_plan(
        request=request,
        source_session_id=source_session_id,
        target_session_id=target_session_id,
        surface_kind=surface_kind,
        strategy=strategy,
        deadline=deadline,
        replay_execution_plan_id=(
            f"replay-plan:{request.request_id}" if requires_replay else None
        ),
    )


def _outcome_text(outcome: Any, *names: str) -> str | None:
    """只读 Probe 结果上的独立观察字段，不回退到 target_invariants。"""
    for name in names:
        value = getattr(outcome, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _apply_request_terminal(
    *,
    request_store: Any,
    stored: StoredEvidenceRequest,
    attempt: RestorationAttempt,
    pass_manifest_id: str,
) -> None:
    if attempt.status is RestorationAttemptStatus.EQUIVALENT:
        await request_store.transition(
            command=TransitionEvidenceRequestCommand(
                request_id=stored.request.request_id,
                expected_status=EvidenceRequestStatus.RUNNING,
                target_status=EvidenceRequestStatus.SATISFIED,
                row_version=stored.row_version,
                resolution_manifest_id=pass_manifest_id,
                resolution_reason=(
                    f"restoration_equivalent:{attempt.attempt_id}:"
                    f"{attempt.assessment.assessment_id if attempt.assessment else 'none'}"
                ),
            )
        )
        return
    if attempt.status is RestorationAttemptStatus.UNFULFILLABLE:
        await request_store.transition(
            command=TransitionEvidenceRequestCommand(
                request_id=stored.request.request_id,
                expected_status=EvidenceRequestStatus.RUNNING,
                target_status=EvidenceRequestStatus.UNFULFILLABLE,
                row_version=stored.row_version,
                resolution_reason=f"restoration_unfulfillable:{attempt.error_code}",
            )
        )
        return
    # DIVERGENT / FAILED：保留 RUNNING，等待后续重试或人工；写清原因
    logger.info(
        "evidence_request=%s 恢复未等价 status=%s error=%s",
        stored.request.request_id,
        attempt.status.value,
        attempt.error_code,
    )


def _resolve_session_ref(
    *,
    runtime: Any | None,
    task_id: str,
    session_id: str,
    surface_kind: SurfaceKind,
) -> SurfaceSessionRef | None:
    if runtime is None:
        return None
    return SurfaceSessionRef(
        task_id=task_id,
        session_id=session_id,
        surface_kind=surface_kind,
        primary_surface_ref=app_primary_surface_ref(session_id),
    )


def _resolve_formal_restoration_coordinator(
    runtime: Any | None,
) -> Any | None:
    """读取并校验正式 Runtime 的恢复 Coordinator；缺失时不允许直驱。"""

    if runtime is None:
        return None
    require = getattr(runtime, "require_restoration_coordinator", None)
    if callable(require):
        try:
            coordinator = require()
        except Exception:  # noqa: BLE001 - 入口统一按缺失处理
            return None
    else:
        coordinator = getattr(runtime, "restoration_coordinator", None)
    if not callable(getattr(coordinator, "coordinate", None)):
        return None
    return coordinator


def _invariant_text(request: EvidenceRequest, dimension: str, key: str) -> str | None:
    block = request.target_invariants.get(dimension)
    if not isinstance(block, dict):
        return None
    value = block.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
