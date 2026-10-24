from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import replace
from importlib import import_module
from pathlib import Path

import pytest

from services.api.app.application.apk_identity import ApkIdentity
from services.api.app.domain.iam.access_scope import AccessScope

TEST_ACCESS_SCOPE = AccessScope(
    actor_user_id="test-user",
    tenant_id="tenant-1",
    permissions=frozenset({"taskObjects:read:any"}),
)


async def _chunks(payload: bytes, *, chunk_size: int = 5):
    for offset in range(0, len(payload), chunk_size):
        yield payload[offset : offset + chunk_size]


def _apk_bytes_with_package(package_name: str) -> bytes:
    """构造含 UTF-16LE 包名的最小 ZIP，供 Manifest 扫描回退命中。"""
    buffer = io.BytesIO()
    encoded = package_name.encode("utf-16le")
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"\x00\x00" + encoded + b"\x00\x00")
    return buffer.getvalue()


def _fixed_identity(package_name: str = "com.example.demo") -> ApkIdentity:
    return ApkIdentity(
        package_name=package_name,
        version_name="1.0.0",
        version_code=1,
    )


class InMemoryPlatformBuildRepository:
    def __init__(self, domain_module) -> None:
        self._domain = domain_module
        self.existing = None
        self.pending = None
        self.rejected: list[tuple[str, str]] = []
        self.created_count = 0
        self.last_verified_identity: dict[str, object] | None = None

    async def find_verified_by_sha256(self, sha256: str, *, access_scope: AccessScope):
        assert access_scope is not None
        if self.existing is not None and self.existing.sha256 == sha256:
            return self.existing
        return None

    async def create_upload(
        self,
        *,
        original_filename: str,
        sha256: str,
        size_bytes: int,
        created_by: str,
        tenant_id: str,
    ):
        assert tenant_id == "tenant-1"
        self.created_count += 1
        self.pending = self._domain.PlatformBuild(
            build_id="build-1",
            platform="android",
            original_filename=original_filename,
            storage_path=None,
            sha256=sha256,
            size_bytes=size_bytes,
            status=self._domain.PlatformBuildStatus.UPLOADING,
            created_by=created_by,
        )
        return self.pending

    async def mark_verified(
        self,
        *,
        build_id: str,
        storage_path: str,
        package_name: str | None = None,
        version_name: str | None = None,
        version_code: int | None = None,
    ):
        assert self.pending is not None and self.pending.build_id == build_id
        self.last_verified_identity = {
            "package_name": package_name,
            "version_name": version_name,
            "version_code": version_code,
        }
        self.pending = replace(
            self.pending,
            storage_path=storage_path,
            status=self._domain.PlatformBuildStatus.VERIFIED,
            package_name=package_name,
            version_name=version_name,
            version_code=version_code,
        )
        return self.pending

    async def mark_rejected(self, *, build_id: str, reason: str) -> None:
        self.rejected.append((build_id, reason))


async def test_upload_apk_streams_to_checksum_addressed_storage(tmp_path) -> None:
    application = import_module("services.api.app.application.platform_builds")
    domain = import_module("services.api.app.domain.task.platform_build")
    storage = import_module(
        "services.api.app.infrastructure.storage.local_platform_build_store"
    )
    repository = InMemoryPlatformBuildRepository(domain)
    service = application.PlatformBuildService(
        repository=repository,
        store=storage.LocalPlatformBuildStore(root=tmp_path),
        max_size_bytes=1024,
        identity_extractor=lambda _path: _fixed_identity(),
    )
    payload = b"PK\x03\x04" + b"verified-apk"

    build = await service.upload_apk(
        original_filename="target.apk",
        chunks=_chunks(payload),
        access_scope=TEST_ACCESS_SCOPE,
    )

    expected_sha256 = hashlib.sha256(payload).hexdigest()
    assert build.platform_build_ref == "app-build:build-1"
    assert build.sha256 == expected_sha256
    assert build.status is domain.PlatformBuildStatus.VERIFIED
    assert build.package_name == "com.example.demo"
    assert build.version_name == "1.0.0"
    assert build.version_code == 1
    assert build.storage_path == f"{expected_sha256[:2]}/{expected_sha256}.apk"
    assert (tmp_path / build.storage_path).read_bytes() == payload
    assert list((tmp_path / ".staging").glob("*.part")) == []


