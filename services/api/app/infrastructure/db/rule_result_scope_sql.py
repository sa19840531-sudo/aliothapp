"""按质检规则统一的规则结果口径（2026-09-17 立川拍板）。

一个任务的规则结果不分是哪个 Run 写的：探索主 Run、H5/App 独立判定 Run 写下的结论合在一起算，
同一任务、同一规则、同一页面在库里本来就只有一条。收口时给没真正判到的规则预置的占位
（没执行完、这一轮没匹配到页面、要登录才能判、覆盖缺口），只要同一条规则在任务里已经有真实结论
就不再单独算；没有真实结论时照常显示。报告、报告中心、任务详情、待确认数和收口判断都读这一段。

2026-09-22 补一类占位：底层页面被弹窗遮挡时展示层门禁拒绝判定（判定器没看过页面内容就记了
证据不足）。弹窗关掉后同一条规则在**同一页面类型**判出真实结论就顶掉它；别的页面的结论不算，
因为每页各自的封面、标记还是要各自看（沈钧铭验收 App 任务：3 期规则 56 条证据不足全是这种）。
"""

from services.api.app.application.rule_auth_gate import AUTH_REQUIREMENT_NOT_MET_REASON
from services.api.app.application.task_progress import RULE_NOT_EXECUTED_REASONS

RULE_PLACEHOLDER_REASONS = RULE_NOT_EXECUTED_REASONS | {
    "not_matched_by_run",
    AUTH_REQUIREMENT_NOT_MET_REASON,
}

# 展示层门禁「底层页面被遮挡」的拒绝码（domain/evidence/presentation.PresentationRejectionCode）
OCCLUDED_PLACEHOLDER_REASON_CODE = "BASE_PAGE_OCCLUDED"


def outside_current_h5_run_sql(alias: str) -> str:
    """H5 V2 排除旧主 Run，保留由当前主 Run 的 Capture 发起的独立判定结果。"""
    owner = """COALESCE(current_h5_task.finalization_agent_run_id, (
        SELECT current_main.id FROM qc_agent_run AS current_main
        WHERE current_main.task_id = current_h5_task.id AND current_main.agent_type = 'main'
        ORDER BY current_main.created_at DESC LIMIT 1
    ))"""
    return f"""EXISTS (
        SELECT 1 FROM qc_task AS current_h5_task
        WHERE current_h5_task.id = {alias}.task_id
          AND current_h5_task.target_platform = 'h5'
          AND LOWER(COALESCE(current_h5_task.task_config->>'workflow_version', '')) = 'v2'
          AND {alias}.agent_run_id IS DISTINCT FROM {owner}
          AND NOT EXISTS (
              SELECT 1 FROM qc_capture AS current_source_capture
              WHERE current_source_capture.id = {alias}.page_capture_id
                AND current_source_capture.agent_run_id = {owner}
          )
    )"""


def superseded_placeholder_sql(alias: str) -> str:
    """布尔 SQL：同任务同一条规则已有真实结论时，占位不再单独算。

    H5 V2 只认可当前主 Run 及其采集发起的判定，旧主 Run 不能替代本次缺口。
    其他平台保留任务级的跨判定 Run 口径。

    遮挡占位更严：真实结论必须落在同一页面类型上。
    """

    return f"""(
        {_placeholder_sql(alias)}
        AND EXISTS (
            SELECT 1
            FROM qc_rule_execution AS concluded
            WHERE concluded.task_id = {alias}.task_id
              AND concluded.rule_id = {alias}.rule_id
              AND NOT {_placeholder_sql("concluded")}
              AND NOT {outside_current_h5_run_sql("concluded")}
              AND (
                  NOT {_occluded_placeholder_sql(alias)}
                  OR {_capture_page_type_sql("concluded")} = {_capture_page_type_sql(alias)}
              )
        )
    )"""


def _placeholder_sql(alias: str) -> str:
    reasons = ", ".join(f"'{reason}'" for reason in sorted(RULE_PLACEHOLDER_REASONS))
    return f"""(
        (
            {alias}.execution_status = 'skipped'
            AND COALESCE({alias}.judge_input->>'bootstrap', 'false') = 'true'
        )
        OR COALESCE({alias}.judge_output->>'reason', '') IN ({reasons})
        OR {_occluded_placeholder_sql(alias)}
    )"""


