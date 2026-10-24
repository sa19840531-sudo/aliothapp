"""引导页补采必须使用独立浏览器生命周期。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from io import BytesIO

import pytest
from PIL import Image

from services.api.app.application.web_guide_candidate import WebGuideCandidate
from services.api.app.infrastructure.browser import web_guide_browser


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (1920, 1080)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_guide_observer_owns_browser_and_captures_same_landing(monkeypatch) -> None:
    events = []
    class Playwright:
        async def __aenter__(self):
            events.append("playwright_start")
            return self
        async def __aexit__(self, *args):
            events.append("playwright_stop")
    class Browser:
        async def new_context(self):
            class ProbePage:
                async def evaluate(self, expression):
                    assert expression == "navigator.userAgent"
                    return "Mozilla/5.0 HeadlessChrome/154.0.0.0 Safari/537.36"

            class ProbeContext:
                async def new_page(self):
                    return ProbePage()

                async def close(self):
                    pass

            return ProbeContext()

        async def close(self):
            events.append("browser_close")
    class View:
        url = "https://guide.example/app"
        async def title(self): return "下载 APP"
        async def content(self): return "<main><h1>下载 APP</h1></main>"
        async def screenshot(self): return _png()
        async def ui_snapshot(self): return {"elements": []}
        async def har_entries(self): return [{"resource_type": "document", "url": self.url}]
    class Transport:
        har_entries = [{"resource_type": "document", "url": "https://guide.example/app"}]
    @asynccontextmanager
    async def isolated(**kwargs):
        events.append(("isolated", kwargs["url"], kwargs["enable_javascript"]))
        assert kwargs["user_agent"] == "Mozilla/5.0 Chrome/154.0.0.0 Safari/537.36"
        yield View(), Transport()
        events.append("context_close")
    async def launch(_playwright):
        events.append("browser_launch")
        return Browser()
    monkeypatch.setattr(web_guide_browser, "isolated_ad_page", isolated)
    observer = web_guide_browser.WebGuidePageBrowser(
        browser_launcher=launch, playwright_factory=Playwright,
    )
    candidate = WebGuideCandidate(
        candidate_id="c", source_capture_id="s", source_url="https://site.example/",
        destination_url="https://guide.example/app", label="官方 APP", kind="page_candidate",
    )
    landing = await observer.observe(candidate)
    assert landing.final_url == "https://guide.example/app"
    assert landing.redirect_chain[-1] == landing.final_url
    assert events == [
        "playwright_start", "browser_launch",
        ("isolated", "https://guide.example/app", True),
        "context_close", "browser_close", "playwright_stop",
    ]
