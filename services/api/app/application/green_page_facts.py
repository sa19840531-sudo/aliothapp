"""页面共享事实与统一结果适配，沿用原观察包和原件权限。"""

from services.api.app.application.green_fact_bindings import bind_green_fact_views
from services.api.app.application.green_rule_evidence import reduce_green_subitems


async def page_fact_views(*, bundle, definition, request, packet, evidence_reader):
    config = definition.judge_config.get("green_shared_bindings")
    if config is None:
        return ()
    identity = packet.packet_payload.get("identity", {})
    platform, actions, issues = identity.get("target_platform"), {}, ()
    current, seen = evidence_reader, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        method = getattr(current, "read_green_fact_context", None)
        if method is not None:
            platform, actions, issues = await method(
                task_id=request.task_id,
                capture_roles=bundle.capture_roles,
                source_agent_run_id=request.source_agent_run_id
                or identity.get("source_agent_run_id"),
            )
            break
        current = getattr(current, "inner", getattr(current, "_inner", None))
    else:
        if definition.judge_config["green_rule_contract"]["rule_key"] == "41":
            issues = ("green_page_formal_action_reader_missing",)
    sources = tuple(
        (
            item.page_capture_id,
            item.capture_role,
            item.artifact_id,
            item.artifact_type,
            item.payload,
        )
        for item in bundle.artifacts
    )
    return bind_green_fact_views(
        sources,
        config,
        issues=issues,
        action_ids=tuple(actions),
        target_platform=platform,
        formal_actions=actions,
    )


def page_outcome(items, views, *, reason="共享判定已按本次冻结对象逐项完成"):
    status, missing = reduce_green_subitems(items)
    allowed = {artifact for view in views for artifact in view.artifact_ids}
    by_capture = {}
    for view in views:
        owners = view.get("artifact_capture_ids", {})
        for artifact in view.artifact_ids:
            capture = owners.get(artifact)
            if capture in view.capture_ids:
                by_capture.setdefault(capture, set()).add(artifact)
    subitems = []
    for item in items:
        refs = set().union(*(by_capture.get(capture, set()) for capture in item.capture_ids))
        subitems.append(
            {
                "key": item.key,
                "status": item.status,
                "evaluated": item.status != "evidence_insufficient",
                "confidence": 1.0 if item.status in {"pass", "fail"} else None,
                "reason": item.reason,
                "missing": list(item.missing),
                "evidence_refs": sorted(refs & allowed),
            }
        )
    return {
        "execution_status": status,
        "confidence": 1.0 if status in {"pass", "fail"} else 0.0,
        "reason": reason,
        "missing_evidence": list(missing),
        "subitems": subitems,
        "legacy_visual_fallback_allowed": False,
    }
