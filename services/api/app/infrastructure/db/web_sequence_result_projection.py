"""真实 Web 同锚点多用途的审计投影；原 Attempt 判定保持不变。"""

import json
from dataclasses import replace

from services.api.app.application.rule_execution import RuleExecutionLeaseConflictError


def _object(value):
    return json.loads(value) if isinstance(value, str) else value


_SCOPE_SQL = """
SELECT work.id::text AS work_item_id, work.task_id::text, work.producer_agent_run_id::text,
    work.page_capture_id::text, work.finalization_epoch, member.rule_id::text, member.rule_code,
    member.contract_hash, member.evidence_input_set_id, work.consumer_agent_run_id::text,
    (task.status='running' AND task.finalization_epoch=work.finalization_epoch
     AND task.finalization_state IN ('open','exploration_draining','session_draining')
     AND producer.status IN ('running','done','failed','aborted')
     AND COALESCE(producer.output_context->>'cancel_requested','false')='false'
     AND producer.id=(SELECT latest.id FROM qc_agent_run latest WHERE latest.task_id=task.id
         AND latest.agent_type='main' ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)) AS authorized
FROM qc_inspection_work_item work
JOIN qc_inspection_work_item_rule member ON member.work_item_id=work.id
JOIN qc_task task ON task.id=work.task_id AND task.target_platform='web'
    AND task.task_config->>'workflow_version'='v2'
    AND task.task_config->>'execution_mode'='full_inspection'
    AND task.task_config->>'web_inspection_notification_mode'='enforced'
JOIN qc_agent_run producer ON producer.id=work.producer_agent_run_id AND producer.task_id=task.id
    AND producer.agent_type='main'
WHERE work.id=$1::uuid AND work.control_mode='enforced' AND work.session_dependency='snapshot'
    AND work.strategy='llm'
    AND EXISTS (
        SELECT 1 FROM qc_inspection_work_item sibling
        JOIN qc_inspection_work_item_rule bound ON bound.work_item_id=sibling.id
            AND bound.rule_id IN (SELECT participant.rule_id FROM qc_inspection_work_item_rule participant
                WHERE participant.work_item_id=work.id)
        JOIN qc_evidence_input_set inputs ON inputs.input_set_id=bound.evidence_input_set_id
            AND inputs.task_id=sibling.task_id AND inputs.source_agent_run_id=sibling.producer_agent_run_id
            AND inputs.finalization_epoch=sibling.finalization_epoch AND inputs.rule_id=bound.rule_id
            AND inputs.rule_contract_hash=bound.contract_hash AND inputs.scope='web_sequence'
        JOIN qc_web_evidence_sequence sequence ON sequence.sequence_id=inputs.web_evidence_sequence_id
            AND sequence.task_id=inputs.task_id AND sequence.source_agent_run_id=inputs.source_agent_run_id
            AND sequence.finalization_epoch=inputs.finalization_epoch AND sequence.anchor_capture_id=sibling.page_capture_id
        JOIN qc_surface_session session ON session.session_id=sequence.session_id AND session.task_id=task.id
            AND session.session_id='web:'||producer.id::text AND session.session_owner='web-v2-workflow'
            AND session.surface_kind='web'
        WHERE sibling.task_id=work.task_id AND sibling.producer_agent_run_id=work.producer_agent_run_id
            AND sibling.finalization_epoch=work.finalization_epoch AND sibling.page_capture_id=work.page_capture_id
            AND sibling.control_mode='enforced' AND sibling.session_dependency='snapshot' AND sibling.strategy='llm'
    )
"""

# 原恢复 SQL 先排除真实同源序列用途，随后才由授权审计投影进行关联。
WEB_COMPOSITE_RECOVERY_EXCLUSION_SQL = (
    "AND NOT EXISTS (SELECT 1 FROM ("
    + _SCOPE_SQL
    + ") web_scope WHERE web_scope.rule_id=member.rule_id::text)"
)


async def _scopes(connection, work_item_id):
    payload = await connection.fetchval(
        "SELECT jsonb_agg(to_jsonb(scoped)) FROM (" + _SCOPE_SQL + ") scoped", work_item_id
    )
    return _object(payload) or []


