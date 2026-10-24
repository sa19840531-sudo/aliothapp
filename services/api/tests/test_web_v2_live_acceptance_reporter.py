"""验证增量持久化、分层门槛和凭据隔离。"""

import importlib
import importlib.util
import json

import pytest


def module():
    name = "scripts.validation.web_v2_live_acceptance"
    try:
        found = importlib.util.find_spec(name)
    except ModuleNotFoundError:
        found = None
    assert found, "缺少分层验收报告器"
    return importlib.import_module(name)


def snapshot():
    return {
        "task_id": "task-1",
        "run_id": "run-1",
        "requested_url": "https://site.test/",
        "status": "completed",
        "session_closed": True,
        "report_finalized": True,
        "root_navigation": "succeeded",
        "candidate_filter_observed": True,
        "checkpoints": [
            {
                "logical_page_key": "root",
                "current_stage": "session_close",
                "stage_status": "succeeded",
                "row_version": 14,
                "attempt_count": 1,
            }
        ],
        "captures": [
            {
                "capture_id": "capture-1",
                "role": "bootstrap",
                "page_type": "home",
                "requested_url": "https://site.test/",
                "final_url": "https://site.test/",
                "confidence": 0.95,
                "artifacts": [
                    {"artifact_id": "dom-1", "artifact_type": "dom"},
                    {"artifact_id": "png-1", "artifact_type": "screenshot"},
                ],
            }
        ],
        "frontier": [{"menu_entry_id": "entry-1", "status": "budget_not_executed"}],
    }


def test_reporter_emits_only_changes_after_cursor_and_survives_restart(tmp_path):
    api = module()
    current = snapshot()
    reporter = api.AcceptanceReporter(lambda: current, tmp_path)
    first = reporter.collect_delta(None)
    assert first.event_ids
    reporter.write_delta(first)
    reporter.write_delta(first)
    restarted = api.AcceptanceReporter(lambda: current, tmp_path)
    assert restarted.collect_delta(first.next_cursor).event_ids == ()
    current["checkpoints"][0].update(row_version=15, attempt_count=2)
    second = restarted.collect_delta(first.next_cursor)
    assert len(second.events) == 1
    assert second.events[0]["kind"] == "checkpoint"
    assert set(first.event_ids).isdisjoint(second.event_ids)
    restarted.write_delta(second)
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert len(rows) == len(first.events) + 1
    assert (tmp_path / "report.md").read_text().count("dom-1") == 1


def test_minimum_witness_does_not_require_suite_diversity():
    api = module()
    current = snapshot()
    assert api.SiteMinimumWitnessPolicy().evaluate(current).complete
    current["checkpoints"][0]["stage_status"] = "retry_wait"
    result = api.SiteMinimumWitnessPolicy().evaluate(current)
    assert not result.complete
    assert "nonterminal_checkpoint" in result.missing
    matrix = api.SuiteCapabilityMatrix.from_sites(
        [
            {
                "capabilities": {
                    "detail_structure_clusters": ["a", "b", "c", "d"],
                    "list_structure_clusters": ["a", "b"],
                }
            },
            current,
        ]
    )
    assert matrix.detail_structure_clusters == 4
    assert not matrix.complete


@pytest.mark.parametrize(
    "field",
    ["checkpoints", "captures", "candidate_filter_observed", "report_finalized", "session_closed"],
)
def test_missing_evidence_cannot_pass_minimum(field):
    current = snapshot()
    current.pop(field)
    assert not module().SiteMinimumWitnessPolicy().evaluate(current).complete


def test_external_tls_failure_is_not_classifier_regression():
    api = module()
    item = api.classify_validation_failure("WEB_GOTO_TLS_FAILED", response_received=False)
    assert item.category == "external_transport"
    assert not item.blocks_code_acceptance
    assert api.classify_validation_failure(
        "WEB_CLASSIFICATION_FAILED", response_received=True
    ).blocks_code_acceptance


