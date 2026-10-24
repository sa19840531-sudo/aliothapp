"""证据查询 Tool 改走清单来源：清单优先、附件表兜底、来源可见。"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

from services.api.app.agents.tools import action_evidence_tools
from services.api.app.agents.tools.action_evidence_tools import (
    _capture_from_row,
    _capture_payload,
)

NOW = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"


def _artifact(
    *,
    artifact_id: str,
    artifact_type: str,
    with_checksum: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "storage_path": f"{TASK_ID}/{CAPTURE_ID}/{artifact_id}",
        "task_id": TASK_ID,
        "page_capture_id": CAPTURE_ID,
        "created_at": NOW,
    }
    if with_checksum:
        payload["checksum"] = "sha256:" + "a" * 64
        payload["size_bytes"] = 128
    return payload


def _row(
    *,
    manifest_artifacts: list[dict[str, Any]] | None,
    table_artifacts: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    return {
        "before_capture_id": CAPTURE_ID,
        "before_task_id": TASK_ID,
        "before_surface_kind": "web",
        "before_action_execution_id": "action-001",
        "before_capture_role": "before_action",
        "before_capture_status": "captured",
        "before_captured_at": NOW,
        "before_url": "https://example.com/list",
        "before_page_type": "product_list",
        "before_action_log": [],
        "before_artifacts": table_artifacts,
        "before_manifest_artifacts": manifest_artifacts,
        "before_manifest_id": (
            "evidence-manifest:abc" if manifest_artifacts else None
        ),
        "before_manifest_version": 2 if manifest_artifacts else None,
        "before_manifest_quality_status": "pass" if manifest_artifacts else None,
    }


def test_query_reads_artifacts_from_manifest_payload() -> None:
    """SQL 层从清单载荷取附件，而不是只查附件表。"""
    source = inspect.getsource(action_evidence_tools._query_action_evidence)

    assert "FROM qc_evidence_manifest AS latest" in source
    assert "latest.manifest_payload -> 'artifacts'" in source
    assert "ORDER BY latest.manifest_version DESC" in source
    # 清单附件带 checksum，是受裁决约束的可校验引用
    assert "'checksum', item ->> 'checksum'" in source


def test_manifest_artifacts_win_over_artifact_table() -> None:
    capture = _capture_from_row(
        _row(
            manifest_artifacts=[
                _artifact(
                    artifact_id="from-manifest",
                    artifact_type="dom",
                    with_checksum=True,
                )
            ],
            table_artifacts=[
                _artifact(artifact_id="from-table", artifact_type="dom")
            ],
        ),
        prefix="before",
    )

    assert capture is not None
    assert capture["evidence_source"] == "evidence_manifest"
    assert [item["artifact_id"] for item in capture["artifacts"]] == ["from-manifest"]
    assert capture["manifest_id"] == "evidence-manifest:abc"
    assert capture["manifest_quality_status"] == "pass"


def test_falls_back_to_artifact_table_without_manifest() -> None:
    """清单缺席（如 App 旁路写入失败）时不能让探索瞎掉，回退并标注来源。"""
    capture = _capture_from_row(
        _row(
            manifest_artifacts=None,
            table_artifacts=[
                _artifact(artifact_id="from-table", artifact_type="dom")
            ],
        ),
        prefix="before",
    )

    assert capture is not None
    assert capture["evidence_source"] == "artifact_table"
    assert [item["artifact_id"] for item in capture["artifacts"]] == ["from-table"]
    assert capture["manifest_id"] is None


def test_capture_payload_surfaces_manifest_identity() -> None:
    snapshot = {
        "task_id": TASK_ID,
        "action_execution_id": "action-001",
        "before": _capture_from_row(
            _row(
                manifest_artifacts=[
                    _artifact(
                        artifact_id="a-dom",
                        artifact_type="dom",
                        with_checksum=True,
                    ),
                    _artifact(
                        artifact_id="a-shot",
                        artifact_type="screenshot",
                        with_checksum=True,
                    ),
                    _artifact(
                        artifact_id="a-ui",
                        artifact_type="ui_snapshot",
                        with_checksum=True,
                    ),
                ],
                table_artifacts=None,
            ),
            prefix="before",
        ),
    }

    payload = _capture_payload(snapshot, side="before", expected_role="before_action")

    assert payload is not None
    assert payload["evidence_source"] == "evidence_manifest"
    assert payload["manifest"] == {
        "manifest_id": "evidence-manifest:abc",
        "manifest_version": 2,
        "quality_status": "pass",
    }
    assert payload["dom"]["artifact_id"] == "a-dom"


def test_capture_payload_omits_manifest_block_when_absent() -> None:
    snapshot = {
        "task_id": TASK_ID,
        "action_execution_id": "action-001",
        "before": _capture_from_row(
            _row(
                manifest_artifacts=None,
                table_artifacts=[
                    _artifact(artifact_id="a-dom", artifact_type="dom"),
                ],
            ),
            prefix="before",
        ),
    }

    payload = _capture_payload(snapshot, side="before", expected_role="before_action")

    assert payload is not None
    assert payload["evidence_source"] == "artifact_table"
    assert "manifest" not in payload
