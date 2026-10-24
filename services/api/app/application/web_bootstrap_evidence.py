"""仅认证 Web 从零启动的唯一无父入口；普通动作绝不继承此例外。"""


def trusted_web_bootstrap_actions(snapshot, captures, actions):
    run_id = snapshot.get("agent_run_id")
    session_id = snapshot.get("session_id")
    epoch = snapshot.get("finalization_epoch")
    if (
        snapshot.get("target_platform") != "web"
        or snapshot.get("workflow_version") != "v2"
        or snapshot.get("session_owner") != "web-v2-workflow"
        or not run_id
        or session_id != "web:" + run_id
        or type(epoch) is not int
        or epoch < 0
    ):
        return frozenset()
    first = [row for row in actions.values() if row.get("command_sequence") == 1]
    if len(first) != 1:
        return frozenset()
    action = first[0]
    action_id = action.get("action_execution_id")
    before = captures.get(action.get("source_capture_id"), {})
    after = captures.get(action.get("result_capture_id"), {})
    if (
        action.get("intent_id") != "web-bootstrap:" + run_id
        or action.get("intent_source_capture_id") != "web-entry:" + run_id
        or action.get("intent_surface_kind") != "web"
        or action.get("intent_task_id") != snapshot.get("task_id")
        or action.get("intent_session_id") != session_id
        or action.get("initial_surface_action") is not True
        or action.get("action_type") != "navigate"
        or action.get("web_capture_stage") != "web_entry_probe"
        or action.get("requested_source_capture_id") is not None
        or action.get("finalization_epoch") != epoch
        or before.get("url") != "about:blank"
        or before.get("parent_capture_id") is not None
        or before.get("page_parent_capture_id") is not None
        or before.get("page_type") is not None
        or before.get("capture_role") != "before_action"
        or after.get("capture_role") != "after_action"
        or after.get("parent_capture_id") != before.get("capture_id")
        or after.get("page_parent_capture_id") != before.get("capture_id")
        or not str(after.get("url") or "").startswith(("https://", "http://"))
    ):
        return frozenset()
    for capture in (before, after):
        if (
            capture.get("task_id") != snapshot.get("task_id")
            or capture.get("agent_run_id") != run_id
            or capture.get("surface_session_id") != session_id
            or capture.get("surface_kind") != "web"
            or capture.get("capture_status") != "captured"
            or capture.get("session_command_sequence") != 1
            or capture.get("action_execution_id") != action_id
            or capture.get("finalization_epoch") != epoch
            or capture.get("page_finalization_epoch") != epoch
        ):
            return frozenset()
    return frozenset({action_id})
