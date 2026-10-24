"""没有可证明的冻结期限时，不能取得分类无限等待资格。"""

from types import SimpleNamespace

import pytest

from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
from services.api.app.infrastructure.db import web_sequence_classification_wait as waiting
from services.api.tests.test_web_run_policy_snapshot import policy_payload


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["snapshot", "binding"])
async def test_classification_wait_requires_trusted_frozen_deadline(missing, monkeypatch):
    class Connection:
        async def fetchrow(self, *args):
            return {"finalization_state": "open", "status": "running"}

    snapshot = WebRunPolicySnapshot.create(
        task_id="task", agent_run_id="run", payload=policy_payload()
    )

    async def read(*args):
        return None if missing == "snapshot" else snapshot

    monkeypatch.setattr(waiting, "read_web_policy_on_connection", read)
    assert not await waiting.may_wait_for_classification(
        Connection(),
        SimpleNamespace(task_id="task", source_agent_run_id="run"),
        {
            "policy_hash": snapshot.payload_sha256
            if missing == "snapshot"
            else "not-the-frozen-policy"
        },
    )