async def test_upload_apk_writes_source_url_sidecar(tmp_path) -> None:
    """带原始直链上传时写 <apk>.srcurl，供云机自行下载绕开远端 ADB 慢传。"""
    application = import_module("services.api.app.application.platform_builds")
    domain = import_module("services.api.app.domain.task.platform_build")
    storage = import_module(
        "services.api.app.infrastructure.storage.local_platform_build_store"
    )
    service = application.PlatformBuildService(
        repository=InMemoryPlatformBuildRepository(domain),
        store=storage.LocalPlatformBuildStore(root=tmp_path),
        max_size_bytes=1024,
        identity_extractor=lambda _path: _fixed_identity(),
    )

    build = await service.upload_apk(
        original_filename="target.apk",
        chunks=_chunks(b"PK\x03\x04" + b"verified-apk"),
        access_scope=TEST_ACCESS_SCOPE,
        source_url="https://cdn.example.com/app/target.apk",
    )

    sidecar = tmp_path / f"{build.storage_path}.srcurl"
    assert sidecar.read_text(encoding="utf-8") == "https://cdn.example.com/app/target.apk"


async def test_upload_apk_rejects_non_http_source_url(tmp_path) -> None:
    application = import_module("services.api.app.application.platform_builds")
    domain = import_module("services.api.app.domain.task.platform_build")
    storage = import_module(
        "services.api.app.infrastructure.storage.local_platform_build_store"
    )
    service = application.PlatformBuildService(
        repository=InMemoryPlatformBuildRepository(domain),
        store=storage.LocalPlatformBuildStore(root=tmp_path),
        max_size_bytes=1024,
        identity_extractor=lambda _path: _fixed_identity(),
    )

    with pytest.raises(application.PlatformBuildValidationError, match="source_url"):
        await service.upload_apk(
            original_filename="target.apk",
            chunks=_chunks(b"PK\x03\x04" + b"payload"),
            access_scope=TEST_ACCESS_SCOPE,
            source_url="file:///etc/passwd",
        )


async def test_upload_apk_persists_package_from_manifest_scan(tmp_path) -> None:
    application = import_module("services.api.app.application.platform_builds")
    domain = import_module("services.api.app.domain.task.platform_build")
    storage = import_module(
        "services.api.app.infrastructure.storage.local_platform_build_store"
    )
    repository = InMemoryPlatformBuildRepository(domain)
    service = application.PlatformBuildService(
        repository=repository,
        store=storage.LocalPlatformBuildStore(root=tmp_path),
        max_size_bytes=64_000,
    )
    payload = _apk_bytes_with_package("com.edxuf.euloop")

    build = await service.upload_apk(
        original_filename="hlapp.apk",
        chunks=_chunks(payload, chunk_size=1024),
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert build.status is domain.PlatformBuildStatus.VERIFIED
    assert build.package_name == "com.edxuf.euloop"
    assert repository.last_verified_identity == {
        "package_name": "com.edxuf.euloop",
        "version_name": None,
        "version_code": None,
    }


async def test_upload_apk_rejects_when_package_cannot_be_resolved(tmp_path) -> None:
    application = import_module("services.api.app.application.platform_builds")
    domain = import_module("services.api.app.domain.task.platform_build")
    storage = import_module(
        "services.api.app.infrastructure.storage.local_platform_build_store"
    )
    repository = InMemoryPlatformBuildRepository(domain)
    service = application.PlatformBuildService(
        repository=repository,
        store=storage.LocalPlatformBuildStore(root=tmp_path),
        max_size_bytes=1024,
        identity_extractor=lambda _path: ApkIdentity(
            package_name=None,
            version_name=None,
            version_code=None,
        ),
    )

    with pytest.raises(application.PlatformBuildValidationError, match="package_name"):
        await service.upload_apk(
            original_filename="target.apk",
            chunks=_chunks(b"PK\x03\x04" + b"no-package"),
            access_scope=TEST_ACCESS_SCOPE,
        )

    assert repository.rejected == [("build-1", "APK_PACKAGE_UNRESOLVED")]


async def test_local_store_stage_closes_file_handle_before_return(
    tmp_path,
    monkeypatch,
) -> None:
    storage = import_module(
        "services.api.app.infrastructure.storage.local_platform_build_store"
    )
    opened_handles = []
    original_open = Path.open

    def tracking_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        opened_handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)
    store = storage.LocalPlatformBuildStore(root=tmp_path)

    await store.stage(_chunks(b"PK\x03\x04tracked"), max_size_bytes=1024)

    assert len(opened_handles) == 1
    assert opened_handles[0].closed is True


