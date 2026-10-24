"""App Task 快照到 Goal/Budget/Playbook 的唯一映射回归。"""

from __future__ import annotations

import pytest

from services.api.app.application.app_capture_playbook import (
    resolve_app_capture_playbook,
)
from services.api.app.application.app_exploration_policy import (
    AppExplorationPolicyError,
    build_app_exploration_runtime_policy,
    build_dynamic_app_exploration_runtime_policy,
)
from services.api.app.application.dynamic_app_exploration import (
    build_dynamic_app_exploration_objective,
)
from services.api.app.domain.exploration import AppExplorationGoal

pytestmark = pytest.mark.filterwarnings(
    "ignore:build_app_exploration_runtime_policy 已弃用:DeprecationWarning"
)

_PLAYBOOK_APKS = (
    "unknown.apk",
    "91cg_x.apk",
    "hlapp_x.apk",
    "hlwapp_x.apk",
    "sns_91sp_x.apk",
    "sns_911bl_x.apk",
    "sns_51aw_x.apk",
    "sns-hjsq_x.apk",
    "dx093j_x.apk",
)


def _policy(
    apk: str,
    *,
    scan_mode: str = "full",
    max_depth: int = 4,
    duration_seconds: int = 3_600,
    max_steps: int | None = None,
    page_sampling_policy: dict | None = None,
    journey_steps: tuple[str, ...] = (),
):
    return build_app_exploration_runtime_policy(
        task_config={"scan_mode": scan_mode, "max_depth": max_depth},
        loop_guard={
            "max_depth": max_depth,
            "max_duration_seconds": duration_seconds,
            "max_steps": max_steps,
        },
        page_sampling_policy=page_sampling_policy or {},
        playbook=resolve_app_capture_playbook(apk_filename=apk),
        journey_steps=journey_steps,
    )


@pytest.mark.parametrize("apk", _PLAYBOOK_APKS)
def test_all_current_playbooks_are_reachable_in_full_and_quick_budget(apk: str) -> None:
    full = _policy(apk)
    quick = _policy(
        apk,
        scan_mode="quick",
        max_depth=2,
        duration_seconds=900,
        max_steps=40,
        page_sampling_policy={"mode": "first_n", "max_pages": 3},
    )

    assert full.curriculum_progress.required_action_count <= full.max_actions
    assert quick.curriculum_progress.required_action_count <= quick.max_actions
    assert full.duration_ms >= resolve_app_capture_playbook(apk_filename=apk).duration_ms
    assert quick.duration_ms == 900_000
    assert len(quick.active_tabs) <= 3
    assert full.policy_hash.startswith("sha256:")
    assert quick.policy_hash != full.policy_hash


def test_full_quick_custom_use_task_record_as_distinct_runtime_budget() -> None:
    full = _policy("unknown.apk")
    quick = _policy(
        "unknown.apk",
        scan_mode="quick",
        max_depth=2,
        duration_seconds=900,
        max_steps=40,
        page_sampling_policy={"mode": "first_n", "max_pages": 3},
    )
    custom = _policy(
        "unknown.apk",
        scan_mode="custom",
        max_depth=3,
    )

    assert (full.max_actions, full.max_navigation_depth, full.duration_ms) == (
        28,
        4,
        3_600_000,
    )
    assert (quick.max_actions, quick.max_navigation_depth, quick.duration_ms) == (
        40,
        2,
        900_000,
    )
    assert (custom.max_actions, custom.max_navigation_depth, custom.duration_ms) == (
        28,
        3,
        3_600_000,
    )


def test_minimum_business_pages_comes_from_sampled_tabs_or_journey() -> None:
    quick = _policy(
        "hlapp_x.apk",
        scan_mode="quick",
        max_depth=2,
        duration_seconds=900,
        max_steps=40,
        page_sampling_policy={"mode": "first_n", "max_pages": 3},
    )
    journey = _policy(
        "unknown.apk",
        journey_steps=("登录", "详情"),
    )

    assert quick.minimum_business_pages == 3
    assert len(quick.active_tabs) == 3
    assert journey.minimum_business_pages == 2
    assert journey.journey_steps == ("登录", "详情")


def test_full_accepts_ruleset_by_page_type_sampling_policy() -> None:
    policy = _policy(
        "unknown.apk",
        page_sampling_policy={
            "mode": "by_page_type",
            "max_pages": 100,
            "include_page_types": [],
            "exclude_page_types": [],
        },
    )

    assert policy.page_limit == 100
    assert policy.active_tabs == resolve_app_capture_playbook(apk_filename="unknown.apk").tabs


