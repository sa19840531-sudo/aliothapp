from __future__ import annotations

from importlib import import_module

from fastapi.testclient import TestClient

from services.api.app.main import create_app


class RecordingPlatformBuildService:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def upload_apk(self, *, original_filename, chunks, access_scope, source_url=None):
        content = b"".join([chunk async for chunk in chunks])
        self.calls.append(
            {
                "original_filename": original_filename,
                "content": content,
                "access_scope": access_scope,
                "source_url": source_url,
            }
        )
        return self.result


def test_upload_app_build_returns_verified_reference() -> None:
    api_module = import_module("services.api.app.api.v1.app_builds")
    domain = import_module("services.api.app.domain.task.platform_build")
    result = domain.PlatformBuild(
        build_id="build-1",
        platform="android",
        original_filename="target.apk",
        storage_path="aa/sha.apk",
        sha256="a" * 64,
        size_bytes=15,
        status=domain.PlatformBuildStatus.VERIFIED,
        created_by="test-user",
    )
    service = RecordingPlatformBuildService(result)
    app = create_app()
    app.dependency_overrides[api_module._get_platform_build_service] = lambda: service
    client = TestClient(app)

    response = client.post(
        "/api/v1/app-builds",
        files={
            "file": (
                "target.apk",
                b"PK\x03\x04verified-apk",
                "application/vnd.android.package-archive",
            )
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "platform_build_ref": "app-build:build-1",
        "status": "verified",
        "original_filename": "target.apk",
        "sha256": "a" * 64,
        "size_bytes": 15,
        "package_name": None,
        "version_name": None,
        "version_code": None,
    }
    assert len(service.calls) == 1
    assert service.calls[0]["original_filename"] == "target.apk"
    assert service.calls[0]["content"] == b"PK\x03\x04verified-apk"
    assert service.calls[0]["access_scope"].actor_user_id == "test-user"