async def test_upload_apk_rejects_invalid_zip_magic_and_cleans_staging(tmp_path) -> None:
    application = import_module("services.api.app.application.platform_builds")
    domain = import_module("services.api.app.domain.task.platform_build")
    storage = import_module(
        "services.api.app.infrastructure.storage.local_platform_build_store"
    )
    repository = InMemoryPlatformBuildRepository(domain)
    service = application.PlatformBuildService(
        repository=repository,
        store=storage.LocalPlatformBuildStore(root=tmp_path),
        max_size_bytes=1024,
        identity_extractor=lambda _path: _fixed_identity(),
    )

    with pytest.raises(application.PlatformBuildValidationError, match="APK/ZIP"):
        await service.upload_apk(
            original_filename="target.apk",
            chunks=_chunks(b"not-an-apk"),
            access_scope=TEST_ACCESS_SCOPE,
        )

    assert repository.rejected == [("build-1", "APK_MAGIC_INVALID")]
    assert list(tmp_path.rglob("*.part")) == []
    assert list(tmp_path.rglob("*.apk")) == []


async def test_upload_apk_reuses_verified_checksum_without_second_database_row(
    tmp_path,
) -> None:
    application = import_module("services.api.app.application.platform_builds")
    domain = import_module("services.api.app.domain.task.platform_build")
    storage = import_module(
        "services.api.app.infrastructure.storage.local_platform_build_store"
    )
    payload = b"PK\x03\x04" + b"same-apk"
    sha256 = hashlib.sha256(payload).hexdigest()
    repository = InMemoryPlatformBuildRepository(domain)
    repository.existing = domain.PlatformBuild(
        build_id="existing-build",
        platform="android",
        original_filename="existing.apk",
        storage_path=f"{sha256[:2]}/{sha256}.apk",
        sha256=sha256,
        size_bytes=len(payload),
        status=domain.PlatformBuildStatus.VERIFIED,
        created_by="user-0",
        package_name="com.example.existing",
    )
    service = application.PlatformBuildService(
        repository=repository,
        store=storage.LocalPlatformBuildStore(root=tmp_path),
        max_size_bytes=1024,
        identity_extractor=lambda _path: _fixed_identity(),
    )

    build = await service.upload_apk(
        original_filename="duplicate.apk",
        chunks=_chunks(payload),
        access_scope=TEST_ACCESS_SCOPE,
    )

    assert build.build_id == "existing-build"
    assert build.package_name == "com.example.existing"
    assert repository.created_count == 0
    assert list(tmp_path.rglob("*.part")) == []


async def test_upload_apk_rejects_size_over_configured_limit(tmp_path) -> None:
    application = import_module("services.api.app.application.platform_builds")
    domain = import_module("services.api.app.domain.task.platform_build")
    storage = import_module(
        "services.api.app.infrastructure.storage.local_platform_build_store"
    )
    service = application.PlatformBuildService(
        repository=InMemoryPlatformBuildRepository(domain),
        store=storage.LocalPlatformBuildStore(root=tmp_path),
        max_size_bytes=8,
        identity_extractor=lambda _path: _fixed_identity(),
    )

    with pytest.raises(application.PlatformBuildValidationError, match="大小上限"):
        await service.upload_apk(
            original_filename="too-large.apk",
            chunks=_chunks(b"PK\x03\x04oversized"),
            access_scope=TEST_ACCESS_SCOPE,
        )

    assert list(tmp_path.rglob("*.part")) == []
