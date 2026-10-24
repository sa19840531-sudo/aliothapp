import hashlib
import json
from typing import Any

import pytest

from services.api.app.application.rule_judge import (
    RuleJudgeEvidenceInsufficientError,
    RuleJudgeValidationError,
)
from services.api.app.infrastructure.rule_judge_evidence import (
    PostgresRuleJudgeEvidenceReader,
)


class FakeAcquire:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    async def __aenter__(self) -> "FakeConnection":
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        packet_row: dict[str, Any] | None = None,
    ) -> None:
        self.connection = FakeConnection(rows, packet_row=packet_row)

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeConnection:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        packet_row: dict[str, Any] | None,
    ) -> None:
        self.rows = rows
        self.packet_row = packet_row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((" ".join(query.split()), args))
        return self.rows

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((" ".join(query.split()), args))
        if (
            self.packet_row is not None
            and "INSERT INTO qc_observation_packet" in query
            and set(self.packet_row) == {"observation_packet_id"}
        ):
            return {
                **self.packet_row,
                "evidence_artifact_ids": args[3],
                "packet_payload": args[4],
                "quality_gate_status": args[6],
                "quality_gate_reason": args[7],
            }
        return self.packet_row

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append((" ".join(query.split()), args))
        if "FROM qc_observation_packet" in query:
            return None
        return None


class FakeArtifactStore:
    def __init__(self, payload: bytes | dict[str, bytes]) -> None:
        self.payload = payload
        self.read_calls: list[str] = []

    async def read(self, storage_path: str) -> bytes:
        self.read_calls.append(storage_path)
        if isinstance(self.payload, dict):
            return self.payload[storage_path]
        return self.payload


def _artifact_row(
    payload: bytes,
    *,
    artifact_id: str = "artifact-1",
    page_capture_id: str = "capture-1",
    artifact_type: str = "visible_text",
    content_type: str = "text/plain; charset=utf-8",
) -> dict[str, Any]:
    extension = "txt" if artifact_type == "visible_text" else "json"
    return {
        "artifact_id": artifact_id,
        "page_capture_id": page_capture_id,
        "artifact_type": artifact_type,
        "source": "baseline",
        "storage_path": f"task-1/{page_capture_id}/{artifact_id}.{extension}",
        "content_type": content_type,
        "checksum": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "size_bytes": len(payload),
    }


async def test_rule_judge_evidence_reader_loads_owned_visible_text() -> None:
    payload = "真实页面文本".encode()
    pool = FakePool([_artifact_row(payload)])
    store = FakeArtifactStore(payload)
    reader = PostgresRuleJudgeEvidenceReader(pool=pool, artifact_store=store)

    result = await reader.read_visible_text(
        task_id="task-1",
        page_capture_id="capture-1",
        evidence_artifact_ids=("artifact-1", "artifact-2"),
    )

    query, args = pool.connection.calls[0]
    assert "capture.task_id = $2::uuid" in query
    assert "evidence.id = ANY($3::uuid[])" in query
    assert "evidence.artifact_type = 'visible_text'" in query
    assert args == ("capture-1", "task-1", ["artifact-1", "artifact-2"])
    assert store.read_calls == ["task-1/capture-1/artifact-1.txt"]
    assert result == "真实页面文本"


async def test_rule_judge_evidence_reader_treats_empty_visible_text_as_insufficient() -> None:
    payload = b""
    reader = PostgresRuleJudgeEvidenceReader(
        pool=FakePool([_artifact_row(payload)]),
        artifact_store=FakeArtifactStore(payload),
    )

    with pytest.raises(RuleJudgeEvidenceInsufficientError, match="证据内容为空"):
        await reader.read_visible_text(
            task_id="task-1",
            page_capture_id="capture-1",
            evidence_artifact_ids=("artifact-1",),
        )


async def test_rule_judge_evidence_reader_rejects_missing_visible_text() -> None:
    reader = PostgresRuleJudgeEvidenceReader(
        pool=FakePool([]),
        artifact_store=FakeArtifactStore(b""),
    )

    with pytest.raises(RuleJudgeValidationError, match="唯一的 visible_text"):
        await reader.read_visible_text(
            task_id="task-1",
            page_capture_id="capture-1",
            evidence_artifact_ids=("artifact-1",),
        )


