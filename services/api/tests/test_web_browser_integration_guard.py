"""真实浏览器可用时，采集器异常必须失败，不得伪装成环境跳过。"""

import os

import pytest


@pytest.mark.asyncio
async def test_collector_failure_is_not_hidden_by_browser_skip(monkeypatch):
    from playwright.async_api import BrowserType

    from services.api.tests import test_web_obstruction_snapshot as snapshot_tests

    original = BrowserType.launch

    async def launch(browser_type, **kwargs):
        executable = os.environ.get("ALIOTH_TEST_CHROME_EXECUTABLE")
        if executable:
            kwargs["executable_path"] = executable
        return await original(browser_type, **kwargs)

    async def broken_collector(*args, **kwargs):
        raise RuntimeError("T10_COLLECTOR_REAL_FAILURE")

    monkeypatch.setattr(BrowserType, "launch", launch)
    monkeypatch.setattr(snapshot_tests, "_collect_ui_snapshot", broken_collector)
    try:
        with pytest.raises(RuntimeError, match="T10_COLLECTOR_REAL_FAILURE"):
            await snapshot_tests.test_production_snapshot_suppresses_iframe_captcha_layer()
    except pytest.skip.Exception:
        pytest.fail("真实 collector 异常被错误吞为浏览器环境 skip")
