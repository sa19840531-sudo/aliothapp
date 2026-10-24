"""星語 Official App API 套件。"""

from services.api.app.api.v1.xingyu_official.docs import docs_router
from services.api.app.api.v1.xingyu_official.gate import xingyu_official_router_enabled
from services.api.app.api.v1.xingyu_official.router import router

__all__ = ["docs_router", "router", "xingyu_official_router_enabled"]
