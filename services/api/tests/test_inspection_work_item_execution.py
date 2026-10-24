"""持久化质检工作项租约执行与恢复测试。"""

import asyncio
import logging

from services.api.app.agents.runtime.inspection_work_item_execution import (
    InspectionWorkItemAttempt,
)
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemExecutionLink,
    InspectionWorkItemLease,
    InspectionWorkItemRule,
    InspectionWorkItemStatus,
    InspectionWorkItemSyncResult,
    _validate_attempt_payload,
)


class FakeService:
    def __init__(self, sync_result: InspectionWorkItemSyncResult) -> None:
        self.sync_result = sync_result
        self.transitions = []
        self.attempt_transitions = []
        self.rule_transitions = []
        self.renew_calls = []
        self.row_version = 1

    async def transition(self, command):
        self.transitions.append(command)
        self.row_version += 1
        return self.row_version

    async def start_attempt(self, command):
        self.start_command = command
        return "attempt-1"

    async def transition_attempt(self, command):
        self.attempt_transitions.append(command)

    async def transition_rule(self, command):
        self.rule_transitions.append(command)

    async def sync_formal_results(self, *, work_item_id, lease_token):
        assert work_item_id == "work-1"
        assert lease_token == "lease-1"
        return self.sync_result

    async def renew_lease(self, command):
        self.renew_calls.append(command)
        self.row_version += 1
        return self.row_version


def _lease(*, target_platform: str | None = None) -> InspectionWorkItemLease:
    return InspectionWorkItemLease(
        work_item_id="work-1",
        task_id="task-1",
        page_capture_id="capture-1",
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        input_hash="input-1",
        producer_agent_run_id="producer-run-1",
        consumer_agent_run_id="consumer-run-1",
        lease_token="lease-1",
        attempt_no=1,
        row_version=1,
        target_platform=target_platform,
        rules=(
            InspectionWorkItemRule(
                rule_id="rule-1",
                rule_code="alioth_1",
                ordinal=0,
                contract_hash="contract-1",
            ),
        ),
    )


def _terminal_sync() -> InspectionWorkItemSyncResult:
    return InspectionWorkItemSyncResult(
        work_item_id="work-1",
        expected_rule_count=1,
        terminal_rule_count=1,
        pending_rule_count=0,
        written_rule_count=1,
        links=(
            InspectionWorkItemExecutionLink(
                rule_id="rule-1",
                rule_code="alioth_1",
                execution_status="pass",
                observation_packet_id="packet-1",
                rule_execution_id="execution-1",
            ),
        ),
    )


async def test_judgement_is_persisted_before_formal_result_finishes_item() -> None:
    service = FakeService(_terminal_sync())
    attempt = InspectionWorkItemAttempt(
        service=service,
        lease=_lease(),
        consumer_agent_run_id="run-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
    )

    await attempt.start()
    assert service.start_command.input_hash == "input-1"
    assert service.start_command.work_item_input_hash is None
    await attempt.persist_judgement(
        {
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "observation_packet_id": "packet-1",
                    "screenshot_count": 1,
                }
            ]
        }
    )
    result = await attempt.finalize_after_tool({"execution_status": "success"})

    assert result["execution_id"] == "execution-1"
    assert [command.target_status.value for command in service.attempt_transitions] == [
        "judged",
        "written",
    ]
    assert service.rule_transitions[0].target_status.value == "judged"
    assert [command.target_status for command in service.transitions] == [
        InspectionWorkItemStatus.RUNNING,
        InspectionWorkItemStatus.SUCCEEDED,
    ]


async def test_attempt_payload_omits_raw_evidence_before_persistence() -> None:
    service = FakeService(_terminal_sync())
    attempt = InspectionWorkItemAttempt(
        service=service,
        lease=_lease(),
        consumer_agent_run_id="run-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
    )

    await attempt.start()
    await attempt.persist_judgement(
        {
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "observation_packet_id": "packet-1",
                    "evidence_artifact_ids": ["artifact-1"],
                    "judge_output": {
                        "reasoning_summary": "倒计时符合预期",
                        "screenshot_count": 1,
                        "screenshot": "data:image/png;base64,AAAA",
                        "visual_refs": [
                            {
                                "artifact_id": "artifact-1",
                                "preview": "data:image/png;base64,BBBB",
                            }
                        ],
                    },
                }
            ],
            "dom": "<html>raw</html>",
        }
    )

    payload = service.attempt_transitions[0].result_payload
    item = payload["items"][0]
    assert "dom" not in payload
    assert "screenshot" not in item["judge_output"]
    assert item["judge_output"]["screenshot_count"] == 1
    assert item["judge_output"]["visual_refs"][0]["artifact_id"] == "artifact-1"
    assert item["judge_output"]["visual_refs"][0]["preview"] == "[raw evidence data url omitted]"
    assert service.rule_transitions[0].target_status.value == "judged"


