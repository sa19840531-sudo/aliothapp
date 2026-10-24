"""重啟遠端 ReDroid 並等待 boot。"""

from __future__ import annotations

import os
import sys
import time

import paramiko

HOST = "192.46.230.40"
PASSWORD = os.environ.get("ALIOTH_REMOTE_ROOT_PASSWORD", "")


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 180) -> tuple[int, str]:
    print("=====", cmd[:160], flush=True)
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    text = (stdout.read() + stderr.read()).decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if text.strip():
        print(text[-4000:] if len(text) > 4000 else text, flush=True)
    return code, text


def main() -> int:
    if not PASSWORD:
        print("缺少 ALIOTH_REMOTE_ROOT_PASSWORD")
        return 2
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username="root",
        password=PASSWORD,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        run(client, 'docker ps -a --filter name=redroid --format "{{.Names}} {{.Status}}"')
        run(client, "bash /opt/redroid/start.sh", timeout=180)
        for i in range(40):
            time.sleep(5)
            _, text = run(
                client,
                "adb connect 127.0.0.1:5555 >/dev/null; "
                "adb -s 127.0.0.1:5555 shell getprop sys.boot_completed 2>/dev/null",
                timeout=40,
            )
            if text.strip() == "1":
                print("BOOT_OK", flush=True)
                break
            print(f"wait {i} boot={text.strip()!r}", flush=True)
        else:
            run(client, "docker logs --tail 50 redroid 2>&1")
            return 1
        run(
            client,
            'docker ps --filter name=redroid --format "{{.Names}} {{.Status}} {{.Ports}}"',
        )
        run(client, "adb -s 127.0.0.1:5555 shell wm size")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
