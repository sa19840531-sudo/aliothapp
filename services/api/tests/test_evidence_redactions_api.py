"""公开局部脱敏入口使用真实渲染器，验证权限、内容和交付前的审计边界。"""

import base64
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1 import object_access
from services.api.app.api.v2 import evidence_redactions as routes
from services.api.app.application.evidence_redaction_render import POLICY_VERSION, render_redaction
from services.api.app.application.evidence_redactions import plan_digest
from services.api.app.domain.evidence.redaction import RedactionSpec
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.main import create_app
from services.api.tests.conftest import TEST_AUTH_USER, pop_test_object_access

ARTIFACT = "00000000-0000-0000-0000-000000009701"
PLAN = "00000000-0000-0000-0000-000000009702"
TASK = "00000000-0000-0000-0000-000000009703"
MANAGE = {"evidenceRedactions:manage", "evidenceExports:original", "evidenceExports:redacted"}
SECRET = "13800138000"
TEXT = f"订单已支付，手机 {SECRET}，金额 42.50 元 ✅"


def digest(content):
    return "sha256:" + sha256(content).hexdigest()


def text_spec():
    start = TEXT.index(SECRET)
    return {"method": "text_ranges", "ranges": [{"start": start, "end": start + len(SECRET)}]}


def frozen_plan(source, content, spec):
    rendered = render_redaction(content, source["original_content_type"], RedactionSpec(**spec))
    plan = {
        "redaction_id": PLAN,
        "schema_version": "evidence-redaction.v1",
        "artifact_id": ARTIFACT,
        "source": deepcopy(source),
        "spec": RedactionSpec(**spec).model_dump(mode="json", exclude_none=True),
        "policy_version": POLICY_VERSION,
        "derived_checksum": rendered.checksum,
        "derived_size_bytes": rendered.size_bytes,
        "derived_content_type": rendered.content_type,
        "business_reviewable": True,
        "reviewed_by": TEST_AUTH_USER.id,
        "tenant_id": TEST_AUTH_USER.tenant_id,
        "created_at": datetime.now(UTC).isoformat(),
    }
    plan["plan_hash"] = plan_digest(plan)
    return plan


def context(*, permissions=None, media="text/plain", content=None, spec=None, tenant=None):
    content = TEXT.encode() if content is None else content
    spec = text_spec() if spec is None else spec
    tenant = tenant or TEST_AUTH_USER.tenant_id
    source = {
        "artifact_id": ARTIFACT,
        "task_id": TASK,
        "agent_run_id": "00000000-0000-0000-0000-000000009704",
        "session_id": "session-local-redaction",
        "capture_id": "00000000-0000-0000-0000-000000009705",
        "manifest_id": "evidence-manifest:" + "a" * 64,
        "manifest_version": 1,
        "manifest_hash": "a" * 64,
        "original_checksum": digest(content),
        "original_size_bytes": len(content),
        "original_content_type": media,
        "artifact_type": "visible_text" if media.startswith("text/") else "console_log",
    }
    source["source_hash"] = stable_protocol_hash(source)
    source["tenant_id"] = TEST_AUTH_USER.tenant_id
    plan = frozen_plan(source, content, spec)
    plans = {PLAN: deepcopy(plan)}
    audit = SimpleNamespace(record_redaction=AsyncMock())
    repo = SimpleNamespace(
        source=AsyncMock(side_effect=lambda **_kwargs: deepcopy(source)),
        get=AsyncMock(side_effect=lambda **kwargs: deepcopy(plans.get(kwargs["redaction_id"]))),
        list_for_artifact=AsyncMock(side_effect=lambda **_kwargs: deepcopy(list(plans.values()))),
    )

    async def save(*, plan, audit, audit_kwargs):
        # 公开接口只验证保存请求的合同；真实事务与回滚由 PostgreSQL 专项覆盖。
        await audit.record_redaction(**audit_kwargs)
        plans[plan["redaction_id"]] = deepcopy(plan)

    repo.save = AsyncMock(side_effect=save)
    reader = SimpleNamespace(evidence_file=AsyncMock(return_value={"content": content}))
    scope = AccessScope(
        actor_user_id=TEST_AUTH_USER.id,
        tenant_id=tenant,
        permissions=frozenset(MANAGE if permissions is None else permissions),
    )

    async def allows(*, artifact_id, scope):
        return artifact_id == ARTIFACT and scope.tenant_id == TEST_AUTH_USER.tenant_id

    access = SimpleNamespace(can_access_artifact=AsyncMock(side_effect=allows))
    subject = {
        "id": TEST_AUTH_USER.id,
        "username": TEST_AUTH_USER.username,
        "full_name": TEST_AUTH_USER.full_name,
        "role": "平台管理员",
        "status": "enabled",
        "tenant_id": tenant,
    }
    users = SimpleNamespace(find_by_id=AsyncMock(return_value=subject))
    app = create_app()
    pop_test_object_access(app)
    app.dependency_overrides[object_access._get_object_access_repository] = lambda: access
    app.dependency_overrides[routes.require_access_scope] = lambda: scope
    app.dependency_overrides[routes._get_redaction_repository] = lambda: repo
    app.dependency_overrides[routes._get_task_insight_service] = lambda: reader
    app.dependency_overrides[routes._get_download_audit_repository] = lambda: audit
    app.dependency_overrides[routes._get_ticket_subject_repository] = lambda: users
    return SimpleNamespace(
        api=TestClient(app),
        repo=repo,
        reader=reader,
        access=access,
        audit=audit,
        users=users,
        source=source,
        plan=plan,
        plans=plans,
        content=content,
        spec=spec,
    )


