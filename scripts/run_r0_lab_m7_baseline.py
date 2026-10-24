"""R0：Lab=M7 真机基线（生产 AUTHORITY 全关）。

步骤：
1. 预检 ADB／Lab 组合根／生产 authority
2. 跑一台 ONIRENT 正式质检（默认杏吧 qa-b:20660）
3. 落盘 `.tmp_r0_lab_m7_baseline/` 供开闸评审引用

不打开任何 ALIOTH_APP_MILESTONE_AUTHORITY_*。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

OUT_DIR = _ROOT / ".tmp_r0_lab_m7_baseline"
LOG = OUT_DIR / "run.log"


def _log(line: str) -> None:
    print(line, flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip() + "\n")


def _write_json(name: str, payload: object) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def preflight() -> dict[str, object]:
    """容器内组合根 + 宿主机 ADB；不打印密钥。"""

    adb = subprocess.run(
        ["adb", "devices"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    devices = [ln for ln in adb.stdout.splitlines() if "\tdevice" in ln]
    code = r"""
import json
from services.api.app.application.app_lab_composition import resolve_app_lab_composition
from services.api.app.application.app_command_broker import resolve_app_command_broker_mode
from services.api.app.application.app_exploration_grayscale import resolve_app_m5_schedules_from_graph
from services.api.app.application.app_graph_recapture import resolve_app_m6_runtime_ready
from services.api.app.application.app_bounded_exploration_milestones import (
    milestone_authority_enabled,
    build_app_bounded_exploration_milestone_policy,
)
from services.api.app.application.app_gate_retirement_shadow import (
    resolve_app_m7_gate_enforcement,
)

