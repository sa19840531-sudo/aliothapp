"""把已锁定的 App 任务配置解析为唯一的探索运行策略。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.api.app.application.app_capture_playbook import AppCapturePlaybook
from services.api.app.application.app_exploration_target import (
    AppExplorationTargetSnapshot,
    read_locked_app_exploration_target,
)
from services.api.app.application.app_frontier_quantity_contract import (
    build_quantity_contract_snapshot,
    exploration_mode_is_quantity_unbounded,
)
from services.api.app.application.app_script_curriculum import (
    AppCurriculumProgress,
    build_app_curriculum_progress,
)
from services.api.app.domain.protocol import stable_protocol_hash

APP_EXPLORATION_RUNTIME_POLICY_VERSION = "app-exploration-runtime-policy.v3"
_SUPPORTED_SCAN_MODES = frozenset({"quick", "custom", "full"})
_SUPPORTED_EXPLORATION_MODES = frozenset({"dynamic_frontier", "playbook_curriculum"})
_DYNAMIC_DEFAULT_MAX_ACTIONS = {
    "quick": 30,
    "custom": 48,
    "full": 72,
}
_APP_MAX_ACTIONS_HARD_CAP = 600


class AppExplorationPolicyError(ValueError):
    """任务策略无效或无法在已锁定预算内完成。"""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, kw_only=True)
class AppExplorationRuntimePolicy:
    """APP_READY 后冻结并随每条 Work 传播的运行策略。"""

    scan_mode: str
    max_actions: int
    max_navigation_depth: int
    duration_ms: int
    deadline_seconds: int
    page_limit: int | None
    minimum_business_pages: int
    active_tabs: tuple[str, ...]
    journey_steps: tuple[str, ...]
    required_action_count: int
    playbook_id: str | None
    curriculum_progress: AppCurriculumProgress | None
    exploration_mode: str = "playbook_curriculum"
    app_target_snapshot: AppExplorationTargetSnapshot | None = None
    budget_source: str = "agent_run.loop_guard"
    quantity_unbounded: bool = False
    protocol_version: str = APP_EXPLORATION_RUNTIME_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.exploration_mode not in _SUPPORTED_EXPLORATION_MODES:
            raise AppExplorationPolicyError(
                "App exploration_mode 不受支持",
                error_code="APP_EXPLORATION_POLICY_INVALID",
            )
        if self.budget_source != "agent_run.loop_guard":
            raise AppExplorationPolicyError(
                "App budget_source 不受支持",
                error_code="APP_EXPLORATION_POLICY_INVALID",
            )
        if self.scan_mode not in _SUPPORTED_SCAN_MODES:
            raise AppExplorationPolicyError(
                "App scan_mode 不受支持",
                error_code="APP_EXPLORATION_POLICY_INVALID",
            )
        for field_name in (
            "max_actions",
            "max_navigation_depth",
            "duration_ms",
            "deadline_seconds",
            "minimum_business_pages",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AppExplorationPolicyError(
                    f"{field_name} 必须为正整数",
                    error_code="APP_EXPLORATION_POLICY_INVALID",
                )
        if (
            isinstance(self.required_action_count, bool)
            or not isinstance(self.required_action_count, int)
            or self.required_action_count < 0
        ):
            raise AppExplorationPolicyError(
                "required_action_count 必须为非负整数",
                error_code="APP_EXPLORATION_POLICY_INVALID",
            )
        if self.page_limit is not None and (
            isinstance(self.page_limit, bool)
            or not isinstance(self.page_limit, int)
            or self.page_limit <= 0
        ):
            raise AppExplorationPolicyError(
                "page_limit 必须为正整数或空",
                error_code="APP_EXPLORATION_POLICY_INVALID",
            )
        expected_unbounded = exploration_mode_is_quantity_unbounded(self.exploration_mode)
        if self.quantity_unbounded != expected_unbounded:
            raise AppExplorationPolicyError(
                "quantity_unbounded 必须与 exploration_mode 一致",
                error_code="APP_EXPLORATION_POLICY_INVALID",
            )
        if self.required_action_count > self.max_actions:
            raise AppExplorationPolicyError(
                "App Playbook 必达阶段超出任务动作预算",
                error_code="APP_EXPLORATION_POLICY_UNREACHABLE",
            )
        if self.duration_ms != self.deadline_seconds * 1_000:
            raise AppExplorationPolicyError(
                "App duration 与 deadline 不一致",
                error_code="APP_EXPLORATION_POLICY_INVALID",
            )
        if self.app_target_snapshot is not None and not isinstance(
            self.app_target_snapshot,
            AppExplorationTargetSnapshot,
        ):
            raise AppExplorationPolicyError(
                "app_target_snapshot 必须为 AppExplorationTargetSnapshot",
                error_code="APP_EXPLORATION_POLICY_INVALID",
            )
        if self.exploration_mode == "dynamic_frontier":
            if (
                self.playbook_id is not None
                or self.curriculum_progress is not None
                or self.required_action_count != 0
                or self.active_tabs
            ):
                raise AppExplorationPolicyError(
                    "动态 App 探索不得绑定 Playbook、课纲或固定 Tab",
                    error_code="APP_EXPLORATION_POLICY_INVALID",
                )
        else:
            if self.playbook_id is None:
                raise AppExplorationPolicyError(
                    "playbook_id 不能为空",
                    error_code="APP_EXPLORATION_POLICY_INVALID",
                )
            _required_text(self.playbook_id, "playbook_id")
            if not isinstance(self.curriculum_progress, AppCurriculumProgress):
                raise AppExplorationPolicyError(
                    "curriculum_progress 必须为 AppCurriculumProgress",
                    error_code="APP_EXPLORATION_POLICY_INVALID",
                )
            if (
                self.curriculum_progress.playbook_id != self.playbook_id
                or self.curriculum_progress.required_action_count != self.required_action_count
            ):
                raise AppExplorationPolicyError(
                    "App Runtime Policy 与课纲进度不一致",
                    error_code="APP_EXPLORATION_POLICY_INVALID",
                )
        if (
            not self.quantity_unbounded
            and self.page_limit is not None
            and self.minimum_business_pages > self.page_limit
        ):
            raise AppExplorationPolicyError(
                "App Goal 最低业务页数超出页面抽样上限",
                error_code="APP_EXPLORATION_POLICY_UNREACHABLE",
            )
        if self.protocol_version != APP_EXPLORATION_RUNTIME_POLICY_VERSION:
            raise AppExplorationPolicyError(
                "App 探索运行策略版本不受支持",
                error_code="APP_EXPLORATION_POLICY_INVALID",
            )

    @property
    def policy_hash(self) -> str:
        return "sha256:" + stable_protocol_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "scan_mode": self.scan_mode,
            "budget_source": self.budget_source,
            "max_actions": self.max_actions,
            "max_navigation_depth": self.max_navigation_depth,
            "duration_ms": self.duration_ms,
            "deadline_seconds": self.deadline_seconds,
            "page_limit": self.page_limit,
            "quantity_unbounded": self.quantity_unbounded,
            "quantity_contract": build_quantity_contract_snapshot(
                exploration_mode=self.exploration_mode,
                page_limit=self.page_limit,
            ),
            "minimum_business_pages": self.minimum_business_pages,
            "active_tabs": list(self.active_tabs),
            "journey_steps": list(self.journey_steps),
            "required_action_count": self.required_action_count,
            "playbook_id": self.playbook_id,
            "curriculum_progress": (
                self.curriculum_progress.to_dict()
                if self.curriculum_progress is not None
                else None
            ),
            "exploration_mode": self.exploration_mode,
            "app_target_snapshot": (
                self.app_target_snapshot.to_dict() if self.app_target_snapshot is not None else None
            ),
        }
        if include_hash:
            result["policy_hash"] = self.policy_hash
        return result


def build_app_exploration_runtime_policy(
    *,
    task_config: Mapping[str, Any],
    loop_guard: Mapping[str, Any],
    page_sampling_policy: Mapping[str, Any],
    playbook: AppCapturePlaybook,
    journey_steps: Sequence[str],
) -> AppExplorationRuntimePolicy:
    """【已弃用】Playbook 课纲策略；生产组合根禁止调用。

    正式路径请用 ``build_dynamic_app_exploration_runtime_policy``（有限自由探索）。
    本函数仅保留给历史单测与退役前对照；底栏硬覆盖／账号凭证仍以
    ``app_ui_tree_features``／``app_flow_contract`` 为准，不依赖本策略。
    """

    import warnings

    warnings.warn(
        "build_app_exploration_runtime_policy 已弃用；"
        "生产请使用 build_dynamic_app_exploration_runtime_policy",
        DeprecationWarning,
        stacklevel=2,
    )

    scan_mode = _required_text(task_config.get("scan_mode"), "scan_mode")
    if scan_mode not in _SUPPORTED_SCAN_MODES:
        raise _invalid("App scan_mode 不受支持")
    task_depth = _positive_int(task_config.get("max_depth"), "task_config.max_depth")
    max_depth = _positive_int(loop_guard.get("max_depth"), "loop_guard.max_depth")
    if task_depth != max_depth:
        raise _invalid("App task_config.max_depth 与 loop_guard.max_depth 不一致")
    duration_seconds = _positive_int(
        loop_guard.get("max_duration_seconds"),
        "loop_guard.max_duration_seconds",
    )
    explicit_max_actions = _explicit_app_max_actions(loop_guard)

    page_limit = _page_limit(page_sampling_policy, scan_mode=scan_mode)
    tabs = _unique_texts(playbook.tabs)
    active_tabs = tabs[:page_limit] if page_limit is not None else tabs
    journeys = _unique_texts(journey_steps)
    target_snapshot = read_locked_app_exploration_target(task_config)
    minimum_business_pages = (
        target_snapshot.minimum_business_pages
        if target_snapshot is not None
        else max(1, len(journeys) if journeys else len(active_tabs))
    )
    curriculum = build_app_curriculum_progress(
        playbook,
        active_tabs=active_tabs,
    )
    max_actions = (
        explicit_max_actions
        if explicit_max_actions is not None
        else min(playbook.max_actions, _APP_MAX_ACTIONS_HARD_CAP)
    )
    duration_ms = duration_seconds * 1_000
    # Quick 已通过页面上限和动作预算裁剪课纲，Run 快照时限应保持权威；
    # 完整／自定义模式仍拒绝装不下 Playbook 建议时长的不可达配置。
    if scan_mode != "quick" and playbook.duration_ms > duration_ms:
        raise AppExplorationPolicyError(
            "App Playbook 建议运行时长超出任务时限",
            error_code="APP_EXPLORATION_POLICY_UNREACHABLE",
        )
    if curriculum.required_action_count > max_actions:
        raise AppExplorationPolicyError(
            "App Playbook 必达阶段超出任务动作预算",
            error_code="APP_EXPLORATION_POLICY_UNREACHABLE",
        )
    from services.api.app.domain.exploration.contracts import (
        ExplorationValidationError,
        split_app_action_budget_pools,
    )

    try:
        max_business, _max_obstruction, _floor = split_app_action_budget_pools(
            max_actions=max_actions,
            required_action_count=curriculum.required_action_count,
            duration_ms=duration_ms,
        )
    except ExplorationValidationError as exc:
        raise AppExplorationPolicyError(
            str(exc),
            error_code="APP_EXPLORATION_POLICY_UNREACHABLE",
        ) from exc
    if curriculum.required_action_count > max_business:
        raise AppExplorationPolicyError(
            "App Playbook 必达阶段超出业务动作预算（清障预留后）",
            error_code="APP_EXPLORATION_POLICY_UNREACHABLE",
        )
    if page_limit is not None and minimum_business_pages > page_limit:
        raise AppExplorationPolicyError(
            "App Journey 目标超出页面抽样上限",
            error_code="APP_EXPLORATION_POLICY_UNREACHABLE",
        )
    return AppExplorationRuntimePolicy(
        scan_mode=scan_mode,
        max_actions=max_actions,
        max_navigation_depth=max_depth,
        duration_ms=duration_ms,
        deadline_seconds=duration_seconds,
        page_limit=page_limit,
        minimum_business_pages=minimum_business_pages,
        active_tabs=active_tabs,
        journey_steps=journeys,
        required_action_count=curriculum.required_action_count,
        playbook_id=playbook.playbook_id,
        curriculum_progress=curriculum,
        exploration_mode="playbook_curriculum",
        app_target_snapshot=target_snapshot,
        quantity_unbounded=False,
    )


def build_dynamic_app_exploration_runtime_policy(
    *,
    task_config: Mapping[str, Any],
    loop_guard: Mapping[str, Any],
    page_sampling_policy: Mapping[str, Any],
    journey_steps: Sequence[str],
) -> AppExplorationRuntimePolicy:
    """从锁定任务快照构建与包名、APK 文件名无关的动态 Frontier 策略。"""

    scan_mode = _required_text(task_config.get("scan_mode"), "scan_mode")
    if scan_mode not in _SUPPORTED_SCAN_MODES:
        raise _invalid("App scan_mode 不受支持")
    task_depth = _positive_int(task_config.get("max_depth"), "task_config.max_depth")
    max_depth = _positive_int(loop_guard.get("max_depth"), "loop_guard.max_depth")
    if task_depth != max_depth:
        raise _invalid("App task_config.max_depth 与 loop_guard.max_depth 不一致")
    duration_seconds = _positive_int(
        loop_guard.get("max_duration_seconds"),
        "loop_guard.max_duration_seconds",
    )
    explicit_max_actions = _explicit_app_max_actions(loop_guard)

    page_limit = _page_limit(page_sampling_policy, scan_mode=scan_mode)
    journeys = _unique_texts(journey_steps)
    target_snapshot = read_locked_app_exploration_target(task_config)
    minimum_business_pages = (
        target_snapshot.minimum_business_pages
        if target_snapshot is not None
        else max(2, len(journeys))
    )
    # 数量不限量：page_limit 仅作可选提示，不得阻断动态目标。
    return AppExplorationRuntimePolicy(
        scan_mode=scan_mode,
        max_actions=(
            explicit_max_actions
            if explicit_max_actions is not None
            else _DYNAMIC_DEFAULT_MAX_ACTIONS[scan_mode]
        ),
        max_navigation_depth=max_depth,
        duration_ms=duration_seconds * 1_000,
        deadline_seconds=duration_seconds,
        page_limit=page_limit,
        minimum_business_pages=minimum_business_pages,
        active_tabs=(),
        journey_steps=journeys,
        required_action_count=0,
        playbook_id=None,
        curriculum_progress=None,
        exploration_mode="dynamic_frontier",
        app_target_snapshot=target_snapshot,
        quantity_unbounded=True,
    )


def _page_limit(policy: Mapping[str, Any], *, scan_mode: str) -> int | None:
    if not policy:
        return None
    mode = policy.get("mode")
    if mode == "by_page_type":
        if scan_mode == "quick":
            raise _invalid("quick App 任务必须使用 first_n 页面抽样")
        raw_limit = policy.get("max_pages")
        return (
            None
            if raw_limit is None
            else _positive_int(raw_limit, "page_sampling_policy.max_pages")
        )
    if mode != "first_n":
        raise _invalid("App page_sampling_policy.mode 不受支持")
    limit = _positive_int(policy.get("max_pages"), "page_sampling_policy.max_pages")
    if scan_mode != "quick":
        raise _invalid("只有 quick App 任务可以使用 first_n 页面抽样")
    return limit


def _explicit_app_max_actions(loop_guard: Mapping[str, Any]) -> int | None:
    """解析 App 专用动作预算，并兼容通用护栏的 ``max_steps`` 别名。"""

    configured: list[tuple[str, int]] = []
    for field_name in ("max_actions", "max_steps"):
        value = loop_guard.get(field_name)
        if value is not None:
            configured.append(
                (field_name, _positive_int(value, f"loop_guard.{field_name}"))
            )
    if not configured:
        return None
    values = {value for _field_name, value in configured}
    if len(values) > 1:
        raise _invalid("loop_guard.max_actions 与 loop_guard.max_steps 不一致")
    return min(configured[0][1], _APP_MAX_ACTIONS_HARD_CAP)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise _invalid(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _invalid(f"{field_name} 必须为正整数")
    return value


def _unique_texts(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for item in values:
        value = _required_text(item, "目标项")
        if value not in result:
            result.append(value)
    return tuple(result)


def _invalid(message: str) -> AppExplorationPolicyError:
    return AppExplorationPolicyError(
        message,
        error_code="APP_EXPLORATION_POLICY_INVALID",
    )
