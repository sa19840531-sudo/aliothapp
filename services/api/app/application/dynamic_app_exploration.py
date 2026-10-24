"""与具体 APK、包名无关的 App 动态页面探索目标。"""

from __future__ import annotations

from services.api.app.application.app_exploration_target import (
    APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC,
    APP_EXPLORATION_TARGET_COMPLETION_MODE_EXPLICIT,
)
from services.api.app.domain.exploration import AppExplorationGoal

DYNAMIC_APP_EXPLORATION_OBJECTIVE_VERSION = "dynamic-app-exploration-objective.v3"
_LEGACY_EXPLICIT_OBJECTIVE_VERSION = "dynamic-app-exploration-objective.v1"
_SUPPORTED_COMPLETION_MODES = {
    APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC,
    APP_EXPLORATION_TARGET_COMPLETION_MODE_EXPLICIT,
}


def build_dynamic_app_exploration_objective(
    goal: AppExplorationGoal,
    *,
    completion_mode: str = APP_EXPLORATION_TARGET_COMPLETION_MODE_DYNAMIC,
) -> str:
    """把锁定覆盖目标转为通用 Detect／Assess／Frontier 探索目标。"""

    if not isinstance(goal, AppExplorationGoal):
        raise TypeError("goal 必须为 AppExplorationGoal")
    if completion_mode not in _SUPPORTED_COMPLETION_MODES:
        raise ValueError("completion_mode 不受支持")
    if completion_mode == APP_EXPLORATION_TARGET_COMPLETION_MODE_EXPLICIT:
        # 历史 v1 Checkpoint 已冻结该文本哈希，显式目标必须保持可恢复。
        return (
            f"{_LEGACY_EXPLICIT_OBJECTIVE_VERSION}："
            "持续识别当前 App 页面和遮挡状态，从每张新截图动态发现安全、可逆且高信息增益的"
            "候选；优先探索尚未覆盖的目标页面类型与里程碑，保留 page/app sibling 到全局 "
            "Frontier，当前分支耗尽后通过 BACK 恢复并继续。不得依赖 APK 文件名、包名映射、"
            "固定 Tab 清单或逐 App 点击脚本。"
            f" required_page_types={','.join(goal.required_page_types)}；"
            f" required_milestones={','.join(goal.required_milestones)}；"
            f" minimum_business_pages={goal.minimum_business_pages}。"
        )
    return (
        f"{DYNAMIC_APP_EXPLORATION_OBJECTIVE_VERSION}："
        "持续识别当前 App 页面和遮挡状态，从每张新截图动态发现安全、可逆且高信息增益的"
        "候选；页面类型目录仅作为动态识别、候选排序和覆盖报告词表，不假设每个 App 都拥有"
        "目录中的全部页面。保留 page/app sibling 到全局 Frontier，当前分支耗尽后通过 "
        "BACK 恢复并继续，直到全局 Frontier 耗尽。不得依赖 APK 文件名、包名映射、固定 "
        "Tab 清单或逐 App 点击脚本。线路检测和开屏阶段先有界等待；仍失败时只允许依次尝试"
        "当前目标包内明确的备用线路或连接重试，并在每次动作后重新截图，禁止重复点击同一"
        "线路。允许关闭当前可验证的低风险遮挡；禁止直连、翻墙、重装、下载、充值、支付和"
        "点击外链广告主体。登录注册"
        "属于独立的有界测试准备，不得由只读动态 Frontier 猜测手机号、验证码或密码。"
        f" page_type_catalog={','.join(goal.required_page_types)}；"
        f" discovery_milestones={','.join(goal.required_milestones)}；"
        f" minimum_business_pages={goal.minimum_business_pages}。"
    )