async def lock_web_projection_scope(connection, command):
    """普通旧 Work 没有序列同源成员时不进入新分支。"""
    if not command.work_item_id:
        return None
    rows = await _scopes(connection, command.work_item_id)
    candidates = [
        row
        for row in rows
        if row["rule_id"] == command.rule_id or row["rule_code"] == command.rule_code
    ]
    if not candidates:
        return None
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{command.task_id}"
    )
    rows = await _scopes(connection, command.work_item_id)
    scope = next(
        (
            row
            for row in rows
            if row["rule_id"] == command.rule_id or row["rule_code"] == command.rule_code
        ),
        None,
    )
    if (
        scope is None
        or scope["authorized"] is not True
        or scope["task_id"] != command.task_id
        or scope["page_capture_id"] != command.page_capture_id
    ):
        raise RuleExecutionLeaseConflictError("Web 多用途正式投影当前周期未授权")
    return dict(scope)


async def _source(connection, scope, attempt_id, *, execution_status, packet_id):
    row = await connection.fetchrow(
        "SELECT result_payload,consumer_agent_run_id::text,status FROM qc_inspection_attempt "
        "WHERE id=$1::uuid AND work_item_id=$2::uuid AND (status IN ('judged','written','retryable_error') "
        "OR (status='terminal_error' AND error_code='WEB_RULE_CONTRACT_INCOMPATIBLE'))",
        attempt_id,
        scope["work_item_id"],
    )
    if row is None:
        raise RuleExecutionLeaseConflictError("Web 多用途投影缺少已持久的原 Attempt")
    payload = _object(row["result_payload"])
    decisions = [
        item for item in payload.get("decisions", []) if item.get("rule_id") == scope["rule_id"]
    ]
    if len(decisions) != 1:
        raise RuleExecutionLeaseConflictError("Web 多用途原 Attempt 规则不唯一")
    decision = decisions[0]
    if (
        payload.get("task_id") != scope["task_id"]
        or payload.get("work_item_id") != scope["work_item_id"]
        or payload.get("attempt_id") != attempt_id
        or decision.get("observation_packet_id") != packet_id
        or decision.get("work_item_id") != scope["work_item_id"]
        or decision.get("attempt_id") != attempt_id
        or (
            decision.get("execution_status") != execution_status
            and execution_status not in {"manual_review", "error", "evidence_insufficient"}
        )
    ):
        raise RuleExecutionLeaseConflictError("Web 多用途命令与原判定身份不一致")
    evidence_ids = decision.get("evidence_artifact_ids") or []
    if packet_id is not None:
        packet = await connection.fetchrow(
            "SELECT packet_payload,evidence_artifact_ids,quality_gate_status "
            "FROM qc_observation_packet WHERE id=$1::uuid AND task_id=$2::uuid AND page_capture_id=$3::uuid "
            "AND rule_id=$4::uuid AND created_by_agent_run_id=$5::uuid",
            packet_id,
            scope["task_id"],
            scope["page_capture_id"],
            scope["rule_id"],
            row["consumer_agent_run_id"],
        )
        if packet is None or not set(evidence_ids).issubset(
            _object(packet["evidence_artifact_ids"])
        ):
            raise RuleExecutionLeaseConflictError("Web 原判定 Packet 或附件归属不一致")
        if (
            execution_status in {"pass", "fail", "warning"}
            and packet["quality_gate_status"] != "pass"
        ):
            raise RuleExecutionLeaseConflictError("Web 原判定 Packet 齐全性未通过")
        if scope["evidence_input_set_id"] is not None:
            binding = _object(packet["packet_payload"]).get("web_evidence", {})
            if (
                binding.get("input_set_id") != scope["evidence_input_set_id"]
                or binding.get("rule_contract_hash") != scope["contract_hash"]
            ):
                raise RuleExecutionLeaseConflictError("Web 原判定 Packet 的逐规则绑定不一致")
    elif evidence_ids or execution_status in {"pass", "fail", "warning"}:
        raise RuleExecutionLeaseConflictError("Web 原判定缺少 Packet 来源")
    source = {
        key: scope[key]
        for key in ("work_item_id", "rule_id", "contract_hash", "evidence_input_set_id")
    } | {
        "attempt_id": attempt_id,
        "observation_packet_id": packet_id,
        "execution_status": execution_status,
        "evidence_artifact_ids": evidence_ids,
        "consumer_agent_run_id": row["consumer_agent_run_id"],
    }
    if row["status"] == "terminal_error":
        from services.api.app.infrastructure.db.web_sequence_committed_source import (
            assert_committed_terminal_source,
        )

        # 原 payload/Packet 已按普通路径逐字段复验，还必须证明这一条真正写入而非整批推定。
        await assert_committed_terminal_source(connection, scope, source)
    return source