def request_body(ctx, **overrides):
    return {
        "source_checksum": ctx.source["original_checksum"],
        "manifest_id": ctx.source["manifest_id"],
        "manifest_version": ctx.source["manifest_version"],
        "spec": ctx.spec,
        "review_confirmed": True,
        **overrides,
    }


def call(ctx, operation, **overrides):
    prefix = f"/api/v2/evidence/{ARTIFACT}/redactions"
    if operation == "read":
        return ctx.api.get(f"/api/v2/evidence-redactions/{PLAN}/file")
    if operation == "list":
        return ctx.api.get(prefix)
    return ctx.api.post(
        prefix + ("/preview" if operation == "preview" else ""),
        json=request_body(ctx, **overrides),
    )


def event(ctx):
    return ctx.audit.record_redaction.call_args.kwargs


@pytest.mark.parametrize("operation", ["preview", "create", "read"])
def test_public_operation_preserves_business_text_and_removes_selected_secret(operation):
    ctx = context()
    response = call(ctx, operation)
    assert response.status_code == 200, response.text
    if operation == "preview":
        content = base64.b64decode(response.json()["content_base64"])
    elif operation == "create":
        created = response.json()["redaction_id"]
        read = ctx.api.get(f"/api/v2/evidence-redactions/{created}/file")
        assert read.status_code == 200
        content = read.content
    else:
        content = response.content
    assert SECRET.encode() not in content
    assert "订单已支付".encode() in content and b"42.50" in content and "✅".encode() in content
    assert "[已脱敏]".encode() in content
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-evidence-redaction-request-id"]
    assert ctx.reader.evidence_file.return_value["content"] == ctx.content
    assert all(
        item.kwargs == {"original": True} for item in ctx.reader.evidence_file.await_args_list
    )
    assert all(item.args == (ARTIFACT,) for item in ctx.reader.evidence_file.await_args_list)
    assert event(ctx)["result"] == "allowed"


@pytest.mark.parametrize("operation", ["preview", "read"])
def test_public_json_derivative_preserves_business_values_and_typed_facts(operation):
    content = b'{"phone":"13800138000","amount":42.5000,"paid":true,"status":"completed"}'
    ctx = context(
        media="application/json",
        content=content,
        spec={"method": "json_pointers", "pointers": ["/phone"]},
    )
    response = call(ctx, operation)
    assert response.status_code == 200
    derived = (
        base64.b64decode(response.json()["content_base64"])
        if operation == "preview"
        else response.content
    )
    assert SECRET.encode() not in derived and b"42.5000" in derived
    assert json_value(derived) == {
        "phone": "[已脱敏]",
        "amount": 42.5,
        "paid": True,
        "status": "completed",
    }
    assert ctx.content == content


