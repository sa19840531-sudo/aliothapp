"""聚合规则期望候选、EvidenceInputSet、Work 与 Outbox 的事务仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from services.api.app.application.aggregate_inspection_publication import (
    AggregateEvidenceSource,
    AggregatePublicationSnapshot,
    AggregateRuleSource,
    build_aggregate_rule_source,
)
from services.api.app.application.aggregate_inspection_work_items import (
    PublishAggregateInspectionWorkItemCommand,
)
from services.api.app.application.aggregate_rule_config import (
    resolve_rule_evidence_input_configuration,
)
from services.api.app.application.evidence_presentation import (
    presentation_from_capture_row,
)
from services.api.app.domain.evidence import (
    AppExplorationResult,
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceInputSet,
    EvidenceManifestReference,
)
from services.api.app.domain.evidence.presentation import PresentationAssessment
from services.api.app.domain.inspection.rule_scope import (
    RuleScopeAuthority,
    RuleScopePlan,
)
from services.api.app.infrastructure.db.aggregate_inspection_work_item_repository import (
    PostgresAggregateInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.aggregate_rule_inventory import read_frozen_aggregate_rule
from services.api.app.infrastructure.db.aggregate_rule_inventory_sql import (
    aggregate_expected_rule_count_sql,
)
from services.api.app.infrastructure.db.app_evidence_index_repository import (
    PostgresAppEvidenceIndexRepository,
)
from services.api.app.infrastructure.db.evidence_input_set_repository import (
    PostgresEvidenceInputSetRepository,
)
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    assert_finalization_recovery_fence,
)
from services.api.app.infrastructure.db.inspection_scope_sql import (
    page_type_in_inspection_scope_sql,
    rule_in_inspection_scope_sql,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    read_frozen_task_rule_inventory_on_connection,
)
from services.api.app.infrastructure.db.web_coverage_snapshot_row import (
    parse_web_coverage_snapshot_row,
)


class AggregateInspectionPublicationPersistenceError(RuntimeError):
    """任务收口快照或候选输入不满足聚合发布合同。"""


class PostgresAggregateInspectionPublicationRepository:
    """每条规则在同一事务提交 InputSet、Work、Candidate 与 Outbox。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def is_app_recovery_ready(
        self, *, task_id: str, source_agent_run_id: str, finalization_epoch: int,
        require_missing: bool = True,
    ) -> bool:
        """快照正式交接完整且聚合尚未发布时，才允许超时前恢复调度。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT index.index_id, index.content_sha256
                FROM qc_task AS task
                JOIN qc_agent_run AS owner ON owner.id=task.finalization_agent_run_id
                    AND owner.task_id=task.id AND owner.id=$2::uuid
                JOIN qc_app_evidence_index AS index ON index.task_id=task.id
                    AND index.agent_run_id=owner.id AND index.status='sealed'
                JOIN qc_app_exploration_result AS result ON result.task_id=task.id
                    AND result.agent_run_id=owner.id
                    AND result.evidence_index_id=index.index_id
                    AND result.evidence_index_hash=index.content_sha256
                    AND result.surface_session_id=index.surface_session_id
                JOIN qc_app_inspection_publication AS publication ON publication.task_id=task.id
                    AND publication.source_agent_run_id=owner.id
                    AND publication.finalization_epoch=task.finalization_epoch
                    AND publication.evidence_index_id=index.index_id
                    AND publication.evidence_index_hash=index.content_sha256
                JOIN qc_surface_session AS session ON session.task_id=task.id
                    AND session.session_id=index.surface_session_id
                WHERE task.id=$1::uuid AND task.target_platform='app'
                    AND task.status='running' AND task.deleted_at IS NULL
                    AND task.finalization_state='session_draining'
                    AND task.finalization_epoch=$3
                    AND lower(COALESCE(task.task_config->>'workflow_version',''))='v2'
                    AND COALESCE(task.task_config->>'execution_mode','full_inspection')
                        ='full_inspection'
                    AND COALESCE(owner.output_context->>'cancel_requested','false')<>'true'
                    AND publication.status IN ('published','not_applicable','needs_review')
                    AND publication.terminal_entry_count=publication.expected_entry_count
                    AND session.status IN ('closed','lost','failed')
                    AND session.finalization_epoch=task.finalization_epoch
                    AND session.platform_build_ref=index.build_id
                    AND session.lease_token IS NULL AND session.lease_owner IS NULL
                    AND session.lease_expires_at IS NULL
                    AND NOT EXISTS (SELECT 1 FROM qc_inspection_work_item AS work
                        WHERE work.task_id=task.id AND work.finalization_epoch=$3
                          AND work.control_mode='enforced'
                          AND work.status NOT IN (
                            'succeeded','partial','failed','cancelled','superseded'))
                    AND NOT EXISTS (SELECT 1 FROM qc_evidence_request AS request
                        WHERE request.task_id=task.id
                          AND request.status IN ('pending','routed','running'))
                    AND (NOT $4::boolean OR {aggregate_expected_rule_count_sql('task')} > (
                        SELECT count(*) FROM qc_aggregate_inspection_candidate AS candidate
                        WHERE candidate.task_id=task.id
                          AND candidate.source_agent_run_id=owner.id
                          AND candidate.finalization_epoch=$3
                    ))
                    AND ($4::boolean OR NOT EXISTS (
                        SELECT 1 FROM qc_aggregate_inspection_work_item AS work
                        WHERE work.task_id=task.id AND work.finalization_epoch=$3
                          AND work.control_mode='enforced'
                          AND work.status NOT IN (
                            'succeeded','evidence_insufficient','failed','cancelled','superseded')
                    ))
                """,
                task_id, source_agent_run_id, finalization_epoch, require_missing,
            )
        if row is None:
            return False
        try:
            # 重算完整 Index 和 Result 哈希，不凭状态标签为损坏或旧版材料续命。
            index = await PostgresAppEvidenceIndexRepository(pool=pool).get_by_run_id(
                run_id=source_agent_run_id,
            )
            if index is None or (
                index.index_id != row['index_id']
                or index.content_sha256 != row['content_sha256']
            ):
                return False
            snapshot = await self.load_snapshot(
                task_id=task_id, source_agent_run_id=source_agent_run_id,
                finalization_epoch=finalization_epoch,
            )
        except (ValueError, AggregateInspectionPublicationPersistenceError):
            return False
        return snapshot.scope_authority is not None

    async def load_snapshot(
        self,
        *,
        task_id: str,
        source_agent_run_id: str,
        finalization_epoch: int,
    ) -> AggregatePublicationSnapshot:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{task_id}",
            )
            task = await connection.fetchrow(
                """
                SELECT
                    task.id::text AS task_id,
                    task.target_platform,
                    task.finalization_agent_run_id::text AS source_agent_run_id,
                    task.finalization_epoch,
                    ruleset.snapshot_hash AS ruleset_snapshot_hash,
                    web_scope.id::text AS web_scope_id,
                    web_scope.task_id::text AS web_scope_task_id,
                    web_scope.agent_run_id::text AS web_scope_agent_run_id,
                    web_scope.finalization_epoch AS web_scope_finalization_epoch,
                    web_scope.required_page_types AS web_required_page_types,
                    web_scope.observed_page_types AS web_observed_page_types,
                    web_scope.missing_page_types AS web_missing_page_types,
                    web_scope.unrecognized_page_types AS web_unrecognized_page_types,
                    web_scope.normalization_version AS web_normalization_version,
                    web_scope.coverage_status AS web_coverage_status,
                    web_scope.snapshot_hash AS web_scope_hash,
                    web_scope.created_at AS web_scope_created_at,
                    web_scope.applicability_snapshot AS web_applicability_snapshot,
                    app_scope.surface_session_id AS app_scope_session_id,
                    app_scope.evidence_index_id AS app_scope_evidence_index_id,
                    app_scope.protocol_version AS app_scope_protocol_version,
                    app_scope.outcome AS app_scope_outcome,
                    app_scope.terminal_reason_code AS app_scope_terminal_reason_code,
                    app_scope.evidence_index_hash AS app_scope_evidence_index_hash,
                    app_scope.result_sha256 AS app_scope_hash,
                    app_scope.coverage_summary AS app_coverage_summary,
                    app_scope.evidence_gaps AS app_scope_evidence_gaps,
                    app_scope.started_at AS app_scope_started_at,
                    app_scope.completed_at AS app_scope_completed_at
                FROM qc_task AS task
                INNER JOIN qc_ruleset AS ruleset ON ruleset.id = task.ruleset_id
                LEFT JOIN qc_web_coverage_snapshot AS web_scope
                    ON web_scope.task_id = task.id
                   AND web_scope.agent_run_id = task.finalization_agent_run_id
                   AND web_scope.finalization_epoch = task.finalization_epoch
                LEFT JOIN qc_app_exploration_result AS app_scope
                    ON app_scope.task_id = task.id
                   AND app_scope.agent_run_id = task.finalization_agent_run_id
                WHERE task.id = $1::uuid
                  AND task.status IN ('running', 'review')
                  AND task.finalization_state = 'session_draining'
                  AND task.finalization_agent_run_id = $2::uuid
                  AND task.finalization_epoch = $3
                  AND (task.target_platform <> 'app' OR (
                      task.deleted_at IS NULL AND NOT EXISTS (
                          SELECT 1 FROM qc_agent_run AS owner
                          WHERE owner.id=task.finalization_agent_run_id
                            AND COALESCE(owner.output_context->>'cancel_requested','false')='true'
                      )
                  ))
                """,
                task_id,
                source_agent_run_id,
                finalization_epoch,
            )
            if task is None:
                raise AggregateInspectionPublicationPersistenceError(
                    "任务未处于当前 Run/Epoch 的 session_draining，或规则集未封存"
                )
            inventory = await read_frozen_task_rule_inventory_on_connection(
                connection, task_id=task_id
            )
            if inventory is not None:
                task = dict(task)
                task["ruleset_snapshot_hash"] = inventory["ruleset_snapshot_hash"]
            if not str(task["ruleset_snapshot_hash"] or "").strip():
                raise AggregateInspectionPublicationPersistenceError("任务冻结规则集未封存")
            if inventory is not None:
                rows = [
                    rule for rule in inventory["rules"] if rule["applicability"] == "applicable"
                ]
            else:
                rows = await connection.fetch(
                    f"""
                SELECT
                    rule.id::text AS rule_id,
                    rule.rule_code,
                    rule.category,
                    rule.executor_type,
                    rule.severity,
                    rule.check_point,
                    rule.input_contract,
                    rule.judge_config,
                    rule.manual_review_policy,
                    rule.steps,
                    rule.applicable_page_types
                FROM qc_task AS task
                INNER JOIN qc_rule AS rule
                  ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
                WHERE task.id = $1::uuid
                  AND rule.status = 'enabled'
                  AND (rule.channel IS NULL OR rule.channel = task.channel)
                  -- 只挂在质检范围外页面类型上的汇总规则不进这次任务
                  AND {rule_in_inspection_scope_sql("task.task_config", "rule")}
                ORDER BY rule.sort_order, rule.rule_code
                    """,
                    task_id,
                )
            rules: list[AggregateRuleSource] = []
            for raw in rows:
                row = _row_mapping(raw)
                config = resolve_rule_evidence_input_configuration(
                    _json_mapping(row["judge_config"]),
                    require_executable=False,
                )
                if config.scope is EvidenceInputScope.PAGE:
                    continue
                normalized = dict(row)
                normalized["judge_config"] = _json_mapping(row["judge_config"])
                normalized["input_contract"] = _json_mapping(row["input_contract"])
                normalized["manual_review_policy"] = _json_mapping(row["manual_review_policy"])
                normalized["steps"] = _json_sequence(row["steps"])
                normalized["applicable_page_types"] = _json_text_tuple(row["applicable_page_types"])
                rules.append(build_aggregate_rule_source(normalized))
            capture_in_scope = page_type_in_inspection_scope_sql(
                "task.task_config",
                "COALESCE(page.page_type, app_entry.page_type, capture.metadata->>'page_type', '')",
            )
            evidence_rows = await connection.fetch(
                f"""
                WITH latest_manifest AS (
                    SELECT DISTINCT ON (manifest.capture_id)
                        manifest.capture_id,
                        manifest.manifest_payload,
                        manifest.quality_status
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.task_id = $1::uuid
                    ORDER BY manifest.capture_id, manifest.manifest_version DESC
                ), current_app_entry AS (
                    SELECT DISTINCT ON (entry.capture_id)
                        entry.capture_id,
                        entry.sequence_no,
                        manifest.manifest_payload,
                        visual.page_state AS visual_page_state,
                        visual.overlay_signature AS visual_overlay_signature,
                        visual.surface_facts AS visual_surface_facts,
                        COALESCE(
                            NULLIF(visual.visual_page_type, ''),
                            NULLIF(entry.metadata->>'visual_page_type', ''),
                            NULLIF(entry.metadata->>'page_type', '')
                        ) AS page_type
                    FROM qc_app_evidence_index AS index
                    INNER JOIN qc_app_evidence_index_entry AS entry
                        ON entry.index_id = index.index_id
                    INNER JOIN qc_evidence_manifest AS manifest
                        ON manifest.manifest_id = entry.evidence_manifest_id
                       AND manifest.manifest_version = entry.evidence_manifest_version
                       AND manifest.task_id = index.task_id
                       AND manifest.capture_id = entry.capture_id
                       AND manifest.session_id = index.surface_session_id
                       AND manifest.quality_status = 'pass'
                    LEFT JOIN qc_app_visual_observation AS visual
                        ON visual.capture_id = entry.capture_id
                    WHERE index.task_id = $1::uuid
                      AND index.agent_run_id = $2::uuid
                      AND index.status = 'sealed'
                    ORDER BY entry.capture_id, entry.sequence_no
                )
                SELECT
                    capture.id::text AS capture_id,
                    capture.capture_role,
                    capture.surface_kind,
                    capture.metadata,
                    page.page_state,
                    app_entry.visual_page_state,
                    app_entry.visual_overlay_signature,
                    app_entry.visual_surface_facts,
                    COALESCE(
                        app_entry.manifest_payload,
                        latest.manifest_payload
                    ) AS manifest_payload,
                    COALESCE(
                        page.page_seq,
                        app_entry.sequence_no,
                        row_number() OVER (
                            ORDER BY capture.captured_at, capture.id
                        )::integer
                    ) AS page_sequence,
                    COALESCE(
                        page.page_type,
                        app_entry.page_type,
                        capture.metadata->>'page_type'
                    ) AS page_type,
                    COALESCE(
                        capture.metadata->>'journey_execution_id',
                        capture.metadata->'journey'->>'execution_id'
                    ) AS journey_execution_id,
                    COALESCE(
                        capture.metadata->>'environment_profile_id',
                        capture.metadata->'environment'->>'profile_id'
                    ) AS environment_profile_id
                FROM qc_capture AS capture
                INNER JOIN qc_task AS task ON task.id = capture.task_id
                LEFT JOIN latest_manifest AS latest
                    ON latest.capture_id = capture.id
                LEFT JOIN qc_page_capture AS page ON page.id = capture.id
                LEFT JOIN current_app_entry AS app_entry
                    ON app_entry.capture_id = capture.id
                WHERE capture.task_id = $1::uuid
                  AND capture.agent_run_id = $2::uuid
                  AND capture.capture_status = 'captured'
                  AND capture.capture_role <> 'supplemental'
                  -- 经过的页面只留认路记录，不当汇总规则的证据
                  AND {capture_in_scope}
                  AND (
                      (
                          capture.surface_kind IN ('android', 'ios')
                          AND app_entry.capture_id IS NOT NULL
                      )
                      OR (
                          capture.surface_kind NOT IN ('android', 'ios')
                          AND latest.quality_status = 'pass'
                      )
                  )
                ORDER BY page_sequence, capture.id
                """,
                task_id,
                source_agent_run_id,
            )
        evidence = tuple(_evidence_from_row(row) for row in evidence_rows)
        try:
            scope_authority = _scope_authority_from_task(task)
        except ValueError as exc:
            raise AggregateInspectionPublicationPersistenceError(
                "规则范围权威快照校验失败"
            ) from exc
        return AggregatePublicationSnapshot(
            task_id=str(task["task_id"]),
            source_agent_run_id=str(task["source_agent_run_id"]),
            finalization_epoch=int(task["finalization_epoch"]),
            ruleset_snapshot_hash=str(task["ruleset_snapshot_hash"]),
            rules=tuple(rules),
            evidence=evidence,
            scope_authority=scope_authority,
            checkpoint_journeys=task.get("target_platform") == "h5",
        )

    async def load_journey_selection(self, *, snapshot, rule):
        from services.api.app.application.inspection_work_items import (
            build_inspection_rule_contract_hash,
        )
        from services.api.app.infrastructure.db.journey_input_repository import (
            load_journey_selection,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            selection = await load_journey_selection(
                connection,
                task_id=snapshot.task_id,
                agent_run_id=snapshot.source_agent_run_id,
                epoch=snapshot.finalization_epoch,
                rule_hash=build_inspection_rule_contract_hash(rule.rule_payload),
            )
        if selection is None:
            return None
        plan_id = selection["contract"]["plan"]["execution_plan_id"]
        selection["evidence"] = tuple(
            AggregateEvidenceSource(
                candidate=EvidenceInputCandidate(
                    manifest=EvidenceManifestReference.from_dict(manifest),
                    capture_role=binding["role"],
                    page_sequence=index,
                    journey_execution_id=plan_id,
                ),
                page_type=None,
            )
            for index, (manifest, binding) in enumerate(
                (
                    (manifest, binding)
                    for checkpoint in selection["contract"]["checkpoints"]
                    for manifest, binding in zip(
                        checkpoint["manifests"], checkpoint["bindings"], strict=True
                    )
                ),
                1,
            )
        )
        return selection

    async def persist_published(
        self,
        *,
        input_set: EvidenceInputSet,
        command: PublishAggregateInspectionWorkItemCommand,
        expected_member_count: int,
        scope_plan: RuleScopePlan | None,
        journey_selection=None,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{input_set.task_id}",
            )
            await _assert_app_aggregate_write_scope(
                connection, task_id=input_set.task_id,
                source_agent_run_id=input_set.source_agent_run_id,
                finalization_epoch=input_set.finalization_epoch,
            )
            if recovery_lease_token is not None:
                await assert_finalization_recovery_fence(
                    connection,
                    task_id=input_set.task_id,
                    recovery_lease_token=recovery_lease_token,
                    recovery_generation=recovery_generation,
                    recovery_lease_expires_at=recovery_lease_expires_at,
                )
            persisted = await PostgresEvidenceInputSetRepository(pool=pool).add_on_connection(
                connection, input_set=input_set
            )
            if journey_selection is not None:
                from services.api.app.infrastructure.db.journey_input_repository import (
                    persist_journey_selection,
                )

                await persist_journey_selection(
                    connection,
                    input_set=input_set,
                    selection={
                        key: journey_selection[key] for key in ("selection_hash", "contract")
                    },
                )
            work = await PostgresAggregateInspectionWorkItemRepository(
                pool=pool
            ).publish_on_connection(connection, command=command)
            row = await connection.fetchrow(
                """
                INSERT INTO qc_aggregate_inspection_candidate AS existing (
                    task_id, source_agent_run_id, rule_id, rule_code,
                    rule_contract_hash, scope, scope_key, finalization_epoch,
                    status, expected_member_count, evidence_input_set_id,
                    work_item_id, rule_scope_plan_hash, rule_scope_plan
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4,
                    $5, $6, $7, $8,
                    'published', $9, $10, $11::uuid, $12, $13::jsonb
                )
                ON CONFLICT ON CONSTRAINT uq_qc_aggregate_candidate_rule_epoch
                DO UPDATE SET
                    updated_at = existing.updated_at,
                    rule_scope_plan_hash = COALESCE(
                        existing.rule_scope_plan_hash,
                        EXCLUDED.rule_scope_plan_hash
                    ),
                    rule_scope_plan = COALESCE(
                        existing.rule_scope_plan,
                        EXCLUDED.rule_scope_plan
                    )
                WHERE existing.source_agent_run_id = EXCLUDED.source_agent_run_id
                  AND existing.rule_contract_hash = EXCLUDED.rule_contract_hash
                  AND existing.scope = EXCLUDED.scope
                  AND existing.status IN ('published', 'completed')
                  AND existing.expected_member_count = EXCLUDED.expected_member_count
                  AND existing.evidence_input_set_id = EXCLUDED.evidence_input_set_id
                  AND existing.work_item_id = EXCLUDED.work_item_id
                  AND (
                      existing.rule_scope_plan_hash IS NULL
                      OR (
                          existing.rule_scope_plan_hash
                              IS NOT DISTINCT FROM EXCLUDED.rule_scope_plan_hash
                          AND existing.rule_scope_plan
                              IS NOT DISTINCT FROM EXCLUDED.rule_scope_plan
                      )
                  )
                RETURNING id
                """,
                input_set.task_id,
                input_set.source_agent_run_id,
                input_set.rule_id,
                input_set.rule_code,
                input_set.rule_contract_hash,
                input_set.scope.value,
                input_set.scope_key,
                input_set.finalization_epoch,
                expected_member_count,
                input_set.input_set_id,
                work.work_item_id,
                scope_plan.plan_hash if scope_plan is not None else None,
                _json_dumps(scope_plan.to_dict()) if scope_plan is not None else None,
            )
            if row is None:
                raise AggregateInspectionPublicationPersistenceError(
                    "聚合候选幂等身份已绑定不同输入"
                )
        return persisted.created or work.created

    async def persist_disposition(
        self,
        *,
        snapshot: AggregatePublicationSnapshot,
        rule: AggregateRuleSource,
        status: str,
        expected_member_count: int,
        code: str,
        message: str,
        scope_plan: RuleScopePlan | None,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> None:
        if status not in {"not_applicable", "evidence_insufficient"}:
            raise ValueError("聚合候选前置终态不受支持")
        rule_contract_hash = _rule_contract_hash(rule)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{snapshot.task_id}",
            )
            await _assert_app_aggregate_write_scope(
                connection, task_id=snapshot.task_id,
                source_agent_run_id=snapshot.source_agent_run_id,
                finalization_epoch=snapshot.finalization_epoch,
            )
            if recovery_lease_token is not None:
                await assert_finalization_recovery_fence(
                    connection,
                    task_id=snapshot.task_id,
                    recovery_lease_token=recovery_lease_token,
                    recovery_generation=recovery_generation,
                    recovery_lease_expires_at=recovery_lease_expires_at,
                )
            await read_frozen_aggregate_rule(
                connection, task_id=snapshot.task_id, rule_id=rule.rule_id,
                rule_code=rule.rule_code, contract_hash=rule_contract_hash,
                scope=rule.evidence.scope.value, scope_key=rule.evidence.scope_key,
                source_agent_run_id=snapshot.source_agent_run_id,
                finalization_epoch=snapshot.finalization_epoch,
                ruleset_snapshot_hash=snapshot.ruleset_snapshot_hash,
            )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_aggregate_inspection_candidate AS existing (
                    task_id, source_agent_run_id, rule_id, rule_code,
                    rule_contract_hash, scope, scope_key, finalization_epoch,
                    status, expected_member_count, disposition_code,
                    disposition_message, finished_at,
                    rule_scope_plan_hash, rule_scope_plan
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4,
                    $5, $6, $7, $8,
                    $9, $10, $11, $12, now(), $13, $14::jsonb
                )
                ON CONFLICT ON CONSTRAINT uq_qc_aggregate_candidate_rule_epoch
                DO UPDATE SET
                    status = EXCLUDED.status,
                    disposition_message = CASE
                        WHEN existing.status = EXCLUDED.status
                            THEN existing.disposition_message
                        ELSE EXCLUDED.disposition_message
                    END,
                    updated_at = CASE
                        WHEN existing.status = EXCLUDED.status THEN existing.updated_at
                        ELSE now()
                    END,
                    rule_scope_plan_hash = COALESCE(
                        existing.rule_scope_plan_hash,
                        EXCLUDED.rule_scope_plan_hash
                    ),
                    rule_scope_plan = COALESCE(
                        existing.rule_scope_plan,
                        EXCLUDED.rule_scope_plan
                    )
                WHERE existing.source_agent_run_id = EXCLUDED.source_agent_run_id
                  AND existing.rule_contract_hash = EXCLUDED.rule_contract_hash
                  AND existing.scope = EXCLUDED.scope
                  AND (
                      existing.status = EXCLUDED.status
                      -- 旧版本写下的同一处置（待复核，或被遮挡时借用的不适用）升级为证据不足
                      OR (
                          EXCLUDED.status = 'evidence_insufficient'
                          AND (
                              existing.status = 'needs_review'
                              OR (
                                  existing.status = 'not_applicable'
                                  AND existing.disposition_code
                                      = 'AGGREGATE_PRESENTATION_EVIDENCE_REJECTED'
                              )
                          )
                      )
                  )
                  AND existing.expected_member_count = EXCLUDED.expected_member_count
                  AND existing.disposition_code = EXCLUDED.disposition_code
                  AND (
                      existing.rule_scope_plan_hash IS NULL
                      OR (
                          existing.rule_scope_plan_hash
                              IS NOT DISTINCT FROM EXCLUDED.rule_scope_plan_hash
                          AND existing.rule_scope_plan
                              IS NOT DISTINCT FROM EXCLUDED.rule_scope_plan
                      )
                  )
                RETURNING id
                """,
                snapshot.task_id,
                snapshot.source_agent_run_id,
                rule.rule_id,
                rule.rule_code,
                rule_contract_hash,
                rule.evidence.scope.value,
                rule.evidence.scope_key,
                snapshot.finalization_epoch,
                status,
                expected_member_count,
                code,
                message,
                scope_plan.plan_hash if scope_plan is not None else None,
                _json_dumps(scope_plan.to_dict()) if scope_plan is not None else None,
            )
            if row is None:
                raise AggregateInspectionPublicationPersistenceError("聚合候选终态幂等身份发生漂移")
            if status == "evidence_insufficient":
                await _record_evidence_insufficient(
                    connection,
                    snapshot=snapshot,
                    rule=rule,
                    expected_member_count=expected_member_count,
                    code=code,
                    message=message,
                    scope_plan=scope_plan,
                )

    async def list_session_draining(self, *, limit: int = 100) -> tuple[tuple[str, str, int], ...]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id::text AS task_id,
                    finalization_agent_run_id::text AS agent_run_id,
                    finalization_epoch
                FROM qc_task
                WHERE (
                    status = 'running'
                    OR (status = 'review' AND target_platform = 'h5')
                )
                  AND finalization_state = 'session_draining'
                  AND finalization_agent_run_id IS NOT NULL
                ORDER BY session_draining_at, id
                LIMIT $1
                """,
                limit,
            )
        return tuple(
            (str(row["task_id"]), str(row["agent_run_id"]), int(row["finalization_epoch"]))
            for row in rows
        )

    async def _get_pool(self) -> Any:
        return self._pool or await get_postgres_pool(self._database_url)


