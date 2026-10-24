"""复用任务对象权限和证据原文读服务的短流程读侧。"""

import asyncio
from functools import lru_cache
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from services.api.app.api.v1.auth import (
    _build_user_response,
    _tenant_disabled,
    require_access_scope,
)
from services.api.app.api.v1.evidence import (
    _get_download_audit_repository,
    _get_task_insight_service,
    _get_ticket_subject_repository,
)
from services.api.app.api.v1.object_access import (
    _get_object_access_repository,
    require_task_read_access,
)
from services.api.app.api.v2.evidence_redactions import _get_redaction_repository
from services.api.app.application.evidence_archive import EvidenceArchiveError, policy_version
from services.api.app.application.evidence_redactions import validate_plan, validate_source
from services.api.app.application.h5_shortflow_read import (
    prepare_shortflow_archive,
    shortflow_export_plan,
)
from services.api.app.core.errors import AliothError
from services.api.app.domain.evidence.redaction import RedactionError
from services.api.app.domain.iam.access_scope import AccessScope, access_scope_from_user
from services.api.app.infrastructure.db.h5_shortflow_read_repository import (
    PostgresH5ShortflowReadRepository,
)

router = APIRouter()


@lru_cache
def _get_repository():
    return PostgresH5ShortflowReadRepository()


@router.get("/h5-shortflows/catalog")
async def catalog(repository: Annotated[object, Depends(_get_repository)]):
    return await repository.catalog()


async def _read(repository, task_id, scope):
    try:
        result = await repository.read(task_id=str(task_id), access_scope=scope)
    except ValueError as exc:
        raise AliothError(message="短流程台账身份不一致，请核对原始证据", status_code=409) from exc
    if result is None:
        raise AliothError(message="H5 任务不存在或不可访问", status_code=404)
    return result


@router.get("/tasks/{task_id}/h5-shortflows")
async def get_shortflows(
    task_id: UUID,
    repository: Annotated[object, Depends(_get_repository)],
    scope: Annotated[AccessScope, Depends(require_task_read_access)],
):
    return await _read(repository, task_id, scope)