def json_value(content):
    import json

    return json.loads(content)


@pytest.mark.parametrize("operation", ["read", "list"])
def test_redacted_only_reader_can_use_frozen_material_and_safe_summaries(operation):
    ctx = context(permissions={"evidenceExports:redacted"})
    ctx.users.find_by_id.return_value["role"] = "审计员"
    response = call(ctx, operation)
    assert response.status_code == 200, response.text
    assert event(ctx)["result"] == "allowed"
    if operation == "list":
        assert response.json()["can_manage"] is False
        item = response.json()["items"][0]
        assert "spec" not in item and "tenant_id" not in item["source"]
        assert "storage_path" not in response.text and SECRET not in response.text
        ctx.reader.evidence_file.assert_not_awaited()
    else:
        assert "nosniff" == response.headers["x-content-type-options"]
        assert "application/json" not in response.headers["content-type"]


@pytest.mark.parametrize("operation", ["preview", "create"])
@pytest.mark.parametrize("missing", list(MANAGE))
def test_every_management_permission_is_independently_required(operation, missing):
    ctx = context(permissions=MANAGE - {missing})
    response = call(ctx, operation)
    assert response.status_code == 403
    ctx.reader.evidence_file.assert_not_awaited()
    ctx.repo.source.assert_not_awaited()
    ctx.repo.save.assert_not_awaited()
    assert event(ctx)["result"] == "denied"
    assert event(ctx)["error_code"] == "REDACTION_PERMISSION_DENIED"


@pytest.mark.parametrize("operation", ["preview", "create", "read", "list"])
def test_no_redacted_permission_blocks_even_an_accessible_artifact(operation):
    ctx = context(permissions=set())
    response = call(ctx, operation)
    assert response.status_code == 403
    assert event(ctx)["result"] == "denied"
    ctx.reader.evidence_file.assert_not_awaited()


@pytest.mark.parametrize("operation", ["preview", "create", "read", "list"])
def test_cross_tenant_object_is_hidden_without_loading_source_bytes(operation):
    ctx = context(tenant="other-tenant")
    response = call(ctx, operation)
    assert response.status_code == 404
    assert event(ctx)["result"] == "denied"
    assert event(ctx)["error_code"] == "REDACTION_ACCESS_DENIED"
    ctx.reader.evidence_file.assert_not_awaited()


@pytest.mark.parametrize("operation", ["preview", "create", "read", "list"])
def test_late_object_revocation_withholds_all_prepared_payload(operation):
    ctx = context()
    ctx.access.can_access_artifact.side_effect = [True, False]
    response = call(ctx, operation)
    assert response.status_code == 404
    assert "content_base64" not in response.text and "42.50" not in response.text
    assert event(ctx)["result"] == "denied"
    assert event(ctx)["error_code"] == "REDACTION_ACCESS_DENIED"
    ctx.repo.save.assert_not_awaited()


@pytest.mark.parametrize(
    "subject",
    [None, {"status": "disabled"}, {"tenant_id": "other-tenant"}, {"tenant_status": "disabled"}],
)
@pytest.mark.parametrize("operation", ["preview", "create", "read", "list"])
def test_late_subject_revocation_does_not_release_material_or_summary(subject, operation):
    ctx = context()
    if subject is None:
        ctx.users.find_by_id.return_value = None
    else:
        ctx.users.find_by_id.return_value.update(subject)
    response = call(ctx, operation)
    assert response.status_code == 404
    assert event(ctx)["result"] == "denied"
    assert event(ctx)["error_code"] == "REDACTION_SUBJECT_REVOKED"
    ctx.repo.save.assert_not_awaited()
    assert "content_base64" not in response.text and "42.50" not in response.text


