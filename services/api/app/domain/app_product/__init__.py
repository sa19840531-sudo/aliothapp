"""APP 产品 Profile 域：按包名沉淀产品专属编排知识，供 Agent Skill / Tool 读取。"""

from services.api.app.domain.app_product.orchestration import (
    mama_agent_curriculum,
    membership_double_back_plan,
    suggest_next_action,
    suggest_popup_close_action,
)
from services.api.app.domain.app_product.popup_close import (
    DEFAULT_POPUP_CLOSE_BY_TYPE,
    STARTUP_SEMANTIC_MARKERS,
    is_startup_close_type,
    normalize_popup_type,
    resolve_popup_close_plan,
)
from services.api.app.domain.app_product.profiles import (
    AppProductProfile,
    build_app_product_capability_facts,
    is_vision_only_tap_key,
    list_profile_packages,
    load_profile,
    resolve_profile_dir,
)

__all__ = [
    "AppProductProfile",
    "DEFAULT_POPUP_CLOSE_BY_TYPE",
    "STARTUP_SEMANTIC_MARKERS",
    "build_app_product_capability_facts",
    "is_startup_close_type",
    "is_vision_only_tap_key",
    "list_profile_packages",
    "load_profile",
    "mama_agent_curriculum",
    "membership_double_back_plan",
    "normalize_popup_type",
    "resolve_popup_close_plan",
    "resolve_profile_dir",
    "suggest_next_action",
    "suggest_popup_close_action",
]
