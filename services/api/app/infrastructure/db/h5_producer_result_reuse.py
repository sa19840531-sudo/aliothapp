"""只读核验主 Run 结果的原输入闭包，并在回填 SQL 中重验同一事实。"""

from __future__ import annotations

import json
from typing import Any

from services.api.app.domain.evidence import (
    EvidenceInputScope,
    EvidenceInputSet,
    EvidenceManifestReference,
    EvidenceQualityStatus,
)

# 预读与最终回填使用完全相同的投影，不能把预读后改变的事实当成已验证。
PAGE_INPUT_EVIDENCE_SQL = """
        SELECT jsonb_agg(jsonb_build_object(
            'binding', to_jsonb(binding), 'capture', to_jsonb(capture),
            'original', to_jsonb(original), 'latest', to_jsonb(latest),
            'artifacts', (SELECT COALESCE(jsonb_agg(to_jsonb(artifact) ORDER BY ids.ordinal),'[]')
                FROM jsonb_array_elements_text(binding.artifact_ids)
                     WITH ORDINALITY AS ids(artifact_id,ordinal)
                LEFT JOIN qc_evidence_artifact AS artifact ON artifact.id::text=ids.artifact_id),
            'packets', (SELECT COALESCE(jsonb_agg(to_jsonb(packet) ORDER BY ids.ordinal),'[]')
                FROM jsonb_array_elements_text(binding.observation_packet_ids)
                     WITH ORDINALITY AS ids(packet_id,ordinal)
                LEFT JOIN qc_observation_packet AS packet ON packet.id::text=ids.packet_id)
        ) ORDER BY binding.ordinal) AS items
        FROM qc_evidence_input_set_member AS binding
        LEFT JOIN qc_capture AS capture ON capture.id=binding.capture_id
        LEFT JOIN qc_evidence_manifest AS original ON original.manifest_id=binding.manifest_id
        LEFT JOIN LATERAL (
            SELECT manifest.* FROM qc_evidence_manifest AS manifest
            WHERE manifest.task_id=binding.task_id AND manifest.capture_id=binding.capture_id
            ORDER BY (to_jsonb(manifest)->>'manifest_version')::int DESC NULLS LAST,
                manifest.manifest_id DESC LIMIT 1
        ) AS latest ON TRUE
        WHERE binding.input_set_id=inputs.input_set_id
"""

PRODUCER_REUSE_PROOF_SQL = """
    SELECT work.id AS work_id, member.rule_id, execution.id AS execution_id,
        jsonb_build_object(
            'work', to_jsonb(work), 'member', to_jsonb(member),
            'execution', to_jsonb(execution), 'input_set', to_jsonb(inputs),
            'evidence', evidence.items
        ) AS proof
    FROM qc_inspection_work_item AS work
    JOIN qc_task AS task ON task.id=work.task_id AND lower(task.target_platform)='h5'
    JOIN qc_inspection_work_item_rule AS member ON member.work_item_id=work.id
    JOIN qc_rule_execution AS execution
      ON execution.task_id=work.task_id AND execution.rule_id=member.rule_id
     AND execution.page_capture_id=work.page_capture_id
     AND execution.agent_run_id IN (work.producer_agent_run_id,work.consumer_agent_run_id)
    JOIN qc_evidence_input_set AS inputs
      ON inputs.input_set_id=member.evidence_input_set_id
     AND inputs.input_set_id=execution.evidence_input_set_id
    JOIN LATERAL (__PAGE_INPUT_EVIDENCE__) AS evidence ON TRUE
    WHERE work.task_id=$1::uuid
      AND work.status IN ('pending','waiting_evidence','retry_wait','leased','running')
      AND work.control_mode='enforced' AND member.status IN ('pending','judged')
      AND work.finalization_epoch=task.finalization_epoch
""".replace("__PAGE_INPUT_EVIDENCE__", PAGE_INPUT_EVIDENCE_SQL)


async def verified_producer_proofs(connection: Any, *, task_id: str) -> str:
    """不补造绑定、不回写历史，只返回协议与实时证据均可信的事实投影。"""
    # 防止最终 UPDATE 等锁后重取成员新版本，却沿用语句开始时的旧授权投影。
    rows = await connection.fetch(
        PRODUCER_REUSE_PROOF_SQL
        + " ORDER BY work.id, member.rule_id, execution.id FOR UPDATE OF work, member, execution",
        task_id,
    )
    verified = []
    for row in rows:
        proof = json.loads(row["proof"]) if isinstance(row["proof"], str) else row["proof"]
        if producer_proof_is_valid(proof):
            verified.append(proof)
    return json.dumps(verified, ensure_ascii=False)


