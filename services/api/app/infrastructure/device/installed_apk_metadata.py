"""只传输安装包的清单与资源索引，供本地 aapt 读取应用名称。"""

from __future__ import annotations

import logging
import shlex
import subprocess
import tarfile
import tempfile
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)
_METADATA_MEMBERS = ("AndroidManifest.xml", "resources.arsc")


def download_installed_apk_metadata(
    *,
    remote_apk_path: str,
    local_apk_path: Path,
    run_adb: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> bool:
    """设备端提取两个元数据文件；不支持时返回 False，由调用者保留整包回退。"""

    remote_dir = f"/data/local/tmp/alioth-apk-metadata-{uuid4().hex}"
    remote_archive = f"{remote_dir}/metadata.tar.gz"
    quote = shlex.quote
    commands = [f"mkdir -p {quote(remote_dir)}"]
    commands.extend(
        f"unzip -p {quote(remote_apk_path)} {quote(name)} > {quote(f'{remote_dir}/{name}')}"
        for name in _METADATA_MEMBERS
    )
    commands.append(
        f"tar -czf {quote(remote_archive)} -C {quote(remote_dir)} "
        + " ".join(map(quote, _METADATA_MEMBERS))
    )
    try:
        if run_adb(["shell", " && ".join(commands)]).returncode != 0:
            return False
        with tempfile.TemporaryDirectory(prefix="alioth-apk-metadata-") as temp_dir:
            archive_path = Path(temp_dir) / "metadata.tar.gz"
            if run_adb(["pull", remote_archive, str(archive_path)]).returncode != 0:
                return False
            # 不解压远端 tar 路径，只读取两个固定成员，避免写入任意本地路径。
            with tarfile.open(archive_path) as archive:
                members: dict[str, bytes] = {}
                for name in _METADATA_MEMBERS:
                    member = archive.getmember(name)
                    if not member.isfile() or member.size <= 0:
                        return False
                    stream = archive.extractfile(member)
                    if stream is None:
                        return False
                    with stream:
                        members[name] = stream.read()
            metadata_apk = Path(temp_dir) / "metadata.apk"
            with zipfile.ZipFile(metadata_apk, "w") as apk:
                for name, content in members.items():
                    apk.writestr(name, content)
            metadata_apk.replace(local_apk_path)
        return True
    except (
        OSError,
        EOFError,
        RuntimeError,
        subprocess.SubprocessError,
        tarfile.TarError,
        zlib.error,
        KeyError,
        ValueError,
    ):
        # 控制器以 RuntimeError 子类报告 ADB 失败，截断压缩流也必须保留整包回退。
        logger.info("安装包轻量元数据读取失败，将回退完整 APK", exc_info=True)
        return False
    finally:
        try:
            run_adb(["shell", f"rm -rf {quote(remote_dir)}"])
        except (OSError, subprocess.SubprocessError, RuntimeError):
            logger.info("安装包元数据临时目录清理失败", exc_info=True)
