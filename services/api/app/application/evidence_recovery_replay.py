"""按页面图可信边逐步恢复目标状态的应用层编排。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from services.api.app.application.page_graph_contracts import PageGraphRepository
from services.api.app.domain.inspection import RestorationPlan, RestorationStrategy
from services.api.app.domain.interaction import SurfaceKind


class EvidenceRecoveryReplayError(RuntimeError):
    """恢复路线不能安全继续时返回的结构化错误。"""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        transition_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.transition_id = transition_id
        self.retryable = False


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryObservation:
    """运行面独立采集的当前页面状态，不复用历史 Capture 作为现实状态。"""

    capture_id: str
    page_identity: str
    state_fingerprint: str
    observed_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("capture_id", "page_identity", "state_fingerprint"):
            _required_text(getattr(self, field_name), field_name)
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at 必须包含时区")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryGraphStep:
    """从权威页面图读取、等待运行面重新证明的一条历史动作边。"""

    transition_id: str
    source_state_id: str
    target_state_id: str
    action_kind: str
    semantic_target: str | None
    source_identity: str
    source_state_fingerprint: str
    target_identity: str
    target_state_fingerprint: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryStepReceipt:
    """统一 Interaction/Capture 入口执行一条恢复边后的真实回执。"""

    transition_id: str
    action_execution_id: str
    after: RecoveryObservation

    def __post_init__(self) -> None:
        _required_text(self.transition_id, "transition_id")
        _required_text(self.action_execution_id, "action_execution_id")
        if not isinstance(self.after, RecoveryObservation):
            raise TypeError("after 必须为 RecoveryObservation")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceRecoveryReplayOutcome:
    """完整路线执行成功后的引用型结果。"""

    arrived: bool
    result_capture_id: str
    action_execution_ids: tuple[str, ...]
    transition_ids: tuple[str, ...]


class EvidenceRecoveryRuntime(Protocol):
    """由现有 SurfaceDriver、CaptureCoordinator 适配的恢复运行面。"""

    async def observe(
        self,
        *,
        task_id: str,
        session_id: str,
        plan: RestorationPlan,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> RecoveryObservation: ...

    async def execute(
        self,
        *,
        task_id: str,
        session_id: str,
        plan: RestorationPlan,
        agent_run_id: str,
        finalization_epoch: int,
        step: RecoveryGraphStep,
        source: RecoveryObservation,
    ) -> RecoveryStepReceipt: ...


class EvidenceRecoveryReplayExecutor:
    """重新观察起点，并按计划顺序逐边执行和验证到达。"""

    def __init__(
        self,
        *,
        repository: PageGraphRepository,
        runtime: EvidenceRecoveryRuntime,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._now = now or (lambda: datetime.now(UTC))

    async def execute(
        self,
        *,
        plan: RestorationPlan,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> EvidenceRecoveryReplayOutcome:
        self._validate_request(
            plan=plan,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        # 当前目标就地 pin：只重新观察补帧，不进入页面图逐边重放。
        if plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN:
            return await self._observe_original_surface(
                plan=plan,
                agent_run_id=agent_run_id,
                finalization_epoch=finalization_epoch,
            )
        graph = await self._repository.get_graph(
            task_id=plan.task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        steps = _load_plan_steps(
            graph,
            plan=plan,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        allow_h5_identity_rebind = (
            graph.get("target_platform") == "h5"
            and plan.surface_kind is SurfaceKind.WEB
        )
        current = await self._runtime.observe(
            task_id=plan.task_id,
            session_id=plan.target_session_id,
            plan=plan,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        _require_observation_matches(
            current,
            identity=steps[0].source_identity,
            state_fingerprint=steps[0].source_state_fingerprint,
            reason_code="RECOVERY_SOURCE_STATE_DRIFTED",
            transition_id=steps[0].transition_id,
            allow_fingerprint_drift=allow_h5_identity_rebind,
        )

        action_execution_ids: list[str] = []
        for step in steps:
            if self._now() >= plan.deadline:
                raise EvidenceRecoveryReplayError(
                    reason_code="RECOVERY_DEADLINE_EXHAUSTED",
                    message="恢复计划已到截止时间，停止执行后续页面图边",
                    transition_id=step.transition_id,
                )
            _require_observation_matches(
                current,
                identity=step.source_identity,
                state_fingerprint=step.source_state_fingerprint,
                reason_code="RECOVERY_SOURCE_STATE_DRIFTED",
                transition_id=step.transition_id,
                allow_fingerprint_drift=allow_h5_identity_rebind,
            )
            receipt = await self._runtime.execute(
                task_id=plan.task_id,
                session_id=plan.target_session_id,
                plan=plan,
                agent_run_id=agent_run_id,
                finalization_epoch=finalization_epoch,
                step=step,
                source=current,
            )
            if receipt.transition_id != step.transition_id:
                raise EvidenceRecoveryReplayError(
                    reason_code="RECOVERY_RECEIPT_IDENTITY_MISMATCH",
                    message="恢复动作回执与当前页面图边不一致",
                    transition_id=step.transition_id,
                )
            _require_observation_matches(
                receipt.after,
                identity=step.target_identity,
                state_fingerprint=step.target_state_fingerprint,
                reason_code="RECOVERY_STEP_ARRIVAL_DIVERGED",
                transition_id=step.transition_id,
                allow_fingerprint_drift=allow_h5_identity_rebind,
            )
            action_execution_ids.append(receipt.action_execution_id)
            current = receipt.after

        return EvidenceRecoveryReplayOutcome(
            arrived=True,
            result_capture_id=current.capture_id,
            action_execution_ids=tuple(action_execution_ids),
            transition_ids=tuple(step.transition_id for step in steps),
        )

    async def _observe_original_surface(
        self,
        *,
        plan: RestorationPlan,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> EvidenceRecoveryReplayOutcome:
        """原 Surface Pin 只重新观察当前面，不伪造页面图动作边。"""

        current = await self._runtime.observe(
            task_id=plan.task_id,
            session_id=plan.target_session_id,
            plan=plan,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        return EvidenceRecoveryReplayOutcome(
            arrived=True,
            result_capture_id=current.capture_id,
            action_execution_ids=(),
            transition_ids=(),
        )

    def _validate_request(
        self,
        *,
        plan: RestorationPlan,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> None:
        if not isinstance(plan, RestorationPlan):
            raise TypeError("plan 必须为 RestorationPlan")
        _required_text(agent_run_id, "agent_run_id")
        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        if self._now() >= plan.deadline:
            raise EvidenceRecoveryReplayError(
                reason_code="RECOVERY_DEADLINE_EXHAUSTED",
                message="恢复计划已到截止时间",
            )
        if plan.strategy is RestorationStrategy.ORIGINAL_SURFACE_PIN:
            if plan.source_session_id != plan.target_session_id:
                raise _invalid_route("原 Surface Pin 不能切换 Session")
            if plan.replay_execution_plan_id is not None or plan.normalization_step_ids:
                raise _invalid_route("原 Surface Pin 不得携带页面图恢复路线")
            return
        if plan.strategy not in {
            RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
            RestorationStrategy.SAME_APP_SESSION,
            RestorationStrategy.NEW_SESSION_REPLAY,
        }:
            raise EvidenceRecoveryReplayError(
                reason_code="RECOVERY_ROUTE_NOT_APPLICABLE",
                message="当前恢复策略不使用页面图逐边重放",
            )
        if not plan.normalization_step_ids:
            raise EvidenceRecoveryReplayError(
                reason_code="RECOVERY_ROUTE_INVALID",
                message="页面图恢复计划缺少动作边",
            )


def _load_plan_steps(
    graph: Mapping[str, Any],
    *,
    plan: RestorationPlan,
    agent_run_id: str,
    finalization_epoch: int,
) -> tuple[RecoveryGraphStep, ...]:
    expected_scope = {
        "task_id": plan.task_id,
        "agent_run_id": agent_run_id,
        "finalization_epoch": finalization_epoch,
    }
    if graph.get("status") != "available" or any(
        graph.get(field_name) != value for field_name, value in expected_scope.items()
    ):
        raise _invalid_route("页面图与恢复计划执行域不一致")
    states = _indexed_rows(graph.get("states"), key="state_id", field_name="states")
    transitions = _indexed_rows(
        graph.get("transitions"),
        key="transition_id",
        field_name="transitions",
    )
    steps: list[RecoveryGraphStep] = []
    prior_target_id: str | None = None
    for transition_id in plan.normalization_step_ids:
        edge = transitions.get(transition_id)
        if edge is None:
            raise _invalid_route("恢复计划引用的页面图边不存在")
        source_state_id = _required_graph_text(edge.get("source_state_id"), "source_state_id")
        target_state_id = _required_graph_text(edge.get("target_state_id"), "target_state_id")
        source = states.get(source_state_id)
        target = states.get(target_state_id)
        if source is None or target is None:
            raise _invalid_route("页面图边缺少源状态或目标状态")
        if prior_target_id is not None and source_state_id != prior_target_id:
            raise _invalid_route("恢复计划中的页面图边不连续")
        _require_trusted_edge(
            edge,
            source=source,
            target=target,
            plan=plan,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
        )
        steps.append(
            RecoveryGraphStep(
                transition_id=transition_id,
                source_state_id=source_state_id,
                target_state_id=target_state_id,
                action_kind=_required_graph_text(edge.get("action_kind"), "action_kind"),
                semantic_target=_optional_text(edge.get("semantic_target")),
                source_identity=_required_graph_text(
                    source.get("logical_page_key"), "source.logical_page_key"
                ),
                source_state_fingerprint=_required_graph_text(
                    source.get("state_fingerprint"), "source.state_fingerprint"
                ),
                target_identity=_required_graph_text(
                    target.get("logical_page_key"), "target.logical_page_key"
                ),
                target_state_fingerprint=_required_graph_text(
                    target.get("state_fingerprint"), "target.state_fingerprint"
                ),
            )
        )
        prior_target_id = target_state_id
    if not steps or steps[-1].target_state_id != plan.original_capture_id:
        raise _invalid_route("恢复计划没有到达 Evidence Request 的目标 Capture")
    return tuple(steps)


def _require_trusted_edge(
    edge: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    plan: RestorationPlan,
    agent_run_id: str,
    finalization_epoch: int,
) -> None:
    expected_scope = {
        "task_id": plan.task_id,
        "agent_run_id": agent_run_id,
        "surface_session_id": plan.source_session_id,
        "surface_kind": plan.surface_kind.value,
        "finalization_epoch": finalization_epoch,
    }
    for row in (edge, source, target):
        if any(row.get(field_name) != value for field_name, value in expected_scope.items()):
            raise _invalid_route("页面图边或状态越出恢复计划执行域")
    if (
        edge.get("status") != "completed"
        or edge.get("proof") != "trusted"
        or edge.get("risk_level") != "low"
        or edge.get("replay_eligible") is not True
    ):
        raise _invalid_route("页面图边不是可重放的可信低风险成功边")
    for state in (source, target):
        if (
            state.get("identity_status") != "confirmed"
            or state.get("counts_as_business_page") is not True
            or state.get("is_overlay") is True
            or state.get("is_external") is True
        ):
            raise _invalid_route("页面图恢复状态不是已确认业务页面")


def _indexed_rows(
    value: Any,
    *,
    key: str,
    field_name: str,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _invalid_route(f"页面图 {field_name} 格式无效")
    result: dict[str, Mapping[str, Any]] = {}
    for row in value:
        if not isinstance(row, Mapping):
            raise _invalid_route(f"页面图 {field_name} 包含无效记录")
        row_id = _required_graph_text(row.get(key), f"{field_name}.{key}")
        if row_id in result:
            raise _invalid_route(f"页面图 {field_name} 包含重复身份")
        result[row_id] = row
    return result


def _require_observation_matches(
    observation: RecoveryObservation,
    *,
    identity: str,
    state_fingerprint: str,
    reason_code: str,
    transition_id: str,
    allow_fingerprint_drift: bool = False,
) -> None:
    if not isinstance(observation, RecoveryObservation):
        raise TypeError("运行面观察结果必须为 RecoveryObservation")
    if observation.page_identity != identity or (
        not allow_fingerprint_drift
        and observation.state_fingerprint != state_fingerprint
    ):
        raise EvidenceRecoveryReplayError(
            reason_code=reason_code,
            message="运行面实际页面状态与页面图恢复边不一致",
            transition_id=transition_id,
        )


def _invalid_route(message: str) -> EvidenceRecoveryReplayError:
    return EvidenceRecoveryReplayError(
        reason_code="RECOVERY_ROUTE_INVALID",
        message=message,
    )


def _required_graph_text(value: Any, field_name: str) -> str:
    try:
        return _required_text(value, field_name)
    except (TypeError, ValueError) as exc:
        raise _invalid_route(str(exc)) from exc


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _required_graph_text(value, "semantic_target")
