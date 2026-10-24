"""原页面只读端口必须逐次授权，不能用旧快照或冒充 Web 序列。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.h5_page_input_reader import H5PageInputObservationReader
from services.api.app.application.observation_packet import (
    ObservationPacketValidationError,
    ObservationSource,
)


@pytest.mark.parametrize(
    "failure", [None, "platform", "capture", "hash", "quality", "expired_after_read"]
)
async def test_page_reader_exact_manifest_and_late_authority(failure):
    member = SimpleNamespace(
        capture_id="capture",
        manifest_id="original",
        manifest_version=1,
        manifest_hash="hash",
        artifact_ids=("artifact",),
    )
    inputs = SimpleNamespace(
        members=(member,),
        input_set_id="input",
        input_set_hash="input-hash",
        rule_contract_hash="contract",
    )
    context = ObservationSource(
        task_id="task",
        page_capture_id="capture",
        page_url="https://site.test/",
        page_title=None,
        page_type="home",
        page_state=None,
        action_log=(),
        artifacts=(),
        target_platform="h5",
        agent_run_id="source",
    )
    if failure == "platform":
        context = replace(context, target_platform="web")
    reads = []
    authorization = []

    class Bindings:
        async def load_authorized_bundle(self, **identity):
            authorization.append(identity)
            if failure == "expired_after_read" and len(authorization) == 2:
                raise ObservationPacketValidationError("租约已过期")
            return inputs, None

    class Contexts:
        async def load_capture_context(self, **identity):
            return context

    class Exact:
        async def load_for_manifest(self, **identity):
            reads.append(identity)
            return SimpleNamespace(
                capture_id="other" if failure == "capture" else "capture",
                manifest_id="original",
                session_id="session",
                manifest_version=1,
                manifest_hash="other" if failure == "hash" else "hash",
                artifact_ids=("artifact",),
                quality_status=SimpleNamespace(value="fail" if failure == "quality" else "pass"),
                artifacts=(
                    SimpleNamespace(
                        artifact_id="artifact",
                        artifact_type="visible_text",
                        content_type="text/plain",
                        payload=b"loading",
                    ),
                ),
            )

    reader = H5PageInputObservationReader(
        inner=object(), contexts=Contexts(), bindings=Bindings(), packet_loader=Exact()
    )
    identity = dict(
        task_id="task",
        page_capture_id="capture",
        source_agent_run_id="source",
        input_set_id="input",
        rule_id="rule",
        rule_contract_hash="contract",
        work_item_id="work",
        attempt_id="attempt",
        consumer_agent_run_id="consumer",
        lease_token="private",
    )
    if failure:
        with pytest.raises(ObservationPacketValidationError):
            await reader.load_source_for_page_input_set(**identity)
    else:
        for _ in range(2):
            result = await reader.load_source_for_page_input_set(**identity)
            assert result.artifacts[0].payload == b"loading"
            assert result.web_sequence is None
            assert result.original_page_input["attempt_id"] == "attempt"
            assert "private" not in repr(result.original_page_input)
        assert len(authorization) == 4
    assert all(item["manifest_id"] == "original" for item in reads)