def test_report_files_strip_credentials_dom_and_free_text(tmp_path):
    api = module()
    current = snapshot()
    current["captures"][0].update(
        requested_url="https://alice:password-secret@site.test/?token=token-secret#cookie-secret",
        dom="<html>private-body</html>",
        title="private-body",
        password="password-secret",
        last_error_detail={"message": "password-secret"},
    )
    reporter = api.AcceptanceReporter(lambda: current, tmp_path, secrets=("password-secret",))
    reporter.write_delta(reporter.collect_delta(None))
    text = "".join(path.read_text() for path in tmp_path.iterdir())
    for value in ("alice", "password-secret", "token-secret", "cookie-secret", "private-body"):
        assert value not in text
    assert "capture-1" in text
    assert "0.95" in text


def test_dispatch_limits_and_extension_are_bounded():
    api = module()
    assert api.dispatch_capacity(1, active_count=1) == 0
    assert api.dispatch_capacity(8, active_count=1) == 2
    with pytest.raises(ValueError):
        api.dispatch_capacity(0, active_count=0)
    state = {"extension_count": 0, "deadline": 100}
    api.extend_observation(state, minutes=30)
    assert state["deadline"] == 1900
    with pytest.raises(ValueError):
        api.extend_observation(state, minutes=30)


def test_cli_requires_explicit_credentials_and_supports_task12_commands():
    api = module()
    for command in (
        "preflight",
        "start",
        "monitor",
        "close-complete",
        "extend-incomplete",
        "report",
    ):
        assert api.build_parser().parse_args([command]).command == command
    with pytest.raises(ValueError, match="凭据"):
        api.credentials(api.build_parser().parse_args(["start"]), environ={})


def test_live_acceptance_monitor_defaults_to_realtime_sampling():
    args = module().build_parser().parse_args(["monitor"])

    assert args.interval_seconds == 2


def test_live_acceptance_accepts_explicit_evidence_root(tmp_path):
    api = module()

    args = api.build_parser().parse_args(
        ["monitor", "--evidence-root", str(tmp_path / "shared-evidence")]
    )

    assert args.evidence_root == tmp_path / "shared-evidence"


def test_changed_capture_does_not_emit_existing_artifacts_again(tmp_path):
    current = snapshot()
    reporter = module().AcceptanceReporter(lambda: current, tmp_path)
    first = reporter.collect_delta()
    current["captures"][0]["confidence"] = 0.8
    second = reporter.collect_delta(first.next_cursor)
    assert "dom-1" not in json.dumps(second.events)
    assert "png-1" not in json.dumps(second.events)
    assert any(row.get("confidence") == 0.8 for row in second.events)


def test_root_failure_must_be_structured_external_failure():
    current = snapshot()
    current.update(root_navigation=None, root_failure=True, root_gap=True)
    assert not module().SiteMinimumWitnessPolicy().evaluate(current).complete


def test_database_projection_preserves_navigation_classification_and_empty_discovery():
    from scripts.validation import web_acceptance_source as source

    assert hasattr(source, "project_database_rows"), "缺少数据库事实映射"
    rows = {
        "run": {
            "status": "completed",
            "finalization_state": "sealed",
            "session_status": "closed",
            "payload": {"budget": {"menu_limit": 10}},
        },
        "checkpoints": [
            {
                "logical_page_key": "home",
                "current_stage": "session_close",
                "stage_status": "succeeded",
                "gap_codes": [],
                "cursor_payload": {},
            }
        ],
        "captures": [
            {
                "capture_id": "cap",
                "final_url": "https://site.test/final",
                "role": "bootstrap",
                "page_type": "video_detail",
                "confidence": 0.9,
                "assessment": {"structure_fingerprint": "cluster1"},
                "navigation": ["https://site.test/request", "https://site.test/final"],
            }
        ],
        "artifacts": [{"capture_id": "cap", "artifact_id": "png", "artifact_type": "screenshot"}],
        "actions": [],
        "frontier": [],
        "phases": [{"phase_key": "web_stage_href_discovery", "status": "completed"}],
    }
    state = source.project_database_rows(rows, task_id="task", run_id="run")
    assert state["candidate_filter_observed"] is True
    assert state["captures"][0]["requested_url"] == "https://site.test/request"
    assert state["captures"][0]["confidence"] == 0.9
    assert state["capabilities"]["detail_structure_clusters"] == ["cluster1"]


