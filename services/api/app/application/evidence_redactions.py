"""平台脱敏表示：冻结来源与操作，重建时核验派生摘要，不修改证据事实。"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.api.app.application.evidence_redaction_render import (
    POLICY_VERSION,
    render_redaction,
)
from services.api.app.domain.evidence.redaction import RedactionError, RedactionSpec
from services.api.app.domain.protocol import stable_protocol_hash

MAX_SOURCE_BYTES = 16 * 1024 * 1024


class RedactionRequest(BaseModel):
    """调用方只声明预期原件和局部范围，证据归属始终由服务端确认。"""

    model_config = ConfigDict(extra="forbid")
    source_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    manifest_id: str | None = Field(default=None, max_length=200)
    manifest_version: int | None = Field(default=None, strict=True, ge=1)
    spec: RedactionSpec
    review_confirmed: bool = Field(default=False, strict=True)


def parse_request(value):
    try:
        request = RedactionRequest.model_validate(value)
        if (request.manifest_id is None) != (request.manifest_version is None):
            raise ValueError("Manifest身份与版本须同时提供")
        return request
    except (ValidationError, ValueError) as exc:
        raise RedactionError(
            "REDACTION_REQUEST_INVALID", "脱敏范围或来源声明无效", status_code=422,
        ) from exc


async def source_for(repository, artifact_id, *, manifest_id=None, manifest_version=None):
    try:
        source = await repository.source(
            artifact_id=artifact_id, manifest_id=manifest_id, manifest_version=manifest_version,
        )
    except ValueError as exc:
        raise RedactionError("REDACTION_SOURCE_CHANGED", "证据身份或清单成员已变化") from exc
    if source is None:
        raise RedactionError("REDACTION_SOURCE_NOT_FOUND", "材料不存在或不可访问", status_code=404)
    return source


async def read_original(reader, source):
    size = source["original_size_bytes"]
    if type(size) is not int or size < 1 or size > MAX_SOURCE_BYTES:
        raise RedactionError("REDACTION_BYTE_LIMIT", "单份脱敏原件须在16 MiB以内")
    try:
        file = await asyncio.wait_for(
            reader.evidence_file(source["artifact_id"], original=True), timeout=10,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise RedactionError("REDACTION_SOURCE_INVALID", "证据原件缺失或摘要不符") from exc
    except TimeoutError as exc:
        raise RedactionError(
            "REDACTION_STORAGE_TIMEOUT", "证据存储读取超时", status_code=503,
        ) from exc
    if not file or not isinstance(file.get("content"), bytes):
        raise RedactionError("REDACTION_SOURCE_INVALID", "证据原件缺失或摘要不符")
    from services.api.app.application.evidence_archive import checksum

    content = file["content"]
    if len(content) != size or checksum(content) != source["original_checksum"]:
        raise RedactionError("REDACTION_SOURCE_INVALID", "证据原件大小或摘要不符")
    return content


def plan_digest(plan):
    """只散列已定义的冻结事实；创建时间、操作者和数据库主键不改变策略身份。"""
    return stable_protocol_hash({
        key: plan[key] for key in (
            "schema_version", "source", "spec", "policy_version", "derived_checksum",
            "derived_size_bytes", "derived_content_type", "business_reviewable",
        )
    })


async def prepare_plan(*, repository, reader, artifact_id, request, actor, tenant_id, source=None):
    if source is None:
        source = await source_for(
            repository, artifact_id, manifest_id=request.manifest_id,
            manifest_version=request.manifest_version,
        )
    if source["original_checksum"] != request.source_checksum:
        raise RedactionError("REDACTION_SOURCE_CHANGED", "原件版本已变化，请重新查看")
    rendered = await asyncio.to_thread(
        render_redaction, await read_original(reader, source),
        source["original_content_type"], request.spec,
    )
    if rendered.size_bytes > MAX_SOURCE_BYTES:
        raise RedactionError("REDACTION_BYTE_LIMIT", "派生材料超过16 MiB上限")
    plan = {
        "redaction_id": str(uuid4()), "schema_version": "evidence-redaction.v1",
        "artifact_id": artifact_id, "source": source,
        "spec": request.spec.model_dump(mode="json", exclude_none=True),
        "policy_version": POLICY_VERSION, "derived_checksum": rendered.checksum,
        "derived_size_bytes": rendered.size_bytes,
        "derived_content_type": rendered.content_type, "business_reviewable": True,
        "reviewed_by": actor, "tenant_id": tenant_id,
        "created_at": datetime.now(UTC).isoformat(),
    }
    plan["plan_hash"] = plan_digest(plan)
    return plan, rendered


async def validate_source(repository, plan):
    source = plan["source"]
    current = await source_for(
        repository, source["artifact_id"], manifest_id=source.get("manifest_id"),
        manifest_version=source.get("manifest_version"),
    )
    if current != source:
        raise RedactionError("REDACTION_SOURCE_CHANGED", "脱敏版本与当前原件身份不一致")


async def rebuild_plan(*, repository, reader, plan):
    validate_plan(plan)
    await validate_source(repository, plan)
    return await asyncio.to_thread(
        render_frozen_plan, await read_original(reader, plan["source"]), plan,
    )


def render_frozen_plan(content, plan):
    """读取与整组交付共享同一冻结策略校验，不能用新参数重算旧表示。"""
    validate_plan(plan)
    rendered = render_redaction(
        content,
        plan["source"]["original_content_type"], RedactionSpec.model_validate(plan["spec"]),
    )
    if (
        rendered.checksum != plan["derived_checksum"]
        or rendered.size_bytes != plan["derived_size_bytes"]
        or rendered.content_type != plan["derived_content_type"]
    ):
        raise RedactionError("REDACTION_DERIVATIVE_CHANGED", "派生材料与已核验版本不一致")
    return rendered


def validate_plan(plan):
    """损坏或未知策略在读取原件前拒绝，固定原因不外发内部字段。"""
    try:
        valid = (
            plan["policy_version"] == POLICY_VERSION
            and plan["schema_version"] == "evidence-redaction.v1"
            and plan["business_reviewable"] is True
            and plan["artifact_id"] == plan["source"]["artifact_id"]
            and plan["plan_hash"] == plan_digest(plan)
        )
        RedactionSpec.model_validate(plan["spec"])
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise RedactionError("REDACTION_PLAN_INVALID", "脱敏版本策略或摘要不一致")


def public_plan(plan):
    """复核方不接收局部指令、原站文本、存储位置或未允许的计划字段。"""
    result = {key: plan[key] for key in (
        "redaction_id", "artifact_id", "policy_version", "plan_hash", "derived_checksum",
        "derived_size_bytes", "derived_content_type", "business_reviewable", "reviewed_by",
        "created_at",
    )}
    result["source"] = {key: plan["source"].get(key) for key in (
        "artifact_id", "task_id", "agent_run_id", "session_id", "capture_id", "manifest_id",
        "manifest_version", "manifest_hash", "source_hash", "original_checksum",
        "original_size_bytes", "original_content_type", "artifact_type",
    )}
    return result
