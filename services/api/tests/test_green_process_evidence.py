"""绿色只读原件边界的受控组件用例；连接和图片均为夹具，不代表真实采集。"""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from services.api.app.application.green_evidence_contracts import GreenEvidenceSet
from services.api.app.application.green_rule_catalog import build_green_rule_payload
from services.api.app.application.green_rule_evidence import (
    GreenSubitem,
    evaluate_green_aggregate_context,
    green_page_preflight,
    reduce_green_subitems,
)
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
    seal_evidence_input_set,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    SurfaceKind,
    build_action_intent_hash,
)
from services.api.app.infrastructure.db.green_process_evidence import load_green_process_evidence
from services.api.app.infrastructure.rule_judge_evidence import (
    TrustedEvidenceArtifact,
    TrustedEvidenceBundle,
)

NOW = datetime(2026, 10, 19, tzinfo=UTC)


class FixtureConnection:
    """只提供 SELECT 接口；任何写入都会因接口不存在而失败。"""

    def __init__(self, key="s35"):
        self.ids = {
            name: str(uuid4()) for name in ("task", "run", "rule", "before", "after", "origin")
        }
        self.captures, self.artifacts, self.payloads, self.manifests = {}, {}, {}, {}
        self.identity = {
            "finalization_epoch": 0,
            "owner_run": self.ids["run"],
            "target_platform": "app",
        }
        self.extra_captures, candidates = [], []
        for index, role in enumerate(("before_action", "after_action")):
            capture_id = self.ids["before" if index == 0 else "after"]
            artifact_id = str(uuid4())
            payload = json.dumps(
                {
                    "schema_version": "ui-snapshot.v2",
                    "business_success": True,
                    "all_windows_complete": True,
                    "green_pass": True,
                }
            ).encode()
            path = f"{self.ids['task']}/{capture_id}/{artifact_id}.json"
            checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
            manifest = build_evidence_manifest_reference(
                task_id=self.ids["task"],
                session_id="session-1",
                capture_id=capture_id,
                manifest_version=1,
                quality_status=EvidenceQualityStatus.PASS,
                created_at=NOW,
                artifacts=(
                    ArtifactReference(
                        artifact_id=artifact_id,
                        artifact_type="ui_snapshot",
                        role=role,
                        storage_path=path,
                        checksum=checksum,
                        size_bytes=len(payload),
                        content_type="application/json",
                    ),
                ),
            )
            self.manifests[capture_id] = manifest.to_dict()
            self.captures[capture_id] = {
                "id": capture_id,
                "task_id": self.ids["task"],
                "agent_run_id": self.ids["run"],
                "surface_session_id": "session-1",
                "capture_status": "captured",
                "capture_role": role,
                "metadata": {"finalization_epoch": 0},
                "action_execution_id": "action-1",
                "session_command_sequence": 2,
                "parent_capture_id": self.ids["origin"] if index == 0 else self.ids["before"],
                "captured_at": NOW + timedelta(milliseconds=index * 2),
                "manifest_payload": manifest.to_dict(),
            }
            self.artifacts[artifact_id] = {
                "artifact_type": "ui_snapshot",
                "source": "baseline",
                "lifecycle_status": "active",
                "storage_path": path,
                "checksum": checksum,
                "size_bytes": len(payload),
                "content_type": "application/json",
            }
            self.payloads[path] = payload
            candidates.append(
                EvidenceInputCandidate(
                    manifest=manifest, capture_role=role, page_sequence=index + 1
                )
            )
        rule = build_green_rule_payload(key)
        scope_key = rule["judge_config"]["green_rule_contract"]["input_kind"]
        self.input_set = seal_evidence_input_set(
            task_id=self.ids["task"],
            source_agent_run_id=self.ids["run"],
            rule_id=self.ids["rule"],
            rule_code=rule["rule_code"],
            rule_contract_hash="frozen-contract",
            ruleset_snapshot_hash="frozen-ruleset",
            scope=EvidenceInputScope.TASK,
            scope_key=scope_key,
            finalization_epoch=0,
            eligibility_snapshot_id="eligibility-1",
            eligibility_snapshot_hash="eligibility-hash",
            selection_policy_hash="selection-hash",
            eligible_capture_ids=tuple(item.manifest.capture_id for item in candidates),
            candidates=tuple(candidates),
            sealed_at=NOW,
        )
        intent = ActionIntent(
            task_id=self.ids["task"],
            session_id="session-1",
            intent_id="intent-1",
            idempotency_key="intent-key",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=self.ids["origin"],
            action_type=InteractionOperation.CLICK,
            semantic_target="受控按钮",
            purpose="组件用例",
            capture_profile=CaptureProfile(
                profile_id="fixture",
                before=CaptureDirective(role="before_action", artifact_types=("ui_snapshot",)),
                after=CaptureDirective(role="after_action", artifact_types=("ui_snapshot",)),
            ),
            deadline=NOW + timedelta(seconds=30),
        )
        self.actions = [
            {
                "task_id": self.ids["task"],
                "action_execution_id": "action-1",
                "session_id": "session-1",
                "source_capture_id": self.ids["before"],
                "result_capture_id": self.ids["after"],
                "requested_source_capture_id": self.ids["origin"],
                "command_sequence": 2,
                "status": "completed",
                "started_at": NOW + timedelta(milliseconds=1),
                "action_finished_at": NOW + timedelta(milliseconds=1),
                "intent_id": intent.intent_id,
                "idempotency_key": intent.idempotency_key,
                "intent_payload": intent.to_dict(),
                "intent_hash": build_action_intent_hash(intent),
            }
        ]
        self.rule = rule

    async def fetchrow(self, sql, *args):
        if "SELECT input_set_payload,input_set_hash" in sql:
            return {
                "input_set_payload": self.input_set.to_dict(),
                "input_set_hash": self.input_set.input_set_hash,
            }
        if "SELECT task.finalization_epoch" in sql:
            return self.identity
        if "SELECT capture.*" in sql:
            return self.captures.get(args[0])
        if "SELECT manifest_payload" in sql:
            return {"manifest_payload": self.manifests[args[0]]}
        if "SELECT * FROM qc_evidence_artifact" in sql:
            return self.artifacts.get(args[0])
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if "FROM qc_evidence_input_set_member" in sql:
            return [member.to_dict() for member in self.input_set.members]
        if "SELECT action.*" in sql:
            return self.actions
        if "SELECT capture.id::text" in sql:
            return [
                {"id": key, "capture_status": "captured", "manifest_payload": value}
                for key, value in self.manifests.items()
            ] + self.extra_captures
        raise AssertionError(sql)

    async def read(self, path):
        return self.payloads[path]


