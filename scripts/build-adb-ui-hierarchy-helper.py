"""用本地 Android SDK/JDK 重建一次性 UIA 工具；运行时不需要编译工具。"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, type=Path)
    parser.add_argument("--build-tools", required=True, type=Path)
    parser.add_argument("--java-home", required=True, type=Path)
    args = parser.parse_args()
    directory = (
        Path(__file__).resolve().parents[1]
        / "services/api/app/infrastructure/device/android"
    )
    libraries = [args.platform / path for path in (
        "uiautomator.jar", "android.jar", "optional/android.test.base.jar",
        "optional/android.test.runner.jar",
    )]
    with tempfile.TemporaryDirectory(prefix="alioth-uia-build-") as temporary:
        root = Path(temporary)
        classes, dex = root / "classes", root / "dex"
        classes.mkdir()
        dex.mkdir()
        subprocess.run([
            str(args.java_home / "bin/javac"), "--release", "8", "-g:none",
            "-cp", os.pathsep.join(map(str, libraries)), "-d", str(classes),
            str(directory / "CurrentHierarchy.java"),
        ], check=True)
        subprocess.run([
            str(args.build_tools / "d8"), "--min-api", "18",
            "--lib", str(args.platform / "android.jar"),
            "--lib", str(args.platform / "uiautomator.jar"),
            "--output", str(dex),
            str(classes / "com/alioth/observation/CurrentHierarchy.class"),
        ], env={**os.environ, "JAVA_HOME": str(args.java_home)}, check=True)
        destination = directory / "current_hierarchy.jar"
        # 固定 ZIP 元数据，源码及工具版本相同时可逐字节复现。
        entry = zipfile.ZipInfo("classes.dex", date_time=(1980, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr(entry, (dex / "classes.dex").read_bytes())
        print(f"{destination}: sha256={hashlib.sha256(destination.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
