"""实况证据导出只读本次产物，不保存票据或跨主机下载。"""

import base64
import hashlib
import json
import os

import pytest

from scripts import web_live_acceptance


@pytest.mark.parametrize(
    "ticket_path,checksum,expected",
    [
        ("/evidence/11111111-1111-4111-8111-111111111111/file?ticket=secret-ticket", None, "saved"),
        ("https://unrelated.test/file?ticket=secret-ticket", None, "download_failed"),
        (
            "/evidence/11111111-1111-4111-8111-111111111111/file?ticket=x&representation=preview",
            None,
            "download_failed",
        ),
        (
            "/evidence/11111111-1111-4111-8111-111111111111/file?ticket=x&ticket=y",
            None,
            "download_failed",
        ),
        (
            "/evidence/11111111-1111-4111-8111-111111111111/file?ticket=secret-ticket",
            "sha256:" + "0" * 64,
            "download_failed",
        ),
    ],
)
def test_artifact_export_keeps_local_checksum_and_rejects_foreign_ticket(
    ticket_path,
    checksum,
    expected,
    tmp_path,
):
    payload = b"<main>scoped fixture</main>"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    artifact_id = "11111111-1111-4111-8111-111111111111"
    requests = []

    def requester(method, endpoint, body, headers):
        assert method == "POST"
        assert endpoint == f"http://127.0.0.1:8000/api/v1/evidence/{artifact_id}/ticket"
        assert headers == {"Authorization": "Bearer private-token"}
        return {"url": ticket_path}

    def fetch_bytes(url):
        requests.append(url)
        return payload

    manifest = web_live_acceptance.export_live_artifacts(
        output=tmp_path,
        sites=[
            {
                "evidence_bundle": {
                    "captures": [
                        {
                            "artifacts": [
                                {
                                    "artifact_id": artifact_id,
                                    "artifact_type": "dom",
                                    "checksum": checksum or digest,
                                }
                            ]
                        }
                    ]
                }
            }
        ],
        api_base_url="http://127.0.0.1:8000",
        access_token="private-token",
        requester=requester,
        fetch_bytes=fetch_bytes,
    )
    assert manifest[0]["status"] == expected
    if expected == "saved":
        assert (tmp_path / manifest[0]["path"]).read_bytes() == payload
        assert manifest[0]["checksum"] == digest
    elif ticket_path.startswith("https:"):
        assert not requests
    saved = json.loads((tmp_path / "evidence/artifact-manifest.json").read_text(encoding="utf-8"))
    assert "secret-ticket" not in str(saved)
    assert "private-token" not in str(saved)


def test_export_includes_all_ad_artifact_ids_and_keeps_json_screenshot_payload(tmp_path):
    pair_id = "11111111-1111-4111-8111-111111111111"
    trace_id = "22222222-2222-4222-8222-222222222222"
    manifest = web_live_acceptance.export_live_artifacts(
        output=tmp_path,
        api_base_url="http://127.0.0.1:8000",
        access_token="private-token",
        sites=[
            {
                "ad_landing_evidence": {
                    "artifact_refs": {"source_screenshot": pair_id, "landing_screenshot": pair_id},
                    "artifact_ids": [pair_id, trace_id],
                }
            }
        ],
        requester=lambda method, endpoint, body, headers: {
            "url": "/evidence/" + endpoint.split("/")[-2] + "/file?ticket=private"
        },
        fetch_bytes=lambda url: (
            b'{"purpose":"ad_landing_consistency","status":"observed"}',
            "sha256:"
            + hashlib.sha256(
                b'{"purpose":"ad_landing_consistency","status":"observed"}'
            ).hexdigest(),
        ),
    )
    assert len(manifest) == 2 and all(row["status"] == "saved" for row in manifest)
    assert all(row["path"].endswith(".json") for row in manifest)


