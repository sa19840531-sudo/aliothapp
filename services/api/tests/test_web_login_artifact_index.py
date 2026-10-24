"""隔离登录附件须先取得可审计索引，才能交给账户结果。"""

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_object_store_registration_precedes_artifact_publication():
    from services.api.app.application.web_login_artifact_index import (
        WebLoginIndexedArtifactStore,
    )

    events = []
    artifact = SimpleNamespace(
        artifact_id="00000000-0000-0000-0000-000000000004", artifact_type="screenshot",
        storage_path="task/capture/artifact-1.png", content_type="image/png",
        checksum="sha256:" + "a" * 64, size_bytes=12, metadata={"encoding": "binary"},
    )

    class Store:
        async def store(self, **kwargs):
            events.append(("object", kwargs))
            return artifact

    class Index:
        async def register(self, **kwargs):
            events.append(("index", kwargs))

    result = await WebLoginIndexedArtifactStore(
        object_store=Store(), index=Index(), agent_run_id="run-1",
        main_session_id="web:run-1",
    ).store(
        task_id="task-1", page_capture_id="source-1", artifact_id="artifact-1",
        artifact_type="screenshot", content=b"\x89PNG\r\n\x1a\nimg", step="authenticated",
        isolated_session_id="web:run-1:login",
    )
    assert result is artifact
    assert [event[0] for event in events] == ["object", "index"]
    assert "step" not in events[0][1]
    assert events[1][1]["source_capture_id"] == "source-1"
    assert events[1][1]["step"] == "authenticated"
    assert events[1][1]["isolated_session_id"] == "web:run-1:login"
    assert events[1][1]["artifact"] is artifact


@pytest.mark.asyncio
async def test_index_failure_does_not_return_unregistered_artifact():
    from services.api.app.application.web_login_artifact_index import (
        WebLoginArtifactIndexError,
        WebLoginIndexedArtifactStore,
    )

    events = []

    class Store:
        async def store(self, **kwargs):
            return SimpleNamespace(
                artifact_id=kwargs["artifact_id"], created=True,
                storage_path="task/source/artifact-1.png",
            )

    class Index:
        async def register(self, **kwargs):
            raise RuntimeError("raw database detail must not leak")

        async def confirm_registration(self, **kwargs):
            events.append(("confirm", kwargs["artifact"].artifact_id))
            return False

    indexed = WebLoginIndexedArtifactStore(
        object_store=Store(), index=Index(), agent_run_id="run-1",
        main_session_id="web:run-1",
    )
    with pytest.raises(WebLoginArtifactIndexError) as error:
        await indexed.store(
            task_id="task-1", page_capture_id="source-1", artifact_id="artifact-1",
            artifact_type="screenshot", content=b"\x89PNG\r\n\x1a\nimg",
            step="authenticated", isolated_session_id="web:run-1:login",
        )
    assert error.value.error_code == "WEB_LOGIN_ACCOUNT_ARTIFACT_INDEX_FAILED"
    assert "raw database" not in str(error.value)
    assert events == [("confirm", "artifact-1")]


@pytest.mark.asyncio
async def test_ambiguous_registration_keeps_committed_artifact():
    from services.api.app.application.web_login_artifact_index import (
        WebLoginIndexedArtifactStore,
    )

    artifact = SimpleNamespace(
        artifact_id="artifact-1", created=True,
        storage_path="task/source/artifact-1.png",
    )
    class Store:
        async def store(self, **_kwargs):
            return artifact

    class Index:
        async def register(self, **_kwargs):
            raise RuntimeError("commit acknowledgment lost")

        async def confirm_registration(self, **kwargs):
            assert kwargs["task_id"] == "task-1"
            assert kwargs["source_capture_id"] == "source-1"
            assert kwargs["artifact"] is artifact
            return True

    result = await WebLoginIndexedArtifactStore(
        object_store=Store(), index=Index(), agent_run_id="run-1",
        main_session_id="web:run-1",
    ).store(
        task_id="task-1", page_capture_id="source-1", artifact_id="artifact-1",
        artifact_type="screenshot", content=b"\x89PNG\r\n\x1a\nimg",
        step="authenticated", isolated_session_id="web:run-1:login",
    )
    assert result is artifact