@pytest.mark.parametrize("operation", ["preview", "create"])
def test_late_role_downgrade_does_not_use_initial_management_permissions(operation):
    ctx = context()
    ctx.users.find_by_id.return_value["role"] = "审计员"
    response = call(ctx, operation)
    assert response.status_code == 403
    assert event(ctx)["result"] == "denied"
    assert event(ctx)["error_code"] == "REDACTION_PERMISSION_DENIED"
    ctx.repo.save.assert_not_awaited()


def test_create_requires_explicit_review_confirmation_but_preview_does_not():
    ctx = context()
    response = call(ctx, "create", review_confirmed=False)
    assert response.status_code == 422
    assert event(ctx)["error_code"] == "REDACTION_REVIEW_REQUIRED"
    assert event(ctx)["result"] == "failed"
    ctx.reader.evidence_file.assert_not_awaited()
    ctx.repo.save.assert_not_awaited()
    assert call(ctx, "preview", review_confirmed=False).status_code == 200


@pytest.mark.parametrize(
    "fault",
    [
        None,
        {"content": b"broken"},
        FileNotFoundError("private/storage"),
        ValueError("private/hash"),
    ],
)
@pytest.mark.parametrize("operation", ["preview", "create", "read"])
def test_original_corruption_has_fixed_failure_audit_and_no_partial_derivative(fault, operation):
    ctx = context()
    if isinstance(fault, Exception):
        ctx.reader.evidence_file.side_effect = fault
    else:
        ctx.reader.evidence_file.return_value = fault
    response = call(ctx, operation)
    assert response.status_code == 409
    assert event(ctx)["result"] == "failed"
    assert event(ctx)["error_code"] == "REDACTION_SOURCE_INVALID"
    assert event(ctx)["task_id"] == TASK
    assert event(ctx)["source_hash"] == ctx.source["source_hash"]
    assert event(ctx)["original_checksum"] == digest(ctx.content)
    assert "private/" not in response.text and "content_base64" not in response.text
    ctx.repo.save.assert_not_awaited()


@pytest.mark.parametrize("operation", ["preview", "create", "read", "list"])
def test_source_drift_after_material_preparation_blocks_release(operation):
    ctx = context()
    changed = {**ctx.source, "manifest_hash": "b" * 64}
    ctx.repo.source.side_effect = [deepcopy(ctx.source), changed]
    response = call(ctx, operation)
    assert response.status_code == 409, response.text
    assert event(ctx)["result"] == "failed"
    assert event(ctx)["error_code"] == "REDACTION_SOURCE_CHANGED"
    ctx.repo.save.assert_not_awaited()


def test_client_stale_checksum_is_rejected_before_reading_original():
    ctx = context()
    response = call(ctx, "create", source_checksum="sha256:" + "0" * 64)
    assert response.status_code == 409
    assert event(ctx)["error_code"] == "REDACTION_SOURCE_CHANGED"
    ctx.reader.evidence_file.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_hash", "0" * 64),
        ("policy_version", "unregistered-policy"),
        ("business_reviewable", False),
        ("schema_version", "unregistered-schema"),
    ],
)
def test_tampered_plan_contract_cannot_release_a_blob(field, value):
    ctx = context()
    ctx.plans[PLAN][field] = value
    response = call(ctx, "read")
    assert response.status_code == 409
    assert event(ctx)["error_code"] == "REDACTION_PLAN_INVALID"
    assert "content_base64" not in response.text and "42.50" not in response.text
    ctx.reader.evidence_file.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("derived_checksum", "sha256:" + "0" * 64),
        ("derived_size_bytes", 7),
        ("derived_content_type", "application/octet-stream"),
    ],
)
def test_recomputed_plan_hash_does_not_bypass_frozen_derivative_identity(field, value):
    ctx = context()
    ctx.plans[PLAN][field] = value
    ctx.plans[PLAN]["plan_hash"] = plan_digest(ctx.plans[PLAN])
    response = call(ctx, "read")
    assert response.status_code == 409
    assert event(ctx)["result"] == "failed"
    assert event(ctx)["error_code"] == "REDACTION_DERIVATIVE_CHANGED"


