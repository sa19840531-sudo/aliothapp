"""在遠端 Linux 安裝並啟動 ReDroid（一次性運維腳本）。"""

from __future__ import annotations

import os
import sys
import time

import paramiko

HOST = "192.46.230.40"
USER = "root"
PASSWORD = os.environ.get("ALIOTH_REMOTE_ROOT_PASSWORD", "")



def run(client: paramiko.SSHClient, cmd: str, timeout: int = 900) -> tuple[int, str]:
    print("=====", cmd[:160], flush=True)
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    text = out + err
    if len(text) > 6000:
        print(text[-6000:], flush=True)
    else:
        print(text, flush=True)
    print("exit", code, flush=True)
    return code, text


def main() -> int:
    if not PASSWORD:
        print("請設定環境變數 ALIOTH_REMOTE_ROOT_PASSWORD", flush=True)
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
        run(
            client,
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update -qq && "
            "apt-get install -y linux-modules-extra-$(uname -r) "
            "android-sdk-platform-tools unzip curl ca-certificates 2>&1 | tail -60",
        )
        run(client, "which adb; adb version | head -3")
        run(
            client,
            'modprobe binder_linux devices="binder,hwbinder,vndbinder" 2>&1 || true; '
            "mkdir -p /dev/binderfs; "
            "mountpoint -q /dev/binderfs || mount -t binder binder /dev/binderfs; "
            "ls /dev/binderfs",
        )
        run(client, "mkdir -p /opt/redroid/data; docker rm -f redroid 2>/dev/null || true")
        code, _ = run(
            client,
            "docker pull redroid/redroid:12.0.0_64only-latest 2>&1 | tail -30",
            timeout=1200,
        )
        if code != 0:
            return code
        run(
            client,
            "docker run -itd --privileged --name redroid "
            "-v /opt/redroid/data:/data "
            "-p 5555:5555 "
            "redroid/redroid:12.0.0_64only-latest "
            "androidboot.redroid_gpu_mode=guest "
            "androidboot.use_memfd=true "
            "androidboot.redroid_width=1080 "
            "androidboot.redroid_height=1920 "
            "androidboot.redroid_dpi=320",
        )
        run(
            client,
            'docker ps -a --filter name=redroid --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"',
        )
        # wait boot via adb
        run(client, "adb kill-server 2>/dev/null || true; adb connect 127.0.0.1:5555")
        ready = False
        for i in range(60):
            _, text = run(
                client,
                "adb connect 127.0.0.1:5555 >/dev/null; "
                "adb -s 127.0.0.1:5555 shell getprop sys.boot_completed 2>/dev/null; "
                "adb -s 127.0.0.1:5555 get-state 2>/dev/null",
                timeout=60,
            )
            if "1" in text.splitlines()[:3] or "device" in text:
                # stricter check
                _, boot = run(
                    client,
                    "adb -s 127.0.0.1:5555 shell getprop sys.boot_completed",
                    timeout=30,
                )
                if boot.strip() == "1":
                    ready = True
                    print("BOOT_OK", flush=True)
                    break
            print(f"waiting boot {i}...", flush=True)
            time.sleep(5)
            # show container logs if unhealthy
            if i in (10, 20, 40):
                run(client, "docker logs --tail 40 redroid 2>&1 || true", timeout=60)
        if not ready:
            run(client, "docker logs --tail 80 redroid 2>&1 || true")
            run(client, "docker inspect redroid --format '{{.State.Status}} {{.State.Error}}'")
            return 1
        run(client, "adb -s 127.0.0.1:5555 shell getprop ro.product.cpu.abilist")
        run(client, "adb -s 127.0.0.1:5555 shell wm size")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
