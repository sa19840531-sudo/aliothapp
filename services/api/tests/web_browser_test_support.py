"""仅供 Web 测试显式选择现成 Chrome；环境跳过严格限定在启动阶段。"""

import os
from pathlib import Path


class WebBrowserUnavailable(RuntimeError):
    """浏览器文件确实缺失，不包含页面/采集/断言异常。"""


async def launch_web_test_browser(browser_type, **kwargs):
    from playwright.async_api import Error

    executable = os.environ.get("ALIOTH_TEST_CHROME_EXECUTABLE")
    if executable:
        if not Path(executable).is_file():
            raise WebBrowserUnavailable("显式 Web 测试浏览器不存在")
        kwargs.setdefault("executable_path", executable)
    try:
        return await browser_type.launch(**kwargs)
    except Error as exc:
        if "Executable doesn't exist" in str(exc):
            raise WebBrowserUnavailable("默认 Web 测试浏览器未安装") from exc
        raise
