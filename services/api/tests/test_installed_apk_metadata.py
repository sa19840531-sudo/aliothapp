"""安装包轻量元数据下载的设备命令与归档安全回归。"""

from __future__ import annotations

import io
import random
import re
import shlex
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from services.api.app.infrastructure.device.google_emulator import GoogleEmulatorError
from services.api.app.infrastructure.device.installed_apk_metadata import (
    download_installed_apk_metadata,
)

_REMOTE_APK_PATH = "/sdcard/Download/odd name's.apk"
_MANIFEST = b"binary manifest bytes"
_RESOURCES = b"binary resources bytes"


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    """用真实 tarfile 生成设备 pull 得到的归档内容。"""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _truncated_gzip_tar_bytes() -> bytes:
    """生成固定随机内容的截断 gzip tar，确保读取成员时触发 EOFError。"""

    rng = random.Random(20260912)
    archive = _tar_bytes(
        {
            "AndroidManifest.xml": rng.randbytes(65536),
            "resources.arsc": rng.randbytes(65536),
        }
    )
    # 去掉压缩流尾部，保留 tar 成员头但破坏 gzip 结束标记。
    return archive[:-1024]


class _FakeAdb:
    def __init__(self, archive: bytes, *, fail_on: str | None = None) -> None:
        self.archive = archive
        self.fail_on = fail_on
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        call = list(args)
        self.calls.append(call)
        command_text = " ".join(call[1:])
        if call and call[0] == "shell":
            if self.fail_on is not None and self.fail_on in command_text:
                return subprocess.CompletedProcess(
                    call,
                    127,
                    stdout="",
                    stderr=f"{self.fail_on}: not found",
                )
            return subprocess.CompletedProcess(call, 0, stdout="", stderr="")
        if call and call[0] == "pull":
            Path(call[-1]).write_bytes(self.archive)
            return subprocess.CompletedProcess(call, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(call, 0, stdout="", stderr="")


def _remote_dirs(adb: _FakeAdb) -> set[str]:
    command_text = " ".join(" ".join(call) for call in adb.calls)
    return set(
        re.findall(
            r"/data/local/tmp/alioth-apk-metadata-[0-9a-f-]+",
            command_text,
        )
    )


def _assert_cleanup(adb: _FakeAdb, remote_dir: str) -> None:
    cleanup_calls = [
        call
        for call in adb.calls
        if call and call[0] == "shell" and "rm -rf" in " ".join(call)
    ]
    assert cleanup_calls
    assert any(remote_dir in " ".join(call) for call in cleanup_calls)


def test_downloads_only_two_metadata_members_and_quotes_remote_apk_path(
    tmp_path: Path,
) -> None:
    """成功时只 pull 压缩元数据包，并将固定两项写成可读 APK。"""

    archive = _tar_bytes(
        {
            "AndroidManifest.xml": _MANIFEST,
            "resources.arsc": _RESOURCES,
            # 归档中的其它成员不得被复制进输出 APK。
            "classes.dex": b"must not be copied",
        }
    )
    adb = _FakeAdb(archive)
    local_apk_path = tmp_path / "metadata.apk"

    assert download_installed_apk_metadata(
        remote_apk_path=_REMOTE_APK_PATH,
        local_apk_path=local_apk_path,
        run_adb=adb,
    ) is True

    with zipfile.ZipFile(local_apk_path) as apk:
        assert apk.namelist() == ["AndroidManifest.xml", "resources.arsc"]
        assert apk.read("AndroidManifest.xml") == _MANIFEST
        assert apk.read("resources.arsc") == _RESOURCES
        assert apk.testzip() is None

    pull_calls = [call for call in adb.calls if call and call[0] == "pull"]
    assert len(pull_calls) == 1
    assert pull_calls[0][1].endswith("/metadata.tar.gz")
    assert _REMOTE_APK_PATH not in pull_calls[0]

    shell_scripts = [
        call[1]
        for call in adb.calls
        if call and call[0] == "shell" and len(call) == 2
    ]
    metadata_script = next(script for script in shell_scripts if "unzip" in script)
    assert "tar -czf " in metadata_script
    # shell 命令解析后路径仍是一个完整参数，空格和单引号不能改变命令边界。
    assert _REMOTE_APK_PATH in shlex.split(metadata_script)
    assert shlex.quote(_REMOTE_APK_PATH) in metadata_script

    remote_dirs = _remote_dirs(adb)
    assert len(remote_dirs) == 1
    remote_dir = next(iter(remote_dirs))
    assert remote_dir.startswith("/data/local/tmp/alioth-apk-metadata-")
    _assert_cleanup(adb, remote_dir)


@pytest.mark.parametrize("unsupported_tool", ["unzip", "tar"])
def test_unsupported_device_tool_returns_false_and_cleans_remote_directory(
    tmp_path: Path,
    unsupported_tool: str,
) -> None:
    adb = _FakeAdb(
        _tar_bytes(
            {
                "AndroidManifest.xml": _MANIFEST,
                "resources.arsc": _RESOURCES,
            }
        ),
        fail_on=unsupported_tool,
    )
    local_apk_path = tmp_path / f"unsupported-{unsupported_tool}.apk"

    assert download_installed_apk_metadata(
        remote_apk_path="/sdcard/input.apk",
        local_apk_path=local_apk_path,
        run_adb=adb,
    ) is False

    assert not local_apk_path.exists()
    remote_dirs = _remote_dirs(adb)
    assert len(remote_dirs) == 1
    _assert_cleanup(adb, next(iter(remote_dirs)))
    assert not any(call and call[0] == "pull" for call in adb.calls)


@pytest.mark.parametrize(
    ("archive", "case_id"),
    [
        (b"not a tar archive", "invalid-tar"),
        (_tar_bytes({"AndroidManifest.xml": _MANIFEST}), "missing-resources"),
    ],
)
def test_invalid_or_incomplete_metadata_tar_returns_false_without_output(
    tmp_path: Path,
    archive: bytes,
    case_id: str,
) -> None:
    adb = _FakeAdb(archive)
    local_apk_path = tmp_path / f"{case_id}.apk"

    assert download_installed_apk_metadata(
        remote_apk_path="/sdcard/input.apk",
        local_apk_path=local_apk_path,
        run_adb=adb,
    ) is False

    assert not local_apk_path.exists()
    remote_dirs = _remote_dirs(adb)
    assert len(remote_dirs) == 1
    _assert_cleanup(adb, next(iter(remote_dirs)))


def test_truncated_gzip_tar_returns_false_and_cleans_remote_directory(
    tmp_path: Path,
) -> None:
    """截断压缩流触发 EOFError 时，必须回退且清理远端目录。"""

    archive = _truncated_gzip_tar_bytes()
    probe = tmp_path / "probe-truncated.tar.gz"
    probe.write_bytes(archive)
    with pytest.raises(EOFError):
        with tarfile.open(probe) as tar:
            tar.getmember("AndroidManifest.xml")

    adb = _FakeAdb(archive)
    local_apk_path = tmp_path / "truncated.apk"

    assert download_installed_apk_metadata(
        remote_apk_path="/sdcard/input.apk",
        local_apk_path=local_apk_path,
        run_adb=adb,
    ) is False

    assert not local_apk_path.exists()
    remote_dirs = _remote_dirs(adb)
    assert len(remote_dirs) == 1
    _assert_cleanup(adb, next(iter(remote_dirs)))


@pytest.mark.parametrize("raise_at", ["shell", "pull"])
def test_google_emulator_error_returns_false_and_attempts_cleanup(
    tmp_path: Path,
    raise_at: str,
) -> None:
    """ADB 控制器异常必须转为回退结果，并始终尝试清理远端目录。"""

    calls: list[list[str]] = []

    def run_adb(args: list[str]) -> subprocess.CompletedProcess[str]:
        call = list(args)
        calls.append(call)
        is_cleanup = call and call[0] == "shell" and "rm -rf" in " ".join(call)
        if call and call[0] == raise_at and not is_cleanup:
            raise GoogleEmulatorError(f"{raise_at} failed")
        return subprocess.CompletedProcess(call, 0, stdout="", stderr="")

    local_apk_path = tmp_path / f"adb-{raise_at}.apk"
    assert download_installed_apk_metadata(
        remote_apk_path="/sdcard/input.apk",
        local_apk_path=local_apk_path,
        run_adb=run_adb,
    ) is False

    assert not local_apk_path.exists()
    cleanup_calls = [
        call
        for call in calls
        if call and call[0] == "shell" and "rm -rf" in " ".join(call)
    ]
    assert cleanup_calls


def test_cleanup_google_emulator_error_does_not_override_success(
    tmp_path: Path,
) -> None:
    """成功生成元数据后清理异常不应覆盖成功结果。"""

    adb = _FakeAdb(
        _tar_bytes(
            {
                "AndroidManifest.xml": _MANIFEST,
                "resources.arsc": _RESOURCES,
            }
        )
    )
    original_call = adb.__call__

    def run_adb(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "shell" and "rm -rf" in " ".join(args):
            adb.calls.append(list(args))
            raise GoogleEmulatorError("cleanup failed")
        return original_call(args)

    local_apk_path = tmp_path / "cleanup-error.apk"
    assert download_installed_apk_metadata(
        remote_apk_path="/sdcard/input.apk",
        local_apk_path=local_apk_path,
        run_adb=run_adb,
    ) is True

    with zipfile.ZipFile(local_apk_path) as apk:
        assert apk.namelist() == ["AndroidManifest.xml", "resources.arsc"]
    remote_dirs = _remote_dirs(adb)
    assert len(remote_dirs) == 1
    _assert_cleanup(adb, next(iter(remote_dirs)))
