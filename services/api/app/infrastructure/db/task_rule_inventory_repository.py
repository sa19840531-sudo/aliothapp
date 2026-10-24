"""规则全清单的原子冻结与同 Run 事实读取；不新增伪造判定记录。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from services.api.app.application.rule_auth_gate import (
    AUTH_REQUIREMENT_AUTHENTICATED,
    AUTH_REQUIREMENT_GUEST,
)
from services.api.app.application.task_rule_inventory import (
    TaskRuleInventoryError,
    build_task_rule_inventory,
    parse_task_rule_inventory,
    requires_task_rule_inventory,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.app_aggregate_dispositions import (
    read_app_aggregate_not_applicable_dispositions,
)
from services.api.app.infrastructure.db.capture_epoch import capture_epoch_sql
from services.api.app.infrastructure.db.pool import get_postgres_pool

_RULE_COLUMNS = """
    rule.id::text AS rule_id, rule.rule_code, rule.name, rule.description,
    rule.category, rule.severity, rule.executor_type, rule.scope, rule.page_type,
    rule.applicable_page_types, rule.url_pattern, rule.steps, rule.input_contract,
    rule.check_point, rule.judge_config, rule.manual_review_policy, rule.sort_order,
    rule.channel, rule.ruleset_id::text AS source_ruleset_id,
    rule_ruleset.ruleset_code, rule_ruleset.name AS ruleset_name
