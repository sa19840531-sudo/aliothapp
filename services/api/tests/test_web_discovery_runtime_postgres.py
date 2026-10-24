"""真实 V2 冻结、Chrome、Coordinator、PG 预算与 Runtime 收口贯通。"""

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.api.app.api.v2.schemas import V2TaskSubmissionRequest
from services.api.app.application import web_runtime_composition as composition
from services.api.app.application.v2_task_entries import ExplorationTaskV2EntryService
from services.api.app.application.web_age_gate import WebAgeGateAssessment
from services.api.app.application.web_evidence_bundle import build_web_evidence_bundle
from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_page_readiness import WebObstructionAssessment
from services.api.app.core.config import Settings
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.browser.surface_driver import PlaywrightSurfaceDriverAdapter
from services.api.app.infrastructure.db import web_logical_target_repository as targets_module
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    PostgresWebRunPolicySnapshotRepository,
)
from services.api.tests.test_v2_task_entrypoints import (
    TEST_ACCESS_SCOPE,
    RecordingSubmissionService,
    _web_task_payload,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _command,
    _database,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _upgrade_isolated_postgres as _upgrade_isolated_postgres,
)
from services.api.tests.test_web_primary_content_capture_postgres import (
    BrowserCaptureWriter,
    BrowserInteractionExecutor,
    CaptureCoordinator,
    InMemoryActionEvidenceBuffer,
    InteractionCommandValidator,
    LocalEvidenceArtifactStore,
    PageCapturePersistenceService,
    PlaywrightPageCaptureBrowser,
    PostgresActionExecutionRepository,
    PostgresPageCaptureRepository,
    PostgresSessionCommandGate,
    PostgresSurfaceSessionRepository,
    PostgresTaskCommandScopeReader,
    PostgresTrustedUiSnapshotReader,
    PostgresWebCaptureEvidenceRepository,
    PostgresWebLogicalTargetRepository,
    RecordingEvidenceExitGate,
    UiSnapshotSemanticTargetResolver,
    WebDesktopCapturePolicyBrowser,
)
from services.api.tests.test_web_runtime_composition import _web_record

requires_database = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_TEST_CHROME_EXECUTABLE"),
    reason="须显式隔离PG及受控Chrome；不连接主库或外站",
)


def _runtime_provider(builder, model, policies):
    return composition.PostgresWebRuntimeProvider(
        capture_runtime_builder=builder, assessment_agent=model,
        policy_repository=policies,
    )


def _request_payload():
    payload = _web_task_payload(url="https://example.test/")
    payload["target"]["exploration_target"] = {
        "required_page_types": ["content_detail"],
    }
    return payload


def test_runtime_fixture_constructor_is_valid_without_postgres():
    assert _runtime_provider(object(), ModelOnly(), object()) is not None


def test_runtime_fixture_uses_current_public_request():
    assert V2TaskSubmissionRequest.model_validate(_request_payload()).to_command()


def test_public_request_rejects_historical_discovery_override():
    from pydantic import ValidationError

    payload = _request_payload()
    payload["target"]["exploration_target"]["discovery_policy"] = {"enabled": False}
    with pytest.raises(ValidationError):
        V2TaskSubmissionRequest.model_validate(payload)


class DiscoveryBrowser(PlaywrightPageCaptureBrowser):
    """只替换启动路径和网络传输；交互及采集保持真实生产实现。"""

    def __init__(self, *, href, same_url=False):
        super().__init__()
        self.href = href
        self.same_url = same_url

    async def _launch_browser(self, playwright):
        executable = os.environ["ALIOTH_TEST_CHROME_EXECUTABLE"]
        assert Path(executable).is_file()
        return await playwright.chromium.launch(executable_path=executable, headless=True)

    async def _create_session(self, async_playwright, *, request):
        session = await super()._create_session(async_playwright, request=request)

        async def route_page(route):
            path = route.request.url.split("example.test", 1)[-1]
            target = (
                '<a href="/news">资讯</a>'
                if self.href
                else '<button type="button" onclick="go()">资讯</button>'
            )
            navigation = (
                (
                    "<nav>" + target + '<form onsubmit="bad();return false">'
                    '<button type="submit">提交资料</button></form>'
                    '<button type="button" onclick="bad()">确认授权</button>'
                    '<a download href="/file" onclick="bad()">获取文件</a></nav>'
                )
                if path == "/"
                else ""
            )
            await route.fulfill(
                content_type="text/html",
                body='<html><meta charset="utf-8"><title>中性文章</title>'
                '<body style="margin:0"><script>'
                + (
                    'function go(){sessionStorage.setItem("clicks",'
                    'String(Number(sessionStorage.getItem("clicks")||0)+1));'
                    + (
                        'document.querySelector("h1").textContent="另一个主体对象";'
                        if self.same_url
                        else 'location.href="/news";'
                    )
                    + '}function bad(){sessionStorage.setItem("bad","1")}'
                )
                + '</script><div style="height:350px">中性页头</div><article style="height:900px">'
                + navigation
                + "<h1>受控正文 "
                + path
                + "</h1><p>"
                + ("这是一篇中性受控内容，用于验证自然采集与导航证据。" * 50)
                + '</p></article><footer style="height:800px">中性页脚</footer></body></html>',
            )

        await session.context.route("**/*", route_page)
        return session


