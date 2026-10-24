"""可信 Capture 驱动的 Web 通用 frontier 调度。"""

from __future__ import annotations

import heapq
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_runtime_discovery import (
    CanonicalSiteScope,
    TrustedWebCaptureAuthority,
    WebCapture,
    classify_runtime_href,
)
from services.api.app.domain.exploration.web_frontier_policy import WebFrontierPolicy
from services.api.app.domain.page.web_origin import canonical_web_origin
from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeAnchor:
    """稳定 DOM Capture 中的一条真实 href 观察。"""

    href: str
    dom_role: str = "menu"
    structure_fingerprint: str = ""
    priority: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class WebFrontierCandidate:
    """规范 URL 与来源谱系分离的可调度候选。"""

    canonical_url: str
    route_shape: str
    dom_role: str
    structure_fingerprint: str
    structure_cluster_key: str
    priority: int
    source_capture_id: str
    source_page_url: str
    source_origin: str
    raw_href: str
    depth: int
    budget_reason: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WebFrontierIngestResult:
    """一次 Capture 发现的新增、拒绝与预算终态。"""

    inserted_candidates: tuple[WebFrontierCandidate, ...]
    actionable_candidates: tuple[WebFrontierCandidate, ...]
    budget_not_executed: tuple[WebFrontierCandidate, ...]
    rejected_count: int
    alias_count: int
    frontier_urls: tuple[str, ...]
    logical_urls: frozenset[str]

    def logical_candidates_for(self, url: str) -> int:
        try:
            canonical = _canonical_frontier_url(url)
        except ValueError:
            return 0
        return int(canonical in self.logical_urls)


@dataclass(frozen=True, slots=True, kw_only=True)
class WebFrontierExecutionSummary:
    executed_unique_menu_count: int
    menu_limit_skipped_count: int
    detail_candidate_count: int
    page_budget_skipped_count: int
    executed_page_count: int