def _occluded_placeholder_sql(alias: str) -> str:
    return f"""(
        {alias}.execution_status = 'evidence_insufficient'
        AND {alias}.judge_output #>> '{{presentation_rejection,reason_code}}'
            = '{OCCLUDED_PLACEHOLDER_REASON_CODE}'
    )"""


def _capture_page_type_sql(alias: str) -> str:
    """采集的页面类型：H5/Web 在 qc_page_capture.page_type，App 在 qc_capture.metadata。"""
    return f"""COALESCE(
        (
            SELECT scope_page_capture.page_type
            FROM qc_page_capture AS scope_page_capture
            WHERE scope_page_capture.id = {alias}.page_capture_id
        ),
        (
            SELECT scope_common_capture.metadata->>'page_type'
            FROM qc_capture AS scope_common_capture
            WHERE scope_common_capture.id = {alias}.page_capture_id
        )
    )"""


def rule_replacement_ctes_sql(scope_sql: str) -> str:
    """明细读取按任务批量计算被真实结论替代的执行，不改变写侧的逐条门禁。

    页型比较保留 SQL 的空值语义：遮挡占位只由同页型结论替代。占位表达式
    为 NULL 且替代存在时，原 NOT 谓词也为 NULL，因此同样要从明细排除。
    当前 H5 主 Run 和由其 Capture 产生的独立判定仍是唯一有效替代来源。
    """
    return f"""
                WITH replacement_task AS MATERIALIZED (
                    SELECT task.id, task.target_platform, task.task_config,
                           COALESCE(task.finalization_agent_run_id, (
                               SELECT main.id FROM qc_agent_run AS main
                               WHERE main.task_id = task.id AND main.agent_type = 'main'
                               ORDER BY main.created_at DESC LIMIT 1
                           )) AS owner_run_id
                    FROM qc_task AS task
                    WHERE task.id = $1::uuid
                      {scope_sql}
                ), replacement_facts AS MATERIALIZED (
                    SELECT execution.id AS execution_id, execution.rule_id,
                           {_placeholder_sql('execution')} AS is_placeholder,
                           {_occluded_placeholder_sql('execution')} AS is_occluded,
                           COALESCE(page.page_type, capture.metadata->>'page_type') AS page_type,
                           CASE WHEN task.target_platform = 'h5'
                                 AND LOWER(COALESCE(
                                     task.task_config->>'workflow_version', ''
                                 )) = 'v2'
                                THEN execution.agent_run_id IS NOT DISTINCT FROM task.owner_run_id
                                     OR (capture.agent_run_id = task.owner_run_id) IS TRUE
                                ELSE TRUE END AS current_conclusion
                    FROM qc_rule_execution AS execution
                    JOIN replacement_task AS task ON task.id = execution.task_id
                    LEFT JOIN qc_page_capture AS page ON page.id = execution.page_capture_id
                    LEFT JOIN qc_capture AS capture ON capture.id = execution.page_capture_id
                ), concluded_rules AS MATERIALIZED (
                    SELECT DISTINCT rule_id FROM replacement_facts
                    WHERE NOT is_placeholder AND current_conclusion
                ), concluded_page_types AS MATERIALIZED (
                    SELECT DISTINCT rule_id, page_type FROM replacement_facts
                    WHERE NOT is_placeholder AND current_conclusion AND page_type IS NOT NULL
                ), superseded_execution_ids AS MATERIALIZED (
                    SELECT facts.execution_id
                    FROM replacement_facts AS facts
                    WHERE facts.is_placeholder IS NOT FALSE
                      AND EXISTS (SELECT 1 FROM concluded_rules AS concluded
                                  WHERE concluded.rule_id = facts.rule_id)
                      AND (NOT facts.is_occluded OR EXISTS (
                          SELECT 1 FROM concluded_page_types AS concluded
                          WHERE concluded.rule_id = facts.rule_id
                            AND concluded.page_type = facts.page_type
                      ))
                )
    """
