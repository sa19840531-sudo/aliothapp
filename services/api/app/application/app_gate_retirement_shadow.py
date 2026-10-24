"""App Gate／退役（M7）：影子对账默认；Lab 可开图辅助 Gate 裁决。

生产默认 ``authority.M7=false``：
- 只记录 ``would_block_if_enforced`` 预览；
- ``gate_consumes_graph=false``／``enforces_gate=false``；
- **不**改 Completion Gate SQL、不写正式 block_reasons。

Lab／灰度 ``build_lab_m7_gate_enforcement_milestone_policy()``：
- 可声明 ``gate_consumes_graph=true`` 与 Lab 裁决（``lab_gate_passed``／``lab_block_reasons``）；
- 退役目录可标 ``ready_for_retirement_review``（指标齐全时）；
- 恒 ``mutates_completion_gate_sql=false``、``deletion_allowed=false``。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.api.app.application.app_bounded_exploration_milestones import (
    APP_BOUNDED_EXPLORATION_METRIC_KEYS,
    build_app_bounded_exploration_milestone_policy,
    milestone_authority_enabled,
    milestone_shadow_enabled,
)
from services.api.app.application.page_graph_projection import (
    reconcile_coverage_with_page_graph,
)
from services.api.app.domain.protocol import freeze_json_mapping

APP_M7_GATE_RETIREMENT_VERSION = "app-gate-retirement-shadow.v2"
APP_M7_WORKBENCH_PROJECTION_VERSION = "app-workbench-page-graph-projection.v1"
APP_M7_RETIREMENT_CATALOG_VERSION = "app-retirement-candidate-catalog.v2"

APP_RETIREMENT_CANDIDATES: tuple[dict[str, Any], ...] = (
    {
        "candidate_id": "legacy_path_lineage_projection",
        "surface": "app",
        "description": "旧 path／lineage 投影分支（非页面状态图权威）",
        "required_metric_keys": (
            "page_graph_coverage_reconcile_mismatch_count",
            "verified_arrival_count",
        ),
        "deletion_allowed": False,
        "status": "catalog_only",
    },
    {
        "candidate_id": "site_specific_exploration_adapter",
        "surface": "app",
        "description": "页面／站点专项探索适配分支",
        "required_metric_keys": (
            "stable_capture_success_count",
            "non_stable_as_business_evidence_count",
            "overlay_close_failure_terminal_count",
        ),
        "deletion_allowed": False,
        "status": "catalog_only",
    },
    {
        "candidate_id": "agent_direct_adb_fallback",
        "surface": "app",
        "description": "Agent 直连 ADB／Driver 兜底路径（应已由 Broker 替代）",
        "required_metric_keys": (
            "agent_direct_adb_attempt_count",
            "external_package_escape_count",
        ),
        "deletion_allowed": False,
        "status": "catalog_only",
    },
)


def build_lab_m7_gate_enforcement_milestone_policy(
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Lab／灰度显式打开 M7 图辅助 Gate 裁决；生产工厂不得调用。"""

    raw = dict(overrides) if isinstance(overrides, Mapping) else {}
    authority = dict(raw.get("authority") or {})
    shadow = dict(raw.get("shadow") or {})
    authority["M4"] = True
    authority["M5"] = True
    authority["M6"] = True
    authority["M7"] = True
    shadow["M4"] = True
    shadow["M5"] = True
    shadow["M6"] = True
    shadow["M7"] = True
    return build_app_bounded_exploration_milestone_policy(
        {**raw, "authority": authority, "shadow": shadow},
        apply_production_env=False,
    )


def resolve_app_m7_gate_shadow_active(
    policy: Mapping[str, Any] | None = None,
) -> bool:
    """M7 影子／工作台投影是否启用。"""

    return milestone_shadow_enabled(policy, "M7")


def resolve_app_m7_gate_enforcement(
    policy: Mapping[str, Any] | None = None,
) -> bool:
    """是否允许 Lab 图辅助 Gate 裁决；生产默认 false。"""

    return milestone_authority_enabled(policy, "M7")


