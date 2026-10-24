"""持久身份只接受同 Run 冻结策略下的服务端分类事实。"""

import pytest

from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY


def row():
    identity = DEFAULT_WEB_URL_IDENTITY_POLICY.logical_id("https://example.test/#/detail/1")
    return dict(
        capture_id="one",
        url="https://example.test/#/detail/1",
        assessment_present=True,
        page_logical_id=identity,
        common_logical_id=identity,
        page_identity_version=DEFAULT_WEB_URL_IDENTITY_POLICY.version,
        common_identity_version=DEFAULT_WEB_URL_IDENTITY_POLICY.version,
    )


def test_verified_classification_and_both_capture_sides_must_agree():
    from services.api.app.infrastructure.db.web_capture_identity import (
        resolve_persisted_capture_identity,
    )

    valid = row()
    assert (
        resolve_persisted_capture_identity(valid, DEFAULT_WEB_URL_IDENTITY_POLICY)[
            "logical_page_id"
        ]
        == valid["page_logical_id"]
    )
    for update in (
        {"assessment_present": False},
        {"common_logical_id": "web-logical:" + "a" * 64},
        {"page_identity_version": "web-url-identity.v1"},
        {"url": "https://reader:secret@example.test/#/detail/1"},
    ):
        assert (
            resolve_persisted_capture_identity(
                dict(valid, **update), DEFAULT_WEB_URL_IDENTITY_POLICY
            )["logical_page_id"]
            is None
        )


def test_format_valid_untrusted_report_metadata_is_rejected():
    from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
    from services.api.tests.test_web_evidence_bundle import _valid_snapshot

    snapshot = _valid_snapshot()
    for capture in snapshot["captures"]:
        capture.pop("logical_page_identity_status", None)
    bundle = build_web_evidence_bundle(snapshot)
    assert bundle["status"] == "needs_review"
    assert "WEB_LOGICAL_ID_UNTRUSTED" in bundle["reason_codes"]


@pytest.mark.asyncio
async def test_final_web_publisher_rejects_missing_server_identity():
    from services.api.app.application.h5_live_inspection_publish import H5LiveInspectionTaskGate
    from services.api.tests.test_h5_live_inspection_publish import (
        _RUN_ID,
        _TASK_ID,
        _capture,
        _service,
    )

    class MissingReader:
        async def read_for_run(self, **kwargs):
            return {}

    service, _, _, publisher = _service(
        H5LiveInspectionTaskGate(
            target_platform="web", task_config={"execution_mode": "full_inspection"}
        ),
        [_capture(capture_id="one")],
    )
    service._web_identity_reader = MissingReader()
    assert await service.publish_pending(task_id=_TASK_ID, agent_run_id=_RUN_ID) == 0
    assert publisher.calls == []


def test_all_live_report_consumers_use_server_identity_not_redacted_url():
    from services.api.app.application.report_insight import _web_report_logical_pages
    from services.api.app.application.task_insight import _web_logical_page_nodes

    rows = [
        {
            "page_capture_id": str(index),
            "node_seq": index,
            "url": "https://example.test/",
            "page_type": "content_detail",
        }
        for index in (1, 2)
    ]
    identities = {
        row["page_capture_id"]: {
            "logical_page_identity_status": "verified",
            "logical_page_id": "web-logical:" + row["page_capture_id"] * 64,
            "canonical_url": "https://example.test/#/detail/" + row["page_capture_id"],
        }
        for row in rows
    }
    _, pages = _web_report_logical_pages(rows, [], trusted_identities=identities)
    nodes = _web_logical_page_nodes(rows, assessments=[], trusted_identities=identities)
    assert len(pages) == len(nodes) == 2
    assert pages[0]["canonical_url"] == pages[1]["canonical_url"]
    assert nodes[0]["logical_page_id"] != nodes[1]["logical_page_id"]
    assert all(item["page_role"] != "home" for item in pages)
    rows[0]["logical_page_id"] = "web-logical:" + "f" * 64
    missing = _web_logical_page_nodes(rows, assessments=[], trusted_identities={})
    assert all(not node.get("logical_page_id") for node in missing)


@pytest.mark.asyncio
async def test_report_keeps_untrusted_capture_in_explicit_diagnostic_collection():
    from services.api.tests.test_report_insight import FakeReportRepository, _service

    repository = FakeReportRepository()
    repository.context.update(
        target_platform="web", task_config={"workflow_version": "v2"}, status="running"
    )
    repository.h5_page_rows = [
        {
            "page_capture_id": "untrusted",
            "url": "https://example.test/#/detail/1",
            "capture_role": "after_action",
        }
    ]

    async def conflicting_identity(**kwargs):
        return {"untrusted": {"logical_page_identity_status": "WEB_LOGICAL_ID_CONFLICT"}}

    repository.read_web_capture_identities = conflicting_identity
    result = await _service(repository).report_insight("task-1")
    assert result["web_logical_pages"] == []
    assert result["web_identity_diagnostics"] == [
        {"capture_id": "untrusted", "reason_code": "WEB_LOGICAL_ID_CONFLICT"}
    ]
