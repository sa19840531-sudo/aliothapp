"""App 质检包覆盖范围统一入口：按 package_name 取 exploration_target。"""

from __future__ import annotations

from services.api.app.domain.exploration.generic5_coverage_scope import (
    GENERIC5_PACKAGE_NAMES,
    generic5_exploration_target,
    generic5_task_remark,
)
from services.api.app.domain.exploration.hjll_coverage_scope import (
    HJLL_PACKAGE_NAMES,
    hjll_exploration_target,
    hjll_task_remark,
)
from services.api.app.domain.exploration.hlapp_coverage_scope import (
    HLAPP_PACKAGE_NAME,
    HLAPP_PACKAGE_NAMES,
    hlapp_exploration_target,
    hlapp_task_remark,
)
from services.api.app.domain.exploration.pc51_coverage_scope import (
    PC51_PACKAGE_NAME,
    pc51_exploration_target,
    pc51_task_remark,
)

# package_name → scope code
_SCOPE_BY_PACKAGE: dict[str, str] = {
    PC51_PACKAGE_NAME.casefold(): "pc51",
    HLAPP_PACKAGE_NAME.casefold(): "hlapp",
}
for _pkg in HJLL_PACKAGE_NAMES:
    _SCOPE_BY_PACKAGE[_pkg.casefold()] = "hjll"
for _pkg in HLAPP_PACKAGE_NAMES:
    _SCOPE_BY_PACKAGE[_pkg.casefold()] = "hlapp"
for _pkg in GENERIC5_PACKAGE_NAMES:
    _SCOPE_BY_PACKAGE[_pkg.casefold()] = "generic5"


def resolve_package_scope_code(package_name: str | None) -> str | None:
    """返回 hjll / pc51 / hlapp / generic5；未知包返回 None。"""

    key = str(package_name or "").strip().casefold()
    if not key:
        return None
    return _SCOPE_BY_PACKAGE.get(key)


def exploration_target_for_package(package_name: str | None) -> dict[str, object]:
    """按包名返回已锁定的 exploration_target；未知包抛 ValueError。"""

    code = resolve_package_scope_code(package_name)
    if code == "hjll":
        return hjll_exploration_target()
    if code == "pc51":
        return pc51_exploration_target()
    if code == "hlapp":
        return hlapp_exploration_target()
    if code == "generic5":
        return generic5_exploration_target()
    raise ValueError(f"未登记的 App 质检覆盖范围包名：{package_name!r}")


def task_remark_for_package(
    package_name: str | None,
    *,
    profile: str,
    apk_name: str,
    script_path: str = "",
) -> str:
    """按包名生成统一备注。"""

    code = resolve_package_scope_code(package_name)
    if code == "hjll":
        return hjll_task_remark(profile=profile, apk_name=apk_name)
    if code == "pc51":
        return pc51_task_remark(profile=profile, apk_name=apk_name)
    if code == "hlapp":
        return hlapp_task_remark(
            profile=profile,
            apk_name=apk_name,
            script_path=script_path or "untitled1.py",
        )
    if code == "generic5":
        return generic5_task_remark(profile=profile, apk_name=apk_name)
    raise ValueError(f"未登记的 App 质检覆盖范围包名：{package_name!r}")
