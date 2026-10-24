"""脱敏交付沿用当前主体、任务对象范围和原件生命周期权限。"""

from services.api.app.api.v1.auth import _build_user_response, _tenant_disabled
from services.api.app.domain.evidence.redaction import RedactionError
from services.api.app.domain.iam.access_scope import access_scope_from_user

MANAGE_PERMISSIONS = frozenset({
    "evidenceRedactions:manage", "evidenceExports:original", "evidenceExports:redacted",
})


def can_manage(scope):
    return MANAGE_PERMISSIONS <= scope.permissions


async def authorize(*, scope, access, artifact_id, manage=False):
    if not await access.can_access_artifact(artifact_id=artifact_id, scope=scope):
        raise RedactionError("REDACTION_ACCESS_DENIED", "材料不存在或不可访问", status_code=404)
    required = MANAGE_PERMISSIONS if manage else {"evidenceExports:redacted"}
    if not required <= scope.permissions:
        raise RedactionError("REDACTION_PERMISSION_DENIED", "没有当前脱敏操作权限", status_code=403)


async def current_scope(*, scope, users):
    row = await users.find_by_id(scope.actor_user_id)
    if (
        row is None or row.get("status") != "enabled" or _tenant_disabled(row)
        or str(row.get("tenant_id") or "") != scope.tenant_id
    ):
        raise RedactionError("REDACTION_SUBJECT_REVOKED", "材料不存在或不可访问", status_code=404)
    return access_scope_from_user(_build_user_response(row))
