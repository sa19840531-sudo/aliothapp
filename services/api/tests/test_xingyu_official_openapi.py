"""星語 Official OpenAPI 组装冒烟测试。"""

from services.api.app.api.v1.xingyu_official.openapi import (
    build_xingyu_official_openapi,
    render_xingyu_swagger_html,
)


def test_xingyu_openapi_contains_core_paths() -> None:
    spec = build_xingyu_official_openapi()
    assert spec["openapi"].startswith("3.")
    paths = spec["paths"]
    for path in (
        "/v1/sessions",
        "/v1/sessions/reattach",
        "/v1/sessions/close",
        "/v1/apps/install",
        "/v1/apps/open",
        "/v1/apps/capture",
        "/v1/apps/capture-ui-tree",
        "/v1/apps/execute",
        "/v1/api-doc",
        "/v1/openapi.json",
    ):
        assert path in paths


def test_xingyu_swagger_html_points_to_openapi() -> None:
    html = render_xingyu_swagger_html()
    assert "SwaggerUIBundle" in html
    assert "/v1/openapi.json" in html
    assert "星語 Official" in html
