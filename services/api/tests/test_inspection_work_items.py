"""页面质检持久化工作项协议与状态机测试。"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from services.api.app.application.inspection_work_items import (
    ClaimInspectionWorkItemCommand,
    InspectionAttemptStatus,
    InspectionRuleEvidenceBinding,
    InspectionWorkItemConflictError,
    InspectionWorkItemLease,
    InspectionWorkItemRule,
    InspectionWorkItemRuleStatus,
    InspectionWorkItemService,
    InspectionWorkItemStatus,
    InspectionWorkItemValidationError,
    PublishedInspectionWorkItem,
    PublishInspectionWorkItemCommand,
    RenewInspectionWorkItemLeaseCommand,
    TransitionInspectionAttemptCommand,
    TransitionInspectionWorkItemCommand,
    TransitionInspectionWorkItemRuleCommand,
    build_inspection_attempt_input_hash,
    build_inspection_rule_contract_hash,
    build_inspection_work_item_input_hash,
)


class FakeRepository:
    def __init__(self) -> None:
        self.published: list[tuple[PublishInspectionWorkItemCommand, str]] = []
        self.claims: list[tuple[ClaimInspectionWorkItemCommand, str]] = []
        self.transitions: list[TransitionInspectionWorkItemCommand] = []
        self.attempt_transitions: list[TransitionInspectionAttemptCommand] = []
        self.rule_transitions: list[TransitionInspectionWorkItemRuleCommand] = []
        self.transition_result: int | None = 3
        self.attempt_transition_result = True
        self.nonterminal = False

    async def publish(self, *, command, input_hash):
        self.published.append((command, input_hash))
        return PublishedInspectionWorkItem(
            work_item_id="work-1",
            status=InspectionWorkItemStatus.PENDING,
            input_hash=input_hash,
            created=True,
        )

    async def claim(self, *, command, lease_token):
        self.claims.append((command, lease_token))
        return InspectionWorkItemLease(
            work_item_id="work-1",
            task_id=command.task_id,
            page_capture_id="capture-1",
            batch_key="hybrid:visual:snapshot",
            strategy="hybrid",
            category="visual",
            session_dependency="snapshot",
            input_hash="input-hash",
            producer_agent_run_id="producer-run-1",
            consumer_agent_run_id=command.consumer_agent_run_id,
            lease_token=lease_token,
            attempt_no=1,
            row_version=1,
            rules=(),
        )

    async def transition(self, *, command):
        self.transitions.append(command)
        return self.transition_result

    async def renew_lease(self, *, command):
        return self.transition_result

    async def start_attempt(self, *, command):
        return "attempt-1"

    async def transition_attempt(self, *, command):
        self.attempt_transitions.append(command)
        return self.attempt_transition_result

    async def transition_rule(self, *, command):
        self.rule_transitions.append(command)
        return True

    async def sync_formal_results(self, *, work_item_id, lease_token):
        raise NotImplementedError

    async def has_nonterminal_work(self, *, task_id):
        return self.nonterminal

    async def finalize_task_work(self, **kwargs):
        raise NotImplementedError


def _rule() -> InspectionWorkItemRule:
    return InspectionWorkItemRule(
        rule_id="rule-1",
        rule_code="alioth_1",
        ordinal=0,
        contract_hash="contract-hash",
    )


def _publish_command() -> PublishInspectionWorkItemCommand:
    return PublishInspectionWorkItemCommand(
        task_id="task-1",
        page_capture_id="capture-1",
        producer_agent_run_id="run-1",
        ruleset_id="ruleset-1",
        ruleset_version=3,
        ruleset_snapshot_hash="ruleset-hash",
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        rules=(_rule(),),
    )


async def test_publish_builds_stable_input_hash() -> None:
    repository = FakeRepository()
    service = InspectionWorkItemService(repository=repository)
    command = _publish_command()

    first = await service.publish(command)
    second = await service.publish(command)

    assert first.input_hash == second.input_hash
    assert repository.published[0][1] == build_inspection_work_item_input_hash(command)


def test_shadow_and_enforced_items_have_distinct_input_hashes() -> None:
    command = _publish_command()

    assert build_inspection_work_item_input_hash(
        replace(command, control_mode="shadow")
    ) != build_inspection_work_item_input_hash(replace(command, control_mode="enforced"))


def test_supplemental_manifest_changes_attempt_input_hash_stably() -> None:
    first = InspectionRuleEvidenceBinding(
        rule_id="rule-1",
        rule_code="alioth_1",
        page_capture_id="capture-2",
        producer_agent_run_id="run-2",
        manifest_id="manifest-2",
        manifest_version=2,
        artifact_ids=("shot-2",),
    )
    second = replace(first, rule_id="rule-2", rule_code="alioth_2")

    without_supplement = build_inspection_attempt_input_hash(
        work_item_input_hash="base-hash",
        rule_evidence=(),
    )
    with_supplement = build_inspection_attempt_input_hash(
        work_item_input_hash="base-hash",
        rule_evidence=(first, second),
    )

    assert without_supplement == "base-hash"
    assert with_supplement != "base-hash"
    assert with_supplement == build_inspection_attempt_input_hash(
        work_item_input_hash="base-hash",
        rule_evidence=(second, first),
    )
    assert with_supplement != build_inspection_attempt_input_hash(
        work_item_input_hash="base-hash",
        rule_evidence=(replace(first, manifest_id="manifest-3"), second),
    )


def test_rule_evidence_binding_rejects_invalid_or_duplicate_capture_roles() -> None:
    base = InspectionRuleEvidenceBinding(
        rule_id="rule-1",
        rule_code="alioth_1",
        page_capture_id="capture-2",
        producer_agent_run_id="run-2",
        manifest_id="manifest-2",
        manifest_version=2,
        artifact_ids=("shot-2",),
    )

    with pytest.raises(InspectionWorkItemValidationError, match="二元组"):
        replace(base, evidence_capture_roles=(("before", "capture-1", "extra"),))
    with pytest.raises(InspectionWorkItemValidationError, match="重复角色"):
        replace(
            base,
            evidence_capture_roles=(
                ("primary", "capture-2"),
                ("primary", "capture-3"),
            ),
        )


def test_resealed_input_set_identity_enters_attempt_hash_without_moving_legacy_hashes() -> None:
    """补采重新封存的输入集合并进 Attempt 身份；没有封存集合的旧补采哈希保持不变。"""
    legacy = InspectionRuleEvidenceBinding(
        rule_id="rule-1",
        rule_code="alioth_1",
        page_capture_id="capture-2",
        producer_agent_run_id="run-2",
        manifest_id="manifest-2",
        manifest_version=2,
        artifact_ids=("shot-2",),
    )
    resealed = replace(
        legacy,
        evidence_input_set_id="evidence-input-set:" + "a" * 64,
        evidence_input_set_hash="a" * 64,
    )
    legacy_hash = build_inspection_attempt_input_hash(
        work_item_input_hash="base-hash",
        rule_evidence=(legacy,),
    )

    assert legacy_hash == _stable_hash_for_test(
        {
            "work_item_input_hash": "base-hash",
            "rule_evidence": [
                {
                    "rule_id": "rule-1",
                    "rule_code": "alioth_1",
                    "page_capture_id": "capture-2",
                    "producer_agent_run_id": "run-2",
                    "manifest_id": "manifest-2",
                    "manifest_version": 2,
                    "artifact_ids": ["shot-2"],
                    "observation_packet_ids": [],
                }
            ],
        }
    )
    assert build_inspection_attempt_input_hash(
        work_item_input_hash="base-hash",
        rule_evidence=(resealed,),
    ) != legacy_hash
    with pytest.raises(InspectionWorkItemValidationError, match="同时提供"):
        replace(legacy, evidence_input_set_id="evidence-input-set:" + "a" * 64)


def _stable_hash_for_test(payload: dict) -> str:
    from services.api.app.application.inspection_work_items import _stable_hash

    return _stable_hash(payload)


def test_rule_contract_hash_ignores_non_judgement_display_fields() -> None:
    rule = {
        "rule_id": "rule-1",
        "rule_code": "alioth_1",
        "category": "visual",
        "executor_type": "hybrid",
        "check_point": "检查按钮",
        "input_contract": {"required_artifacts": ["screenshot"]},
        "display_name": "页面展示名称",
    }

    assert build_inspection_rule_contract_hash(rule) == build_inspection_rule_contract_hash(
        {**rule, "display_name": "修改后的展示名称"}
    )
    assert build_inspection_rule_contract_hash(rule) != build_inspection_rule_contract_hash(
        {**rule, "check_point": "检查另一个按钮"}
    )


def test_rule_contract_hash_locks_journey_steps() -> None:
    rule = {
        "rule_id": "rule-1",
        "rule_code": "alioth_1",
        "steps": [{"type": "action", "operation": "tap", "target": "登录"}],
    }

    assert build_inspection_rule_contract_hash(rule) != build_inspection_rule_contract_hash(
        {
            **rule,
            "steps": [{"type": "action", "operation": "swipe", "target": "登录"}],
        }
    )


async def test_active_session_publish_requires_owner_run() -> None:
    service = InspectionWorkItemService(repository=FakeRepository())

    with pytest.raises(
        InspectionWorkItemValidationError,
        match="session_owner_agent_run_id",
    ):
        await service.publish(
            replace(
                _publish_command(),
                session_dependency="active_session",
                batch_key="hybrid:interaction:active_session",
            )
        )


async def test_publish_rejects_duplicate_rule_identity() -> None:
    service = InspectionWorkItemService(repository=FakeRepository())

    with pytest.raises(InspectionWorkItemValidationError, match="重复规则"):
        await service.publish(
            replace(
                _publish_command(),
                rules=(
                    _rule(),
                    replace(_rule(), ordinal=1),
                ),
            )
        )


async def test_claim_generates_unique_lease_token() -> None:
    repository = FakeRepository()
    service = InspectionWorkItemService(repository=repository)
    command = ClaimInspectionWorkItemCommand(
        task_id="task-1",
        consumer_agent_run_id="run-1",
        lease_owner="worker-1",
        lease_seconds=180,
        session_dependency="snapshot",
    )

    first = await service.claim(command)
    second = await service.claim(command)

    assert first is not None
    assert second is not None
    assert first.lease_token != second.lease_token


async def test_running_can_finish_and_conflict_is_not_silently_ignored() -> None:
    repository = FakeRepository()
    service = InspectionWorkItemService(repository=repository)
    command = TransitionInspectionWorkItemCommand(
        work_item_id="work-1",
        expected_status=InspectionWorkItemStatus.RUNNING,
        target_status=InspectionWorkItemStatus.SUCCEEDED,
        row_version=2,
        lease_token="lease-1",
    )

    assert await service.transition(command) == 3
    assert repository.transitions == [command]

    repository.transition_result = None
    with pytest.raises(InspectionWorkItemConflictError):
        await service.transition(command)


@pytest.mark.parametrize(
    "target_status",
    (InspectionWorkItemStatus.SUCCEEDED, InspectionWorkItemStatus.PARTIAL),
)
async def test_waiting_evidence_can_finish_after_terminal_request_reconciliation(
    target_status: InspectionWorkItemStatus,
) -> None:
    repository = FakeRepository()
    service = InspectionWorkItemService(repository=repository)
    command = TransitionInspectionWorkItemCommand(
        work_item_id="work-1",
        expected_status=InspectionWorkItemStatus.WAITING_EVIDENCE,
        target_status=target_status,
        row_version=2,
    )

    assert await service.transition(command) == 3
    assert repository.transitions == [command]


async def test_lease_renewal_returns_next_row_version() -> None:
    repository = FakeRepository()
    service = InspectionWorkItemService(repository=repository)

    assert (
        await service.renew_lease(
            RenewInspectionWorkItemLeaseCommand(
                work_item_id="work-1",
                consumer_agent_run_id="run-1",
                lease_token="lease-1",
                row_version=2,
                lease_seconds=180,
            )
        )
        == 3
    )

    repository.transition_result = None
    with pytest.raises(InspectionWorkItemConflictError, match="租约已过期"):
        await service.renew_lease(
            RenewInspectionWorkItemLeaseCommand(
                work_item_id="work-1",
                consumer_agent_run_id="run-1",
                lease_token="lease-1",
                row_version=2,
                lease_seconds=180,
            )
        )


async def test_retry_wait_requires_next_attempt_time() -> None:
    service = InspectionWorkItemService(repository=FakeRepository())

    with pytest.raises(InspectionWorkItemValidationError, match="next_attempt_at"):
        await service.transition(
            TransitionInspectionWorkItemCommand(
                work_item_id="work-1",
                expected_status=InspectionWorkItemStatus.RUNNING,
                target_status=InspectionWorkItemStatus.RETRY_WAIT,
                row_version=2,
                lease_token="lease-1",
            )
        )

    await service.transition(
        TransitionInspectionWorkItemCommand(
            work_item_id="work-1",
            expected_status=InspectionWorkItemStatus.RUNNING,
            target_status=InspectionWorkItemStatus.RETRY_WAIT,
            row_version=2,
            lease_token="lease-1",
            next_attempt_at=datetime.now(UTC),
        )
    )


async def test_attempt_must_persist_payload_before_written() -> None:
    repository = FakeRepository()
    service = InspectionWorkItemService(repository=repository)

    with pytest.raises(InspectionWorkItemValidationError, match="result_payload"):
        await service.transition_attempt(
            TransitionInspectionAttemptCommand(
                attempt_id="attempt-1",
                expected_status=InspectionAttemptStatus.RUNNING,
                target_status=InspectionAttemptStatus.JUDGED,
                lease_token="lease-1",
            )
        )

    await service.transition_attempt(
        TransitionInspectionAttemptCommand(
            attempt_id="attempt-1",
            expected_status=InspectionAttemptStatus.RUNNING,
            target_status=InspectionAttemptStatus.JUDGED,
            lease_token="lease-1",
            result_payload={"items": [{"rule_code": "alioth_1", "status": "pass"}]},
        )
    )
    assert repository.attempt_transitions[0].target_status == (InspectionAttemptStatus.JUDGED)


async def test_attempt_rejects_raw_evidence_but_allows_artifact_ids() -> None:
    service = InspectionWorkItemService(repository=FakeRepository())

    with pytest.raises(InspectionWorkItemValidationError, match="原始证据字段"):
        await service.transition_attempt(
            TransitionInspectionAttemptCommand(
                attempt_id="attempt-1",
                expected_status=InspectionAttemptStatus.RUNNING,
                target_status=InspectionAttemptStatus.JUDGED,
                lease_token="lease-1",
                result_payload={"dom": "<html>raw</html>"},
            )
        )

    await service.transition_attempt(
        TransitionInspectionAttemptCommand(
            attempt_id="attempt-1",
            expected_status=InspectionAttemptStatus.RUNNING,
            target_status=InspectionAttemptStatus.JUDGED,
            lease_token="lease-1",
            result_payload={
                "screenshot_artifact_ids": ["artifact-1"],
                "screenshot_count": 1,
                "dom_summary": {"element_count": 3},
                "execution_status": "pass",
            },
        )
    )


async def test_rule_member_terminal_state_requires_rule_execution() -> None:
    repository = FakeRepository()
    service = InspectionWorkItemService(repository=repository)

    with pytest.raises(InspectionWorkItemValidationError, match="rule_execution_id"):
        await service.transition_rule(
            TransitionInspectionWorkItemRuleCommand(
                work_item_id="work-1",
                rule_id="rule-1",
                expected_status=InspectionWorkItemRuleStatus.JUDGED,
                target_status=InspectionWorkItemRuleStatus.WRITTEN,
                lease_token="lease-1",
            )
        )

    await service.transition_rule(
        TransitionInspectionWorkItemRuleCommand(
            work_item_id="work-1",
            rule_id="rule-1",
            expected_status=InspectionWorkItemRuleStatus.JUDGED,
            target_status=InspectionWorkItemRuleStatus.WRITTEN,
            lease_token="lease-1",
            observation_packet_id="packet-1",
            rule_execution_id="execution-1",
        )
    )
    assert repository.rule_transitions[0].target_status == (InspectionWorkItemRuleStatus.WRITTEN)
