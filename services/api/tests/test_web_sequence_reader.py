"""显式序列读取不依赖旧 Manifest 模式，不吞掉逐帧闭包。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.tests.test_web_sequence_observation import _ready_source


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
async def test_sequence_adapter_uses_exact_each_manifest_and_delegates_ordinary(mode):
    from services.api.app.application.web_sequence_reader import WebSequenceObservationReader

    context = replace(_ready_source(True), page_capture_id="after-2")
    frames = context.web_sequence["sequence"]["frames"]
    members = [
        SimpleNamespace(
            capture_id=f["capture_id"],
            manifest_id="manifest-" + f["capture_id"],
            manifest_version=1,
            manifest_hash="hash-" + f["capture_id"],
            artifact_ids=("artifact-" + f["capture_id"],),
            capture_role=f["capture_role"],
        )
        for f in frames
    ]
    input_set = SimpleNamespace(
        members=members,
        input_set_id="set-1",
        rule_id="rule-1",
        rule_contract_hash="locked",
        finalization_epoch=2,
    )

    class Legacy:
        async def load_source(self, **kwargs):
            return mode

    class Context:
        async def load_capture_context(self, **kwargs):
            return replace(context, page_capture_id=kwargs["page_capture_id"])

    class Bindings:
        async def load_authorized(self, **kwargs):
            return input_set, context.web_sequence["sequence"]

    class Exact:
        def __init__(self):
            self.reads = []

        async def load_for_manifest(self, *, task_id, manifest_id):
            self.reads.append(manifest_id)
            capture_id = manifest_id.removeprefix("manifest-")
            text = {
                "before-1": "SOURCE_ONLY",
                "after-1": "LOADING_1",
                "before-2": "LOADING_1",
                "after-2": "LOADING_2",
            }[capture_id]
            artifact = SimpleNamespace(
                artifact_id="artifact-" + capture_id,
                artifact_type="visible_text",
                content_type="text/plain",
                payload=text.encode(),
            )
            return SimpleNamespace(
                capture_id=capture_id,
                session_id="web:run-1",
                manifest_id=manifest_id,
                manifest_version=1,
                manifest_hash="hash-" + capture_id,
                artifact_ids=(artifact.artifact_id,),
                artifacts=(artifact,),
                quality_status=SimpleNamespace(value="pass"),
            )

    exact = Exact()
    reader = WebSequenceObservationReader(
        inner=Legacy(), contexts=Context(), bindings=Bindings(), packet_loader=exact
    )
    assert await reader.load_source(task_id="task-1", page_capture_id="capture-1") == mode
    result = await reader.load_source_for_input_set(
        task_id="task-1",
        page_capture_id="after-2",
        source_agent_run_id="run-1",
        input_set_id="set-1",
        rule_id="rule-1",
        rule_contract_hash="locked",
    )
    assert exact.reads == [m.manifest_id for m in members]
    assert len(result.artifacts) == 1
    assert result.artifacts[0].payload == b"LOADING_2"
    assert len(result.web_sequence_sources) == 4
    assert result.web_sequence_sources[0].artifacts[0].payload == b"SOURCE_ONLY"
    assert len(result.web_sequence["sequence"]["frames"]) == 4
    assert result.web_sequence["sequence"]["frames"][1]["artifact_ids"] == ["artifact-after-1"]


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
def test_actual_rule_judge_bootstrap_wraps_sequence_reader_without_changing_legacy(
    mode, monkeypatch
):
    from services.api.app.agents.rule_judge import bootstrap
    from services.api.app.application.manifest_read_cutover import ManifestReadMode
    from services.api.app.application.web_sequence_reader import WebSequenceObservationReader

    legacy = object()
    monkeypatch.setattr(
        bootstrap,
        "build_manifest_read_composition",
        lambda: SimpleNamespace(
            reader=legacy,
            effective_mode=ManifestReadMode(mode),
        ),
    )
    bootstrap.build_default_rule_judge_agent.cache_clear()
    try:
        agent = bootstrap.build_default_rule_judge_agent()
        reader = agent._service._observation_packet_service._repository
        assert isinstance(reader, WebSequenceObservationReader)
        from services.api.app.application.h5_page_input_reader import H5PageInputObservationReader

        assert isinstance(reader._inner, H5PageInputObservationReader)
        assert reader._inner._inner is legacy
        assert agent._service._manifest_quality_authoritative is (mode == "on")
    finally:
        bootstrap.build_default_rule_judge_agent.cache_clear()
