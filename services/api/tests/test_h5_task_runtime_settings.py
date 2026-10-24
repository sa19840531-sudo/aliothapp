"""H5 运行时设置的锁定策略映射测试。"""

import pytest

from services.api.app.agents.runtime.task_execution_policy import (
    InvalidTaskExecutionPolicyError,
)
from services.api.app.application.h5_task_runtime_settings import (
    build_h5_task_runtime_settings,
)
from services.api.app.core.config import AgentRuntimeSettings


@pytest.mark.parametrize(
    (
        "mode",
        "loop_guard",
        "sampling",
        "expected_actions",
        "expected_depth",
        "expected_seconds",
        "expected_pages",
        "expected_ai_calls",
        "expected_total_tokens",
    ),
    [
        (
            "quick",
            {"max_steps": 40, "max_depth": 2, "timeout_minutes": 15},
            {"max_pages": 3},
            40,
            2,
            900.0,
            3,
            64,
            None,
        ),
        (
            "full",
            {"max_steps": 900, "max_depth": 4, "timeout_minutes": 60},
            {},
            80,
            4,
            3600.0,
            None,
            200,
            None,
        ),
        (
            "custom",
            {"max_steps": 75, "max_depth": 3, "timeout_minutes": 60},
            {},
            60,
            3,
            3600.0,
            None,
            72,
            None,
        ),
    ],
)
def test_build_h5_task_runtime_settings(
    mode,
    loop_guard,
    sampling,
    expected_actions,
    expected_depth,
    expected_seconds,
    expected_pages,
    expected_ai_calls,
    expected_total_tokens,
) -> None:
    """若忽略锁定策略，快速、完整和自定义扫描会使用错误预算。"""
    result = build_h5_task_runtime_settings(
        task_config={"scan_mode": mode, "max_depth": 8, "journey_steps": ["商品详情"]},
        loop_guard=loop_guard,
        page_sampling_policy={
            **sampling,
            "depth_limit": 6,
            "include_page_types": ["home", "product_detail"],
            "exclude_page_types": ["payment"],
            "rule_target_page_types": ["product_detail"],
        },
        defaults=AgentRuntimeSettings(
            max_steps=200,
            max_duration_seconds=3600,
            max_steps_hard_cap=600,
        ),
    )

    assert result.max_actions == expected_actions
    assert result.max_depth == expected_depth
    assert result.max_duration_seconds == expected_seconds
    assert result.max_pages == expected_pages
    assert result.max_ai_calls == expected_ai_calls
    assert result.max_total_tokens == expected_total_tokens
    assert result.journey_steps == ("商品详情",)
    assert result.include_page_types == ("home", "product_detail")
    assert result.exclude_page_types == ("payment",)
    assert not hasattr(result, "rule_target_page_types")
    assert "rule_target_page_types" not in result.sampling_policy_summary


@pytest.mark.parametrize(
    ("task_config", "sampling"),
    [
        ({"scan_mode": "unsupported"}, {}),
        ({"journey_steps": "商品详情"}, {}),
        ({}, {"max_pages": True}),
        ({}, {"include_page_types": ["home", 2]}),
        ({}, {"include_page_types": ["startup_ad"]}),
        ({}, {"include_page_types": ["home"], "exclude_page_types": ["home"]}),
    ],
)
def test_build_h5_task_runtime_settings_rejects_invalid_locked_policies(
    task_config, sampling
) -> None:
    """若放宽锁定策略的形状或矛盾页型，运行会在不可审计状态下继续。"""
    with pytest.raises(InvalidTaskExecutionPolicyError):
        build_h5_task_runtime_settings(
            task_config=task_config,
            loop_guard={"max_depth": 2},
            page_sampling_policy=sampling,
            defaults=AgentRuntimeSettings(),
        )


def test_h5_runtime_ignores_ruleset_target_page_types() -> None:
    result = build_h5_task_runtime_settings(
        task_config={"scan_mode": "full"},
        loop_guard={"max_depth": 2},
        page_sampling_policy={
            "include_page_types": ["home"],
            "exclude_page_types": ["payment"],
            "rule_target_page_types": ["payment"],
        },
        defaults=AgentRuntimeSettings(),
    )

    assert result.include_page_types == ("home",)
    assert result.exclude_page_types == ("payment",)
    assert result.sampling_policy_summary == {
        "max_pages": None,
        "journey_steps": [],
        "include_page_types": ["home"],
        "exclude_page_types": ["payment"],
    }


def test_h5_runtime_uses_stricter_locked_call_and_token_limits() -> None:
    result = build_h5_task_runtime_settings(
        task_config={"scan_mode": "full"},
        loop_guard={"max_ai_calls": 12, "max_total_tokens": 300_000},
        page_sampling_policy={},
        defaults=AgentRuntimeSettings(),
    )

    assert result.max_ai_calls == 12
    assert result.max_total_tokens == 300_000


@pytest.mark.parametrize(
    ("scan_mode", "expected_depth"),
    [("quick", 2), ("custom", 3), ("full", 4)],
)
def test_h5_runtime_uses_scan_mode_depth_when_legacy_snapshot_omits_depth(
    scan_mode: str,
    expected_depth: int,
) -> None:
    """旧任务缺少深度字段时不得静默退化为只能探索当前页面。"""

    result = build_h5_task_runtime_settings(
        task_config={"scan_mode": scan_mode},
        loop_guard={},
        page_sampling_policy={},
        defaults=AgentRuntimeSettings(),
    )

    assert result.max_depth == expected_depth


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "invalid"])
def test_h5_runtime_rejects_invalid_total_token_limit(value: object) -> None:
    with pytest.raises(InvalidTaskExecutionPolicyError, match="max_total_tokens"):
        build_h5_task_runtime_settings(
            task_config={"scan_mode": "full"},
            loop_guard={"max_total_tokens": value},
            page_sampling_policy={},
            defaults=AgentRuntimeSettings(),
        )
