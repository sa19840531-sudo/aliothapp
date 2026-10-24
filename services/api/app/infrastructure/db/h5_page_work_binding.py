"""普通 H5 Work 在首次发布时绑定已有页面 InputSet，不改写历史成员。"""

import json

from services.api.app.application.observation_packet import ObservationPacketValidationError
from services.api.app.infrastructure.db.h5_producer_result_reuse import read_verified_page_input
from services.api.app.infrastructure.db.task_rule_inventory_sql import frozen_h5_rule_identity_sql


async def insert_h5_page_work_rule(connection, *, work_item_id, rule):
    work = await connection.fetchrow(
        "SELECT work.*, task.target_platform FROM qc_inspection_work_item work "
        "JOIN qc_task task ON task.id=work.task_id WHERE work.id=$1::uuid",
        work_item_id,
    )
    if work is None or work["target_platform"] != "h5":
        return None
    inputs = await read_verified_page_input(
        connection,
        input_set_id=rule.evidence_input_set_id,
        packet_runs=tuple(
            str(run)
            for run in (
                work["producer_agent_run_id"],
                work["consumer_agent_run_id"],
            )
            if run
        ),
    )
    if inputs is None or (
        inputs.task_id != str(work["task_id"])
        or inputs.source_agent_run_id != str(work["producer_agent_run_id"])
        or inputs.eligible_capture_ids != (str(work["page_capture_id"]),)
        or inputs.finalization_epoch != work["finalization_epoch"]
        or inputs.ruleset_snapshot_hash != work["ruleset_snapshot_hash"]
        or inputs.rule_id != rule.rule_id
        or inputs.rule_code != rule.rule_code
        or inputs.rule_contract_hash != rule.contract_hash
    ):
        return None
    return await connection.fetchval(
        """
        INSERT INTO qc_inspection_work_item_rule AS existing
            (work_item_id,rule_id,rule_code,ordinal,contract_hash,evidence_input_set_id)
        SELECT work.id,rule.id,$3,$4,$5,$6 FROM qc_inspection_work_item work
        JOIN qc_task task ON task.id=work.task_id JOIN qc_rule rule ON rule.id=$2::uuid
        WHERE work.id=$1::uuid AND __RULE_IDENTITY__
        ON CONFLICT ON CONSTRAINT uq_qc_inspection_work_item_rule
        DO UPDATE SET updated_at=existing.updated_at
        WHERE existing.evidence_input_set_id=EXCLUDED.evidence_input_set_id
          AND existing.contract_hash=EXCLUDED.contract_hash AND existing.ordinal=EXCLUDED.ordinal
        RETURNING id::text
        """.replace(
            "__RULE_IDENTITY__",
            frozen_h5_rule_identity_sql(
                task="task",
                rule="rule",
                code="$3::varchar",
                ruleset="work.ruleset_id",
            ),
        ),
        work_item_id,
        rule.rule_id,
        rule.rule_code,
        rule.ordinal,
        rule.contract_hash,
        rule.evidence_input_set_id,
    )


