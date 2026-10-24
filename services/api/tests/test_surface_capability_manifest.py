import pytest

from services.api.app.domain import interaction as interaction_domain


def test_app_manifest_rejects_missing_required_ready_capability() -> None:
    manifest = interaction_domain.SurfaceCapabilityManifest(
        adapter_id="xingyu.fake",
        adapter_version="1.0",
        surface_kind=interaction_domain.SurfaceKind.ANDROID,
        platform="android",
        backends=("fake",),
        actions={
            "click": interaction_domain.CapabilityDescriptor(
                status=interaction_domain.CapabilityStatus.READY
            )
        },
        observations={
            "screenshot": interaction_domain.CapabilityDescriptor(
                status=interaction_domain.CapabilityStatus.READY
            )
        },
        artifacts={
            "screenshot": interaction_domain.CapabilityDescriptor(
                status=interaction_domain.CapabilityStatus.READY
            )
        },
    )

    with pytest.raises(interaction_domain.CapabilityMismatchError, match="install"):
        manifest.require_ready(
            (
                "session",
                "install",
                "open",
                "click",
                "scroll",
                "back",
                "screenshot",
                "close",
            )
        )


def test_android_screenshot_only_manifest_declares_ui_tree_ready() -> None:
    manifest = interaction_domain.build_android_screenshot_only_manifest(
        adapter_id="xingyu.fake",
        adapter_version="1.0",
        backend="fake",
    )

    assert manifest.actions["input"].status is interaction_domain.CapabilityStatus.READY
    assert (
        manifest.observations["visible_text"].status
        is interaction_domain.CapabilityStatus.READY
    )
    assert manifest.observations["ui_tree"].status is interaction_domain.CapabilityStatus.READY
    assert manifest.artifacts["ui_tree"].status is interaction_domain.CapabilityStatus.READY
    assert "不保证每帧落库" in str(manifest.observations["ui_tree"].notes or "")
    assert manifest.observations["view_hierarchy"].status is interaction_domain.CapabilityStatus.READY
    assert "默认不持久化" in str(manifest.observations["view_hierarchy"].notes or "")
    assert manifest.artifacts["visible_text"].status is interaction_domain.CapabilityStatus.READY
    assert (
        manifest.artifacts["media_state"].status
        is interaction_domain.CapabilityStatus.PARTIAL
    )
    assert manifest.artifacts["screenshot"].status is interaction_domain.CapabilityStatus.READY
    assert manifest.artifacts["ocr_text"].status is interaction_domain.CapabilityStatus.UNSUPPORTED
    manifest.require_ready(
        (
            "session",
            "install",
            "open",
            "click",
            "scroll",
            "back",
            "screenshot",
            "close",
        )
    )


def test_manifest_round_trip_ignores_additive_unknown_fields() -> None:
    payload = {
        "protocol_version": "surface-capabilities.v1",
        "adapter_id": "xingyu.official",
        "adapter_version": "1.0",
        "surface_kind": "android",
        "platform": "android",
        "backends": ["cloud_phone"],
        "actions": {
            "click": {
                "status": "ready",
                "target_modes": ["coordinate"],
                "fields": ["x", "y"],
                "future_descriptor_field": True,
            }
        },
        "observations": {
            "screenshot": {
                "status": "ready",
                "target_modes": [],
                "fields": ["png"],
            }
        },
        "artifacts": {
            "screenshot": {
                "status": "ready",
                "target_modes": [],
                "fields": ["content_type", "sha256"],
            }
        },
        "features": {"schema_version": "1.0"},
        "future_manifest_field": {"ignored": True},
    }

    manifest = interaction_domain.SurfaceCapabilityManifest.from_dict(payload)

    assert manifest.adapter_id == "xingyu.official"
    assert manifest.actions["click"].target_modes == ("coordinate",)
    assert manifest.to_dict() == {
        "protocol_version": "surface-capabilities.v1",
        "adapter_id": "xingyu.official",
        "adapter_version": "1.0",
        "surface_kind": "android",
        "platform": "android",
        "backends": ["cloud_phone"],
        "actions": {
            "click": {
                "status": "ready",
                "target_modes": ["coordinate"],
                "fields": ["x", "y"],
                "notes": None,
            }
        },
        "observations": {
            "screenshot": {
                "status": "ready",
                "target_modes": [],
                "fields": ["png"],
                "notes": None,
            }
        },
        "artifacts": {
            "screenshot": {
                "status": "ready",
                "target_modes": [],
                "fields": ["content_type", "sha256"],
                "notes": None,
            }
        },
        "features": {"schema_version": "1.0"},
    }
