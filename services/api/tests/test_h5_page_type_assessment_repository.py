"""H5 页面类型评估 PostgreSQL 仓储合同测试。"""

from __future__ import annotations

import json

import pytest

from services.api.app.domain.exploration.page_type_assessment import (
    build_page_type_assessment_id,
)
from services.api.app.infrastructure.db.h5_page_type_assessment_repository import (
    H5PageTypeAssessmentPersistenceError,
    H5PageTypeAssessmentRepository,
)

_TASK_ID = "00000000-0000-0000-0000-000000000001"
_RUN_ID = "00000000-0000-0000-0000-000000000002"
_CAPTURE_ID = "00000000-0000-0000-0000-000000000003"
_OTHER_ID = "00000000-0000-0000-0000-000000000004"


def _accepted_payload(
    *,
    schema_version: str = "h5-page-assessment.v1",
    status: str = "covered",
    page_capture_id: str = _CAPTURE_ID,
) -> dict[str, object]:
    reason_code = {
        "covered": "h5_page_detection_covered",
        "requires_auth": "h5_page_requires_auth",
        "blocked_by_safety": "h5_page_blocked_by_safety",
        "not_found": "h5_page_not_found",
        "evidence_insufficient": "h5_page_detection_low_confidence",
        "not_applicable": "h5_page_not_applicable",
    }[status]
    return {
        "protocol_version": "h5-page-type-assessment-adapter.v1",
        "execution_status": "accepted",
        "retryable": False,
        "page_type_assessment": {
            "schema_version": schema_version,
            "page_type": "home",
            "status": status,
            "reason_code": reason_code,
            "page_capture_ids": [page_capture_id],
            "evidence_artifact_ids": ["artifact-home"],
            "exploration_action_ids": [],
            "page_states": ["stable"],
        },
    }


def test_assessment_record_hash_is_stable_across_mapping_order() -> None:
    """若 canonical JSON 排序被移除，重放同一事实会被错误识别为冲突。"""
    first = H5PageTypeAssessmentRepository.build_payload_hash(
        {
            "page_type": "home",
            "status": "covered",
            "reason_code": "h5_page_detection_covered",
        }
    )
    second = H5PageTypeAssessmentRepository.build_payload_hash(
        {
            "reason_code": "h5_page_detection_covered",
            "status": "covered",
            "page_type": "home",
        }
    )

    assert first == second


def test_payload_hash_is_ignored_recursively_when_building_canonical_hash() -> None:
    """若 wrapper 或 inner 的 caller hash 参与摘要，同一事实会产生自引用漂移。"""
    first = H5PageTypeAssessmentRepository.build_payload_hash(
        {
            "payload_hash": "caller-wrapper-a",
            "page_type_assessment": {
                "payload_hash": "caller-inner-a",
                "page_type": "home",
            },
        }
    )
    second = H5PageTypeAssessmentRepository.build_payload_hash(
        {
            "payload_hash": "caller-wrapper-b",
            "page_type_assessment": {
                "payload_hash": "caller-inner-b",
                "page_type": "home",
            },
        }
    )

    assert first == second


