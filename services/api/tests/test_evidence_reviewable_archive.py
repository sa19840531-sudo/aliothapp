"""公开整组交付必须选择同源不可变表示，局部副本保留业务上下文。"""

import json
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import pytest
from PIL import Image

from services.api.app.api.v2 import h5_shortflows
from services.api.app.application.evidence_archive import checksum
from services.api.app.application.evidence_redaction_render import render_redaction
from services.api.app.application.evidence_redactions import plan_digest
from services.api.app.domain.evidence.redaction import POLICY_VERSION, RedactionSpec
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.tests.test_h5_shortflow_read_api import TASK, client


def group_client(**options):
    api, repository, reader, audit, view = client(**options)
    output = BytesIO()
    image = Image.new("RGB", (20, 10), "green")
    for x in range(10):
        for y in range(10):
            image.putpixel((x, y), (255, 0, 0))
    image.save(output, format="PNG")
    content = [output.getvalue(), b'{"account":"SECRET-ACCOUNT","price":99,"paid":true}']
    specs = [RedactionSpec(method="image_regions", image_width=20, image_height=10,
                           regions=[dict(x=0, y=0, width=10, height=10)]),
             RedactionSpec(method="json_pointers", pointers=["/account"])]
    plan = view["plans"][0]
    plan["agent_run_id"] = "run"
    attempt = plan["checkpoints"][0]["attempts"][0]
    attempt["session_id"] = "session"
    plans, sources, original = {}, {}, {}
    for index, binding in enumerate(attempt["bindings"]):
        artifact_id = f"artifact-{index}"
        media = ["image/png", "application/json"][index]
        artifact = {"artifact_id": artifact_id, "artifact_type": ["screenshot", "dom"][index],
                    "checksum": checksum(content[index]), "size_bytes": len(content[index]),
                    "content_type": media}
        binding.update(manifest_id=f"manifest-{index}", manifest_version=2, artifacts=[artifact])
        source = dict(
            artifact_id=artifact_id, task_id=TASK, agent_run_id="run", session_id="session",
            capture_id=binding["capture_id"], manifest_id=binding["manifest_id"],
            manifest_version=2, manifest_hash="a" * 64, original_checksum=artifact["checksum"],
            original_size_bytes=artifact["size_bytes"], original_content_type=media,
            artifact_type=artifact["artifact_type"],
        )
        source.update(source_hash=stable_protocol_hash(source), tenant_id="tenant")
        rendered = render_redaction(content[index], media, specs[index])
        frozen = dict(
            redaction_id=str(uuid4()), schema_version="evidence-redaction.v1", source=source,
            artifact_id=artifact_id, spec=specs[index].model_dump(mode="json", exclude_none=True),
            policy_version=POLICY_VERSION, business_reviewable=True,
            derived_checksum=rendered.checksum, derived_size_bytes=rendered.size_bytes,
            derived_content_type=rendered.content_type,
        )
        frozen["plan_hash"] = plan_digest(frozen)
        plans[frozen["redaction_id"]] = frozen
        sources[artifact_id], original[artifact_id] = source, content[index]
    redactions = SimpleNamespace(
        get=AsyncMock(side_effect=lambda *, redaction_id: plans.get(redaction_id)),
        source=AsyncMock(side_effect=lambda **kwargs: sources[kwargs["artifact_id"]]),
    )
    reader.evidence_file.side_effect = lambda artifact_id, **_: {"content": original[artifact_id]}
    api.app.dependency_overrides[h5_shortflows._get_redaction_repository] = lambda: redactions
    body = {"snapshot_hash": view["snapshot_hash"],
            "redaction_ids": {p["artifact_id"]: key for key, p in plans.items()}}
    return api, body, plans, sources, redactions, reader, audit, view


def download(api, body):
    return api.post(f"/api/v2/tasks/{TASK}/h5-shortflows/plan/archive", json=body)


