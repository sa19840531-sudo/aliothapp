"""复用分类不能抹掉真实 fresh 观察，也不能要求它再次分类。"""

from copy import deepcopy

import pytest

from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.tests.test_web_capture_evidence_repository import Pool
from services.api.tests.test_web_evidence_bundle import _append_action_pair, _valid_snapshot


def reused_snapshot(scenario):
    snapshot = _valid_snapshot()
    previous = "menu-ready:after"
    new_list = scenario not in {"menu", "menu_list"}
    for action_id, page_type, sequence in (
        ("fresh-menu", "content_list" if new_list else "", 5),
        ("fresh-ready", "content_list" if new_list else "", 6),
    ):
        previous = _append_action_pair(
            snapshot,
            action_id=action_id,
            intent_id=action_id + "-intent",
            requested_id=previous,
            page_type=page_type,
            sequence=sequence,
            result_url="https://example.test/list" if new_list else "https://example.test/watch",
        )
    row = dict(
        menu_entry_id="reused",
        execution_slot=2,
        status="logical_target_reused",
        menu_action_count=1,
        menu_intent_id="fresh-menu-intent",
        menu_source_capture_id="menu-ready:after",
        menu_action_execution_id="fresh-menu",
        menu_probe_capture_id="fresh-menu:after",
        menu_ready_capture_id="fresh-ready:after",
        menu_page_type="content_list" if new_list else "content_detail",
        detail_action_count=0,
        detail_page_type="content_detail" if new_list else None,
        reused_capture_id="menu-ready:after",
        reused_logical_page_id="web-logical:" + "d" * 64,
        reused_target_stage={
            "menu": "menu",
            "menu_list": "menu",
            "detail_before": "detail_before_action",
            "detail_after": "detail_after_action",
        }[scenario],
    )
    if scenario == "menu_list":
        snapshot["ledger"][1].update(
            status="menu_page_captured",
            menu_page_type="content_list",
        )
        for capture in snapshot["captures"]:
            if str(capture["capture_id"]).startswith("menu-"):
                capture["page_type"] = "content_list"
        row.update(
            menu_page_type="content_list",
            reused_capture_id="menu-ready:after",
            reused_logical_page_id="web-logical:" + "d" * 64,
        )
    if scenario == "detail_after":
        row.update(
            detail_action_count=1,
            detail_intent_id="fresh-detail-intent",
            detail_source_capture_id=previous,
            detail_action_execution_id="fresh-detail",
            detail_probe_capture_id="fresh-detail:after",
            detail_ready_capture_id="fresh-detail-ready:after",
        )
        for action_id, sequence in (("fresh-detail", 7), ("fresh-detail-ready", 8)):
            previous = _append_action_pair(
                snapshot,
                action_id=action_id,
                intent_id=action_id + "-intent",
                requested_id=previous,
                page_type="",
                sequence=sequence,
            )
    # fresh 重复观察没有分类和逻辑身份；只能使用独立 reused 引用。
    for capture in snapshot["captures"]:
        if capture["capture_id"].startswith("fresh-") and (
            not new_list or capture["capture_id"].startswith("fresh-detail")
        ):
            capture["page_type"] = None
            capture["logical_page_id"] = None
            capture["logical_page_identity_status"] = "WEB_LOGICAL_ID_UNTRUSTED"
    snapshot["ledger"].append(row)
    return snapshot


