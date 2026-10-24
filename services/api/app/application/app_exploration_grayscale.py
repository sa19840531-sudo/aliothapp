"""App 有限探索灰度（M5）：三队列、图去重；Lab 可开图辅助调度。

生产默认 ``authority.M5=false``：
- 仍做 discovery 图／Coverage 去重与外跳旁路合同；
- ``schedules_from_graph=false``，不改 Coverage／Gate。

Lab／灰度 ``build_lab_m5_graph_schedule_milestone_policy()``：
- ``schedules_from_graph=true``：对未访问目标候选做图辅助优先排序；
- 仍恒 ``gate_consumes_graph=false``、``coverage_authority_changed=false``；
- 外跳不计业务覆盖；复合动作禁止跨 epoch 坐标重放。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.application.app_bounded_exploration_milestones import (
    build_app_bounded_exploration_milestone_policy,
    milestone_authority_enabled,
    milestone_shadow_enabled,
)
from services.api.app.application.app_package_boundary import ExternalActivityKind
from services.api.app.application.bounded_exploration_scheduler import (
    BoundedExplorationPolicy,
    BoundedExplorationScheduler,
    ExplorationQueue,
)
from services.api.app.domain.exploration.snapshots import CandidateAction
from services.api.app.domain.protocol import freeze_json_mapping

APP_M5_GRAYSCALE_VERSION = "app-exploration-grayscale.v2"
APP_COMPOSITE_REBIND_VERSION = "app-composite-action-rebind.v1"

_EXTERNAL_BYPASS_ALLOWED = frozenset(
    {
        ExternalActivityKind.BROWSER.value,
        ExternalActivityKind.STORE.value,
    }
)
_EXTERNAL_BYPASS_FORBIDDEN = frozenset(
    {
        ExternalActivityKind.PAYMENT.value,
        ExternalActivityKind.SETTINGS.value,
        ExternalActivityKind.LAUNCHER.value,
        ExternalActivityKind.OTHER.value,
    }
)


def build_lab_m5_graph_schedule_milestone_policy(
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Lab／灰度显式打开 M5 图辅助调度；生产工厂不得调用本函数。"""

    raw = dict(overrides) if isinstance(overrides, Mapping) else {}
    authority = dict(raw.get("authority") or {})
    shadow = dict(raw.get("shadow") or {})
    # Lab 单开 M5 时仍带上 M4，便于与生产开闸链对齐验证。
    authority["M4"] = True
    authority["M5"] = True
    shadow["M4"] = True
    shadow["M5"] = True
    return build_app_bounded_exploration_milestone_policy(
        {**raw, "authority": authority, "shadow": shadow},
        apply_production_env=False,
    )


def resolve_app_m5_grayscale_active(
    policy: Mapping[str, Any] | None = None,
) -> bool:
    """M5 影子／灰度是否启用（去重与旁路合同）。"""

    return milestone_shadow_enabled(policy, "M5")


def resolve_app_m5_schedules_from_graph(
    policy: Mapping[str, Any] | None = None,
) -> bool:
    """是否允许图辅助调度排序；生产默认 false。"""

    return milestone_authority_enabled(policy, "M5")