def test_public_group_preserves_business_pixels_and_json_with_complete_traceable_receipt():
    api, body, plans, _, _, reader, audit, _ = group_client(
        permissions=["evidenceExports:redacted"],
    )
    response = download(api, body)
    assert response.status_code == 200, response.text
    assert response.headers["x-evidence-export-policy"] == POLICY_VERSION
    assert response.headers["cache-control"] == "no-store"
    with ZipFile(BytesIO(response.content)) as archive:
        receipt = json.loads(archive.read("receipt.json"))
        assert receipt["business_reviewable"] and receipt["schema_version"].endswith(".v3")
        assert receipt["mode"] == "redacted" and receipt["policy_version"] == POLICY_VERSION
        assert len(receipt["materials"]) == 2
        for item in receipt["materials"]:
            frozen = plans[item["redaction_id"]]
            data = archive.read(item["path"])
            assert checksum(data) == item["checksum"] == frozen["derived_checksum"]
            assert item["redaction_plan_hash"] == frozen["plan_hash"]
            assert item["original_checksum"] == frozen["source"]["original_checksum"]
            assert item["manifest_version"] == 2
            if item["content_type"] == "image/png":
                with Image.open(BytesIO(data)) as derived:
                    assert derived.getpixel((15, 5)) == (0, 128, 0)
                    assert derived.getpixel((5, 5)) != (255, 0, 0)
            else:
                result = json.loads(data)
                assert result["price"] == 99 and result["paid"] is True
                assert b"SECRET-ACCOUNT" not in data
    event = audit.record_export.call_args.kwargs
    assert event["result"] == "allowed" and event["policy_version"] == POLICY_VERSION
    assert {item["redaction_id"] for item in event["materials"]} == set(plans)
    assert reader.evidence_file.await_count == 2


@pytest.mark.parametrize("fault", ["missing", "extra", "unknown", "wrong_source", "tampered",
                                  "derived_hash", "manifest", "run", "size"])
def test_ambiguous_or_tampered_redaction_selection_never_delivers_partial_group(fault):
    api, body, plans, _, _, reader, audit, _ = group_client()
    first = next(iter(plans.values()))
    if fault == "missing":
        body["redaction_ids"].pop(first["artifact_id"])
    elif fault == "extra":
        body["redaction_ids"]["unknown"] = str(uuid4())
    elif fault == "unknown":
        body["redaction_ids"][first["artifact_id"]] = str(uuid4())
    elif fault == "wrong_source":
        body["redaction_ids"][first["artifact_id"]] = list(plans)[1]
    elif fault == "tampered":
        first["spec"]["regions"][0]["width"] = 9
    elif fault == "derived_hash":
        first["derived_checksum"] = "sha256:" + "a" * 64
        first["plan_hash"] = plan_digest(first)
    else:
        key = {"manifest": "manifest_version", "run": "agent_run_id",
               "size": "original_size_bytes"}[fault]
        first["source"][key] = 99 if fault != "run" else "other-run"
        first["plan_hash"] = plan_digest(first)
    response = download(api, body)
    assert response.status_code == 409, response.text
    assert "application/zip" not in response.headers["content-type"]
    assert audit.record_export.call_args.kwargs["result"] == "failed"
    if fault in {"tampered", "manifest", "run", "size"}:
        reader.evidence_file.assert_not_called()
    if fault in {"tampered", "derived_hash", "manifest", "run", "size"}:
        event = audit.record_export.call_args.kwargs
        assert {item["redaction_id"] for item in event["materials"]} == set(plans)
        assert all(item.get("redaction_plan_hash") for item in event["materials"])


def test_late_source_drift_and_revoked_permission_close_delivery_after_preparation():
    api, body, _, sources, redactions, _, audit, _ = group_client()
    calls = 0

    async def drifting_source(**kwargs):
        nonlocal calls
        calls += 1
        value = deepcopy(sources[kwargs["artifact_id"]])
        if calls > 2:
            value["source_hash"] = "b" * 64
        return value

    redactions.source.side_effect = drifting_source
    response = download(api, body)
    assert response.status_code == 409
    assert audit.record_export.call_args.kwargs["error_code"] == "REDACTION_SOURCE_CHANGED"
    api, body, _, _, _, _, audit, _ = group_client(revoked=True)
    response = download(api, body)
    assert response.status_code == 404
    assert audit.record_export.call_args.kwargs["result"] == "denied"


def test_local_export_requires_redacted_permission_and_audit_before_any_response_bytes():
    api, body, _, _, _, reader, audit, _ = group_client(permissions=["evidenceExports:original"])
    assert download(api, body).status_code == 403
    reader.evidence_file.assert_not_called()
    assert audit.record_export.call_args.kwargs["result"] == "denied"
    api, body, _, _, _, _, audit, _ = group_client()
    audit.record_export.side_effect = RuntimeError("private storage path")
    response = download(api, body)
    assert response.status_code == 503
    assert "EXPORT_AUDIT_UNAVAILABLE" in response.text and "private storage" not in response.text
