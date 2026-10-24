"""人工确认口径的 SQL 片段，与 ``task_progress.rule_requires_human_confirmation`` 同源。

仅已执行且业务结论待复核的规则需要人工确认，缺证、未执行和平台故障不能人工补成业务结论。
通过、未通过、本产品不适用直接生效。任务是否停在「待复核」、人工确认后能否转完成、
报告列表的待确认数都读这一段，改口径只改这里和 task_progress。
"""

from services.api.app.application.task_progress import RULE_NOT_EXECUTED_REASONS


def requires_human_confirmation_sql(alias: str) -> str:
    """返回一个布尔 SQL 表达式：该条规则执行结果是否需要人工确认（未含 human_verdict 判空）。"""
    column = f"{alias}." if alias else ""
    reasons = ", ".join(f"'{reason}'" for reason in sorted(RULE_NOT_EXECUTED_REASONS))
    platform_pattern = (
        "^(capability_|deterministic_dependency_|judge_provider_|"
        "llm_provider_|platform_|semantic_provider_)"
    )
    platform_codes = (
        "'prompt_input_token_budget', 'semantic_batch_single_failed', 'semantic_judge_unavailable'"
    )
    return f"""(
        {column}execution_status = 'manual_review'
        AND COALESCE({column}judge_input->>'bootstrap', 'false') <> 'true'
        AND lower(btrim(COALESCE({column}judge_output->>'reason', ''))) NOT IN ({reasons})
        -- 模型线路或平台能力错误产生的转人工，没有可确认的业务结论
        AND NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements_text(
                CASE WHEN jsonb_typeof({column}judge_output->'model_output'->'conflicts') = 'array'
                THEN {column}judge_output->'model_output'->'conflicts' ELSE '[]'::jsonb END
            ) AS diagnostic(code)
            WHERE lower(btrim(diagnostic.code)) ~ '{platform_pattern}'
               OR lower(btrim(diagnostic.code)) IN ({platform_codes})
        )
        AND lower(btrim(COALESCE({column}judge_output->>'reason', ''))) !~ '{platform_pattern}'
        AND lower(btrim(COALESCE({column}judge_output->>'reason', ''))) NOT IN ({platform_codes})
        AND COALESCE({column}result_summary, '') NOT LIKE '%语义规则判定超过独立墙钟预算%'
        AND COALESCE({column}result_summary, '') NOT LIKE '%模型 API 返回 HTTP 500%'
        AND COALESCE({column}result_summary, '') NOT LIKE '%质检平台尚未配置错别字词典或白名单%'
    )"""