def context(fixture, evidence):
    inputs = fixture.input_set
    return SimpleNamespace(
        rule_payload=fixture.rule,
        green_evidence=evidence,
        input_set=inputs,
        lease=SimpleNamespace(
            task_id=inputs.task_id,
            rule_id=inputs.rule_id,
            rule_contract_hash=inputs.rule_contract_hash,
            evidence_input_set_id=inputs.input_set_id,
            input_hash=inputs.input_set_hash,
            finalization_epoch=inputs.finalization_epoch,
            scope="task",
            scope_key=inputs.scope_key,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["s35", "s72", "s49", "s73", "62"])
async def test_existing_originals_and_driver_success_never_invent_business_pass(key):
    fixture = FixtureConnection(key)
    evidence = await load_green_process_evidence(
        fixture, input_set=fixture.input_set, artifact_store=fixture
    )
    assert len(evidence.actions) == 1 and len(evidence.captures) == 2
    assert not evidence.issues
    result = evaluate_green_aggregate_context(context(fixture, evidence))
    assert result.execution_status == "evidence_insufficient"
    green = result.wrapper_payload["green"]
    assert [item["key"] for item in green["subitems"]] == fixture.rule["judge_config"][
        "green_rule_contract"
    ]["subchecks"]
    assert all(
        item["missing"] and item["status"] == "evidence_insufficient" for item in green["subitems"]
    )
    assert all(
        item["evaluated"] is False and item["confidence"] is None for item in green["subitems"]
    )
    assert green["technical_execution"]["formal_completed_action_count"] == 1
    assert '"storage_path"' not in json.dumps(green) and '"green_pass"' not in json.dumps(green)
    assert '"business_success": true' not in json.dumps(green)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["run", "epoch", "manifest", "bytes", "path", "action", "intent", "late_capture"]
)
async def test_original_and_action_drift_changes_fingerprint_and_preserves_gap(fault):
    fixture = FixtureConnection()
    first = await load_green_process_evidence(
        fixture, input_set=fixture.input_set, artifact_store=fixture
    )
    if fault == "run":
        fixture.identity["owner_run"] = "foreign-run"
    elif fault == "epoch":
        fixture.identity["finalization_epoch"] = 1
    elif fault == "manifest":
        fixture.manifests[fixture.ids["after"]] = copy.deepcopy(
            fixture.manifests[fixture.ids["after"]]
        )
        fixture.manifests[fixture.ids["after"]]["manifest_version"] = 2
    elif fault == "bytes":
        key = next(iter(fixture.payloads))
        fixture.payloads[key] = b"altered-original"
    elif fault == "path":
        next(iter(fixture.artifacts.values()))["storage_path"] = "../foreign.json"
    elif fault == "action":
        fixture.actions[0]["session_id"] = "foreign-session"
    elif fault == "intent":
        fixture.actions[0]["intent_hash"] = "invented-driver-success"
    else:
        fixture.extra_captures.append(
            {
                "id": str(uuid4()),
                "capture_status": "captured",
                "manifest_payload": {"quality_status": "fail", "manifest_version": 3},
            }
        )
    changed = await load_green_process_evidence(
        fixture, input_set=fixture.input_set, artifact_store=fixture
    )
    assert changed.fingerprint != first.fingerprint
    assert changed.issues
    assert (
        evaluate_green_aggregate_context(context(fixture, changed)).execution_status
        == "evidence_insufficient"
    )


