"""页面持续非空闲时，一次性读取当前可访问性节点，不等待页面静止。"""

from __future__ import annotations

import base64
import shlex
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree


def dump_current_ui_hierarchy(
    shell_output: Callable[[list[str], float], str],
    shell: Callable[[list[str], float], None],
    log: Callable[[str], None],
) -> str:
    """只在标准 dump 明确 idle 超时后调用；每次独立文件，失败不复用旧树。"""
    token = uuid4().hex
    remote_jar = f"/data/local/tmp/alioth_uia_{token}.jar"
    remote_xml = f"/data/local/tmp/alioth_uia_{token}.xml"
    payload = Path(__file__).with_name("android").joinpath("current_hierarchy.jar").read_bytes()
    encoded = base64.b64encode(payload).decode("ascii")
    classpath = ":".join((
        "/system/framework/android.test.runner.jar",
        "/system/framework/android.test.base.jar",
        "/system/framework/uiautomator.jar",
        remote_jar,
    ))
    try:
        shell(["sh", "-c", shlex.quote(f"printf %s {encoded} | base64 -d > {remote_jar}")], 10.0)
        # 系统包装脚本会改写 ANDROID_DATA，legacy dump 随之写错目录。
        # 直接调用相同入口，权限仍是原 ADB shell，不安装 APK、不启常驻服务。
        output = shell_output([
            "env", "ANDROID_DATA=/data", f"CLASSPATH={classpath}",
            "app_process", "/system/bin", "com.android.commands.uiautomator.Launcher",
            "runtest", "-c", "com.alioth.observation.CurrentHierarchy#testDump",
            "-e", "output", remote_xml, "-e", "jars", remote_jar,
        ], 20.0)
        if "OK (1 test)" not in output or "FAILURES!!!" in output or "shortMsg=" in output:
            raise RuntimeError("UIA_CURRENT_ROOT_DUMP_FAILED：一次性节点采集未成功")
        text = shell_output(["cat", remote_xml], 10.0).strip()
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise RuntimeError("UIA_CURRENT_ROOT_INVALID_XML：未取得有效当前节点") from exc
        if root.tag != "hierarchy" or root.find("node") is None:
            raise RuntimeError("UIA_CURRENT_ROOT_INVALID_XML：未取得有效当前节点")
        # 制品标明没有等待 idle；取得结构不能单独证明页面稳定或业务到达。
        root.set("alioth-observation-mode", "current_root_without_idle")
        text = ElementTree.tostring(root, encoding="unicode")
        log(f"ui_tree dump mode=current_root_without_idle bytes={len(text.encode('utf-8'))}")
        return text
    finally:
        try:
            shell(["rm", "-f", remote_jar, remote_xml], 3.0)
        except Exception as exc:  # noqa: BLE001 - 清理不能覆盖采集原始结果
            log(f"ui_tree fallback cleanup skipped: {type(exc).__name__}")