@pytest.mark.asyncio
async def test_caller_hash_and_record_identity_are_not_persisted_or_hashed() -> None:
    """若信任 caller identity/hash，重放可篡改自然身份或形成 hash 自引用。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))
    first = _accepted_payload()
    first["payload_hash"] = "caller-wrapper-a"
    first["assessment_record_id"] = "caller-record-a"
    first["page_type_assessment"]["payload_hash"] = "caller-inner-a"
    second = _accepted_payload()
    second["payload_hash"] = "caller-wrapper-b"
    second["assessment_record_id"] = "caller-record-b"
    second["page_type_assessment"]["payload_hash"] = "caller-inner-b"

    first_record = await repository.upsert(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5:run-1",
        page_capture_id=_CAPTURE_ID,
        detection_result={"detection_status": "recognized", "page_state": "stable"},
        assessment_payload=first,
    )
    second_record = await repository.upsert(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5:run-1",
        page_capture_id=_CAPTURE_ID,
        detection_result={"detection_status": "recognized", "page_state": "stable"},
        assessment_payload=second,
    )

    assert first_record.payload_hash == second_record.payload_hash
    assert first_record.assessment_record_id == second_record.assessment_record_id
    assert "payload_hash" not in connection.persisted_payloads[0]
    assert "assessment_record_id" not in connection.persisted_payloads[0]


@pytest.mark.parametrize("identity_location", ["wrapper", "inner"])
@pytest.mark.asyncio
async def test_caller_assessment_identity_is_hash_neutral_and_server_overwritten(
    identity_location: str,
) -> None:
    """若 caller assessment_id 参与事实，伪造值会改变 hash 或原样污染审计。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))
    expected_id = build_page_type_assessment_id(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        page_type="home",
        catalog_sha256="catalog-sha",
    )
    records = []
    for supplied_id in (None, expected_id, "page-type-assessment:forged"):
        payload = _accepted_payload()
        payload["page_type_assessment"]["catalog_sha256"] = "catalog-sha"
        target = payload if identity_location == "wrapper" else payload["page_type_assessment"]
        if supplied_id is not None:
            target["assessment_id"] = supplied_id
        records.append(
            await repository.upsert(
                task_id=_TASK_ID,
                agent_run_id=_RUN_ID,
                session_id="h5:run-1",
                page_capture_id=_CAPTURE_ID,
                detection_result={
                    "detection_status": "recognized",
                    "catalog_sha256": "catalog-sha",
                },
                assessment_payload=payload,
            )
        )

    assert {record.payload_hash for record in records} == {records[0].payload_hash}
    assert {record.assessment_id for record in records} == {expected_id}
    for persisted in connection.persisted_payloads:
        decoded = json.loads(persisted)
        assert decoded["assessment_id"] == expected_id
        assert decoded["adapter_result"]["page_type_assessment"]["assessment_id"] == expected_id
        assert "page-type-assessment:forged" not in persisted


@pytest.mark.asyncio
async def test_persisted_hash_covers_detection_columns() -> None:
    """若 hash 只覆盖 assessment JSON，检测列可绕过 row_version 被静默篡改。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))

    stable = await repository.upsert(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5:run-1",
        page_capture_id=_CAPTURE_ID,
        detection_result={"detection_status": "recognized", "page_state": "stable"},
        assessment_payload=_accepted_payload(),
    )
    changed = await repository.upsert(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5:run-1",
        page_capture_id=_CAPTURE_ID,
        detection_result={"detection_status": "recognized", "page_state": "loading"},
        assessment_payload=_accepted_payload(),
        expected_row_version=stable.row_version,
    )

    assert changed.payload_hash != stable.payload_hash


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("confidence", 0.61),
        ("risk_flags", ["authentication_required"]),
        ("evidence_artifact_ids", ["artifact-new"]),
        ("catalog_sha256", "catalog-new"),
        ("model", "detector-v2"),
    ],
)
@pytest.mark.asyncio
async def test_complete_detection_content_changes_persisted_hash(
    changed_field: str,
    changed_value: object,
) -> None:
    """若只保留 status/state，证据、Catalog 与 provenance 漂移不会进入并发事实。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))
    detection = {
        "detection_status": "recognized",
        "page_state": "stable",
        "confidence": 0.95,
        "risk_flags": [],
        "evidence_artifact_ids": ["artifact-home"],
        "catalog_sha256": "catalog-sha",
        "model": "detector-v1",
        "matched_catalog_signals": {"ocr_keywords": ["首页"], "dom_hints": ["main"]},
    }
    payload = _accepted_payload()
    payload["page_type_assessment"]["catalog_sha256"] = detection["catalog_sha256"]
    first = await repository.upsert(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5:run-1",
        page_capture_id=_CAPTURE_ID,
        detection_result=detection,
        assessment_payload=payload,
    )
    changed_detection = dict(detection)
    changed_detection[changed_field] = changed_value
    if changed_field == "catalog_sha256":
        changed_payload = _accepted_payload()
        changed_payload["page_type_assessment"]["catalog_sha256"] = changed_value
    else:
        changed_payload = payload
    changed = await repository.upsert(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5:run-1",
        page_capture_id=_CAPTURE_ID,
        detection_result=changed_detection,
        assessment_payload=changed_payload,
        expected_row_version=first.row_version,
    )

    assert changed.payload_hash != first.payload_hash
    persisted = json.loads(connection.persisted_payloads[-1])
    assert persisted["detection_result"][changed_field] == changed_value


