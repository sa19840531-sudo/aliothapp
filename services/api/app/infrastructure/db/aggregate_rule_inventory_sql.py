"""聚合候选预期数量：App 冻结清单与历史规则目录分开取数。"""


def aggregate_expected_rule_count_sql(task_alias: str) -> str:
    """冻结后新增、删除、启停或改策略的在线规则不改变本任务聚合义务。"""
    return f"""CASE
        WHEN {task_alias}.target_platform IN ('app', 'h5')
         AND lower(COALESCE({task_alias}.task_config->>'workflow_version', '')) = 'v2'
         AND COALESCE({task_alias}.task_config->>'execution_mode', 'full_inspection')
             = 'full_inspection'
         AND {task_alias}.task_config ? 'rule_inventory'
        THEN (
            SELECT count(*)::integer
            FROM jsonb_array_elements(CASE
                WHEN jsonb_typeof({task_alias}.task_config #> '{{rule_inventory,rules}}') = 'array'
                THEN {task_alias}.task_config #> '{{rule_inventory,rules}}'
                ELSE '[]'::jsonb END) AS frozen(rule)
            WHERE frozen.rule->>'applicability' = 'applicable'
              AND COALESCE(
                  frozen.rule #>> '{{judge_config,capability_config,evidence_input_scope}}',
                  'page'
              ) IN ('journey', 'task', 'environment_matrix')
        )
        ELSE (
            SELECT count(rule.id)::integer FROM qc_rule AS rule
            WHERE rule.ruleset_id = ANY(qc_ruleset_scope({task_alias}.ruleset_id))
              AND rule.status = 'enabled'
              AND (rule.channel IS NULL OR rule.channel = {task_alias}.channel)
              AND COALESCE(
                  rule.judge_config->'capability_config'->>'evidence_input_scope', 'page'
              ) IN ('journey', 'task', 'environment_matrix')
        ) END"""
