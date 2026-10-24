"""质检范围的 SQL 片段，与 ``domain.task.inspection_scope`` 同源。

范围存在 ``task_config.inspection_scope.page_types``；没有这个配置或是空数组表示不限制。
``task_config_sql`` 传任务配置的 SQL 表达式：已连 qc_task 的写 ``task.task_config``，
没连的用 ``task_config_of_sql`` 按任务 ID 取。
"""


def task_config_of_sql(task_id_sql: str) -> str:
    """按任务 ID 取任务配置的标量子查询，给没有连 qc_task 的查询用。"""

    return (
        "(SELECT scoped_task.task_config FROM qc_task AS scoped_task "
        f"WHERE scoped_task.id = {task_id_sql})"
    )


def page_type_in_inspection_scope_sql(task_config_sql: str, page_type_sql: str) -> str:
    """布尔 SQL：页面类型在任务的质检范围内（范围为空时恒为真）。"""

    scope = f"({task_config_sql} #> '{{inspection_scope,page_types}}')"
    return (
        f"(COALESCE(jsonb_array_length({scope}), 0) = 0 OR {scope} ? lower(btrim({page_type_sql})))"
    )


def rule_in_inspection_scope_sql(task_config_sql: str, rule_alias: str) -> str:
    """布尔 SQL：规则挂的页面类型和质检范围有交集（范围为空时恒为真）。"""

    scope = f"({task_config_sql} #> '{{inspection_scope,page_types}}')"
    return (
        f"(COALESCE(jsonb_array_length({scope}), 0) = 0 "
        f"OR {rule_alias}.applicable_page_types ?| "
        f"ARRAY(SELECT jsonb_array_elements_text({scope})))"
    )
