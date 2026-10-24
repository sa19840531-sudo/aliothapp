"""真实规则业务完成的 SQL 口径，与 task_progress 的完成资格同源。"""

from services.api.app.application.task_progress import (
    RULE_EVIDENCE_GAP_REASONS,
    RULE_NOT_EXECUTED_REASONS,
)
from services.api.app.infrastructure.db.rule_confirmation_sql import (
    requires_human_confirmation_sql,
)


def completed_rule_execution_sql(alias: str) -> str:
    """返回完成资格；通过、未通过、真实不适用及可确认业务复核计入，技术缺口不计入。"""
    column = f"{alias}." if alias else ""
    reasons = ", ".join(f"'{reason}'" for reason in sorted(RULE_NOT_EXECUTED_REASONS))
    evidence_gap_reasons = ", ".join(
        f"'{reason}'" for reason in sorted(RULE_EVIDENCE_GAP_REASONS)
    )
    return f"""(
        COALESCE({column}judge_input->>'bootstrap', 'false') <> 'true'
        AND lower(btrim(COALESCE({column}judge_output->>'reason', ''))) NOT IN ({reasons})
        AND (
            {column}execution_status <> 'skipped'
            OR (
                lower(btrim(COALESCE({column}error_code, ''))) NOT IN ({evidence_gap_reasons})
                AND lower(btrim(COALESCE({column}judge_output->>'error_code', '')))
                    NOT IN ({evidence_gap_reasons})
                AND lower(btrim(COALESCE({column}judge_output->>'reason', '')))
                    NOT IN ({evidence_gap_reasons})
            )
        )
        AND (
            {column}execution_status IN ('pass', 'fail', 'warning', 'skipped')
            OR {requires_human_confirmation_sql(alias)}
        )
    )"""
