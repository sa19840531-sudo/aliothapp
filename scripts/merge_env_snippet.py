"""把 snippet env 檔 merge 進 target（依 marker 區塊替換）。"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

MARKER_BEGIN = "# >>> {marker}"
MARKER_END = "# <<< {marker}"


def merge_snippet(target: Path, snippet: Path, marker: str) -> None:
    snippet_text = snippet.read_text(encoding="utf-8").strip()
    block = f"{MARKER_BEGIN.format(marker=marker)}\n{snippet_text}\n{MARKER_END.format(marker=marker)}\n"

    if target.exists():
        text = target.read_text(encoding="utf-8")
    else:
        text = ""

    pattern = re.compile(
        rf"{re.escape(MARKER_BEGIN.format(marker=marker))}.*?{re.escape(MARKER_END.format(marker=marker))}\n?",
        re.DOTALL,
    )
    if pattern.search(text):
        text = pattern.sub(block, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n" + block

    # 移除 snippet 內與 target 重複的 key（在 marker 外）
    snippet_keys = set()
    for line in snippet_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            snippet_keys.add(line.split("=", 1)[0].strip())

    def strip_dup_keys(body: str) -> str:
        out: list[str] = []
        in_block = False
        for line in body.splitlines(keepends=True):
            if MARKER_BEGIN.format(marker=marker) in line:
                in_block = True
                out.append(line)
                continue
            if MARKER_END.format(marker=marker) in line:
                in_block = False
                out.append(line)
                continue
            if not in_block and "=" in line and not line.lstrip().startswith("#"):
                key = line.split("=", 1)[0].strip()
                if key in snippet_keys:
                    continue
            out.append(line)
        return "".join(out)

    text = strip_dup_keys(text)
    target.write_text(text, encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--target", required=True)
    p.add_argument("--snippet", required=True)
    p.add_argument("--marker", default="CROSS_SERVER_MOBILE_DEMO")
    args = p.parse_args()
    merge_snippet(Path(args.target), Path(args.snippet), args.marker)
    print(f"merged {args.snippet} -> {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