"""


async def read_frozen_task_rule_inventory_on_connection(
    connection: Any,
    *,
    task_id: str,
    access_scope: AccessScope | None = None,
) -> dict[str, Any] | None:
    """规则消费入口复用同一身份校验；旧任务无冻结清单沿用既有路径。"""

    scope_sql = ""
    args: list[Any] = [task_id]
    if access_scope is not None:
        scope_sql = """AND ($2::boolean OR (task.tenant_id=$3::uuid AND (
            $4::boolean OR task.submitter_id=$5::uuid OR task.department_name=$6::varchar)))"""
        args.extend(
            [
                access_scope.can_read_any,
                access_scope.tenant_id,
                access_scope.can_read_tenant,
                access_scope.actor_user_id,
                access_scope.department_scope,
            ]
        )
    task = await connection.fetchrow(
        f"""SELECT task.target_platform, task.channel, task.task_config,
                   task.ruleset_id::text AS ruleset_id
            FROM qc_task AS task WHERE task.id=$1::uuid {scope_sql}""",
        *args,
    )
    if task is None or task.get("target_platform") not in {"app", "h5", "web"}:
        return None
    config = _json(task["task_config"], {})
    if not requires_task_rule_inventory(task["target_platform"], config):
        return None
    inventory = parse_task_rule_inventory(config)
    if inventory is not None and (
        inventory["ruleset_id"] != task["ruleset_id"]
        or inventory["target_platform"] != task["target_platform"]
        or inventory["channel"] != task["channel"]
    ):
        raise TaskRuleInventoryError("规则清单与任务身份不一致")
    return inventory


async def freeze_task_rule_inventory_on_connection(
    connection: Any,
    *,
    target_platform: str,
    ruleset_code: str,
    ruleset_version: int,
    channel: str,
    task_config: Mapping[str, Any],
) -> dict[str, Any]:
    """调用方提交事务中冻结完整规则，服务端重建而不信任请求携带的清单。"""

    config = dict(task_config)
    config.pop("rule_inventory", None)
    config.pop("green_rules_contract", None)
    green_web_candidate = (
        target_platform == "web"
        and str(config.get("workflow_version") or "").casefold() == "v2"
        and config.get("execution_mode", "full_inspection") == "full_inspection"
    )
    if not green_web_candidate and not requires_task_rule_inventory(target_platform, config):
        return config
    row = await connection.fetchrow(
        f"""
        SELECT ruleset.id::text AS ruleset_id, ruleset.version AS ruleset_version,
               ruleset.snapshot_hash AS ruleset_snapshot_hash,
               COALESCE((
                   SELECT jsonb_agg(to_jsonb(frozen_rule) ORDER BY frozen_rule.sort_order,
                                                                  frozen_rule.rule_id)
                   FROM (SELECT {_RULE_COLUMNS}
                         FROM qc_rule AS rule
                         JOIN qc_ruleset AS rule_ruleset ON rule_ruleset.id = rule.ruleset_id
                         WHERE rule.ruleset_id = ANY(qc_ruleset_scope(ruleset.id))
                           AND rule.status = 'enabled') AS frozen_rule
               ), '[]'::jsonb) AS rules
        FROM qc_ruleset AS ruleset
        WHERE ruleset.ruleset_code = $1 AND ruleset.version = $2
          AND ruleset.status = 'enabled'
        FOR SHARE OF ruleset
        """,
        ruleset_code,
        ruleset_version,
    )
    if row is None:
        return config
    rules = _json(row["rules"], [])
    if green_web_candidate:
        from services.api.app.application.green_rule_catalog import (
            validate_green_rule_configuration,
        )

        # 仅实际选择新绿色合同的 Web 新任务扩展冻结；请求不能自行声明新合同。
        if not any(validate_green_rule_configuration(rule) is not None for rule in rules):
            return config
        config["green_rules_contract"] = "green-rule-contract.v2"
    inventory = build_task_rule_inventory(
        ruleset_id=str(row["ruleset_id"]),
        ruleset_version=int(row["ruleset_version"]),
        ruleset_snapshot_hash=row["ruleset_snapshot_hash"],
        target_platform=target_platform,
        channel=channel,
        task_config=config,
        rules=rules,
    )
    config["rule_inventory"] = inventory
    config["ruleset_rule_total"] = inventory["rule_count"]
    return config


class PostgresTaskRuleInventoryRepository:
    """只读归集逐规则应用清单及权威主 Run 的执行、工作项、登录事实。"""

    def __init__(self, *, pool: Any | None = None) -> None:
        self._pool = pool

    async def load_snapshot(
        self,
        *,
        task_id: str,
        agent_run_id: str | None = None,
        access_scope: AccessScope | None = None,
    ) -> dict[str, Any] | None:
        pool = self._pool or await get_postgres_pool()
        scope_sql = ""
        args: list[Any] = [task_id, agent_run_id]
        if access_scope is not None:
            scope_sql = """
                AND ($3::boolean OR (task.tenant_id = $4::uuid AND (
                    $5::boolean OR task.submitter_id = $6::uuid
                    OR task.department_name = $7::varchar)))
            """
            args.extend(
                [
                    access_scope.can_read_any,
                    access_scope.tenant_id,
                    access_scope.can_read_tenant,
                    access_scope.actor_user_id,
                    access_scope.department_scope,
                ]
            )
        async with (
            pool.acquire() as connection,
            connection.transaction(isolation="repeatable_read", readonly=True),
        ):
            task = await connection.fetchrow(
                f"""
                SELECT task.id::text AS task_id, task.task_config, task.target_platform,
                       task.channel, task.ruleset_id::text AS ruleset_id, task.created_at,
                       task.status AS task_status, task.finalization_state,
                       task.finalization_epoch AS task_finalization_epoch,
                       task.completion_gate_snapshot_id,
                       CASE WHEN task.finalization_state IN ('finalizing', 'sealed')
                                  AND bound_gate.gate_passed
                                  AND task.finalization_epoch > 0
                            THEN task.finalization_epoch - 1
                            ELSE task.finalization_epoch END AS finalization_epoch,
                       COALESCE(task.finalization_agent_run_id, (
                           SELECT owner.id FROM qc_agent_run AS owner
                           WHERE owner.task_id = task.id AND owner.agent_type = 'main'
                           ORDER BY owner.created_at DESC, owner.id DESC LIMIT 1
                       ))::text AS agent_run_id
                FROM qc_task AS task
                LEFT JOIN qc_completion_gate_snapshot AS bound_gate
                  ON bound_gate.task_id = task.id
                 AND bound_gate.gate_snapshot_id = task.completion_gate_snapshot_id
                 AND bound_gate.finalization_epoch = task.finalization_epoch
                WHERE task.id = $1::uuid {scope_sql}
                  AND ($2::uuid IS NULL OR EXISTS (
                      SELECT 1 FROM qc_agent_run AS requested
                      WHERE requested.id = $2::uuid AND requested.task_id = task.id
                  ))
                """,
                *args,
            )
            if task is None:
                return None
            result = dict(task)
            config = _json(task["task_config"], {})
            result["task_config"] = config
            inventory, status, reason = await _read_inventory(connection, task, config)
            result.update(
                inventory=inventory,
                inventory_status=status,
                unavailable_reason=reason,
                terminal=task["task_status"] in {"done", "failed", "canceled", "review"}
                or task["finalization_state"] == "sealed",
            )
            owner = result["agent_run_id"]
            # 请求历史 Run 不能借用当前主 Run 的结果补齐历史清单。
            if agent_run_id is not None and agent_run_id != owner:
                result.update(
                    inventory_status="unavailable", unavailable_reason="run_owner_mismatch"
                )
                result["inventory"] = None
                result.update(auth_gate=None, session_auth_states=[], executions=[], work_rules=[],
                              aggregate_dispositions=[])
                return result
            executions = await connection.fetch(
                f"""
                SELECT execution.id::text AS execution_id, execution.rule_id::text AS rule_id,
                       execution.agent_run_id::text AS agent_run_id, execution.execution_status,
                       execution.error_code, execution.judge_input, execution.judge_output,
                       execution.human_verdict, execution.page_capture_id::text AS page_capture_id
                       , COALESCE(aggregate_work.rule_contract_hash, (
                           SELECT member.contract_hash
                           FROM qc_inspection_work_item_rule AS member
                           JOIN qc_inspection_work_item AS work ON work.id = member.work_item_id
                           WHERE member.rule_execution_id = execution.id
                             AND member.rule_id = execution.rule_id
                             AND work.task_id = execution.task_id
                             AND work.page_capture_id = execution.page_capture_id
                             AND work.producer_agent_run_id = $2::uuid
                             AND work.finalization_epoch = $3
                             AND work.control_mode = 'enforced'
                             AND work.status <> 'superseded'
                           LIMIT 1
                       )) AS proof_contract_hash
                FROM qc_rule_execution AS execution
                LEFT JOIN qc_capture AS capture ON capture.id = execution.page_capture_id
                                               AND capture.task_id = execution.task_id
                LEFT JOIN qc_aggregate_inspection_work_item AS aggregate_work
                  ON aggregate_work.id = execution.aggregate_work_item_id
                 AND aggregate_work.task_id = execution.task_id
                 AND aggregate_work.rule_id = execution.rule_id
                WHERE execution.task_id = $1::uuid
                  AND ((capture.agent_run_id = $2::uuid
                        AND ({capture_epoch_sql("capture")}) = $3)
                       OR (aggregate_work.producer_agent_run_id = $2::uuid
                           AND aggregate_work.finalization_epoch = $3))
                  AND (NOT (execution.judge_input ? 'finalization_epoch')
                       OR execution.judge_input->>'finalization_epoch' = $3::text)
                """,
                task_id,
                owner,
                task["finalization_epoch"],
            )
            work_rules = await connection.fetch(
                """
                SELECT member.rule_id::text AS rule_id,
                       member.rule_execution_id::text AS execution_id,
                       work.id::text AS work_item_id, work.status AS work_status,
                       member.status AS rule_status, work.session_dependency,
                       member.last_error_code AS error_code, member.contract_hash
                FROM qc_inspection_work_item AS work
                JOIN qc_inspection_work_item_rule AS member ON member.work_item_id = work.id
                JOIN qc_capture AS capture ON capture.id = work.page_capture_id
                                          AND capture.task_id = work.task_id
                WHERE work.task_id = $1::uuid AND capture.agent_run_id = $2::uuid
                  AND work.finalization_epoch = $3
                  AND work.control_mode = 'enforced' AND work.status <> 'superseded'
                UNION ALL
                SELECT work.rule_id::text, NULL::text, work.id::text, work.status,
                       work.status, 'aggregate', work.last_error_code, work.rule_contract_hash
                FROM qc_aggregate_inspection_work_item AS work
                WHERE work.task_id = $1::uuid AND work.producer_agent_run_id = $2::uuid
                  AND work.finalization_epoch = $3 AND work.control_mode = 'enforced'
                  AND work.status <> 'superseded'
                """,
                task_id,
                owner,
                task["finalization_epoch"],
            )
            auth_rows = await connection.fetch(
                f"""
                SELECT DISTINCT
                       LOWER(COALESCE(metadata->>'session_auth', 'unknown')) AS session_auth
                FROM qc_capture AS capture
                WHERE task_id = $1::uuid AND agent_run_id = $2::uuid
                  AND ({capture_epoch_sql("capture")}) = $3
                """,
                task_id,
                owner,
                task["finalization_epoch"],
            )
            states = {row["session_auth"] for row in auth_rows}
            capture_facts = []
            if task["target_platform"] == "h5":
                capture_facts = await connection.fetch(
                    f"""
                    SELECT DISTINCT page.id::text AS capture_id, page.page_type,
                           page.url, COALESCE(page.page_state, 'stable') AS page_state
                    FROM qc_capture AS capture
                    JOIN qc_page_capture AS page ON page.id = capture.id
                                                AND page.task_id = capture.task_id
                    WHERE capture.task_id = $1::uuid AND capture.agent_run_id = $2::uuid
                      AND ({capture_epoch_sql("capture")}) = $3
                      AND capture.capture_role = 'after_action'
                      AND capture.capture_status = 'captured'
                      AND page.detection_status = 'recognized'
                      AND COALESCE(page.inspection_lifecycle_status, 'active') = 'active'
                      AND EXISTS (
                          SELECT 1 FROM qc_h5_evidence_inventory_item AS inventory
                          WHERE inventory.task_id = capture.task_id
                            AND inventory.agent_run_id = capture.agent_run_id
                            AND inventory.after_capture_id = capture.id
                            AND inventory.status = 'pass'
                      )
                    """,
                    task_id, owner, task["finalization_epoch"],
                )
            has_authenticated = bool(states & {"authenticated", "logged_in", "authed", "user"})
            has_guest = bool(states & {"guest", "anonymous", "visitor"})
            result.update(
                executions=[_decode_row(row) for row in executions],
                work_rules=[dict(row) for row in work_rules],
                aggregate_dispositions=(await read_app_aggregate_not_applicable_dispositions(
                    connection, task_id=task_id, inventory=inventory,
                    source_agent_run_id=owner, finalization_epoch=task["finalization_epoch"],
                    allow_sealed=True,
                )) if inventory is not None and task["target_platform"] == "app" else [],
                capture_facts=[dict(row) for row in capture_facts],
                session_auth_states=sorted(states),
                auth_gate=(
                    None
                    if has_authenticated and has_guest
                    else (
                        AUTH_REQUIREMENT_GUEST
                        if has_authenticated
                        else AUTH_REQUIREMENT_AUTHENTICATED
                    )
                ),
            )
            return result


async def _read_inventory(
    connection: Any, task: Mapping[str, Any], config: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str, str | None]:
    try:
        inventory = parse_task_rule_inventory(config)
    except TaskRuleInventoryError as exc:
        return None, "invalid", str(exc)
    if inventory is not None:
        if (
            inventory["ruleset_id"] != task["ruleset_id"]
            or inventory["target_platform"] != task["target_platform"]
            or inventory["channel"] != task["channel"]
        ):
            return None, "invalid", "ruleset_identity_mismatch"
        return inventory, "frozen", None
    if not requires_task_rule_inventory(task["target_platform"], config):
        return None, "unavailable", "inventory_not_required"
    total = config.get("ruleset_rule_total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        return None, "unavailable", "historical_rule_total_missing"
    # 历史重建必须能证明规则及组合规则集在任务创建后均未改动，且数量完整。
    row = await connection.fetchrow(
        f"""
        SELECT ruleset.version, ruleset.snapshot_hash,
               ruleset.updated_at <= $2 AS ruleset_unchanged,
               NOT EXISTS (SELECT 1 FROM qc_ruleset AS member
                           WHERE member.id = ANY(qc_ruleset_scope(ruleset.id))
                             AND (member.created_at > $2 OR member.updated_at > $2))
                   AS scope_unchanged,
               NOT EXISTS (SELECT 1 FROM qc_rule AS rule
                           WHERE rule.ruleset_id = ANY(qc_ruleset_scope(ruleset.id))
                             AND (rule.created_at > $2 OR rule.updated_at > $2)) AS rules_unchanged,
               COALESCE((SELECT jsonb_agg(to_jsonb(frozen_rule)) FROM (
                   SELECT {_RULE_COLUMNS} FROM qc_rule AS rule
                   JOIN qc_ruleset AS rule_ruleset ON rule_ruleset.id = rule.ruleset_id
                   WHERE rule.ruleset_id = ANY(qc_ruleset_scope(ruleset.id))
                     AND rule.status = 'enabled'
               ) AS frozen_rule), '[]'::jsonb) AS rules
        FROM qc_ruleset AS ruleset WHERE ruleset.id = $1::uuid
        """,
        task["ruleset_id"],
        task["created_at"],
    )
    if row is None or not all(
        row[key] for key in ("ruleset_unchanged", "scope_unchanged", "rules_unchanged")
    ):
        return None, "unavailable", "historical_rules_changed"
    rules = _json(row["rules"], [])
    if len(rules) != total:
        return None, "unavailable", "historical_rule_count_mismatch"
    return (
        build_task_rule_inventory(
            ruleset_id=task["ruleset_id"],
            ruleset_version=row["version"],
            ruleset_snapshot_hash=row["snapshot_hash"],
            target_platform=task["target_platform"],
            channel=task["channel"],
            task_config=config,
            rules=rules,
            source="legacy_verified",
        ),
        "legacy_verified",
        None,
    )


def _json(value: Any, fallback: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value if value is not None else fallback


def _decode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("judge_input", "judge_output"):
        result[key] = _json(result.get(key), {})
    return result
