"""H5 动作前快照继承页面状态图身份的约束测试。"""

from copy import deepcopy

import pytest

from services.api.app.domain.exploration.page_graph import build_page_state_snapshot
from services.api.app.infrastructure.db.page_graph_repository import (
    _trusted_h5_source_identity_override,
)


def _logical_surface() -> dict[str, object]:
    return {
        "protocol_version": "logical_surface.v1",
        "identity_fingerprint": "sha256:" + "1" * 64,
        "state_fingerprint": "sha256:" + "2" * 64,
        "render_profile": {"kind": "dom_spa", "status": "confirmed"},
        "evidence_quality": "high",
    }


def _row() -> dict[str, object]:
    capture_id = "11111111-1111-1111-1111-111111111111"
    return {
        "target_platform": "h5",
        "requested_source_capture_id": capture_id,
        "source_parent_capture_id": capture_id,
        "source_capture_id": "22222222-2222-2222-2222-222222222222",
        "source_surface_kind": "web",
        "source_capture_role": "before_action",
        "source_url": "https://example.test/home",
        "requested_source_url": "https://example.test/home",
        "source_metadata": {
            "pipeline_metrics": {
                "capture_consistency": {
                    "status": "consistent_with_capability_gaps",
                    "changed_signals": [],
                }
            }
        },
        "requested_identity_status": "confirmed",
        "requested_identity_source": "logical_surface",
        "requested_state_payload": {"logical_surface": _logical_surface()},
    }


def _intent() -> dict[str, object]:
    return {
        "parameters": {
            "logical_target": {
                "protocol_version": "logical_target.v1",
                "source_identity_fingerprint": "sha256:" + "1" * 64,
                "source_state_fingerprint": "sha256:" + "2" * 64,
            }
        }
    }


def test_h5_before_action_inherits_confirmed_requested_source_identity() -> None:
    override = _trusted_h5_source_identity_override(_row(), _intent())

    assert override["logical_surface"] == _logical_surface()
    assert override["page_graph_identity_lineage"] == {
        "protocol_version": "h5-action-source-lineage.v1",
        "verified": True,
        "requested_source_capture_id": "11111111-1111-1111-1111-111111111111",
        "source_capture_id": "22222222-2222-2222-2222-222222222222",
        "verified_by": "parent_capture_consistency_and_logical_target",
    }

    snapshot = build_page_state_snapshot(
        capture_id="22222222-2222-2222-2222-222222222222",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=0,
        command_sequence=1,
        capture_role="before_action",
        metadata=override,
        page_type="home",
        page_state="stable",
    )
    assert snapshot.identity.status == "confirmed"
    assert snapshot.state_payload["page_graph_identity_lineage"] == override[
        "page_graph_identity_lineage"
    ]


@pytest.mark.parametrize(
    ("mutate", "value"),
    (
        ("target_platform", "web"),
        ("source_parent_capture_id", "33333333-3333-3333-3333-333333333333"),
        ("source_capture_role", "observation"),
        ("requested_identity_status", "unknown"),
        ("requested_identity_source", "unconfirmed"),
        ("requested_source_url", "https://example.test/detail"),
    ),
)
def test_h5_source_identity_rejects_untrusted_lineage(mutate: str, value: object) -> None:
    row = _row()
    row[mutate] = value

    assert _trusted_h5_source_identity_override(row, _intent()) == {}


def test_h5_source_identity_rejects_capture_drift() -> None:
    row = _row()
    metadata = deepcopy(row["source_metadata"])
    metadata["pipeline_metrics"]["capture_consistency"]["changed_signals"] = ["page_url"]
    row["source_metadata"] = metadata

    assert _trusted_h5_source_identity_override(row, _intent()) == {}


def test_h5_source_identity_rejects_unsigned_logical_target() -> None:
    intent = _intent()
    intent["parameters"]["logical_target"]["source_state_fingerprint"] = (
        "sha256:" + "3" * 64
    )

    assert _trusted_h5_source_identity_override(_row(), intent) == {}
