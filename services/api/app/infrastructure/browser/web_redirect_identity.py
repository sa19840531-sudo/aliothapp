"""从浏览器响应的原生 document 请求链提取 Web 重定向事实。"""

from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY


def natural_web_redirect_chain(response) -> tuple[str, ...]:
    request = getattr(response, "request", None)
    chain = []
    seen = set()
    while request is not None:
        if id(request) in seen or len(chain) >= 20:
            return ()
        seen.add(id(request))
        if (
            getattr(request, "method", "") != "GET"
            or getattr(request, "resource_type", "") != "document"
        ):
            return ()
        try:
            chain.append(DEFAULT_WEB_URL_IDENTITY_POLICY.canonical(request.url))
        except (ValueError, AttributeError):
            return ()
        request = getattr(request, "redirected_from", None)
    return tuple(reversed(chain)) if len(chain) > 1 else ()