@pytest.mark.parametrize("execution_limit,consistent", [(10, True), (None, False), (17, False)])
def test_database_projection_exposes_execution_budget_drift(execution_limit, consistent):
    from scripts.validation import web_acceptance_source as source

    state = source.project_database_rows(
        {
            "run": {
                "status": "completed",
                "finalization_state": "sealed",
                "session_status": "closed",
                "payload": {"budget": {"menu_limit": 10}},
            },
            "budgets": [{"site_id": "example", "menu_limit": execution_limit}],
            "checkpoints": [],
            "captures": [],
            "artifacts": [],
            "actions": [],
            "frontier": [],
            "phases": [],
        },
        task_id="task",
        run_id="run",
    )
    assert state["policy_menu_limit"] == 10
    assert state["execution_menu_limit"] == execution_limit
    assert state["menu_limit_consistent"] is consistent
    assert any(row["error_code"] == "WEB_BUDGET_POLICY_MISMATCH" for row in state["failures"]) is (
        not consistent
    )


def test_live_source_reads_frontier_error_from_real_diagnostic_column():
    import inspect

    from scripts.validation import web_acceptance_source as source

    source_text = inspect.getsource(source.read_database_state)
    assert "run.status::text AS status" in source_text
    assert "discovery_manifest #>> '{candidate,url}' AS canonical_url" in source_text
    assert "diagnostic #>> '{error_code}' AS error_code" in source_text
    assert "SELECT menu_entry_id,status,canonical_url" not in source_text


def test_candidate_delta_keeps_formal_menu_and_detail_execution_facts(tmp_path) -> None:
    api = module()
    current = snapshot()
    current["candidates"] = [
        {
            "candidate_kind": "menu",
            "candidate_id": "web-nav-entry:random",
            "menu_entry_id": "discovery:random",
            "raw_href": "/topics/42?private=value",
            "raw_href_gap": None,
            "normalized_url": "https://candidate-layout.test/topics/42?private=value",
            "canonical_url": "https://candidate-layout.test/topics/42?private=value",
            "source_capture_id": "capture-source",
            "candidate_source_kind": "anchor",
            "structure_cluster_key": "cluster:ab12",
            "frontier_status": "pending",
            "execution_slot": 3,
            "budget_state": "allocated",
            "budget_reason": None,
            "executed": False,
            "action_status": None,
        },
        {
            "candidate_kind": "detail",
            "candidate_id": "web-candidate:random",
            "menu_entry_id": "discovery:random",
            "raw_href": None,
            "raw_href_gap": "WEB_DETAIL_RAW_HREF_NOT_PERSISTED",
            "normalized_url": "https://candidate-layout.test/article/7?private=value",
            "canonical_url": "https://candidate-layout.test/article/7?private=value",
            "source_capture_id": "capture-detail-source",
            "candidate_source_kind": None,
            "structure_cluster_key": None,
            "frontier_status": "detail_reserved",
            "execution_slot": None,
            "source_menu_execution_slot": 3,
            "budget_state": "not_applicable",
            "budget_reason": None,
            "executed": True,
            "action_status": "succeeded",
        },
    ]

    delta = api.AcceptanceReporter(lambda: current, tmp_path).collect_delta()
    candidates = [row for row in delta.events if row["kind"] == "candidate"]

    assert len(candidates) == 2
    assert candidates[0]["raw_href"] == "/topics/42"
    assert candidates[0]["normalized_url"] == "https://candidate-layout.test/topics/42"
    assert candidates[0]["source_capture_id"] == "capture-source"
    assert candidates[0]["frontier_status"] == "pending"
    assert candidates[0]["execution_slot"] == 3
    assert candidates[0]["executed"] is False
    assert candidates[1]["raw_href"] is None
    assert candidates[1]["raw_href_gap"] == "WEB_DETAIL_RAW_HREF_NOT_PERSISTED"
    assert candidates[1]["execution_slot"] is None
    assert candidates[1]["source_menu_execution_slot"] == 3
    assert candidates[1]["budget_state"] == "not_applicable"
    assert candidates[1]["action_status"] == "succeeded"