@pytest.mark.parametrize(
    "overrides",
    (
        {"task_config": {"scan_mode": "mystery", "max_depth": 4}},
        {"task_config": {"scan_mode": "full", "max_depth": 3}},
        {"loop_guard": {"max_depth": 4, "max_duration_seconds": 1, "max_steps": None}},
        {"loop_guard": {"max_depth": 4, "max_duration_seconds": 3_600, "max_steps": 1}},
    ),
)
def test_invalid_or_unreachable_task_policy_fails_closed(overrides: dict) -> None:
    values = {
        "task_config": {"scan_mode": "full", "max_depth": 4},
        "loop_guard": {
            "max_depth": 4,
            "max_duration_seconds": 3_600,
            "max_steps": None,
        },
        "page_sampling_policy": {},
        "playbook": resolve_app_capture_playbook(apk_filename="unknown.apk"),
        "journey_steps": (),
    }
    values.update(overrides)

    with pytest.raises(AppExplorationPolicyError):
        build_app_exploration_runtime_policy(**values)


def test_quick_journey_beyond_page_limit_is_rejected() -> None:
    with pytest.raises(AppExplorationPolicyError, match="Journey") as error:
        _policy(
            "unknown.apk",
            scan_mode="quick",
            max_depth=2,
            duration_seconds=900,
            max_steps=40,
            page_sampling_policy={"mode": "first_n", "max_pages": 3},
            journey_steps=("一", "二", "三", "四"),
        )

    assert error.value.error_code == "APP_EXPLORATION_POLICY_UNREACHABLE"


@pytest.mark.parametrize(
    ("scan_mode", "max_depth", "max_actions"),
    (("quick", 2, 30), ("custom", 3, 48), ("full", 4, 72)),
)
def test_dynamic_policy_has_no_package_playbook_or_fixed_tabs(
    scan_mode: str,
    max_depth: int,
    max_actions: int,
) -> None:
    page_sampling = (
        {"mode": "first_n", "max_pages": 3} if scan_mode == "quick" else {}
    )
    policy = build_dynamic_app_exploration_runtime_policy(
        task_config={"scan_mode": scan_mode, "max_depth": max_depth},
        loop_guard={
            "max_depth": max_depth,
            "max_duration_seconds": 900,
            "max_steps": None,
        },
        page_sampling_policy=page_sampling,
        journey_steps=(),
    )

    assert policy.exploration_mode == "dynamic_frontier"
    assert policy.max_actions == max_actions
    assert policy.playbook_id is None
    assert policy.curriculum_progress is None
    assert policy.active_tabs == ()
    assert policy.required_action_count == 0
    assert policy.to_dict()["protocol_version"] == "app-exploration-runtime-policy.v3"


def test_dynamic_policy_honors_explicit_long_run_action_budget() -> None:
    policy = build_dynamic_app_exploration_runtime_policy(
        task_config={"scan_mode": "full", "max_depth": 4},
        loop_guard={
            "max_depth": 4,
            "max_duration_seconds": 7_200,
            "max_steps": None,
            "max_actions": 100,
        },
        page_sampling_policy={"mode": "by_page_type", "max_pages": 100},
        journey_steps=(),
    )

    assert policy.max_actions == 100
    assert policy.duration_ms == 7_200_000
    assert policy.page_limit == 100


@pytest.mark.parametrize("configured_max_actions", [600, 601])
def test_dynamic_policy_keeps_app_action_budget_hard_cap(
    configured_max_actions: int,
) -> None:
    policy = build_dynamic_app_exploration_runtime_policy(
        task_config={"scan_mode": "full", "max_depth": 4},
        loop_guard={
            "max_depth": 4,
            "max_duration_seconds": 7_200,
            "max_steps": None,
            "max_actions": configured_max_actions,
        },
        page_sampling_policy={},
        journey_steps=(),
    )

    assert policy.max_actions == 600


def test_dynamic_objective_is_identical_across_packages() -> None:
    def _goal(package_name: str) -> AppExplorationGoal:
        return AppExplorationGoal(
            required_page_types=("home", "profile"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=2,
            allowed_packages=(package_name,),
        )

    first = build_dynamic_app_exploration_objective(_goal("uk.pnixc.juywgn"))
    second = build_dynamic_app_exploration_objective(_goal("com.hdxy.ls"))

    assert first == second
    assert "Playbook" not in first
    assert "uk.pnixc.juywgn" not in first
    assert "com.hdxy.ls" not in first
    assert "页面类型目录仅作为动态识别、候选排序和覆盖报告词表" in first
    assert "不假设每个 App 都拥有目录中的全部页面" in first
    assert "禁止直连、翻墙、重装、下载、充值、支付" in first
    assert "登录注册属于独立的有界测试准备" in first
    assert "required_page_types=" not in first


def test_explicit_objective_preserves_strict_target_semantics_and_v1_hash_text() -> None:
    goal = AppExplorationGoal(
        required_page_types=("home", "profile"),
        required_milestones=("journey:login",),
        minimum_business_pages=2,
        allowed_packages=("com.example",),
    )

    objective = build_dynamic_app_exploration_objective(
        goal,
        completion_mode="explicit_targets",
    )

    assert objective.startswith("dynamic-app-exploration-objective.v1：")
    assert "required_page_types=home,profile" in objective
    assert "required_milestones=journey:login" in objective
    assert "页面类型目录仅作为" not in objective
