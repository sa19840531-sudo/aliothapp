"""绿色展示材料的精确清单、归属和原件读取，不推导业务结论。"""

import hashlib
from io import BytesIO

from PIL import Image

from services.api.app.application.green_rule_result_projection import object_value
from services.api.app.domain.evidence import EvidenceInputSet, EvidenceManifestReference
from services.api.app.infrastructure.db.green_process_evidence import (
    _MAX_ARTIFACT_BYTES,
    _MAX_CAPTURE_COUNT,
    _MAX_TOTAL_BYTES,
    _owned_path,
)


async def read_green_result_materials(
    connection, *, task, row, validated, artifact_store, verified_originals=None
):
    packet = object_value(row["packet_payload"])
    references = []
    if row.get("input_set_payload"):
        inputs = EvidenceInputSet.from_dict(object_value(row["input_set_payload"]))
        references = [
            (item.capture_id, item.manifest_id, item.manifest_version, item.manifest_hash)
            for item in inputs.members
        ]
    else:
        payload = object_value(packet["packet_payload"])
        quality = object_value(payload["quality_gate"])
        if quality.get("source") != "evidence_manifest":
            raise ValueError("页面Packet没有权威清单门禁")
        facts = object_value(payload.get("facts", {})).get("capture_roles", {})
        for capture_id in validated["capture_ids"]:
            if capture_id == packet["page_capture_id"]:
                references.append(
                    (capture_id, quality["manifest_id"], quality["manifest_version"], None)
                )
            else:
                roles = [
                    fact for fact in facts.values() if fact.get("page_capture_id") == capture_id
                ]
                if len(roles) != 1 or not roles[0].get("manifest_id"):
                    raise ValueError("非主角色缺少正式清单绑定")
                references.append((capture_id, roles[0]["manifest_id"], None, None))
    if len(references) > _MAX_CAPTURE_COUNT:
        raise ValueError("超过已有只读输入容量")
    result, total = {}, 0
    allowed = set(validated["allowed_artifact_ids"])
    for capture_id, manifest_id, version, manifest_hash in references:
        source = await connection.fetchrow(
            """SELECT capture.id::text,capture.task_id::text,capture.agent_run_id::text,
                      capture.capture_status,capture.metadata,capture.surface_session_id,
                      session.task_id::text AS session_task_id,
                      session.finalization_epoch AS session_epoch,
                      frozen.manifest_payload,latest.manifest_payload AS latest_payload
               FROM qc_capture capture
               JOIN qc_surface_session session ON session.session_id=capture.surface_session_id
               JOIN qc_evidence_manifest frozen ON frozen.capture_id=capture.id
                 AND frozen.manifest_id=$2
               LEFT JOIN LATERAL (SELECT manifest_payload FROM qc_evidence_manifest
                 WHERE capture_id=capture.id ORDER BY manifest_version DESC LIMIT 1) latest ON true
               WHERE capture.id=$1::uuid AND capture.task_id=$3::uuid""",
            capture_id,
            manifest_id,
            task["task_id"],
        )
        if source is None:
            raise ValueError("正式采集或清单不存在")
        manifest = EvidenceManifestReference.from_dict(object_value(source["manifest_payload"]))
        if (
            source["agent_run_id"] != task["source_run_id"]
            or source["capture_status"] != "captured"
            or source["session_task_id"] != task["task_id"]
            or source["session_epoch"] != task["read_epoch"]
            or object_value(source["metadata"]).get("finalization_epoch") != task["read_epoch"]
            or manifest.task_id != task["task_id"]
            or manifest.capture_id != capture_id
            or manifest.session_id != source["surface_session_id"]
            or manifest.quality_status.value != "pass"
            or (version is not None and manifest.manifest_version != version)
            or (manifest_hash is not None and manifest.manifest_hash != manifest_hash)
            or EvidenceManifestReference.from_dict(object_value(source["latest_payload"]))
            != manifest
        ):
            raise ValueError("原件运行/周期/会话或最新清单质量已变化")
        for reference in manifest.artifacts:
            if reference.artifact_id not in allowed:
                continue
            artifact = await connection.fetchrow(
                "SELECT * FROM qc_evidence_artifact WHERE id=$1::uuid "
                "AND task_id=$2::uuid AND page_capture_id=$3::uuid",
                reference.artifact_id,
                task["task_id"],
                capture_id,
            )
            if (
                artifact is None
                or artifact["source"] not in {"baseline", "rule_supplement", "app_runtime"}
                or (
                    artifact["lifecycle_status"] not in {"active", "archived"}
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
                        task_id=task["task_id"],
                        capture_id=capture_id,
                        artifact_id=reference.artifact_id,
                    )
                )
            ):
                raise ValueError("正式Artifact身份不一致")
            size = reference.size_bytes
            if (
                type(size) is not int
                or not 0 <= size <= _MAX_ARTIFACT_BYTES
                or total + size > _MAX_TOTAL_BYTES
            ):
                raise ValueError("超过已有只读原件容量")
            total += size
            png, screenshot = False, reference.artifact_type == "screenshot"
            try:
                content = (
                    verified_originals[reference.artifact_id]
                    if verified_originals is not None
                    else await artifact_store.read(reference.storage_path)
                )
                intact = (
                    isinstance(content, bytes)
                    and len(content) == size
                    and ("sha256:" + hashlib.sha256(content).hexdigest() == reference.checksum)
                )
                png = (
                    intact
                    and reference.content_type == "image/png"
                    and content.startswith(b"\x89PNG\r\n\x1a\n")
                )
                if png:
                    try:
                        with Image.open(BytesIO(content)) as image:
                            png = image.format == "PNG" and min(image.size) > 0
                            image.verify()
                    except (OSError, ValueError, Image.DecompressionBombError):
                        png = False
                if not screenshot and not intact:
                    raise ValueError("判据原件字节与正式清单摘要不一致")
            except (OSError, LookupError):
                if not screenshot:
                    raise ValueError("判据原件缺失") from None
            result[reference.artifact_id] = {
                "artifact_id": reference.artifact_id,
                "artifact_type": reference.artifact_type,
                "capture_id": capture_id,
                "png_verified": bool(png),
            }
    if set(result) != allowed:
        raise ValueError("正式输入材料未全部关联")
    return list(result.values())
