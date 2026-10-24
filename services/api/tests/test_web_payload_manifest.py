"""技术完整性和分类后更强需求必须产生不同的真实裁决版本。"""

from dataclasses import replace
from hashlib import sha256

import pytest

from services.api.app.domain.exploration.evidence_exit_requirements import (
    EvidenceExitRequirementStatus,
    build_active_session_evidence_requirement,
)
from services.api.app.domain.exploration.web_payload_requirements import (
    create_payload_requirement_set,
    rebuild_payload_requirement_set,
)
from services.api.tests.test_evidence_manifest_writer import NOW, _artifact, _pending_manifest


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [None, "missing", "checksum", "missing_dom"])
async def test_payload_objects_are_read_and_checked_before_preservation_pass(damage):
    from services.api.app.application.web_payload_manifest import adjudicate_web_payload_manifest

    data = {"screenshot": b"\x89PNG\r\n\x1a\ncurrent", "dom": b"<body>current</body>"}
    artifacts = tuple(
        replace(
            _artifact(artifact_id=name, artifact_type=name),
            checksum="sha256:" + sha256(content).hexdigest(),
            size_bytes=len(content),
        )
        for name, content in data.items()
    )
    manifest = _pending_manifest(artifacts if damage != "missing_dom" else artifacts[:1])
    requirements = create_payload_requirement_set(
        task_id=manifest.task_id,
        agent_run_id="run",
        session_id=manifest.session_id,
        finalization_epoch=0,
        capture_id=manifest.capture_id,
        policy_hash="a" * 64,
        capture_role="primary",
        committed_artifact_types=("screenshot", "dom"),
        created_at=NOW,
    )

    class Store:
        async def read(self, path):
            if damage == "missing":
                raise FileNotFoundError("中性丢失")
            return (
                b"invalid" if damage == "checksum" else data[path.rsplit("/", 1)[-1].split(".")[0]]
            )

    result = await adjudicate_web_payload_manifest(
        manifest=manifest,
        requirements=requirements,
        artifact_store=Store(),
        phase="capture_preservation",
        now=NOW,
    )
    assert result.manifest_version == 2
    assert result.quality_status.value == ("pass" if damage is None else "fail")
    if damage is None:
        stronger = build_active_session_evidence_requirement(
            task_id=manifest.task_id,
            capture_id=manifest.capture_id,
            rule_id="actual-rule",
            rule_code="more-screenshots",
            required_artifact_types=("screenshot", "dom"),
            required_capture_roles=("primary",),
            min_screenshot_count=2,
            status=EvidenceExitRequirementStatus.PENDING,
        )
        latest = rebuild_payload_requirement_set(
            requirements, requirements=(*requirements.requirements, stronger), created_at=NOW
        )
        checked = await adjudicate_web_payload_manifest(
            manifest=result,
            requirements=latest,
            artifact_store=Store(),
            phase="classified_rule_requirements",
            now=NOW,
        )
        assert checked.manifest_version == 3 and checked.quality_status.value == "fail"
        assert result.quality_status.value == "pass"
