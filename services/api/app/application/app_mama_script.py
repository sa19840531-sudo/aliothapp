"""MaMa短剧產品編排：整份沿用 mama.py，不把點位拆回通用 Agent。

通用 Agent 只負責啟動這份錄製腳本，並把腳本寫出的截圖記入證據、做驗證。
觸控、等待、前台回復與順序都留在 mama.py。這裡不讀視覺框，不改座標。
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

MAMA_PACKAGE = "com.dzu4.app"
MAMA_ORCHESTRATOR_ID = "mama"
MAMA_SCRIPT_DONE_KEY = "mama_script_completed"
MAMA_SCRIPT_INDEX_KEY = "mama_script_index"
MAMA_SCRIPT_ATTEMPTED_KEY = "mama_script_attempted"
MAMA_SCRIPT_REASON_CODE = "mama_script_completed"
MAMA_SCRIPT_BLOCKED_CODE = "mama_script_blocked"
# AirtestIDE 正在維護的錄製腳本。可用 MAMA_SCRIPT 指到別的副本。
DEFAULT_MAMA_SCRIPT = Path(
    r"C:\Users\show\AppData\Local\Temp\AirtestIDE\scripts\untitled.air"
    r"\mama.air\mama.py"
)
# 質檢 worker 在容器裡，讀不到上面的 Windows 路徑。這份是同一支腳本的工作區副本。
_BUNDLED_MAMA_SCRIPT = (
    Path(__file__).resolve().parents[2] / "resources" / "recording" / "mama.py"
)
# 進應用與最後一次頁面返回的留證。缺圖只記驗證失敗，不改由 Agent 重選點。
_REQUIRED_SHOTS = ("01", "22")


def resolve_mama_script_path() -> Path:
    """優先用仍存在的顯式路徑或本機 Airtest 腳本，否則用容器讀得到的副本。"""

    override = os.environ.get("MAMA_SCRIPT", "").strip()
    if override:
        path = Path(override)
        if path.is_file():
            return path
    if DEFAULT_MAMA_SCRIPT.is_file():
        return DEFAULT_MAMA_SCRIPT
    if _BUNDLED_MAMA_SCRIPT.is_file():
        return _BUNDLED_MAMA_SCRIPT
    if override:
        return Path(override)
    return DEFAULT_MAMA_SCRIPT


def _metadata(work: ExplorationWork) -> Mapping[str, object]:
    metadata = work.frontier.observation.metadata
    return metadata if isinstance(metadata, Mapping) else {}


def _is_mama_work(work: ExplorationWork) -> bool:
    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        return False
    package_name = str(_metadata(work).get("package_name") or "").strip()
    if (
        resolve_product_orchestrator_id(package_name=package_name)
        == MAMA_ORCHESTRATOR_ID
    ):
        return True
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    return any(
        resolve_product_orchestrator_id(package_name=str(item))
        == MAMA_ORCHESTRATOR_ID
        for item in allowed
    )


def _expected_package(work: ExplorationWork) -> str:
    package_name = str(_metadata(work).get("package_name") or "").strip()
    if package_name.casefold() == MAMA_PACKAGE:
        return package_name
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    for item in allowed:
        if str(item).strip().casefold() == MAMA_PACKAGE:
            return str(item).strip()
    return package_name or "com.dzu4.app"


def script_mama_override(
    work: ExplorationWork,
) -> ActionIntent | ExplorationTerminal | None:
    """MaMa 的下一步來自 mama.py。做完由系統截動作後的圖。其他產品返回 None。"""

    if not isinstance(work, ExplorationWork) or not _is_mama_work(work):
        return None
    return next_script_playback(
        work,
        steps=parse_script_playback_steps(resolve_mama_script_path()),
        prefix="mama",
        index_key=MAMA_SCRIPT_INDEX_KEY,
        done_key=MAMA_SCRIPT_DONE_KEY,
        reason_code=MAMA_SCRIPT_REASON_CODE,
        expected_package=_expected_package(work),
        script_name="mama.py",
    )


def apply_mama_script_progress(
    result: dict[str, object],
    *,
    prior: Mapping[str, object],
    completed_action: object,
    action_succeeded: bool | None,
) -> None:
    """這一步成功才進下一步。失敗留在同一步，不整份重跑。"""

    apply_script_playback_progress(
        result,
        prior=prior,
        completed_action=completed_action,
        action_succeeded=action_succeeded,
        steps=parse_script_playback_steps(resolve_mama_script_path()),
        prefix="mama",
        index_key=MAMA_SCRIPT_INDEX_KEY,
        done_key=MAMA_SCRIPT_DONE_KEY,
    )


def serial_from_android_driver(driver: object) -> str:
    """只用目前這台裝置的端點。沒有端點就失敗，不猜別台雲手機。"""

    controller = getattr(driver, "_cloud_phone_controller", None)
    endpoint = getattr(controller, "endpoint", None)
    if isinstance(endpoint, str) and endpoint.strip():
        return endpoint.strip()
    for key in ("ALIOTH_ADB_SERIAL", "ANDROID_SERIAL", "MAMA_SERIAL"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    raise RuntimeError("MaMa录制脚本缺少当前设备 serial")


def verify_mama_recording_shots(
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


def run_mama_recording_script(
    *,
    serial: str,
    script_path: Path | None = None,
    runner: Callable[..., object] | None = None,
) -> dict[str, object]:
    """啟動整份 mama.py。呼叫端只拿截圖清單做記錄與驗證。"""

    device = str(serial or "").strip()
    if not device:
        raise RuntimeError("MaMa录制脚本缺少当前设备 serial")
    path = script_path or resolve_mama_script_path()
    if not path.is_file():
        return {
            "ok": False,
            "error_code": "MAMA_SCRIPT_MISSING",
            "script_path": str(path),
            "serial": device,
            "shots": [],
            "verification": verify_mama_recording_shots(()),
        }
    timeout_raw = os.environ.get("MAMA_SCRIPT_TIMEOUT_SECONDS", "").strip()
    try:
        timeout = int(timeout_raw) if timeout_raw else 900
    except ValueError:
        timeout = 900
    timeout = min(1800, max(30, timeout))
    env = os.environ.copy()
    env["MAMA_SERIAL"] = device
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
            "error_code": "MAMA_SCRIPT_TIMEOUT",
            "script_path": str(path),
            "serial": device,
            "shots": shots,
            "operations": operations,
            "verification": verify_mama_recording_shots(shots),
        }
    return_code = int(getattr(completed, "returncode", 1))
    shots = _collect_shots(path.parent / "shots", before=before)
    verification = verify_mama_recording_shots(shots)
    verified = verification.get("verified") is True
    stderr = str(getattr(completed, "stderr", "") or "")
    return {
        "ok": return_code == 0 and verified,
        "error_code": (
            None
            if return_code == 0 and verified
            else "MAMA_SCRIPT_FAILED"
            if return_code != 0
            else "MAMA_SCRIPT_EVIDENCE_MISSING"
        ),
        "return_code": return_code,
        "script_path": str(path),
        "serial": device,
        "stderr_tail": stderr[-500:],
        "shots": shots,
        "operations": operations,
        "verification": verification,
    }
