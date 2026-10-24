from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import health as health_module
from services.api.app.api.v1.health import ReadinessDependency, ReadinessResponse
from services.api.app.core.config import EvidenceStorageSettings, Settings
from services.api.app.main import create_app


def test_health_endpoint_returns_service_status() -> None:
    client = TestClient(create_app())

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "Alioth API",
        "environment": "local",
    }


def _readiness_report(*, ready: bool) -> ReadinessResponse:
    dependency_status = "ok" if ready else "failed"
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        service="Alioth API",
        environment="local",
        checked_at=datetime.now(UTC),
        dependencies={
            "postgres": ReadinessDependency(
                status=dependency_status,
                code=None if ready else "postgres_unavailable",
            ),
            "redis": ReadinessDependency(
                status=dependency_status,
                code=None if ready else "redis_unavailable",
            ),
            "evidence_storage": ReadinessDependency(status=dependency_status),
            "worker": ReadinessDependency(
                status=dependency_status,
                code=None if ready else "worker_heartbeat_missing",
            ),
        },
    )


def test_readiness_endpoint_returns_200_only_when_all_dependencies_are_ready() -> None:
    app = create_app()
    app.dependency_overrides[health_module._get_readiness_report] = lambda: _readiness_report(
        ready=True
    )

    response = TestClient(app).get("/api/v1/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["dependencies"]["worker"]["status"] == "ok"


def test_readiness_endpoint_returns_503_with_structured_failure_codes() -> None:
    app = create_app()
    app.dependency_overrides[health_module._get_readiness_report] = lambda: _readiness_report(
        ready=False
    )

    response = TestClient(app).get("/api/v1/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"]["redis"]["code"] == "redis_unavailable"
    assert "postgresql://" not in response.text


def test_ready_alias_uses_the_same_readiness_contract() -> None:
    app = create_app()
    app.dependency_overrides[health_module._get_readiness_report] = lambda: _readiness_report(
        ready=True
    )

    response = TestClient(app).get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_is_fresh_heartbeat_rejects_stale_and_future_values() -> None:
    now = datetime.now(UTC)

    assert health_module._is_fresh_heartbeat(
        now - timedelta(seconds=5), now=now, max_age_seconds=30
    )
    assert not health_module._is_fresh_heartbeat(
        now - timedelta(seconds=31), now=now, max_age_seconds=30
    )
    assert not health_module._is_fresh_heartbeat(
        now + timedelta(seconds=1), now=now, max_age_seconds=30
    )


@pytest.mark.asyncio
async def test_redis_and_worker_check_requires_a_fresh_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    class FakeQueue:
        def __init__(self, _client: object, *, ttl_seconds: int) -> None:
            assert ttl_seconds > 0

        async def read_worker_heartbeats(self) -> list[dict[str, object]]:
            return [
                {
                    "heartbeat_at": datetime.now(UTC),
                    "roles": ["explorer", "judge"],
                }
            ]

    client = FakeClient()
    monkeypatch.setenv("ALIOTH_READINESS_REQUIRE_WORKER_HEARTBEAT", "true")
    monkeypatch.setattr(health_module, "get_settings", lambda: Settings())
    monkeypatch.setattr(health_module, "build_redis_client", lambda _settings: client)
    monkeypatch.setattr(health_module, "RedisAgentRunQueue", FakeQueue)

    redis_status, worker_status = await health_module._check_redis_and_worker()

    assert redis_status.status == "ok"
    assert worker_status.status == "ok"


@pytest.mark.asyncio
async def test_redis_and_worker_check_rejects_missing_required_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    class FakeQueue:
        def __init__(self, _client: object, *, ttl_seconds: int) -> None:
            assert ttl_seconds > 0

        async def read_worker_heartbeats(self) -> list[dict[str, object]]:
            return [{"heartbeat_at": datetime.now(UTC), "roles": ["explorer"]}]

    monkeypatch.setenv("ALIOTH_READINESS_REQUIRE_WORKER_HEARTBEAT", "true")
    monkeypatch.setattr(health_module, "get_settings", lambda: Settings())
    monkeypatch.setattr(health_module, "build_redis_client", lambda _settings: FakeClient())
    monkeypatch.setattr(health_module, "RedisAgentRunQueue", FakeQueue)

    redis_status, worker_status = await health_module._check_redis_and_worker()

    assert redis_status.status == "ok"
    assert worker_status.status == "failed"
    assert worker_status.code == "worker_role_missing"


@pytest.mark.asyncio
async def test_local_evidence_storage_check_requires_an_existing_writable_root(
    tmp_path,
) -> None:
    ready_settings = Settings(
        evidence_storage=EvidenceStorageSettings(
            backend="local",
            local_root=str(tmp_path),
        )
    )
    missing_settings = Settings(
        evidence_storage=EvidenceStorageSettings(
            backend="local",
            local_root=str(tmp_path / "missing"),
        )
    )

    assert (await health_module._check_evidence_storage(ready_settings)).status == "ok"
    missing = await health_module._check_evidence_storage(missing_settings)
    assert missing.status == "failed"
    assert missing.code == "evidence_root_missing"


@pytest.mark.asyncio
async def test_local_evidence_storage_check_rejects_fsync_io_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fail_fsync(_file_descriptor: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(health_module.os, "fsync", fail_fsync)
    settings = Settings(
        evidence_storage=EvidenceStorageSettings(
            backend="local",
            local_root=str(tmp_path),
        )
    )

    result = await health_module._check_evidence_storage(settings)

    assert result.status == "failed"
    assert result.code == "evidence_root_io_error"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_local_evidence_storage_check_rejects_exhausted_filesystem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class ExhaustedFilesystem:
        @staticmethod
        def disk_usage(_path: object) -> object:
            return type("DiskUsage", (), {"free": 0})()

    monkeypatch.setattr(
        health_module,
        "shutil",
        ExhaustedFilesystem,
        raising=False,
    )
    settings = Settings(
        evidence_storage=EvidenceStorageSettings(
            backend="local",
            local_root=str(tmp_path),
        )
    )

    result = await health_module._check_evidence_storage(settings)

    assert result.status == "failed"
    assert result.code == "evidence_root_insufficient_space"


@pytest.mark.asyncio
async def test_minio_evidence_storage_check_uses_public_readiness_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        async def check_ready(self) -> bool:
            return True

    settings = Settings(
        evidence_storage=EvidenceStorageSettings(
            backend="minio",
            minio_endpoint="minio:9000",
            minio_access_key="access",
            minio_secret_key="secret",
        )
    )
    monkeypatch.setattr(
        health_module,
        "build_evidence_artifact_store",
        lambda _settings: FakeStore(),
    )

    result = await health_module._check_evidence_storage(settings)

    assert result.status == "ok"
