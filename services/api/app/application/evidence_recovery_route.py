"""从页面状态图规划补采路线：选风险较低、步骤较少且能逐步重新验证的那一条。

排期「根据页面路线规划补采路径」的规划侧。三条硬约束：

1. 只认同一 Task/Run/Epoch 的可信低风险成功边，合同漂移一律拒绝执行；
2. 只选**能按语义重新找到落点**的边——没有语义落点的边只能靠历史坐标重放，
   这里直接判为不可重跑，不把必然失败的路线发给执行端；
3. 选不出路线时给出确定的原因码和一句大白话，不退化成一个笼统的「不可补采」。

本模块只做选路和解释，不碰浏览器、设备和库。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from services.api.app.domain.protocol import stable_protocol_hash

# 执行端重新观察后能按语义重新绑定的动作；其余动作只能靠历史坐标，禁止入选。
RECOVERY_ROUTE_REDRIVABLE_ACTIONS: frozenset[str] = frozenset(
    {"click", "back", "observe", "scroll"}
)

# 这些动作必须带语义落点，否则重跑时只能按坐标点，视为不可重跑。
_SEMANTIC_TARGET_REQUIRED_ACTIONS: frozenset[str] = frozenset({"click"})

# 无语义落点、靠历史栈或滚动位置的动作：能走，但重跑容易走偏。
_POSITION_DEPENDENT_ACTIONS: frozenset[str] = frozenset({"back", "scroll"})


class EvidenceRecoveryRouteValidationError(ValueError):
    """页面图恢复路线越权、失真或不可执行。"""


class RouteRiskTier(StrEnum):
    """整条路线的风险档；取路线里最差的一步。"""

    STABLE = "stable"
    FRAGILE = "fragile"


class RouteUnavailableReason(StrEnum):
    """规划不出可执行路线的确定原因。"""

    # 沿用已经在补采台账里出现过的原因码，别让历史记录读不懂。
    GRAPH_NO_TRUSTED_ROUTE = "TRUSTED_RECOVERY_ROUTE_UNAVAILABLE"
    TARGET_NOT_REACHED = "RECOVERY_ROUTE_TARGET_NOT_REACHED"
    NOT_REDRIVABLE = "RECOVERY_ROUTE_NOT_REDRIVABLE"


_TIER_RANK: Mapping[RouteRiskTier, int] = {
    RouteRiskTier.STABLE: 0,
    RouteRiskTier.FRAGILE: 1,
}

TIER_LABELS: Mapping[RouteRiskTier, str] = {
    RouteRiskTier.STABLE: "每一步都能按语义重新找到",
    RouteRiskTier.FRAGILE: "有步骤需要按当前页面重新绑定，重跑可能走偏",
}

# 每条原因：给运营看的一句话 + 该怎么办。
REASON_EXPLANATIONS: Mapping[str, tuple[str, str]] = {
    RouteUnavailableReason.GRAPH_NO_TRUSTED_ROUTE.value: (
        "页面图里没有可信的低风险历史路线",
        "这一轮的页面跳转没留下可信转场证明（或页面身份没确认），回不到目标页面，"
        "要先把页面状态图的转场证明补上。",
    ),
    RouteUnavailableReason.TARGET_NOT_REACHED.value: (
        "有历史路线，但没有一条通到要补采的那个页面",
        "目标页面这一轮只从别的入口到过，换个起点或等探索再覆盖一次。",
    ),
    RouteUnavailableReason.NOT_REDRIVABLE.value: (
        "通到目标页面的路线里有步骤没法重新走一遍",
        "这些步骤没有语义落点，重跑只能照历史坐标点，平台不允许；"
        "要让探索把这类动作记上可复现的语义目标。",
    ),
    # 下面几条不是选路失败，而是这条缺口压根儿不该走导航恢复；一起登记，
    # 免得台账上一半原因有人话、一半只剩代码。
    "RECAPTURE_FORBIDDEN_BY_REQUEST": (
        "这条规则不允许重新走一遍取证",
        "规则要的是当场那一下的证据（比如点击瞬间的弹窗），重走一遍就不算同一份证据，"
        "只能靠下一轮探索原地采到。",
    ),
    "ORIGINAL_SURFACE_NO_LONGER_CURRENT": (
        "只能在原页面补，但页面已经走掉了",
        "这条规则要求证据跟原页面是同一次停留，探索已经翻到别的页面，补不回来了；"
        "要在同一页内把证据一次采全。",
    ),
    "ORIGINAL_ATOMIC_RECAPTURE_FORBIDDEN": (
        "这条缺口按规定不能事后补",
        "规则要求的是原始那一帧，任何重采都不算；只能下一轮探索时一次采对。",
    ),
    "TARGET_GRAPH_STATE_UNAVAILABLE": (
        "要补的那个页面在页面图里没有对应状态",
        "页面图这一轮没给这张截图建页面状态；修好投影或补录状态后可再试，不要立刻当成确定性不可补采。",
    ),
    "RECAPTURE_CONTEXT_UNAVAILABLE": (
        "读不到这条缺口当前的执行上下文",
        "对应的会话或截图已经不在当前运行里；上下文恢复后可再试，过期前不要落 unfulfillable。",
    ),
    "EVIDENCE_REQUEST_EXPIRED": (
        "补采请求已经过期",
        "请求有效期内没排到执行，任务收口时按证据不足结算；预算紧时会反复出现。",
    ),
    "PAGE_GRAPH_ROUTE_CONTRACT_INVALID": (
        "页面图返回的路线不符合约定",
        "路线的运行域、步骤连续性或可信标记和约定不一致，平台宁可不走也不猜；修好投影后可再试补采。",
    ),
    "PAGE_GRAPH_ROUTE_TRANSIENT_FAILURE": (
        "页面图恢复路线暂时读失败",
        "读页面图或规划时遇到临时故障，请求仍保持可重试；修好后会再排补采，不要当成确定性不可补采。",
    ),
    "ACTIVE_SESSION_TARGET_DRIFTED": (
        "要在原页面当场补采，但探索已经离开了那个页面",
        "这条缺口只能趁页面还停在原地时补；探索已经走到别的页面，这一轮补不回来，"
        "要在离开页面之前把证据采全。",
    ),
    "RECAPTURE_SESSION_DEPENDENCY_UNSUPPORTED": (
        "这条缺口要求的补采方式，这个端还不支持",
        "补采请求要求的会话条件（比如全新会话）这个端做不到，只能等下一轮探索采到。",
    ),
    "SNAPSHOT_NEW_SESSION_RUNTIME_UNAVAILABLE": (
        "这个端还不能关掉原会话、重开新会话来补采",
        "规则要求换一个全新会话重新采一次，H5 现在做不到，这一轮补不回来；"
        "只能等下一轮探索时一次采到。",
    ),
    "RECAPTURE_REQUEST_CONSTRAINT_UNSUPPORTED": (
        "补采请求里的限制条件排不出可执行的补采方案",
        "请求的限制条件和这个端的恢复方式对不上，平台不硬凑；找研发核对这条规则的补采要求。",
    ),
}

# 各端补采入口会给同一个原因码加上端前缀（如 H5_TARGET_GRAPH_STATE_UNAVAILABLE）
_SURFACE_PREFIXES = ("H5_", "APP_", "WEB_")


def registered_route_reason(code: str) -> str | None:
    """原因码登记过就返回登记用的那个码（带端前缀的先按原样找，再去掉前缀找），否则 None。"""

    if code in REASON_EXPLANATIONS:
        return code
    for prefix in _SURFACE_PREFIXES:
        if code.startswith(prefix) and code.removeprefix(prefix) in REASON_EXPLANATIONS:
            return code.removeprefix(prefix)
    return None


def explain_route_reason(code: str) -> dict[str, str]:
    """把规划失败原因翻成运营看得懂的一句话和处理建议。"""

    label, advice = REASON_EXPLANATIONS.get(
        registered_route_reason(code) or code,
        (code, "未登记的补采选路原因，找研发确认。"),
    )
    return {"reason_code": code, "reason_label": label, "advice": advice}


@dataclass(frozen=True, slots=True)
class EvidenceRecoveryRoute:
    """已锁定到同一 Task/Run/Epoch 的可信历史路线。"""

    route_id: str
    source_state_id: str
    target_state_id: str
    target_identity: str
    transition_ids: tuple[str, ...]
    risk_tier: RouteRiskTier = RouteRiskTier.STABLE
    fragile_step_ids: tuple[str, ...] = ()

    @property
    def risk_label(self) -> str:
        return TIER_LABELS[self.risk_tier]


@dataclass(frozen=True, slots=True)
class EvidenceRecoveryRoutePlan:
    """选路结果；没有可执行路线时必须带确定原因。"""

    route: EvidenceRecoveryRoute | None
    reason_code: str | None = None
    blocked_action_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.route is None:
            if not isinstance(self.reason_code, str) or not self.reason_code.strip():
                raise ValueError("没有可执行路线时必须提供 reason_code")
        elif self.reason_code is not None:
            raise ValueError("已选出路线不能携带 reason_code")

    @property
    def planned(self) -> bool:
        return self.route is not None

    def explain(self) -> dict[str, Any]:
        """只读解释，供补采台账与任务详情展示。"""

        if self.route is not None:
            return {
                "planned": True,
                "route_id": self.route.route_id,
                "step_count": len(self.route.transition_ids),
                "risk_tier": self.route.risk_tier.value,
                "risk_label": self.route.risk_label,
                "fragile_step_ids": list(self.route.fragile_step_ids),
            }
        explained = explain_route_reason(str(self.reason_code))
        if self.blocked_action_kinds:
            explained["blocked_action_kinds"] = list(self.blocked_action_kinds)
        return {"planned": False, **explained}


def plan_evidence_recovery_route(
    response: Mapping[str, Any],
    *,
    task_id: str,
    agent_run_id: str,
    finalization_epoch: int,
    source_state_id: str,
    target_state_id: str,
    target_identity: str,
) -> EvidenceRecoveryRoutePlan:
    """按「风险较低 → 步骤较少」选一条能逐步重新验证的路线。"""

    # 图不可用时走的是 page_graph.v1 的空壳应答，先认状态再校验路线协议。
    status = response.get("status")
    if status in {"unavailable", "unreachable"}:
        return EvidenceRecoveryRoutePlan(
            route=None,
            reason_code=RouteUnavailableReason.GRAPH_NO_TRUSTED_ROUTE.value,
        )
    _require_scope(
        response,
        task_id=task_id,
        agent_run_id=agent_run_id,
        finalization_epoch=finalization_epoch,
    )
    if status != "available":
        raise EvidenceRecoveryRouteValidationError("页面图恢复状态不受支持")
    if response.get("requires_stepwise_reverification") is not True:
        raise EvidenceRecoveryRouteValidationError("页面图恢复路线未要求逐边复核")

    routes = response.get("routes")
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)):
        raise EvidenceRecoveryRouteValidationError("页面图恢复路线必须为数组")

    matched = 0
    candidates: list[EvidenceRecoveryRoute] = []
    blocked_action_kinds: set[str] = set()
    for raw_route in routes:
        if not isinstance(raw_route, Mapping):
            raise EvidenceRecoveryRouteValidationError("页面图恢复路线格式无效")
        if not _matches_target(
            raw_route,
            source_state_id=source_state_id,
            target_state_id=target_state_id,
            target_identity=target_identity,
        ):
            continue
        matched += 1
        route, blocked = _validate_route(
            raw_route,
            task_id=task_id,
            agent_run_id=agent_run_id,
            finalization_epoch=finalization_epoch,
            source_state_id=source_state_id,
            target_state_id=target_state_id,
            target_identity=target_identity,
            target_platform=response.get("target_platform"),
        )
        if route is None:
            blocked_action_kinds.update(blocked)
            continue
        candidates.append(route)

    if candidates:
        return EvidenceRecoveryRoutePlan(route=min(candidates, key=_route_order))
    if matched:
        return EvidenceRecoveryRoutePlan(
            route=None,
            reason_code=RouteUnavailableReason.NOT_REDRIVABLE.value,
            blocked_action_kinds=tuple(sorted(blocked_action_kinds)),
        )
    return EvidenceRecoveryRoutePlan(
        route=None,
        reason_code=RouteUnavailableReason.TARGET_NOT_REACHED.value,
    )


def _route_order(route: EvidenceRecoveryRoute) -> tuple[int, int, int, str]:
    """风险档优先，其次不稳步数，再其次总步数，最后按 ID 保证结果稳定。"""

    return (
        _TIER_RANK[route.risk_tier],
        len(route.fragile_step_ids),
        len(route.transition_ids),
        route.route_id,
    )


def _require_scope(
    response: Mapping[str, Any],
    *,
    task_id: str,
    agent_run_id: str,
    finalization_epoch: int,
) -> None:
    if response.get("protocol_version") != "page_graph_recovery.v1":
        raise EvidenceRecoveryRouteValidationError("页面图恢复协议版本不受支持")
    expected = {
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "finalization_epoch": finalization_epoch,
    }
    for field_name, value in expected.items():
        if response.get(field_name) != value:
            raise EvidenceRecoveryRouteValidationError(
                f"页面图恢复路线 {field_name} 与请求执行域不一致"
            )


def _matches_target(
    route: Mapping[str, Any],
    *,
    source_state_id: str,
    target_state_id: str,
    target_identity: str,
) -> bool:
    return (
        route.get("source_state_id") == source_state_id
        and route.get("target_state_id") == target_state_id
        and route.get("target_page_key") == target_identity
    )


def _validate_route(
    route: Mapping[str, Any],
    *,
    task_id: str,
    agent_run_id: str,
    finalization_epoch: int,
    source_state_id: str,
    target_state_id: str,
    target_identity: str,
    target_platform: Any,
) -> tuple[EvidenceRecoveryRoute | None, tuple[str, ...]]:
    """校验一条路线；不可重跑时返回挡路的动作类型而不是抛错。"""

    raw_steps = route.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes)):
        raise EvidenceRecoveryRouteValidationError("页面图恢复步骤必须为数组")
    if not raw_steps:
        raise EvidenceRecoveryRouteValidationError("页面图导航恢复路线不能为空")

    current_state_id = source_state_id
    transition_ids: list[str] = []
    fragile_step_ids: list[str] = []
    blocked_action_kinds: set[str] = set()
    source_rebind = route.get("source_rebind")
    for step_index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            raise EvidenceRecoveryRouteValidationError("页面图恢复步骤格式无效")
        transition_id = _required_text(raw_step.get("transition_id"), "transition_id")
        step_source = _required_text(raw_step.get("source_state_id"), "source_state_id")
        step_target = _required_text(raw_step.get("target_state_id"), "target_state_id")
        if step_source != current_state_id:
            if step_index != 0 or not _valid_h5_source_rebind(
                source_rebind,
                target_platform=target_platform,
                requested_source_state_id=current_state_id,
                historical_source_state_id=step_source,
            ):
                raise EvidenceRecoveryRouteValidationError("页面图恢复步骤不连续")
            fragile_step_ids.append(transition_id)
        if (
            raw_step.get("status") != "completed"
            or raw_step.get("proof") != "trusted"
            or raw_step.get("risk_level") != "low"
            or raw_step.get("replay_eligible") is not True
        ):
            raise EvidenceRecoveryRouteValidationError("页面图恢复步骤不是可信低风险成功边")
        if transition_id in transition_ids:
            raise EvidenceRecoveryRouteValidationError("页面图恢复步骤不能重复")
        action_kind = _step_action_kind(raw_step)
        if not _redrivable(action_kind, raw_step):
            blocked_action_kinds.add(action_kind)
        elif _fragile(action_kind, raw_step):
            fragile_step_ids.append(transition_id)
        transition_ids.append(transition_id)
        current_state_id = step_target
    if current_state_id != target_state_id:
        raise EvidenceRecoveryRouteValidationError("页面图恢复步骤没有到达目标状态")
    if blocked_action_kinds:
        return None, tuple(sorted(blocked_action_kinds))

    identity = {
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "finalization_epoch": finalization_epoch,
        "source_state_id": source_state_id,
        "target_state_id": target_state_id,
        "target_identity": target_identity,
        "transition_ids": transition_ids,
    }
    return (
        EvidenceRecoveryRoute(
            route_id=f"page-graph-route:{stable_protocol_hash(identity)}",
            source_state_id=source_state_id,
            target_state_id=target_state_id,
            target_identity=target_identity,
            transition_ids=tuple(transition_ids),
            risk_tier=(
                RouteRiskTier.FRAGILE if fragile_step_ids else RouteRiskTier.STABLE
            ),
            fragile_step_ids=tuple(fragile_step_ids),
        ),
        (),
    )


def _valid_h5_source_rebind(
    value: Any,
    *,
    target_platform: Any,
    requested_source_state_id: str,
    historical_source_state_id: str,
) -> bool:
    """只接受投影器签发的 H5 首步逻辑身份换绑合同。"""

    if target_platform != "h5" or not isinstance(value, Mapping):
        return False
    return (
        value.get("protocol_version") == "h5-page-graph-source-rebind.v1"
        and value.get("requested_source_state_id") == requested_source_state_id
        and value.get("historical_source_state_id") == historical_source_state_id
        and isinstance(value.get("logical_page_key"), str)
        and bool(str(value.get("logical_page_key")).strip())
        and value.get("verified_by")
        == "confirmed_logical_identity_and_canonical_url"
    )


def _step_action_kind(step: Mapping[str, Any]) -> str:
    value = step.get("action_kind")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return "unknown"


def _redrivable(action_kind: str, step: Mapping[str, Any]) -> bool:
    """执行端重新观察后能不能按语义重新绑定这一步。"""

    if action_kind not in RECOVERY_ROUTE_REDRIVABLE_ACTIONS:
        return False
    if action_kind in _SEMANTIC_TARGET_REQUIRED_ACTIONS:
        semantic_target = step.get("semantic_target")
        return isinstance(semantic_target, str) and bool(semantic_target.strip())
    return True


def _fragile(action_kind: str, step: Mapping[str, Any]) -> bool:
    """这一步重跑时容易走偏：靠历史栈或滚动位置，或当初执行就出过错。"""

    if action_kind in _POSITION_DEPENDENT_ACTIONS:
        return True
    return any(code.startswith("error:") for code in _reason_codes(step))


def _reason_codes(step: Mapping[str, Any]) -> tuple[str, ...]:
    raw = step.get("reason_codes")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceRecoveryRouteValidationError(f"{field_name} 不能为空")
    return value.strip()
