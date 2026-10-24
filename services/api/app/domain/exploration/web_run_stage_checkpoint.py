"""Web 专用不可变阶段游标；只承载恢复引用，不承载页面内容。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4


class WebRunStage(StrEnum):
    BOOTSTRAP_NAVIGATION = "bootstrap_navigation"
    OBSTRUCTION_CLEAR = "obstruction_clear"
    READINESS = "readiness"
    PRIMARY_CAPTURE = "primary_capture"
    FULL_PAGE_CAPTURE = "full_page_capture"
    CLASSIFICATION = "classification"
    HREF_DISCOVERY = "href_discovery"
    CANDIDATE_NAVIGATION = "candidate_navigation"
    FRONTIER_DRAIN = "frontier_drain"
    EVIDENCE_RECONCILIATION = "evidence_reconciliation"
    COVERAGE_SNAPSHOT = "coverage_snapshot"
    REPORT_FINALIZATION = "report_finalization"
    SESSION_CLOSE = "session_close"
    AD_ISOLATED_NAVIGATION = "ad_isolated_navigation"
    AD_CAPTURE = "ad_capture"
    AD_CLOSEOUT = "ad_closeout"


class WebStageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    GAPPED = "gapped"
    CANCELLED = "cancelled"


_BUSINESS = tuple(WebRunStage)[:13]
_ADS = tuple(WebRunStage)[13:]
_SUCCESSORS = {a: b for path in (_BUSINESS, _ADS) for a, b in zip(path, path[1:], strict=False)}
_GAP_TARGETS = {
    WebRunStage.BOOTSTRAP_NAVIGATION: WebRunStage.FRONTIER_DRAIN,
    WebRunStage.CANDIDATE_NAVIGATION: WebRunStage.FRONTIER_DRAIN,
    WebRunStage.READINESS: WebRunStage.FRONTIER_DRAIN,
    WebRunStage.PRIMARY_CAPTURE: WebRunStage.FRONTIER_DRAIN,
    WebRunStage.CLASSIFICATION: WebRunStage.FRONTIER_DRAIN,
    WebRunStage.AD_ISOLATED_NAVIGATION: WebRunStage.AD_CLOSEOUT,
}
_REFERENCE_FIELDS = {
    "menu_navigation_capture_id",
    "parent_capture_id",
    "login_source_capture_id",
    "capture_id",
    "menu_ledger_id",
    "session_id",
    "idempotency_key",
    "obstruction_fingerprint",
    "surface_phase_run_id",
}
_NUMBER_FIELDS = {"candidate_href_index", "finalization_epoch"}
_URL_FIELDS = {"current_url", "requested_url", "final_url"}
# 复用审计安全规则的整词边界语义；领域层不反向依赖 application。
_SENSITIVE_QUERY_KEY = re.compile(
    r"(^|_)(password|passwd|pwd|secret|token|authorization|auth|auth_header|"
    r"cookie|cookies|api_key|apikey|access_key|secret_key|private_key|credential|"
    r"credentials|bearer|jwt|session_token|refresh_token|otp|signature|sig)$",
    re.I,
)


def _code(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", value):
        raise ValueError("错误和 gap 只能保存结构化代码")
    return value


def _reference(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[\w:./-]{1,512}", value):
        raise ValueError("恢复引用必须为不含页面内容和凭据的标识")
    return value


def _url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("URL 无效")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("URL 必须为无凭据 HTTP(S)")
    if parsed.fragment or any(
        _SENSITIVE_QUERY_KEY.search(key.replace("-", "_"))
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise ValueError("URL 不得保存令牌或凭据")
    return value


def _blocked_login_requests(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, tuple | list) or len(value) > 20:
        raise ValueError("登录拦截记录只允许有界源站列表")
    result = []
    for item in value:
        if not isinstance(item, tuple | list) or len(item) != 2:
            raise ValueError("登录拦截记录格式无效")
        origin, method = item
        if not isinstance(origin, str) or len(origin) > 300:
            raise ValueError("登录拦截源站无效")
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            origin != f"{parsed.scheme}://{parsed.netloc}" or
            not isinstance(method, str) or
            not re.fullmatch(r"[A-Z]{3,10}", method)):
            raise ValueError("登录拦截记录只能保存 HTTP(S) 源站与方法")
        result.append((origin, method))
    return tuple(result)


def _login_account_page_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("登录账户页 URL 无效")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or value != f"https://{parsed.netloc}{parsed.path or '/'}"
    ):
        raise ValueError("登录账户页 URL 只能保存无查询参数的 HTTPS 地址")
    return value


def _login_artifact_references(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple | list) or len(value) > 24:
        raise ValueError("登录账户制品引用数量无效")
    return tuple(_reference(item) for item in value)


def _login_account_page_visits(value: object) -> tuple[tuple[str, str, str, str], ...]:
    if not isinstance(value, tuple | list) or len(value) > 2:
        raise ValueError("登录账户页访问记录无效")
    visits = []
    for item in value:
        if not isinstance(item, tuple | list) or len(item) != 4:
            raise ValueError("登录账户页访问记录无效")
        page_type, page_url, screenshot_id, dom_id = item
        if page_type not in {"recharge_page", "topup_page"}:
            raise ValueError("登录账户页类型无效")
        visits.append((
            page_type, _login_account_page_url(page_url),
            _reference(screenshot_id), _reference(dom_id),
        ))
    if len({item[0] for item in visits}) != len(visits):
        raise ValueError("登录账户页类型重复")
    if len(visits) == 2 and (
        tuple(item[0] for item in visits) != ("recharge_page", "topup_page")
        or urlsplit(visits[0][1]).netloc != urlsplit(visits[1][1]).netloc
    ):
        raise ValueError("登录账户页访问顺序或源站无效")
    return tuple(visits)


def _payload(value: Mapping[str, object]) -> Mapping[str, object]:
    result = {}
    for key, item in value.items():
        if key in _REFERENCE_FIELDS:
            result[key] = _reference(item)
        elif key in _URL_FIELDS:
            result[key] = _url(item)
        elif key in _NUMBER_FIELDS and type(item) is int and item >= 0:
            result[key] = item
        elif key == "login_branch_status" and item in {
            "skipped", "authenticated", "incomplete"
        }:
            result[key] = item
        elif key == "login_branch_reason":
            result[key] = _code(item)
        elif key == "login_cross_origin_status" and item in {"clear", "blocked"}:
            result[key] = item
        elif key == "login_blocked_requests":
            result[key] = _blocked_login_requests(item)
        elif key == "login_account_status" and item in {
            "not_run", "secret_residue", "obstructed", "avatar_obstructed",
            "entry_unproven", "entry_ambiguous", "entry_obstructed",
            "account_observed", "recharge_opened",
            "inspection_failed", "guest_after_fresh_navigation", "account_auth_unverified",
            "avatar_missing",
        }:
            result[key] = item
        elif key == "login_account_page_type" and item in {
            "recharge_page", "topup_page"
        }:
            result[key] = item
        elif key == "login_account_page_url":
            result[key] = _login_account_page_url(item)
        elif key == "login_account_page_visits":
            result[key] = _login_account_page_visits(item)
        elif key == "login_account_avatar_opened" and type(item) is bool:
            result[key] = item
        elif key == "login_account_avatar_dom_artifact_id":
            result[key] = _reference(item)
        elif key in {"login_account_artifact_ids", "login_account_storage_paths"}:
            result[key] = _login_artifact_references(item)
        else:
            raise ValueError("cursor_payload 只允许已定义的恢复引用")
    return MappingProxyType(result)


def _error_detail(value: Mapping[str, object]) -> Mapping[str, object]:
    audit = {}
    references = {}
    for key, item in value.items():
        if key in {"raw_error_code", "cause_type"}:
            if not isinstance(item, str) or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_.-]{0,127}", item
            ):
                raise ValueError("协调诊断只能保存有界安全 token")
            audit[key] = item
        elif key == "coordination_stage":
            if item not in {"before_capture", "action", "after_capture"}:
                raise ValueError("协调阶段必须属于固定枚举")
            audit[key] = item
        elif key == "retryable":
            if type(item) is not bool:
                raise ValueError("协调重试标记必须是布尔值")
            audit[key] = item
        else:
            references[key] = item
    return MappingProxyType({**_payload(references), **audit})


@dataclass(frozen=True, kw_only=True)
class WebStageRetryPolicy:
    max_attempts: int = 1
    base_delay_seconds: int = 1
    gap_code: str = "WEB_STAGE_FAILED"

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ValueError("阶段尝试次数必须为 1..3")
        if type(self.base_delay_seconds) is not int or not 1 <= self.base_delay_seconds <= 60:
            raise ValueError("退避基数必须为 1..60 秒")
        _code(self.gap_code)

    @classmethod
    def for_failure(cls, stage: WebRunStage, error_code: str) -> WebStageRetryPolicy:
        _code(error_code)
        if error_code in {
            "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT", "WEB_EXPLORATION_DEADLINE_EXHAUSTED"
        }:
            return cls(gap_code=error_code)
        if error_code.startswith("WEB_LOGIN_"):
            # 登录提交不可安全重放；保留独立缺口代码供质检与恢复观察。
            return cls(gap_code=error_code)
        if stage is WebRunStage.READINESS:
            return cls(max_attempts=2, gap_code=error_code)
        matrix = {
            WebRunStage.BOOTSTRAP_NAVIGATION: (3, "WEB_NAVIGATION_RETRY_EXHAUSTED"),
            WebRunStage.CANDIDATE_NAVIGATION: (3, "WEB_NAVIGATION_RETRY_EXHAUSTED"),
            WebRunStage.OBSTRUCTION_CLEAR: (2, "WEB_OBSTRUCTION_UNRESOLVED"),
            WebRunStage.PRIMARY_CAPTURE: (2, "WEB_PRIMARY_CAPTURE_FAILED"),
            WebRunStage.FULL_PAGE_CAPTURE: (2, "WEB_FULL_PAGE_SCREENSHOT_FAILED"),
            WebRunStage.AD_ISOLATED_NAVIGATION: (2, "WEB_AD_PAGE_OPEN_FAILED"),
        }
        attempts, gap = matrix.get(stage, (1, error_code))
        return cls(max_attempts=attempts, gap_code=gap)


@dataclass(frozen=True, kw_only=True)
class WebStageGap:
    stage: WebRunStage
    stage_generation: int
    code: str
    error_code: str
    attempt_count: int

    def __post_init__(self):
        if not isinstance(self.stage, WebRunStage):
            raise ValueError("gap 阶段必须属于 Web 固定阶段图")
        if type(self.stage_generation) is not int or self.stage_generation < 0:
            raise ValueError("gap 阶段代次必须为非负整数")
        if type(self.attempt_count) is not int or self.attempt_count <= 0:
            raise ValueError("gap 尝试次数必须为正整数")
        _code(self.code)
        _code(self.error_code)


@dataclass(frozen=True, kw_only=True)
class WebStageCursor:
    run_id: str
    logical_page_key: str
    current_stage: WebRunStage = WebRunStage.BOOTSTRAP_NAVIGATION
    status: WebStageStatus = WebStageStatus.PENDING
    stage_generation: int = 0
    cursor_payload: Mapping[str, object] = field(default_factory=dict)
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    last_error_code: str | None = None
    last_error_detail: Mapping[str, object] = field(default_factory=dict)
    gaps: tuple[WebStageGap, ...] = ()
    obstruction_attempts: Mapping[str, int] = field(default_factory=dict)
    artifact_ids: tuple[str, ...] = ()
    row_version: int = 0
    claimed_worker_id: str | None = None
    claim_token: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self):
        UUID(self.run_id)
        _reference(self.logical_page_key)
        if not isinstance(self.current_stage, WebRunStage) or not isinstance(
            self.status, WebStageStatus
        ):
            raise ValueError("阶段或状态无效")
        for value in (self.stage_generation, self.attempt_count, self.row_version):
            if type(value) is not int or value < 0:
                raise ValueError("游标计数必须为非负整数")
        for value in (
            self.created_at,
            self.updated_at,
            self.next_attempt_at,
            self.claimed_at,
            self.lease_expires_at,
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("阶段时间必须包含时区")
        if self.last_error_code is not None:
            _code(self.last_error_code)
        if any(not isinstance(gap, WebStageGap) for gap in self.gaps):
            raise ValueError("gap 必须是已校验的结构化记录")
        if self.status is WebStageStatus.GAPPED and (
            self.attempt_count <= 0
            or not any(
                gap.stage is self.current_stage
                and gap.stage_generation == self.stage_generation
                and gap.attempt_count == self.attempt_count
                and gap.error_code == self.last_error_code
                for gap in self.gaps
            )
        ):
            raise ValueError("gapped 必须有匹配当前阶段、代次、尝试次数和原因的审计记录")
        if (self.status is WebStageStatus.RETRY_WAIT) != (self.next_attempt_at is not None):
            raise ValueError("重试状态必须且只能持有下次尝试时间")
        if self.status is WebStageStatus.RUNNING and (
            not self.claim_token
            or not self.claimed_worker_id
            or self.claimed_at is None
            or self.lease_expires_at is None
            or self.lease_expires_at <= self.claimed_at
            or self.attempt_count == 0
        ):
            raise ValueError("运行状态必须持有有效领取信息")
        attempts = dict(self.obstruction_attempts)
        for fingerprint, count in attempts.items():
            _reference(fingerprint)
            if type(count) is not int or not 0 < count <= 2:
                raise ValueError("每个弹窗指纹最多尝试两次")
        object.__setattr__(self, "obstruction_attempts", MappingProxyType(attempts))
        object.__setattr__(self, "cursor_payload", _payload(self.cursor_payload))
        object.__setattr__(self, "last_error_detail", _error_detail(self.last_error_detail))
        object.__setattr__(self, "gaps", tuple(self.gaps))
        object.__setattr__(
            self, "artifact_ids", tuple(str(UUID(item)) for item in self.artifact_ids)
        )

    @classmethod
    def start(
        cls,
        *,
        run_id: str,
        logical_page_key: str,
        stage: WebRunStage = WebRunStage.BOOTSTRAP_NAVIGATION,
        **kwargs,
    ) -> WebStageCursor:
        return cls(run_id=run_id, logical_page_key=logical_page_key, current_stage=stage, **kwargs)

    @property
    def gap_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(gap.code for gap in self.gaps))

    @property
    def next_stage(self) -> WebRunStage | None:
        if self.status not in {WebStageStatus.SUCCEEDED, WebStageStatus.GAPPED}:
            return None
        if self.status is WebStageStatus.GAPPED and self.current_stage in _GAP_TARGETS:
            return _GAP_TARGETS[self.current_stage]
        return _SUCCESSORS.get(self.current_stage)

    @property
    def is_terminal(self) -> bool:
        return self.status is WebStageStatus.CANCELLED or (
            self.status in {WebStageStatus.SUCCEEDED, WebStageStatus.GAPPED}
            and self.next_stage is None
        )

    @property
    def idempotency_key(self) -> str:
        identity = (
            f"{self.run_id}\0{self.logical_page_key}\0{self.current_stage}\0{self.stage_generation}"
        )
        return "web-stage:" + sha256(identity.encode()).hexdigest()

    def advance(self, stage: WebRunStage) -> WebStageCursor:
        if self.is_terminal or stage != self.next_stage:
            raise ValueError("阶段只能从已完成结果推进到固定后继，禁止回退或跳步")
        return replace(
            self,
            current_stage=stage,
            status=WebStageStatus.PENDING,
            attempt_count=0,
            next_attempt_at=None,
            claimed_worker_id=None,
            claim_token=None,
            claimed_at=None,
            lease_expires_at=None,
        )

    def claim(self, *, worker_id: str, now: datetime, lease_seconds: int = 60) -> WebStageCursor:
        _reference(worker_id)
        if not 1 <= lease_seconds <= 3600 or self.is_terminal:
            raise ValueError("终态不可领取，租约必须有界")
        cursor = self.advance(self.next_stage) if self.next_stage else self
        if cursor.status is WebStageStatus.RUNNING and (
            cursor.lease_expires_at is None or cursor.lease_expires_at > now
        ):
            raise ValueError("阶段已由有效租约领取")
        if cursor.next_attempt_at and cursor.next_attempt_at > now:
            raise ValueError("尚未到重试时间")
        if cursor.status is WebStageStatus.RUNNING:
            policy = WebStageRetryPolicy.for_failure(
                cursor.current_stage, "WEB_STAGE_LEASE_EXPIRED"
            )
            attempts = cursor.attempt_count
            if cursor.current_stage is WebRunStage.OBSTRUCTION_CLEAR:
                fingerprint = cursor.cursor_payload.get("obstruction_fingerprint", "unidentified")
                attempts = min(2, cursor.obstruction_attempts.get(fingerprint, 0) + 1)
                cursor = replace(
                    cursor,
                    obstruction_attempts={**cursor.obstruction_attempts, fingerprint: attempts},
                )
            if attempts >= policy.max_attempts:
                return cursor.record_gap_and_advance(
                    error_code="WEB_STAGE_LEASE_EXPIRED", gap_code=policy.gap_code
                )
        return replace(
            cursor,
            status=WebStageStatus.RUNNING,
            attempt_count=cursor.attempt_count + 1,
            claimed_worker_id=worker_id,
            claim_token=str(uuid4()),
            claimed_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            next_attempt_at=None,
            updated_at=now,
        )

    def _running(self):
        if self.status is not WebStageStatus.RUNNING:
            raise ValueError("阶段结果只能由已领取阶段写入")

    def mark_succeeded(self, *, artifact_ids: tuple[str, ...] = ()) -> WebStageCursor:
        self._running()
        return replace(
            self,
            status=WebStageStatus.SUCCEEDED,
            next_attempt_at=None,
            artifact_ids=tuple(dict.fromkeys((*self.artifact_ids, *artifact_ids))),
        )

    def record_gap_and_advance(
        self,
        *,
        error_code: str,
        gap_code: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> WebStageCursor:
        self._running()
        policy = WebStageRetryPolicy.for_failure(self.current_stage, error_code)
        gap = WebStageGap(
            stage=self.current_stage,
            stage_generation=self.stage_generation,
            code=gap_code or policy.gap_code,
            error_code=error_code,
            attempt_count=self.attempt_count,
        )
        return replace(
            self,
            status=WebStageStatus.GAPPED,
            next_attempt_at=None,
            last_error_code=error_code,
            last_error_detail=detail or {},
            cursor_payload=self._failure_cursor_payload(detail),
            gaps=(*self.gaps, gap),
        )

    def schedule_retry(
        self,
        *,
        error_code: str,
        now: datetime,
        transient: bool = True,
        detail: Mapping[str, object] | None = None,
        policy: WebStageRetryPolicy | None = None,
    ) -> WebStageCursor:
        self._running()
        policy = policy or WebStageRetryPolicy.for_failure(self.current_stage, error_code)
        cursor = self
        attempts = self.attempt_count
        if self.current_stage is WebRunStage.OBSTRUCTION_CLEAR:
            fingerprint = (detail or {}).get("obstruction_fingerprint") or self.cursor_payload.get(
                "obstruction_fingerprint", "unidentified"
            )
            attempts = min(2, self.obstruction_attempts.get(fingerprint, 0) + 1)
            cursor = replace(
                self, obstruction_attempts={**self.obstruction_attempts, fingerprint: attempts}
            )
        if not transient or attempts >= policy.max_attempts:
            return cursor.record_gap_and_advance(
                error_code=error_code, gap_code=policy.gap_code, detail=detail
            )
        return replace(
            cursor,
            status=WebStageStatus.RETRY_WAIT,
            last_error_code=error_code,
            last_error_detail=detail or {},
            cursor_payload=cursor._failure_cursor_payload(detail),
            next_attempt_at=now
            + timedelta(seconds=policy.base_delay_seconds * 2 ** (attempts - 1)),
        )

    def _failure_cursor_payload(self, detail):
        """就绪换帧引用与失败结算原子落库，不改变会话、代次或原始父帧。"""
        if self.current_stage is not WebRunStage.READINESS or not detail:
            return self.cursor_payload
        validated = _error_detail(detail)
        if "capture_id" not in validated and "current_url" not in validated:
            return self.cursor_payload
        if "capture_id" not in validated or "current_url" not in validated:
            raise ValueError("就绪恢复必须同时提供当前 Capture 与 URL")
        UUID(validated["capture_id"])
        for key in ("session_id", "finalization_epoch"):
            if key in validated and validated[key] != self.cursor_payload.get(key):
                raise ValueError("就绪恢复不得切换会话或代次")
        return {
            **self.cursor_payload,
            "capture_id": validated["capture_id"],
            "current_url": validated["current_url"],
        }

    def fail_transient(self, error_code: str, *, max_attempts: int) -> WebStageCursor:
        """纯领域驱动便捷入口；生产领取和回写仍通过仓储 CAS。"""
        now = self.next_attempt_at or datetime.now(UTC)
        cursor = (
            self
            if self.status is WebStageStatus.RUNNING
            else self.claim(worker_id="domain", now=now)
        )
        policy = replace(
            WebStageRetryPolicy.for_failure(self.current_stage, error_code),
            max_attempts=max_attempts,
        )
        return cursor.schedule_retry(error_code=error_code, now=now, policy=policy)
