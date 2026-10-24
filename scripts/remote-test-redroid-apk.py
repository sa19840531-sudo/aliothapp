"""上傳 APK 到遠端 ReDroid 並測試啟動。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import paramiko

HOST = "192.46.230.40"
USER = "root"
PASSWORD = os.environ.get("ALIOTH_REMOTE_ROOT_PASSWORD", "")
LOCAL_APK = Path(
    os.environ.get(
        "ALIOTH_TEST_APK",
        r"F:\alioth\.alioth-mobile-demo\uploads\2fb6a726e375407c9ad5aa5519c26e29_91cg_1.3.5_260710_2.apk",
    )
)
REMOTE_APK = "/tmp/test_app.apk"
PKG = "me.mfanv.npymci"


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 180) -> tuple[int, str]:
    print("=====", cmd[:160], flush=True)
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    text = (out + err).strip()
    if text:
        print(text[-5000:] if len(text) > 5000 else text, flush=True)
    print("exit", code, flush=True)
    return code, text


def main() -> int:
    if not PASSWORD:
        print("缺少 ALIOTH_REMOTE_ROOT_PASSWORD")
        return 2
    if not LOCAL_APK.exists():
        print(f"APK 不存在: {LOCAL_APK}")
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
        print(f"upload {LOCAL_APK} -> {REMOTE_APK}", flush=True)
        sftp = client.open_sftp()
        sftp.put(str(LOCAL_APK), REMOTE_APK)
        sftp.close()
        print("upload done", flush=True)

        run(client, "adb connect 127.0.0.1:5555")
        run(client, f"adb -s 127.0.0.1:5555 install -r {REMOTE_APK}", timeout=300)
        run(
            client,
            "adb -s 127.0.0.1:5555 shell pm path " + PKG,
        )
        run(
            client,
            "adb -s 127.0.0.1:5555 shell dumpsys package "
            + PKG
            + " | egrep 'primaryCpuAbi|versionName|nativeLibraryDir' | head -20",
        )
        # clear log and launch
        run(client, "adb -s 127.0.0.1:5555 logcat -c")
        run(
            client,
            "adb -s 127.0.0.1:5555 shell monkey -p "
            + PKG
            + " -c android.intent.category.LAUNCHER 1",
        )
        time.sleep(8)
        run(
            client,
            "adb -s 127.0.0.1:5555 shell pidof " + PKG + " || echo NOT_RUNNING",
        )
        run(
            client,
            "adb -s 127.0.0.1:5555 logcat -d | egrep -i "
            "'FATAL|AndroidRuntime|ndk_translation|SIGILL|DEBUG|Force finishing|"
            + PKG
            + "' | tail -60",
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
