"""P站產品編排：整份執行 P.py，不把點位拆回通用 Agent。

通用 Agent 只負責啟動這份錄製腳本，並把腳本寫出的截圖記入證據、做驗證。
觸控、等待、前台回復與順序都留在 P.py。這裡不讀視覺框，不改座標。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from services.api.app.application.app_script_playback import (
    apply_script_playback_progress,
    next_script_playback,
    parse_script_playback_steps,
)
from services.api.app.application.app_ui_tree_features import (
    resolve_product_orchestrator_id,
)
from services.api.app.domain.exploration import (
    ExplorationTerminal,
    ExplorationWork,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    SurfaceKind,
)

PZHAN_PACKAGE = "com.defaultns.base"
PZHAN_ORCHESTRATOR_ID = "pzhan"
PZHAN_SCRIPT_DONE_KEY = "pzhan_script_completed"
PZHAN_SCRIPT_INDEX_KEY = "pzhan_script_index"
PZHAN_SCRIPT_ATTEMPTED_KEY = "pzhan_script_attempted"
PZHAN_SCRIPT_REASON_CODE = "pzhan_script_completed"
PZHAN_SCRIPT_BLOCKED_CODE = "pzhan_script_blocked"
# AirtestIDE 正在維護的錄製腳本。可用 P_SCRIPT 指到別的副本。
DEFAULT_PZHAN_SCRIPT = Path(
    r"C:\Users\show\AppData\Local\Temp\AirtestIDE\scripts\untitled.air\P.air\P.py"
)
# 進應用與最後一次頁面返回的留證。缺圖只記驗證失敗，不改由 Agent 重選點。
_REQUIRED_SHOTS = ("01", "36")


def resolve_pzhan_script_path() -> Path:
    override = os.environ.get("P_SCRIPT", "").strip()
    if override:
        return Path(override)
    return DEFAULT_PZHAN_SCRIPT


def _metadata(work: ExplorationWork) -> Mapping[str, object]:
    metadata = work.frontier.observation.metadata
    return metadata if isinstance(metadata, Mapping) else {}


def _is_pzhan_work(work: ExplorationWork) -> bool:
    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        return False
    package_name = str(_metadata(work).get("package_name") or "").strip()
    if resolve_product_orchestrator_id(package_name=package_name) == PZHAN_ORCHESTRATOR_ID:
        return True
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    return any(
        resolve_product_orchestrator_id(package_name=str(item)) == PZHAN_ORCHESTRATOR_ID
        for item in allowed
    )


def _expected_package(work: ExplorationWork) -> str:
    package_name = str(_metadata(work).get("package_name") or "").strip()
    if package_name == PZHAN_PACKAGE:
        return package_name
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    for item in allowed:
        if str(item).strip() == PZHAN_PACKAGE:
            return PZHAN_PACKAGE
    return package_name or PZHAN_PACKAGE


def script_pzhan_override(
    work: ExplorationWork,
) -> ActionIntent | ExplorationTerminal | None:
    """P站的下一步只有「跑完整份腳本」或收口。其他產品返回 None。"""

    if not isinstance(work, ExplorationWork) or not _is_pzhan_work(work):
        return None
    return next_script_playback(
        work,
        steps=parse_script_playback_steps(resolve_pzhan_script_path()),
        prefix="pzhan",
        index_key=PZHAN_SCRIPT_INDEX_KEY,
        done_key=PZHAN_SCRIPT_DONE_KEY,
        reason_code=PZHAN_SCRIPT_REASON_CODE,
        expected_package=_expected_package(work),
        script_name="P.py",
    )


def apply_pzhan_script_progress(
    result: dict[str, object],
    *,
    prior: Mapping[str, object],
    completed_action: object,
    action_succeeded: bool | None,
) -> None:
    """腳本整份跑完才算完成。失敗只標記已嘗試，不把點位交回通用 Agent。"""

    apply_script_playback_progress(
        result,
        prior=prior,
        completed_action=completed_action,
        action_succeeded=action_succeeded,
        steps=parse_script_playback_steps(resolve_pzhan_script_path()),
        prefix="pzhan",
        index_key=PZHAN_SCRIPT_INDEX_KEY,
        done_key=PZHAN_SCRIPT_DONE_KEY,
    )


def serial_from_android_driver(driver: object) -> str:
    """只用目前這台裝置的端點。沒有端點就失敗，不猜別台雲手機。"""

    controller = getattr(driver, "_cloud_phone_controller", None)
    endpoint = getattr(controller, "endpoint", None)
    if isinstance(endpoint, str) and endpoint.strip():
        return endpoint.strip()
    for key in ("ALIOTH_ADB_SERIAL", "ANDROID_SERIAL", "P_SERIAL"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    raise RuntimeError("P 录制脚本缺少当前设备 serial")


def verify_pzhan_recording_shots(
    shots: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """只核對腳本有沒有交出留證，不根據畫面改點。"""

    names = [str(item.get("name") or "") for item in shots]
    present = set(names)
    missing = [name for name in _REQUIRED_SHOTS if name not in present]
    return {
        "recorded": len(shots),
        "shot_names": names,
        "missing_required": missing,
        "verified": not missing,
        "point_selection": "recording_script",
    }


def _collect_shots(shots_dir: Path, *, started: float) -> list[dict[str, object]]:
    if not shots_dir.is_dir():
        return []
    found: list[dict[str, object]] = []
    for path in sorted(shots_dir.glob("*.png")):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime + 1 < started or stat.st_size <= 8:
            continue
        found.append({"name": path.stem, "path": str(path), "bytes": int(stat.st_size)})
    return found


def run_pzhan_recording_script(
    *,
    serial: str,
    script_path: Path | None = None,
    runner: Callable[..., object] | None = None,
) -> dict[str, object]:
    """啟動整份 P.py。呼叫端只拿截圖清單做記錄與驗證。"""

    device = str(serial or "").strip()
    if not device:
        raise RuntimeError("P 录制脚本缺少当前设备 serial")
    path = script_path or resolve_pzhan_script_path()
    if not path.is_file():
        return {
            "ok": False,
            "error_code": "PZHAN_SCRIPT_MISSING",
            "script_path": str(path),
            "serial": device,
            "shots": [],
            "verification": verify_pzhan_recording_shots(()),
        }
    timeout_raw = os.environ.get("P_SCRIPT_TIMEOUT_SECONDS", "").strip()
    try:
        timeout = int(timeout_raw) if timeout_raw else 900
    except ValueError:
        timeout = 900
    timeout = min(1800, max(30, timeout))
    env = os.environ.copy()
    env["P_SERIAL"] = device
    env["ANDROID_SERIAL"] = device
    started = time.time()
    command = [sys.executable, str(path), device]
    invoke = runner or subprocess.run
    try:
        completed = invoke(
            command,
            cwd=str(path.parent),
            env=env,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        shots = _collect_shots(path.parent / "shots", started=started)
        return {
            "ok": False,
            "error_code": "PZHAN_SCRIPT_TIMEOUT",
            "script_path": str(path),
            "serial": device,
            "shots": shots,
            "verification": verify_pzhan_recording_shots(shots),
        }
    return_code = int(getattr(completed, "returncode", 1))
    shots = _collect_shots(path.parent / "shots", started=started)
    stderr = str(getattr(completed, "stderr", "") or "")
    return {
        "ok": return_code == 0,
        "error_code": None if return_code == 0 else "PZHAN_SCRIPT_FAILED",
        "return_code": return_code,
        "script_path": str(path),
        "serial": device,
        "stderr_tail": stderr[-500:],
        "shots": shots,
        "verification": verify_pzhan_recording_shots(shots),
    }