async def test_attempt_payload_flattens_deep_branches_and_stays_persistable() -> None:
    service = FakeService(_terminal_sync())
    attempt = InspectionWorkItemAttempt(
        service=service,
        lease=_lease(),
        consumer_agent_run_id="run-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
    )

    await attempt.start()
    deep: dict = {
        "verdict": "ok",
        "dom": "<html>raw</html>",
        "preview": "data:image/png;base64,CCCC",
    }
    for _ in range(14):
        deep = {"layer": deep}
    await attempt.persist_judgement(
        {
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "observation_packet_id": "packet-1",
                    "judge_output": deep,
                }
            ]
        }
    )

    payload = service.attempt_transitions[0].result_payload
    _validate_attempt_payload(payload)
    item = payload["items"][0]
    flattened = item["judge_output"]
    for _ in range(20):
        if isinstance(flattened, str):
            break
        flattened = flattened["layer"]
    assert isinstance(flattened, str)
    assert flattened.startswith("[嵌套超限压平] ")
    assert "verdict" in flattened
    assert "<html>" not in flattened
    assert "data:image" not in flattened
    assert service.rule_transitions[0].target_status.value == "judged"


async def test_existing_formal_result_recovers_without_repeating_member_judgement() -> None:
    service = FakeService(_terminal_sync())
    attempt = InspectionWorkItemAttempt(
        service=service,
        lease=_lease(),
        consumer_agent_run_id="run-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
    )

    await attempt.start()
    result = await attempt.recover_formal_results()

    assert result["recovered"] is True
    assert service.rule_transitions == []
    assert service.attempt_transitions[0].result_payload["recovered_from_rule_executions"] is True
    assert service.transitions[-1].target_status == InspectionWorkItemStatus.SUCCEEDED


async def test_incomplete_retry_releases_lease_to_retry_wait() -> None:
    service = FakeService(
        InspectionWorkItemSyncResult(
            work_item_id="work-1",
            expected_rule_count=1,
            terminal_rule_count=0,
            pending_rule_count=1,
            written_rule_count=0,
            links=(),
        )
    )
    attempt = InspectionWorkItemAttempt(
        service=service,
        lease=_lease(),
        consumer_agent_run_id="run-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
    )

    await attempt.start()
    await attempt.persist_recovered_judgement(
        {"items": [{"rule_code": "alioth_1", "execution_status": "pass"}]}
    )
    result = await attempt.finalize_after_tool({"execution_status": "success"})

    assert result is None
    assert service.attempt_transitions[-1].target_status.value == "retryable_error"
    assert service.transitions[-1].target_status == InspectionWorkItemStatus.RETRY_WAIT
    assert service.transitions[-1].next_attempt_at is not None

    await asyncio.wait_for(attempt._renew_until_stopped(asyncio.Event()), timeout=0.1)


async def test_complete_evidence_requests_park_work_in_waiting_evidence() -> None:
    service = FakeService(
        InspectionWorkItemSyncResult(
            work_item_id="work-1",
            expected_rule_count=1,
            terminal_rule_count=0,
            pending_rule_count=1,
            written_rule_count=0,
            links=(),
        )
    )
    attempt = InspectionWorkItemAttempt(
        service=service,
        lease=_lease(target_platform="h5"),
        consumer_agent_run_id="run-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
    )

    await attempt.start()
    await attempt.persist_recovered_judgement(
        {
            "execution_status": "evidence_requested",
            "evidence_requests": [
                {
                    "work_item_id": "work-1",
                    "rule_id": "rule-1",
                    "rule_code": "alioth_1",
                }
            ],
        }
    )
    result = await attempt.finalize_after_tool(
        {
            "execution_status": "evidence_requested",
            "evidence_requests": [
                {
                    "request_id": "request-1",
                    "work_item_id": "work-1",
                    "rule_id": "rule-1",
                    "rule_code": "alioth_1",
                }
            ],
            "admitted_evidence_request_ids": ["request-1"],
        }
    )

    assert result is None
    assert service.attempt_transitions[-1].target_status.value == "terminal_error"
    assert service.transitions[-1].target_status == InspectionWorkItemStatus.WAITING_EVIDENCE
    assert service.transitions[-1].next_attempt_at is None


