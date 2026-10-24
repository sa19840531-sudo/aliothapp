"""Web Coverage Gap 候选规则的共享 SQL 适用口径。"""

WEB_GUEST_GAP_RULE_APPLICABILITY_SQL = """
    rule.status = 'enabled'
    AND (rule.channel IS NULL OR rule.channel = task.channel)
    AND (
        jsonb_typeof(rule.input_contract->'target_clients') IS DISTINCT FROM 'array'
        OR jsonb_array_length(rule.input_contract->'target_clients') = 0
        OR rule.input_contract->'target_clients' ? 'WEB'
    )
    AND COALESCE(rule.input_contract->>'auth_requirement', '') <> 'authenticated'
    AND jsonb_typeof(rule.applicable_page_types) = 'array'
    AND rule.applicable_page_types ? missing.page_type
    AND COALESCE(
        rule.judge_config->'capability_config'->>'evidence_input_scope',
        'page'
    ) = 'page'
"""
