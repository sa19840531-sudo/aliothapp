"""受信证据闭集的原件读取、保守派生与原子打包，供各质检读侧复用。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, UnidentifiedImageError

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 1000
ORIGINAL_POLICY = "evidence-original.v1"
REDACTION_POLICY = "evidence-conservative-mask.v1"
READ_TIMEOUT_SECONDS = 10
_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/json": ".json",
    "text/html": ".html",
    "text/plain": ".txt",
}


class EvidenceArchiveError(ValueError):
    """只暴露固定错误码与受信材料身份，禁止把存储路径或异常原文外发。"""

    def __init__(
        self, code: str, message: str, *, artifact_id: str | None = None, status_code: int = 409
    ):
        self.code = code
        self.artifact_id = artifact_id
        self.status_code = status_code
        suffix = f"，材料 {artifact_id}" if artifact_id else ""
        super().__init__(f"{message}（{code}{suffix}）")


@dataclass(frozen=True)
class EvidenceArchive:
    """完整生成后才可审计和交付的内容及材料追溯收据。"""

    content: bytes
    materials: tuple[dict[str, Any], ...]
    checksum: str
    policy_version: str


def checksum(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def policy_version(mode: str, *, reviewable=False) -> str:
    if mode not in {"original", "redacted"}:
        raise EvidenceArchiveError("EXPORT_MODE_INVALID", "导出模式无效")
    if reviewable:
        from services.api.app.domain.evidence.redaction import POLICY_VERSION

        if mode != "redacted":
            raise EvidenceArchiveError("EXPORT_MODE_INVALID", "局部脱敏须使用脱敏模式")
        return POLICY_VERSION
    return ORIGINAL_POLICY if mode == "original" else REDACTION_POLICY


async def build_evidence_archive(
    *, entries, receipt, reader, mode="original", read_timeout=READ_TIMEOUT_SECONDS,
    redactions=None,
) -> EvidenceArchive:
    """全部原件和派生件验证成功才创建可交付 ZIP；不写原件或证据台账。"""
    policy = policy_version(mode, reviewable=redactions is not None)
    if not entries or len(entries) > MAX_ARCHIVE_MEMBERS:
        raise EvidenceArchiveError("EXPORT_MEMBER_LIMIT", "没有绑定材料或材料数量超过上限")
    if any(type(item["size_bytes"]) is not int or item["size_bytes"] < 0 for _, item, _ in entries):
        raise EvidenceArchiveError("ARTIFACT_SIZE_INVALID", "材料大小无效")
    if sum(item["size_bytes"] for _, item, _ in entries) > MAX_ARCHIVE_BYTES:
        raise EvidenceArchiveError("EXPORT_BYTE_LIMIT", "整组材料超过 64 MiB 下载上限")
    if redactions is not None and set(redactions) != {
        item["artifact_id"] for _, item, _ in entries
    }:
        raise EvidenceArchiveError("REDACTION_MEMBERS_MISMATCH", "每份材料须明确选择同源脱敏版本")
    if redactions is not None:
        for _, artifact, context in entries:
            _validate_local_binding(artifact, context, receipt, redactions[artifact["artifact_id"]])
    materials, contents = [], []
    total = 0
    for stem, artifact, context in entries:
        artifact_id = artifact["artifact_id"]
        try:
            file = await asyncio.wait_for(
                reader.evidence_file(artifact_id, original=True),
                timeout=read_timeout,
            )
        except FileNotFoundError as exc:
            raise EvidenceArchiveError(
                "ARTIFACT_MISSING", "证据原件缺失", artifact_id=artifact_id, status_code=404
            ) from exc
        except TimeoutError as exc:
            raise EvidenceArchiveError(
                "ARTIFACT_READ_TIMEOUT",
                "证据存储读取超时",
                artifact_id=artifact_id,
                status_code=503,
            ) from exc
        except ValueError as exc:
            raise EvidenceArchiveError(
                "ARTIFACT_INTEGRITY_MISMATCH", "证据原件校验失败", artifact_id=artifact_id
            ) from exc
        except Exception as exc:
            raise EvidenceArchiveError(
                "ARTIFACT_STORAGE_UNAVAILABLE",
                "证据存储暂不可用",
                artifact_id=artifact_id,
                status_code=503,
            ) from exc
        if file is None:
            raise EvidenceArchiveError(
                "ARTIFACT_MISSING", "证据原件缺失", artifact_id=artifact_id, status_code=404
            )
        content = file.get("content")
        if (
            not isinstance(content, bytes)
            or len(content) != artifact["size_bytes"]
            or checksum(content) != artifact["checksum"]
        ):
            raise EvidenceArchiveError(
                "ARTIFACT_INTEGRITY_MISMATCH", "证据原件校验失败", artifact_id=artifact_id
            )
        media = (artifact.get("content_type") or "").split(";", 1)[0].strip().lower()
        strategy = "unchanged_original"
        redaction_metadata = {}
        if redactions is not None:
            content, media, strategy, redaction_metadata = await _local_derivative(
                content, artifact, context, receipt, redactions[artifact_id],
            )
        elif mode == "redacted":
            content, media, strategy = _masked_derivative(content, media, artifact_id)
        total += len(content)
        if total > MAX_ARCHIVE_BYTES:
            raise EvidenceArchiveError("EXPORT_BYTE_LIMIT", "派生材料超过下载上限")
        name = stem + _EXTENSIONS.get(media, ".bin")
        materials.append(
            {
                "path": name,
                "artifact_id": artifact_id,
                "artifact_type": artifact["artifact_type"],
                **context,
                "original_checksum": artifact["checksum"],
                "original_size_bytes": artifact["size_bytes"],
                "checksum": checksum(content),
                "size_bytes": len(content),
                "content_type": media or "application/octet-stream",
                "representation": mode,
                "policy_version": policy,
                "redaction_strategy": strategy,
                "business_reviewable": mode == "original" or redactions is not None,
                **redaction_metadata,
            }
        )
        contents.append((name, content))
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, content in contents:
            archive.writestr(name, content)
        archive.writestr(
            "receipt.json",
            json.dumps(
                {
                    **receipt,
                    "schema_version": (
                        "evidence-group-export.v3" if redactions is not None
                        else "evidence-group-export.v2"
                    ),
                    "mode": mode,
                    "policy_version": policy,
                    "business_reviewable": mode == "original" or redactions is not None,
                    "materials": materials,
                    "notice": (
                        "原件台账导出，不代表规则或报告通过"
                        if mode == "original"
                        else "人工核验范围内的业务复核副本；遮盖处不可判读，不代表规则通过"
                        if redactions is not None
                        else "保守遮盖副本仅供结构与来源追溯，未核验局部隐私区域，不可据此判定业务"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    content = output.getvalue()
    return EvidenceArchive(content, tuple(materials), checksum(content), policy)


async def _local_derivative(content, artifact, context, receipt, plan):
    from services.api.app.application.evidence_redactions import render_frozen_plan
    from services.api.app.domain.evidence.redaction import RedactionError

    try:
        rendered = await asyncio.to_thread(render_frozen_plan, content, plan)
    except RedactionError as exc:
        raise EvidenceArchiveError(
            exc.code, "冻结脱敏版本无法重建", artifact_id=artifact["artifact_id"],
            status_code=exc.status_code,
        ) from exc
    return (
        rendered.content, rendered.content_type.split(";", 1)[0], rendered.strategy,
        {"redaction_id": plan["redaction_id"], "redaction_plan_hash": plan["plan_hash"],
         "source_hash": plan["source"]["source_hash"]},
    )


def _validate_local_binding(artifact, context, receipt, plan):
    from services.api.app.application.evidence_redactions import validate_plan
    from services.api.app.domain.evidence.redaction import RedactionError

    try:
        validate_plan(plan)
    except RedactionError as exc:
        raise EvidenceArchiveError(
            exc.code, "冻结脱敏版本无效", artifact_id=artifact["artifact_id"],
        ) from exc
    source = plan["source"]
    expected = {
        "artifact_id": artifact["artifact_id"], "original_checksum": artifact["checksum"],
        "original_size_bytes": artifact["size_bytes"],
        "original_content_type": artifact.get("content_type") or "application/octet-stream",
        **{key: context.get(key) for key in (
            "session_id", "capture_id", "manifest_id", "manifest_version",
        )},
        **{key: receipt[key] for key in ("task_id", "agent_run_id") if key in receipt},
    }
    if any(source.get(key) != value for key, value in expected.items()):
        raise EvidenceArchiveError(
            "REDACTION_SOURCE_MISMATCH", "脱敏版本不属于当前原件或清单",
            artifact_id=artifact["artifact_id"],
        )


def _masked_derivative(content: bytes, media: str, artifact_id: str):
    """缺少已核验局部遮盖范围时，显式移除全部原文，绝不声称可供业务判读。"""
    try:
        if media in {"image/png", "image/jpeg", "image/webp"}:
            with Image.open(BytesIO(content)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("图片实际格式不受支持")
                if source.width * source.height > 40_000_000:
                    raise ValueError("图片像素数量超限")
                source.load()
                masked = Image.new("RGB", source.size, color=(32, 32, 32))
                output = BytesIO()
                masked.save(output, format="PNG")
            return output.getvalue(), "image/png", "full_frame_masked"
        if media == "application/json":
            value = json.loads(content.decode("utf-8"))
            masked = _mask_json(value)
            return (
                json.dumps(masked, ensure_ascii=False).encode("utf-8"),
                media,
                "all_json_keys_and_values_masked",
            )
        if media in {"text/plain", "text/html"}:
            content.decode("utf-8")
            if media == "text/html":
                return (
                    b'<!doctype html><meta charset="utf-8"><p>[content masked]</p>',
                    media,
                    "full_document_masked",
                )
            return b"[content masked]\n", media, "full_text_masked"
    except (
        ValueError,
        UnicodeError,
        RecursionError,
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
    ) as exc:
        raise EvidenceArchiveError(
            "REDACTION_SOURCE_INVALID", "材料无法按已注册策略生成遮盖副本", artifact_id=artifact_id
        ) from exc
    raise EvidenceArchiveError(
        "REDACTION_FORMAT_UNSUPPORTED", "材料格式没有已注册的遮盖策略", artifact_id=artifact_id
    )


def _mask_json(value, depth=0):
    if depth > 32:
        raise ValueError("JSON层级超限")
    if isinstance(value, dict):
        return {
            f"field_{index + 1}": _mask_json(item, depth + 1)
            for index, item in enumerate(value.values())
        }
    if isinstance(value, list):
        return [_mask_json(item, depth + 1) for item in value]
    return "[masked]"
