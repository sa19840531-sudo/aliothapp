"""页面类型与页面导航知识域。"""

from services.api.app.domain.page.catalog import PageTypeCatalog, load_default_page_type_catalog
from services.api.app.domain.page.registry import (
    PageTypeRegistry,
    get_default_page_type_registry,
)
from services.api.app.domain.page.web_page_catalog import (
    WEB_BUSINESS_PAGE_TYPE_CODES,
    WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES,
    WEB_LIFECYCLE_PAGE_TYPE_CODES,
    WEB_NEITHER_PAGE_TYPE_CODES,
    WEB_PAGE_TYPE_CODES,
    WebPageTypeCatalogError,
    load_web_page_type_catalog,
)
from services.api.app.domain.page.web_page_knowledge import (
    WebCommonSiteObservations,
    WebNavigationSignals,
    WebPageMediaRules,
    WebPageTypeCatalog,
    WebPageTypeGroups,
)
from services.api.app.domain.page.web_site_menu_registry import (
    WebCatalogStatus,
    WebCollectionEligibility,
    WebMenuDecision,
    WebPageClassificationHint,
    WebSiteMenuCatalog,
    WebSiteMenuCatalogError,
    WebSiteMenuCatalogNotFoundError,
    WebSiteMenuContext,
    WebSiteMenuEntry,
    WebSiteMenuRegistry,
    get_default_web_site_menu_registry,
)

__all__ = [
    "PageTypeCatalog",
    "PageTypeRegistry",
    "WEB_BUSINESS_PAGE_TYPE_CODES",
    "WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES",
    "WEB_LIFECYCLE_PAGE_TYPE_CODES",
    "WEB_NEITHER_PAGE_TYPE_CODES",
    "WEB_PAGE_TYPE_CODES",
    "WebPageTypeCatalogError",
    "WebPageTypeCatalog",
    "WebPageTypeGroups",
    "WebPageMediaRules",
    "WebNavigationSignals",
    "WebCommonSiteObservations",
    "WebCatalogStatus",
    "WebCollectionEligibility",
    "WebMenuDecision",
    "WebPageClassificationHint",
    "WebSiteMenuCatalog",
    "WebSiteMenuContext",
    "WebSiteMenuCatalogError",
    "WebSiteMenuCatalogNotFoundError",
    "WebSiteMenuEntry",
    "WebSiteMenuRegistry",
    "get_default_page_type_registry",
    "get_default_web_site_menu_registry",
    "load_default_page_type_catalog",
    "load_web_page_type_catalog",
]