class WebFrontierScheduler:
    """消费冻结策略并逐项领取同源 frontier。"""

    def __init__(
        self,
        *,
        policy: WebFrontierPolicy,
        capture_authority: TrustedWebCaptureAuthority,
        menu_limit: int,
        anchor_reader: Callable[[WebCapture], Sequence[RuntimeAnchor]] | None = None,
        clock: Callable[[], float] | None = None,
        started_at: float | None = None,
    ) -> None:
        if type(menu_limit) is not int or menu_limit < 1:
            raise ValueError("menu_limit 必须为正整数")
        self._policy = policy
        self._capture_authority = capture_authority
        self._menu_limit = menu_limit
        self.anchor_reader = anchor_reader or (lambda capture: capture.anchors)
        self._clock = clock or monotonic
        self._started_at = self._clock() if started_at is None else started_at
        self._capture_results: dict[str, WebFrontierIngestResult] = {}
        self._candidates: dict[str, WebFrontierCandidate] = {}
        self._aliases: dict[str, str] = {}
        self._source_baselines: set[str] = set()
        self._lineage: dict[str, list[str]] = defaultdict(list)
        self._states: dict[str, str] = {}
        self._structure_counts: Counter[str] = Counter()
        self._query_variants: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._queue: list[tuple[int, int, str]] = []
        self._sequence = 0
        self._inflight_by_origin: Counter[str] = Counter()
        self._executed_pages = 0
        self._executed_menu_urls: set[str] = set()
        self._menu_limit_skipped_urls: set[str] = set()
        self._page_budget_skipped_urls: set[str] = set()

    @classmethod
    def from_frozen_policy(
        cls,
        frozen_policy,
        *,
        capture_authority: TrustedWebCaptureAuthority,
        anchor_reader: Callable[[WebCapture], Sequence[RuntimeAnchor]] | None = None,
        clock: Callable[[], float] | None = None,
        started_at: float | None = None,
    ) -> WebFrontierScheduler:
        """只从当前 Run 冻结正文装配执行预算。"""
        return cls(
            policy=frozen_policy.frontier_policy,
            capture_authority=capture_authority,
            menu_limit=frozen_policy.menu_limit,
            anchor_reader=anchor_reader,
            clock=clock,
            started_at=started_at,
        )

    def ingest(
        self,
        source_capture: WebCapture,
        anchors: Sequence[RuntimeAnchor],
    ) -> WebFrontierIngestResult:
        self._capture_authority.assert_discoverable(source_capture)
        cached = self._capture_results.get(source_capture.capture_state_fingerprint)
        if cached is not None:
            return cached
        self._register_capture_aliases(source_capture)
        self._source_baselines.add(
            self._resolve_alias(_canonical_frontier_url(source_capture.final_url))
        )
        inserted: list[WebFrontierCandidate] = []
        actionable: list[WebFrontierCandidate] = []
        budget_not_executed: list[WebFrontierCandidate] = []
        rejected_count = 0
        alias_count = 0
        ranked_anchors = sorted(
            enumerate(anchors),
            key=lambda item: (-item[1].priority, item[0]),
        )
        for _position, observed in ranked_anchors:
            candidate = self._normalize(source_capture, observed)
            if candidate is None:
                rejected_count += 1
                continue
            if candidate.canonical_url in self._source_baselines:
                alias_count += 1
                self._append_lineage(candidate.canonical_url, source_capture.capture_id)
                continue
            existing = self._candidates.get(candidate.canonical_url)
            if existing is not None:
                alias_count += 1
                self._append_lineage(candidate.canonical_url, source_capture.capture_id)
                continue
            budget_reason = self._budget_reason(candidate)
            if budget_reason is not None:
                candidate = _with_budget_reason(candidate, budget_reason)
                budget_not_executed.append(candidate)
                self._states[candidate.canonical_url] = "budget_not_executed"
            else:
                actionable.append(candidate)
                self._states[candidate.canonical_url] = "pending"
                self._sequence += 1
                heapq.heappush(
                    self._queue,
                    (-candidate.priority, self._sequence, candidate.canonical_url),
                )
                self._structure_counts[candidate.structure_cluster_key] += 1
                self._remember_query_variant(candidate.canonical_url)
            self._candidates[candidate.canonical_url] = candidate
            self._append_lineage(candidate.canonical_url, source_capture.capture_id)
            inserted.append(candidate)
        result = WebFrontierIngestResult(
            inserted_candidates=tuple(inserted),
            actionable_candidates=tuple(actionable),
            budget_not_executed=tuple(budget_not_executed),
            rejected_count=rejected_count,
            alias_count=alias_count,
            frontier_urls=self._pending_urls(),
            logical_urls=frozenset((*self._candidates, *self._aliases)),
        )
        self._capture_results[source_capture.capture_state_fingerprint] = result
        return result

    async def discover_once(self, source_capture: WebCapture) -> WebFrontierIngestResult:
        """先验签 Capture，再读取 DOM anchors；相同状态直接复用。"""
        self._capture_authority.assert_discoverable(source_capture)
        cached = self._capture_results.get(source_capture.capture_state_fingerprint)
        if cached is not None:
            return cached
        return self.ingest(source_capture, tuple(self.anchor_reader(source_capture)))

    def assert_discoverable(self, source_capture: WebCapture) -> None:
        """在读取任何 DOM 证据前暴露同一份 Capture authority 门禁。"""
        self._capture_authority.assert_discoverable(source_capture)

    async def claim_next(self) -> WebFrontierCandidate | None:
        """在全部冻结执行约束下领取一个候选。"""
        if self._duration_exhausted():
            self._terminate_pending_for_page_budget("WEB_FRONTIER_DURATION_EXHAUSTED")
            return None
        if self._executed_pages >= self._policy.max_pages:
            self._terminate_pending_for_page_budget("WEB_FRONTIER_PAGE_BUDGET_EXHAUSTED")
            return None
        deferred: list[tuple[int, int, str]] = []
        selected: WebFrontierCandidate | None = None
        while self._queue:
            queue_item = heapq.heappop(self._queue)
            candidate_url = self._resolve_alias(queue_item[2])
            candidate = self._candidates.get(candidate_url)
            if candidate is None:
                continue
            if self._states[candidate.canonical_url] != "pending":
                continue
            if candidate.dom_role == "menu" and (len(self._executed_menu_urls) >= self._menu_limit):
                self._states[candidate.canonical_url] = "budget_not_executed"
                self._menu_limit_skipped_urls.add(candidate.canonical_url)
                continue
            if (
                self._inflight_by_origin[candidate.source_origin]
                >= self._policy.max_concurrency_per_origin
            ):
                deferred.append(queue_item)
                continue
            selected = candidate
            self._states[candidate.canonical_url] = "inflight"
            self._inflight_by_origin[candidate.source_origin] += 1
            self._executed_pages += 1
            if candidate.dom_role == "menu":
                self._executed_menu_urls.add(candidate.canonical_url)
            break
        for item in deferred:
            heapq.heappush(self._queue, item)
        return selected

    async def mark_visited(self, url: str) -> None:
        canonical = self._resolve_alias(_canonical_frontier_url(url))
        candidate = self._candidates.get(canonical)
        if candidate is None:
            return
        state = self._states.get(canonical)
        if state == "inflight":
            self._inflight_by_origin[candidate.source_origin] -= 1
        elif state == "pending":
            if candidate.dom_role == "menu" and (len(self._executed_menu_urls) >= self._menu_limit):
                self._states[canonical] = "budget_not_executed"
                self._menu_limit_skipped_urls.add(canonical)
                return
            if self._executed_pages >= self._policy.max_pages:
                self._states[canonical] = "budget_not_executed"
                self._page_budget_skipped_urls.add(canonical)
                return
            self._executed_pages += 1
            if candidate.dom_role == "menu":
                self._executed_menu_urls.add(canonical)
        self._states[canonical] = "visited"

    def source_lineage_for(self, url: str) -> tuple[str, ...]:
        canonical = self._resolve_alias(_canonical_frontier_url(url))
        return tuple(self._lineage.get(canonical, ()))

    @property
    def execution_summary(self) -> WebFrontierExecutionSummary:
        return WebFrontierExecutionSummary(
            executed_unique_menu_count=len(self._executed_menu_urls),
            menu_limit_skipped_count=len(self._menu_limit_skipped_urls),
            detail_candidate_count=sum(
                candidate.dom_role == "detail" for candidate in self._candidates.values()
            ),
            page_budget_skipped_count=len(self._page_budget_skipped_urls),
            executed_page_count=self._executed_pages,
        )

    @property
    def canonical_site_origin(self) -> str:
        return self._capture_authority.site_scope.canonical_site_origin

    def _normalize(
        self,
        source_capture: WebCapture,
        observed: RuntimeAnchor,
    ) -> WebFrontierCandidate | None:
        raw_href = str(observed.href).strip()
        if not raw_href or raw_href.startswith("#"):
            return None
        classification = classify_runtime_href(source_capture.final_url, raw_href)
        if classification.code != "actionable" or classification.resolved_url is None:
            return None
        if _unsafe_navigation_target(classification.resolved_url):
            return None
        try:
            canonical_url = self._resolve_alias(
                _canonical_frontier_url(classification.resolved_url)
            )
            source_origin = canonical_web_origin(source_capture.final_url)
        except ValueError:
            return None
        if source_origin != self._capture_authority.site_scope.canonical_site_origin:
            return None
        route_shape = _route_shape(canonical_url)
        structure = (
            observed.structure_fingerprint.strip()
            or sha256(f"{observed.dom_role}:{route_shape}".encode()).hexdigest()
        )
        structure_cluster_key = web_frontier_structure_cluster_key(
            route_shape=route_shape,
            dom_role=observed.dom_role,
            structure_fingerprint=structure,
        )
        return WebFrontierCandidate(
            canonical_url=canonical_url,
            route_shape=route_shape,
            dom_role=observed.dom_role,
            structure_fingerprint=structure,
            structure_cluster_key=structure_cluster_key,
            priority=observed.priority,
            source_capture_id=source_capture.capture_id,
            source_page_url=source_capture.final_url,
            source_origin=source_origin,
            raw_href=raw_href,
            depth=source_capture.depth + 1,
        )

    def _budget_reason(self, candidate: WebFrontierCandidate) -> str | None:
        if self._duration_exhausted():
            return "WEB_FRONTIER_DURATION_EXHAUSTED"
        if candidate.depth > self._policy.max_depth:
            return "WEB_FRONTIER_DEPTH_EXHAUSTED"
        if (
            self._structure_counts[candidate.structure_cluster_key]
            >= self._policy.max_samples_per_structure
        ):
            return "WEB_FRONTIER_STRUCTURE_BUDGET_EXHAUSTED"
        parsed = urlsplit(candidate.canonical_url)
        variants = self._query_variants[(candidate.source_origin, parsed.path)]
        if (
            parsed.query
            and parsed.query not in variants
            and len(variants) >= self._policy.max_query_variants_per_path
        ):
            return "WEB_FRONTIER_QUERY_BUDGET_EXHAUSTED"
        return None

    def _remember_query_variant(self, canonical_url: str) -> None:
        parsed = urlsplit(canonical_url)
        if parsed.query:
            self._query_variants[(canonical_web_origin(canonical_url), parsed.path)].add(
                parsed.query
            )

    def _append_lineage(self, canonical_url: str, capture_id: str) -> None:
        lineage = self._lineage[self._resolve_alias(canonical_url)]
        if capture_id not in lineage:
            lineage.append(capture_id)

    def _register_capture_aliases(self, capture: WebCapture) -> None:
        observed_urls = (capture.requested_url, *capture.redirect_chain, capture.final_url)
        canonical_urls: list[str] = []
        for url in observed_urls:
            try:
                canonical = _canonical_frontier_url(url)
            except ValueError:
                continue
            if canonical not in canonical_urls:
                canonical_urls.append(canonical)
        if not canonical_urls:
            return
        existing = [
            self._resolve_alias(url)
            for url in canonical_urls
            if self._resolve_alias(url) in self._candidates
        ]
        representative = canonical_urls[-1]
        for preferred_state in ("inflight", "visited", "pending", "budget_not_executed"):
            matched = next(
                (url for url in existing if self._states.get(url) == preferred_state),
                None,
            )
            if matched is not None:
                representative = matched
                break
        for url in canonical_urls:
            previous = self._resolve_alias(url)
            if previous != representative and previous in self._candidates:
                self._merge_candidate(previous, representative)
            self._aliases[url] = representative
        self._aliases[representative] = representative
        self._append_lineage(representative, capture.capture_id)

    def _merge_candidate(self, source: str, target: str) -> None:
        """将重定向两端归并为一个逻辑候选，并保留执行状态与谱系。"""
        if source == target:
            return
        source_candidate = self._candidates[source]
        if target not in self._candidates:
            self._candidates[target] = self._candidates[source]
        target_candidate = self._candidates[target]
        source_state = self._states.pop(source, None)
        target_state = self._states.get(target)
        states = {state for state in (source_state, target_state) if state is not None}
        final_state = next(
            (
                state
                for state in ("visited", "inflight", "pending", "budget_not_executed")
                if state in states
            ),
            None,
        )
        initial_inflight = tuple(
            candidate
            for candidate, state in (
                (source_candidate, source_state),
                (target_candidate, target_state),
            )
            if state == "inflight"
        )
        for candidate in initial_inflight:
            self._inflight_by_origin[candidate.source_origin] -= 1
        if final_state == "inflight":
            self._inflight_by_origin[target_candidate.source_origin] += 1
        initial_executions = sum(
            state in {"inflight", "visited"} for state in (source_state, target_state)
        )
        final_executions = int(final_state in {"inflight", "visited"})
        self._executed_pages -= max(0, initial_executions - final_executions)
        source_menu_executed = source in self._executed_menu_urls
        target_menu_executed = target in self._executed_menu_urls
        self._executed_menu_urls.discard(source)
        self._executed_menu_urls.discard(target)
        if (source_menu_executed or target_menu_executed) and final_executions:
            self._executed_menu_urls.add(target)
        menu_skipped = bool(
            source in self._menu_limit_skipped_urls or target in self._menu_limit_skipped_urls
        )
        page_skipped = bool(
            source in self._page_budget_skipped_urls or target in self._page_budget_skipped_urls
        )
        self._menu_limit_skipped_urls.difference_update((source, target))
        self._page_budget_skipped_urls.difference_update((source, target))
        if final_state == "budget_not_executed":
            if menu_skipped:
                self._menu_limit_skipped_urls.add(target)
            if page_skipped:
                self._page_budget_skipped_urls.add(target)
        if final_state is not None:
            self._states[target] = final_state
        for capture_id in self._lineage.pop(source, ()):
            self._append_lineage(target, capture_id)
        self._candidates.pop(source, None)
        if source_state in {"pending", "inflight", "visited"}:
            self._structure_counts[source_candidate.structure_cluster_key] -= 1
        if target_state not in {"pending", "inflight", "visited"} and final_state in {
            "pending",
            "inflight",
            "visited",
        }:
            self._structure_counts[target_candidate.structure_cluster_key] += 1
        self._aliases[source] = target

    def _resolve_alias(self, url: str) -> str:
        current = url
        seen: set[str] = set()
        while current in self._aliases and current not in seen:
            seen.add(current)
            successor = self._aliases[current]
            if successor == current:
                break
            current = successor
        return current

    def _duration_exhausted(self) -> bool:
        return self._clock() - self._started_at >= self._policy.max_duration_seconds

    def _pending_urls(self) -> tuple[str, ...]:
        return tuple(url for url, state in self._states.items() if state == "pending")

    def ranked_actionable_candidates(self) -> tuple[WebFrontierCandidate, ...]:
        """按冻结优先级输出仍可进入持久二次门禁的候选。"""
        return tuple(
            sorted(
                (
                    candidate
                    for url, candidate in self._candidates.items()
                    if self._states.get(url) == "pending"
                ),
                key=lambda candidate: (-candidate.priority, candidate.canonical_url),
            )
        )

    def _terminate_pending_for_page_budget(self, _reason: str) -> None:
        for url, state in tuple(self._states.items()):
            if state == "pending":
                self._states[url] = "budget_not_executed"
                self._page_budget_skipped_urls.add(url)
        self._queue.clear()