@pytest.mark.asyncio
async def test_reconciliation_outage_does_not_delete_uncertain_object():
    from services.api.app.application.web_login_artifact_index import (
        WebLoginArtifactIndexError,
        WebLoginIndexedArtifactStore,
    )

    class Store:
        async def store(self, **kwargs):
            return SimpleNamespace(
                artifact_id=kwargs["artifact_id"], created=True,
                storage_path="task/source/artifact-1.png",
            )

    class Index:
        async def register(self, **_kwargs):
            raise RuntimeError("database unavailable")

        async def confirm_registration(self, **_kwargs):
            raise RuntimeError("database still unavailable")

    with pytest.raises(WebLoginArtifactIndexError):
        await WebLoginIndexedArtifactStore(
            object_store=Store(), index=Index(), agent_run_id="run-1",
            main_session_id="web:run-1",
        ).store(
            task_id="task-1", page_capture_id="source-1", artifact_id="artifact-1",
            artifact_type="screenshot", content=b"\x89PNG\r\n\x1a\nimg",
            step="authenticated", isolated_session_id="web:run-1:login",
        )


@pytest.mark.asyncio
async def test_cancelled_registration_is_logged_without_publishing_artifact(caplog):
    import asyncio

    from services.api.app.application.web_login_artifact_index import (
        WebLoginIndexedArtifactStore,
    )

    class Store:
        async def store(self, **kwargs):
            return SimpleNamespace(artifact_id=kwargs["artifact_id"])

    class Index:
        async def register(self, **_kwargs):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await WebLoginIndexedArtifactStore(
            object_store=Store(), index=Index(), agent_run_id="run-1",
            main_session_id="web:run-1",
        ).store(
            task_id="task-1", page_capture_id="source-1", artifact_id="artifact-1",
            artifact_type="screenshot", content=b"\x89PNG\r\n\x1a\nimg",
            step="authenticated", isolated_session_id="web:run-1:login",
        )
    assert "WEB_LOGIN_ACCOUNT_ARTIFACT_RECONCILIATION_PENDING" in caplog.text
    assert "task-1" in caplog.text and "artifact-1" in caplog.text


@pytest.mark.asyncio
async def test_cancelled_confirmation_is_logged_without_publishing_artifact(caplog):
    import asyncio

    from services.api.app.application.web_login_artifact_index import (
        WebLoginIndexedArtifactStore,
    )

    class Store:
        async def store(self, **kwargs):
            return SimpleNamespace(artifact_id=kwargs["artifact_id"])

    class Index:
        async def register(self, **_kwargs):
            raise RuntimeError("commit acknowledgment lost")

        async def confirm_registration(self, **_kwargs):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await WebLoginIndexedArtifactStore(
            object_store=Store(), index=Index(), agent_run_id="run-1",
            main_session_id="web:run-1",
        ).store(
            task_id="task-1", page_capture_id="source-1", artifact_id="artifact-1",
            artifact_type="screenshot", content=b"\x89PNG\r\n\x1a\nimg",
            step="authenticated", isolated_session_id="web:run-1:login",
        )
    assert "WEB_LOGIN_ACCOUNT_ARTIFACT_RECONCILIATION_PENDING" in caplog.text
    assert "task-1" in caplog.text and "artifact-1" in caplog.text