async def test_rule_judge_evidence_reader_rejects_checksum_mismatch() -> None:
    payload = "真实页面文本".encode()
    row = _artifact_row(payload)
    row["checksum"] = "sha256:" + "0" * 64
    reader = PostgresRuleJudgeEvidenceReader(
        pool=FakePool([row]),
        artifact_store=FakeArtifactStore(payload),
    )

    with pytest.raises(RuleJudgeValidationError, match="checksum"):
        await reader.read_visible_text(
            task_id="task-1",
            page_capture_id="capture-1",
            evidence_artifact_ids=("artifact-1",),
        )


async def test_rule_judge_evidence_reader_rejects_storage_path_mismatch() -> None:
    payload = "真实页面文本".encode()
    row = _artifact_row(payload)
    row["storage_path"] = "other-task/capture-1/artifact-1.txt"
    reader = PostgresRuleJudgeEvidenceReader(
        pool=FakePool([row]),
        artifact_store=FakeArtifactStore(payload),
    )

    with pytest.raises(RuleJudgeValidationError, match="存储路径归属"):
        await reader.read_visible_text(
            task_id="task-1",
            page_capture_id="capture-1",
            evidence_artifact_ids=("artifact-1",),
        )


async def test_rule_judge_evidence_reader_creates_multi_capture_packet() -> None:
    visible_payload = "操作前页面".encode()
    trace_payload = b'[{"action":"click","result":"success"}]'
    after_visible_payload = "操作后页面".encode()
    rows = [
        _artifact_row(visible_payload),
        _artifact_row(
            trace_payload,
            artifact_id="artifact-2",
            page_capture_id="capture-2",
            artifact_type="action_trace",
            content_type="application/json",
        ),
        _artifact_row(
            after_visible_payload,
            artifact_id="artifact-3",
            page_capture_id="capture-2",
            artifact_type="visible_text",
        ),
    ]
    pool = FakePool(rows, packet_row={"observation_packet_id": "packet-1"})
    store = FakeArtifactStore(
        {
            str(rows[0]["storage_path"]): visible_payload,
            str(rows[1]["storage_path"]): trace_payload,
            str(rows[2]["storage_path"]): after_visible_payload,
        }
    )
    reader = PostgresRuleJudgeEvidenceReader(pool=pool, artifact_store=store)

    bundle = await reader.prepare_observation_packet(
        task_id="task-1",
        agent_run_id="run-1",
        rule_id="rule-1",
        page_capture_id="capture-1",
        evidence_capture_roles={"primary": "capture-1", "after": "capture-2"},
        evidence_artifact_ids=("artifact-1", "artifact-2", "artifact-3"),
        required_artifact_types=("visible_text", "action_trace"),
    )

    assert bundle.observation_packet_id == "packet-1"
    assert bundle.capture_roles == {"primary": "capture-1", "after": "capture-2"}
    assert [item.capture_role for item in bundle.artifacts] == ["primary", "after", "after"]
    assert bundle.require_unique("visible_text").decode_text() == "操作前页面"
    assert bundle.require_unique("action_trace").decode_json()[0]["action"] == "click"
    artifact_query, artifact_args = pool.connection.calls[0]
    assert "capture.id = ANY($2::uuid[])" in artifact_query
    assert artifact_args == (
        "task-1",
        ["capture-1", "capture-2"],
        ["artifact-1", "artifact-2", "artifact-3"],
    )
    packet_query, packet_args = pool.connection.calls[1]
    assert "INSERT INTO qc_observation_packet" in packet_query
    assert "ON CONFLICT" in packet_query
    assert "DO UPDATE SET updated_at = packet.updated_at" in packet_query
    assert "input_fingerprint IS NOT NULL" in packet_query
    assert packet_args[0:3] == ("task-1", "rule-1", "capture-1")
    assert packet_args[5] == "run-1"
    assert len(packet_args[8]) == 64


