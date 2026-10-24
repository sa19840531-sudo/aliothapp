"""广告目标去重身份；保留路径与查询语义，不按域名粗暴合并。"""

from urllib.parse import urlsplit, urlunsplit

from services.api.app.domain.page.web_origin import canonical_web_origin


def canonical_ad_destination(url: str) -> str:
    parts = urlsplit(url)
    origin = urlsplit(canonical_web_origin(url))
    return urlunsplit((origin.scheme, origin.netloc, parts.path or "/", parts.query, ""))
