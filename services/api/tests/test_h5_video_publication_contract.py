"""普通视频不能用几何不变或旧主截图冒充已加载；支付展示不回退。"""

from copy import deepcopy

import pytest

from services.api.app.application.h5_capture_readiness import h5_capture_is_inspection_ready
from services.api.tests.test_h5_checkpoint_consumption import sample


def metadata():
    samples = [sample(), sample(instant=1600)]
    return {
        "pipeline_metrics": {
            "page_inspection_ready": True,
            "h5_video_publication": {
                "schema_version": "h5-video-publication.v1",
                "samples": samples,
                "primary_screenshot_sha256": samples[-1]["screenshot_sha256"],
                "page_url": samples[-1]["snapshot"]["url"],
            },
        }
    }


def test_existing_admission_must_not_accept_geometry_without_a_presented_frame():
    value = metadata()
    for item in value["pipeline_metrics"]["h5_video_publication"]["samples"]:
        item.pop("video_frame")
    assert not h5_capture_is_inspection_ready(value, page_state="stable")


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "geometry_only",
        "old_primary",
        "loading",
        "pixels_changed",
        "source_changed",
        "missing_attachment",
        "wrong_url",
        "playing_stalled",
    ],
)
def test_ordinary_video_requires_presented_loaded_frames_and_actual_attachment_closure(fault):
    value = metadata()
    contract = value["pipeline_metrics"]["h5_video_publication"]
    checksums = ["sha256:" + "a" * 64]
    if fault == "missing":
        value["pipeline_metrics"].pop("h5_video_publication")
    elif fault == "geometry_only":
        for item in contract["samples"]:
            item.pop("video_frame")
    elif fault == "old_primary":
        contract["primary_screenshot_sha256"] = "c" * 64
        checksums.append("sha256:" + "c" * 64)
    elif fault == "loading":
        contract["samples"][-1]["snapshot"]["loading"] = True
    elif fault == "pixels_changed":
        contract["samples"][-1]["video_region_sha256"] = "c" * 64
    elif fault == "source_changed":
        contract["samples"][-1]["video_frame"]["resource_generation"] = 2
    elif fault == "missing_attachment":
        checksums = []
    elif fault == "wrong_url":
        contract["page_url"] = "https://example.test/other"
    elif fault == "playing_stalled":
        for item in contract["samples"]:
            item["snapshot"]["elements"][0]["media"]["paused"] = False
    assert h5_capture_is_inspection_ready(
        value, page_state="stable", page_type="video_detail", screenshot_checksums=checksums
    ) is (fault is None)


def test_advancing_playback_is_loaded_without_requiring_identical_pixels():
    value = deepcopy(metadata())
    samples = value["pipeline_metrics"]["h5_video_publication"]["samples"]
    for item in samples:
        item["snapshot"]["elements"][0]["media"]["paused"] = False
    samples[-1]["video_frame"]["presented_frames"] += 1
    samples[-1]["video_region_sha256"] = "d" * 64
    assert h5_capture_is_inspection_ready(
        value,
        page_state="stable",
        page_type="video_detail",
        screenshot_checksums=["sha256:" + "a" * 64],
    )


@pytest.mark.parametrize("page_type", ["home", "payment", "recharge", "content_detail"])
def test_non_video_page_keeps_body_readiness_without_inventing_video_evidence(page_type):
    assert h5_capture_is_inspection_ready(
        {"pipeline_metrics": {"page_inspection_ready": True}},
        page_state="stable",
        page_type=page_type,
        screenshot_checksums=[],
    )


def test_actual_native_video_contract_is_required_even_when_classifier_calls_it_content_detail():
    value = metadata()
    value["pipeline_metrics"]["h5_video_publication"]["samples"] = []
    assert not h5_capture_is_inspection_ready(
        value, page_state="stable", page_type="content_detail", screenshot_checksums=[]
    )


@pytest.mark.parametrize("primary", [[], ["sha256:" + "c" * 64], ["sha256:" + "a" * 64] * 2])
def test_old_or_ambiguous_primary_cannot_be_replaced_by_an_unrelated_sample(primary):
    assert not h5_capture_is_inspection_ready(
        metadata(),
        page_state="stable",
        page_type="video_detail",
        screenshot_checksums=["sha256:" + "a" * 64],
        primary_screenshot_checksums=primary,
    )
