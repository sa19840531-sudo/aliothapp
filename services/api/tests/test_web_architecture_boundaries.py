"""Web V2 专属模块不得直接依赖 H5/App Explorer。"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WEB_MODULE_DIRS = (
    ROOT / "services/api/app/application",
    ROOT / "services/api/app/agents/explorer",
    ROOT / "services/api/app/domain/page",
    ROOT / "services/api/app/domain/exploration",
)


def web_architecture_violations() -> tuple[str, ...]:
    violations: list[str] = []
    for directory in WEB_MODULE_DIRS:
        for path in directory.glob("web_*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                module = ""
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                elif isinstance(node, ast.Import):
                    module = " ".join(alias.name for alias in node.names)
                if ".application.h5_" in module or ".application.app_" in module:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{module}")
    return tuple(violations)


def test_web_modules_do_not_import_h5_or_app_explorers() -> None:
    assert web_architecture_violations() == ()


def test_architecture_h5_and_app_composition_do_not_import_web_budget_ledger() -> None:
    forbidden = {
        "services.api.app.application.web_exploration_budget_ledger",
        "services.api.app.application.web_evidence_bundle",
        "services.api.app.infrastructure.db.web_exploration_budget_ledger_repository",
        "services.api.app.infrastructure.db.web_capture_evidence_repository",
    }
    violations: list[str] = []
    for relative_path in (
        "services/api/app/application/h5_runtime_composition.py",
        "services/api/app/application/app_runtime_composition.py",
    ):
        path = ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") in forbidden:
                violations.append(f"{relative_path}:{node.lineno}:{node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden:
                        violations.append(f"{relative_path}:{node.lineno}:{alias.name}")

    assert violations == []


def test_shared_task_finalization_repository_composes_web_gate_sql() -> None:
    """通用 Finalization 仓储不得内嵌 Web Capture 表结构。"""

    shared_repository = (
        ROOT / "services/api/app/infrastructure/db/task_finalization_repository.py"
    ).read_text(encoding="utf-8")
    web_sql = (
        ROOT / "services/api/app/infrastructure/db/web_completion_gate_sql.py"
    ).read_text(encoding="utf-8")

    assert "qc_page_capture" not in shared_repository
    assert "WEB_COMPLETION_GATE_INVENTORY_CTES_SQL" in shared_repository
    assert "qc_page_capture" in web_sql


def test_web_explorer_skill_keeps_canonical_types_and_runtime_boundaries() -> None:
    content = (
        ROOT / "services/api/app/agents/skills/web-explorer-workflow/SKILL.md"
    ).read_text(encoding="utf-8")

    for page_type in (
        "video_list",
        "video_detail",
        "content_list",
        "content_detail",
    ):
        assert page_type in content
    for marker in (
        "当前可见层",
        "成年入口",
        "到达验证",
        "候选不是到达证明",
        "质检通知",
        "不得直接调用 Playwright",
    ):
        assert marker in content
    for forbidden in (
        "heiliao.com",
        "hl365.com",
        "91jav1.com",
        "91porna.com",
        "huangguoai.com",
        "avhub360.com",
        "shell",
        "javascript:",
    ):
        assert forbidden not in content.casefold()
