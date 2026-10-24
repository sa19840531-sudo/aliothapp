"""页面探索收口 Tool 测试。"""

import json

from services.api.app.agents.tools import exploration_tools  # noqa: F401
from services.api.app.agents.tools.registry import PermissionLevel, default_registry


async def test_exploration_closure_tool_accepts_explicit_targets() -> None:
    metadata = default_registry.get("exploration_closure_tool")
    assert metadata is not None

    result = await metadata.tool.invoke(
        arguments={
            "reason": "max_pages_reached",
            "action_status": "skipped",
            "exploration_action_ids": ["exploration-1"],
        }
    )

    payload = json.loads(result[0].text)
    assert payload["execution_status"] == "accepted"
    assert payload["exploration_action_ids"] == ["exploration-1"]


async def test_exploration_closure_tool_requires_target() -> None:
    metadata = default_registry.get("exploration_closure_tool")
    assert metadata is not None

    result = await metadata.tool.invoke(arguments={"reason": "no_target"})

    payload = json.loads(result[0].text)
    assert payload["execution_status"] == "blocked"
    assert payload["error_code"] == "EXPLORATION_CLOSURE_TARGET_REQUIRED"


def test_exploration_closure_tool_is_registered_as_safe() -> None:
    metadata = default_registry.get("exploration_closure_tool")

    assert metadata is not None
    assert metadata.permission_level == PermissionLevel.SAFE
    assert "reason" in metadata.tool.parameters()["required"]


async def test_exploration_closure_uses_dynamic_product_scope() -> None:
    metadata = default_registry.get("exploration_closure_tool")
    assert metadata is not None

    result = await metadata.tool.invoke(
        arguments={
            "reason": "dynamic_scope_complete",
            "page_type_assessments": [
                {
                    "page_type": "home",
                    "status": "evidence_insufficient",
                    "reason_code": "not_observed",
                }
            ],
            "assessment_scope_page_types": ["home", "content_list"],
            "assessment_scope_complete": True,
        }
    )

    payload = json.loads(result[0].text)
    assert payload["execution_status"] == "blocked"
    assert payload["missing_page_types"] == ["content_list"]