@pytest.mark.parametrize("operation", ["preview", "create", "read", "list"])
def test_audit_failure_never_releases_material_or_summary(operation):
    ctx = context()
    ctx.audit.record_redaction.side_effect = RuntimeError("private/audit/path")
    initial_plans = deepcopy(ctx.plans)
    response = call(ctx, operation)
    assert response.status_code == 503
    assert "content_base64" not in response.text and "42.50" not in response.text
    assert "private/audit/path" not in response.text
    assert ctx.plans == initial_plans


def test_missing_frozen_plan_is_audited_without_disclosing_original_identity():
    ctx = context()
    ctx.plans.clear()
    response = call(ctx, "read")
    assert response.status_code == 404
    assert event(ctx)["result"] == "denied"
    assert event(ctx)["error_code"] == "REDACTION_NOT_FOUND"
    assert event(ctx)["artifact_id"] == "redaction:not-found"
    assert event(ctx)["redaction_id"] == PLAN
    assert event(ctx)["task_id"] is None and event(ctx)["original_checksum"] is None
    assert event(ctx)["source_hash"] is None
    assert event(ctx)["request_id"] == response.json()["error"]["error_id"]
    ctx.reader.evidence_file.assert_not_awaited()


def test_frozen_create_has_safe_summary_and_one_matching_creation_audit():
    ctx = context()
    response = call(ctx, "create")
    assert response.status_code == 200
    summary = response.json()
    assert "spec" not in summary and "content_base64" not in summary
    assert "storage_path" not in response.text and SECRET not in response.text
    ctx.repo.save.assert_awaited_once()
    ctx.audit.record_redaction.assert_awaited_once()
    saved = ctx.repo.save.call_args.kwargs["plan"]
    recorded = event(ctx)
    assert saved["plan_hash"] == plan_digest(saved)
    assert saved["source"] == ctx.source
    assert saved["spec"]["method"] == "text_ranges"
    assert recorded["redaction_plan_hash"] == saved["plan_hash"] == summary["plan_hash"]
    assert recorded["original_checksum"] == digest(ctx.content)
    assert recorded["derived_checksum"] == summary["derived_checksum"]
    assert recorded["request_id"] == response.headers["x-evidence-redaction-request-id"]
    assert recorded["result"] == "allowed" and recorded["operation"] == "create"


def test_list_source_projection_is_a_whitelist_even_if_stored_plan_has_extra_fields():
    ctx = context(permissions={"evidenceExports:redacted"})
    ctx.plans[PLAN]["source"].update(
        {
            "storage_path": "private/storage/path",
            "spec": {"secret": SECRET},
            "unregistered_original_text": "private business source",
        }
    )
    response = call(ctx, "list")
    assert response.status_code == 200
    assert "storage_path" not in response.text
    assert "private/storage/path" not in response.text
    assert "unregistered_original_text" not in response.text
    assert SECRET not in response.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"review_confirmed": "true"},
        {"manifest_version": True},
        {"manifest_version": 1.0},
        {"manifest_id": None},
        {"source_checksum": "sha256:not-valid"},
        {"storage_path": "private/storage/path"},
        {"tenant_id": "foreign-tenant"},
        {"spec": {"method": "text_ranges", "ranges": []}},
        {"spec": {"method": "reviewed_unchanged", "arbitrary_policy": "private-policy"}},
    ],
)
def test_client_cannot_add_authority_fields_or_bypass_strict_request_contract(overrides):
    ctx = context()
    response = call(ctx, "create", **overrides)
    assert response.status_code == 422
    assert event(ctx)["result"] == "failed"
    assert event(ctx)["error_code"] == "REDACTION_REQUEST_INVALID"
    assert "private/" not in response.text and "private-policy" not in response.text
    ctx.reader.evidence_file.assert_not_awaited()
    ctx.repo.save.assert_not_awaited()


