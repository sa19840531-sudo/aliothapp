"""沿既有任务权限与只读事务读取绿色第4版八条正式结果，不写业务表。"""

from services.api.app.application.green_rule_result_projection import (
    green_result_identity,
    object_value,
    project_green_subitems,
    validate_green_execution,
)
from services.api.app.application.task_rule_inventory import parse_task_rule_inventory
from services.api.app.domain.evidence import EvidenceInputSet
from services.api.app.infrastructure.db.evidence_input_set_repository import _member_from_row
from services.api.app.infrastructure.db.green_process_evidence import load_green_process_evidence
from services.api.app.infrastructure.db.green_rule_result_materials import (
    read_green_result_materials,
)
from services.api.app.infrastructure.db.journey_epoch import closed_journey_epoch_is_bound


async def read_green_rule_results(
    connection, *, task_id, execution_ids, artifact_store, scope_sql="", scope_args=()
):
    task_row = await connection.fetchrow(
        f"""SELECT task.id::text AS task_id,task.task_config,task.ruleset_id::text AS ruleset_id,
                   task.target_platform,task.channel,task.finalization_epoch,
                   COALESCE(task.finalization_agent_run_id,(SELECT run.id FROM qc_agent_run run
                     WHERE run.task_id=task.id AND run.agent_type='main'
                     ORDER BY run.created_at DESC,run.id DESC LIMIT 1))::text AS source_run_id
            FROM qc_task task WHERE task.id=$1::uuid {scope_sql}""",
        task_id,
        *scope_args,
    )
    if task_row is None:
        return {}
    task = dict(task_row)
    try:
        inventory = parse_task_rule_inventory(object_value(task["task_config"]))
    except (ValueError, TypeError, KeyError):
        return {}
    if inventory is None or any(
        inventory[key] != task[key] for key in ("ruleset_id", "target_platform", "channel")
    ):
        return {}
    rules = {}
    for rule in inventory["rules"]:
        try:
            if green_result_identity(rule) is not None:
                rules[rule["rule_id"]] = rule
        except (ValueError, TypeError, KeyError):
            continue
    if not rules:
        return {}
    rows = await connection.fetch(
        """SELECT to_jsonb(execution) AS execution_payload,to_jsonb(packet) AS packet_payload,
                  to_jsonb(review) AS review_payload,consumer.task_id::text AS consumer_task_id,
                  to_jsonb(aggregate_work) AS aggregate_work_payload,
                  to_jsonb(aggregate_attempt) AS aggregate_attempt_payload,
                  to_jsonb(member) AS page_member_payload,to_jsonb(work) AS page_work_payload,
                  to_jsonb(attempt) AS page_attempt_payload,inputs.input_set_payload,
                  inputs.input_set_hash AS stored_input_hash
           FROM qc_rule_execution execution
           LEFT JOIN qc_agent_run consumer ON consumer.id=execution.agent_run_id
           LEFT JOIN qc_observation_packet packet ON packet.id=execution.observation_packet_id
           LEFT JOIN qc_review_item review ON review.rule_execution_id=execution.id
             AND review.task_id=execution.task_id
           LEFT JOIN qc_aggregate_inspection_work_item aggregate_work
             ON aggregate_work.id=execution.aggregate_work_item_id
           LEFT JOIN qc_aggregate_inspection_attempt aggregate_attempt
             ON aggregate_attempt.work_item_id=aggregate_work.id
             AND aggregate_attempt.attempt_no=aggregate_work.attempt_count
           LEFT JOIN qc_inspection_work_item_rule member ON member.rule_execution_id=execution.id
             AND member.rule_id=execution.rule_id
           LEFT JOIN qc_inspection_work_item work ON work.id=member.work_item_id
           LEFT JOIN qc_inspection_attempt attempt ON attempt.work_item_id=work.id
             AND attempt.attempt_no=work.attempt_count
           LEFT JOIN qc_evidence_input_set inputs
             ON inputs.input_set_id=execution.evidence_input_set_id
           WHERE execution.task_id=$1::uuid AND execution.id=ANY($2::uuid[])
           ORDER BY execution.id""",
        task_id,
        execution_ids,
    )
    projections, duplicate_ids = {}, set()
    for raw in rows:
        row = dict(raw)
        execution = object_value(row["execution_payload"])
        rule = rules.get(execution["rule_id"])
        if rule is None:
            continue
        identity = green_result_identity(rule)
        execution_id = execution["id"]
        if execution_id in projections:
            duplicate_ids.add(execution_id)
            continue
        projections[execution_id] = identity
        try:
            work = object_value(row["aggregate_work_payload"] or row["page_work_payload"])
            epoch = work["finalization_epoch"]
            if epoch != task["finalization_epoch"] and not await closed_journey_epoch_is_bound(
                connection,
                task_id=task_id,
                agent_run_id=task["source_run_id"],
                epoch=epoch,
            ):
                raise ValueError("旧周期没有正式Gate精确绑定")
            read_task = {**task, "read_epoch": epoch}
            if row.get("input_set_payload"):
                payload = object_value(row["input_set_payload"])
                members = await connection.fetch(
                    """SELECT ordinal,capture_id::text AS capture_id,capture_role,page_sequence,
                              manifest_id,manifest_version,manifest_hash,artifact_ids,
                              observation_packet_ids,journey_execution_id,environment_profile_id
                       FROM qc_evidence_input_set_member
                       WHERE input_set_id=$1 ORDER BY ordinal""",
                    payload["input_set_id"],
                )
                row["stored_members"] = [_member_from_row(member).to_dict() for member in members]
            validated = validate_green_execution(task=read_task, rule=rule, row=row)
            try:
                verified_originals = None
                if validated["contract"]["input_kind"] != "page":
                    evidence = await load_green_process_evidence(
                        connection,
                        input_set=EvidenceInputSet.from_dict(
                            object_value(row["input_set_payload"])
                        ),
                        artifact_store=artifact_store,
                        allow_closed_epoch=epoch != task["finalization_epoch"],
                    )
                    wrapper = object_value(
                        object_value(execution["judge_output"])["wrapper_payload"]
                    )
                    if evidence.fingerprint != wrapper["green"]["evidence_fingerprint"]:
                        raise ValueError("正式Action/原件读取指纹与当次判定已变化")
                    verified_originals = {
                        original.artifact_id: original.payload
                        for capture in evidence.captures
                        for original in capture.originals
                    }
                artifacts = await read_green_result_materials(
                    connection,
                    task=read_task,
                    row=row,
                    validated=validated,
                    artifact_store=artifact_store,
                    verified_originals=verified_originals,
                )
            except (ValueError, TypeError, KeyError):
                identity.update(
                    linkage_status="evidence_changed",
                    linkage_notice="正式原件或最新清单已变化，暂不沿用分项展示",
                )
                continue
            subitems = project_green_subitems(validated, artifacts)
            identity.update(
                linkage_status="linked",
                linkage_notice=None,
                execution_status=execution["execution_status"],
                review_status=validated["review_status"],
                review_reason=validated["review_reason"],
                missing_evidence=validated["missing_evidence"],
                subitems=subitems,
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    for execution_id in duplicate_ids:
        projections[execution_id].update(
            linkage_status="input_mismatch",
            linkage_notice="正式结果关联不唯一，暂不展示分项结论",
            execution_status=None,
            review_status=None,
            review_reason=None,
            missing_evidence=[],
            subitems=[],
        )
    return projections
