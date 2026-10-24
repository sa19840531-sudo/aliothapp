"""PostgreSQL 页面质检持久化工作项仓储。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.inspection_work_items import (
    ClaimInspectionWorkItemCommand,
    CompletedInspectionWorkItemResult,
    FindCompletedInspectionWorkItemCommand,
    InspectionRuleEvidenceBinding,
    InspectionWorkItemClosureSettlement,
    InspectionWorkItemConflictError,
    InspectionWorkItemExecutionLink,
    InspectionWorkItemFinalization,
    InspectionWorkItemLease,
    InspectionWorkItemRule,
    InspectionWorkItemStatus,
    InspectionWorkItemSyncResult,
    InspectionWorkItemValidationError,
    PublishedInspectionWorkItem,
    PublishInspectionWorkItemCommand,
    RenewInspectionWorkItemLeaseCommand,
    StartInspectionAttemptCommand,
    TransitionInspectionAttemptCommand,
    TransitionInspectionWorkItemCommand,
    TransitionInspectionWorkItemRuleCommand,
    build_inspection_attempt_input_hash,
)
from services.api.app.application.outbox import INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE
from services.api.app.application.run_usage import (
    merge_usage_payloads,
    total_ai_calls_from_usage,
    total_cost_from_usage,
    total_tokens_from_usage,
)
from services.api.app.domain.protocol import build_protocol_envelope
from services.api.app.infrastructure.db.app_consumer_run_lifecycle import (
    finish_app_consumer_run_if_idle_on_connection,
)
from services.api.app.infrastructure.db.evidence_supplement_reseal import (
    reseal_supplement_input_sets_on_connection,
    seal_supplement_input_set_on_connection,
)
from services.api.app.infrastructure.db.h5_consumer_run_lifecycle import (
    finish_h5_consumer_run_if_idle_on_connection,
)
from services.api.app.infrastructure.db.h5_producer_result_reuse import (
    PRODUCER_REUSE_PROOF_SQL,
    verified_producer_proofs,
)
from services.api.app.infrastructure.db.outbox_writer import insert_outbox_event
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.task_rule_inventory_sql import frozen_h5_rule_identity_sql


def _parse_rule_evidence_binding(row: Any) -> InspectionRuleEvidenceBinding:
    # 清单载荷缺失或不是对象时按空清单处理，不能让领取整体崩掉。
    manifest = _json_object(row.get("manifest_payload"))
    roles = _h5_probe_capture_roles(row)
    primary_capture_id = dict(roles).get("primary", str(row["page_capture_id"]))
    primary_manifest_id = str(row["manifest_id"])
    primary_manifest_version = int(row["manifest_version"])
    manifest_refs = []
    parent_manifest = _json_object(row.get("parent_manifest_payload"))
    if any(capture_id == str(row.get("parent_capture_id")) for _, capture_id in roles) and (
        row.get("parent_manifest_id") is None
    ):
        raise InspectionWorkItemValidationError("H5 补采前帧缺少当前精确 PASS Manifest")
    if roles and row.get("parent_manifest_id") is not None:
        for role, capture_id in roles:
            parent = capture_id == str(row["parent_capture_id"])
            manifest_refs.append(
                (
                    role,
                    str(row["parent_manifest_id"] if parent else row["manifest_id"]),
                    str(row["parent_manifest_version"] if parent else row["manifest_version"]),
                )
            )
        if primary_capture_id != str(row["page_capture_id"]):
            primary_manifest_id = str(row["parent_manifest_id"])
            primary_manifest_version = int(row["parent_manifest_version"])
    elif primary_capture_id != str(row["page_capture_id"]):
        raise InspectionWorkItemValidationError("H5 补采基线 primary 缺少精确 PASS Manifest")
    artifacts = manifest.get("artifacts")
    packet_ids = manifest.get("observation_packet_ids")
    if manifest_refs and any(
        capture_id == str(row["parent_capture_id"]) for _, capture_id in roles
    ):
        # 前后角色各自锁定真实清单，不把结果清单冒充刷新前或来源页。
        parent_artifacts = parent_manifest.get("artifacts")
        parent_packet_ids = parent_manifest.get("observation_packet_ids")
        artifacts = [
            *(artifacts if isinstance(artifacts, list) else []),
            *(parent_artifacts if isinstance(parent_artifacts, list) else []),
        ]
        packet_ids = [
            *(packet_ids if isinstance(packet_ids, list) else []),
            *(parent_packet_ids if isinstance(parent_packet_ids, list) else []),
        ]
    return InspectionRuleEvidenceBinding(
        rule_id=str(row["rule_id"]),
        rule_code=str(row["rule_code"]),
        page_capture_id=primary_capture_id,
        producer_agent_run_id=str(row["producer_agent_run_id"]),
        manifest_id=primary_manifest_id,
        manifest_version=primary_manifest_version,
        artifact_ids=tuple(
            dict.fromkeys(
                str(item["artifact_id"])
                for item in artifacts
                if isinstance(item, dict) and item.get("artifact_id")
            )
        )
        if isinstance(artifacts, list)
        else (),
        observation_packet_ids=tuple(dict.fromkeys(str(item) for item in packet_ids))
        if isinstance(packet_ids, list)
        else (),
        evidence_input_set_id=(
            str(row["evidence_input_set_id"])
            if row.get("evidence_input_set_id") is not None
            else None
        ),
        evidence_input_set_hash=(
            str(row["evidence_input_set_hash"])
            if row.get("evidence_input_set_id") is not None
            else None
        ),
        evidence_capture_roles=roles,
        evidence_manifest_refs=tuple(manifest_refs),
    )


def _h5_probe_capture_roles(row: Any) -> tuple[tuple[str, str], ...]:
    """把 H5 Probe 的运行时角色投影为规则合同角色。"""
    if (
        row.get("target_platform") not in {None, "h5"}
        or str(row.get("capture_role") or "") != "probe_result_replay"
        or str(row.get("parent_capture_role") or "") != "probe_before"
        or row.get("parent_capture_id") is None
    ):
        return ()
    parent_capture_id = str(row["parent_capture_id"])
    result_capture_id = str(row["page_capture_id"])
    request_payload = _json_object(row.get("request_payload"))
    target_invariants = _json_object(request_payload.get("target_invariants"))
    semantic = _json_object(target_invariants.get("semantic"))
    requested_roles = (
        tuple(
            str(item).strip()
            for item in semantic.get("capture_roles", ())
            if isinstance(item, str) and str(item).strip()
        )
        if isinstance(semantic.get("capture_roles"), list)
        else ()
    )
    contract = _json_object(row.get("rule_input_contract"))
    contract_roles = contract.get("required_capture_roles")
    if row.get("target_platform") == "h5" and isinstance(contract_roles, list) and contract_roles:
        # 请求里可能只有缺失角色，完整判定范围仍以任务冻结的规则合同为准。
        requested_roles = tuple(
            str(role).strip() for role in contract_roles if isinstance(role, str) and role.strip()
        )
    if row.get("target_platform") == "h5" and any(
        role.startswith("location_") for role in requested_roles
    ):
        # 前后帧只有时序血缘，不能冒充两个商品展示位置或把旧基线设为当前目标。
        return (("primary", result_capture_id),)
    result_role = next(
        (role for role in requested_roles if role not in {"before", "primary"}),
        "primary",
    )
    if (
        row.get("target_platform") == "h5"
        and result_role == "primary"
        and "before" not in requested_roles
    ):
        # 单帧补采不交接额外 before 附件，避免判定裁剪角色后附件仍越界。
        return (("primary", result_capture_id),)
    if result_role == "source":
        return (("source", parent_capture_id), ("primary", result_capture_id))
    parent_role = "before" if result_role == "primary" else "primary"
    return (
        (parent_role, parent_capture_id),
        (result_role, result_capture_id),
    )


async def _load_rule_evidence_bindings(
    connection: Any,
    *,
    work_item_id: str,
) -> tuple[InspectionRuleEvidenceBinding, ...]:
    """每条规则取最近一次已满足请求的补采 Manifest 及其重新封存的输入集合。"""
    rows = await connection.fetch(
        """
        SELECT DISTINCT ON (request.rule_id)
            request.rule_id::text,
            request.rule_code,
            task.target_platform,
            manifest.capture_id::text AS page_capture_id,
            capture.agent_run_id::text AS producer_agent_run_id,
            capture.capture_role,
            parent_capture.id::text AS parent_capture_id,
            parent_capture.capture_role AS parent_capture_role,
            parent_manifest.manifest_id AS parent_manifest_id,
            parent_manifest.manifest_version AS parent_manifest_version,
            parent_manifest.manifest_payload AS parent_manifest_payload,
            manifest.manifest_id,
            manifest.manifest_version,
            manifest.manifest_payload,
            request.request_payload,
            CASE WHEN task.target_platform = 'h5' THEN
                CASE WHEN task.task_config ? 'rule_inventory' THEN (
                    SELECT frozen_rule->'input_contract'
                    FROM jsonb_array_elements(task.task_config #> '{rule_inventory,rules}')
                        AS frozen_rule
                    WHERE frozen_rule->>'rule_id' = request.rule_id::text
                      AND frozen_rule->>'rule_code' = request.rule_code
                    LIMIT 1
                ) ELSE (
                    SELECT rule.input_contract FROM qc_rule AS rule
                    WHERE rule.id = request.rule_id
                ) END
            END AS rule_input_contract,
            input_set.input_set_id AS evidence_input_set_id,
            input_set.input_set_hash AS evidence_input_set_hash
        FROM qc_evidence_request AS request
        INNER JOIN qc_task AS task ON task.id = request.task_id
        INNER JOIN qc_evidence_supplement AS supplement
            ON supplement.request_id = request.request_id
           AND supplement.manifest_id = request.resolution_manifest_id
        INNER JOIN qc_evidence_manifest AS manifest
            ON manifest.manifest_id = supplement.manifest_id
           AND manifest.task_id = request.task_id
           AND manifest.quality_status = 'pass'
        INNER JOIN qc_capture AS capture
            ON capture.id = manifest.capture_id
           AND capture.task_id = request.task_id
           AND capture.capture_status = 'captured'
        LEFT JOIN qc_capture AS parent_capture
            ON parent_capture.id = capture.parent_capture_id
           AND parent_capture.task_id = capture.task_id
           AND parent_capture.capture_status = 'captured'
           AND parent_capture.agent_run_id = capture.agent_run_id
           AND parent_capture.surface_session_id = capture.surface_session_id
           AND parent_capture.action_execution_id = capture.action_execution_id
        LEFT JOIN LATERAL (
            SELECT parent.manifest_id, parent.manifest_version, parent.manifest_payload,
                   parent.quality_status
            FROM qc_evidence_manifest AS parent
            WHERE task.target_platform = 'h5'
              AND capture.capture_role = 'probe_result_replay'
              AND parent_capture.capture_role = 'probe_before'
              AND parent.task_id = request.task_id
              AND parent.capture_id = parent_capture.id
            ORDER BY parent.manifest_version DESC
            LIMIT 1
        ) AS parent_manifest ON parent_manifest.quality_status = 'pass'
        INNER JOIN qc_inspection_work_item_rule AS member
            ON member.work_item_id = request.work_item_id
           AND member.rule_id = request.rule_id
           AND member.status IN ('pending', 'judged')
        LEFT JOIN qc_evidence_input_set AS input_set
            ON input_set.task_id = request.task_id
           AND input_set.input_set_id = supplement.evidence_input_set_id
        WHERE request.work_item_id = $1::uuid
          AND request.status = 'satisfied'
        ORDER BY
            request.rule_id,
            request.completed_at DESC NULLS LAST,
            supplement.created_at DESC,
            supplement.supplement_id DESC
        """,
        work_item_id,
    )
    return tuple(_parse_rule_evidence_binding(item) for item in rows)


async def _attempt_resealed_bindings(
    connection: Any,
    *,
    work_item_id: str,
    lease_token: str,
) -> tuple[InspectionRuleEvidenceBinding, ...]:
    """返回本次 Attempt 实际消费、且已重新封存的补采绑定。

    只有当前绑定能复算出 Attempt 的输入哈希时才算数；判定途中又接上了新补采就返回空，
    由下一次 Attempt 写回，不能把没消费过的输入集合挂到执行上。
    """
    attempt = await connection.fetchrow(
        """
        SELECT
            attempt.input_hash,
            work_item.input_hash AS work_item_input_hash
        FROM qc_inspection_attempt AS attempt
        INNER JOIN qc_inspection_work_item AS work_item
            ON work_item.id = attempt.work_item_id
           AND work_item.attempt_count = attempt.attempt_no
           AND work_item.lease_token = attempt.lease_token
        WHERE attempt.work_item_id = $1::uuid
          AND attempt.lease_token = $2::uuid
        """,
        work_item_id,
        lease_token,
    )
    if attempt is None:
        return ()
    bindings = await _load_rule_evidence_bindings(connection, work_item_id=work_item_id)
    resealed = tuple(item for item in bindings if item.evidence_input_set_id is not None)
    if not resealed:
        return ()
    consumed_hash = build_inspection_attempt_input_hash(
        work_item_input_hash=str(attempt["work_item_input_hash"]),
        rule_evidence=bindings,
    )
    return resealed if consumed_hash == str(attempt["input_hash"]) else ()


async def publish_inspection_work_item_on_connection(
    connection: Any,
    *,
    command: PublishInspectionWorkItemCommand,
    input_hash: str,
) -> PublishedInspectionWorkItem:
    """复用调用方事务幂等写入一个 Work 及成员；是否唤醒由命令显式决定。"""

    row = await connection.fetchrow(
        """
        INSERT INTO qc_inspection_work_item AS existing (
            task_id, page_capture_id, producer_agent_run_id,
            session_owner_agent_run_id, ruleset_id, ruleset_version,
            ruleset_snapshot_hash, protocol_version, batch_key,
            strategy, category, session_dependency, input_hash,
            priority, max_attempts, control_mode, finalization_epoch
        )
        SELECT
            task.id,
            page_capture.id,
            producer.id,
            $4::uuid,
            ruleset.id,
            ruleset.version,
            ruleset.snapshot_hash,
            $8,
            $9,
            $10,
            $11,
            $12,
            $13,
            $14,
            $15,
            $16,
            task.finalization_epoch
        FROM qc_task AS task
        INNER JOIN qc_capture AS page_capture
            ON page_capture.id = $2::uuid
           AND page_capture.task_id = task.id
        INNER JOIN qc_agent_run AS producer
            ON producer.id = $3::uuid
           AND producer.task_id = task.id
        INNER JOIN qc_ruleset AS ruleset
            ON ruleset.id = $5::uuid
           AND ruleset.id = task.ruleset_id
        WHERE task.id = $1::uuid
          AND task.status = 'running'
          AND task.finalization_state IN (
              'open', 'exploration_draining', 'session_draining'
          )
          AND page_capture.agent_run_id = producer.id
          AND ruleset.version = $6
          AND ruleset.snapshot_hash IS NOT DISTINCT FROM $7
          AND (
              $4::uuid IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM qc_agent_run AS session_owner
                  WHERE session_owner.id = $4::uuid
                    AND session_owner.task_id = task.id
              )
          )
        ON CONFLICT ON CONSTRAINT uq_qc_inspection_work_item_identity
        DO UPDATE SET updated_at = existing.updated_at
        RETURNING id::text, status, (xmax = 0) AS created,
            (SELECT task.target_platform FROM qc_task task WHERE task.id=existing.task_id)
                AS target_platform
        """,
        command.task_id,
        command.page_capture_id,
        command.producer_agent_run_id,
        command.session_owner_agent_run_id,
        command.ruleset_id,
        command.ruleset_version,
        command.ruleset_snapshot_hash,
        command.protocol_version,
        command.batch_key,
        command.strategy,
        command.category,
        command.session_dependency,
        input_hash,
        command.priority,
        command.max_attempts,
        command.control_mode,
    )
    if row is None:
        raise RuntimeError("工作项关联的任务、页面采集、生产 Run 或锁定规则集不一致")
    work_item_id = str(row["id"])
    for rule in command.rules:
        if rule.evidence_input_set_id is not None:
            if row.get("target_platform") == "h5":
                from services.api.app.infrastructure.db.h5_page_work_binding import (
                    insert_h5_page_work_rule,
                )
                member_id = await insert_h5_page_work_rule(
                    connection, work_item_id=work_item_id, rule=rule,
                )
                if member_id is None:
                    raise RuntimeError(f"H5 规则 {rule.rule_code} 缺少一致的冻结 InputSet")
                continue
            from services.api.app.infrastructure.db.web_sequence_work_binding import (
                insert_web_sequence_work_rule,
            )

            member_id = await insert_web_sequence_work_rule(connection, work_item_id=work_item_id, rule=rule)
            if member_id is None:
                raise RuntimeError(f"Web 规则 {rule.rule_code} 缺少一致的冻结 InputSet")
            continue
        rule_member_id = await connection.fetchval(
            """
            INSERT INTO qc_inspection_work_item_rule AS existing (
                work_item_id, rule_id, rule_code, ordinal, contract_hash
            )
            SELECT
                work_item.id,
                rule.id,
                $3::varchar,
                $4,
                $5
            FROM qc_inspection_work_item AS work_item
            JOIN qc_task AS inventory_task ON inventory_task.id = work_item.task_id
            INNER JOIN qc_rule AS rule
                ON rule.id = $2::uuid
            WHERE work_item.id = $1::uuid
              AND __H5_RULE_IDENTITY__
            ON CONFLICT ON CONSTRAINT uq_qc_inspection_work_item_rule
            DO UPDATE SET updated_at = existing.updated_at
            RETURNING id::text
            """.replace("__H5_RULE_IDENTITY__", frozen_h5_rule_identity_sql(
                task="inventory_task", rule="rule", code="$3::varchar",
                ruleset="work_item.ruleset_id",
            )),
            work_item_id,
            rule.rule_id,
            rule.rule_code,
            rule.ordinal,
            rule.contract_hash,
        )
        if rule_member_id is None:
            raise RuntimeError(f"规则 {rule.rule_code} 不属于工作项锁定规则集")
    if command.control_mode == "enforced" and not command.defer_wakeup:
        await insert_outbox_event(
            connection,
            aggregate_type="inspection_work_item",
            aggregate_id=work_item_id,
            envelope=build_protocol_envelope(
                message_type=INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
                payload_protocol_version=command.protocol_version,
                idempotency_key=input_hash,
                task_id=command.task_id,
                aggregate_type="inspection_work_item",
                aggregate_id=work_item_id,
                aggregate_version=0,
                occurred_at=datetime.now(UTC),
                payload={
                    "protocol_version": command.protocol_version,
                    "work_item_id": work_item_id,
                    "task_id": command.task_id,
                    "page_capture_id": command.page_capture_id,
                    "batch_key": command.batch_key,
                    "strategy": command.strategy,
                    "category": command.category,
                    "session_dependency": command.session_dependency,
                    "control_mode": command.control_mode,
                    "input_hash": input_hash,
                },
                correlation_id=command.task_id,
            ),
        )
    return PublishedInspectionWorkItem(
        work_item_id=work_item_id,
        status=InspectionWorkItemStatus(str(row["status"])),
        input_hash=input_hash,
        created=bool(row["created"]),
    )


class PostgresInspectionWorkItemRepository:
    """以工作项行作为单体内持久化命令队列，并使用租约避免重复消费。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def publish(
        self,
        *,
        command: PublishInspectionWorkItemCommand,
        input_hash: str,
    ) -> PublishedInspectionWorkItem:
        """在一个事务内幂等发布批次及其规则成员。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{command.task_id}",
            )

            return await publish_inspection_work_item_on_connection(
                connection,
                command=command,
                input_hash=input_hash,
            )

    async def read_original_page_inputs(self, *, task_id, source_agent_run_id, page_capture_id, rule_contracts):
        """候选计划只继承已有主 Run 原输入，不从当前材料补造旧结果血缘。"""
        from services.api.app.infrastructure.db.h5_producer_result_reuse import (
            read_verified_page_input,
        )

        pool = await self._get_pool()
        bindings = {}
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT execution.rule_id::text, execution.evidence_input_set_id,
                       task.finalization_epoch, ruleset.snapshot_hash AS ruleset_snapshot_hash
                FROM qc_rule_execution execution JOIN qc_task task ON task.id=execution.task_id
                JOIN qc_ruleset ruleset ON ruleset.id=task.ruleset_id
                WHERE execution.task_id=$1::uuid AND execution.agent_run_id=$2::uuid
                    AND execution.page_capture_id=$3::uuid AND task.target_platform='h5'
                    AND execution.evidence_input_set_id IS NOT NULL
                """, task_id, source_agent_run_id, page_capture_id,
            )
            for row in rows:
                rule_id = row["rule_id"]
                if rule_id not in rule_contracts:
                    continue
                inputs = await read_verified_page_input(connection, input_set_id=row["evidence_input_set_id"],
                    packet_runs=(source_agent_run_id,))
                if inputs is not None and all((
                    inputs.task_id == task_id, inputs.source_agent_run_id == source_agent_run_id,
                    inputs.eligible_capture_ids == (page_capture_id,), inputs.rule_id == rule_id,
                    inputs.rule_contract_hash == rule_contracts.get(rule_id),
                    inputs.finalization_epoch == row["finalization_epoch"],
                    inputs.ruleset_snapshot_hash == row["ruleset_snapshot_hash"],
                )):
                    bindings[rule_id] = inputs.input_set_id
        return bindings

    async def find_completed_result(
        self,
        *,
        command: FindCompletedInspectionWorkItemCommand,
    ) -> CompletedInspectionWorkItemResult | None:
        """读取当前任务周期内与精确规则批次匹配的已完成正式结果。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH completed_work_item AS (
                    SELECT work_item.id, work_item.page_capture_id
                    FROM qc_inspection_work_item AS work_item
                    INNER JOIN qc_task AS task ON task.id = work_item.task_id
                    WHERE work_item.task_id = $1::uuid
                      AND work_item.page_capture_id = $2::uuid
                      AND work_item.control_mode = 'enforced'
                      AND work_item.status IN ('succeeded', 'partial')
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND task.status = 'running'
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      AND ARRAY(
                          SELECT member.rule_code
                          FROM qc_inspection_work_item_rule AS member
                          WHERE member.work_item_id = work_item.id
                          ORDER BY member.ordinal
                      ) = $3::varchar[]
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_inspection_work_item_rule AS member
                          WHERE member.work_item_id = work_item.id
                            AND member.rule_execution_id IS NULL
                      )
                    ORDER BY work_item.finished_at DESC NULLS LAST
                    LIMIT 1
                )
                SELECT
                    completed.id::text AS work_item_id,
                    completed.page_capture_id::text,
                    member.rule_id::text,
                    member.rule_code,
                    execution.execution_status,
                    member.observation_packet_id::text,
                    member.rule_execution_id::text
                FROM completed_work_item AS completed
                INNER JOIN qc_inspection_work_item_rule AS member
                    ON member.work_item_id = completed.id
                INNER JOIN qc_rule_execution AS execution
                    ON execution.id = member.rule_execution_id
                ORDER BY member.ordinal
                """,
                command.task_id,
                command.page_capture_id,
                list(command.rule_codes),
            )
        if not rows or len(rows) != len(command.rule_codes):
            return None
        return CompletedInspectionWorkItemResult(
            work_item_id=str(rows[0]["work_item_id"]),
            page_capture_id=str(rows[0]["page_capture_id"]),
            links=tuple(
                InspectionWorkItemExecutionLink(
                    rule_id=str(row["rule_id"]),
                    rule_code=str(row["rule_code"]),
                    execution_status=str(row["execution_status"]),
                    observation_packet_id=(
                        str(row["observation_packet_id"])
                        if row["observation_packet_id"] is not None
                        else None
                    ),
                    rule_execution_id=str(row["rule_execution_id"]),
                )
                for row in rows
            ),
        )


    async def get_rule_input_set_context(
        self,
        *,
        work_item_id: str,
        rule_id: str,
    ):
        """供补采重封读取规则绑定与工作项封存上下文。"""
        from services.api.app.application.supplement_input_set_reseal import (
            WorkItemInputSetContext,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    member.evidence_input_set_id,
                    member.contract_hash,
                    work_item.producer_agent_run_id::text AS producer_agent_run_id,
                    work_item.finalization_epoch,
                    COALESCE(
                        work_item.ruleset_snapshot_hash,
                        member.contract_hash
                    ) AS ruleset_snapshot_hash
                FROM qc_inspection_work_item_rule AS member
                INNER JOIN qc_inspection_work_item AS work_item
                    ON work_item.id = member.work_item_id
                WHERE member.work_item_id = $1::uuid
                  AND member.rule_id = $2::uuid
                """,
                work_item_id,
                rule_id,
            )
            if row is None:
                return None
            if row["producer_agent_run_id"] is None:
                return None
            return WorkItemInputSetContext(
                evidence_input_set_id=(
                    str(row["evidence_input_set_id"])
                    if row["evidence_input_set_id"] is not None
                    else None
                ),
                contract_hash=str(row["contract_hash"]),
                producer_agent_run_id=str(row["producer_agent_run_id"]),
                finalization_epoch=int(row["finalization_epoch"]),
                ruleset_snapshot_hash=str(row["ruleset_snapshot_hash"]),
            )

    async def seal_supplement_input_set(
        self,
        *,
        request_id: str,
    ):
        """补采挂接后立即封存；与领取重判共用同一套封存，已封过就返回那个集合。"""
        from services.api.app.application.supplement_input_set_reseal import (
            SealedSupplementInputSet,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            sealed = await seal_supplement_input_set_on_connection(
                connection,
                request_id=request_id,
            )
        if sealed is None:
            return None
        input_set_id, input_set_hash, created = sealed
        return SealedSupplementInputSet(
            input_set_id=input_set_id,
            input_set_hash=input_set_hash,
            created=created,
        )

    async def claim(
        self,
        *,
        command: ClaimInspectionWorkItemCommand,
        lease_token: str,
    ) -> InspectionWorkItemLease | None:
        """领取一个可执行或租约已过期的批次。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                WITH candidate AS (
                    SELECT work_item.id, task.target_platform
                    FROM qc_inspection_work_item AS work_item
                    INNER JOIN qc_task AS task ON task.id = work_item.task_id
                    WHERE work_item.task_id = $1::uuid
                      AND (
                          task.status = 'running'
                          OR (
                              task.status = 'review'
                              AND task.finalization_state = 'session_draining'
                              AND EXISTS (
                                  SELECT 1
                                  FROM qc_agent_run AS recovery_consumer
                                  WHERE recovery_consumer.id = $3::uuid
                                    AND recovery_consumer.task_id = work_item.task_id
                                    AND recovery_consumer.agent_type = 'rule_judge'
                                    AND recovery_consumer.status = 'running'
                                    AND recovery_consumer.input_context->>'source' IN (
                                        'h5_inspection_publication',
                                        'app_inspection_publication'
                                    )
                              )
                          )
                      )
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND work_item.control_mode = 'enforced'
                      AND EXISTS (
                          SELECT 1
                          FROM qc_agent_run AS consumer
                          WHERE consumer.id = $3::uuid
                            AND consumer.task_id = work_item.task_id
                            AND (
                                consumer.status = 'running'
                                OR (
                                    $9::boolean
                                    AND task.status = 'running'
                                    AND task.finalization_state IN (
                                        'exploration_draining', 'session_draining'
                                    )
                                    AND task.finalization_agent_run_id = $3::uuid
                                    AND task.target_platform = 'web'
                                    AND task.task_config->>'workflow_version' = 'v2'
                                    AND task.task_config->>'execution_mode' =
                                        'full_inspection'
                                    AND task.task_config->>'web_inspection_notification_mode'
                                        = 'enforced'
                                    AND consumer.agent_type = 'main'
                                    AND consumer.status IN ('done', 'failed', 'aborted')
                                    AND work_item.session_dependency = 'active_session'
                                    AND work_item.producer_agent_run_id = $3::uuid
                                    AND work_item.session_owner_agent_run_id = $3::uuid
                                    AND work_item.finalization_epoch =
                                        task.finalization_epoch
                                )
                            )
                      )
                      AND work_item.attempt_count < work_item.max_attempts
                      AND (
                          (
                              work_item.status IN ('pending', 'retry_wait')
                              AND work_item.next_attempt_at <= now()
                          )
                          OR (
                              work_item.status IN ('leased', 'running')
                              AND work_item.lease_expires_at <= now()
                          )
                      )
                      AND (
                          $2::text IS NULL
                          OR work_item.session_dependency = $2
                      )
                      AND (
                          $4::uuid IS NULL
                          OR work_item.page_capture_id = $4::uuid
                      )
                      AND (
                          cardinality($5::varchar[]) = 0
                          OR ARRAY(
                              SELECT member.rule_code
                              FROM qc_inspection_work_item_rule AS member
                              WHERE member.work_item_id = work_item.id
                                AND member.status IN ('pending', 'judged')
                              ORDER BY member.ordinal
                          ) = $5::varchar[]
                      )
                      AND (
                          work_item.session_dependency = 'snapshot'
                          OR work_item.session_owner_agent_run_id = $3::uuid
                      )
                    ORDER BY
                        work_item.priority ASC,
                        work_item.next_attempt_at ASC,
                        work_item.created_at ASC
                    FOR UPDATE OF work_item SKIP LOCKED
                    LIMIT 1
                )
                UPDATE qc_inspection_work_item AS work_item
                SET
                    status = 'leased',
                    consumer_agent_run_id = $3::uuid,
                    lease_owner = $6,
                    lease_token = $7::uuid,
                    lease_expires_at = now() + make_interval(secs => $8),
                    attempt_count = work_item.attempt_count + 1,
                    row_version = work_item.row_version + 1,
                    started_at = COALESCE(work_item.started_at, now()),
                    last_error_code = NULL,
                    last_error_message = NULL,
                    updated_at = now()
                FROM candidate
                WHERE work_item.id = candidate.id
                RETURNING
                    work_item.id::text,
                    work_item.task_id::text,
                    work_item.page_capture_id::text,
                    work_item.batch_key,
                    work_item.strategy,
                    work_item.category,
                    work_item.session_dependency,
                    work_item.input_hash,
                    work_item.producer_agent_run_id::text,
                    work_item.consumer_agent_run_id::text,
                    work_item.lease_token::text,
                    work_item.attempt_count,
                    work_item.row_version,
                    candidate.target_platform
                """,
                command.task_id,
                command.session_dependency,
                command.consumer_agent_run_id,
                command.page_capture_id,
                list(command.rule_codes),
                command.lease_owner,
                lease_token,
                command.lease_seconds,
                command.allow_terminal_web_owner_recovery,
            )
            if row is None:
                return None
            rule_rows = await connection.fetch(
                """
                SELECT
                    rule_id::text,
                    rule_code,
                    ordinal,
                    contract_hash,
                    evidence_input_set_id
                FROM qc_inspection_work_item_rule
                WHERE work_item_id = $1::uuid
                  AND status IN ('pending', 'judged')
                ORDER BY ordinal ASC
                """,
                str(row["id"]),
            )
            # 新 Attempt 之前先把已接上的补采封成输入集合，Attempt 身份只引用封存后的集合
            await reseal_supplement_input_sets_on_connection(
                connection, work_item_id=str(row["id"])
            )
            rule_evidence = await _load_rule_evidence_bindings(
                connection, work_item_id=str(row["id"])
            )
            work_item_input_hash = str(row["input_hash"])
            attempt_input_hash = build_inspection_attempt_input_hash(
                work_item_input_hash=work_item_input_hash,
                rule_evidence=rule_evidence,
            )
            recovery_row = await connection.fetchrow(
                """
                SELECT result_payload
                FROM qc_inspection_attempt
                WHERE work_item_id = $1::uuid
                  AND attempt_no < $2
                  AND input_hash = $3
                  AND status IN ('judged', 'retryable_error', 'abandoned')
                  AND result_payload <> '{}'::jsonb
                ORDER BY attempt_no DESC
                LIMIT 1
                """,
                str(row["id"]),
                int(row["attempt_count"]),
                attempt_input_hash,
            )
            await connection.execute(
                """
                UPDATE qc_inspection_attempt
                SET
                    status = 'abandoned',
                    error_code = COALESCE(
                        error_code,
                        'INSPECTION_ATTEMPT_LEASE_RECLAIMED'
                    ),
                    error_message = COALESCE(
                        error_message,
                        '工作项旧租约已被新消费者接管'
                    ),
                    finished_at = COALESCE(finished_at, now()),
                    updated_at = now()
                WHERE work_item_id = $1::uuid
                  AND attempt_no < $2
                  AND status IN ('running', 'judged')
                """,
                str(row["id"]),
                int(row["attempt_count"]),
            )
        if row["producer_agent_run_id"] is None or row["consumer_agent_run_id"] is None:
            raise InspectionWorkItemConflictError("工作项缺少证据生产或判定消费 Run 身份")
        return InspectionWorkItemLease(
            work_item_id=str(row["id"]),
            task_id=str(row["task_id"]),
            page_capture_id=str(row["page_capture_id"]),
            batch_key=str(row["batch_key"]),
            strategy=str(row["strategy"]),
            category=str(row["category"]) if row["category"] is not None else None,
            session_dependency=str(row["session_dependency"]),
            input_hash=attempt_input_hash,
            producer_agent_run_id=str(row["producer_agent_run_id"]),
            consumer_agent_run_id=str(row["consumer_agent_run_id"]),
            lease_token=str(row["lease_token"]),
            attempt_no=int(row["attempt_count"]),
            row_version=int(row["row_version"]),
            rules=tuple(
                InspectionWorkItemRule(
                    rule_id=str(rule_row["rule_id"]),
                    rule_code=str(rule_row["rule_code"]),
                    ordinal=int(rule_row["ordinal"]),
                    contract_hash=str(rule_row["contract_hash"]),
                    evidence_input_set_id=rule_row.get("evidence_input_set_id"),
                )
                for rule_row in rule_rows
            ),
            recoverable_result_payload=(
                _json_dict(recovery_row.get("result_payload")) if recovery_row is not None else None
            ),
            target_platform=(
                str(row.get("target_platform")) if row.get("target_platform") is not None else None
            ),
            work_item_input_hash=work_item_input_hash,
            rule_evidence=rule_evidence,
        )

    async def transition(
        self,
        *,
        command: TransitionInspectionWorkItemCommand,
    ) -> int | None:
        """使用状态、租约与 row_version 三重条件原子推进工作项。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                UPDATE qc_inspection_work_item
                SET
                    status = $2,
                    next_attempt_at = CASE
                        WHEN $2 = 'retry_wait' THEN $6::timestamptz
                        WHEN $2 = 'pending' THEN now()
                        ELSE next_attempt_at
                    END,
                    lease_owner = CASE
                        WHEN $2 IN ('leased', 'running') THEN lease_owner
                        ELSE NULL
                    END,
                    lease_token = CASE
                        WHEN $2 IN ('leased', 'running') THEN lease_token
                        ELSE NULL
                    END,
                    lease_expires_at = CASE
                        WHEN $2 IN ('leased', 'running') THEN lease_expires_at
                        ELSE NULL
                    END,
                    last_error_code = $7,
                    last_error_message = $8,
                    finished_at = CASE
                        WHEN $2 IN (
                            'succeeded', 'partial', 'failed',
                            'cancelled', 'superseded'
                        )
                        THEN now()
                        ELSE NULL
                    END,
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND status = $3
                  AND row_version = $4
                  AND (
                      $5::uuid IS NULL
                      OR (
                          lease_token = $5::uuid
                          AND lease_expires_at > now()
                      )
                  )
                  AND (
                      $2 <> 'succeeded'
                      OR NOT EXISTS (
                          SELECT 1
                          FROM qc_inspection_work_item_rule AS member
                          WHERE member.work_item_id = qc_inspection_work_item.id
                            AND member.status <> 'written'
                      )
                  )
                  AND (
                      $2 <> 'partial'
                      OR (
                          NOT EXISTS (
                              SELECT 1
                              FROM qc_inspection_work_item_rule AS member
                              WHERE member.work_item_id = qc_inspection_work_item.id
                                AND member.status IN ('pending', 'judged')
                          )
                          AND EXISTS (
                              SELECT 1
                              FROM qc_inspection_work_item_rule AS member
                              WHERE member.work_item_id = qc_inspection_work_item.id
                                AND member.status <> 'written'
                          )
                      )
                  )
                RETURNING row_version
                """,
                command.work_item_id,
                command.target_status.value,
                command.expected_status.value,
                command.row_version,
                command.lease_token,
                command.next_attempt_at,
                command.error_code,
                command.error_message,
            )
        return int(value) if value is not None else None

    async def renew_lease(
        self,
        *,
        command: RenewInspectionWorkItemLeaseCommand,
    ) -> int | None:
        """只允许当前消费者在旧租约过期前续租。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                UPDATE qc_inspection_work_item
                SET
                    lease_expires_at = now() + make_interval(secs => $5),
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND consumer_agent_run_id = $2::uuid
                  AND lease_token = $3::uuid
                  AND row_version = $4
                  AND status IN ('leased', 'running')
                  AND lease_expires_at > now()
                  AND EXISTS (
                      SELECT 1
                      FROM qc_agent_run AS consumer
                      INNER JOIN qc_task AS task
                        ON task.id = qc_inspection_work_item.task_id
                      WHERE consumer.id = $2::uuid
                        AND consumer.task_id = qc_inspection_work_item.task_id
                        AND (
                            consumer.status = 'running'
                            OR (
                                $6::boolean
                                AND task.status = 'running'
                                AND task.finalization_state IN (
                                    'exploration_draining', 'session_draining'
                                )
                                AND task.finalization_agent_run_id = $2::uuid
                                AND task.target_platform = 'web'
                                AND task.task_config->>'workflow_version' = 'v2'
                                AND task.task_config->>'execution_mode' =
                                    'full_inspection'
                                AND task.task_config->>'web_inspection_notification_mode'
                                    = 'enforced'
                                AND consumer.agent_type = 'main'
                                AND consumer.status IN ('done', 'failed', 'aborted')
                                AND qc_inspection_work_item.session_dependency =
                                    'active_session'
                                AND qc_inspection_work_item.producer_agent_run_id =
                                    $2::uuid
                                AND qc_inspection_work_item.session_owner_agent_run_id =
                                    $2::uuid
                                AND qc_inspection_work_item.finalization_epoch =
                                    task.finalization_epoch
                            )
                        )
                  )
                RETURNING row_version
                """,
                command.work_item_id,
                command.consumer_agent_run_id,
                command.lease_token,
                command.row_version,
                command.lease_seconds,
                command.allow_terminal_web_owner_recovery,
            )
        return int(value) if value is not None else None

    async def start_attempt(
        self,
        *,
        command: StartInspectionAttemptCommand,
    ) -> str:
        """只有当前有效租约才能创建或读取同序号判定尝试。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                INSERT INTO qc_inspection_attempt AS existing (
                    work_item_id, consumer_agent_run_id, attempt_no,
                    lease_token, input_hash
                )
                SELECT
                    work_item.id,
                    $2::uuid,
                    $3,
                    $4::uuid,
                    $5::varchar
                FROM qc_inspection_work_item AS work_item
                WHERE work_item.id = $1::uuid
                  AND work_item.consumer_agent_run_id = $2::uuid
                  AND work_item.attempt_count = $3
                  AND work_item.lease_token = $4::uuid
                  AND work_item.input_hash = $6::varchar
                  AND work_item.status IN ('leased', 'running')
                  AND work_item.lease_expires_at > now()
                ON CONFLICT ON CONSTRAINT uq_qc_inspection_attempt_no
                DO UPDATE SET updated_at = existing.updated_at
                RETURNING id::text
                """,
                command.work_item_id,
                command.consumer_agent_run_id,
                command.attempt_no,
                command.lease_token,
                command.input_hash,
                command.work_item_input_hash or command.input_hash,
            )
        if value is None:
            raise InspectionWorkItemConflictError("工作项租约、输入哈希或尝试序号已变化")
        return str(value)

    async def transition_attempt(
        self,
        *,
        command: TransitionInspectionAttemptCommand,
    ) -> bool:
        """持久化有界 Judge 输出，并使用当前状态防止旧尝试覆盖新结果。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                UPDATE qc_inspection_attempt AS attempt
                SET
                    status = $2::varchar,
                    result_payload = COALESCE($4::jsonb, attempt.result_payload),
                    error_code = $5,
                    error_message = $6,
                    judged_at = CASE
                        WHEN $2::varchar = 'judged'
                        THEN COALESCE(attempt.judged_at, now())
                        ELSE attempt.judged_at
                    END,
                    finished_at = CASE
                        WHEN $2::varchar IN (
                            'written', 'retryable_error',
                            'terminal_error', 'abandoned'
                        )
                        THEN now()
                        ELSE NULL
                    END,
                    updated_at = now()
                FROM qc_inspection_work_item AS work_item
                WHERE attempt.id = $1::uuid
                  AND attempt.status = $3::varchar
                  AND attempt.lease_token = $7::uuid
                  AND work_item.id = attempt.work_item_id
                  AND work_item.status = 'running'
                  AND work_item.lease_token = $7::uuid
                  AND work_item.lease_expires_at > now()
                RETURNING attempt.id::text
                """,
                command.attempt_id,
                command.target_status.value,
                command.expected_status.value,
                (
                    json.dumps(
                        command.result_payload,
                        ensure_ascii=False,
                        default=str,
                    )
                    if command.result_payload is not None
                    else None
                ),
                command.error_code,
                command.error_message,
                command.lease_token,
            )
        return value is not None

    async def transition_rule(
        self,
        *,
        command: TransitionInspectionWorkItemRuleCommand,
    ) -> bool:
        """推进规则成员，并校验 Packet/Execution 与任务、页面、规则归属一致。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                UPDATE qc_inspection_work_item_rule AS member
                SET
                    status = $4,
                    observation_packet_id = COALESCE(
                        $6::uuid,
                        member.observation_packet_id
                    ),
                    rule_execution_id = COALESCE(
                        $7::uuid,
                        member.rule_execution_id
                    ),
                    last_error_code = $8,
                    last_error_message = $9,
                    updated_at = now()
                FROM qc_inspection_work_item AS work_item
                WHERE member.work_item_id = $1::uuid
                  AND member.rule_id = $2::uuid
                  AND member.status = $3
                  AND work_item.id = member.work_item_id
                  AND work_item.status = 'running'
                  AND work_item.lease_token = $5::uuid
                  AND work_item.lease_expires_at > now()
                  AND (
                      $6::uuid IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM qc_observation_packet AS packet
                          WHERE packet.id = $6::uuid
                            AND packet.task_id = work_item.task_id
                            AND packet.page_capture_id = work_item.page_capture_id
                            AND packet.rule_id = member.rule_id
                            AND packet.created_by_agent_run_id = work_item.consumer_agent_run_id
                      )
                  )
                  AND (
                      $7::uuid IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM qc_rule_execution AS execution
                          WHERE execution.id = $7::uuid
                            AND execution.task_id = work_item.task_id
                            AND execution.page_capture_id = work_item.page_capture_id
                            AND execution.rule_id = member.rule_id
                            AND execution.agent_run_id = work_item.consumer_agent_run_id
                      )
                  )
                RETURNING member.id::text
                """,
                command.work_item_id,
                command.rule_id,
                command.expected_status.value,
                command.target_status.value,
                command.lease_token,
                command.observation_packet_id,
                command.rule_execution_id,
                command.error_code,
                command.error_message,
            )
        return value is not None

    async def sync_formal_results(
        self,
        *,
        work_item_id: str,
        lease_token: str,
    ) -> InspectionWorkItemSyncResult:
        """在有效租约下把正式规则执行结果关联回工作项成员。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            task_id = await connection.fetchval(
                "SELECT task_id::text FROM qc_inspection_work_item WHERE id=$1::uuid", work_item_id,
            )
            if task_id is not None:
                # 与发布、补采和收口一致：先 Task 锁，再 Work/Attempt/执行行锁。
                await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task_id}")
            from services.api.app.infrastructure.db.web_sequence_result_projection import (
                WEB_COMPOSITE_RECOVERY_EXCLUSION_SQL,
                sync_web_projection,
            )

            # 成员关联后就不再是 pending/judged，本次 Attempt 实际消费的补采绑定要先取
            resealed_bindings = await _attempt_resealed_bindings(
                connection, work_item_id=work_item_id, lease_token=lease_token
            )
            from services.api.app.infrastructure.db.h5_page_work_binding import bind_h5_page_results

            h5_page_executions = await bind_h5_page_results(
                connection, work_item_id=work_item_id, lease_token=lease_token,
            )
            await connection.execute(
                f"""
                UPDATE qc_inspection_work_item_rule AS member
                SET
                    status = CASE execution.execution_status
                        WHEN 'evidence_insufficient' THEN 'evidence_insufficient'
                        WHEN 'skipped' THEN 'skipped'
                        WHEN 'error' THEN 'error'
                        ELSE 'written'
                    END,
                    observation_packet_id = execution.observation_packet_id,
                    rule_execution_id = execution.id,
                    updated_at = now()
                FROM
                    qc_inspection_work_item AS work_item,
                    qc_rule_execution AS execution
                WHERE work_item.id = $1::uuid
                  AND work_item.id = member.work_item_id
                  AND work_item.control_mode = 'enforced'
                  AND work_item.status = 'running'
                  AND work_item.lease_token = $2::uuid
                  AND work_item.lease_expires_at > now()
                  AND execution.task_id = work_item.task_id
                  AND execution.page_capture_id = work_item.page_capture_id
                  AND execution.rule_id = member.rule_id
                  AND execution.agent_run_id = work_item.consumer_agent_run_id
                  AND (
                      execution.observation_packet_id IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM qc_observation_packet AS packet
                          WHERE packet.id = execution.observation_packet_id
                            AND packet.created_by_agent_run_id = work_item.consumer_agent_run_id
                      )
                  )
                  AND member.status IN ('pending', 'judged')
                  AND (
                      member.evidence_input_set_id IS NULL
                      OR NOT EXISTS (SELECT 1 FROM qc_task bound_task
                          WHERE bound_task.id=work_item.task_id AND bound_task.target_platform='h5')
                      OR execution.id::text=ANY($3::text[])
                  )
                  {WEB_COMPOSITE_RECOVERY_EXCLUSION_SQL}
                """,
                work_item_id,
                lease_token,
                h5_page_executions,
            )
            if resealed_bindings:
                # 补采重判的规则执行指回实际消费的封存输入集合，审计能从执行反查到补采
                await connection.execute(
                    """
                    UPDATE qc_rule_execution AS execution
                    SET evidence_input_set_id = binding.input_set_id,
                        updated_at = now()
                    FROM qc_inspection_work_item_rule AS member,
                         jsonb_to_recordset($2::jsonb)
                            AS binding(rule_id uuid, input_set_id varchar)
                    WHERE member.work_item_id = $1::uuid
                      AND member.rule_id = binding.rule_id
                      AND execution.id = member.rule_execution_id
                      AND execution.evidence_input_set_id
                          IS DISTINCT FROM binding.input_set_id
                    """,
                    work_item_id,
                    json.dumps(
                        [
                            {"rule_id": item.rule_id, "input_set_id": item.evidence_input_set_id}
                            for item in resealed_bindings
                        ]
                    ),
                )
            _guarded_rules,web_linked = await sync_web_projection(connection,work_item_id=work_item_id,lease_token=lease_token)
            summary = await connection.fetchrow(
                """
                SELECT
                    count(member.id)::integer AS expected_rule_count,
                    count(member.id) FILTER (
                        WHERE member.status NOT IN ('pending', 'judged')
                    )::integer AS terminal_rule_count,
                    count(member.id) FILTER (
                        WHERE member.status IN ('pending', 'judged')
                    )::integer AS pending_rule_count,
                    count(member.id) FILTER (
                        WHERE member.status = 'written'
                    )::integer AS written_rule_count
                FROM qc_inspection_work_item AS work_item
                LEFT JOIN qc_inspection_work_item_rule AS member
                    ON member.work_item_id = work_item.id
                WHERE work_item.id = $1::uuid
                  AND work_item.control_mode = 'enforced'
                  AND work_item.status = 'running'
                  AND work_item.lease_token = $2::uuid
                  AND work_item.lease_expires_at > now()
                GROUP BY work_item.id
                """,
                work_item_id,
                lease_token,
            )
            if summary is None:
                raise InspectionWorkItemConflictError("工作项租约已失效，不能同步正式规则结果")
            link_rows = await connection.fetch(
                """
                SELECT
                    member.rule_id::text,
                    member.rule_code,
                    execution.execution_status,
                    member.observation_packet_id::text,
                    member.rule_execution_id::text
                FROM qc_inspection_work_item_rule AS member
                INNER JOIN qc_rule_execution AS execution
                    ON execution.id = member.rule_execution_id
                WHERE member.work_item_id = $1::uuid
                ORDER BY member.ordinal
                """,
                work_item_id,
            )
        return InspectionWorkItemSyncResult(
            work_item_id=work_item_id,
            expected_rule_count=int(summary["expected_rule_count"]),
            terminal_rule_count=int(summary["terminal_rule_count"]),
            pending_rule_count=int(summary["pending_rule_count"]),
            written_rule_count=int(summary["written_rule_count"]),
            links=tuple(
                InspectionWorkItemExecutionLink(
                    rule_id=str(row["rule_id"]),
                    rule_code=str(row["rule_code"]),
                    execution_status=web_linked.get(str(row["rule_id"]),str(row["execution_status"])),
                    observation_packet_id=(
                        str(row["observation_packet_id"])
                        if row["observation_packet_id"] is not None
                        else None
                    ),
                    rule_execution_id=str(row["rule_execution_id"]),
                )
                for row in link_rows
            ),
        )

    async def find_active_consumer_run(self, *, task_id: str) -> str | None:
        """优先返回 H5/App Publication 逻辑 Run，再兼容既有任务运行 Run。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                WITH candidate AS (
                    SELECT
                        consumer.id,
                        consumer.started_at,
                        consumer.created_at,
                        0 AS priority
                    FROM qc_task AS task
                    INNER JOIN qc_h5_inspection_publication AS publication
                        ON publication.task_id = task.id
                       AND publication.finalization_epoch = task.finalization_epoch
                       AND publication.mode = 'enforced'
                    INNER JOIN qc_agent_run AS consumer
                        ON consumer.id = publication.consumer_agent_run_id
                       AND consumer.task_id = task.id
                       AND consumer.agent_type = 'rule_judge'
                       AND consumer.status = 'running'
                    WHERE task.id = $1::uuid
                      AND task.status IN ('running', 'review')
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      AND EXISTS (
                          SELECT 1
                          FROM qc_h5_inspection_publication_work_item AS link
                          INNER JOIN qc_inspection_work_item AS work_item
                              ON work_item.id = link.work_item_id
                          WHERE link.publication_id = publication.id
                            AND work_item.status NOT IN (
                                'succeeded', 'partial', 'failed',
                                'cancelled', 'superseded'
                            )
                      )
                    UNION ALL
                    SELECT
                        consumer.id,
                        consumer.started_at,
                        consumer.created_at,
                        0 AS priority
                    FROM qc_task AS task
                    INNER JOIN qc_app_inspection_publication AS publication
                        ON publication.task_id = task.id
                       AND publication.finalization_epoch = task.finalization_epoch
                    INNER JOIN qc_agent_run AS consumer
                        ON consumer.task_id = task.id
                       AND consumer.agent_type = 'rule_judge'
                       AND consumer.status = 'running'
                       AND consumer.input_context->>'source'
                           = 'app_inspection_publication'
                       AND (consumer.input_context->>'finalization_epoch')::integer
                           = publication.finalization_epoch
                    WHERE task.id = $1::uuid
                      AND task.status IN ('running', 'review')
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      AND EXISTS (
                          SELECT 1
                          FROM qc_app_inspection_publication_work_item AS link
                          INNER JOIN qc_inspection_work_item AS work_item
                              ON work_item.id = link.work_item_id
                          WHERE link.publication_id = publication.id
                            AND work_item.status NOT IN (
                                'succeeded', 'partial', 'failed',
                                'cancelled', 'superseded'
                            )
                      )
                    UNION ALL
                    SELECT run.id, run.started_at, run.created_at, 1 AS priority
                    FROM qc_agent_run AS run
                    INNER JOIN qc_task AS task ON task.id = run.task_id
                    WHERE run.task_id = $1::uuid
                      AND run.status = 'running'
                      AND task.status = 'running'
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                )
                SELECT id::text
                FROM candidate
                ORDER BY priority, started_at DESC NULLS LAST, created_at DESC
                LIMIT 1
                """,
                task_id,
            )
        return str(value) if value is not None else None

    async def finish_publication_consumer_run_if_idle(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> bool:
        """关闭 H5 或 App 当前 epoch 已排空的逻辑消费 Run。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            if await finish_h5_consumer_run_if_idle_on_connection(
                connection,
                task_id=task_id,
                consumer_agent_run_id=consumer_agent_run_id,
            ):
                return True
            return await finish_app_consumer_run_if_idle_on_connection(
                connection,
                task_id=task_id,
                consumer_agent_run_id=consumer_agent_run_id,
            )

    async def finish_h5_consumer_run_if_idle(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> bool:
        """通过统一全链口径关闭当前 epoch 的 H5 逻辑消费 Run。"""

        return await self.finish_publication_consumer_run_if_idle(
            task_id=task_id,
            consumer_agent_run_id=consumer_agent_run_id,
        )

    async def record_consumer_usage(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
        usage: dict[str, Any],
    ) -> bool:
        """把异步规则消费 Run 的 LLM 用量合并写入 Agent Run 审计载荷。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT output_context
                FROM qc_agent_run
                WHERE id = $2::uuid
                  AND task_id = $1::uuid
                  AND agent_type IN ('main', 'rule_judge')
                FOR UPDATE
                """,
                task_id,
                consumer_agent_run_id,
            )
            if row is None:
                return False
            output_context = _json_object(row["output_context"])
            current_usage = output_context.get("usage")
            output_context["usage"] = merge_usage_payloads(
                current_usage if isinstance(current_usage, dict) else {},
                usage,
            )
            await connection.execute(
                """
                UPDATE qc_agent_run
                SET
                    output_context = $3::jsonb,
                    total_ai_calls = $4::integer,
                    total_tokens = $5::integer,
                    total_cost = $6::numeric,
                    updated_at = now()
                WHERE id = $2::uuid
                  AND task_id = $1::uuid
                  AND agent_type IN ('main', 'rule_judge')
                """,
                task_id,
                consumer_agent_run_id,
                json.dumps(output_context, ensure_ascii=False, default=str),
                total_ai_calls_from_usage(output_context["usage"]),
                total_tokens_from_usage(output_context["usage"]),
                total_cost_from_usage(output_context["usage"]),
            )
        return True

    async def reopen_waiting_evidence(self, *, task_id: str) -> int:
        """终结不可补规则，只把已有 PASS 补采的 waiting_evidence 工作项重开。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            # 与 Publication/最终化保持同一锁序，先锁任务再锁补证工作与结果。
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task_id}",
            )
            producer_proofs = await verified_producer_proofs(connection, task_id=task_id)
            rows = await connection.fetch(
                """
                WITH approved_producer_reuse AS MATERIALIZED (
                    SELECT live.work_id, live.rule_id, live.execution_id
                    FROM (__PRODUCER_REUSE_PROOF__) AS live
                    JOIN jsonb_array_elements($2::jsonb) AS verified(proof)
                      ON verified.proof=live.proof
                ), candidate_work AS MATERIALIZED (
                    SELECT work_item.*, task.target_platform AS task_target_platform
                    FROM qc_inspection_work_item AS work_item
                    INNER JOIN qc_task AS task ON task.id = work_item.task_id
                    WHERE task.id = $1::uuid
                      AND work_item.status = 'waiting_evidence'
                      AND work_item.control_mode = 'enforced'
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND task.status IN ('running', 'review')
                      AND task.finalization_state IN (
                          'open', 'exploration_draining', 'session_draining'
                      )
                      -- 尝试次数是否用尽只决定能否重开，不影响不可补请求的终态结算
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_evidence_request AS inflight_request
                          WHERE inflight_request.work_item_id = work_item.id
                            AND inflight_request.status IN (
                                'pending', 'routed', 'running'
                            )
                      )
                    FOR UPDATE OF work_item
                ),
                eligible_work AS MATERIALIZED (
                    SELECT work_item.*
                    FROM candidate_work AS work_item
                    WHERE work_item.attempt_count < work_item.max_attempts
                      AND (
                          lower(work_item.task_target_platform) <> 'h5'
                          OR work_item.last_error_code IS DISTINCT FROM 'H5_EVIDENCE_CONTRACT_INVALID'
                      )
                ),
                bound_request AS MATERIALIZED (
                    SELECT request.*,
                        row_number() OVER (
                            PARTITION BY request.work_item_id, request.rule_id
                            ORDER BY request.created_at DESC, request.request_id DESC
                        ) AS request_order
                    FROM qc_evidence_request AS request
                    INNER JOIN candidate_work AS work_item
                        ON work_item.id = request.work_item_id
                       AND work_item.task_id = request.task_id
                       AND work_item.finalization_epoch = request.finalization_epoch
                    WHERE work_item.page_capture_id = request.target_capture_id
                       OR (
                           lower(work_item.task_target_platform) = 'h5'
                           AND EXISTS (
                               SELECT 1
                               FROM qc_evidence_request AS prior_request
                               INNER JOIN qc_evidence_supplement AS prior_supplement
                                   ON prior_supplement.request_id = prior_request.request_id
                                  AND prior_supplement.manifest_id = prior_request.resolution_manifest_id
                               INNER JOIN qc_evidence_manifest AS prior_manifest
                                   ON prior_manifest.manifest_id = prior_supplement.manifest_id
                                  AND prior_manifest.task_id = request.task_id
                                  AND prior_manifest.quality_status = 'pass'
                               INNER JOIN qc_capture AS prior_capture
                                   ON prior_capture.id = prior_manifest.capture_id
                                  AND prior_capture.task_id = request.task_id
                                  AND prior_capture.agent_run_id = work_item.producer_agent_run_id
                                  AND prior_capture.capture_status = 'captured'
                               WHERE prior_request.work_item_id = request.work_item_id
                                 AND prior_request.task_id = request.task_id
                                 AND prior_request.rule_id = request.rule_id
                                 AND prior_request.finalization_epoch = request.finalization_epoch
                                 AND prior_request.status = 'satisfied'
                                 AND prior_request.created_at < request.created_at
                                 AND (
                                     prior_manifest.capture_id = request.target_capture_id
                                     OR EXISTS (
                                         SELECT 1 FROM qc_capture AS before_capture
                                         INNER JOIN qc_evidence_manifest AS before_manifest
                                             ON before_manifest.capture_id = before_capture.id
                                            AND before_manifest.task_id = request.task_id
                                            AND before_manifest.quality_status = 'pass'
                                         WHERE before_capture.id = request.target_capture_id
                                           AND before_capture.id = prior_capture.parent_capture_id
                                           AND before_capture.task_id = prior_capture.task_id
                                           AND before_capture.agent_run_id = prior_capture.agent_run_id
                                           AND before_capture.surface_session_id = prior_capture.surface_session_id
                                           AND before_capture.action_execution_id = prior_capture.action_execution_id
                                           AND before_capture.capture_role = 'probe_before'
                                           AND prior_capture.capture_role = 'probe_result_replay'
                                           AND before_capture.capture_status = 'captured'
                                     )
                                 )
                           )
                       )
                ),
                effective_request AS MATERIALIZED (
                    SELECT request.*
                    FROM bound_request AS request
                    INNER JOIN candidate_work AS work_item
                        ON work_item.id = request.work_item_id
                    -- H5 重判只能由最新请求决定，不能用旧补采成功覆盖新缺口。
                    WHERE lower(work_item.task_target_platform) IS DISTINCT FROM 'h5'
                       OR request.request_order = 1
                ),
                satisfied_evidence AS MATERIALIZED (
                    SELECT DISTINCT
                        request.work_item_id,
                        request.rule_id
                    FROM effective_request AS request
                    INNER JOIN candidate_work AS work_item
                        ON work_item.id = request.work_item_id
                       AND work_item.task_id = request.task_id
                       AND work_item.finalization_epoch = request.finalization_epoch
                    INNER JOIN qc_inspection_attempt AS attempt
                        ON attempt.id = request.attempt_id
                       AND attempt.work_item_id = work_item.id
                       AND attempt.consumer_agent_run_id = work_item.consumer_agent_run_id
                    INNER JOIN qc_evidence_supplement AS supplement
                        ON supplement.request_id = request.request_id
                       AND supplement.manifest_id = request.resolution_manifest_id
                    INNER JOIN qc_evidence_manifest AS manifest
                        ON manifest.manifest_id = supplement.manifest_id
                       AND manifest.task_id = request.task_id
                       AND manifest.quality_status = 'pass'
                    INNER JOIN qc_capture AS capture
                        ON capture.id = manifest.capture_id
                       AND capture.task_id = request.task_id
                       AND capture.capture_status = 'captured'
                    WHERE request.status = 'satisfied'
                ),
                terminal_request AS MATERIALIZED (
                    SELECT DISTINCT ON (request.work_item_id, request.rule_id)
                        request.request_id,
                        request.work_item_id,
                        work_item.page_capture_id,
                        request.task_id,
                        request.rule_id,
                        request.rule_code,
                        request.target_capture_id,
                        request.status,
                        request.resolution_reason,
                        request.completed_at
                    FROM effective_request AS request
                    INNER JOIN candidate_work AS work_item
                        ON work_item.id = request.work_item_id
                       AND work_item.task_id = request.task_id
                       AND work_item.finalization_epoch = request.finalization_epoch
                    INNER JOIN qc_inspection_attempt AS attempt
                        ON attempt.id = request.attempt_id
                       AND attempt.work_item_id = work_item.id
                       AND attempt.consumer_agent_run_id = work_item.consumer_agent_run_id
                    INNER JOIN qc_inspection_work_item_rule AS member
                        ON member.work_item_id = request.work_item_id
                       AND member.rule_id = request.rule_id
                       AND member.status IN ('pending', 'judged')
                    WHERE request.status IN (
                        'unfulfillable', 'expired', 'cancelled'
                    )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM satisfied_evidence
                          WHERE satisfied_evidence.work_item_id = request.work_item_id
                            AND satisfied_evidence.rule_id = request.rule_id
                      )
                    ORDER BY
                        request.work_item_id,
                        request.rule_id,
                        request.completed_at DESC NULLS LAST,
                        request.request_id DESC
                ),
                terminal_execution_source AS MATERIALIZED (
                    SELECT DISTINCT ON (
                        request.task_id,
                        request.rule_id,
                        work_item.page_capture_id
                    )
                        request.request_id,
                        request.work_item_id,
                        request.task_id,
                        request.rule_id,
                        request.target_capture_id,
                        request.status,
                        request.resolution_reason,
                        request.completed_at,
                        work_item.page_capture_id,
                        work_item.consumer_agent_run_id
                    FROM terminal_request AS request
                    INNER JOIN candidate_work AS work_item
                        ON work_item.id = request.work_item_id
                       AND work_item.task_id = request.task_id
                    ORDER BY
                        request.task_id,
                        request.rule_id,
                        work_item.page_capture_id,
                        request.completed_at DESC NULLS LAST,
                        request.request_id DESC
                ),
                terminal_execution_write AS (
                    INSERT INTO qc_rule_execution AS execution (
                        task_id,
                        rule_id,
                        page_capture_id,
                        source_idempotency_key,
                        agent_run_id,
                        execution_status,
                        judge_strategy,
                        severity,
                        result_summary,
                        problem_desc,
                        judge_input,
                        judge_output,
                        review_status,
                        review_reason,
                        error_code,
                        error_message,
                        started_at,
                        finished_at
                    )
                    SELECT DISTINCT ON (request.task_id, request.rule_id, request.page_capture_id)
                        request.task_id,
                        request.rule_id,
                        request.page_capture_id,
                        'evidence-request-terminal:' || request.request_id,
                        work_item.consumer_agent_run_id,
                        'evidence_insufficient',
                        rule.executor_type,
                        rule.severity,
                        '补采请求已终结，未获得满足规则要求的证据',
                        COALESCE(
                            request.resolution_reason,
                            '证据请求状态为 ' || request.status
                        ),
                        jsonb_build_object(
                            'schema_version', 'v1',
                            'source', 'evidence_request_terminalization',
                            'request_id', request.request_id,
                            'request_status', request.status,
                            'target_capture_id', request.target_capture_id::text
                        ),
                        jsonb_build_object(
                            'schema_version', 'v1',
                            'conclusion', 'evidence_insufficient',
                            'reason_code', 'evidence_request_' || request.status,
                            'resolution_reason', request.resolution_reason
                        ),
                        'pending',
                        COALESCE(
                            request.resolution_reason,
                            '补采请求未能获得合格证据'
                        ),
                        'EVIDENCE_REQUEST_' || upper(request.status),
                        COALESCE(
                            request.resolution_reason,
                            '补采请求未能获得合格证据'
                        ),
                        request.completed_at,
                        request.completed_at
                    FROM terminal_execution_source AS request
                    INNER JOIN candidate_work AS work_item
                        ON work_item.id = request.work_item_id
                       AND work_item.task_id = request.task_id
                       AND work_item.page_capture_id = request.page_capture_id
                    INNER JOIN qc_rule AS rule ON rule.id = request.rule_id
                    ORDER BY
                        request.task_id,
                        request.rule_id,
                        request.page_capture_id,
                        request.completed_at DESC NULLS LAST,
                        request.request_id DESC
                    ON CONFLICT (task_id, rule_id, page_capture_id)
                    DO UPDATE SET
                        agent_run_id = EXCLUDED.agent_run_id,
                        execution_status = EXCLUDED.execution_status,
                        judge_strategy = EXCLUDED.judge_strategy,
                        severity = EXCLUDED.severity,
                        result_summary = EXCLUDED.result_summary,
                        problem_desc = EXCLUDED.problem_desc,
                        judge_input = EXCLUDED.judge_input,
                        judge_output = EXCLUDED.judge_output || jsonb_build_object(
                            'superseded_placeholder', jsonb_build_object(
                                'agent_run_id', execution.agent_run_id,
                                'judge_output', execution.judge_output
                            )
                        ),
                        review_status = EXCLUDED.review_status,
                        review_reason = EXCLUDED.review_reason,
                        error_code = EXCLUDED.error_code,
                        error_message = EXCLUDED.error_message,
                        started_at = EXCLUDED.started_at,
                        finished_at = EXCLUDED.finished_at,
                        updated_at = now()
                    -- 只替换 H5 主 Run 的未执行占位，不覆盖已判结论或其他平台。
                    WHERE execution.execution_status = 'skipped'
                      AND execution.judge_output->>'reason' = 'candidate_not_executed'
                      AND EXISTS (
                          SELECT 1 FROM candidate_work AS work_item
                          WHERE lower(work_item.task_target_platform) = 'h5'
                            AND work_item.task_id = execution.task_id
                            AND work_item.page_capture_id = execution.page_capture_id
                            AND work_item.producer_agent_run_id = execution.agent_run_id
                            AND work_item.consumer_agent_run_id = EXCLUDED.agent_run_id
                      )
                    RETURNING
                        execution.id,
                        execution.task_id,
                        execution.rule_id,
                        execution.page_capture_id,
                        execution.agent_run_id,
                        execution.observation_packet_id,
                        execution.execution_status,
                        true AS terminal_gap
                ),
                terminal_execution AS MATERIALIZED (
                    SELECT * FROM terminal_execution_write
                    UNION ALL
                    SELECT execution.id, execution.task_id, execution.rule_id,
                        execution.page_capture_id, execution.agent_run_id,
                        execution.observation_packet_id, execution.execution_status,
                        (execution.execution_status = 'evidence_insufficient'
                         AND execution.judge_input->>'source' = 'evidence_request_terminalization')
                            AS terminal_gap
                    FROM qc_rule_execution AS execution
                    INNER JOIN terminal_execution_source AS request
                        ON request.task_id = execution.task_id
                       AND request.rule_id = execution.rule_id
                       AND request.page_capture_id = execution.page_capture_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM terminal_execution_write AS written
                        WHERE written.id = execution.id
                    )
                ),
                review_item AS (
                    INSERT INTO qc_review_item (
                        rule_execution_id,
                        task_id,
                        trigger_reason,
                        status,
                        priority,
                        review_reason
                    )
                    SELECT DISTINCT ON (execution.id)
                        execution.id,
                        execution.task_id,
                        'evidence_insufficient',
                        'pending',
                        CASE rule.severity
                            WHEN 'critical' THEN 10
                            WHEN 'high' THEN 20
                            WHEN 'medium' THEN 50
                            ELSE 100
                        END,
                        COALESCE(
                            request.resolution_reason,
                            '补采请求未能获得合格证据'
                        )
                    FROM terminal_execution AS execution
                    INNER JOIN terminal_execution_source AS request
                        ON request.task_id = execution.task_id
                       AND request.rule_id = execution.rule_id
                       AND request.page_capture_id = execution.page_capture_id
                       AND request.consumer_agent_run_id = execution.agent_run_id
                    INNER JOIN qc_rule AS rule ON rule.id = execution.rule_id
                    WHERE execution.execution_status = 'evidence_insufficient'
                    ON CONFLICT (rule_execution_id) DO UPDATE SET
                        trigger_reason = EXCLUDED.trigger_reason,
                        status = EXCLUDED.status,
                        priority = EXCLUDED.priority,
                        review_reason = EXCLUDED.review_reason,
                        reviewed_at = NULL,
                        updated_at = now()
                    RETURNING id
                ),
                usable_terminal_execution AS MATERIALIZED (
                    SELECT execution.*, work_item.id AS work_item_id
                    FROM terminal_execution AS execution
                    INNER JOIN candidate_work AS work_item
                        ON work_item.task_id=execution.task_id
                       AND work_item.page_capture_id=execution.page_capture_id
                    WHERE (
                        work_item.consumer_agent_run_id=execution.agent_run_id
                        AND (lower(work_item.task_target_platform)<>'h5' OR execution.terminal_gap)
                    ) OR (
                        lower(work_item.task_target_platform)='h5'
                        AND EXISTS (
                            SELECT 1 FROM approved_producer_reuse AS proof
                            WHERE proof.work_id=work_item.id AND proof.rule_id=execution.rule_id
                              AND proof.execution_id=execution.id
                        )
                    )
                ),
                terminal_member AS (
                    UPDATE qc_inspection_work_item_rule AS member
                    SET
                        status = CASE execution.execution_status
                            WHEN 'evidence_insufficient' THEN 'evidence_insufficient'
                            WHEN 'skipped' THEN 'skipped'
                            WHEN 'error' THEN 'error'
                            ELSE 'written'
                        END,
                        observation_packet_id = execution.observation_packet_id,
                        rule_execution_id = execution.id,
                        last_error_code = CASE
                            WHEN execution.execution_status = 'evidence_insufficient'
                            THEN 'EVIDENCE_REQUEST_TERMINAL'
                            ELSE NULL
                        END,
                        last_error_message = request.resolution_reason,
                        updated_at = now()
                    FROM usable_terminal_execution AS execution
                    INNER JOIN candidate_work AS work_item
                        ON work_item.id=execution.work_item_id
                    INNER JOIN terminal_request AS request
                        ON request.work_item_id = work_item.id
                       AND request.task_id = execution.task_id
                       AND request.rule_id = execution.rule_id
                       AND request.page_capture_id = execution.page_capture_id
                    WHERE member.work_item_id = request.work_item_id
                      AND member.rule_id = request.rule_id
                      AND member.status IN ('pending', 'judged')
                    RETURNING member.work_item_id, member.rule_id
                ),
                closed_work AS (
                    UPDATE qc_inspection_work_item AS work_item
                    SET
                        status = CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM usable_terminal_execution AS execution
                                WHERE execution.work_item_id = work_item.id
                                  AND execution.execution_status IN (
                                      'evidence_insufficient', 'skipped', 'error'
                                  )
                            ) THEN 'partial'
                            ELSE 'succeeded'
                        END,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        finished_at = now(),
                        row_version = work_item.row_version + 1,
                        updated_at = now()
                    FROM candidate_work AS candidate
                    WHERE work_item.id = candidate.id
                      AND EXISTS (
                          SELECT 1
                          FROM terminal_request
                          WHERE terminal_request.work_item_id = work_item.id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_inspection_work_item_rule AS member
                          WHERE member.work_item_id = work_item.id
                            AND member.status IN ('pending', 'judged')
                            AND NOT EXISTS (
                                SELECT 1
                                FROM usable_terminal_execution AS execution
                                WHERE execution.work_item_id = work_item.id
                                  AND execution.rule_id = member.rule_id
                            )
                      )
                    RETURNING work_item.id
                ),
                reopened AS (
                    UPDATE qc_inspection_work_item AS work_item
                    SET
                        status = 'pending',
                        next_attempt_at = now(),
                        row_version = work_item.row_version + 1,
                        updated_at = now()
                    FROM eligible_work
                    WHERE work_item.id = eligible_work.id
                      -- 仅仍有重试额度的工作项可重开；耗尽项只走上方终态结算。
                      AND work_item.attempt_count < work_item.max_attempts
                      AND eligible_work.attempt_count < eligible_work.max_attempts
                      AND EXISTS (
                          SELECT 1
                          FROM satisfied_evidence
                          INNER JOIN qc_inspection_work_item_rule AS member
                              ON member.work_item_id = satisfied_evidence.work_item_id
                             AND member.rule_id = satisfied_evidence.rule_id
                             AND member.status IN ('pending', 'judged')
                          WHERE satisfied_evidence.work_item_id = work_item.id
                      )
                RETURNING
                    work_item.id::text,
                    work_item.task_id::text,
                    work_item.page_capture_id::text,
                    work_item.protocol_version,
                    work_item.batch_key,
                    work_item.strategy,
                    work_item.category,
                    work_item.session_dependency,
                    work_item.input_hash,
                    work_item.control_mode,
                    work_item.row_version
                )
                SELECT * FROM reopened
                """.replace("__PRODUCER_REUSE_PROOF__", PRODUCER_REUSE_PROOF_SQL),
                task_id,
                producer_proofs,
            )
            for row in rows:
                work_item_id = str(row["id"])
                row_version = int(row["row_version"])
                protocol_version = str(row["protocol_version"])
                input_hash = str(row["input_hash"])
                await insert_outbox_event(
                    connection,
                    aggregate_type="inspection_work_item",
                    aggregate_id=work_item_id,
                    envelope=build_protocol_envelope(
                        message_type=INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE,
                        payload_protocol_version=protocol_version,
                        idempotency_key=f"evidence-reopen:{work_item_id}:{row_version}",
                        task_id=str(row["task_id"]),
                        aggregate_type="inspection_work_item",
                        aggregate_id=work_item_id,
                        aggregate_version=row_version,
                        occurred_at=datetime.now(UTC),
                        payload={
                            "protocol_version": protocol_version,
                            "work_item_id": work_item_id,
                            "task_id": str(row["task_id"]),
                            "page_capture_id": str(row["page_capture_id"]),
                            "batch_key": str(row["batch_key"]),
                            "strategy": str(row["strategy"]),
                            "category": row["category"],
                            "session_dependency": row["session_dependency"],
                            "control_mode": str(row["control_mode"]),
                            "input_hash": input_hash,
                            "reopen_reason": "evidence_resolved",
                        },
                        correlation_id=str(row["task_id"]),
                    ),
                )
        return len(rows)

    async def settle_stalled_work_for_closure(
        self,
        *,
        task_id: str,
        active_session_agent_run_id: str | None = None,
        snapshot_generation_closed: bool = False,
        active_session_exhausted_only: bool = False,
    ) -> InspectionWorkItemClosureSettlement:
        """把收口阶段再也推进不了的工作项按证据不足结算，让门禁拿得到终态。

        两类会被结算：
        1. 收口中（session_draining），或调用方已确认不再生成新 Snapshot 时，自己不可能
           再动的——没有在途补采请求，且不满足重开条件；retry_wait 还能重试的、
           waiting_evidence 已拿到合格补采且还有重试次数的都不碰。
        2. 传了 ``active_session_agent_run_id`` 时，该 Run 名下依赖活会话的非终态工作项——
           浏览器会话马上要关，关掉之后不允许跨会话重放交互状态，再等也拿不到证据。
           H5 页面检查点额外传 ``active_session_exhausted_only``，只结算已耗尽的无租约工作。

        结算写正式的证据不足规则执行并留放弃原因（专项三「预算耗尽保留缺口和放弃原因」），
        不把缺失证据伪装成通过；任务已进入 finalizing/sealed 时不结算（写执行有触发器拦）。
        """
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            producer_proofs = await verified_producer_proofs(connection, task_id=task_id)
            rows = await connection.fetch(
                """
                WITH approved_reuse AS MATERIALIZED (
                    SELECT live.work_id,live.rule_id,live.execution_id
                    FROM (__PRODUCER_REUSE_PROOF__) live
                    JOIN jsonb_array_elements($5::jsonb) verified(proof) ON verified.proof=live.proof
                ), stalled_work AS MATERIALIZED (
                    SELECT
                        work_item.*,
                        task.target_platform AS task_target_platform,
                        CASE
                            WHEN exhausted_budget.task_id IS NOT NULL
                                AND exhausted_budget.admitted_calls < exhausted_budget.max_calls
                                THEN 'h5_llm_deadline_exhausted'
                            WHEN exhausted_budget.task_id IS NOT NULL
                                THEN 'h5_llm_call_budget_exhausted'
                            WHEN $2::uuid IS NOT NULL
                                AND NOT $4::boolean
                                AND work_item.session_dependency = 'active_session'
                                AND work_item.producer_agent_run_id = $2::uuid
                                AND work_item.session_owner_agent_run_id = $2::uuid
                                THEN 'active_session_closing'
                            WHEN work_item.status = 'retry_wait' THEN 'retry_budget_exhausted'
                            WHEN EXISTS (
                                SELECT 1
                                FROM qc_evidence_request AS request
                                WHERE request.work_item_id = work_item.id
                                  AND request.status = 'satisfied'
                            ) THEN 'supplement_satisfied_but_retry_exhausted'
                            WHEN EXISTS (
                                SELECT 1
                                FROM qc_evidence_request AS request
                                WHERE request.work_item_id = work_item.id
                            ) THEN 'supplement_unfulfillable'
                            ELSE 'no_supplement_request'
                        END AS stall_reason,
                        CASE
                            WHEN exhausted_budget.task_id IS NOT NULL
                                THEN 'H5 冻结物理调用预算或截止已耗尽，按证据不足结算'
                            WHEN $2::uuid IS NOT NULL
                                AND NOT $4::boolean
                                AND work_item.session_dependency = 'active_session'
                                AND work_item.producer_agent_run_id = $2::uuid
                                AND work_item.session_owner_agent_run_id = $2::uuid
                                THEN '浏览器会话即将关闭，活会话规则无法再取证，按证据不足结算'
                            WHEN work_item.status = 'retry_wait'
                                THEN '重试次数已用尽，任务收口前按证据不足结算'
                            WHEN EXISTS (
                                SELECT 1
                                FROM qc_evidence_request AS request
                                WHERE request.work_item_id = work_item.id
                                  AND request.status = 'satisfied'
                            ) THEN '补采证据已获得但重试次数用尽，任务收口前按证据不足结算'
                            WHEN EXISTS (
                                SELECT 1
                                FROM qc_evidence_request AS request
                                WHERE request.work_item_id = work_item.id
                            ) THEN '补采请求已终结且无法再取证，任务收口前按证据不足结算'
                            ELSE '等待补采但没有可执行的补采请求，任务收口前按证据不足结算'
                        END AS stall_reason_text
                    FROM qc_inspection_work_item AS work_item
                    INNER JOIN qc_task AS task ON task.id = work_item.task_id
                    LEFT JOIN qc_h5_llm_call_budget AS exhausted_budget
                      ON lower(task.target_platform) = 'h5'
                     AND exhausted_budget.task_id = task.id
                     AND exhausted_budget.finalization_epoch = task.finalization_epoch
                     AND exhausted_budget.agent_run_id = work_item.producer_agent_run_id
                     AND (
                       exhausted_budget.admitted_calls >= exhausted_budget.max_calls
                       OR exhausted_budget.deadline <= now()
                     )
                    WHERE work_item.task_id = $1::uuid
                      AND work_item.control_mode = 'enforced'
                      AND work_item.finalization_epoch = task.finalization_epoch
                      AND task.finalization_state NOT IN ('finalizing', 'sealed')
                      AND (
                          NOT $4::boolean
                          OR (
                              lower(task.target_platform) = 'h5'
                              AND work_item.session_dependency = 'active_session'
                              AND work_item.status IN ('retry_wait', 'waiting_evidence')
                              AND work_item.attempt_count >= work_item.max_attempts
                              AND work_item.lease_token IS NULL
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM qc_evidence_request AS inflight
                                  WHERE inflight.work_item_id = work_item.id
                                    AND inflight.status IN ('pending', 'routed', 'running')
                              )
                          )
                      )
                      AND work_item.status NOT IN (
                          'succeeded', 'partial', 'failed', 'cancelled', 'superseded'
                      )
                      -- H5 所有收口分支共用资源屏障，显式关闭会话也不能夺取在途工作。
                      AND (
                          lower(task.target_platform) <> 'h5'
                          OR (
                              (work_item.lease_token IS NULL OR work_item.lease_expires_at <= now())
                              AND NOT EXISTS (
                                  SELECT 1 FROM qc_evidence_request AS inflight
                                  WHERE inflight.work_item_id = work_item.id
                                    AND inflight.status IN ('pending', 'routed', 'running')
                              )
                          )
                      )
                      -- 晚到发布也仍可能补齐证据，不能提前写入缺口结论。
                      AND (
                          lower(task.target_platform) <> 'h5'
                          OR NOT EXISTS (
                              SELECT 1 FROM qc_h5_inspection_publication AS publication
                              WHERE publication.task_id = task.id
                                AND publication.finalization_epoch = task.finalization_epoch
                                AND publication.status IN ('pending', 'leased', 'retry_wait')
                          )
                      )
                      AND EXISTS (
                          SELECT 1
                          FROM qc_inspection_work_item_rule AS member
                          WHERE member.work_item_id = work_item.id
                            AND member.status IN ('pending', 'judged')
                      )
                      AND (
                          (
                              exhausted_budget.task_id IS NOT NULL
                              AND NOT $4::boolean
                              AND ($3::boolean OR task.finalization_state = 'session_draining')
                              AND (
                                  work_item.session_dependency = 'snapshot'
                                  OR (
                                      work_item.session_dependency = 'active_session'
                                      AND work_item.session_owner_agent_run_id = work_item.producer_agent_run_id
                                      AND EXISTS (
                                          SELECT 1 FROM qc_capture AS capture
                                          INNER JOIN qc_surface_session AS session
                                            ON session.session_id = capture.surface_session_id
                                           AND session.task_id = capture.task_id
                                           AND session.finalization_epoch = work_item.finalization_epoch
                                          WHERE capture.id = work_item.page_capture_id
                                            AND capture.task_id = work_item.task_id
                                            AND capture.agent_run_id = work_item.producer_agent_run_id
                                            AND session.status = 'closed'
                                      )
                                  )
                              )
                              AND work_item.status IN ('pending', 'retry_wait', 'waiting_evidence', 'leased', 'running')
                              AND (work_item.lease_token IS NULL OR work_item.lease_expires_at <= now())
                              AND NOT EXISTS (
                                  SELECT 1 FROM qc_evidence_request AS inflight
                                  WHERE inflight.work_item_id = work_item.id
                                    AND inflight.status IN ('pending', 'routed', 'running')
                              )
                          )
                          OR
                          -- 浏览器会话即将关闭：该 Run 的活会话工作项不可能再取证
                          (
                              $2::uuid IS NOT NULL
                              AND work_item.session_dependency = 'active_session'
                              AND work_item.producer_agent_run_id = $2::uuid
                              AND work_item.session_owner_agent_run_id = $2::uuid
                          )
                          -- 收口中，或 H5 主 Run 已明确停止生成新快照，且工作自己再也推进不了
                          OR (
                              (
                                  task.finalization_state = 'session_draining'
                                  OR $3::boolean
                              )
                              AND work_item.status IN ('waiting_evidence', 'retry_wait')
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM qc_evidence_request AS inflight
                                  WHERE inflight.work_item_id = work_item.id
                                    AND inflight.status IN ('pending', 'routed', 'running')
                              )
                              AND NOT (
                                  work_item.attempt_count < work_item.max_attempts
                                  AND (
                                      lower(task.target_platform) <> 'h5'
                                      OR work_item.last_error_code IS DISTINCT FROM 'H5_EVIDENCE_CONTRACT_INVALID'
                                  )
                                  AND (
                                      work_item.status = 'retry_wait'
                                      OR EXISTS (
                                          SELECT 1
                                          FROM qc_evidence_request AS satisfied
                                          WHERE satisfied.work_item_id = work_item.id
                                            AND satisfied.status = 'satisfied'
                                      )
                                  )
                              )
                          )
                      )
                    FOR UPDATE OF work_item
                ),
                settled_execution AS (
                    INSERT INTO qc_rule_execution AS execution (
                        task_id, rule_id, page_capture_id, source_idempotency_key,
                        agent_run_id, execution_status, judge_strategy, severity,
                        result_summary, problem_desc, judge_input, judge_output,
                        review_status, review_reason, error_code, error_message,
                        started_at, finished_at
                    )
                    SELECT DISTINCT ON (
                        work_item.task_id, member.rule_id, work_item.page_capture_id
                    )
                        work_item.task_id,
                        member.rule_id,
                        work_item.page_capture_id,
                        'inspection-closure-settlement:' || work_item.id::text
                            || ':' || member.rule_id::text,
                        COALESCE(
                            work_item.consumer_agent_run_id,
                            work_item.session_owner_agent_run_id,
                            work_item.producer_agent_run_id
                        ),
                        'evidence_insufficient',
                        rule.executor_type,
                        rule.severity,
                        '任务收口前结算：规则未能取得可判定证据',
                        work_item.stall_reason_text,
                        jsonb_build_object(
                            'schema_version', 'v1',
                            'source', 'inspection_work_item_closure_settlement',
                            'work_item_id', work_item.id::text,
                            'work_item_status', work_item.status,
                            'attempt_count', work_item.attempt_count,
                            'max_attempts', work_item.max_attempts
                        ),
                        jsonb_build_object(
                            'schema_version', 'v1',
                            'conclusion', 'evidence_insufficient',
                            'reason_code', 'inspection_closure_' || work_item.stall_reason,
                            'closure_settlement', jsonb_build_object(
                                'stall_reason', work_item.stall_reason,
                                'session_dependency', work_item.session_dependency
                            )
                        ),
                        'pending',
                        work_item.stall_reason_text,
                        'INSPECTION_CLOSURE_SETTLEMENT',
                        work_item.stall_reason_text,
                        now(),
                        now()
                    FROM stalled_work AS work_item
                    INNER JOIN qc_inspection_work_item_rule AS member
                        ON member.work_item_id = work_item.id
                       AND member.status IN ('pending', 'judged')
                    INNER JOIN qc_rule AS rule ON rule.id = member.rule_id
                    ORDER BY
                        work_item.task_id,
                        member.rule_id,
                        work_item.page_capture_id,
                        work_item.updated_at DESC,
                        work_item.id DESC
                    ON CONFLICT (task_id, rule_id, page_capture_id)
                    DO UPDATE SET updated_at = execution.updated_at
                    RETURNING
                        execution.id,
                        execution.task_id,
                        execution.rule_id,
                        execution.page_capture_id,
                        execution.observation_packet_id,
                        execution.execution_status,
                        execution.review_reason,
                        execution.agent_run_id,
                        execution.evidence_input_set_id,
                        (execution.execution_status='evidence_insufficient'
                         AND execution.judge_input->>'source' IN (
                             'evidence_request_terminalization','inspection_work_item_closure_settlement'
                         )) AS terminal_gap
                ), usable_settled_execution AS MATERIALIZED (
                    SELECT execution.*,work.id AS work_item_id
                    FROM settled_execution execution JOIN stalled_work work
                      ON work.task_id=execution.task_id AND work.page_capture_id=execution.page_capture_id
                    WHERE lower(work.task_target_platform)<>'h5' OR (
                        execution.terminal_gap AND execution.agent_run_id=COALESCE(
                            work.consumer_agent_run_id,work.session_owner_agent_run_id,
                            work.producer_agent_run_id)
                    ) OR EXISTS (
                        SELECT 1 FROM approved_reuse proof WHERE proof.work_id=work.id
                        AND proof.rule_id=execution.rule_id AND proof.execution_id=execution.id
                    )
                ), unverified_gap AS (
                    UPDATE qc_inspection_work_item_rule member
                    SET status='evidence_insufficient', rule_execution_id=NULL, observation_packet_id=NULL,
                        last_error_code='H5_RESULT_INPUT_CONTRACT_UNVERIFIED',
                        last_error_message=jsonb_build_object(
                            'schema_version','h5-inspection-contract-gap.v1',
                            'reason','原输入合同不能核验；保留原结论，本成员以证据不足终结',
                            'work_item_id',work.id::text, 'rule_id',member.rule_id::text,
                            'expected_contract_hash',member.contract_hash,
                            'original_input_set_id',member.evidence_input_set_id,
                            'conflicting_execution_id',execution.id::text,
                            'conflicting_input_set_id',execution.evidence_input_set_id,
                            'finalization_epoch',work.finalization_epoch
                        )::text, updated_at=now()
                    FROM stalled_work work,settled_execution execution
                    WHERE lower(work.task_target_platform)='h5' AND member.work_item_id=work.id
                      AND member.status IN ('pending','judged') AND member.rule_id=execution.rule_id
                      AND execution.task_id=work.task_id AND execution.page_capture_id=work.page_capture_id
                      AND NOT EXISTS (
                          SELECT 1 FROM usable_settled_execution accepted
                          WHERE accepted.work_item_id=work.id AND accepted.rule_id=member.rule_id
                      )
                    RETURNING member.work_item_id,member.rule_id
                ),
                settled_review AS (
                    INSERT INTO qc_review_item (
                        rule_execution_id, task_id, trigger_reason, status, priority, review_reason
                    )
                    SELECT DISTINCT ON (execution.id)
                        execution.id,
                        execution.task_id,
                        'evidence_insufficient',
                        'pending',
                        CASE rule.severity
                            WHEN 'critical' THEN 10
                            WHEN 'high' THEN 20
                            WHEN 'medium' THEN 50
                            ELSE 100
                        END,
                        execution.review_reason
                    FROM usable_settled_execution AS execution
                    INNER JOIN qc_rule AS rule ON rule.id = execution.rule_id
                    WHERE execution.execution_status = 'evidence_insufficient'
                    ON CONFLICT (rule_execution_id) DO UPDATE SET
                        trigger_reason = EXCLUDED.trigger_reason,
                        status = EXCLUDED.status,
                        priority = EXCLUDED.priority,
                        review_reason = EXCLUDED.review_reason,
                        reviewed_at = NULL,
                        updated_at = now()
                    RETURNING id
                ),
                settled_member AS (
                    UPDATE qc_inspection_work_item_rule AS member
                    SET
                        status = CASE execution.execution_status
                            WHEN 'evidence_insufficient' THEN 'evidence_insufficient'
                            WHEN 'skipped' THEN 'skipped'
                            WHEN 'error' THEN 'error'
                            ELSE 'written'
                        END,
                        observation_packet_id = execution.observation_packet_id,
                        rule_execution_id = execution.id,
                        last_error_code = CASE
                            WHEN execution.execution_status = 'evidence_insufficient'
                            THEN 'INSPECTION_CLOSURE_SETTLEMENT'
                            ELSE NULL
                        END,
                        last_error_message = CASE
                            WHEN execution.execution_status = 'evidence_insufficient'
                            THEN execution.review_reason
                            ELSE NULL
                        END,
                        updated_at = now()
                    FROM usable_settled_execution AS execution
                    INNER JOIN stalled_work AS work_item ON work_item.id=execution.work_item_id
                    WHERE member.work_item_id = work_item.id
                      AND member.rule_id = execution.rule_id
                      AND member.status IN ('pending', 'judged')
                    RETURNING member.work_item_id, member.rule_id
                ),
                settled_attempt AS (
                    UPDATE qc_inspection_attempt AS attempt
                    SET
                        status = 'abandoned',
                        error_code = COALESCE(attempt.error_code, 'INSPECTION_CLOSURE_SETTLEMENT'),
                        error_message = COALESCE(
                            attempt.error_message,
                            '任务收口前结算，未完成的尝试不再继续'
                        ),
                        finished_at = COALESCE(attempt.finished_at, now()),
                        updated_at = now()
                    FROM stalled_work AS work_item
                    WHERE attempt.work_item_id = work_item.id
                      AND attempt.status IN ('running', 'judged')
                    RETURNING attempt.id
                ),
                closed_work AS (
                    UPDATE qc_inspection_work_item AS work_item
                    SET
                        status = CASE
                            WHEN EXISTS (
                                SELECT 1 FROM unverified_gap gap WHERE gap.work_item_id=work_item.id
                            ) OR EXISTS (
                                SELECT 1
                                FROM usable_settled_execution AS execution
                                WHERE execution.work_item_id = work_item.id
                                  AND execution.execution_status IN (
                                      'evidence_insufficient', 'skipped', 'error'
                                  )
                            ) THEN 'partial'
                            ELSE 'succeeded'
                        END,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = 'INSPECTION_CLOSURE_SETTLEMENT',
                        last_error_message = stalled.stall_reason_text,
                        finished_at = now(),
                        row_version = work_item.row_version + 1,
                        updated_at = now()
                    FROM stalled_work AS stalled
                    WHERE work_item.id = stalled.id
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_inspection_work_item_rule AS member
                          WHERE member.work_item_id = work_item.id
                            AND member.status IN ('pending', 'judged')
                            AND NOT EXISTS (
                                SELECT 1
                                FROM settled_member
                                WHERE settled_member.work_item_id = member.work_item_id
                                  AND settled_member.rule_id = member.rule_id
                            )
                            AND NOT EXISTS (
                                SELECT 1 FROM unverified_gap gap
                                WHERE gap.work_item_id=member.work_item_id AND gap.rule_id=member.rule_id
                            )
                      )
                    RETURNING work_item.id, stalled.stall_reason
                )
                SELECT
                    closed_work.id::text AS work_item_id,
                    closed_work.stall_reason,
                    (
                        SELECT count(*)
                        FROM settled_member
                        WHERE settled_member.work_item_id = closed_work.id
                    )::integer + (
                        SELECT count(*) FROM unverified_gap gap WHERE gap.work_item_id=closed_work.id
                    )::integer AS settled_rule_count
                FROM closed_work
                """.replace("__PRODUCER_REUSE_PROOF__", PRODUCER_REUSE_PROOF_SQL),
                task_id,
                active_session_agent_run_id,
                snapshot_generation_closed,
                active_session_exhausted_only,
                producer_proofs,
            )
        reason_counts: dict[str, int] = {}
        settled_rule_count = 0
        for row in rows:
            reason = str(row["stall_reason"])
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            settled_rule_count += int(row["settled_rule_count"])
        return InspectionWorkItemClosureSettlement(
            task_id=task_id,
            settled_work_item_count=len(rows),
            settled_rule_count=settled_rule_count,
            reason_counts=reason_counts,
        )

    async def has_nonterminal_work(self, *, task_id: str) -> bool:
        """报告收口前检查任务是否仍有持久化质检工作。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM qc_inspection_work_item
                    WHERE task_id = $1::uuid
                      AND control_mode = 'enforced'
                      AND status NOT IN (
                          'succeeded', 'partial', 'failed',
                          'cancelled', 'superseded'
                      )
                )
                """,
                task_id,
            )
        return bool(value)

    async def count_inflight_leased_work(self, *, task_id: str) -> int:
        """统计有效判定租约及 H5 在途发布、补证，供收口门禁有界等待。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT count(*) + (
                    SELECT count(*)
                    FROM qc_h5_inspection_publication AS publication
                    INNER JOIN qc_task AS task ON task.id = publication.task_id
                    WHERE publication.task_id = $1::uuid
                      AND lower(task.target_platform) = 'h5'
                      AND publication.finalization_epoch = task.finalization_epoch
                      AND publication.status IN ('pending', 'leased', 'retry_wait')
                ) + (
                    SELECT count(*)
                    FROM qc_evidence_request AS request
                    INNER JOIN qc_task AS task ON task.id = request.task_id
                    WHERE request.task_id = $1::uuid
                      AND lower(task.target_platform) = 'h5'
                      AND request.finalization_epoch = task.finalization_epoch
                      AND request.status IN ('pending', 'routed', 'running')
                )
                FROM qc_inspection_work_item
                WHERE task_id = $1::uuid
                  AND control_mode = 'enforced'
                  AND status IN ('leased', 'running')
                  AND lease_expires_at > now()
                """,
                task_id,
            )
        return int(value or 0)

    async def count_claimable_pending_work(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> int:
        """统计消费者仍可领取推进的积压工作项，供收口门禁宽限等待。

        与 claim 的消费者身份和可见性条件对齐；尤其 active_session 只能由
        会话属主 Run 领取，不能因属主仍在运行就误算给另一个 Publication Judge。
        """
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT count(*)
                FROM qc_inspection_work_item AS work_item
                INNER JOIN qc_task AS task ON task.id = work_item.task_id
                WHERE work_item.task_id = $1::uuid
                  AND (
                      task.status = 'running'
                      OR (
                          task.status = 'review'
                          AND task.finalization_state = 'session_draining'
                          AND EXISTS (
                              SELECT 1
                              FROM qc_agent_run AS recovery_consumer
                              WHERE recovery_consumer.id = $2::uuid
                                AND recovery_consumer.task_id = work_item.task_id
                                AND recovery_consumer.agent_type = 'rule_judge'
                                AND recovery_consumer.status = 'running'
                                AND recovery_consumer.input_context->>'source' IN (
                                    'h5_inspection_publication',
                                    'app_inspection_publication'
                                )
                          )
                      )
                  )
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND work_item.finalization_epoch = task.finalization_epoch
                  AND work_item.control_mode = 'enforced'
                  AND EXISTS (
                      SELECT 1
                      FROM qc_agent_run AS consumer
                      WHERE consumer.id = $2::uuid
                        AND consumer.task_id = work_item.task_id
                        AND consumer.status = 'running'
                  )
                  AND work_item.attempt_count < work_item.max_attempts
                  AND (
                      (
                          work_item.status IN ('pending', 'retry_wait')
                          AND work_item.next_attempt_at <= now()
                      )
                      OR (
                          work_item.status IN ('leased', 'running')
                          AND work_item.lease_expires_at <= now()
                      )
                  )
                  AND (
                      work_item.session_dependency = 'snapshot'
                      OR work_item.session_owner_agent_run_id = $2::uuid
                  )
                """,
                task_id,
                consumer_agent_run_id,
            )
        return int(value or 0)

    async def count_waitable_nonterminal_work(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> int:
        """统计消费者可继续推进的积压，避免未来重试窗口被误判为空闲。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT count(*)
                FROM qc_inspection_work_item AS work_item
                INNER JOIN qc_task AS task ON task.id = work_item.task_id
                WHERE work_item.task_id = $1::uuid
                  AND (
                      task.status = 'running'
                      OR (
                          task.status = 'review'
                          AND task.finalization_state = 'session_draining'
                          AND EXISTS (
                              SELECT 1
                              FROM qc_agent_run AS recovery_consumer
                              WHERE recovery_consumer.id = $2::uuid
                                AND recovery_consumer.task_id = work_item.task_id
                                AND recovery_consumer.agent_type = 'rule_judge'
                                AND recovery_consumer.status = 'running'
                                AND recovery_consumer.input_context->>'source' IN (
                                    'h5_inspection_publication',
                                    'app_inspection_publication'
                                )
                          )
                      )
                  )
                  AND task.finalization_state IN (
                      'open', 'exploration_draining', 'session_draining'
                  )
                  AND work_item.finalization_epoch = task.finalization_epoch
                  AND work_item.control_mode = 'enforced'
                  AND EXISTS (
                      SELECT 1
                      FROM qc_agent_run AS consumer
                      WHERE consumer.id = $2::uuid
                        AND consumer.task_id = work_item.task_id
                        AND consumer.status = 'running'
                  )
                  AND work_item.attempt_count < work_item.max_attempts
                  AND (
                      work_item.status IN ('pending', 'retry_wait')
                      OR (
                          work_item.status IN ('leased', 'running')
                          AND work_item.lease_expires_at <= now()
                      )
                  )
                  AND (
                      work_item.session_dependency = 'snapshot'
                      OR work_item.session_owner_agent_run_id = $2::uuid
                  )
                """,
                task_id,
                consumer_agent_run_id,
            )
        return int(value or 0)

    async def terminate_web_contract_incompatibility(self, *, command) -> int:
        """只向已认证 Web 多用途合同不兼容暴露无正式结果的原子技术收口。"""
        from services.api.app.infrastructure.db.web_sequence_contract_terminalization import (
            terminate_contract_incompatibility,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            return await terminate_contract_incompatibility(connection, command)

    async def cancel_nonterminal(self, *, task_id: str, reason: str) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"inspection-work-cancel:{task_id}",
            )
            await connection.execute(
                """
                UPDATE qc_inspection_work_item_rule AS member
                SET
                    status = 'skipped',
                    last_error_code = 'WORKFLOW_CANCELED',
                    last_error_message = $2,
                    updated_at = now()
                FROM qc_inspection_work_item AS work_item
                WHERE work_item.task_id = $1::uuid
                  AND work_item.control_mode = 'enforced'
                  AND work_item.status NOT IN (
                      'succeeded', 'partial', 'failed',
                      'cancelled', 'superseded'
                  )
                  AND member.work_item_id = work_item.id
                  AND member.status IN ('pending', 'judged')
                """,
                task_id,
                reason,
            )
            rows = await connection.fetch(
                """
                UPDATE qc_inspection_work_item
                SET
                    status = 'cancelled',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = 'WORKFLOW_CANCELED',
                    last_error_message = $2,
                    finished_at = now(),
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND control_mode = 'enforced'
                  AND status NOT IN (
                      'succeeded', 'partial', 'failed',
                      'cancelled', 'superseded'
                  )
                RETURNING id
                """,
                task_id,
                reason,
            )
            return len(rows)


    async def finalize_task_work(
        self,
        *,
        task_id: str,
        error_code: str,
        error_message: str,
    ) -> InspectionWorkItemFinalization:
        """报告前恢复已有正式结果，并失败关闭仍无结果的正式工作项。"""
        from services.api.app.infrastructure.db.web_sequence_finalization import (
            WEB_FINALIZATION_RECOVERY_EXCLUSION_SQL,
            recover_web_sources_for_finalization,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            is_h5 = await connection.fetchval(
                "SELECT lower(target_platform) = 'h5' FROM qc_task WHERE id=$1::uuid",
                task_id,
            )
            if is_h5:
                # 与 Publication 写入共用屏障，并锁住工作行，避免领取和收口互相覆盖。
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"task-finalization:{task_id}",
                )
                await connection.fetch(
                    "SELECT id FROM qc_inspection_work_item WHERE task_id=$1::uuid FOR UPDATE",
                    task_id,
                )
                deferred = await connection.fetchval(
                    """SELECT
                    (SELECT count(*) FROM qc_inspection_work_item AS work
                     INNER JOIN qc_task AS task ON task.id=work.task_id
                     WHERE work.task_id=$1::uuid AND work.control_mode='enforced'
                       AND work.finalization_epoch=task.finalization_epoch
                       AND work.status IN ('leased','running')
                       AND work.lease_expires_at>now())
                    + (SELECT count(*) FROM qc_h5_inspection_publication AS publication
                       INNER JOIN qc_task AS task ON task.id=publication.task_id
                       WHERE publication.task_id=$1::uuid
                         AND publication.finalization_epoch=task.finalization_epoch
                         AND publication.status IN ('pending','leased','retry_wait'))
                    + (SELECT count(*) FROM qc_evidence_request AS request
                       INNER JOIN qc_task AS task ON task.id=request.task_id
                       WHERE request.task_id=$1::uuid
                         AND request.finalization_epoch=task.finalization_epoch
                         AND request.status IN ('pending','routed','running'))""",
                    task_id,
                )
                if deferred:
                    return InspectionWorkItemFinalization(
                        task_id=task_id, recovered_work_item_count=0,
                        failed_work_item_count=0, active_session_failed_count=0,
                        abandoned_attempt_count=0, deferred_retry_count=int(deferred),
                    )
            await recover_web_sources_for_finalization(connection, task_id)
            await connection.execute(
                f"""
                UPDATE qc_inspection_work_item_rule AS member
                SET
                    status = CASE execution.execution_status
                        WHEN 'evidence_insufficient' THEN 'evidence_insufficient'
                        WHEN 'skipped' THEN 'skipped'
                        WHEN 'error' THEN 'error'
                        ELSE 'written'
                    END,
                    observation_packet_id = execution.observation_packet_id,
                    rule_execution_id = execution.id,
                    updated_at = now()
                FROM
                    qc_inspection_work_item AS work_item,
                    qc_rule_execution AS execution
                WHERE work_item.id = member.work_item_id
                  AND work_item.task_id = $1::uuid
                  AND work_item.control_mode = 'enforced'
                  AND work_item.status NOT IN (
                      'succeeded', 'partial', 'failed',
                      'cancelled', 'superseded'
                  )
                  AND execution.task_id = work_item.task_id
                  AND execution.page_capture_id = work_item.page_capture_id
                  AND execution.rule_id = member.rule_id
                  AND member.status IN ('pending', 'judged')
                  {WEB_FINALIZATION_RECOVERY_EXCLUSION_SQL}
                """,
                task_id,
            )
            recovered_rows = await connection.fetch(
                """
                UPDATE qc_inspection_work_item AS work_item
                SET
                    status = CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM qc_inspection_work_item_rule AS member
                            WHERE member.work_item_id = work_item.id
                              AND member.status <> 'written'
                        )
                        THEN 'partial'
                        ELSE 'succeeded'
                    END,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    finished_at = now(),
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE work_item.task_id = $1::uuid
                  AND work_item.control_mode = 'enforced'
                  AND work_item.status NOT IN (
                      'succeeded', 'partial', 'failed',
                      'cancelled', 'superseded'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_inspection_work_item_rule AS member
                      WHERE member.work_item_id = work_item.id
                        AND member.status IN ('pending', 'judged')
                  )
                RETURNING work_item.id
                """,
                task_id,
            )
            recovered_ids = [str(row["id"]) for row in recovered_rows]
            if recovered_ids:
                await connection.execute(
                    """
                    UPDATE qc_inspection_attempt
                    SET
                        status = 'written',
                        finished_at = COALESCE(finished_at, now()),
                        updated_at = now()
                    WHERE work_item_id = ANY($1::uuid[])
                      AND status IN ('running', 'judged')
                    """,
                    recovered_ids,
                )
            abandoned_rows = await connection.fetch(
                """
                UPDATE qc_inspection_attempt AS attempt
                SET
                    status = 'abandoned',
                    error_code = COALESCE(
                        attempt.error_code,
                        CASE
                            WHEN work_item.session_dependency = 'active_session'
                            THEN 'INSPECTION_ACTIVE_SESSION_LOST'
                            ELSE $2
                        END
                    ),
                    error_message = COALESCE(
                        attempt.error_message,
                        CASE
                            WHEN work_item.session_dependency = 'active_session'
                            THEN '浏览器会话结束前未完成质检，禁止跨会话重放交互状态'
                            ELSE $3
                        END
                    ),
                    finished_at = COALESCE(attempt.finished_at, now()),
                    updated_at = now()
                FROM qc_inspection_work_item AS work_item
                WHERE work_item.task_id = $1::uuid
                  AND work_item.control_mode = 'enforced'
                  AND work_item.status NOT IN (
                      'succeeded', 'partial', 'failed',
                      'cancelled', 'superseded'
                  )
                  AND NOT (
                      work_item.session_dependency = 'snapshot'
                      AND work_item.status = 'retry_wait'
                      AND work_item.attempt_count < work_item.max_attempts
                  )
                  AND attempt.work_item_id = work_item.id
                  AND attempt.status IN ('running', 'judged')
                RETURNING attempt.id
                """,
                task_id,
                error_code,
                error_message,
            )
            await connection.execute(
                """
                UPDATE qc_inspection_work_item_rule AS member
                SET
                    status = 'error',
                    last_error_code = CASE
                        WHEN work_item.session_dependency = 'active_session'
                        THEN 'INSPECTION_ACTIVE_SESSION_LOST'
                        ELSE $2
                    END,
                    last_error_message = CASE
                        WHEN work_item.session_dependency = 'active_session'
                        THEN '浏览器会话结束前未完成质检，禁止跨会话重放交互状态'
                        ELSE $3
                    END,
                    updated_at = now()
                FROM qc_inspection_work_item AS work_item
                WHERE work_item.task_id = $1::uuid
                  AND work_item.control_mode = 'enforced'
                  AND work_item.status NOT IN (
                      'succeeded', 'partial', 'failed',
                      'cancelled', 'superseded'
                  )
                  AND NOT (
                      work_item.session_dependency = 'snapshot'
                      AND work_item.status = 'retry_wait'
                      AND work_item.attempt_count < work_item.max_attempts
                  )
                  AND member.work_item_id = work_item.id
                  AND member.status IN ('pending', 'judged')
                """,
                task_id,
                error_code,
                error_message,
            )
            failed_rows = await connection.fetch(
                """
                UPDATE qc_inspection_work_item AS work_item
                SET
                    status = 'failed',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_error_code = CASE
                        WHEN session_dependency = 'active_session'
                        THEN 'INSPECTION_ACTIVE_SESSION_LOST'
                        ELSE $2
                    END,
                    last_error_message = CASE
                        WHEN session_dependency = 'active_session'
                        THEN '浏览器会话结束前未完成质检，禁止跨会话重放交互状态'
                        ELSE $3
                    END,
                    finished_at = now(),
                    row_version = row_version + 1,
                    updated_at = now()
                WHERE task_id = $1::uuid
                  AND control_mode = 'enforced'
                  AND status NOT IN (
                      'succeeded', 'partial', 'failed',
                      'cancelled', 'superseded'
                  )
                  AND NOT (
                      work_item.session_dependency = 'snapshot'
                      AND work_item.status = 'retry_wait'
                      AND work_item.attempt_count < work_item.max_attempts
                  )
                RETURNING id, session_dependency
                """,
                task_id,
                error_code,
                error_message,
            )
            deferred_retry_count = int(
                await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM qc_inspection_work_item
                    WHERE task_id = $1::uuid
                      AND control_mode = 'enforced'
                      AND session_dependency = 'snapshot'
                      AND status = 'retry_wait'
                      AND attempt_count < max_attempts
                    """,
                    task_id,
                )
            )
        return InspectionWorkItemFinalization(
            task_id=task_id,
            recovered_work_item_count=len(recovered_rows),
            failed_work_item_count=len(failed_rows),
            active_session_failed_count=sum(
                1 for row in failed_rows if row["session_dependency"] == "active_session"
            ),
            abandoned_attempt_count=len(abandoned_rows),
            deferred_retry_count=deferred_retry_count,
        )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _json_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return dict(parsed) if isinstance(parsed, dict) else None
    return None


def _json_object(value: Any) -> dict[str, Any]:
    return _json_dict(value) or {}