class ModelOnly:
    """仅替换外部模型响应；四类型结构门禁、录制与 T5 物化仍真实运行。"""

    def __init__(self):
        self.calls = 0

    async def assess(self, **kwargs):
        return WebAgeGateAssessment(
            gate_present=False,
            confirmation_candidate_id=None,
            confidence=0.99,
            reason="受控页面无门禁",
            human_verification_present=False,
        )

    async def assess_obstruction(self, **kwargs):
        return WebObstructionAssessment(
            obstruction_present=False,
            dismiss_candidate_id=None,
            confidence=0.99,
            reason="受控页面无遮挡",
            human_verification_present=False,
            age_gate_present=False,
        )

    async def classify(self, **kwargs):
        self.calls += 1
        return WebPageAssessment(
            page_type="content_detail", confidence=0.99, evidence=("可见长正文",)
        )


@pytest.mark.asyncio
@requires_database
@pytest.mark.parametrize(
    "href,same_url",
    [(True, False), (False, False), (False, True)],
)
async def test_real_v2_frozen_discovery_runtime_preserves_sources_after_actions_and_cold_read(
    tmp_path, monkeypatch, href, same_url
):
    async with _database() as (connection, pool, _other, ids):
        # 入口将重建会话与首页采集，先解除共享样本的当前页面引用。
        await connection.execute(
            "UPDATE qc_surface_session SET current_capture_id=NULL WHERE session_id=$1",
            ids["session"],
        )
        await connection.execute("DELETE FROM qc_capture WHERE id=$1::uuid", ids["capture"])
        await connection.execute(
            "DELETE FROM qc_surface_session WHERE task_id=$1::uuid", ids["task"]
        )
        ids["session"] = "web:" + ids["run"]
        payload = _request_payload()
        submissions = RecordingSubmissionService()
        await ExplorationTaskV2EntryService(submissions, web_v2_mode="exploration_only").submit(
            V2TaskSubmissionRequest.model_validate(payload).to_command(),
            access_scope=TEST_ACCESS_SCOPE,
        )
        record = _web_record().model_copy(update={"run_id": ids["run"], "task_id": ids["task"]})
        record.task_config.update(submissions.calls[0]["task_config"])
        record.task_config["_task_target"]["target_url"] = "https://example.test/"
        await connection.execute(
            "UPDATE qc_task SET task_config=$2::jsonb WHERE id=$1::uuid",
            ids["task"],
            json.dumps(record.task_config),
        )
        await connection.execute(
            "UPDATE qc_agent_run SET input_context=$2::jsonb WHERE id=$1::uuid",
            ids["run"],
            json.dumps({"task_config": record.task_config}),
        )
        store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
        persistence = PageCapturePersistenceService(
            repository=PostgresPageCaptureRepository(pool=pool), artifact_store=store
        )
        browser = WebDesktopCapturePolicyBrowser(
            browser=DiscoveryBrowser(href=href, same_url=same_url),
            include_blocked_navigation=True,
        )
        snapshots = PostgresTrustedUiSnapshotReader(pool=pool, artifact_store=store)
        evidence = PostgresWebCaptureEvidenceRepository(pool=pool)
        targets = PostgresWebLogicalTargetRepository(pool=pool)
        sessions = PostgresSurfaceSessionRepository(pool=pool)
        validator = InteractionCommandValidator(
            scope_reader=PostgresTaskCommandScopeReader(pool=pool)
        )
        buffer = InMemoryActionEvidenceBuffer()
        resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=snapshots)
        coordinator = CaptureCoordinator(
            capture_writer=BrowserCaptureWriter(
                browser=browser, persistence=persistence, evidence_buffer=buffer
            ),
            interaction_executor=BrowserInteractionExecutor(
                browser=browser,
                target_resolver=resolver,
                evidence_buffer=buffer,
                command_validator=validator,
            ),
            evidence_exit_gate=RecordingEvidenceExitGate(),
            session_command_gate=PostgresSessionCommandGate(
                repository=sessions, lease_owner="t8-runtime", session_owner="web-v2-workflow"
            ),
            action_execution_repository=PostgresActionExecutionRepository(pool=pool),
            command_validator=validator,
        )
        driver = PlaywrightSurfaceDriverAdapter(
            browser=browser,
            target_resolver=resolver,
            evidence_buffer=buffer,
            command_validator=validator,
        )
        built = SimpleNamespace(
            coordinator=coordinator,
            surface_driver=driver,
            surface_sessions=sessions,
            web_desktop_capture_policy=browser,
            trusted_ui_snapshot_reader=snapshots,
        )
        monkeypatch.setattr(composition, "PostgresWebCaptureEvidenceRepository", lambda: evidence)
        monkeypatch.setattr(targets_module, "PostgresWebLogicalTargetRepository", lambda: targets)
        monkeypatch.setattr(
            composition,
            "PostgresWebExplorationBudgetLedgerRepository",
            lambda **kwargs: PostgresWebExplorationBudgetLedgerRepository(pool=pool, **kwargs),
        )
        policies = PostgresWebRunPolicySnapshotRepository(pool=pool)

        def builder(**kwargs):
            from services.api.app.infrastructure.db.web_discovery_action_authorization import (
                PostgresWebDiscoveryActionAuthorization,
            )

            resolver._web_discovery_authorizer = PostgresWebDiscoveryActionAuthorization(
                snapshot=kwargs["web_run_policy"], pool=pool
            )
            return built

        model = ModelOnly()
        provider = _runtime_provider(builder, model, policies)
        try:
            runtime = await provider.build(record=record, settings=Settings())
            command = replace(_command(ids), deadline=runtime.deadline)
            home = await runtime.prepare_home(command, adult_self_attestation=None)
            assert home.page_type == "content_detail"
            result = await runtime.explore(
                command,
                initial_capture_id=home.ready_capture_id,
                initial_page_type=home.page_type,
                initial_navigation_capture_id=home.navigation_current_capture_id,
            )
            summary = result.to_dict()
            assert composition._web_exploration_is_partial(result, summary=summary) is False, (
                summary
            )
            restored = await runtime.budget_ledger.read_summary(
                agent_run_id=ids["run"],
                site_id=runtime.explorer.navigation_context.resolve_site_context(command.target_url).site.site_id,
            )
            executed = [
                item.to_dict()
                for item in restored.runtime_discovered_ledger
                if item.execution.menu_action_count
            ]
            assert len(executed) == int(href), restored.to_dict()
            if not href:
                assert model.calls == 1
                assert (
                    await browser._sessions[ids["session"]].page.evaluate(
                        "sessionStorage.getItem('clicks')"
                    )
                    is None
                )
                assert not any(
                    row.execution and row.execution.status.value == "logical_target_reused"
                    for row in restored.runtime_discovered_ledger
                )
            else:
                assert executed[0]["status"] == "detail_captured"
                assert executed[0]["source_capture_id"] != executed[0]["menu_ready_capture_id"]
            snapshot = await evidence.list_for_task(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                access_scope=AccessScope(
                    actor_user_id=ids["user"],
                    tenant_id=ids["tenant"],
                    permissions=frozenset({"taskObjects:read:any"}),
                ),
            )
            bundle = build_web_evidence_bundle(snapshot)
            assert bundle["status"] == "evidence_ready", (
                json.dumps(
                    {
                        "reasons": bundle["reason_codes"],
                        "ledger": snapshot["ledger"],
                        "actions": snapshot["actions"],
                        "graph": [
                            dict(row)
                            for row in await connection.fetch(
                                "SELECT action_execution_id,proof,counts_as_business_arrival,"
                                "outcome_payload "
                                "FROM qc_page_graph_transition WHERE agent_run_id=$1::uuid",
                                ids["run"],
                            )
                        ],
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
            rejected = [
                row
                for row in snapshot["ledger"]
                if row["source_kind"] == "runtime_discovered" and row["menu_action_count"] == 0
            ]
            assert len(rejected) >= 3
            assert all(row["error_code"] != "WEB_DISCOVERY_PENDING" for row in rejected)
            assert (
                await browser._sessions[ids["session"]].page.evaluate(
                    "sessionStorage.getItem('bad')"
                )
                is None
            )
            record.task_config["web_exploration_target"]["discovery_policy"] = {
                "enabled": False,
                "max_unlisted_menus": 0,
                "same_origin_only": True,
            }
            cold = await provider.build(record=record, settings=Settings())
            assert cold.policy_snapshot == runtime.policy_snapshot
            assert cold.explorer.discovery_policy.enabled is True
            assert (await cold.read_persisted_exploration(command)).to_dict() == result.to_dict()
            if same_url:
                await cold.explorer.explore(
                    command,
                    initial_capture_id=home.ready_capture_id,
                    initial_page_type=home.page_type,
                    initial_navigation_capture_id=home.navigation_current_capture_id,
                )
                assert (
                    await browser._sessions[ids["session"]].page.evaluate(
                        "sessionStorage.getItem('clicks')"
                    )
                    is None
                )
        finally:
            await browser.close_session(ids["session"])
