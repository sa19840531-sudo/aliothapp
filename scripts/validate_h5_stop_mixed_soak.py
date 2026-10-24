"""显式专库中的180分钟混合长测；不把定期故障注入当失败重试。"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit


def source_identity(root):
    paths = [*root.joinpath("services/api").rglob("*.py"),
             *root.joinpath("scripts").glob("validate_h5_stop_mixed_soak.py")]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if "__pycache__" not in path.parts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--canvas-database", required=True)
    parser.add_argument("--fault-database", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # 运行器本身也拒绝远端、业务库和已存在输出目录；库由调用方显式新建并迁移。
    for url, name in ((args.canvas_database, "alioth_test_h5_soak"),
                      (args.fault_database, "alioth_test_h5_stop_soak")):
        parsed = urlsplit(url)
        if (parsed.scheme != "postgresql" or parsed.hostname != "127.0.0.1"
                or parsed.username != "alioth_test" or parsed.path != "/" + name):
            parser.error("仅接受显式回环隔离专库和alioth_test角色")
    args.output.mkdir(parents=True, exist_ok=False)
    identities = source_identity(root)
    (args.output / "source-identities.json").write_text(json.dumps(identities, indent=2))
    env = {**os.environ, "ALIOTH_DATABASE_URL":
           "postgresql://unreachable@127.0.0.1:1/unreachable",
           "ALIOTH_LLM_API_KEY": "isolated-soak-no-provider"}
    start = time.monotonic()
    report = {"started_at": datetime.now(UTC).isoformat(), "requested_seconds": 10800,
              "full_180_minute_mixed_passed": False, "real_site_acceptance": False,
              "model_provider": "deterministic_local_stub", "usage_available": False,
              "pricing_configured": False, "fault_interval_seconds": 300,
              "expected_fault_cycles": 36, "fault_cycles": [], "failure": None}
    children = []
    logs = []

    def journal(kind, **values):
        with (args.output / "history.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"at": datetime.now(UTC).isoformat(),
                                  "elapsed_seconds": time.monotonic() - start,
                                  "kind": kind, **values}, default=str) + "\n")

    def launch(name, command, values):
        log = (args.output / f"{name}.log").open("w")
        logs.append(log)
        process = subprocess.Popen(command, cwd=root, env={**env, **values},
                                   stdout=log, stderr=subprocess.STDOUT)
        children.append(process)
        journal("started", name=name, pid=process.pid)
        return process

    def snapshot():
        report["elapsed_seconds"] = time.monotonic() - start
        (args.output / "progress.json").write_text(json.dumps(report, indent=2))

    base = [sys.executable, "-m", "pytest", "-q", "--tb=short",
            "-o", "faulthandler_timeout=90"]
    try:
        canvas = launch("canvas", [*base,
            "services/api/tests/test_h5_canvas_mixed_soak.py",
            "-o", "python_functions=test_* soak_*",
            f"--basetemp={args.output / 'canvas-data'}",
            f"--junitxml={args.output / 'canvas.xml'}"],
            {"H5_CANVAS_SOAK_DATABASE_URL": args.canvas_database,
             "H5_CANVAS_SOAK_SECONDS": "10800"})
        fault = None
        cycle = 0
        while time.monotonic() - start < 10950:
            elapsed = time.monotonic() - start
            if source_identity(root) != identities:
                raise RuntimeError("源文件中途变化，不能把混合版本称为同轮通过")
            if fault is not None and fault.poll() is not None:
                report["fault_cycles"][-1]["exit_code"] = fault.returncode
                journal("fault_cycle_finished", cycle=cycle-1, exit_code=fault.returncode)
                if fault.returncode:
                    raise RuntimeError(f"故障注入周期{cycle-1}失败；保留首次结果，不自动重试")
                fault = None
            if canvas.poll() is not None:
                report["canvas_exit_code"] = canvas.returncode
                if canvas.returncode:
                    raise RuntimeError("采集长测失败；不重跑以覆盖失败")
                if elapsed < 10800:
                    raise RuntimeError("采集长测过早结束，不能算完整180分钟")
                if fault is None:
                    break
            if fault is None and cycle < 36 and elapsed >= 300 * cycle:
                name = f"fault-{cycle:02d}"
                fault = launch(name, [*base,
                    "services/api/tests/test_h5_stop_receipt_finalization_e2e.py",
                    f"--basetemp={args.output / (name + '-data')}",
                    f"--junitxml={args.output / (name + '.xml')}"],
                    {"ALIOTH_TEST_DATABASE_URL": args.fault_database})
                report["fault_cycles"].append({"cycle": cycle, "pid": fault.pid,
                    "started_at": datetime.now(UTC).isoformat(), "exit_code": None})
                cycle += 1
            snapshot()
            time.sleep(10)
        else:
            raise RuntimeError("长测超过排空宽限，保留阻断，不刷新运行期限")
        native = list((args.output / "canvas-data").rglob("result.json"))
        if len(native) != 1 or not json.loads(native[0].read_text())["full_180_minute_soak"]:
            raise RuntimeError("缺少原采集、冻结预算和物理排空的完整长测结果")
        if len(report["fault_cycles"]) != 36:
            raise RuntimeError("故障注入周期缺失")
        report["full_180_minute_mixed_passed"] = True
    except BaseException as error:
        report["failure"] = f"{type(error).__name__}:{error}"
        journal("failure", error=report["failure"])
    finally:
        # 子测试负责关闭原句柄。失败不盲杀、不伪造停止；保留在途PID供后续核查。
        report["child_processes"] = [
            {"pid": process.pid, "exit_code": process.poll()} for process in children
        ]
        for log in logs:
            log.close()
        snapshot()
        (args.output / "result.json").write_text(json.dumps(report, indent=2))
    return 0 if report["full_180_minute_mixed_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
