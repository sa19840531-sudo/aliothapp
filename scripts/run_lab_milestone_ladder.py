"""依序把 ALIOTH_APP_LAB_MILESTONE 写成 M4→M5→M6→M7，重建 Worker 并校验组合根。"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILES = [
    ROOT / "infra" / "env" / ".env.local",
    ROOT / ".env.local",
]
COMPOSE_DIR = ROOT / "infra"
MILESTONES = ("M4", "M5", "M6", "M7")
ENV_KEY = "ALIOTH_APP_LAB_MILESTONE"


def set_lab_milestone(milestone: str) -> None:
    line = f"{ENV_KEY}={milestone}"
    pattern = re.compile(rf"^{re.escape(ENV_KEY)}=.*$", re.M)
    for path in ENV_FILES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            text = pattern.sub(line, text)
        else:
            if not text.endswith("\n"):
                text += "\n"
            text += (
                f"\n# Lab 真机对账组合根（由 scripts/run_lab_milestone_ladder.py 维护）\n"
                f"{line}\n"
            )
        path.write_text(text, encoding="utf-8")
        print(f"updated {path} -> {milestone}")


def recreate_worker() -> None:
    # 本地容器可能由不同 compose project 创建；先强制删名再 up，避免 name conflict。
    subprocess.run(["docker", "rm", "-f", "alioth-worker"], check=False)
    cmd = [
        "docker",
        "compose",
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.local-dual-port.yml",
        "up",
        "-d",
        "--no-deps",
        "alioth-worker",
    ]
    print("recreate worker:", " ".join(cmd))
    subprocess.run(cmd, cwd=COMPOSE_DIR, check=True)
    # 等待进程可 import
    for _ in range(60):
        probe = subprocess.run(
            [
                "docker",
                "exec",
                "alioth-worker",
                "python",
                "-c",
                "print('ready')",
            ],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0 and "ready" in (probe.stdout or ""):
            return
        time.sleep(1)
    raise RuntimeError("worker not ready after recreate")


def verify_in_worker(expected: str) -> dict[str, object]:
    code = r"""
import json
from services.api.app.application.app_lab_composition import resolve_app_lab_composition
from services.api.app.application.app_command_broker import resolve_app_command_broker_mode
from services.api.app.application.app_exploration_grayscale import resolve_app_m5_schedules_from_graph
from services.api.app.application.app_graph_recapture import resolve_app_m6_runtime_ready
from services.api.app.application.app_bounded_exploration_milestones import milestone_authority_enabled

c = resolve_app_lab_composition()
p = c["milestone_policy"]
out = {
  "enabled": c.get("enabled"),
  "milestone": c.get("milestone"),
  "lab_plan_ready": c.get("lab_plan_ready"),
  "lab_executes_probe": c.get("lab_executes_probe"),
  "lab_supplemental_ready": c.get("lab_supplemental_ready"),
  "broker_mode": str(resolve_app_command_broker_mode(p)),
  "schedules_from_graph": resolve_app_m5_schedules_from_graph(p),
  "m6_runtime_ready": resolve_app_m6_runtime_ready(p),
  "auth_M4": milestone_authority_enabled(p, "M4"),
  "auth_M5": milestone_authority_enabled(p, "M5"),
  "auth_M6": milestone_authority_enabled(p, "M6"),
  "auth_M7": milestone_authority_enabled(p, "M7"),
}
print(json.dumps(out, ensure_ascii=False))
"""
    proc = subprocess.run(
        ["docker", "exec", "alioth-worker", "python", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"verify failed: {proc.stderr or proc.stdout}")
    line = proc.stdout.strip().splitlines()[-1]
    import json

    data = json.loads(line)
    assert data["enabled"] is True, data
    assert data["milestone"] == expected, data
    expect_plan = expected in {"M6", "M7"}
    assert data["lab_plan_ready"] is expect_plan, data
    # 刀 B 默认关：M6/M7 不得自动打开关闭前 Probe。
    assert data["lab_executes_probe"] is False, data
    assert data["lab_supplemental_ready"] is False, data
    assert data["broker_mode"] == "actions", data
    assert data["schedules_from_graph"] is (expected in {"M5", "M6", "M7"}), data
    assert data["m6_runtime_ready"] is (expected in {"M6", "M7"}), data
    assert data["auth_M4"] is True, data
    assert data["auth_M5"] is (expected in {"M5", "M6", "M7"}), data
    assert data["auth_M6"] is (expected in {"M6", "M7"}), data
    assert data["auth_M7"] is (expected == "M7"), data
    return data


def verify_adb() -> None:
    proc = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=True)
    lines = [ln for ln in proc.stdout.splitlines() if "\tdevice" in ln]
    print(f"adb devices online: {len(lines)}")
    for ln in lines:
        print(" ", ln)
    if len(lines) < 1:
        raise RuntimeError("no ONIRENT adb device online")


def main() -> int:
    print("=== step1 ONIRENT adb ===")
    verify_adb()
    results = []
    for ms in MILESTONES:
        print(f"\n=== Lab {ms} ===")
        set_lab_milestone(ms)
        recreate_worker()
        data = verify_in_worker(ms)
        print("PASS", data)
        results.append(data)
    print("\n=== ladder done ===")
    for item in results:
        print(item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
