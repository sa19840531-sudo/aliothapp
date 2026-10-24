#!/usr/bin/env python3
"""雲手機 AI 視覺一步閉環煙測（經 Interaction Runtime，禁止直呼 adb input/screencap）。

用法：
  set ALIOTH_CLOUD_PHONE_ADB=x.x.x.x:5555
  set ALIOTH_LLM_API_KEY=sk-...
  python scripts/run-cloud-phone-ai-smoke.py

可選：
  ALIOTH_AI_GOAL   預設「關閉彈窗或點擊明顯的主按鈕／廣告關閉」
  ALIOTH_AI_STEPS  預設 3
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


async def main() -> int:
    from services.api.app.infrastructure.device.android_capture_runtime import (
        build_mobile_demo_android_runtime,
        start_mobile_demo_android_runtime,
    )
    from services.api.app.mobile_demo_ai_driver import (
        LLMConfigurationError,
        observe_via_runtime,
        resolve_ai_driver_backend,
        run_ai_loop,
        white_pixel_ratio,
    )

    if not (os.getenv("ALIOTH_CLOUD_PHONE_ADB") or os.getenv("ALIOTH_ADB_SERIAL")):
        print("ERROR: set ALIOTH_CLOUD_PHONE_ADB=host:port")
        return 2

    goal = os.getenv(
        "ALIOTH_AI_GOAL",
        "关闭弹窗或点击明显的主按钮/广告关闭按钮；若已是可用主界面则 done",
    )
    steps = int(os.getenv("ALIOTH_AI_STEPS") or "3")
    out_dir = ROOT / ".alioth-mobile-demo" / "cloud-phone-ai-smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    backend = resolve_ai_driver_backend()
    print(f"backend={backend} (via Interaction Runtime)")

    probe_runtime = build_mobile_demo_android_runtime(
        task_id="ai-smoke-probe",
        session_id=f"probe-{uuid4().hex[:8]}",
        backend=backend,
        shots_dir=out_dir / "_probe_runtime",
    )
    try:
        await start_mobile_demo_android_runtime(probe_runtime)
        probe = out_dir / "probe.png"
        await observe_via_runtime(probe_runtime, probe)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR Runtime/ADB: {type(exc).__name__}: {exc}")
        print("Set a live free-cloud-phone ADB endpoint in ALIOTH_CLOUD_PHONE_ADB")
        return 2
    finally:
        await probe_runtime.close()

    pct = white_pixel_ratio(probe)
    print(f"probe white_pct={pct:.1f} -> {probe}")
    if pct >= 80.0:
        print("WARN: screen mostly white; AI may not help until app renders")

    try:
        log = await run_ai_loop(goal=goal, out_dir=out_dir, steps=steps, backend=backend)
    except LLMConfigurationError as exc:
        print(f"ERROR LLM config: {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1

    print("=== AI loop ===")
    for row in log:
        print(
            f"step={row['step']} action={row['action']} "
            f"xy=({row.get('x')},{row.get('y')}) via={row.get('via')!r} "
            f"reason={row.get('reason')!r}"
        )
    print(f"log: {out_dir / 'ai_loop.json'}")
    print("AI_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
