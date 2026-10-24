from importlib import import_module

import pytest

from services.api.app.application.capture_profiles import (
    APP_DEFAULT_AFTER_ARTIFACT_TYPES,
    APP_MAIN_CAPTURE_ARTIFACT_TYPES,
    APP_MAIN_CAPTURE_BEFORE_ARTIFACT_TYPES,
)
from services.api.app.domain.interaction import InteractionOperation, SurfaceKind
from services.api.app.domain.task import TaskTargetPlatform

_APP_AFTER = APP_DEFAULT_AFTER_ARTIFACT_TYPES


def test_app_capture_profile_requests_ui_tree_without_browser_artifacts() -> None:
    module = import_module("services.api.app.application.capture_profiles")
    profile = module.CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP)

    assert profile.before.artifact_types == APP_MAIN_CAPTURE_BEFORE_ARTIFACT_TYPES
    assert profile.after.artifact_types == APP_DEFAULT_AFTER_ARTIFACT_TYPES
    assert "temporal_trace" not in profile.after.artifact_types
    requested = set(profile.before.artifact_types) | set(profile.after.artifact_types)
    assert requested.isdisjoint({"dom", "ocr_text"})
    assert "ui_tree" in requested
    assert "ui_snapshot" in requested
    assert "visible_text" in requested
    assert "device_log" in requested


def test_app_probe_profile_requests_ui_tree() -> None:
    module = import_module("services.api.app.application.capture_profiles")
    profile = module.CaptureProfilePolicy.for_app_probe(
        operation=InteractionOperation.OBSERVE,
        required_artifact_types=("ui_tree",),
    )

    assert profile.before.role == "before_action"
    assert profile.after.role == "after_action"
    assert profile.before.artifact_types == APP_MAIN_CAPTURE_BEFORE_ARTIFACT_TYPES
    assert "ui_tree" in profile.after.artifact_types
    assert "view_hierarchy" in profile.after.artifact_types
    assert "screenshot" in profile.after.artifact_types
    assert "ui_snapshot" in profile.after.artifact_types
    assert "visible_text" in profile.after.artifact_types
    assert set(profile.after.artifact_types).isdisjoint({"dom", "ocr_text"})


def test_app_probe_seek_media_fails_closed_without_formal_media_state() -> None:
    module = import_module("services.api.app.application.capture_profiles")

    with pytest.raises(ValueError, match="media_state"):
        module.CaptureProfilePolicy.for_app_probe(
            operation=InteractionOperation.SEEK_MEDIA,
        )


def test_android_surface_profile_is_operation_aware_without_browser_artifacts() -> None:
    module = import_module("services.api.app.application.capture_profiles")

    profile = module.CaptureProfilePolicy.for_surface(
        SurfaceKind.ANDROID,
        InteractionOperation.CLICK,
    )

    assert profile.profile_id == "app-click-v1"
    assert profile.before.artifact_types == APP_MAIN_CAPTURE_BEFORE_ARTIFACT_TYPES
    assert profile.after.artifact_types == APP_DEFAULT_AFTER_ARTIFACT_TYPES


def test_android_timed_capture_profile_requests_temporal_trace() -> None:
    module = import_module("services.api.app.application.capture_profiles")
    profile = module.CaptureProfilePolicy.for_surface(
        SurfaceKind.ANDROID,
        InteractionOperation.TIMED_CAPTURE,
    )
    assert profile.profile_id == "app-timed_capture-v1"
    assert "temporal_trace" in profile.after.artifact_types
    assert "temporal_trace" in APP_MAIN_CAPTURE_ARTIFACT_TYPES


def test_h5_capture_profile_preserves_browser_observation_artifacts() -> None:
    module = import_module("services.api.app.application.capture_profiles")
    profile = module.CaptureProfilePolicy.for_platform(TaskTargetPlatform.H5)

    assert profile.before.artifact_types == (
        "screenshot",
        "dom",
        "visible_text",
        "ui_snapshot",
    )
    assert profile.after.artifact_types == (
        "screenshot",
        "dom",
        "visible_text",
        "ui_snapshot",
        "action_trace",
    )


def test_seek_media_profile_captures_media_truth_before_and_after() -> None:
    module = import_module("services.api.app.application.capture_profiles")
    profile = module.CaptureProfilePolicy.for_surface(
        SurfaceKind.WEB,
        InteractionOperation.SEEK_MEDIA,
    )

    assert profile.profile_id == "web-seek_media-v1"
    assert "media_state" in profile.before.artifact_types
    assert "media_state" in profile.after.artifact_types
    assert "action_trace" in profile.after.artifact_types
