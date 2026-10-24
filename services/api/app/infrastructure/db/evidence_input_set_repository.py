"""EvidenceInputSet 的 PostgreSQL 不可变仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.application.evidence_input_sets import PersistedEvidenceInputSet
from services.api.app.domain.evidence import (
    EvidenceInputSet,
    EvidenceInputSetMember,
    EvidenceManifestReference,
)
from services.api.app.infrastructure.db.aggregate_rule_inventory import read_frozen_aggregate_rule
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.task_rule_inventory_sql import frozen_h5_rule_identity_sql


class EvidenceInputSetPersistenceError(RuntimeError):
    """输入集合的任务阶段、规则快照或证据血缘无法通过可信校验。"""


class PostgresEvidenceInputSetRepository:
    """在任务封存锁内保存 EvidenceInputSet 及其有序成员。"""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        pool: Any | None = None,
        # 探索 Run 失败后任务会先转 review，收口阶段仍是 session_draining，聚合输入
        # 集合必须还能封存，否则门禁永远等不到终态（与 count_waitable_nonterminal_work
        # 已经承认的「review + session_draining」口径一致）。阶段仍由下一行严格限定。
        permitted_task_statuses: tuple[str, ...] = ("running", "review"),
        permitted_finalization_states: tuple[str, ...] = ("session_draining",),
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._permitted_task_statuses = _validated_text_tuple(
            permitted_task_statuses,
            "permitted_task_statuses",
        )
        self._permitted_finalization_states = _validated_text_tuple(
            permitted_finalization_states,
            "permitted_finalization_states",
        )

    async def add(
        self,
        *,
        input_set: EvidenceInputSet,
    ) -> PersistedEvidenceInputSet:
        if not isinstance(input_set, EvidenceInputSet):
            raise TypeError("input_set 必须为 EvidenceInputSet")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"task-finalization:{input_set.task_id}",
            )
            return await self.add_on_connection(
                connection,
                input_set=input_set,
            )

    async def add_on_connection(
        self,
        connection: Any,
        *,
        input_set: EvidenceInputSet,
    ) -> PersistedEvidenceInputSet:
        """复用调用方事务，供 InputSet、Work、Candidate、Outbox 原子提交。"""

        if not isinstance(input_set, EvidenceInputSet):
            raise TypeError("input_set 必须为 EvidenceInputSet")
        await self._validate_context(connection, input_set=input_set)
        for member in input_set.members:
            await self._validate_member(connection, input_set=input_set, member=member)
        created, persisted_input_set = await self._insert_or_verify(
            connection,
            input_set=input_set,
        )
        await self._verify_persisted_members(
            connection,
            input_set=persisted_input_set,
        )
        return PersistedEvidenceInputSet(
            input_set=persisted_input_set,
            created=created,
        )

    async def get(self, *, input_set_id: str) -> EvidenceInputSet | None:
        if not isinstance(input_set_id, str) or not input_set_id.strip():
            raise ValueError("input_set_id 不能为空")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            payload = await connection.fetchval(
                """
                SELECT input_set_payload
                FROM qc_evidence_input_set
                WHERE input_set_id = $1
                """,
                input_set_id,
            )
        if payload is None:
            return None
        try:
            return EvidenceInputSet.from_dict(
                _as_mapping(payload, "input_set_payload")
            )
        except ValueError as exc:
            raise EvidenceInputSetPersistenceError(
                "已持久化 EvidenceInputSet 载荷无法通过协议校验"
            ) from exc

    async def _validate_context(self, connection: Any, *, input_set: EvidenceInputSet) -> None:
        frozen_rule = None if input_set.scope.value == "page" else await read_frozen_aggregate_rule(
            connection, task_id=input_set.task_id, rule_id=input_set.rule_id,
            rule_code=input_set.rule_code, contract_hash=input_set.rule_contract_hash,
            scope=input_set.scope.value, scope_key=input_set.scope_key,
            source_agent_run_id=input_set.source_agent_run_id,
            finalization_epoch=input_set.finalization_epoch,
            ruleset_snapshot_hash=input_set.ruleset_snapshot_hash,
        )
        if frozen_rule is not None:
            valid = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM qc_task AS task
                    JOIN qc_agent_run AS run ON run.task_id=task.id AND run.id=$2::uuid
                    JOIN qc_rule AS rule ON rule.id=$3::uuid
                    WHERE task.id=$1::uuid AND task.finalization_agent_run_id=run.id
                      AND task.status=ANY($4::text[])
                      AND task.finalization_state=ANY($5::text[])
                      AND task.finalization_epoch=$6
                )
                """,
                input_set.task_id, input_set.source_agent_run_id, input_set.rule_id,
                list(self._permitted_task_statuses), list(self._permitted_finalization_states),
                input_set.finalization_epoch,
            )
            if not valid:
                raise EvidenceInputSetPersistenceError("冻结聚合输入的 Task/Run/阶段不匹配")
            return
        valid = await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM qc_task AS task
                INNER JOIN qc_agent_run AS agent_run
                    ON agent_run.task_id = task.id
                   AND agent_run.id = $2::uuid
                INNER JOIN qc_ruleset AS ruleset
                    ON ruleset.id = task.ruleset_id
                INNER JOIN qc_rule AS rule
                    ON rule.id = $3::uuid
                WHERE task.id = $1::uuid
                  AND task.status = ANY($4::text[])
                  AND task.finalization_state = ANY($5::text[])
                  AND task.finalization_epoch = $6
                  AND ruleset.snapshot_hash = $7
                  AND __H5_RULE_IDENTITY__
            )
            """.replace("__H5_RULE_IDENTITY__", frozen_h5_rule_identity_sql(
                task="task", rule="rule", code="$8::varchar", ruleset="task.ruleset_id",
            )),
            input_set.task_id,
            input_set.source_agent_run_id,
            input_set.rule_id,
            list(self._permitted_task_statuses),
            list(self._permitted_finalization_states),
            input_set.finalization_epoch,
            input_set.ruleset_snapshot_hash,
            input_set.rule_code,
        )
        if not valid:
            raise EvidenceInputSetPersistenceError(
                "任务阶段、Finalization Epoch、Agent Run 或规则集快照不匹配"
            )

    async def _validate_member(
        self,
        connection: Any,
        *,
        input_set: EvidenceInputSet,
        member: EvidenceInputSetMember,
    ) -> None:
        row = await connection.fetchrow(
            """
            SELECT
                capture.capture_role,
                manifest.manifest_payload
            FROM qc_capture AS capture
            INNER JOIN qc_evidence_manifest AS manifest
                ON manifest.task_id = capture.task_id
               AND manifest.capture_id = capture.id
               AND manifest.manifest_id = $4
               AND manifest.manifest_version = $5
               AND manifest.manifest_hash = $6
               AND manifest.quality_status = 'pass'
            WHERE capture.task_id = $1::uuid
              AND capture.id = $2::uuid
              AND capture.agent_run_id = $3::uuid
              AND capture.capture_status = 'captured'
            """,
            input_set.task_id,
            member.capture_id,
            input_set.source_agent_run_id,
            member.manifest_id,
            member.manifest_version,
            member.manifest_hash,
        )
        if row is None or str(row["capture_role"]) != member.capture_role:
            raise EvidenceInputSetPersistenceError(
                f"Capture {member.capture_id} 的归属、角色或 PASS Manifest 不匹配"
            )
        try:
            manifest = EvidenceManifestReference.from_dict(
                _as_mapping(row["manifest_payload"], "manifest_payload")
            )
        except ValueError as exc:
            raise EvidenceInputSetPersistenceError(
                f"Manifest {member.manifest_id} 的证据闭包无法通过协议校验"
            ) from exc
        expected_artifact_ids = tuple(item.artifact_id for item in manifest.artifacts)
        if (
            manifest.task_id != input_set.task_id
            or manifest.capture_id != member.capture_id
            or manifest.manifest_id != member.manifest_id
            or manifest.manifest_version != member.manifest_version
            or manifest.manifest_hash != member.manifest_hash
            or expected_artifact_ids != member.artifact_ids
            or manifest.observation_packet_ids != member.observation_packet_ids
        ):
            raise EvidenceInputSetPersistenceError(
                f"Manifest {member.manifest_id} 与成员证据闭包不一致"
            )

    async def _insert_or_verify(
        self,
        connection: Any,
        *,
        input_set: EvidenceInputSet,
    ) -> tuple[bool, EvidenceInputSet]:
        payload = input_set.to_dict()
        existing = await connection.fetchrow(
            """
            SELECT input_set_id, input_set_hash, input_set_payload
            FROM qc_evidence_input_set
            WHERE task_id = $1::uuid
              AND rule_id = $2::uuid
              AND scope = $3
              AND scope_key = $4
              AND finalization_epoch = $5
            """,
            input_set.task_id,
            input_set.rule_id,
            input_set.scope.value,
            input_set.scope_key,
            input_set.finalization_epoch,
        )
        if existing is not None:
            try:
                persisted = EvidenceInputSet.from_dict(
                    _as_mapping(existing["input_set_payload"], "input_set_payload")
                )
            except ValueError as exc:
                raise EvidenceInputSetPersistenceError(
                    "已持久化 EvidenceInputSet 载荷无法通过协议校验"
                ) from exc
            if (
                str(existing["input_set_id"]) != input_set.input_set_id
                or str(existing["input_set_hash"]) != input_set.input_set_hash
                or persisted.input_set_id != input_set.input_set_id
                or persisted.input_set_hash != input_set.input_set_hash
            ):
                raise EvidenceInputSetPersistenceError(
                    "同一权威候选快照已被其他 EvidenceInputSet 内容占用"
                )
            return False, persisted
        row = await connection.fetchrow(
            """
            INSERT INTO qc_evidence_input_set (
                input_set_id, input_set_hash, task_id, source_agent_run_id,
                rule_id, rule_code, rule_contract_hash, ruleset_snapshot_hash,
                scope, scope_key, finalization_epoch, eligibility_snapshot_id,
                eligibility_snapshot_hash, selection_policy_hash,
                eligible_capture_ids, protocol_version, input_set_payload, sealed_at
            ) VALUES (
                $1, $2, $3::uuid, $4::uuid,
                $5::uuid, $6, $7, $8,
                $9, $10, $11, $12,
                $13, $14, $15::jsonb, $16, $17::jsonb, $18
            )
            RETURNING input_set_id
            """,
            input_set.input_set_id,
            input_set.input_set_hash,
            input_set.task_id,
            input_set.source_agent_run_id,
            input_set.rule_id,
            input_set.rule_code,
            input_set.rule_contract_hash,
            input_set.ruleset_snapshot_hash,
            input_set.scope.value,
            input_set.scope_key,
            input_set.finalization_epoch,
            input_set.eligibility_snapshot_id,
            input_set.eligibility_snapshot_hash,
            input_set.selection_policy_hash,
            _json_dumps(list(input_set.eligible_capture_ids)),
            input_set.protocol_version,
            _json_dumps(payload),
            input_set.sealed_at,
        )
        if row is None or str(row["input_set_id"]) != input_set.input_set_id:
            raise EvidenceInputSetPersistenceError("EvidenceInputSet 写入失败")
        for member in input_set.members:
            await connection.execute(
                """
                INSERT INTO qc_evidence_input_set_member (
                    input_set_id, task_id, ordinal, capture_id, capture_role,
                    page_sequence, manifest_id, manifest_version, manifest_hash,
                    artifact_ids, observation_packet_ids, journey_execution_id,
                    environment_profile_id
                ) VALUES (
                    $1, $2::uuid, $3, $4::uuid, $5,
                    $6, $7, $8, $9,
                    $10::jsonb, $11::jsonb, $12, $13
                )
                """,
                input_set.input_set_id,
                input_set.task_id,
                member.ordinal,
                member.capture_id,
                member.capture_role,
                member.page_sequence,
                member.manifest_id,
                member.manifest_version,
                member.manifest_hash,
                _json_dumps(list(member.artifact_ids)),
                _json_dumps(list(member.observation_packet_ids)),
                member.journey_execution_id,
                member.environment_profile_id,
            )
        return True, input_set

    async def _verify_persisted_members(
        self,
        connection: Any,
        *,
        input_set: EvidenceInputSet,
    ) -> None:
        rows = await connection.fetch(
            """
            SELECT
                ordinal, capture_id::text AS capture_id, capture_role,
                page_sequence, manifest_id, manifest_version, manifest_hash,
                artifact_ids, observation_packet_ids, journey_execution_id,
                environment_profile_id
            FROM qc_evidence_input_set_member
            WHERE input_set_id = $1
            ORDER BY ordinal
            """,
            input_set.input_set_id,
        )
        try:
            persisted = tuple(_member_from_row(row) for row in rows)
        except ValueError as exc:
            raise EvidenceInputSetPersistenceError(
                "已持久化 EvidenceInputSet 成员无法通过协议校验"
            ) from exc
        if persisted != input_set.members:
            raise EvidenceInputSetPersistenceError(
                "EvidenceInputSet 成员未完整落库或与封存载荷不一致"
            )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _member_from_row(row: Mapping[str, Any]) -> EvidenceInputSetMember:
    return EvidenceInputSetMember(
        ordinal=int(row["ordinal"]),
        capture_id=str(row["capture_id"]),
        capture_role=str(row["capture_role"]),
        page_sequence=int(row["page_sequence"]),
        manifest_id=str(row["manifest_id"]),
        manifest_version=int(row["manifest_version"]),
        manifest_hash=str(row["manifest_hash"]),
        artifact_ids=_as_text_tuple(row["artifact_ids"], "artifact_ids"),
        observation_packet_ids=_as_text_tuple(
            row["observation_packet_ids"],
            "observation_packet_ids",
        ),
        journey_execution_id=(
            str(row["journey_execution_id"])
            if row["journey_execution_id"] is not None
            else None
        ),
        environment_profile_id=(
            str(row["environment_profile_id"])
            if row["environment_profile_id"] is not None
            else None
        ),
    )


def _validated_text_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    items = tuple(values)
    if not items or any(
        not isinstance(item, str) or not item.strip() or item != item.strip()
        for item in items
    ):
        raise ValueError(f"{field_name} 必须为非空状态元组")
    return tuple(dict.fromkeys(items))


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise EvidenceInputSetPersistenceError(f"{field_name} 必须为对象")
    return value


def _as_text_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise EvidenceInputSetPersistenceError(f"{field_name} 必须为数组")
    items = tuple(value)
    if any(not isinstance(item, str) or not item for item in items):
        raise EvidenceInputSetPersistenceError(f"{field_name} 只能包含非空字符串")
    return items


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
