"""V1 Main Agent 的同步派发兼容实现。"""

from services.api.app.agents.runtime.legacy_dispatch.dispatcher import (
    LegacyV1WorkDispatcher,
)
from services.api.app.agents.runtime.legacy_dispatch.models import WorkDispatch
from services.api.app.agents.runtime.legacy_dispatch.rule_dispatch import (
    prepare_page_rule_dispatch,
)

__all__ = [
    "LegacyV1WorkDispatcher",
    "WorkDispatch",
    "prepare_page_rule_dispatch",
]
