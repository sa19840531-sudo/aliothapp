"""已认证业务主体的对象级访问范围。

该模块只表达权限和对象判定，不执行数据库查询，也不接受客户端传入的 owner、
tenant 或 department。API 层负责把数据库中的当前用户投影为 :class:`AccessScope`，
业务仓储再使用本模块的判定结果构造查询条件。

数据范围三级：
- 总部（``taskObjects:*:any``）跨公司；
- 公司管理员 / 审计员（``taskObjects:read:tenant`` 等）看本公司全部；
- 部门账号（``taskObjects:read:department``）看本公司里本部门的对象 + 自己发起的；
  账号没有填部门时退回只看自己发起的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _normalized_department(value: Any) -> str:
    """部门名去空白；工作台把没填的部门存成 '-'，也当作没有部门。"""

    text = str(value or "").strip()
    return "" if text == "-" else text


@dataclass(frozen=True, slots=True)
class AccessScope:
    """当前用户可访问的租户、部门和 owner 范围。"""

    actor_user_id: str
    tenant_id: str
    permissions: frozenset[str]
    department_name: str = ""

    def __post_init__(self) -> None:
        if not self.actor_user_id.strip():
            raise ValueError("AccessScope.actor_user_id 不能为空")
        if not self.tenant_id.strip():
            raise ValueError("AccessScope.tenant_id 不能为空")

    @property
    def can_read_any(self) -> bool:
        """是否可以跨租户读取任务类对象。"""

        return "taskObjects:read:any" in self.permissions

    @property
    def can_write_any(self) -> bool:
        """是否可以跨租户写入任务类对象。"""

        return "taskObjects:write:any" in self.permissions

    @property
    def can_read_tenant(self) -> bool:
        """是否可以读取当前租户中其他 owner 的对象。"""

        return self.can_read_any or "taskObjects:read:tenant" in self.permissions

    @property
    def can_review_tenant(self) -> bool:
        """是否可以审核当前租户中其他 owner 的对象。"""

        return (
            "taskObjects:review:any" in self.permissions
            or "taskObjects:review:tenant" in self.permissions
        )

    @property
    def can_read_department(self) -> bool:
        """是否可以读取本部门其他人发起的对象（部门账号）。"""

        return "taskObjects:read:department" in self.permissions

    @property
    def can_review_department(self) -> bool:
        """是否可以审核本部门其他人发起的对象（部门账号）。"""

        return "taskObjects:review:department" in self.permissions

    @property
    def department_scope(self) -> str | None:
        """SQL 用的读范围部门名：能按部门读且账号有部门时返回部门名，否则 None。

        None 传进 ``task.department_name = $n::varchar`` 永远不匹配，自然退回 owner 范围。
        """

        department = _normalized_department(self.department_name)
        if department and self.can_read_department:
            return department
        return None

    @property
    def review_department_scope(self) -> str | None:
        """SQL 用的审核范围部门名，同上，只看审核权限。"""

        department = _normalized_department(self.department_name)
        if department and self.can_review_department:
            return department
        return None

    def _same_department(self, department_name: Any) -> bool:
        department = _normalized_department(department_name)
        own = _normalized_department(self.department_name)
        return bool(department) and bool(own) and department == own

    def can_read_resource(
        self, *, owner_id: str, tenant_id: str, department_name: Any = None
    ) -> bool:
        """判断当前用户是否能读指定 owner/tenant/department 的对象。"""

        if self.can_read_any:
            return True
        if tenant_id != self.tenant_id:
            return False
        if owner_id == self.actor_user_id or self.can_read_tenant:
            return True
        return self.can_read_department and self._same_department(department_name)

    def can_write_resource(self, *, owner_id: str, tenant_id: str, department_name: Any = None) -> bool:
        """判断当前用户是否能写指定 owner/tenant 的对象。

        普通角色默认只拥有 owner write；同租户审核角色与同部门账号都不因此获得写权限。
        """

        if self.can_write_any:
            return True
        return tenant_id == self.tenant_id and owner_id == self.actor_user_id

    def can_review_resource(
        self, *, owner_id: str, tenant_id: str, department_name: Any = None
    ) -> bool:
        """判断当前用户是否能审核指定 owner/tenant/department 的对象。"""

        if tenant_id != self.tenant_id:
            return "taskObjects:review:any" in self.permissions
        if owner_id == self.actor_user_id or self.can_review_tenant:
            return True
        return self.can_review_department and self._same_department(department_name)


def access_scope_from_user(user: Any) -> AccessScope:
    """从 AuthUserResponse 或等价的当前用户对象构造访问范围。

    ``tenant_id`` 必须来自服务端数据库加载的当前用户；缺失时立即失败，避免把
    空租户误当作可查询的默认租户。``department_name`` 同样只认服务端加载的值。
    """

    actor_user_id = str(getattr(user, "id", "")).strip()
    tenant_id = str(getattr(user, "tenant_id", "")).strip()
    permissions = frozenset(
        str(permission).strip()
        for permission in (getattr(user, "permissions", ()) or ())
        if str(permission).strip()
    )
    return AccessScope(
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        permissions=permissions,
        department_name=_normalized_department(getattr(user, "department_name", "")),
    )


__all__ = ["AccessScope", "access_scope_from_user"]
