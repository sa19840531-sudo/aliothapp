"""主体补图使用真实 Chrome、Coordinator、Writer、PG 台账与报告消费链。"""

import asyncio
import json
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from services.api.app.application.capture_coordination import CaptureCoordinationCommand
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.interaction_command_validation import InteractionCommandValidator
from services.api.app.application.interaction_runtime import InMemoryActionEvidenceBuffer
from services.api.app.application.page_capture_persistence import PageCapturePersistenceService
from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
    WebMenuLedgerTerminalRecord,
)
from services.api.app.application.web_exploration_workflow import WebCaptureStage
from services.api.app.application.web_logical_target import (
    WebLogicalTargetClassifier,
    WebLogicalTargetPrimaryCapture,
)
from services.api.app.application.web_menu_exploration import WebMenuExplorer, WebMenuVisitResult
from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_page_readiness import _readiness_intent
from services.api.app.application.web_page_type_assessment import WebPageTypeAssessmentRecorder
from services.api.app.application.web_primary_content_capture import WebPrimaryContentCaptureService
from services.api.app.application.web_primary_content_diagnostic import primary_content_diagnostic
from services.api.app.application.web_runtime_composition import WebV2Runtime
from services.api.app.application.web_runtime_evidence import (
    WebReadyCaptureReaderAdapter,
    WebRuntimeEvidenceSource,
)
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.domain.page import WebSiteMenuCatalog
from services.api.app.infrastructure.browser.capture_runtime import (
    BrowserCaptureWriter,
    BrowserInteractionExecutor,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    PlaywrightPageCaptureBrowser,
)
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)
from services.api.app.infrastructure.browser.ui_snapshot_reader import (
    PostgresTrustedUiSnapshotReader,
)
from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
    WebDesktopCapturePolicyBrowser,
)
from services.api.app.infrastructure.db.action_execution_repository import (
    PostgresActionExecutionRepository,
)
from services.api.app.infrastructure.db.command_scope_reader import PostgresTaskCommandScopeReader
from services.api.app.infrastructure.db.page_capture_repository import PostgresPageCaptureRepository
from services.api.app.infrastructure.db.session_command_gate import PostgresSessionCommandGate
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.app.infrastructure.db.web_logical_target_repository import (
    PostgresWebLogicalTargetRepository,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_capture_coordinator import RecordingEvidenceExitGate
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _command,
    _site,
)
from services.api.tests.test_web_logical_target_postgres import target_database
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)
from services.api.tests.web_frozen_assessment_support import frozen_assessment_options

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_TEST_CHROME_EXECUTABLE"),
    reason="须显式提供本轮隔离PG与受控Chrome，不用主库或外站",
)


class ControlledBrowser(PlaywrightPageCaptureBrowser):
    """只替换浏览器启动路径和外部站点传输，采样/交互/采集全部真实执行。"""

    def __init__(self, *, card_height=900):
        super().__init__()
        self.card_height = card_height

    async def _launch_browser(self, playwright):
        executable = os.environ["ALIOTH_TEST_CHROME_EXECUTABLE"]
        assert Path(executable).is_file()
        return await playwright.chromium.launch(executable_path=executable, headless=True)

    async def _create_session(self, async_playwright, *, request):
        session = await super()._create_session(async_playwright, request=request)
        await session.context.route(
            "**/*",
            lambda route: route.fulfill(
                content_type="text/html; charset=utf-8",
                body='<html><title>受控文章</title><body style="margin:0">'
                '<aside class="advertisement" style="height:1400px;background:#ddd">'
                '中性广告占位</aside><main id="body" style="height:2000px">'
                '<nav style="position:fixed;top:0;height:80px;width:100%;background:white;'
                'z-index:2"><a href="/menu/next">下一菜单</a> '
                '<a href="/menu/other">其他栏目</a></nav>'
                f'<section><article class="card" style="height:{self.card_height}px">'
                '<a href="/article/one">第一篇文章</a>'
                '<p>首页第一项真实内容摘要，不是导航或加载占位。</p></article>'
                f'<article class="card" style="height:{self.card_height}px">'
                '<a href="/article/two">第二篇文章</a>'
                '<p>首页第二项真实内容摘要，不是广告或页脚。</p></article></section></main>'
                '<footer style="height:800px">页脚</footer></body></html>',
            ),
        )
        return session


