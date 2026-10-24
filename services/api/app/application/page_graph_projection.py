"""页面图的只读树、进度和恢复路线投影；不产生页面到达或覆盖事实。"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Mapping
from typing import Any

from services.api.app.application.app_external_transition import (
    project_external_transitions_for_shadow,
)

_PENDING = {"pending", "selected", "leased", "running", "retry_wait", "planned"}


def build_exploration_tree(
    graph: dict[str, Any],
    *,
    progress_facts: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """只聚合已确认的业务页面；待访问入口与未知观察不伪装为页面节点。"""
    states = _scoped_states(graph)
    by_id = {str(state["state_id"]): state for state in states}
    page_nodes: dict[str, dict] = {}
    state_to_page: dict[str, str] = {}
    facts = {str(item["capture_id"]): item for item in progress_facts if item.get("capture_id")}
    for state in states:
        if not _business(state):
            continue
        identity = state["logical_page_key"]
        node = page_nodes.setdefault(
            identity,
            {
                "page_key": identity,
                "parent_page_key": None,
                "children": [],
                "states": {},
                "overlays": [],
                "targets": [],
                "transition_ids": [],
            },
        )
        version = node["states"].setdefault(
            state["state_fingerprint"],
            {
                "state_fingerprint": state["state_fingerprint"],
                "capture_ids": [],
                "latest_capture_id": None,
                "status": "visited",
                "reason_codes": [],
            },
        )
        capture_id = str(state["state_id"])
        version["capture_ids"].append(capture_id)
        version["latest_capture_id"] = capture_id
        version.update(_progress(facts.get(capture_id, {})))
        state_to_page[capture_id] = identity

    incoming: dict[str, list[str]] = defaultdict(list)
    for state in states:
        parent_id = str(state.get("parent_capture_id") or "")
        if parent_id in by_id:
            incoming[str(state["state_id"])].append(parent_id)
    cross_edges = []
    for edge in _scoped_edges(graph):
        source_id, target_id = str(edge.get("source_state_id")), str(edge.get("target_state_id"))
        if source_id not in by_id or target_id not in by_id:
            continue
        incoming[target_id].append(source_id)
        source_key, target_key = state_to_page.get(source_id), state_to_page.get(target_id)
        if source_key is not None:
            page_nodes[source_key]["transition_ids"].append(edge["transition_id"])
        if (
            not _trusted_edge(edge)
            or edge.get("counts_as_business_arrival") is not True
            or source_key is None
            or target_key is None
        ):
            continue
        if source_key == target_key:
            continue
        if page_nodes[target_key]["parent_page_key"] is not None or _would_cycle(
            page_nodes, source_key, target_key
        ):
            cross_edges.append(edge["transition_id"])
            continue
        page_nodes[target_key]["parent_page_key"] = source_key
        page_nodes[source_key]["children"].append(target_key)

    unattached_overlays = []
    unknown_observations = []
    for state in states:
        capture_id = str(state["state_id"])
        if state.get("is_overlay"):
            owner = _overlay_owner(capture_id, by_id, incoming, state_to_page)
            overlay = {
                "capture_id": capture_id,
                "page_state": state.get("page_state"),
                "state_payload": state.get("state_payload", {}),
            }
            if owner is None:
                unattached_overlays.append(overlay)
            else:
                page_nodes[owner]["overlays"].append(overlay)
        elif not _business(state):
            unknown_observations.append(capture_id)

    unassigned_targets = []
    for candidate in candidates:
        target = dict(candidate)
        status = candidate.get("status")
        target["progress_status"] = "unvisited" if status in _PENDING else status
        source_key = state_to_page.get(str(candidate.get("source_capture_id")))
        if source_key is None:
            unassigned_targets.append(target)
        else:
            page_nodes[source_key]["targets"].append(target)

    for node in page_nodes.values():
        node["states"] = list(node["states"].values())
        statuses = {state["status"] for state in node["states"]}
        pending_targets = any(target.get("status") in _PENDING for target in node["targets"])
        blocked_targets = any(
            target.get("status") in {"blocked", "failed"} for target in node["targets"]
        )
        node["status"] = (
            "needs_evidence"
            if "needs_evidence" in statuses
            else "blocked"
            if "blocked" in statuses or (blocked_targets and not pending_targets)
            else "completed"
            if statuses == {"completed"} and not pending_targets
            else "visited"
        )
    return {
        "protocol_version": "page_exploration_tree.v1",
        "source": "page_graph_read_projection",
        "roots": [key for key, node in page_nodes.items() if node["parent_page_key"] is None],
        "nodes": list(page_nodes.values()),
        "cross_transition_ids": cross_edges,
        "unattached_overlays": unattached_overlays,
        "unknown_observation_ids": unknown_observations,
        "unassigned_targets": unassigned_targets,
        "coverage_authority_changed": False,
    }


def build_recovery_routes(
    graph: dict[str, Any],
    *,
    source_state_id: str | None,
    target_identity: str | None,
    limit: int = 20,
) -> dict[str, Any]:
    """按连续状态生成有限最短路线，不把筛选出来的独立边冒充完整路径。"""
    if graph.get("finalization_epoch") is None:
        return {"status": "unavailable", "reason_code": "run_epoch_unknown", "routes": []}
    states = {str(row["state_id"]): row for row in _scoped_states(graph) if _business(row)}
    if source_state_id is not None and source_state_id not in states:
        return {"status": "unreachable", "reason_code": "source_state_not_in_run", "routes": []}
    adjacency: dict[tuple, list[dict]] = defaultdict(list)
    for edge in _scoped_edges(graph):
        source = states.get(str(edge.get("source_state_id")))
        target = states.get(str(edge.get("target_state_id")))
        if (
            source is None
            or target is None
            or not _trusted_edge(edge)
            or edge.get("replay_eligible") is not True
            or edge.get("risk_level") != "low"
            or not source.get("surface_session_id")
            or source.get("surface_session_id") != target.get("surface_session_id")
        ):
            continue
        adjacency[_state_key(source)].append(edge)
    for edges in adjacency.values():
        edges.sort(key=lambda item: str(item["transition_id"]))
    sources = [states[source_state_id]] if source_state_id is not None else list(states.values())
    routes, seen_routes = [], set()
    max_routes = min(max(limit, 1), 100)
    for requested_source in sources:
        route_sources = _recovery_route_sources(
            graph,
            requested_source=requested_source,
            states=states,
        )
        for source, source_rebind in route_sources:
            if len(routes) >= max_routes:
                break
            start = _state_key(source)
            queue = deque([(start, [])])
            seen = {start}
            while queue and len(routes) < max_routes:
                current, path = queue.popleft()
                if len(path) >= 100:
                    continue
                for edge in adjacency.get(current, []):
                    target = states[str(edge["target_state_id"])]
                    key = _state_key(target)
                    if key in seen:
                        continue
                    seen.add(key)
                    steps = [*path, edge]
                    if len(steps) <= 100:
                        queue.append((key, steps))
                    route_key = (
                        _state_key(requested_source),
                        key,
                        str(source.get("state_id")),
                    )
                    if (
                        target_identity is None
                        or target["logical_page_key"] == target_identity
                    ) and route_key not in seen_routes:
                        seen_routes.add(route_key)
                        route = {
                            "source_state_id": str(requested_source["state_id"]),
                            "target_state_id": str(target["state_id"]),
                            "target_page_key": target["logical_page_key"],
                            "steps": steps,
                        }
                        if source_rebind is not None:
                            route["source_rebind"] = source_rebind
                        routes.append(route)
                        if len(routes) >= max_routes:
                            break
        if len(routes) >= max_routes:
            break
    return {
        "status": "available" if routes else "unreachable",
        "routes": routes,
        "requires_stepwise_reverification": True,
        "route_policy": "同运行、同会话的可信低风险历史路线；执行时必须逐步重新观察和验证",
    }


def _recovery_route_sources(
    graph: dict[str, Any],
    *,
    requested_source: dict[str, Any],
    states: dict[str, dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """H5 可从同逻辑页面的历史可信状态重新绑定首步，其他平台仍要求精确状态。"""

    result: list[tuple[dict[str, Any], dict[str, Any] | None]] = [
        (requested_source, None)
    ]
    if graph.get("target_platform") != "h5" or not _business(requested_source):
        return result
    requested_session = requested_source.get("surface_session_id")
    requested_identity = requested_source.get("logical_page_key")
    requested_url = _canonical_state_url(requested_source)
    if requested_url is None:
        return result
    for candidate in states.values():
        if (
            candidate.get("state_id") == requested_source.get("state_id")
            or not _business(candidate)
            or candidate.get("surface_session_id") != requested_session
            or candidate.get("logical_page_key") != requested_identity
            or _canonical_state_url(candidate) != requested_url
        ):
            continue
        result.append(
            (
                candidate,
                {
                    "protocol_version": "h5-page-graph-source-rebind.v1",
                    "requested_source_state_id": str(requested_source["state_id"]),
                    "historical_source_state_id": str(candidate["state_id"]),
                    "logical_page_key": requested_identity,
                    "verified_by": "confirmed_logical_identity_and_canonical_url",
                },
            )
        )
    return result


def _canonical_state_url(state: dict[str, Any]) -> str | None:
    payload = state.get("state_payload")
    if not isinstance(payload, dict):
        return None
    value = payload.get("url")
    if not isinstance(value, str) or not value.strip():
        return None
    from services.api.app.domain.exploration.page_graph import canonical_url_identity

    return canonical_url_identity(value)


def _scoped_states(graph: dict) -> list[dict]:
    if not graph.get("task_id") or not graph.get("agent_run_id"):
        return []
    rows = [
        row
        for row in graph.get("states", [])
        if (
            row.get("task_id") == graph.get("task_id")
            and row.get("agent_run_id") == graph.get("agent_run_id")
            and row.get("finalization_epoch") == graph.get("finalization_epoch")
            and row.get("state_id")
            and row.get("logical_page_key")
            and row.get("state_fingerprint")
        )
    ]
    normalized_rows = []
    for row in rows:
        normalized_row = dict(row)
        normalized_row["state_payload"] = _state_payload_mapping(row.get("state_payload"))
        normalized_rows.append(normalized_row)
    return sorted(
        normalized_rows,
        key=lambda row: (str(row.get("captured_at") or ""), str(row["state_id"])),
    )


def _state_payload_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}



def _scoped_edges(graph: dict) -> list[dict]:
    return [
        edge
        for edge in graph.get("transitions", [])
        if (
            edge.get("task_id") == graph.get("task_id")
            and edge.get("agent_run_id") == graph.get("agent_run_id")
            and edge.get("finalization_epoch") == graph.get("finalization_epoch")
        )
    ]


def _business(state: dict) -> bool:
    payload = state.get("state_payload")
    if not isinstance(payload, dict):
        payload = {}
    admission = payload.get("web_evidence_admission")
    if isinstance(admission, dict) and admission.get("can_project_business_node") is not True:
        return False
    return (
        state.get("identity_status") == "confirmed"
        and state.get("counts_as_business_page") is True
        and not state.get("is_overlay")
        and not state.get("is_external")
    )


def _trusted_edge(edge: dict) -> bool:
    return edge.get("status") == "completed" and edge.get("proof") == "trusted"


def reconcile_coverage_with_page_graph(
    *,
    coverage_metadata: dict[str, Any] | None,
    graph: dict[str, Any] | None,
) -> dict[str, Any]:
    """影子对账：对比探索 Coverage 业务身份与图上可信到达页，不改 Gate／Coverage 权威。

    返回可写入审计的摘要；matched／only_in_* 仅供诊断与后续切读准备。
    """

    meta = coverage_metadata if isinstance(coverage_metadata, dict) else {}
    coverage_ids = {
        str(item).strip()
        for item in (meta.get("covered_business_state_ids") or ())
        if isinstance(item, str) and item.strip()
    }
    graph_ids: set[str] = set()
    overlay_count = 0
    external_count = 0
    if isinstance(graph, dict):
        for state in _scoped_states(graph):
            if state.get("is_overlay"):
                overlay_count += 1
                continue
            if state.get("is_external"):
                external_count += 1
                continue
            if not _business(state):
                continue
            key = state.get("logical_page_key")
            if isinstance(key, str) and key.strip():
                graph_ids.add(key.strip())
        for edge in _scoped_edges(graph):
            if not _trusted_edge(edge) or edge.get("counts_as_business_arrival") is not True:
                continue
            target_id = str(edge.get("target_state_id") or "")
            for state in _scoped_states(graph):
                if str(state.get("state_id")) != target_id:
                    continue
                if state.get("is_overlay") or state.get("is_external"):
                    continue
                key = state.get("logical_page_key")
                if isinstance(key, str) and key.strip():
                    graph_ids.add(key.strip())
    matched = sorted(coverage_ids & graph_ids)
    only_coverage = sorted(coverage_ids - graph_ids)
    only_graph = sorted(graph_ids - coverage_ids)
    return {
        "protocol_version": "app-coverage-page-graph-shadow-reconcile.v2",
        "coverage_authority_changed": False,
        "schedules_from_graph": False,
        "gate_consumes_graph": False,
        "matched_count": len(matched),
        "only_in_coverage_count": len(only_coverage),
        "only_in_graph_count": len(only_graph),
        "overlay_state_count": overlay_count,
        "external_state_count": external_count,
        "matched_page_keys": matched,
        "only_in_coverage": only_coverage,
        "only_in_graph": only_graph,
        "aligned": not only_coverage and not only_graph,
    }


_APP_PAGE_GRAPH_SHADOW_SNAPSHOT_VERSION = "app-page-graph-shadow-snapshot.v1"
_APP_PAGE_GRAPH_SHADOW_SCOPE = {
    "task_id": "app-shadow-dual-write",
    "agent_run_id": "app-shadow-dual-write",
    "finalization_epoch": 0,
}
_OVERLAY_PAGE_STATES = frozenset(
    {
        "popup",
        "modal",
        "overlay",
        "startup_ad",
        "countdown",
        "auth_gate",
        "login_required",
        "system_dialog",
        "permission_dialog",
    }
)


def build_app_page_graph_shadow_snapshot(
    *,
    coverage_metadata: dict[str, Any] | None,
    prior_graph: dict[str, Any] | None = None,
    current_frame: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从 Coverage 业务身份与当前帧双写影子图；Overlay／外跳只作诊断态。

    不产生正式到达边，不改 Coverage／Gate。状态幂等按 logical_page_key／state_id 合并。
    """

    meta = coverage_metadata if isinstance(coverage_metadata, dict) else {}
    frame = current_frame if isinstance(current_frame, dict) else {}
    states_by_id: dict[str, dict[str, Any]] = {}
    transitions: list[dict[str, Any]] = []
    scope = dict(_APP_PAGE_GRAPH_SHADOW_SCOPE)

    if isinstance(prior_graph, dict):
        for state in prior_graph.get("states") or ():
            if not isinstance(state, dict):
                continue
            sid = str(state.get("state_id") or state.get("logical_page_key") or "").strip()
            if not sid:
                continue
            merged = {**scope, **dict(state), **scope}
            states_by_id[sid] = merged
        for edge in prior_graph.get("transitions") or ():
            if isinstance(edge, dict):
                transitions.append({**scope, **dict(edge), **scope})

    for raw_key in meta.get("covered_business_state_ids") or ():
        key = str(raw_key).strip()
        if not key:
            continue
        existing = states_by_id.get(key)
        if existing and existing.get("counts_as_business_page") is True:
            continue
        states_by_id[key] = {
            **scope,
            "state_id": key,
            "logical_page_key": key,
            "state_fingerprint": "covered",
            "identity_status": "confirmed",
            "counts_as_business_page": True,
            "is_overlay": False,
            "is_external": False,
            "source": "coverage_dual_write_shadow",
        }

    capture_id = str(
        frame.get("capture_id")
        or meta.get("latest_capture_id")
        or meta.get("source_capture_id")
        or ""
    ).strip()
    page_state = str(frame.get("page_state") or "").strip().casefold()
    package_within = frame.get("package_within_boundary")
    if package_within is None:
        package_within = True
    overlay_signature = frame.get("overlay_signature")
    business_identity = str(
        frame.get("business_page_identity") or meta.get("business_page_identity") or ""
    ).strip()

    if capture_id and package_within is False:
        states_by_id[f"external:{capture_id}"] = {
            **scope,
            "state_id": f"external:{capture_id}",
            "logical_page_key": f"external:{capture_id}",
            "state_fingerprint": page_state or "external",
            "identity_status": "inferred",
            "counts_as_business_page": False,
            "is_overlay": False,
            "is_external": True,
            "page_state": page_state or None,
            "source": "current_frame_shadow",
        }
    elif capture_id and (page_state in _OVERLAY_PAGE_STATES or overlay_signature):
        states_by_id[f"overlay:{capture_id}"] = {
            **scope,
            "state_id": f"overlay:{capture_id}",
            "logical_page_key": f"overlay:{capture_id}",
            "state_fingerprint": page_state or "overlay",
            "identity_status": "inferred",
            "counts_as_business_page": False,
            "is_overlay": True,
            "is_external": False,
            "page_state": page_state or None,
            "overlay_signature": overlay_signature,
            "source": "current_frame_shadow",
        }
    elif (
        capture_id
        and business_identity
        and frame.get("stable_business_page") is True
        and page_state == "stable"
    ):
        states_by_id.setdefault(
            business_identity,
            {
                **scope,
                "state_id": business_identity,
                "logical_page_key": business_identity,
                "state_fingerprint": "stable",
                "identity_status": "confirmed",
                "counts_as_business_page": True,
                "is_overlay": False,
                "is_external": False,
                "source": "current_frame_shadow",
                "latest_capture_id": capture_id,
            },
        )

    external_transitions = project_external_transitions_for_shadow(
        meta.get("external_transitions")
    )
    episode_revisions = [
        dict(item)
        for item in (meta.get("episode_state_revisions") or ())
        if isinstance(item, dict) and item.get("entity") == "PageStateRevision"
    ]
    landing_observations = [
        dict(item)
        for item in (meta.get("landing_observations") or ())
        if isinstance(item, dict) and item.get("entity") == "LandingObservation"
    ]
    return {
        **scope,
        "protocol_version": _APP_PAGE_GRAPH_SHADOW_SNAPSHOT_VERSION,
        "status": "shadow_dual_write",
        "coverage_authority_changed": False,
        "schedules_from_graph": False,
        "gate_consumes_graph": False,
        "states": list(states_by_id.values()),
        "transitions": transitions,
        "external_transitions": external_transitions,
        "episode_state_revisions": episode_revisions,
        "landing_observations": landing_observations,
    }


