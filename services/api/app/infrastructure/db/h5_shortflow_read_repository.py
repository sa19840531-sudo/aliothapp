"""已发布目录与逐 Run/检查点证据的只读投影，禁止通过读请求绑定或结算。"""

import json

from fastapi.encoders import jsonable_encoder

from services.api.app.application.evidence_review_projection import (
    execution_timeline,
    manifest_review,
)
from services.api.app.application.h5_shortflow_read import definition_summary
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.journey import parse_journey_definition, parse_journey_execution_plan
from services.api.app.domain.journey.validation import stable_hash
from services.api.app.infrastructure.db.h5_shortflow_rule_results import read_shortflow_rule_results
from services.api.app.infrastructure.db.pool import get_postgres_pool


def _object(value):
    return json.loads(value) if isinstance(value, str) else dict(value or {})


class PostgresH5ShortflowReadRepository:
    def __init__(self, *, pool=None):
        self._pool = pool

    async def catalog(self):
        pool = self._pool or await get_postgres_pool()
        async with (
            pool.acquire() as connection,
            connection.transaction(isolation="repeatable_read", readonly=True),
        ):
            rows = await connection.fetch(
                "SELECT definition_payload FROM qc_journey_definition "
                "WHERE schema_version='journey-dsl.v2' ORDER BY definition_id,version DESC"
            )
            definitions = [
                definition_summary(parse_journey_definition(_object(row["definition_payload"])))
                for row in rows
            ]
            rules = await connection.fetch(
                """SELECT rule.id::text AS rule_id,rule.rule_code,rule.name,ruleset.ruleset_code
                   FROM qc_rule rule JOIN qc_ruleset ruleset ON ruleset.id=rule.ruleset_id
                   WHERE rule.status='enabled' AND ruleset.status='enabled'
                     AND rule.judge_config #>> '{capability_config,evidence_input_scope}'='journey'
                     AND rule.judge_config #>> '{capability_config,evidence_scope_key}'
                         ='journey_observation'
                     AND rule.input_contract->'target_clients' ? 'H5'
                   ORDER BY ruleset.ruleset_code,rule.rule_code,rule.id"""
            )
        return {
            "definitions": [item for item in definitions if item is not None],
            "rules": [dict(row) for row in rules],
        }

    async def read(self, *, task_id, access_scope):
        pool = self._pool or await get_postgres_pool()
        async with (
            pool.acquire() as connection,
            connection.transaction(isolation="repeatable_read", readonly=True),
        ):
            task = await connection.fetchrow(
                """SELECT task.id::text AS task_id,task.finalization_epoch,task.finalization_state,
                          task.finalization_agent_run_id::text
                   FROM qc_task task WHERE task.id=$1::uuid AND task.target_platform='h5'
                     AND task.deleted_at IS NULL
                     AND ($2::boolean OR (task.tenant_id=$3::uuid AND (
                       $4::boolean OR task.submitter_id=$5::uuid
                       OR task.department_name=$6::varchar)))""",
                task_id,
                access_scope.can_read_any,
                access_scope.tenant_id,
                access_scope.can_read_tenant,
                access_scope.actor_user_id,
                access_scope.department_scope,
            )
            if task is None:
                return None
            rows = await connection.fetch(
                "SELECT execution_plan_id,agent_run_id::text,plan_payload "
                "FROM qc_journey_execution_plan WHERE task_id=$1::uuid "
                "AND schema_version='journey-execution-plan.v2' "
                "ORDER BY created_at,execution_plan_id",
                task_id,
            )
            plans = []
            for row in rows:
                plan = parse_journey_execution_plan(_object(row["plan_payload"]))
                if (
                    plan.task_id != task_id
                    or plan.execution_plan_id != row["execution_plan_id"]
                    or plan.agent_run_id != row["agent_run_id"]
                ):
                    raise ValueError("流程冻结任务身份不一致")
                checkpoints = []
                for checkpoint in plan.checkpoints:
                    attempts = await connection.fetch(
                        """SELECT attempt_id,session_id,finalization_epoch,status,
                                  observation_status,created_at,
                                  source_capture_id::text,intent_id
                           FROM qc_journey_checkpoint_attempt WHERE execution_plan_id=$1
                           AND task_id=$2::uuid AND agent_run_id=$3::uuid AND checkpoint_id=$4
                           ORDER BY created_at,attempt_id""",
                        plan.execution_plan_id,
                        task_id,
                        plan.agent_run_id,
                        checkpoint.checkpoint_id,
                    )
                    values = []
                    for attempt in attempts:
                        value = jsonable_encoder(
                            {
                                key: attempt[key]
                                for key in (
                                    "attempt_id",
                                    "session_id",
                                    "finalization_epoch",
                                    "status",
                                    "observation_status",
                                    "created_at",
                                )
                            }
                        )
                        value["bindings"] = await self._bindings(
                            connection, task_id, plan.agent_run_id, attempt
                        )
                        values.append(value)
                    checkpoints.append(
                        {
                            "checkpoint_id": checkpoint.checkpoint_id,
                            "depends_on": list(checkpoint.depends_on),
                            "attempts": values,
                        }
                    )
                plans.append(
                    {
                        "execution_plan_id": plan.execution_plan_id,
                        "agent_run_id": plan.agent_run_id,
                        **plan.journey_version.to_dict(),
                        "scenario_key": plan.scenario_key,
                        "rule_contract_hashes": list(plan.rule_contract_hashes),
                        "checkpoints": checkpoints,
                        "timeline": execution_timeline(checkpoints),
                        **await read_shortflow_rule_results(
                            connection,
                            plan=plan,
                            task=task,
                            checkpoint_views=checkpoints,
                        ),
                    }
                )
        result = {key: task[key] for key in ("task_id", "finalization_epoch", "finalization_state")}
        result["plans"] = plans
        result = jsonable_encoder(result)
        return {**result, "snapshot_hash": stable_hash(result)}

    async def _bindings(self, connection, task_id, run_id, attempt):
        rows = await connection.fetch(
            """SELECT evidence.role,evidence.capture_id::text,evidence.action_execution_id,
                      evidence.manifest_id,evidence.manifest_version,evidence.binding_payload,
                      capture.captured_at,capture.surface_session_id,capture.capture_role,
                      capture.metadata->>'finalization_epoch' AS capture_epoch,
                      action.started_at AS action_started_at,
                      action.action_finished_at AS action_finished_at,
                      manifest.manifest_payload,bound.manifest_payload AS bound_manifest_payload
               FROM qc_journey_checkpoint_evidence evidence
               LEFT JOIN qc_capture capture ON capture.id=evidence.capture_id
                 AND capture.task_id=$2::uuid AND capture.agent_run_id=$3::uuid
               LEFT JOIN qc_action_execution action
                 ON action.action_execution_id=evidence.action_execution_id
                 AND action.task_id=$2::uuid AND action.session_id=$4
                 AND action.finalization_epoch=$5
                 AND action.intent_id=$6 AND action.requested_source_capture_id=$7::uuid
                 AND action.status='completed' AND capture.capture_status='captured'
                 AND action.created_at>=$8 AND capture.captured_at>=$8
                 AND capture.action_execution_id=action.action_execution_id
                 AND capture.session_command_sequence=action.command_sequence
                 AND ((evidence.role='before_action' AND capture.id=action.source_capture_id
                       AND capture.parent_capture_id=$7::uuid
                       AND capture.captured_at<=action.started_at)
                   OR (evidence.role='after_action' AND capture.id=action.result_capture_id
                       AND capture.parent_capture_id=action.source_capture_id
                       AND capture.captured_at>=action.action_finished_at))
               LEFT JOIN qc_evidence_manifest bound ON bound.capture_id=capture.id
                 AND bound.manifest_id=evidence.manifest_id
                 AND bound.manifest_version=evidence.manifest_version
               LEFT JOIN LATERAL (
                   SELECT manifest_payload FROM qc_evidence_manifest
                   WHERE capture_id=capture.id ORDER BY manifest_version DESC LIMIT 1
               ) manifest ON action.action_execution_id IS NOT NULL
               WHERE evidence.attempt_id=$1 ORDER BY evidence.role DESC""",
            attempt["attempt_id"],
            task_id,
            run_id,
            attempt["session_id"],
            attempt["finalization_epoch"],
            attempt["intent_id"],
            attempt["source_capture_id"],
            attempt["created_at"],
        )
        values = []
        for row in rows:
            value = {
                key: row[key]
                for key in (
                    "role",
                    "capture_id",
                    "action_execution_id",
                    "manifest_id",
                    "manifest_version",
                    "captured_at",
                )
            }
            value.update(
                integrity="binding_mismatch",
                latest_manifest_version=None,
                quality_status="unknown",
                artifacts=[],
                manifest_versions=[],
                action_started_at=row["action_started_at"],
                action_finished_at=row["action_finished_at"],
            )
            values.append(value)
            if (
                row["manifest_payload"] is None
                or row["bound_manifest_payload"] is None
                or row["surface_session_id"] != attempt["session_id"]
                or row["capture_role"] != row["role"]
                or row["capture_epoch"] != str(attempt["finalization_epoch"])
            ):
                continue
            try:
                manifest = EvidenceManifestReference.from_dict(_object(row["manifest_payload"]))
                bound = EvidenceManifestReference.from_dict(_object(row["bound_manifest_payload"]))
            except ValueError:
                continue
            value.update(
                latest_manifest_version=manifest.manifest_version,
                quality_status=manifest.quality_status.value,
            )
            artifacts = await connection.fetch(
                "SELECT id::text AS artifact_id,artifact_type,checksum,storage_path,"
                "content_type,size_bytes FROM qc_evidence_artifact "
                "WHERE task_id=$1::uuid AND page_capture_id=$2::uuid ORDER BY id",
                task_id, row["capture_id"],
            )
            versions = await connection.fetch(
                "SELECT manifest_payload FROM qc_evidence_manifest "
                "WHERE task_id=$1::uuid AND capture_id=$2::uuid AND session_id=$3 "
                "ORDER BY manifest_version DESC,manifest_id",
                task_id, row["capture_id"], attempt["session_id"],
            )
            for version in versions:
                try:
                    history = EvidenceManifestReference.from_dict(
                        _object(version["manifest_payload"])
                    )
                except ValueError:
                    continue
                if (history.task_id, history.capture_id, history.session_id) != (
                    task_id, row["capture_id"], attempt["session_id"],
                ):
                    continue
                value["manifest_versions"].append(manifest_review(
                    history, artifacts=artifacts, role=row["role"],
                    bound=bound.manifest_id, latest=manifest.manifest_id,
                ))
            if (
                manifest.task_id != bound.task_id
                or manifest.session_id != bound.session_id
                or manifest.capture_id != bound.capture_id
                or manifest.artifacts != bound.artifacts
            ):
                value["integrity"] = "manifest_changed"
                continue
            binding = _object(row["binding_payload"])
            if (
                manifest.task_id != task_id
                or manifest.capture_id != row["capture_id"]
                or manifest.session_id != attempt["session_id"]
                or bound.manifest_hash != binding.get("manifest_hash")
                or set(binding.get("artifact_ids", []))
                != {item.artifact_id for item in manifest.artifacts}
            ):
                continue
            actual = {item["artifact_id"]: item for item in artifacts}
            if set(actual) != {item.artifact_id for item in manifest.artifacts} or any(
                item.role != row["role"]
                or any(
                    actual[item.artifact_id][key] != getattr(item, key)
                    for key in (
                        "artifact_type",
                        "checksum",
                        "storage_path",
                        "size_bytes",
                        "content_type",
                    )
                )
                for item in manifest.artifacts
            ):
                continue
            value["integrity"] = "current"
            value["artifacts"] = [
                {
                    key: item[key]
                    for key in (
                        "artifact_id",
                        "artifact_type",
                        "checksum",
                        "content_type",
                        "size_bytes",
                    )
                }
                for item in artifacts
            ]
        return jsonable_encoder(values)
