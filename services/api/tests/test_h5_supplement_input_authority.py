"""恢复补采必须按实际生产者及整组角色授权，不借原会话或遗漏前帧。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from services.api.app.application.inspection_work_items import (
    InspectionRuleEvidenceBinding,
    build_inspection_attempt_input_hash,
)
from services.api.app.application.observation_packet import ObservationPacketValidationError
from services.api.app.infrastructure.db.h5_page_binding_reader import PostgresH5PageBindingReader


@pytest.mark.parametrize(
    "fault", [None, "source", "capture", "input", "attempt", "binding", "rule_code"]
)
async def test_restored_input_uses_actual_producer_and_bound_roles(monkeypatch, fault):
    from services.api.app.infrastructure.db import h5_page_binding_reader as reader_module
    from services.api.app.infrastructure.db import inspection_work_item_repository as repository

    binding = InspectionRuleEvidenceBinding(
        rule_id="rule",
        rule_code="PRICE",
        page_capture_id="before",
        producer_agent_run_id="restored-run",
        manifest_id="before-manifest",
        manifest_version=1,
        artifact_ids=("before-artifact", "after-artifact"),
        observation_packet_ids=(),
        evidence_input_set_id="supplement-input",
        evidence_input_set_hash="supplement-hash",
        evidence_capture_roles=(("primary", "before"), ("after", "after")),
        evidence_manifest_refs=(
            ("primary", "before-manifest", "1"),
            ("after", "after-manifest", "1"),
        ),
    )
    inputs = SimpleNamespace(
        task_id="task",
        source_agent_run_id="restored-run",
        rule_id="rule",
        rule_code="OTHER" if fault == "rule_code" else "PRICE",
        rule_contract_hash="contract",
        finalization_epoch=0,
        ruleset_snapshot_hash="snapshot",
        eligible_capture_ids=("after",),
        input_set_id="supplement-input",
        members=(SimpleNamespace(capture_id="after", manifest_id="after-manifest"),),
    )
    row = dict(
        input_hash="work-hash",
        finalization_epoch=0,
        ruleset_snapshot_hash="snapshot",
        evidence_input_set_id="original-input",
        rule_code="PRICE",
        producer_agent_run_id="original-run",
        page_capture_id="original-capture",
        attempt_input_hash=build_inspection_attempt_input_hash(
            work_item_input_hash="work-hash",
            rule_evidence=(binding,),
        ),
    )
    if fault == "attempt":
        row["attempt_input_hash"] = "old-attempt"

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield

        async def execute(self, *args):
            pass

        async def fetchrow(self, *args):
            return row

    async def bindings(*args, **kwargs):
        if fault == "binding":
            from services.api.app.application.inspection_work_items import (
                InspectionWorkItemValidationError,
            )

            raise InspectionWorkItemValidationError("补采前帧缺少当前精确 PASS Manifest")
        return (binding,)

    async def verified(*args, **kwargs):
        assert "restored-run" in kwargs["packet_runs"]
        return inputs

    monkeypatch.setattr(repository, "_load_rule_evidence_bindings", bindings)
    monkeypatch.setattr(reader_module, "read_verified_page_input", verified)
    identity = dict(
        task_id="task",
        page_capture_id="foreign" if fault == "capture" else "before",
        source_agent_run_id="original-run" if fault == "source" else "restored-run",
        input_set_id="original-input" if fault == "input" else "supplement-input",
        rule_id="rule",
        rule_contract_hash="contract",
        work_item_id="work",
        attempt_id="attempt",
        consumer_agent_run_id="judge",
        lease_token="lease",
        connection=Connection(),
    )
    reader = PostgresH5PageBindingReader()
    if fault:
        with pytest.raises(ObservationPacketValidationError):
            await reader.load_authorized(**identity)
    else:
        assert await reader.load_authorized(**identity) is inputs


async def test_supplement_handoff_keeps_private_authority_with_original_member():
    from dataclasses import replace

    from services.api.app.application.inspection_handoff import build_inspection_handoff
    from services.api.tests.test_inspection_handoff import _lease

    lease = _lease()
    rule = replace(lease.rules[0], evidence_input_set_id="original-input")
    binding = InspectionRuleEvidenceBinding(
        rule_id=rule.rule_id,
        rule_code=rule.rule_code,
        page_capture_id="new-capture",
        producer_agent_run_id="restored-run",
        manifest_id="new-manifest",
        manifest_version=1,
        artifact_ids=("artifact",),
        observation_packet_ids=(),
        evidence_input_set_id="supplement-input",
        evidence_input_set_hash="supplement-hash",
    )
    handoff = build_inspection_handoff(
        replace(lease, target_platform="h5", rules=(rule,), rule_evidence=(binding,)),
        attempt_id="attempt",
    )
    assert handoff.original_page_input_sets == {rule.rule_code: "supplement-input"}
    assert handoff.rules[0].evidence_input_set_id == "original-input"
    assert handoff.evidence_for_rule(rule.rule_code).producer_agent_run_id == "restored-run"
    assert "original_page_input_sets" not in handoff.to_dict()


@pytest.mark.parametrize("fault", [None, "foreign_manifest", "artifact", "late_binding"])
async def test_exact_before_frame_is_read_under_current_supplement_authority(fault):
    from dataclasses import replace

    from services.api.app.application.h5_page_input_reader import H5PageInputObservationReader
    from services.api.app.application.observation_packet import ObservationSource

    member = SimpleNamespace(
        capture_id="after",
        manifest_id="after-manifest",
        manifest_version=1,
        manifest_hash="after-hash",
        artifact_ids=("after-artifact",),
    )
    inputs = SimpleNamespace(
        members=(member,),
        input_set_id="input",
        input_set_hash="input-hash",
        rule_contract_hash="contract",
    )
    binding = InspectionRuleEvidenceBinding(
        rule_id="rule",
        rule_code="PRICE",
        page_capture_id="before",
        producer_agent_run_id="restored",
        manifest_id="before-manifest",
        manifest_version=2,
        artifact_ids=("before-artifact", "after-artifact"),
        observation_packet_ids=(),
        evidence_capture_roles=(("primary", "before"), ("after", "after")),
        evidence_manifest_refs=(
            ("primary", "before-manifest", "2"),
            ("after", "after-manifest", "1"),
        ),
    )
    authorizations = []
    reads = []

    class Bindings:
        async def load_authorized_bundle(self, **identity):
            authorizations.append(identity)
            return inputs, (
                replace(binding, artifact_ids=("changed",))
                if fault == "late_binding" and len(authorizations) == 2
                else binding
            )

    class Contexts:
        async def load_capture_context(self, **identity):
            return ObservationSource(
                task_id="task",
                page_capture_id="before",
                page_url="https://fixture.test/",
                page_title=None,
                page_type="membership",
                page_state=None,
                action_log=(),
                artifacts=(),
                target_platform="h5",
                agent_run_id="restored",
            )

    class Packets:
        async def load_for_manifest(self, **identity):
            reads.append(identity)
            return SimpleNamespace(
                capture_id="before",
                manifest_id="before-manifest",
                manifest_version=2,
                manifest_hash="before-hash",
                session_id="restored-session",
                artifact_ids=("foreign" if fault == "artifact" else "before-artifact",),
                quality_status=SimpleNamespace(value="pass"),
                artifacts=(
                    SimpleNamespace(
                        artifact_id="before-artifact",
                        artifact_type="ui_snapshot",
                        content_type="application/json",
                        payload=b"{}",
                    ),
                ),
            )

    reader = H5PageInputObservationReader(
        inner=object(),
        contexts=Contexts(),
        bindings=Bindings(),
        packet_loader=Packets(),
    )
    identity = dict(
        task_id="task",
        page_capture_id="before",
        source_agent_run_id="restored",
        input_set_id="input",
        rule_id="rule",
        rule_contract_hash="contract",
        work_item_id="work",
        attempt_id="attempt",
        consumer_agent_run_id="judge",
        lease_token="private",
        manifest_id=("after-manifest" if fault == "foreign_manifest" else "before-manifest"),
    )
    if fault:
        with pytest.raises(ObservationPacketValidationError):
            await reader.load_source_for_page_input_set(**identity)
    else:
        source = await reader.load_source_for_page_input_set(**identity)
        assert source.manifest_quality.manifest_id == "before-manifest"
        assert source.artifacts[0].artifact_id == "before-artifact"
        assert source.original_page_input["manifest_id"] == "after-manifest"
        assert len(authorizations) == 2
        assert reads == [{"task_id": "task", "manifest_id": "before-manifest"}]


async def test_extra_role_cannot_fall_back_to_unbound_latest_manifest():
    from services.api.app.application.observation_capture_roles import load_role_evidence
    from services.api.app.application.observation_packet import (
        BuildObservationPacketRequest,
        ObservationSource,
    )

    primary = ObservationSource(
        task_id="task",
        page_capture_id="before",
        page_url="https://fixture.test/",
        page_title=None,
        page_type="membership",
        page_state=None,
        action_log=(),
        artifacts=(),
        target_platform="h5",
        agent_run_id="restored",
    )
    keys = []

    async def load(key):
        keys.append(key)
        raise ObservationPacketValidationError("后帧当前授权已失效")

    request = BuildObservationPacketRequest(
        task_id="task",
        page_capture_id="original",
        rule_id="rule",
        rule_code="PRICE",
        rule_name="价格反馈",
        category="text",
        executor_type="rule_engine",
        check_point="价格反馈",
        input_contract={},
        judge_config={},
        manual_review_policy={},
        requested_evidence_artifact_ids=(),
        agent_input={},
        agent_run_id="judge",
        source_agent_run_id="restored",
        original_page_input_set_id="supplement",
        expected_contract_hash="contract",
        work_item_id="work",
        attempt_id="attempt",
        lease_token="private",
        evidence_capture_roles={"primary": "before", "after": "after"},
        evidence_manifest_refs={
            "after": {"manifest_id": "after-manifest", "manifest_version": "1"}
        },
    )
    with pytest.raises(ObservationPacketValidationError, match="已失效"):
        await load_role_evidence(request=request, primary=primary, load_source=load)
    assert keys == [
        (
            "task",
            "after",
            "restored",
            "after-manifest",
            "supplement",
            "rule",
            "contract",
            "work",
            "attempt",
            "judge",
            "private",
            "h5_page",
        )
    ]
