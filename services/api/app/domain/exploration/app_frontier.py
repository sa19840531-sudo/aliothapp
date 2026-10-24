"""App 任务级全局 Frontier 的版本化领域合同。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.exploration.snapshots import CandidateAction
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.domain.protocol import (
    ProtocolContractError,
    freeze_json_mapping,
    parse_aware_datetime,
    read_known_fields,
    require_mapping,
    require_text,
    stable_protocol_hash,
    thaw_json_mapping,
)

APP_FRONTIER_ITEM_PROTOCOL_VERSION = "app-frontier-item.v1"
APP_FRONTIER_FINGERPRINT_STRATEGY = "app_frontier_fingerprint_strategy"
APP_GLOBAL_NAVIGATION_FINGERPRINT_V2 = "global_navigation.v2"
APP_GLOBAL_NAVIGATION_FINGERPRINT_V3 = "global_navigation.v3"
APP_GLOBAL_NAVIGATION_FINGERPRINT_V4 = "global_navigation.v4"
APP_GLOBAL_NAVIGATION_FINGERPRINT_V5 = "global_navigation.v5"
APP_GLOBAL_NAVIGATION_FINGERPRINT_V6 = "global_navigation.v6"
APP_GLOBAL_NAVIGATION_FINGERPRINT_V7 = "global_navigation.v7"
APP_GLOBAL_NAVIGATION_FINGERPRINT_V8 = "global_navigation.v8"


class AppFrontierStatus(StrEnum):
    """任务级候选从发现到收口的持久化状态。"""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    VISITED = "visited"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"

    @property
    def is_terminal(self) -> bool:
        return self in {
            self.VISITED,
            self.BLOCKED,
            self.FAILED,
            self.CANCELLED,
            self.SUPERSEDED,
        }


@dataclass(frozen=True, kw_only=True)
class AppFrontierItem:
    """绑定源截图和页面身份的可恢复 App 候选。"""

    frontier_item_id: str
    task_id: str
    agent_run_id: str
    session_id: str
    source_work_id: str
    source_capture_id: str
    source_page_identity: str
    candidate_fingerprint: str
    candidate: CandidateAction
    surface_kind: SurfaceKind
    navigation_path: tuple[str, ...]
    priority: int
    discovered_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = APP_FRONTIER_ITEM_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "frontier_item_id",
            "task_id",
            "agent_run_id",
            "session_id",
            "source_work_id",
            "source_capture_id",
            "source_page_identity",
            "candidate_fingerprint",
        ):
            require_text(getattr(self, field_name), field_name)
        if not isinstance(self.candidate, CandidateAction):
            raise TypeError("candidate 必须为 CandidateAction")
        if self.candidate.expected_surface_kind not in {None, self.surface_kind}:
            raise ValueError("候选 Surface 与 Frontier Item 不一致")
        if self.candidate.candidate_id != self.metadata.get(
            "candidate_id",
            self.candidate.candidate_id,
        ):
            raise ValueError("Frontier Item metadata candidate_id 不一致")
        if not isinstance(self.surface_kind, SurfaceKind) or self.surface_kind is SurfaceKind.WEB:
            raise ValueError("AppFrontierItem 只接受 App Surface")
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or self.priority < 0
        ):
            raise ValueError("priority 必须为非负整数")
        if not isinstance(self.discovered_at, datetime) or self.discovered_at.utcoffset() is None:
            raise ValueError("discovered_at 必须为带时区时间")
        navigation_path = _unique_text_tuple(self.navigation_path, "navigation_path")
        try:
            metadata = freeze_json_mapping(self.metadata, "metadata")
        except ProtocolContractError as exc:
            raise ValueError(str(exc)) from exc
        if self.protocol_version != APP_FRONTIER_ITEM_PROTOCOL_VERSION:
            raise ValueError("AppFrontierItem protocol_version 不受支持")
        expected_fingerprint = build_app_frontier_candidate_fingerprint(
            source_page_identity=self.source_page_identity,
            candidate=self.candidate,
        )
        if self.candidate_fingerprint != expected_fingerprint:
            raise ValueError("candidate_fingerprint 与候选事实不一致")
        expected_id = build_app_frontier_item_id(
            agent_run_id=self.agent_run_id,
            candidate_fingerprint=self.candidate_fingerprint,
        )
        if self.frontier_item_id != expected_id:
            raise ValueError("frontier_item_id 与 Run、候选指纹不一致")
        object.__setattr__(self, "navigation_path", navigation_path)
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "frontier_item_id": self.frontier_item_id,
            "task_id": self.task_id,
            "agent_run_id": self.agent_run_id,
            "session_id": self.session_id,
            "source_work_id": self.source_work_id,
            "source_capture_id": self.source_capture_id,
            "source_page_identity": self.source_page_identity,
            "candidate_fingerprint": self.candidate_fingerprint,
            "candidate": self.candidate.to_dict(),
            "surface_kind": self.surface_kind.value,
            "navigation_path": list(self.navigation_path),
            "priority": self.priority,
            "discovered_at": self.discovered_at.isoformat(),
            "metadata": thaw_json_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppFrontierItem:
        values = read_known_fields(
            payload,
            known_fields=frozenset(
                {
                    "protocol_version",
                    "frontier_item_id",
                    "task_id",
                    "agent_run_id",
                    "session_id",
                    "source_work_id",
                    "source_capture_id",
                    "source_page_identity",
                    "candidate_fingerprint",
                    "candidate",
                    "surface_kind",
                    "navigation_path",
                    "priority",
                    "discovered_at",
                    "metadata",
                }
            ),
            required_fields=frozenset(
                {
                    "protocol_version",
                    "frontier_item_id",
                    "task_id",
                    "agent_run_id",
                    "session_id",
                    "source_work_id",
                    "source_capture_id",
                    "source_page_identity",
                    "candidate_fingerprint",
                    "candidate",
                    "surface_kind",
                    "navigation_path",
                    "priority",
                    "discovered_at",
                }
            ),
            field_name="AppFrontierItem",
        )
        return cls(
            frontier_item_id=values["frontier_item_id"],
            task_id=values["task_id"],
            agent_run_id=values["agent_run_id"],
            session_id=values["session_id"],
            source_work_id=values["source_work_id"],
            source_capture_id=values["source_capture_id"],
            source_page_identity=values["source_page_identity"],
            candidate_fingerprint=values["candidate_fingerprint"],
            candidate=CandidateAction.from_dict(require_mapping(values["candidate"], "candidate")),
            surface_kind=SurfaceKind(values["surface_kind"]),
            navigation_path=_unique_text_tuple(values["navigation_path"], "navigation_path"),
            priority=values["priority"],
            discovered_at=parse_aware_datetime(values["discovered_at"], "discovered_at"),
            metadata=require_mapping(values.get("metadata", {}), "metadata"),
            protocol_version=values["protocol_version"],
        )


@dataclass(frozen=True, kw_only=True)
class AppFrontierLease:
    """领取后携带并发写回凭证的 Frontier Item。"""

    item: AppFrontierItem
    status: AppFrontierStatus
    lease_token: str
    attempt_count: int
    row_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.item, AppFrontierItem):
            raise TypeError("item 必须为 AppFrontierItem")
        if self.status not in {AppFrontierStatus.LEASED, AppFrontierStatus.RUNNING}:
            raise ValueError("App Frontier 租约只能绑定 leased/running 状态")
        require_text(self.lease_token, "lease_token")
        if (
            isinstance(self.attempt_count, bool)
            or not isinstance(self.attempt_count, int)
            or self.attempt_count <= 0
        ):
            raise ValueError("attempt_count 必须为正整数")
        if (
            isinstance(self.row_version, bool)
            or not isinstance(self.row_version, int)
            or self.row_version < 0
        ):
            raise ValueError("row_version 必须为非负整数")


@dataclass(frozen=True, kw_only=True)
class AppFrontierCounts:
    """终态判断使用的任务级 Frontier 计数。"""

    pending: int
    leased: int
    running: int
    retry_wait: int
    visited: int
    blocked: int
    failed: int
    cancelled: int
    superseded: int

    def __post_init__(self) -> None:
        for value in self.to_dict().values():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("App Frontier 计数必须为非负整数")

    @property
    def nonterminal_count(self) -> int:
        return self.pending + self.leased + self.running + self.retry_wait

    @property
    def exhausted(self) -> bool:
        return self.nonterminal_count == 0

    def to_dict(self) -> dict[str, int]:
        return {
            "pending": self.pending,
            "leased": self.leased,
            "running": self.running,
            "retry_wait": self.retry_wait,
            "visited": self.visited,
            "blocked": self.blocked,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "superseded": self.superseded,
        }


def build_app_frontier_item(
    *,
    task_id: str,
    agent_run_id: str,
    session_id: str,
    source_work_id: str,
    source_capture_id: str,
    source_page_identity: str,
    candidate: CandidateAction,
    surface_kind: SurfaceKind,
    navigation_path: tuple[str, ...],
    priority: int,
    discovered_at: datetime,
    metadata: Mapping[str, Any] | None = None,
) -> AppFrontierItem:
    fingerprint = build_app_frontier_candidate_fingerprint(
        source_page_identity=source_page_identity,
        candidate=candidate,
    )
    return AppFrontierItem(
        frontier_item_id=build_app_frontier_item_id(
            agent_run_id=agent_run_id,
            candidate_fingerprint=fingerprint,
        ),
        task_id=task_id,
        agent_run_id=agent_run_id,
        session_id=session_id,
        source_work_id=source_work_id,
        source_capture_id=source_capture_id,
        source_page_identity=source_page_identity,
        candidate_fingerprint=fingerprint,
        candidate=candidate,
        surface_kind=surface_kind,
        navigation_path=navigation_path,
        priority=priority,
        discovered_at=discovered_at,
        metadata={"candidate_id": candidate.candidate_id, **dict(metadata or {})},
    )


def build_app_frontier_candidate_fingerprint(
    *,
    source_page_identity: str,
    candidate: CandidateAction,
) -> str:
    """同一 Run 内按页面语义、动作和目标去重，不绑定瞬时坐标或 Capture ID。"""

    require_text(source_page_identity, "source_page_identity")
    if not isinstance(candidate, CandidateAction):
        raise TypeError("candidate 必须为 CandidateAction")
    fingerprint_strategy = candidate.metadata.get(APP_FRONTIER_FINGERPRINT_STRATEGY)
    if fingerprint_strategy == APP_GLOBAL_NAVIGATION_FINGERPRINT_V8:
        scope = str(candidate.metadata.get("frontier_scope") or "page").casefold()
        payload = {
            "strategy": APP_GLOBAL_NAVIGATION_FINGERPRINT_V8,
            "scope": scope,
            "source_page_class": (
                _source_page_semantic_class(source_page_identity)
                if scope == "page"
                else None
            ),
            "action_type": candidate.action_type.value,
            "semantic_destination": _global_navigation_destination_v8(candidate),
        }
    elif fingerprint_strategy == APP_GLOBAL_NAVIGATION_FINGERPRINT_V7:
        scope = str(candidate.metadata.get("frontier_scope") or "page").casefold()
        payload = {
            "strategy": APP_GLOBAL_NAVIGATION_FINGERPRINT_V7,
            "scope": scope,
            "source_page_class": (
                _source_page_semantic_class(source_page_identity)
                if scope == "page"
                else None
            ),
            "action_type": candidate.action_type.value,
            "semantic_destination": _global_navigation_destination_v7(candidate),
        }
    elif fingerprint_strategy == APP_GLOBAL_NAVIGATION_FINGERPRINT_V6:
        scope = str(candidate.metadata.get("frontier_scope") or "page").casefold()
        payload = {
            "strategy": APP_GLOBAL_NAVIGATION_FINGERPRINT_V6,
            "scope": scope,
            "source_page_class": (
                _source_page_semantic_class(source_page_identity)
                if scope == "page"
                else None
            ),
            "action_type": candidate.action_type.value,
            "semantic_destination": _global_navigation_destination_v5(candidate),
        }
    elif fingerprint_strategy == APP_GLOBAL_NAVIGATION_FINGERPRINT_V5:
        payload = {
            "strategy": APP_GLOBAL_NAVIGATION_FINGERPRINT_V5,
            "action_type": candidate.action_type.value,
            "semantic_destination": _global_navigation_destination_v5(candidate),
        }
    elif fingerprint_strategy == APP_GLOBAL_NAVIGATION_FINGERPRINT_V4:
        payload = {
            "strategy": APP_GLOBAL_NAVIGATION_FINGERPRINT_V4,
            "action_type": candidate.action_type.value,
            "semantic_destination": _global_navigation_destination_v4(candidate),
        }
    elif fingerprint_strategy == APP_GLOBAL_NAVIGATION_FINGERPRINT_V3:
        payload = {
            "strategy": APP_GLOBAL_NAVIGATION_FINGERPRINT_V3,
            "action_type": candidate.action_type.value,
            "semantic_destination": _global_navigation_destination_v3(candidate),
        }
    elif fingerprint_strategy == APP_GLOBAL_NAVIGATION_FINGERPRINT_V2:
        # v2 已写入真实 Run，必须保留旧算法供在途 Item 反序列化和恢复。
        payload = {
            "strategy": APP_GLOBAL_NAVIGATION_FINGERPRINT_V2,
            "action_type": candidate.action_type.value,
            "semantic_destination": _global_navigation_destination_v2(candidate),
        }
    else:
        # v1 历史候选不带策略标记，继续按原算法校验和恢复，避免在途 Run 漂移。
        payload = {
            "source_page_identity": source_page_identity.casefold(),
            "action_type": candidate.action_type.value,
            "semantic_target": candidate.semantic_target.casefold(),
            "expected_state_hint": str(
                candidate.metadata.get("expected_state_hint") or ""
            ).casefold(),
        }
    return "app-frontier-fingerprint:" + stable_protocol_hash(payload)


def with_app_frontier_fingerprint_strategy(
    candidate: CandidateAction,
    *,
    strategy: str = APP_GLOBAL_NAVIGATION_FINGERPRINT_V8,
) -> CandidateAction:
    """为全局候选附加跨截图语义身份；v8 归一化通用控件类型后缀。"""

    if not isinstance(candidate, CandidateAction):
        raise TypeError("candidate 必须为 CandidateAction")
    if strategy not in {
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V2,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V3,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V4,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V5,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V6,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V7,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V8,
    }:
        raise ValueError("App Frontier 指纹策略不受支持")
    scope = candidate.metadata.get("frontier_scope")
    if scope != "app" and not (
        strategy
        in {
            APP_GLOBAL_NAVIGATION_FINGERPRINT_V6,
            APP_GLOBAL_NAVIGATION_FINGERPRINT_V7,
            APP_GLOBAL_NAVIGATION_FINGERPRINT_V8,
        }
        and scope == "page"
    ):
        return candidate
    return replace(
        candidate,
        metadata={
            **dict(candidate.metadata),
            APP_FRONTIER_FINGERPRINT_STRATEGY: strategy,
        },
    )


def _source_page_semantic_class(source_page_identity: str) -> str:
    """从稳定业务身份提取页型，避免同页滚动或同 Tab 重绘制造新分支。"""

    parts = source_page_identity.casefold().split("|")
    if len(parts) >= 3 and parts[2].strip():
        return parts[2].strip()
    return source_page_identity.casefold()


def _global_navigation_destination_v2(candidate: CandidateAction) -> str:
    """保留 v2 的 purpose 优先算法，只用于校验历史持久化 Item。"""

    purpose = _normalize_navigation_text(candidate.metadata.get("purpose"))
    if purpose and purpose not in {"打开", "进入", "切换", "查看", "导航"}:
        return purpose
    target = _normalize_navigation_text(candidate.semantic_target)
    return target or candidate.semantic_target.strip().casefold()


def _global_navigation_destination_v3(candidate: CandidateAction) -> str:
    """优先使用控件本身的目的语义，合并跨截图、跨语言的全局导航别名。"""

    target = _canonical_navigation_destination(candidate.semantic_target)
    if target:
        return target
    purpose = _canonical_navigation_destination(candidate.metadata.get("purpose"))
    return purpose or candidate.semantic_target.strip().casefold()


def _global_navigation_destination_v4(candidate: CandidateAction) -> str:
    """技术控件名优先采用自然语言用途，避免同一控件中英文双报。"""

    target = _canonical_navigation_destination(candidate.semantic_target)
    purpose = _canonical_navigation_destination(candidate.metadata.get("purpose"))
    raw_target = candidate.semantic_target.strip().casefold()
    if re.fullmatch(r"[a-z0-9_-]+", raw_target):
        normalized_target = _normalize_navigation_text(raw_target)
        if target and target != normalized_target:
            return target
        if purpose:
            return purpose
    return target or purpose or raw_target


def _global_navigation_destination_v5(candidate: CandidateAction) -> str:
    """在 v4 基础上归一化通用中文导航动词和页面容器后缀。"""

    target = _canonical_navigation_destination_v5(candidate.semantic_target)
    purpose = _canonical_navigation_destination_v5(candidate.metadata.get("purpose"))
    raw_target = candidate.semantic_target.strip().casefold()
    if re.fullmatch(r"[a-z0-9_-]+", raw_target):
        normalized_target = _normalize_navigation_text_v5(raw_target)
        if target and target != normalized_target:
            return target
        if purpose:
            return purpose
    return target or purpose or raw_target


def _global_navigation_destination_v7(candidate: CandidateAction) -> str:
    """混合中英文技术控件名优先使用用途，保留自然业务目标的区分度。"""

    target = _canonical_navigation_destination_v7(candidate.semantic_target)
    purpose = _canonical_navigation_destination_v7(candidate.metadata.get("purpose"))
    if (
        _looks_like_technical_navigation_target(candidate.semantic_target)
        and purpose
        and purpose not in _GENERIC_NAVIGATION_PURPOSES
    ):
        return purpose
    return target or purpose or candidate.semantic_target.strip().casefold()


def _global_navigation_destination_v8(candidate: CandidateAction) -> str:
    """去掉底栏、导航、横幅等控件类型后缀，保留真实业务目标。"""

    target = _canonical_navigation_destination_v8(candidate.semantic_target)
    purpose = _canonical_navigation_destination_v8(candidate.metadata.get("purpose"))
    if (
        _looks_like_technical_navigation_target(candidate.semantic_target)
        and purpose
        and purpose not in _GENERIC_NAVIGATION_PURPOSES
    ):
        return purpose
    return target or purpose or candidate.semantic_target.strip().casefold()


_GENERIC_NAVIGATION_PURPOSES = {"打开", "进入", "切换", "查看", "导航"}


def _looks_like_technical_navigation_target(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return bool(re.fullmatch(r"[a-z0-9_-]+", normalized)) or bool(
        re.search(
            r"(?:^|[\s_-])(?:banner|bottom|btn|button|fab|nav|promo|tab)(?:$|[\s_-])",
            normalized,
        )
    ) or bool(
        re.match(
            r"(?:banner|bottom|btn|button|fab|nav|promo|tab)(?=[^\x00-\x7f])",
            normalized,
        )
    )


_NAVIGATION_DESTINATION_ALIASES = {
    "home": "首页",
    "index": "首页",
    "main": "首页",
    "mine": "个人中心",
    "me": "个人中心",
    "my": "个人中心",
    "profile": "个人中心",
    "我的": "个人中心",
    "个人中心": "个人中心",
    "video": "视频",
    "videos": "视频",
    "videolist": "视频",
    "视频列表": "视频",
    "search": "搜索",
    "搜索框": "搜索",
    "welfare": "福利",
    "benefits": "福利",
    "community": "社区",
    "chigua": "吃瓜",
    "latestblackmaterial": "最新黑料",
    "latestblackmaterials": "最新黑料",
    "aitech": "AI科技",
    "aitechnology": "AI科技",
    "publishfab": "发布",
}


def _canonical_navigation_destination(value: object) -> str:
    normalized = _normalize_navigation_text(value)
    if not normalized:
        return ""
    return _NAVIGATION_DESTINATION_ALIASES.get(normalized, normalized)


def _canonical_navigation_destination_v5(value: object) -> str:
    normalized = _normalize_navigation_text_v5(value)
    if not normalized:
        return ""
    return _NAVIGATION_DESTINATION_ALIASES.get(normalized, normalized)


def _canonical_navigation_destination_v7(value: object) -> str:
    normalized = _normalize_navigation_text_v5(value)
    if not normalized:
        return ""
    if normalized == "发布内容":
        return "发布"
    return _NAVIGATION_DESTINATION_ALIASES.get(normalized, normalized)


def _canonical_navigation_destination_v8(value: object) -> str:
    normalized = _normalize_navigation_text_v8(value)
    if not normalized:
        return ""
    if normalized == "发布内容":
        return "发布"
    return _NAVIGATION_DESTINATION_ALIASES.get(normalized, normalized)


def _normalize_navigation_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = re.sub(r"[\s\-_—–·:：/\\|「」『』【】\[\]()（）]+", "", normalized)
    prefixes = (
        "bottomnavigation",
        "bottomnavbar",
        "bottomnav",
        "bottomtab",
        "navigation",
        "navbar",
        "nav",
        "tabbar",
        "tab",
        "切换至",
        "切换到",
        "打开",
        "进入",
        "前往",
        "查看",
        "当前",
        "底部",
        "底栏",
        "顶部",
    )
    suffixes = (
        "navigation",
        "navbar",
        "nav",
        "tabbar",
        "tab",
        "button",
        "btn",
        "bar",
        "list",
        "选项卡",
        "标签",
        "底栏",
        "频道",
        "分区",
        "页面",
        "入口",
        "按钮",
    )
    changed = True
    while normalized and changed:
        changed = False
        for prefix in prefixes:
            if normalized.startswith(prefix) and len(normalized) > len(prefix):
                normalized = normalized[len(prefix) :]
                changed = True
        for suffix in suffixes:
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                normalized = normalized[: -len(suffix)]
                changed = True
    return normalized


def _normalize_navigation_text_v5(value: object) -> str:
    """v5 只增加通用导航语法；v2/v3/v4 继续使用冻结的旧归一化。"""

    normalized = _normalize_navigation_text(value)
    changed = True
    while normalized and changed:
        changed = False
        for prefix in ("切换",):
            if normalized.startswith(prefix) and len(normalized) > len(prefix):
                normalized = normalized[len(prefix) :]
                changed = True
        for suffix in ("主列表", "列表", "首页", "页"):
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                normalized = normalized[: -len(suffix)]
                changed = True
    return normalized


def _normalize_navigation_text_v8(value: object) -> str:
    """v8 只裁剪通用控件类型，不能吞掉活动名、频道名等业务语义。"""

    normalized = _normalize_navigation_text_v5(value)
    changed = True
    while normalized and changed:
        changed = False
        for suffix in (
            "底部导航栏",
            "底部导航",
            "底部选项卡",
            "底部标签",
            "底部tab",
            "底部栏",
            "导航栏",
            "标签页",
            "底部",
            "导航",
            "横幅",
            "banner",
        ):
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                normalized = normalized[: -len(suffix)]
                changed = True
    return normalized


def build_app_frontier_item_id(*, agent_run_id: str, candidate_fingerprint: str) -> str:
    require_text(agent_run_id, "agent_run_id")
    require_text(candidate_fingerprint, "candidate_fingerprint")
    return "app-frontier:" + stable_protocol_hash(
        {"agent_run_id": agent_run_id, "candidate_fingerprint": candidate_fingerprint}
    )


def _unique_text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} 必须为字符串数组")
    result: list[str] = []
    for item in value:
        require_text(item, field_name)
        if item not in result:
            result.append(item)
    return tuple(result)
