"""公开读接口及下载的权限、快照漂移反例；不调用原站或模型。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import object_access
from services.api.app.api.v2 import h5_shortflows
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.main import create_app
from services.api.tests.conftest import TEST_AUTH_USER, pop_test_object_access
from services.api.tests.test_h5_shortflow_frontend_contract import snapshot

TASK = "00000000-0000-0000-0000-000000001234"


def client(*, revoked=False, drift=False, permissions=None, subject=None):
    view, content = snapshot()
    view["task_id"] = TASK
    app = create_app()
    repo = SimpleNamespace(
        catalog=AsyncMock(return_value={"definitions": [], "rules": []}),
        read=AsyncMock(return_value=view),
    )
    if drift:
        repo.read.side_effect = [view, {**view, "snapshot_hash": "0" * 64}]
    access = SimpleNamespace(
        can_access_task=AsyncMock(side_effect=[True, not revoked]),
        can_access_artifact=AsyncMock(return_value=True),
    )
    reader = SimpleNamespace(evidence_file=AsyncMock(return_value={"content": content}))
    audit = SimpleNamespace(record_export=AsyncMock())
    scope = AccessScope(
        actor_user_id=TEST_AUTH_USER.id,
        tenant_id=TEST_AUTH_USER.tenant_id,
        permissions=frozenset(
            permissions
            if permissions is not None
            else [
                "evidenceExports:original",
                "evidenceExports:redacted",
            ]
        ),
    )
    users = SimpleNamespace(
        find_by_id=AsyncMock(
            return_value={
                "id": scope.actor_user_id,
                "username": "tester",
                "full_name": "测试用户",
                "role": "平台管理员",
                "status": "enabled",
                "tenant_id": scope.tenant_id,
                **(subject or {}),
            }
        )
    )
    pop_test_object_access(app)
    app.dependency_overrides[object_access._get_object_access_repository] = lambda: access
    app.dependency_overrides[h5_shortflows._get_repository] = lambda: repo
    app.dependency_overrides[h5_shortflows._get_task_insight_service] = lambda: reader
    app.dependency_overrides[h5_shortflows._get_download_audit_repository] = lambda: audit
    app.dependency_overrides[h5_shortflows.require_access_scope] = lambda: scope
    app.dependency_overrides[h5_shortflows._get_ticket_subject_repository] = lambda: users
    return TestClient(app), repo, reader, audit, view


def test_public_catalog_and_checkpoint_read_not_legacy_steps():
    api, repo, reader, audit, view = client()
    assert api.get("/api/v2/h5-shortflows/catalog").status_code == 200
    assert api.get(f"/api/v2/tasks/{TASK}/h5-shortflows").json() == view
    reader.evidence_file.assert_not_called()
    audit.record_export.assert_not_called()


def test_whole_group_download_checks_original_and_records_audit():
    api, repo, reader, audit, view = client()
    result = api.get(
        f"/api/v2/tasks/{TASK}/h5-shortflows/plan/archive?snapshot_hash={view['snapshot_hash']}"
    )
    assert result.status_code == 200, result.text
    assert result.headers["content-type"] == "application/zip"
    assert result.headers["cache-control"] == "no-store"
    assert reader.evidence_file.await_count == 2
    assert repo.read.await_count == 2
    audit.record_export.assert_awaited_once()
    recorded = audit.record_export.call_args.kwargs
    assert recorded["request_id"] == result.headers["x-evidence-export-request-id"]
    assert recorded["mode"] == "original"
    assert recorded["result"] == "allowed"
    assert recorded["archive_checksum"].startswith("sha256:")
    assert recorded["materials"][0]["checksum"] == recorded["materials"][0]["original_checksum"]


def test_task_permission_revoked_while_packing_does_not_release_file():
    api, repo, reader, audit, view = client(revoked=True)
    result = api.get(
        f"/api/v2/tasks/{TASK}/h5-shortflows/plan/archive?snapshot_hash={view['snapshot_hash']}"
    )
    assert result.status_code == 404
    audit.record_export.assert_awaited_once()
    assert audit.record_export.call_args.kwargs["result"] == "denied"
    assert audit.record_export.call_args.kwargs["error_code"] == "OBJECT_ACCESS_REVOKED"


def test_late_manifest_during_download_returns_conflict_not_old_archive():
    api, repo, reader, audit, view = client(drift=True)
    result = api.get(
        f"/api/v2/tasks/{TASK}/h5-shortflows/plan/archive?snapshot_hash={view['snapshot_hash']}"
    )
    assert result.status_code == 409
    audit.record_export.assert_awaited_once()
    assert audit.record_export.call_args.kwargs["result"] == "failed"
    assert audit.record_export.call_args.kwargs["error_code"] == "EXPORT_SNAPSHOT_CHANGED"


def test_stale_snapshot_and_foreign_plan_do_not_read_original():
    api, repo, reader, audit, view = client()
    assert (
        api.get(
            f"/api/v2/tasks/{TASK}/h5-shortflows/foreign/archive?snapshot_hash={view['snapshot_hash']}"
        ).status_code
        == 409
    )
    assert (
        api.get(
            f"/api/v2/tasks/{TASK}/h5-shortflows/plan/archive?snapshot_hash={'0' * 64}"
        ).status_code
        == 409
    )
    reader.evidence_file.assert_not_called()
    assert audit.record_export.await_count == 2
    assert {item.kwargs["error_code"] for item in audit.record_export.await_args_list} == {
        "EXPORT_PLAN_NOT_FOUND",
        "EXPORT_SNAPSHOT_CHANGED",
    }


def test_repository_denial_does_not_leak_task_or_artifacts():
    api, repo, reader, audit, view = client()
    repo.read.return_value = None
    assert api.get(f"/api/v2/tasks/{TASK}/h5-shortflows").status_code == 404
    reader.evidence_file.assert_not_called()


def download(api, view, mode="original"):
    return api.get(
        f"/api/v2/tasks/{TASK}/h5-shortflows/plan/archive",
        params={"snapshot_hash": view["snapshot_hash"], "mode": mode},
    )


@pytest.mark.parametrize(
    "failure,status,code",
    [
        (FileNotFoundError("private/storage/path"), 404, "ARTIFACT_MISSING"),
        (ValueError("private/storage/path"), 409, "ARTIFACT_INTEGRITY_MISMATCH"),
        (TimeoutError("private/storage/path"), 503, "ARTIFACT_READ_TIMEOUT"),
        (RuntimeError("private/storage/path"), 503, "ARTIFACT_STORAGE_UNAVAILABLE"),
        (None, 404, "ARTIFACT_MISSING"),
        ({"content": b"broken"}, 409, "ARTIFACT_INTEGRITY_MISMATCH"),
    ],
)
def test_original_fault_is_located_and_audited_without_partial_archive(failure, status, code):
    api, repo, reader, audit, view = client()
    original = reader.evidence_file.return_value
    reader.evidence_file.side_effect = [original, failure]
    response = download(api, view)
    assert response.status_code == status, response.text
    assert "application/zip" not in response.headers["content-type"]
    assert "private/storage/path" not in response.text
    assert code in response.text and "frame" in response.text
    audit.record_export.assert_awaited_once()
    event = audit.record_export.call_args.kwargs
    assert event["result"] == "failed" and event["error_code"] == code
    assert event["failed_artifact_id"] == "frame"
    assert event["request_id"] == response.json()["error"]["error_id"]


@pytest.mark.parametrize(
    "subject",
    [
        {"status": "disabled"},
        {"tenant_id": "tenant-other"},
        {"tenant_status": "disabled"},
    ],
)
def test_current_subject_revocation_is_audited_before_release(subject):
    api, repo, reader, audit, view = client(subject=subject)
    response = download(api, view)
    assert response.status_code == 404
    event = audit.record_export.call_args.kwargs
    assert event["result"] == "denied" and event["error_code"] == "SUBJECT_REVOKED"


def test_mode_permission_is_independent_of_task_read_scope():
    api, repo, reader, audit, view = client(permissions=["evidenceExports:redacted"])
    assert download(api, view, "original").status_code == 403
    reader.evidence_file.assert_not_called()
    repo.read.assert_not_called()
    assert audit.record_export.call_args.kwargs["error_code"] == "EXPORT_MODE_FORBIDDEN"


def test_review_permission_does_not_expand_task_read_scope():
    api, repo, reader, audit, view = client()
    access = api.app.dependency_overrides[h5_shortflows._get_object_access_repository]()
    access.can_access_task.side_effect = None
    access.can_access_task.return_value = False
    assert download(api, view).status_code == 404
    reader.evidence_file.assert_not_called()
    repo.read.assert_not_called()
    event = audit.record_export.call_args.kwargs
    assert not event["artifact_ids"] and event["result"] == "denied"


@pytest.mark.parametrize("late", [False, True])
def test_member_access_and_lifecycle_are_checked_before_read_and_delivery(late):
    api, repo, reader, audit, view = client()
    access = api.app.dependency_overrides[h5_shortflows._get_object_access_repository]()
    access.can_access_artifact.side_effect = [True, False] if late else [False]
    response = download(api, view)
    assert response.status_code == 404
    event = audit.record_export.call_args.kwargs
    assert event["result"] == "denied" and event["failed_artifact_id"] == "frame"
    assert event["error_code"] == ("ARTIFACT_ACCESS_REVOKED" if late else "ARTIFACT_ACCESS_DENIED")
    if not late:
        reader.evidence_file.assert_not_called()


@pytest.mark.parametrize("failed", [False, True])
def test_audit_failure_never_releases_archive_or_storage_error(failed):
    api, repo, reader, audit, view = client()
    if failed:
        reader.evidence_file.side_effect = FileNotFoundError("storage-secret")
    audit.record_export.side_effect = RuntimeError("database-secret")
    response = download(api, view)
    assert response.status_code == 503
    assert "EXPORT_AUDIT_UNAVAILABLE" in response.text
    assert "secret" not in response.text
    assert "application/zip" not in response.headers["content-type"]


def test_redacted_png_is_derived_and_receipt_cannot_leak_plan_text():
    import json
    from hashlib import sha256
    from io import BytesIO
    from zipfile import ZipFile

    from PIL import Image

    api, repo, reader, audit, view = client()
    output = BytesIO()
    Image.new("RGB", (20, 10), "red").save(output, format="PNG")
    original = output.getvalue()
    plan = view["plans"][0]
    plan["rule_result_reason"] = "private@example.com password=secret"
    for binding in plan["checkpoints"][0]["attempts"][0]["bindings"]:
        binding["artifacts"][0].update(
            size_bytes=len(original), checksum="sha256:" + sha256(original).hexdigest()
        )
    reader.evidence_file.return_value = {"content": original}
    response = download(api, view, "redacted")
    assert response.status_code == 200, response.text
    assert response.headers["x-evidence-export-mode"] == "redacted"
    with ZipFile(BytesIO(response.content)) as archive:
        receipt = json.loads(archive.read("receipt.json"))
        assert "private@example.com" not in archive.read("receipt.json").decode()
        assert receipt["request_id"] == response.headers["x-evidence-export-request-id"]
        assert receipt["business_reviewable"] is False
        for material in receipt["materials"]:
            assert material["redaction_strategy"] == "full_frame_masked"
            assert material["checksum"] != material["original_checksum"]
            with Image.open(BytesIO(archive.read(material["path"]))) as masked:
                assert masked.size == (20, 10)
                assert masked.getpixel((0, 0)) == (32, 32, 32)
    assert reader.evidence_file.return_value["content"] == original
    assert audit.record_export.call_args.kwargs["mode"] == "redacted"


def test_unknown_binary_cannot_be_falsely_marked_redacted():
    api, repo, reader, audit, view = client()
    view["plans"][0]["checkpoints"][0]["attempts"][0]["bindings"][0]["artifacts"][0][
        "content_type"
    ] = "application/octet-stream"
    response = download(api, view, "redacted")
    assert response.status_code == 409
    assert "REDACTION_FORMAT_UNSUPPORTED" in response.text
    assert audit.record_export.call_args.kwargs["result"] == "failed"
