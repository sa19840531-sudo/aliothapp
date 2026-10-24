"""Web Interaction Runtime 进程级持有权与 Tool 借用边界。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.api.app.core.config import WorkflowSettings
from services.api.app.infrastructure.browser import capture_runtime as runtime_module
from services.api.app.infrastructure.browser.capture_runtime import (
    WebCaptureRuntime,
    get_or_create_web_capture_runtime,
    reset_web_capture_runtime_for_tests,
    set_web_capture_runtime_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    reset_web_capture_runtime_for_tests()
    yield
    reset_web_capture_runtime_for_tests()


def test_get_or_create_web_capture_runtime_is_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[object] = []
    browser_arguments: list[dict[str, object]] = []

    class FakeBrowser:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            browser_arguments.append(dict(kwargs))

    def fake_build(*, browser=None):  # noqa: ANN001
        item = SimpleNamespace(
            _browser=browser or FakeBrowser(),
            _driver=object(),
            coordinator=object(),
            session_command_gate=object(),
        )
        built.append(item)
        return item

    monkeypatch.setattr(runtime_module, "build_default_web_capture_runtime", fake_build)
    monkeypatch.setattr(
        runtime_module,
        "PlaywrightPageCaptureBrowser",
        FakeBrowser,
    )
    monkeypatch.setattr(
        runtime_module,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(page_capture_concurrency=2),
            workflow=WorkflowSettings(
                h5_logical_surface_enabled=True,
                h5_logical_surface_shadow=True,
                h5_browser_proxy_server=None,
                h5_browser_proxy_bypass=None,
            ),
        ),
    )

    first = get_or_create_web_capture_runtime()
    second = get_or_create_web_capture_runtime()

    assert first is second
    assert len(built) == 1
    assert browser_arguments == [
        {
            "max_concurrent_captures": 2,
            "logical_surface_enabled": True,
            "logical_surface_shadow": True,
            "h5_visual_coordinate_mode": "off",
            "cli_mode": "off",
            "cli_script": WorkflowSettings().h5_cli_script,
            "cli_workspace_root": WorkflowSettings().h5_cli_workspace_root,
        }
    ]


def test_get_or_create_web_capture_runtime_passes_h5_browser_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_arguments: list[dict[str, object]] = []

    class FakeBrowser:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            browser_arguments.append(dict(kwargs))

    monkeypatch.setattr(
        runtime_module,
        "build_default_web_capture_runtime",
        lambda *, browser=None: SimpleNamespace(
            _browser=browser,
            _driver=object(),
            coordinator=object(),
            session_command_gate=object(),
        ),
    )
    monkeypatch.setattr(runtime_module, "PlaywrightPageCaptureBrowser", FakeBrowser)
    monkeypatch.setattr(
        runtime_module,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(page_capture_concurrency=1),
            workflow=WorkflowSettings(
                h5_logical_surface_enabled=False,
                h5_logical_surface_shadow=False,
                h5_browser_proxy_server="http://host.docker.internal:7897",
                h5_browser_proxy_bypass="localhost,127.0.0.1",
            ),
        ),
    )

    get_or_create_web_capture_runtime()

    assert browser_arguments == [
        {
            "max_concurrent_captures": 1,
            "logical_surface_enabled": False,
            "logical_surface_shadow": False,
            "h5_visual_coordinate_mode": "off",
            "cli_mode": "off",
            "cli_script": WorkflowSettings().h5_cli_script,
            "cli_workspace_root": WorkflowSettings().h5_cli_workspace_root,
            "proxy_server": "http://host.docker.internal:7897",
            "proxy_bypass": "localhost,127.0.0.1",
        }
    ]


def test_page_capture_tools_borrows_from_injected_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.api.app.agents.tools import page_capture_tools

    browser = object()

    class StubRuntime:
        def borrow_browser_for_legacy_agent_tool(self):
            return browser

        async def close_browser_session(self, session_id: str) -> None:
            self.closed = session_id

    stub = StubRuntime()
    set_web_capture_runtime_for_tests(stub)  # type: ignore[arg-type]

    assert page_capture_tools._get_page_capture_browser() is browser


def test_web_capture_runtime_exposes_surface_driver_property() -> None:
    driver = object()
    runtime = WebCaptureRuntime(
        coordinator=object(),  # type: ignore[arg-type]
        session_command_gate=object(),  # type: ignore[arg-type]
        _browser=object(),  # type: ignore[arg-type]
        _executor=object(),  # type: ignore[arg-type]
        _evidence_buffer=object(),  # type: ignore[arg-type]
        _driver=driver,
    )
    assert runtime.surface_driver is driver
    assert runtime.borrow_browser_for_legacy_agent_tool() is runtime._browser