async def read_bundle_snapshot(snapshot, monkeypatch):
    class Connection:
        async def fetchrow(self, query, *args):
            return {
                key: snapshot[key]
                for key in (
                    "task_id",
                    "agent_run_id",
                    "session_id",
                    "site_id",
                    "menu_limit",
                    "budget_state",
                    "finalization_epoch",
                    "normalization_version",
                )
            }

        async def fetch(self, query, *args):
            for table, key in (
                ("qc_capture AS common_capture", "captures"),
                ("qc_web_menu_ledger_entry", "ledger"),
                ("qc_action_execution", "actions"),
                ("qc_evidence_artifact", "artifacts"),
            ):
                if "FROM " + table in query:
                    return deepcopy(snapshot[key])
            raise AssertionError(query)

    async def identities(connection, **kwargs):
        # 此边界模拟真实仓储认证输出；PG 测试覆盖真实分类关联，未认证 fresh 保持未认证。
        return {
            row["capture_id"]: {
                "logical_page_id": row["logical_page_id"],
                "logical_page_identity_status": row["logical_page_identity_status"],
            }
            for row in snapshot["captures"]
        }

    monkeypatch.setattr(
        "services.api.app.infrastructure.db.web_capture_identity.read_web_capture_identities",
        identities,
    )
    return await PostgresWebCaptureEvidenceRepository(pool=Pool(Connection())).list_for_task(
        task_id=snapshot["task_id"],
        agent_run_id=snapshot["agent_run_id"],
        access_scope=AccessScope(
            actor_user_id="reader", tenant_id="tenant", permissions=frozenset()
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("normalization_version", "home_page_type"),
    [
        ("web-page-type-assessment.v2", "content_list"),
        ("web-page-type-assessment.v3", "home"),
    ],
)
async def test_reader_preserves_exact_home_normalization_version(
    normalization_version,
    home_page_type,
    monkeypatch,
):
    snapshot = reused_snapshot("menu")
    snapshot["normalization_version"] = normalization_version
    snapshot["ledger"][0]["menu_page_type"] = home_page_type
    for capture in snapshot["captures"]:
        if str(capture["capture_id"]).startswith("home-"):
            capture["page_type"] = home_page_type

    decoded = await read_bundle_snapshot(snapshot, monkeypatch)

    assert decoded["normalization_version"] == normalization_version
    assert build_web_evidence_bundle(decoded)["status"] == "evidence_ready"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario, page_count, detail_count",
    [
        ("menu", 2, 0),
        ("menu_list", 2, 0),
        ("detail_before", 3, 0),
        ("detail_after", 3, 1),
    ],
)
async def test_reader_build_preserves_reused_observations_without_fresh_classification(
    scenario,
    page_count,
    detail_count,
    monkeypatch,
):
    snapshot = reused_snapshot(scenario)
    classifications = sum(bool(row["page_type"]) for row in snapshot["captures"])
    decoded = await read_bundle_snapshot(snapshot, monkeypatch)
    result = build_web_evidence_bundle(decoded)
    assert result["status"] == "evidence_ready", result["reason_codes"]
    assert len(result["formal_pages"]) == page_count
    assert {row["capture_id"] for row in result["captures"]} == {
        row["capture_id"] for row in snapshot["captures"]
    }
    assert result["entries"][-1]["menu_action_count"] == 1
    assert result["entries"][-1]["detail_action_count"] == detail_count
    assert sum(bool(row["page_type"]) for row in decoded["captures"]) == classifications
    assert all(
        row["page_type"] is None
        for row in decoded["captures"]
        if row["logical_page_identity_status"] == "WEB_LOGICAL_ID_UNTRUSTED"
    )
    if scenario.startswith("detail"):
        assert "fresh-ready:after" in {row["primary_capture_id"] for row in result["formal_pages"]}


@pytest.mark.parametrize("field", ["web_evidence_admission", "page_parent_capture_id"])
def test_reused_fresh_observation_cannot_bypass_admission_or_parent_chain(field):
    snapshot = reused_snapshot("menu")
    fresh = next(row for row in snapshot["captures"] if row["capture_id"] == "fresh-ready:after")
    fresh[field] = {} if field == "web_evidence_admission" else "wrong-parent"
    result = build_web_evidence_bundle(snapshot)
    assert result["status"] == "needs_review"
    assert "evidence_incomplete" in result["reason_codes"]


@pytest.mark.parametrize("stage", [None, "unknown"])
def test_legacy_or_unknown_reuse_stage_preserves_observations_for_review(stage):
    snapshot = reused_snapshot("detail_before")
    snapshot["ledger"][-1]["reused_target_stage"] = stage
    result = build_web_evidence_bundle(snapshot)
    assert result["status"] == "needs_review"
    assert "logical_target_reuse_invalid" in result["reason_codes"]
    assert "fresh-ready:after" in {row["capture_id"] for row in result["captures"]}


def test_terminal_record_retains_detail_before_reuse_stage_without_fake_action():
    from services.api.app.application.web_menu_exploration import (
        WebMenuVisitResult,
        _terminal_record,
    )
    from services.api.app.domain.exploration import WebMenuTerminalStatus
    from services.api.app.domain.page import WebSiteMenuRegistry
    from services.api.tests.test_web_menu_exploration import _command

    site = WebSiteMenuRegistry().resolve_site("http://91porna.com/")
    record = _terminal_record(
        command=_command(),
        site_id=site.site_id,
        entry=site.entries[1],
        result=WebMenuVisitResult(
            final_capture_id="fresh-list",
            page_type="content_list",
            menu_page_type="content_list",
            detail_page_type="content_detail",
            status=WebMenuTerminalStatus.LOGICAL_TARGET_REUSED,
            reused_capture_id="original",
            reused_logical_page_id="web-logical:" + "d" * 64,
        ),
    )
    assert record.detail_page_type is None
    assert record.detail_action_execution_id is None
    assert record.diagnostic["reused_target_stage"] == "detail_before_action"
