"""App QC Foundation 的依赖边界回归测试。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.task import TaskTargetPlatform

APP_ROOT = Path(__file__).parents[1] / "app"
_APP_PACKAGE_PARTS = ("services", "api", "app")


def _python_paths(relative_root: Path) -> tuple[Path, ...]:
    """返回现存 Python 文件，防止目标模块改名后边界测试静默通过。"""
    root = APP_ROOT / relative_root
    if root.is_file():
        return (root,)
    if root.is_dir():
        return tuple(root.rglob("*.py"))
    raise AssertionError(f"架构边界目标不存在：{relative_root.as_posix()}")


def _find_forbidden_imports(
    relative_root: Path,
    forbidden_fragments: frozenset[str],
) -> list[str]:
    """返回目标目录内命中禁用依赖的导入位置。"""
    violations: list[str] = []
    for path in _python_paths(relative_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module_name in _resolved_import_modules(path, node):
                if any(
                    _module_matches_forbidden(module_name, fragment)
                    for fragment in forbidden_fragments
                ):
                    relative_path = path.relative_to(APP_ROOT).as_posix()
                    violations.append(f"{relative_path}:{node.lineno}:{module_name}")
    return violations


def _resolved_import_modules(
    path: Path,
    node: ast.Import | ast.ImportFrom,
) -> tuple[str, ...]:
    """将普通和相对 from-import 还原为包含 alias 的完整模块路径。"""
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)

    module_parts = tuple(part for part in (node.module or "").split(".") if part)
    if node.level:
        relative_parent = path.relative_to(APP_ROOT).parent
        package_parts = _APP_PACKAGE_PARTS + tuple(relative_parent.parts)
        keep_count = len(package_parts) - (node.level - 1)
        if keep_count < 0:
            return ()
        base_parts = package_parts[:keep_count] + module_parts
    else:
        base_parts = module_parts

    resolved: list[str] = []
    for alias in node.names:
        alias_parts = () if alias.name == "*" else tuple(alias.name.split("."))
        candidate_parts = base_parts + alias_parts
        if candidate_parts:
            resolved.append(".".join(candidate_parts))
    return tuple(resolved)


def _module_matches_forbidden(module_name: str, forbidden_module: str) -> bool:
    """按完整路径段匹配；星号只允许表达单个段的受限后缀通配。"""
    module_parts = tuple(part.casefold() for part in module_name.split(".") if part)
    forbidden_parts = tuple(
        part.casefold() for part in forbidden_module.split(".") if part
    )
    if not forbidden_parts or len(forbidden_parts) > len(module_parts):
        return False
    width = len(forbidden_parts)
    return any(
        all(
            _module_segment_matches(module_part, forbidden_part)
            for module_part, forbidden_part in zip(
                module_parts[index : index + width],
                forbidden_parts,
                strict=True,
            )
        )
        for index in range(len(module_parts) - width + 1)
    )


def _module_segment_matches(module_part: str, forbidden_part: str) -> bool:
    """精确匹配单段，或处理形如 page_capture* 的非空前缀。"""
    if "*" not in forbidden_part:
        return module_part == forbidden_part
    if forbidden_part.count("*") != 1 or not forbidden_part.endswith("*"):
        return False
    prefix = forbidden_part[:-1]
    return bool(prefix) and module_part.startswith(prefix)


def _find_environment_reads(
    relative_root: Path,
    *,
    variable_name: str,
    excluded_paths: frozenset[str] = frozenset(),
) -> list[str]:
    """用 AST 定位对指定环境变量的直接读取，避免注释或审计字段误报。"""
    violations: list[str] = []
    for path in _python_paths(relative_root):
        relative_path = path.relative_to(APP_ROOT).as_posix()
        if relative_path in excluded_paths:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_getenv_names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "os"
            for alias in node.names
            if alias.name == "getenv"
        }
        imported_environ_names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "os"
            for alias in node.names
            if alias.name == "environ"
        }
        os_module_names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "os"
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                key = node.slice
                reads_environ = (
                    isinstance(node.value, ast.Name)
                    and node.value.id in imported_environ_names
                ) or (
                    isinstance(node.value, ast.Attribute)
                    and node.value.attr == "environ"
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id in os_module_names
                )
                if (
                    reads_environ
                    and isinstance(key, ast.Constant)
                    and key.value == variable_name
                ):
                    violations.append(f"{relative_path}:{node.lineno}")
                continue
            if not isinstance(node, ast.Call):
                continue
            first_argument = _environment_key_argument(node)
            if not (
                isinstance(first_argument, ast.Constant)
                and first_argument.value == variable_name
            ):
                continue
            direct_getenv = (
                isinstance(node.func, ast.Name)
                and node.func.id in imported_getenv_names
            )
            os_getenv = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "getenv"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in os_module_names
            )
            environ_get = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and (
                    (
                        isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "environ"
                        and isinstance(node.func.value.value, ast.Name)
                        and node.func.value.value.id in os_module_names
                    )
                    or (
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id in imported_environ_names
                    )
                )
            )
            if direct_getenv or os_getenv or environ_get:
                violations.append(f"{relative_path}:{node.lineno}")
    return violations


def _environment_key_argument(node: ast.Call) -> ast.expr | None:
    """返回 getenv／environ.get 的位置 key 或显式 key= 参数。"""
    if node.args:
        return node.args[0]
    return next(
        (keyword.value for keyword in node.keywords if keyword.arg == "key"),
        None,
    )


def _find_constructor_calls(
    relative_root: Path,
    *,
    class_names: frozenset[str],
) -> list[str]:
    """返回目标目录内直接构造指定类的调用位置。"""
    violations: list[str] = []
    root = APP_ROOT / relative_root
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in class_names:
                relative_path = path.relative_to(APP_ROOT)
                violations.append(f"{relative_path}:{node.lineno}:{name}")
    return violations


def test_explorer_cannot_import_concrete_driver_implementations() -> None:
    """Explorer 只能产出 ActionIntent，不能绕过执行器操作页面。"""
    violations = _find_forbidden_imports(
        Path("agents/explorer"),
        frozenset(
            {
                "playwright",
                "appium",
                "airtest",
                "mobile_demo*",
                "surface_driver*",
                "android_surface_driver",
                "app_runtime*",
                "app_v2_runtime",
                "app_workflow*",
                "app_bootstrap_repository",
                "app_screenshot_understanding",
                "artifact_reader",
                "page_capture*",
                "web_agent_page_capture",
                "capture_runtime",
                "android_capture_runtime",
            }
        ),
    )

    assert violations == []


def test_rule_judge_cannot_import_driver_or_app_runtime() -> None:
    """Rule Judge 只能消费证据，不能自行打开页面或调用 Driver。"""
    violations = _find_forbidden_imports(
        Path("agents/rule_judge"),
        frozenset(
            {
                "surface_driver*",
                "android_surface_driver",
                "mobile_demo*",
                "app_runtime*",
                "app_v2_runtime",
                "app_workflow*",
                "app_bootstrap_repository",
                "app_screenshot_understanding",
                "artifact_reader",
                "page_capture*",
                "web_agent_page_capture",
                "capture_runtime",
                "android_capture_runtime",
                "playwright",
            }
        ),
    )

    assert violations == []


def test_app_exploration_module_cannot_import_or_publish_formal_inspection() -> None:
    """探索子流程不得在 Evidence Index 封存前创建正式质检库存。"""

    source = (
        APP_ROOT / "application" / "app_screenshot_exploration.py"
    ).read_text(encoding="utf-8")

    assert "app_inspection_publisher" not in source
    assert "AppCaptureInspectionPort" not in source
    assert "_publish_inspection" not in source


def test_agent_tools_must_not_construct_playwright_browser() -> None:
    """Agent Tool 不得自行构造 Playwright；只能经 Web Interaction Runtime 借用。"""
    violations = _find_constructor_calls(
        Path("agents/tools"),
        class_names=frozenset({"PlaywrightPageCaptureBrowser"}),
    )
    assert violations == []


def test_agent_tools_must_not_import_raw_playwright_package() -> None:
    """Agent Tool 不得直接依赖 playwright 包 API。"""
    violations = _find_forbidden_imports(
        Path("agents/tools"),
        frozenset({"playwright"}),
    )
    assert violations == []


def test_page_capture_tools_borrows_browser_from_web_runtime() -> None:
    """page_capture_tools 必须经 Web Runtime CaptureCoordinator 双采。"""
    source = (
        APP_ROOT / "agents" / "tools" / "page_capture_tools.py"
    ).read_text(encoding="utf-8")
    assert "get_or_create_web_capture_runtime" in source
    assert "run_agent_page_capture" in source
    assert "AgentPageCaptureSpec" in source
    assert (
        "coordinate_agent_page_capture" in source
        or "execute_agent_page_capture_with_browser" in source
    )
    assert "PlaywrightPageCaptureBrowser(" not in source


def test_app_capture_profile_allows_ui_tree_without_browser_dom() -> None:
    """App 可持久化由可访问性层投影的 visible_text，但不得伪装 DOM/OCR。"""
    profile = CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP)
    requested = set(profile.before.artifact_types) | set(profile.after.artifact_types)

    assert {"screenshot", "action_trace", "ui_tree", "ui_snapshot"}.issubset(requested)
    assert {"device_log", "network_trace", "har", "fault_artifact"}.issubset(requested)
    assert "visible_text" in requested
    assert requested.isdisjoint({"dom", "ocr_text"})



def test_production_worker_routes_app_before_generic_main_agent() -> None:
    """Screenshot Exploration 未接 Handoff 前，旧 Worker 不得执行半条 App 流程。"""
    worker = (APP_ROOT / "worker.py").read_text(encoding="utf-8")

    assert "AppWorkflowRouter" in worker
    assert "AppWorkflowDispatcher" in worker
    assert "WorkflowRoute.APP" in worker
    assert "run_main_agent" in worker
    assert "FakeAppRuntime" not in worker
    assert "SurfaceDriver" not in worker


def test_fake_runtime_is_only_constructed_behind_environment_gate() -> None:
    """Fake Provider 只能由专用工厂创建，且生产分支必须在构造前拒绝。"""
    factory = (
        APP_ROOT / "infrastructure" / "app_runtime" / "factory.py"
    ).read_text(encoding="utf-8")
    fake_constructor = factory.index("runtime = FakeAppRuntime")
    production_gate = factory.index('self._environment in {"prod", "production"}')

    assert production_gate < fake_constructor
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT).as_posix()
        if relative in {
            "infrastructure/app_runtime/factory.py",
            "infrastructure/app_runtime/fake.py",
        }:
            continue
        text = path.read_text(encoding="utf-8")
        assert "FakeAppRuntime(" not in text, relative


def test_android_capture_runtime_holds_private_driver_only() -> None:
    """排期第 12 列 APP：Driver 只能由 Android Interaction Runtime 私有持有。"""
    source = (
        APP_ROOT / "infrastructure" / "device" / "android_capture_runtime.py"
    ).read_text(encoding="utf-8")
    assert "_driver: AndroidAdbSurfaceDriver" in source
    assert "class AndroidCaptureRuntime" in source
    assert "class MobileDemoAndroidRuntime" in source
    assert "CaptureCoordinator" in source
    assert "build_mobile_demo_android_runtime" in source
    assert "build_default_android_capture_runtime" in source
    # 禁止对外公开可赋值的 driver 字段
    assert "\ndriver: AndroidAdbSurfaceDriver" not in source
    assert "def get_driver(" not in source
    assert "def borrow_driver(" not in source


def test_agent_ad_capture_must_go_through_capture_coordinator() -> None:
    """未知 APK agent_workflow：决策后动作必须经 CaptureCoordinator。"""
    path = APP_ROOT / "api" / "v1" / "mobile_demo_agent_ad_capture.py"
    if not path.exists():
        pytest.skip("Mobile Demo Agent Ad Capture 已下线，正式链路改由 App Workflow 承接")
    source = path.read_text(encoding="utf-8")
    assert "CaptureCoordinator" in source
    assert "coordinate_intent" in source
    assert "build_mobile_demo_android_runtime" in source
    assert "AndroidAdbSurfaceDriver" in source
    tree = ast.parse(source, filename=str(path))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "subprocess" not in imported
    # 允许从 adb_ops 仅导入坐标常量，不得构造 AdbDeviceOps
    assert "AdbDeviceOps(" not in source
    assert "subprocess.run" not in source
    assert "subprocess.Popen" not in source


def test_mobile_demo_ai_driver_must_not_call_adb_directly() -> None:
    """AI POC 不得直呼 adb screencap / input；只能经 Runtime。"""
    path = APP_ROOT / "mobile_demo_ai_driver.py"
    if not path.exists():
        pytest.skip("Mobile Demo AI Driver 已下线，正式链路改由 App Workflow 承接")
    source = path.read_text(encoding="utf-8")
    assert "observe_via_runtime" in source
    assert "apply_action_via_runtime" in source
    assert "CaptureCoordinator" in source
    tree = ast.parse(source, filename=str(path))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "subprocess" not in imported
    assert "subprocess.run" not in source
    assert "subprocess.Popen" not in source
    assert '["shell", "input"' not in source


def test_web_capture_runtime_holds_private_browser_and_driver() -> None:
    """Web Interaction Runtime 私有持有浏览器与 SurfaceDriver Adapter。"""
    source = (
        APP_ROOT / "infrastructure" / "browser" / "capture_runtime.py"
    ).read_text(encoding="utf-8")
    assert "_browser:" in source
    assert "_executor:" in source
    assert "run_agent_page_capture" in source
    assert "PlaywrightSurfaceDriverAdapter" in source
    assert "get_or_create_web_capture_runtime" in source
    # 禁止对外公开可赋值的 browser 字段
    assert "\nbrowser: PlaywrightPageCaptureBrowser" not in source


def test_h5_visual_detector_cannot_import_browser_or_database() -> None:
    """视觉检测器只解析专用证据，不能持有浏览器或数据库实现。"""
    violations = _find_forbidden_imports(
        Path("agents/tools/h5_visual_navigation_detection.py"),
        frozenset({"playwright", "infrastructure.db", "sqlalchemy", "asyncpg"}),
    )

    assert violations == []


def test_h5_visual_domain_cannot_import_agent_framework_or_opencv() -> None:
    """视觉领域合同必须保持纯 Python，不能依赖模型框架或图像实现。"""
    visual_violations: list[str] = []
    for relative_path in (
        Path("domain/exploration/h5_visual_navigation.py"),
        Path("domain/exploration/h5_logical_surface.py"),
    ):
        visual_violations.extend(
            _find_forbidden_imports(
                relative_path,
                frozenset({"agent_framework", "cv2", "opencv"}),
            )
        )

    assert visual_violations == []


def test_h5_visual_image_runtime_cannot_import_page_catalog_or_workflow() -> None:
    """图像 Runtime 只做复验和转换，不能反向依赖页型目录或业务流程。"""
    violations = _find_forbidden_imports(
        Path("infrastructure/browser/h5_visual_navigation_runtime.py"),
        frozenset(
            {
                "domain.page",
                "page_type_catalog",
                "page_type_registry",
                "h5_exploration_workflow",
                "application.workflow",
            }
        ),
    )

    assert violations == []


def test_h5_visual_mode_environment_is_read_only_by_core_config() -> None:
    """业务代码只能消费装配后的模式值，不能绕过 core config 读取环境变量。"""
    violations = _find_environment_reads(
        Path("."),
        variable_name="ALIOTH_H5_VISUAL_COORDINATE_MODE",
        excluded_paths=frozenset({"core/config.py"}),
    )

    assert violations == []


def test_forbidden_import_scan_resolves_from_alias_and_relative_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from-import 的别名和相对层级都必须还原成完整模块后再匹配。"""
    app_root = tmp_path / "app"
    fixture = app_root / "agents" / "tools" / "probe.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        "from services.api.app.infrastructure import db\n"
        "from ...infrastructure import db as relative_db\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "services.api.tests.test_app_architecture_boundaries.APP_ROOT",
        app_root,
    )

    violations = _find_forbidden_imports(
        Path("agents/tools/probe.py"),
        frozenset({"infrastructure.db"}),
    )

    assert violations == [
        "agents/tools/probe.py:1:services.api.app.infrastructure.db",
        "agents/tools/probe.py:2:services.api.app.infrastructure.db",
    ]


