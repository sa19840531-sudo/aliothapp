"""规划序必须落到持久账本，不能把同链接出现次数当作全局顺序。"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.tests.test_web_runtime_stage_effects import CAPTURE, command
from services.api.tests.test_web_runtime_stage_frontier import candidate


class RecordingConnection:
    """只替换数据库传输；记录真实仓储生成的 INSERT 参数。"""

    def __init__(self):
        self.inserted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def transaction(self):
        return self

    def acquire(self):
        return self

    async def fetchrow(self, _query, *_args):
        return {"state": "open"}

    async def execute(self, query, *args):
        assert "INSERT INTO qc_web_menu_ledger_entry" in query
        self.inserted.append({
            "position": args[3],
            "manifest": json.loads(args[8]),
            "query": query,
        })


async def _discover(candidates):
    connection = RecordingConnection()
    ledger = PostgresWebExplorationBudgetLedgerRepository(pool=connection)
    site = SimpleNamespace(site_id="runtime", origin="https://example.test/", entries=("home",))

    class Source:
        def bind_latest_navigation_capture(self, session_id, capture_id):
            assert capture_id == CAPTURE

        def navigation_candidates(self, session_id):
            return candidates

    frontier = WebRuntimeStageFrontier(
        ledger=ledger, candidate_source=Source(), navigator=None, site=site,
    )
    await frontier.discover(command(), source_capture_id=CAPTURE, page_role="home")
    return connection, ledger, site


@pytest.mark.parametrize("positions", [(1, 1, 1), (3, 1, 2)])
async def test_ranked_frontier_order_survives_sql_projection_without_rebinding_identity(positions):
    planned = tuple(
        replace(candidate(label), menu_position=position)
        for label, position in zip(("c", "a", "b"), positions, strict=True)
    )
    originals = tuple(item.to_dict() for item in planned)

    connection, _, _ = await _discover(planned)

    persisted = connection.inserted
    assert [item["position"] for item in persisted] == [1, 2, 3]
    assert [item["manifest"]["candidate"]["url"] for item in persisted] == [
        item.url for item in planned
    ]
    assert [item["manifest"]["discovery_rank"] for item in persisted] == [0, 1, 2]
    assert tuple(item["manifest"]["candidate"] for item in persisted) == originals
    assert tuple(item.to_dict() for item in planned) == originals


async def test_legacy_discovery_without_rank_keeps_original_position_contract():
    connection = RecordingConnection()
    ledger = PostgresWebExplorationBudgetLedgerRepository(pool=connection)
    site = SimpleNamespace(site_id="runtime", origin="https://example.test/", entries=("home",))
    await ledger.record_discovery(
        command=command(), site=site, candidate=replace(candidate("legacy"), menu_position=7),
        source_capture_id=CAPTURE, error_code="WEB_DISCOVERY_PENDING",
    )
    assert connection.inserted[0]["position"] == 8
    assert "discovery_rank" not in connection.inserted[0]["manifest"]


async def test_discovery_replay_sql_cannot_rewrite_frozen_rank_or_candidate_manifest():
    connection, _, _ = await _discover((candidate("c"),))
    update = connection.inserted[0]["query"].split("DO UPDATE SET", 1)[1].split("WHERE", 1)[0]
    assert "diagnostic=EXCLUDED.diagnostic" in update
    assert "catalog_position" not in update
    assert "discovery_manifest" not in update


@pytest.mark.parametrize("rank", [-1, True, 1.5, "1"])
async def test_discovery_rank_rejects_non_ordinal_without_database_write(rank):
    connection = RecordingConnection()
    ledger = PostgresWebExplorationBudgetLedgerRepository(pool=connection)
    site = SimpleNamespace(site_id="runtime", origin="https://example.test/", entries=("home",))
    with pytest.raises(ValueError, match="discovery_rank"):
        await ledger.record_discovery(
            command=command(), site=site, candidate=candidate("a"),
            source_capture_id=CAPTURE, error_code="WEB_DISCOVERY_PENDING", discovery_rank=rank,
        )
    assert connection.inserted == []


async def test_legacy_tied_positions_have_stable_url_and_identity_read_order():
    class ReadConnection(RecordingConnection):
        async def fetchrow(self, _query, *_args):
            return {
                "site_id": "runtime", "catalog_version": "v1", "catalog_sha256": "a" * 64,
                "normalization_version": "v1", "menu_limit": 5, "menu_total": 1,
            }

        async def fetch(self, query, *_args):
            self.read_sql = " ".join(query.split())
            return []

    connection = ReadConnection()
    ledger = PostgresWebExplorationBudgetLedgerRepository(pool=connection)
    await ledger.read_summary(agent_run_id=command().agent_run_id, site_id="runtime")
    order = connection.read_sql.rsplit("ORDER BY", 1)[1]
    assert order.index("catalog_position") < order.index("{candidate,url}")
    assert order.index("{candidate,url}") < order.index("menu_entry_id")
    assert 'COLLATE "C"' in order
    assert connection.inserted == []
