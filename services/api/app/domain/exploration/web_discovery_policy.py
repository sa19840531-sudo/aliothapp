"""Web 任务级目录外发现授权及不可变运行策略。"""

from collections.abc import Mapping
from dataclasses import dataclass

from services.api.app.domain.page.web_page_catalog import WEB_BUSINESS_PAGE_TYPE_CODES

LEGACY_WEB_DISCOVERY_POLICY = {
    "version": "web-discovery.v1",
    "menu_limit": 10,
    "unlisted_action": "record_only_needs_review",
    "shared_deadline": True,
}


@dataclass(frozen=True, kw_only=True)
class WebDiscoveryPolicy:
    enabled: bool = False
    max_unlisted_menus: int | None = 0
    same_origin_only: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or self.same_origin_only is not True:
            raise ValueError("Web 发现必须显式布尔启用且仅限同源")
        if self.max_unlisted_menus is not None and (
            type(self.max_unlisted_menus) is not int or self.max_unlisted_menus < 0
        ):
            raise ValueError("Web 发现配额必须为 NULL 或非负整数")
        if self.enabled != (self.max_unlisted_menus is None or self.max_unlisted_menus > 0):
            raise ValueError("Web 发现启用时配额须大于零，关闭时须为零")

    @classmethod
    def for_new_run(cls) -> "WebDiscoveryPolicy":
        """返回新 Run 唯一允许的动态发现策略。"""
        return cls(enabled=True, max_unlisted_menus=None, same_origin_only=True)

    @classmethod
    def from_config(cls, value: object) -> "WebDiscoveryPolicy":
        if value is None:
            return cls()
        if not isinstance(value, Mapping) or set(value) != {
            "enabled",
            "max_unlisted_menus",
            "same_origin_only",
        }:
            raise ValueError("Web discovery_policy 必须包含且仅包含完整授权字段")
        return cls(**value)

    def to_config(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "max_unlisted_menus": self.max_unlisted_menus,
            "same_origin_only": self.same_origin_only,
        }


def validate_discovery_required_types(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or any(
            not isinstance(item, str) or item not in WEB_BUSINESS_PAGE_TYPE_CODES
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError("Web 发现调度必须消费冻结的规范 required 目标")
    return tuple(value)