async def test_read_batch_reuses_bytes_but_creates_packet_for_each_rule() -> None:
    payload = "同一份页面证据".encode()
    row = _artifact_row(payload)
    pool = FakePool([row], packet_row={"observation_packet_id": "packet-1"})
    store = FakeArtifactStore(payload)
    reader = PostgresRuleJudgeEvidenceReader(pool=pool, artifact_store=store)

    with reader.read_batch():
        first = await reader.prepare_observation_packet(
            task_id="task-1",
            agent_run_id="run-1",
            rule_id="rule-1",
            page_capture_id="capture-1",
            evidence_capture_roles={"primary": "capture-1"},
            evidence_artifact_ids=("artifact-1",),
            required_artifact_types=("visible_text",),
        )
        second = await reader.prepare_observation_packet(
            task_id="task-1",
            agent_run_id="run-1",
            rule_id="rule-2",
            page_capture_id="capture-1",
            evidence_capture_roles={"primary": "capture-1"},
            evidence_artifact_ids=("artifact-1",),
            required_artifact_types=("visible_text",),
        )

    assert first.observation_packet_id == second.observation_packet_id == "packet-1"
    artifact_queries = [
        call for call in pool.connection.calls if "FROM qc_evidence_artifact" in call[0]
    ]
    packet_calls = [
        call for call in pool.connection.calls if "INSERT INTO qc_observation_packet" in call[0]
    ]
    assert len(artifact_queries) == 2
    assert len(packet_calls) == 2
    assert [call[1][1] for call in packet_calls] == ["rule-1", "rule-2"]
    assert store.read_calls == [row["storage_path"]]


async def test_read_batch_does_not_bypass_changed_storage_path_ownership() -> None:
    payload = "归属校验".encode()
    row = _artifact_row(payload)
    pool = FakePool([row], packet_row={"observation_packet_id": "packet-1"})
    store = FakeArtifactStore(payload)
    reader = PostgresRuleJudgeEvidenceReader(pool=pool, artifact_store=store)

    with reader.read_batch():
        await reader.prepare_observation_packet(
            task_id="task-1",
            agent_run_id="run-1",
            rule_id="rule-1",
            page_capture_id="capture-1",
            evidence_capture_roles={"primary": "capture-1"},
            evidence_artifact_ids=("artifact-1",),
            required_artifact_types=("visible_text",),
        )
        pool.connection.rows[0] = {
            **row,
            "storage_path": "task-2/capture-1/artifact-1.txt",
        }
        with pytest.raises(RuleJudgeValidationError, match="存储路径归属"):
            await reader.prepare_observation_packet(
                task_id="task-1",
                agent_run_id="run-1",
                rule_id="rule-2",
                page_capture_id="capture-1",
                evidence_capture_roles={"primary": "capture-1"},
                evidence_artifact_ids=("artifact-1",),
                required_artifact_types=("visible_text",),
            )

    assert store.read_calls == [row["storage_path"]]


async def test_read_batch_reloads_and_revalidates_changed_checksum() -> None:
    payload = "摘要变化".encode()
    row = _artifact_row(payload)
    pool = FakePool([row], packet_row={"observation_packet_id": "packet-1"})
    store = FakeArtifactStore(payload)
    reader = PostgresRuleJudgeEvidenceReader(pool=pool, artifact_store=store)

    with reader.read_batch():
        await reader.prepare_observation_packet(
            task_id="task-1",
            agent_run_id="run-1",
            rule_id="rule-1",
            page_capture_id="capture-1",
            evidence_capture_roles={"primary": "capture-1"},
            evidence_artifact_ids=("artifact-1",),
            required_artifact_types=("visible_text",),
        )
        pool.connection.rows[0] = {
            **row,
            "checksum": "sha256:" + "0" * 64,
        }
        with pytest.raises(RuleJudgeValidationError, match="checksum"):
            await reader.prepare_observation_packet(
                task_id="task-1",
                agent_run_id="run-1",
                rule_id="rule-2",
                page_capture_id="capture-1",
                evidence_capture_roles={"primary": "capture-1"},
                evidence_artifact_ids=("artifact-1",),
                required_artifact_types=("visible_text",),
            )

    assert store.read_calls == [row["storage_path"], row["storage_path"]]


async def test_rule_judge_evidence_reader_rejects_cross_run_capture_roles() -> None:
    pool = FakePool(
        [{"capture_id": "capture-1", "agent_run_id": "another-producer-run"}]
    )
    reader = PostgresRuleJudgeEvidenceReader(
        pool=pool,
        artifact_store=FakeArtifactStore(b""),
    )

    with pytest.raises(RuleJudgeValidationError, match="生产 Run"):
        await reader.prepare_observation_packet(
            task_id="task-1",
            agent_run_id="consumer-run-1",
            source_agent_run_id="producer-run-1",
            rule_id="rule-1",
            page_capture_id="capture-1",
            evidence_capture_roles=None,
            evidence_artifact_ids=(),
            required_artifact_types=(),
        )


