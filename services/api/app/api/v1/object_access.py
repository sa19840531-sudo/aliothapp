"""用户面对象访问依赖；拒绝与不存在统一返回 404。"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from services.api.app.api.v1.auth import require_access_scope
from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.object_access_repository import (
    ObjectAccessAction,
    PostgresObjectAccessRepository,
)


@lru_cache
def _get_object_access_repository() -> PostgresObjectAccessRepository:
    return PostgresObjectAccessRepository()


async def require_task_read_access(
    task_id: str,
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[
        PostgresObjectAccessRepository,
        Depends(_get_object_access_repository),
    ],
) -> AccessScope:
    if not await repository.can_access_task(task_id=task_id, scope=scope):
        _raise_not_found()
    return scope


async def require_task_write_access(
    task_id: str,
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[
        PostgresObjectAccessRepository,
        Depends(_get_object_access_repository),
    ],
) -> AccessScope:
    if not await repository.can_access_task(
        task_id=task_id,
        scope=scope,
        action=ObjectAccessAction.WRITE,
    ):
        _raise_not_found()
    return scope


async def require_task_review_access(
    task_id: str,
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[
        PostgresObjectAccessRepository,
        Depends(_get_object_access_repository),
    ],
) -> AccessScope:
    if not await repository.can_access_task(
        task_id=task_id,
        scope=scope,
        action=ObjectAccessAction.REVIEW,
    ):
        _raise_not_found()
    return scope


async def require_artifact_read_access(
    artifact_id: str,
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[
        PostgresObjectAccessRepository,
        Depends(_get_object_access_repository),
    ],
) -> AccessScope:
    if not await repository.can_access_artifact(artifact_id=artifact_id, scope=scope):
        _raise_not_found()
    return scope


async def require_artifact_write_access(
    artifact_id: str,
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[
        PostgresObjectAccessRepository,
        Depends(_get_object_access_repository),
    ],
) -> AccessScope:
    if not await repository.can_access_artifact(
        artifact_id=artifact_id,
        scope=scope,
        action=ObjectAccessAction.WRITE,
    ):
        _raise_not_found()
    return scope


async def require_review_access(
    review_id: str,
    scope: Annotated[AccessScope, Depends(require_access_scope)],
    repository: Annotated[
        PostgresObjectAccessRepository,
        Depends(_get_object_access_repository),
    ],
) -> AccessScope:
    if not await repository.can_access_review(review_id=review_id, scope=scope):
        _raise_not_found()
    return scope


def _raise_not_found() -> None:
    raise AliothError(message="未找到请求的资源", status_code=404)