def milestone_policy_from_coverage_metadata(
    coverage_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """从覆盖元数据读取可选里程碑覆盖；缺省跟随 Lab／生产组合根。"""

    from services.api.app.application.app_lab_composition import (
        resolve_effective_app_milestone_policy,
    )

    meta = coverage_metadata if isinstance(coverage_metadata, Mapping) else {}
    raw = meta.get("app_bounded_exploration_milestone_policy")
    if not isinstance(raw, Mapping):
        # 兼容旧键名
        raw = meta.get("app_bounded_exploration_milestones")
    if isinstance(raw, Mapping):
        # 元数据内已是完整策略；overrides 优先，且不再叠生产 env，避免冲掉 Lab。
        return build_app_bounded_exploration_milestone_policy(
            raw,
            apply_production_env=False,
        )
    return resolve_effective_app_milestone_policy()


def build_app_m5_queue_policy_snapshot(
    policy: BoundedExplorationPolicy | None = None,
) -> dict[str, Any]:
    """冻结三队列比例快照，供审计；不改调度器实现。"""

    bounded = policy or BoundedExplorationPolicy()
    return {
        "policy_version": bounded.policy_version,
        "policy_hash": bounded.policy_hash,
        "required_ratio": bounded.required_ratio,
        "evidence_gap_ratio": bounded.evidence_gap_ratio,
        "discovery_ratio": bounded.discovery_ratio,
        "queues": [item.value for item in ExplorationQueue],
    }


def graph_business_page_keys(graph: Mapping[str, Any] | None) -> set[str]:
    """影子图上已确认的业务页身份集合。"""

    if not isinstance(graph, Mapping):
        return set()
    keys: set[str] = set()
    for state in graph.get("states") or ():
        if not isinstance(state, Mapping):
            continue
        if state.get("counts_as_business_page") is not True:
            continue
        if state.get("is_overlay") or state.get("is_external"):
            continue
        if state.get("identity_status") != "confirmed":
            continue
        key = str(state.get("logical_page_key") or "").strip()
        if key:
            keys.add(key)
    return keys


def coverage_business_page_keys(coverage_metadata: Mapping[str, Any] | None) -> set[str]:
    meta = coverage_metadata if isinstance(coverage_metadata, Mapping) else {}
    keys: set[str] = set()
    for item in meta.get("covered_business_state_ids") or ():
        text = str(item).strip()
        if text:
            keys.add(text)
    identity = str(meta.get("business_page_identity") or "").strip()
    if identity:
        keys.add(identity)
    return keys


def _candidate_target_keys(candidate: CandidateAction) -> set[str]:
    meta = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
    keys: set[str] = set()
    for field in (
        "expected_business_page_identity",
        "business_page_identity",
        "expected_next_page_identity",
    ):
        value = str(meta.get(field) or "").strip()
        if value:
            keys.add(value)
    expected_type = str(meta.get("expected_next_page_type") or "").strip().casefold()
    if expected_type:
        keys.add(f"page_type:{expected_type}")
    return keys


def dedup_discovery_candidates_by_page_graph(
    candidates: Sequence[CandidateAction],
    *,
    graph: Mapping[str, Any] | None,
    coverage_metadata: Mapping[str, Any] | None = None,
    required_page_types: Sequence[str] = (),
    evidence_gap_page_types: Sequence[str] = (),
    missing_page_types: Sequence[str] = (),
    scheduler: BoundedExplorationScheduler | None = None,
) -> tuple[list[CandidateAction], list[dict[str, str]]]:
    """对 DISCOVERY 队列做图／覆盖身份去重；必检与缺证队列保留。"""

    engine = scheduler or BoundedExplorationScheduler()
    visited = graph_business_page_keys(graph) | coverage_business_page_keys(coverage_metadata)
    meta = coverage_metadata if isinstance(coverage_metadata, Mapping) else {}
    covered_types = {
        str(item).strip().casefold()
        for item in (meta.get("covered_page_types") or ())
        if str(item).strip()
    }
    kept: list[CandidateAction] = []
    suppressed: list[dict[str, str]] = []
    for candidate in candidates:
        queue = engine.classify(
            candidate,
            required_page_types=required_page_types,
            evidence_gap_page_types=evidence_gap_page_types,
            missing_page_types=missing_page_types,
        )
        if queue is not ExplorationQueue.DISCOVERY:
            kept.append(candidate)
            continue
        cand_meta = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
        if (
            cand_meta.get("coverage_page_seek") is True
            or cand_meta.get("home_category_grid_seek") is True
            or cand_meta.get("home_top_category_tab_seek") is True
            or cand_meta.get("control_role") in {"dismiss", "skip"}
            or cand_meta.get("frontier_scope") == "app"
        ):
            kept.append(candidate)
            continue
        targets = _candidate_target_keys(candidate)
        hit = targets & visited
        type_hits = {
            key.removeprefix("page_type:")
            for key in targets
            if key.startswith("page_type:") and key.removeprefix("page_type:") in covered_types
        }
        if hit or type_hits:
            suppressed.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "reason": "m5_graph_discovery_dedup",
                    "matched_keys": ",".join(sorted(hit | {f"page_type:{t}" for t in type_hits})),
                }
            )
            continue
        kept.append(candidate)
    return kept, suppressed