async def test_rule_judge_evidence_reader_rejects_frozen_packet_drift() -> None:
    payload = "页面文本".encode()
    pool = FakePool(
        [_artifact_row(payload)],
        packet_row={
            "observation_packet_id": "packet-1",
            "evidence_artifact_ids": json.dumps(["artifact-drifted"]),
            "packet_payload": json.dumps({"schema_version": "drifted"}),
            "quality_gate_status": "pass",
            "quality_gate_reason": "证据归属、类型、路径、大小和摘要校验通过",
        },
    )
    reader = PostgresRuleJudgeEvidenceReader(
        pool=pool,
        artifact_store=FakeArtifactStore(payload),
    )

    with pytest.raises(RuleJudgeValidationError, match="已冻结"):
        await reader.prepare_observation_packet(
            task_id="task-1",
            agent_run_id="consumer-run-1",
            rule_id="rule-1",
            page_capture_id="capture-1",
            evidence_capture_roles=None,
            evidence_artifact_ids=(),
            required_artifact_types=("visible_text",),
        )


async def test_rule_judge_evidence_reader_resolves_trusted_page_artifacts_when_omitted() -> None:
    payload = "页面加载正常".encode()
    pool = FakePool(
        [_artifact_row(payload)],
        packet_row={"observation_packet_id": "packet-1"},
    )
    reader = PostgresRuleJudgeEvidenceReader(
        pool=pool,
        artifact_store=FakeArtifactStore(payload),
    )

    bundle = await reader.prepare_observation_packet(
        task_id="task-1",
        agent_run_id="run-1",
        rule_id="rule-1",
        page_capture_id="capture-1",
        evidence_capture_roles=None,
        evidence_artifact_ids=(),
        required_artifact_types=("visible_text",),
    )

    artifact_query, artifact_args = pool.connection.calls[0]
    assert "cardinality($3::uuid[]) = 0" in artifact_query
    assert artifact_args == ("task-1", ["capture-1"], [])
    assert [item.artifact_id for item in bundle.artifacts] == ["artifact-1"]


async def test_missing_required_type_reports_after_loading_other_artifacts() -> None:
    """Capture 有其他可信制品但缺少契约类型时，必须报告缺少的类型。"""
    payload = b'{"nodes": []}'
    row = _artifact_row(
        payload,
        artifact_type="screenshot",
        content_type="application/json",
    )
    pool = FakePool([row])
    reader = PostgresRuleJudgeEvidenceReader(
        pool=pool,
        artifact_store=FakeArtifactStore(payload),
    )

    with pytest.raises(
        RuleJudgeEvidenceInsufficientError,
        match="规则判定缺少必需证据类型：visible_text",
    ) as excinfo:
        await reader.prepare_observation_packet(
            task_id="task-1",
            agent_run_id="run-1",
            rule_id="rule-1",
            page_capture_id="capture-1",
            evidence_capture_roles=None,
            evidence_artifact_ids=(),
            required_artifact_types=("visible_text",),
        )

    assert "证据采集角色未引用有效证据" not in str(excinfo.value)
    query, _ = pool.connection.calls[0]
    assert "evidence.artifact_type = ANY($4::varchar[])" not in query


async def test_rule_judge_evidence_reader_rejects_invalid_primary_role() -> None:
    reader = PostgresRuleJudgeEvidenceReader(
        pool=FakePool([]),
        artifact_store=FakeArtifactStore(b""),
    )

    with pytest.raises(RuleJudgeValidationError, match="primary"):
        await reader.prepare_observation_packet(
            task_id="task-1",
            agent_run_id="run-1",
            rule_id="rule-1",
            page_capture_id="capture-1",
            evidence_capture_roles={"primary": "capture-2"},
            evidence_artifact_ids=("artifact-1",),
            required_artifact_types=("visible_text",),
        )


