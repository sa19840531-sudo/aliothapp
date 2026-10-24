"""任务的质检范围：建任务时在「页面范围」里选的页面类型（2026-09-17 立川拍板）。

选了部分页面类型时，探索可以经过没选中的页面去找目标页面，但经过的页面（包括开屏、没选中的首页）
只留认路记录：不判规则，不当证据，不进报告和页面覆盖。只挂在范围外页面类型上的规则不进这次任务。
选「全部」或都不选时范围为空，表示不限制。旧任务没有这个配置，同样按不限制处理。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

INSPECTION_SCOPE_CONFIG_KEY = "inspection_scope"


def build_inspection_scope_config(page_types: Iterable[str] | None) -> dict[str, list[str]]:
    """写进 task_config 的质检范围；页面类型统一小写、去重、排序。"""

    return {"page_types": sorted(_normalized_page_types(page_types or ()))}


def inspection_scope_page_types(task_config: Mapping[str, object] | None) -> frozenset[str]:
    """读出任务的质检范围；返回空集合表示不限制。"""

    if not isinstance(task_config, Mapping):
        return frozenset()
    scope = task_config.get(INSPECTION_SCOPE_CONFIG_KEY)
    if not isinstance(scope, Mapping):
        return frozenset()
    page_types = scope.get("page_types")
    if not isinstance(page_types, (list, tuple)):
        return frozenset()
    return frozenset(_normalized_page_types(page_types))


def page_type_in_inspection_scope(scope: frozenset[str], page_type: object) -> bool:
    """范围为空时任何页面都在范围内；否则页面类型必须被选中。"""

    return not scope or str(page_type or "").strip().casefold() in scope


def _normalized_page_types(page_types: Iterable[object]) -> set[str]:
    return {
        value.strip().casefold() for value in page_types if isinstance(value, str) and value.strip()
    }
