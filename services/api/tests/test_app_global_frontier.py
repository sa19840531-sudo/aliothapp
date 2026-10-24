"""App 任务级 Frontier 的领域、恢复与投影回归。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.app_coverage_page_seek import APP_COVERAGE_PAGE_SEEK_VERSION
from services.api.app.application.app_frontier_restoration import (
    AppFrontierRestorationError,
    bind_app_frontier_restoration_candidate,
    build_app_frontier_restore_intent,
)
from services.api.app.application.app_global_frontier import (
    APP_FRONTIER_RESTORATION_CANDIDATES,
    AppGlobalFrontierService,
)
from services.api.app.domain.exploration import (
    APP_GLOBAL_NAVIGATION_FINGERPRINT_V4,
    APP_GLOBAL_NAVIGATION_FINGERPRINT_V5,
    APP_GLOBAL_NAVIGATION_FINGERPRINT_V8,
    AppExplorationGoal,
    AppFrontierCounts,
    AppFrontierLease,
    AppFrontierStatus,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_app_frontier_candidate_fingerprint,
    build_exploration_work,
    with_app_frontier_fingerprint_strategy,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)


class MemoryFrontierRepository:
    def __init__(self) -> None:
        self.items = {}

    async def publish_candidates(self, *, command):
        result = []
        for item in command.candidates:
            current = self.items.get(item.candidate_fingerprint)
            if current is None:
                self.items[item.candidate_fingerprint] = item
                result.append(item)
            elif current.metadata.get("status", "pending") == "pending":
                self.items[item.candidate_fingerprint] = item
                result.append(item)
        return tuple(result)

    async def counts(self, *, task_id, agent_run_id, session_id):
        del task_id, agent_run_id, session_id
        counts = {status.value: 0 for status in AppFrontierStatus}
        for item in self.items.values():
            counts[str(item.metadata.get("status", "pending"))] += 1
        return AppFrontierCounts(**counts)

    async def next_pending(self, *, task_id, agent_run_id, session_id):
        del task_id, agent_run_id, session_id
        return next(
            (
                item
                for item in self.items.values()
                if item.metadata.get("status", "pending") == "pending"
            ),
            None,
        )

    async def list_pending(self, *, task_id, agent_run_id, session_id, limit=256):
        del task_id, agent_run_id, session_id
        return tuple(
            item
            for item in self.items.values()
            if item.metadata.get("status", "pending") == "pending"
        )[:limit]


def _candidate(
    candidate_id: str,
    target: str,
    *,
    scope: str,
    action_type: InteractionOperation = InteractionOperation.CLICK,
    expected_page_type: str | None = None,
) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=target,
        action_type=action_type,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "candidate_id": candidate_id,
            "purpose": f"进入{target}",
            "expected_state_hint": target,
            "frontier_scope": scope,
            **(
                {"expected_next_page_type": expected_page_type}
                if expected_page_type is not None
                else {}
            ),
        },
    )


def _work(
    *,
    candidates: tuple[CandidateAction, ...],
    page_identity: str = "page:home",
    location: str = "app://com.example.target/.HomeActivity",
    covered_page_types: tuple[str, ...] = (),
    app_goal: AppExplorationGoal | None = None,
    semantic_action_counts: dict[str, int] | None = None,
    metadata: dict[str, object] | None = None,
):
    now = datetime.now(UTC)
    frontier = FrontierSnapshot(
        snapshot_id="frontier-home",
        observation=SurfaceObservation(
            capture_id="00000000-0000-0000-0000-000000000111",
            surface_kind=SurfaceKind.ANDROID,
            location=location,
            state_version="sha256:" + "a" * 64,
            observed_at=now,
            page_type="home",
            page_state="stable",
            metadata={
                "business_page_identity": page_identity,
                "app_navigation_path": ["page:app_ready", page_identity],
                "covered_page_types": list(covered_page_types),
                "app_semantic_action_counts": semantic_action_counts or {},
                **(metadata or {}),
            },
        ),
        candidates=candidates,
    )
    return build_exploration_work(
        task_id="00000000-0000-0000-0000-000000000001",
        session_id="app:00000000-0000-0000-0000-000000000001",
        branch_id="app:frontier-home",
        objective="探索 App 业务页面",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=frontier.observation.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_operations=(
                InteractionOperation.CLICK,
                InteractionOperation.BACK,
            ),
            allowed_hosts=("com.example.target",),
            max_navigation_depth=3,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=8,
            remaining_actions=8,
            max_depth=3,
            remaining_duration_ms=60_000,
        ),
        deadline=now + timedelta(minutes=5),
        app_goal=app_goal,
    )


@pytest.mark.asyncio
async def test_registers_all_explicit_global_siblings_and_deduplicates_reobservations() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    work = _work(
        candidates=(
            _candidate("candidate-a", "首页 Tab", scope="app"),
            _candidate("candidate-b", "消息 Tab", scope="app"),
            _candidate("candidate-c", "个人中心 Tab", scope="app"),
            _candidate("candidate-local", "关闭弹窗", scope="local"),
        )
    )

    first = await service.register_work_candidates(work=work, agent_run_id=_run_id())
    second = await service.register_work_candidates(work=work, agent_run_id=_run_id())

    assert len(first) == 3
    assert len(second) == 3
    assert {item.candidate.semantic_target for item in repository.items.values()} == {
        "首页 Tab",
        "消息 Tab",
        "个人中心 Tab",
    }
    projected = await service.project_counts(work=work, agent_run_id=_run_id())
    summary = projected.frontier.observation.metadata["app_global_frontier"]
    assert summary["pending"] == 3
    assert summary["exhausted"] is False


@pytest.mark.asyncio
async def test_global_navigation_aliases_share_one_frontier_item_across_pages() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    first = replace(
        _candidate(
            "candidate-profile-bottom",
            "底部我的标签",
            scope="app",
            expected_page_type="profile",
        ),
        metadata={
            **dict(
                _candidate(
                    "candidate-profile-bottom",
                    "底部我的标签",
                    scope="app",
                    expected_page_type="profile",
                ).metadata
            ),
            "purpose": "切换至个人中心频道",
        },
    )
    alias = replace(
        _candidate(
            "candidate-profile-alias",
            "我的底栏",
            scope="app",
            expected_page_type="content_list",
        ),
        metadata={
            **dict(
                _candidate(
                    "candidate-profile-alias",
                    "我的底栏",
                    scope="app",
                    expected_page_type="content_list",
                ).metadata
            ),
            "purpose": "进入个人中心",
        },
    )

    first_registered = await service.register_work_candidates(
        work=_work(candidates=(first, alias)),
        agent_run_id=_run_id(),
    )
    second_registered = await service.register_work_candidates(
        work=_work(candidates=(alias,), page_identity="page:video-list"),
        agent_run_id=_run_id(),
    )

    assert len(first_registered) == 1
    assert len(second_registered) == 1
    assert len(repository.items) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_target", "alias_target"),
    (
        ("底栏视频", "bottom_tab_video"),
        ("底栏我的", "tab_mine"),
        ("顶部搜索框", "search_bar"),
        ("底栏吃瓜", "tab_chigua"),
    ),
)
async def test_global_navigation_cross_language_aliases_share_destination(
    first_target: str,
    alias_target: str,
) -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    first = _candidate("candidate-first", first_target, scope="app")
    alias = _candidate("candidate-alias", alias_target, scope="app")

    first_registered = await service.register_work_candidates(
        work=_work(candidates=(first,)),
        agent_run_id=_run_id(),
    )
    alias_registered = await service.register_work_candidates(
        work=_work(candidates=(alias,), page_identity="page:profile"),
        agent_run_id=_run_id(),
    )

    assert len(first_registered) == 1
    assert len(alias_registered) == 1
    assert len(repository.items) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_target", "first_purpose", "alias_target", "alias_purpose"),
    (
        ("底栏裸聊", "切换裸聊首页", "bottom_tab_luoliao", "切换到底栏裸聊"),
        ("底栏外围", "切换外围页", "bottom_tab_waiwei", "查看当前外围Tab"),
        ("底栏我的", "进入个人中心", "bottom_tab_wode", "进入我的页面"),
        ("申请入驻", "进入入驻申请", "apply_settle_in", "申请入驻"),
        ("活动横幅", "查看活动详情", "promo_banner", "打开活动横幅"),
        ("发布", "去发布内容", "publish_fab", "发布内容"),
        ("底栏外围", "切换到外围页", "tab_waiwei", "切换外围频道"),
        ("底栏裸聊", "切换到裸聊首页", "tab_luoliao", "切换裸聊主列表"),
    ),
)
async def test_global_navigation_machine_identifier_uses_natural_language_purpose(
    first_target: str,
    first_purpose: str,
    alias_target: str,
    alias_purpose: str,
) -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)

    def with_purpose(candidate: CandidateAction, purpose: str) -> CandidateAction:
        return replace(
            candidate,
            metadata={**dict(candidate.metadata), "purpose": purpose},
        )

    first = with_purpose(
        _candidate("candidate-first", first_target, scope="app"),
        first_purpose,
    )
    alias = with_purpose(
        _candidate("candidate-alias", alias_target, scope="app"),
        alias_purpose,
    )

    first_registered = await service.register_work_candidates(
        work=_work(candidates=(first,)),
        agent_run_id=_run_id(),
    )
    alias_registered = await service.register_work_candidates(
        work=_work(candidates=(alias,), page_identity="page:content-list"),
        agent_run_id=_run_id(),
    )

    assert len(first_registered) == 1
    assert len(alias_registered) == 1
    assert len(repository.items) == 1


def test_global_navigation_v5_expands_chinese_grammar_without_rewriting_v4() -> None:
    first = replace(
        _candidate("candidate-first", "底栏外围", scope="app"),
        metadata={
            **dict(_candidate("candidate-first", "底栏外围", scope="app").metadata),
            "purpose": "切换到外围页",
        },
    )
    alias = replace(
        _candidate("candidate-alias", "tab_waiwei", scope="app"),
        metadata={
            **dict(_candidate("candidate-alias", "tab_waiwei", scope="app").metadata),
            "purpose": "切换外围频道",
        },
    )

    def fingerprint(candidate: CandidateAction, strategy: str) -> str:
        return build_app_frontier_candidate_fingerprint(
            source_page_identity="page:home",
            candidate=with_app_frontier_fingerprint_strategy(
                candidate,
                strategy=strategy,
            ),
        )

    assert fingerprint(first, APP_GLOBAL_NAVIGATION_FINGERPRINT_V4) != fingerprint(
        alias,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V4,
    )
    assert fingerprint(first, APP_GLOBAL_NAVIGATION_FINGERPRINT_V5) == fingerprint(
        alias,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V5,
    )


@pytest.mark.asyncio
async def test_global_navigation_keeps_distinct_business_destinations() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)

    registered = await service.register_work_candidates(
        work=_work(
            candidates=(
                _candidate("candidate-today", "今日热瓜", scope="app"),
                _candidate("candidate-latest", "最新黑料", scope="app"),
            )
        ),
        agent_run_id=_run_id(),
    )

    assert len(registered) == 2
    assert len(repository.items) == 2


@pytest.mark.asyncio
async def test_page_candidates_with_same_purpose_remain_distinct() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    first = replace(
        _candidate("candidate-card-a", "第一个视频", scope="page"),
        metadata={
            **dict(_candidate("candidate-card-a", "第一个视频", scope="page").metadata),
            "purpose": "打开视频详情",
        },
    )
    second = replace(
        _candidate("candidate-card-b", "第二个视频", scope="page"),
        metadata={
            **dict(_candidate("candidate-card-b", "第二个视频", scope="page").metadata),
            "purpose": "打开视频详情",
        },
    )

    registered = await service.register_work_candidates(
        work=_work(candidates=(first, second)),
        agent_run_id=_run_id(),
    )

    assert len(registered) == 2
    assert len(repository.items) == 2


@pytest.mark.asyncio
async def test_v6_page_candidate_deduplicates_same_page_type_redraw() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    first = _candidate("candidate-card-old", "热门第一项", scope="page")
    reobserved = _candidate("candidate-card-new", "热门第一项", scope="page")

    first_registered = await service.register_work_candidates(
        work=_work(
            candidates=(first,),
            page_identity="com.example.target|.MainActivity|content_list|depth:1|scope:nav:a",
        ),
        agent_run_id=_run_id(),
    )
    second_registered = await service.register_work_candidates(
        work=_work(
            candidates=(reobserved,),
            page_identity="com.example.target|.MainActivity|content_list|depth:1|scope:nav:b",
        ),
        agent_run_id=_run_id(),
    )

    assert len(first_registered) == 1
    assert len(second_registered) == 1
    assert len(repository.items) == 1
    assert (
        first_registered[0].candidate.metadata["app_frontier_fingerprint_strategy"]
        == APP_GLOBAL_NAVIGATION_FINGERPRINT_V8
    )


@pytest.mark.asyncio
async def test_v7_page_candidate_merges_mixed_technical_alias_by_purpose() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)

    def category_candidate(candidate_id: str, target: str) -> CandidateAction:
        candidate = _candidate(candidate_id, target, scope="page")
        return replace(
            candidate,
            metadata={**dict(candidate.metadata), "purpose": "切换全部分类"},
        )

    first_registered = await service.register_work_candidates(
        work=_work(
            candidates=(category_candidate("candidate-all-en", "tab_all"),),
            page_identity=("com.example.target|.MainActivity|content_list|depth:1|scope:nav:a"),
        ),
        agent_run_id=_run_id(),
    )
    second_registered = await service.register_work_candidates(
        work=_work(
            candidates=(category_candidate("candidate-all-zh", "tab_全部"),),
            page_identity=("com.example.target|.MainActivity|content_list|depth:3|scope:depth:3"),
        ),
        agent_run_id=_run_id(),
    )

    assert len(first_registered) == 1
    assert len(second_registered) == 1
    assert len(repository.items) == 1
    assert (
        first_registered[0].candidate.metadata["app_frontier_fingerprint_strategy"]
        == APP_GLOBAL_NAVIGATION_FINGERPRINT_V8
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_target", "alias_target"),
    (
        ("裸聊底栏", "裸聊底部导航"),
        ("外围底部标签", "外围底栏"),
        ("夏婉安直播活动 banner", "夏婉安直播活动横幅"),
        ("世界杯活动横幅", "世界杯活动 banner"),
    ),
)
async def test_v8_merges_generic_control_suffix_aliases(
    first_target: str,
    alias_target: str,
) -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)

    await service.register_work_candidates(
        work=_work(
            candidates=(_candidate("candidate-first", first_target, scope="page"),),
            page_identity=("com.example.target|.MainActivity|content_list|depth:1|scope:nav:a"),
        ),
        agent_run_id=_run_id(),
    )
    await service.register_work_candidates(
        work=_work(
            candidates=(_candidate("candidate-alias", alias_target, scope="page"),),
            page_identity=("com.example.target|.MainActivity|content_list|depth:3|scope:nav:b"),
        ),
        agent_run_id=_run_id(),
    )

    assert len(repository.items) == 1


def test_v8_preserves_distinct_named_activities() -> None:
    first = with_app_frontier_fingerprint_strategy(
        _candidate("candidate-first", "世界杯活动横幅", scope="page")
    )
    second = with_app_frontier_fingerprint_strategy(
        _candidate("candidate-second", "夏婉安直播活动横幅", scope="page")
    )

    first_fingerprint = build_app_frontier_candidate_fingerprint(
        source_page_identity="com.example|.Main|home|depth:1|scope:a",
        candidate=first,
    )
    second_fingerprint = build_app_frontier_candidate_fingerprint(
        source_page_identity="com.example|.Main|home|depth:1|scope:b",
        candidate=second,
    )

    assert first.metadata["app_frontier_fingerprint_strategy"] == (
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V8
    )
    assert first_fingerprint != second_fingerprint


@pytest.mark.asyncio
async def test_v7_page_candidate_merges_publish_alias_by_specific_purpose() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)

    def publish_candidate(candidate_id: str, target: str) -> CandidateAction:
        candidate = _candidate(candidate_id, target, scope="page")
        return replace(
            candidate,
            metadata={**dict(candidate.metadata), "purpose": "发布内容"},
        )

    for candidate in (
        publish_candidate("candidate-publish-en", "publish_fab"),
        publish_candidate("candidate-publish-zh", "fab发布"),
    ):
        await service.register_work_candidates(
            work=_work(
                candidates=(candidate,),
                page_identity=("com.example.target|.MainActivity|content_list|depth:1|scope:nav:a"),
            ),
            agent_run_id=_run_id(),
        )

    assert len(repository.items) == 1


@pytest.mark.asyncio
async def test_v7_generic_purpose_does_not_merge_distinct_technical_targets() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)

    for candidate_id, target in (
        ("candidate-all", "tab_all"),
        ("candidate-profile", "tab_profile"),
    ):
        candidate = _candidate(candidate_id, target, scope="page")
        await service.register_work_candidates(
            work=_work(
                candidates=(
                    replace(
                        candidate,
                        metadata={**dict(candidate.metadata), "purpose": "切换"},
                    ),
                ),
                page_identity=("com.example.target|.MainActivity|content_list|depth:1|scope:nav:a"),
            ),
            agent_run_id=_run_id(),
        )

    assert len(repository.items) == 2


@pytest.mark.asyncio
async def test_v7_mixed_alias_remains_distinct_across_page_types() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    candidate = replace(
        _candidate("candidate-all", "tab_全部", scope="page"),
        metadata={
            **dict(_candidate("candidate-all", "tab_全部", scope="page").metadata),
            "purpose": "切换全部分类",
        },
    )

    for page_type in ("content_list", "profile"):
        await service.register_work_candidates(
            work=_work(
                candidates=(candidate,),
                page_identity=(f"com.example.target|.MainActivity|{page_type}|depth:1|scope:nav:a"),
            ),
            agent_run_id=_run_id(),
        )

    assert len(repository.items) == 2


@pytest.mark.asyncio
async def test_v6_page_candidate_remains_distinct_across_page_types() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    candidate = _candidate("candidate-card", "热门第一项", scope="page")

    await service.register_work_candidates(
        work=_work(
            candidates=(candidate,),
            page_identity="com.example.target|.MainActivity|content_list|depth:1|scope:nav:a",
        ),
        agent_run_id=_run_id(),
    )
    await service.register_work_candidates(
        work=_work(
            candidates=(candidate,),
            page_identity="com.example.target|.MainActivity|profile|depth:1|scope:nav:b",
        ),
        agent_run_id=_run_id(),
    )

    assert len(repository.items) == 2


def test_v6_page_candidate_revalidates_after_same_page_type_redraw() -> None:
    candidate = _candidate("candidate-card-old", "热门第一项", scope="page")
    source = _work(
        candidates=(candidate,),
        page_identity="com.example.target|.MainActivity|content_list|depth:1|scope:nav:a",
    )
    registered_candidate = with_app_frontier_fingerprint_strategy(candidate)
    item = _frontier_item(source, registered_candidate)
    current = _work(
        candidates=(_candidate("candidate-card-current", "热门第一项", scope="page"),),
        page_identity="com.example.target|.MainActivity|content_list|depth:1|scope:nav:b",
    )

    intent = build_app_frontier_restore_intent(work=current, item=item)

    assert intent.parameters["candidate_id"] == "candidate-card-current"
    assert intent.parameters["app_frontier_restore_only"] is False


@pytest.mark.asyncio
async def test_global_navigation_alias_revalidates_on_another_business_page() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    stored = replace(
        _candidate("candidate-profile-old", "底部我的标签", scope="app"),
        metadata={
            **dict(_candidate("candidate-profile-old", "底部我的标签", scope="app").metadata),
            "purpose": "进入个人中心",
        },
    )
    item = (
        await service.register_work_candidates(
            work=_work(candidates=(stored,)),
            agent_run_id=_run_id(),
        )
    )[0]
    current = replace(
        _candidate("candidate-profile-current", "我的底栏", scope="app"),
        metadata={
            **dict(_candidate("candidate-profile-current", "我的底栏", scope="app").metadata),
            "purpose": "切换至个人中心频道",
        },
    )
    reobserved = _work(candidates=(current,), page_identity="page:video-list")

    intent = build_app_frontier_restore_intent(work=reobserved, item=item)

    assert intent.parameters["candidate_id"] == "candidate-profile-current"
    assert intent.parameters["app_frontier_restore_only"] is False


@pytest.mark.asyncio
async def test_global_frontier_prioritizes_uncovered_required_page_type() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    goal = AppExplorationGoal(
        required_page_types=("home", "profile"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=2,
        allowed_packages=("com.example.target",),
    )
    work = _work(
        candidates=(
            _candidate(
                "candidate-home",
                "首页 Tab",
                scope="app",
                expected_page_type="home",
            ),
            _candidate(
                "candidate-profile",
                "个人中心 Tab",
                scope="app",
                expected_page_type="profile",
            ),
            _candidate("candidate-unknown", "发现 Tab", scope="app"),
        ),
        covered_page_types=("home",),
        app_goal=goal,
    )

    registered = await service.register_work_candidates(work=work, agent_run_id=_run_id())
    priorities = {item.candidate.candidate_id: item.priority for item in registered}

    assert set(priorities) == {"candidate-profile", "candidate-unknown"}
    assert priorities["candidate-profile"] < priorities["candidate-unknown"]


@pytest.mark.asyncio
async def test_bottom_nav_tab_seek_registers_even_when_page_type_not_in_required() -> None:
    """杏吧：吃瓜／交友→post_list、我的→profile 不在 required 时仍须登记底栏硬覆盖。"""

    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    goal = AppExplorationGoal(
        required_page_types=(
            "home", "content_list", "content_detail", "video_list", "video_detail",
        ),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=5,
        allowed_packages=("com.example.target",),
    )

    def _tab(candidate_id: str, label: str, expected: str) -> CandidateAction:
        base = _candidate(
            candidate_id,
            f"底栏「{label}」",
            scope="app",
            expected_page_type=expected,
        )
        return replace(
            base,
            metadata={
                **dict(base.metadata),
                "bottom_navigation_tab_seek": True,
                "coverage_page_seek": True,
                "require_screenshot_evidence": True,
                "purpose": f"点击底栏「{label}」并截图留证",
            },
        )

    work = _work(
        candidates=(
            _tab("tab-video", "视频", "video_list"),
            _tab("tab-chigua", "吃瓜", "post_list"),
            _tab("tab-jiaoyou", "交友", "post_list"),
            _tab("tab-app", "应用", "home"),
            _tab("tab-wode", "我的", "profile"),
        ),
        covered_page_types=("video_list",),
        app_goal=goal,
        metadata={"page_state_override": "ignored"},
    )
    # 当前稳定页为 home（_work 默认）；视频栏仍应可登记做截图留证。
    registered = await service.register_work_candidates(work=work, agent_run_id=_run_id())
    ids = {item.candidate.candidate_id for item in registered}

    assert ids == {
        "tab-video",
        "tab-chigua",
        "tab-jiaoyou",
        "tab-app",
        "tab-wode",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["next_missing_goal_pending", "next_pending"])
async def test_unmapped_bottom_nav_pending_beats_home_grid(selector: str) -> None:
    """无 expected 页型的底栏（如 AI科技）不得被首页宫格入口饿死。"""

    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    goal = AppExplorationGoal(
        required_page_types=("home", "content_list", "video_list"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=3,
        allowed_packages=("com.example.target",),
    )

    def _bottom(candidate_id: str, label: str) -> CandidateAction:
        base = _candidate(candidate_id, f"底栏「{label}」", scope="app")
        return replace(
            base,
            exploration_value=98,
            metadata={
                **dict(base.metadata),
                "bottom_navigation_tab_seek": True,
                "coverage_page_seek": True,
                "require_screenshot_evidence": True,
                "purpose": f"点击底栏「{label}」并截图留证",
            },
        )

    home_grid = replace(
        _candidate("grid-entry", "入口_空降少女", scope="page"),
        exploration_value=95,
        metadata={
            **dict(_candidate("grid-entry", "入口_空降少女", scope="page").metadata),
            "coverage_page_seek": True,
            "home_category_grid_seek": True,
            "purpose": "点击分栏目入口「入口_空降少女」并截图留证",
        },
    )
    work = _work(
        candidates=(_bottom("tab-ai", "AI科技"), home_grid),
        covered_page_types=("home", "content_list", "video_list"),
        app_goal=goal,
        metadata={
            "visited_bottom_navigation_labels": ["精选", "片库", "直播", "首页", "圈子"],
            "required_page_types": ["home", "content_list", "video_list"],
            "app_runtime_policy": {
                "app_target_snapshot": {
                    "playbook": {
                        "tabs": ["首页", "精选", "片库", "直播", "AI科技", "圈子"],
                    }
                }
            },
        },
    )
    await service.register_work_candidates(work=work, agent_run_id=_run_id())
    pending = await getattr(service, selector)(work=work, agent_run_id=_run_id())

    assert pending is not None
    assert pending.candidate.candidate_id == "tab-ai"



@pytest.mark.asyncio
async def test_pending_frontier_prefers_higher_priority_missing_page_type() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    goal = AppExplorationGoal(
        required_page_types=("home", "video_list", "content_detail"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=2,
        allowed_packages=("com.example.target",),
    )
    await service.register_work_candidates(
        work=_work(
            candidates=(
                _candidate(
                    "candidate-detail",
                    "内容卡片",
                    scope="page",
                    expected_page_type="content_detail",
                ),
                _candidate(
                    "candidate-video",
                    "视频底栏",
                    scope="app",
                    expected_page_type="video_list",
                ),
            ),
            covered_page_types=("home",),
            app_goal=goal,
        ),
        agent_run_id=_run_id(),
    )
    pending = await service.next_pending(
        work=_work(
            candidates=(),
            covered_page_types=("home",),
            app_goal=goal,
        ),
        agent_run_id=_run_id(),
    )

    assert pending is not None
    assert pending.candidate.candidate_id == "candidate-video"


@pytest.mark.asyncio
async def test_restore_pending_bypasses_scheduler_deadline_stop() -> None:
    """本地终态 restore 不得被 scheduler deadline 滤光，否则会空转 Deferred。"""

    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    goal = AppExplorationGoal(
        required_page_types=("home", "video_list"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=2,
        allowed_packages=("com.example.target",),
    )
    await service.register_work_candidates(
        work=_work(
            candidates=(
                _candidate(
                    "candidate-video",
                    "视频底栏",
                    scope="app",
                    expected_page_type="video_list",
                ),
            ),
            covered_page_types=("home",),
            app_goal=goal,
        ),
        agent_run_id=_run_id(),
    )
    expired = replace(
        _work(candidates=(), covered_page_types=("home",), app_goal=goal),
        deadline=datetime.now(UTC) - timedelta(minutes=1),
    )

    assert await service.next_pending(work=expired, agent_run_id=_run_id()) is None
    restored = await service.next_pending_for_restore(
        work=expired,
        agent_run_id=_run_id(),
    )
    assert restored is not None
    assert restored.candidate.candidate_id == "candidate-video"


@pytest.mark.asyncio
async def test_pending_frontier_prefers_required_target_over_unknown_app_scope() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    goal = AppExplorationGoal(
        required_page_types=("home", "content_detail"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=2,
        allowed_packages=("com.example.target",),
    )
    await service.register_work_candidates(
        work=_work(
            candidates=(
                _candidate("candidate-unknown", "发现 Tab", scope="app"),
                _candidate(
                    "candidate-detail",
                    "内容卡片",
                    scope="page",
                    expected_page_type="content_detail",
                ),
            ),
            covered_page_types=("home",),
            app_goal=goal,
        ),
        agent_run_id=_run_id(),
    )

    pending = await service.next_pending(
        work=_work(
            candidates=(),
            covered_page_types=("home",),
            app_goal=goal,
        ),
        agent_run_id=_run_id(),
    )

    assert pending is not None
    assert pending.candidate.candidate_id == "candidate-detail"


@pytest.mark.asyncio
async def test_goal_frontier_bounds_alternatives_for_same_missing_page_type() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    goal = AppExplorationGoal(
        required_page_types=("home", "content_detail"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=2,
        allowed_packages=("com.example.target",),
    )
    candidates = tuple(
        replace(
            _candidate(
                f"candidate-detail-{index}",
                f"内容卡片 {index}",
                scope="page",
                expected_page_type="content_detail",
            ),
            exploration_value=80 + index,
        )
        for index in range(5)
    )

    registered = await service.register_work_candidates(
        work=_work(
            candidates=candidates,
            covered_page_types=("home",),
            app_goal=goal,
        ),
        agent_run_id=_run_id(),
    )

    assert {item.candidate.candidate_id for item in registered} == {
        "candidate-detail-4",
    }


@pytest.mark.asyncio
async def test_goal_frontier_uses_existing_pending_quota_before_registering_more() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    goal = AppExplorationGoal(
        required_page_types=("home", "profile"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=2,
        allowed_packages=("com.example.target",),
    )
    first = _work(
        candidates=(
            _candidate(
                "candidate-profile-a",
                "个人中心底栏",
                scope="app",
                expected_page_type="profile",
            ),
            _candidate(
                "candidate-profile-b",
                "头像入口",
                scope="page",
                expected_page_type="profile",
            ),
        ),
        covered_page_types=("home",),
        app_goal=goal,
    )
    await service.register_work_candidates(work=first, agent_run_id=_run_id())

    second = await service.register_work_candidates(
        work=_work(
            candidates=(
                _candidate(
                    "candidate-profile-c",
                    "我的资料入口",
                    scope="page",
                    expected_page_type="profile",
                ),
            ),
            page_identity="page:content-list",
            covered_page_types=("home",),
            app_goal=goal,
        ),
        agent_run_id=_run_id(),
    )

    assert second == ()
    assert len(repository.items) == 1


@pytest.mark.asyncio
async def test_goal_frontier_passes_through_unselected_pages_only_while_targets_missing() -> None:
    """只选了详情页时，要能经过列表页、个人中心去找；目标都走到后不再往没选的页面走。"""
    goal = AppExplorationGoal(
        required_page_types=("video_detail",),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=1,
        allowed_packages=("com.example.target",),
    )
    candidates = (
        _candidate(
            "candidate-video-list",
            "视频底栏",
            scope="app",
            expected_page_type="video_list",
        ),
        _candidate(
            "candidate-profile",
            "我的底栏",
            scope="app",
            expected_page_type="profile",
        ),
        _candidate(
            "candidate-home",
            "首页底栏",
            scope="app",
            expected_page_type="home",
        ),
    )

    searching = await AppGlobalFrontierService(
        repository=MemoryFrontierRepository()
    ).register_work_candidates(
        work=_work(candidates=candidates, covered_page_types=("home",), app_goal=goal),
        agent_run_id=_run_id(),
    )
    finished = await AppGlobalFrontierService(
        repository=MemoryFrontierRepository()
    ).register_work_candidates(
        work=_work(
            candidates=candidates,
            covered_page_types=("home", "video_detail"),
            app_goal=goal,
        ),
        agent_run_id=_run_id(),
    )

    assert {item.candidate.candidate_id for item in searching} == {
        "candidate-video-list",
        "candidate-profile",
    }
    assert finished == ()


@pytest.mark.asyncio
async def test_global_frontier_suppresses_repeated_synonym_for_covered_target() -> None:
    from services.api.app.application.app_semantic_progress import (
        build_app_semantic_action_key,
    )

    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    english = _candidate(
        "candidate-login-en",
        "login_button",
        scope="page",
        expected_page_type="home",
    )
    chinese = replace(
        _candidate(
            "candidate-login-zh",
            "登录按钮",
            scope="page",
            expected_page_type="home",
        ),
        metadata={
            **dict(english.metadata),
            "candidate_id": "candidate-login-zh",
            "purpose": "提交登录",
        },
    )
    english = replace(
        english,
        metadata={**dict(english.metadata), "purpose": "提交登录"},
    )
    semantic_key = build_app_semantic_action_key(english)
    work = _work(
        candidates=(chinese,),
        page_identity="page:login-changed",
        covered_page_types=("home",),
        semantic_action_counts={semantic_key: 2},
    )

    registered = await service.register_work_candidates(
        work=work,
        agent_run_id=_run_id(),
    )

    assert build_app_semantic_action_key(chinese) == semantic_key
    assert registered == ()
    assert repository.items == {}


@pytest.mark.asyncio
async def test_pending_frontier_uses_source_page_to_detect_first_successful_duplicate() -> None:
    from services.api.app.application.app_semantic_progress import (
        build_app_semantic_action_key,
    )

    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    discovered = _work(
        candidates=(
            _candidate(
                "candidate-profile",
                "个人中心底栏",
                scope="app",
                expected_page_type="profile",
            ),
        ),
        page_identity="page:home",
    )
    (item,) = await service.register_work_candidates(
        work=discovered,
        agent_run_id=_run_id(),
    )
    semantic_key = build_app_semantic_action_key(
        item.candidate,
        page_identity=item.source_page_identity,
    )
    latest = replace(
        discovered,
        frontier=replace(
            discovered.frontier,
            observation=replace(
                discovered.frontier.observation,
                metadata={
                    **dict(discovered.frontier.observation.metadata),
                    "business_page_identity": "page:profile",
                    "covered_page_types": ("home", "profile"),
                    "app_semantic_action_counts": {semantic_key: 1},
                },
            ),
        ),
    )

    assert service.pending_item_is_redundant(work=latest, item=item) is True


@pytest.mark.asyncio
async def test_pending_frontier_ignores_restoration_depth_and_scope_for_duplicate() -> None:
    from services.api.app.application.app_semantic_progress import (
        build_app_semantic_action_key,
    )

    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    candidate = replace(
        _candidate(
            "candidate-acg",
            "底栏_二次元",
            scope="app",
            expected_page_type="home",
        ),
        metadata={
            **dict(
                _candidate(
                    "candidate-acg",
                    "底栏_二次元",
                    scope="app",
                    expected_page_type="home",
                ).metadata
            ),
            "purpose": "进入情涩AI功能",
        },
    )
    source_identity = (
        "com.example.target|com.example.MainActivity|home|depth:0|scope:app_ready"
    )
    current_identity = (
        "com.example.target|com.example.MainActivity|home|depth:17|scope:nav:changed"
    )
    discovered = _work(candidates=(candidate,), page_identity=source_identity)
    (item,) = await service.register_work_candidates(
        work=discovered,
        agent_run_id=_run_id(),
    )
    current_key = build_app_semantic_action_key(
        item.candidate,
        page_identity=current_identity,
    )
    latest = replace(
        discovered,
        frontier=replace(
            discovered.frontier,
            observation=replace(
                discovered.frontier.observation,
                metadata={
                    **dict(discovered.frontier.observation.metadata),
                    "business_page_identity": current_identity,
                    "covered_page_types": ("home",),
                    "app_semantic_action_counts": {current_key: 1},
                },
            ),
        ),
    )

    assert build_app_semantic_action_key(
        item.candidate,
        page_identity=source_identity,
    ) == current_key
    assert service.pending_item_is_redundant(work=latest, item=item) is True


@pytest.mark.asyncio
async def test_pending_frontier_keeps_candidate_for_uncovered_page_type() -> None:
    from services.api.app.application.app_semantic_progress import (
        build_app_semantic_action_key,
    )

    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    work = _work(
        candidates=(
            _candidate(
                "candidate-profile",
                "个人中心底栏",
                scope="app",
                expected_page_type="profile",
            ),
        ),
        page_identity="page:home",
        covered_page_types=("home",),
    )
    (item,) = await service.register_work_candidates(work=work, agent_run_id=_run_id())
    semantic_key = build_app_semantic_action_key(
        item.candidate,
        page_identity=item.source_page_identity,
    )
    latest = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={
                    **dict(work.frontier.observation.metadata),
                    "app_semantic_action_counts": {semantic_key: 1},
                },
            ),
        ),
    )

    assert service.pending_item_is_redundant(work=latest, item=item) is False


@pytest.mark.parametrize(
    ("action_type", "expected_page_type", "covered_page_types", "suppressed"),
    [
        (InteractionOperation.CLICK, "profile", ("home",), False),
        (InteractionOperation.SCROLL, "home", ("home",), True),
        (InteractionOperation.OBSERVE, "home", ("home",), False),
        (InteractionOperation.BACK, "home", ("home",), False),
    ],
)
def test_semantic_guard_preserves_uncovered_and_non_click_candidates(
    action_type: InteractionOperation,
    expected_page_type: str,
    covered_page_types: tuple[str, ...],
    suppressed: bool,
) -> None:
    from services.api.app.application.app_semantic_progress import (
        build_app_semantic_action_key,
        should_suppress_redundant_app_candidate,
    )

    candidate = _candidate(
        "candidate-safe",
        "安全候选",
        scope="app",
        action_type=action_type,
        expected_page_type=expected_page_type,
    )

    assert (
        should_suppress_redundant_app_candidate(
            candidate=candidate,
            coverage_metadata={
                "covered_page_types": covered_page_types,
                "app_semantic_action_counts": {
                    build_app_semantic_action_key(candidate): 3,
                },
            },
        )
        is suppressed
    )


def test_semantic_guard_never_suppresses_current_popup_close() -> None:
    from services.api.app.application.app_semantic_progress import (
        build_app_semantic_action_key,
        should_suppress_redundant_app_candidate,
    )

    original = _candidate(
        "candidate-popup-close-next-layer",
        "关闭广告弹窗",
        scope="local",
    )
    candidate = replace(
        original,
        metadata={
            **dict(original.metadata),
            "purpose": "关闭中间推广弹层；当前截图唯一孤立 X 像素结构校准",
            "expected_state_hint": "close_popup_x",
        },
    )
    semantic_key = build_app_semantic_action_key(
        candidate,
        page_identity="home|app_ready",
    )

    assert should_suppress_redundant_app_candidate(
        candidate=candidate,
        coverage_metadata={
            "business_page_identity": "home|app_ready",
            "app_semantic_action_counts": {semantic_key: 9},
        },
    ) is False


def test_semantic_guard_never_suppresses_provider_popup_close_with_home_hint() -> None:
    from services.api.app.application.app_semantic_progress import (
        build_app_semantic_action_key,
        should_suppress_redundant_app_candidate,
    )

    candidate = _candidate(
        "candidate-provider-popup-close",
        "关闭活动弹窗",
        scope="local",
    )
    candidate = replace(
        candidate,
        metadata={
            **dict(candidate.metadata),
            "purpose": "关闭促销弹层回到首页",
            "expected_state_hint": "home",
        },
    )

    assert should_suppress_redundant_app_candidate(
        candidate=candidate,
        coverage_metadata={
            "app_semantic_action_counts": {
                build_app_semantic_action_key(candidate): 4,
            },
        },
    ) is False


@pytest.mark.asyncio
async def test_global_frontier_reprioritizes_pending_items_from_latest_coverage() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    goal = AppExplorationGoal(
        required_page_types=("home", "profile"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=2,
        allowed_packages=("com.example.target",),
    )
    discovered = _work(
        candidates=(
            _candidate("candidate-home", "首页 Tab", scope="app", expected_page_type="home"),
            _candidate("candidate-unknown", "发现 Tab", scope="app"),
            _candidate(
                "candidate-profile",
                "个人中心 Tab",
                scope="app",
                expected_page_type="profile",
            ),
        ),
        app_goal=goal,
    )
    await service.register_work_candidates(work=discovered, agent_run_id=_run_id())
    latest = replace(
        discovered,
        frontier=replace(
            discovered.frontier,
            observation=replace(
                discovered.frontier.observation,
                metadata={
                    **dict(discovered.frontier.observation.metadata),
                    "covered_page_types": ["home"],
                },
            ),
        ),
    )

    pending = await service.next_pending(work=latest, agent_run_id=_run_id())

    assert pending is not None
    assert pending.candidate.candidate_id == "candidate-profile"


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["next_missing_goal_pending", "next_pending"])
async def test_missing_goal_entry_includes_covered_page_type_with_evidence_gap(selector) -> None:
    """页型已覆盖但 Evidence Index 缺证时，定向领取仍应触发补采。"""
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    goal = AppExplorationGoal(
        required_page_types=("home", "profile"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=2,
        allowed_packages=("com.example.target",),
    )
    discovered = _work(
        candidates=(
            _candidate("candidate-discovery", "自由发现入口", scope="app"),
            _candidate(
                "candidate-profile-gap",
                "个人中心补采入口",
                scope="app",
                expected_page_type="profile",
            ),
        ),
        covered_page_types=("home", "profile"),
        app_goal=goal,
        metadata={
            "app_evidence_index": {
                "evidence_gaps": [
                    {"reason_code": "capture_missing", "page_type": "profile"},
                ],
            },
        },
    )
    discovered = replace(
        discovered,
        budget=ExplorationBudget(
            max_actions=20, remaining_actions=20, max_depth=3, remaining_duration_ms=60_000,
        ),
    )
    await service.register_work_candidates(work=discovered, agent_run_id=_run_id())

    pending = await getattr(service, selector)(
        work=replace(
            discovered,
            frontier=replace(discovered.frontier, candidates=()),
        ),
        agent_run_id=_run_id(),
    )

    assert pending is not None
    assert pending.candidate.candidate_id == "candidate-profile-gap"


@pytest.mark.asyncio
async def test_terminal_reobservation_does_not_reexecute_visited_global_candidate() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    candidate = _candidate(
        "candidate-a",
        "首页 Tab",
        scope="app",
        expected_page_type="home",
    )
    work = _work(candidates=(candidate,))
    registered = await service.register_work_candidates(work=work, agent_run_id=_run_id())
    item = registered[0]
    repository.items[item.candidate_fingerprint] = replace(
        item,
        metadata={**dict(item.metadata), "status": "visited"},
    )

    replay = await service.register_work_candidates(work=work, agent_run_id=_run_id())
    retained = service.retain_registered_candidates(work=work, registered=replay)

    assert replay == ()
    assert retained.frontier.candidates == ()
    restoration_payloads = retained.frontier.observation.metadata[
        APP_FRONTIER_RESTORATION_CANDIDATES
    ]
    assert len(restoration_payloads) == 1
    assert CandidateAction.from_dict(restoration_payloads[0]) == candidate


@pytest.mark.parametrize("scope", ["page", "app"])
def test_restore_blocks_same_page_type_when_candidate_did_not_reappear(
    scope: str,
) -> None:
    sibling = _candidate("candidate-home-item", "首页推荐项", scope=scope)
    source = _work(
        candidates=(sibling,),
        page_identity="com.example.target|.MainActivity|home|depth:1|scope:nav:main",
    )
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(sibling))
    home_anchor = _candidate(
        "candidate-home-tab",
        "首页 Tab",
        scope="app",
        expected_page_type="home",
    )
    current = _work(
        candidates=(home_anchor,),
        page_identity="com.example.target|.MainActivity|home|depth:2|scope:nav:ai",
    )
    retained = AppGlobalFrontierService(
        repository=MemoryFrontierRepository()
    ).retain_registered_candidates(work=current, registered=())

    assert retained.frontier.candidates == ()
    with pytest.raises(AppFrontierRestorationError) as exc_info:
        build_app_frontier_restore_intent(work=retained, item=item)

    assert exc_info.value.error_code == "APP_FRONTIER_CANDIDATE_REVALIDATION_REQUIRED"


def test_restore_rebinds_safe_missing_post_publish_entry_after_label_drift() -> None:
    goal = AppExplorationGoal(
        required_page_types=("post_list", "post_publish"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=2,
        allowed_packages=("com.example.target",),
    )
    stored = replace(
        _candidate(
            "candidate-publish-plus",
            "发布加号按钮",
            scope="page",
            expected_page_type="post_publish",
        ),
        metadata={
            **dict(
                _candidate(
                    "candidate-publish-plus",
                    "发布加号按钮",
                    scope="page",
                    expected_page_type="post_publish",
                ).metadata
            ),
            "purpose": "发布新帖/内容",
        },
    )
    source = _work(
        candidates=(stored,),
        page_identity="com.example.target|.MainActivity|post_list|name:old",
        covered_page_types=("post_list",),
        app_goal=goal,
    )
    item = _frontier_item(
        source,
        with_app_frontier_fingerprint_strategy(stored),
    )
    current = replace(
        _candidate(
            "candidate-publish-current",
            "发布按钮",
            scope="page",
            expected_page_type="post_publish",
        ),
        metadata={
            **dict(
                _candidate(
                    "candidate-publish-current",
                    "发布按钮",
                    scope="page",
                    expected_page_type="post_publish",
                ).metadata
            ),
            "purpose": "发布新帖",
        },
    )
    reobserved = _work(
        candidates=(current,),
        page_identity="com.example.target|.MainActivity|post_list|name:new",
        covered_page_types=("post_list",),
        app_goal=goal,
    )

    intent = build_app_frontier_restore_intent(work=reobserved, item=item)

    assert intent.parameters["candidate_id"] == current.candidate_id
    assert intent.parameters["app_frontier_restore_only"] is False


def test_restore_does_not_rebind_final_post_submission_as_editor_entry() -> None:
    goal = AppExplorationGoal(
        required_page_types=("post_list", "post_publish"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=2,
        allowed_packages=("com.example.target",),
    )
    stored = _candidate(
        "candidate-publish-plus",
        "发布加号按钮",
        scope="page",
        expected_page_type="post_publish",
    )
    source = _work(
        candidates=(stored,),
        page_identity="com.example.target|.MainActivity|post_list|name:old",
        covered_page_types=("post_list",),
        app_goal=goal,
    )
    item = _frontier_item(
        source,
        with_app_frontier_fingerprint_strategy(stored),
    )
    submit = replace(
        _candidate(
            "candidate-submit-post",
            "确认发布",
            scope="page",
            expected_page_type="post_publish",
        ),
        metadata={
            **dict(
                _candidate(
                    "candidate-submit-post",
                    "确认发布",
                    scope="page",
                    expected_page_type="post_publish",
                ).metadata
            ),
            "purpose": "提交发布内容",
        },
    )
    reobserved = _work(
        candidates=(submit,),
        page_identity="com.example.target|.MainActivity|post_list|name:new",
        covered_page_types=("post_list",),
        app_goal=goal,
    )

    with pytest.raises(AppFrontierRestorationError) as exc_info:
        build_app_frontier_restore_intent(work=reobserved, item=item)

    assert exc_info.value.error_code == "APP_FRONTIER_CANDIDATE_REVALIDATION_REQUIRED"


def test_restore_uses_hint_only_current_frame_navigation_anchor() -> None:
    sibling = _candidate("candidate-content-item", "内容卡片", scope="page")
    source = _work(
        candidates=(sibling,),
        page_identity="com.example.target|.MainActivity|content_list|depth:1|scope:list",
    )
    item = _frontier_item(source, sibling)
    content_anchor = _candidate(
        "candidate-content-tab",
        "内容 Tab",
        scope="app",
    )
    content_anchor = replace(
        content_anchor,
        metadata={
            **dict(content_anchor.metadata),
            "expected_state_hint": "content_list",
        },
    )
    loading = _work(
        candidates=(content_anchor,),
        page_identity="com.example.target|.MainActivity|line_loading|depth:2|scope:line",
    )
    retained = AppGlobalFrontierService(
        repository=MemoryFrontierRepository()
    ).retain_registered_candidates(work=loading, registered=())

    assert retained.frontier.candidates == ()
    restoration_payloads = retained.frontier.observation.metadata[
        APP_FRONTIER_RESTORATION_CANDIDATES
    ]
    assert len(restoration_payloads) == 1

    intent = build_app_frontier_restore_intent(work=retained, item=item)

    assert intent.parameters["candidate_id"] == content_anchor.candidate_id
    assert intent.parameters["app_frontier_restore_only"] is True


def test_restore_rejects_candidate_not_in_current_observation_metadata() -> None:
    sibling = _candidate("candidate-home-item", "首页推荐项", scope="page")
    source = _work(
        candidates=(sibling,),
        page_identity="com.example.target|.MainActivity|home|depth:1|scope:nav:main",
    )
    item = _frontier_item(source, sibling)
    home_anchor = _candidate(
        "candidate-home-tab",
        "首页 Tab",
        scope="app",
        expected_page_type="home",
    )
    current = _work(
        candidates=(home_anchor,),
        page_identity="com.example.target|.MainActivity|home|depth:2|scope:nav:ai",
    )
    retained = AppGlobalFrontierService(
        repository=MemoryFrontierRepository()
    ).retain_registered_candidates(work=current, registered=())
    intent = build_app_frontier_restore_intent(work=retained, item=item)
    forged = replace(
        intent,
        parameters={**dict(intent.parameters), "candidate_id": "candidate-forged"},
    )

    with pytest.raises(AppFrontierRestorationError) as exc_info:
        bind_app_frontier_restoration_candidate(work=retained, decision=forged)

    assert exc_info.value.error_code == "APP_FRONTIER_RESTORATION_CANDIDATE_UNBOUND"


def test_restore_reaches_settings_source_through_profile_parent_route() -> None:
    sibling = _candidate("candidate-settings-item", "账号与安全", scope="page")
    source = _work(
        candidates=(sibling,),
        page_identity="com.example.target|.MainActivity|settings|depth:2|scope:settings",
    )
    item = _frontier_item(source, sibling)
    profile_anchor = _candidate(
        "candidate-profile-tab",
        "我的 Tab",
        scope="app",
        expected_page_type="profile",
    )
    home = _work(
        candidates=(profile_anchor,),
        page_identity="com.example.target|.MainActivity|home|depth:1|scope:nav:main",
    )

    first_intent = build_app_frontier_restore_intent(work=home, item=item)

    assert first_intent.parameters["candidate_id"] == "candidate-profile-tab"
    assert first_intent.parameters["app_frontier_restore_only"] is True

    settings_anchor = _candidate(
        "candidate-settings-gear",
        "设置",
        scope="page",
        expected_page_type="settings",
    )
    profile = _work(
        candidates=(settings_anchor,),
        page_identity="com.example.target|.MainActivity|profile|depth:1|scope:nav:mine",
    )

    second_intent = build_app_frontier_restore_intent(work=profile, item=item)

    assert second_intent.parameters["candidate_id"] == "candidate-settings-gear"
    assert second_intent.parameters["app_frontier_restore_only"] is True


def test_restore_revalidates_current_visual_candidate_and_never_replays_old_id() -> None:
    old = _candidate("candidate-old", "消息 Tab", scope="app")
    work = _work(candidates=(old,))
    item = _frontier_item(work, old)
    current = _candidate("candidate-current", "消息 Tab", scope="app")
    reobserved = replace(work, frontier=replace(work.frontier, candidates=(current,)))

    intent = build_app_frontier_restore_intent(work=reobserved, item=item)

    assert intent.parameters["candidate_id"] == "candidate-current"
    assert intent.parameters["app_frontier_item_id"] == item.frontier_item_id
    assert intent.source_capture_id == work.source_capture_id


def test_revalidated_global_target_is_rebound_when_normal_frontier_was_deduplicated() -> None:
    candidate = _candidate(
        "candidate-profile-tab",
        "我的 Tab",
        scope="app",
        expected_page_type="profile",
    )
    current = _work(
        candidates=(candidate,),
        page_identity="com.example.target|.MainActivity|content_detail|depth:2|scope:detail",
    )
    item = _frontier_item(current, with_app_frontier_fingerprint_strategy(candidate))
    retained = AppGlobalFrontierService(
        repository=MemoryFrontierRepository()
    ).retain_registered_candidates(work=current, registered=())

    assert retained.frontier.candidates == ()
    intent = build_app_frontier_restore_intent(work=retained, item=item)
    assert intent.parameters["app_frontier_restore_only"] is False

    rebound = bind_app_frontier_restoration_candidate(work=retained, decision=intent)

    assert rebound.frontier.candidates == (candidate,)


def test_restore_uses_controlled_back_when_current_page_differs() -> None:
    sibling = _candidate("candidate-old", "消息 Tab", scope="app")
    source = _work(candidates=(sibling,))
    item = _frontier_item(source, sibling)
    back = _candidate(
        "candidate-back",
        "返回",
        scope="local",
        action_type=InteractionOperation.BACK,
    )
    detail = _work(candidates=(back,), page_identity="page:detail")

    intent = build_app_frontier_restore_intent(work=detail, item=item)

    assert intent.action_type is InteractionOperation.BACK
    assert intent.parameters["app_frontier_restore_only"] is True
    assert intent.parameters["candidate_id"] == "candidate-back"


def test_restore_uses_current_global_anchor_for_source_page_type() -> None:
    sibling = _candidate("candidate-profile-item", "我的订单", scope="page")
    source = _work(
        candidates=(sibling,),
        page_identity="com.example.target|.MainActivity|profile|depth:1|scope:nav:mine",
    )
    item = _frontier_item(source, sibling)
    profile_anchor = _candidate(
        "candidate-profile-tab",
        "我的 Tab",
        scope="app",
        expected_page_type="profile",
    )
    current = _work(
        candidates=(profile_anchor,),
        page_identity="com.example.target|.MainActivity|content_list|depth:2|scope:nav:list",
    )

    intent = build_app_frontier_restore_intent(work=current, item=item)

    assert intent.parameters["candidate_id"] == "candidate-profile-tab"
    assert intent.parameters["app_frontier_restore_only"] is True
    assert "全局导航锚点" in intent.purpose


def test_restore_uses_current_visible_click_back_control() -> None:
    sibling = _candidate("candidate-profile-item", "我的订单", scope="page")
    source = _work(
        candidates=(sibling,),
        page_identity="com.example.target|.MainActivity|profile|depth:1|scope:nav:mine",
    )
    item = _frontier_item(source, sibling)
    click_back = replace(
        _candidate("candidate-click-back", "返回", scope="local"),
        metadata={
            **dict(_candidate("candidate-click-back", "返回", scope="local").metadata),
            "purpose": "返回上一页",
        },
    )
    detail = _work(
        candidates=(click_back,),
        page_identity="com.example.target|.DetailActivity|content_detail|depth:2|scope:detail",
    )

    intent = build_app_frontier_restore_intent(work=detail, item=item)

    assert intent.action_type is InteractionOperation.CLICK
    assert intent.parameters["candidate_id"] == "candidate-click-back"
    assert intent.parameters["app_frontier_restore_only"] is True
    assert "显式返回控件" in intent.purpose


def test_restore_does_not_treat_business_target_containing_back_as_page_back() -> None:
    sibling = _candidate("candidate-profile-item", "我的订单", scope="page")
    source = _work(
        candidates=(sibling,),
        page_identity="com.example.target|.MainActivity|profile|depth:1|scope:nav:mine",
    )
    item = _frontier_item(source, sibling)
    business_target = replace(
        _candidate("candidate-return-campaign", "返回首页活动", scope="local"),
        metadata={
            **dict(
                _candidate(
                    "candidate-return-campaign",
                    "返回首页活动",
                    scope="local",
                ).metadata
            ),
            "purpose": "查看返回活动",
        },
    )
    detail = _work(
        candidates=(business_target,),
        page_identity="com.example.target|.DetailActivity|content_detail|depth:2|scope:detail",
    )

    with pytest.raises(AppFrontierRestorationError) as error:
        build_app_frontier_restore_intent(work=detail, item=item)

    assert error.value.error_code == "APP_FRONTIER_SOURCE_UNRECOVERABLE"


def test_restore_uses_global_anchor_when_current_page_identity_is_unknown() -> None:
    sibling = _candidate("candidate-profile-item", "我的订单", scope="page")
    source = _work(
        candidates=(sibling,),
        page_identity="com.example.target|.MainActivity|profile|depth:1|scope:nav:mine",
    )
    item = _frontier_item(source, sibling)
    profile_anchor = _candidate(
        "candidate-profile-tab",
        "我的 Tab",
        scope="app",
        expected_page_type="profile",
    )
    current = _work(candidates=(profile_anchor,))
    metadata = dict(current.frontier.observation.metadata)
    metadata.pop("business_page_identity")
    current = replace(
        current,
        frontier=replace(
            current.frontier,
            observation=replace(current.frontier.observation, metadata=metadata),
        ),
    )

    intent = build_app_frontier_restore_intent(work=current, item=item)

    assert intent.parameters["candidate_id"] == "candidate-profile-tab"
    assert intent.parameters["app_frontier_restore_only"] is True


def test_restore_blocks_changed_source_without_revalidated_candidate() -> None:
    sibling = _candidate("candidate-old", "消息 Tab", scope="app")
    source = _work(candidates=(sibling,))
    item = _frontier_item(source, sibling)
    changed = replace(source, frontier=replace(source.frontier, candidates=()))

    with pytest.raises(AppFrontierRestorationError, match="重新识别"):
        build_app_frontier_restore_intent(work=changed, item=item)


def test_restore_forces_back_when_current_foreground_left_target_package() -> None:
    sibling = _candidate("candidate-old", "bottom_tab_chigua", scope="app")
    source = _work(candidates=(sibling,))
    item = _frontier_item(source, sibling)
    back = _candidate(
        "candidate-back",
        "系统返回键",
        scope="local",
        action_type=InteractionOperation.BACK,
    )
    chrome = _work(
        candidates=(sibling, back),
        page_identity="org.chromium...|home|depth:0|scope:app_ready",
        location="app://com.android.chrome/org.chromium.chrome.browser.firstrun.FirstRunActivity",
    )

    intent = build_app_frontier_restore_intent(work=chrome, item=item)

    assert intent.action_type is InteractionOperation.BACK
    assert intent.parameters["app_frontier_restore_only"] is True
    assert intent.parameters["candidate_id"] == "candidate-back"
    assert "包边界" in intent.purpose


def test_restore_blocks_external_package_without_back_candidate() -> None:
    sibling = _candidate("candidate-old", "bottom_tab_chigua", scope="app")
    source = _work(candidates=(sibling,))
    item = _frontier_item(source, sibling)
    chrome = _work(
        candidates=(sibling,),
        page_identity="org.chromium...|home|depth:0|scope:app_ready",
        location="app://com.android.chrome/org.chromium.chrome.browser.firstrun.FirstRunActivity",
    )

    with pytest.raises(AppFrontierRestorationError, match="包边界") as error:
        build_app_frontier_restore_intent(work=chrome, item=item)

    assert error.value.error_code == "APP_FRONTIER_PACKAGE_BOUNDARY"


@pytest.mark.asyncio
async def test_register_skips_candidates_when_foreground_left_target_package() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    work = _work(
        candidates=(
            _candidate("candidate-a", "首页 Tab", scope="app"),
            _candidate("candidate-b", "消息 Tab", scope="app"),
        ),
        location="app://com.android.chrome/org.chromium.chrome.browser.firstrun.FirstRunActivity",
    )

    registered = await service.register_work_candidates(work=work, agent_run_id=_run_id())

    assert registered == ()
    assert repository.items == {}


@pytest.mark.asyncio
async def test_coverage_page_seek_not_registered_to_global_frontier() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    seek = _coverage_seek_candidate("seek-home-video-old")

    registered = await service.register_work_candidates(
        work=_work(candidates=(seek,)),
        agent_run_id=_run_id(),
    )

    assert registered == ()
    assert repository.items == {}
    retained = service.retain_registered_candidates(
        work=_work(candidates=(seek,)), registered=registered,
    )
    assert retained.frontier.candidates == ()
    assert retained.frontier.observation.metadata[APP_FRONTIER_RESTORATION_CANDIDATES] == ()


def test_legacy_synthetic_coverage_seek_cannot_be_restored() -> None:
    stored = _coverage_seek_candidate("seek-home-video-old")
    source = _work(candidates=(stored,))
    item = _frontier_item(source, stored)
    current_seek = _coverage_seek_candidate("seek-home-video-current")
    current = _work(candidates=(current_seek,))

    with pytest.raises(AppFrontierRestorationError) as exc_info:
        build_app_frontier_restore_intent(work=current, item=item)
    assert exc_info.value.error_code == "APP_FRONTIER_CANDIDATE_REVALIDATION_REQUIRED"


@pytest.mark.asyncio
async def test_package_boundary_recovery_survives_global_frontier_register_and_retain() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    recovery = _candidate(
        "candidate-visual-back",
        "视觉识别系统返回",
        scope="local",
        action_type=InteractionOperation.BACK,
    )
    recovery = replace(
        recovery,
        metadata={
            **dict(recovery.metadata),
            "purpose": "返回目标 App",
            "package_boundary_recovery": True,
        },
    )
    work = _work(
        candidates=(recovery,),
        location="app://com.android.chrome/org.chromium.chrome.browser.ChromeTabbedActivity",
    )

    registered = await service.register_work_candidates(work=work, agent_run_id=_run_id())
    retained = service.retain_registered_candidates(work=work, registered=registered)

    assert registered == ()
    assert retained.frontier.candidates == (recovery,)


def test_frontier_lease_requires_running_identity_and_positive_attempt() -> None:
    work = _work(candidates=(_candidate("candidate-a", "消息 Tab", scope="app"),))
    item = _frontier_item(work, work.frontier.candidates[0])

    lease = AppFrontierLease(
        item=item,
        status=AppFrontierStatus.LEASED,
        lease_token="00000000-0000-0000-0000-000000000333",
        attempt_count=1,
        row_version=2,
    )

    assert lease.item == item
    with pytest.raises(ValueError, match="leased/running"):
        AppFrontierLease(
            item=item,
            status=AppFrontierStatus.PENDING,
            lease_token="00000000-0000-0000-0000-000000000333",
            attempt_count=1,
            row_version=2,
        )
    with pytest.raises(ValueError, match="attempt_count"):
        AppFrontierLease(
            item=item,
            status=AppFrontierStatus.RUNNING,
            lease_token="00000000-0000-0000-0000-000000000333",
            attempt_count=0,
            row_version=2,
        )


def _frontier_item(work, candidate):
    from services.api.app.domain.exploration import build_app_frontier_item

    return build_app_frontier_item(
        task_id=work.task_id,
        agent_run_id=_run_id(),
        session_id=work.session_id,
        source_work_id=work.work_id,
        source_capture_id=work.source_capture_id,
        source_page_identity=work.frontier.observation.metadata["business_page_identity"],
        candidate=candidate,
        surface_kind=work.surface_kind,
        navigation_path=("page:app_ready", "page:home"),
        priority=10,
        discovered_at=work.frontier.observation.observed_at,
    )


def test_app_frontier_restore_attempt_helpers_cap_at_two() -> None:
    from services.api.app.application.app_frontier_restoration import (
        APP_FRONTIER_RESTORE_ATTEMPTS_KEY,
        app_frontier_restore_attempts_exhausted,
        with_incremented_app_frontier_restore_attempt,
    )

    item_id = "frontier-item-1"
    metadata: dict[str, object] = {}
    for _ in range(2):
        assert not app_frontier_restore_attempts_exhausted(
            metadata,
            frontier_item_id=item_id,
        )
        metadata = with_incremented_app_frontier_restore_attempt(
            metadata,
            frontier_item_id=item_id,
        )
    assert metadata[APP_FRONTIER_RESTORE_ATTEMPTS_KEY][item_id] == 2
    assert app_frontier_restore_attempts_exhausted(
        metadata,
        frontier_item_id=item_id,
    )


def _coverage_seek_candidate(
    candidate_id: str,
    *,
    expected_page_type: str = "video_list",
    semantic_target: str = "覆盖寻页：底栏视频",
) -> CandidateAction:
    purpose = f"覆盖寻页进入{expected_page_type}"
    contract = {
        "protocol_version": APP_COVERAGE_PAGE_SEEK_VERSION,
        "normalized_bounds": {"left": 0.35, "top": 0.91, "right": 0.65, "bottom": 0.99},
        "expected_next_page_type": expected_page_type,
        "purpose": purpose,
        "frontier_scope": "app",
        "confidence": 0.8,
    }
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=98,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "candidate_id": candidate_id,
            "purpose": purpose,
            "frontier_scope": "app",
            "expected_next_page_type": expected_page_type,
            "coverage_page_seek": True,
            "coverage_page_seek_contract": contract,
        },
    )


def _run_id() -> str:
    return "00000000-0000-0000-0000-000000000222"


def _same_page_rule_gap_work(*, scope: str = "page"):
    from services.api.app.application.app_navigation_entry_provenance import (
        tag_candidate_action_provenance,
    )
    from services.api.app.application.app_rule_coverage_facts import project_app_rule_coverage_facts

    candidate = _candidate(
        "current-category", "切换当前页分类", scope=scope, expected_page_type="home",
    )
    candidate = replace(
        candidate,
        metadata=tag_candidate_action_provenance(
            candidate.metadata,
            discovery_source="vision",
            source_capture_id="00000000-0000-0000-0000-000000000111",
        ),
    )
    facts_scope = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "agent_run_id": _run_id(),
        "session_id": "app:00000000-0000-0000-0000-000000000001",
        "finalization_epoch": 3,
        "surface_kind": "android",
        "source": "current_task_surface_session",
    }
    coverage = project_app_rule_coverage_facts(
        {"inventory_hash": "a" * 64, "rules": [{
            "rule_id": "00000000-0000-0000-0000-000000000333",
            "rule_code": "category-comparison",
            "applicability": "applicable",
            "applicable_page_types": ["home"],
            "input_contract": {},
            "judge_config": {"capability_config": {"requires_journey": True}},
        }]},
        scope=facts_scope,
        completed_rule_ids=set(),
        captures=(),
    )
    return _work(
        candidates=(candidate,),
        covered_page_types=("home",),
        app_goal=AppExplorationGoal(
            required_page_types=("home", "profile"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=1,
            allowed_packages=("com.example.target",),
        ),
        metadata={
            "agent_run_id": _run_id(),
            "exploration_scheduler_facts_scope": facts_scope,
            "app_rule_inventory_coverage": coverage,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["page", "app"])
async def test_current_page_rule_gap_keeps_fresh_visible_same_page_candidate(scope) -> None:
    service = AppGlobalFrontierService(repository=MemoryFrontierRepository())
    work = _same_page_rule_gap_work(scope=scope)

    registered = await service.register_work_candidates(work=work, agent_run_id=_run_id())
    retained = service.retain_registered_candidates(work=work, registered=registered)

    assert len(registered) == 1
    assert retained.frontier.candidates == work.frontier.candidates
    assert (await service.counts(work=work, agent_run_id=_run_id())).exhausted is False


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "no_rule_facts", "generic_gap_only", "completed", "other_page", "bad_protocol",
    "bad_source", "missing_scope", "wrong_task", "wrong_run", "wrong_session", "old_epoch",
    "stale_candidate", "untrusted_candidate", "occluded", "visited_candidate", "outside_package",
    "no_actions", "no_business_actions", "no_duration", "expired", "too_deep",
])
async def test_current_page_rule_gap_does_not_bypass_scope_visibility_or_budget(invalid) -> None:
    work = _same_page_rule_gap_work()
    metadata = work.frontier.observation.to_dict()["metadata"]
    coverage = metadata["app_rule_inventory_coverage"]
    candidate = work.frontier.candidates[0]
    if invalid in {"no_rule_facts", "generic_gap_only"}:
        metadata.pop("app_rule_inventory_coverage")
        if invalid == "generic_gap_only":
            metadata["app_evidence_index"] = {"evidence_gaps": [{"page_type": "home"}]}
    elif invalid == "completed":
        coverage.update(pending_rule_count=0, pending_rules=[], evidence_gaps=[])
    elif invalid == "other_page":
        coverage["pending_rules"][0]["target_page_types"] = ["profile"]
        coverage["evidence_gaps"][0]["target_page_types"] = ["profile"]
    elif invalid in {"bad_protocol", "bad_source"}:
        coverage["protocol_version" if invalid == "bad_protocol" else "source"] = "agent_draft"
    elif invalid == "missing_scope":
        metadata.pop("exploration_scheduler_facts_scope")
    elif invalid in {"wrong_task", "wrong_run", "wrong_session", "old_epoch"}:
        key = {"wrong_task": "task_id", "wrong_run": "agent_run_id",
               "wrong_session": "session_id", "old_epoch": "finalization_epoch"}[invalid]
        coverage[key] = 2 if invalid == "old_epoch" else "another-scope"
    elif invalid in {"stale_candidate", "untrusted_candidate"}:
        candidate = replace(candidate, metadata={
            **dict(candidate.metadata),
            ("source_capture_id" if invalid == "stale_candidate" else "discovery_source"):
                "old-capture" if invalid == "stale_candidate" else "agent_draft",
        })
    elif invalid == "occluded":
        metadata["surface_facts"] = {"popup_visible": True}
    elif invalid == "visited_candidate":
        work = replace(work, frontier=replace(
            work.frontier, visited_candidate_ids=(candidate.candidate_id,),
        ))
    elif invalid == "outside_package":
        work = replace(work, frontier=replace(work.frontier, observation=replace(
            work.frontier.observation, location="app://com.android.chrome/.MainActivity",
        )))
    elif invalid in {"no_actions", "no_business_actions", "no_duration"}:
        budget = (ExplorationBudget(
            max_actions=8, remaining_actions=2, max_depth=3, remaining_duration_ms=60_000,
            max_business_actions=6, remaining_business_actions=0,
            max_obstruction_actions=2, remaining_obstruction_actions=2,
        ) if invalid == "no_business_actions" else replace(
            work.budget,
            **({"remaining_actions": 0, "remaining_business_actions": 0}
               if invalid == "no_actions" else {"remaining_duration_ms": 0}),
        ))
        work = replace(work, budget=budget)
    elif invalid == "expired":
        work = replace(work, deadline=datetime.now(UTC) - timedelta(seconds=1))
    elif invalid == "too_deep":
        candidate = replace(candidate, navigation_depth=4)
    work = replace(work, frontier=replace(
        work.frontier, candidates=(candidate,),
        observation=replace(work.frontier.observation, metadata=metadata),
    ))
    service = AppGlobalFrontierService(repository=MemoryFrontierRepository())

    assert await service.register_work_candidates(work=work, agent_run_id=_run_id()) == ()


@pytest.mark.asyncio
async def test_current_page_rule_gap_keeps_quota_and_never_reopens_visited_or_semantic_duplicate():
    from services.api.app.application.app_semantic_progress import build_app_semantic_action_key

    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    work = _same_page_rule_gap_work()
    original = work.frontier.candidates[0]
    alternatives = tuple(replace(
        original, candidate_id=f"category-{index}", semantic_target=f"分类 {index}",
        exploration_value=80 + index,
    ) for index in range(4))
    work = replace(work, frontier=replace(work.frontier, candidates=alternatives))
    (first,) = await service.register_work_candidates(work=work, agent_run_id=_run_id())
    assert first.candidate.candidate_id == "category-3"
    assert len(repository.items) == 1
    (replayed,) = await service.register_work_candidates(work=work, agent_run_id=_run_id())
    assert replayed.candidate_fingerprint == first.candidate_fingerprint
    repository.items[first.candidate_fingerprint] = replace(
        first, metadata={**dict(first.metadata), "status": "visited"},
    )
    only_first = replace(work, frontier=replace(work.frontier, candidates=(first.candidate,)))
    assert await service.register_work_candidates(work=only_first, agent_run_id=_run_id()) == ()
    metadata = dict(only_first.frontier.observation.metadata)
    metadata["app_semantic_action_counts"] = {build_app_semantic_action_key(
        first.candidate, page_identity=first.source_page_identity,
    ): 1}
    semantic_replay = replace(only_first, frontier=replace(
        only_first.frontier,
        observation=replace(only_first.frontier.observation, metadata=metadata),
    ))
    fresh_service = AppGlobalFrontierService(repository=MemoryFrontierRepository())
    assert await fresh_service.register_work_candidates(
        work=semantic_replay, agent_run_id=_run_id(),
    ) == ()


@pytest.mark.asyncio
async def test_promoted_visual_navigation_survives_pending_quota_and_reobservation() -> None:
    repository = MemoryFrontierRepository()
    service = AppGlobalFrontierService(repository=repository)
    visual = _candidate("visual-first", "账户入口", scope="app", expected_page_type="profile")
    visual = replace(visual, metadata={**dict(visual.metadata), "coverage_page_seek": True})
    goal = AppExplorationGoal(
        required_page_types=("home", "profile"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=2,
        allowed_packages=("com.example.target",),
    )
    work = _work(candidates=(visual,), app_goal=goal, covered_page_types=("home",))
    first = await service.register_work_candidates(work=work, agent_run_id=_run_id())
    fresh = replace(visual, candidate_id="visual-fresh")
    current = replace(work, frontier=replace(work.frontier, candidates=(fresh,)))
    second = await service.register_work_candidates(work=current, agent_run_id=_run_id())
    retained = service.retain_registered_candidates(work=current, registered=second)

    assert len(first) == len(second) == len(repository.items) == 1
    assert second[0].candidate_fingerprint == first[0].candidate_fingerprint
    assert retained.frontier.candidates == (fresh,)
    intent = build_app_frontier_restore_intent(work=retained, item=first[0])
    assert intent.parameters["candidate_id"] == "visual-fresh"
    assert intent.parameters["app_frontier_restore_only"] is False
    assert "app_coverage_page_seek" not in intent.parameters


@pytest.mark.parametrize(
    "role,label", [("dismiss", "×"), ("dismiss", "Fermer"), ("skip", "スキップ")]
)
def test_obstructed_source_recovers_before_revalidating_navigation(role: str, label: str) -> None:
    nav = _candidate("nav-old", "账户入口", scope="app", expected_page_type="profile")
    source = _work(candidates=(nav,))
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(nav))
    close = _candidate("close-current", label, scope="local")
    close = replace(
        close, metadata={"control_role": role, "source_capture_id": source.source_capture_id}
    )
    blocked = replace(
        source,
        frontier=replace(
            source.frontier,
            candidates=(close,),
            observation=replace(
                source.frontier.observation, page_state="popup", overlay_signature="notice"
            ),
        ),
    )
    recovery = build_app_frontier_restore_intent(work=blocked, item=item)
    assert recovery.parameters["candidate_id"] == close.candidate_id
    assert recovery.parameters["app_frontier_restore_only"] is True

    clear = replace(source, frontier=replace(source.frontier, candidates=()))
    with pytest.raises(AppFrontierRestorationError) as error:
        build_app_frontier_restore_intent(work=clear, item=item)
    assert error.value.error_code == "APP_FRONTIER_CANDIDATE_REVALIDATION_REQUIRED"


def test_overlay_must_not_replay_visible_business_navigation() -> None:
    nav = _candidate("nav", "账户入口", scope="app", expected_page_type="profile")
    source = _work(candidates=(nav,))
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(nav))
    blocked = replace(
        source,
        frontier=replace(
            source.frontier,
            observation=replace(
                source.frontier.observation, page_state="popup", overlay_signature="notice"
            ),
        ),
    )
    with pytest.raises(AppFrontierRestorationError) as error:
        build_app_frontier_restore_intent(work=blocked, item=item)
    assert error.value.error_code == "APP_FRONTIER_SOURCE_UNRECOVERABLE"


def test_paywall_backs_out_before_bottom_nav_slot_rebind() -> None:
    """视频详情付费遮罩无底栏时须 BACK 离开，禁止签发空底栏水平槽重绑。"""

    tab = _candidate("tab-short", "底栏短剧", scope="app", expected_page_type="video_list")
    tab = replace(
        tab,
        metadata={
            **dict(tab.metadata),
            "bottom_navigation_tab_seek": True,
            "normalized_bounds": {
                "left": 0.16,
                "top": 0.90,
                "right": 0.32,
                "bottom": 0.98,
            },
        },
    )
    source = _work(candidates=(tab,))
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(tab))
    system_back = _candidate(
        "system-back",
        "系统返回",
        scope="local",
        action_type=InteractionOperation.BACK,
    )
    blocked = replace(
        source,
        frontier=replace(
            source.frontier,
            candidates=(system_back,),
            observation=replace(
                source.frontier.observation,
                page_type="video_detail",
                page_state="overlay",
                overlay_signature="unlock-paywall",
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "business_page_identity": "page:video_detail",
                    "surface_facts": {
                        "popup_visible": True,
                        "dialog_kind": "unlock_paywall",
                        "trusted_accessibility_controls": {"bottom_navigation": []},
                    },
                },
            ),
        ),
    )

    intent = build_app_frontier_restore_intent(work=blocked, item=item)

    assert intent.parameters["candidate_id"] == system_back.candidate_id
    assert intent.parameters["app_frontier_restore_only"] is True
    assert intent.action_type is InteractionOperation.BACK
    assert "水平槽" not in str(intent.parameters.get("purpose") or "")
    assert intent.parameters.get("require_runtime_bottom_nav_rebind") is not True


def test_payment_page_backs_out_before_bottom_nav_slot_rebind() -> None:
    """payment 页型优先池内语义 BACK，禁止空底栏水平槽重绑与合成左上角点击（F02）。"""

    tab = _candidate("tab-ai", "底栏AI", scope="app", expected_page_type="activity")
    tab = replace(
        tab,
        metadata={
            **dict(tab.metadata),
            "bottom_navigation_tab_seek": True,
            "normalized_bounds": {
                "left": 0.50,
                "top": 0.90,
                "right": 0.66,
                "bottom": 0.98,
            },
        },
    )
    source = _work(candidates=(tab,))
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(tab))
    system_back = _candidate(
        "system-back-payment",
        "系统返回",
        scope="local",
        action_type=InteractionOperation.BACK,
    )
    blocked = replace(
        source,
        frontier=replace(
            source.frontier,
            candidates=(system_back,),
            observation=replace(
                source.frontier.observation,
                page_type="payment",
                page_state="overlay",
                overlay_signature="national-day-card",
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "business_page_identity": "page:payment",
                    "surface_facts": {
                        "popup_visible": True,
                        "dialog_kind": "purchase",
                        "dialog_title": "国庆尊享卡",
                        "trusted_accessibility_controls": {"bottom_navigation": []},
                    },
                },
            ),
        ),
    )

    intent = build_app_frontier_restore_intent(work=blocked, item=item)

    assert intent.action_type is InteractionOperation.BACK
    assert intent.parameters["candidate_id"] == system_back.candidate_id
    assert not str(intent.parameters["candidate_id"]).startswith(
        "forced-short-drama-top-left-back:"
    )
    assert intent.parameters.get("short_drama_top_left_back") is not True
    assert intent.parameters.get("require_runtime_bottom_nav_rebind") is not True


def test_payment_page_synthesizes_system_back_when_pool_empty() -> None:
    """payment 遮罩无底栏时用语义 BACK，禁止合成左上角坐标（F02）。"""

    from services.api.app.application.app_frontier_restoration import (
        build_app_frontier_restore_intent,
    )

    tab = _candidate("tab-ai", "底栏AI", scope="app", expected_page_type="activity")
    tab = replace(
        tab,
        metadata={
            **dict(tab.metadata),
            "bottom_navigation_tab_seek": True,
            "normalized_bounds": {
                "left": 0.50,
                "top": 0.90,
                "right": 0.66,
                "bottom": 0.98,
            },
        },
    )
    source = _work(candidates=(tab,))
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(tab))
    blocked = replace(
        source,
        frontier=replace(
            source.frontier,
            candidates=(),
            observation=replace(
                source.frontier.observation,
                page_type="payment",
                page_state="overlay",
                overlay_signature="unlock-paywall",
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "business_page_identity": "page:payment",
                    "surface_facts": {
                        "popup_visible": True,
                        "dialog_kind": "unlock_paywall",
                        "trusted_accessibility_controls": {"bottom_navigation": []},
                    },
                },
            ),
        ),
    )

    intent = build_app_frontier_restore_intent(work=blocked, item=item)

    assert intent.action_type is InteractionOperation.BACK
    assert str(intent.parameters["candidate_id"]).startswith("forced-system-back:")
    assert intent.parameters.get("short_drama_top_left_back") is not True
    assert intent.parameters["app_frontier_restore_only"] is True


def test_payment_overlay_does_not_click_close_x_when_restoring_bottom_tab() -> None:
    """付费遮罩寻底栏时禁止点关闭 X（会外跳浏览器），必须系统 BACK。"""

    tab = _candidate("tab-ai", "底栏AI", scope="app", expected_page_type="activity")
    tab = replace(
        tab,
        metadata={
            **dict(tab.metadata),
            "bottom_navigation_tab_seek": True,
            "normalized_bounds": {
                "left": 0.50,
                "top": 0.90,
                "right": 0.66,
                "bottom": 0.98,
            },
        },
    )
    close_x = _candidate("close-x", "关闭弹层下方X", scope="local")
    close_x = replace(
        close_x,
        metadata={
            **dict(close_x.metadata),
            "control_role": "dismiss",
            "purpose": "点弹层下方关闭控件",
            "expected_state_hint": "close_popup_x",
        },
    )
    source = _work(candidates=(tab, close_x))
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(tab))
    blocked = replace(
        source,
        frontier=replace(
            source.frontier,
            candidates=(close_x,),
            observation=replace(
                source.frontier.observation,
                page_type="payment",
                page_state="overlay",
                overlay_signature="unlock-paywall",
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "business_page_identity": "page:payment",
                    "surface_facts": {
                        "popup_visible": True,
                        "dialog_kind": "unlock_paywall",
                        "trusted_accessibility_controls": {"bottom_navigation": []},
                    },
                },
            ),
        ),
    )

    intent = build_app_frontier_restore_intent(work=blocked, item=item)

    assert intent.action_type is InteractionOperation.BACK
    assert "关闭" not in str(intent.semantic_target or "")
    assert "close" not in str(intent.parameters.get("candidate_id") or "").casefold()
    assert str(intent.parameters.get("candidate_id") or "").startswith(
        "forced-system-back:"
    )


def test_membership_center_restores_top_left_not_revalidated_bottom_tab() -> None:
    """会员中心即使底栏标签被重验，无视觉「<」时也须语义 BACK，禁止点底栏。"""

    tab = _candidate("tab-short", "底栏短剧", scope="app", expected_page_type="video_list")
    tab = replace(
        tab,
        metadata={
            **dict(tab.metadata),
            "bottom_navigation_tab_seek": True,
            "normalized_bounds": {
                "left": 0.18,
                "top": 0.90,
                "right": 0.30,
                "bottom": 0.98,
            },
        },
    )
    source = _work(candidates=(tab,))
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(tab))
    blocked = replace(
        source,
        frontier=replace(
            source.frontier,
            candidates=(tab,),
            observation=replace(
                source.frontier.observation,
                page_type="payment",
                page_state="stable",
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "visual_summary": "会员中心会员套餐与支付页",
                    "business_page_identity": "page:payment",
                    "surface_facts": {
                        "trusted_accessibility_controls": {"bottom_navigation": []},
                    },
                },
            ),
        ),
    )

    intent = build_app_frontier_restore_intent(work=blocked, item=item)

    assert intent.action_type is InteractionOperation.BACK
    assert str(intent.parameters["candidate_id"]).startswith("forced-system-back:")
    assert intent.parameters.get("short_drama_top_left_back") is not True
    assert intent.parameters["candidate_id"] != tab.candidate_id


def test_missing_bottom_label_does_not_issue_empty_slot_rebind() -> None:
    """同标签缺失时禁止签发空水平槽重绑；应阻断或改点当前帧可见栏。"""

    from services.api.app.application.app_frontier_restoration import (
        build_app_frontier_restore_intent,
    )

    stale = _candidate("tab-dark", "底栏暗网", scope="app", expected_page_type="content_list")
    stale = replace(
        stale,
        metadata={
            **dict(stale.metadata),
            "bottom_navigation_tab_seek": True,
            "normalized_bounds": {
                "left": 0.33,
                "top": 0.90,
                "right": 0.48,
                "bottom": 0.98,
            },
        },
    )
    current = _candidate("tab-short", "底栏短剧", scope="app", expected_page_type="video_list")
    current = replace(
        current,
        metadata={
            **dict(current.metadata),
            "bottom_navigation_tab_seek": True,
            "normalized_bounds": {
                "left": 0.16,
                "top": 0.90,
                "right": 0.32,
                "bottom": 0.98,
            },
        },
    )
    source = _work(candidates=(stale,))
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(stale))
    blocked = replace(
        source,
        frontier=replace(
            source.frontier,
            candidates=(current,),
            observation=replace(
                source.frontier.observation,
                page_type="home",
                page_state="stable",
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "required_page_types": ["home", "video_list"],
                    "covered_page_types": ["home"],
                    "visited_bottom_navigation_labels": ["主页"],
                    "package_name": "dark.wwcskrpojs.ebjhyebjbl",
                    "app_curriculum_progress": {"playbook_id": "hjll"},
                    "surface_facts": {
                        "trusted_accessibility_controls": {
                            "bottom_navigation": [
                                {"label": "短剧", "selected": False},
                                {"label": "主页", "selected": True},
                            ],
                            "bottom_navigation_count": 2,
                        }
                    },
                },
            ),
        ),
    )
    intent = build_app_frontier_restore_intent(work=blocked, item=item)
    assert intent.parameters.get("require_runtime_bottom_nav_rebind") is not True
    assert "水平槽" not in str(intent.parameters.get("purpose") or "")
    assert intent.parameters["candidate_id"] == current.candidate_id


def test_substitute_skips_visited_selected_and_exhausted_bottom_tabs() -> None:
    """substitute 不得再点已访／已选中／空点耗尽栏，应改点其它未访栏。"""

    from services.api.app.application.app_frontier_restoration import (
        build_app_frontier_restore_intent,
    )
    from services.api.app.application.app_semantic_progress import (
        APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS,
        APP_BOTTOM_NAV_SEEK_ATTEMPT_THRESHOLD,
    )

    stale = _candidate("tab-shequ", "底栏社区", scope="app", expected_page_type="post_list")
    stale = replace(
        stale,
        metadata={
            **dict(stale.metadata),
            "bottom_navigation_tab_seek": True,
            "playbook_tab_index": 4,
            "normalized_bounds": {
                "left": 0.66,
                "top": 0.90,
                "right": 0.82,
                "bottom": 0.98,
            },
        },
    )
    short = _candidate("tab-short", "底栏短剧", scope="app", expected_page_type="video_list")
    short = replace(
        short,
        metadata={
            **dict(short.metadata),
            "bottom_navigation_tab_seek": True,
            "playbook_tab_index": 1,
            "normalized_bounds": {
                "left": 0.16,
                "top": 0.90,
                "right": 0.32,
                "bottom": 0.98,
            },
        },
    )
    shequ_again = _candidate(
        "tab-shequ-now", "底栏社区", scope="app", expected_page_type="post_list"
    )
    shequ_again = replace(
        shequ_again,
        metadata={
            **dict(shequ_again.metadata),
            "bottom_navigation_tab_seek": True,
            "playbook_tab_index": 4,
            "normalized_bounds": {
                "left": 0.66,
                "top": 0.90,
                "right": 0.82,
                "bottom": 0.98,
            },
        },
    )
    source = _work(candidates=(stale,))
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(stale))
    blocked = replace(
        source,
        frontier=replace(
            source.frontier,
            candidates=(shequ_again, short),
            observation=replace(
                source.frontier.observation,
                page_type="home",
                page_state="stable",
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "required_page_types": ["home", "video_list", "post_list"],
                    "covered_page_types": ["home"],
                    "visited_bottom_navigation_labels": ["主页"],
                    APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS: {
                        "社区": APP_BOTTOM_NAV_SEEK_ATTEMPT_THRESHOLD,
                    },
                    "package_name": "dark.wwcskrpojs.ebjhyebjbl",
                    "app_curriculum_progress": {"playbook_id": "hjll"},
                    "surface_facts": {
                        "pixel_selected_labels": ["主页"],
                        "trusted_accessibility_controls": {
                            "bottom_navigation": [
                                {"label": "主页", "selected": True},
                                {"label": "短剧", "selected": False},
                                {"label": "社区", "selected": False},
                            ],
                            "bottom_navigation_count": 3,
                        },
                    },
                },
            ),
        ),
    )
    intent = build_app_frontier_restore_intent(work=blocked, item=item)
    assert intent.parameters["candidate_id"] == short.candidate_id


def test_bottom_nav_restore_prefers_profile_credential_over_popup_x() -> None:
    """已到「我的」遇账号凭证时，restore 须点保存凭证，禁止清障点下方 X。"""

    from services.api.app.application.app_semantic_progress import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
    )

    pkg = "dark.wwcskrpojs.ebjhyebjbl"
    sibling = _candidate("tab-ai", "底栏AI", scope="app", expected_page_type="home")
    sibling = replace(
        sibling,
        metadata={
            **dict(sibling.metadata),
            "bottom_navigation_tab_seek": True,
            "playbook_tab_index": 3,
            "normalized_bounds": {
                "left": 0.50,
                "top": 0.90,
                "right": 0.66,
                "bottom": 0.98,
            },
        },
    )
    source = _work(
        candidates=(sibling,),
        page_identity=f"{pkg}|.MainActivity|home|depth:1|scope:nav:main",
        location=f"app://{pkg}/.MainActivity",
        app_goal=AppExplorationGoal(
            required_page_types=("home", "profile"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=2,
            allowed_packages=(pkg,),
        ),
    )
    source = replace(
        source,
        boundaries=replace(source.boundaries, allowed_hosts=(pkg,)),
    )
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(sibling))
    close_x = _candidate("popup-x", "关闭弹窗下方X", scope="local")
    close_x = replace(
        close_x,
        metadata={
            **dict(close_x.metadata),
            "control_role": "dismiss",
            "obstruction_signature": "popup:ad",
            "normalized_bounds": {
                "left": 0.45,
                "top": 0.86,
                "right": 0.55,
                "bottom": 0.92,
            },
        },
    )
    blocked = replace(
        source,
        frontier=replace(
            source.frontier,
            candidates=(close_x,),
            observation=replace(
                source.frontier.observation,
                page_type="profile",
                page_state="popup",
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "package_name": pkg,
                    "visual_summary": "账号凭证 保存凭证 我已保存 请勿删除此凭证",
                    VISITED_BOTTOM_NAVIGATION_LABELS_KEY: ["主页", "我的"],
                    "surface_facts": {"dialog_kind": "account_credential"},
                },
            ),
        ),
    )

    intent = build_app_frontier_restore_intent(work=blocked, item=item)

    assert intent.semantic_target == "保存凭证"
    assert "我已保存" in intent.purpose or "保存凭证" in intent.purpose
    assert "下方 X" in intent.purpose
    assert intent.parameters.get("app_frontier_restore_only") is True
    assert str(intent.parameters.get("candidate_id") or "").startswith(
        "forced-profile-credential-save:"
    )
    # 注入候选必须能回绑，否则实跑会 APP_FRONTIER_RESTORATION_CANDIDATE_UNBOUND。
    rebound = bind_app_frontier_restoration_candidate(work=blocked, decision=intent)
    assert any(
        item.candidate_id == intent.parameters["candidate_id"]
        for item in rebound.frontier.candidates
    )
    assert any(
        item.metadata.get("profile_credential_save") is True
        for item in rebound.frontier.candidates
    )


def test_non_bottom_nav_restore_prefers_profile_credential_over_popup_x() -> None:
    """已到「我的」后，非底栏 sibling 也不得把凭证弹层当广告点下方 X。"""

    from services.api.app.application.app_semantic_progress import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
    )

    pkg = "dark.wwcskrpojs.ebjhyebjbl"
    sibling = _candidate("home-latest", "上方分类最新", scope="local")
    sibling = replace(
        sibling,
        metadata={
            **dict(sibling.metadata),
            "home_top_category_tab_seek": True,
            "normalized_bounds": {
                "left": 0.04,
                "top": 0.12,
                "right": 0.16,
                "bottom": 0.18,
            },
        },
    )
    source = _work(
        candidates=(sibling,),
        page_identity=f"{pkg}|.MainActivity|home|depth:1|scope:nav:main",
        location=f"app://{pkg}/.MainActivity",
        app_goal=AppExplorationGoal(
            required_page_types=("home", "profile"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=2,
            allowed_packages=(pkg,),
        ),
    )
    source = replace(
        source,
        boundaries=replace(source.boundaries, allowed_hosts=(pkg,)),
    )
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(sibling))
    close_x = _candidate("popup-x", "关闭弹层下方X", scope="local")
    close_x = replace(
        close_x,
        metadata={
            **dict(close_x.metadata),
            "control_role": "dismiss",
            "obstruction_signature": "popup:ad",
            "normalized_bounds": {
                "left": 0.45,
                "top": 0.86,
                "right": 0.55,
                "bottom": 0.92,
            },
        },
    )
    blocked = replace(
        source,
        frontier=replace(
            source.frontier,
            candidates=(close_x,),
            observation=replace(
                source.frontier.observation,
                page_type="profile",
                page_state="modal",
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "package_name": pkg,
                    "visual_summary": "我的页上账号凭证弹窗含二维码",
                    "ocr_text": "账号凭证 保存凭证 我已保存 请勿删除此凭证",
                    VISITED_BOTTOM_NAVIGATION_LABELS_KEY: [
                        "主页",
                        "短剧",
                        "暗网",
                        "ai",
                        "社区",
                        "我的",
                    ],
                    "surface_facts": {"dialog_kind": "account_credential"},
                },
            ),
        ),
    )

    intent = build_app_frontier_restore_intent(work=blocked, item=item)

    assert intent.semantic_target == "保存凭证"
    assert intent.parameters.get("candidate_id") != close_x.candidate_id
    assert str(intent.parameters.get("candidate_id") or "").startswith(
        "forced-profile-credential-save:"
    )


def test_completed_profile_tab_blocks_stale_restore_sibling() -> None:
    """课纲已到「我的」且底栏硬覆盖完成：旧 sibling 必须阻断，才能收口。"""

    from services.api.app.application.app_frontier_restoration import (
        AppFrontierRestorationError,
    )
    from services.api.app.application.app_semantic_progress import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
    )

    pkg = "dark.wwcskrpojs.ebjhyebjbl"
    sibling = _candidate("popup-x", "关闭弹层下方X", scope="local")
    source = _work(
        candidates=(sibling,),
        page_identity=f"{pkg}|.MainActivity|profile|depth:1|scope:nav:main",
        location=f"app://{pkg}/.MainActivity",
        app_goal=AppExplorationGoal(
            required_page_types=("home", "profile"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=2,
            allowed_packages=(pkg,),
        ),
    )
    source = replace(
        source,
        boundaries=replace(source.boundaries, allowed_hosts=(pkg,)),
    )
    item = _frontier_item(source, with_app_frontier_fingerprint_strategy(sibling))
    done = replace(
        source,
        frontier=replace(
            source.frontier,
            candidates=(sibling,),
            observation=replace(
                source.frontier.observation,
                page_type="profile",
                page_state="stable",
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "package_name": pkg,
                    "visual_summary": "我的页个人中心",
                    "app_curriculum_progress": {"playbook_id": "hjll"},
                    VISITED_BOTTOM_NAVIGATION_LABELS_KEY: [
                        "主页",
                        "短剧",
                        "暗网",
                        "ai",
                        "社区",
                        "我的",
                    ],
                },
            ),
        ),
    )

    with pytest.raises(AppFrontierRestorationError) as error:
        build_app_frontier_restore_intent(work=done, item=item)
    assert error.value.retryable is False
    assert "我的" in str(error.value)
