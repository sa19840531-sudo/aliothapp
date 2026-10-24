"""正式 App 质检 OpenAPI 冒烟。"""

from services.api.app.api.v1.app_qc_openapi import (
    build_app_qc_openapi,
    render_app_qc_swagger_html,
)


def test_app_qc_openapi_contains_core_paths() -> None:
    spec = build_app_qc_openapi()
    assert spec["info"]["title"] == "Alioth 正式 App 质检 API"
    paths = spec["paths"]
    for path in (
        "/api/v1/auth/login",
        "/api/v1/app-builds",
        "/api/v1/tasks",
        "/api/v1/workstation/tasks/{task_id}/page-timeline",
        "/api/v1/workstation/tasks/{task_id}/app-stage",
        "/api/v1/evidence/{artifact_id}/file",
        "/api/v1/app-qc/api-doc",
    ):
        assert path in paths


def test_app_qc_openapi_documents_sealed_exploration_projection() -> None:
    spec = build_app_qc_openapi()
    operation = spec["paths"]["/api/v1/workstation/tasks/{task_id}/app-stage"]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    exploration = schema["properties"]["app_exploration"]

    assert exploration["nullable"] is True
    properties = exploration["properties"]
    for field in (
        "status",
        "surface_phase",
        "coverage_summary",
        "terminal_reason_code",
        "outcome",
        "evidence_index_id",
        "evidence_index_hash",
        "evidence_gap_count",
        "surface_session_status",
    ):
        assert field in properties
    assert "close_failed" in properties["status"]["enum"]
    assert "probe_work" not in schema["properties"]


def test_app_qc_swagger_html_points_to_openapi() -> None:
    html = render_app_qc_swagger_html()
    assert "SwaggerUIBundle" in html
    assert "/api/v1/app-qc/openapi.json" in html
    assert "正式 App 质检" in html
