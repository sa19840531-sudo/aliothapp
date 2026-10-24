"""七站 Web 浏览器侧只读回归；不提交表单，也不代替正式 Worker 验收。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.api.app.application.web_desktop_navigation import (  # noqa: E402
    extract_navigation_candidates,
)
from services.api.app.application.web_guide_candidate import (  # noqa: E402
    select_web_guide_candidates,
)
from services.api.app.application.web_guide_page import assess_web_guide_landing  # noqa: E402
from services.api.app.infrastructure.browser.page_capture import (  # noqa: E402
    PageCaptureMode,
    PageCaptureRequest,
    PlaywrightPageCaptureBrowser,
)
from services.api.app.infrastructure.browser.web_desktop_capture_policy import (  # noqa: E402
    WebDesktopCapturePolicyBrowser,
)
from services.api.app.infrastructure.browser.web_guide_browser import (  # noqa: E402
    WebGuidePageBrowser,
)

SITES = (
    ("chigua", "https://chigua.com/"),
    ("heiliao", "https://heiliao.com/"),
    ("hl365", "https://hl365.com/"),
    ("91jav1", "https://www.91jav1.com/"),
    ("91porna", "http://91porna.com/"),
    ("huangguoai", "http://huangguoai.com/"),
    ("avhub360", "https://avhub360.com/"),
)
PROFILE = {
    "viewport_width": 1920, "viewport_height": 1080,
    "screen_width": 1920, "screen_height": 1080,
    "device_scale_factor": 1.0, "is_mobile": False,
    "has_touch": False, "color_scheme": "light",
}


class LocalWebBrowser(PlaywrightPageCaptureBrowser):
    def __init__(self, *, chrome_path: str):
        super().__init__()
        self._chrome_path = chrome_path

    async def _launch_browser(self, playwright):
        return await playwright.chromium.launch(
            headless=True, executable_path=self._chrome_path,
        )


def _artifact(result, artifact_type):
    for artifact in result.artifacts:
        if artifact.artifact_type == artifact_type:
            return artifact.content
    return None


def _png(content):
    if isinstance(content, dict) and content.get("encoding") == "base64":
        return base64.b64decode(content["data"], validate=True)
    if isinstance(content, dict) and content.get("frames"):
        return _png(content["frames"][0])
    return None


def _save_capture(folder: Path, name: str, result):
    screenshot = _png(_artifact(result, "screenshot"))
    dom = _artifact(result, "dom")
    snapshot = _artifact(result, "ui_snapshot")
    if screenshot:
        (folder / f"{name}.png").write_bytes(screenshot)
    if isinstance(dom, str):
        (folder / f"{name}.html").write_text(dom, encoding="utf-8")
    if isinstance(snapshot, dict):
        (folder / f"{name}.json").write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8",
        )
    return {
        "status": result.execution_status,
        "final_url": result.final_url,
        "screenshot": bool(screenshot),
        "dom": isinstance(dom, str) and bool(dom),
        "snapshot": isinstance(snapshot, dict),
        "snapshot_elements": len(snapshot.get("elements", [])) if isinstance(snapshot, dict) else 0,
    }, snapshot if isinstance(snapshot, dict) else {}


def _navigation_audit(html: str, snapshot: dict, url: str) -> dict:
    """在当前留存快照上运行生产菜单提取器，区分发现与可执行。"""
    if not html or not url:
        return {"status": "missing_evidence"}
    try:
        all_entries = extract_navigation_candidates(
            html, base_url=url, snapshot=snapshot, include_blocked=True,
        )
        actionable = extract_navigation_candidates(
            html, base_url=url, snapshot=snapshot,
        )
    except Exception as exc:  # noqa: BLE001 - 审计失败不影响浏览器侧采集
        return {"status": "failed", "error": type(exc).__name__}
    return {
        "status": "ok", "all_count": len(all_entries),
        "actionable_count": len(actionable),
        "risk_counts": dict(Counter(entry.risk_level for entry in all_entries)),
        "actionable_sample": [
            {"label": entry.label[:80], "url": entry.url}
            for entry in actionable[:10]
        ],
    }


def _candidate_audit(snapshot: dict, source_url: str, source_capture_id: str) -> list[dict]:
    candidates = select_web_guide_candidates(
        source_capture_id=source_capture_id,
        source_url=source_url, snapshot=snapshot,
    )
    observed = []
    for candidate in candidates:
        matches = [
            row for row in snapshot.get("elements", [])
            if isinstance(row, dict) and isinstance(row.get("href"), str)
            and urljoin(source_url, row["href"]) == candidate.destination_url
        ]
        observed.append({
            "url": candidate.destination_url, "kind": candidate.kind,
            "label": candidate.label,
            "in_web_navigation": any(
                row.get("within_web_navigation") is True for row in matches
            ),
            "in_footer": any(row.get("within_footer") is True for row in matches),
            "matching_elements": len(matches),
        })
    return observed


def _menu_urls(
    site: str, origin: str, snapshot: dict, catalog: dict, limit: int,
    *, html: str = "",
):
    entries = []
    matched = next((row for row in catalog["sites"] if urlsplit(row["origin"]).hostname
                    == urlsplit(origin).hostname), None)
    if matched is not None:
        entries = [entry["href"] for entry in matched["entries"]
                   if entry.get("decision") == "allowlisted_observe_only"
                   and entry.get("collection_eligibility", {}).get("eligible") is True]
    elif html:
        # 不能把通用 within_navigation 标记下的正文卡片当成顶部菜单。
        entries = [
            candidate.url for candidate in extract_navigation_candidates(
                html, base_url=origin, snapshot=snapshot,
            )
            if candidate.actionability == "actionable" and candidate.url
        ]
    urls = []
    for entry in entries:
        if not isinstance(entry, str) or not entry:
            continue
        url = urljoin(origin, entry)
        if urlsplit(url).hostname != urlsplit(origin).hostname or url in urls or url == origin:
            continue
        urls.append(url)
        if len(urls) == limit:
            break
    return urls


async def _capture(browser, session: str, url: str, *, observe: bool = False):
    return await browser.capture(PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE if observe else PageCaptureMode.GOTO,
        url=None if observe else url,
        session_id=session,
        web_capture_stage="web_page_ready_baseline" if observe else "web_entry_probe",
        startup_policy="direct_home",
        full_page=False, frame_count=1, timeout_ms=20_000,
        **PROFILE,
    ))


async def run_site(site: str, origin: str, *, output: Path, chrome_path: str,
                   catalog: dict, menu_limit: int):
    folder = output / site
    folder.mkdir(parents=True, exist_ok=True)
    browser_impl = LocalWebBrowser(chrome_path=chrome_path)
    browser = WebDesktopCapturePolicyBrowser(browser=browser_impl)
    observer = WebGuidePageBrowser(browser_launcher=browser_impl._launch_browser)
    session = f"guide-smoke-{site}"
    report = {"site": site, "origin": origin, "home": None,
              "guide_candidates": [], "guide_landings": [], "menus": [],
              "post_guide_home": None}
    try:
        home = await _capture(browser, session, origin)
        report["home"], snapshot = _save_capture(folder, "home", home)
        final_url = home.final_url or origin
        home_html = _artifact(home, "dom")
        report["home_navigation_audit"] = _navigation_audit(
            home_html if isinstance(home_html, str) else "", snapshot, final_url,
        )
        main_session = browser_impl._sessions.get(session)
        main_context = main_session.context if main_session is not None else None
        main_page = main_session.page if main_session is not None else None
        report["guide_candidates"] = _candidate_audit(
            snapshot, final_url, f"smoke-{site}-home",
        )
        candidates = select_web_guide_candidates(
            source_capture_id=f"smoke-{site}-home", source_url=final_url,
            snapshot=snapshot,
        )
        for candidate in candidates:
            if candidate.kind != "page_candidate":
                report["guide_landings"].append({
                    "url": candidate.destination_url, "status": "skipped_resource",
                })
                continue
            try:
                landing = await asyncio.wait_for(observer.observe(candidate), timeout=20)
                assessment = assess_web_guide_landing(landing)
                index = len(report["guide_landings"])
                (folder / f"guide-{index}.png").write_bytes(landing.screenshot)
                (folder / f"guide-{index}.html").write_text(landing.html, encoding="utf-8")
                (folder / f"guide-{index}.json").write_text(
                    json.dumps(landing.ui_snapshot, ensure_ascii=False), encoding="utf-8",
                )
                report["guide_landings"].append({
                    "url": candidate.destination_url, "final_url": landing.final_url,
                    "status": assessment.status, "page_type": assessment.page_type,
                    "reason_code": assessment.reason_code,
                    "screenshot": True, "dom": True, "snapshot": True,
                    "navigation_audit": _navigation_audit(
                        landing.html, dict(landing.ui_snapshot), landing.final_url,
                    ),
                })
            except Exception as exc:  # noqa: BLE001 - 单站支线失败后仍检查主会话
                report["guide_landings"].append({
                    "url": candidate.destination_url,
                    "status": "incomplete", "error": type(exc).__name__,
                    "reason_code": getattr(exc, "reason", None),
                })
        observed_session = browser_impl._sessions.get(session)
        report["main_session_identity"] = {
            "same_context": bool(
                main_context is not None and observed_session is not None
                and observed_session.context is main_context
            ),
            "same_page": bool(
                main_page is not None and observed_session is not None
                and observed_session.page is main_page
            ),
            "same_url_before_observe": bool(main_page and main_page.url == final_url),
        }
        after = await _capture(browser, session, final_url, observe=True)
        report["post_guide_home"], _ = _save_capture(folder, "post-guide-home", after)
        for index, url in enumerate(_menu_urls(
            site, final_url, snapshot, catalog, menu_limit,
            html=home_html if isinstance(home_html, str) else "",
        )):
            try:
                menu = await _capture(browser, session, url)
                saved, menu_snapshot = _save_capture(folder, f"menu-{index}", menu)
                menu_url = menu.final_url or url
                menu_html = _artifact(menu, "dom")
                report["menus"].append({
                    "requested_url": url, **saved,
                    "navigation_audit": _navigation_audit(
                        menu_html if isinstance(menu_html, str) else "",
                        menu_snapshot, menu_url,
                    ),
                    "guide_candidates": _candidate_audit(
                        menu_snapshot, menu_url, f"smoke-{site}-menu-{index}",
                    ),
                })
            except Exception as exc:  # noqa: BLE001 - 逐项记录实际采集失败
                report["menus"].append({
                    "requested_url": url, "status": "failed", "error": type(exc).__name__,
                })
            print(site, "menu", index + 1, report["menus"][-1]["status"], flush=True)
    except Exception as exc:  # noqa: BLE001 - 七站逐站独立统计
        report["site_error"] = type(exc).__name__
        report["site_error_message"] = str(exc)[:300]
    finally:
        await browser.close_session(session)
        (folder / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    print(site, "complete", json.dumps({
        "home": (report["home"] or {}).get("status"),
        "guides": [item["status"] for item in report["guide_landings"]],
        "menus": len(report["menus"]),
        "error": report.get("site_error"),
    }, ensure_ascii=False), flush=True)
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--menu-limit", type=int, default=10)
    parser.add_argument("--site", choices=[name for name, _ in SITES])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    catalog = json.loads((Path(__file__).resolve().parents[2]
                          / "services/api/app/domain/page/web_site_menu_catalog.json")
                         .read_text(encoding="utf-8"))
    reports = []
    for site, origin in SITES:
        if args.site and site != args.site:
            continue
        reports.append(await run_site(
            site, origin, output=args.output, chrome_path=args.chrome,
            catalog=catalog, menu_limit=args.menu_limit,
        ))
    (args.output / "summary.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(main())
