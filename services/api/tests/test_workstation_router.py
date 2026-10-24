from services.api.app.main import create_app


def test_workstation_namespace_is_mounted() -> None:
    app = create_app()

    paths = set(app.openapi()["paths"])

    assert "/api/v1/workstation/tasks" in paths
    assert "/api/v1/workstation/reports" in paths
    assert "/api/v1/workstation/reviews" in paths
    assert "/api/v1/workstation/dashboard/overview" in paths
