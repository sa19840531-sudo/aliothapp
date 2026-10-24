from __future__ import annotations

from importlib import import_module

from services.api.app.domain.iam.access_scope import AccessScope

TEST_ACCESS_SCOPE = AccessScope(
    actor_user_id="user-1",
    tenant_id="tenant-1",
    permissions=frozenset({"taskObjects:read:any"}),
)


class FakeAcquire:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeConnection:
    def __init__(self, row=None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.row

    async def execute(self, query: str, *args):
        self.calls.append((" ".join(query.split()), args))
        return "UPDATE 1"


class FakePool:
    def __init__(self, row=None) -> None:
        self.connection = FakeConnection(row)

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def _build_row(*, status: str, storage_path: str | None):
    return {
        "id": "build-1",
        "platform": "android",
        "original_filename": "target.apk",
        "storage_path": storage_path,
        "sha256": "a" * 64,
        "size_bytes": 15,
        "status": status,
        "created_by": "user-1",
        "package_name": None,
        "version_name": None,
        "version_code": None,
    }


async def test_repository_finds_only_verified_checksum() -> None:
    module = import_module(
        "services.api.app.infrastructure.db.platform_build_repository"
    )
    pool = FakePool(_build_row(status="verified", storage_path="aa/build.apk"))
    repository = module.PostgresPlatformBuildRepository(pool=pool)

    build = await repository.find_verified_by_sha256(
        "a" * 64,
        access_scope=TEST_ACCESS_SCOPE,
    )

    query, args = pool.connection.calls[0]
    assert "WHERE sha256 = $1" in query
    assert "status = 'verified'" in query
    assert "tenant_id = $2::uuid" in query
    assert "created_by = $3::uuid" in query
    assert args == (
        "a" * 64,
        "tenant-1",
        "user-1",
    )
    assert build is not None
    assert build.platform_build_ref == "app-build:build-1"


async def test_repository_resolves_only_verified_android_reference() -> None:
    module = import_module(
        "services.api.app.infrastructure.db.platform_build_repository"
    )
    pool = FakePool(_build_row(status="verified", storage_path="aa/build.apk"))
    repository = module.PostgresPlatformBuildRepository(pool=pool)

    build = await repository.get_verified_by_ref("app-build:build-1")

    query, args = pool.connection.calls[0]
    assert "WHERE id = $1::uuid" in query
    assert "platform = 'android'" in query
    assert "status = 'verified'" in query
    assert args == ("build-1",)
    assert build is not None
    assert build.platform_build_ref == "app-build:build-1"


async def test_repository_creates_upload_owned_by_authenticated_user() -> None:
    module = import_module(
        "services.api.app.infrastructure.db.platform_build_repository"
    )
    pool = FakePool(_build_row(status="uploading", storage_path=None))
    repository = module.PostgresPlatformBuildRepository(pool=pool)

    build = await repository.create_upload(
        original_filename="target.apk",
        sha256="a" * 64,
        size_bytes=15,
        created_by="user-1",
        tenant_id="tenant-1",
    )

    query, args = pool.connection.calls[0]
    assert "INSERT INTO qc_platform_build" in query
    assert "'android'" in query
    assert "'uploading'" in query
    assert "$4::uuid" in query
    assert "$5::uuid" in query
    assert args == ("target.apk", "a" * 64, 15, "user-1", "tenant-1")
    assert build.status.value == "uploading"


async def test_repository_marks_upload_verified_once() -> None:
    module = import_module(
        "services.api.app.infrastructure.db.platform_build_repository"
    )
    pool = FakePool(_build_row(status="verified", storage_path="aa/build.apk"))
    repository = module.PostgresPlatformBuildRepository(pool=pool)

    build = await repository.mark_verified(
        build_id="build-1",
        storage_path="aa/build.apk",
        package_name="com.example.app",
        version_name="1.2.3",
        version_code=123,
    )

    query, args = pool.connection.calls[0]
    assert "status = 'verified'" in query
    assert "package_name = $3" in query
    assert "version_name = $4" in query
    assert "version_code = $5" in query
    assert "WHERE id = $1::uuid AND status = 'uploading'" in query
    assert args == (
        "build-1",
        "aa/build.apk",
        "com.example.app",
        "1.2.3",
        123,
    )
    assert build.status.value == "verified"


async def test_repository_records_rejection_reason_only_while_uploading() -> None:
    module = import_module(
        "services.api.app.infrastructure.db.platform_build_repository"
    )
    pool = FakePool()
    repository = module.PostgresPlatformBuildRepository(pool=pool)

    await repository.mark_rejected(
        build_id="build-1",
        reason="APK_MAGIC_INVALID",
    )

    query, args = pool.connection.calls[0]
    assert "status = 'rejected'" in query
    assert "rejection_reason = $2" in query
    assert "WHERE id = $1::uuid AND status = 'uploading'" in query
    assert args == ("build-1", "APK_MAGIC_INVALID")
