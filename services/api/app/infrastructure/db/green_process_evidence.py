"""重读绿色 TASK 输入的权威台账和原件，不增加采集、网络或设备权限。"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from services.api.app.application.green_evidence_contracts import (
    GREEN_PROCESS_SCOPE_KEYS,
    GreenAction,
    GreenCapture,
    GreenOriginal,
    _verified_evidence_set,
)
from services.api.app.domain.evidence import EvidenceInputSet, EvidenceManifestReference
from services.api.app.domain.interaction import ActionIntent, build_action_intent_hash
from services.api.app.infrastructure.db.evidence_input_set_repository import _member_from_row

_TRUSTED_SOURCES = {"baseline", "rule_supplement", "app_runtime"}
_MAX_CAPTURE_COUNT = 32
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 128 * 1024 * 1024


class _GreenReadCapacityExceeded(ValueError):
    """技术读取容量不足时拒绝整个输入，不截取早期样本形成判定。"""


def _object(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("绿色原件台账不是对象")
    return value


def _owned_path(value, *, task_id, capture_id, artifact_id):
    if not isinstance(value, str):
        return False
    path = PurePosixPath(value)
    return (
        isinstance(value, str)
        and "\\" not in value
        and not path.is_absolute()
        and path.parts[:2] == (task_id, capture_id)
        and len(path.parts) == 3
        and path.stem == artifact_id
        and not {".", ".."}.intersection(path.parts)
    )


async def load_green_process_evidence(
    connection, *, input_set, artifact_store, allow_closed_epoch=False
):
    """调用方持有原聚合读取事务；本函数只有 SELECT 和原件读取，不落库。"""
    if not isinstance(input_set, EvidenceInputSet):
        raise TypeError("绿色输入必须使用已校验的 EvidenceInputSet")
    if type(allow_closed_epoch) is not bool:
        raise TypeError("封存周期读取标志必须为布尔值")
    base = dict(
        task_id=input_set.task_id,
        run_id=input_set.source_agent_run_id,
        epoch=input_set.finalization_epoch,
        input_set_id=input_set.input_set_id,
        input_hash=input_set.input_set_hash,
        scope_key=input_set.scope_key,
        target_platform=None,
    )
    issues, captures, actions = [], [], []
    source_hashes = []

    def result(**values):
        digest_payload = {**base, **values, "source_hashes": source_hashes}
        digest_payload.pop("captures", None)
        digest_payload.pop("actions", None)
        fingerprint = hashlib.sha256(
            json.dumps(
                digest_payload,
                sort_keys=True,
                default=str,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        return _verified_evidence_set(**base, **values, fingerprint=fingerprint)

    if input_set.scope.value != "task" or input_set.scope_key not in GREEN_PROCESS_SCOPE_KEYS:
        return result(issues=("green_input_scope_mismatch",))
    if len(input_set.members) > _MAX_CAPTURE_COUNT:
        return result(issues=("green_capture_capacity_exceeded",))
    saved = await connection.fetchrow(
        "SELECT input_set_payload,input_set_hash FROM qc_evidence_input_set "
        "WHERE input_set_id=$1 AND task_id=$2::uuid AND source_agent_run_id=$3::uuid "
        "AND finalization_epoch=$4",
        input_set.input_set_id,
        input_set.task_id,
        input_set.source_agent_run_id,
        input_set.finalization_epoch,
    )
    source_hashes.append(_source_hash(saved))
    try:
        if (
            saved is None
            or saved["input_set_hash"] != input_set.input_set_hash
            or EvidenceInputSet.from_dict(_object(saved["input_set_payload"])) != input_set
        ):
            raise ValueError("input_set_mismatch")
        member_rows = await connection.fetch(
            "SELECT ordinal,capture_id::text AS capture_id,capture_role,page_sequence,"
            "manifest_id,manifest_version,manifest_hash,artifact_ids,observation_packet_ids,"
            "journey_execution_id,environment_profile_id FROM qc_evidence_input_set_member "
            "WHERE input_set_id=$1 ORDER BY ordinal",
            input_set.input_set_id,
        )
        source_hashes.extend(_source_hash(row) for row in member_rows)
        if tuple(_member_from_row(row) for row in member_rows) != input_set.members:
            raise ValueError("input_set_members_mismatch")
    except (ValueError, TypeError, KeyError):
        return result(issues=("green_persisted_input_mismatch",))
    identity = await connection.fetchrow(
        "SELECT task.finalization_epoch,task.finalization_agent_run_id::text AS owner_run,"
        "task.target_platform "
        "FROM qc_task task JOIN qc_agent_run run ON run.task_id=task.id AND run.id=$2::uuid "
        "WHERE task.id=$1::uuid AND task.deleted_at IS NULL",
        input_set.task_id,
        input_set.source_agent_run_id,
    )
    if (
        identity is not None
        and identity["finalization_epoch"] != input_set.finalization_epoch
        and allow_closed_epoch
    ):
        from services.api.app.infrastructure.db.journey_epoch import closed_journey_epoch_is_bound

        if await closed_journey_epoch_is_bound(
            connection,
            task_id=input_set.task_id,
            agent_run_id=input_set.source_agent_run_id,
            epoch=input_set.finalization_epoch,
        ):
            # 仅正式Gate证明同一冻结周期可读时归一；不能让合法任务封存本身改变当次原件摘要。
            identity = {**dict(identity), "finalization_epoch": input_set.finalization_epoch}
    if (
        identity is None
        or identity["finalization_epoch"] != input_set.finalization_epoch
        or identity["owner_run"] != input_set.source_agent_run_id
    ):
        return result(issues=("green_task_run_epoch_mismatch",))
    source_hashes.append(_source_hash(identity))
    base["target_platform"] = identity.get("target_platform")
    total_bytes = 0
    for member in input_set.members:
        try:
            row = await connection.fetchrow(
                "SELECT capture.*,manifest.manifest_payload FROM qc_capture capture "
                "JOIN qc_surface_session session ON session.session_id=capture.surface_session_id "
                "AND session.task_id=capture.task_id AND session.finalization_epoch=$6 "
                "JOIN qc_evidence_manifest manifest ON manifest.capture_id=capture.id "
                "AND manifest.manifest_id=$4 AND manifest.manifest_version=$5 "
                "WHERE capture.id=$1::uuid AND capture.task_id=$2::uuid "
                "AND capture.agent_run_id=$3::uuid",
                member.capture_id,
                input_set.task_id,
                input_set.source_agent_run_id,
                member.manifest_id,
                member.manifest_version,
                input_set.finalization_epoch,
            )
            source_hashes.append(_source_hash(row))
            if row is None:
                raise ValueError("capture_identity_mismatch")
            if (
                str(row["id"]) != member.capture_id
                or str(row["task_id"]) != input_set.task_id
                or str(row["agent_run_id"]) != input_set.source_agent_run_id
            ):
                raise ValueError("capture_identity_mismatch")
            manifest = EvidenceManifestReference.from_dict(_object(row["manifest_payload"]))
            metadata = _object(row["metadata"])
            if (
                manifest.task_id != input_set.task_id
                or manifest.capture_id != member.capture_id
                or manifest.session_id != row["surface_session_id"]
                or manifest.manifest_hash != member.manifest_hash
                or manifest.manifest_id != member.manifest_id
                or manifest.manifest_version != member.manifest_version
                or manifest.quality_status.value != "pass"
                or row["capture_status"] != "captured"
                or row["capture_role"] != member.capture_role
                or metadata.get("finalization_epoch") != input_set.finalization_epoch
                or tuple(item.artifact_id for item in manifest.artifacts) != member.artifact_ids
            ):
                raise ValueError("manifest_or_epoch_mismatch")
            latest = await connection.fetchrow(
                "SELECT manifest_payload FROM qc_evidence_manifest WHERE capture_id=$1::uuid "
                "ORDER BY manifest_version DESC LIMIT 1",
                member.capture_id,
            )
            source_hashes.append(_source_hash(latest))
            if (
                latest is None
                or EvidenceManifestReference.from_dict(_object(latest["manifest_payload"]))
                != manifest
            ):
                raise ValueError("latest_manifest_changed")
            originals = []
            for reference in manifest.artifacts:
                artifact = await connection.fetchrow(
                    "SELECT * FROM qc_evidence_artifact WHERE id=$1::uuid "
                    "AND task_id=$2::uuid AND page_capture_id=$3::uuid",
                    reference.artifact_id,
                    input_set.task_id,
                    member.capture_id,
                )
                source_hashes.append(_source_hash(artifact))
                if (
                    artifact is None
                    or artifact["source"] not in _TRUSTED_SOURCES
                    or artifact["lifecycle_status"] not in ("active", "archived")
                ):
                    raise ValueError("artifact_owner_or_lifecycle_mismatch")
                if (
                    reference.role != member.capture_role
                    or any(
                        artifact[key] != getattr(reference, key)
                        for key in (
                            "artifact_type",
                            "checksum",
                            "storage_path",
                            "size_bytes",
                            "content_type",
                        )
                    )
                    or not _owned_path(
                        artifact["storage_path"],
                        task_id=input_set.task_id,
                        capture_id=member.capture_id,
                        artifact_id=reference.artifact_id,
                    )
                ):
                    raise ValueError("artifact_identity_mismatch")
                size = artifact["size_bytes"]
                if (
                    type(size) is not int
                    or not 0 <= size <= _MAX_ARTIFACT_BYTES
                    or total_bytes + size > _MAX_TOTAL_BYTES
                ):
                    raise _GreenReadCapacityExceeded("artifact_capacity_exceeded")
                payload = await artifact_store.read(artifact["storage_path"])
                source_hashes.append(
                    hashlib.sha256(payload).hexdigest()
                    if isinstance(payload, bytes)
                    else "original_bytes_missing"
                )
                if (
                    not isinstance(payload, bytes)
                    or len(payload) != size
                    or "sha256:" + hashlib.sha256(payload).hexdigest() != reference.checksum
                ):
                    raise ValueError("artifact_original_bytes_mismatch")
                total_bytes += size
                originals.append(
                    GreenOriginal(
                        reference.artifact_id,
                        reference.artifact_type,
                        member.capture_id,
                        reference.checksum,
                        payload,
                    )
                )
            captures.append(
                GreenCapture(
                    member.capture_id,
                    manifest.session_id,
                    member.capture_role,
                    row["action_execution_id"],
                    row["session_command_sequence"],
                    str(row["parent_capture_id"]) if row["parent_capture_id"] else None,
                    row["captured_at"],
                    member.manifest_id,
                    member.manifest_version,
                    tuple(originals),
                )
            )
        except _GreenReadCapacityExceeded:
            return result(issues=("green_original_capacity_exceeded",))
        except (ValueError, TypeError, KeyError, OSError):
            issues.append(f"green_capture_unverified:{member.capture_id}")
    by_id = {item.capture_id: item for item in captures}
    observed = await connection.fetch(
        "SELECT capture.id::text,capture.capture_status,latest.manifest_payload "
        "FROM qc_capture capture LEFT JOIN LATERAL(SELECT manifest_payload "
        "FROM qc_evidence_manifest WHERE capture_id=capture.id "
        "ORDER BY manifest_version DESC LIMIT 1) latest ON true "
        "WHERE capture.task_id=$1::uuid AND capture.agent_run_id=$2::uuid "
        "AND (capture.metadata->>'finalization_epoch'=$3 "
        "OR coalesce(capture.metadata->>'finalization_epoch','') !~ '^[0-9]+$') "
        "ORDER BY capture.id",
        input_set.task_id,
        input_set.source_agent_run_id,
        str(input_set.finalization_epoch),
    )
    frozen_capture_ids = {member.capture_id for member in input_set.members}
    for row in observed:
        source_hashes.append(_source_hash(row))
        if row["id"] not in frozen_capture_ids:
            issues.append("green_capture_not_in_frozen_input:" + row["id"])
    rows = await connection.fetch(
        "SELECT action.* FROM qc_action_execution action "
        "LEFT JOIN qc_capture source ON source.id=action.requested_source_capture_id "
        "AND source.task_id=action.task_id "
        "WHERE action.task_id=$1::uuid AND action.finalization_epoch=$2 "
        "AND (source.agent_run_id=$3::uuid OR action.requested_source_capture_id IS NULL) "
        "ORDER BY action.created_at,action.action_execution_id",
        input_set.task_id,
        input_set.finalization_epoch,
        input_set.source_agent_run_id,
    )
    for row in rows:
        source_hashes.append(_source_hash(row))
        before = by_id.get(str(row["source_capture_id"]))
        after = by_id.get(str(row["result_capture_id"]))
        if before is None or after is None:
            issues.append("green_action_window_not_frozen:" + row["action_execution_id"])
            continue
        if not _valid_action(row, before, after):
            issues.append("green_action_lineage_mismatch:" + row["action_execution_id"])
            continue
        actions.append(
            GreenAction(
                row["action_execution_id"],
                row["session_id"],
                before.capture_id,
                after.capture_id,
                row["command_sequence"],
                row["started_at"],
                row["action_finished_at"],
                json.dumps(_object(row["intent_payload"])).encode(),
            )
        )
    if not actions:
        issues.append("green_formal_action_window_missing")
    return result(
        captures=tuple(captures), actions=tuple(actions), issues=tuple(dict.fromkeys(issues))
    )


def _source_hash(row):
    """原始台账仅参与内部摘要，不把元数据或存储地址带到公开结果。"""
    return hashlib.sha256(
        json.dumps(
            dict(row) if row is not None else None,
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


def _valid_action(row, before, after):
    """严格复用正式动作前后血缘；执行完成不能推导业务成功。"""
    if any(row[key] is None for key in ("started_at", "action_finished_at", "command_sequence")):
        return False
    try:
        intent = ActionIntent.from_dict(_object(row["intent_payload"]))
        if (
            intent.task_id != str(row["task_id"])
            or intent.session_id != row["session_id"]
            or intent.intent_id != row["intent_id"]
            or intent.idempotency_key != row["idempotency_key"]
            or intent.source_capture_id != str(row["requested_source_capture_id"])
            or build_action_intent_hash(intent) != row["intent_hash"]
        ):
            return False
    except (ValueError, TypeError, KeyError):
        return False
    return (
        row["status"] == "completed"
        and before.role == "before_action"
        and after.role == "after_action"
        and before.session_id == after.session_id == row["session_id"]
        and before.action_id == after.action_id == row["action_execution_id"]
        and before.command_sequence == after.command_sequence == row["command_sequence"]
        and before.parent_capture_id == str(row["requested_source_capture_id"])
        and after.parent_capture_id == before.capture_id
        and before.captured_at
        <= row["started_at"]
        <= row["action_finished_at"]
        <= after.captured_at
    )
