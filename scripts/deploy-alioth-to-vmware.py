#!/usr/bin/env python3
"""將本機 F:\\alioth 同步到 VMware Ubuntu 訪客 ~/alioth。

用法（Windows）:
  python scripts/deploy-alioth-to-vmware.py

環境變數（可選）:
  ALIOTH_VM_HOST / ALIOTH_VM_USER / ALIOTH_VM_PASSWORD
"""
from __future__ import annotations

import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
HOST = os.getenv("ALIOTH_VM_HOST", "192.168.33.128")
USER = os.getenv("ALIOTH_VM_USER", "eevee")
PASSWORD = os.getenv("ALIOTH_VM_PASSWORD", "1215627")
REMOTE_DIR = "/home/eevee/alioth"

EXCLUDE_DIR_NAMES = {
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "arm64-vm",
    "runs",
    ".git",
    ".alioth-mobile-demo",
    ".tmp_docs",
    ".tmp_docker_recovery",
    "extracted_media",
    "dist",
    "build",
    ".cursor",
}
EXCLUDE_FILE_SUFFIXES = {".pyc", ".pyo", ".apk", ".tgz", ".tar", ".zip", ".7z", ".iso", ".vmdk", ".vdi"}


def _should_exclude(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = set(rel.parts)
    if parts & EXCLUDE_DIR_NAMES:
        return True
    if any(p.startswith(".tmp") for p in rel.parts):
        return True
    if path.suffix.lower() in EXCLUDE_FILE_SUFFIXES:
        return True
    name = path.name
    if name.startswith(".tmp_") or name.startswith("tmp_"):
        return True
    # 部署產物本身不回灌
    if rel.as_posix().startswith(".alioth-mobile-demo/"):
        return True
    return False


def build_tarball(tar_path: Path) -> None:
    with tarfile.open(tar_path, "w:gz", format=tarfile.GNU_FORMAT) as tar:
        for p in ROOT.rglob("*"):
            if not p.is_file():
                continue
            if _should_exclude(p, ROOT):
                continue
            tar.add(p, arcname=p.relative_to(ROOT).as_posix())


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "alioth-deploy.tgz"
        print(f"packing {ROOT} ...")
        build_tarball(tar_path)
        size = tar_path.stat().st_size
        print(f"tarball {size} bytes")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            HOST,
            username=USER,
            password=PASSWORD,
            timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )
        remote_tar = f"/home/{USER}/alioth-deploy.tgz"
        print(f"upload -> {HOST}:{remote_tar}")
        sftp = client.open_sftp()
        last = [0]
        t0 = time.time()

        def cb(transferred: int, total: int) -> None:
            if transferred - last[0] >= 8 * 1024 * 1024 or transferred == total:
                last[0] = transferred
                pct = 100.0 * transferred / total if total else 0
                print(f"  {transferred}/{total} ({pct:.1f}%)")

        sftp.put(str(tar_path), remote_tar, callback=cb)
        sftp.close()
        print(f"upload_done {time.time() - t0:.1f}s")

        cmds = [
            f"rm -rf {REMOTE_DIR} && mkdir -p {REMOTE_DIR}",
            f"tar -xzf {remote_tar} -C {REMOTE_DIR}",
            f"rm -f {remote_tar}",
            f"test -f {REMOTE_DIR}/pyproject.toml && echo SYNC_OK",
            f"du -sh {REMOTE_DIR}",
        ]
        for cmd in cmds:
            print("====", cmd)
            _, stdout, stderr = client.exec_command(cmd, timeout=180)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            # GNU tar 對 Windows 屬性警告可忽略
            if out.strip():
                print(out.strip())
            if err.strip() and "SCHILY.fflags" not in err:
                print(err.strip())
        client.close()
    print("done")


if __name__ == "__main__":
    main()
