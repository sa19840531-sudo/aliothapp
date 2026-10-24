"""规则状态触发使用其声明的真实场景角色，动作结果帧仍保留为 primary。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, kw_only=True)
class AppActionBeforeScene:
    """精确动作配对中通过可信清单核验的动作前页面场景。"""

    capture_id: str
    page_type: str | None
    page_state: str


class AppActionBeforeSceneReader(Protocol):
    """读取同任务、生产 Run、会话和阶段的真实动作前场景。"""

    async def get_before_scene(
        self,
        *,
        task_id: str,
        capture_id: str,
        agent_run_id: str,
    ) -> AppActionBeforeScene | None: ...


def requires_completed_app_action_pair(input_contract: Mapping[str, Any]) -> bool:
    """显式要求 before 和 primary 的规则只能交接完整可信动作配对。"""

    roles = input_contract.get("required_capture_roles")
    return isinstance(roles, list) and {"before", "primary"}.issubset(roles)


def required_state_before_role(
    *,
    judge_config: Mapping[str, Any],
    input_contract: Mapping[str, Any],
) -> str | None:
    """仅对显式声明 before/primary 和前角色的状态规则使用动作前场景。"""

    engine = judge_config.get("engine_config")
    if not isinstance(engine, Mapping):
        return None
    states = engine.get("required_page_states")
    roles = input_contract.get("required_capture_roles")
    if (
        isinstance(states, list)
        and states
        and isinstance(roles, list)
        and {"before", "primary"}.issubset(roles)
        and engine.get("before_role") == "before"
        and engine.get("after_role", "primary") == "primary"
    ):
        return "before"
    return None


def is_startup_ad_skip_rule(rule: Mapping[str, Any]) -> bool:
    """使用显式能力契约识别开屏跳过规则，不依赖规则编号。"""

    judge_config = rule.get("judge_config")
    capability = (
        judge_config.get("capability_config") if isinstance(judge_config, Mapping) else None
    )
    return isinstance(capability, Mapping) and capability.get("batch_group") == "startup-ad-skip"
