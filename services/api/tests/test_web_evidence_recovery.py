"""详情页证据供给、菜单摘要和只读目标查询的现场回归。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.web_exploration_workflow import web_capture_profile
from services.api.app.application.web_payload_manifest import adjudicate_web_payload_manifest
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
async def test_web_baseline_supplies_detail_rule_payload_without_weakening_manifest():
    from hashlib import sha256

    from services.api.app.infrastructure.browser.capture_runtime import _merge_action_evidence
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureArtifact,
        PageCaptureMode,
    )
    from services.api.tests.test_web_capture_runtime import _page_result

    directive = web_capture_profile("web-readiness.v1").after
    supplied = _page_result(mode=PageCaptureMode.OBSERVE)
    # 浏览器只在采集合同声明时读取真实媒体状态。
    if "media_state" in directive.artifact_types:
        supplied.artifacts.append(PageCaptureArtifact("media_state", None, {"media": []}))
    action = replace(
        _page_result(mode=PageCaptureMode.OBSERVE),
        network_requests=[{"url": "https://example.test/archives/1", "status": 200}],
    )
    captured = _merge_action_evidence(
        capture_result=supplied, action_result=action,
        required_types=set(directive.artifact_types),
    )
    contents = {a.artifact_type: str(a.content).encode() for a in captured.artifacts}
    artifacts = tuple(
        replace(_artifact(artifact_id=kind, artifact_type=kind),
                size_bytes=len(data), checksum="sha256:" + sha256(data).hexdigest())
        for kind, data in contents.items()
    )
    manifest = _pending_manifest(artifacts)
    requirements = create_payload_requirement_set(
        task_id=manifest.task_id, agent_run_id="run", session_id=manifest.session_id,
        finalization_epoch=0, capture_id=manifest.capture_id, policy_hash="a" * 64,
        capture_role="primary", committed_artifact_types=tuple(contents), created_at=NOW,
    )
    business = tuple(build_active_session_evidence_requirement(
        task_id=manifest.task_id, capture_id=manifest.capture_id, rule_id=code, rule_code=code,
        required_artifact_types=types, required_capture_roles=("primary",),
        status=EvidenceExitRequirementStatus.PENDING,
    ) for code, types in (("alioth_29", ("har",)), ("alioth_ops_3", ("media_state",))))
    requirements = rebuild_payload_requirement_set(
        requirements, requirements=(*requirements.requirements, *business), created_at=NOW,
    )

    class Store:
        async def read(self, path):
            return contents[path.rsplit("/", 1)[-1].split(".")[0]]

    checked = await adjudicate_web_payload_manifest(
        manifest=manifest, requirements=requirements, artifact_store=Store(),
        phase="classified_rule_requirements", now=NOW,
    )
    assert checked.quality_status.value == "pass"
    har = next(a for a in captured.artifacts if a.artifact_type == "har")
    assert har.content["entries"] == [
        {"url": "https://example.test/archives/1", "status": 200}
    ]
    # 缺失证据仍须失败，禁止通过降低门禁掩盖采集缺口。
    missing = _pending_manifest(tuple(a for a in artifacts if a.artifact_type != "har"))
    failed = await adjudicate_web_payload_manifest(
        manifest=missing, requirements=requirements, artifact_store=Store(),
        phase="classified_rule_requirements", now=NOW,
    )
    assert failed.quality_status.value == "fail"


@pytest.mark.asyncio
async def test_runtime_menu_summary_includes_discovered_success_failure_and_budget():
    from services.api.app.application.web_runtime_composition import (
        ProductionWebV2RuntimeExecutor,
        WebV2Runtime,
    )
    from services.api.app.core.config import Settings
    from services.api.tests.test_web_runtime_composition import _web_record

    rows = [
        {"entry_id": str(i), "status": status, "source_kind": "runtime_discovered"}
        for i, status in enumerate(["detail_captured"] * 9 + ["navigation_failed"]
                                   + ["menu_limit_skipped"] * 22
                                   + ["runtime_discovered_unreviewed"] * 2)
    ]
    summary = {"menu_total": 1, "frontier_remaining": 0,
               "menu_ledger": [{"entry_id": "home", "status": "home_baseline"}],
               "runtime_discovered_ledger": rows}

    class Bootstrapper:
        async def start(self, command):
            return SimpleNamespace(after_capture=SimpleNamespace(capture_id="home"))

    class Explorer:
        async def explore(self, command, **kwargs):
            return SimpleNamespace(page_timeline=("home", "detail"), page_type_keys=("home",),
                                   failed_urls=(), to_dict=lambda: summary)

    class Runtime:
        async def close_exploration_session(self, **kwargs):
            pass

    result = await ProductionWebV2RuntimeExecutor(worker_instance_id="worker").run(
        WebV2Runtime(bootstrapper=Bootstrapper(), capture_runtime=Runtime(), explorer=Explorer()),
        record=_web_record(), settings=Settings(),
    )
    assert "35 个菜单终态" in result.final_text
    assert "9 个详情采集成功" in result.final_text
    assert "22 个因菜单额度跳过" in result.final_text
    assert "1 个导航页面需复核" in result.final_text
