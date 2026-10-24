"""H5 纯探索合同与运行时架构回归。"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from services.api.app.agents.explorer import h5_contracts
from services.api.app.application.h5_exploration_turn import H5ExplorationTurnRunner
from services.api.app.application.h5_runtime_composition import (
    H5RuntimePort,
    H5V2Runtime,
    PersistingH5PageDetector,
    ProductionH5V2RuntimeExecutor,
)
from services.api.app.core.config import Settings
from services.api.app.infrastructure.browser.capture_runtime import (
    build_h5_exploration_capture_coordinator,
)
from services.api.tests.test_h5_runtime_composition import (
    _completed_outcome,
    _h5_record,
    _runtime_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PURE_H5_MODULES = (
    "services/api/app/agents/explorer/h5_contracts.py",
    "services/api/app/application/h5_exploration_turn.py",
    "services/api/app/application/h5_exploration_observation.py",
    "services/api/app/application/h5_runtime_composition.py",
    "services/api/app/application/h5_task_runtime_settings.py",
    "services/api/app/domain/exploration/h5_frontier.py",
    "services/api/app/application/orchestration/exploration_service.py",
)
FORBIDDEN_H5_EXPLORATION_SYMBOLS = {
    "ProbeWork",
    "QcOrchestrator",
    "PostgresProbeWorkPublisher",
    "SupplementalEvidenceOperation",
    "build_current_supplemental_evidence_policy",
    "rule_target_page_types",
    "EvidenceExitGateBlockedError",
    "EvidenceExitBusinessOutcome",
    "H5EvidenceHandoffPort",
    "evidence_closure",
    "h5_candidate_requests_ad_landing_evidence",
    "capture_ad_landing_evidence",
    "InventoryPersistencePort",
    "H5EvidenceInventoryItem",
    "H5EvidenceInventoryStatus",
    "PostgresH5EvidenceInventoryRepository",
    "ad_landing_capability",
}


def test_h5_agent_contract_accepts_only_exploration_work() -> None:
    contract = getattr(h5_contracts, "H5ExplorerAgent", None)
    assert contract is not None
    annotation = inspect.signature(contract.decide_next).parameters["work"].annotation
    assert annotation == "ExplorationWork"


def test_h5_runtime_public_contract_has_no_work_type_selector() -> None:
    for method in (
        H5ExplorationTurnRunner.run_one,
        H5RuntimePort.run_one_turn,
        H5V2Runtime.run_one_turn,
    ):
        assert "work_type" not in inspect.signature(method).parameters
    assert "probe_claim_enabled" not in inspect.signature(ProductionH5V2RuntimeExecutor).parameters

    assert "evidence_closure" not in inspect.signature(ProductionH5V2RuntimeExecutor).parameters


@pytest.mark.parametrize("relative_path", PURE_H5_MODULES)
def test_h5_pure_exploration_modules_have_no_inspection_dependency_symbols(
    relative_path: str,
) -> None:
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            names.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)

    assert names.isdisjoint(FORBIDDEN_H5_EXPLORATION_SYMBOLS)


def test_regression_rules_document_current_h5_pure_exploration_boundary() -> None:
    text = (REPO_ROOT / "docs/专项能力与高频回归规则.md").read_text(encoding="utf-8")
    assert "H5 主探索与 Probe 补采必须按能力边界解耦" not in text
    assert "H5 探索必须保持纯探索、零质检语义" in text
    assert "H5 Session 关闭前必须先形成终态 Evidence Inventory" not in text
    assert "H5 Session 关闭不得等待质检库存" in text


def test_h5_capture_coordinator_has_no_quality_exit_gate() -> None:
    source = inspect.getsource(build_h5_exploration_capture_coordinator)
    assert "PostgresEvidenceExitGate" not in source
    assert "action_recovery_captures=action_recovery_captures" in source


def test_h5_runtime_uses_dedicated_pure_exploration_close() -> None:
    source = inspect.getsource(H5V2Runtime.close)
    assert "close_h5_exploration_session" in source
    assert "capture_runtime.close_session" not in source


def test_h5_page_detection_writeback_skips_evidence_readiness() -> None:
    source = inspect.getsource(PersistingH5PageDetector.detect)
    assert "finalize_evidence_readiness=False" in source


def test_worker_keeps_handoff_only_after_exploration_stage() -> None:
    tree = ast.parse((REPO_ROOT / "services/api/app/worker.py").read_text(encoding="utf-8"))

    def calls_named(name: str) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name
            )
        ]

    builder_calls = calls_named("build_h5_workflow_handler")
    assert len(builder_calls) == 1
    builder_keywords = {item.arg for item in builder_calls[0].keywords}
    assert "evidence_closure" not in builder_keywords

    process_calls = calls_named("_process_h5_v2_one")
    assert len(process_calls) == 1
    h5_keywords = {item.arg: item.value for item in process_calls[0].keywords}
    handoff = h5_keywords["h5_evidence_handoff"]
    assert isinstance(handoff, ast.Name)
    assert handoff.id == "h5_evidence_handoff"

    web_calls = calls_named("_process_web_v2_one")
    assert len(web_calls) == 1
    web_keywords = {item.arg: item.value for item in web_calls[0].keywords}
    assert "h5_evidence_handoff" not in web_keywords
    reconciliation = web_keywords["web_inspection_reconciliation"]
    assert isinstance(reconciliation, ast.Name)
    assert reconciliation.id == "web_inspection_reconciliation"


@pytest.mark.asyncio
async def test_executor_consumes_only_exploration_until_terminal() -> None:
    calls: list[str] = []

    class Runtime:
        runtime_settings = _runtime_settings(max_actions=3)

        async def start(self, command):
            del command
            return None

        async def run_one_turn(self, *, task_id, agent_run_id):
            del task_id, agent_run_id
            calls.append("exploration")
            return _completed_outcome()

        async def close(self, *, task_id, session_id):
            del task_id, session_id

    result = await ProductionH5V2RuntimeExecutor(worker_instance_id="worker-h5").run(
        Runtime(), record=_h5_record(), settings=Settings()
    )

    assert calls == ["exploration"]
    assert result.working_memory.step_count == 1
