"""Web 站点隔离登录浏览器侧回归；凭据只从仓库外白名单读取。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.validation.web_guide_seven_site_smoke import (  # noqa: E402
    LocalWebBrowser,
    _capture,
    _save_capture,
)
from services.api.app.agents.explorer.web_login_agent import (  # noqa: E402
    WebLoginLocalVisualAgent,
)
from services.api.app.application.web_login_account_inspection import (  # noqa: E402
    WebLoginAccountInspector,
)
from services.api.app.application.web_login_allowlist import (  # noqa: E402
    load_web_login_allowlist,
)
from services.api.app.application.web_login_auth_origins import (  # noqa: E402
    DEFAULT_WEB_LOGIN_AUTH_ORIGINS_PATH,
    load_web_login_auth_origins,
)
from services.api.app.application.web_login_branch import WebLoginBranch  # noqa: E402
from services.api.app.infrastructure.browser.web_desktop_capture_policy import (  # noqa: E402
    WebDesktopCapturePolicyBrowser,
)
from services.api.app.infrastructure.browser.web_login_session import (  # noqa: E402
    WebLoginSessionBrowser,
)


class FileArtifactStore:
    def __init__(self, folder: Path):
        self._folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    async def store(
        self, *, task_id: str, page_capture_id: str, artifact_id: str,
        artifact_type: str, content: bytes | str,
    ):
        del task_id, page_capture_id
        suffix = ".png" if artifact_type == "screenshot" else ".html"
        path = self._folder / f"{artifact_id}{suffix}"
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return SimpleNamespace(artifact_id=artifact_id, storage_path=str(path))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", default="https://hifans1.com/")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    allowlist = load_web_login_allowlist(args.allowlist)
    if allowlist.resolve(args.url) is None:
        raise RuntimeError("WEB_LOGIN_NOT_ALLOWLISTED")
    auth_policy = load_web_login_auth_origins(
        DEFAULT_WEB_LOGIN_AUTH_ORIGINS_PATH, allowlist=allowlist,
    )
    browser_impl = LocalWebBrowser(chrome_path=args.chrome)
    browser = WebDesktopCapturePolicyBrowser(browser=browser_impl)
    session = "web-login-new-site-smoke"
    report: dict[str, object] = {"site": args.url}
    try:
        home = await _capture(browser, session, args.url)
        report["home"], _ = _save_capture(args.output, "home", home)
        main_session = browser_impl._sessions.get(session)
        main_page = main_session.page if main_session is not None else None
        main_context = main_session.context if main_session is not None else None
        login_browser = WebLoginSessionBrowser(
            browser_impl, auth_policy=auth_policy,
        )
        inspector = WebLoginAccountInspector(
            browser=login_browser,
            artifact_store=FileArtifactStore(args.output / "account"),
        )
        branch = WebLoginBranch(
            allowlist=allowlist, browser=login_browser,
            visual=WebLoginLocalVisualAgent(), inspector=inspector,
        )
        outcome = await branch.run(
            session_id=session,
            target_url=home.final_url or args.url,
            task_id="web-login-new-site-smoke",
            source_capture_id="web-login-new-site-home",
            adult_declared=True,
        )
        report["login"] = asdict(outcome)
        observed = browser_impl._sessions.get(session)
        report["main_session_identity"] = {
            "same_page": main_page is not None and observed is not None
            and observed.page is main_page,
            "same_context": main_context is not None and observed is not None
            and observed.context is main_context,
            "same_url": main_page is not None
            and main_page.url == (home.final_url or args.url),
        }
        after = await _capture(
            browser, session, home.final_url or args.url, observe=True,
        )
        report["post_login_main"] = _save_capture(
            args.output, "post-login-main", after,
        )[0]
    except Exception as exc:  # noqa: BLE001 - 不让外部异常或页面内容进入日志
        report["error_type"] = type(exc).__name__
    finally:
        await browser.close_session(session)
        (args.output / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    print(json.dumps({
        "home_status": (report.get("home") or {}).get("status"),
        "login_status": (report.get("login") or {}).get("status"),
        "login_reason": (report.get("login") or {}).get("reason_code"),
        "account_status": (report.get("login") or {}).get("account_status"),
        "error_type": report.get("error_type"),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