def prioritize_candidates_by_page_graph(
    candidates: Sequence[CandidateAction],
    *,
    graph: Mapping[str, Any] | None,
    coverage_metadata: Mapping[str, Any] | None = None,
    schedules_from_graph: bool = False,
) -> tuple[list[CandidateAction], list[dict[str, str]]]:
    """Lab 图调度：未访问目标候选优先；不改 Coverage／Gate，不删除必检／关层。"""

    if not schedules_from_graph:
        return list(candidates), []
    visited = graph_business_page_keys(graph) | coverage_business_page_keys(coverage_metadata)
    boosted: list[CandidateAction] = []
    normal: list[CandidateAction] = []
    audit: list[dict[str, str]] = []
    for candidate in candidates:
        cand_meta = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
        # 必检／关层／覆盖寻页保持相对顺序，不因图调度被挤到队尾之外的策略：仍可 boost。
        targets = _candidate_target_keys(candidate)
        unvisited = bool(targets) and not (targets & visited)
        recovery = cand_meta.get("graph_recovery") is True or cand_meta.get("m5_graph_priority") is True
        if unvisited or recovery:
            boosted.append(candidate)
            audit.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "reason": "m5_graph_schedule_boost_unvisited"
                    if unvisited
                    else "m5_graph_schedule_boost_recovery",
                }
            )
        else:
            normal.append(candidate)
    return boosted + normal, audit


def evaluate_external_bypass(
    *,
    external_kind: str | ExternalActivityKind | None,
    bounce_count: int = 0,
    bounce_limit: int = 3,
    return_failure_count: int = 0,
    return_failure_limit: int = 2,
) -> dict[str, Any]:
    """条件外跳旁路：允许观察+回源的种类与熔断；不计业务覆盖。"""

    kind = (
        external_kind.value
        if isinstance(external_kind, ExternalActivityKind)
        else str(external_kind or "").strip().casefold()
    )
    reasons: list[str] = []
    allow_observe = kind in _EXTERNAL_BYPASS_ALLOWED
    if kind in _EXTERNAL_BYPASS_FORBIDDEN or not kind:
        allow_observe = False
        reasons.append("external_kind_forbidden_or_unknown")
    if bounce_count >= bounce_limit:
        allow_observe = False
        reasons.append("external_bounce_limit")
    if return_failure_count >= return_failure_limit:
        allow_observe = False
        reasons.append("external_return_failure_limit")
    if allow_observe:
        reasons.append("observe_and_bounded_recover_only")
    return {
        "protocol_version": "app-external-bypass.v1",
        "external_kind": kind or None,
        "allow_observe_landing": allow_observe,
        "allow_business_actions": False,
        "counts_as_business_coverage": False,
        "require_bounded_recovery": True,
        "reason_codes": reasons,
    }


def build_composite_action_rebind_contract(
    *,
    source_capture_id: str | None,
    requires_rebind_after_scroll: bool = True,
) -> dict[str, Any]:
    """复合动作（先滚后点）合同：禁止跨 Capture epoch 重放坐标。"""

    capture_id = str(source_capture_id or "").strip() or None
    return {
        "protocol_version": APP_COMPOSITE_REBIND_VERSION,
        "source_capture_id": capture_id,
        "requires_rebind_after_scroll": requires_rebind_after_scroll,
        "cross_epoch_coordinate_replay_forbidden": True,
        "status": "declared" if capture_id else "awaiting_capture",
    }


def attach_app_m5_grayscale_shadow(
    coverage_metadata: Mapping[str, Any] | None,
    *,
    suppressed: Sequence[Mapping[str, str]] | None = None,
    schedule_boosts: Sequence[Mapping[str, str]] | None = None,
    external_bypass: Mapping[str, Any] | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
    queue_policy: BoundedExplorationPolicy | None = None,
) -> dict[str, Any]:
    """写入 M5 灰度审计；Gate／Coverage 权威恒不切换。"""

    policy = (
        dict(milestone_policy)
        if isinstance(milestone_policy, Mapping)
        else build_app_bounded_exploration_milestone_policy()
    )
    meta = dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    active = resolve_app_m5_grayscale_active(policy)
    schedules = resolve_app_m5_schedules_from_graph(policy)
    adb_actions = milestone_authority_enabled(policy, "M4")
    payload = {
        "protocol_version": APP_M5_GRAYSCALE_VERSION,
        "active": active,
        "milestone": "M5",
        "queue_policy": build_app_m5_queue_policy_snapshot(queue_policy),
        "graph_dedup_enabled": active,
        "discovery_suppressed": [dict(item) for item in (suppressed or ())],
        "schedule_boosts": [dict(item) for item in (schedule_boosts or ())],
        "external_bypass": dict(external_bypass)
        if isinstance(external_bypass, Mapping)
        else None,
        "schedules_from_graph": schedules,
        "gate_consumes_graph": False,
        "coverage_authority_changed": False,
        "adb_actions_enabled": adb_actions,
    }
    meta["app_exploration_grayscale_shadow"] = dict(
        freeze_json_mapping(payload, "app_exploration_grayscale_shadow")
    )
    return meta
