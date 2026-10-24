"""规则结果写入的合同来源；冻结规则仍须关联真实规则实体。"""


def frozen_h5_rule_identity_sql(*, task: str, rule: str, code: str, ruleset: str) -> str:
    """H5 按冻结编号关联实体，其余平台保持实时规则集归属和编号校验。"""
    return f"""(
        CASE WHEN {task}.target_platform = 'h5' AND {task}.task_config ? 'rule_inventory'
        THEN EXISTS (
            SELECT 1 FROM jsonb_array_elements(
                {task}.task_config #> '{{rule_inventory,rules}}'
            ) AS frozen_rule
            WHERE frozen_rule->>'rule_id' = {rule}.id::text
              AND frozen_rule->>'rule_code' = {code}
              AND frozen_rule->>'applicability' = 'applicable'
        )
        ELSE {rule}.ruleset_id = ANY(qc_ruleset_scope({ruleset}))
             AND {rule}.rule_code = {code}
        END
    )"""


def rule_execution_target_sql(*, frozen: bool) -> str:
    """保持原有结果写入参数及证据校验，只替换规则元数据来源。"""

    if frozen:
        return """
            SELECT task.id AS task_id, rule.id AS rule_id,
                   frozen_rule.rule_code, frozen_rule.executor_type, frozen_rule.severity
            FROM qc_task AS task
            CROSS JOIN LATERAL jsonb_to_recordset(task.task_config #> '{rule_inventory,rules}')
                AS frozen_rule(rule_id text, rule_code text, executor_type text,
                               severity text, applicability text, sort_order integer)
            INNER JOIN qc_rule AS rule ON rule.id::text = frozen_rule.rule_id
            WHERE task.id = $1::uuid
              AND frozen_rule.applicability = 'applicable'
              AND frozen_rule.rule_id = $2::uuid::text
              AND frozen_rule.rule_code = $3::varchar
            ORDER BY frozen_rule.sort_order ASC
            LIMIT 1
        """
    return """
        SELECT task.id AS task_id, rule.id AS rule_id,
               rule.rule_code, rule.executor_type, rule.severity
        FROM qc_task task
        JOIN qc_rule rule ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
        WHERE task.id = $1::uuid AND rule.status = 'enabled'
          AND (($2::uuid IS NOT NULL AND rule.id = $2::uuid)
               OR ($3::varchar IS NOT NULL AND rule.rule_code = $3))
        ORDER BY rule.sort_order ASC
        LIMIT 1
    """