def test_live_source_reads_candidate_projection_only_from_formal_columns():
    import inspect

    from scripts.validation import web_acceptance_source as source

    source_text = inspect.getsource(source.read_database_state)
    assert "discovery_manifest #>> '{candidate,target_hint}' AS raw_href" in source_text
    assert "detail_intent.action_status" in source_text
    assert "NULL::integer AS execution_slot" in source_text
    assert "entry.execution_slot AS source_menu_execution_slot" in source_text
    assert "'not_applicable'::text AS budget_state" in source_text
    assert "WEB_DETAIL_RAW_HREF_NOT_PERSISTED" in source_text


def test_live_source_maps_relative_artifact_storage_to_local_evidence_path(tmp_path):
    api = module()
    root = tmp_path / "evidence"
    local = root / "task" / "capture" / "artifact.png"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"png")
    state = snapshot()
    artifact = state["captures"][0]["artifacts"][0]
    artifact["storage_path"] = "task/capture/artifact.png"

    api.attach_local_artifact_paths(state, root)

    assert artifact == {
        "artifact_id": "dom-1",
        "artifact_type": "dom",
        "local_path": str(local.resolve()),
    }


def test_live_source_rejects_artifact_storage_path_outside_evidence_root(tmp_path):
    api = module()
    state = snapshot()
    state["captures"][0]["artifacts"][0]["storage_path"] = "../outside.png"

    with pytest.raises(ValueError, match="越出"):
        api.attach_local_artifact_paths(state, tmp_path / "evidence")


@pytest.mark.asyncio
async def test_submission_uses_queue_and_persisted_idempotency(tmp_path):
    api = module()
    from scripts.validation.web_acceptance_source import submission_payload

    payload = submission_payload("https://site.test/")
    assert "exploration_target" not in payload["target"]
    manifest = {
        "worker_count": 1,
        "site_timeout_seconds": 1800,
        "sites": [
            {"requested_url": "https://one.test/", "idempotency_key": "one"},
            {"requested_url": "https://two.test/", "idempotency_key": "two"},
        ],
    }
    path = tmp_path / "state.json"

    class API:
        async def request(self, method, route, *, payload, idempotency_key):
            persisted = json.loads(path.read_text())
            assert persisted["sites"][0]["idempotency_key"] == "one"
            assert route == "/api/v2/exploration-tasks"
            return {"task_id": idempotency_key + "-task", "run_id": idempotency_key + "-run"}

    await api.submit_pending(API(), manifest, path)
    assert manifest["sites"][0]["task_id"] == "one-task"
    assert "task_id" not in manifest["sites"][1]
    manifest["sites"][0]["terminal"] = True
    await api.submit_pending(API(), manifest, path)
    assert manifest["sites"][1]["task_id"] == "two-task"


def test_worker_capacity_counts_only_fresh_web_roles():
    from datetime import UTC, datetime, timedelta

    from scripts.validation import web_acceptance_source as source

    assert hasattr(source, "available_web_workers"), "缺少 Web Worker 角色过滤"
    now = datetime.now(UTC)
    assert (
        source.available_web_workers(
            [
                {"roles": ["web_explorer"], "heartbeat_at": now},
                {"roles": ["main"], "heartbeat_at": now},
                {"roles": ["web_explorer"], "heartbeat_at": now - timedelta(minutes=5)},
            ],
            now=now,
        )
        == 1
    )


