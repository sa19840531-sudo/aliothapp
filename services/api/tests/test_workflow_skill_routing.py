"""Workflow 路由与 Skill 选择的确定性测试。"""

import pytest

from services.api.app.agents import bootstrap as main_bootstrap
from services.api.app.application.app_workflow_router import WorkflowRoute
from services.api.app.application.workflow_skill_routing import (
    skill_sub_dirs_for_route,
)


def test_h5_skill_selection_keeps_qc_workflow_first() -> None:
    assert skill_sub_dirs_for_route(WorkflowRoute.H5)[0] == "qc-workflow"
    assert "app-workflow" not in skill_sub_dirs_for_route(WorkflowRoute.H5)
    assert skill_sub_dirs_for_route(WorkflowRoute.H5)[-2:] == (
        "green-content-safety",
        "green-process-evidence",
    )


def test_app_skill_selection_includes_green_rules_without_generic_qc_workflow() -> None:
    assert skill_sub_dirs_for_route(WorkflowRoute.APP) == (
        "app-workflow",
        "app-product-capability",
        "green-content-safety",
        "green-process-evidence",
    )
    assert "qc-workflow" not in skill_sub_dirs_for_route(WorkflowRoute.APP)
    assert "phase" not in skill_sub_dirs_for_route(WorkflowRoute.APP)
    assert "app-script-rhythm" not in skill_sub_dirs_for_route(WorkflowRoute.APP)


def test_unknown_route_is_rejected() -> None:
    with pytest.raises(ValueError, match="WorkflowRoute"):
        skill_sub_dirs_for_route("unknown")  # type: ignore[arg-type]


def test_generic_main_agent_mounts_h5_skills_without_app_workflow(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _provider(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(main_bootstrap, "build_skills_context_provider", _provider)
    monkeypatch.setattr(main_bootstrap, "build_default_capability_manifest", lambda: ())
    monkeypatch.setattr(
        main_bootstrap, "build_chat_client", lambda _settings, **_kwargs: object()
    )
    monkeypatch.setattr(main_bootstrap, "build_main_agent", lambda **kwargs: kwargs)

    main_bootstrap._build_main_agent(main_bootstrap.get_settings().agent_runtime)

    assert captured["sub_dirs"] == (
        "qc-workflow",
        "phase",
        "rule",
        "navigation-boundary-skill",
        "green-content-safety",
        "green-process-evidence",
    )