@pytest.mark.asyncio
async def test_detection_mapping_order_does_not_change_persisted_hash() -> None:
    """完整 detection 进入 hash 后仍必须按递归键排序保持重放稳定。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))
    first = await repository.upsert(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5:run-1",
        page_capture_id=_CAPTURE_ID,
        detection_result={
            "detection_status": "recognized",
            "page_state": "stable",
            "matched_catalog_signals": {"ocr_keywords": ["首页"], "dom_hints": ["main"]},
        },
        assessment_payload=_accepted_payload(),
    )
    replay = await repository.upsert(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5:run-1",
        page_capture_id=_CAPTURE_ID,
        detection_result={
            "matched_catalog_signals": {"dom_hints": ["main"], "ocr_keywords": ["首页"]},
            "page_state": "stable",
            "detection_status": "recognized",
        },
        assessment_payload=_accepted_payload(),
    )

    assert replay.payload_hash == first.payload_hash


@pytest.mark.parametrize(
    "malformed_value",
    [float("nan"), {"unsupported"}],
)
@pytest.mark.asyncio
async def test_complete_detection_rejects_non_json_values_before_sql(
    malformed_value: object,
) -> None:
    """未持久化列也属于 detection 事实，非法 JSON 不得被裁剪后绕过校验。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))

    with pytest.raises((TypeError, ValueError), match="JSON"):
        await repository.upsert(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            session_id="h5:run-1",
            page_capture_id=_CAPTURE_ID,
            detection_result={
                "detection_status": "recognized",
                "page_state": "stable",
                "confidence": malformed_value,
            },
            assessment_payload=_accepted_payload(),
        )

    assert connection.calls == []


@pytest.mark.asyncio
async def test_upsert_rejects_mismatched_inner_capture_identity_before_sql() -> None:
    """若 inner capture 可漂移，数据库复合外键也无法证明评估引用的是输入 Capture。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))

    with pytest.raises(ValueError, match="page_capture_ids"):
        await repository.upsert(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            session_id="h5:run-1",
            page_capture_id=_CAPTURE_ID,
            detection_result={"detection_status": "recognized"},
            assessment_payload=_accepted_payload(page_capture_id=_OTHER_ID),
        )

    assert connection.calls == []


@pytest.mark.asyncio
async def test_upsert_rejects_mismatched_detection_capture_identity_before_sql() -> None:
    """若 detection 引用另一 Capture，不能把其判定落到当前自然身份。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))

    with pytest.raises(ValueError, match="detection_result.page_capture_id"):
        await repository.upsert(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            session_id="h5:run-1",
            page_capture_id=_CAPTURE_ID,
            detection_result={
                "page_capture_id": _OTHER_ID,
                "detection_status": "recognized",
            },
            assessment_payload=_accepted_payload(),
        )

    assert connection.calls == []


