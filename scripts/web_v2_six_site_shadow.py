"""六站 Web V2 菜单固件与生产 Catalog 的离线影子对账。"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.web_v2_live_evidence import (  # noqa: E402, I001
    build_live_process_nodes as _build_live_process_nodes,
    normalize_live_site_evidence_bundle as _normalize_live_site_evidence_bundle,
    required_capture_ids_from_ledger,
    valid_action_execution_id as _valid_action_execution_id,
    validate_live_site_evidence_bundle as _validate_live_site_evidence_bundle,
    validate_report_evidence_identity as _validate_report_evidence_identity,
)
from services.api.app.application.web_assessment_evidence import (  # noqa: E402
    sanitize_web_model_reason,
)
from services.api.app.application.web_menu_exploration import (  # noqa: E402
    WEB_MENU_COLLECTION_LIMIT,  # noqa: F401
    WebMenuCollectionPlan,
    plan_web_menu_collection,
)
from services.api.app.domain.page.web_origin import canonical_web_origin  # noqa: E402
from services.api.app.domain.page.web_site_menu_registry import (  # noqa: E402
    RuntimeWebSiteMenuRegistry,
    WebCatalogStatus,
    WebMenuDecision,
    WebSiteMenuCatalog,
    WebSiteMenuEntry,
    WebSiteMenuRegistry,
)
_required_capture_ids_from_ledger = required_capture_ids_from_ledger

DEFAULT_AUDIT_PATH = ROOT / "docs/data/web_v2_six_site_menu_allowlist.json"
DEFAULT_CATALOG_PATH = ROOT / "services/api/app/domain/page/web_site_menu_catalog.json"
DEFAULT_LIVE_SITE_TIMEOUT_SECONDS = 11_400.0
EXPECTED_LIVE_EXECUTED_MENU_COUNT = 51
EXPECTED_LIVE_MENU_LIMIT_SKIPPED_COUNT = 106

SIX_SITE_TARGETS = (
    "https://heiliao.com/",
    "https://hl365.com/",
    "https://www.91jav1.com/",
    "http://91porna.com/",
    "http://huangguoai.com/",
    "https://avhub360.com/",
)


@dataclass(frozen=True, slots=True)
class SixSiteAcceptanceFixture:
    """单站 Web V2 回归必须满足的固定验收边界。"""

    site_id: str
    entry_url: str
    canonical_home_url: str
    allowed_redirect_origins: tuple[str, ...]
    required_page_types: tuple[str, ...]
    menu_limit: int = 10
    expected_obstructions: tuple[str, ...] = ("advertisement", "age_gate")
    detail_target_required: bool = True
    accepted_report_stages: tuple[str, ...] = ("preview", "final_review", "final")
    allowed_manual_blocking_reasons: tuple[str, ...] = (
        "popup_blocked",
        "human_verification_blocked",
    )


_ALL_WEB_PAGE_TYPES = (
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail",
)
SIX_SITE_ACCEPTANCE_FIXTURES = (
    SixSiteAcceptanceFixture(
        site_id="heiliao_com",
        entry_url="https://heiliao.com/",
        canonical_home_url="https://heiliao.com/",
        allowed_redirect_origins=("https://heiliao.com",),
        required_page_types=_ALL_WEB_PAGE_TYPES,
    ),
    SixSiteAcceptanceFixture(
        site_id="hl365_com",
        entry_url="https://hl365.com/",
        canonical_home_url="https://hl365.com/",
        allowed_redirect_origins=("https://hl365.com",),
        required_page_types=_ALL_WEB_PAGE_TYPES,
    ),
    SixSiteAcceptanceFixture(
        site_id="www_91jav1_com",
        entry_url="https://www.91jav1.com/",
        canonical_home_url="https://www.91jav1.com/",
        allowed_redirect_origins=("https://www.91jav1.com",),
        required_page_types=_ALL_WEB_PAGE_TYPES,
    ),
    SixSiteAcceptanceFixture(
        site_id="91porna_com",
        entry_url="http://91porna.com/",
        canonical_home_url="http://91porna.com/",
        allowed_redirect_origins=("http://91porna.com", "https://91porna.com"),
        required_page_types=_ALL_WEB_PAGE_TYPES,
    ),
    SixSiteAcceptanceFixture(
        site_id="huangguoai_com",
        entry_url="http://huangguoai.com/",
        canonical_home_url="http://huangguoai.com/",
        allowed_redirect_origins=(
            "http://huangguoai.com",
            "https://huangguoai.com",
        ),
        required_page_types=_ALL_WEB_PAGE_TYPES,
    ),
    SixSiteAcceptanceFixture(
        site_id="avhub360_com",
        entry_url="https://avhub360.com/",
        canonical_home_url="https://avhub360.com/",
        allowed_redirect_origins=("https://avhub360.com",),
        required_page_types=("home", "video_list", "video_detail"),
    ),
)

_TARGET_SITE_IDS = dict(
    zip(
        SIX_SITE_TARGETS,
        (
            "heiliao_com",
            "hl365_com",
            "www_91jav1_com",
            "91porna_com",
            "huangguoai_com",
            "avhub360_com",
        ),
        strict=True,
    )
)
_SITE_ID_TARGETS = {site_id: target_url for target_url, site_id in _TARGET_SITE_IDS.items()}
_UUID_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_WEB_CANDIDATE_ID_PATTERN = re.compile(r"web-candidate:[0-9a-f]{64}")
_WEB_ERROR_CODE_PATTERN = re.compile(r"WEB_[A-Z0-9_]+")
_WEB_CANONICAL_PAGE_TYPES = frozenset(
    {"home", "video_list", "video_detail", "content_list", "content_detail"}
)


@dataclass(frozen=True, slots=True)
class SiteShadowResult:
    """单站审计固件与生产 Catalog 的身份对账结果。"""

    target_url: str
    site_id: str
    audit_entry_count: int
    catalog_entry_count: int
    missing_entry_ids: tuple[str, ...]
    extra_entry_ids: tuple[str, ...]
    changed_entry_ids: tuple[str, ...]

    @property
    def drift_count(self) -> int:
        return len(self.missing_entry_ids) + len(self.extra_entry_ids) + len(self.changed_entry_ids)


@dataclass(frozen=True, slots=True)
class SixSiteShadowReport:
    """六站离线影子验收汇总。"""

    site_count: int
    active_anchor_count: int
    home_baseline_count: int
    allowlisted_observe_only_count: int
    blocked_cross_origin_count: int
    comment_only_anchor_count: int
    summary_mismatch_fields: tuple[str, ...]
    missing_site_ids: tuple[str, ...]
    extra_site_ids: tuple[str, ...]
    sites: tuple[SiteShadowResult, ...]

    @property
    def drift_count(self) -> int:
        return (
            len(self.summary_mismatch_fields)
            + len(self.missing_site_ids)
            + len(self.extra_site_ids)
            + sum(site.drift_count for site in self.sites)
        )


@dataclass(frozen=True, slots=True)
class LiveSiteSubmission:
    """通过正式完整质检 API 提交的单站任务回执。"""

    target_url: str
    task_id: str
    run_id: str
    status: str


@dataclass(frozen=True, slots=True)
class LiveSiteResult:
    """单站从提交、终态到最终报告的完整闭环。"""

    target_url: str
    task_id: str
    run_id: str
    task_status: str
    report_status: str
    failure_reason: str | None
    report_stage: str
    is_final: bool
    exploration_summary: dict[str, object]
    report_overall_status: str | None = None
    report_error_code: str | None = None
    submission_status: str | None = None
    evidence_bundle: dict[str, object] = field(default_factory=dict)
    process_nodes: tuple[dict[str, object], ...] = ()
    report_url: str | None = None
    workstation_report_path: str | None = None
    page_type_review_count: int | None = None
    pending_confirmation_count: int | None = None
    duplicate_url_count: int | None = None
    inspection_total_count: int | None = None
    inspection_pending_count: int | None = None
    logical_page_assessments: tuple[dict[str, object], ...] = ()
    screenshot_selections: tuple[dict[str, object], ...] = ()
    server_completion_gate_passed: bool | None = None
    server_report_bound: bool | None = None
    task_list_report_verified: bool = False
    task_detail_report_verified: bool = False
    manual_page_reviews: tuple[dict[str, object], ...] = ()
    coverage_snapshot: dict[str, object] = field(default_factory=dict)
    acceptance_dimensions: dict[str, object] = field(default_factory=dict, init=False)

    def __post_init__(self):
        from scripts.web_acceptance_dimensions import acceptance_dimensions

        object.__setattr__(self, "acceptance_dimensions", acceptance_dimensions(self))


@dataclass(frozen=True, slots=True)
class LiveSixSiteReport:
    """六站正式探索聚合，验证错误存在时不得宣称通过。"""

    site_count: int
    menu_total: int
    menu_limit: int | None
    executed_same_origin_count: int
    expected_executed_same_origin_count: int
    menu_limit_skipped_count: int
    expected_menu_limit_skipped_count: int
    home_baseline_count: int
    detail_count: int
    blocked_cross_origin_count: int
    frontier_remaining: int
    validation_errors: tuple[str, ...]
    sites: tuple[LiveSiteResult, ...]
    requested_targets: tuple[str, ...] = SIX_SITE_TARGETS

    @property
    def acceptance_scope(self) -> str:
        return "all_six_sites" if self.requested_targets == SIX_SITE_TARGETS else "selected_sites"

    @property
    def acceptance_failures(self) -> tuple[str, ...]:
        """只消费既有六维状态，既不重算覆盖也不把正式带缺口收口当完整验收。"""
        expected = {
            "contract_integrity": "passed", "actual_coverage": "complete",
            "ledger_integrity": "terminal_statuses_recorded", "manual_review": "completed",
            "server_gate": "passed", "report_binding": "final",
        }
        return tuple(
            f"site_{index}:{dimension}:{site.acceptance_dimensions.get(dimension, {}).get('status', 'unverified')}"
            for index, site in enumerate(self.sites, 1)
            for dimension, status in expected.items()
            if site.acceptance_dimensions.get(dimension, {}).get("status") != status
        )

    @property
    def live_validation_passed(self) -> bool:
        return not self.validation_errors

    @property
    def manual_report_review_passed(self) -> bool:
        return bool(self.sites) and all(site.acceptance_dimensions["manual_review"]["status"] == "completed" for site in self.sites)

    @property
    def server_completion_gate_passed(self) -> bool:
        return bool(self.sites) and all(
            site.server_completion_gate_passed is True
            and site.server_report_bound is True
            and site.report_stage == "final"
            and site.is_final
            for site in self.sites
        )

    @property
    def passed(self) -> bool:
        return (
            self.live_validation_passed
            and self.manual_report_review_passed
            and self.server_completion_gate_passed
            and not self.acceptance_failures
        )


def _apply_manual_report_review(
    report: LiveSixSiteReport,
    *,
    reviewer: Callable[[LiveSiteResult], tuple],
) -> LiveSixSiteReport:
    """逐站录入任务列表与详情报告入口的显式人工复核结果。"""

    reviewed_sites: list[LiveSiteResult] = []
    for site in report.sites:
        decision = reviewer(site)
        if (
            not isinstance(decision, tuple)
            or len(decision) not in {2, 3}
            or any(type(value) is not bool for value in decision[:2])
            or (len(decision) == 3 and not isinstance(decision[2], tuple))
        ):
            raise ValueError("人工报告复核结果必须是两个布尔值")
        reviewed_sites.append(
            replace(
                site,
                task_list_report_verified=decision[0],
                task_detail_report_verified=decision[1],
                manual_page_reviews=decision[2] if len(decision) == 3 else (),
            )
        )
    return replace(report, sites=tuple(reviewed_sites))


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Catalog 根节点必须是对象：{path}")
    return payload


def _normalize_live_targets(targets: Sequence[str]) -> tuple[str, ...]:
    """接受安全 HTTPS 目标，规范化身份并保留请求顺序。"""

    if isinstance(targets, str | bytes):
        raise ValueError("live targets 必须是站点 URL 序列")
    selected = tuple(_canonical_https_target(target) for target in targets)
    if not selected:
        raise ValueError("live targets 至少需要一个站点")
    if len(selected) != len(set(selected)):
        raise ValueError("live targets 不得包含重复站点")
    return selected


def _canonical_https_target(value: str) -> str:
    """目标不携带凭据、查询、片段或浏览器歧义字符。"""
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("live target 必须为安全 HTTPS URL")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or "?" in value or "#" in value or "\\" in value):
        raise ValueError("live target 必须为无凭据、查询和片段的 HTTPS URL")
    return canonical_web_origin(value) + _canonical_live_target_path(parsed.path or "/")


def _canonical_live_target_path(path: str) -> str:
    """按浏览器特殊 URL 规则消解点段，保留编码斜杠及重复斜杠身份。"""
    segments = path.split("/")[1:]
    result: list[str] = []
    for index, segment in enumerate(segments):
        dot_segment = re.sub("%2e", ".", segment, flags=re.IGNORECASE)
        if dot_segment in {".", ".."}:
            if dot_segment == ".." and result:
                result.pop()
            if index == len(segments) - 1:
                result.append("")
        else:
            result.append(segment)
    # 路径百分号保留原有转义；Unicode 与浏览器路径不安全字符统一序列化。
    return urllib.parse.quote("/" + "/".join(result), safe="/%!$&'()*+,-.:;=@[]^_|~")


def _targets_from_site_ids(site_ids: Sequence[str] | None) -> tuple[str, ...]:
    """兼容目录站点快捷选择；提交时统一使用 HTTPS。"""

    if not site_ids:
        raise ValueError("live 模式必须指定 --target-url 或 --site-id")
    selected_ids = tuple(site_ids)
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("--site-id 不得重复")
    return _normalize_live_targets(tuple(
        target.replace("http://", "https://", 1)
        for site_id, target in _SITE_ID_TARGETS.items() if site_id in selected_ids
    ))


def _site_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sites = payload.get("sites")
    if not isinstance(sites, list):
        raise ValueError("Catalog.sites 必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for site in sites:
        if not isinstance(site, dict) or not isinstance(site.get("site_id"), str):
            raise ValueError("Catalog.sites[*].site_id 必须是字符串")
        site_id = site["site_id"]
        if site_id in result:
            raise ValueError(f"Catalog 存在重复 site_id：{site_id}")
        result[site_id] = site
    return result


def _entry_map(site: dict[str, Any] | None) -> dict[str, tuple[Any, ...]]:
    if site is None:
        return {}
    entries = site.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"站点 {site.get('site_id')} 的 entries 必须是数组")
    result: dict[str, tuple[Any, ...]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("entry_id"), str):
            raise ValueError("Catalog.entries[*].entry_id 必须是字符串")
        entry_id = entry["entry_id"]
        if entry_id in result:
            raise ValueError(f"Catalog 存在重复 entry_id：{entry_id}")
        menu_path = entry.get("menu_path")
        if isinstance(menu_path, list):
            normalized_menu_path = tuple(menu_path)
        else:
            normalized_menu_path = menu_path
        result[entry_id] = (
            normalized_menu_path,
            entry.get("href"),
            entry.get("decision"),
        )
    return result


def _decision_count(catalog_sites: dict[str, dict[str, Any]], decision: str) -> int:
    return sum(
        1
        for site in catalog_sites.values()
        for entry in site.get("entries", [])
        if entry.get("decision") == decision
    )


def reconcile_all_fixture_sites(
    *,
    audit_path: Path = DEFAULT_AUDIT_PATH,
    catalog_path: Path = DEFAULT_CATALOG_PATH,
) -> SixSiteShadowReport:
    """对账审计固件和生产 Catalog，不发起任何外部网络访问。"""

    audit = _load_json(audit_path)
    catalog = _load_json(catalog_path)
    audit_sites = _site_map(audit)
    catalog_sites = _site_map(catalog)
    expected_site_ids = set(_TARGET_SITE_IDS.values())

    site_results: list[SiteShadowResult] = []
    for target_url, site_id in _TARGET_SITE_IDS.items():
        audit_entries = _entry_map(audit_sites.get(site_id))
        catalog_entries = _entry_map(catalog_sites.get(site_id))
        common_ids = audit_entries.keys() & catalog_entries.keys()
        site_results.append(
            SiteShadowResult(
                target_url=target_url,
                site_id=site_id,
                audit_entry_count=len(audit_entries),
                catalog_entry_count=len(catalog_entries),
                missing_entry_ids=tuple(sorted(audit_entries.keys() - catalog_entries.keys())),
                extra_entry_ids=tuple(sorted(catalog_entries.keys() - audit_entries.keys())),
                changed_entry_ids=tuple(
                    sorted(
                        entry_id
                        for entry_id in common_ids
                        if audit_entries[entry_id] != catalog_entries[entry_id]
                    )
                ),
            )
        )

    summary_fields = (
        "site_count",
        "active_anchor_count",
        "home_baseline_count",
        "allowlisted_navigation_count",
        "blocked_cross_origin_count",
        "ignored_commented_anchor_count",
    )
    audit_summary = audit.get("summary", {})
    catalog_summary = catalog.get("summary", {})
    summary_mismatches = tuple(
        field for field in summary_fields if audit_summary.get(field) != catalog_summary.get(field)
    )
    active_anchor_count = sum(len(site.get("entries", [])) for site in catalog_sites.values())

    return SixSiteShadowReport(
        site_count=len(catalog_sites),
        active_anchor_count=active_anchor_count,
        home_baseline_count=_decision_count(catalog_sites, "home_baseline"),
        allowlisted_observe_only_count=_decision_count(catalog_sites, "allowlisted_observe_only"),
        blocked_cross_origin_count=_decision_count(catalog_sites, "blocked_cross_origin"),
        comment_only_anchor_count=int(catalog_summary.get("ignored_commented_anchor_count", 0)),
        summary_mismatch_fields=summary_mismatches,
        missing_site_ids=tuple(sorted(expected_site_ids - catalog_sites.keys())),
        extra_site_ids=tuple(sorted(catalog_sites.keys() - expected_site_ids)),
        sites=tuple(site_results),
    )


def render_markdown(report: SixSiteShadowReport) -> str:
    """把离线对账结果渲染为可审计 Markdown。"""

    status = "通过" if report.drift_count == 0 else "失败"
    lines = [
        "# Web V2 六站菜单离线影子验收",
        "",
        f"- 结论：{status}",
        f"- 站点数：{report.site_count}",
        f"- 活动入口：{report.active_anchor_count}",
        f"- 首页基线：{report.home_baseline_count}",
        f"- 同源白名单菜单：{report.allowlisted_observe_only_count}",
        f"- 跨域阻断入口：{report.blocked_cross_origin_count}",
        f"- 注释锚点：{report.comment_only_anchor_count}",
        f"- 漂移项：{report.drift_count}",
        "",
        "| 站点 | 审计条目 | 生产条目 | 漂移 |",
        "|---|---:|---:|---:|",
    ]
    lines.extend(
        f"| {site.target_url} | {site.audit_entry_count} | "
        f"{site.catalog_entry_count} | {site.drift_count} |"
        for site in report.sites
    )
    return "\n".join(lines) + "\n"


def submit_live_sites(
    *,
    api_base_url: str,
    access_token: str,
    adult_self_attestation: str,
    sender: Callable[[str, dict[str, object], dict[str, str]], dict[str, object]],
    targets: Sequence[str] = (),
) -> tuple[LiveSiteSubmission, ...]:
    """显式声明成年后，按调用方目标顺序提交正式完整质检任务。"""

    if adult_self_attestation != "adult-v1":
        raise ValueError("live 模式必须显式传入 adult-v1 成年声明")
    if not access_token.strip():
        raise ValueError("live 模式必须提供访问令牌")
    endpoint = _validate_api_base_url(api_base_url) + "/api/v2/tasks"
    submissions: list[LiveSiteSubmission] = []
    for index, target_url in enumerate(_normalize_live_targets(targets), start=1):
        payload = _live_submission_payload(index=index, target_url=target_url)
        response = sender(
            endpoint,
            payload,
            {
                "Authorization": f"Bearer {access_token.strip()}",
                "Idempotency-Key": f"web-v2-live-{uuid4()}",
            },
        )
        submissions.append(
            LiveSiteSubmission(
                target_url=target_url,
                task_id=str(response.get("task_id") or ""),
                run_id=str(response.get("run_id") or ""),
                status=str(response.get("status") or ""),
            )
        )
        if not submissions[-1].task_id or not submissions[-1].run_id:
            raise RuntimeError(f"{target_url} 提交回执缺少 task_id 或 run_id")
    return tuple(submissions)


def run_live_sites(
    *,
    api_base_url: str,
    access_token: str,
    adult_self_attestation: str,
    requester: Callable[
        [str, str, dict[str, object] | None, dict[str, str]],
        dict[str, object],
    ],
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    poll_interval_seconds: float = 2.0,
    site_timeout_seconds: float = DEFAULT_LIVE_SITE_TIMEOUT_SECONDS,
    targets: Sequence[str] = (),
) -> LiveSixSiteReport:
    """逐站提交并等待可用报告，上一站未形成终态证据时不提交下一站。"""

    if adult_self_attestation != "adult-v1":
        raise ValueError("live 模式必须显式传入 adult-v1 成年声明")
    token = access_token.strip()
    if not token:
        raise ValueError("live 模式必须提供访问令牌")
    if not math.isfinite(poll_interval_seconds) or not math.isfinite(site_timeout_seconds):
        raise ValueError("轮询间隔和单站超时必须为有限值")
    if poll_interval_seconds < 0 or site_timeout_seconds <= 0:
        raise ValueError("轮询间隔和单站超时必须有效")
    api_base = _validate_api_base_url(api_base_url)
    selected_targets = _normalize_live_targets(targets)
    headers = {"Authorization": f"Bearer {token}"}
    results: list[LiveSiteResult] = []
    for index, target_url in enumerate(selected_targets, start=1):
        submission_payload = requester(
            "POST",
            f"{api_base}/api/v2/tasks",
            _live_submission_payload(index=index, target_url=target_url),
            {
                **headers,
                "Idempotency-Key": f"web-v2-live-{uuid4()}",
            },
        )
        task_id = str(submission_payload.get("task_id") or "").strip()
        run_id = str(submission_payload.get("run_id") or "").strip()
        submission_status = str(submission_payload.get("status") or "").strip()
        if not task_id or not run_id:
            raise RuntimeError(f"{target_url} 提交回执缺少 task_id 或 run_id")
        deadline = monotonic() + site_timeout_seconds
        while True:
            task = requester(
                "GET",
                f"{api_base}/api/v1/workstation/tasks/{urllib.parse.quote(task_id, safe='')}",
                None,
                headers,
            )
            task_status = str(task.get("status") or "")
            report_status = str(task.get("report_status") or "")
            if task_status in {
                "completed",
                "done",
                "failed",
                "canceled",
                "cancelled",
                "review",
            }:
                break
            if monotonic() >= deadline:
                raise TimeoutError(f"{target_url} 在限定时间内未形成任务终态")
            sleep(poll_interval_seconds)
        task_run_id = str(task.get("run_id") or "").strip()
        if task_run_id and task_run_id != run_id:
            raise RuntimeError(f"{target_url} 任务详情 run_id 与提交回执不一致")
        report_url = (
            f"{api_base}/api/v1/workstation/reports/"
            f"{urllib.parse.quote(task_id, safe='')}?"
            + urllib.parse.urlencode({"agent_run_id": run_id})
        )
        while True:
            try:
                report = requester("GET", report_url, None, headers)
            except urllib.error.HTTPError as exc:
                if exc.code not in {404, 409, 425}:
                    raise
                report = {}
            report_stage = str(report.get("report_stage") or "")
            report_availability = str(report.get("report_availability") or "")
            final_available = report_stage == "final" and report.get("is_final") is True
            terminal_preview_available = (
                report_stage in {"preview", "final_review"}
                and report_availability == "preview"
                and isinstance(report.get("page_exploration"), dict)
            )
            if final_available or terminal_preview_available:
                break
            if monotonic() >= deadline:
                raise TimeoutError(f"{target_url} 在限定时间内未形成可用探索报告")
            sleep(poll_interval_seconds)
        exploration = report.get("page_exploration")
        if not isinstance(exploration, dict):
            raise RuntimeError(f"{target_url} 最终报告缺少 page_exploration")
        report_summary = report.get("summary")
        report_summary = report_summary if isinstance(report_summary, dict) else {}
        evidence_url = (
            f"{api_base}/api/v1/workstation/tasks/"
            f"{urllib.parse.quote(task_id, safe='')}/web-evidence"
        )
        try:
            raw_evidence_bundle = requester("GET", evidence_url, None, headers)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            raw_evidence_bundle = {}
        evidence_bundle = _normalize_live_site_evidence_bundle(raw_evidence_bundle)
        web_preview = report.get("web_preview")
        web_preview = web_preview if isinstance(web_preview, dict) else {}
        page_type_review = web_preview.get("page_type_review")
        page_type_review = page_type_review if isinstance(page_type_review, dict) else {}
        completion_gate = web_preview.get("completion_gate")
        completion_gate = completion_gate if isinstance(completion_gate, dict) else {}
        inspection_progress = web_preview.get("inspection_progress")
        inspection_progress = (
            inspection_progress if isinstance(inspection_progress, dict) else {}
        )
        logical_page_assessments = _normalize_live_logical_page_assessments(
            report.get("web_logical_pages")
        )
        page_type_review_count = _optional_nonnegative_int(
            page_type_review.get("pending")
        )
        if page_type_review_count is None:
            page_type_review_count = sum(
                page.get("page_type") not in _WEB_CANONICAL_PAGE_TYPES
                or page.get("classification_status") != "classified"
                for page in logical_page_assessments
            )
        inspection_total_count = _optional_nonnegative_int(
            inspection_progress.get("total")
        )
        if inspection_total_count is None:
            inspection_total_count = len(logical_page_assessments)
        inspection_pending_count = _optional_nonnegative_int(
            inspection_progress.get("pending")
        )
        if inspection_pending_count is None:
            inspection_pending_count = sum(
                page.get("inspection_status")
                not in {"completed", "degraded", "failed"}
                for page in logical_page_assessments
            )
        pending_confirmation_count = _optional_nonnegative_int(
            completion_gate.get("pending_confirmations")
        )
        if pending_confirmation_count is None:
            confirmation_progress = report.get("confirm_progress")
            confirmation_progress = (
                confirmation_progress if isinstance(confirmation_progress, dict) else {}
            )
            pending_confirmation_count = _optional_nonnegative_int(
                confirmation_progress.get("pending")
            )
        final_contract_available = (
            final_available and report.get("export_allowed") is True
        )
        exploration_summary = dict(exploration)
        if not final_available:
            exploration_summary = _hydrate_preview_exploration_summary(
                target_url=target_url,
                report_exploration=exploration_summary,
                evidence_bundle=evidence_bundle,
            )
        results.append(
            LiveSiteResult(
                target_url=target_url,
                task_id=task_id,
                run_id=run_id,
                task_status=task_status,
                report_status=report_status,
                failure_reason=(
                    str(task["failure_reason"]) if task.get("failure_reason") is not None else None
                ),
                report_stage=str(report.get("report_stage") or ""),
                is_final=report.get("is_final") is True,
                exploration_summary=exploration_summary,
                coverage_snapshot=(
                    dict(exploration.get("web_coverage") or {}, task_id=task_id, agent_run_id=run_id)
                    if report.get("task_id") == task_id
                    and report_summary.get("run_id") == run_id
                    and isinstance(exploration.get("web_coverage"), dict)
                    else {}
                ),
                report_overall_status=(
                    str(report_summary.get("overall_status") or "").strip() or None
                ),
                report_error_code=(str(report_summary.get("error_code") or "").strip() or None),
                submission_status=submission_status or None,
                evidence_bundle=evidence_bundle,
                report_url=report_url,
                workstation_report_path=(
                    "/reports?" + urllib.parse.urlencode({"task": task_id})
                ),
                page_type_review_count=page_type_review_count,
                pending_confirmation_count=pending_confirmation_count,
                duplicate_url_count=_formal_page_duplicate_url_count(
                    evidence_bundle
                ),
                inspection_total_count=inspection_total_count,
                inspection_pending_count=inspection_pending_count,
                logical_page_assessments=logical_page_assessments,
                screenshot_selections=_build_live_screenshot_selections(
                    evidence_bundle
                ),
                server_completion_gate_passed=(
                    completion_gate.get("gate_passed") is True
                    or final_contract_available
                ),
                server_report_bound=(
                    completion_gate.get("report_bound") is True
                    or final_contract_available
                ),
                process_nodes=_build_live_process_nodes(
                    submission_status=submission_status or None,
                    task_status=task_status,
                    report_stage=str(report.get("report_stage") or ""),
                    evidence_bundle=evidence_bundle,
                ),
            )
        )
    return _aggregate_live_results(
        tuple(results),
        expected_targets=selected_targets,
    )


def _hydrate_preview_exploration_summary(
    *,
    target_url: str,
    report_exploration: dict[str, object],
    evidence_bundle: dict[str, object],
) -> dict[str, object]:
    """用终态证据账本补齐预览报告不携带的站点级菜单统计。"""

    result = dict(report_exploration)
    site_id = evidence_bundle.get("site_id") or report_exploration.get("site_id") or RuntimeWebSiteMenuRegistry().resolve_site_context(target_url).site.site_id
    evidence_entries = evidence_bundle.get("entries")
    evidence_entries = evidence_entries if isinstance(evidence_entries, list) else []
    statuses = [
        str(entry.get("status") or "") for entry in evidence_entries if isinstance(entry, dict)
    ]
    status_counts = Counter(statuses)
    evidence_summary = evidence_bundle.get("summary")
    evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
    nonterminal_statuses = {"pending", "home_pending", "menu_reserved", "detail_reserved"}
    public_ledger = []
    for entry in evidence_entries:
        if not isinstance(entry, dict):
            continue
        public_ledger.append({
            **entry,
            "entry_id": entry.get("menu_entry_id"),
            "source_capture_id": entry.get("menu_source_capture_id"),
            "canonical_url": entry.get("canonical_url") or (
                target_url if entry.get("status") == "home_baseline" else ""
            ),
        })

    result.update(
        {
            "site_id": site_id,
            "menu_total": len(evidence_entries),
            "menu_limit": evidence_bundle.get("menu_limit", report_exploration.get("menu_limit")),
            "menu_ledger": [row for row in public_ledger if row.get("source_kind") != "runtime_discovered"],
            "runtime_discovered_ledger": [row for row in public_ledger if row.get("source_kind") == "runtime_discovered"],
            "executed_same_origin_count": _safe_int(evidence_summary.get("menu_count")),
            "menu_limit_skipped_count": status_counts.get("menu_limit_skipped", 0),
            "status_counts": dict(status_counts),
            "frontier_remaining": sum(status in nonterminal_statuses for status in statuses),
        }
    )
    return result


def _live_submission_payload(*, index: int, target_url: str) -> dict[str, object]:
    return {
        "task_name": f"Web V2 实况探索 {index}",
        "target": {
            "platform": "web",
            "url": target_url,
            "browser_profile_id": "desktop_fhd_1920x1080_v1",
            "adult_self_attestation": {
                "declared": True,
                "statement_version": "adult-v1",
            },
        },
        "channel": "api",
        "scan_mode": "full",
        "remark": "Web V2 只探索验收；人机验证出现即停止当前站点。",
    }


def _aggregate_live_results(
    sites: tuple[LiveSiteResult, ...],
    *,
    expected_targets: Sequence[str],
) -> LiveSixSiteReport:
    """按本轮目标和运行账本聚合，静态目录不再决定探索分母。"""
    selected_targets = _normalize_live_targets(expected_targets)
    errors: list[str] = []
    if tuple(site.target_url for site in sites) != selected_targets:
        errors.append("实况目标或执行顺序与请求目标不一致")
    registry = WebSiteMenuRegistry(DEFAULT_CATALOG_PATH)
    expected_executed = 0
    for site in sites:
        context = registry.resolve_site_context(site.target_url).site
        runtime_context = RuntimeWebSiteMenuRegistry().resolve_site_context(site.target_url).site
        if site.exploration_summary.get("site_id") == runtime_context.site_id:
            from scripts.web_v2_runtime_live_validation import validate_runtime_live_site
            expected_executed += sum(
                _safe_int(row.get("menu_action_count"))
                for row in site.evidence_bundle.get("entries", []) if isinstance(row, dict)
            )
            errors.extend(validate_runtime_live_site(site, runtime_context))
            errors.extend(_validate_live_acceptance_dimensions(site))
            continue
        ledger = site.exploration_summary.get("menu_ledger")
        ledger = ledger if isinstance(ledger, list) else []
        entries: list[WebSiteMenuEntry] = []
        catalog_entries = {entry.entry_id: entry for entry in context.entries}
        home_entries = {entry.entry_id: entry for entry in context.entries
                        if entry.decision is WebMenuDecision.HOME_BASELINE}
        for raw in ledger:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("canonical_url") or "")
            try:
                same_origin = context.contains_url_origin(url)
            except ValueError:
                same_origin = False
                errors.append(f"{site.target_url}: 菜单 URL 无效")
            entry_id = str(raw.get("entry_id") or "")
            if entry_id in home_entries:
                entries.append(home_entries[entry_id])
                continue
            if entry_id in catalog_entries:
                entries.append(catalog_entries[entry_id])
                continue
            menu_path = raw.get("menu_path")
            if not isinstance(menu_path, list) or not menu_path or any(
                not isinstance(label, str) or not label.strip() for label in menu_path
            ):
                errors.append(f"{site.target_url}: menu_path 无效")
                menu_path = []
            decision = (WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY if same_origin else
                        WebMenuDecision.BLOCKED_CROSS_ORIGIN)
            entries.append(WebSiteMenuEntry(
                entry_id=entry_id, menu_path=tuple(menu_path),
                href=url, canonical_url=url, decision=decision, is_same_origin=same_origin,
            ))
        runtime_site = replace(context, entries=tuple(entries))
        plan = WebMenuCollectionPlan(
            selected_entries=tuple(entry for entry in entries
                                   if entry.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY),
            skipped_entries=(), alias_of={},
        )
        expected_executed += len(plan.selected_entries)
        runtime_catalog = {
            "site_id": context.site_id, "origin": context.origin,
            "entries": [{"entry_id": entry.entry_id, "menu_path": list(entry.menu_path),
                         "href": entry.href, "decision": entry.decision.value}
                        for entry in entries],
        }
        errors.extend(_validate_live_site_ledger(
            site, runtime_catalog, collection_plan=plan, registry_site=runtime_site,
            expected_menu_limit=None,
        ))
        if site.report_stage != "final" or not site.is_final:
            errors.append(f"{site.target_url}: 报告尚未正式封存")
        errors.extend(_validate_live_site_evidence_bundle(
            target_url=site.target_url, task_id=site.task_id, run_id=site.run_id,
            site_id=context.site_id, expected_menu_count=len(plan.selected_entries),
            bundle=site.evidence_bundle, allow_count_limit_skips=False,
        ))
        errors.extend(_validate_report_evidence_identity(
            site, require_navigation_identity=True, catalog_entry_ids=frozenset(catalog_entries),
        ))
        errors.extend(_validate_live_acceptance_dimensions(site))

    return LiveSixSiteReport(
        site_count=len(sites), requested_targets=selected_targets,
        menu_total=sum(_safe_int(site.exploration_summary.get("menu_total")) for site in sites),
        menu_limit=None,
        executed_same_origin_count=sum(_safe_int(site.exploration_summary.get(
            "executed_same_origin_count")) for site in sites),
        expected_executed_same_origin_count=expected_executed,
        menu_limit_skipped_count=sum(_status_count(site.exploration_summary,
                                                  "menu_limit_skipped") for site in sites),
        expected_menu_limit_skipped_count=0,
        home_baseline_count=sum(_status_count(site.exploration_summary, "home_baseline") for site in sites),
        detail_count=sum(_status_count(site.exploration_summary, "detail_captured") for site in sites),
        blocked_cross_origin_count=sum(_status_count(site.exploration_summary, "cross_origin_blocked") for site in sites),
        frontier_remaining=sum(_safe_int(site.exploration_summary.get("frontier_remaining")) for site in sites),
        validation_errors=tuple(errors), sites=sites,
    )


def _aggregate_historical_fixture_results(
    sites: tuple[LiveSiteResult, ...],
    *,
    expected_targets: Sequence[str] = SIX_SITE_TARGETS,
) -> LiveSixSiteReport:
    selected_targets = tuple(expected_targets)
    if not selected_targets or len(set(selected_targets)) != len(selected_targets):
        raise ValueError("历史夹具目标必须非空且不可重复")
    menu_total = sum(_safe_int(item.exploration_summary.get("menu_total")) for item in sites)
    executed = sum(
        _safe_int(item.exploration_summary.get("executed_same_origin_count")) for item in sites
    )
    limit_skipped = sum(
        _status_count(item.exploration_summary, "menu_limit_skipped") for item in sites
    )
    frontier = sum(_safe_int(item.exploration_summary.get("frontier_remaining")) for item in sites)
    home = sum(_status_count(item.exploration_summary, "home_baseline") for item in sites)
    detail = sum(
        _safe_int(
            item.evidence_bundle.get("summary", {}).get("detail_count")
            if isinstance(item.evidence_bundle.get("summary"), dict)
            else 0
        )
        for item in sites
    )
    cross_origin = sum(
        _status_count(item.exploration_summary, "cross_origin_blocked") for item in sites
    )
    errors: list[str] = []
    catalog = _load_json(DEFAULT_CATALOG_PATH)
    catalog_sites = _site_map(catalog)
    registry = WebSiteMenuRegistry(DEFAULT_CATALOG_PATH)
    registry_sites = {site.site_id: site for site in registry.sites}
    collection_plans = {
        site_id: plan_web_menu_collection(site, menu_limit=10)
        for site_id, site in registry_sites.items()
    }
    catalog_expected_executed = sum(
        len(plan.selected_entries) for plan in collection_plans.values()
    )
    catalog_expected_limit_skipped = sum(
        len(plan.skipped_entries) for plan in collection_plans.values()
    )
    if catalog_expected_executed != EXPECTED_LIVE_EXECUTED_MENU_COUNT:
        errors.append(
            "Catalog 每站菜单分布漂移："
            f"expected_executed={EXPECTED_LIVE_EXECUTED_MENU_COUNT}, "
            f"actual={catalog_expected_executed}"
        )
    if catalog_expected_limit_skipped != EXPECTED_LIVE_MENU_LIMIT_SKIPPED_COUNT:
        errors.append(
            "Catalog 每站菜单分布漂移："
            f"expected_limit_skipped={EXPECTED_LIVE_MENU_LIMIT_SKIPPED_COUNT}, "
            f"actual={catalog_expected_limit_skipped}"
        )
    selected_catalog_sites = [
        catalog_sites[_TARGET_SITE_IDS[target_url]] for target_url in selected_targets
    ]
    expected_executed = sum(
        len(collection_plans[str(site.get("site_id") or "")].selected_entries)
        for site in selected_catalog_sites
    )
    expected_limit_skipped = sum(
        len(collection_plans[str(site.get("site_id") or "")].skipped_entries)
        for site in selected_catalog_sites
    )
    expected = {
        "site_count": len(selected_targets),
        "menu_total": sum(len(site.get("entries", [])) for site in selected_catalog_sites),
        "executed_same_origin_count": expected_executed,
        "menu_limit_skipped_count": expected_limit_skipped,
        "home_baseline_count": len(selected_targets),
        "detail_count": expected_executed,
        "blocked_cross_origin_count": sum(
            sum(
                isinstance(entry, dict) and entry.get("decision") == "blocked_cross_origin"
                for entry in site.get("entries", [])
            )
            for site in selected_catalog_sites
        ),
        "frontier_remaining": 0,
    }
    actual = {
        "site_count": len(sites),
        "menu_total": menu_total,
        "executed_same_origin_count": executed,
        "menu_limit_skipped_count": limit_skipped,
        "home_baseline_count": home,
        "detail_count": detail,
        "blocked_cross_origin_count": cross_origin,
        "frontier_remaining": frontier,
    }
    actual_targets = tuple(item.target_url for item in sites)
    if actual_targets != selected_targets:
        errors.append("六站目标或执行顺序与选定白名单子集不一致")
    errors.extend(
        f"{name}: expected={expected_value}, actual={actual[name]}"
        for name, expected_value in expected.items()
        if actual[name] != expected_value
    )
    for item in sites:
        expected_site_id = _TARGET_SITE_IDS.get(item.target_url)
        if expected_site_id is None:
            errors.append(f"{item.target_url}: 不是六站固定目标")
            continue
        catalog_site = catalog_sites.get(expected_site_id)
        if catalog_site is None:
            errors.append(f"{item.target_url}: Catalog 缺少站点 {expected_site_id}")
            continue
        collection_plan = collection_plans[expected_site_id]
        expected_site_menu_count = len(collection_plan.selected_entries)
        if item.report_stage == "final" and item.is_final:
            errors.extend(
                _validate_live_site_ledger(
                    item,
                    catalog_site,
                    collection_plan=collection_plan,
                    registry_site=registry_sites[expected_site_id],
                )
            )
            errors.extend(
                _validate_live_site_evidence_bundle(
                    target_url=item.target_url,
                    task_id=item.task_id,
                    run_id=item.run_id,
                    site_id=expected_site_id,
                    expected_menu_count=expected_site_menu_count,
                    bundle=item.evidence_bundle,
                )
            )
            errors.extend(_validate_report_evidence_identity(item))
        else:
            errors.extend(
                _validate_live_preview_site(
                    item,
                    catalog_site,
                    collection_plan=collection_plan,
                )
            )
        errors.extend(_validate_live_acceptance_dimensions(item))
    return LiveSixSiteReport(
        site_count=len(sites),
        menu_total=menu_total,
        menu_limit=10,
        executed_same_origin_count=executed,
        expected_executed_same_origin_count=expected_executed,
        menu_limit_skipped_count=limit_skipped,
        expected_menu_limit_skipped_count=expected_limit_skipped,
        home_baseline_count=home,
        detail_count=detail,
        blocked_cross_origin_count=cross_origin,
        frontier_remaining=frontier,
        validation_errors=tuple(errors),
        sites=sites,
        requested_targets=selected_targets,
    )


def _validate_live_preview_site(
    site: LiveSiteResult,
    catalog_site: dict[str, Any],
    *,
    collection_plan: WebMenuCollectionPlan | None = None,
) -> list[str]:
    """验证预览的终态证据身份，不要求尚未封存的正式报告账本。"""

    prefix = f"{site.target_url} preview"
    errors: list[str] = []
    if not _valid_uuid_id(site.task_id):
        errors.append(f"{prefix}: task_id 格式无效")
    if not _valid_uuid_id(site.run_id):
        errors.append(f"{prefix}: run_id 格式无效")
    if site.task_status not in {"review", "completed", "done"}:
        errors.append(f"{prefix}: 任务不是可查看终态")
    if site.report_stage not in {"preview", "final_review"}:
        errors.append(f"{prefix}: 报告阶段不是 preview/final_review")
    if site.failure_reason:
        errors.append(f"{prefix}: failure_reason 非空")

    bundle = site.evidence_bundle
    expected_site_id = str(catalog_site.get("site_id") or "")
    if bundle.get("task_id") != site.task_id:
        errors.append(f"{prefix}: evidence task_id 不一致")
    if bundle.get("agent_run_id") != site.run_id:
        errors.append(f"{prefix}: evidence agent_run_id 不一致")
    if bundle.get("site_id") != expected_site_id:
        errors.append(f"{prefix}: evidence site_id 不一致")
    if bundle.get("status") not in {"evidence_ready", "needs_review"}:
        errors.append(f"{prefix}: evidence 状态不可用于预览")

    catalog_entries = catalog_site.get("entries")
    catalog_entries = catalog_entries if isinstance(catalog_entries, list) else []
    evidence_entries = bundle.get("entries")
    if not isinstance(evidence_entries, list):
        return [*errors, f"{prefix}: evidence entries 不是数组"]
    expected_ids = {
        str(entry.get("entry_id") or "") for entry in catalog_entries if isinstance(entry, dict)
    }
    actual_ids = [
        str(entry.get("menu_entry_id") or "")
        for entry in evidence_entries
        if isinstance(entry, dict)
    ]
    if len(actual_ids) != len(evidence_entries):
        errors.append(f"{prefix}: evidence entries 包含非对象")
    if len(actual_ids) != len(set(actual_ids)):
        errors.append(f"{prefix}: menu_entry_id 重复")
    if set(actual_ids) != expected_ids:
        errors.append(f"{prefix}: evidence 菜单身份与 Catalog 不一致")

    nonterminal_statuses = {"pending", "home_pending", "menu_reserved", "detail_reserved", ""}
    if any(
        str(entry.get("status") or "") in nonterminal_statuses
        for entry in evidence_entries
        if isinstance(entry, dict)
    ):
        errors.append(f"{prefix}: evidence 仍含非终态菜单")
    for entry in evidence_entries:
        if not isinstance(entry, dict):
            continue
        for field_name in ("menu_page_type", "detail_page_type"):
            page_type = entry.get(field_name)
            if page_type is not None and page_type not in _WEB_CANONICAL_PAGE_TYPES:
                errors.append(f"{prefix}: {field_name} 含目录外页型")
    summary = bundle.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    if collection_plan is None:
        collection_plan = plan_web_menu_collection(
            WebSiteMenuRegistry(DEFAULT_CATALOG_PATH).resolve_site(
                str(catalog_site.get("origin") or "")
            ),
            menu_limit=10,
        )
    expected_menu_count = len(collection_plan.selected_entries)
    if _safe_int(summary.get("home_count")) != 1:
        errors.append(f"{prefix}: 首页证据计数必须为 1")
    if _safe_int(summary.get("menu_count")) != expected_menu_count:
        errors.append(f"{prefix}: 已执行菜单计数与预算不一致")
    detail_count = _safe_int(summary.get("detail_count"))
    if detail_count > expected_menu_count:
        errors.append(f"{prefix}: 详情计数超过菜单预算")
    return errors


def _validate_live_site_ledger(
    site: LiveSiteResult,
    catalog_site: dict[str, Any],
    *,
    collection_plan: WebMenuCollectionPlan | None = None,
    registry_site: WebSiteMenuCatalog | None = None,
    expected_menu_limit: int | None = 10,
) -> list[str]:
    errors: list[str] = []
    summary = site.exploration_summary
    expected_site_id = str(catalog_site.get("site_id") or "")
    prefix = f"{site.target_url}"
    if not _valid_uuid_id(site.task_id):
        errors.append(f"{prefix}: task_id 格式无效")
    if not _valid_uuid_id(site.run_id):
        errors.append(f"{prefix}: run_id 格式无效")
    if site.failure_reason:
        errors.append(f"{prefix}: failure_reason 非空")
    if site.report_error_code and not _valid_web_error_code(site.report_error_code):
        errors.append(f"{prefix}: report_error_code 格式无效")
    if site.report_overall_status == "error" or site.report_error_code:
        errors.append(
            f"{prefix}: 最终报告错误 "
            f"overall_status={site.report_overall_status or '-'}, "
            f"error_code={site.report_error_code or '-'}"
        )
    if summary.get("site_id") != expected_site_id:
        errors.append(
            f"{prefix}: site_id expected={expected_site_id}, actual={summary.get('site_id')}"
        )
    entries = catalog_site.get("entries")
    ledger = summary.get("menu_ledger")
    if not isinstance(entries, list) or not isinstance(ledger, list):
        return [*errors, f"{prefix}: Catalog entries 或 menu_ledger 不是数组"]
    if _safe_int(summary.get("menu_total")) != len(entries):
        errors.append(f"{prefix}: menu_total 与本站 Catalog 不一致")
    if registry_site is None:
        registry_site = WebSiteMenuRegistry(DEFAULT_CATALOG_PATH).resolve_site(
            str(catalog_site.get("origin") or "")
        )
    if collection_plan is None:
        collection_plan = plan_web_menu_collection(
            registry_site,
            menu_limit=expected_menu_limit,
        )
    selected_entry_ids = {entry.entry_id for entry in collection_plan.selected_entries}
    expected_executed = len(selected_entry_ids)
    expected_limit_skipped = len(collection_plan.skipped_entries)
    registry_entries = {entry.entry_id: entry for entry in registry_site.entries}
    if "menu_limit" not in summary or summary.get("menu_limit") != expected_menu_limit:
        errors.append(f"{prefix}: menu_limit 必须为 {expected_menu_limit}")
    if _safe_int(summary.get("executed_same_origin_count")) != expected_executed:
        errors.append(
            f"{prefix}: executed_same_origin_count "
            f"expected={expected_executed}, "
            f"actual={_safe_int(summary.get('executed_same_origin_count'))}"
        )
    if _safe_int(summary.get("menu_limit_skipped_count")) != expected_limit_skipped:
        errors.append(
            f"{prefix}: menu_limit_skipped_count "
            f"expected={expected_limit_skipped}, "
            f"actual={_safe_int(summary.get('menu_limit_skipped_count'))}"
        )
    if _safe_int(summary.get("frontier_remaining")) != 0:
        errors.append(f"{prefix}: frontier_remaining 必须为 0")
    if len(ledger) != len(entries):
        errors.append(f"{prefix}: menu_ledger 长度 expected={len(entries)}, actual={len(ledger)}")
    raw_ledger = [raw for raw in ledger if isinstance(raw, dict)]
    if len(raw_ledger) != len(ledger):
        errors.append(f"{prefix}: menu_ledger 包含非对象条目")
    ids = [str(raw.get("entry_id") or "") for raw in raw_ledger]
    if len(ids) != len(set(ids)):
        errors.append(f"{prefix}: menu_ledger entry_id 重复")
    expected_by_id = {
        str(entry.get("entry_id") or ""): entry for entry in entries if isinstance(entry, dict)
    }
    if set(ids) != set(expected_by_id):
        missing = sorted(set(expected_by_id) - set(ids))
        extra = sorted(set(ids) - set(expected_by_id))
        errors.append(f"{prefix}: 菜单身份漂移 missing={missing}, extra={extra}")
    origin = str(catalog_site.get("origin") or "")
    for raw in raw_ledger:
        entry_id = str(raw.get("entry_id") or "")
        expected = expected_by_id.get(entry_id)
        if expected is None:
            continue
        errors.extend(
            _validate_live_ledger_entry(
                prefix,
                origin,
                expected,
                raw,
                should_execute=entry_id in selected_entry_ids,
                expected_skip_error_code=_expected_skip_error_code(
                    entry_id,
                    collection_plan=collection_plan,
                    registry_entry=registry_entries[entry_id],
                ),
                expected_alias_of=collection_plan.alias_of.get(entry_id),
            )
        )
    actual_counts = Counter(str(raw.get("status") or "") for raw in raw_ledger)
    reported = summary.get("status_counts")
    if not isinstance(reported, dict):
        errors.append(f"{prefix}: status_counts 不是对象")
    else:
        reported_nonzero = {
            str(status): _safe_int(count)
            for status, count in reported.items()
            if _safe_int(count) > 0
        }
        if reported_nonzero != dict(actual_counts):
            errors.append(
                f"{prefix}: status_counts 与逐菜单账本不一致 "
                f"reported={reported_nonzero}, actual={dict(actual_counts)}"
            )
    if site.task_status not in {"completed", "done"}:
        errors.append(f"{prefix}: 任务终态为 {site.task_status}")
    return errors


def _expected_skip_error_code(
    entry_id: str,
    *,
    collection_plan: WebMenuCollectionPlan,
    registry_entry: WebSiteMenuEntry,
) -> str | None:
    """复用生产采集计划解释未执行原因，避免把别名和不适用入口算成限额溢出。"""

    skipped_entry_ids = {entry.entry_id for entry in collection_plan.skipped_entries}
    if entry_id not in skipped_entry_ids:
        return None
    if entry_id in collection_plan.alias_of:
        return "WEB_MENU_LOGICAL_ALIAS"
    if registry_entry.status is WebCatalogStatus.DEPRECATED:
        return "WEB_MENU_CATALOG_DEPRECATED"
    if not registry_entry.collection_eligibility.eligible:
        return "WEB_MENU_COLLECTION_INELIGIBLE"
    return "WEB_MENU_LIMIT_SKIPPED"


def _validate_live_ledger_entry(
    prefix: str,
    origin: str,
    expected: dict[str, Any],
    actual: dict[str, object],
    *,
    should_execute: bool = True,
    expected_skip_error_code: str | None = None,
    expected_alias_of: str | None = None,
) -> list[str]:
    errors: list[str] = []
    entry_id = str(expected.get("entry_id") or "")
    label = f"{prefix} [{entry_id}]"
    capture_fields = (
        "source_capture_id",
        "final_capture_id",
        "menu_probe_capture_id",
        "menu_ready_capture_id",
        "detail_capture_id",
        "detail_probe_capture_id",
        "detail_ready_capture_id",
    )
    for field_name in capture_fields:
        field_value = actual.get(field_name)
        if field_value and not _valid_uuid_id(field_value):
            errors.append(f"{label}: {field_name} Capture ID 格式无效")
    for field_name in (
        "menu_action_execution_id",
        "detail_action_execution_id",
    ):
        field_value = actual.get(field_name)
        if field_value and not _valid_action_execution_id(field_value):
            errors.append(f"{label}: {field_name} Action ID 格式无效")
    for field_name in (
        "menu_readiness_evidence_capture_ids",
        "detail_readiness_evidence_capture_ids",
    ):
        field_value = actual.get(field_name)
        if isinstance(field_value, list) and any(not _valid_uuid_id(item) for item in field_value):
            errors.append(f"{label}: {field_name} Capture ID 格式无效")
    error_code = actual.get("error_code")
    if error_code and not _valid_web_error_code(error_code):
        errors.append(f"{label}: error_code 格式无效")
    raw_candidate_ids = actual.get("detail_candidate_ids")
    if isinstance(raw_candidate_ids, list) and any(
        not _valid_web_candidate_id(item) for item in raw_candidate_ids
    ):
        errors.append(f"{label}: 详情候选 ID 格式无效")
    selected_candidate_id = actual.get("selected_detail_candidate_id")
    if selected_candidate_id and not _valid_web_candidate_id(selected_candidate_id):
        errors.append(f"{label}: 已选详情候选 ID 格式无效")
    detail_action_count = actual.get("detail_action_count")
    if (
        isinstance(detail_action_count, bool)
        or not isinstance(detail_action_count, int)
        or not 0 <= detail_action_count <= 1
    ):
        errors.append(f"{label}: 详情动作次数必须为 0 或 1")
    menu_action_count = actual.get("menu_action_count")
    if (
        isinstance(menu_action_count, bool)
        or not isinstance(menu_action_count, int)
        or not 0 <= menu_action_count <= 1
    ):
        errors.append(f"{label}: 菜单动作次数必须为 0 或 1")
    expected_path = tuple(
        " ".join(str(item).split()).casefold() for item in expected.get("menu_path", [])
    )
    actual_path = actual.get("menu_path")
    normalized_actual_path = (
        tuple(" ".join(str(item).split()).casefold() for item in actual_path)
        if isinstance(actual_path, list)
        else ()
    )
    if normalized_actual_path != expected_path:
        errors.append(f"{label}: menu_path 与 Catalog 不一致")
    expected_url = urllib.parse.urljoin(f"{origin.rstrip('/')}/", str(expected.get("href") or ""))
    actual_url = str(actual.get("canonical_url") or "")
    if not _catalog_runtime_url_matches(expected_url, actual_url):
        errors.append(f"{label}: canonical_url 与 Catalog 不一致")
    decision = str(expected.get("decision") or "")
    status = str(actual.get("status") or "")
    if decision == "home_baseline":
        if menu_action_count != 0:
            errors.append(f"{label}: 首页菜单动作次数必须为 0")
        if detail_action_count != 0:
            errors.append(f"{label}: 首页详情动作次数必须为 0")
        if status != "home_baseline":
            errors.append(f"{label}: 首页终态必须为 home_baseline，实际为 {status}")
        if not actual.get("source_capture_id") or not actual.get("final_capture_id"):
            errors.append(f"{label}: 首页缺少来源或最终 Capture")
        home_probe = str(actual.get("menu_probe_capture_id") or "")
        home_ready = str(actual.get("menu_ready_capture_id") or "")
        page_type = actual.get("page_type")
        menu_page_type = actual.get("menu_page_type")
        canonical_home_type = menu_page_type in _WEB_CANONICAL_PAGE_TYPES and page_type in {
            None,
            menu_page_type,
        }
        historical_home_type = page_type == "home" and menu_page_type == "home"
        if (
            not (canonical_home_type or historical_home_type)
            or not home_probe
            or not home_ready
            or home_probe == home_ready
            or actual.get("final_capture_id") != home_ready
            or not _capture_lineage_matches(
                actual.get("menu_readiness_evidence_capture_ids"),
                probe_capture_id=home_probe,
                ready_capture_id=home_ready,
            )
        ):
            errors.append(f"{label}: 首页 probe/ready 与业务页型证据链无效")
        unexpected = _unexpected_populated_fields(
            actual,
            allowed_fields={
                "entry_id",
                "menu_path",
                "canonical_url",
                "status",
                "source_capture_id",
                "final_capture_id",
                "page_type",
                "menu_probe_capture_id",
                "menu_ready_capture_id",
                "menu_page_type",
                "menu_readiness_evidence_capture_ids",
                "menu_dismissed_layers",
                "menu_action_count",
                "detail_action_count",
            },
        )
        if unexpected:
            errors.append(f"{label}: 首页基线不得产生菜单或详情证据 {unexpected}")
        return errors
    if decision == "blocked_cross_origin":
        if menu_action_count != 0:
            errors.append(f"{label}: 跨域阻断菜单动作次数必须为 0")
        if detail_action_count != 0:
            errors.append(f"{label}: 跨域阻断详情动作次数必须为 0")
        if status != "cross_origin_blocked":
            errors.append(f"{label}: 跨域菜单终态必须为 cross_origin_blocked，实际为 {status}")
        if actual.get("source_capture_id") or actual.get("final_capture_id"):
            errors.append(f"{label}: 跨域阻断菜单不得产生 Capture 血缘")
        if not actual.get("error_code"):
            errors.append(f"{label}: 跨域阻断缺少错误码")
        if actual.get("error_code") != "WEB_MENU_CROSS_ORIGIN_BLOCKED":
            errors.append(f"{label}: 跨域阻断错误码不正确")
        unexpected = _unexpected_populated_fields(
            actual,
            allowed_fields={
                "entry_id",
                "menu_path",
                "canonical_url",
                "status",
                "error_code",
                "menu_action_count",
                "detail_action_count",
            },
        )
        if unexpected:
            errors.append(f"{label}: 跨域阻断不得产生页面采集证据 {unexpected}")
        return errors
    if not should_execute:
        if menu_action_count != 0:
            errors.append(f"{label}: 限额跳过菜单动作次数必须为 0")
        if detail_action_count != 0:
            errors.append(f"{label}: 限额跳过详情动作次数必须为 0")
        if status != "menu_limit_skipped":
            errors.append(f"{label}: 超出菜单上限的终态必须为 menu_limit_skipped，实际为 {status}")
        forbidden_fields = (
            "final_capture_id",
            "page_type",
            "menu_intent_id",
            "menu_action_execution_id",
            "detail_capture_id",
            "menu_probe_capture_id",
            "menu_ready_capture_id",
            "menu_page_type",
            "detail_candidate_ids",
            "selected_detail_candidate_id",
            "detail_selection_confidence",
            "detail_selection_reason",
            "detail_probe_capture_id",
            "detail_ready_capture_id",
            "detail_page_type",
            "detail_intent_id",
            "detail_action_execution_id",
            "menu_readiness_evidence_capture_ids",
            "detail_readiness_evidence_capture_ids",
            "menu_dismissed_layers",
            "detail_dismissed_layers",
        )
        populated = [name for name in forbidden_fields if actual.get(name)]
        if populated:
            errors.append(f"{label}: 限额跳过不得产生页面采集证据 {populated}")
        expected_error_code = expected_skip_error_code or "WEB_MENU_LIMIT_SKIPPED"
        if actual.get("error_code") != expected_error_code:
            errors.append(
                f"{label}: 未执行菜单错误码不正确 "
                f"expected={expected_error_code}, actual={actual.get('error_code')}"
            )
        if actual.get("alias_of") != expected_alias_of:
            errors.append(
                f"{label}: 菜单别名指向不正确 "
                f"expected={expected_alias_of}, actual={actual.get('alias_of')}"
            )
        return errors
    if status not in {"detail_captured", "menu_page_captured"}:
        errors.append(f"{label}: 白名单菜单失败终态 {status}")
        return errors
    if menu_action_count != 1:
        errors.append(f"{label}: 白名单菜单动作次数必须为 1")
    if not actual.get("menu_intent_id") or not actual.get("menu_action_execution_id"):
        errors.append(f"{label}: 菜单动作缺少 intent/action 标识")
    required_menu_fields = (
        "source_capture_id",
        "final_capture_id",
        "menu_probe_capture_id",
        "menu_ready_capture_id",
        "menu_page_type",
    )
    missing_menu_fields = [name for name in required_menu_fields if not actual.get(name)]
    if missing_menu_fields:
        errors.append(f"{label}: 菜单证据链缺失 {missing_menu_fields}")
    menu_probe_capture_id = str(actual.get("menu_probe_capture_id") or "")
    menu_ready_capture_id = str(actual.get("menu_ready_capture_id") or "")
    menu_evidence = actual.get("menu_readiness_evidence_capture_ids")
    if not _capture_lineage_matches(
        menu_evidence,
        probe_capture_id=menu_probe_capture_id,
        ready_capture_id=menu_ready_capture_id,
    ):
        errors.append(f"{label}: 菜单 readiness 血缘未绑定 probe/ready Capture")
    if actual.get("final_capture_id") != actual.get("menu_ready_capture_id"):
        errors.append(f"{label}: final_capture_id 未绑定菜单 ready Capture")
    menu_page_type = str(actual.get("menu_page_type") or "")
    if menu_page_type not in {
        "video_list",
        "video_detail",
        "content_list",
        "content_detail",
    }:
        errors.append(f"{label}: menu_page_type 不属于 Web 四类业务类型")
    if actual.get("page_type") != menu_page_type:
        errors.append(f"{label}: page_type 与 menu_page_type 不一致")
    if actual.get("error_code"):
        errors.append(f"{label}: 成功菜单不应携带 error_code")
    if menu_page_type in {"video_list", "content_list"}:
        if detail_action_count != 1:
            errors.append(f"{label}: 列表页详情动作次数必须为 1")
        if not actual.get("detail_intent_id") or not actual.get("detail_action_execution_id"):
            errors.append(f"{label}: 列表详情动作缺少 intent/action 标识")
        required_detail_fields = (
            "detail_candidate_ids",
            "selected_detail_candidate_id",
            "detail_selection_confidence",
            "detail_selection_reason",
            "detail_capture_id",
            "detail_probe_capture_id",
            "detail_ready_capture_id",
        )
        missing_detail_fields = [name for name in required_detail_fields if not actual.get(name)]
        if status != "detail_captured" or missing_detail_fields:
            errors.append(f"{label}: 列表页未形成下一层详情证据链 {missing_detail_fields}")
        detail_candidate_ids = actual.get("detail_candidate_ids")
        if (
            not isinstance(detail_candidate_ids, list)
            or not detail_candidate_ids
            or any(
                not isinstance(candidate_id, str)
                or not candidate_id.strip()
                or not _valid_web_candidate_id(candidate_id)
                for candidate_id in detail_candidate_ids
            )
            or len(set(detail_candidate_ids)) != len(detail_candidate_ids)
            or actual.get("selected_detail_candidate_id") not in detail_candidate_ids
        ):
            errors.append(f"{label}: 详情选择未绑定候选清单")
        confidence = actual.get("detail_selection_confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0.7 <= float(confidence) <= 1
        ):
            errors.append(f"{label}: 详情选择置信度不可信")
        reason = actual.get("detail_selection_reason")
        if not _valid_detail_selection_reason(reason):
            errors.append(f"{label}: 详情选择理由不是规范脱敏单行文本")
        if actual.get("detail_page_type") not in {
            "video_detail",
            "content_detail",
        }:
            errors.append(f"{label}: 列表页详情类型不属于 Web 详情闭集")
        detail_probe_capture_id = str(actual.get("detail_probe_capture_id") or "")
        detail_ready_capture_id = str(actual.get("detail_ready_capture_id") or "")
        detail_evidence = actual.get("detail_readiness_evidence_capture_ids")
        if actual.get("detail_capture_id") != actual.get(
            "detail_ready_capture_id"
        ) or not _capture_lineage_matches(
            detail_evidence,
            probe_capture_id=detail_probe_capture_id,
            ready_capture_id=detail_ready_capture_id,
        ):
            errors.append(f"{label}: 详情 readiness 血缘未绑定 probe/ready Capture")
    elif menu_page_type in {"video_detail", "content_detail"}:
        if detail_action_count != 0:
            errors.append(f"{label}: 直接详情页详情动作次数必须为 0")
        if (
            actual.get("detail_intent_id")
            or actual.get("detail_action_execution_id")
            or actual.get("detail_probe_capture_id")
            or actual.get("detail_readiness_evidence_capture_ids")
        ):
            errors.append(f"{label}: 直接详情页不得产生额外详情动作标识")
        logical_detail_capture = actual.get("detail_ready_capture_id") or actual.get(
            "menu_ready_capture_id"
        )
        if status != "detail_captured" or not logical_detail_capture:
            errors.append(f"{label}: 直接详情页缺少逻辑详情 Capture")
        logical_detail_type = actual.get("detail_page_type") or menu_page_type
        if logical_detail_type != menu_page_type:
            errors.append(f"{label}: 直接详情页类型不匹配")
        if logical_detail_capture != actual.get("menu_ready_capture_id"):
            errors.append(f"{label}: 直接详情 readiness 血缘未绑定菜单 ready Capture")
        if actual.get("detail_capture_id") not in {
            None,
            "",
            actual.get("menu_ready_capture_id"),
        }:
            errors.append(f"{label}: 直接详情 Capture 未绑定菜单 ready Capture")
    elif menu_page_type == "home" and status != "menu_page_captured":
        errors.append(f"{label}: 菜单回首页的终态必须为 menu_page_captured")
    elif menu_page_type == "home" and detail_action_count != 0:
        errors.append(f"{label}: 菜单回首页的详情动作次数必须为 0")
    return errors


def _unexpected_populated_fields(
    actual: dict[str, object],
    *,
    allowed_fields: set[str],
) -> list[str]:
    """失败分支仅允许明确字段，拒绝夹带任何页面采集证据。"""

    empty_values = (None, "", (), [], {})
    return sorted(
        field_name
        for field_name, value in actual.items()
        if field_name not in allowed_fields and value not in empty_values
    )


def _capture_lineage_matches(
    raw: object,
    *,
    probe_capture_id: str,
    ready_capture_id: str,
) -> bool:
    if (
        not probe_capture_id
        or not ready_capture_id
        or not isinstance(raw, list)
        or len(raw) < 2
        or any(not isinstance(item, str) or not item.strip() for item in raw)
    ):
        return False
    return raw[0] == probe_capture_id and raw[-1] == ready_capture_id


def _catalog_runtime_url_matches(catalog_url: str, runtime_url: str) -> bool:
    try:
        catalog = urllib.parse.urlsplit(catalog_url)
        runtime = urllib.parse.urlsplit(runtime_url)
        catalog_port = catalog.port
        runtime_port = runtime.port
    except ValueError:
        return False
    if (
        catalog.scheme.casefold() not in {"http", "https"}
        or runtime.scheme.casefold() not in {"http", "https"}
        or not catalog.hostname
        or not runtime.hostname
        or runtime.username is not None
        or runtime.password is not None
        or bool(runtime.fragment)
    ):
        return False
    catalog_default_port = 443 if catalog.scheme == "https" else 80
    runtime_default_port = 443 if runtime.scheme == "https" else 80
    catalog_port_identity = (
        "" if catalog_port in {None, catalog_default_port} else str(catalog_port)
    )
    runtime_port_identity = (
        "" if runtime_port in {None, runtime_default_port} else str(runtime_port)
    )
    catalog_identity = (
        catalog.hostname.casefold(),
        catalog_port_identity,
        catalog.path or "/",
        catalog.query,
    )
    runtime_identity = (
        runtime.hostname.casefold(),
        runtime_port_identity,
        runtime.path or "/",
        runtime.query,
    )
    return catalog_identity == runtime_identity and (
        catalog.scheme.casefold() == runtime.scheme.casefold()
        or (catalog.scheme.casefold() == "http" and runtime.scheme.casefold() == "https")
    )


def _status_count(summary: dict[str, object], status: str) -> int:
    counts = summary.get("status_counts")
    return _safe_int(counts.get(status)) if isinstance(counts, dict) else 0


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0, int(value))


def _optional_count_text(value: object) -> str:
    normalized = _optional_nonnegative_int(value)
    return str(normalized) if normalized is not None else "未取得"


def _optional_ratio_text(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "-"
    return f"{max(0.0, min(float(value), 1.0)):.2f}"


def _normalize_live_logical_page_assessments(
    value: object,
) -> tuple[dict[str, object], ...]:
    """白名单化报告逐页判定，禁止原始诊断与模型输出进入验收产物。"""

    if not isinstance(value, list):
        return ()
    return tuple(
        {
            "logical_page_id": item.get("logical_page_id"),
            "canonical_url": _public_report_url(item.get("canonical_url")),
            "primary_capture_id": item.get("primary_capture_id"),
            "page_type": item.get("page_type"),
            "classification_status": item.get("classification_status"),
            "inspection_status": item.get("inspection_status"),
        }
        for item in value
        if isinstance(item, dict)
    )


def _formal_page_duplicate_url_count(bundle: dict[str, object]) -> int:
    summary = bundle.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    return _safe_int(summary.get("duplicate_formal_url_count"))


def _validate_live_acceptance_dimensions(site: LiveSiteResult) -> list[str]:
    """逐页校验类型、质检、主截图与重复 URL 四个实况验收维度。"""

    prefix = site.target_url
    errors: list[str] = []
    if site.page_type_review_count != 0:
        errors.append(
            f"{prefix}: 实际覆盖尚有页面类型待复核，数量为 "
            f"{_optional_count_text(site.page_type_review_count)}"
        )
    if site.pending_confirmation_count != 0:
        errors.append(
            f"{prefix}: 服务端仍有待人工确认，数量为 "
            f"{_optional_count_text(site.pending_confirmation_count)}"
        )
    if site.duplicate_url_count != 0:
        errors.append(
            f"{prefix}: 重复正式 URL 数必须为 0，实际为 "
            f"{_optional_count_text(site.duplicate_url_count)}"
        )
    if site.inspection_total_count is None or site.inspection_pending_count != 0:
        errors.append(
            f"{prefix}: 质检进度未排空，total="
            f"{_optional_count_text(site.inspection_total_count)}，pending="
            f"{_optional_count_text(site.inspection_pending_count)}"
        )

    formal_pages = site.evidence_bundle.get("formal_pages")
    formal_pages = formal_pages if isinstance(formal_pages, list) else []
    formal_by_identity = {
        str(page.get("logical_page_id") or ""): page
        for page in formal_pages
        if isinstance(page, dict) and str(page.get("logical_page_id") or "")
    }
    assessment_by_identity = {
        str(page.get("logical_page_id") or ""): page
        for page in site.logical_page_assessments
        if str(page.get("logical_page_id") or "")
    }
    if not formal_by_identity or set(assessment_by_identity) != set(formal_by_identity):
        errors.append(f"{prefix}: 正式证据页与报告逐页判定集合不一致")
    for logical_page_id, formal_page in formal_by_identity.items():
        assessment = assessment_by_identity.get(logical_page_id, {})
        canonical_url = str(formal_page.get("logical_page_url") or logical_page_id)
        if (
            assessment.get("page_type") not in _WEB_CANONICAL_PAGE_TYPES
            or assessment.get("classification_status") != "classified"
        ):
            errors.append(f"{prefix}: {canonical_url} 未完成页面类型判定")
        if assessment.get("inspection_status") not in {
            "completed",
            "degraded",
            "failed",
        }:
            errors.append(f"{prefix}: {canonical_url} 未形成页面质检终态")
        if assessment.get("primary_capture_id") != formal_page.get("primary_capture_id"):
            errors.append(f"{prefix}: {canonical_url} 报告主 Capture 与证据包不一致")

    selection_by_identity = {
        str(item.get("logical_page_id") or ""): item
        for item in site.screenshot_selections
        if str(item.get("logical_page_id") or "")
    }
    if len(selection_by_identity) != len(site.screenshot_selections) or set(
        selection_by_identity
    ) != set(formal_by_identity):
        errors.append(f"{prefix}: 主截图选择未覆盖每个正式逻辑页")
    for logical_page_id, selection in selection_by_identity.items():
        canonical_url = str(selection.get("logical_page_url") or logical_page_id)
        if (
            not selection.get("primary_capture_id")
            or not selection.get("screenshot_artifact_id")
            or not selection.get("capture_purpose")
        ):
            errors.append(f"{prefix}: {canonical_url} 主截图选择不完整")
    return errors


def _build_live_screenshot_selections(
    bundle: dict[str, object],
) -> tuple[dict[str, object], ...]:
    captures = bundle.get("captures")
    captures = captures if isinstance(captures, list) else []
    capture_by_id = {
        str(item.get("capture_id") or ""): item
        for item in captures
        if isinstance(item, dict) and str(item.get("capture_id") or "")
    }
    pages = bundle.get("formal_pages")
    pages = pages if isinstance(pages, list) else []
    selections: list[dict[str, object]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        primary_capture_id = str(page.get("primary_capture_id") or "")
        capture = capture_by_id.get(primary_capture_id, {})
        artifacts = capture.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, list) else []
        screenshot = next(
            (
                item
                for item in artifacts
                if isinstance(item, dict) and item.get("artifact_type") == "screenshot"
            ),
            {},
        )
        primary = capture.get("primary_content_capture")
        primary = primary if isinstance(primary, dict) else {}
        selections.append(
            {
                "logical_page_id": page.get("logical_page_id"),
                "logical_page_url": page.get("logical_page_url"),
                "primary_capture_id": primary_capture_id or None,
                "screenshot_artifact_id": screenshot.get("artifact_id"),
                "capture_purpose": (
                    capture.get("capture_purpose") or page.get("capture_purpose")
                ),
                "obstruction_capture_id": primary.get("parent_capture_id"),
                "before_ad_coverage": primary.get("before_ad_coverage"),
                "after_ad_coverage": primary.get("after_ad_coverage"),
            }
        )
    return tuple(selections)


def _http_json_sender(
    endpoint: str,
    payload: dict[str, object],
    headers: dict[str, str],
) -> dict[str, object]:
    return _http_json_request("POST", endpoint, payload, headers)


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """API 调用禁止自动重定向，防止 Authorization 跨源转发。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        raise RuntimeError("WEB_LIVE_API_REDIRECT_BLOCKED")


