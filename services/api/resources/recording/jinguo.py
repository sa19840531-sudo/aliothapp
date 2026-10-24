"""禁果 (jinguo)：纯 ADB 启动 App（不用 Airtest / Yosemite）。

python jinguo.py 172.104.172.119:30660
set JINGUO_SERIAL=172.104.172.119:30660
python jinguo.py
AirtestIDE 运行时读取 --device Android://127.0.0.1:5037/<serial>
IDE 会把 jinguo.air 目录塞进 argv，不能当成 adb serial。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

__author__ = "Show"

APP_DIR = r"F:\apptest\禁果"
# QA-A 的禁果短剧是 dark.NVYCy.AmBPu 1.0.0。QA-B 是另一个应用 com.huangguo.drama 1.3.4。
APP_PACKAGE = "dark.NVYCy.AmBPu"
APP_FALLBACK_ACTIVITY = "com.xvproject.MainActivity"
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
    for key in ("JINGUO_SERIAL", "ANDROID_SERIAL"):
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
        print(f"[jinguo] adb error: {exc}")
        return ""


def adb_shell(cmdline, timeout=60):
    return adb("shell", cmdline, timeout=timeout)


def connect_device():
    """已连上就不要反复 adb connect，否则 AirtestIDE 设备窗容易黑屏。"""
    if not DEVICE_SERIAL:
        raise RuntimeError("未指定设备。用法: python jinguo.py <serial>  或设定 JINGUO_SERIAL")
    state = adb("get-state").strip()
    if state != "device":
        # 尚未在线时不能带 -s，否则 connect 本身会失败
        adb("connect", DEVICE_SERIAL, timeout=15, select_device=False)
        time.sleep(1.0)
        state = adb("get-state").strip()
    print(f"[jinguo] adb={_find_adb()} serial={DEVICE_SERIAL} state={state}")
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
    temp_dir = tempfile.mkdtemp(prefix="jinguo-apk-")
    temp_apk = os.path.join(temp_dir, "app.apk")
    try:
        shutil.copyfile(apk_path, temp_apk)
        out = subprocess.check_output(
            [aapt, "dump", "badging", temp_apk],
            stderr=subprocess.STDOUT,
        )
    except Exception as exc:
        print(f"[jinguo] read apk package failed: {exc}")
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
    """用目录里包名等于 APP_PACKAGE 的 apk。QA-A 和 QA-B 不是同一个应用。"""
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
    raise RuntimeError(
        f"安装目录里没有包名 {APP_PACKAGE} 的 apk。QA-A 和 QA-B 不是同一个应用，不能混装。"
    )


def ensure_app_installed():
    """没装才安装 F:\\apptest\\禁果 里的包。"""
    apk = resolve_app_apk()
    if "package:" in adb_shell(f"pm path {APP_PACKAGE}"):
        print(f"[jinguo] already installed {APP_PACKAGE}")
        return
    print(f"[jinguo] install {apk}")
    result = adb("install", "-r", apk, timeout=180)
    print(f"[jinguo] install -> {result.strip()[:200]}")
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
    print(f"[jinguo] force-stop {APP_PACKAGE}")
    adb_shell(f"am force-stop {APP_PACKAGE}")
    time.sleep(1.0)

    component = resolve_launcher_component(APP_PACKAGE)
    print(f"[jinguo] am start -n {component}")
    launch = adb_shell(f"am start -n {component}")
    print(f"[jinguo] am start -> {launch.strip()[:200]}")
    if "Error" in launch or "Exception" in launch:
        print("[jinguo] monkey fallback")
        adb_shell(f"monkey -p {APP_PACKAGE} -c android.intent.category.LAUNCHER 1")
    time.sleep(3.0)

    focus = current_focus()
    print(f"[jinguo] focus {focus.strip()[:180]}")
    if APP_PACKAGE not in focus:
        raise RuntimeError(f"启动后未进入 App: {focus.strip()}")
    print(f"[jinguo] started OK package={APP_PACKAGE}")
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
    remote = f"/sdcard/jinguo_{name}.png"
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
                print("[jinguo] remote size unavailable, pull anyway")
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
            print(f"[jinguo] shot {local} ({os.path.getsize(local):d} bytes)")
            return local
        if os.path.isfile(local):
            try:
                os.remove(local)
            except Exception:
                pass
        print(f"[jinguo] 截图失败 try={attempt + 1:d} remote={info[:80]}")
        time.sleep(0.8)
    return ""


def press_cloud_back():
    """云手机底部导航右侧的返回键。keyevent 4 按不到这个键。"""
    print("[jinguo] cloud phone back {} {}".format(*CLOUD_BACK_TAP))
    adb_shell("input tap {} {}".format(*CLOUD_BACK_TAP))


def dismiss_episode_sheet_if_open():
    """没按到左上角返回时会弹出选集；出现选集要按云手机系统 BACK（CLOUD_BACK_TAP）。"""
    path = "/sdcard/jinguo_ui.xml"
    adb_shell("uiautomator dump %s" % path, timeout=40)
    xml = adb_shell("cat %s" % path, timeout=20)
    # 展开面板有「更多好剧」；未离开播放页时底部仍有「选集」「金币解锁」。
    if "更多好剧" not in xml and "选集" not in xml and "金币解锁" not in xml:
        return False
    print("[jinguo] episode sheet/bar visible -> cloud back")
    press_cloud_back()
    return True


# 开屏右上角「进入」。原 ENTRY_TAP (986, 242) 落在按钮左侧的广告上，外跳到浏览器。
# 聊天图 485x1024，含状态栏，按宽度缩放。按钮范围图上 x 374-464、y 81-123，中心 (419, 102)。
# x = 419 * 1440 / 485，y = 102 * 1440 / 485。
ENTRY_TAP = (1244, 303)
# 首页底部圆圈 X。原 LOWER_BUTTON_TAP (722, 2506) 落在圆圈下沿，页面仍停在首页。
# 聊天图 485x1024，含状态栏，按宽度缩放。圆圈范围图上 x 225-259、y 809-843，中心 (242, 826)。
# x = 242 * 1440 / 485，y = 826 * 1440 / 485。
LOWER_BUTTON_TAP = (719, 2452)
# tpl1792031683662.png record_pos=(0.0, 0.585)，resolution=(1440, 3040)。不用 Template。
# x = 0.0 * 720 + 720，y = 0.585 * 1520 + 1520。
LOWER_BUTTON_TAP_2 = (720, 2409)
# tpl1792031733740.png record_pos=(0.004, 0.584)，resolution=(1440, 3040)。不用 Template。
# x = 0.004 * 720 + 720，y = 0.584 * 1520 + 1520。
LOWER_BUTTON_TAP_3 = (723, 2408)
# tpl1792031778966.png record_pos=(0.002, 0.585)，resolution=(1440, 3040)。不用 Template。
# x = 0.002 * 720 + 720，y = 0.585 * 1520 + 1520。
LOWER_BUTTON_TAP_4 = (721, 2409)
# tpl1792031816998.png record_pos=(0.001, 0.582)，resolution=(1440, 3040)。不用 Template。
# x = 0.001 * 720 + 720，y = 0.582 * 1520 + 1520。
LOWER_BUTTON_TAP_5 = (721, 2405)
# tpl1792031868919.png record_pos=(0.001, 0.586)，resolution=(1440, 3040)。不用 Template。
# x = 0.001 * 720 + 720，y = 0.586 * 1520 + 1520。
LOWER_BUTTON_TAP_6 = (721, 2411)
# tpl1792031923268.png record_pos=(0.001, 0.225)，resolution=(1440, 3040)。不用 Template。
# x = 0.001 * 720 + 720，y = 0.225 * 1520 + 1520。
MID_BUTTON_TAP = (721, 1862)
# tpl1792031962375.png record_pos=(-0.006, 0.597)，resolution=(1440, 3040)。不用 Template。
# x = -0.006 * 720 + 720，y = 0.597 * 1520 + 1520。
LOWER_BUTTON_TAP_7 = (716, 2427)
# 首页两列里左边第一部「诱色游戏」。聊天图 490x1024，含状态栏，按宽度缩放。
# 封面范围图上 x 16-244、y 444-726，中心 (130, 585)。
# x = 130 * 1440 / 490，y = 585 * 1440 / 490。
FIRST_LEFT_VIDEO_TAP = (382, 1719)
# 播放页左上角返回。原 PAGE_BACK_TAP (48, 294) 对准尖端，动作完成但页面仍停在第2集付费解锁。
# 17:03 聊天图 485x1024，含状态栏，按宽度缩放。箭头笔画图上 x 15-26、y 88-109，中心 (20.9, 98.9)。
# x = 20.9 * 1440 / 485，y = 98.9 * 1440 / 485。
PAGE_BACK_TAP = (62, 294)
# 返回后底部「剧场」。原 BOTTOM_TAB_TAP (528, 2838) 落在「视频」文字上，17 仍停在视频。
# shots/17 聊天图 485x1024，含状态栏，按宽度缩放。
# 剧场图标图上 x 101-125、y 908-932，笔画中心 (113.5, 920)。
# x = 113.5 * 1440 / 485，y = 920 * 1440 / 485。
BOTTOM_TAB_TAP = (337, 2732)
# 底部页签。原 BOTTOM_TAB_TAP_2 (659, 2833) 被手势带拒绝（y/3040>=0.92），只抬高 y。
# x 保持 659；y = 2794（2794/3040=0.919）。
BOTTOM_TAB_TAP_2 = (659, 2794)
# tpl1792032772833.png record_pos=(0.083, 0.871)，resolution=(1440, 3040)。不用 Template。
# 原点 (780, 2844) 被手势带拒绝（y/3040>=0.92），只抬高 y。
# x 保持 780；y = 2794（2794/3040=0.919）。
BOTTOM_TAB_TAP_3 = (780, 2794)
# tpl1792032834437.png record_pos=(0.236, 0.869)，resolution=(1440, 3040)。不用 Template。
# 原点 (890, 2841) 被手势带拒绝（y/3040>=0.92），只抬高 y。
# x 保持 890；y = 2794（2794/3040=0.919）。
BOTTOM_TAB_TAP_4 = (890, 2794)
# 底部「我的」。原 BOTTOM_TAB_TAP_5 (1007, 2841) 落在「福利」文字上，26 仍停在福利签到。
# shots/26 聊天图 485x1024，含状态栏，按宽度缩放。
# 我的图标图上 x 427-447、y 907-932，笔画中心 (436.9, 920.8)。
# x = 436.9 * 1440 / 485，y = 920.8 * 1440 / 485。
BOTTOM_TAB_TAP_5 = (1297, 2734)
# tpl1792032969730.png record_pos=(0.362, -0.574)，resolution=(1440, 3040)。不用 Template。
# x = 0.362 * 720 + 720，y = -0.574 * 1520 + 1520。
UPPER_BUTTON_TAP = (981, 648)
# tpl1792033035973.png record_pos=(0.006, 0.858)，resolution=(1440, 3040)。不用 Template。
# 原点 (724, 2824) 被手势带拒绝（y/3040>=0.92），只抬高 y。
# x 保持 724；y = 2794（2794/3040=0.919）。
BOTTOM_BUTTON_TAP = (724, 2794)
# tpl1792033089835.png record_pos=(-0.004, 0.751)，resolution=(1440, 3040)。不用 Template。
# x = -0.004 * 720 + 720，y = 0.751 * 1520 + 1520。
LOWER_BUTTON_TAP_8 = (717, 2662)
# 云手机底部导航右侧的返回键。keyevent 4 按不到这个键。
# 同一批云手机上箭头实色是 (1120, 2955)。
CLOUD_BACK_TAP = (1120, 2955)
# tpl1792033303435.png record_pos=(-0.006, 0.358)，resolution=(1440, 3040)。不用 Template。
# x = -0.006 * 720 + 720，y = 0.358 * 1520 + 1520。
MID_BUTTON_TAP_2 = (716, 2064)
# 充值页左上角返回。原 PAGE_BACK_TAP_2 (394, 172) 落在箭头右侧，32 仍停在充值金币。
# shots/32 聊天图 485x1024，含状态栏，按宽度缩放。箭头尖端图上 (18, 81)。
# x = 18 * 1440 / 485，y = 81 * 1440 / 485。
PAGE_BACK_TAP_2 = (53, 240)


print("[jinguo] ===== start =====")
try:
    connect_device()
    start_app()
    print("[jinguo] wait 5s")
    time.sleep(5.0)
    capture_screen("01")
    print("[jinguo] tap {} {}".format(*ENTRY_TAP))
    adb_shell("input tap {} {}".format(*ENTRY_TAP))
    time.sleep(1.0)
    capture_screen("02")
    print("[jinguo] tap {} {}".format(*LOWER_BUTTON_TAP))
    adb_shell("input tap {} {}".format(*LOWER_BUTTON_TAP))
    time.sleep(1.0)
    capture_screen("03")
    print("[jinguo] tap {} {}".format(*LOWER_BUTTON_TAP_2))
    adb_shell("input tap {} {}".format(*LOWER_BUTTON_TAP_2))
    time.sleep(1.0)
    capture_screen("04")
    print("[jinguo] tap {} {}".format(*LOWER_BUTTON_TAP_3))
    adb_shell("input tap {} {}".format(*LOWER_BUTTON_TAP_3))
    time.sleep(1.0)
    capture_screen("05")
    print("[jinguo] tap {} {}".format(*LOWER_BUTTON_TAP_4))
    adb_shell("input tap {} {}".format(*LOWER_BUTTON_TAP_4))
    time.sleep(1.0)
    capture_screen("06")
    print("[jinguo] tap {} {}".format(*LOWER_BUTTON_TAP_5))
    adb_shell("input tap {} {}".format(*LOWER_BUTTON_TAP_5))
    time.sleep(1.0)
    capture_screen("07")
    print("[jinguo] tap {} {}".format(*LOWER_BUTTON_TAP_6))
    adb_shell("input tap {} {}".format(*LOWER_BUTTON_TAP_6))
    time.sleep(1.0)
    capture_screen("08")
    print("[jinguo] tap {} {}".format(*MID_BUTTON_TAP))
    adb_shell("input tap {} {}".format(*MID_BUTTON_TAP))
    time.sleep(1.0)
    capture_screen("09")
    print("[jinguo] tap {} {}".format(*LOWER_BUTTON_TAP_7))
    adb_shell("input tap {} {}".format(*LOWER_BUTTON_TAP_7))
    time.sleep(1.0)
    capture_screen("10")
    print("[jinguo] tap {} {}".format(*FIRST_LEFT_VIDEO_TAP))
    adb_shell("input tap {} {}".format(*FIRST_LEFT_VIDEO_TAP))
    time.sleep(1.0)
    capture_screen("11")
    for index in range(4):
        time.sleep(5.0)
        capture_screen(f"{12 + index:02d}")
    print("[jinguo] tap {} {}".format(*PAGE_BACK_TAP))
    adb_shell("input tap {} {}".format(*PAGE_BACK_TAP))
    time.sleep(1.0)
    capture_screen("16")
    # 18:26：没按到左上角返回会弹出选集；出现选集要按云手机系统 BACK（CLOUD_BACK_TAP）。
    if dismiss_episode_sheet_if_open():
        time.sleep(1.0)
        print("[jinguo] tap {} {}".format(*PAGE_BACK_TAP))
        adb_shell("input tap {} {}".format(*PAGE_BACK_TAP))
        time.sleep(1.0)
        capture_screen("16")
    print("[jinguo] tap {} {}".format(*BOTTOM_TAB_TAP))
    adb_shell("input tap {} {}".format(*BOTTOM_TAB_TAP))
    time.sleep(1.0)
    capture_screen("17")
    for index in range(4):
        time.sleep(5.0)
        capture_screen(f"{18 + index:02d}")
    print("[jinguo] tap {} {}".format(*BOTTOM_TAB_TAP_2))
    adb_shell("input tap {} {}".format(*BOTTOM_TAB_TAP_2))
    time.sleep(1.0)
    capture_screen("22")
    print("[jinguo] tap {} {}".format(*BOTTOM_TAB_TAP_3))
    adb_shell("input tap {} {}".format(*BOTTOM_TAB_TAP_3))
    time.sleep(1.0)
    capture_screen("23")
    print("[jinguo] tap {} {}".format(*BOTTOM_TAB_TAP_4))
    adb_shell("input tap {} {}".format(*BOTTOM_TAB_TAP_4))
    time.sleep(1.0)
    capture_screen("24")
    print("[jinguo] tap {} {}".format(*BOTTOM_TAB_TAP_5))
    adb_shell("input tap {} {}".format(*BOTTOM_TAB_TAP_5))
    time.sleep(1.0)
    capture_screen("25")
    print("[jinguo] tap {} {}".format(*UPPER_BUTTON_TAP))
    adb_shell("input tap {} {}".format(*UPPER_BUTTON_TAP))
    time.sleep(1.0)
    capture_screen("26")
    print("[jinguo] tap {} {}".format(*BOTTOM_BUTTON_TAP))
    adb_shell("input tap {} {}".format(*BOTTOM_BUTTON_TAP))
    time.sleep(1.0)
    capture_screen("27")
    print("[jinguo] tap {} {}".format(*LOWER_BUTTON_TAP_8))
    adb_shell("input tap {} {}".format(*LOWER_BUTTON_TAP_8))
    time.sleep(1.0)
    capture_screen("28")
    print("[jinguo] browser wait 5s")
    time.sleep(5.0)
    capture_screen("29")
    press_cloud_back()
    time.sleep(1.0)
    capture_screen("30")
    print("[jinguo] tap {} {}".format(*MID_BUTTON_TAP_2))
    adb_shell("input tap {} {}".format(*MID_BUTTON_TAP_2))
    time.sleep(1.0)
    capture_screen("31")
    print("[jinguo] tap {} {}".format(*PAGE_BACK_TAP_2))
    adb_shell("input tap {} {}".format(*PAGE_BACK_TAP_2))
    time.sleep(1.0)
    capture_screen("32")
except Exception as exc:
    print(f"[jinguo] ABORT: {exc}")
    raise