async def bind_h5_page_results(connection, *, work_item_id, lease_token):
    """只关联当前 Attempt 实际消费的输入，不给旧执行记录补造血缘。"""
    from services.api.app.infrastructure.db.h5_page_binding_reader import (
        PostgresH5PageBindingReader,
    )
    from services.api.app.infrastructure.db.inspection_work_item_repository import (
        _load_rule_evidence_bindings,
    )

    rows = await connection.fetch(
        """
        SELECT work.task_id::text, work.producer_agent_run_id::text,
               work.consumer_agent_run_id::text, attempt.id::text AS attempt_id,
               member.rule_id::text, member.contract_hash, execution.id::text AS execution_id,
               execution.evidence_input_set_id, packet.packet_payload, packet.evidence_artifact_ids
        FROM qc_inspection_work_item work
        JOIN qc_task task ON task.id=work.task_id AND task.target_platform='h5'
        JOIN qc_inspection_work_item_rule member ON member.work_item_id=work.id
            AND member.evidence_input_set_id IS NOT NULL AND member.status IN ('pending','judged')
        JOIN qc_inspection_attempt attempt ON attempt.work_item_id=work.id
            AND attempt.attempt_no=work.attempt_count AND attempt.status='running'
            AND attempt.lease_token=work.lease_token
        JOIN qc_rule_execution execution ON execution.task_id=work.task_id
            AND execution.page_capture_id=work.page_capture_id AND execution.rule_id=member.rule_id
            AND execution.agent_run_id=work.consumer_agent_run_id
        JOIN qc_observation_packet packet ON packet.id=execution.observation_packet_id
            AND packet.created_by_agent_run_id=work.consumer_agent_run_id
            AND packet.task_id=work.task_id AND packet.page_capture_id=work.page_capture_id
            AND packet.rule_id=member.rule_id
        WHERE work.id=$1::uuid AND work.status='running' AND work.lease_token=$2::uuid
            AND work.lease_expires_at>now()
            AND (execution.execution_status NOT IN ('pass','fail')
                 OR packet.quality_gate_status='pass')
        FOR UPDATE OF work, member, attempt, execution
        """,
        work_item_id,
        lease_token,
    )
    bindings = {item.rule_id: item for item in await _load_rule_evidence_bindings(
        connection, work_item_id=work_item_id,
    )}
    verified = []
    for row in rows:
        payload = (
            json.loads(row["packet_payload"])
            if isinstance(row["packet_payload"], str)
            else row["packet_payload"]
        )
        marker = payload.get("identity", {}).get("original_page_input")
        if (
            not isinstance(marker, dict)
            or any(
                marker.get(key) != value
                for key, value in {
                    "work_item_id": work_item_id,
                    "attempt_id": row["attempt_id"],
                    "consumer_agent_run_id": row["consumer_agent_run_id"],
                    "rule_contract_hash": row["contract_hash"],
                }.items()
            )
            or row["evidence_input_set_id"] not in {None, marker.get("input_set_id")}
        ):
            continue
        binding = bindings.get(row["rule_id"])
        source = binding.page_capture_id if binding else await connection.fetchval(
            "SELECT capture_id::text FROM qc_evidence_input_set_member "
            "WHERE input_set_id=$1 AND ordinal=1",
            marker.get("input_set_id"),
        )
        try:
            inputs = await PostgresH5PageBindingReader().load_authorized(
                task_id=row["task_id"],
                page_capture_id=source,
                source_agent_run_id=(binding.producer_agent_run_id if binding
                                     else row["producer_agent_run_id"]),
                input_set_id=marker.get("input_set_id"),
                rule_id=row["rule_id"],
                rule_contract_hash=row["contract_hash"],
                work_item_id=work_item_id,
                attempt_id=row["attempt_id"],
                consumer_agent_run_id=row["consumer_agent_run_id"],
                lease_token=lease_token,
                connection=connection,
            )
        except ObservationPacketValidationError:
            continue
        if (
            marker.get("input_set_hash") != inputs.input_set_hash
            or marker.get("manifest_id") != inputs.members[0].manifest_id
        ):
            continue
        artifacts = (
            json.loads(row["evidence_artifact_ids"])
            if isinstance(row["evidence_artifact_ids"], str)
            else row["evidence_artifact_ids"]
        )
        required_artifacts = binding.artifact_ids if binding else inputs.members[0].artifact_ids
        if not isinstance(artifacts, list) or not set(required_artifacts).issubset(
            artifacts
        ):
            continue
        await connection.execute(
            "UPDATE qc_rule_execution SET evidence_input_set_id=$2 WHERE id=$1::uuid "
            "AND (evidence_input_set_id IS NULL OR evidence_input_set_id=$2)",
            row["execution_id"],
            inputs.input_set_id,
        )
        verified.append(row["execution_id"])
    return verified
