"""权限与账号领域导出。"""

from services.api.app.domain.iam.access_scope import (
    AccessScope,
    access_scope_from_user,
)

__all__ = ["AccessScope", "access_scope_from_user"]