def test_close_complete_refuses_live_checkpoint_and_incomplete_suite_report():
    api = module()
    assert hasattr(api, "verify_close"), "缺少受控收口检查"
    current = snapshot()
    assert api.verify_close(current)
    current["checkpoints"][0]["stage_status"] = "running"
    assert not api.verify_close(current)


@pytest.mark.asyncio
async def test_artifact_verification_checks_ownership_and_missing_references():
    from scripts.validation import web_acceptance_source as source

    assert hasattr(source, "referenced_artifact_ids"), "缺少完整引用收集"
    current = snapshot()
    current["checkpoints"][0]["artifact_ids"] = ["missing-artifact"]
    assert source.referenced_artifact_ids(current) == {"dom-1", "png-1", "missing-artifact"}


@pytest.mark.asyncio
async def test_live_worker_reader_uses_real_queue_decoder(monkeypatch):
    from datetime import UTC, datetime

    from redis.asyncio import Redis

    from scripts.validation.web_acceptance_source import read_web_worker_count

    class Connection:
        async def scan_iter(self, **_kwargs):
            yield "alioth:agent-runs:worker-heartbeat:one"

        async def mget(self, _keys):
            return [
                json.dumps(
                    {"roles": ["web_explorer"], "heartbeat_at": datetime.now(UTC).isoformat()}
                )
            ]

        async def aclose(self):
            pass

    monkeypatch.setattr(Redis, "from_url", lambda *_args, **_kwargs: Connection())
    assert await read_web_worker_count("redis://unused") == 1


def test_frozen_policy_cannot_pass_after_disabled_discovery():
    from scripts.validation import web_acceptance_source as source
    from services.api.tests.test_web_run_policy_snapshot import policy_payload
    from services.api.tests.test_web_run_policy_snapshot import snapshot as frozen

    assert hasattr(source, "verify_frozen_policy"), "缺少当前冻结策略核验"
    body = policy_payload()
    row = frozen(body)
    assert source.verify_frozen_policy(body, row.payload_sha256, task_id="task", run_id="run") > 0
    body["discovery_policy"]["enabled"] = False
    with pytest.raises(ValueError):
        source.verify_frozen_policy(body, row.payload_sha256, task_id="task", run_id="run")


def test_live_reader_audits_prior_frozen_catalog_after_current_version_advances():
    from scripts.validation import web_acceptance_source as source
    from services.api.tests.test_web_run_policy_snapshot import policy_payload
    from services.api.tests.test_web_run_policy_snapshot import snapshot as frozen

    body = policy_payload()
    body["page_catalog"]["version"] = "2026-09-29.web.v14"
    body["guide_version"] = "2026-09-29.web.v14"
    row = frozen(body)

    assert source.verify_frozen_policy(
        body, row.payload_sha256, task_id="task", run_id="run"
    ) == body["budget"]["menu_limit"]

    body["budget"]["menu_limit"] += 1
    with pytest.raises(ValueError):
        source.verify_frozen_policy(
            body, row.payload_sha256, task_id="task", run_id="run"
        )


def test_live_reader_records_terminal_failure_before_policy_freeze():
    from scripts.validation import web_acceptance_source as source

    assert (
        source.verify_available_frozen_policy(
            None, None, run_status="running", task_id="task", run_id="run"
        )
        is None
    )
    assert (
        source.verify_available_frozen_policy(
            None, None, run_status="pending", task_id="task", run_id="run"
        )
        is None
    )
    assert (
        source.verify_available_frozen_policy(
            None, None, run_status="failed", task_id="task", run_id="run"
        )
        is None
    )
    state = source.project_database_rows(
        {
            "run": {
                "status": "failed", "finalization_state": "open",
                "session_status": None, "payload": None,
            },
            "budgets": [], "checkpoints": [], "captures": [], "artifacts": [],
            "actions": [], "frontier": [], "phases": [],
        },
        task_id="task", run_id="run",
    )
    assert any(
        row["error_code"] == "VALIDATION_POLICY_MISSING"
        for row in state["failures"]
    )
    with pytest.raises(ValueError, match="VALIDATION_POLICY_MISSING"):
        source.verify_available_frozen_policy(
            None, None, run_status="done", task_id="task", run_id="run"
        )