def _rank(status):
    # 选择一份最保守的真实判定，不把未知或待人工与通过合成为通过。
    return {
        "fail": 6,
        "warning": 5,
        "error": 4,
        "evidence_insufficient": 3,
        "manual_review": 3,
        "skipped": 2,
        "pass": 1,
    }.get(status, 4)


async def project_web_canonical(connection, command, scope):
    current = await _source(
        connection,
        scope,
        command.attempt_id,
        execution_status=command.execution_status,
        packet_id=command.observation_packet_id,
    )
    if current["evidence_artifact_ids"] != command.evidence_artifact_ids:
        raise RuleExecutionLeaseConflictError("Web 当前命令附件不同于原 Attempt，拒绝用旧结果遮盖")
    if current["consumer_agent_run_id"] != command.agent_run_id:
        raise RuleExecutionLeaseConflictError("Web 多用途当前消费者与原 Attempt 不一致")
    existing = await connection.fetchrow(
        "SELECT * FROM qc_rule_execution WHERE task_id=$1::uuid "
        "AND rule_id=$2::uuid AND page_capture_id=$3::uuid AND source_idempotency_key IS NULL FOR UPDATE",
        command.task_id,
        scope["rule_id"],
        command.page_capture_id,
    )
    sources = []
    chosen = current
    projected = command
    if existing is not None:
        provenance = _object(existing["judge_output"]).get("web_composite_provenance")
        if provenance is None:
            # 先前稳定用途已执行、之后才封存序列时，必须回溯真实已写 Attempt。
            prior = await connection.fetchrow(
                "SELECT a.id::text,a.work_item_id::text FROM qc_inspection_attempt a "
                "JOIN qc_inspection_work_item w ON w.id=a.work_item_id JOIN qc_inspection_work_item_rule m ON m.work_item_id=w.id "
                "WHERE w.task_id=$1::uuid AND w.producer_agent_run_id=$2::uuid AND w.finalization_epoch=$3 "
                "AND w.page_capture_id=$4::uuid AND m.rule_id=$5::uuid AND m.rule_execution_id=$6::uuid "
                "AND a.status='written' AND a.result_payload->'decisions' @> $7::jsonb ORDER BY a.created_at LIMIT 1",
                scope["task_id"],
                scope["producer_agent_run_id"],
                scope["finalization_epoch"],
                scope["page_capture_id"],
                scope["rule_id"],
                existing["id"],
                json.dumps([{"observation_packet_id": str(existing["observation_packet_id"])}]),
            )
            if prior is None:
                raise RuleExecutionLeaseConflictError(
                    "Web 已有正式结果缺少可认证原 Attempt，拒绝覆盖"
                )
            prior_scopes = await _scopes(connection, prior["work_item_id"])
            prior_scope = next(
                (row for row in prior_scopes if row["rule_id"] == scope["rule_id"]), None
            )
            if prior_scope is None:
                from services.api.app.infrastructure.db.web_sequence_legacy_source import (
                    read_bound_legacy_scope,
                )

                prior_scope = await read_bound_legacy_scope(
                    connection,
                    scope,
                    work_item_id=prior["work_item_id"],
                    attempt_id=prior["id"],
                    execution_id=str(existing["id"]),
                    packet_id=str(existing["observation_packet_id"])
                    if existing["observation_packet_id"]
                    else None,
                )
            sources = [
                await _source(
                    connection,
                    prior_scope,
                    prior["id"],
                    execution_status=existing["execution_status"],
                    packet_id=str(existing["observation_packet_id"])
                    if existing["observation_packet_id"]
                    else None,
                )
            ]
        else:
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
                raise RuleExecutionLeaseConflictError("Web 已有组合来源跨任务、Run 或周期")
            sources = list(provenance.get("sources", []))
            for source in sources:
                source_scopes = await _scopes(connection, source["work_item_id"])
                source_scope = next(
                    (row for row in source_scopes if row["rule_id"] == scope["rule_id"]), None
                )
                if source_scope is None:
                    from services.api.app.infrastructure.db.web_sequence_legacy_source import (
                        read_bound_legacy_scope,
                    )

                    source_scope = await read_bound_legacy_scope(
                        connection,
                        scope,
                        work_item_id=source["work_item_id"],
                        attempt_id=source["attempt_id"],
                        execution_id=str(existing["id"]),
                        packet_id=source["observation_packet_id"],
                    )
                if (
                    source_scope is None
                    or any(
                        source_scope[key] != scope[key]
                        for key in (
                            "task_id",
                            "producer_agent_run_id",
                            "finalization_epoch",
                            "page_capture_id",
                            "rule_id",
                        )
                    )
                    or await _source(
                        connection,
                        source_scope,
                        source["attempt_id"],
                        execution_status=source["execution_status"],
                        packet_id=source["observation_packet_id"],
                    )
                    != source
                ):
                    raise RuleExecutionLeaseConflictError("Web 已有组合来源不能重放认证")
        if not sources or len(sources) > 64:
            raise RuleExecutionLeaseConflictError("Web 组合来源闭包缺失或超出审计上限")
        if any(source["contract_hash"] != current["contract_hash"] for source in sources):
            from services.api.app.application.web_sequence_errors import (
                WebRuleContractIncompatibleError,
            )

            raise WebRuleContractIncompatibleError(
                "WEB_RULE_CONTRACT_INCOMPATIBLE：原始判定保留，拒绝合并不同冻结规则标准",
                command=command,
            )
        if _rank(existing["execution_status"]) >= _rank(command.execution_status):
            chosen = next(
                (
                    item
                    for item in sources
                    if item["observation_packet_id"]
                    == (
                        str(existing["observation_packet_id"])
                        if existing["observation_packet_id"]
                        else None
                    )
                    and item["execution_status"] == existing["execution_status"]
                ),
                None,
            )
            if chosen is None:
                raise RuleExecutionLeaseConflictError("Web canonical 与原材料来源不一致")
            fields = (
                "execution_status",
                "judge_strategy",
                "confidence",
                "severity",
                "result_summary",
                "problem_desc",
                "review_status",
                "review_reason",
                "error_code",
                "error_message",
                "started_at",
                "finished_at",
            )
            projected = replace(
                command,
                **{key: existing[key] for key in fields},
                agent_run_id=str(existing["agent_run_id"]),
                observation_packet_id=str(existing["observation_packet_id"])
                if existing["observation_packet_id"]
                else None,
                evidence_artifact_ids=_object(existing["evidence_artifact_ids"]),
                judge_input=_object(existing["judge_input"]),
                judge_output=_object(existing["judge_output"]),
            )
    if current not in sources:
        sources.append(current)
    provenance = {
        key: scope[key]
        for key in (
            "task_id",
            "producer_agent_run_id",
            "finalization_epoch",
            "page_capture_id",
            "rule_id",
        )
    } | {
        "schema_version": "web-canonical-provenance.v1",
        "selection": "most_conservative_real_decision",
        "selected_attempt_id": chosen["attempt_id"],
        "sources": sources,
    }
    return replace(
        projected, judge_output={**projected.judge_output, "web_composite_provenance": provenance}
    )


