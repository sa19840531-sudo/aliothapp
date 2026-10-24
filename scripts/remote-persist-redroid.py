"""在遠端寫入 ReDroid 常駐啟動腳本。"""

from __future__ import annotations

import os
import sys
import textwrap

import paramiko

HOST = "192.46.230.40"
USER = "root"
PASSWORD = os.environ.get("ALIOTH_REMOTE_ROOT_PASSWORD", "")

START_SH = textwrap.dedent(
    """\
    #!/bin/bash
    set -euo pipefail
    modprobe binder_linux devices="binder,hwbinder,vndbinder" || true
    mkdir -p /dev/binderfs
    mountpoint -q /dev/binderfs || mount -t binder binder /dev/binderfs
    mkdir -p /opt/redroid/data
    docker rm -f redroid 2>/dev/null || true
    docker run -itd --privileged --name redroid \\
      --restart unless-stopped \\
      --memory=4g --cpus=4 \\
      -v /opt/redroid/data:/data \\
      -p 5555:5555 \\
      redroid/redroid:12.0.0_64only-latest \\
      androidboot.redroid_gpu_mode=guest \\
      androidboot.use_memfd=true \\
      androidboot.redroid_width=1080 \\
      androidboot.redroid_height=1920 \\
      androidboot.redroid_dpi=320 \\
      androidboot.redroid_fps=30
    echo "redroid started; adb connect 127.0.0.1:5555 或 adb connect <公網IP>:5555"
    """
)


def main() -> int:
    if not PASSWORD:
        print("缺少 ALIOTH_REMOTE_ROOT_PASSWORD")
        return 2
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
    try:
        sftp = client.open_sftp()
        with sftp.file("/opt/redroid/start.sh", "w") as f:
            f.write(START_SH)
        sftp.chmod("/opt/redroid/start.sh", 0o755)
        sftp.close()
        _, stdout, _ = client.exec_command(
            "docker update --restart unless-stopped redroid 2>&1; "
            "docker ps --filter name=redroid --format '{{.Names}} {{.Status}} {{.Ports}}'; "
            "ss -lntp | grep 5555 || netstat -lntp 2>/dev/null | grep 5555 || true",
            timeout=60,
        )
        print(stdout.read().decode("utf-8", "replace"))
        print("wrote /opt/redroid/start.sh")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
