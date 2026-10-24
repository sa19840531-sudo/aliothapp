"""就绪事实必须在采集、增量发布、最终交接三处一致。"""

import json
from dataclasses import replace

import pytest

from services.api.app.application.h5_capture_readiness import h5_capture_is_inspection_ready
from services.api.app.application.h5_evidence_handoff import (
    H5EvidenceNotReadyError,
    _validate_capture,
)
from services.api.app.application.h5_live_inspection_publish import _is_pending_publishable
from services.api.tests.test_h5_live_inspection_publish import _capture


@pytest.mark.parametrize(
    "ready,state,block,diagnostic,expected",
    [
        (True, "stable", [], False, True),
        (True, "popup", [], False, True),
        (False, "stable", [], False, False),
        (None, "stable", [], False, False),
        (True, "loading", [], False, False),
        (True, "stable", ["business_media_loading"], False, False),
        (True, "stable", [], True, False),
    ],
)
@pytest.mark.parametrize("json_text", [False, True])
def test_frame_readiness_is_required_without_demoting_ready_dynamic_content(
    ready,
    state,
    block,
    diagnostic,
    expected,
    json_text,
):
    metadata = {
        "pipeline_metrics": {
            "page_inspection_ready": ready,
            "page_settle_complete": False,
            "diagnostic_capture": diagnostic,
            "page_settle_diagnostics": {"blocking_reasons": block},
        }
    }
    admitted = h5_capture_is_inspection_ready(
        json.dumps(metadata) if json_text else metadata,
        page_state=state,
    )
    assert admitted is expected
    capture = replace(
        _capture(capture_id="ready-capture"), inspection_ready=admitted, page_state=state
    )
    assert _is_pending_publishable(capture) is expected
    if expected:
        _validate_capture(capture)
    else:
        with pytest.raises(H5EvidenceNotReadyError):
            _validate_capture(capture)


def test_historical_unknown_readiness_is_not_silently_accepted():
    capture = replace(_capture(capture_id="unknown-capture"), inspection_ready=None)
    assert not _is_pending_publishable(capture)
    with pytest.raises(H5EvidenceNotReadyError):
        _validate_capture(capture)