@pytest.mark.asyncio
@pytest.mark.parametrize("known,card_height,expected", [
    (True, 900, "captured"), (False, 900, "captured"), (False, 88, "blocked"),
], ids=["True", "False", "dense-blocked"])
async def test_real_primary_chain_keeps_baseline_and_report_primary(tmp_path, known, card_height, expected):
    async with target_database() as (connection, pool, _other, ids):
        await connection.execute(
            "UPDATE qc_agent_run SET input_context=$2::jsonb WHERE id=$1::uuid",
            ids["run"],
            json.dumps({"task_config": {"_task_target": {"platform": "web"}}}),
        )
        store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
        persistence = PageCapturePersistenceService(
            repository=PostgresPageCaptureRepository(pool=pool), artifact_store=store
        )
        browser = WebDesktopCapturePolicyBrowser(browser=ControlledBrowser(card_height=card_height))
        snapshot_reader = PostgresTrustedUiSnapshotReader(pool=pool, artifact_store=store)
        frozen_options = await frozen_assessment_options(connection, ids)
        repository = PostgresWebCaptureEvidenceRepository(pool=pool, **frozen_options)
        targets = PostgresWebLogicalTargetRepository(pool=pool)
        scope = dict(task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"])
        deadline = datetime.now(UTC) + timedelta(minutes=3)
        source = WebRuntimeEvidenceSource(
            **scope, deadline=deadline, index_reader=repository, snapshot_reader=snapshot_reader
        )
        try:
            # 初始被动基线自身真实采样，并经严格 Writer 入库；不预分类任何动作前帧。
            initial = await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.GOTO,
                    url="https://example.test/",
                    session_id=ids["session"],
                    web_capture_stage="web_page_probe",
                    viewport_width=1920,
                    viewport_height=1080,
                    screen_width=1920,
                    screen_height=1080,
                    device_scale_factor=1.0,
                    is_mobile=False,
                    has_touch=False,
                )
            )
            await connection.execute(
                "UPDATE qc_surface_session SET lease_token=$2::uuid, lease_owner='t7-bootstrap', "
                "lease_expires_at=now()+interval '3 minutes',command_sequence=1 "
                "WHERE session_id=$1",
                ids["session"],
                str(uuid4()),
            )
            saved = await persistence.persist(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                result=initial,
                capture_role="primary",
                surface_session_id=ids["session"],
                session_command_sequence=1,
                finalization_epoch=0,
                inherit_parent_semantics=False,
            )
            baseline = saved.page_capture_id
            await connection.execute(
                "UPDATE qc_surface_session SET current_capture_id=$2::uuid,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL WHERE session_id=$1",
                ids["session"],
                baseline,
            )

            class RecordedClassifier:
                """只替换外部模型结论；录制、占有、补判与物化全部使用实际生产实现。"""

                calls = 0

                async def classify(self, ready):
                    self.calls += 1
                    page_type = "home" if known or self.calls > 1 else None
                    assessment = WebPageAssessment(
                        page_type=page_type,
                        confidence=0.95 if page_type else 0.2,
                        evidence=("受控根URL与重复内容卡片结构",),
                    )
                    await WebPageTypeAssessmentRecorder(
                        repository=repository, **frozen_options
                    ).persist(
                        **scope, capture=ready, assessment=assessment
                    )
                    return assessment

            model = RecordedClassifier()
            classifier = WebLogicalTargetClassifier(classifier=model, repository=targets, **scope)
            validator = InteractionCommandValidator(
                scope_reader=PostgresTaskCommandScopeReader(pool=pool)
            )
            buffer = InMemoryActionEvidenceBuffer()
            coordinator = CaptureCoordinator(
                capture_writer=BrowserCaptureWriter(
                    browser=browser, persistence=persistence, evidence_buffer=buffer
                ),
                interaction_executor=BrowserInteractionExecutor(
                    browser=browser,
                    target_resolver=UiSnapshotSemanticTargetResolver(
                        snapshot_reader=snapshot_reader
                    ),
                    evidence_buffer=buffer,
                    command_validator=validator,
                ),
                evidence_exit_gate=RecordingEvidenceExitGate(),
                session_command_gate=PostgresSessionCommandGate(
                    repository=PostgresSurfaceSessionRepository(pool=pool),
                    lease_owner="t7-runtime",
                    session_owner="web-v2-workflow",
                ),
                action_execution_repository=PostgresActionExecutionRepository(pool=pool),
                command_validator=validator,
            )
            probed = await coordinator.coordinate(
                CaptureCoordinationCommand(
                    agent_run_id=ids["run"],
                    intent=_readiness_intent(
                        evidence=await source.read_page(capture_id=baseline),
                        action_type=InteractionOperation.OBSERVE,
                        semantic_target="采集页面探测帧",
                        purpose="受控首页探测",
                        stage=WebCaptureStage.PAGE_PROBE,
                        candidate=None,
                    ),
                )
            )
            probe = probed.after_capture.capture_id
            observed = await coordinator.coordinate(
                CaptureCoordinationCommand(
                    agent_run_id=ids["run"],
                    intent=_readiness_intent(
                        evidence=await source.read_page(capture_id=probe),
                        action_type=InteractionOperation.OBSERVE,
                        semantic_target="采集无遮挡页面基线",
                        purpose="冻结受控首页基线",
                        stage=WebCaptureStage.PAGE_READY_BASELINE,
                        candidate=None,
                    ),
                )
            )
            baseline = observed.after_capture.capture_id
            baseline_index = await repository.read(task_id=ids["task"], capture_id=baseline)
            (tmp_path / "baseline-readiness.json").write_text(
                json.dumps(baseline_index.pipeline_metrics, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            service = WebLogicalTargetPrimaryCapture(
                service=WebPrimaryContentCaptureService(
                    coordinator=coordinator, evidence_source=source, metadata_writer=repository
                ),
                repository=targets,
            )
            args = dict(
                **scope,
                capture_id=baseline,
                page_url="https://example.test/",
                page_type="home",
                deadline=deadline,
            )
            if known:
                # 起始探测与就绪帧已经真实落库；仅复用这两个准备阶段回执，不替换补图链。
                primary_results = []

                async def capture_primary(**kwargs):
                    captured = await service.capture(**kwargs)
                    primary_results.append(captured)
                    return captured

                primary_call = AsyncMock(side_effect=capture_primary)
                runtime = WebV2Runtime(
                    bootstrapper=SimpleNamespace(start=AsyncMock(return_value=probed)),
                    capture_runtime=object(),
                    navigation_candidate_source=browser,
                    age_evidence_reader=SimpleNamespace(read=source.read_age),
                    age_gate_handler=SimpleNamespace(
                        handle=AsyncMock(
                            return_value=SimpleNamespace(
                                after_capture_id=None,
                                gate_disappeared=False,
                            )
                        )
                    ),
                    page_readiness=SimpleNamespace(
                        prepare=AsyncMock(
                            return_value=SimpleNamespace(
                                status="ready",
                                ready_capture_id=baseline,
                                evidence_capture_ids=(probe, baseline),
                                dismissed_layers=(),
                            )
                        )
                    ),
                    home_ready_capture_reader=WebReadyCaptureReaderAdapter(source=source),
                    page_type_classifier=classifier,
                    primary_content_capture=SimpleNamespace(capture=primary_call),
                )
                home = await asyncio.wait_for(
                    runtime.prepare_home(
                        _command(ids),
                        adult_self_attestation=None,
                    ),
                    timeout=45,
                )
                result = primary_results[0]
                assert primary_call.await_count == 1
                assert home.ready_capture_id == baseline
                candidate = next(
                    c
                    for c in browser.navigation_candidates(ids["session"])
                    if c.url.endswith("/menu/next")
                )
                # 先运行实际 T5.find 暴露旧 source，而不是只断言新增字段缺失。
                assert (
                    await targets.find(
                        **scope, source_capture_id=candidate.source_capture_id, url=candidate.url
                    )
                    is None
                )
                assert home.navigation_current_capture_id == result.evidence_capture_id
                assert candidate.source_capture_id == home.navigation_current_capture_id
                # 实际 Explorer 消费独立初始动作来源，HOME 分类/时间线仍保留 baseline。
                menu_site = replace(
                    _site(),
                    entries=(
                        _site().entries[0],
                        replace(
                            _site().entries[1],
                            href="/menu/next",
                            canonical_url=candidate.url,
                            menu_path=("下一菜单",),
                        ),
                    ),
                )
                visited_sources = []

                async def visit_next(**kwargs):
                    visited_sources.append(kwargs["source_capture_id"])
                    assert (
                        await targets.claim(
                            **scope,
                            source_capture_id=kwargs["source_capture_id"],
                            url=candidate.url,
                        )
                    )["created"]
                    return WebMenuVisitResult(
                        final_capture_id=None,
                        page_type=None,
                        status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                    )

                registry = SimpleNamespace(
                    version="t7-handoff",
                    catalog_content_sha256="b" * 64,
                    # 此子用例仅检查已列菜单的来源交接，不冒充完整动态发现验收。
                    discovery_policy=WebDiscoveryPolicy(),
                    resolve_site_context=lambda url: SimpleNamespace(site=replace(menu_site, catalog_version="t7-handoff"), content_sha256="b" * 64),
                )
                explorer = WebMenuExplorer(
                    navigation_context=registry,
                    candidate_source=browser,
                    page_visitor=SimpleNamespace(visit=visit_next),
                    logical_target_repository=targets,
                )
                summary = await replace(runtime, explorer=explorer).explore(
                    _command(ids),
                    initial_capture_id=home.ready_capture_id,
                    initial_page_type=home.page_type,
                    initial_navigation_capture_id=home.navigation_current_capture_id,
                )
                assert visited_sources == [result.evidence_capture_id]
                assert summary.menu_ledger[0].final_capture_id == baseline
                assert summary.page_timeline == (baseline,)
            else:
                await classifier.classify(
                    await source.read_ready(capture_id=baseline, page_role="home")
                )
                result = await asyncio.wait_for(service.capture(**args), timeout=45)
            (tmp_path / "primary-result.json").write_text(
                json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8",
            )
            assert result.status == expected, asdict(result)
            if expected == "blocked":
                # 真实短卡片居中后仍留近半屏广告，不能改准入阈值或多滚动来凑正式页面。
                assert result.reason_code == "WEB_PRIMARY_CONTENT_VIEWPORT_BLOCKED"
                assert result.after_ad_coverage > 0.35
                assert result.scroll_count == 2
                assert result.evidence_capture_id is None
                assert model.calls == 1
                return
            primary = result.evidence_capture_id
            assert primary != baseline and result.scroll_count == 1
            ready = await source.read_ready(capture_id=primary, page_role="home")
            assert (
                ready.web_capture_stage == ready.capture_purpose == "web_primary_content_evidence"
            )
            after = await connection.fetchrow(
                "SELECT parent_capture_id::text,action_execution_id "
                "FROM qc_capture WHERE id=$1::uuid",
                primary,
            )
            action = await connection.fetchrow(
                "SELECT requested_source_capture_id::text,source_capture_id::text,"
                "result_capture_id::text FROM qc_action_execution WHERE action_execution_id=$1",
                after["action_execution_id"],
            )
            assert action["requested_source_capture_id"] == baseline
            assert after["parent_capture_id"] == action["source_capture_id"] != baseline
            assert action["result_capture_id"] == primary
            if not known:
                assert (await classifier.classify(ready)).page_type == "home"
            assert model.calls == (1 if known else 2)
            await classifier.classify(ready)
            assert model.calls == (1 if known else 2)
            assessments = await connection.fetch(
                "SELECT page_capture_id::text AS capture_id FROM qc_h5_page_type_assessment "
                "WHERE agent_run_id=$1::uuid",
                ids["run"],
            )
            assert {r["capture_id"] for r in assessments} == (
                {baseline} if known else {baseline, primary}
            )
            stored_types = await connection.fetch(
                "SELECT id::text,page_type FROM qc_page_capture WHERE id=ANY($1::uuid[])",
                [baseline, primary],
            )
            assert {r["id"]: r["page_type"] for r in stored_types} == (
                {baseline: "home", primary: None}
                if known
                else {baseline: None, primary: "home"}
            )
            resumed = await WebLogicalTargetPrimaryCapture(
                service=object(), repository=targets
            ).capture(**dict(args, capture_id=primary))
            assert resumed.scroll_count == 0
            assert resumed.evidence_capture_id == primary
            assert resumed.interaction_outcomes == result.interaction_outcomes
            assert resumed.attempts == result.attempts
            assert resumed.reused and resumed.original_scroll_count == 1

            site = _site()
            site = WebSiteMenuCatalog(
                site_id=site.site_id,
                origin=site.origin,
                navigation_evidence=site.navigation_evidence,
                entries=(site.entries[0],),
            )
            budget = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
            command = _command(ids)
            await budget.bootstrap(
                command, site, catalog_version="t7-controlled", catalog_sha256="a" * 64
            )
            home_record = WebMenuLedgerTerminalRecord(
                    **scope,
                    site_id=site.site_id,
                    menu_entry_id="home",
                    status=WebMenuTerminalStatus.HOME_BASELINE,
                    menu_probe_capture_id=probe,
                    menu_ready_capture_id=baseline if known else primary,
                    menu_page_type="home",
                    diagnostic={
                        "primary_content_diagnostics": [primary_content_diagnostic(result)]
                    },
                )
            # 新协议不允许旧夹具用文章详情冒充HOME；拒绝前后账本保持原样。
            before_ledger = await connection.fetchval(
                "SELECT row_to_json(entry)::text FROM qc_web_menu_ledger_entry entry "
                "WHERE agent_run_id=$1::uuid AND menu_entry_id='home'", ids["run"],
            )
            with pytest.raises(WebExplorationBudgetLedgerConflictError):
                await budget.finalize(replace(home_record, menu_page_type="content_detail"))
            assert await connection.fetchval(
                "SELECT row_to_json(entry)::text FROM qc_web_menu_ledger_entry entry "
                "WHERE agent_run_id=$1::uuid AND menu_entry_id='home'", ids["run"],
            ) == before_ledger
            await budget.finalize(home_record)
            await budget.seal(command=command, site_id=site.site_id)
            snapshot = await repository.list_for_task(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                access_scope=AccessScope(
                    actor_user_id=ids["user"],
                    tenant_id=ids["tenant"],
                    permissions=frozenset({"taskObjects:read:any"}),
                ),
            )
            bundle = build_web_evidence_bundle(snapshot)
            (tmp_path / "formal-bundle.json").write_text(
                json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
            )
            assert bundle["status"] == "evidence_ready", json.dumps(
                {
                    "reasons": bundle["reason_codes"],
                    "entries": bundle["entries"],
                    "captures": [
                        {
                            k: c.get(k)
                            for k in (
                                "capture_id",
                                "capture_role",
                                "capture_purpose",
                                "logical_page_identity_status",
                                "web_evidence_admission",
                                "primary_content_capture",
                            )
                        }
                        for c in snapshot["captures"]
                    ],
                },
                ensure_ascii=False,
            )
            assert len(bundle["formal_pages"]) == 1
            assert bundle["formal_pages"][0]["primary_capture_id"] == primary
            assert baseline in bundle["formal_pages"][0]["observation_capture_ids"]
            assert bundle["formal_pages"][0]["page_type"] == "home"
            diagnostic = bundle["entries"][0]["primary_content_diagnostics"][0]
            assert diagnostic["attempts"][0]["before_capture_id"] == after["parent_capture_id"]
            assert diagnostic["attempts"][0]["action_execution_id"] == after["action_execution_id"]
            assert diagnostic["interaction_outcomes"][0]["after_capture_id"] == primary
            assert diagnostic["scroll_count"] == 1
            primary_metadata = next(c for c in snapshot["captures"] if c["capture_id"] == primary)[
                "primary_content_capture"
            ]
            assert primary_metadata["parent_capture_id"] == baseline
            assert primary_metadata["direct_parent_capture_id"] == after["parent_capture_id"]
            from services.api.app.infrastructure.db.web_capture_evidence_repository import (
                WebCapturePageTypePersistenceError,
            )

            with pytest.raises(WebCapturePageTypePersistenceError):
                await repository.mark_primary_content_capture(
                    **scope,
                    capture_id=primary,
                    parent_capture_id=probe,
                    direct_parent_capture_id=after["parent_capture_id"],
                    logical_page_id=result.logical_page_id,
                    capture_purpose="web_primary_content_evidence",
                    target_candidate_id=result.target_candidate_id,
                    scroll_count=1,
                    before_ad_coverage=result.before_ad_coverage,
                    after_ad_coverage=result.after_ad_coverage,
                )
            original_metadata = await connection.fetchval(
                "SELECT metadata::text FROM qc_capture WHERE id=$1::uuid", primary
            )
            original_page_metadata = await connection.fetchval(
                "SELECT metadata::text FROM qc_page_capture WHERE id=$1::uuid", primary
            )
            # 使用合法收口生命周期，晚到的主体元数据不得在 T5 拒绝前先落库。
            await connection.execute(
                "UPDATE qc_task SET finalization_state='finalizing',"
                "exploration_draining_at=now(),session_draining_at=now() WHERE id=$1::uuid",
                ids["task"],
            )
            try:
                with pytest.raises(WebCapturePageTypePersistenceError):
                    await repository.mark_primary_content_capture(
                        **scope,
                        capture_id=primary,
                        parent_capture_id=baseline,
                        direct_parent_capture_id=after["parent_capture_id"],
                        logical_page_id=result.logical_page_id,
                        capture_purpose="web_primary_content_evidence",
                        target_candidate_id="late-after-finalizing",
                        scroll_count=1,
                        before_ad_coverage=result.before_ad_coverage,
                        after_ad_coverage=result.after_ad_coverage,
                    )
                assert (
                    await connection.fetchval(
                        "SELECT metadata::text FROM qc_capture WHERE id=$1::uuid", primary
                    )
                    == original_metadata
                )
                assert (
                    await connection.fetchval(
                        "SELECT metadata::text FROM qc_page_capture WHERE id=$1::uuid", primary
                    )
                    == original_page_metadata
                )
            finally:
                await connection.execute(
                    "UPDATE qc_task SET finalization_state='open',exploration_draining_at=NULL,"
                    "session_draining_at=NULL WHERE id=$1::uuid",
                    ids["task"],
                )
            try:
                for invalid_epoch in (None, "not-an-epoch", "0", True, 1e50):
                    await connection.execute(
                        "UPDATE qc_capture SET metadata=jsonb_set(metadata,"
                        "'{finalization_epoch}',$2::jsonb) WHERE id=$1::uuid",
                        primary,
                        json.dumps(invalid_epoch),
                    )
                    invalid = await repository.list_for_task(
                        task_id=ids["task"],
                        agent_run_id=ids["run"],
                        access_scope=AccessScope(
                            actor_user_id=ids["user"],
                            tenant_id=ids["tenant"],
                            permissions=frozenset({"taskObjects:read:any"}),
                        ),
                    )
                    assert build_web_evidence_bundle(invalid)["status"] == "needs_review"
                    if invalid_epoch is None:
                        with pytest.raises(RuntimeError, match="IDENTITY"):
                            await WebReadyCaptureReaderAdapter(source=source).read_current(
                                page_role="home"
                            )
            finally:
                await connection.execute(
                    "UPDATE qc_capture SET metadata=$2::jsonb WHERE id=$1::uuid",
                    primary,
                    original_metadata,
                )
            # 任务和 Run 单独停止运行也不可追加主体元数据，正向链仍保持原值。
            table, row_id = ("qc_task", ids["task"]) if known else ("qc_agent_run", ids["run"])
            await connection.execute(
                f"UPDATE {table} SET status='failed' WHERE id=$1::uuid", row_id
            )
            with pytest.raises(WebCapturePageTypePersistenceError):
                await repository.mark_primary_content_capture(
                    **scope,
                    capture_id=primary,
                    parent_capture_id=baseline,
                    direct_parent_capture_id=after["parent_capture_id"],
                    logical_page_id=result.logical_page_id,
                    capture_purpose="web_primary_content_evidence",
                    target_candidate_id="late-after-stopped",
                    scroll_count=1,
                    before_ad_coverage=result.before_ad_coverage,
                    after_ad_coverage=result.after_ad_coverage,
                )
            assert (
                await connection.fetchval(
                    "SELECT metadata::text FROM qc_capture WHERE id=$1::uuid", primary
                )
                == original_metadata
            )
            assert (
                await connection.fetchval(
                    "SELECT metadata::text FROM qc_page_capture WHERE id=$1::uuid", primary
                )
                == original_page_metadata
            )
            # 终态不回退；由本例隔离数据库夹具删除自建样本，尊重真实生命周期触发器。
        finally:
            await browser.close_session(ids["session"])
