"""整份錄製腳本的截圖只認這次執行寫出的檔，並附上腳本裡的實際操作。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

_CAPTURE_CALL = re.compile(r"""capture_screen\(\s*['"]([^'"]+)['"]\s*\)""")


def snapshot_shot_files(shots_dir: Path) -> dict[str, tuple[int, int]]:
    """跑腳本前記下已有 PNG 的時間和大小。沒改過的檔不算這次證據。"""

    found: dict[str, tuple[int, int]] = {}
    if not shots_dir.is_dir():
        return found
    for path in shots_dir.glob("*.png"):
        try:
            stat = path.stat()
        except OSError:
            continue
        found[str(path)] = (stat.st_mtime_ns, int(stat.st_size))
    return found


def collect_new_shots(
    shots_dir: Path,
    *,
    before: Mapping[str, tuple[int, int]],
) -> list[dict[str, object]]:
    """只收這次新增或覆寫的 PNG。啟動前就在的舊圖不計入。"""

    if not shots_dir.is_dir():
        return []
    found: list[dict[str, object]] = []
    for path in sorted(shots_dir.glob("*.png")):
        try:
            stat = path.stat()
        except OSError:
            continue
        current = (stat.st_mtime_ns, int(stat.st_size))
        if before.get(str(path)) == current or stat.st_size <= 8:
            continue
        found.append({"name": path.stem, "path": str(path), "bytes": int(stat.st_size)})
    return found


def script_operations(script_text: str) -> list[dict[str, str]]:
    """按腳本原文把每次截圖掛到它前面的觸控。不重新判斷點位。"""

    kind = "capture"
    operations: list[dict[str, str]] = []
    for line in script_text.splitlines():
        stripped = line.strip()
        if "input swipe" in stripped:
            kind = "swipe"
        elif "input tap" in stripped:
            kind = "tap"
        elif "input keyevent" in stripped:
            kind = "key"
        match = _CAPTURE_CALL.search(stripped)
        if match is None:
            continue
        operations.append({"kind": kind, "shot_after": match.group(1)})
        kind = "capture"
    return operations


def capture_names(script_text: str) -> tuple[str, ...]:
    return tuple(match.group(1) for match in _CAPTURE_CALL.finditer(script_text))
