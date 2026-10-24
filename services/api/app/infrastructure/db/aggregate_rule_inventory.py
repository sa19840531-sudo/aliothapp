"""聚合写入复用任务冻结合同；旧任务及非 APP 保持原仓储校验。"""

from typing import Any

from services.api.app.application.aggregate_rule_config import (
    resolve_rule_evidence_input_configuration,
)
from services.api.app.application.inspection_work_items import build_inspection_rule_contract_hash
from services.api.app.application.task_rule_inventory import TaskRuleInventoryError
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    read_frozen_task_rule_inventory_on_connection,
)


async def read_frozen_aggregate_rule(
    connection: Any,
    *,
    task_id: str,
    rule_id: str,
    rule_code: str,
    contract_hash: str,
    scope: str,
    scope_key: str,
    source_agent_run_id: str,
    finalization_epoch: int,
    ruleset_snapshot_hash: str | None = None,
) -> dict[str, Any] | None:
    """冻结清单存在时严格匹配输入合同，不因 live 规则编辑切回旧路径。"""

    inventory = await read_frozen_task_rule_inventory_on_connection(connection, task_id=task_id)
    if inventory is None:
        return None
    valid_owner = await connection.fetchval(
        """SELECT EXISTS (
               SELECT 1 FROM qc_task AS task
               JOIN qc_agent_run AS producer
                 ON producer.task_id=task.id AND producer.id=$2::uuid
               WHERE task.id=$1::uuid AND task.finalization_agent_run_id=producer.id
                 AND task.finalization_epoch=$3
                 AND task.status IN ('running','review')
                 AND task.finalization_state='session_draining'
           )""",
        task_id, source_agent_run_id, finalization_epoch,
    )
    if not valid_owner:
        raise TaskRuleInventoryError("冻结聚合规则的 Task/Run/阶段或 Epoch 不匹配")
    rules = [rule for rule in inventory["rules"] if rule["rule_id"] == rule_id]
    if len(rules) != 1:
        raise TaskRuleInventoryError("聚合规则不在任务冻结清单中")
    rule = rules[0]
    evidence = resolve_rule_evidence_input_configuration(
        rule.get("judge_config") or {}, require_executable=False,
    )
    if (
        rule["applicability"] != "applicable"
        or rule["rule_code"] != rule_code
        or build_inspection_rule_contract_hash(rule) != contract_hash
        or evidence.scope.value != scope
        or evidence.scope.value == "page"
        or evidence.scope_key != scope_key
        or (
            ruleset_snapshot_hash is not None
            and inventory["ruleset_snapshot_hash"] != ruleset_snapshot_hash
        )
    ):
        raise TaskRuleInventoryError("聚合输入与任务冻结规则合同不一致")
    return {**rule, "_ruleset_snapshot_hash": inventory["ruleset_snapshot_hash"]}
