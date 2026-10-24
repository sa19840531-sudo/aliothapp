"""糖果短剧：有状态主流程进度（持久化于 observation.metadata）。

不是产品专属编排执行器；只在 agent_profile_only 路径上驱动阶段转移与预算。
阶段：影片 → 上方分类 →（条件）漫画 → 底栏 → 我的／支付。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

TANGGUO_FLOW_STATE_VERSION = "tangguo-flow-state.v2"
TANGGUO_PACKAGE = "com.alz7xm2.drama"

PhaseId = Literal[
    "home_confirm",
    "video_entry",
    "video_verify",
    "back_home",
    "top_tabs",
    "top_tab_restore",
    "comic_entry",
    "comic_detail",
    "comic_reader",
    "comic_scroll",
    "comic_back_home",
    "bottom_tabs",
    "wallet_search",
    "payment_alipay",
    "payment_back_home",
    "final_home",
    "done",
]

PHASE_ORDER: tuple[PhaseId, ...] = (
    "home_confirm",
    "video_entry",
    "video_verify",
    "back_home",
    "top_tabs",
    "top_tab_restore",
    "comic_entry",
    "comic_detail",
    "comic_reader",
    "comic_scroll",
    "comic_back_home",
    "bottom_tabs",
    "wallet_search",
    "payment_alipay",
    "payment_back_home",
    "final_home",
    "done",
)

_COMIC_PHASES = frozenset(
    {
        "comic_entry",
        "comic_detail",
        "comic_reader",
        "comic_scroll",
        "comic_back_home",
    }
)

StageResult = Literal["passed", "failed", "blocked", "skipped", "pending"]
ComicFlowStatus = Literal[
    "not_started",
    "in_progress",
    "passed",
    "failed",
    "blocked",
    "skipped",
]


@dataclass
class TangguoBudgets:
    splash_dismiss_max: int = 3
    popup_dismiss_max: int = 2
    video_candidates_max: int = 2
    back_home_max: int = 2
    profile_scroll_max: int = 2
    top_tab_visit_limit: int = 3
    top_tab_scroll_max: int = 2
    comic_candidates_max: int = 2
    comic_read_swipes: int = 3
    comic_swipe_ratio: float = 0.6
    comic_back_home_max: int = 4
    device_actions_max: int = 100
    ai_calls_max: int = 50
    recoveries_max: int = 4
    retries_max: int = 3
    wall_clock_timeout_sec: int = 3600
    # 底栏按实际分頁数配置，不得套用上方分类的 3 个抽样上限
    tab_device_actions_max: int = 40
    tab_actions_per_tab: int = 4
    top_tab_device_actions_max: int = 18
    comic_device_actions_max: int = 20
    payment_device_actions_max: int = 15
    payment_ai_calls_max: int = 12
    final_home_back_max: int = 4

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> TangguoBudgets:
        if not isinstance(raw, Mapping):
            return cls()

        def _int(name: str, default: int) -> int:
            value = raw.get(name, default)
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else default
            )

        def _float(name: str, default: float) -> float:
            value = raw.get(name, default)
            if isinstance(value, bool):
                return default
            if isinstance(value, (int, float)) and 0.1 <= float(value) <= 0.95:
                return float(value)
            return default

        return cls(
            splash_dismiss_max=_int("splash_dismiss_max", 3),
            popup_dismiss_max=_int("popup_dismiss_max", 2),
            video_candidates_max=_int("video_candidates_max", 2),
            back_home_max=_int("back_home_max", 2),
            profile_scroll_max=_int("profile_scroll_max", 2),
            top_tab_visit_limit=_int("top_tab_visit_limit", 3),
            top_tab_scroll_max=_int("top_tab_scroll_max", 2),
            comic_candidates_max=_int("comic_candidates_max", 2),
            comic_read_swipes=_int("comic_read_swipes", 3),
            comic_swipe_ratio=_float("comic_swipe_ratio", 0.6),
            comic_back_home_max=_int("comic_back_home_max", 4),
            device_actions_max=_int("device_actions_max", 100),
            ai_calls_max=_int("ai_calls_max", 50),
            recoveries_max=_int("recoveries_max", 4),
            retries_max=_int("retries_max", 3),
            wall_clock_timeout_sec=_int("wall_clock_timeout_sec", 3600),
            tab_device_actions_max=_int("tab_device_actions_max", 40),
            tab_actions_per_tab=_int("tab_actions_per_tab", 4),
            top_tab_device_actions_max=_int("top_tab_device_actions_max", 18),
            comic_device_actions_max=_int("comic_device_actions_max", 20),
            payment_device_actions_max=_int("payment_device_actions_max", 15),
            payment_ai_calls_max=_int("payment_ai_calls_max", 12),
            final_home_back_max=_int("final_home_back_max", 4),
        )


@dataclass
class ResumeCheckpoint:
    """子流程进入前保存的接续位置。"""

    phase: str
    remaining_top_tabs: list[str] = field(default_factory=list)
    remaining_bottom_tabs: list[str] = field(default_factory=list)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "remaining_top_tabs": list(self.remaining_top_tabs),
            "remaining_bottom_tabs": list(self.remaining_bottom_tabs),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object] | None) -> ResumeCheckpoint | None:
        if not isinstance(raw, Mapping):
            return None
        phase = str(raw.get("phase") or "").strip()
        if not phase:
            return None
        return cls(
            phase=phase,
            remaining_top_tabs=_str_list(raw.get("remaining_top_tabs")),
            remaining_bottom_tabs=_str_list(raw.get("remaining_bottom_tabs")),
            note=_optional_str(raw.get("note")),
        )


@dataclass
class TangguoFlowState:
    protocol_version: str = TANGGUO_FLOW_STATE_VERSION
    phase: PhaseId = "home_confirm"
    budgets: TangguoBudgets = field(default_factory=TangguoBudgets)
    started_at_iso: str | None = None
    device_actions_used: int = 0
    ai_calls_used: int = 0
    recoveries_used: int = 0
    retries_used: int = 0
    video_attempts: int = 0
    back_home_attempts: int = 0
    profile_scrolls: int = 0
    tab_device_actions_used: int = 0
    top_tab_device_actions_used: int = 0
    top_tab_scrolls: int = 0
    comic_device_actions_used: int = 0
    comic_attempts: int = 0
    comic_swipes_done: int = 0
    comic_swipes_effective: int = 0
    comic_swipes_verified: int = 0
    comic_scroll_source_capture: str | None = None
    comic_scroll_source_fingerprint: str | None = None
    comic_back_home_attempts: int = 0
    payment_device_actions_used: int = 0
    payment_ai_calls_used: int = 0
    # 视频
    attempted_video_labels: list[str] = field(default_factory=list)
    video_target: str | None = None
    video_outcome: str | None = None
    playback_confirmed: bool | None = None
    # 上方分类
    home_default_top_tab: str | None = None
    discovered_top_tabs: list[str] = field(default_factory=list)
    visited_top_tabs: list[str] = field(default_factory=list)
    top_tab_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    top_tabs_status: str | None = None
    # 底栏（全部实际点击，非抽样）
    discovered_bottom_tabs: list[str] = field(default_factory=list)
    pending_bottom_tabs: list[str] = field(default_factory=list)
    visited_bottom_tabs: list[str] = field(default_factory=list)
    failed_bottom_tabs: list[str] = field(default_factory=list)
    blocked_bottom_tabs: list[str] = field(default_factory=list)
    tab_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    bottom_coverage_status: str | None = None  # complete | incomplete
    wallet_search_done: bool = False
    final_home_attempts: int = 0
    payment_back_attempts: int = 0
    payment_navigation_target: str | None = None
    # 漫画（整任务最多一轮）
    comic_flow_status: ComicFlowStatus = "not_started"
    comic_title: str | None = None
    comic_configured_title: str | None = None
    attempted_comic_labels: list[str] = field(default_factory=list)
    comic_outcome: str | None = None
    comic_hit_end: bool | None = None
    comic_mode_mismatch: bool | None = None
    # 钱包／支付
    wallet_search_result: str | None = None
    wallet_entry_label: str | None = None
    payment_farthest_stage: str | None = None
    payment_blocked_reason: str | None = None
    alipay_available: bool | None = None
    alipay_selected: bool | None = None
    stop_reason: str | None = None
    payment_mode: str = "inspect_only"
    allow_pending_order_creation: bool = False
    payment_method: str = "alipay"
    video_title: str | None = None
    video_keywords: list[str] = field(default_factory=list)
    wallet_keywords: list[str] = field(default_factory=list)
    recovery_events: list[str] = field(default_factory=list)
    stage_results: dict[str, str] = field(default_factory=dict)
    resume_checkpoint: ResumeCheckpoint | None = None
    contract_runtime: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # observation.metadata 冻结后可能把 dict 字段变成 mappingproxy；
        # asdict → deepcopy 会 TypeError，导致 TURN_FAILED → 清理回桌面。
        from services.api.app.domain.protocol import thaw_json_mapping

        def _jsonable(value: Any) -> Any:
            if isinstance(value, Mapping):
                return thaw_json_mapping(value)
            if isinstance(value, (list, tuple)):
                return [_jsonable(item) for item in value]
            return value

        payload = {
            key: _jsonable(getattr(self, key))
            for key in self.__dataclass_fields__
            if key not in {"budgets", "resume_checkpoint"}
        }
        payload["budgets"] = {
            key: _jsonable(getattr(self.budgets, key))
            for key in self.budgets.__dataclass_fields__
        }
        if self.resume_checkpoint is not None:
            payload["resume_checkpoint"] = self.resume_checkpoint.to_dict()
        else:
            payload["resume_checkpoint"] = None
        return payload

    def as_report(self) -> dict[str, Any]:
        """结果报告（不含敏感凭证／密码）。"""
        video_status = self.stage_results.get("home_video") or self.stage_results.get(
            "video"
        )
        if not video_status and self.video_outcome:
            video_status = self.video_outcome
        wallet_status = self.wallet_search_result
        if wallet_status is None and self.wallet_search_done:
            wallet_status = "done"
        payment_status = self.stage_results.get("payment")
        if not payment_status and self.payment_farthest_stage:
            payment_status = self.payment_farthest_stage
        return {
            "current_phase": self.phase,
            "phase": self.phase,
            "resume_checkpoint": (
                self.resume_checkpoint.to_dict() if self.resume_checkpoint else None
            ),
            "stage_results": dict(self.stage_results),
            "video_flow_status": video_status,
            "video_target": self.video_target,
            "video_outcome": self.video_outcome,
            "playback_confirmed": self.playback_confirmed,
            "home_default_top_tab": self.home_default_top_tab,
            "discovered_top_tabs": list(self.discovered_top_tabs),
            "visited_top_tabs": list(self.visited_top_tabs),
            "top_tab_results": dict(self.top_tab_results),
            "top_tabs_status": self.top_tabs_status,
            "discovered_bottom_tabs": list(self.discovered_bottom_tabs),
            "bottom_tab_results": dict(self.tab_results),
            "pending_bottom_tabs": list(self.pending_bottom_tabs),
            "visited_bottom_tabs": list(self.visited_bottom_tabs),
            "failed_bottom_tabs": list(self.failed_bottom_tabs),
            "blocked_bottom_tabs": list(self.blocked_bottom_tabs),
            "tab_results": dict(self.tab_results),
            "coverage_status": self.bottom_coverage_status,
            "wallet_search_status": wallet_status,
            "wallet_search_done": self.wallet_search_done,
            "wallet_search_result": self.wallet_search_result,
            "wallet_entry_label": self.wallet_entry_label,
            "comic_flow_status": self.comic_flow_status,
            "comic_title": self.comic_title,
            "comic_outcome": self.comic_outcome,
            "comic_swipes_planned": self.budgets.comic_read_swipes,
            "comic_swipes_done": self.comic_swipes_done,
            "comic_swipes_effective": self.comic_swipes_effective,
            "comic_hit_end": self.comic_hit_end,
            "comic_mode_mismatch": self.comic_mode_mismatch,
            "payment_flow_status": payment_status,
            "alipay_available": self.alipay_available,
            "alipay_selected": self.alipay_selected,
            "payment_farthest_stage": self.payment_farthest_stage,
            "payment_blocked_reason": self.payment_blocked_reason,
            "payment_mode": self.payment_mode,
            "payment_method": self.payment_method,
            "allow_pending_order_creation": self.allow_pending_order_creation,
            "stop_reason": self.stop_reason,
            "device_actions_used": self.device_actions_used,
            "ai_calls_used": self.ai_calls_used,
            "recoveries_used": self.recoveries_used,
            "phase_budgets": asdict(self.budgets),
            "total_budget": {
                "device_actions_max": self.budgets.device_actions_max,
                "device_actions_used": self.device_actions_used,
                "ai_calls_max": self.budgets.ai_calls_max,
                "ai_calls_used": self.ai_calls_used,
                "recoveries_max": self.budgets.recoveries_max,
                "recoveries_used": self.recoveries_used,
                "wall_clock_timeout_sec": self.budgets.wall_clock_timeout_sec,
                "started_at_iso": self.started_at_iso,
            },
            "budgets": asdict(self.budgets),
            "started_at_iso": self.started_at_iso,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object] | None) -> TangguoFlowState:
        if not isinstance(raw, Mapping):
            return cls()
        # 兼容 v1：未知阶段回落 home_confirm；字段缺省用默认
        phase_raw = str(raw.get("phase") or "home_confirm").strip()
        phase: PhaseId = (
            phase_raw if phase_raw in PHASE_ORDER else "home_confirm"
        )  # type: ignore[assignment]
        budgets = TangguoBudgets.from_mapping(
            raw.get("budgets") if isinstance(raw.get("budgets"), Mapping) else None
        )
        stage_raw = raw.get("stage_results")
        stage_results = (
            {str(k): str(v) for k, v in stage_raw.items()}
            if isinstance(stage_raw, Mapping)
            else {}
        )
        tab_results = _dict_of_dicts(raw.get("tab_results"))
        top_tab_results = _dict_of_dicts(raw.get("top_tab_results"))
        comic_status_raw = str(raw.get("comic_flow_status") or "not_started").strip()
        comic_status: ComicFlowStatus = (
            comic_status_raw  # type: ignore[assignment]
            if comic_status_raw
            in {"not_started", "in_progress", "passed", "failed", "blocked", "skipped"}
            else "not_started"
        )
        return cls(
            protocol_version=TANGGUO_FLOW_STATE_VERSION,
            phase=phase,
            budgets=budgets,
            contract_runtime=_mutable_runtime(raw.get("contract_runtime")),
            started_at_iso=_optional_str(raw.get("started_at_iso")),
            device_actions_used=_nonneg_int(raw.get("device_actions_used")),
            ai_calls_used=_nonneg_int(raw.get("ai_calls_used")),
            recoveries_used=_nonneg_int(raw.get("recoveries_used")),
            retries_used=_nonneg_int(raw.get("retries_used")),
            video_attempts=_nonneg_int(raw.get("video_attempts")),
            back_home_attempts=_nonneg_int(raw.get("back_home_attempts")),
            profile_scrolls=_nonneg_int(raw.get("profile_scrolls")),
            tab_device_actions_used=_nonneg_int(raw.get("tab_device_actions_used")),
            top_tab_device_actions_used=_nonneg_int(
                raw.get("top_tab_device_actions_used")
            ),
            top_tab_scrolls=_nonneg_int(raw.get("top_tab_scrolls")),
            comic_device_actions_used=_nonneg_int(raw.get("comic_device_actions_used")),
            comic_attempts=_nonneg_int(raw.get("comic_attempts")),
            comic_swipes_done=_nonneg_int(raw.get("comic_swipes_done")),
            comic_swipes_effective=_nonneg_int(raw.get("comic_swipes_effective")),
            comic_swipes_verified=_nonneg_int(raw.get("comic_swipes_verified")),
            comic_scroll_source_capture=_optional_str(raw.get("comic_scroll_source_capture")),
            comic_scroll_source_fingerprint=_optional_str(raw.get("comic_scroll_source_fingerprint")),
            comic_back_home_attempts=_nonneg_int(raw.get("comic_back_home_attempts")),
            payment_device_actions_used=_nonneg_int(
                raw.get("payment_device_actions_used")
            ),
            payment_ai_calls_used=_nonneg_int(raw.get("payment_ai_calls_used")),
            attempted_video_labels=_str_list(raw.get("attempted_video_labels")),
            video_target=_optional_str(raw.get("video_target")),
            video_outcome=_optional_str(raw.get("video_outcome")),
            playback_confirmed=_optional_bool(raw.get("playback_confirmed")),
            home_default_top_tab=_optional_str(raw.get("home_default_top_tab")),
            discovered_top_tabs=_str_list(raw.get("discovered_top_tabs")),
            visited_top_tabs=_str_list(raw.get("visited_top_tabs")),
            top_tab_results=top_tab_results,
            top_tabs_status=_optional_str(raw.get("top_tabs_status")),
            discovered_bottom_tabs=_str_list(raw.get("discovered_bottom_tabs")),
            pending_bottom_tabs=_str_list(raw.get("pending_bottom_tabs")),
            visited_bottom_tabs=_str_list(raw.get("visited_bottom_tabs")),
            failed_bottom_tabs=_str_list(raw.get("failed_bottom_tabs")),
            blocked_bottom_tabs=_str_list(raw.get("blocked_bottom_tabs")),
            tab_results=tab_results,
            bottom_coverage_status=_optional_str(raw.get("bottom_coverage_status"))
            or _optional_str(raw.get("coverage_status")),
            wallet_search_done=bool(raw.get("wallet_search_done") is True),
            final_home_attempts=_nonneg_int(raw.get("final_home_attempts")),
            payment_back_attempts=_nonneg_int(raw.get("payment_back_attempts")),
            payment_navigation_target=_optional_str(raw.get("payment_navigation_target")),
            comic_flow_status=comic_status,
            comic_title=_optional_str(raw.get("comic_title")),
            comic_configured_title=_optional_str(raw.get("comic_configured_title")),
            attempted_comic_labels=_str_list(raw.get("attempted_comic_labels")),
            comic_outcome=_optional_str(raw.get("comic_outcome")),
            comic_hit_end=_optional_bool(raw.get("comic_hit_end")),
            comic_mode_mismatch=_optional_bool(raw.get("comic_mode_mismatch")),
            wallet_search_result=_optional_str(raw.get("wallet_search_result")),
            wallet_entry_label=_optional_str(raw.get("wallet_entry_label")),
            payment_farthest_stage=_optional_str(raw.get("payment_farthest_stage")),
            payment_blocked_reason=_optional_str(raw.get("payment_blocked_reason")),
            alipay_available=_optional_bool(raw.get("alipay_available")),
            alipay_selected=_optional_bool(raw.get("alipay_selected")),
            stop_reason=_optional_str(raw.get("stop_reason")),
            payment_mode=str(raw.get("payment_mode") or "inspect_only").strip()
            or "inspect_only",
            allow_pending_order_creation=bool(
                raw.get("allow_pending_order_creation") is True
            ),
            payment_method=str(raw.get("payment_method") or "alipay").strip()
            or "alipay",
            video_title=_optional_str(raw.get("video_title")),
            video_keywords=_str_list(raw.get("video_keywords")),
            wallet_keywords=_str_list(raw.get("wallet_keywords")),
            recovery_events=_str_list(raw.get("recovery_events")),
            stage_results=stage_results,
            resume_checkpoint=ResumeCheckpoint.from_dict(
                raw.get("resume_checkpoint")
                if isinstance(raw.get("resume_checkpoint"), Mapping)
                else None
            ),
        )

    def budget_exhausted(self, *, now=None) -> bool:
        if (
            self.device_actions_used >= self.budgets.device_actions_max
            or self.ai_calls_used >= self.budgets.ai_calls_max
        ):
            return True
        return self.wall_clock_exceeded(now=now)

    def wall_clock_exceeded(self, *, now=None) -> bool:
        """整体任务墙钟超时（不因恢复重置 started_at）。"""
        if not self.started_at_iso or self.budgets.wall_clock_timeout_sec <= 0:
            return False
        try:
            from datetime import datetime

            started = datetime.fromisoformat(self.started_at_iso)
            now = now or datetime.now(tz=started.tzinfo)
            return (now - started).total_seconds() >= float(
                self.budgets.wall_clock_timeout_sec
            )
        except (TypeError, ValueError, OSError):
            return False

    def ensure_started_at(self, *, now_iso: str | None = None) -> None:
        if self.started_at_iso:
            return
        if now_iso:
            self.started_at_iso = now_iso
            return
        from datetime import UTC, datetime

        self.started_at_iso = datetime.now(UTC).isoformat()

    def consume_device_action(self) -> bool:
        if self.device_actions_used >= self.budgets.device_actions_max:
            return False
        self.device_actions_used += 1
        return True

    def consume_ai_call(self) -> bool:
        if self.ai_calls_used >= self.budgets.ai_calls_max:
            return False
        self.ai_calls_used += 1
        return True

    def consume_retry(self) -> bool:
        if not self.can_retry():
            return False
        counts, key = self._retry_scope()
        counts[key] = counts.get(key, 0) + 1
        self.retries_used += 1
        return True

    def can_retry(self) -> bool:
        counts, key = self._retry_scope()
        return self.retries_used < 30 and counts.get(key, 0) < self.budgets.retries_max

    def _retry_scope(self):
        """每阶段／栏位分别重试，并保留全局 30 次硬上限。"""
        key = self.phase
        if self.phase == "bottom_tabs" and self.pending_bottom_tabs:
            key += ":" + self.pending_bottom_tabs[0]
        elif self.phase == "top_tabs":
            key += ":" + str(len(self.visited_top_tabs))
        if "retries_by_scope" not in self.contract_runtime:
            # 旧进度的重试归入当前阶段，不转嫁给后续链路。
            self.contract_runtime["retries_by_scope"] = {key: self.retries_used}
        return self.contract_runtime["retries_by_scope"], key

    def record_recovery(self, event: str) -> bool:
        if self.recoveries_used >= self.budgets.recoveries_max:
            return False
        self.recoveries_used += 1
        self.recovery_events.append(str(event))
        return True

    def mark_stage(self, stage: str, result: StageResult) -> None:
        prev = self.stage_results.get(stage)
        if prev in {"passed", "blocked"} and result == "skipped":
            return
        self.stage_results[stage] = result

    def advance(self, phase: PhaseId) -> None:
        self.phase = phase

    def comic_already_terminal(self) -> bool:
        return self.comic_flow_status in {
            "passed",
            "failed",
            "blocked",
            "skipped",
        }

    def sync_pending_bottom_tabs(self) -> None:
        """依完整清单刷新未完成底栏（不含失败／已访）。"""
        done = set(self.visited_bottom_tabs) | set(self.failed_bottom_tabs) | set(
            self.blocked_bottom_tabs
        )
        self.pending_bottom_tabs = [
            t for t in self.discovered_bottom_tabs if t not in done
        ]

    def ensure_bottom_tab_budget(self) -> None:
        """按实际分頁数抬升底栏动作上限（不低于配置基数）。"""
        n = max(len(self.discovered_bottom_tabs), 1)
        needed = n * max(self.budgets.tab_actions_per_tab, 1) + 8
        if self.budgets.tab_device_actions_max < needed:
            self.budgets.tab_device_actions_max = needed

    def compute_bottom_coverage(self) -> str:
        """complete=清单内皆已点击验证；incomplete=尚有未点。"""
        if not self.discovered_bottom_tabs:
            self.bottom_coverage_status = "incomplete"
            return "incomplete"
        clicked = {
            label
            for label, result in self.tab_results.items()
            if isinstance(result, Mapping) and result.get("clicked") is True
        }
        if all(label in clicked for label in self.discovered_bottom_tabs):
            self.bottom_coverage_status = "complete"
            return "complete"
        self.bottom_coverage_status = "incomplete"
        return "incomplete"

    def finalize_bottom_tabs_stage(self) -> None:
        coverage = self.compute_bottom_coverage()
        has_fail = bool(self.failed_bottom_tabs or self.blocked_bottom_tabs)
        any_verify_fail = any(
            isinstance(r, Mapping) and r.get("status") == "failed"
            for r in self.tab_results.values()
        )
        if coverage == "incomplete":
            self.mark_stage("bottom_tabs", "blocked")
        elif has_fail or any_verify_fail:
            # 覆盖完整但部分验证失败：不可报 passed
            self.mark_stage("bottom_tabs", "failed")
        else:
            self.mark_stage("bottom_tabs", "passed")

    def push_comic_checkpoint(self, *, note: str | None = None) -> None:
        if self.phase in _COMIC_PHASES:
            return
        remaining_top = [
            t
            for t in self.discovered_top_tabs
            if t not in self.visited_top_tabs and t != self.home_default_top_tab
        ]
        self.sync_pending_bottom_tabs()
        self.resume_checkpoint = ResumeCheckpoint(
            phase=self.phase,
            remaining_top_tabs=remaining_top,
            remaining_bottom_tabs=list(self.pending_bottom_tabs),
            note=note,
        )

    def push_payment_checkpoint(self, *, note: str | None = None) -> None:
        """进入支付前保存底栏接续位置。"""
        self.sync_pending_bottom_tabs()
        self.resume_checkpoint = ResumeCheckpoint(
            phase="bottom_tabs",
            remaining_top_tabs=[],
            remaining_bottom_tabs=list(self.pending_bottom_tabs),
            note=note or "after_payment",
        )

    def resume_after_comic(self) -> None:
        """漫画结束后恢复主流程阶段；终态 comic 不因回首页重置。"""
        ck = self.resume_checkpoint
        self.resume_checkpoint = None
        if ck is None:
            if self.top_tabs_status in {None, "pending", "in_progress"}:
                self.advance("top_tabs")
            elif self.stage_results.get("top_tab_restore") not in {
                "passed",
                "skipped",
                "failed",
            }:
                self.advance("top_tab_restore")
            else:
                self.advance("bottom_tabs")
            return
        if ck.remaining_bottom_tabs:
            self.pending_bottom_tabs = list(ck.remaining_bottom_tabs)
        # 返回首页后原栏目已不在前台；未完成验证的点击必须重新执行。
        for label in ck.remaining_bottom_tabs:
            if label not in self.visited_bottom_tabs:
                self.tab_results.pop(label, None)
        for label in ck.remaining_top_tabs:
            if label not in self.visited_top_tabs:
                self.top_tab_results.pop(label, None)
        target = ck.phase
        if target in PHASE_ORDER and target not in _COMIC_PHASES:
            self.advance(target)  # type: ignore[arg-type]
        else:
            self.advance("bottom_tabs")

    def resume_after_payment(self) -> None:
        """先返回首页，验证成功后才恢复底栏。"""
        self.advance("payment_back_home")

    def complete_payment_return(self) -> None:
        """支付结束后继续未完成底栏；全部完成则进 final_home。"""
        ck = self.resume_checkpoint
        self.resume_checkpoint = None
        if ck is not None and ck.remaining_bottom_tabs:
            self.pending_bottom_tabs = list(ck.remaining_bottom_tabs)
        self.sync_pending_bottom_tabs()
        if self.pending_bottom_tabs:
            self.advance("bottom_tabs")
        else:
            self.finalize_bottom_tabs_stage()
            self.advance("final_home")


def load_tangguo_flow_config(package_name: str | None) -> dict[str, Any]:
    from services.api.app.application.app_ui_tree_features import load_app_ui_tree_features

    features = load_app_ui_tree_features(package_name=package_name)
    flow = features.get("tangguo_flow")
    return dict(flow) if isinstance(flow, Mapping) else {}


def apply_flow_config(state: TangguoFlowState, config: Mapping[str, object]) -> None:
    """将 features／profile 配置合并进状态（不重置已用预算）。"""
    budgets_raw = config.get("budgets")
    if isinstance(budgets_raw, Mapping):
        if (
            state.device_actions_used == 0
            and state.ai_calls_used == 0
            and state.phase == "home_confirm"
            and not state.stage_results
        ):
            state.budgets = TangguoBudgets.from_mapping(budgets_raw)
    pm = config.get("payment_mode")
    if isinstance(pm, str) and pm.strip():
        state.payment_mode = pm.strip()
    apo = config.get("allow_pending_order_creation")
    if isinstance(apo, bool):
        state.allow_pending_order_creation = apo
    pmethod = config.get("payment_method")
    if isinstance(pmethod, str) and pmethod.strip():
        state.payment_method = pmethod.strip().casefold()
    vt = config.get("video_title")
    if isinstance(vt, str):
        state.video_title = vt.strip() or None
    vk = config.get("video_keywords")
    if isinstance(vk, Sequence) and not isinstance(vk, (str, bytes)):
        state.video_keywords = [str(x).strip() for x in vk if str(x or "").strip()]
    wk = config.get("wallet_keywords")
    if isinstance(wk, Sequence) and not isinstance(wk, (str, bytes)):
        state.wallet_keywords = [str(x).strip() for x in wk if str(x or "").strip()]
    ct = config.get("comic_title")
    if isinstance(ct, str):
        state.comic_configured_title = ct.strip() or None
    # 预算上限仅在全新开局套用；运行中／测试注入的 budgets 不得被配置覆盖。


def merge_flow_state_from_intent_parameters(
    *,
    prior_metadata: Mapping[str, object],
    parameters: Mapping[str, object] | None,
) -> dict[str, object]:
    """动作完成后写回主流程状态（整体预算不因恢复而重置）。"""

    state = TangguoFlowState.from_dict(
        prior_metadata.get("tangguo_flow_state")
        if isinstance(prior_metadata.get("tangguo_flow_state"), Mapping)
        else None
    )
    if isinstance(parameters, Mapping):
        incoming = parameters.get("tangguo_flow_state")
        if isinstance(incoming, Mapping):
            state = TangguoFlowState.from_dict(incoming)
        elif (
            parameters.get("tangguo_flow_device_action") is True
            or parameters.get("bounded_dismiss_tool") is True
        ):
            state.device_actions_used += 1
        if parameters.get("tangguo_flow_ai_call") is True and not isinstance(
            incoming, Mapping
        ):
            state.ai_calls_used += 1
    report = state.as_report()
    return {
        "tangguo_flow_state": state.to_dict(),
        "tangguo_flow_report": report,
    }


def package_uses_tangguo_flow(*, package_name: str | None) -> bool:
    """糖果课纲＝通用 Agent 三合同主流程（非单包专属）。

    - features／profile ``tangguo_flow.enabled=false`` → 关闭
    - ``enabled=true`` 或糖果包名 → 开启
    - 其余：``uses_agent_generic_contract``（无专属编排／agent_profile_only）→ 开启
    """

    package = str(package_name or "").strip()
    if not package:
        return False
    config = load_tangguo_flow_config(package_name)
    if config.get("enabled") is False:
        return False
    if config.get("enabled") is True:
        return True
    if package.casefold() == TANGGUO_PACKAGE.casefold():
        return True
    from services.api.app.application.app_product_orchestration_policy import (
        uses_agent_generic_contract,
    )

    return uses_agent_generic_contract(package)


def package_uses_agent_app_flow(*, package_name: str | None) -> bool:
    """别名：有状态主流程＋无状态辨识（Agent 三合同路径）。"""

    return package_uses_tangguo_flow(package_name=package_name)


def _nonneg_int(value: object) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    if value is True:
        return True
    if value is False:
        return False
    return None


def _str_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _dict_of_dicts(value: object) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(value, Mapping):
        return result
    for key, item in value.items():
        if isinstance(item, Mapping):
            result[str(key)] = dict(item)
    return result


def _mutable_runtime(raw):
    """冻结观察帧的嵌套计数必须解冻后才能推进，不能浅拷贝 mappingproxy。"""
    from services.api.app.domain.protocol import thaw_json_mapping

    return thaw_json_mapping(raw) if isinstance(raw, Mapping) else {}
