"""离线恢复集成夹具：仅替换 PostgreSQL 与浏览器边缘，保留实际生产阶段驱动。"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from services.api.app.application.web_full_page_supplemental_capture import (
    WebFullPageSupplementalResult,
)
from services.api.app.application.web_menu_exploration import WebMenuVisitResult
from services.api.app.application.web_runtime_stage_runner import build_web_runtime_stage_runner
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.page import WebMenuDecision
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    WebStageCheckpointConflict,
)
from services.api.tests.test_web_runtime_stage_effects import RUN, TASK, command
from services.api.tests.test_web_runtime_stage_frontier import candidate


class RecoveryWorld:
    def __init__(self, monkeypatch, *, full_page_failure=False, with_candidates=False):
        self.now = datetime.now(UTC)
        self.rows, self.completed, self.captures, self.full_pages = {}, {}, {}, {}
        self.current = None
        self.closed = False
        self.actions, self.classifications, self.records, self.claimed = [], [], {}, set()
        self.discoveries = {}
        self.full_page_failure = full_page_failure
        self.with_candidates = with_candidates
        world = self

        class Checkpoints:
            async def create(self, cursor):
                return world.rows.setdefault(cursor.logical_page_key, cursor)

            async def get(self, *, run_id, logical_page_key):
                return world.rows[logical_page_key]

            async def list_for_run(self, *, run_id):
                return tuple(world.rows.values())

            async def claim(self, *, logical_page_key, expected_row_version, worker_id, **_kwargs):
                before = world.rows[logical_page_key]
                if before.row_version != expected_row_version:
                    raise WebStageCheckpointConflict("版本失效")
                after = before.claim(worker_id=worker_id, now=world.now)
                world.rows[logical_page_key] = replace(after, row_version=before.row_version + 1)
                return world.rows[logical_page_key]

            def fence(self, cursor):
                current = world.rows[cursor.logical_page_key]
                if (
                    current.claim_token != cursor.claim_token
                    or current.row_version != cursor.row_version
                    or cursor.lease_expires_at <= world.now
                ):
                    raise WebStageCheckpointConflict("租约失效")

            async def mark_succeeded(self, cursor, *, artifact_ids=(), cursor_payload=None):
                self.fence(cursor)
                after = replace(
                    cursor.mark_succeeded(artifact_ids=artifact_ids),
                    cursor_payload={**cursor.cursor_payload, **(cursor_payload or {})},
                    row_version=cursor.row_version + 1,
                )
                world.rows[cursor.logical_page_key] = after
                return after

            async def schedule_retry(self, cursor, **kwargs):
                self.fence(cursor)
                after = replace(
                    cursor.schedule_retry(now=world.now, **kwargs),
                    row_version=cursor.row_version + 1,
                )
                world.rows[cursor.logical_page_key] = after
                return after

            async def record_gap_and_advance(self, cursor, **kwargs):
                self.fence(cursor)
                after = replace(
                    cursor.record_gap_and_advance(**kwargs), row_version=cursor.row_version + 1
                )
                world.rows[cursor.logical_page_key] = after
                return after

        class Evidence:
            def __init__(self, **_kwargs):
                pass

            async def read_epoch(self):
                return 2

            async def assert_active(self, cursor):
                Checkpoints().fence(cursor)

            async def find_completed(self, cursor, *, operation=None):
                return world.completed.get((cursor.idempotency_key, operation))

            async def save_completed(self, cursor, result, *, operation=None):
                Checkpoints().fence(cursor)
                world.completed[cursor.idempotency_key, operation] = result

            async def capture_reference(self, *, capture_id):
                assert capture_id == world.current
                return {"capture_id": capture_id, "current_url": world.captures[capture_id]["url"]}

            async def current_capture_reference(self):
                return await self.capture_reference(capture_id=world.current)

            async def capture_outcome(self, *, capture_id):
                result = dict(world.captures[capture_id])
                if result["page_type"] is None:
                    result["page_type"] = next(
                        (
                            item["page_type"]
                            for item in reversed(tuple(world.captures.values()))
                            if item["url"] == result["url"] and item["page_type"] is not None
                        ),
                        None,
                    )
                return result

            async def has_durable_page_type_assessment(
                self, *, capture_id, finalization_epoch
            ):
                assert finalization_epoch == 2
                return world.captures[capture_id]["page_type"] is not None

            async def read_durable_page_type_assessment(
                self, *, capture_id, finalization_epoch
            ):
                assert finalization_epoch == 2
                page_type = world.captures[capture_id]["page_type"]
                return {"page_type": page_type} if page_type is not None else None

            async def session_closed(self, *, session_id):
                return world.closed

        class Budget:
            async def read_summary(self, **_kwargs):
                return SimpleNamespace(
                    runtime_discovered_ledger=tuple(
                        SimpleNamespace(
                            entry_id=key,
                            execution=world.records.get(key),
                            restored_navigation_candidate=lambda item=item: item,
                        )
                        for key, item in world.discoveries.items()
                    )
                )

            async def record_discovery(self, *, candidate, **_kwargs):
                world.discoveries.setdefault(
                    "discovery:" + candidate.logical_target_identity, candidate
                )

            async def claim_menu(self, *, entry, **_kwargs):
                previous = entry.entry_id in world.claimed
                world.claimed.add(entry.entry_id)
                return SimpleNamespace(intent_id=entry.entry_id, already_reserved=previous)

            async def finalize(self, record):
                world.records[record.menu_entry_id] = record

            async def seal(self, **_kwargs):
                world.actions.append("budget_sealed")

        class Source:
            def bind_latest_navigation_capture(self, session_id, capture_id):
                assert capture_id == world.current

            async def navigation_candidates(self, session_id):
                return (candidate("broken"), candidate("healthy")) if world.with_candidates else ()

        class Navigator:
            async def visit(self, *, candidate, **_kwargs):
                world.actions.append(candidate.url)
                if candidate.url.endswith("broken"):
                    return WebMenuVisitResult(
                        final_capture_id=None,
                        page_type=None,
                        status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                        error_code="WEB_GOTO_TLS_FAILED",
                    )
                capture_id = world.capture(candidate.url, "probe")
                return WebMenuVisitResult(final_capture_id=capture_id, page_type=None)

        @dataclass
        class Ready:
            capture_id: str
            url: str
            supplemental_screenshot_artifact_ids: tuple[str, ...] = ()
            supplemental_evidence_status: str = "not_attempted"
            supplemental_evidence_reason_code: str | None = None

        class ReadyReader:
            async def read(self, *, capture_id, page_role):
                assert capture_id == world.current
                return Ready(capture_id=capture_id, url=world.captures[capture_id]["url"])

        class AgeReader:
            async def read(self, *, capture_id):
                return SimpleNamespace(capture_id=capture_id)

        class Gate:
            async def handle(self, evidence, **_kwargs):
                return SimpleNamespace(after_capture_id=evidence.capture_id)

        class Readiness:
            async def prepare(self, *, source_capture_id, page_role):
                capture_id = world.capture(world.captures[source_capture_id]["url"], "ready")
                return SimpleNamespace(status="stable", ready_capture_id=capture_id)

        class Classifier:
            async def classify(self, ready):
                world.classifications.append(ready.url)
                world.captures[ready.capture_id]["page_type"] = (
                    "home" if ready.url == "https://example.test/" else "content_detail"
                )

        class FullPage:
            async def read_completed(self, request):
                return world.full_pages.get(request.source_capture_id)

            async def capture(self, request):
                world.actions.append("full_page")
                if world.full_page_failure:
                    return WebFullPageSupplementalResult(
                        status="failed",
                        source_capture_id=request.source_capture_id,
                        supplemental_capture_id=None,
                        screenshot_artifact_id=None,
                        reason_code="WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
                    )
                result = WebFullPageSupplementalResult(
                    status="captured",
                    source_capture_id=request.source_capture_id,
                    supplemental_capture_id=str(
                        uuid5(NAMESPACE_URL, request.source_capture_id + ":child")
                    ),
                    screenshot_artifact_id=str(
                        uuid5(NAMESPACE_URL, request.source_capture_id + ":png")
                    ),
                    reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
                )
                world.full_pages[request.source_capture_id] = result
                return result

        class Runtime:
            async def start(self, request):
                world.actions.append("bootstrap")
                return SimpleNamespace(
                    after_capture=SimpleNamespace(
                        capture_id=world.capture(request.target_url, "probe")
                    )
                )

            async def close(self, **_kwargs):
                world.actions.append("session_close")
                world.closed = True

            async def read_persisted_exploration(self, request):
                return SimpleNamespace(
                    page_timeline=tuple(world.captures), page_type_keys=tuple(world.classifications)
                )

        site = SimpleNamespace(
            site_id="runtime",
            origin="https://example.test/",
            entries=(SimpleNamespace(entry_id="home", decision=WebMenuDecision.HOME_BASELINE),),
        )
        runtime = Runtime()
        runtime.navigation_context = SimpleNamespace(
            resolve_site_context=lambda _url: SimpleNamespace(site=site)
        )
        runtime.budget_ledger, runtime.navigation_candidate_source = Budget(), Source()
        runtime.stage_ready_capture_reader = ReadyReader()
        runtime.age_evidence_reader, runtime.age_gate_handler = AgeReader(), Gate()
        runtime.page_readiness, runtime.page_type_classifier = Readiness(), Classifier()
        runtime.pre_gate_supplemental_capture = FullPage()
        monkeypatch.setattr(
            "services.api.app.infrastructure.db.web_run_stage_checkpoint_repository."
            "PostgresWebRunStageCheckpointRepository",
            Checkpoints,
        )
        monkeypatch.setattr(
            "services.api.app.infrastructure.db.web_runtime_stage_evidence_repository."
            "PostgresWebRuntimeStageEvidenceRepository",
            Evidence,
        )

        class Clock:
            @staticmethod
            def now(_tz):
                return world.now

        async def sleep(seconds):
            world.now += timedelta(seconds=seconds)

        monkeypatch.setattr("services.api.app.application.web_runtime_stage_runner.datetime", Clock)
        monkeypatch.setattr(
            "services.api.app.application.web_runtime_stage_runner.asyncio.sleep", sleep
        )
        self.runtime, self.navigator = runtime, Navigator()

    def capture(self, url, kind):
        capture_id = str(uuid5(NAMESPACE_URL, url + kind))
        self.captures[capture_id] = {
            "url": url,
            "page_type": None,
            "action_execution_id": "action:" + capture_id,
        }
        self.current = capture_id
        return capture_id

    def runner(self):
        return build_web_runtime_stage_runner(
            runtime=self.runtime,
            record=SimpleNamespace(task_id=TASK, run_id=RUN),
            navigator=self.navigator,
        )

    async def run(self, *, stop_at=None):
        runner = self.runner()
        await runner.initialize(command(), adult_self_attestation=None)
        for _ in range(100):
            due = await runner.next_due(RUN)
            if due is None:
                return await runner.final_result(RUN)
            if stop_at and (due.next_stage or due.current_stage).value == stop_at:
                return None
            await runner.advance_once(RUN, due.logical_page_key)
        raise AssertionError("阶段调度没有按固定图收口")