def page_input_proof_is_valid(proof: dict[str, Any], *, packet_runs: tuple[str, ...]) -> bool:
    """核验页面原输入闭包，发布和结果复用共享同一规则。"""
    try:
        stored = proof["input_set"]
        inputs = EvidenceInputSet.from_dict(stored["input_set_payload"])
        payload = inputs.to_dict()
        if inputs.scope != EvidenceInputScope.PAGE or len(proof["evidence"]) != 1:
            return False
        if any(
            stored[key] != value
            for key, value in payload.items()
            if key not in {"members", "sealed_at"}
        ):
            return False
        evidence = proof["evidence"][0]
        expected = inputs.members[0]
        binding = evidence["binding"]
        if binding["input_set_id"] != inputs.input_set_id or binding["task_id"] != inputs.task_id:
            return False
        if any(binding[key] != value for key, value in expected.to_dict().items()):
            return False
        capture = evidence["capture"]
        original, latest = (_manifest(evidence[key]) for key in ("original", "latest"))
        if len(evidence["artifacts"]) != len(original.artifacts) or any(
            row["id"] != artifact.artifact_id
            or row["task_id"] != inputs.task_id
            or row["page_capture_id"] != expected.capture_id
            or row["lifecycle_status"] not in {"active", "archived"}
            or any(
                row[key] != getattr(artifact, key)
                for key in (
                    "artifact_type",
                    "storage_path",
                    "checksum",
                    "size_bytes",
                    "content_type",
                )
            )
            for row, artifact in zip(evidence["artifacts"], original.artifacts, strict=True)
        ):
            return False
        if len(evidence["packets"]) != len(expected.observation_packet_ids) or any(
            row["id"] != packet_id
            or row["task_id"] != inputs.task_id
            or row["page_capture_id"] != expected.capture_id
            or row["rule_id"] not in {None, inputs.rule_id}
            or row["created_by_agent_run_id"] not in packet_runs
            or row["quality_gate_status"] != "pass"
            or not row["evidence_artifact_ids"]
            or not set(row["evidence_artifact_ids"]).issubset(expected.artifact_ids)
            for row, packet_id in zip(
                evidence["packets"], expected.observation_packet_ids, strict=True
            )
        ):
            return False
        return (
            capture["task_id"] == inputs.task_id
            and capture["agent_run_id"] == inputs.source_agent_run_id
            and capture["id"] == expected.capture_id
            and capture["capture_status"] == "captured"
            and capture["capture_role"] == expected.capture_role
            and original.task_id == inputs.task_id == latest.task_id
            and original.capture_id == expected.capture_id == latest.capture_id
            and original.session_id == capture["surface_session_id"] == latest.session_id
            and original.manifest_id == expected.manifest_id
            and original.manifest_version == expected.manifest_version
            and original.manifest_hash == expected.manifest_hash
            and tuple(item.artifact_id for item in original.artifacts) == expected.artifact_ids
            and original.observation_packet_ids == expected.observation_packet_ids
            and original.artifacts == latest.artifacts
            and original.observation_packet_ids == latest.observation_packet_ids
            and all(item.role == expected.capture_role for item in original.artifacts)
        )
    except (KeyError, TypeError, ValueError):
        return False


async def read_verified_page_input(
    connection: Any, *, input_set_id: str, packet_runs: tuple[str, ...]
) -> EvidenceInputSet | None:
    """精确读取已有原输入；不按相似页面生成或补造历史输入。"""
    row = await connection.fetchrow(
        "SELECT jsonb_build_object('input_set',to_jsonb(inputs),"
        "'evidence',evidence.items) AS proof "
        "FROM qc_evidence_input_set inputs JOIN LATERAL ("
        + PAGE_INPUT_EVIDENCE_SQL
        + ") evidence ON TRUE WHERE inputs.input_set_id=$1",
        input_set_id,
    )
    if row is None:
        return None
    proof = json.loads(row["proof"]) if isinstance(row["proof"], str) else row["proof"]
    if not page_input_proof_is_valid(proof, packet_runs=packet_runs):
        return None
    return EvidenceInputSet.from_dict(proof["input_set"]["input_set_payload"])


def producer_proof_is_valid(proof: dict[str, Any]) -> bool:
    """复用必须为相同原输入，合法的待复核结论保持原样。"""
    try:
        work, member, execution = (proof[key] for key in ("work", "member", "execution"))
        runs = tuple(
            run
            for run in (
                work["producer_agent_run_id"],
                work["consumer_agent_run_id"],
            )
            if run
        )
        if not page_input_proof_is_valid(proof, packet_runs=runs):
            return False
        inputs = EvidenceInputSet.from_dict(proof["input_set"]["input_set_payload"])
        return (
            inputs.task_id == work["task_id"] == execution["task_id"]
            and inputs.source_agent_run_id == work["producer_agent_run_id"]
            and execution["agent_run_id"] in runs
            and inputs.rule_id == member["rule_id"] == execution["rule_id"]
            and inputs.rule_code == member["rule_code"]
            and inputs.rule_contract_hash == member["contract_hash"]
            and inputs.ruleset_snapshot_hash == work["ruleset_snapshot_hash"]
            and inputs.finalization_epoch == work["finalization_epoch"]
            and inputs.input_set_id == member["evidence_input_set_id"]
            and inputs.input_set_id == execution["evidence_input_set_id"]
            and inputs.eligible_capture_ids == (work["page_capture_id"],)
            and execution["page_capture_id"] == work["page_capture_id"]
            and execution["execution_status"]
            in {
                "pass",
                "fail",
                "warning",
                "manual_review",
                "skipped",
                "error",
                "evidence_insufficient",
            }
            and (
                execution["observation_packet_id"] is None
                or execution["observation_packet_id"] in inputs.members[0].observation_packet_ids
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def _manifest(row: dict[str, Any]) -> EvidenceManifestReference:
    manifest = EvidenceManifestReference.from_dict(row["manifest_payload"])
    if manifest.quality_status != EvidenceQualityStatus.PASS or any(
        row[key] != manifest.to_dict()[key]
        for key in (
            "manifest_id",
            "manifest_version",
            "manifest_hash",
            "task_id",
            "capture_id",
            "session_id",
            "quality_status",
            "protocol_version",
        )
    ):
        raise ValueError("原始或最新 Manifest 质量与身份不可信")
    return manifest
