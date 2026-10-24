"""通用 Swagger UI HTML（正式 App 质检／星语 Official 共用）。"""

from __future__ import annotations


def render_swagger_html(
    *,
    openapi_url: str,
    title: str = "Alioth API 对接",
) -> str:
    """Swagger UI 风格对接站。"""
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{title}</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui.css" />
    <style>
      body {{ margin: 0; background: #fafafa; }}
      .alioth-top {{
        background: linear-gradient(90deg, #0f172a, #1e3a8a);
        color: #e2e8f0;
        padding: 14px 20px;
        display: flex;
        gap: 16px;
        align-items: center;
        flex-wrap: wrap;
      }}
      .alioth-top h1 {{ margin: 0; font-size: 18px; font-weight: 800; }}
      .alioth-top a {{
        color: #93c5fd;
        text-decoration: none;
        font-size: 13px;
        font-weight: 600;
      }}
      .alioth-chip {{
        padding: 2px 10px;
        border-radius: 999px;
        background: rgba(56,189,248,.15);
        border: 1px solid rgba(56,189,248,.35);
        color: #bae6fd;
        font-size: 12px;
      }}
      .swagger-ui .topbar {{ display: none; }}
    </style>
  </head>
  <body>
    <header class="alioth-top">
      <h1>{title}</h1>
      <span class="alioth-chip">OpenAPI 3 / Swagger UI</span>
      <a href="{openapi_url}" target="_blank">openapi.json</a>
    </header>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui-bundle.js" crossorigin></script>
    <script src="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui-standalone-preset.js" crossorigin></script>
    <script>
      window.onload = () => {{
        window.ui = SwaggerUIBundle({{
          url: {openapi_url!r},
          dom_id: '#swagger-ui',
          deepLinking: true,
          presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
          plugins: [SwaggerUIBundle.plugins.DownloadUrl],
          layout: 'StandaloneLayout',
          tryItOutEnabled: true,
          persistAuthorization: true,
        }});
      }};
    </script>
  </body>
</html>
"""
