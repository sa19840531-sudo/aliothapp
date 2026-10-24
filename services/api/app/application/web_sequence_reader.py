"""Web 序列对旧 off/shadow/on 读取器的窄适配，不提供补采或写原始证据能力。"""

from copy import deepcopy
from dataclasses import replace

from services.api.app.application.observation_packet import (
    ManifestQualityProjection,
    ObservationArtifact,
    ObservationPacketValidationError,
)


class WebSequenceObservationReader:
    """普通请求原样委托；显式绑定必须经权威仓储认证并逐成员读取精确 Manifest。"""

    def __init__(self, *, inner, contexts, bindings, packet_loader):
        self._inner = inner
        self._contexts = contexts
        self._bindings = bindings
        self._packet_loader = packet_loader

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def load_source_for_input_set(
        self,
        *,
        task_id,
        page_capture_id,
        source_agent_run_id,
        input_set_id,
        rule_id,
        rule_contract_hash,
        work_item_id=None,
        attempt_id=None,
        consumer_agent_run_id=None,
        lease_token=None,
    ):
        input_set, original_sequence = await self._bindings.load_authorized(
            task_id=task_id,
            page_capture_id=page_capture_id,
            source_agent_run_id=source_agent_run_id,
            input_set_id=input_set_id,
            rule_id=rule_id,
            rule_contract_hash=rule_contract_hash,
            work_item_id=work_item_id,
            attempt_id=attempt_id,
            consumer_agent_run_id=consumer_agent_run_id,
            lease_token=lease_token,
        )
        context = await self._contexts.load_capture_context(
            task_id=task_id,
            page_capture_id=page_capture_id,
        )
        if context is None or context.agent_run_id != source_agent_run_id:
            raise ObservationPacketValidationError("Web 序列锚点上下文不匹配")
        sequence = deepcopy(original_sequence)
        if len(sequence.get("frames", [])) != len(input_set.members):
            raise ObservationPacketValidationError("Web 序列成员数量漂移")
        frame_sources = []
        anchor_packet = None
        anchor_artifacts = ()
        for member, frame in zip(input_set.members, sequence["frames"], strict=True):
            packet = await self._packet_loader.load_for_manifest(
                task_id=task_id,
                manifest_id=member.manifest_id,
            )
            if (
                packet.capture_id != member.capture_id
                or frame.get("capture_id") != member.capture_id
                or frame.get("capture_role") != member.capture_role
                or packet.session_id != sequence["session_id"]
                or packet.manifest_version != member.manifest_version
                or packet.manifest_hash != member.manifest_hash
                or packet.artifact_ids != member.artifact_ids
                or packet.quality_status.value != "pass"
            ):
                raise ObservationPacketValidationError("Web 序列精确 Manifest 成员闭包不匹配")
            frame["artifacts_verified"] = True
            frame["artifact_types"] = [a.artifact_type for a in packet.artifacts]
            frame["artifact_ids"] = list(packet.artifact_ids)
            frame_artifacts = tuple(
                ObservationArtifact(
                    artifact_id=a.artifact_id,
                    artifact_type=a.artifact_type,
                    content_type=a.content_type or "application/octet-stream",
                    payload=a.payload,
                )
                for a in packet.artifacts
            )
            frame_context = await self._contexts.load_capture_context(
                task_id=task_id, page_capture_id=member.capture_id
            )
            if (
                frame_context is None
                or frame_context.page_capture_id != member.capture_id
                or frame_context.agent_run_id != source_agent_run_id
            ):
                raise ObservationPacketValidationError("Web 序列帧语义上下文不匹配")
            frame_sources.append(
                replace(
                    frame_context,
                    artifacts=frame_artifacts,
                    journey_steps=(),
                    web_sequence=None,
                    web_sequence_sources=(),
                )
            )
            if member.capture_id == page_capture_id:
                anchor_packet = packet
                anchor_artifacts = frame_artifacts
        if anchor_packet is None:
            # 锚点必须是封存闭包成员，不得从其他当前页面借上下文。
            raise ObservationPacketValidationError("Web 序列缺少锚点 Manifest")
        return replace(
            context,
            artifacts=anchor_artifacts,
            journey_steps=(),
            web_sequence_sources=tuple(frame_sources),
            manifest_quality=ManifestQualityProjection(
                anchor_packet.manifest_id, anchor_packet.manifest_version, "pass"
            ),
            web_sequence={
                "input_set_id": input_set.input_set_id,
                "rule_id": input_set.rule_id,
                "rule_contract_hash": input_set.rule_contract_hash,
                "finalization_epoch": input_set.finalization_epoch,
                "sequence": sequence,
            },
        )
