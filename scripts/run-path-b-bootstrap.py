#!/usr/bin/env python3
"""Path B：部署後在訪客執行 bootstrap（Docker + binder + ReDroid + 主站）。"""
from __future__ import annotations

import os
import sys
import time

import paramiko

HOST = os.environ.get("ALIOTH_VM_HOST", "192.168.33.128")
USER = os.environ.get("ALIOTH_VM_USER", "eevee")
PASSWORD = os.environ.get("ALIOTH_VM_PASSWORD", "1215627")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
    print("running bootstrap on guest (may take several minutes)...")
    transport = client.get_transport()
    assert transport is not None
    channel = transport.open_session()
    channel.get_pty()
    channel.settimeout(None)
    # 用 sudo -S
    boot = (
        "set -euo pipefail\n"
        f"echo '{PASSWORD}' | sudo -S -v\n"
        "cd /home/eevee/alioth\n"
        "chmod +x scripts/*.sh || true\n"
        "sed -i 's/\\r$//' scripts/linux-bootstrap-path-b.sh "
        "scripts/linux-prepare-redroid-host.sh scripts/start-docker-emulator-stack.sh "
        "scripts/verify-docker-emulator.sh || true\n"
        "bash scripts/linux-bootstrap-path-b.sh\n"
    )
    channel.exec_command("bash -lc " + repr(boot))
    # Better: write remote script
    sftp = client.open_sftp()
    remote = "/tmp/path_b_boot.sh"
    with sftp.file(remote, "w") as f:
        f.write(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            f"echo '{PASSWORD}' | sudo -S -v\n"
            "cd /home/eevee/alioth\n"
            "chmod +x scripts/*.sh || true\n"
            "sed -i 's/\\r$//' scripts/*.sh || true\n"
            "bash scripts/linux-bootstrap-path-b.sh\n"
        )
    sftp.chmod(remote, 0o755)
    sftp.close()

    stdin, stdout, stderr = client.exec_command(f"bash {remote}", get_pty=True, timeout=None)
    # 讀到結束
    while True:
        if stdout.channel.recv_ready():
            chunk = stdout.channel.recv(4096).decode("utf-8", "replace")
            sys.stdout.write(chunk)
            sys.stdout.flush()
        if stdout.channel.exit_status_ready():
            # drain
            while stdout.channel.recv_ready():
                chunk = stdout.channel.recv(4096).decode("utf-8", "replace")
                sys.stdout.write(chunk)
                sys.stdout.flush()
            break
        time.sleep(0.2)
    rc = stdout.channel.recv_exit_status()
    err = stderr.read().decode("utf-8", "replace")
    if err:
        sys.stderr.write(err)
    client.close()
    print(f"\nBOOTSTRAP_EXIT={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