def build_app_retirement_candidate_catalog(
    *,
    observed_metrics: Mapping[str, Any] | None = None,
    lab_review_enabled: bool = False,
) -> dict[str, Any]:
    """生成退役候选目录；永不允许本模块实际删除代码。"""

    metrics = observed_metrics if isinstance(observed_metrics, Mapping) else {}
    items: list[dict[str, Any]] = []
    for raw in APP_RETIREMENT_CANDIDATES:
        required = tuple(raw.get("required_metric_keys") or ())
        missing = [key for key in required if key not in APP_BOUNDED_EXPLORATION_METRIC_KEYS]
        observed_present = [key for key in required if key in metrics]
        metrics_complete = bool(required) and len(observed_present) == len(required)
        ready = lab_review_enabled and metrics_complete and missing == []
        items.append(
            {
                "candidate_id": raw["candidate_id"],
                "surface": raw["surface"],
                "description": raw["description"],
                "required_metric_keys": list(required),
                "metric_keys_known": missing == [],
                "observed_metric_keys_present": observed_present,
                "deletion_allowed": False,
                "status": "ready_for_review" if ready else "catalog_only",
                "ready_for_retirement_review": ready,
            }
        )
    return {
        "protocol_version": APP_M7_RETIREMENT_CATALOG_VERSION,
        "candidates": items,
        "any_deletion_executed": False,
        "rollback_review_required": True,
        "lab_review_enabled": lab_review_enabled,
    }


def build_app_workbench_page_graph_projection(
    graph: Mapping[str, Any] | None,
    *,
    reconcile: Mapping[str, Any] | None = None,
    drives_lab_advisory: bool = False,
) -> dict[str, Any]:
    """工作台图／覆盖拆分投影；正式 Gate 仍不由此驱动。"""

    states = list(graph.get("states") or ()) if isinstance(graph, Mapping) else []
    transitions = (
        list(graph.get("transitions") or ()) if isinstance(graph, Mapping) else []
    )
    business = 0
    overlay = 0
    external = 0
    for state in states:
        if not isinstance(state, Mapping):
            continue
        if state.get("is_overlay"):
            overlay += 1
        elif state.get("is_external"):
            external += 1
        elif state.get("counts_as_business_page") is True:
            business += 1
    trusted_edges = 0
    for edge in transitions:
        if not isinstance(edge, Mapping):
            continue
        if edge.get("status") == "completed" and edge.get("proof") == "trusted":
            trusted_edges += 1
    rec = dict(reconcile) if isinstance(reconcile, Mapping) else {}
    return {
        "protocol_version": APP_M7_WORKBENCH_PROJECTION_VERSION,
        "available": isinstance(graph, Mapping) and bool(graph),
        "state_count": len(states),
        "transition_count": len(transitions),
        "business_state_count": business,
        "overlay_state_count": overlay,
        "external_state_count": external,
        "trusted_edge_count": trusted_edges,
        "coverage_split": {
            "matched_count": int(rec.get("matched_count") or 0),
            "only_in_coverage_count": int(rec.get("only_in_coverage_count") or 0),
            "only_in_graph_count": int(rec.get("only_in_graph_count") or 0),
            "aligned": rec.get("aligned") is True,
        },
        "drives_gate": False,
        "drives_formal_gate": False,
        "drives_lab_advisory": drives_lab_advisory,
        "drives_scheduler": False,
    }