async def sync_web_projection(connection, *, work_item_id, lease_token):
    """仅原 Work/InputSet 已进入组合来源时可恢复，不借另一用途跳过 Judge。"""
    scopes = await _scopes(connection, work_item_id)
    if not scopes:
        return (), {}
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{scopes[0]['task_id']}"
    )
    scopes = await _scopes(connection, work_item_id)
    linked = {}
    for scope in scopes:
        if scope["authorized"] is not True:
            continue
        row = await connection.fetchrow(
            "SELECT id::text,judge_output FROM qc_rule_execution WHERE task_id=$1::uuid "
            "AND page_capture_id=$2::uuid AND rule_id=$3::uuid AND source_idempotency_key IS NULL",
            scope["task_id"],
            scope["page_capture_id"],
            scope["rule_id"],
        )
        if row is None:
            continue
        provenance = _object(row["judge_output"]).get("web_composite_provenance", {})
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
                for key in ("work_item_id", "rule_id", "contract_hash", "evidence_input_set_id")
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
                raise RuleExecutionLeaseConflictError("Web 冷恢复来源与原 Attempt 不一致")
            status = (
                source["execution_status"]
                if source["execution_status"] in {"error", "skipped", "evidence_insufficient"}
                else "written"
            )
            updated = await connection.fetchval(
                "UPDATE qc_inspection_work_item_rule m SET status=$3,observation_packet_id=$4::uuid,"
                "rule_execution_id=$5::uuid,updated_at=now() FROM qc_inspection_work_item w WHERE m.work_item_id=w.id AND "
                "w.id=$1::uuid AND w.status='running' AND w.lease_token=$2::uuid AND w.lease_expires_at>now() "
                "AND m.rule_id=$6::uuid AND m.status IN ('pending','judged') RETURNING m.id",
                work_item_id,
                lease_token,
                status,
                source["observation_packet_id"],
                row["id"],
                scope["rule_id"],
            )
            if updated:
                linked[scope["rule_id"]] = source["execution_status"]
            break
    return tuple(scope["rule_id"] for scope in scopes), linked
