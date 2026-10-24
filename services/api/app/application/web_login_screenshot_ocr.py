"""登录账户截图的本地文字线索。"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import shutil
import struct
import subprocess
from pathlib import Path

_WORDS = re.compile(r"钱包|充值|wallet|recharge|top\s*up", re.I)


class WebLoginScreenshotOcr:
    def __init__(self, *, executable=None, runner=None):
        local = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        self._executable = (
            executable or shutil.which("tesseract") or (str(local) if local.exists() else None)
        )
        self._runner = runner or subprocess.run

    async def read(
        self, png: bytes, *, width: int | None = None, height: int | None = None
    ) -> tuple[dict, ...]:
        if (
            self._executable is None
            or not isinstance(png, bytes)
            or not png.startswith(b"\x89PNG\r\n\x1a\n")
            or len(png) > 16 * 1024 * 1024
        ):
            return ()
        if width is None or height is None:
            if len(png) < 24:
                return ()
            width, height = struct.unpack(">II", png[16:24])
        if not (0 < width <= 32768 and 0 < height <= 32768):
            return ()
        try:
            result = await asyncio.to_thread(
                self._runner,
                [self._executable, "stdin", "stdout", "-l", "chi_sim+eng", "tsv"],
                input=png,
                capture_output=True,
                timeout=12,
                check=False,
            )
            if result.returncode != 0 or len(result.stdout) > 2 * 1024 * 1024:
                return ()
            rows = csv.DictReader(
                io.StringIO(result.stdout.decode("utf-8", "replace")), delimiter="\t"
            )
            lines: dict[tuple[str, ...], list[tuple[str, int, int, int, int]]] = {}
            for row in rows:
                word = (row.get("text") or "").strip()
                if not word or float(row.get("conf") or -1) < 30:
                    continue
                left, top = int(row["left"]), int(row["top"])
                right = left + int(row["width"])
                bottom = top + int(row["height"])
                if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                    continue
                key = tuple(
                    row.get(name, "") for name in ("page_num", "block_num", "par_num", "line_num")
                )
                lines.setdefault(key, []).append((word, left, top, right, bottom))
            hits = []
            for words in lines.values():
                parts = [item[0] for item in words]
                joined = ("" if re.search(r"[\u3400-\u9fff]", "".join(parts)) else " ").join(parts)
                if not _WORDS.search(joined):
                    continue
                hits.append(
                    {
                        "text": joined[:80],
                        "box": (
                            min(item[1] for item in words) / width,
                            min(item[2] for item in words) / height,
                            max(item[3] for item in words) / width,
                            max(item[4] for item in words) / height,
                        ),
                    }
                )
            return tuple(hits[:12])
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
            return ()