@pytest.mark.asyncio
async def test_repository_sql_requires_web_source_but_not_current_capture():
    from services.api.app.infrastructure.db.web_login_artifact_index_repository import (
        PostgresWebLoginArtifactIndexRepository,
    )

    statements = []

    class Connection:
        async def execute(self, sql, *args):
            statements.append((sql, args))

        async def fetchrow(self, sql, *args):
            statements.append((sql, args))
            return {"id": "00000000-0000-0000-0000-000000000004"}

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Connection()

    artifact = SimpleNamespace(
        artifact_id="00000000-0000-0000-0000-000000000004", artifact_type="screenshot",
        storage_path="task/capture/artifact-1.png", content_type="image/png",
        checksum="sha256:" + "a" * 64, size_bytes=12, metadata={"encoding": "binary"},
    )
    await PostgresWebLoginArtifactIndexRepository(pool=Pool()).register(
        task_id="00000000-0000-0000-0000-000000000001",
        agent_run_id="00000000-0000-0000-0000-000000000002",
        main_session_id="web:00000000-0000-0000-0000-000000000002",
        isolated_session_id="web:00000000-0000-0000-0000-000000000002:login",
        source_capture_id="00000000-0000-0000-0000-000000000003",
        step="authenticated", artifact=artifact,
    )
    assert "pg_advisory_xact_lock" in statements[0][0]
    sql, args = statements[1]
    assert "INSERT INTO qc_evidence_artifact" in sql
    assert "'web_login_isolated'" in sql
    assert "NULL::uuid" in sql
    assert "qc_page_capture" in sql and "qc_capture" in sql
    assert "qc_surface_session" in sql and "web-v2-workflow" in sql
    assert "target_platform" in sql and "surface_kind='web'" in sql
    assert "finalization_epoch" in sql
    assert "session.status IN ('active','draining')" in sql
    assert "run.status IN ('running','done')" in sql
    assert "current_capture_id" not in sql
    assert "ON CONFLICT (id)" in sql
    assert "checksum" in sql and "size_bytes" in sql
    assert "00000000-0000-0000-0000-000000000003" in args
    assert '"isolated_session_id": "web:00000000-0000-0000-0000-000000000002:login"' in args[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("indexed,wrong_lineage,expected", [
    (False, False, False),
    (True, False, True),
    (True, True, False),
])
async def test_repository_confirms_only_exact_index_identity(
    indexed, wrong_lineage, expected,
):
    from services.api.app.infrastructure.db.web_login_artifact_index_repository import (
        PostgresWebLoginArtifactIndexRepository,
    )

    artifact = SimpleNamespace(
        artifact_id="00000000-0000-0000-0000-000000000004",
        artifact_type="screenshot", storage_path="task/source/artifact-1.png",
        content_type="image/png", checksum="sha256:" + "a" * 64,
        size_bytes=12, metadata={"encoding": "binary"},
    )
    task_id = "00000000-0000-0000-0000-000000000001"
    run_id = "00000000-0000-0000-0000-000000000002"
    source_id = "00000000-0000-0000-0000-000000000003"
    events = []

    class Connection:
        async def execute(self, sql, *_args):
            events.append("lock")
            assert "pg_advisory_xact_lock" in sql

        async def fetchrow(self, sql, *_args):
            events.append("lookup")
            assert "qc_evidence_artifact" in sql
            if not indexed:
                return None
            return {
                "task_id": task_id,
                "source": "web_login_isolated", "page_capture_id": None,
                "artifact_type": artifact.artifact_type,
                "storage_path": artifact.storage_path,
                "content_type": artifact.content_type,
                "checksum": artifact.checksum,
                "size_bytes": artifact.size_bytes,
                "metadata": {
                    "encoding": "binary", "source_capture_id": source_id,
                    "agent_run_id": run_id, "main_session_id": f"web:{run_id}",
                    "isolated_session_id": f"web:{run_id}:login",
                    "step": "wrong_step" if wrong_lineage else "authenticated",
                    "branch": "isolated_login",
                },
            }

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Connection()

    outcome = await PostgresWebLoginArtifactIndexRepository(
        pool=Pool(),
    ).confirm_registration(
        task_id=task_id, agent_run_id=run_id, main_session_id=f"web:{run_id}",
        isolated_session_id=f"web:{run_id}:login", source_capture_id=source_id,
        step="authenticated", artifact=artifact,
    )
    assert outcome == expected
    assert events == ["lock", "lookup"]


@pytest.mark.asyncio
async def test_repository_rejects_isolated_session_not_owned_by_main():
    from services.api.app.infrastructure.db.web_login_artifact_index_repository import (
        PostgresWebLoginArtifactIndexRepository,
        WebLoginArtifactRegistrationError,
    )

    artifact = SimpleNamespace(
        artifact_id="00000000-0000-0000-0000-000000000004", artifact_type="dom",
        storage_path="task/capture/artifact-1.html",
        content_type="text/html; charset=utf-8",
        checksum="sha256:" + "a" * 64, size_bytes=12, metadata={},
    )
    with pytest.raises(WebLoginArtifactRegistrationError, match="IDENTITY_INVALID"):
        await PostgresWebLoginArtifactIndexRepository(pool=None).register(
            task_id="00000000-0000-0000-0000-000000000001",
            agent_run_id="00000000-0000-0000-0000-000000000002",
            main_session_id="web:00000000-0000-0000-0000-000000000002",
            isolated_session_id="web:another-run:login",
            source_capture_id="00000000-0000-0000-0000-000000000003",
            step="authenticated", artifact=artifact,
        )