def test_real_endpoint_exports_original_pair_and_preserves_preview_auth_and_sha(
    tmp_path, monkeypatch
):
    import logging

    from fastapi.testclient import TestClient

    from services.api.app.api.v1 import evidence
    from services.api.app.application.task_insight import TaskInsightService
    from services.api.app.main import create_app

    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", True)

    artifact_id = "11111111-1111-4111-8111-111111111111"
    frames = [b"source-png", b"landing-png"]
    original = json.dumps(
        {
            "purpose": "ad_landing_consistency",
            "frames": [
                {"index": index, "encoding": "base64", "data": base64.b64encode(frame).decode()}
                for index, frame in enumerate(frames)
            ],
        }
    ).encode()
    digest = "sha256:" + hashlib.sha256(original).hexdigest()

    class Repository:
        checksum = digest

        async def fetch_evidence_artifact(self, requested):
            assert requested == artifact_id
            return {
                "artifact_id": artifact_id,
                "artifact_type": "screenshot",
                "storage_path": "pair.json",
                "content_type": "application/json",
                "checksum": self.checksum,
            }

    class Store:
        async def read(self, path):
            return original

    service = TaskInsightService(repository=Repository(), evidence_store_factory=Store)
    app = create_app()
    app.dependency_overrides[evidence._get_task_insight_service] = lambda: service
    client = TestClient(app)
    ticket = client.post(f"/api/v1/evidence/{artifact_id}/ticket").json()["url"]
    assert (
        client.get(f"/api/v1/evidence/{artifact_id}/file?representation=original").status_code
        == 404
    )
    assert client.get("/api/v1" + ticket).content == frames[0]
    raw = client.get("/api/v1" + ticket + "&representation=original")
    assert raw.status_code == 200
    assert raw.content == original
    assert raw.headers["x-artifact-sha256"] == digest

    def requester(method, url, body, headers):
        return client.post(url, headers=headers).json()

    def fetch_bytes(url):
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 200
        return response.content, response.headers.get("x-artifact-sha256")

    rows = web_live_acceptance.export_live_artifacts(
        output=tmp_path,
        sites=[
            {"artifact_refs": {"source_screenshot": artifact_id, "landing_screenshot": artifact_id}}
        ],
        api_base_url="http://127.0.0.1",
        access_token="not-written",
        requester=requester,
        fetch_bytes=fetch_bytes,
    )
    assert rows[0]["status"] == "saved"
    saved = (tmp_path / rows[0]["path"]).read_bytes()
    assert saved == original and len(json.loads(saved)["frames"]) == 2
    assert rows[0]["checksum"] == digest
    assert rows[0]["representation"] == "original"
    assert "not-written" not in (tmp_path / "evidence/artifact-manifest.json").read_text()
    service._repository.checksum = "sha256:" + "0" * 64
    assert client.get("/api/v1" + ticket + "&representation=original").status_code == 409
    from services.api.tests.conftest import pop_test_ticket_subject

    class DisabledSubject:
        async def find_by_id(self, user_id):
            return {"status": "disabled"}

    pop_test_ticket_subject(app)
    app.dependency_overrides[evidence._get_ticket_subject_repository] = DisabledSubject
    assert client.get("/api/v1" + ticket + "&representation=original").status_code == 404


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="只允许显式隔离 PG")
async def test_real_pg_endpoint_exports_stored_original_checksum(monkeypatch):
    import logging
    from uuid import uuid4

    import httpx

    from services.api.app.api.v1 import evidence
    from services.api.app.application.task_insight import TaskInsightService
    from services.api.app.infrastructure.db.task_insight_repository import (
        PostgresTaskInsightRepository,
    )
    from services.api.app.main import create_app
    from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import _database

    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", True)
    original = (b'{"purpose":"ad_landing_consistency",'
                b'"frames":[{"data":"c291cmNl"},{"data":"bGFuZGluZw=="}]}')
    digest = "sha256:" + hashlib.sha256(original).hexdigest()
    artifact_id = str(uuid4())

    class Store:
        async def read(self, path):
            return original

    async with _database() as (control, pool, _, ids):
        await control.execute(
            "INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,source,"
            "storage_path,content_type,checksum,size_bytes) VALUES($1::uuid,$2::uuid,$3::uuid,"
            "'screenshot','baseline','controlled/pair.json','application/json',$4,$5)",
            artifact_id,
            ids["task"],
            ids["capture"],
            digest,
            len(original),
        )
        try:
            service = TaskInsightService(
                repository=PostgresTaskInsightRepository(pool=pool),
                evidence_store_factory=Store,
            )
            app = create_app()
            app.dependency_overrides[evidence._get_task_insight_service] = lambda: service
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                issued = await client.post(f"/api/v1/evidence/{artifact_id}/ticket")
                raw = await client.get(
                    "/api/v1" + issued.json()["url"] + "&representation=original"
                )
                assert raw.status_code == 200
                assert raw.content == original
                assert raw.headers["x-artifact-sha256"] == digest
        finally:
            await control.execute("DELETE FROM qc_evidence_artifact WHERE id=$1::uuid", artifact_id)
