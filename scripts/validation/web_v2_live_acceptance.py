"""Web V2 实时增量验收入口；凭据必须显式提供，站点只来自验证参数。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.validation.web_acceptance_reporter import (  # noqa: E402,F401
    TERMINAL,
    AcceptanceReporter,
    SiteMinimumWitnessPolicy,
    SuiteCapabilityMatrix,
    ValidationDelta,
    classify_validation_failure,
    dispatch_capacity,
    extend_observation,
    safe_projection,
)
from scripts.validation.web_acceptance_source import (  # noqa: E402
    AcceptanceClient,
    read_database_state,
    read_web_worker_count,
    referenced_artifact_ids,
    submission_payload,
)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("preflight", "start", "monitor", "close-complete", "extend-incomplete", "report"),
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("outputs/web-v2-live-acceptance"))
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--site", action="append", default=[])
    parser.add_argument("--token")
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--database-url")
    parser.add_argument("--redis-url")
    parser.add_argument("--interval-seconds", type=float, default=2)
    parser.add_argument("--site-timeout-minutes", type=float, default=180)
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--require-menu-limit", type=int, default=10)
    for option in (
        "require-dynamic-policy",
        "require-trusted-capture",
        "require-frontier-policy",
        "require-ad-href-only",
        "require-checkpoint",
        "require-two-origin-acceptance",
        "minimum-site-witness",
        "suite-capability-matrix",
        "verify-artifacts",
    ):
        parser.add_argument(f"--{option}", action="store_true")
    return parser


def credentials(args, *, environ=None):
    env = os.environ if environ is None else environ
    token = args.token or env.get("ALIOTH_LIVE_TOKEN", "")
    username = args.username or env.get("ALIOTH_LIVE_USERNAME", "")
    password = args.password or env.get("ALIOTH_LIVE_PASSWORD", "")
    if not token and not (username and password):
        raise ValueError("缺少显式登录凭据：ALIOTH_LIVE_TOKEN 或用户名与密码")
    return {"token": token, "username": username, "password": password}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def submit_pending(client, manifest, path):
    active = sum(
        bool(site.get("task_id")) and not site.get("terminal") for site in manifest["sites"]
    )
    capacity = dispatch_capacity(manifest["worker_count"], active_count=active)
    for site in (row for row in manifest["sites"] if not row.get("task_id")):
        if capacity <= 0:
            break
        # 幂等键先落盘；网络回执丢失时可安全重新提交同一项。
        write_json(path, manifest)
        receipt = await client.request(
            "POST",
            "/api/v2/exploration-tasks",
            payload=submission_payload(site["requested_url"]),
            idempotency_key=site["idempotency_key"],
        )
        if not receipt.get("task_id") or not receipt.get("run_id"):
            raise RuntimeError("VALIDATION_SUBMISSION_RECEIPT_MISSING")
        site.update(
            task_id=receipt["task_id"],
            run_id=receipt["run_id"],
            deadline=time.time() + manifest["site_timeout_seconds"],
            extension_count=0,
        )
        write_json(path, manifest)
        capacity -= 1


async def preflight(args, client, dsn):
    import asyncpg

    await client.request("GET", "/api/v1/readyz")
    count = await read_web_worker_count(args.redis_url)
    dispatch_capacity(count, active_count=0)
    connection = await asyncpg.connect(dsn, timeout=10, command_timeout=15)
    try:
        async with connection.transaction(readonly=True):
            version = await connection.fetchval("SELECT version_num FROM alembic_version")
            checkpoint = await connection.fetchval(
                "SELECT to_regclass('qc_web_run_stage_checkpoint') IS NOT NULL"
            )
            await connection.fetchval(
                "SELECT count(*) FROM qc_web_run_stage_checkpoint WHERE false"
            )
    finally:
        await connection.close(timeout=5)
    if not checkpoint:
        raise RuntimeError("VALIDATION_CHECKPOINT_MIGRATION_MISSING")
    result = {
        "migration_version": version,
        "worker_count": count,
        "concurrency": min(count, 3),
        "checkpoint_table": True,
        "code_contracts": "not_checked",
        "two_origin": "not_checked",
        "requested_menu_limit": args.require_menu_limit,
        "frozen_policy_verification": "on_run_collection",
    }
    required = any(
        getattr(args, key)
        for key in (
            "require_dynamic_policy",
            "require_trusted_capture",
            "require_frontier_policy",
            "require_ad_href_only",
            "require_two_origin_acceptance",
        )
    )
    if required:
        tests = [
            "test_web_policy_creation_boundary.py",
            "test_web_trusted_capture_discovery.py",
            "test_web_frontier_scheduler.py",
            "test_web_ad_landing_evidence.py",
            "test_web_controlled_two_origin_acceptance.py",
        ]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            *(f"services/api/tests/{test}" for test in tests),
            "-q",
            "--disable-warnings",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=Path(__file__).resolve().parents[2],
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=300)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("VALIDATION_PREFLIGHT_TEST_TIMEOUT") from None
        if process.returncode or b" skipped" in output:
            raise RuntimeError("VALIDATION_PREFLIGHT_TESTS_FAILED_OR_SKIPPED")
        result.update(code_contracts="passed", two_origin="passed")
    write_json(args.output / "preflight.json", result)
    return result


async def collect_site(args, client, dsn, site, *, secrets):
    state = await read_database_state(dsn, site["task_id"], site["run_id"])
    attach_local_artifact_paths(state, args.evidence_root or args.output / "evidence")
    state["requested_url"] = site["requested_url"]
    if state["menu_limit"] is not None and state["menu_limit"] != args.require_menu_limit:
        raise RuntimeError("VALIDATION_FROZEN_MENU_LIMIT_MISMATCH")
    folder = args.output / site["directory"]
    reporter = AcceptanceReporter(lambda: state, folder, secrets=secrets)
    cursor_path = folder / "cursor.json"
    cursor = json.loads(cursor_path.read_text(encoding="utf-8")) if cursor_path.exists() else None
    delta = reporter.collect_delta(cursor)
    reporter.write_delta(delta)
    site["terminal"] = state["status"] in TERMINAL
    return delta


def verify_close(state):
    return SiteMinimumWitnessPolicy().evaluate(state).complete


def attach_local_artifact_paths(state, evidence_root):
    """把本地存储相对路径解析为可点击路径，并拒绝目录穿越。"""
    root = Path(evidence_root).resolve()
    for capture in state.get("captures", []):
        for artifact in capture.get("artifacts", []):
            storage_path = artifact.pop("storage_path", None)
            if storage_path is None:
                continue
            if not isinstance(storage_path, str) or not storage_path:
                raise ValueError("VALIDATION_LOCAL_ARTIFACT_PATH_INVALID")
            local_path = (root / storage_path).resolve()
            try:
                local_path.relative_to(root)
            except ValueError:
                raise ValueError("证据存储路径越出本地证据根目录") from None
            artifact["local_path"] = str(local_path)
    return state


def verify_artifacts(state):
    artifacts = [
        artifact for row in state.get("captures", []) for artifact in row.get("artifacts", [])
    ]
    if not artifacts or any(
        not artifact.get("local_path") or not Path(artifact["local_path"]).is_file()
        for artifact in artifacts
    ):
        return False
    observed = {
        artifact["artifact_id"]
        for row in state.get("captures", [])
        for artifact in row.get("artifacts", [])
    }
    return referenced_artifact_ids(state).issubset(observed)


async def run(args):
    if not 0 < args.interval_seconds <= 120 or not 0 < args.site_timeout_minutes <= 180:
        raise ValueError("间隔必须为(0,120]秒；首次单站观察必须为(0,180]分钟")
    auth = credentials(args)
    dsn = args.database_url or os.environ.get("ALIOTH_LIVE_DATABASE_URL", "")
    if not dsn:
        raise ValueError("需要显式 ALIOTH_LIVE_DATABASE_URL 以只读查询 checkpoint")
    args.redis_url = args.redis_url or os.environ.get("ALIOTH_LIVE_REDIS_URL", "")
    if args.command in {"preflight", "start", "monitor"} and not args.redis_url:
        raise ValueError("需要显式 ALIOTH_LIVE_REDIS_URL 以读取 Web Worker 心跳")
    client = AcceptanceClient(args.api_base, **auth)
    try:
        await client.login()
        secrets = tuple(
            value for value in (*auth.values(), client.token, dsn, args.redis_url) if value
        )
        if args.command == "preflight":
            print(json.dumps(await preflight(args, client, dsn), ensure_ascii=False))
            return 0
        path = args.output / "validation-state.json"
        if args.command == "start" and not path.exists():
            if not args.site:
                raise ValueError("start 需要至少一个 --site")
            worker_count = await read_web_worker_count(args.redis_url)
            dispatch_capacity(worker_count, active_count=0)
            sites = []
            for index, url in enumerate(dict.fromkeys(args.site)):
                if safe_projection(url, key="requested_url", secrets=secrets) != url:
                    raise ValueError("站点必须为不含凭据、查询或 fragment 的 HTTP(S) URL")
                sites.append(
                    {
                        "requested_url": url,
                        "directory": f"{index + 1}-{urlsplit(url).hostname}",
                        "idempotency_key": f"web-live-{uuid4()}",
                    }
                )
            write_json(
                path,
                {
                    "worker_count": worker_count,
                    "site_timeout_seconds": args.site_timeout_minutes * 60,
                    "sites": sites,
                },
            )
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if args.command == "start":
            await submit_pending(client, manifest, path)
            print("验证任务已提交；其余站点将在 monitor 释放容量后提交。")
            return 0
        if args.command == "extend-incomplete":
            for site in manifest["sites"]:
                if site.get("task_id") and not site.get("terminal"):
                    extend_observation(site, minutes=args.minutes)
            write_json(path, manifest)
            return 0
        while True:
            summaries = []
            blocked = False
            for site in manifest["sites"]:
                if not site.get("task_id"):
                    continue
                delta = await collect_site(args, client, dsn, site, secrets=secrets)
                summaries.append(delta.summary)
                for event in delta.events:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                minimum = SiteMinimumWitnessPolicy().evaluate(delta.summary)
                if args.command == "close-complete":
                    site["close_verified"] = verify_close(delta.summary)
                    blocked |= not site["close_verified"]
                if args.command == "report":
                    # 来源查询已按真实归属读取 Artifact；缺失主证据仍由最低见证拦截。
                    blocked |= not minimum.complete or not verify_artifacts(delta.summary)
                if not site["terminal"] and time.time() >= site["deadline"]:
                    site["observation_timeout"] = True
            write_json(path, manifest)
            controlled = args.output / "preflight.json"
            witnesses = []
            if controlled.exists():
                result = json.loads(controlled.read_text(encoding="utf-8"))
                if result.get("two_origin") == "passed":
                    witnesses.append(
                        {"capabilities": {"ad_isolation": True, "cross_origin_recovery": True}}
                    )
            matrix = SuiteCapabilityMatrix.from_sites([*summaries, *witnesses])
            write_json(args.output / "suite-capability-matrix.json", asdict(matrix))
            if args.command != "monitor":
                blocked |= len(summaries) != len(manifest["sites"])
                if args.command == "report":
                    write_json(
                        args.output / "final-report.json",
                        {"sites": summaries, "suite": asdict(matrix)},
                    )
                    lines = ["# Web V2 分层实况验收", ""]
                    for summary in summaries:
                        lines.append(
                            f"- Task {summary['task_id']} / Run {summary['run_id']}："
                            f"最低见证={verify_close(summary)}；证据引用={verify_artifacts(summary)}"
                        )
                    lines.extend(["", f"合并能力缺口：{', '.join(matrix.missing) or '无'}", ""])
                    (args.output / "final-report.md").write_text("\n".join(lines), encoding="utf-8")
                    blocked |= not matrix.complete
                return 2 if blocked else 0
            if any(
                site.get("observation_timeout") and not site.get("terminal")
                for site in manifest["sites"]
            ):
                return 2
            manifest["worker_count"] = await read_web_worker_count(args.redis_url)
            await submit_pending(client, manifest, path)
            if all(
                site.get("terminal") or site.get("observation_timeout")
                for site in manifest["sites"]
            ):
                return 0 if all(site.get("terminal") for site in manifest["sites"]) else 2
            remaining = args.interval_seconds
            while remaining > 0:
                duration = min(remaining, 60)
                await asyncio.sleep(duration)
                remaining -= duration
    finally:
        await client.close()


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (Exception, KeyboardInterrupt) as exc:
        # HTTP/DSN/数据库错误可能包含凭据，只输出异常类型，不回显异常正文。
        print(
            f"验收未完成：{type(exc).__name__}；请检查显式凭据、服务和输出状态。", file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
