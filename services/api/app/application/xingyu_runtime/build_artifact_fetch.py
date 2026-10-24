"""依 platform_build_ref 解析本機／快取 APK，並校驗 sha256。"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

# 青雲 PostgresPlatformBuildRepository 約定：app-build:<uuid>
_APP_BUILD_REF = re.compile(
    r"^app-build:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


class BuildArtifactError(Exception):
    """構建制品解析或校驗失敗。"""

    def __init__(self, message: str, *, error_code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


def resolve_build_cache_dir() -> Path | None:
    raw = (os.getenv("ALIOTH_XINGYU_BUILD_CACHE") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def resolve_platform_build_storage_root() -> Path:
    """青雲 LocalPlatformBuildStore 根目录（与 ALIOTH_PLATFORM_BUILD_STORAGE_ROOT 对齐）。"""
    raw = (os.getenv("ALIOTH_PLATFORM_BUILD_STORAGE_ROOT") or "").strip()
    if not raw:
        raw = ".alioth/platform-builds"
    return Path(raw).expanduser().resolve()


def _normalize_sha256(raw: str | None) -> str | None:
    digest = (raw or "").strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return None
    return digest


def _app_build_id(platform_build_ref: str) -> str | None:
    match = _APP_BUILD_REF.fullmatch(platform_build_ref.strip())
    if match is None:
        return None
    return match.group(1).lower()


def _content_addressed_apk(root: Path, sha256: str) -> Path:
    """LocalPlatformBuildStore 约定：``{sha256[:2]}/{sha256}.apk``。"""
    return root / sha256[:2] / f"{sha256}.apk"


def _cache_candidates(cache: Path, ref: str) -> list[Path]:
    """快取目錄內的常見落盤命名（聯調可任選其一）。"""
    names: list[Path] = [
        cache / ref,
        cache / f"{ref}.apk",
        cache / Path(ref).name,
    ]
    build_id = _app_build_id(ref)
    if build_id is not None:
        names.extend(
            [
                cache / f"{build_id}.apk",
                cache / f"app-build-{build_id}.apk",
                cache / f"app-build_{build_id}.apk",
                cache / build_id / "base.apk",
                cache / build_id / "app.apk",
                cache / "app-build" / f"{build_id}.apk",
            ]
        )
    # 去重並保持順序
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in names:
        resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)
    return ordered


def _resolve_storage_path_from_db(build_id: str) -> str | None:
    """同步回查 qc_platform_build.storage_path（同进程 Official / Worker）。"""
    database_url = (os.getenv("ALIOTH_DATABASE_URL") or "").strip()
    if not database_url:
        return None
    sync_url = database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _fetch() -> str | None:
        import asyncpg

        connection = await asyncpg.connect(sync_url, timeout=5)
        try:
            value = await connection.fetchval(
                """
                SELECT storage_path
                FROM qc_platform_build
                WHERE id = $1::uuid
                  AND platform = 'android'
                  AND status = 'verified'
                """,
                build_id,
            )
        finally:
            await connection.close()
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(_fetch())
        except Exception:  # noqa: BLE001
            return None
    # 已在事件循环内时避免嵌套 run；依赖 sha256 内容寻址路径
    return None


def resolve_apk_path(
    platform_build_ref: str,
    *,
    sha256: str | None = None,
) -> Path:
    """解析 APK 路径。

    顺序：绝对路径 → 青雲 platform-builds 内容寻址（sha256）→
    DB ``storage_path`` → 星语 BUILD_CACHE 别名。
    """
    ref = (platform_build_ref or "").strip()
    if not ref:
        raise BuildArtifactError(
            "platform_build_ref 為空",
            error_code="INSTALL_ARTIFACT_NOT_FOUND",
            retryable=False,
        )
    direct = Path(ref).expanduser()
    if direct.is_file():
        return direct.resolve()

    platform_root = resolve_platform_build_storage_root()
    digest = _normalize_sha256(sha256)
    if digest is not None:
        content_path = _content_addressed_apk(platform_root, digest)
        if content_path.is_file():
            return content_path.resolve()

    build_id = _app_build_id(ref)
    if build_id is not None:
        storage_path = _resolve_storage_path_from_db(build_id)
        if storage_path:
            candidate = (platform_root / storage_path).resolve()
            try:
                candidate.relative_to(platform_root)
            except ValueError as exc:
                raise BuildArtifactError(
                    "storage_path 越出 platform-builds 根目录",
                    error_code="INSTALL_ARTIFACT_NOT_FOUND",
                    retryable=False,
                ) from exc
            if candidate.is_file():
                return candidate

    cache = resolve_build_cache_dir()
    if cache is not None:
        for candidate in _cache_candidates(cache, ref):
            if candidate.is_file():
                return candidate.resolve()

    raise BuildArtifactError(
        f"找不到 APK：platform_build_ref={ref}",
        error_code="INSTALL_ARTIFACT_NOT_FOUND",
        retryable=False,
    )


def verify_sha256(path: Path, expected_sha256: str) -> str:
    """計算並校驗檔案 sha256；不符拋 ``INSTALL_CHECKSUM_MISMATCH``。"""
    expected = (expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise BuildArtifactError(
            "sha256 必須為 64 位十六進制",
            error_code="INSTALL_CHECKSUM_INVALID",
            retryable=False,
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise BuildArtifactError(
            "APK sha256 與請求不符",
            error_code="INSTALL_CHECKSUM_MISMATCH",
            retryable=False,
        )
    return actual
