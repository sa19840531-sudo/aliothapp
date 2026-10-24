"""App 产品目标画像、适用性和冲突注册合同测试。"""

from __future__ import annotations

import pytest

from services.api.app.application.app_exploration_target import (
    APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V3,
    AppExplorationTargetError,
    AppExplorationTargetSnapshot,
    build_app_exploration_target_snapshot,
)
from services.api.app.domain.exploration.app_target_profile import (
    APP_TARGET_APPLICABILITY_PROFILE,
    AppTargetProfile,
    AppTargetProfileConflictError,
    AppTargetProfileRegistry,
    get_default_app_target_profile_registry,
)
from services.api.app.domain.exploration.generic5_coverage_scope import (
    GENERIC5_PACKAGE_NAMES,
)


def test_default_registry_resolves_known_packages_without_playbook_fields() -> None:
    registry = get_default_app_target_profile_registry()

    profile = registry.resolve(next(iter(GENERIC5_PACKAGE_NAMES)))

    assert profile is not None
    payload = profile.to_dict()
    assert payload["applicability_mode"] == APP_TARGET_APPLICABILITY_PROFILE
    assert "coordinates" not in payload
    assert "selectors" not in payload
    assert "playbook" not in payload


def test_explicit_profile_and_historical_v3_target_round_trip_with_hash() -> None:
    profile = AppTargetProfile(
        profile_id="test-profile",
        package_names=("com.example.target",),
        required_page_types=("home", "content_detail"),
        optional_page_types=("profile",),
        lifecycle_page_types=("line_loading",),
    )
    restored_profile = AppTargetProfile.from_dict(profile.to_dict())
    assert restored_profile == profile

    registry = AppTargetProfileRegistry((profile,))
    target = build_app_exploration_target_snapshot(
        journey_steps=(),
        package_name="com.example.target",
        profile_id="test-profile",
        profile_registry=registry,
    )
    assert target.protocol_version == APP_EXPLORATION_TARGET_PROTOCOL_VERSION_V3
    assert target.profile_id == "test-profile"
    assert target.applicability_mode == APP_TARGET_APPLICABILITY_PROFILE
    assert target.applicability["optional_page_types"] == ["profile"]
    assert AppExplorationTargetSnapshot.from_dict(target.to_dict()) == target


def test_profile_registry_fails_closed_for_duplicate_package_or_profile_content() -> None:
    first = AppTargetProfile(
        profile_id="first",
        package_names=("com.example.duplicate",),
        required_page_types=("home",),
    )
    second = AppTargetProfile(
        profile_id="second",
        package_names=("com.example.duplicate",),
        required_page_types=("content_list",),
    )
    registry = AppTargetProfileRegistry((first,))

    with pytest.raises(AppTargetProfileConflictError, match="同时属于"):
        registry.register(second)

    with pytest.raises(AppTargetProfileConflictError, match="内容冲突"):
        registry.register(
            AppTargetProfile(
                profile_id="first",
                package_names=("com.example.other",),
                required_page_types=("home",),
            )
        )


def test_known_package_does_not_auto_select_profile_for_dynamic_target() -> None:
    package_name = next(iter(GENERIC5_PACKAGE_NAMES))
    unknown_package_name = "com.example.unregistered"

    known_target = build_app_exploration_target_snapshot(
        journey_steps=(),
        package_name=package_name,
        applicability_mode="dynamic_discovery",
    )
    unknown_target = build_app_exploration_target_snapshot(
        journey_steps=(),
        package_name=unknown_package_name,
        applicability_mode="dynamic_discovery",
    )

    assert known_target.profile_id is None
    assert unknown_target.profile_id is None
    assert known_target.applicability == unknown_target.applicability
    assert known_target.completion_mode == unknown_target.completion_mode == "dynamic_discovery"
    assert known_target.minimum_business_pages == unknown_target.minimum_business_pages == 1
    assert known_target.optional_page_types == unknown_target.optional_page_types == ()
    assert known_target.lifecycle_page_types == unknown_target.lifecycle_page_types == ()
    assert known_target.allowed_packages == (package_name,)
    assert unknown_target.allowed_packages == (unknown_package_name,)


def test_known_package_accepts_custom_target_without_profile() -> None:
    package_name = next(iter(GENERIC5_PACKAGE_NAMES))

    target = build_app_exploration_target_snapshot(
        journey_steps=(),
        package_name=package_name,
        required_page_types=("home",),
        optional_page_types=("profile",),
        lifecycle_page_types=("line_loading",),
        applicability_mode="explicit_targets",
    )

    assert target.profile_id is None
    assert target.applicability_mode == "explicit_targets"
    assert target.required_page_types == ("home",)
    assert target.optional_page_types == ("profile",)
    assert target.lifecycle_page_types == ("line_loading",)
    assert target.allowed_packages == (package_name,)


def test_profile_contract_requires_explicit_profile_id() -> None:
    package_name = "com.example.target"
    profile = AppTargetProfile(
        profile_id="test-profile",
        package_names=(package_name,),
        required_page_types=("home", "content_detail"),
    )
    registry = AppTargetProfileRegistry((profile,))

    target = build_app_exploration_target_snapshot(
        journey_steps=(),
        package_name=package_name,
        profile_id=profile.profile_id,
        profile_registry=registry,
    )

    assert target.profile_id == profile.profile_id
    assert target.applicability_mode == APP_TARGET_APPLICABILITY_PROFILE
    assert target.required_page_types == profile.required_page_types
    assert target.minimum_business_pages == profile.minimum_business_pages

    with pytest.raises(AppExplorationTargetError, match="注册产品目标 profile"):
        build_app_exploration_target_snapshot(
            journey_steps=(), package_name=package_name,
            profile_id=profile.profile_id, profile_registry=registry,
            required_page_types=("home",),
        )
    with pytest.raises(AppExplorationTargetError, match="不属于"):
        build_app_exploration_target_snapshot(
            journey_steps=(), package_name="com.other.target",
            profile_id=profile.profile_id, profile_registry=registry,
        )

    with pytest.raises(AppExplorationTargetError, match="未注册"):
        build_app_exploration_target_snapshot(
            journey_steps=(),
            package_name=package_name,
            profile_id="missing-profile",
            profile_registry=registry,
        )