@pytest.mark.parametrize("identity_field", ["task_id", "agent_run_id"])
@pytest.mark.asyncio
async def test_upsert_rejects_mismatched_inner_owner_identity(identity_field: str) -> None:
    """若 payload owner 与参数不一致，调用方可把跨任务事实伪装成本任务评估。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))
    payload = _accepted_payload()
    payload["page_type_assessment"][identity_field] = _OTHER_ID

    with pytest.raises(ValueError, match=identity_field):
        await repository.upsert(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            session_id="h5:run-1",
            page_capture_id=_CAPTURE_ID,
            detection_result={"detection_status": "recognized"},
            assessment_payload=payload,
        )

    assert connection.calls == []


@pytest.mark.asyncio
async def test_upsert_rejects_non_string_json_object_key_before_sql() -> None:
    """JSON 对象键必须为字符串，禁止规范化时把不同输入静默折叠为同一键。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))
    payload = _accepted_payload()
    payload["page_type_assessment"][1] = "invalid-key"

    with pytest.raises(TypeError, match="JSON.*键"):
        await repository.upsert(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            session_id="h5:run-1",
            page_capture_id=_CAPTURE_ID,
            detection_result={"detection_status": "recognized"},
            assessment_payload=payload,
        )

    assert connection.calls == []


@pytest.mark.parametrize(
    ("task_id", "payload_mutator", "expected_message"),
    [
        ("not-a-uuid", None, "task_id"),
        (_TASK_ID, lambda payload: payload.update({"confidence": float("nan")}), "JSON"),
    ],
)
@pytest.mark.asyncio
async def test_upsert_rejects_malformed_uuid_or_non_json_payload(
    task_id: str,
    payload_mutator,
    expected_message: str,
) -> None:
    """若无效 UUID 或 NaN 穿透规范化，asyncpg/JSONB 会产生边界不一致。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))
    payload = _accepted_payload()
    if payload_mutator is not None:
        payload_mutator(payload["page_type_assessment"])

    with pytest.raises((TypeError, ValueError), match=expected_message):
        await repository.upsert(
            task_id=task_id,
            agent_run_id=_RUN_ID,
            session_id="h5:run-1",
            page_capture_id=_CAPTURE_ID,
            detection_result={"detection_status": "recognized"},
            assessment_payload=payload,
        )

    assert connection.calls == []


@pytest.mark.parametrize(
    ("adapter_payload", "expected_status", "expected_reason"),
    [
        (
            {
                "protocol_version": "h5-page-type-assessment-adapter.v1",
                "execution_status": "blocked",
                "retryable": True,
                "error_code": "PAGE_TYPE_ASSESSMENT_EVIDENCE_REQUIRED",
                "message": "缺少证据",
            },
            "evidence_insufficient",
            "PAGE_TYPE_ASSESSMENT_EVIDENCE_REQUIRED",
        ),
        (
            {
                "protocol_version": "h5-page-type-assessment-adapter.v1",
                "execution_status": "skipped",
                "retryable": False,
                "message": "生命周期页面不进入业务覆盖",
            },
            "not_applicable",
            "H5_PAGE_TYPE_ASSESSMENT_SKIPPED",
        ),
    ],
)
@pytest.mark.asyncio
async def test_repository_maps_nonaccepted_adapter_outcomes_to_auditable_status(
    adapter_payload: dict[str, object],
    expected_status: str,
    expected_reason: str,
) -> None:
    """若 blocked/skipped 无法映射到表状态，Workflow 仍会在发布前丢失审计记录。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))

    record = await repository.upsert(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5:run-1",
        page_capture_id=_CAPTURE_ID,
        detection_result={
            "page_capture_id": _CAPTURE_ID,
            "detection_status": "recognized",
            "page_type": "startup_ad",
            "page_state": "countdown",
        },
        assessment_payload=adapter_payload,
    )

    assert record.status == expected_status
    assert record.reason_codes == (expected_reason,)
    assert record.assessment_payload["adapter_result"]["execution_status"] in {
        "blocked",
        "skipped",
    }


