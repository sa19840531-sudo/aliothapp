"""H5 Explorer 纯页面探索提示词测试。"""

from __future__ import annotations

from services.api.app.agents.explorer import h5_instructions
from services.api.app.agents.explorer.models import ExplorerDecisionModel

FORBIDDEN_INSPECTION_TERMS = (
    "ProbeWork",
    "EvidenceRequest",
    "Rule Judge",
    "ruleset",
    "规则覆盖",
    "质检",
    "补证",
    "报告",
)


def test_h5_explorer_instructions_are_pure_page_exploration() -> None:
    builder = getattr(h5_instructions, "build_h5_explorer_system_instructions", None)
    assert callable(builder)

    prompt = builder(
        output_schema=ExplorerDecisionModel.model_json_schema(),
        required_workflow_skill="h5-explorer-workflow",
    )

    for marker in FORBIDDEN_INSPECTION_TERMS:
        assert marker.casefold() not in prompt.casefold()
    for required in (
        "页面类型覆盖缺口",
        "动态 Frontier",
        "ActionIntent",
        "ExplorationTerminal",
        "source_capture_id",
        "frontier_snapshot_id",
        "state_version",
    ):
        assert required in prompt
