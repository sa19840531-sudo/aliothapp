"""診斷 ReDroid 上 APK 啟動失敗，必要時安裝 libndk。"""

from __future__ import annotations

import os
import sys
import time

import paramiko

HOST = "192.46.230.40"
USER = "root"
PASSWORD = os.environ.get("ALIOTH_REMOTE_ROOT_PASSWORD", "")
PKG = "me.mfanv.npymci"


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 300) -> tuple[int, str]:
    print("=====", cmd[:180], flush=True)
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    text = (out + err).strip()
    if text:
        print(text[-8000:] if len(text) > 8000 else text, flush=True)
    print("exit", code, flush=True)
    return code, text


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
        run(client, "docker ps --filter name=redroid --format '{{.Names}} {{.Status}}'")
        run(client, "adb connect 127.0.0.1:5555")
        run(
            client,
            "adb -s 127.0.0.1:5555 shell cmd package resolve-activity --brief "
            + PKG
            + " | head -5",
        )
        run(
            client,
            "adb -s 127.0.0.1:5555 shell pm dump "
            + PKG
            + " | egrep -i 'android.intent.action.MAIN|LAUNCHER|Activity' | head -30",
        )
        run(
            client,
            "adb -s 127.0.0.1:5555 shell ls -la /system/lib64/libndk_translation.so "
            "/system/bin/houdini64 /vendor/lib64/arm64 /system/lib64/arm64 2>&1 | head -40",
        )
        run(client, "adb -s 127.0.0.1:5555 shell getprop | egrep -i 'ndk|houdini|zygote|abi' | head -40")
        run(client, "adb -s 127.0.0.1:5555 logcat -c")
        # 直接 am start 主 Activity（先前 Google 日誌裡是 com.jycg.icg.MainActivity）
        run(
            client,
            "adb -s 127.0.0.1:5555 shell am start -W -n "
            + PKG
            + "/com.jycg.icg.MainActivity 2>&1",
        )
        time.sleep(6)
        run(client, "adb -s 127.0.0.1:5555 shell pidof " + PKG + " || echo NOT_RUNNING")
        run(
            client,
            "adb -s 127.0.0.1:5555 logcat -d *:E 2>&1 | tail -80",
        )
        run(
            client,
            "adb -s 127.0.0.1:5555 logcat -d 2>&1 | egrep -i "
            "'FATAL|AndroidRuntime|ndk_translation|SIGILL|tombstone|Force finishing|"
            + PKG.replace(".", "\\.")
            + "' | tail -80",
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