@pytest.mark.parametrize("execution_status", ["blocked", "skipped"])
@pytest.mark.parametrize(
    ("identity_field", "identity_value"),
    [
        ("task_id", _OTHER_ID),
        ("agent_run_id", _OTHER_ID),
        ("page_capture_id", _OTHER_ID),
        ("page_capture_ids", [_OTHER_ID]),
    ],
)
@pytest.mark.asyncio
async def test_nonaccepted_nested_owner_identity_fails_closed(
    execution_status: str,
    identity_field: str,
    identity_value: object,
) -> None:
    """synthetic raw 不能绕过 blocked/skipped 原始 inner 的跨任务/Capture 身份校验。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))
    adapter_payload = {
        "protocol_version": "h5-page-type-assessment-adapter.v1",
        "execution_status": execution_status,
        "retryable": execution_status == "blocked",
        "page_type_assessment": {identity_field: identity_value},
    }

    with pytest.raises(ValueError, match=identity_field):
        await repository.upsert(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            session_id="h5:run-1",
            page_capture_id=_CAPTURE_ID,
            detection_result={
                "page_capture_id": _CAPTURE_ID,
                "detection_status": "recognized",
                "page_type": "startup_ad",
            },
            assessment_payload=adapter_payload,
        )

    assert connection.calls == []


@pytest.mark.parametrize("execution_status", ["blocked", "skipped"])
@pytest.mark.asyncio
async def test_nonaccepted_nested_server_identity_is_sanitized(
    execution_status: str,
) -> None:
    """nonaccepted inner 即使归属合法，也只能持久化服务器重建后的规范身份。"""
    connection = _RecordingConnection()
    repository = H5PageTypeAssessmentRepository(pool=_RecordingPool(connection))

    record = await repository.upsert(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5:run-1",
        page_capture_id=_CAPTURE_ID,
        detection_result={
            "page_capture_id": _CAPTURE_ID,
            "detection_status": "recognized",
            "page_type": "startup_ad",
        },
        assessment_payload={
            "protocol_version": "h5-page-type-assessment-adapter.v1",
            "execution_status": execution_status,
            "retryable": execution_status == "blocked",
            "page_type_assessment": {
                "task_id": _TASK_ID,
                "agent_run_id": _RUN_ID,
                "page_capture_ids": [_CAPTURE_ID],
                "assessment_id": "page-type-assessment:forged",
            },
        },
    )

    nested = record.assessment_payload["adapter_result"]["page_type_assessment"]
    assert nested == {
        "task_id": _TASK_ID,
        "agent_run_id": _RUN_ID,
        "page_capture_ids": [_CAPTURE_ID],
        "assessment_id": record.assessment_id,
    }
    assert "page-type-assessment:forged" not in json.dumps(
        record.assessment_payload,
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_upsert_rejects_weaker_or_older_conflicting_assessment() -> None:
    """若冲突分支允许旧 schema 或较弱终态覆盖，审计结论会被降级。"""
    repository = H5PageTypeAssessmentRepository(pool=_ConflictingPool())

    with pytest.raises(H5PageTypeAssessmentPersistenceError, match="冲突"):
        await repository.upsert(
            task_id="00000000-0000-0000-0000-000000000001",
            agent_run_id="00000000-0000-0000-0000-000000000002",
            session_id="h5:run-1",
            page_capture_id="00000000-0000-0000-0000-000000000003",
            detection_result={"detection_status": "recognized", "page_state": "stable"},
            assessment_payload={
                "schema_version": "h5-page-assessment.v1",
                "page_type": "home",
                "status": "evidence_insufficient",
                "reason_code": "h5_page_detection_low_confidence",
                "page_capture_ids": ["00000000-0000-0000-0000-000000000003"],
            },
        )


@pytest.mark.asyncio
async def test_upsert_requires_matching_row_version_for_mutating_replay() -> None:
    """若并发更新不携带当前 row_version，旧 Worker 可覆盖较新的评估事实。"""
    repository = H5PageTypeAssessmentRepository(pool=_ConflictingPool())

    with pytest.raises(ValueError, match="expected_row_version"):
        await repository.upsert(
            task_id="00000000-0000-0000-0000-000000000001",
            agent_run_id="00000000-0000-0000-0000-000000000002",
            session_id="h5:run-1",
            page_capture_id="00000000-0000-0000-0000-000000000003",
            detection_result={"detection_status": "recognized"},
            assessment_payload={
                "page_type": "home",
                "status": "covered",
                "reason_code": "h5_page_detection_covered",
            },
            expected_row_version=-1,
        )


@pytest.mark.asyncio
async def test_list_for_run_rejects_malformed_owner_uuid_before_sql() -> None:
    """读侧也必须在发送 SQL 前拒绝无效 UUID，避免依赖驱动隐式类型错误。"""
    connection = _MonotonicConnection()
    repository = H5PageTypeAssessmentRepository(pool=_MonotonicPool(connection))

    with pytest.raises(ValueError, match="task_id"):
        await repository.list_for_run(task_id="not-a-uuid", agent_run_id=_RUN_ID)


@pytest.mark.parametrize(
    ("first_schema", "first_status", "second_schema", "second_status"),
    [
        ("h5-page-assessment.v1", "covered", "h5-page-assessment.v2", "evidence_insufficient"),
        ("h5-page-assessment.v2", "covered", "h5-page-assessment.v1", "evidence_insufficient"),
    ],
)
@pytest.mark.asyncio
async def test_cross_schema_weaker_status_cannot_replace_effective_assessment(
    first_schema: str,
    first_status: str,
    second_schema: str,
    second_status: str,
) -> None:
    """若 conflict identity 包含 schema，较弱新行会成为 list_for_run 的错误有效投影。"""
    connection = _MonotonicConnection()
    repository = H5PageTypeAssessmentRepository(pool=_MonotonicPool(connection))
    await _upsert_assessment(
        repository,
        schema_version=first_schema,
        status=first_status,
    )

    await _upsert_assessment(
        repository,
        schema_version=second_schema,
        status=second_status,
    )

    [stored] = await repository.list_for_run(task_id=_TASK_ID, agent_run_id=_RUN_ID)
    assert stored.status == "covered"
    assert stored.schema_version == first_schema
    assert len(connection.rows) == 2


@pytest.mark.asyncio
async def test_cross_schema_upgrade_can_preserve_strong_status_with_row_version() -> None:
    """同一 Capture 的 schema 升级必须新增审计版本，不能原地销毁旧事实。"""
    connection = _MonotonicConnection()
    repository = H5PageTypeAssessmentRepository(pool=_MonotonicPool(connection))
    first = await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v1",
        status="covered",
    )

    upgraded = await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v2",
        status="covered",
    )

    assert upgraded.assessment_record_id != first.assessment_record_id
    assert upgraded.schema_version == "h5-page-assessment.v2"
    assert upgraded.status == "covered"
    assert upgraded.row_version == 0
    assert len(connection.rows) == 2


@pytest.mark.parametrize(
    "terminal_status",
    ["not_applicable", "requires_auth", "blocked_by_safety"],
)
@pytest.mark.asyncio
async def test_terminal_status_cannot_be_replaced_by_insufficient_evidence(
    terminal_status: str,
) -> None:
    """终态不是线性 rank；尤其 not_applicable 不得被较新的证据不足降级。"""
    connection = _MonotonicConnection()
    repository = H5PageTypeAssessmentRepository(pool=_MonotonicPool(connection))
    await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v1",
        status=terminal_status,
    )

    await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v2",
        status="evidence_insufficient",
    )
    [effective] = await repository.list_for_run(task_id=_TASK_ID, agent_run_id=_RUN_ID)
    assert effective.status == terminal_status
    assert len(connection.rows) == 2


@pytest.mark.asyncio
async def test_incomparable_terminal_schema_versions_preserve_history() -> None:
    """schema 演进允许新结论成为投影，但旧终态必须仍可审计。"""
    connection = _MonotonicConnection()
    repository = H5PageTypeAssessmentRepository(pool=_MonotonicPool(connection))
    first = await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v1",
        status="requires_auth",
    )

    await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v2",
        status="blocked_by_safety",
    )
    [effective] = await repository.list_for_run(task_id=_TASK_ID, agent_run_id=_RUN_ID)
    assert effective.status == "blocked_by_safety"
    assert effective.assessment_record_id != first.assessment_record_id
    assert len(connection.rows) == 2


@pytest.mark.asyncio
async def test_not_found_can_be_strengthened_to_a_terminal_conclusion() -> None:
    """偏序仍允许在获得明确引用后把 not_found 提升为终态。"""
    connection = _MonotonicConnection()
    repository = H5PageTypeAssessmentRepository(pool=_MonotonicPool(connection))
    await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v1",
        status="not_found",
    )

    updated = await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v2",
        status="requires_auth",
    )

    assert updated.status == "requires_auth"
    assert updated.row_version == 0
    assert len(connection.rows) == 2


@pytest.mark.asyncio
async def test_stronger_older_schema_is_auditable_without_overwriting_newer_row() -> None:
    """版本历史与有效投影分离；强结论可保底，但不得覆写新 schema 行。"""
    connection = _MonotonicConnection()
    repository = H5PageTypeAssessmentRepository(pool=_MonotonicPool(connection))
    await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v2",
        status="evidence_insufficient",
    )

    await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v1",
        status="covered",
    )
    [effective] = await repository.list_for_run(task_id=_TASK_ID, agent_run_id=_RUN_ID)
    assert effective.status == "covered"
    assert effective.schema_version == "h5-page-assessment.v1"
    assert len(connection.rows) == 2


@pytest.mark.asyncio
async def test_identical_cross_worker_replay_is_idempotent_without_expected_row_version() -> None:
    """若同 hash 重放仍递增版本，并发 Worker 会制造无意义版本冲突。"""
    connection = _MonotonicConnection()
    repository = H5PageTypeAssessmentRepository(pool=_MonotonicPool(connection))
    first = await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v2",
        status="covered",
    )
    replay = await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v2",
        status="covered",
    )

    assert replay.payload_hash == first.payload_hash
    assert replay.row_version == first.row_version == 0


@pytest.mark.asyncio
async def test_stale_row_version_cannot_mutate_effective_assessment() -> None:
    """若 SQL 不原子比较 row_version，两个 Worker 可依次覆盖同一自然身份。"""
    connection = _MonotonicConnection()
    repository = H5PageTypeAssessmentRepository(pool=_MonotonicPool(connection))
    first = await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v2",
        status="evidence_insufficient",
    )
    await _upsert_assessment(
        repository,
        schema_version="h5-page-assessment.v2",
        status="covered",
        expected_row_version=first.row_version,
    )

    with pytest.raises(H5PageTypeAssessmentPersistenceError, match="冲突"):
        await _upsert_assessment(
            repository,
            schema_version="h5-page-assessment.v2",
            status="requires_auth",
            expected_row_version=first.row_version,
        )


async def _upsert_assessment(
    repository: H5PageTypeAssessmentRepository,
    *,
    schema_version: str,
    status: str,
    expected_row_version: int | None = None,
):
    return await repository.upsert(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5:run-1",
        page_capture_id=_CAPTURE_ID,
        detection_result={"detection_status": "recognized", "page_state": "stable"},
        assessment_payload=_accepted_payload(
            schema_version=schema_version,
            status=status,
        ),
        expected_row_version=expected_row_version,
    )


class _ConflictingConnection:
    async def fetchrow(self, query: str, *arguments: object):
        assert "ON CONFLICT" in query
        assert "row_version" in query
        assert "payload_hash" in query
        return None


class _Acquire:
    async def __aenter__(self) -> _ConflictingConnection:
        return _ConflictingConnection()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _ConflictingPool:
    def acquire(self) -> _Acquire:
        return _Acquire()


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.persisted_payloads: list[str] = []

    async def fetchrow(self, query: str, *arguments: object):
        self.calls.append((query, arguments))
        self.persisted_payloads.append(str(arguments[12]))
        return {
            "assessment_record_id": arguments[0],
            "assessment_id": arguments[1],
            "task_id": arguments[2],
            "agent_run_id": arguments[3],
            "session_id": arguments[4],
            "page_capture_id": arguments[5],
            "detection_status": arguments[6],
            "page_type": arguments[7],
            "page_state": arguments[8],
            "status": arguments[9],
            "reason_codes": json.loads(str(arguments[10])),
            "safety_summary": json.loads(str(arguments[11])),
            "assessment_payload": json.loads(str(arguments[12])),
            "schema_version": arguments[13],
            "payload_hash": arguments[14],
            "row_version": 0,
        }


class _RecordingAcquire:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _RecordingConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _RecordingPool:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    def acquire(self) -> _RecordingAcquire:
        return _RecordingAcquire(self._connection)


def _status_can_advance(*, existing: str, incoming: str) -> bool:
    if existing == incoming or existing == "evidence_insufficient":
        return True
    return existing == "not_found" and incoming in {
        "covered",
        "requires_auth",
        "blocked_by_safety",
        "not_applicable",
    }


def _schema_number(schema_version: str) -> int:
    return int(schema_version.rsplit(".v", maxsplit=1)[1])


def _status_rank(status: str) -> int:
    if status in {"covered", "requires_auth", "blocked_by_safety", "not_applicable"}:
        return 3
    if status == "not_found":
        return 2
    return 1


class _MonotonicConnection:
    """在测试内执行 SQL 合同的自然身份、状态偏序和版本语义。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, object]] = {}

    async def fetchrow(self, query: str, *arguments: object):
        assert "ON CONFLICT (page_capture_id, schema_version)" in query
        assert "existing.row_version = $16::bigint" in query
        assert "schema_version = EXCLUDED.schema_version" not in query
        assert "existing.status = 'evidence_insufficient'" in query
        assert "CASE EXCLUDED.status" not in query
        incoming = {
            "assessment_record_id": arguments[0],
            "assessment_id": arguments[1],
            "task_id": arguments[2],
            "agent_run_id": arguments[3],
            "session_id": arguments[4],
            "page_capture_id": arguments[5],
            "detection_status": arguments[6],
            "page_type": arguments[7],
            "page_state": arguments[8],
            "status": arguments[9],
            "reason_codes": json.loads(str(arguments[10])),
            "safety_summary": json.loads(str(arguments[11])),
            "assessment_payload": json.loads(str(arguments[12])),
            "schema_version": arguments[13],
            "payload_hash": arguments[14],
            "row_version": 0,
        }
        expected_row_version = arguments[15]
        key = (str(incoming["page_capture_id"]), str(incoming["schema_version"]))
        row = self.rows.get(key)
        if row is None:
            self.rows[key] = incoming
        elif row["payload_hash"] == incoming["payload_hash"]:
            pass
        elif (
            expected_row_version is not None
            and row["row_version"] == expected_row_version
            and _status_can_advance(
                existing=str(row["status"]),
                incoming=str(incoming["status"]),
            )
        ):
            incoming["row_version"] = int(row["row_version"]) + 1
            self.rows[key] = incoming
        else:
            return None
        return dict(self.rows[key])

    async def fetch(self, query: str, *arguments: object):
        assert "PARTITION BY page_capture_id" in query
        matching = [
            row
            for row in self.rows.values()
            if row["task_id"] == arguments[0] and row["agent_run_id"] == arguments[1]
        ]
        if not matching:
            return []
        effective = max(
            matching,
            key=lambda row: (
                _status_rank(str(row["status"])),
                _schema_number(str(row["schema_version"])),
                int(row["row_version"]),
            ),
        )
        return [dict(effective)]


class _MonotonicAcquire:
    def __init__(self, connection: _MonotonicConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _MonotonicConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _MonotonicPool:
    def __init__(self, connection: _MonotonicConnection) -> None:
        self._connection = connection

    def acquire(self) -> _MonotonicAcquire:
        return _MonotonicAcquire(self._connection)
