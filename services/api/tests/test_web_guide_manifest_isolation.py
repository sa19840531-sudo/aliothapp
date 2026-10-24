"""引导页清单失败只能保留在支线，不能改变 Web 主线收口。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.api.app.domain.evidence import EvidenceQualityStatus


@pytest.mark.asyncio
async def test_guide_manifest_failure_does_not_abort_normal_web_finalization() -> None:
    from services.api.app.application.web_manifest_finalizer import (
        WebPageManifestFinalizer,
    )

    guide = SimpleNamespace(
        capture_id="guide-capture",
        metadata={"capture_purpose": "web_guide_page"},
        manifest=SimpleNamespace(quality_status=EvidenceQualityStatus.PENDING),
    )
    normal = SimpleNamespace(
        capture_id="main-capture",
        metadata={}, manifest=SimpleNamespace(quality_status=EvidenceQualityStatus.PENDING),
    )
    calls = []

    class Repository:
        async def list_latest_for_session(self, **_kwargs):
            return (normal,)

        async def list_optional_guides_for_session(self, **_kwargs):
            return (guide,)

    class Finalizer:
        async def finalize(self, manifest, **_kwargs):
            calls.append(manifest)
            if manifest is guide.manifest:
                raise RuntimeError("guide object unreadable")

    service = WebPageManifestFinalizer(repository=Repository(), finalizer=Finalizer())
    settled = await service.finalize_session(
        task_id="task", session_id="web:run", agent_run_id="run",
    )
    assert calls == [normal.manifest, guide.manifest]
    assert settled == 1


@pytest.mark.asyncio
async def test_normal_web_manifest_failure_still_blocks_finalization() -> None:
    from services.api.app.application.web_manifest_finalizer import (
        WebPageManifestFinalizer,
    )

    normal = SimpleNamespace(
        capture_id="main-capture", metadata={},
        manifest=SimpleNamespace(quality_status=EvidenceQualityStatus.PENDING),
    )

    class Repository:
        async def list_latest_for_session(self, **_kwargs):
            return (normal,)

        async def list_optional_guides_for_session(self, **_kwargs):
            return ()

    class Finalizer:
        async def finalize(self, _manifest, **_kwargs):
            raise RuntimeError("main manifest failed")

    service = WebPageManifestFinalizer(repository=Repository(), finalizer=Finalizer())
    with pytest.raises(RuntimeError, match="main manifest failed"):
        await service.finalize_session(
            task_id="task", session_id="web:run", agent_run_id="run",
        )


@pytest.mark.asyncio
async def test_guide_inventory_read_failure_does_not_abort_normal_web_finalization() -> None:
    from services.api.app.application.web_manifest_finalizer import WebPageManifestFinalizer

    normal = SimpleNamespace(
        manifest=SimpleNamespace(quality_status=EvidenceQualityStatus.PENDING),
    )
    calls = []

    class Repository:
        async def list_latest_for_session(self, **_kwargs):
            return (normal,)

        async def list_optional_guides_for_session(self, **_kwargs):
            raise ValueError("malformed optional manifest")

    class Finalizer:
        async def finalize(self, manifest, **_kwargs):
            calls.append(manifest)

    service = WebPageManifestFinalizer(repository=Repository(), finalizer=Finalizer())
    assert await service.finalize_session(
        task_id="task", session_id="web:run", agent_run_id="run",
    ) == 1
    assert calls == [normal.manifest]


@pytest.mark.asyncio
async def test_malformed_guide_manifest_is_filtered_before_main_parse() -> None:
    from datetime import UTC, datetime

    from services.api.app.infrastructure.db.web_evidence_manifest_repository import (
        PostgresWebEvidenceManifestRepository,
    )

    row = {
        "capture_id": "main", "manifest_payload": None,
        "agent_run_id": "run", "capture_role": "primary",
        "captured_at": datetime.now(UTC), "metadata": {},
        "action_execution_id": None,
    }

    class Connection:
        async def fetch(self, query, *_args):
            if "IS DISTINCT FROM 'web_guide_page'" in query:
                return [row]
            assert "= 'web_guide_page'" in query
            return [
                dict(row, capture_id="broken-guide", manifest_payload="{broken",
                     metadata={"capture_purpose": "web_guide_page"}),
                dict(row, capture_id="valid-guide", metadata={"capture_purpose": "web_guide_page"}),
            ]

    class Lease:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Lease()

    repository = PostgresWebEvidenceManifestRepository(pool=Pool())
    kwargs = {"task_id": "task", "session_id": "web:run", "agent_run_id": "run"}
    main = await repository.list_latest_for_session(**kwargs)
    guides = await repository.list_optional_guides_for_session(**kwargs)
    assert [item.capture_id for item in main] == ["main"]
    assert [item.capture_id for item in guides] == ["valid-guide"]


def test_web_reconciliation_excludes_optional_guide_capture_from_main_gate() -> None:
    from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
        _RECONCILIATION_STATE_SQL,
    )

    assert "commit_page.metadata->>'capture_purpose'" in _RECONCILIATION_STATE_SQL
    assert "'web_guide_page'" in _RECONCILIATION_STATE_SQL
