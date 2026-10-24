"""真实冻结夹具工厂不能回退到当前目录、外来任务或被篡改正文。"""

import pytest

from services.api.app.application.web_page_type_assessment import WebPageTypeCatalogIdentity
from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicyError
from services.api.tests.test_web_run_policy_snapshot import snapshot
from services.api.tests.web_frozen_assessment_support import frozen_assessment_options


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "missing", "foreign", "tampered"])
async def test_assessment_fixture_reads_only_exact_frozen_contract(mutation, monkeypatch):
    frozen = snapshot()
    row = {
        "task_id": frozen.task_id, "agent_run_id": frozen.agent_run_id,
        "payload": frozen.to_payload(), "payload_sha256": frozen.payload_sha256,
    }
    expected = WebPageTypeCatalogIdentity.from_frozen_catalog_payload(
        row["payload"]["page_catalog"],
    )
    if mutation == "foreign":
        row["task_id"] = "other-task"
    elif mutation == "tampered":
        row["payload"]["workflow_skill"] += "\n被篡改"

    class Connection:
        calls = 0

        async def fetchrow(self, query, run_id):
            self.calls += 1
            assert "qc_web_run_policy_snapshot" in query
            assert run_id == frozen.agent_run_id
            return None if mutation == "missing" else row

        async def execute(self, *args):
            raise AssertionError("冻结夹具读取不能回写原合同")

    def no_current_policy(**kwargs):
        raise AssertionError("不能用当前发布源补历史目录")

    monkeypatch.setattr(
        "services.api.app.application.web_run_policy.load_published_web_policy", no_current_policy,
    )
    connection = Connection()
    ids = {"task": frozen.task_id, "run": frozen.agent_run_id}
    if mutation == "missing":
        with pytest.raises(AssertionError, match="先建立实际"):
            await frozen_assessment_options(connection, ids)
    elif mutation == "foreign":
        with pytest.raises(WebRunPolicyError, match="OWNERSHIP"):
            await frozen_assessment_options(connection, ids)
    elif mutation == "tampered":
        with pytest.raises(ValueError, match="SHA256"):
            await frozen_assessment_options(connection, ids)
    else:
        actual = await frozen_assessment_options(connection, ids)
        assert actual["catalog_identity"] == expected
        assert actual["assessment_schema_version"] == "web-page-type-assessment.v4"
    assert connection.calls == 1