class TrustedWebFrontierCandidateSource:
    """将既有 DOM 候选源收口到可信 Capture 与冻结 frontier 门禁。"""

    def __init__(
        self,
        *,
        delegate: object,
        capture_metadata_reader: object,
        ready_capture_reader: object,
        frozen_policy: object,
        target_url: str,
        session_id: str,
    ) -> None:
        self._delegate = delegate
        self._capture_metadata_reader = capture_metadata_reader
        self._ready_capture_reader = ready_capture_reader
        self._frozen_policy = frozen_policy
        self._target_url = target_url
        self._session_id = session_id
        self._bound_capture_ids: dict[str, str] = {}
        self._bound_page_roles: dict[str, tuple[str, str]] = {}
        self._scheduler: WebFrontierScheduler | None = None
        self._originals_by_url: dict[str, WebNavigationCandidate] = {}
        self._projected_by_url: dict[str, WebNavigationCandidate] = {}
        self._active_capture: WebCapture | None = None
        self._active_page_role: str | None = None

    def bind_latest_navigation_capture(self, session_id: str, capture_id: str) -> None:
        binder = getattr(self._delegate, "bind_latest_navigation_capture", None)
        if callable(binder):
            binder(session_id, capture_id)
        self._bound_capture_ids[session_id] = capture_id

    def bind_navigation_page_role(
        self, session_id: str, capture_id: str, page_role: str
    ) -> None:
        if page_role not in {"home", "menu", "detail"}:
            raise ValueError("WEB_DISCOVERY_PAGE_ROLE_INVALID")
        if self._bound_capture_ids.get(session_id) != capture_id:
            raise ValueError("WEB_DISCOVERY_SOURCE_NOT_CURRENT")
        self._bound_page_roles[session_id] = (capture_id, page_role)

    async def navigation_candidates(
        self,
        session_id: str,
    ) -> tuple[WebNavigationCandidate, ...]:
        if session_id != self._session_id:
            raise ValueError("WEB_DISCOVERY_SOURCE_SESSION_UNTRUSTED")
        capture_id = self._bound_capture_ids.get(session_id)
        if not capture_id:
            return ()
        metadata = await self._capture_metadata_reader.read(
            capture_id=capture_id,
            depth=0,
        )
        requested_url = metadata.requested_url
        depth = self._depth_for_requested_url(requested_url)
        source_capture = replace(metadata, depth=depth)
        if self._scheduler is None:
            scope = CanonicalSiteScope.bind(
                self._target_url,
                source_capture.redirect_chain or (source_capture.final_url,),
            )
            authority = TrustedWebCaptureAuthority(
                site_scope=scope,
                session_id=session_id,
                finalization_epoch=source_capture.finalization_epoch,
                root_capture_id=source_capture.capture_id,
            )
            menu_limit = getattr(self._frozen_policy, "menu_limit", None)
            self._scheduler = WebFrontierScheduler(
                policy=self._frozen_policy.frontier_policy,
                capture_authority=authority,
                menu_limit=menu_limit,
                anchor_reader=self._read_anchors,
            )
        self._scheduler.assert_discoverable(source_capture)
        bound_role = self._bound_page_roles.get(session_id)
        page_role = (
            bound_role[1]
            if bound_role is not None and bound_role[0] == capture_id
            else "home"
            if _canonical_frontier_url(source_capture.final_url)
            == _canonical_frontier_url(self._target_url)
            else "menu"
        )
        ready = await self._ready_capture_reader.read(
            capture_id=capture_id,
            page_role=page_role,
        )
        if str(getattr(ready, "capture_id", "")) != capture_id:
            raise ValueError("WEB_DISCOVERY_SOURCE_NOT_CURRENT")
        if str(getattr(ready, "page_role", "")) != page_role:
            raise ValueError("WEB_DISCOVERY_PAGE_ROLE_INVALID")
        self._active_capture = source_capture
        self._active_page_role = page_role
        await self._scheduler.discover_once(source_capture)
        for candidate in self._scheduler.ranked_actionable_candidates():
            original = self._originals_by_url.get(candidate.canonical_url)
            if original is None:
                continue
            self._projected_by_url[candidate.canonical_url] = replace(
                original,
                url=candidate.canonical_url,
                source_capture_id=candidate.source_capture_id,
                source_page_url=candidate.source_page_url,
                frontier_route_shape=candidate.route_shape,
                frontier_dom_role=candidate.dom_role,
                frontier_structure_fingerprint=candidate.structure_fingerprint,
                frontier_structure_cluster_key=candidate.structure_cluster_key,
                frontier_site_origin=self._scheduler.canonical_site_origin,
            )
        return tuple(
            self._projected_by_url[item.canonical_url]
            for item in self._scheduler.ranked_actionable_candidates()
            if item.canonical_url in self._projected_by_url
        )

    def _read_anchors(self, capture: WebCapture) -> tuple[RuntimeAnchor, ...]:
        if capture is not self._active_capture:
            raise ValueError("WEB_DISCOVERY_SOURCE_NOT_CURRENT")
        reader = getattr(self._delegate, "authorized_navigation_candidates", None)
        if callable(reader):
            observed_candidates = tuple(
                reader(
                    capture.session_id,
                    capture_id=capture.capture_id,
                    page_role=self._active_page_role,
                )
            )
        else:
            reader = getattr(self._delegate, "navigation_candidates", None)
            observed_candidates = tuple(reader(capture.session_id)) if callable(reader) else ()
        if not callable(reader):
            return ()
        anchors: list[RuntimeAnchor] = []
        for position, candidate in enumerate(observed_candidates):
            href = str(candidate.url or candidate.target_hint or "")
            structure = sha256(
                "\x1f".join(
                    (
                        candidate.source_kind,
                        candidate.source_signal,
                        candidate.source_selector,
                        ",".join(candidate.navigation_evidence),
                    )
                ).encode("utf-8")
            ).hexdigest()
            observed = RuntimeAnchor(
                href=href,
                dom_role="menu",
                structure_fingerprint=structure,
                # menu_position 是同目标重复出现次数，不是导航 DOM 顺序。
                priority=max(0, 10_000 - position),
            )
            anchors.append(observed)
            try:
                canonical = _canonical_frontier_url(candidate.url or "")
            except ValueError:
                continue
            self._originals_by_url[canonical] = candidate
        return tuple(anchors)

    def _depth_for_requested_url(self, requested_url: str) -> int:
        try:
            canonical = _canonical_frontier_url(requested_url)
        except ValueError:
            return 0
        original = self._projected_by_url.get(canonical)
        return max(0, original.menu_depth) if original is not None else 0

    def __getattr__(self, name: str):
        """父链与冷恢复能力继续复用原候选源，不产生第二套状态机。"""
        return getattr(self._delegate, name)


