"""App Explorer 默认组合的回归测试。"""

from services.api.app.agents.explorer import bootstrap


def test_default_explorer_mounts_app_workflow_skill(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class SpyExplorer:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(bootstrap, "MafExplorerAgent", SpyExplorer)
    monkeypatch.setattr(bootstrap, "build_chat_client", lambda _settings, **_kwargs: object())
    bootstrap.build_default_explorer_agent.cache_clear()

    bootstrap.build_default_explorer_agent()

    assert captured["required_workflow_skills"] == (
        "app-workflow",
        "app-product-capability",
    )
    providers = captured["context_providers"]
    assert isinstance(providers, list)
    assert len(providers) == 1
    provider = providers[0]
    assert getattr(provider, "source_id", None) == "app-workflow"
