"""App 任务创建边界锁定的探索目标快照合同。"""

from __future__ import annotations

import pytest

from services.api.app.application.app_exploration_target import (
    APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V1,
    AppExplorationTargetError,
    AppExplorationTargetSnapshot,
    build_app_exploration_target_snapshot,
    read_locked_app_exploration_target,
)
from services.api.app.domain.interaction import (
    CapabilityDescriptor,
    CapabilityStatus,
    SurfaceCapabilityManifest,
    SurfaceKind,
    build_android_screenshot_only_manifest,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.protocol import stable_protocol_hash


def test_target_snapshot_normalizes_once_and_detects_runtime_tampering() -> None:
    snapshot = build_app_exploration_target_snapshot(
        journey_steps=("登录", "内容详情"),
        required_page_types=("HOME", "content_detail", "HOME"),
        required_milestones=("journey:登录", "surface:content_detail"),
        minimum_business_pages=3,
        device_capability_template="android_visual_evidence_v1",
    )

    assert snapshot.required_page_types == ("home", "content_detail")
    assert snapshot.required_milestones == ("journey:登录", "surface:content_detail")
    assert snapshot.target_hash.startswith("sha256:")
    assert (
        read_locked_app_exploration_target({"app_exploration_target": snapshot.to_dict()})
        == snapshot
    )

    tampered = snapshot.to_dict()
    tampered["minimum_business_pages"] = 4
    with pytest.raises(AppExplorationTargetError, match="hash"):
        AppExplorationTargetSnapshot.from_dict(tampered)


def test_target_snapshot_uses_shared_catalog_and_journey_milestones() -> None:
    snapshot = build_app_exploration_target_snapshot(
        journey_steps=("首页", "详情"),
    )

    assert snapshot.required_page_types == (
        get_default_page_type_registry().coverage_required_codes
    )
    assert snapshot.completion_mode == "dynamic_discovery"
    assert snapshot.required_milestones == ("surface:stable_business_page",)
    assert snapshot.minimum_business_pages == 1
    assert snapshot.requires_real_visual is True


def test_explicit_page_targets_keep_strict_completion_and_legacy_defaults() -> None:
    snapshot = build_app_exploration_target_snapshot(
        journey_steps=("首页", "详情"),
        required_page_types=("HOME", "content_detail"),
    )

    assert snapshot.completion_mode == "explicit_targets"
    assert snapshot.required_page_types == ("home", "content_detail")
    assert snapshot.required_milestones == ("journey:首页", "journey:详情")
    assert snapshot.minimum_business_pages == 2


def test_v1_target_snapshot_is_read_as_explicit_and_uses_v1_hash_fields() -> None:
    values = {
        "protocol_version": APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V1,
        "required_page_types": ["home", "content_detail"],
        "required_milestones": ["journey:首页"],
        "minimum_business_pages": 2,
        "device_capability_template": "android_visual_readonly_v1",
        "requires_real_visual": True,
    }
    payload = {
        **values,
        "target_hash": "sha256:" + stable_protocol_hash(values),
    }

    snapshot = AppExplorationTargetSnapshot.from_dict(payload)

    assert snapshot.protocol_version == APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V1
    assert snapshot.completion_mode == "explicit_targets"
    assert snapshot.to_dict() == payload
    assert snapshot.recompute_target_hash() == payload["target_hash"]


def test_target_snapshot_rejects_unknown_or_lifecycle_page_types() -> None:
    with pytest.raises(AppExplorationTargetError, match="业务页面类型目录"):
        build_app_exploration_target_snapshot(
            journey_steps=(),
            required_page_types=("line_loading", "custom_app_home"),
        )


def test_target_snapshot_persists_optional_lifecycle_and_applicability_in_v3_hash() -> None:
    snapshot = build_app_exploration_target_snapshot(
        journey_steps=(),
        required_page_types=("HOME", "content_detail"),
        optional_page_types=("PROFILE",),
        lifecycle_page_types=("LINE_LOADING",),
        applicability_mode="explicit_targets",
    )

    assert snapshot.protocol_version.endswith(".v3")
    assert snapshot.optional_page_types == ("profile",)
    assert snapshot.lifecycle_page_types == ("line_loading",)
    assert snapshot.applicability == {
        "mode": "explicit_targets",
        "required_page_types": ["home", "content_detail"],
        "optional_page_types": ["profile"],
        "lifecycle_page_types": ["line_loading"],
    }
    restored = AppExplorationTargetSnapshot.from_dict(snapshot.to_dict())
    assert restored == snapshot


def test_target_snapshot_rejects_inconsistent_applicability_mode() -> None:
    with pytest.raises(AppExplorationTargetError, match="dynamic_discovery"):
        build_app_exploration_target_snapshot(
            journey_steps=(),
            required_page_types=("home",),
            applicability_mode="dynamic_discovery",
        )


def test_target_snapshot_rejects_device_manifest_below_locked_template() -> None:
    snapshot = build_app_exploration_target_snapshot(
        journey_steps=(),
        device_capability_template="android_visual_evidence_v1",
    )
    incomplete = SurfaceCapabilityManifest(
        adapter_id="android.incomplete",
        adapter_version="1",
        surface_kind=SurfaceKind.ANDROID,
        platform="android",
        backends=("cloud_phone",),
        actions={
            name: CapabilityDescriptor(status=CapabilityStatus.READY)
            for name in ("session", "install", "open", "click", "scroll", "back", "close")
        },
        observations={
            "screenshot": CapabilityDescriptor(status=CapabilityStatus.READY),
            "ui_tree": CapabilityDescriptor(status=CapabilityStatus.READY),
        },
        artifacts={
            "screenshot": CapabilityDescriptor(status=CapabilityStatus.READY),
        },
    )

    with pytest.raises(AppExplorationTargetError, match="设备能力"):
        snapshot.require_device_capabilities(incomplete)

    snapshot.require_device_capabilities(
        build_android_screenshot_only_manifest(
            adapter_id="android.ready",
            adapter_version="1",
            backend="cloud_phone",
        )
    )


def test_target_snapshot_rejects_installed_package_outside_locked_product() -> None:
    snapshot = build_app_exploration_target_snapshot(
        journey_steps=(),
        package_name="com.example.target",
    )

    snapshot.require_installed_package("COM.EXAMPLE.TARGET")

    with pytest.raises(AppExplorationTargetError, match="实际安装包"):
        snapshot.require_installed_package("com.example.other")


@pytest.mark.parametrize(
    "package_name",
    ("com.example.target", "COM.EXAMPLE.TARGET", "dark.Tozhn.wzmtH", "dark.FiVQo.sMyUI"),
)
@pytest.mark.parametrize("page_types", ((), ("home",)))
def test_target_snapshot_hashes_normalized_package_before_creation(
    package_name: str,
    page_types: tuple[str, ...],
) -> None:
    snapshot = build_app_exploration_target_snapshot(
        journey_steps=(),
        package_name=package_name,
        required_page_types=page_types,
    )

    assert snapshot.allowed_packages == (package_name.casefold(),)
    assert snapshot.target_hash == snapshot.recompute_target_hash()
    assert AppExplorationTargetSnapshot.from_dict(snapshot.to_dict()) == snapshot
    assert snapshot == build_app_exploration_target_snapshot(
        journey_steps=(),
        package_name=package_name.casefold(),
        required_page_types=page_types,
    )
    snapshot.require_installed_package(package_name)

    tampered = snapshot.to_dict()
    tampered["allowed_packages"] = ["com.example.other"]
    with pytest.raises(AppExplorationTargetError, match="hash"):
        AppExplorationTargetSnapshot.from_dict(tampered)


def test_legacy_target_without_package_lock_remains_readable() -> None:
    values = {
        "protocol_version": APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V1,
        "required_page_types": ["home"],
        "required_milestones": ["surface:home"],
        "minimum_business_pages": 1,
        "device_capability_template": "android_visual_readonly_v1",
        "requires_real_visual": True,
    }
    snapshot = AppExplorationTargetSnapshot.from_dict(
        {
            **values,
            "target_hash": "sha256:" + stable_protocol_hash(values),
        }
    )

    snapshot.require_installed_package("com.example.legacy")
