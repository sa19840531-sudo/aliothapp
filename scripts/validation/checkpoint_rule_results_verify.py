"""经正式 HTTP 登录与证据接口验证受控读侧样例，不替代真实采集验收。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import asyncpg


def request(base, path, *, token=None, payload=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if payload is None else json.dumps(payload).encode()
    query = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(query, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


async def business_counts(connection, tasks):
    return {
        table: await connection.fetchval(
            f"SELECT count(*) FROM {table} "
            f"WHERE {'id' if table == 'qc_task' else 'task_id'} = ANY($1::uuid[])",
            tasks,
        )
        for table in (
            "qc_task",
            "qc_rule_execution",
            "qc_action_execution",
            "qc_evidence_input_set",
            "qc_journey_checkpoint_attempt",
            "qc_v2_work_item",
        )
    }


async def main(args):
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8-sig"))
    cases = manifest["cases"]
    tasks = [item["task"] for item in cases]
    connection = await asyncpg.connect(os.environ[args.database_url_env])
    try:
        identity = dict(
            await connection.fetchrow(
                "SELECT current_database() AS database,current_user AS role,"
                "(SELECT system_identifier::text FROM pg_control_system()) AS system_identifier",
            )
        )
        assert identity == manifest["database_identity"]
        before = await business_counts(connection, tasks)
        status, body = request(args.base_url, "/api/v1/auth/login", payload=manifest["login"])
        assert status == 200, (status, body[:300])
        token = json.loads(body)["token"]
        status, body = request(
            args.base_url, "/api/v1/auth/login", payload=manifest["denied_login"]
        )
        assert status == 200, (status, body[:300])
        denied_token = json.loads(body)["token"]
        status, body = request(
            args.base_url,
            "/api/v1/auth/login",
            payload=manifest["workstation_login"],
        )
        assert status == 200, (status, body[:300])
        workstation_session = json.loads(body)
        workstation_token = workstation_session["token"]
        permissions = workstation_session["user"]["permissions"]
        assert "taskObjects:read:department" in permissions
        assert not {"users", "auditLogs", "taskObjects:read:tenant", "taskObjects:read:any"} & set(
            permissions
        )
        checks = []
        for item in cases:
            path = f"/api/v2/tasks/{item['task']}/h5-shortflows"
            assert request(args.base_url, path)[0] == 401
            assert request(args.base_url, path, token=denied_token)[0] == 404
            assert request(args.base_url, path, token=workstation_token)[0] == 200
            status, body = request(args.base_url, path, token=token)
            assert status == 200, (item["outcome"], status, body[:300])
            snapshot = json.loads(body)
            plan = snapshot["plans"][0]
            if item["outcome"] in {"pass", "fail", "evidence_insufficient"}:
                assert plan["rule_results"][0]["execution_status"] == item["outcome"], plan
            elif item["outcome"] == "missing_result":
                assert plan["rule_results"][0]["execution_status"] is None, plan
            else:
                assert plan["rule_results"] == [], plan
            assert all(
                key not in body.decode()
                for key in (
                    '"storage_path"',
                    '"judge_output"',
                    '"contract_payload"',
                    '"metadata"',
                )
            )
            screenshot = next(
                artifact
                for checkpoint in plan["checkpoints"]
                for attempt in checkpoint["attempts"]
                for binding in attempt["bindings"]
                for artifact in binding["artifacts"]
                if artifact["artifact_type"] == "screenshot"
            )
            ticket_path = f"/api/v1/evidence/{screenshot['artifact_id']}/ticket"
            assert request(args.base_url, ticket_path, token=denied_token, payload={})[0] == 404
            status, body = request(args.base_url, ticket_path, token=token, payload={})
            assert status == 200, (status, body[:300])
            status, content = request(args.base_url, "/api/v1" + json.loads(body)["url"])
            assert status == 200, (status, content[:300])
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            assert digest == screenshot["checksum"] and len(content) == screenshot["size_bytes"]
            archive_path = (
                path
                + "/"
                + urllib.parse.quote(plan["execution_plan_id"], safe="")
                + "/archive?snapshot_hash="
                + snapshot["snapshot_hash"]
            )
            status, content = request(args.base_url, archive_path, token=token)
            assert status == 200, (status, content[:300])
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                assert archive.testzip() is None
                archive_entries = len(archive.namelist())
            checks.append(
                {
                    "outcome": item["outcome"],
                    "task_id": item["task"],
                    "plan_status": plan["rule_results_status"],
                    "screenshot_sha256": digest,
                    "archive_entries": archive_entries,
                    "anonymous": 401,
                    "cross_tenant": 404,
                }
            )
        after = await business_counts(connection, tasks)
        assert before == after, (before, after)
        result = {
            "notice": manifest["fixture_notice"],
            "database_identity": identity,
            "checks": checks,
            "business_counts_before": before,
            "business_counts_after": after,
            "http_login": "真实独立API登录；测试账号来自展示种子，未覆盖生产登录依赖",
            "workstation_role": workstation_session["user"]["role"],
            "capture_acceptance": "未验证；业务记录、动作、观测与图片均为明确夹具",
        }
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "checked_cases": len(checks),
                    "business_counts_unchanged": before == after,
                    "output": args.output,
                },
                ensure_ascii=False,
            )
        )
    finally:
        await connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="/validation/seed-manifest.json")
    parser.add_argument("--base-url", default="http://alioth-checkpoint-results-api-20261019:8000")
    parser.add_argument(
        "--database-url-env", default="ALIOTH_CHECKPOINT_RESULTS_CONTAINER_DATABASE_URL"
    )
    parser.add_argument("--output", default="/validation/http-verification.json")
    asyncio.run(main(parser.parse_args()))