def test_explicit_counterexample_keeps_failure_and_unverified_other_subitems():
    status, missing = reduce_green_subitems(
        (
            GreenSubitem("known", "fail", "已证反例", capture_ids=("original",)),
            GreenSubitem("other", "evidence_insufficient", "尚缺原生事实", ("missing_clock",)),
        )
    )
    assert status == "fail" and missing == ("missing_clock",)
    with pytest.raises(ValueError):
        reduce_green_subitems(({"status": "pass"},))
    with pytest.raises(ValueError):
        reduce_green_subitems((GreenSubitem("x", "pass", "不能带缺项", ("missing",)),))


def test_arbitrary_json_and_direct_dataclass_construction_are_not_verified_reads():
    fixture = FixtureConnection()
    forged = GreenEvidenceSet(
        task_id=fixture.ids["task"],
        run_id=fixture.ids["run"],
        epoch=0,
        input_set_id=fixture.input_set.input_set_id,
        input_hash=fixture.input_set.input_set_hash,
        scope_key=fixture.input_set.scope_key,
        fingerprint="invented",
    )
    assert not forged.verified_read
    assert (
        evaluate_green_aggregate_context(context(fixture, forged)).execution_status
        == "evidence_insufficient"
    )
    assert (
        evaluate_green_aggregate_context(context(fixture, {"green_pass": True})).execution_status
        == "evidence_insufficient"
    )


@pytest.mark.asyncio
async def test_display_only_closed_epoch_option_does_not_relax_default_consumer():
    fixture = FixtureConnection()
    original = await load_green_process_evidence(
        fixture, input_set=fixture.input_set, artifact_store=fixture
    )
    fixture.identity["finalization_epoch"] = 1
    default = await load_green_process_evidence(
        fixture, input_set=fixture.input_set, artifact_store=fixture
    )
    assert "green_task_run_epoch_mismatch" in default.issues
    fixture.fetchval = AsyncMock(return_value=False)
    denied = await load_green_process_evidence(
        fixture,
        input_set=fixture.input_set,
        artifact_store=fixture,
        allow_closed_epoch=True,
    )
    assert "green_task_run_epoch_mismatch" in denied.issues
    fixture.fetchval = AsyncMock(return_value=True)
    bound = await load_green_process_evidence(
        fixture,
        input_set=fixture.input_set,
        artifact_store=fixture,
        allow_closed_epoch=True,
    )
    assert bound.fingerprint == original.fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["captures", "single_original", "all_originals"])
async def test_technical_capacity_refuses_whole_input_instead_of_using_early_subset(
    monkeypatch, limit
):
    import services.api.app.infrastructure.db.green_process_evidence as loader

    fixture = FixtureConnection()
    size = len(next(iter(fixture.payloads.values())))
    if limit == "captures":
        monkeypatch.setattr(loader, "_MAX_CAPTURE_COUNT", 1)
    elif limit == "single_original":
        monkeypatch.setattr(loader, "_MAX_ARTIFACT_BYTES", size - 1)
    else:
        monkeypatch.setattr(loader, "_MAX_TOTAL_BYTES", size * 2 - 1)
    evidence = await loader.load_green_process_evidence(
        fixture,
        input_set=fixture.input_set,
        artifact_store=fixture,
    )
    assert evidence.captures == () and evidence.actions == ()
    assert any("capacity_exceeded" in item for item in evidence.issues)
    assert (
        evaluate_green_aggregate_context(context(fixture, evidence)).execution_status
        == "evidence_insufficient"
    )


@pytest.mark.parametrize(
    "key,status",
    [
        ("102", "evidence_insufficient"),
        ("32", "evidence_insufficient"),
        ("41", "evidence_insufficient"),
    ],
)
def test_page_preflight_cannot_fallback_to_visual_identity_or_partial_geometry(key, status):
    original = TrustedEvidenceArtifact(
        "image", "screenshot", "capture", "primary", "image/png", b"fixture"
    )
    bundle = TrustedEvidenceBundle("packet", "capture", {"primary": "capture"}, (original,))
    result = green_page_preflight(key, bundle)
    assert result["execution_status"] == status
    assert result["missing_evidence"] and result["legacy_visual_fallback_allowed"] is False
    assert [item["key"] for item in result["subitems"]] == build_green_rule_payload(key)[
        "judge_config"
    ]["green_rule_contract"]["subchecks"]
    assert all(
        item["evaluated"] is False and item["confidence"] is None for item in result["subitems"]
    )
    assert all(item["missing"] for item in result["subitems"])
    assert (
        green_page_preflight(key, {"all_pass": True})["execution_status"] == "evidence_insufficient"
    )
