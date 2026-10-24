"""禁漫天堂 (jmcomic)：纯 ADB 启动 App（不用 Airtest / Yosemite）。

python jmcomic.py 172.104.172.119:30615
set JMCOMIC_SERIAL=172.104.172.119:30615
python jmcomic.py
AirtestIDE 运行时读取 --device Android://127.0.0.1:5037/<serial>
IDE 会把 jmcomic.air 目录塞进 argv，不能当成 adb serial。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

__author__ = "Show"

APP_DIR = r"F:\apptest\禁漫天堂"
# QA-A 导出的禁漫天堂 1.0.1，文件 jmcomic-qaa-1.0.1.apk。
# 同目录的 jmcomic2-正能量1.8.2.apk 是另一个应用 com.jiaohua_browser。
APP_PACKAGE = "com.xhwj.jmtt"
APP_FALLBACK_ACTIVITY = "com.example.jmtt_app.MainActivity"
# tpl1792049666290.png record_pos=(0.351, -0.296)，resolution=(1440, 3040)。不用 Template。
# x = 0.351 * 720 + 720，y = -0.296 * 1520 + 1520。
MID_BUTTON_TAP = (973, 1070)
# tpl1792049946600.png record_pos=(0.334, -0.415)，resolution=(1440, 3040)。不用 Template。
# x = 0.334 * 720 + 720，y = -0.415 * 1520 + 1520。
MID_BUTTON_TAP_2 = (960, 889)
# 连载更新左边第一本。聊天图 433x921，含状态栏，按宽度缩放。
# 封面范围图上 x 20-144、y 634-810，中心 (82, 722)。
# x = 82 * 1440 / 433，y = 722 * 1440 / 433。
FIRST_LEFT_COMIC_TAP = (273, 2401)
# 详情页「开始阅读」。聊天图 485x1024，含状态栏，按宽度缩放。
# 旧 (877, 2826) 是按钮中心，但落在系统手势带（屏幕高度 92% 以下），点被拒绝，页面仍停在详情。
# 粉色按钮图上 x 137-455、y 937-966。只取按钮上沿，避开手势带。
# x = 296 * 1440 / 485，y = 2794（2794/3040 = 0.919）。
BOTTOM_BUTTON_TAP = (879, 2794)
# 阅读页左上角返回箭头尖端。聊天图 485x1024，含状态栏，按宽度缩放。
# 旧 (44, 258) 落在箭头左边空白。深色箭头范围图上 x 32-46、y 79-90，尖端 (32, 84.5)。
# x = 32 * 1440 / 485，y = 84.5 * 1440 / 485。
PAGE_BACK_TAP = (95, 251)
# 详情页左上角返回箭头尖端。聊天图 485x1024，含状态栏，按宽度缩放。
# 旧 (420, 143) 落在状态栏。箭头范围图上 x 32-47、y 69-79，尖端 (32, 74)。
# x = 32 * 1440 / 485，y = 74 * 1440 / 485。
PAGE_BACK_TAP_2 = (95, 220)
# 首页底部第一个页签。原 (639, 2852) 来自 record_pos，y 落在系统手势带，点被拒绝，页面停在首页。
# 只把 y 抬到 2794（2794/3040 = 0.919），x 仍是 639。
BOTTOM_TAB_TAP = (639, 2794)
# 底部「视频」。聊天图 485x1024，含状态栏，按宽度缩放。
# 旧 (801, 2862) 落在小说和视频中间，下一记点进小说。图标中心图上 (293.0, 926.6)。
# x = 293.0 * 1440 / 485，y = 926.6 * 1440 / 485。
BOTTOM_TAB_TAP_2 = (870, 2751)
# 最新更新左边第一部影片。聊天图 431x923，含状态栏，按宽度缩放。
# 封面范围图上 x 18-210、y 682-807，中心 (114, 744.5)。
# x = 114 * 1440 / 431，y = 744.5 * 1440 / 431。
FIRST_LEFT_VIDEO_TAP = (381, 2487)
# 视频页左上角返回箭头尖端。聊天图 485x1024，含状态栏，按宽度缩放。
# 旧 (407, 169) 落在箭头右边。箭头范围图上 x 23-42、y 74-88，尖端 (23, 81)。
# x = 23 * 1440 / 485，y = 81 * 1440 / 485。
PAGE_BACK_TAP_3 = (68, 240)
# 底部「我的」。聊天图 485x1024，含状态栏，按宽度缩放。
# 旧 (964, 2850) 落在视频和我的中间偏下。图标中心图上 (406.1, 932.6)。
# x = 406.1 * 1440 / 485，y = 932.6 * 1440 / 485。
BOTTOM_TAB_TAP_3 = (1206, 2769)
# 开屏弹窗右上角深色 X。原 POPUP_CLOSE_TAP (1200, 923) 落在叉的上方空白，弹窗仍在。
# 聊天图 485x1024，含状态栏，按宽度缩放。只点这个叉。
# 范围图上 x 394-431、y 351-387，重心 (412.6, 369.1)。
# x = 412.6 * 1440 / 485，y = 369.1 * 1440 / 485。
POPUP_CLOSE_TAP = (1225, 1096)
# 第二个弹窗右侧浅色 X。原 POPUP_CLOSE_TAP_2 (1231, 1100) 落在叉下面的广告上，跳进浏览器。
# 聊天图 485x1024，含状态栏，按宽度缩放。只点这个叉。
# 范围图上 x 399-410、y 304-316，重心 (404.5, 310.0)。
# x = 404.5 * 1440 / 485，y = 310.0 * 1440 / 485。
POPUP_CLOSE_TAP_2 = (1201, 920)
# 第三张截图上的弹窗右侧浅色 X。聊天图 485x1024，含状态栏，按宽度缩放。只点这个叉。
# 范围图上 x 398-410、y 305-317，重心 (404.0, 311.1)。
# x = 404.0 * 1440 / 485，y = 311.1 * 1440 / 485。
POPUP_CLOSE_TAP_3 = (1200, 924)
AAPT_CANDIDATES = (r"F:\Android\Sdk\build-tools\37.0.0\aapt.exe",)


def _is_device_serial(value):
    """路径和 .air 目录不是 adb serial。IDE 会把脚本目录塞进 argv。"""
    text = str(value or "").strip()
    if not text or text.startswith("-"):
        return False
    if any(ch in text for ch in "\\/ \t"):
        return False
    if "://" in text or text.endswith(".air") or text.endswith(".py"):
        return False
    return not os.path.exists(text)


def _serial_from_device_uri(value):
    text = str(value or "").strip()
    if "://" not in text or "/" not in text.split("://", 1)[1]:
        return ""
    serial = text.split("://", 1)[1].split("/", 1)[1].split("?", 1)[0].strip()
    return serial if _is_device_serial(serial) else ""


def _serial_from_argv():
    args = [str(item).strip() for item in sys.argv[1:]]
    for index, arg in enumerate(args):
        if arg == "--device" and index + 1 < len(args):
            serial = _serial_from_device_uri(args[index + 1])
            if serial:
                return serial
        if arg.startswith("--device="):
            serial = _serial_from_device_uri(arg.split("=", 1)[1])
            if serial:
                return serial
    for arg in args:
        if _is_device_serial(arg):
            return arg
    return ""


def _serial_from_airtest_runner():
    """IDE 已解析的当前设备。只读序列号，不调用 Airtest 点击。"""
    try:
        from airtest.cli import runner as airtest_runner
    except Exception:
        return ""
    parsed = getattr(airtest_runner, "args", None)
    devices = getattr(parsed, "device", None) if parsed is not None else None
    if isinstance(devices, str):
        devices = [devices]
    if not isinstance(devices, (list, tuple)):
        return ""
    for item in devices:
        serial = _serial_from_device_uri(item)
        if serial:
            return serial
    return ""


def resolve_device_serial():
    """命令行或 AirtestIDE 当前设备优先，其次环境变量。不写死某一台。"""
    for picker in (_serial_from_argv, _serial_from_airtest_runner):
        serial = picker()
        if serial:
            return serial
    for key in ("JMCOMIC_SERIAL", "ANDROID_SERIAL"):
        value = os.environ.get(key, "").strip()
        if _is_device_serial(value):
            return value
    return ""


DEVICE_SERIAL = resolve_device_serial()

ADB_CANDIDATES = (
    r"F:\Android\Sdk\platform-tools\adb.exe",
    r"D:\Program Files (x86)\ONIRENT\tools\adb\35.0.2-12147458\adb.exe",
    r"D:\Program Files\ONIRENT\tools\adb\adb.exe",
    "adb",
)

_ADB = None


def _find_adb():
    global _ADB
    if _ADB:
        return _ADB
    for path in ADB_CANDIDATES:
        if path != "adb" and os.path.isfile(path):
            _ADB = path
            return _ADB
        if path == "adb":
            _ADB = "adb"
            return _ADB
    _ADB = "adb"
    return _ADB


def adb(*args, timeout=60, select_device=True):
    cmd = [_find_adb(), "-P", "5037"]
    if select_device:
        cmd.extend(["-s", DEVICE_SERIAL])
    cmd.extend(args)
    try:
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
        return out.decode("utf-8", "ignore")
    except subprocess.CalledProcessError as exc:
        return (exc.output or b"").decode("utf-8", "ignore")
    except Exception as exc:
        print(f"[jmcomic] adb error: {exc}")
        return ""


def adb_shell(cmdline, timeout=60):
    return adb("shell", cmdline, timeout=timeout)


def connect_device():
    """已连上就不要反复 adb connect，否则 AirtestIDE 设备窗容易黑屏。"""
    if not DEVICE_SERIAL:
        raise RuntimeError("未指定设备。用法: python jmcomic.py <serial>  或设定 JMCOMIC_SERIAL")
    state = adb("get-state").strip()
    if state != "device":
        adb("connect", DEVICE_SERIAL, timeout=15, select_device=False)
        time.sleep(1.0)
        state = adb("get-state").strip()
    print(f"[jmcomic] adb={_find_adb()} serial={DEVICE_SERIAL} state={state}")
    if state != "device":
        raise RuntimeError(f"设备未就绪: {state}")


def _find_aapt():
    for path in AAPT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return ""


def _apk_package_name(apk_path):
    """aapt 打不开中文路径，先复制到临时目录再读包名。"""
    aapt = _find_aapt()
    if not aapt:
        return ""
    temp_dir = tempfile.mkdtemp(prefix="jmcomic-apk-")
    temp_apk = os.path.join(temp_dir, "app.apk")
    try:
        shutil.copyfile(apk_path, temp_apk)
        out = subprocess.check_output(
            [aapt, "dump", "badging", temp_apk],
            stderr=subprocess.STDOUT,
        )
    except Exception as exc:
        print(f"[jmcomic] read apk package failed: {exc}")
        return ""
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    text = out.decode("utf-8", "ignore")
    marker = "package: name='"
    start = text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = text.find("'", start)
    return text[start:end] if end > start else ""


def resolve_app_apk():
    """用目录里包名等于 APP_PACKAGE 的 apk，不写死文件名。"""
    if not os.path.isdir(APP_DIR):
        raise RuntimeError(f"找不到安装目录: {APP_DIR}")
    found = [
        os.path.join(APP_DIR, name) for name in os.listdir(APP_DIR) if name.lower().endswith(".apk")
    ]
    if not found:
        raise RuntimeError(f"安装目录里没有 apk: {APP_DIR}")
    matched = [path for path in found if _apk_package_name(path) == APP_PACKAGE]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        matched.sort(key=os.path.getmtime)
        return matched[-1]
    raise RuntimeError(f"安装目录里没有包名 {APP_PACKAGE} 的 apk。")


def ensure_app_installed():
    """没装才安装 F:\\apptest\\禁漫天堂 里的包。"""
    apk = resolve_app_apk()
    if "package:" in adb_shell(f"pm path {APP_PACKAGE}"):
        print(f"[jmcomic] already installed {APP_PACKAGE}")
        return
    print(f"[jmcomic] install {apk}")
    result = adb("install", "-r", apk, timeout=180)
    print(f"[jmcomic] install -> {result.strip()[:200]}")
    if "Success" not in result:
        raise RuntimeError(f"安装失败: {result.strip()[:300]}")


def resolve_launcher_component(pkg):
    out = adb_shell(f"cmd package resolve-activity --brief {pkg}")
    for line in out.splitlines():
        text = line.strip()
        if "/" in text and not text.startswith("No activity"):
            return text
    return f"{pkg}/{APP_FALLBACK_ACTIVITY}"


def current_focus():
    return adb_shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -n 2")


def start_app():
    ensure_app_installed()
    print(f"[jmcomic] force-stop {APP_PACKAGE}")
    adb_shell(f"am force-stop {APP_PACKAGE}")
    time.sleep(1.0)

    component = resolve_launcher_component(APP_PACKAGE)
    print(f"[jmcomic] am start -n {component}")
    launch = adb_shell(f"am start -n {component}")
    print(f"[jmcomic] am start -> {launch.strip()[:200]}")
    if "Error" in launch or "Exception" in launch:
        print("[jmcomic] monkey fallback")
        adb_shell(f"monkey -p {APP_PACKAGE} -c android.intent.category.LAUNCHER 1")
    time.sleep(3.0)

    focus = current_focus()
    print(f"[jmcomic] focus {focus.strip()[:180]}")
    if APP_PACKAGE not in focus:
        raise RuntimeError(f"启动后未进入 App: {focus.strip()}")
    print(f"[jmcomic] started OK package={APP_PACKAGE}")
    return APP_PACKAGE


def _png_complete(path):
    """文件被截断时，没传完的下半部会显示成黑色。"""
    try:
        size = os.path.getsize(path)
    except Exception:
        return False
    if size < 32:
        return False
    with open(path, "rb") as handle:
        if handle.read(8) != b"\x89PNG\r\n\x1a\n":
            return False
        handle.seek(-12, os.SEEK_END)
        return handle.read(12) == b"\x00\x00\x00\x00IEND\xaeB`\x82"


def _remote_png_size(remote):
    info = adb_shell(f"ls -l {remote}", timeout=25).strip()
    size = 0
    for part in info.split():
        if part.isdigit() and int(part) > size:
            size = int(part)
    return size, info


def capture_screen(name, timeout=40):
    """screencap 先存到手机再 pull。不用本机重定向，避免把 PNG 写成文本。

    失败只回空字符串，不从截图函数往外抛。
    """
    shot_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")
    if not os.path.isdir(shot_dir):
        os.makedirs(shot_dir)
    local = os.path.join(shot_dir, f"{name}.png")
    remote = f"/sdcard/jmcomic_{name}.png"
    for attempt in range(3):
        if os.path.isfile(local):
            try:
                os.remove(local)
            except Exception:
                pass
        adb_shell(f"rm -f {remote}", timeout=20)
        adb_shell(f"screencap -p {remote}", timeout=timeout)
        last = -1
        size = 0
        info = ""
        for _ in range(3):
            size, info = _remote_png_size(remote)
            if not info:
                print("[jmcomic] remote size unavailable, pull anyway")
                break
            if size > 8 and size == last:
                break
            last = size
            time.sleep(0.4)
        for _ in range(2):
            adb("pull", remote, local, timeout=60)
            if not _png_complete(local):
                continue
            if size > 8 and os.path.getsize(local) != size:
                continue
            break
        adb_shell(f"rm -f {remote}", timeout=20)
        if _png_complete(local):
            print(f"[jmcomic] shot {local} ({os.path.getsize(local):d} bytes)")
            return local
        if os.path.isfile(local):
            try:
                os.remove(local)
            except Exception:
                pass
        print(f"[jmcomic] 截图失败 try={attempt + 1:d} remote={info[:80]}")
        time.sleep(0.8)
    return ""


print("[jmcomic] ===== start =====")
try:
    connect_device()
    start_app()
    print("[jmcomic] wait 1s")
    time.sleep(1.0)
    capture_screen("01")
    print("[jmcomic] tap {} {}".format(*POPUP_CLOSE_TAP))
    adb_shell("input tap {} {}".format(*POPUP_CLOSE_TAP))
    time.sleep(1.0)
    capture_screen("02")
    print("[jmcomic] tap {} {}".format(*POPUP_CLOSE_TAP_2))
    adb_shell("input tap {} {}".format(*POPUP_CLOSE_TAP_2))
    time.sleep(1.0)
    capture_screen("03")
    print("[jmcomic] tap {} {}".format(*POPUP_CLOSE_TAP_3))
    adb_shell("input tap {} {}".format(*POPUP_CLOSE_TAP_3))
    time.sleep(1.0)
    capture_screen("04")
    print("[jmcomic] tap {} {}".format(*FIRST_LEFT_COMIC_TAP))
    adb_shell("input tap {} {}".format(*FIRST_LEFT_COMIC_TAP))
    time.sleep(1.0)
    capture_screen("05")
    print("[jmcomic] tap {} {}".format(*BOTTOM_BUTTON_TAP))
    adb_shell("input tap {} {}".format(*BOTTOM_BUTTON_TAP))
    time.sleep(1.0)
    capture_screen("06")
    print("[jmcomic] tap {} {}".format(*PAGE_BACK_TAP))
    adb_shell("input tap {} {}".format(*PAGE_BACK_TAP))
    time.sleep(1.0)
    capture_screen("07")
    print("[jmcomic] tap {} {}".format(*PAGE_BACK_TAP_2))
    adb_shell("input tap {} {}".format(*PAGE_BACK_TAP_2))
    time.sleep(1.0)
    capture_screen("08")
    print("[jmcomic] tap {} {}".format(*BOTTOM_TAB_TAP))
    adb_shell("input tap {} {}".format(*BOTTOM_TAB_TAP))
    time.sleep(1.0)
    capture_screen("09")
    print("[jmcomic] tap {} {}".format(*BOTTOM_TAB_TAP_2))
    adb_shell("input tap {} {}".format(*BOTTOM_TAB_TAP_2))
    time.sleep(1.0)
    capture_screen("10")
    print("[jmcomic] tap {} {}".format(*FIRST_LEFT_VIDEO_TAP))
    adb_shell("input tap {} {}".format(*FIRST_LEFT_VIDEO_TAP))
    time.sleep(1.0)
    capture_screen("11")
    for index in range(3):
        time.sleep(10.0)
        capture_screen(f"{12 + index:02d}")
    print("[jmcomic] tap {} {}".format(*PAGE_BACK_TAP_3))
    adb_shell("input tap {} {}".format(*PAGE_BACK_TAP_3))
    time.sleep(1.0)
    capture_screen("15")
    print("[jmcomic] tap {} {}".format(*BOTTOM_TAB_TAP_3))
    adb_shell("input tap {} {}".format(*BOTTOM_TAB_TAP_3))
    time.sleep(1.0)
    capture_screen("16")
except Exception as exc:
    print(f"[jmcomic] ABORT: {exc}")
    raise
