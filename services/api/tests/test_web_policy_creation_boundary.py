"""新运行 v8 九类/assessment v4 与历史五类 v3、四类 v2 冻结恢复的入口隔离。"""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.application import web_run_policy
from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
from services.api.app.domain.page.web_site_menu_registry import WebSiteMenuRegistry
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    PostgresWebRunPolicySnapshotRepository,
)

LEGACY_CATALOG = Path(__file__).with_name("fixtures") / "web_page_catalog_20260904_v3.json"


def _legacy_snapshot():
    body = web_run_policy.load_published_web_policy(
        now=datetime(2026, 9, 4, tzinfo=UTC), max_duration_seconds=600
    )
    body["page_catalog"] = json.loads(LEGACY_CATALOG.read_text(encoding="utf-8"))
    body["guide_version"] = body["page_catalog"]["version"]
    body["menu_priors"] = WebSiteMenuRegistry().to_payload()
    body["budget"]["menu_limit"] = 10
    for site in body["menu_priors"]["sites"]:
        site["applicability"]["required_page_types"].remove("home")
        site["applicability"]["optional_page_types"] = [
            page_type
            for page_type in site["applicability"]["optional_page_types"]
            if page_type not in {"activity", "search_page"}
        ]
        for entry in site["entries"]:
            if entry["decision"] == "home_baseline":
                entry["classification_hint"]["expected_page_type"] = "video_list"
    return WebRunPolicySnapshot.create(task_id="task-a", agent_run_id="run-a", payload=body)


def _legacy_five_type_snapshot() -> WebRunPolicySnapshot:
    body = web_run_policy.load_published_web_policy(
        now=datetime(2026, 9, 13, tzinfo=UTC), max_duration_seconds=600
    )
    catalog = deepcopy(body["page_catalog"])
    catalog["version"] = "2026-09-13.web.v7"
    catalog["page_types"] = catalog["page_types"][:5]
    for field in (
        "page_type_groups",
        "media_rules",
        "detail_classification_principles",
        "notes",
    ):
        catalog.pop(field)
    body["page_catalog"] = catalog
    body["guide_version"] = catalog["version"]
    return WebRunPolicySnapshot.create(
        task_id="task-five", agent_run_id="run-five", payload=body
    )


class _Connection:
    def __init__(self, stored=None):
        self.stored = stored
        self.inserts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def fetchrow(self, sql, *args):
        if "SELECT run.task_id" in sql:
            return {"task_id": args[1]}
        assert "FROM qc_web_run_policy_snapshot" in sql
        if self.stored is None:
            return None
        return {
            "task_id": self.stored.task_id,
            "agent_run_id": self.stored.agent_run_id,
            "payload": self.stored.to_payload(),
            "payload_sha256": self.stored.payload_sha256,
        }

    async def fetchval(self, sql, *args):
        assert "SELECT EXISTS" in sql
        return False

    async def execute(self, sql, *args):
        assert "INSERT INTO qc_web_run_policy_snapshot" in sql
        self.inserts += 1
        self.stored = WebRunPolicySnapshot(
            task_id=args[0],
            agent_run_id=args[1],
            payload=json.loads(args[2]),
            payload_sha256=args[3],
        )


def test_new_publication_rejects_real_legacy_catalog_and_four_type_menu(monkeypatch):
    monkeypatch.setattr(web_run_policy, "_PAGE_CATALOG_PATH", LEGACY_CATALOG)
    with pytest.raises(ValueError):
        web_run_policy.load_published_web_policy(
            now=datetime.now(UTC), max_duration_seconds=600
        )


def test_new_publication_does_not_consume_historical_four_type_menu():
    legacy = _legacy_snapshot()
    menu = WebSiteMenuRegistry(payload=legacy.to_payload()["menu_priors"], legacy_frozen=True)
    with pytest.raises(TypeError):
        web_run_policy.load_published_web_policy(
            now=datetime.now(UTC), max_duration_seconds=600, menu_registry=menu
        )


@pytest.mark.asyncio
async def test_first_freeze_rejects_real_legacy_body_before_any_insert():
    connection = _Connection()
    repository = PostgresWebRunPolicySnapshotRepository(pool=connection)
    with pytest.raises(ValueError):
        await repository.freeze_or_read(_legacy_snapshot())
    assert connection.inserts == 0


@pytest.mark.asyncio
async def test_first_freeze_accepts_current_body_as_v4():
    current_body = web_run_policy.load_published_web_policy(
        now=datetime.now(UTC), max_duration_seconds=600
    )
    candidate = WebRunPolicySnapshot.create(
        task_id="task-a", agent_run_id="run-a", payload=current_body
    )
    connection = _Connection()
    stored = await PostgresWebRunPolicySnapshotRepository(pool=connection).freeze_or_read(candidate)
    assert connection.inserts == 1
    assert stored == candidate
    assert web_run_policy.materialize_web_policy(stored).assessment_schema_version == (
        "web-page-type-assessment.v4"
    )
    assert web_run_policy.materialize_web_policy(stored).catalog.version == (
        current_body["guide_version"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "freeze_or_read"])
async def test_existing_five_type_freeze_recovers_as_v3(operation):
    legacy = _legacy_five_type_snapshot()
    connection = _Connection(stored=legacy)
    repository = PostgresWebRunPolicySnapshotRepository(pool=connection)
    if operation == "read":
        recovered = await repository.read(
            task_id=legacy.task_id, agent_run_id=legacy.agent_run_id
        )
    else:
        recovered = await repository.freeze_or_read(legacy)
    assert recovered.payload_sha256 == legacy.payload_sha256
    assert recovered.to_payload() == legacy.to_payload()
    assert LegacyWebRunAuditAdapter.load(recovered).assessment_schema_version == (
        "web-page-type-assessment.v3"
    )
    assert connection.inserts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "freeze_or_read"])
async def test_existing_legacy_freeze_recovers_exact_payload_hash_and_v2(operation):
    legacy = _legacy_snapshot()
    connection = _Connection(stored=legacy)
    repository = PostgresWebRunPolicySnapshotRepository(pool=connection)
    if operation == "read":
        recovered = await repository.read(task_id=legacy.task_id, agent_run_id=legacy.agent_run_id)
    else:
        recovered = await repository.freeze_or_read(legacy)
    assert recovered.payload_sha256 == legacy.payload_sha256
    assert recovered.to_payload() == legacy.to_payload()
    assert LegacyWebRunAuditAdapter.load(recovered).assessment_schema_version == (
        "web-page-type-assessment.v2"
    )
    assert connection.inserts == 0