def _evidence_from_row(row: Mapping[str, Any]) -> AggregateEvidenceSource:
    manifest = EvidenceManifestReference.from_dict(_json_mapping(row["manifest_payload"]))
    return AggregateEvidenceSource(
        candidate=EvidenceInputCandidate(
            manifest=manifest,
            capture_role=str(row["capture_role"]),
            page_sequence=int(row["page_sequence"]),
            journey_execution_id=_optional_text(row["journey_execution_id"]),
            environment_profile_id=_optional_text(row["environment_profile_id"]),
        ),
        page_type=_optional_text(row["page_type"]),
        presentation=_presentation_from_row(row),
    )


def _scope_authority_from_task(row: Mapping[str, Any]) -> RuleScopeAuthority | None:
    task_id = str(row["task_id"])
    run_id = str(row["source_agent_run_id"])
    epoch = int(row["finalization_epoch"])
    web_scope_id = _optional_text(row.get("web_scope_id"))
    if web_scope_id is not None:
        web_scope = parse_web_coverage_snapshot_row(
            {
                "id": web_scope_id,
                "task_id": row.get("web_scope_task_id"),
                "agent_run_id": row.get("web_scope_agent_run_id"),
                "finalization_epoch": row.get("web_scope_finalization_epoch"),
                "required_page_types": row.get("web_required_page_types"),
                "observed_page_types": row.get("web_observed_page_types"),
                "missing_page_types": row.get("web_missing_page_types"),
                "unrecognized_page_types": row.get("web_unrecognized_page_types"),
                "normalization_version": row.get("web_normalization_version"),
                "coverage_status": row.get("web_coverage_status"),
                "snapshot_hash": row.get("web_scope_hash"),
                "created_at": row.get("web_scope_created_at"),
                "applicability_snapshot": row.get("web_applicability_snapshot"),
            }
        )
        return RuleScopeAuthority(
            task_id=task_id,
            source_agent_run_id=run_id,
            finalization_epoch=epoch,
            authority_type="web_coverage_snapshot",
            authority_id=web_scope_id,
            authority_hash=web_scope.snapshot_hash,
            scope_verified=True,
            applicable_page_types=tuple(
                (*web_scope.required_page_types, *web_scope.optional_page_types)
            ),
            not_applicable_page_types=web_scope.not_applicable_page_types,
            unresolved_page_types=(),
            negative_evidence_refs=(
                (f"web-coverage:{web_scope_id}",) if web_scope.not_applicable_page_types else ()
            ),
        )
    app_scope_hash = _optional_text(row.get("app_scope_hash"))
    if app_scope_hash is None:
        return None
    app_result = AppExplorationResult.from_dict(
        {
            "protocol_version": row.get("app_scope_protocol_version"),
            "task_id": task_id,
            "run_id": run_id,
            "surface_session_id": row.get("app_scope_session_id"),
            "outcome": row.get("app_scope_outcome"),
            "terminal_reason_code": row.get("app_scope_terminal_reason_code"),
            "evidence_index_id": row.get("app_scope_evidence_index_id"),
            "evidence_index_hash": row.get("app_scope_evidence_index_hash"),
            "coverage_summary": _json_mapping(row.get("app_coverage_summary")),
            "evidence_gaps": _json_sequence(row.get("app_scope_evidence_gaps")),
            "started_at": _datetime_payload(row.get("app_scope_started_at")),
            "completed_at": _datetime_payload(row.get("app_scope_completed_at")),
            "result_sha256": app_scope_hash,
        }
    )
    applicability = _json_mapping(app_result.coverage_summary.get("page_type_applicability"))
    if applicability.get("protocol_version") != "app-page-type-applicability.v2":
        return None
    scope_verified = applicability.get("assessment_scope_verified") is True
    not_applicable = _json_text_tuple(applicability.get("not_applicable_page_types"))
    not_observed = _json_text_tuple(applicability.get("not_observed_page_types"))
    unresolved = _json_text_tuple(applicability.get("unresolved_applicability_page_types"))
    observed_scope_complete = (
        scope_verified
        and not unresolved
        and applicability.get("assessment_scope_complete") is True
        and applicability.get("assessment_scope_kind") == "observed_frontier"
    )
    if observed_scope_complete:
        # 同 Run 已封存的观察范围外规则不适用于本次任务，不宣称整个 App 不含该能力。
        not_applicable = tuple(dict.fromkeys((*not_applicable, *not_observed)))
    else:
        # 没有完整范围证明，新增字段不能把未知页型从规则范围计划中悄悄排除。
        unresolved = tuple(dict.fromkeys((*unresolved, *not_observed)))
    return RuleScopeAuthority(
        task_id=task_id,
        source_agent_run_id=run_id,
        finalization_epoch=epoch,
        authority_type="app_exploration_result",
        authority_id=f"app-result:{run_id}",
        authority_hash=str(app_result.result_sha256),
        scope_verified=scope_verified,
        applicable_page_types=_json_text_tuple(applicability.get("applicable_page_types")),
        not_applicable_page_types=not_applicable,
        unresolved_page_types=unresolved,
        negative_evidence_refs=((f"app-exploration-result:{run_id}",) if not_applicable else ()),
    )


