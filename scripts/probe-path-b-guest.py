#!/usr/bin/env python3
"""Path B：探測 VMware 訪客 KVM / binder / Docker。"""
from __future__ import annotations

import os
import sys

import paramiko

HOST = os.environ.get("ALIOTH_VM_HOST", "192.168.33.128")
USER = os.environ.get("ALIOTH_VM_USER", "eevee")
PASSWORD = os.environ.get("ALIOTH_VM_PASSWORD", "1215627")

CMDS = [
    "uname -a",
    "ls -l /dev/kvm 2>&1 || echo NO_KVM",
    "egrep -c '(vmx|svm)' /proc/cpuinfo || true",
    "lsmod | egrep 'binder|kvm' || true",
    "mountpoint -q /dev/binderfs && echo BINDERFS_OK || echo NO_BINDERFS",
    "docker --version 2>&1 || echo NO_DOCKER",
    "free -h | head -2; nproc",
    "ip -4 -br a | head -10",
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            HOST,
            username=USER,
            password=PASSWORD,
            timeout=20,
            allow_agent=False,
            look_for_keys=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"SSH_FAIL {type(exc).__name__}: {exc}")
        return 1

    print(f"SSH_OK {USER}@{HOST}")
    for cmd in CMDS:
        print(f"==== {cmd}")
        _, stdout, stderr = client.exec_command(cmd, timeout=60)
        out = (stdout.read() + stderr.read()).decode("utf-8", "replace")
        print(out.rstrip())
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
