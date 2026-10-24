"""证据产物回读接口：按 artifact_id 流式返回已落库的证据文件（截图/DOM/文本等）。

补齐"证据只写不读"的缺口：文件本体存于 local/MinIO（``infrastructure/storage``），
元数据存于 ``qc_evidence_artifact``；本接口统一两种后端的 HTTP 出口，任务详情页
的截图缩略图/大图预览、后续视觉判定回读均走这里。
"""

from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from services.api.app.api.v1.auth import (
    _build_user_response,
    download_ticket_signing_secret,
)
from services.api.app.api.v1.object_access import (
    _get_object_access_repository,
    require_artifact_read_access,
    require_artifact_write_access,
)
from services.api.app.application.download_tickets import (
    DownloadTicketClaims,
    DownloadTicketError,
    SignedDownloadTicketService,
)
from services.api.app.application.evidence_lifecycle import (
    EvidenceLifecycleError,
    EvidenceLifecycleService,
)
from services.api.app.application.task_insight import TaskInsightService
from services.api.app.core.config import get_settings
from services.api.app.core.errors import AliothError
from services.api.app.domain.evidence.lifecycle import EvidenceLifecyclePolicy
from services.api.app.domain.iam.access_scope import AccessScope, access_scope_from_user
from services.api.app.infrastructure.db.evidence_download_audit_repository import (
    OBJECT_ACCESS_REVOKED,
    SUBJECT_REVOKED,
    PostgresEvidenceDownloadAuditRepository,
)
from services.api.app.infrastructure.db.evidence_lifecycle_repository import (
    PostgresEvidenceLifecycleRepository,
)
from services.api.app.infrastructure.db.object_access_repository import (
    PostgresObjectAccessRepository,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.user_repository import PostgresUserRepository

router = APIRouter(prefix="/evidence")

_ARTIFACT_TYPE_MEDIA_FALLBACK = {
    "screenshot": "image/png",
    "dom": "text/html; charset=utf-8",
    "visible_text": "text/plain; charset=utf-8",
    "console_log": "application/json",
    "har": "application/json",
}

# 下载文件名要带扩展名，否则存下来的截图没有文件类型、双击打不开
_MEDIA_TYPE_EXTENSION = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "text/html": ".html",
    "text/plain": ".txt",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


def _download_filename(artifact_id: str, media_type: str) -> str:
    base_type = media_type.split(";", 1)[0].strip().casefold()
    return f"{artifact_id}{_MEDIA_TYPE_EXTENSION.get(base_type, '')}"


@lru_cache
def _get_task_insight_service() -> TaskInsightService:
    return TaskInsightService(repository=PostgresTaskInsightRepository())


@lru_cache
def _get_download_ticket_service() -> SignedDownloadTicketService:
    return SignedDownloadTicketService(secret=download_ticket_signing_secret())


@lru_cache
def _get_ticket_subject_repository() -> PostgresUserRepository:
    """票据兑现时重查主体用；与登录路径分开，避免影响 /auth/login 的依赖覆盖。"""
    return PostgresUserRepository()


@lru_cache
def _get_download_audit_repository() -> PostgresEvidenceDownloadAuditRepository:
    return PostgresEvidenceDownloadAuditRepository()


class EvidenceDownloadTicketResponse(BaseModel):
    """前端可用于图片或下载的资源绑定短时地址。"""

    url: str
    expires_at: str


class EvidenceRestoreRequest(BaseModel):
    """证据恢复请求。"""

    reason: str = "manual_restore"


class EvidenceRestoreResponse(BaseModel):
    """证据恢复响应。"""

    artifact_id: str
    task_id: str
    from_status: str
    to_status: str
    changed: bool


@lru_cache
def _get_evidence_lifecycle_service() -> EvidenceLifecycleService:
    settings = get_settings().evidence_lifecycle
    return EvidenceLifecycleService(
        repository=PostgresEvidenceLifecycleRepository(),
        policy=EvidenceLifecyclePolicy(
            policy_version=settings.policy_version,
            retention_days=settings.retention_days,
            gc_enabled=settings.gc_enabled,
            batch_limit=settings.batch_limit,
        ),
    )


@router.post(
    "/{artifact_id}/ticket",
    response_model=EvidenceDownloadTicketResponse,
    summary="签发证据文件短时访问票据",
)
async def issue_evidence_download_ticket(
    artifact_id: str,
    scope: Annotated[AccessScope, Depends(require_artifact_read_access)],
    ticket_service: Annotated[
        SignedDownloadTicketService,
        Depends(_get_download_ticket_service),
    ],
) -> EvidenceDownloadTicketResponse:
    ticket, expires_at = ticket_service.issue(
        resource_kind="evidence_artifact",
        resource_id=artifact_id,
        actor_user_id=scope.actor_user_id,
        tenant_id=scope.tenant_id,
    )
    return EvidenceDownloadTicketResponse(
        url=f"/evidence/{quote(artifact_id)}/file?ticket={quote(ticket)}",
        expires_at=expires_at.isoformat(),
    )


@router.post(
    "/{artifact_id}/restore",
    response_model=EvidenceRestoreResponse,
    summary="恢复 archived／pending_delete 证据为 active",
)
async def restore_evidence_artifact(
    artifact_id: str,
    body: EvidenceRestoreRequest,
    scope: Annotated[AccessScope, Depends(require_artifact_write_access)],
    service: Annotated[
        EvidenceLifecycleService,
        Depends(_get_evidence_lifecycle_service),
    ],
) -> EvidenceRestoreResponse:
    try:
        result = await service.restore_artifact(
            artifact_id=artifact_id,
            actor=scope.actor_user_id,
            reason=body.reason,
        )
    except EvidenceLifecycleError as exc:
        raise AliothError(message=str(exc), status_code=exc.status_code) from exc
    return EvidenceRestoreResponse(
        artifact_id=result.artifact_id,
        task_id=result.task_id,
        from_status=result.from_status.value,
        to_status=result.to_status.value,
        changed=result.changed,
    )


async def _current_scope_for_ticket(
    claims: DownloadTicketClaims,
    *,
    user_repository: PostgresUserRepository,
) -> AccessScope | None:
    """按票据主体重新加载当前用户；账号停用或换租户一律视为已撤权。"""
    row = await user_repository.find_by_id(claims.actor_user_id)
    if row is None or str(row.get("status") or "") != "enabled":
        return None
    if str(row.get("tenant_id") or "") != claims.tenant_id:
        return None
    return access_scope_from_user(_build_user_response(row))


@router.get("/{artifact_id}/file", summary="回读证据文件内容")
async def get_evidence_file(
    artifact_id: str,
    service: Annotated[TaskInsightService, Depends(_get_task_insight_service)],
    ticket_service: Annotated[
        SignedDownloadTicketService,
        Depends(_get_download_ticket_service),
    ],
    user_repository: Annotated[
        PostgresUserRepository,
        Depends(_get_ticket_subject_repository),
    ],
    object_access: Annotated[
        PostgresObjectAccessRepository,
        Depends(_get_object_access_repository),
    ],
    download_audit: Annotated[
        PostgresEvidenceDownloadAuditRepository,
        Depends(_get_download_audit_repository),
    ],
    ticket: Annotated[str | None, Query()] = None,
    download: bool = False,
    format: str | None = None,
    representation: Literal["preview", "original"] = "preview",
) -> Response:
    try:
        claims = ticket_service.verify(
            ticket or "",
            resource_kind="evidence_artifact",
            resource_id=artifact_id,
        )
    except DownloadTicketError as exc:
        raise AliothError(message="未找到请求的资源", status_code=404) from exc
    # 票据只证明签发当时通过了校验；兑现时必须按当前用户事实复验，
    # 否则账号停用、角色降权或换租户之后，未过期的旧票据仍能取走证据。
    scope = await _current_scope_for_ticket(claims, user_repository=user_repository)
    denied_reason: str | None = None
    if scope is None:
        denied_reason = SUBJECT_REVOKED
    elif not await object_access.can_access_artifact(artifact_id=artifact_id, scope=scope):
        denied_reason = OBJECT_ACCESS_REVOKED
    # 放行与拒绝都要留痕，且必须先于内容返回落库：没有审计就不外发证据。
    await download_audit.record(
        artifact_id=artifact_id,
        actor_user_id=claims.actor_user_id,
        tenant_id=claims.tenant_id,
        ticket_id=claims.ticket_id,
        result="denied" if denied_reason else "allowed",
        denied_reason=denied_reason,
    )
    if denied_reason:
        raise AliothError(message="未找到请求的资源", status_code=404)
    try:
        payload = (
            await service.evidence_file(artifact_id, original=True)
            if representation == "original" else await service.evidence_file(artifact_id)
        )
    except FileNotFoundError as exc:
        raise AliothError(message=f"证据文件缺失：{artifact_id}", status_code=404) from exc
    except ValueError as exc:
        raise AliothError(message="原始证据摘要校验失败", status_code=409) from exc
    if payload is None:
        raise AliothError(message=f"未找到证据：{artifact_id}", status_code=404)
    media_type = payload.get("content_type") or _ARTIFACT_TYPE_MEDIA_FALLBACK.get(
        payload["artifact_type"], "application/octet-stream"
    )
    content = payload["content"]
    fmt = (format or "").strip().lower()
    if fmt in ("base64", "b64", "json") and representation != "original":
        # 備援：對方只能收 JSON 內嵌圖時使用；正式路徑仍應吃 PNG 二進位
        import base64

        from fastapi.responses import JSONResponse

        return JSONResponse(
            {
                "artifact_id": payload["artifact_id"],
                "artifact_type": payload["artifact_type"],
                "content_type": media_type,
                "encoding": "base64",
                "data": base64.b64encode(content).decode("ascii"),
            }
        )
    headers = {}
    if representation == "original":
        headers["X-Artifact-SHA256"] = payload["checksum"]
        headers["X-Artifact-Representation"] = "original"
        headers["Cache-Control"] = "no-store"
    if download:
        filename = quote(_download_filename(str(payload["artifact_id"]), media_type))
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{filename}"
    return Response(content=content, media_type=media_type, headers=headers)