def build_app_m7_gate_enforcement_shadow(
    *,
    coverage_metadata: Mapping[str, Any] | None,
    graph: Mapping[str, Any] | None = None,
    observed_metrics: Mapping[str, Any] | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Gate 影子／Lab 裁决：正式 Completion Gate SQL 输入恒不变。"""

    policy = (
        dict(milestone_policy)
        if isinstance(milestone_policy, Mapping)
        else build_app_bounded_exploration_milestone_policy()
    )
    active = resolve_app_m7_gate_shadow_active(policy)
    lab_enforce = resolve_app_m7_gate_enforcement(policy)
    gate_sql = bool(policy.get("production_gate_sql_consumes_graph") is True)
    # SQL 切读仅在 M7 authority + 独立 env 同时满足时生效（policy 已 fail-closed）。
    mutates_gate_sql = bool(lab_enforce and gate_sql)
    meta = coverage_metadata if isinstance(coverage_metadata, Mapping) else {}
    prior_shadow = meta.get("app_coverage_page_graph_shadow")
    if isinstance(prior_shadow, Mapping) and prior_shadow.get("matched_count") is not None:
        reconcile = dict(prior_shadow)
    else:
        reconcile = reconcile_coverage_with_page_graph(
            coverage_metadata=dict(meta),
            graph=graph if isinstance(graph, Mapping) else meta.get("app_page_graph_snapshot"),
        )
    reconcile["coverage_authority_changed"] = False
    # 对账摘要本身不把正式调度／Gate 切到图；Lab 裁决另字段声明。
    reconcile["schedules_from_graph"] = False
    reconcile["gate_consumes_graph"] = False

    page_graph_consistent = reconcile.get("aligned") is True
    would_block_reasons: list[str] = []
    if not page_graph_consistent:
        would_block_reasons.append("page_graph_coverage_mismatch")
    if int(reconcile.get("only_in_coverage_count") or 0) > 0:
        would_block_reasons.append("coverage_identity_missing_on_graph")
    if int(reconcile.get("only_in_graph_count") or 0) > 0:
        would_block_reasons.append("graph_arrival_missing_in_coverage")

    coverage_complete = meta.get("coverage_complete") is True
    visual_quality = meta.get("visual_quality_satisfied") is True
    formal_coverage_inputs = {
        "coverage_complete": coverage_complete,
        "visual_quality_satisfied": visual_quality,
        "source": "coverage_summary_only",
    }

    lab_block_reasons = list(would_block_reasons)
    if lab_enforce:
        if not coverage_complete:
            lab_block_reasons.append("coverage_incomplete")
        if not visual_quality:
            lab_block_reasons.append("visual_quality_unsatisfied")
    lab_gate_passed = bool(lab_enforce and not lab_block_reasons)

    workbench = build_app_workbench_page_graph_projection(
        graph if isinstance(graph, Mapping) else meta.get("app_page_graph_snapshot"),
        reconcile=reconcile,
        drives_lab_advisory=lab_enforce,
    )
    retirement = build_app_retirement_candidate_catalog(
        observed_metrics=observed_metrics,
        lab_review_enabled=lab_enforce,
    )
    payload = {
        "protocol_version": APP_M7_GATE_RETIREMENT_VERSION,
        "active": active,
        "milestone": "M7",
        "gate_shadow_only": not lab_enforce,
        "gate_consumes_graph": bool(lab_enforce or mutates_gate_sql),
        "enforces_gate": lab_enforce,
        "mutates_completion_gate_sql": mutates_gate_sql,
        "writes_formal_block_reasons": mutates_gate_sql,
        "coverage_authority_changed": mutates_gate_sql,
        "schedules_from_graph": False,
        "page_graph_consistent": page_graph_consistent,
        "reconcile": {
            "matched_count": reconcile.get("matched_count"),
            "only_in_coverage_count": reconcile.get("only_in_coverage_count"),
            "only_in_graph_count": reconcile.get("only_in_graph_count"),
            "aligned": reconcile.get("aligned"),
            "overlay_state_count": reconcile.get("overlay_state_count"),
            "external_state_count": reconcile.get("external_state_count"),
        },
        "formal_gate_inputs_unchanged": not mutates_gate_sql,
        "formal_coverage_inputs": formal_coverage_inputs,
        "would_block_if_enforced": bool(would_block_reasons),
        "would_block_reasons": would_block_reasons,
        "lab_gate_passed": lab_gate_passed if lab_enforce else None,
        "lab_block_reasons": lab_block_reasons if lab_enforce else [],
        "workbench_projection": workbench,
        "retirement_catalog": retirement,
        "metric_keys": list(APP_BOUNDED_EXPLORATION_METRIC_KEYS),
        "reason_codes": (
            ["production_gate_sql_consumes_graph", "coverage_authority_changed"]
            if mutates_gate_sql
            else (
                ["lab_m7_gate_advisory", "completion_gate_sql_unchanged"]
                if lab_enforce
                else ["m7_shadow_preview_only"]
            )
        ),
    }
    return dict(freeze_json_mapping(payload, "app_gate_graph_shadow"))


def attach_app_m7_gate_retirement_shadow(
    coverage_metadata: Mapping[str, Any] | None,
    *,
    graph: Mapping[str, Any] | None = None,
    observed_metrics: Mapping[str, Any] | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """写入 M7 Gate／工作台／退役合同；不改正式 Completion Gate SQL。"""

    policy = (
        dict(milestone_policy)
        if isinstance(milestone_policy, Mapping)
        else build_app_bounded_exploration_milestone_policy()
    )
    meta = dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    raw_override = meta.get("app_bounded_exploration_milestone_policy")
    if isinstance(raw_override, Mapping) and milestone_policy is None:
        policy = build_app_bounded_exploration_milestone_policy(raw_override)

    shadow = build_app_m7_gate_enforcement_shadow(
        coverage_metadata=meta,
        graph=graph,
        observed_metrics=observed_metrics,
        milestone_policy=policy,
    )
    meta["app_gate_graph_shadow"] = shadow
    return meta
