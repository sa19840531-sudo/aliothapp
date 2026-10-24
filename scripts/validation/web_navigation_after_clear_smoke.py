"""七站 Web 首页清障前后导航与引导入口诊断；不提交表单。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.validation.web_guide_seven_site_smoke import (  # noqa: E402
    SITES,
    LocalWebBrowser,
    _artifact,
    _candidate_audit,
    _capture,
    _navigation_audit,
    _save_capture,
)
from services.api.app.infrastructure.browser.web_desktop_capture_policy import (  # noqa: E402
    WebDesktopCapturePolicyBrowser,
)
from services.api.app.infrastructure.browser.web_login_session import (  # noqa: E402
    WebLoginSessionBrowser,
)


async def run_site(name: str, url: str, *, chrome: str, output: Path) -> dict:
    folder = output / name
    folder.mkdir(parents=True, exist_ok=True)
    browser_impl = LocalWebBrowser(chrome_path=chrome)
    browser = WebDesktopCapturePolicyBrowser(browser=browser_impl)
    session = f"web-navigation-clear-{name}"
    report: dict[str, object] = {"site": name, "origin": url}
    try:
        before = await _capture(browser, session, url)
        report["before"], before_snapshot = _save_capture(folder, "before", before)
        final_url = before.final_url or url
        parsed = urlsplit(final_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        before_html = _artifact(before, "dom")
        report["before_navigation"] = _navigation_audit(
            before_html if isinstance(before_html, str) else "",
            before_snapshot, final_url,
        )
        report["before_guides"] = _candidate_audit(
            before_snapshot, final_url, f"{name}-before",
        )
        clearance = WebLoginSessionBrowser(browser_impl)
        report["clearance"] = await clearance.clear_obstructions(
            session, origin, adult_declared=True,
        )
        after = await _capture(browser, session, final_url, observe=True)
        report["after"], after_snapshot = _save_capture(folder, "after", after)
        after_url = after.final_url or final_url
        after_html = _artifact(after, "dom")
        report["after_navigation"] = _navigation_audit(
            after_html if isinstance(after_html, str) else "",
            after_snapshot, after_url,
        )
        report["after_guides"] = _candidate_audit(
            after_snapshot, after_url, f"{name}-after",
        )
    except Exception as exc:  # noqa: BLE001 - 逐站记录失败后继续后续站点
        report["error_type"] = type(exc).__name__
    finally:
        await browser.close_session(session)
        (folder / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    print(name, json.dumps({
        "clearance": report.get("clearance"),
        "before_actionable": (report.get("before_navigation") or {}).get("actionable_count"),
        "after_actionable": (report.get("after_navigation") or {}).get("actionable_count"),
        "after_guides": len(report.get("after_guides") or []),
        "error": report.get("error_type"),
    }, ensure_ascii=False), flush=True)
    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    reports = []
    for name, url in SITES:
        reports.append(await run_site(name, url, chrome=args.chrome, output=args.output))
    (args.output / "summary.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(main())
