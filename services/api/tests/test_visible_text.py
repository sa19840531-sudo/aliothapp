"""测试视觉可见文本采集契约。"""

from services.api.app.infrastructure.browser.visible_text import read_visible_text


async def test_visible_text_uses_occlusion_aware_browser_result() -> None:
    class Page:
        def __init__(self) -> None:
            self.inner_text_calls = 0

        async def evaluate(self, script: str) -> str:
            assert "elementsFromPoint" in script
            assert "createTreeWalker" in script
            return "开屏广告\n关闭"

        async def inner_text(self, selector: str) -> str:
            self.inner_text_calls += 1
            return "被遮挡的底层分类 PG娱乐 pg娱乐"

    page = Page()

    assert await read_visible_text(page) == "开屏广告\n关闭"
    assert page.inner_text_calls == 0


async def test_empty_occlusion_aware_result_does_not_fallback_to_hidden_body_text() -> None:
    class Page:
        def __init__(self) -> None:
            self.inner_text_calls = 0

        async def evaluate(self, script: str) -> str:
            return ""

        async def inner_text(self, selector: str) -> str:
            self.inner_text_calls += 1
            return "被全屏图片盖住的底层文本"

    page = Page()

    assert await read_visible_text(page) == ""
    assert page.inner_text_calls == 0


async def test_visible_text_falls_back_when_browser_geometry_is_unavailable() -> None:
    class Page:
        async def evaluate(self, script: str) -> str:
            raise RuntimeError("执行上下文已销毁")

        async def inner_text(self, selector: str) -> str:
            assert selector == "body"
            return "兼容采集文本"

    assert await read_visible_text(Page()) == "兼容采集文本"