def _with_budget_reason(
    candidate: WebFrontierCandidate,
    reason: str,
) -> WebFrontierCandidate:
    return WebFrontierCandidate(
        canonical_url=candidate.canonical_url,
        route_shape=candidate.route_shape,
        dom_role=candidate.dom_role,
        structure_fingerprint=candidate.structure_fingerprint,
        structure_cluster_key=candidate.structure_cluster_key,
        priority=candidate.priority,
        source_capture_id=candidate.source_capture_id,
        source_page_url=candidate.source_page_url,
        source_origin=candidate.source_origin,
        raw_href=candidate.raw_href,
        depth=candidate.depth,
        budget_reason=reason,
    )


def _canonical_frontier_url(url: str) -> str:
    canonical = DEFAULT_WEB_URL_IDENTITY_POLICY.canonical(url)
    parsed = urlsplit(canonical)
    path = parsed.path
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _route_shape(url: str) -> str:
    path = urlsplit(url).path
    shaped = [
        ":id" if re.fullmatch(r"(?:\d+|[0-9a-f]{8,}|[0-9a-f-]{32,})", segment, re.I) else segment
        for segment in path.split("/")
    ]
    return "/".join(shaped) or "/"


def web_frontier_structure_cluster_key(
    *,
    route_shape: str,
    dom_role: str,
    structure_fingerprint: str,
) -> str:
    """用显式三元组生成跨内存与账本一致的结构簇身份。"""
    payload = "\x1f".join((route_shape, dom_role, structure_fingerprint))
    return "web-frontier-structure.v1:" + sha256(payload.encode("utf-8")).hexdigest()


def _unsafe_navigation_target(url: str) -> bool:
    parsed = urlsplit(url)
    text = parsed.path.casefold()
    query = parsed.query.casefold()
    return bool(
        re.search(
            r"(?:^|[/_.?&=\-])(?:login|log-in|signin|sign-in|checkout|payment|pay|"
            r"purchase|upload|submit)(?:$|[/_.?&=\-])",
            text,
        )
        or re.search(
            r"(?:^|[&=\-])(?:login|signin|checkout|payment|pay|purchase|upload|submit)"
            r"(?:$|[&=\-])",
            query,
        )
        or re.search(r"\.(?:apk|dmg|exe|msi|pkg|rar|tar|zip)$", text)
    )


__all__ = [
    "RuntimeAnchor",
    "WebFrontierCandidate",
    "WebFrontierExecutionSummary",
    "WebFrontierIngestResult",
    "WebFrontierScheduler",
    "TrustedWebFrontierCandidateSource",
    "web_frontier_structure_cluster_key",
]
