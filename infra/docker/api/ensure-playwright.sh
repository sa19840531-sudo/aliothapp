#!/bin/sh
set -eu

if ! python - <<'PY'
import importlib.util
import sys

sys.exit(0 if importlib.util.find_spec("playwright") is not None else 1)
PY
then
  echo "未检测到 Playwright Python 依赖，开始安装后端开发依赖..."
  python -m pip install --no-cache-dir -e ".[dev]"
fi

if ! python - <<'PY'
import asyncio

from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        await browser.close()


asyncio.run(main())
PY
then
  echo "未检测到可启动的 Chromium，开始安装 Playwright Chromium..."
  python -m playwright install --with-deps chromium
fi

exec "$@"
