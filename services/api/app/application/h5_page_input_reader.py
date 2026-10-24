"""普通 H5 页面输入精确读取；自然展示门禁仍由 Observation/Judge 执行。"""

from dataclasses import replace

from services.api.app.application.observation_packet import (
    ManifestQualityProjection,
    ObservationArtifact,
    ObservationPacketValidationError,
)


class H5PageInputObservationReader:
    def __init__(self, *, inner, contexts, bindings, packet_loader):
        self._inner = inner
        self._contexts = contexts
        self._bindings = bindings
        self._packet_loader = packet_loader

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def load_source_for_page_input_set(self, *, manifest_id=None, **identity):
        inputs, binding = await self._bindings.load_authorized_bundle(**identity)
        member = inputs.members[0]
        capture_id = identity["page_capture_id"]
        # 页面集合仍封存补采结果；配套前帧只按当前 Attempt 已哈希的角色绑定读取。
        refs = {
            capture: (ref_id, version)
            for role, capture in (binding.evidence_capture_roles if binding else ())
            for ref_role, ref_id, version in binding.evidence_manifest_refs
            if role == ref_role
        }
        expected_id, expected_version = refs.get(
            capture_id,
            (member.manifest_id, str(member.manifest_version)),
        )
        if manifest_id is not None and manifest_id != expected_id:
            raise ObservationPacketValidationError("H5 页面读取清单不属于当前角色")
        context = await self._contexts.load_capture_context(
            task_id=identity["task_id"],
            page_capture_id=identity["page_capture_id"],
        )
        packet = await self._packet_loader.load_for_manifest(
            task_id=identity["task_id"],
            manifest_id=expected_id,
        )
        if context is None or any(
            (
                context.target_platform != "h5",
                context.agent_run_id != identity["source_agent_run_id"],
                context.task_id != identity["task_id"],
                context.page_capture_id != capture_id,
                packet.capture_id != capture_id,
                packet.session_id is None,
                packet.manifest_id != expected_id,
                str(packet.manifest_version) != expected_version,
                capture_id == member.capture_id and packet.manifest_hash != member.manifest_hash,
                capture_id == member.capture_id and packet.artifact_ids != member.artifact_ids,
                binding is not None and not set(packet.artifact_ids).issubset(binding.artifact_ids),
                packet.quality_status.value != "pass",
            )
        ):
            raise ObservationPacketValidationError("H5 原页面精确 Manifest 读取漂移")
        # 文件读取期间租约、补采或质检可能变化，不能用第一次授权缓存放行。
        current = await self._bindings.load_authorized_bundle(**identity)
        if current != (inputs, binding):
            raise ObservationPacketValidationError("H5 原输入在读取期间变化")
        return replace(
            context,
            artifacts=tuple(
                ObservationArtifact(
                    artifact_id=item.artifact_id,
                    artifact_type=item.artifact_type,
                    content_type=item.content_type or "application/octet-stream",
                    payload=item.payload,
                )
                for item in packet.artifacts
            ),
            manifest_quality=ManifestQualityProjection(
                packet.manifest_id,
                packet.manifest_version,
                "pass",
            ),
            original_page_input={
                "input_set_id": inputs.input_set_id,
                "input_set_hash": inputs.input_set_hash,
                "rule_contract_hash": inputs.rule_contract_hash,
                "manifest_id": member.manifest_id,
                "work_item_id": identity["work_item_id"],
                "attempt_id": identity["attempt_id"],
                "consumer_agent_run_id": identity["consumer_agent_run_id"],
            },
        )
