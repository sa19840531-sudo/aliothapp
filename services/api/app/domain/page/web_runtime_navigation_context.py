"""当前 Web 导航只持有目标、安全策略与动态发现配置。"""

from dataclasses import dataclass

from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy
from services.api.app.domain.page.web_origin import canonical_web_origin
from services.api.app.domain.page.web_site_menu_registry import (
    WebSiteMenuContext,
    _runtime_home_context,
)
from services.api.app.domain.page.web_url_identity import WebUrlIdentityPolicy


@dataclass(frozen=True, kw_only=True)
class RuntimeWebNavigationContext:
    target_origin: str
    url_safety_policy: WebUrlIdentityPolicy
    discovery_policy: WebDiscoveryPolicy

    def __post_init__(self) -> None:
        if not self.discovery_policy.enabled:
            raise ValueError("当前 Web 执行必须启用动态发现")
        object.__setattr__(self, "target_origin", canonical_web_origin(self.target_origin))

    def resolve_site_context(self, target_url: str) -> WebSiteMenuContext:
        """复用持久账本首页身份，拒绝把其他 origin 注入当前导航。"""
        if canonical_web_origin(target_url) != self.target_origin:
            raise ValueError("Web 导航上下文目标 origin 不匹配")
        return _runtime_home_context(target_url)
