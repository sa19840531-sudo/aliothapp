# -*- encoding=utf8 -*-
__author__ = "Show"
"""MaMa短剧 (mama)：纯 ADB 启动 App（不用 Airtest / Yosemite）。

只保留启动；其余流程自行重写。

  python mama.py 172.104.172.119:30660
  set MAMA_SERIAL=172.104.172.119:30660
  python mama.py
  AirtestIDE 运行时读取 --device Android://127.0.0.1:5037/<serial>
  IDE 会把 mama.air 目录塞进 argv，不能当成 adb serial。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time


APP_DIR = r"F:\apptest\mama短剧"
# 目录里 apk 的内部包名。不写死 apk 文件名。
APP_PACKAGE = "com.dzu4.app"
APP_FALLBACK_ACTIVITY = "com.mama.mama_app.MainActivity"
AAPT_CANDIDATES = (
    r"F:\Android\Sdk\build-tools\37.0.0\aapt.exe",
)


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
    for key in ("MAMA_SERIAL", "ANDROID_SERIAL"):
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
        print("[mama] adb error: %s" % exc)
        return ""


def adb_shell(cmdline, timeout=60):
    return adb("shell", cmdline, timeout=timeout)


def connect_device():
    """已连上就不要反复 adb connect，否则 AirtestIDE 设备窗容易黑屏。"""
    if not DEVICE_SERIAL:
        raise RuntimeError(
            "未指定设备。用法: python mama.py <serial>  或设定 MAMA_SERIAL"
        )
    state = adb("get-state").strip()
    if state != "device":
        adb("connect", DEVICE_SERIAL, timeout=15, select_device=False)
        time.sleep(1.0)
        state = adb("get-state").strip()
    print("[mama] adb=%s serial=%s state=%s" % (_find_adb(), DEVICE_SERIAL, state))
    if state != "device":
        raise RuntimeError("设备未就绪: %s" % state)


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
    temp_dir = tempfile.mkdtemp(prefix="mama-apk-")
    temp_apk = os.path.join(temp_dir, "app.apk")
    try:
        shutil.copyfile(apk_path, temp_apk)
        out = subprocess.check_output(
            [aapt, "dump", "badging", temp_apk],
            stderr=subprocess.STDOUT,
        )
    except Exception as exc:
        print("[mama] read apk package failed: %s" % exc)
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
        raise RuntimeError("找不到安装目录: %s" % APP_DIR)
    found = [
        os.path.join(APP_DIR, name)
        for name in os.listdir(APP_DIR)
        if name.lower().endswith(".apk")
    ]
    if not found:
        raise RuntimeError("安装目录里没有 apk: %s" % APP_DIR)
    matched = [path for path in found if _apk_package_name(path) == APP_PACKAGE]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        matched.sort(key=os.path.getmtime)
        return matched[-1]
    raise RuntimeError("安装目录里没有包名 %s 的 apk。" % APP_PACKAGE)


def ensure_app_installed():
    """没装才安装 F:\\apptest\\mama短剧 里的包。"""
    apk = resolve_app_apk()
    if "package:" in adb_shell("pm path %s" % APP_PACKAGE):
        print("[mama] already installed %s" % APP_PACKAGE)
        return
    print("[mama] install %s" % apk)
    result = adb("install", "-r", apk, timeout=180)
    print("[mama] install -> %s" % result.strip()[:200])
    if "Success" not in result:
        raise RuntimeError("安装失败: %s" % result.strip()[:300])


def resolve_launcher_component(pkg):
    out = adb_shell("cmd package resolve-activity --brief %s" % pkg)
    for line in out.splitlines():
        text = line.strip()
        if "/" in text and not text.startswith("No activity"):
            return text
    return "%s/%s" % (pkg, APP_FALLBACK_ACTIVITY)


def current_focus():
    return adb_shell(
        "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -n 2"
    )


def start_app():
    ensure_app_installed()
    print("[mama] force-stop %s" % APP_PACKAGE)
    adb_shell("am force-stop %s" % APP_PACKAGE)
    time.sleep(1.0)

    component = resolve_launcher_component(APP_PACKAGE)
    print("[mama] am start -n %s" % component)
    launch = adb_shell("am start -n %s" % component)
    print("[mama] am start -> %s" % launch.strip()[:200])
    if "Error" in launch or "Exception" in launch:
        print("[mama] monkey fallback")
        adb_shell("monkey -p %s -c android.intent.category.LAUNCHER 1" % APP_PACKAGE)
    time.sleep(3.0)

    focus = current_focus()
    print("[mama] focus %s" % focus.strip()[:180])
    if APP_PACKAGE not in focus:
        raise RuntimeError("启动后未进入 App: %s" % focus.strip())
    print("[mama] started OK package=%s" % APP_PACKAGE)
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
    info = adb_shell("ls -l %s" % remote, timeout=25).strip()
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
    local = os.path.join(shot_dir, "%s.png" % name)
    remote = "/sdcard/mama_%s.png" % name
    for attempt in range(3):
        if os.path.isfile(local):
            try:
                os.remove(local)
            except Exception:
                pass
        adb_shell("rm -f %s" % remote, timeout=20)
        adb_shell("screencap -p %s" % remote, timeout=timeout)
        last = -1
        size = 0
        info = ""
        for _ in range(3):
            size, info = _remote_png_size(remote)
            if not info:
                print("[mama] remote size unavailable, pull anyway")
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
        adb_shell("rm -f %s" % remote, timeout=20)
        if _png_complete(local):
            print("[mama] shot %s (%d bytes)" % (local, os.path.getsize(local)))
            return local
        if os.path.isfile(local):
            try:
                os.remove(local)
            except Exception:
                pass
        print("[mama] 截图失败 try=%d remote=%s" % (attempt + 1, info[:80]))
        time.sleep(0.8)
    return ""


# 最新上架左边第一部。聊天图 489x981，含状态栏，按宽度缩放。
# 封面图上 x 18-145、y 670-880，中心 (81.5, 775)。
# x = 81.5 * 1440 / 489，y = 775 * 1440 / 489。
FIRST_LEFT_VIDEO_TAP = (240, 2282)
# tpl1792552755946.png record_pos=(0.012, -0.398)，resolution=(1440, 3040)。不用 Template。
# x = 0.012 * 720 + 720，y = -0.398 * 1520 + 1520。
MID_BUTTON_TAP = (729, 915)
# 试看结束会员墙右下「暂不开通」。云手机 back (1120,2955) 点不到这张墙，
# 只会让后续 06/07 留证长时间停在开通会员页，看起来像卡死。
# 按用户实拍 485×1024 缩放：img~(400,920) → x=400*1440/485，y=920*1440/485。
SKIP_MEMBERSHIP_TAP = (1188, 2730)
# 云手机底部导航右侧的返回键。keyevent 4 按不到这个键。
# 同一批云手机上箭头实色是 (1120, 2955)。
# 关掉会员墙后再连点两下云手机 back（中间不截图）。
CLOUD_BACK_TAP = (1120, 2955)
# tpl1792553178924.png record_pos=(-0.429, -0.901)，resolution=(1440, 3040)。不用 Template。
# x = -0.429 * 720 + 720，y = -0.901 * 1520 + 1520。
PAGE_BACK_TAP = (411, 150)
# 金币钱包页左上角返回（第 21→22）。
# 旧 PAGE_BACK_TAP_2=(403, 137) 落在标题栏空白（页底色），第 22 张仍停在钱包页。
# 实拍 shots/21.png 白箭头尖端约 (79, 216)；与 PAGE_BACK_TAP_3 详情返回不是同一点。
PAGE_BACK_TAP_2 = (79, 216)
# 第 8 张《渔王》详情页左上角返回。按 485×1024 实拍白箭头缩放：img~(33,74) → (99,221)。
# 与 PAGE_BACK_TAP=(411,150) 不是同一点；(411,150) 落在封面棕色区，会打空。
PAGE_BACK_TAP_3 = (99, 221)
# tpl1792553256775.png record_pos=(-0.128, 0.871)，resolution=(1440, 3040)。不用 Template。
# 原点 (628, 2844) 被手势带拒绝（y/3040>=0.92），只抬高 y。
# x 保持 628；y = 2794（2794/3040=0.919）。
BOTTOM_TAB_TAP_2 = (628, 2794)
# tpl1792553321134.png record_pos=(-0.001, 0.522)，resolution=(1440, 3040)。不用 Template。
# x = -0.001 * 720 + 720，y = 0.522 * 1520 + 1520。
MID_BUTTON_TAP_2 = (719, 2313)
# tpl1792553425603.png record_pos=(-0.001, 0.249)，resolution=(1440, 3040)。不用 Template。
# x = -0.001 * 720 + 720，y = 0.249 * 1520 + 1520。
MID_BUTTON_TAP_3 = (719, 1898)
# tpl1792553502758.png record_pos=(0.381, 0.751)，resolution=(1440, 3040)。不用 Template。
# x = 0.381 * 720 + 720，y = 0.751 * 1520 + 1520。
LOWER_BUTTON_TAP = (994, 2662)
# tpl1792553548505.png record_pos=(0.128, 0.871)，resolution=(1440, 3040)。不用 Template。
# 原点 (812, 2844) 被手势带拒绝（y/3040>=0.92），只抬高 y。
# x 保持 812；y = 2794（2794/3040=0.919）。
BOTTOM_TAB_TAP_3 = (812, 2794)
# tpl1792553591184.png record_pos=(0.374, 0.879)，resolution=(1440, 3040)。不用 Template。
# 原点 (989, 2856) 被手势带拒绝（y/3040>=0.92），只抬高 y。
# x 保持 989；y = 2794（2794/3040=0.919）。
BOTTOM_TAB_TAP_4 = (989, 2794)
# 底栏「我」。四等分最右格中心 x≈1260；y 同抬高到 2794。
# 第 14 张实拍仍停在「专题」：旧 BOTTOM_TAB_TAP_4=(989,2794) 落在专题带（约 x=900），打不到「我」。
ME_TAB_TAP = (1260, 2794)
# tpl1792553630877.png record_pos=(-0.001, 0.647)，resolution=(1440, 3040)。不用 Template。
# x = -0.001 * 720 + 720，y = 0.647 * 1520 + 1520。
MID_BUTTON_TAP_4 = (719, 2503)
# tpl1792553668995.png record_pos=(0.237, -0.474)，resolution=(1440, 3040)。不用 Template。
# x = 0.237 * 720 + 720，y = -0.474 * 1520 + 1520。
UPPER_BUTTON_TAP = (891, 800)
# 金币钱包「300 / ¥30」左上格（第 16→17）。
# 旧 MID_BUTTON_TAP_5=(495, 1356) 落在 300|500 缝隙（页底色），第 17 张仍停在钱包页未选中。
# 实拍 1440×3040：左列卡约 x=129–397、首行约 y=1168–1687，中心 (263, 1430)。
MID_BUTTON_TAP_5 = (263, 1430)
# tpl1792553755182.png record_pos=(-0.001, 0.647)，resolution=(1440, 3040)。不用 Template。
# x = -0.001 * 720 + 720，y = 0.647 * 1520 + 1520。
# 与 MID_BUTTON_TAP_4 同坐标，但是下一步。
MID_BUTTON_TAP_6 = (719, 2503)
# tpl1792553806214.png record_pos=(0.009, 0.841)，resolution=(1440, 3040)。不用 Template。
# 原点 (726, 2798) 被手势带拒绝（y/3040>=0.92），只抬高 y。
# x 保持 726；y = 2794（2794/3040=0.919）。
BOTTOM_BUTTON_TAP = (726, 2794)


def press_cloud_back():
    """云手机底部导航右侧的返回键。keyevent 4 按不到这个键。"""
    print("[mama] cloud phone back %s %s" % CLOUD_BACK_TAP)
    adb_shell("input tap %s %s" % CLOUD_BACK_TAP)


def press_cloud_back_twice():
    """第 5、6 张连续两下云手机 back。同一条 adb，中间不截图、不 sleep。

    capture_screen 会 screencap+pull，本身就像 timeout；插在两下 back 中间
    会导致第 7 张还停在影片里。
    """
    x, y = CLOUD_BACK_TAP
    print("[mama] cloud phone back x2 %s %s (no wait between)" % (x, y))
    adb_shell("input tap %s %s ; input tap %s %s" % (x, y, x, y))


print("[mama] ===== start =====")
connect_device()
start_app()
print("[mama] wait 1s")
time.sleep(1.0)
capture_screen("01")
print("[mama] wait 10s")
time.sleep(10.0)
capture_screen("02")
print("[mama] tap %s %s" % FIRST_LEFT_VIDEO_TAP)
adb_shell("input tap %s %s" % FIRST_LEFT_VIDEO_TAP)
time.sleep(1.0)
capture_screen("03")
print("[mama] tap %s %s" % MID_BUTTON_TAP)
adb_shell("input tap %s %s" % MID_BUTTON_TAP)
time.sleep(1.0)
capture_screen("04")
print("[mama] wait 5s")
time.sleep(5.0)
capture_screen("05")
# 试看结束会员墙：先点「暂不开通」，再 cloud_back 两下离开播放页。
print("[mama] tap skip-membership %s %s" % SKIP_MEMBERSHIP_TAP)
adb_shell("input tap %s %s" % SKIP_MEMBERSHIP_TAP)
time.sleep(0.8)
# 第 5+6：连续两下云手机 back。中间不截图、不 sleep（截图会拖慢，第 7 张会还在影片里）。
press_cloud_back_twice()
capture_screen("06")
capture_screen("07")
# 第 8 张《渔王》详情左上角返回；旧 PAGE_BACK_TAP=(411,150) 打在封面上会空。
print("[mama] tap page-back3 %s %s" % PAGE_BACK_TAP_3)
adb_shell("input tap %s %s" % PAGE_BACK_TAP_3)
time.sleep(1.0)
capture_screen("08")
print("[mama] tap %s %s" % BOTTOM_TAB_TAP_2)
adb_shell("input tap %s %s" % BOTTOM_TAB_TAP_2)
time.sleep(1.0)
capture_screen("09")
print("[mama] tap %s %s" % MID_BUTTON_TAP_2)
adb_shell("input tap %s %s" % MID_BUTTON_TAP_2)
time.sleep(1.0)
capture_screen("10")
print("[mama] tap %s %s" % MID_BUTTON_TAP_3)
adb_shell("input tap %s %s" % MID_BUTTON_TAP_3)
time.sleep(1.0)
capture_screen("11")
print("[mama] tap %s %s" % LOWER_BUTTON_TAP)
adb_shell("input tap %s %s" % LOWER_BUTTON_TAP)
time.sleep(1.0)
capture_screen("12")
print("[mama] tap %s %s" % BOTTOM_TAB_TAP_3)
adb_shell("input tap %s %s" % BOTTOM_TAB_TAP_3)
time.sleep(1.0)
capture_screen("13")
print("[mama] tap me-tab %s %s" % ME_TAB_TAP)
adb_shell("input tap %s %s" % ME_TAB_TAP)
time.sleep(1.0)
capture_screen("14")
print("[mama] tap %s %s" % MID_BUTTON_TAP_4)
adb_shell("input tap %s %s" % MID_BUTTON_TAP_4)
time.sleep(1.0)
capture_screen("15")
print("[mama] tap %s %s" % UPPER_BUTTON_TAP)
adb_shell("input tap %s %s" % UPPER_BUTTON_TAP)
time.sleep(1.0)
capture_screen("16")
print("[mama] tap %s %s" % MID_BUTTON_TAP_5)
adb_shell("input tap %s %s" % MID_BUTTON_TAP_5)
time.sleep(1.0)
capture_screen("17")
print("[mama] tap %s %s" % MID_BUTTON_TAP_6)
adb_shell("input tap %s %s" % MID_BUTTON_TAP_6)
time.sleep(1.0)
capture_screen("18")
print("[mama] tap %s %s" % BOTTOM_BUTTON_TAP)
adb_shell("input tap %s %s" % BOTTOM_BUTTON_TAP)
time.sleep(1.0)
capture_screen("19")
print("[mama] wait 5s (browser)")
time.sleep(5.0)
capture_screen("20")
press_cloud_back()
time.sleep(1.0)
capture_screen("21")
print("[mama] tap %s %s" % PAGE_BACK_TAP_2)
adb_shell("input tap %s %s" % PAGE_BACK_TAP_2)
time.sleep(1.0)
capture_screen("22")
# 其余流程自行重写。