@router.get("/tasks/{task_id}/h5-shortflows/{execution_plan_id}/archive")
async def archive(
    task_id: UUID,
    execution_plan_id: str,
    repository: Annotated[object, Depends(_get_repository)],
    reader: Annotated[object, Depends(_get_task_insight_service)],
    access: Annotated[object, Depends(_get_object_access_repository)],
    audit: Annotated[object, Depends(_get_download_audit_repository)],
    users: Annotated[object, Depends(_get_ticket_subject_repository)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    snapshot_hash: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    mode: Literal["original", "redacted"] = "original",
):
    return await _archive_response(
        task_id=task_id, execution_plan_id=execution_plan_id, repository=repository,
        reader=reader, access=access, audit=audit, users=users, scope=scope,
        snapshot_hash=snapshot_hash, mode=mode,
    )


class ReviewableArchiveRequest(BaseModel):
    """明确选择每份不可变脱敏版本，不接受临时遮盖参数。"""

    model_config = ConfigDict(extra="forbid")
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    redaction_ids: dict[str, str] = Field(max_length=1000)


@router.post("/tasks/{task_id}/h5-shortflows/{execution_plan_id}/archive")
async def reviewable_archive(
    task_id: UUID, execution_plan_id: str, body: ReviewableArchiveRequest,
    repository: Annotated[object, Depends(_get_repository)],
    reader: Annotated[object, Depends(_get_task_insight_service)],
    access: Annotated[object, Depends(_get_object_access_repository)],
    audit: Annotated[object, Depends(_get_download_audit_repository)],
    users: Annotated[object, Depends(_get_ticket_subject_repository)],
    redactions: Annotated[object, Depends(_get_redaction_repository)],
    scope: Annotated[AccessScope, Depends(require_access_scope)],
):
    return await _archive_response(
        task_id=task_id, execution_plan_id=execution_plan_id, repository=repository,
        reader=reader, access=access, audit=audit, users=users, scope=scope,
        snapshot_hash=body.snapshot_hash, mode="redacted", redaction_ids=body.redaction_ids,
        redaction_repository=redactions,
    )


async def _archive_response(
    *, task_id, execution_plan_id, repository, reader, access, audit, users, scope,
    snapshot_hash, mode, redaction_ids=None, redaction_repository=None,
):
    request_id = str(uuid4())
    task_id = str(task_id)
    artifact_ids, requested_materials = set(), []
    result, failure, prepared = "failed", None, None
    frozen_redactions = None
    try:
        if not await access.can_access_task(task_id=task_id, scope=scope):
            result = "denied"
            raise EvidenceArchiveError(
                "OBJECT_ACCESS_DENIED", "H5 任务不存在或不可访问", status_code=404
            )
        if f"evidenceExports:{mode}" not in scope.permissions:
            result = "denied"
            raise EvidenceArchiveError(
                "EXPORT_MODE_FORBIDDEN", "没有当前模式的证据导出权限", status_code=403
            )
        snapshot = await _read(repository, task_id, scope)
        plan = shortflow_export_plan(snapshot, execution_plan_id)
        requested_materials = [
            {"artifact_id": artifact["artifact_id"], "original_checksum": artifact["checksum"]}
            for checkpoint in plan["checkpoints"]
            for attempt in checkpoint["attempts"]
            for binding in attempt["bindings"]
            for artifact in binding["artifacts"]
        ]
        artifact_ids = {item["artifact_id"] for item in requested_materials}
        if snapshot["snapshot_hash"] != snapshot_hash:
            raise EvidenceArchiveError("EXPORT_SNAPSHOT_CHANGED", "流程材料已变化，请刷新后导出")
        for artifact_id in sorted(artifact_ids):
            if not await access.can_access_artifact(artifact_id=artifact_id, scope=scope):
                result = "denied"
                raise EvidenceArchiveError(
                    "ARTIFACT_ACCESS_DENIED",
                    "材料不存在或不可访问",
                    artifact_id=artifact_id,
                    status_code=404,
                )
        if redaction_ids is not None:
            if set(redaction_ids) != artifact_ids:
                raise EvidenceArchiveError(
                    "REDACTION_MEMBERS_MISMATCH", "每份材料须明确选择同源脱敏版本",
                )
            frozen_redactions = {}
            for artifact_id, redaction_id in redaction_ids.items():
                frozen = await redaction_repository.get(redaction_id=redaction_id)
                if frozen is None or frozen.get("artifact_id") != artifact_id:
                    raise EvidenceArchiveError(
                        "REDACTION_PLAN_NOT_FOUND", "所选脱敏版本不属于当前材料",
                        artifact_id=artifact_id,
                    )
                for material in requested_materials:
                    if material["artifact_id"] == artifact_id:
                        material.update(
                            redaction_id=frozen.get("redaction_id", redaction_id),
                            redaction_plan_hash=frozen.get("plan_hash"),
                        )
                frozen_redactions[artifact_id] = frozen
            for frozen in frozen_redactions.values():
                validate_plan(frozen)
                await validate_source(redaction_repository, frozen)
        prepared = await asyncio.wait_for(
            prepare_shortflow_archive(
                snapshot, execution_plan_id, reader, mode=mode, request_id=request_id,
                redactions=frozen_redactions,
            ),
            timeout=60,
        )
        # 交付前重新加载主体与对象范围；读请求开始时的权限不能覆盖打包期间的撤权。
        row = await users.find_by_id(scope.actor_user_id)
        if (
            row is None
            or row.get("status") != "enabled"
            or _tenant_disabled(row)
            or str(row.get("tenant_id") or "") != scope.tenant_id
        ):
            result = "denied"
            raise EvidenceArchiveError(
                "SUBJECT_REVOKED", "H5 任务不存在或不可访问", status_code=404
            )
        current = access_scope_from_user(_build_user_response(row))
        if f"evidenceExports:{mode}" not in current.permissions:
            result = "denied"
            raise EvidenceArchiveError("EXPORT_MODE_REVOKED", "证据导出权限已收回", status_code=403)
        if not await access.can_access_task(task_id=task_id, scope=current):
            result = "denied"
            raise EvidenceArchiveError(
                "OBJECT_ACCESS_REVOKED", "H5 任务不存在或不可访问", status_code=404
            )
        for artifact_id in sorted(artifact_ids):
            if not await access.can_access_artifact(artifact_id=artifact_id, scope=current):
                result = "denied"
                raise EvidenceArchiveError(
                    "ARTIFACT_ACCESS_REVOKED",
                    "材料权限或生命周期已变化",
                    artifact_id=artifact_id,
                    status_code=404,
                )
        latest = await _read(repository, task_id, current)
        if latest["snapshot_hash"] != snapshot_hash:
            raise EvidenceArchiveError(
                "EXPORT_SNAPSHOT_CHANGED", "打包期间材料已变化，请刷新后导出"
            )
        for frozen in (frozen_redactions or {}).values():
            await validate_source(redaction_repository, frozen)
        result = "allowed"
    except EvidenceArchiveError as exc:
        failure = exc
    except RedactionError as exc:
        failure = EvidenceArchiveError(exc.code, "脱敏版本来源已变化", status_code=exc.status_code)
    except AliothError as exc:
        failure = EvidenceArchiveError(
            "EXPORT_SNAPSHOT_UNAVAILABLE", "流程台账不可读取", status_code=exc.status_code
        )
    except TimeoutError:
        failure = EvidenceArchiveError("EXPORT_TIMEOUT", "整组材料准备超时", status_code=503)
    except Exception:
        failure = EvidenceArchiveError("EXPORT_UNAVAILABLE", "证据导出暂不可用", status_code=503)
    # 成功、拒绝与失败使用同一个请求身份，且审计先于任何内容交付落库。
    try:
        await audit.record_export(
            request_id=request_id,
            task_id=task_id,
            execution_plan_id=execution_plan_id,
            snapshot_hash=snapshot_hash,
            mode=mode,
            policy_version=policy_version(mode, reviewable=redaction_ids is not None),
            actor_user_id=scope.actor_user_id,
            tenant_id=scope.tenant_id,
            artifact_ids=artifact_ids,
            materials=prepared.materials if prepared else requested_materials,
            result=result,
            error_code=failure.code if failure else None,
            failed_artifact_id=failure.artifact_id if failure else None,
            archive_checksum=prepared.checksum if prepared else None,
        )
    except Exception as exc:
        raise AliothError(
            message="证据导出审计不可用，未交付材料（EXPORT_AUDIT_UNAVAILABLE）",
            status_code=503,
            error_id=request_id,
        ) from exc
    if failure:
        raise AliothError(
            message=str(failure), status_code=failure.status_code, error_id=request_id
        )
    return Response(
        prepared.content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="task_{task_id}_shortflow.zip"',
            "Cache-Control": "no-store",
            "X-Evidence-Export-Request-ID": request_id,
            "X-Evidence-Export-Mode": mode,
            "X-Evidence-Export-Policy": prepared.policy_version,
        },
    )
