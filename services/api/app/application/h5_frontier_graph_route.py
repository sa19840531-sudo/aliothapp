"""H5 sibling 来源恢复复用页面图的可信路线，不生成补采请求。"""

import json
from dataclasses import dataclass

from services.api.app.application.evidence_recovery_replay import RecoveryGraphStep
from services.api.app.application.evidence_recovery_route import plan_evidence_recovery_route
from services.api.app.application.page_graph_projection import build_recovery_routes
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierEntry


@dataclass(frozen=True)
class H5FrontierGraphOutcome:
    """保留真实最新现场及完整路线回执，失败不能被页面相似度覆盖。"""

    capture: object
    detection: object
    arrived: bool
    failure_cause_code: str | None = None


def plan_h5_frontier_graph_steps(
    *, graph: dict, entry: H5TaskFrontierEntry, task_id: str, agent_run_id: str,
    session_id: str, finalization_epoch: int, current_capture_id: str,
) -> tuple[RecoveryGraphStep, ...]:
    """只读取同运行、同会话的可信低风险路径，仍由执行端逐步证明。"""
    scope = {"task_id": task_id, "agent_run_id": agent_run_id,
             "finalization_epoch": finalization_epoch}
    if (graph.get("status") != "available" or graph.get("target_platform") != "h5"
            or any(graph.get(key) != value for key, value in scope.items())):
        return ()
    states = {}
    for row in graph.get("states", []):
        if (any(row.get(key) != value for key, value in scope.items())
                or row.get("surface_session_id") != session_id
                or row.get("surface_kind") != "web"):
            continue
        payload = row.get("state_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                continue
        if not isinstance(payload, dict):
            continue
        states[str(row["state_id"])] = {**row, "state_payload": payload}
    target = states.get(entry.source_capture_id)
    if target is None or current_capture_id not in states:
        return ()
    target_identity = _identity(target)
    if (not target_identity
            or target_identity != entry.candidate.metadata.get("source_identity_fingerprint")):
        return ()
    scoped_graph = {**graph, "states": list(states.values())}
    projection = build_recovery_routes(
        scoped_graph, source_state_id=current_capture_id,
        target_identity=target.get("logical_page_key"),
    )
    paths = []
    for route in projection["routes"]:
        if route["target_state_id"] != entry.source_capture_id:
            continue
        current = current_capture_id
        fragile = 0
        for edge in route["steps"]:
            destination = states[str(edge["target_state_id"])]
            # 每步重新观察会产生新 Capture；只复用公共的同身份同 URL 换绑合同。
            single = build_recovery_routes(
                {**scoped_graph, "transitions": [edge]}, source_state_id=current,
                target_identity=destination["logical_page_key"],
            )
            # 投影会同时给出状态键相同但编号未绑定的路线，只采用显式换绑版本。
            single["routes"] = [candidate for candidate in single["routes"] if
                                str(edge["source_state_id"]) == current
                                or candidate.get("source_rebind", {}).get(
                                    "historical_source_state_id") == str(edge["source_state_id"])]
            plan = plan_evidence_recovery_route(
                {**scope, "protocol_version": "page_graph_recovery.v1", "target_platform": "h5",
                 **single}, **scope, source_state_id=current,
                target_state_id=str(destination["state_id"]),
                target_identity=destination["logical_page_key"],
            )
            if plan.route is None or plan.route.transition_ids != (str(edge["transition_id"]),):
                break
            fragile += len(plan.route.fragile_step_ids)
            current = str(destination["state_id"])
        else:
            paths.append((fragile, route))
    if not paths:
        return ()
    _, route = min(paths, key=lambda item: (
        bool(item[0]), item[0], len(item[1]["steps"]),
        tuple(str(edge["transition_id"]) for edge in item[1]["steps"]),
    ))
    result = []
    for edge in route["steps"]:
        source = states[str(edge["source_state_id"])]
        destination = states[str(edge["target_state_id"])]
        identities = [_identity(row) for row in (source, destination)]
        if not all(isinstance(value, str) and value.strip() for value in identities):
            return ()
        result.append(RecoveryGraphStep(
            transition_id=str(edge["transition_id"]), source_state_id=str(source["state_id"]),
            target_state_id=str(destination["state_id"]), action_kind=edge["action_kind"],
            semantic_target=edge.get("semantic_target"), source_identity=identities[0],
            target_identity=identities[1], source_state_fingerprint=source["state_fingerprint"],
            target_state_fingerprint=destination["state_fingerprint"],
        ))
    return tuple(result)


def _identity(state: dict) -> str | None:
    surface = state["state_payload"].get("logical_surface")
    return surface.get("identity_fingerprint") if isinstance(surface, dict) else None
