"""Web 实况的安全登录、逐站失败隔离与本地证据导出。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path


def prepare_live_access(args, *, requester):
    """在任何任务请求前验证凭据模式和跨身份成年配置。"""
    from scripts.web_v2_six_site_shadow import _validate_api_base_url

    api_base = _validate_api_base_url(args.api_base_url)
    token = os.environ.get("ALIOTH_WEB_V2_ACCESS_TOKEN", "").strip()
    if args.username and token:
        raise ValueError("登录模式与 ALIOTH_WEB_V2_ACCESS_TOKEN 模式互斥")
    if args.password_env and not args.username:
        raise ValueError("--password-env 必须同时指定 --username")
    statement = args.adult_self_attestation
    source = "explicit_argument"
    if args.adult_statement_env:
        if statement:
            raise ValueError("成年声明参数与环境配置模式互斥")
        statement = os.environ.get(args.adult_statement_env, "").strip()
        source = "environment"
        if not statement and args.adult_statement_env == "ALIOTH_WEB_ADULT_STATEMENT":
            from services.api.app.core.config import _build_web_adult_self_attestation_settings

            configured = _build_web_adult_self_attestation_settings()
            if configured.enabled:
                statement = configured.statement_version
                source = "persistent_web_configuration"
    if statement != "adult-v1":
        raise ValueError("live 模式需要已持久配置的 adult-v1 成年声明")
    if args.username:
        password = os.environ.get(args.password_env or "ALIOTH_LIVE_PASSWORD", "")
        if not password:
            raise ValueError("指定的密码环境变量未配置")
        try:
            receipt = requester(
                "POST",
                f"{api_base}/api/v1/auth/login",
                {"username": args.username, "password": password},
                {},
            )
            token = str(receipt.get("token") or "").strip()
        except Exception:
            raise ValueError("WEB_LIVE_LOGIN_FAILED") from None
        if not token:
            raise ValueError("WEB_LIVE_LOGIN_TOKEN_MISSING")
    if not token:
        raise ValueError("live 模式必须提供登录环境凭据或访问令牌")
    return (
        token,
        statement,
        {
            "started_at": datetime.now(UTC).isoformat(),
            "authentication": {"fresh_login": bool(args.username), "token_stored": False},
            "adult_statement": {
                "source": source,
                "version": statement,
                "scope": "account_session_site_independent",
            },
            "sites": [],
        },
    )


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_live_acceptance(args, *, access_token, statement, audit, targets, requester):
    """逐站保存状态；单站网络异常不抹除已提交身份或后续目标。"""
    from scripts.web_v2_six_site_shadow import (
        LiveSiteResult,
        _aggregate_live_results,
        _build_live_process_nodes,
        _hydrate_preview_exploration_summary,
        _normalize_live_site_evidence_bundle,
        run_live_sites,
    )

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for target in targets:
        site_audit = {"target_url": target, "events": []}
        audit["sites"].append(site_audit)
        state = {}

        def tracked_request(
            method, endpoint, payload, headers, *, state=state, site_audit=site_audit
        ):
            if method == "POST" and endpoint.endswith("/api/v2/tasks"):
                if audit["adult_statement"]["source"] == "persistent_web_configuration":
                    payload = dict(payload, target=dict(payload["target"]))
                    payload["target"].pop("adult_self_attestation", None)
            response = requester(method, endpoint, payload, headers)
            if method == "POST" and endpoint.endswith("/api/v2/tasks"):
                state["submission"] = response
                event = {key: response.get(key) for key in ("task_id", "run_id", "status")}
                site_audit.update(event)
            elif "/workstation/tasks/" in endpoint and not endpoint.endswith("/web-evidence"):
                state["task"] = response
                event = {
                    key: response.get(key)
                    for key in ("task_id", "run_id", "status", "report_status")
                }
            else:
                if endpoint.endswith("/web-evidence"):
                    state["evidence"] = response
                elif "/workstation/reports/" in endpoint:
                    state["report"] = response
                return response
            if not site_audit["events"] or site_audit["events"][-1] != event:
                site_audit["events"].append(event)
                _write_json(output / "execution-audit.json", audit)
            return response

        try:
            site = run_live_sites(
                api_base_url=args.api_base_url,
                access_token=access_token,
                adult_self_attestation=statement,
                requester=tracked_request,
                poll_interval_seconds=args.poll_interval_seconds,
                site_timeout_seconds=args.site_timeout_seconds,
                targets=(target,),
            ).sites[0]
        except Exception as exc:
            code = (
                "WEB_LIVE_SERVER_ERROR"
                if isinstance(exc, urllib.error.HTTPError) and exc.code >= 500
                else "WEB_LIVE_HTTP_ERROR"
                if isinstance(exc, urllib.error.HTTPError)
                else "WEB_LIVE_TIMEOUT"
                if isinstance(exc, TimeoutError)
                else "WEB_LIVE_NETWORK_ERROR"
                if isinstance(exc, urllib.error.URLError)
                else "WEB_LIVE_REPORT_FAILED"
            )
            submission = state.get("submission", {})
            task = state.get("task", {})
            task_id, run_id = (
                str(submission.get("task_id") or ""),
                str(submission.get("run_id") or ""),
            )
            bundle = _normalize_live_site_evidence_bundle(state.get("evidence", {}))
            if task_id and not state.get("evidence"):
                try:
                    raw = requester(
                        "GET",
                        f"{args.api_base_url.rstrip('/')}/api/v1/workstation/tasks/{task_id}/web-evidence",
                        None,
                        {"Authorization": f"Bearer {access_token}"},
                    )
                    bundle = _normalize_live_site_evidence_bundle(raw)
                except Exception:
                    pass
            summary = _hydrate_preview_exploration_summary(
                target_url=target,
                report_exploration={},
                evidence_bundle=bundle,
            )
            status = str(
                task.get("status") or ("outcome_unknown" if task_id else "submission_failed")
            )
            site = LiveSiteResult(
                target_url=target,
                task_id=task_id,
                run_id=run_id,
                task_status=status,
                report_status=str(task.get("report_status") or "unavailable"),
                failure_reason=code,
                report_stage="unavailable",
                is_final=False,
                exploration_summary=summary,
                evidence_bundle=bundle,
                process_nodes=_build_live_process_nodes(
                    submission_status=submission.get("status"),
                    task_status=status,
                    report_stage="unavailable",
                    evidence_bundle=bundle,
                ),
            )
        results.append(site)
        site_audit.update(
            task_id=site.task_id,
            run_id=site.run_id,
            status=site.task_status,
            failure_reason=site.failure_reason,
            report_stage=site.report_stage,
        )
        _write_json(output / "execution-audit.json", audit)
        _write_json(output / "evidence" / f"site-{len(results)}.json", asdict(site))
        _write_json(output / "progress-report.json", {"sites": [asdict(item) for item in results]})
    audit["finished_at"] = datetime.now(UTC).isoformat()
    artifacts = export_live_artifacts(
        output=output,
        sites=[asdict(site) for site in results],
        api_base_url=args.api_base_url,
        access_token=access_token,
        requester=requester,
    )
    audit["artifacts"] = {
        "total": len(artifacts),
        "saved": sum(item["status"] == "saved" for item in artifacts),
    }
    _write_json(output / "execution-audit.json", audit)
    return _aggregate_live_results(tuple(results), expected_targets=targets)


def write_live_json_report(output: Path, report) -> None:
    _write_json(output / "final-exploration-report.json", asdict(report))


def export_live_artifacts(
    *, output, sites, api_base_url, access_token, requester, fetch_bytes=None
):
    """以短时票据回读本次引用的产物，落盘前核对路径和摘要。"""
    from scripts.web_v2_six_site_shadow import _RejectRedirectHandler, _validate_api_base_url

    api_base = _validate_api_base_url(api_base_url)
    artifacts = {}
    extensions = {
        "dom": ".html",
        "screenshot": ".png",
        "visible_text": ".txt",
        "ui_snapshot": ".json",
        "action_trace": ".json",
        "unknown": ".bin",
    }

    def collect(value):
        if isinstance(value, dict):
            artifact_id = str(value.get("artifact_id") or "")
            if (
                re.fullmatch(r"[0-9a-fA-F-]{36}", artifact_id)
                and value.get("artifact_type") in extensions
            ):
                artifacts[artifact_id] = value
            refs = value.get("artifact_refs")
            if isinstance(refs, dict):
                for role, reference in refs.items():
                    if isinstance(reference, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", reference):
                        kind = "screenshot" if role.endswith("screenshot") else "dom"
                        artifacts.setdefault(
                            reference, {"artifact_id": reference, "artifact_type": kind}
                        )
            raw_ids = value.get("artifact_ids")
            for reference in raw_ids if isinstance(raw_ids, (list, tuple)) else ():
                if isinstance(reference, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", reference):
                    artifacts.setdefault(
                        reference, {"artifact_id": reference, "artifact_type": "unknown"}
                    )
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list | tuple):
            for nested in value:
                collect(nested)

    collect(sites)
    if fetch_bytes is None:
        opener = urllib.request.build_opener(_RejectRedirectHandler())

        def fetch_bytes(url):
            with opener.open(url, timeout=60) as response:
                return response.read(), response.headers.get("X-Artifact-SHA256")

    manifest = []
    for artifact_id, artifact in sorted(artifacts.items()):
        kind = artifact["artifact_type"]
        row = {"artifact_id": artifact_id, "artifact_type": kind}
        try:
            receipt = requester(
                "POST",
                f"{api_base}/api/v1/evidence/{artifact_id}/ticket",
                None,
                {"Authorization": f"Bearer {access_token}"},
            )
            ticket_path = str(receipt.get("url") or "")
            parsed = urllib.parse.urlsplit(ticket_path)
            if (
                parsed.scheme
                or parsed.netloc
                or parsed.fragment
                or parsed.path != f"/evidence/{artifact_id}/file"
            ):
                raise ValueError("WEB_LIVE_ARTIFACT_TICKET_INVALID")
            query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
            if set(query) != {"ticket"} or len(query["ticket"]) != 1:
                raise ValueError("WEB_LIVE_ARTIFACT_TICKET_INVALID")
            fetched = fetch_bytes(f"{api_base}/api/v1{ticket_path}&representation=original")
            payload, original_checksum = (
                fetched if isinstance(fetched, tuple) else (fetched, artifact.get("checksum"))
            )
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            if original_checksum != digest or (
                artifact.get("checksum") and artifact["checksum"] != digest
            ):
                raise ValueError("WEB_LIVE_ARTIFACT_CHECKSUM_MISMATCH")
            extension = extensions[kind]
            if payload.lstrip().startswith((b"{", b"[")):
                try:
                    json.loads(payload)
                except (ValueError, UnicodeError):
                    pass
                else:
                    extension = ".json"
            relative = f"evidence/artifacts/{artifact_id}{extension}"
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            row.update(
                status="saved",
                path=relative,
                checksum=digest,
                size_bytes=len(payload),
                representation="original",
            )
        except Exception:
            row.update(status="download_failed", reason_code="WEB_LIVE_ARTIFACT_DOWNLOAD_FAILED")
        manifest.append(row)
    _write_json(output / "evidence/artifact-manifest.json", manifest)
    return manifest
