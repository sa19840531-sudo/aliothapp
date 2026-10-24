"""任务终结时只恢复已被当前 Web Work 合同明确纳入的真实来源。"""

from services.api.app.application.rule_execution import RuleExecutionLeaseConflictError
from services.api.app.infrastructure.db.web_sequence_result_projection import (
    _SCOPE_SQL,
    _object,
    _scopes,
    _source,
)

WEB_FINALIZATION_RECOVERY_EXCLUSION_SQL = (
    "AND NOT EXISTS (SELECT 1 FROM ("
    + _SCOPE_SQL.replace("work.id=$1::uuid", "work.id=work_item.id")
    + ") web_scope WHERE web_scope.rule_id=member.rule_id::text)"
)


async def recover_web_sources_for_finalization(connection, task_id):
    candidates = await connection.fetch(
        "SELECT id::text FROM qc_inspection_work_item WHERE task_id=$1::uuid AND control_mode='enforced' "
        "AND status NOT IN ('succeeded','partial','failed','cancelled','superseded')",
        task_id,
    )
    for candidate in candidates:
        scopes = await _scopes(connection, candidate["id"])
        if not scopes:
            continue
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task_id}"
        )
        for scope in await _scopes(connection, candidate["id"]):
            if scope["authorized"] is not True:
                continue
            execution = await connection.fetchrow(
                "SELECT id::text,judge_output FROM qc_rule_execution WHERE task_id=$1::uuid "
                "AND rule_id=$2::uuid AND page_capture_id=$3::uuid AND source_idempotency_key IS NULL",
                task_id,
                scope["rule_id"],
                scope["page_capture_id"],
            )
            if execution is None:
                continue
            provenance = _object(execution["judge_output"]).get("web_composite_provenance", {})
            if any(
                provenance.get(key) != scope[key]
                for key in (
                    "task_id",
                    "producer_agent_run_id",
                    "finalization_epoch",
                    "page_capture_id",
                    "rule_id",
                )
            ):
                continue
            for source in provenance.get("sources", []):
                if any(
                    source.get(key) != scope[key]
                    for key in (
                        "work_item_id",
                        "rule_id",
                        "contract_hash",
                        "evidence_input_set_id",
                    )
                ):
                    continue
                if (
                    await _source(
                        connection,
                        scope,
                        source["attempt_id"],
                        execution_status=source["execution_status"],
                        packet_id=source["observation_packet_id"],
                    )
                    != source
                ):
                    raise RuleExecutionLeaseConflictError("Web 任务终结恢复来源无法复验")
                status = (
                    source["execution_status"]
                    if source["execution_status"]
                    in (
                        "error",
                        "skipped",
                        "evidence_insufficient",
                    )
                    else "written"
                )
                await connection.execute(
                    "UPDATE qc_inspection_work_item_rule SET status=$3,observation_packet_id=$4::uuid,"
                    "rule_execution_id=$5::uuid,updated_at=now() WHERE work_item_id=$1::uuid "
                    "AND rule_id=$2::uuid AND status IN ('pending','judged')",
                    scope["work_item_id"],
                    scope["rule_id"],
                    status,
                    source["observation_packet_id"],
                    execution["id"],
                )
                break