c = resolve_app_lab_composition()
p = dict(c["milestone_policy"])
prod = build_app_bounded_exploration_milestone_policy()
out = {
  "lab": {
    "enabled": c.get("enabled"),
    "milestone": c.get("milestone"),
    "lab_plan_ready": c.get("lab_plan_ready"),
    "lab_executes_probe": c.get("lab_executes_probe"),
    "lab_supplemental_ready": c.get("lab_supplemental_ready"),
    "broker_mode": str(resolve_app_command_broker_mode(p)),
    "schedules_from_graph": bool(resolve_app_m5_schedules_from_graph(p)),
    "m6_runtime_ready": bool(resolve_app_m6_runtime_ready(p)),
    "m7_lab_gate_enforcement": bool(resolve_app_m7_gate_enforcement(p)),
  },
  "note": "刀 A=lab_plan_ready；刀 B=lab_executes_probe；lab_supplemental_ready 仅为刀 B 兼容别名",
  "lab_policy_auth_bits": {m: bool(milestone_authority_enabled(p, m)) for m in ("M4","M5","M6","M7")},
  "production_authority": {k: bool(v) for k, v in dict(prod.get("authority") or {}).items()},
  "production_gate_sql_consumes_graph": bool(prod.get("production_gate_sql_consumes_graph")),
  "production_order_violations": list(prod.get("production_authority_order_violations") or []),
}
print(json.dumps(out, ensure_ascii=False))
"""
    proc = subprocess.run(
        ["docker", "exec", "alioth-worker", "python", "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "docker exec failed")
    stdout = proc.stdout or ""
    composition = json.loads(stdout.strip().splitlines()[-1])
    lab = composition["lab"]
    prod_auth = composition["production_authority"]
    if not lab.get("enabled") or lab.get("milestone") != "M7":
        raise RuntimeError(f"Lab 未处于 M7: {lab}")
    if lab.get("lab_executes_probe") or lab.get("lab_supplemental_ready"):
        raise RuntimeError(
            "R0 禁止刀 B（lab_executes_probe／关闭前 Probe）；"
            f"请 unset ALIOTH_APP_LAB_EXECUTES_PROBE: {lab}"
        )
    if any(prod_auth.get(m) for m in ("M4", "M5", "M6", "M7")):
        raise RuntimeError(f"生产 AUTHORITY 必须全关: {prod_auth}")
    if composition.get("production_gate_sql_consumes_graph"):
        raise RuntimeError("Gate SQL 读图不得在 R0 打开")
    if len(devices) < 1:
        raise RuntimeError("无 ONIRENT ADB device")
    payload = {
        "protocol": "app-r0-lab-m7-baseline.v1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "adb_devices": devices,
        "composition": composition,
        "r0_gates": {
            "lab_m7": True,
            "production_authority_off": True,
            "gate_sql_off": True,
            "adb_online": True,
        },
    }
    _write_json("preflight.json", payload)
    _log(f"preflight_ok devices={len(devices)} lab=M7 prod_auth_off=1")
    return payload


def run_device_qc() -> dict[str, object]:
    """优先用空闲 qa-a（黑料）；若指定 R0_PROFILE=qa-b 再用杏吧。"""

    import os

    profile = (os.environ.get("R0_PROFILE") or "qa-a").strip().casefold()
    if profile == "qa-b":
        from scripts._run_xingba_qc_onirent_20660 import ITEM
        from scripts._run_xingba_qc_onirent_20660 import OUT_DIR as DEVICE_OUT
        from scripts._run_xingba_qc_onirent_20660 import main as device_main

        result_name = "xingba.json"
    else:
        from scripts._run_hlapp_qc_onirent_smn975f import ITEM
        from scripts._run_hlapp_qc_onirent_smn975f import OUT_DIR as DEVICE_OUT
        from scripts._run_hlapp_qc_onirent_smn975f import main as device_main

        result_name = "hlapp.json"

    _log(f"device_qc_start profile={profile} label={ITEM['label']} apk={ITEM['path']}")
    device_main()
    result_path = DEVICE_OUT / result_name
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    _write_json("device_qc.json", result)
    return result


def classify_failures(result: dict[str, object]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    status = str(result.get("status") or "")
    if status in {"", "pending"}:
        rows.append({"class": "incomplete", "detail": "无终态"})
    elif status in {"poll_timeout", "timeout"}:
        rows.append({"class": "timeout", "detail": status})
    elif status in {"skipped"}:
        rows.append({"class": "skipped", "detail": str(result.get("reason") or "")})
    elif status not in {"completed", "success", "passed", "finished", "done"}:
        # 保留原始 status，便于人工归类
        rows.append({"class": "non_success_status", "detail": status})

    cov = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    page_rate = cov.get("page_coverage_rate")
    scoped = cov.get("scoped_page_coverage_pct")
    rule_rate = cov.get("rule_coverage_rate")
    if isinstance(page_rate, (int, float)) and page_rate < 0.5:
        rows.append({"class": "low_page_coverage", "detail": f"page_coverage_rate={page_rate}"})
    if isinstance(scoped, (int, float)) and scoped < 50:
        rows.append({"class": "low_scoped_coverage", "detail": f"scoped_page_coverage_pct={scoped}"})
    if isinstance(rule_rate, (int, float)) and rule_rate < 0.3:
        rows.append({"class": "low_rule_coverage", "detail": f"rule_coverage_rate={rule_rate}"})

    terminal = result.get("terminal_reason") or result.get("error") or result.get("reason")
    if terminal and status not in {"skipped"}:
        rows.append({"class": "terminal_reason", "detail": str(terminal)[:500]})
    return rows


def write_summary(preflight: dict[str, object], result: dict[str, object]) -> Path:
    cov = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    failures = classify_failures(result)
    summary = {
        "protocol": "app-r0-lab-m7-baseline.v1",
        "phase": "R0",
        "lab_milestone": "M7",
        "production_authority": "all_false_except_m0",
        "preflight_ok": True,
        "adb_devices": preflight.get("adb_devices"),
        "task_id": result.get("task_id"),
        "device": result.get("device"),
        "label": result.get("label"),
        "status": result.get("status"),
        "metrics": {
            "page_coverage_rate": cov.get("page_coverage_rate"),
            "scoped_page_coverage_pct": cov.get("scoped_page_coverage_pct"),
            "rule_coverage_rate": cov.get("rule_coverage_rate"),
            "covered_page_types": cov.get("covered_page_types"),
            "required_page_types": cov.get("required_page_types")
            or (result.get("exploration_target") or {}).get("required_page_types"),
        },
        "failure_classes": failures,
        "next": "指标稳定后进入 R1 仅评 M4 生产权威；禁止跳级",
        "refs": {
            "review_plan": "docs/design/2026-09-08-app-production-authority-rollout-review.md",
            "device_out": str((_ROOT / ".tmp_hlapp_qc_onirent").resolve()),
            "xingba_out_alt": str((_ROOT / ".tmp_xingba_qc_onirent_20660").resolve()),
        },
    }
    path = _write_json("summary.json", summary)
    md = [
        "# R0 Lab=M7 真机基线摘要",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "- Lab：M7（生产 AUTHORITY 全关）",
        f"- 设备：{result.get('device')}",
        f"- 样本：{result.get('label')} task={result.get('task_id')}",
        f"- 状态：{result.get('status')}",
        f"- 页面覆盖率：{cov.get('page_coverage_rate')}",
        f"- 范围页覆盖%：{cov.get('scoped_page_coverage_pct')}",
        f"- 规则覆盖率：{cov.get('rule_coverage_rate')}",
        "",
        "## 失败分类",
    ]
    if failures:
        for row in failures:
            md.append(f"- `{row['class']}`：{row['detail']}")
    else:
        md.append("- （无自动分类命中；仍须人工抽检 Receipt／外跳／跨 epoch）")
    md.extend(
        [
            "",
            "## 下一步",
            "- 本摘要进入开闸评审附件；稳定后只评 **M4**。",
            "- 禁止打开 `ALIOTH_APP_MILESTONE_AUTHORITY_M*`／Gate SQL。",
        ]
    )
    (OUT_DIR / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    _log(f"summary_written {path}")
    return path


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if LOG.exists():
        LOG.write_text("", encoding="utf-8")
    _log("=== R0 Lab=M7 baseline start ===")
    pre = preflight()
    result = run_device_qc()
    write_summary(pre, result)
    _log("=== R0 done ===")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        _log(f"R0_FAILED {type(exc).__name__}: {exc}")
        raise