def attach_app_coverage_page_graph_shadow(
    coverage_metadata: dict[str, Any] | None,
    *,
    graph: dict[str, Any] | None = None,
    current_frame: dict[str, Any] | None = None,
    milestone_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """双写影子图快照并对账；仅 M5／M7+SQL env 打开时提升调度／Gate 权威位。"""

    from services.api.app.application.app_bounded_exploration_milestones import (
        milestone_authority_enabled,
        milestone_shadow_enabled,
    )
    from services.api.app.application.app_lab_composition import (
        resolve_effective_app_milestone_policy,
    )

    meta = dict(coverage_metadata) if isinstance(coverage_metadata, dict) else {}
    policy = (
        milestone_policy
        if isinstance(milestone_policy, dict)
        else resolve_effective_app_milestone_policy()
    )
    shadow_enabled = milestone_shadow_enabled(policy, "M2")
    # M2 仅影子；调度权威在 M5，Gate／Coverage 权威在 M7 + SQL env。
    if milestone_authority_enabled(policy, "M2"):
        raise ValueError("M2 批次禁止打开页面图调度／Coverage 权威")
    schedules_from_graph = milestone_authority_enabled(policy, "M5")
    gate_sql = bool(policy.get("production_gate_sql_consumes_graph") is True)
    gate_authority = bool(
        milestone_authority_enabled(policy, "M7") and gate_sql
    )

    snapshot = (
        build_app_page_graph_shadow_snapshot(
            coverage_metadata=meta,
            prior_graph=graph if isinstance(graph, dict) else None,
            current_frame=current_frame,
        )
        if shadow_enabled
        else {
            "protocol_version": _APP_PAGE_GRAPH_SHADOW_SNAPSHOT_VERSION,
            "status": "shadow_disabled",
            "coverage_authority_changed": False,
            "schedules_from_graph": False,
            "gate_consumes_graph": False,
            "states": [],
            "transitions": [],
        }
    )
    shadow = reconcile_coverage_with_page_graph(
        coverage_metadata=meta,
        graph=snapshot,
    )
    # 权威位仅由里程碑／SQL env 打开；调用方不得改写。
    shadow["coverage_authority_changed"] = gate_authority
    shadow["schedules_from_graph"] = schedules_from_graph
    shadow["gate_consumes_graph"] = gate_authority
    shadow["dual_write"] = shadow_enabled
    shadow["milestone"] = "M2"
    meta["app_page_graph_snapshot"] = snapshot
    meta["app_coverage_page_graph_shadow"] = shadow
    return meta


def _state_key(state: dict) -> tuple:
    return state.get("surface_session_id"), state["logical_page_key"], state["state_fingerprint"]


def _progress(fact: dict) -> dict:
    evidence, inspection = fact.get("evidence_status"), fact.get("inspection_status")
    status = "visited"
    if evidence in {"insufficient", "failed"}:
        status = "needs_evidence"
    elif fact.get("blocked") is True:
        status = "blocked"
    elif (
        evidence == "complete"
        and inspection == "completed"
        and fact.get("exploration_finished") is True
    ):
        status = "completed"
    return {
        "status": status,
        "reason_codes": list(fact.get("reason_codes") or []),
        "evidence_status": evidence or "unknown",
        "inspection_status": inspection or "unknown",
    }


def _would_cycle(nodes: dict, parent: str, child: str) -> bool:
    seen = set()
    while parent is not None and parent not in seen:
        if parent == child:
            return True
        seen.add(parent)
        parent = nodes[parent]["parent_page_key"]
    return False


def _overlay_owner(
    capture_id: str, states: dict, incoming: dict, state_to_page: dict
) -> str | None:
    session = states[capture_id].get("surface_session_id")
    queue, seen = deque(incoming.get(capture_id, [])), {capture_id}
    while queue:
        source = queue.popleft()
        if source in seen or states[source].get("surface_session_id") != session:
            continue
        seen.add(source)
        if source in state_to_page:
            return state_to_page[source]
        queue.extend(incoming.get(source, []))
    return None
