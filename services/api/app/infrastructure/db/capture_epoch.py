"""从不可变采集记录或其实际动作关联读取原始阶段，不借用会话当前阶段。"""

import re


def capture_epoch_sql(alias: str = "capture") -> str:
    """生成内部查询表达式；只允许代码提供的简单 SQL 表别名。

    新采集在持久化时冻结 metadata 中的阶段。历史记录只有在实际关联
    到同任务、同会话的动作前/后采集时，才可使用该动作的固定阶段。
    元数据声明非法或与实际动作冲突时返回 NULL，不猜测当前阶段。
    """
    if not re.fullmatch(r"[a-z_][a-z_0-9]*", alias):
        raise ValueError("Capture SQL 别名非法")
    recorded = (
        f"CASE WHEN {alias}.metadata->>'finalization_epoch' ~ '^[0-9]{{1,9}}$' "
        f"THEN ({alias}.metadata->>'finalization_epoch')::integer ELSE NULL END"
    )
    linked = f"""(
        SELECT capture_action.finalization_epoch
        FROM qc_action_execution AS capture_action
        WHERE capture_action.action_execution_id = {alias}.action_execution_id
          AND capture_action.task_id = {alias}.task_id
          AND capture_action.session_id = {alias}.surface_session_id
          AND (
              ({alias}.capture_role = 'before_action'
               AND capture_action.source_capture_id = {alias}.id)
              OR ({alias}.capture_role = 'after_action'
                  AND capture_action.result_capture_id = {alias}.id)
          )
    )"""
    return f"""CASE
        WHEN {alias}.metadata ? 'finalization_epoch' THEN
            CASE WHEN {linked} IS NULL OR ({recorded}) = {linked}
                THEN ({recorded}) ELSE NULL END
        ELSE {linked}
    END"""