async def _assert_app_aggregate_write_scope(
    connection: Any, *, task_id: str, source_agent_run_id: str, finalization_epoch: int,
) -> None:
    """正式落库事务重新锁定 App Task/Run，拒绝准备后到来的取消或软删除。"""

    task = await connection.fetchrow(
        """SELECT target_platform,status,deleted_at,finalization_state,finalization_epoch,
                  finalization_agent_run_id::text AS owner
           FROM qc_task WHERE id=$1::uuid FOR UPDATE""", task_id,
    )
    if task is None or task.get("target_platform") != "app":
        return
    valid_owner = await connection.fetchval(
        """SELECT id FROM qc_agent_run WHERE id=$1::uuid AND task_id=$2::uuid
               AND COALESCE(output_context->>'cancel_requested','false')<>'true'
           FOR UPDATE""", source_agent_run_id, task_id,
    )
    if (
        task["deleted_at"] is not None or task["status"] not in {"running", "review"}
        or task["finalization_state"] != "session_draining"
        or task["finalization_epoch"] != finalization_epoch
        or task["owner"] != source_agent_run_id or not valid_owner
    ):
        raise AggregateInspectionPublicationPersistenceError(
            "App 聚合来源已取消、删除或收口身份变化"
        )


async def _record_evidence_insufficient(
    connection: Any,
    *,
    snapshot: AggregatePublicationSnapshot,
    rule: AggregateRuleSource,
    expected_member_count: int,
    code: str,
    message: str,
    scope_plan: RuleScopePlan | None,
) -> None:
    """证据不足的候选在同一事务留下规则执行与待复核项：报告看得到，人工能处理，收口按复核策略放行。"""
    frozen_rule = await read_frozen_aggregate_rule(
        connection, task_id=snapshot.task_id, rule_id=rule.rule_id,
        rule_code=rule.rule_code, contract_hash=_rule_contract_hash(rule),
        scope=rule.evidence.scope.value, scope_key=rule.evidence.scope_key,
        source_agent_run_id=snapshot.source_agent_run_id,
        finalization_epoch=snapshot.finalization_epoch,
        ruleset_snapshot_hash=snapshot.ruleset_snapshot_hash,
    )
    args = [
        snapshot.task_id,
        rule.rule_id,
        (
            f"aggregate-disposition:{snapshot.source_agent_run_id}:"
            f"{snapshot.finalization_epoch}:{rule.evidence.scope_key}"
        ),
        snapshot.source_agent_run_id,
        message,
        _json_dumps(
            {
                "schema_version": "v1",
                "source": "aggregate_candidate_disposition",
                "finalization_epoch": snapshot.finalization_epoch,
                "scope": rule.evidence.scope.value,
                "scope_key": rule.evidence.scope_key,
                "disposition_code": code,
                "expected_member_count": expected_member_count,
                "rule_scope_plan_hash": scope_plan.plan_hash if scope_plan is not None else None,
            }
        ),
        _json_dumps(
            {
                "schema_version": "v1",
                "conclusion": "evidence_insufficient",
                "reason_code": code,
            }
        ),
        code,
    ]
    executor_sql, severity_sql = "rule.executor_type", "rule.severity"
    if frozen_rule is not None:
        executor_sql, severity_sql = "$9::varchar", "$10::varchar"
        args.extend([frozen_rule["executor_type"], frozen_rule["severity"]])
    execution_id = await connection.fetchval(
        f"""
        WITH inserted AS (
            INSERT INTO qc_rule_execution (
                task_id, rule_id, page_capture_id, source_idempotency_key,
                agent_run_id, execution_status, judge_strategy, severity,
                result_summary, problem_desc, judge_input, judge_output,
                review_status, review_reason, error_code, started_at, finished_at
            )
            SELECT
                $1::uuid, rule.id, NULL, $3,
                $4::uuid, 'evidence_insufficient', {executor_sql}, {severity_sql},
                '没有可用证据，规则未能自动判定，已转人工确认', $5, $6::jsonb, $7::jsonb,
                'pending', $5, $8, now(), now()
            FROM qc_rule AS rule
            WHERE rule.id = $2::uuid
            ON CONFLICT (task_id, rule_id, source_idempotency_key)
                WHERE source_idempotency_key IS NOT NULL
            DO NOTHING
            RETURNING id
        )
        SELECT id::text FROM inserted
        UNION ALL
        SELECT id::text
        FROM qc_rule_execution
        WHERE task_id = $1::uuid
          AND rule_id = $2::uuid
          AND source_idempotency_key = $3
        LIMIT 1
        """,
        *args,
    )
    if execution_id is None:
        raise AggregateInspectionPublicationPersistenceError("聚合证据不足的规则执行写入失败")
    # 已被人工处理过的复核项保持原样：收口对账每轮都会重放，不能把它重新打开
    await connection.execute(
        """
        INSERT INTO qc_review_item (
            rule_execution_id, task_id, trigger_reason, status, priority, review_reason
        )
        SELECT
            execution.id, execution.task_id, 'evidence_insufficient', 'pending',
            CASE execution.severity
                WHEN 'critical' THEN 10
                WHEN 'high' THEN 20
                WHEN 'medium' THEN 50
                ELSE 100
            END,
            execution.review_reason
        FROM qc_rule_execution AS execution
        WHERE execution.id = $1::uuid
        ON CONFLICT (rule_execution_id) DO NOTHING
        """,
        execution_id,
    )


def _presentation_from_row(row: Mapping[str, Any]) -> PresentationAssessment | None:
    surface_facts = row.get("visual_surface_facts")
    return presentation_from_capture_row(
        surface_kind=_optional_text(row.get("surface_kind")),
        metadata=_json_mapping(row.get("metadata")),
        page_state=_optional_text(row.get("page_state")),
        visual_page_state=_optional_text(row.get("visual_page_state")),
        visual_overlay_signature=_optional_text(row.get("visual_overlay_signature")),
        visual_surface_facts=_json_mapping(surface_facts) if surface_facts is not None else None,
    )


def _rule_contract_hash(rule: AggregateRuleSource) -> str:
    from services.api.app.application.inspection_work_items import (
        build_inspection_rule_contract_hash,
    )

    return build_inspection_rule_contract_hash(rule.rule_payload)


def _row_mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    return dict(row)


def _json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        return {}
    return value


def _json_text_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _json_sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _datetime_payload(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