@pytest.mark.parametrize(
    "fault,expected_code",
    [
        (ValueError("private/source/path"), "REDACTION_SOURCE_CHANGED"),
        (RuntimeError("postgres://private/source/path"), "REDACTION_UNAVAILABLE"),
    ],
)
def test_repository_failures_are_fixed_safe_and_audited(fault, expected_code):
    ctx = context()
    ctx.repo.source.side_effect = fault
    response = call(ctx, "preview")
    assert response.status_code == (409 if isinstance(fault, ValueError) else 503)
    assert event(ctx)["result"] == "failed" and event(ctx)["error_code"] == expected_code
    assert "private/source/path" not in response.text
    ctx.reader.evidence_file.assert_not_awaited()


def test_save_failure_is_failed_audit_and_does_not_return_an_unfrozen_plan():
    ctx = context()
    ctx.repo.save.side_effect = RuntimeError("private/save/path")
    response = call(ctx, "create")
    assert response.status_code == 503
    assert event(ctx)["result"] == "failed"
    assert event(ctx)["error_code"] == "REDACTION_UNAVAILABLE"
    assert "private/save/path" not in response.text and "derived_checksum" not in response.text
    assert len(ctx.plans) == 1


def test_reviewed_unchanged_is_an_explicit_privileged_freeze_with_a_traceable_identity():
    ctx = context(spec={"method": "reviewed_unchanged"})
    response = call(ctx, "create")
    assert response.status_code == 200
    created = response.json()["redaction_id"]
    material = ctx.api.get(f"/api/v2/evidence-redactions/{created}/file")
    assert material.status_code == 200 and material.content == ctx.content
    saved = ctx.repo.save.call_args.kwargs["plan"]
    assert saved["spec"]["method"] == "reviewed_unchanged"
    assert saved["derived_checksum"] == saved["source"]["original_checksum"]
    assert saved["plan_hash"] == plan_digest(saved)


@pytest.mark.parametrize(
    "missing",
    [
        "spec",
        "source",
        "artifact_id",
        "derived_checksum",
        "derived_size_bytes",
        "derived_content_type",
        "plan_hash",
        "schema_version",
        "policy_version",
        "business_reviewable",
    ],
)
def test_missing_frozen_plan_fields_have_stable_rejection_before_original_read(missing):
    ctx = context()
    del ctx.plans[PLAN][missing]
    response = call(ctx, "read")
    assert response.status_code == 409, response.text
    assert event(ctx)["result"] == "failed"
    assert event(ctx)["error_code"] == "REDACTION_PLAN_INVALID"
    ctx.reader.evidence_file.assert_not_awaited()


def test_malformed_frozen_spec_with_recomputed_hash_is_rejected_before_original_read():
    ctx = context()
    ctx.plans[PLAN]["spec"] = {"method": "text_ranges", "ranges": []}
    ctx.plans[PLAN]["plan_hash"] = plan_digest(ctx.plans[PLAN])
    response = call(ctx, "read")
    assert response.status_code == 409
    assert event(ctx)["error_code"] == "REDACTION_PLAN_INVALID"
    ctx.reader.evidence_file.assert_not_awaited()


def test_foreign_outer_artifact_cannot_relabel_a_frozen_plan():
    ctx = context()
    ctx.plans[PLAN]["artifact_id"] = "00000000-0000-0000-0000-000000009799"
    response = call(ctx, "read")
    assert response.status_code == 409
    assert event(ctx)["error_code"] == "REDACTION_PLAN_INVALID"
    ctx.reader.evidence_file.assert_not_awaited()


@pytest.mark.parametrize("operation", ["preview", "create"])
def test_renderer_failure_preserves_the_trusted_source_in_the_failure_audit(operation):
    ctx = context()
    response = call(
        ctx,
        operation,
        spec={
            "method": "text_ranges",
            "ranges": [{"start": 0, "end": len(TEXT)}],
        },
    )
    assert response.status_code == 409
    recorded = event(ctx)
    assert recorded["result"] == "failed" and recorded["error_code"] == "REDACTION_FULL_COVERAGE"
    assert recorded["task_id"] == TASK
    assert recorded["source_hash"] == ctx.source["source_hash"]
    assert recorded["original_checksum"] == digest(ctx.content)
    assert recorded["request_id"] == response.json()["error"]["error_id"]
    assert SECRET not in response.text and "content_base64" not in response.text
    ctx.repo.save.assert_not_awaited()