def test_forbidden_import_scan_matches_segments_without_substring_false_positive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """domain.page 边界不得误伤名称相近但不同段的 page_* 模块。"""
    app_root = tmp_path / "app"
    fixture = app_root / "domain" / "probe.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        "import services.api.app.domain.page_types\n"
        "from services.api.app.domain import page_helpers\n"
        "import services.api.app.unrelated.safe_page_capture_tools\n"
        "import services.api.app.unrelated.safe_app_workflow_router\n"
        "import services.api.app.unrelated.safe_mobile_demo_ai_driver\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "services.api.tests.test_app_architecture_boundaries.APP_ROOT",
        app_root,
    )

    violations = _find_forbidden_imports(
        Path("domain/probe.py"),
        frozenset(
            {
                "domain.page",
                "page_capture*",
                "app_workflow*",
                "mobile_demo*",
            }
        ),
    )

    assert violations == []


def test_forbidden_import_scan_supports_explicit_single_segment_module_families(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受限后缀通配只扩展同一模块段，并覆盖已登记的架构模块族。"""
    app_root = tmp_path / "app"
    fixture = app_root / "agents" / "explorer" / "probe.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        "import services.api.app.agents.tools.page_capture_tools\n"
        "from services.api.app.infrastructure.db import page_capture_repository\n"
        "from services.api.app.application import app_workflow_router\n"
        "from services.api.app import mobile_demo_ai_driver\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "services.api.tests.test_app_architecture_boundaries.APP_ROOT",
        app_root,
    )

    violations = _find_forbidden_imports(
        Path("agents/explorer/probe.py"),
        frozenset({"page_capture*", "app_workflow*", "mobile_demo*"}),
    )

    assert violations == [
        "agents/explorer/probe.py:1:services.api.app.agents.tools.page_capture_tools",
        "agents/explorer/probe.py:2:services.api.app.infrastructure.db.page_capture_repository",
        "agents/explorer/probe.py:3:services.api.app.application.app_workflow_router",
        "agents/explorer/probe.py:4:services.api.app.mobile_demo_ai_driver",
    ]


def test_environment_read_scan_accepts_positional_and_key_keyword(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """os.getenv 的位置参数和 key= 写法都必须命中同一配置边界。"""
    app_root = tmp_path / "app"
    fixture = app_root / "application" / "probe.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        "import os\n"
        'os.getenv("ALIOTH_H5_VISUAL_COORDINATE_MODE")\n'
        'os.getenv(key="ALIOTH_H5_VISUAL_COORDINATE_MODE")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "services.api.tests.test_app_architecture_boundaries.APP_ROOT",
        app_root,
    )

    violations = _find_environment_reads(
        Path("application/probe.py"),
        variable_name="ALIOTH_H5_VISUAL_COORDINATE_MODE",
    )

    assert violations == ["application/probe.py:2", "application/probe.py:3"]
