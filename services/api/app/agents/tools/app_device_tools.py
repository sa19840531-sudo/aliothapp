"""Android 设备试点 Tool：截图 / 点击 / 云手机 back（不经产品 Airtest 脚本）。

环境变量：

- ``ALIOTH_ADB_SERIAL``：设备 serial（可含 ``host:port``）
- ``ALIOTH_ADB_PATH``：adb 可执行文件；未设则用 ``adb``
- ``ALIOTH_APP_SHOT_DIR``：截图落盘目录；未设则系统临时目录下 ``alioth_app_shots``
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from services.api.app.agents.tools.registry import PermissionLevel, tool
from services.api.app.domain.app_product import load_profile

logger = logging.getLogger(__name__)

_DEFAULT_CLOUD_BACK = (1120, 2955)
_CATEGORY = "app-device"


def _adb_bin() -> str:
    return (os.getenv("ALIOTH_ADB_PATH") or "adb").strip() or "adb"


def _serial() -> str:
    serial = (os.getenv("ALIOTH_ADB_SERIAL") or "").strip()
    if not serial:
        raise RuntimeError("未设置 ALIOTH_ADB_SERIAL，无法调用设备 Tool")
    return serial


def _shot_dir() -> Path:
    raw = (os.getenv("ALIOTH_APP_SHOT_DIR") or "").strip()
    if raw:
        path = Path(raw)
    else:
        path = Path(tempfile.gettempdir()) / "alioth_app_shots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _adb(args: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    cmd = [_adb_bin(), "-s", _serial(), *args]
    logger.info("app device adb: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _resolve_cloud_back(package_name: str | None) -> tuple[int, int]:
    if package_name:
        profile = load_profile(package_name)
        if profile is not None:
            return profile.cloud_back_tap
    return _DEFAULT_CLOUD_BACK


def _shell_ok(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:400]
        raise RuntimeError(f"adb 失败 code={result.returncode}: {err}")


def load_app_product_profile_impl(package_name: str) -> dict[str, Any]:
    """按包加载只读能力事实（F04）：不含绝对坐标／taps。"""
    from services.api.app.domain.app_product.profiles import (
        build_app_product_capability_facts,
    )

    return build_app_product_capability_facts(package_name)


def app_screenshot_impl(name: str = "shot") -> dict[str, Any]:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (name or "shot"))[:64]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    local = _shot_dir() / f"{safe}_{stamp}.png"
    remote = f"/sdcard/alioth_app_{safe}_{stamp}.png"
    _shell_ok(_adb(["shell", "screencap", "-p", remote], timeout=90))
    pull = _adb(["pull", remote, str(local)], timeout=90)
    _adb(["shell", "rm", "-f", remote], timeout=30)
    _shell_ok(pull)
    if not local.is_file() or local.stat().st_size < 32:
        raise RuntimeError(f"截图 pull 失败或文件过小: {local}")
    return {"ok": True, "path": str(local), "bytes": local.stat().st_size}


def app_tap_impl(x: int, y: int) -> dict[str, Any]:
    _shell_ok(_adb(["shell", "input", "tap", str(int(x)), str(int(y))], timeout=30))
    return {"ok": True, "x": int(x), "y": int(y)}


def app_cloud_back_impl(package_name: str | None = None) -> dict[str, Any]:
    x, y = _resolve_cloud_back(package_name)
    _shell_ok(_adb(["shell", "input", "tap", str(x), str(y)], timeout=30))
    return {"ok": True, "action": "cloud_back", "x": x, "y": y, "package_name": package_name}


def app_cloud_back_twice_impl(package_name: str | None = None) -> dict[str, Any]:
    x, y = _resolve_cloud_back(package_name)
    shell = f"input tap {x} {y} ; input tap {x} {y}"
    _shell_ok(_adb(["shell", shell], timeout=30))
    return {
        "ok": True,
        "action": "cloud_back_twice",
        "x": x,
        "y": y,
        "package_name": package_name,
        "forbid_between": ["sleep", "screenshot", "capture_screen"],
    }


def app_tap_from_profile_impl(package_name: str, tap_key: str) -> dict[str, Any]:
    profile = load_profile(package_name)
    if profile is None:
        return {"ok": False, "error": "profile_not_found", "package_name": package_name}
    # 关窗类键即使写在 profile.json 也不允许脚本式点击——弹窗在则云手机 back。
    from services.api.app.domain.app_product.profiles import is_vision_only_tap_key

    if is_vision_only_tap_key(tap_key):
        return {
            "ok": False,
            "error": "popup_close_requires_vision",
            "package_name": package_name,
            "tap_key": tap_key,
            "popup_close_policy": profile.popup_close_policy,
            "hint": (
                "弹窗广告：优先文案关钮／几何安全圆 X／蒙层空白；"
                "仍挡路再用 app_cloud_back_twice_tool 连点两下底栏「<」；"
                "开屏/首屏用语义进入/跳过；禁止脚本固定点"
            ),
        }
    point = profile.key_taps.get(tap_key)
    if point is None:
        return {
            "ok": False,
            "error": "tap_key_not_found",
            "package_name": package_name,
            "tap_key": tap_key,
            "available": sorted(profile.key_taps.keys()),
        }
    x, y = profile.clamp_tap_y(*point)
    _shell_ok(_adb(["shell", "input", "tap", str(x), str(y)], timeout=30))
    return {"ok": True, "tap_key": tap_key, "x": x, "y": y, "package_name": package_name}


@tool(
    name="load_app_product_profile_tool",
    description=(
        "按当前 package_name 加载只读 Product 能力事实："
        "时序／关窗策略／语义恢复提示；禁止返回绝对坐标或可执行 taps。"
    ),
    category=_CATEGORY,
    permission_level=PermissionLevel.SAFE,
)
def load_app_product_profile_tool(package_name: str) -> dict[str, Any]:
    """返回当前包只读能力事实；找不到时 ok=false，仍可通用探索。"""
    return load_app_product_profile_impl(package_name)


@tool(
    name="app_screenshot_tool",
    description=(
        "对当前 Android 设备截图并 pull 到本机；返回本地路径。"
        "不要插在 cloud_back_twice 两下之间。"
    ),
    category=_CATEGORY,
    permission_level=PermissionLevel.SENSITIVE,
)
def app_screenshot_tool(name: str = "shot") -> dict[str, Any]:
    """screencap 到设备再 pull；关键时序中间禁止调用。"""
    return app_screenshot_impl(name)


@tool(
    name="app_tap_tool",
    description="对当前设备执行 input tap(x,y)。优先使用 Product Profile 中的 taps 坐标。",
    category=_CATEGORY,
    permission_level=PermissionLevel.SENSITIVE,
)
def app_tap_tool(x: int, y: int) -> dict[str, Any]:
    """绝对坐标点击。"""
    return app_tap_impl(x, y)


@tool(
    name="app_cloud_back_tool",
    description=(
        "点击云手机底部系统导航栏最右侧「<」返回箭头"
        "（||| / Home / < 三键的右边那个；Profile cloud_back_tap，默认 1120,2955；"
        "ADB input tap，不是 keyevent 4）。"
        "关弹窗用此键，禁止点弹窗中间灰色圆 ×。"
        "可传 package_name 覆盖落点。"
    ),
    category=_CATEGORY,
    permission_level=PermissionLevel.SENSITIVE,
)
def app_cloud_back_tool(package_name: str | None = None) -> dict[str, Any]:
    """单次云手机 back（底部导航栏最右侧「<」）。"""
    return app_cloud_back_impl(package_name)


@tool(
    name="app_cloud_back_twice_tool",
    description=(
        "同一条 adb 连续两下底部导航栏最右侧「<」；中间无 sleep、无截图。"
        "MaMa 会员拦截必须用此 Tool；禁止点弹窗圆 ×。"
    ),
    category=_CATEGORY,
    permission_level=PermissionLevel.SENSITIVE,
)
def app_cloud_back_twice_tool(package_name: str | None = None) -> dict[str, Any]:
    """连续两下云手机 back，禁止在两次之间插截图。"""
    return app_cloud_back_twice_impl(package_name)


@tool(
    name="app_tap_from_profile_tool",
    description="按 Product Profile 的 taps 键名点击（如 me_tab、detail_page_back）。",
    category=_CATEGORY,
    permission_level=PermissionLevel.SENSITIVE,
)
def app_tap_from_profile_tool(package_name: str, tap_key: str) -> dict[str, Any]:
    """从 Profile.taps 取坐标再 tap。"""
    return app_tap_from_profile_impl(package_name, tap_key)
