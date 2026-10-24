"""App Capture 质检标注应写入 detection_status=recognized。"""

from __future__ import annotations

from services.api.app.application.app_inspection_publisher import (
    build_capture_metadata_patch,
)


def test_build_capture_metadata_patch_sets_recognized_for_catalog_page() -> None:
    patch = build_capture_metadata_patch(
        inspection_status="published",
        page_type="home",
        page_state="stable",
        published_count=3,
    )
    assert patch["page_type"] == "home"
    assert patch["page_state"] == "stable"
    assert patch["detection_status"] == "recognized"
    assert patch["inspection_status"] == "published"


def test_build_capture_metadata_patch_skips_recognized_for_unmatched() -> None:
    patch = build_capture_metadata_patch(
        inspection_status="unmatched_page_type",
        page_type="not_a_real_page",
        page_state="unknown",
        published_count=0,
    )
    assert patch["page_type"] == "not_a_real_page"
    assert "detection_status" not in patch