async def test_rule_judge_evidence_reader_rejects_missing_required_type() -> None:
    payload = "页面文本".encode()
    reader = PostgresRuleJudgeEvidenceReader(
        pool=FakePool([_artifact_row(payload)]),
        artifact_store=FakeArtifactStore(payload),
    )

    with pytest.raises(RuleJudgeEvidenceInsufficientError, match="action_trace"):
        await reader.prepare_observation_packet(
            task_id="task-1",
            agent_run_id="run-1",
            rule_id="rule-1",
            page_capture_id="capture-1",
            evidence_capture_roles=None,
            evidence_artifact_ids=("artifact-1",),
            required_artifact_types=("visible_text", "action_trace"),
        )


async def test_rule_judge_evidence_reader_keeps_all_artifacts_for_empty_contract() -> None:
    """规则未声明必需证据类型时仍读取 Capture 下全部可信制品。"""
    payload = "页面文本".encode()
    pool = FakePool(
        [_artifact_row(payload)],
        packet_row={"observation_packet_id": "packet-1"},
    )
    reader = PostgresRuleJudgeEvidenceReader(
        pool=pool,
        artifact_store=FakeArtifactStore(payload),
    )

    bundle = await reader.prepare_observation_packet(
        task_id="task-1",
        agent_run_id="run-1",
        rule_id="rule-1",
        page_capture_id="capture-1",
        evidence_capture_roles=None,
        evidence_artifact_ids=(),
        required_artifact_types=(),
    )

    artifact_query, artifact_args = pool.connection.calls[0]
    assert "evidence.artifact_type = ANY($4::varchar[])" not in artifact_query
    assert artifact_args == ("task-1", ["capture-1"], [])
    assert [item.artifact_id for item in bundle.artifacts] == ["artifact-1"]


class PresentationAwareConnection(FakeConnection):
    """按 SQL 分流：附件查询回附件行，采集查询回持久化的展示层评估行。"""

    def __init__(self, rows, *, packet_row, capture_rows) -> None:
        super().__init__(rows, packet_row=packet_row)
        self.capture_rows = capture_rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((" ".join(query.split()), args))
        if "capture.metadata" in query:
            return self.capture_rows
        return self.rows


async def test_rule_judge_evidence_reader_returns_capture_presentations() -> None:
    payload = "真实页面文本".encode()
    presentation = {
        "protocol_version": "presentation-stack.v1",
        "topmost_layer_kind": "page",
        "base_page_visibility": "visible",
        "blocking_scope": "none",
        "mutation_mode": "instrumented_diagnostic",
        "hit_test_status": "available",
        "evidence_sources": ["h5_ui_snapshot", "h5_action_log"],
        "diagnostic_operations": [
            {"kind": "style_hide_obstruction", "source": "verified_high_z_obstruction_hide"}
        ],
        "uncertainty": [],
    }
    pool = FakePool([_artifact_row(payload)], packet_row={"observation_packet_id": "packet-1"})
    pool.connection = PresentationAwareConnection(
        [_artifact_row(payload)],
        packet_row={"observation_packet_id": "packet-1"},
        capture_rows=[
            {
                "capture_id": "capture-1",
                "surface_kind": "web",
                "metadata": json.dumps(
                    {"presentation": presentation, "page_state": "stable"}
                ),
            }
        ],
    )
    reader = PostgresRuleJudgeEvidenceReader(pool=pool, artifact_store=FakeArtifactStore(payload))

    bundle = await reader.prepare_observation_packet(
        task_id="task-1",
        agent_run_id="run-1",
        rule_id="rule-1",
        page_capture_id="capture-1",
        evidence_capture_roles={"primary": "capture-1"},
        evidence_artifact_ids=("artifact-1",),
        required_artifact_types=("visible_text",),
    )

    assert set(bundle.capture_presentations) == {"capture-1"}
    assessment = bundle.capture_presentations["capture-1"]
    assert assessment is not None
    assert assessment.mutation_mode.value == "instrumented_diagnostic"
    capture_query, capture_args = next(
        call for call in pool.connection.calls if "capture.metadata FROM qc_capture" in call[0]
    )
    assert "qc_page_capture" not in capture_query
    assert capture_args == ("task-1", ["capture-1"])
    # 附件查询仍是第一条读取，既有断言顺序不受影响。
    assert "qc_evidence_artifact" in pool.connection.calls[0][0]
