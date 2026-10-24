"""跨端证据的局部脱敏预览、不可变版本和受控业务复核。"""

import base64
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Response

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.api.v1.evidence import (
    _download_filename,
    _get_download_audit_repository,
    _get_task_insight_service,
    _get_ticket_subject_repository,
)
from services.api.app.api.v1.object_access import _get_object_access_repository
from services.api.app.api.v2.evidence_redaction_access import authorize, can_manage, current_scope
from services.api.app.application.evidence_redactions import (
    parse_request,
    prepare_plan,
    public_plan,
    rebuild_plan,
    source_for,
    validate_plan,
    validate_source,
)
from services.api.app.core.errors import AliothError
from services.api.app.domain.evidence.redaction import RedactionError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.evidence_redaction_repository import (
    PostgresEvidenceRedactionRepository,
)

router = APIRouter()


@lru_cache
def _get_redaction_repository():
    return PostgresEvidenceRedactionRepository()


@dataclass
class RedactionServices:
    scope: AccessScope
    repository: object
    reader: object
    access: object
    audit: object
    users: object


def _services(
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[object, Depends(_get_redaction_repository)],
    reader: Annotated[object, Depends(_get_task_insight_service)],
    access: Annotated[object, Depends(_get_object_access_repository)],
    audit: Annotated[object, Depends(_get_download_audit_repository)],
    users: Annotated[object, Depends(_get_ticket_subject_repository)],
):
    return RedactionServices(scope, repository, reader, access, audit, users)


async def _operate(operation, services, *, artifact_id=None, redaction_id=None, body=None):
    scope, repository = services.scope, services.repository
    request_id, plan, source, rendered, payload = str(uuid4()), None, None, None, None
    failure, result, audited = None, "failed", False
    manage = operation in {"preview", "create"}
    try:
        if operation == "read":
            plan = await repository.get(redaction_id=redaction_id)
            if plan is None:
                raise RedactionError("REDACTION_NOT_FOUND", "材料不存在或不可访问", status_code=404)
            validate_plan(plan)
            source = plan["source"]
            artifact_id = source["artifact_id"]
        await authorize(
            scope=scope, access=services.access, artifact_id=artifact_id, manage=manage,
        )
        if manage:
            request = parse_request(body)
            if operation == "create" and not request.review_confirmed:
                raise RedactionError(
                    "REDACTION_REVIEW_REQUIRED", "请核对派生内容可供业务复核并确认敏感范围",
                    status_code=422,
                )
            source = await source_for(
                repository, artifact_id, manifest_id=request.manifest_id,
                manifest_version=request.manifest_version,
            )
            plan, rendered = await prepare_plan(
                repository=repository, reader=services.reader, artifact_id=artifact_id,
                request=request, actor=scope.actor_user_id, tenant_id=scope.tenant_id,
                source=source,
            )
            source = plan["source"]
            payload = public_plan(plan)
            if operation == "preview":
                payload["content_base64"] = base64.b64encode(rendered.content).decode("ascii")
        elif operation == "read":
            source = plan["source"]
            rendered = await rebuild_plan(repository=repository, reader=services.reader, plan=plan)
        else:
            source = await source_for(repository, artifact_id)
            items = await repository.list_for_artifact(artifact_id=artifact_id)
            payload = {"items": [public_plan(item) for item in items]}
        current = await current_scope(scope=scope, users=services.users)
        await authorize(
            scope=current, access=services.access, artifact_id=artifact_id, manage=manage,
        )
        if plan:
            await validate_source(repository, plan)
        elif await source_for(repository, artifact_id) != source:
            raise RedactionError("REDACTION_SOURCE_CHANGED", "材料身份已变化，请刷新")
        if operation == "list":
            payload["can_manage"] = can_manage(current)
        result = "allowed"
        if operation == "create":
            await repository.save(
                plan=plan, audit=services.audit,
                audit_kwargs=_audit_fields(
                    request_id, operation, scope, artifact_id, plan, source, result, None,
                ),
            )
            audited = True
    except RedactionError as exc:
        failure = exc
        result = "denied" if exc.status_code in {403, 404} else "failed"
    except Exception:
        result = "failed"
        failure = RedactionError("REDACTION_UNAVAILABLE", "证据脱敏暂不可用", status_code=503)
    if not audited:
        try:
            await services.audit.record_redaction(**_audit_fields(
                request_id, operation, scope, artifact_id, plan, source, result, failure,
                requested_id=redaction_id,
            ))
        except Exception as exc:
            raise AliothError(
                message="脱敏审计不可用，未交付材料（REDACTION_AUDIT_UNAVAILABLE）",
                status_code=503, error_id=request_id,
            ) from exc
    if failure:
        raise AliothError(
            message=str(failure), status_code=failure.status_code,
            error_id=request_id,
        )
    headers = {"Cache-Control": "no-store", "X-Evidence-Redaction-Request-ID": request_id}
    if operation == "read":
        filename = _download_filename(plan["redaction_id"], rendered.content_type)
        return Response(
            rendered.content, media_type=rendered.content_type,
            headers={**headers, "Content-Disposition": f'attachment; filename="{filename}"',
                     "X-Content-Type-Options": "nosniff"},
        )
    from fastapi.responses import JSONResponse

    return JSONResponse(payload, headers=headers)


def _audit_fields(
    request_id, operation, scope, artifact_id, plan, source, result, failure, requested_id=None,
):
    try:
        requested_id = str(UUID(str(requested_id)))
    except (ValueError, TypeError, AttributeError):
        requested_id = None
    return {
        "request_id": request_id, "operation": operation,
        "artifact_id": (
            artifact_id if artifact_id and len(artifact_id) <= 64 else "redaction:not-found"
        ),
        "actor_user_id": scope.actor_user_id, "tenant_id": scope.tenant_id,
        "result": result, "error_code": failure.code if failure else None,
        "task_id": source.get("task_id") if source else None,
        "redaction_id": plan.get("redaction_id") if plan else requested_id,
        "redaction_plan_hash": plan.get("plan_hash") if plan else None,
        "source_hash": source.get("source_hash") if source else None,
        "original_checksum": source.get("original_checksum") if source else None,
        "derived_checksum": plan.get("derived_checksum") if plan else None,
        "policy_version": plan.get("policy_version") if plan else None,
    }


@router.post("/evidence/{artifact_id}/redactions/preview")
async def preview(
    artifact_id: str, body: dict, services: Annotated[RedactionServices, Depends(_services)],
):
    return await _operate("preview", services, artifact_id=artifact_id, body=body)


@router.post("/evidence/{artifact_id}/redactions")
async def create(
    artifact_id: str, body: dict, services: Annotated[RedactionServices, Depends(_services)],
):
    return await _operate("create", services, artifact_id=artifact_id, body=body)


@router.get("/evidence/{artifact_id}/redactions")
async def list_versions(
    artifact_id: str, services: Annotated[RedactionServices, Depends(_services)],
):
    return await _operate("list", services, artifact_id=artifact_id)


@router.get("/evidence-redactions/{redaction_id}/file")
async def file(redaction_id: str, services: Annotated[RedactionServices, Depends(_services)]):
    return await _operate("read", services, redaction_id=redaction_id)
