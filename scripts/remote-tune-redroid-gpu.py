"""調整 ReDroid GPU 設定並驗證 Flutter App 畫面。"""

from __future__ import annotations

import os
import sys
import time

import paramiko

HOST = "192.46.230.40"
USER = "root"
PASSWORD = os.environ.get("ALIOTH_REMOTE_ROOT_PASSWORD", "")
PKG = "me.mfanv.npymci"
ACT = f"{PKG}/com.jycg.icg.MainActivity"


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 300) -> tuple[int, str]:
    print("=====", cmd[:180], flush=True)
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    text = (out + err).strip()
    if text:
        print(text[-6000:] if len(text) > 6000 else text, flush=True)
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
        # 先看目前狀態
        run(client, "adb connect 127.0.0.1:5555")
        run(client, f"adb -s 127.0.0.1:5555 shell pidof {PKG} || echo NOT_RUNNING")
        run(
            client,
            "adb -s 127.0.0.1:5555 shell screencap -p /sdcard/redroid_shot1.png && "
            "ls -l /sdcard/redroid_shot1.png",
        )
        run(
            client,
            "adb -s 127.0.0.1:5555 shell dumpsys activity activities | "
            "egrep -i 'mResumedActivity|mFocusedApp|flutter|Error' | head -20",
        )

        # 重建容器：加大顯示、guest GPU、開 memfd
        run(client, "docker rm -f redroid 2>/dev/null || true")
        run(
            client,
            "docker run -itd --privileged --name redroid "
            "--memory=4g --cpus=4 "
            "-v /opt/redroid/data:/data "
            "-p 5555:5555 "
            "redroid/redroid:12.0.0_64only-latest "
            "androidboot.redroid_gpu_mode=guest "
            "androidboot.use_memfd=true "
            "androidboot.redroid_width=1080 "
            "androidboot.redroid_height=1920 "
            "androidboot.redroid_dpi=320 "
            "androidboot.redroid_fps=30",
        )
        # 等 boot
        for i in range(40):
            time.sleep(5)
            _, boot = run(
                client,
                "adb connect 127.0.0.1:5555 >/dev/null; "
                "adb -s 127.0.0.1:5555 shell getprop sys.boot_completed 2>/dev/null",
                timeout=60,
            )
            if boot.strip() == "1":
                print("BOOT_OK", flush=True)
                break
            print(f"wait {i}", flush=True)
        else:
            run(client, "docker logs --tail 50 redroid 2>&1")
            return 1

        # data volume 應保留已安裝 APK
        run(client, f"adb -s 127.0.0.1:5555 shell pm path {PKG}")
        run(client, "adb -s 127.0.0.1:5555 logcat -c")
        run(client, f"adb -s 127.0.0.1:5555 shell am start -n {ACT}")
        time.sleep(10)
        run(client, f"adb -s 127.0.0.1:5555 shell pidof {PKG} || echo NOT_RUNNING")
        run(
            client,
            "adb -s 127.0.0.1:5555 logcat -d 2>&1 | egrep -i "
            "'FATAL|SIGILL|Skia|platform view|Force finishing|flutter' | tail -40",
        )
        run(
            client,
            "adb -s 127.0.0.1:5555 shell screencap -p /sdcard/redroid_shot2.png && "
            "ls -l /sdcard/redroid_shot2.png && "
            "adb -s 127.0.0.1:5555 pull /sdcard/redroid_shot2.png /tmp/redroid_shot2.png",
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