def test_collect_site_defers_menu_limit_check_during_policy_freeze_race():
    import inspect

    api = module()
    source_text = inspect.getsource(api.collect_site)

    assert 'state["menu_limit"] is not None' in source_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,expected",
    [("monitor", 0), ("close-complete", 0), ("extend-incomplete", 0), ("report", 2)],
)
async def test_cli_commands_use_durable_state_and_do_not_cancel_runs(
    tmp_path, monkeypatch, command, expected
):
    api = module()
    monkeypatch.setenv("ALIOTH_LIVE_TOKEN", "private-token")
    monkeypatch.setenv("ALIOTH_LIVE_DATABASE_URL", "private-dsn")
    monkeypatch.setenv("ALIOTH_LIVE_REDIS_URL", "private-redis")
    current = snapshot()
    current["menu_limit"] = 10

    class Client:
        token = "private-token"

        def __init__(self, *_args, **_kwargs):
            pass

        async def login(self):
            pass

        async def close(self):
            pass

        async def request(self, *args, **kwargs):
            raise AssertionError("读取和观察命令不得取消或修改 Run")

    async def read(*_args):
        return current

    async def workers(*_args):
        return 1

    monkeypatch.setattr(api, "AcceptanceClient", Client)
    monkeypatch.setattr(api, "read_database_state", read)
    monkeypatch.setattr(api, "read_web_worker_count", workers)
    state = {
        "worker_count": 1,
        "site_timeout_seconds": 1800,
        "sites": [
            {
                "task_id": "task-1",
                "run_id": "run-1",
                "requested_url": "https://site.test/",
                "directory": "site",
                "deadline": 9999999999,
                "extension_count": 0,
            }
        ],
    }
    api.write_json(tmp_path / "validation-state.json", state)
    assert (
        await api.run(api.build_parser().parse_args([command, "--output", str(tmp_path)]))
        == expected
    )
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "private-" not in path.read_text(encoding="utf-8")
    if command == "report":
        assert (tmp_path / "final-report.md").is_file()
    if command == "extend-incomplete":
        updated = json.loads((tmp_path / "validation-state.json").read_text())
        assert updated["sites"][0]["extension_count"] == 1


def test_missing_local_evidence_reference_fails_verification(tmp_path):
    current = snapshot()
    current["captures"][0]["artifacts"][0]["local_path"] = str(tmp_path / "missing.html")
    assert not module().verify_artifacts(current)


def test_home_checkpoint_reference_proves_home_for_observation_capture():
    current = snapshot()
    current["captures"][0]["role"] = "observation"
    current["checkpoints"][0].update(
        logical_page_key="home", cursor_payload={"capture_id": "capture-1"}
    )
    assert module().SiteMinimumWitnessPolicy().evaluate(current).complete


@pytest.mark.asyncio
async def test_preflight_reads_migration_and_worker_facts_without_claiming_deployed_policy(
    tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    import asyncpg

    api = module()

    class Connection:
        @asynccontextmanager
        async def transaction(self, *, readonly):
            assert readonly
            yield

        async def fetchval(self, sql):
            return "0115" if "version_num" in sql else True

        async def close(self, **_kwargs):
            pass

    async def connect(*_args, **_kwargs):
        return Connection()

    async def workers(*_args):
        return 1

    class Client:
        async def request(self, method, path):
            assert method == "GET" and path == "/api/v1/readyz"
            return {"status": "ok"}

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(api, "read_web_worker_count", workers)
    args = api.build_parser().parse_args(["preflight", "--output", str(tmp_path)])
    result = await api.preflight(args, Client(), "unused")
    assert result["migration_version"] == "0115"
    assert result["concurrency"] == 1
    assert result["frozen_policy_verification"] == "on_run_collection"
    assert result["two_origin"] == "not_checked"