async def test_h5_unadmitted_evidence_request_remains_retryable() -> None:
    service = FakeService(
        InspectionWorkItemSyncResult(
            work_item_id="work-1",
            expected_rule_count=1,
            terminal_rule_count=0,
            pending_rule_count=1,
            written_rule_count=0,
            links=(),
        )
    )
    attempt = InspectionWorkItemAttempt(
        service=service,
        lease=_lease(target_platform="h5"),
        consumer_agent_run_id="run-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
    )

    await attempt.start()
    await attempt.finalize_after_tool(
        {
            "execution_status": "evidence_requested",
            "evidence_requests": [
                {
                    "request_id": "request-1",
                    "work_item_id": "work-1",
                    "rule_id": "rule-1",
                    "rule_code": "alioth_1",
                }
            ],
            "admitted_evidence_request_ids": [],
        }
    )

    assert service.attempt_transitions[-1].target_status.value == "retryable_error"
    assert service.transitions[-1].target_status == InspectionWorkItemStatus.RETRY_WAIT


async def test_non_h5_complete_evidence_requests_keep_existing_retry_semantics() -> None:
    service = FakeService(
        InspectionWorkItemSyncResult(
            work_item_id="work-1",
            expected_rule_count=1,
            terminal_rule_count=0,
            pending_rule_count=1,
            written_rule_count=0,
            links=(),
        )
    )
    attempt = InspectionWorkItemAttempt(
        service=service,
        lease=_lease(target_platform="web"),
        consumer_agent_run_id="run-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
    )

    await attempt.start()
    await attempt.persist_recovered_judgement(
        {
            "execution_status": "evidence_requested",
            "evidence_requests": [
                {
                    "work_item_id": "work-1",
                    "rule_id": "rule-1",
                    "rule_code": "alioth_1",
                }
            ],
        }
    )
    await attempt.finalize_after_tool(
        {
            "execution_status": "evidence_requested",
            "evidence_requests": [
                {
                    "work_item_id": "work-1",
                    "rule_id": "rule-1",
                    "rule_code": "alioth_1",
                }
            ],
        }
    )

    assert service.attempt_transitions[-1].target_status.value == "retryable_error"
    assert service.transitions[-1].target_status == InspectionWorkItemStatus.RETRY_WAIT


async def test_partial_evidence_requests_keep_missing_members_retryable() -> None:
    service = FakeService(
        InspectionWorkItemSyncResult(
            work_item_id="work-1",
            expected_rule_count=1,
            terminal_rule_count=0,
            pending_rule_count=1,
            written_rule_count=0,
            links=(),
        )
    )
    attempt = InspectionWorkItemAttempt(
        service=service,
        lease=_lease(target_platform="h5"),
        consumer_agent_run_id="run-1",
        lease_seconds=180,
        retry_backoff_seconds=2,
    )

    await attempt.start()
    await attempt.persist_recovered_judgement(
        {"execution_status": "evidence_requested", "evidence_requests": []}
    )
    await attempt.finalize_after_tool(
        {"execution_status": "evidence_requested", "evidence_requests": []}
    )

    assert service.attempt_transitions[-1].target_status.value == "retryable_error"
    assert service.transitions[-1].target_status == InspectionWorkItemStatus.RETRY_WAIT


async def test_lease_heartbeat_logs_successful_renewal(caplog) -> None:
    service = FakeService(_terminal_sync())
    attempt = InspectionWorkItemAttempt(
        service=service,
        lease=_lease(),
        consumer_agent_run_id="run-1",
        lease_seconds=3,
        retry_backoff_seconds=2,
    )
    stop_event = asyncio.Event()

    async def stop_after_first_renewal() -> None:
        while not service.renew_calls:
            await asyncio.sleep(0.01)
        stop_event.set()

    with caplog.at_level(logging.INFO):
        await asyncio.gather(
            attempt._renew_until_stopped(stop_event),
            stop_after_first_renewal(),
        )

    assert len(service.renew_calls) == 1
    assert any(
        "inspection_lease_renewed" in record.message
        and "work_item_id=work-1" in record.message
        for record in caplog.records
    )
