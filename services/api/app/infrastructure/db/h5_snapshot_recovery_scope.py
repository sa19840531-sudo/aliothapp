"""H5 新会话补采的数据库执行许可；不扩大普通 Web/App 的权限。"""


def h5_snapshot_request_constraints_sql(request: str = "request") -> str:
    """只有明确接受新会话 C 级逻辑等价证据的请求可以跨关闭会话。"""
    return f"""(
        {request}.session_dependency = 'snapshot'
        AND {request}.replay_policy = 'new_session_allowed'
        AND {request}.request_payload->>'required_equivalence' = 'logical_equivalent'
        AND {request}.request_payload->>'minimum_source_grade' IN ('C', 'D')
    )"""


def h5_snapshot_task_permission_sql(task: str = "task", capture: str = "capture") -> str:
    """只允许当前主 Run 的未封存 H5 任务恢复，旧 Run 和取消均拒绝。"""
    return f"""(
        lower({task}.target_platform) = 'h5'
        AND {task}.status IN ('running', 'review')
        AND {task}.finalization_state IN ('open', 'exploration_draining', 'session_draining')
        AND (
            {task}.finalization_agent_run_id = {capture}.agent_run_id
            OR ({task}.status = 'running' AND {task}.finalization_state = 'open'
                AND {task}.finalization_agent_run_id IS NULL)
        )
        AND EXISTS (SELECT 1 FROM qc_agent_run AS h5_main
            WHERE h5_main.id = {capture}.agent_run_id AND h5_main.task_id = {task}.id
              AND h5_main.agent_type = 'main' AND h5_main.status IN ('running', 'done', 'failed'))
    )"""


def h5_snapshot_session_permission_sql(
    *,
    task: str = "task",
    session_id: str = "session.session_id",
    owner: str = "session.session_owner",
    kind: str = "session.surface_kind",
    attempt_id: str | None = None,
) -> str:
    """会话必须对应已落库恢复尝试和仍有效的请求/Probe 执行许可。"""
    constraints = h5_snapshot_request_constraints_sql("h5_request")
    task_permission = h5_snapshot_task_permission_sql(task, "h5_capture")
    attempt_binding = f"AND h5_attempt.attempt_id = {attempt_id}" if attempt_id else ""
    return f"""EXISTS (
        SELECT 1 FROM qc_restoration_attempt AS h5_attempt
        INNER JOIN qc_evidence_request AS h5_request
            ON h5_request.request_id = h5_attempt.request_id
           AND h5_request.task_id = h5_attempt.task_id
           AND h5_request.finalization_epoch = h5_attempt.finalization_epoch
        INNER JOIN qc_capture AS h5_capture
            ON h5_capture.id = h5_attempt.original_capture_id
           AND h5_capture.task_id = h5_attempt.task_id
           AND h5_capture.surface_kind = 'web' AND h5_capture.capture_status = 'captured'
        INNER JOIN qc_surface_session AS h5_source
            ON h5_source.session_id = h5_capture.surface_session_id
           AND h5_source.session_id = h5_attempt.source_session_id
           AND h5_source.task_id = h5_attempt.task_id
           AND h5_source.finalization_epoch = h5_attempt.finalization_epoch
           AND h5_source.status IN ('closed', 'lost')
        WHERE h5_attempt.task_id = {task}.id
          {attempt_binding}
          AND h5_attempt.target_session_id = {session_id}
          AND h5_attempt.finalization_epoch = {task}.finalization_epoch
          AND h5_attempt.strategy = 'new_session_replay'
          AND h5_attempt.status IN ('running', 'equivalent')
          AND (h5_attempt.plan_payload->>'deadline')::timestamptz > now()
          AND {kind} = 'web'
          AND {owner} = 'h5-snapshot-recapture:' || h5_capture.agent_run_id::text
          AND {session_id} ~
              ('^h5:' || h5_capture.agent_run_id::text || ':recapture:[0-9a-f]{{20}}$')
          AND {task_permission} AND {constraints}
          AND ((h5_source.session_owner = 'h5-workflow'
                AND h5_source.session_id = 'h5:' || h5_capture.agent_run_id::text)
               OR (h5_source.session_owner = 'h5-browser-recovery:' || h5_capture.agent_run_id::text
                   AND h5_source.session_id ~ ('^h5:' || h5_capture.agent_run_id::text
                       || ':recovery:[0-9a-f]{{20}}$')))
          AND h5_request.expires_at > now()
          AND (
              (h5_request.status = 'pending' AND h5_request.lease_token IS NOT NULL
               AND h5_request.lease_expires_at > now())
              OR (h5_request.status IN ('routed', 'running') AND EXISTS (
                  SELECT 1 FROM qc_v2_work_item AS h5_probe
                  WHERE h5_probe.task_id = {task}.id
                    AND h5_probe.evidence_request_id = h5_request.request_id
                    AND h5_probe.finalization_epoch = {task}.finalization_epoch
                    AND h5_probe.work_type = 'probe'
                    AND h5_probe.status IN ('pending', 'leased', 'running', 'retry_wait')
                    AND h5_probe.work_payload->>'session_id' = {session_id}
              ))
          )
    )"""
