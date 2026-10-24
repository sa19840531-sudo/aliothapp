"""哔咔漫画產品編排：按 latest.py 的順序逐步執行，不把點位交回通用 Agent。

通用 Agent 只負責啟動下一步、記錄系統在動作後截的圖，並做驗證。
觸控、等待、前台回復與順序都留在 latest.py。這裡不讀視覺框，不改座標。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from services.api.app.application.app_script_playback import (
    apply_script_playback_progress,
    next_script_playback,
    parse_script_playback_steps,
)
from services.api.app.domain.exploration import (
    ExplorationTerminal,
    ExplorationWork,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    SurfaceKind,
)

# 哔咔漫画。只認這個包，不按名稱猜其他產品。
LATEST_PACKAGE = "com.lnhlz.livzq"
LATEST_SCRIPT_DONE_KEY = "latest_script_completed"
LATEST_SCRIPT_INDEX_KEY = "latest_script_index"
LATEST_SCRIPT_ATTEMPTED_KEY = "latest_script_attempted"
LATEST_SCRIPT_REASON_CODE = "latest_script_completed"
LATEST_SCRIPT_BLOCKED_CODE = "latest_script_blocked"
# AirtestIDE 正在維護的錄製腳本。可用 LATEST_SCRIPT 指到別的副本。
DEFAULT_LATEST_SCRIPT = Path(
    r"C:\Users\show\AppData\Local\Temp\AirtestIDE\scripts\untitled.air"
    r"\latest.air\latest.py"
)
# 進應用與最後一次留證。缺圖只記驗證失敗，不改由 Agent 重選點。
_REQUIRED_SHOTS = ("01", "37")


def resolve_latest_script_path() -> Path:
    override = os.environ.get("LATEST_SCRIPT", "").strip()
    if override:
        return Path(override)
    return DEFAULT_LATEST_SCRIPT


def _metadata(work: ExplorationWork) -> Mapping[str, object]:
    metadata = work.frontier.observation.metadata
    return metadata if isinstance(metadata, Mapping) else {}


def _package_matches(package_name: object) -> bool:
    return str(package_name or "").strip().casefold() == LATEST_PACKAGE.casefold()


def _is_latest_work(work: ExplorationWork) -> bool:
    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        return False
    if _package_matches(_metadata(work).get("package_name")):
        return True
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    return any(_package_matches(item) for item in allowed)


def _expected_package(work: ExplorationWork) -> str:
    package_name = str(_metadata(work).get("package_name") or "").strip()
    if _package_matches(package_name):
        return package_name
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    for item in allowed:
        if _package_matches(item):
            return str(item).strip()
    return LATEST_PACKAGE


def script_latest_override(
    work: ExplorationWork,
) -> ActionIntent | ExplorationTerminal | None:
    """哔咔漫画的下一步是 latest.py 的下一筆。其他產品返回 None。"""

    if not isinstance(work, ExplorationWork) or not _is_latest_work(work):
        return None
    return next_script_playback(
        work,
        steps=parse_script_playback_steps(resolve_latest_script_path()),
        prefix="latest",
        index_key=LATEST_SCRIPT_INDEX_KEY,
        done_key=LATEST_SCRIPT_DONE_KEY,
        reason_code=LATEST_SCRIPT_REASON_CODE,
        expected_package=_expected_package(work),
        script_name="latest.py",
    )


def apply_latest_script_progress(
    result: dict[str, object],
    *,
    prior: Mapping[str, object],
    completed_action: object,
    action_succeeded: bool | None,
) -> None:
    """這一步成功才前進。失敗停在同一步，不把點位交回通用 Agent。"""

    apply_script_playback_progress(
        result,
        prior=prior,
        completed_action=completed_action,
        action_succeeded=action_succeeded,
        steps=parse_script_playback_steps(resolve_latest_script_path()),
        prefix="latest",
        index_key=LATEST_SCRIPT_INDEX_KEY,
        done_key=LATEST_SCRIPT_DONE_KEY,
    )


def serial_from_android_driver(driver: object) -> str:
    """只用目前這台裝置的端點。沒有端點就失敗，不猜別台雲手機。"""

    controller = getattr(driver, "_cloud_phone_controller", None)
    endpoint = getattr(controller, "endpoint", None)
    if isinstance(endpoint, str) and endpoint.strip():
        return endpoint.strip()
    for key in ("ALIOTH_ADB_SERIAL", "ANDROID_SERIAL", "LATEST_SERIAL"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    raise RuntimeError("哔咔漫画录制脚本缺少当前设备 serial")


def verify_latest_recording_shots(
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


def _collect_shots(
    shots_dir: Path,
    *,
    before: Mapping[str, tuple[int, int]],
) -> list[dict[str, object]]:
    from services.api.app.application.app_recording_evidence import collect_new_shots

    return collect_new_shots(shots_dir, before=before)


def run_latest_recording_script(
    *,
    serial: str,
    script_path: Path | None = None,
    runner: Callable[..., object] | None = None,
) -> dict[str, object]:
    """啟動整份 latest.py。呼叫端只拿截圖清單做記錄與驗證。"""

    device = str(serial or "").strip()
    if not device:
        raise RuntimeError("哔咔漫画录制脚本缺少当前设备 serial")
    path = script_path or resolve_latest_script_path()
    if not path.is_file():
        return {
            "ok": False,
            "error_code": "LATEST_SCRIPT_MISSING",
            "script_path": str(path),
            "serial": device,
            "shots": [],
            "verification": verify_latest_recording_shots(()),
        }
    timeout_raw = os.environ.get("LATEST_SCRIPT_TIMEOUT_SECONDS", "").strip()
    try:
        timeout = int(timeout_raw) if timeout_raw else 900
    except ValueError:
        timeout = 900
    timeout = min(1800, max(30, timeout))
    env = os.environ.copy()
    env["LATEST_SERIAL"] = device
    env["ANDROID_SERIAL"] = device
    script_text = path.read_text(encoding="utf-8")
    from services.api.app.application.app_recording_evidence import (
        script_operations,
        snapshot_shot_files,
    )

    before = snapshot_shot_files(path.parent / "shots")
    operations = script_operations(script_text)
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
        shots = _collect_shots(path.parent / "shots", before=before)
        return {
            "ok": False,
            "error_code": "LATEST_SCRIPT_TIMEOUT",
            "script_path": str(path),
            "serial": device,
            "shots": shots,
            "operations": operations,
            "verification": verify_latest_recording_shots(shots),
        }
    return_code = int(getattr(completed, "returncode", 1))
    shots = _collect_shots(path.parent / "shots", before=before)
    verification = verify_latest_recording_shots(shots)
    verified = verification.get("verified") is True
    stderr = str(getattr(completed, "stderr", "") or "")
    return {
        "ok": return_code == 0 and verified,
        "error_code": (
            None
            if return_code == 0 and verified
            else "LATEST_SCRIPT_FAILED"
            if return_code != 0
            else "LATEST_SCRIPT_EVIDENCE_MISSING"
        ),
        "return_code": return_code,
        "script_path": str(path),
        "serial": device,
        "stderr_tail": stderr[-500:],
        "shots": shots,
        "operations": operations,
        "verification": verification,
    }
