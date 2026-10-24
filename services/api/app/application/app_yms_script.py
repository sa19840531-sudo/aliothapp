"""欲漫涩產品編排：一步一步執行 yms.py，不把點位交回通用 Agent。

每一步做完，由系統截一張動作後的圖，Agent 直接看那張圖。
座標仍是腳本裡的點，不按畫面重選。
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

YMS_PACKAGE = "dark.tozhn.wzmth"
YMS_ORCHESTRATOR_ID = "yms"
YMS_SCRIPT_DONE_KEY = "yms_script_completed"
YMS_SCRIPT_INDEX_KEY = "yms_script_index"
YMS_SCRIPT_ATTEMPTED_KEY = "yms_script_attempted"
YMS_SCRIPT_REASON_CODE = "yms_script_completed"
YMS_SCRIPT_BLOCKED_CODE = "yms_script_blocked"
# AirtestIDE 正在維護的錄製腳本。可用 YMS_SCRIPT 指到別的副本。
DEFAULT_YMS_SCRIPT = Path(
    r"C:\Users\show\AppData\Local\Temp\AirtestIDE\scripts\untitled.air\yms.air\yms.py"
)
# 進應用與最後一次頁面返回的留證。缺圖只記驗證失敗，不改由 Agent 重選點。
_REQUIRED_SHOTS = ("01", "50")


def resolve_yms_script_path() -> Path:
    override = os.environ.get("YMS_SCRIPT", "").strip()
    if override:
        return Path(override)
    return DEFAULT_YMS_SCRIPT


def _metadata(work: ExplorationWork) -> Mapping[str, object]:
    metadata = work.frontier.observation.metadata
    return metadata if isinstance(metadata, Mapping) else {}


def _is_yms_work(work: ExplorationWork) -> bool:
    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        return False
    package_name = str(_metadata(work).get("package_name") or "").strip()
    if resolve_product_orchestrator_id(package_name=package_name) == YMS_ORCHESTRATOR_ID:
        return True
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    return any(
        resolve_product_orchestrator_id(package_name=str(item)) == YMS_ORCHESTRATOR_ID
        for item in allowed
    )


def _expected_package(work: ExplorationWork) -> str:
    package_name = str(_metadata(work).get("package_name") or "").strip()
    if package_name.casefold() == YMS_PACKAGE:
        return package_name
    allowed = getattr(work.app_goal, "allowed_packages", ()) if work.app_goal else ()
    for item in allowed:
        if str(item).strip().casefold() == YMS_PACKAGE:
            return str(item).strip()
    return package_name or "dark.Tozhn.wzmtH"


def script_yms_override(
    work: ExplorationWork,
) -> ActionIntent | ExplorationTerminal | None:
    """欲漫涩的下一步來自 yms.py。做完由系統截動作後的圖。其他產品返回 None。"""

    if not isinstance(work, ExplorationWork) or not _is_yms_work(work):
        return None
    return next_script_playback(
        work,
        steps=parse_script_playback_steps(resolve_yms_script_path()),
        prefix="yms",
        index_key=YMS_SCRIPT_INDEX_KEY,
        done_key=YMS_SCRIPT_DONE_KEY,
        reason_code=YMS_SCRIPT_REASON_CODE,
        expected_package=_expected_package(work),
        script_name="yms.py",
    )


def apply_yms_script_progress(
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
        steps=parse_script_playback_steps(resolve_yms_script_path()),
        prefix="yms",
        index_key=YMS_SCRIPT_INDEX_KEY,
        done_key=YMS_SCRIPT_DONE_KEY,
    )


def serial_from_android_driver(driver: object) -> str:
    """只用目前這台裝置的端點。沒有端點就失敗，不猜別台雲手機。"""

    controller = getattr(driver, "_cloud_phone_controller", None)
    endpoint = getattr(controller, "endpoint", None)
    if isinstance(endpoint, str) and endpoint.strip():
        return endpoint.strip()
    for key in ("ALIOTH_ADB_SERIAL", "ANDROID_SERIAL", "YMS_SERIAL"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    raise RuntimeError("欲漫涩录制脚本缺少当前设备 serial")


def verify_yms_recording_shots(
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


def run_yms_recording_script(
    *,
    serial: str,
    script_path: Path | None = None,
    runner: Callable[..., object] | None = None,
) -> dict[str, object]:
    """啟動整份 yms.py。呼叫端只拿截圖清單做記錄與驗證。"""

    device = str(serial or "").strip()
    if not device:
        raise RuntimeError("欲漫涩录制脚本缺少当前设备 serial")
    path = script_path or resolve_yms_script_path()
    if not path.is_file():
        return {
            "ok": False,
            "error_code": "YMS_SCRIPT_MISSING",
            "script_path": str(path),
            "serial": device,
            "shots": [],
            "verification": verify_yms_recording_shots(()),
        }
    timeout_raw = os.environ.get("YMS_SCRIPT_TIMEOUT_SECONDS", "").strip()
    try:
        timeout = int(timeout_raw) if timeout_raw else 900
    except ValueError:
        timeout = 900
    timeout = min(1800, max(30, timeout))
    env = os.environ.copy()
    env["YMS_SERIAL"] = device
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
            "error_code": "YMS_SCRIPT_TIMEOUT",
            "script_path": str(path),
            "serial": device,
            "shots": shots,
            "operations": operations,
            "verification": verify_yms_recording_shots(shots),
        }
    return_code = int(getattr(completed, "returncode", 1))
    shots = _collect_shots(path.parent / "shots", before=before)
    verification = verify_yms_recording_shots(shots)
    verified = verification.get("verified") is True
    stderr = str(getattr(completed, "stderr", "") or "")
    return {
        "ok": return_code == 0 and verified,
        "error_code": (
            None
            if return_code == 0 and verified
            else "YMS_SCRIPT_FAILED"
            if return_code != 0
            else "YMS_SCRIPT_EVIDENCE_MISSING"
        ),
        "return_code": return_code,
        "script_path": str(path),
        "serial": device,
        "stderr_tail": stderr[-500:],
        "shots": shots,
        "operations": operations,
        "verification": verification,
    }