def _http_json_request(
    method: str,
    endpoint: str,
    payload: dict[str, object] | None,
    headers: dict[str, str],
) -> dict[str, object]:
    _validate_http_endpoint(endpoint)
    request = urllib.request.Request(
        endpoint,
        data=(
            json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        ),
        headers=(
            {"Content-Type": "application/json", **headers}
            if payload is not None
            else dict(headers)
        ),
        method=method,
    )
    opener = urllib.request.build_opener(_RejectRedirectHandler())
    with opener.open(request, timeout=30) as response:  # noqa: S310
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("live API 返回值不是对象")
    return result


def _validate_api_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("API 根地址端口无效") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("API 根地址禁止包含凭据")
    if not parsed.hostname or parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("API 根地址必须为 HTTP(S) 绝对地址")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("API 根地址不得包含路径、查询或片段")
    host = parsed.hostname.casefold()
    is_loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme.casefold() != "https" and not is_loopback:
        raise ValueError("远程 live API 必须使用 HTTPS")
    return urllib.parse.urlunsplit((parsed.scheme.casefold(), parsed.netloc, "", "", ""))


def _validate_http_endpoint(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("live API 端点禁止包含凭据")
    host = str(parsed.hostname or "").casefold()
    if parsed.scheme.casefold() not in {"http", "https"} or not host:
        raise ValueError("live API 端点无效")
    if parsed.scheme.casefold() != "https" and host not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("远程 live API 必须使用 HTTPS")


def render_live_markdown(report: LiveSixSiteReport) -> str:
    """渲染六站完整过程、聚合门禁和逐菜单证据链。"""

    lines = [
        "# Web V2 实况探索报告",
        "",
        f"- 结论：{'通过' if report.passed else '未通过'}",
        f"- 验收范围：请求目标（{len(report.requested_targets)} 站）",
        f"- 未完成验收维度：{', '.join(report.acceptance_failures) or '无'}",
        f"- 站点：{report.site_count}/{len(report.requested_targets)}",
        f"- 菜单总数：{report.menu_total}",
        f"- 菜单数量上限：{'不限' if report.menu_limit is None else report.menu_limit}",
        f"- 同源菜单执行：{report.executed_same_origin_count}/"
        f"{report.expected_executed_same_origin_count}",
        f"- 限额跳过：{report.menu_limit_skipped_count}/{report.expected_menu_limit_skipped_count}",
        f"- 首页基线：{report.home_baseline_count}/{len(report.requested_targets)}",
        f"- 逻辑详情：{report.detail_count}/{report.expected_executed_same_origin_count}",
        f"- 跨域阻断：{report.blocked_cross_origin_count}",
        f"- Frontier 剩余：{report.frontier_remaining}",
        "",
        "## 验收层级",
        "",
        "- 代码测试：必须引用独立 pytest、Vitest 与构建日志，本实况运行不替代代码测试。",
        "- 离线固定样本：必须独立执行 Catalog 与固定证据样本回归，本实况运行不替代离线回归。",
        f"- 真实站点运行：{'通过' if report.live_validation_passed else '未通过'}。",
        f"- 人工复核：{'通过' if report.manual_report_review_passed else '未通过'}；"
        "仅在任务列表和任务详情入口均由人工确认后完成。",
        f"- Completion Gate：{'通过' if report.server_completion_gate_passed else '未通过'}，"
        "未通过时报告保持 preview。",
        "",
    ]
    if report.validation_errors:
        lines.extend(["## 聚合门禁错误", ""])
        lines.extend(f"- {_markdown_text(item)}" for item in report.validation_errors)
        lines.append("")
    lines.extend(
        [
            "## 站点执行概览",
            "",
            "| 顺序 | 站点 | Task 状态 | 报告 | Catalog | 首页 | "
            "执行菜单 | 逻辑详情 | 限额跳过 | Frontier |",
            "|---:|---|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for index, site in enumerate(report.sites, start=1):
        summary = site.exploration_summary
        evidence_summary = site.evidence_bundle.get("summary")
        evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
        lines.append(
            f"| {index} | {_markdown_cell(site.target_url)} | "
            f"{_markdown_cell(site.task_status)} | "
            f"{_markdown_cell(site.report_stage)} | "
            f"{_safe_int(summary.get('menu_total'))} | "
            f"{_safe_int(evidence_summary.get('home_count'))} | "
            f"{_safe_int(summary.get('executed_same_origin_count'))} | "
            f"{_safe_int(evidence_summary.get('detail_count'))} | "
            f"{_safe_int(summary.get('menu_limit_skipped_count'))} | "
            f"{_safe_int(summary.get('frontier_remaining'))} |"
        )
    for index, site in enumerate(report.sites, start=1):
        summary = site.exploration_summary
        status_counts_text = json.dumps(
            summary.get("status_counts", {}),
            ensure_ascii=False,
            sort_keys=True,
        )
        report_error_code = _markdown_identifier(
            site.report_error_code or "-",
            identifier_kind="error_code",
        )
        lines.extend(
            [
                "",
                f"## {index}. {_markdown_text(site.target_url)}",
                "",
                f"- task_id：{_markdown_identifier(site.task_id, identifier_kind='task_run')}",
                f"- run_id：{_markdown_identifier(site.run_id, identifier_kind='task_run')}",
                f"- 任务状态：{_markdown_text(site.task_status)}",
                f"- 报告状态：{_markdown_text(site.report_status)} / "
                f"{_markdown_text(site.report_stage)}",
                f"- 报告结论：{_markdown_text(site.report_overall_status or '-')}",
                f"- 报告错误码：{report_error_code}",
                f"- 失败原因：{_markdown_text(site.failure_reason or '-')}",
                "- 页面类型："
                f"{_markdown_text(', '.join(map(str, summary.get('page_type_keys', []))) or '-')}",
                f"- 状态计数：{_markdown_text(status_counts_text)}",
                f"- 证据包状态：{_markdown_text(site.evidence_bundle.get('status') or '-')}",
                f"- 报告入口：{_public_live_report_url(site.report_url)}；"
                f"工作台 {_public_workstation_report_path(site.workstation_report_path)}",
                "- 报告入口人工复核："
                f"任务列表：{'已验证' if site.task_list_report_verified else '未验证'}；"
                f"任务详情：{'已验证' if site.task_detail_report_verified else '未验证'}",
                f"- 页面类型待复核：{_optional_count_text(site.page_type_review_count)}",
                f"- 待人工确认：{_optional_count_text(site.pending_confirmation_count)}",
                f"- 重复正式 URL：{_optional_count_text(site.duplicate_url_count)}",
                *[f"- {name}：{_markdown_text(str(value))}" for name, value in site.acceptance_dimensions.items()],
                "",
                "| 菜单路径 | URL | 状态 | 来源 Capture | 菜单就绪 | 详情选择 | 详情就绪 | 错误 |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        ledger = _business_ad_ledger(summary)
        if isinstance(ledger, list):
            for raw in ledger:
                if not isinstance(raw, dict):
                    continue
                menu_path = raw.get("menu_path")
                menu_text = _markdown_cell(
                    " / ".join(map(str, menu_path)) if isinstance(menu_path, list) else "-"
                )
                candidate_ids = raw.get("detail_candidate_ids")
                candidate_count = len(candidate_ids) if isinstance(candidate_ids, list) else 0
                selected_id = _markdown_cell(
                    raw.get("selected_detail_candidate_id") or "-",
                    identifier_kind="candidate",
                )
                confidence = raw.get("detail_selection_confidence")
                confidence_text = (
                    f"{float(confidence):.2f}"
                    if isinstance(confidence, int | float) and not isinstance(confidence, bool)
                    else "-"
                )
                reason = _markdown_cell(raw.get("detail_selection_reason") or "-")
                detail_selection = (
                    f"候选{candidate_count} / {selected_id} / {confidence_text} / {reason}"
                    if candidate_count or selected_id != "-"
                    else "-"
                )
                source_capture_id = _markdown_cell(
                    raw.get("source_capture_id") or "-",
                    identifier_kind="capture",
                )
                menu_ready_capture_id = _markdown_cell(
                    raw.get("menu_ready_capture_id") or "-",
                    identifier_kind="capture",
                )
                detail_ready_capture_id = _markdown_cell(
                    raw.get("detail_ready_capture_id") or "-",
                    identifier_kind="capture",
                )
                error_code = _markdown_cell(
                    raw.get("error_code") or "-",
                    identifier_kind="error_code",
                )
                lines.append(
                    f"| {menu_text} | "
                    f"{_markdown_cell(_public_report_url(raw.get('canonical_url')))} | "
                    f"{_markdown_cell(raw.get('status') or '-')} | "
                    f"{source_capture_id} | "
                    f"{menu_ready_capture_id} | "
                    f"{detail_selection} | "
                    f"{detail_ready_capture_id} | "
                    f"{error_code} |"
                )
        lines.extend(
            [
                "",
                "### 广告落地证据",
                "",
                "| 业务页 | 源 URL | 落地 URL | 候选 | 状态 | Capture | Artifact | 错误码 |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        ad_row_count = 0
        ledger = _business_ad_ledger(site.exploration_summary)
        if isinstance(ledger, list):
            for raw in ledger:
                if not isinstance(raw, dict):
                    continue
                evidence = raw.get("ad_landing_evidence")
                if not isinstance(evidence, dict):
                    continue
                menu_path = raw.get("menu_path")
                page_label = (
                    " / ".join(map(str, menu_path))
                    if isinstance(menu_path, list)
                    else "-"
                )
                artifact_text = _markdown_ad_artifact_refs(evidence)
                capture_id = _markdown_cell(
                    evidence.get("capture_id") or "-",
                    identifier_kind="capture",
                )
                reason_code = _markdown_cell(
                    evidence.get("reason_code") or "-",
                    identifier_kind="error_code",
                )
                lines.append(
                    f"| {_markdown_cell(page_label)} | "
                    f"{_markdown_cell(_public_report_url(evidence.get('source_url')))} | "
                    f"{_markdown_cell(_public_report_url(evidence.get('landing_url')))} | "
                    f"{_markdown_cell(evidence.get('candidate_kind') or '-')} | "
                    f"{_markdown_cell(evidence.get('status') or '-')} | "
                    f"{capture_id} | "
                    f"{artifact_text} | "
                    f"{reason_code} |"
                )
                ad_row_count += 1
        if ad_row_count == 0:
            lines.append("| - | - | - | - | not_scheduled | - | - | - |")
        lines.extend(
            [
                "",
                "### 探索过程节点",
                "",
                "| 阶段 | 菜单身份 | 状态 | 页面类型 | Action | Capture |",
                "|---|---|---|---|---|---|",
            ]
        )
        for node in site.process_nodes:
            action_execution_id = _markdown_cell(
                node.get("action_execution_id") or "-",
                identifier_kind="action",
            )
            lines.append(
                f"| {_markdown_cell(node.get('phase') or '-')} | "
                f"{_markdown_cell(node.get('entry_id') or '-')} | "
                f"{_markdown_cell(node.get('status') or '-')} | "
                f"{_markdown_cell(node.get('page_type') or '-')} | "
                f"{action_execution_id} | "
                f"{_markdown_cell(node.get('capture_id') or '-', identifier_kind='capture')} |"
            )
        lines.extend(
            [
                "",
                "### 截图选择",
                "",
                "| 逻辑 URL | 主 Capture | 主截图 Artifact | 用途 | "
                "遮挡前态 | 广告覆盖率（前→后） |",
                "|---|---|---|---|---|---|",
            ]
        )
        if not site.screenshot_selections:
            lines.append("| - | - | - | - | - | - |")
        for selection in site.screenshot_selections:
            before_coverage = _optional_ratio_text(selection.get("before_ad_coverage"))
            after_coverage = _optional_ratio_text(selection.get("after_ad_coverage"))
            primary_capture_id = _markdown_cell(
                selection.get("primary_capture_id") or "-",
                identifier_kind="capture",
            )
            screenshot_artifact_id = _markdown_cell(
                selection.get("screenshot_artifact_id") or "-",
                identifier_kind="capture",
            )
            obstruction_capture_id = _markdown_cell(
                selection.get("obstruction_capture_id") or "-",
                identifier_kind="capture",
            )
            lines.append(
                f"| {_markdown_cell(_public_report_url(selection.get('logical_page_url')))} | "
                f"{primary_capture_id} | "
                f"{screenshot_artifact_id} | "
                f"{_markdown_cell(selection.get('capture_purpose') or '-')} | "
                f"{obstruction_capture_id} | "
                f"{before_coverage}→{after_coverage} |"
            )
    return "\n".join(lines) + "\n"


def render_live_html(report: LiveSixSiteReport) -> str:
    """生成无脚本、无外链资源、仅含脱敏统计与过程节点的静态 HTML。"""

    def text(value: object) -> str:
        return html.escape(_safe_runtime_text(value), quote=True)

    def identifier(value: object, kind: str) -> str:
        return html.escape(_safe_identifier(value, identifier_kind=kind), quote=True)

    error_items = (
        "".join(f"<li>{text(item)}</li>" for item in report.validation_errors) or "<li>无</li>"
    )
    site_row_items: list[str] = []
    for index, site in enumerate(report.sites, start=1):
        evidence_summary = site.evidence_bundle.get("summary")
        evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
        site_row_items.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{text(site.target_url)}</td>"
            f"<td>{text(site.task_status)}</td>"
            f"<td>{text(site.report_stage)}</td>"
            f"<td>{_safe_int(site.exploration_summary.get('menu_total'))}</td>"
            f"<td>{_safe_int(evidence_summary.get('home_count'))}</td>"
            f"<td>{_safe_int(site.exploration_summary.get('executed_same_origin_count'))}</td>"
            f"<td>{_safe_int(evidence_summary.get('detail_count'))}</td>"
            f"<td>{text(site.evidence_bundle.get('status') or '-')}</td>"
            "</tr>"
        )
    site_rows = "".join(site_row_items)
    site_sections: list[str] = []
    for index, site in enumerate(report.sites, start=1):
        node_rows = (
            "".join(
                "<tr>"
                f"<td>{text(node.get('phase') or '-')}</td>"
                f"<td>{text(node.get('entry_id') or '-')}</td>"
                f"<td>{text(node.get('status') or '-')}</td>"
                f"<td>{text(node.get('page_type') or '-')}</td>"
                f"<td>{identifier(node.get('action_execution_id') or '-', 'action')}</td>"
                f"<td>{identifier(node.get('capture_id') or '-', 'capture')}</td>"
                "</tr>"
                for node in site.process_nodes
            )
            or '<tr><td colspan="6">无过程节点</td></tr>'
        )
        screenshot_rows = (
            "".join(
                "<tr>"
                f"<td>{text(_public_report_url(item.get('logical_page_url')))}</td>"
                f"<td>{identifier(item.get('primary_capture_id') or '-', 'capture')}</td>"
                f"<td>{identifier(item.get('screenshot_artifact_id') or '-', 'capture')}</td>"
                f"<td>{text(item.get('capture_purpose') or '-')}</td>"
                f"<td>{identifier(item.get('obstruction_capture_id') or '-', 'capture')}</td>"
                f"<td>{text(_optional_ratio_text(item.get('before_ad_coverage')))}→"
                f"{text(_optional_ratio_text(item.get('after_ad_coverage')))}</td>"
                "</tr>"
                for item in site.screenshot_selections
            )
            or '<tr><td colspan="6">无截图选择</td></tr>'
        )
        ad_row_items: list[str] = []
        ledger = _business_ad_ledger(site.exploration_summary)
        if isinstance(ledger, list):
            for raw in ledger:
                if not isinstance(raw, dict):
                    continue
                evidence = raw.get("ad_landing_evidence")
                if not isinstance(evidence, dict):
                    continue
                menu_path = raw.get("menu_path")
                page_label = (
                    " / ".join(map(str, menu_path))
                    if isinstance(menu_path, list)
                    else "-"
                )
                artifact_text = _safe_ad_artifact_refs(evidence)
                ad_row_items.append(
                    "<tr>"
                    f"<td>{text(page_label)}</td>"
                    f"<td>{text(_public_report_url(evidence.get('source_url')))}</td>"
                    f"<td>{text(_public_report_url(evidence.get('landing_url')))}</td>"
                    f"<td>{text(evidence.get('candidate_kind') or '-')}</td>"
                    f"<td>{text(evidence.get('status') or '-')}</td>"
                    f"<td>{identifier(evidence.get('capture_id') or '-', 'capture')}</td>"
                    f"<td>{text(artifact_text)}</td>"
                    f"<td>{identifier(evidence.get('reason_code') or '-', 'error_code')}</td>"
                    "</tr>"
                )
        ad_rows = "".join(ad_row_items) or '<tr><td colspan="8">未调度</td></tr>'
        site_sections.append(
            f"<section><h2>{index}. {text(site.target_url)}</h2>"
            "<dl>"
            f"<dt>Task ID</dt><dd>{identifier(site.task_id, 'task_run')}</dd>"
            f"<dt>Run ID</dt><dd>{identifier(site.run_id, 'task_run')}</dd>"
            f"<dt>失败原因</dt><dd>{text(site.failure_reason or '-')}</dd>"
            f"<dt>证据状态</dt><dd>{text(site.evidence_bundle.get('status') or '-')}</dd>"
            f"<dt>报告入口</dt><dd>{text(_public_live_report_url(site.report_url))}；"
            f"工作台 {text(_public_workstation_report_path(site.workstation_report_path))}</dd>"
            "<dt>报告入口人工复核</dt><dd>"
            f"任务列表：{'已验证' if site.task_list_report_verified else '未验证'}；"
            f"任务详情：{'已验证' if site.task_detail_report_verified else '未验证'}</dd>"
            f"<dt>页面类型待复核</dt><dd>{text(_optional_count_text(site.page_type_review_count))}</dd>"
            f"<dt>待人工确认</dt><dd>{text(_optional_count_text(site.pending_confirmation_count))}</dd>"
            f"<dt>重复正式 URL</dt><dd>{text(_optional_count_text(site.duplicate_url_count))}</dd>"
            "</dl>"
            "<h3>六维验收</h3><dl>"
            + "".join(f"<dt>{text(name)}</dt><dd>{text(value)}</dd>" for name, value in site.acceptance_dimensions.items())
            + "</dl>"
            "<table><thead><tr><th>阶段</th><th>菜单身份</th><th>状态</th>"
            "<th>页面类型</th><th>Action</th><th>Capture</th></tr></thead>"
            f"<tbody>{node_rows}</tbody></table>"
            "<h3>截图选择</h3><table><thead><tr><th>逻辑 URL</th><th>主 Capture</th>"
            "<th>主截图 Artifact</th><th>用途</th><th>遮挡前态</th><th>广告覆盖率（前→后）</th>"
            f"</tr></thead><tbody>{screenshot_rows}</tbody></table>"
            "<h3>广告落地证据</h3><table><thead><tr><th>业务页</th><th>源 URL</th>"
            "<th>落地 URL</th><th>候选</th><th>状态</th><th>Capture</th>"
            "<th>Artifact</th><th>错误码</th>"
            f"</tr></thead><tbody>{ad_rows}</tbody></table></section>"
        )
    conclusion = "通过" if report.passed else "未通过"
    live_conclusion = "通过" if report.live_validation_passed else "未通过"
    manual_conclusion = "通过" if report.manual_report_review_passed else "未通过"
    gate_conclusion = "通过" if report.server_completion_gate_passed else "未通过"
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Web V2 实况探索报告</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:1440px;margin:24px auto;"
        "padding:0 20px;color:#172033;background:#f7f8fb}section,.summary{background:#fff;"
        "border:1px solid #dce1ea;border-radius:10px;padding:18px;margin:16px 0}"
        "table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #dce1ea;"
        "padding:8px;text-align:left;vertical-align:top}th{background:#eef2f7}"
        "dl{display:grid;grid-template-columns:140px 1fr;gap:6px}dt{font-weight:700}dd{margin:0}"
        "code{overflow-wrap:anywhere}</style></head><body>"
        "<h1>Web V2 实况探索报告</h1>"
        '<div class="summary">'
        f"<p>结论：{conclusion}</p>"
        f"<p>验收范围：请求目标（{len(report.requested_targets)} 站）</p>"
        f"<p>未完成验收维度：{html.escape(', '.join(report.acceptance_failures) or '无')}</p>"
        f"<p>站点：{report.site_count}/{len(report.requested_targets)}；菜单总数：{report.menu_total}；"
        f"实际菜单：{report.executed_same_origin_count}/{report.expected_executed_same_origin_count}；"
        f"逻辑详情：{report.detail_count}/{report.expected_executed_same_origin_count}；"
        f"菜单数量上限：{'不限' if report.menu_limit is None else report.menu_limit}；Frontier：{report.frontier_remaining}</p>"
        "</div><h2>验收层级</h2><ul>"
        "<li>代码测试：必须引用独立 pytest、Vitest 与构建日志。</li>"
        "<li>离线固定样本：必须独立执行 Catalog 与固定证据样本回归。</li>"
        f"<li>真实站点运行：{live_conclusion}。</li>"
        f"<li>人工复核：{manual_conclusion}；本轮逐页实际复核记录及任务列表、详情入口均验证后完成。</li>"
        f"<li>Completion Gate：{gate_conclusion}，未通过时保持 preview。</li>"
        "</ul><h2>聚合门禁</h2><ul>"
        f"{error_items}</ul><h2>站点概览</h2>"
        "<table><thead><tr><th>顺序</th><th>站点</th><th>任务</th><th>报告</th>"
        "<th>Catalog</th><th>首页</th><th>执行菜单</th><th>逻辑详情</th><th>证据</th></tr></thead>"
        f"<tbody>{site_rows}</tbody></table>{''.join(site_sections)}"
        "</body></html>"
    )


def _valid_detail_selection_reason(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        sanitized = sanitize_web_model_reason(value)
    except ValueError:
        return False
    return sanitized == value


def _public_report_url(value: object) -> str:
    """公开报告 URL 只保留 scheme、host、port 和 path。"""

    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return "[redacted-sensitive]"
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{hostname}:{parsed.port}"
        return urllib.parse.urlunsplit(
            (parsed.scheme.casefold(), netloc, parsed.path or "/", "", "")
        )
    except ValueError:
        return "[redacted-sensitive]"


def _public_live_report_url(value: object) -> str:
    """实况报告地址仅保留受控的 Run 查询参数。"""

    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        public_base = _public_report_url(value)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
        run_id = str((query.get("agent_run_id") or [""])[0]).strip()
        if public_base == "[redacted-sensitive]" or not _valid_uuid_id(run_id):
            return public_base
        return public_base + "?" + urllib.parse.urlencode({"agent_run_id": run_id})
    except (TypeError, ValueError):
        return "[redacted-sensitive]"


def _public_workstation_report_path(value: object) -> str:
    """工作台相对路由只允许 reports 页面和一个 Task UUID。"""

    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
        task_id = str((query.get("task") or [""])[0]).strip()
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.path != "/reports"
            or not _valid_uuid_id(task_id)
        ):
            return "[redacted-sensitive]"
        return "/reports?" + urllib.parse.urlencode({"task": task_id})
    except (TypeError, ValueError):
        return "[redacted-sensitive]"


def _markdown_text(value: object) -> str:
    """将任意运行时字段收敛为无凭据、单行、不可注入的 Markdown 文本。"""

    normalized = _safe_runtime_text(value)
    escaped = html.escape(normalized, quote=True).replace("\\", "\\\\")
    for marker in ("`", "*", "_", "[", "]", "|", "#"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped


def _markdown_identifier(value: object, *, identifier_kind: str) -> str:
    """按字段类型只允许生产 ID 合同，禁止共享宽泛命名例外。"""

    normalized = _safe_identifier(value, identifier_kind=identifier_kind)
    escaped = html.escape(normalized, quote=True).replace("\\", "\\\\")
    for marker in ("`", "*", "_", "[", "]", "|", "#"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped


def _business_ad_ledger(summary: Mapping[str, object]) -> list[dict[str, object]]:
    """广告表同时展示 Catalog 和已执行运行时菜单的持久证据。"""
    raw = summary.get("menu_ledger")
    result = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    runtime = summary.get("runtime_discovered_ledger")
    if isinstance(runtime, list):
        for item in runtime:
            execution = item.get("execution", item) if isinstance(item, dict) else None
            if isinstance(execution, dict):
                result.append({**execution, "menu_path": execution.get("menu_path")
                               or item.get("menu_path", [])})
    return result


def _markdown_ad_artifact_refs(evidence: Mapping[str, object]) -> str:
    """按证据角色渲染广告落地四件套引用。"""

    refs = evidence.get("artifact_refs")
    if isinstance(refs, Mapping):
        parts = [
            f"{_markdown_text(role)}="
            f"{_markdown_identifier(refs.get(role), identifier_kind='capture')}"
            for role in (
                "source_dom",
                "source_screenshot",
                "landing_dom",
                "landing_screenshot",
            )
            if refs.get(role)
        ]
        if parts:
            return ", ".join(parts)
    artifact_ids = evidence.get("artifact_ids")
    if isinstance(artifact_ids, list) and artifact_ids:
        return ", ".join(
            _markdown_identifier(item, identifier_kind="capture")
            for item in artifact_ids
        )
    return "-"


def _safe_ad_artifact_refs(evidence: Mapping[str, object]) -> str:
    """HTML 报告使用同一角色顺序与标识符白名单。"""

    refs = evidence.get("artifact_refs")
    if isinstance(refs, Mapping):
        parts = [
            f"{role}={_safe_identifier(refs.get(role), identifier_kind='capture')}"
            for role in (
                "source_dom",
                "source_screenshot",
                "landing_dom",
                "landing_screenshot",
            )
            if refs.get(role)
        ]
        if parts:
            return ", ".join(parts)
    artifact_ids = evidence.get("artifact_ids")
    if isinstance(artifact_ids, list) and artifact_ids:
        return ", ".join(
            _safe_identifier(item, identifier_kind="capture")
            for item in artifact_ids
        )
    return "-"


def _safe_runtime_text(value: object) -> str:
    """把不可信运行时文本脱敏为可安全进入 JSON 或 Markdown 的单行值。"""

    try:
        return sanitize_web_model_reason(str(value))
    except ValueError:
        return "[redacted-sensitive]"


def _safe_identifier(value: object, *, identifier_kind: str) -> str:
    """返回符合字段合同的原始标识符，否则统一替换。"""

    normalized = " ".join(str(value).split())
    validators = {
        "task_run": _valid_uuid_id,
        "capture": _valid_uuid_id,
        "candidate": _valid_web_candidate_id,
        "action": _valid_action_execution_id,
        "error_code": _valid_web_error_code,
    }
    validator = validators.get(identifier_kind)
    if validator is None:
        raise ValueError("identifier_kind 无效")
    if normalized != "-" and not validator(normalized):
        normalized = "[redacted-sensitive]"
    return normalized


def _markdown_cell(
    value: object,
    *,
    identifier_kind: str | None = None,
) -> str:
    """把不可信值收敛成单行 Markdown 表格单元格。"""

    return (
        _markdown_identifier(value, identifier_kind=identifier_kind)
        if identifier_kind is not None
        else _markdown_text(value)
    )


def _valid_uuid_id(value: object) -> bool:
    return isinstance(value, str) and _UUID_ID_PATTERN.fullmatch(value.strip()) is not None


def _valid_web_candidate_id(value: object) -> bool:
    return isinstance(value, str) and _WEB_CANDIDATE_ID_PATTERN.fullmatch(value.strip()) is not None


def _valid_web_error_code(value: object) -> bool:
    return isinstance(value, str) and _WEB_ERROR_CODE_PATTERN.fullmatch(value.strip()) is not None


def _write_stdout_utf8(text: str, *, stream: Any | None = None) -> None:
    """绕过 Windows 控制台代码页，以 UTF-8 输出机器结果。"""

    target = sys.stdout if stream is None else stream
    binary = getattr(target, "buffer", None)
    if binary is not None and callable(getattr(binary, "write", None)):
        binary.write(text.encode("utf-8"))
        flush = getattr(binary, "flush", None)
        if callable(flush):
            flush()
        return
    target.write(text)
    flush = getattr(target, "flush", None)
    if callable(flush):
        flush()


def _safe_live_stdout_payload(
    report: LiveSixSiteReport,
    *,
    report_path: Path,
    html_report_path: Path | None = None,
) -> dict[str, object]:
    """输出严格白名单摘要，禁止把运行时原始响应写入终端或 CI 日志。"""

    return {
        "passed": bool(report.passed),
        "acceptance_scope": report.acceptance_scope,
        "acceptance_failure_count": len(report.acceptance_failures),
        "acceptance_failures": list(report.acceptance_failures),
        "live_validation_passed": bool(report.live_validation_passed),
        "manual_report_review_passed": bool(report.manual_report_review_passed),
        "server_completion_gate_passed": bool(report.server_completion_gate_passed),
        "site_count": _safe_int(report.site_count),
        "menu_total": _safe_int(report.menu_total),
        "menu_limit": None if report.menu_limit is None else _safe_int(report.menu_limit),
        "executed_same_origin_count": _safe_int(report.executed_same_origin_count),
        "expected_executed_same_origin_count": _safe_int(
            report.expected_executed_same_origin_count
        ),
        "menu_limit_skipped_count": _safe_int(report.menu_limit_skipped_count),
        "expected_menu_limit_skipped_count": _safe_int(report.expected_menu_limit_skipped_count),
        "home_baseline_count": _safe_int(report.home_baseline_count),
        "detail_count": _safe_int(report.detail_count),
        "blocked_cross_origin_count": _safe_int(report.blocked_cross_origin_count),
        "frontier_remaining": _safe_int(report.frontier_remaining),
        "validation_error_count": len(report.validation_errors),
        "sites": [
            {
                "target_url": _safe_runtime_text(site.target_url),
                "task_id": _safe_identifier(
                    site.task_id,
                    identifier_kind="task_run",
                ),
                "run_id": _safe_identifier(
                    site.run_id,
                    identifier_kind="task_run",
                ),
                "task_status": _safe_runtime_text(site.task_status),
                "report_stage": _safe_runtime_text(site.report_stage),
                "is_final": bool(site.is_final),
                "menu_total": _safe_int(site.exploration_summary.get("menu_total")),
                "home_count": _safe_int(
                    site.evidence_bundle.get("summary", {}).get("home_count")
                    if isinstance(site.evidence_bundle.get("summary"), dict)
                    else 0
                ),
                "executed_same_origin_count": _safe_int(
                    site.exploration_summary.get("executed_same_origin_count")
                ),
                "detail_count": _safe_int(
                    site.evidence_bundle.get("summary", {}).get("detail_count")
                    if isinstance(site.evidence_bundle.get("summary"), dict)
                    else 0
                ),
                "menu_limit_skipped_count": _safe_int(
                    site.exploration_summary.get("menu_limit_skipped_count")
                ),
                "frontier_remaining": _safe_int(site.exploration_summary.get("frontier_remaining")),
            }
            for site in report.sites
        ],
        "report_path": _safe_runtime_text(report_path.resolve()),
        "html_report_path": (
            _safe_runtime_text(html_report_path.resolve()) if html_report_path is not None else None
        ),
    }


def _interactive_report_review(site: LiveSiteResult) -> tuple:
    """在实况任务结束后等待操作者逐项确认两个工作台报告入口。"""

    print(
        "\n请人工复核站点："
        f"{_safe_runtime_text(site.target_url)}\n"
        f"API 报告：{_public_live_report_url(site.report_url)}\n"
        f"工作台入口：{_public_workstation_report_path(site.workstation_report_path)}",
        file=sys.stderr,
        flush=True,
    )

    def confirmed(prompt: str) -> bool:
        print(prompt + "（输入 yes 确认）：", file=sys.stderr, flush=True)
        return sys.stdin.readline().strip().casefold() == "yes"

    entries = (confirmed("任务列表已能打开该任务报告"), confirmed("任务详情已能打开同一任务报告"))
    print("实际复核者姓名或标识（未复核请留空）：", file=sys.stderr, flush=True)
    reviewer = sys.stdin.readline().strip()[:100]
    from datetime import datetime

    records = tuple({"task_id": site.task_id, "run_id": site.run_id,
                     "logical_page_id": page.get("logical_page_id"), "reviewer": reviewer,
                     "reviewed_at": datetime.now(UTC).isoformat(),
                     "reviewed": bool(reviewer) and confirmed(f"已逐页检查 {page.get('logical_page_id')} 的截图、分类与质检原因")}
                    for page in site.logical_page_assessments)
    return (*entries, records)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    parser.add_argument(
        "--live",
        action="store_true",
        help="通过正式 API 顺序提交指定目标探索；默认只做离线对账",
    )
    target_selection = parser.add_mutually_exclusive_group()
    target_selection.add_argument(
        "--site-id",
        action="append",
        choices=tuple(_SITE_ID_TARGETS),
        help="live 模式仅提交指定六站白名单子集；可重复，执行顺序按固定清单归一化",
    )
    target_selection.add_argument(
        "--target-url", action="append",
        help="live 模式的 HTTPS 目标；可重复传入不同 URL，按请求顺序执行",
    )
    parser.add_argument(
        "--adult-self-attestation",
        choices=("adult-v1",),
        help="live 模式必须显式声明 adult-v1",
    )
    parser.add_argument("--username", help="通过现有登录接口获取本次新令牌")
    parser.add_argument("--password-env", help="密码所在的环境变量名，不接受明文密码参数")
    parser.add_argument("--adult-statement-env", help="成年声明环境变量；默认名称可复用持久 Web 配置")
    parser.add_argument("--output-dir", type=Path, help="保存 JSON、Markdown、HTML 和逐站证据")
    parser.add_argument(
        "--api-base-url",
        default="http://127.0.0.1:8000",
        help="Alioth API 根地址",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=2.0,
        help="live 模式任务轮询间隔",
    )
    parser.add_argument(
        "--site-timeout-seconds",
        type=float,
        default=DEFAULT_LIVE_SITE_TIMEOUT_SECONDS,
        help="live 模式每个站点最大等待秒数，默认 75 分钟",
    )
    parser.add_argument(
        "--live-report-path",
        type=Path,
        default=ROOT / "docs/reports/2026-09-01-web-v2-six-site-hard-budget-retest.md",
        help="live 模式完整 Markdown 报告路径",
    )
    parser.add_argument(
        "--live-html-report-path",
        type=Path,
        default=ROOT / "docs/reports/2026-09-01-web-v2-six-site-hard-budget-retest.html",
        help="live 模式静态 HTML 报告路径",
    )
    parser.add_argument(
        "--interactive-report-review",
        action="store_true",
        help="live 完成后逐站等待人工确认任务列表和详情报告入口",
    )
    args = parser.parse_args(argv)
    if args.live:
        from scripts.web_live_acceptance import (
            prepare_live_access,
            run_live_acceptance,
            write_live_json_report,
        )

        try:
            selected_targets = (_normalize_live_targets(args.target_url)
                                if args.target_url else _targets_from_site_ids(args.site_id))
            access_token, statement, audit = prepare_live_access(args, requester=_http_json_request)
        except ValueError as exc:
            parser.error(str(exc))
        if args.output_dir:
            live_report = run_live_acceptance(
                args, access_token=access_token, statement=statement, audit=audit,
                targets=selected_targets, requester=_http_json_request,
            )
            args.live_report_path = args.output_dir / "final-exploration-report.md"
            args.live_html_report_path = args.output_dir / "final-exploration-report.html"
        else:
            live_report = run_live_sites(
                api_base_url=args.api_base_url,
                access_token=access_token,
                adult_self_attestation=statement,
                requester=_http_json_request,
                poll_interval_seconds=args.poll_interval_seconds,
                site_timeout_seconds=args.site_timeout_seconds,
                targets=selected_targets,
            )
        if args.interactive_report_review:
            live_report = _apply_manual_report_review(
                live_report,
                reviewer=_interactive_report_review,
            )
        if args.output_dir:
            write_live_json_report(args.output_dir, live_report)
        args.live_report_path.parent.mkdir(parents=True, exist_ok=True)
        args.live_report_path.write_text(
            render_live_markdown(live_report),
            encoding="utf-8",
        )
        args.live_html_report_path.parent.mkdir(parents=True, exist_ok=True)
        args.live_html_report_path.write_text(
            render_live_html(live_report),
            encoding="utf-8",
        )
        payload = _safe_live_stdout_payload(
            live_report,
            report_path=args.live_report_path,
            html_report_path=args.live_html_report_path,
        )
        _write_stdout_utf8(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0 if live_report.passed else 1
    report = reconcile_all_fixture_sites()
    if args.json:
        _write_stdout_utf8(json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n")
    else:
        _write_stdout_utf8(render_markdown(report))
    return 0 if report.drift_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
