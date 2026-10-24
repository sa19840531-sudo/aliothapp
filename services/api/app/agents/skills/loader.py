"""Skill 加载器：将 phase/、rule/ 与任务级 Workflow Skill 文档
包装为 Agent 可用的 ContextProvider。

本模块严格遵循 [Agent Skills 规范](https://agentskills.io)：每个含 ``SKILL.md`` 的
子目录即一个 Skill，Agent 通过渐进式披露（L1 摘要常驻系统提示词、L2 通过框架内置
的 ``load_skill`` 工具按需加载完整正文、L3 通过 ``read_skill_resource`` 加载附属
资源）读取，不直接执行——这与产生副作用/返回结构化数据的
[../tools/](../tools/README.md)（function calling 工具）是完全不同的两个概念，
二者的区别与命名沿革详见 [README.md](README.md)。

``SkillsProvider`` 目前是 Agent Framework 的 experimental API；1.10.0 版本行为已实测
可用（按 ``skill_paths`` 传入的父目录，向下扫描最多 2 层查找 ``SKILL.md`` 文件，
每个含 ``SKILL.md`` 的子目录即一个可被模型渐进式加载的 Skill），但后续升级
 Agent Framework 版本时需重新验证其签名与行为是否变化。
"""

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path

from agent_framework import ContextProvider, FunctionTool, Skill, SkillsProvider

from services.api.app.application.app_workflow_router import WorkflowRoute
from services.api.app.application.workflow_skill_routing import skill_sub_dirs_for_route

_SKILLS_ROOT = Path(__file__).resolve().parent
_DEFAULT_SUB_DIRS = (
    "phase",
    "rule",
    "qc-workflow",
    "app-workflow",
    "web-explorer-workflow",
    "navigation-boundary-skill",
    "green-content-safety",
    "green-process-evidence",
)
_READ_ONLY_SKILL_TOOL_NAMES = frozenset(
    {
        SkillsProvider.LOAD_SKILL_TOOL_NAME,
        SkillsProvider.READ_SKILL_RESOURCE_TOOL_NAME,
    }
)
APP_WORKFLOW_SKILL_VERSION = "1.7.0"
H5_EXPLORER_WORKFLOW_SKILL_VERSION = "1.1.0"
WEB_EXPLORER_WORKFLOW_SKILL_VERSION = "1.0.0"
_APP_WORKFLOW_REQUIRED_MARKERS = (
    "install → open → bootstrap capture → APP_READY",
    "APP_READY 不是任务终态",
    "不得调用 Driver",
    "qc_app_capture",
    "动态 Frontier",
    "页面类型覆盖缺口",
    "不得按 APK 文件名或包名选择固定 Playbook",
    "探索操作规程",
    "界面结构优先",
    "视觉识别降级",
    "滑动后重新定位",
    "系统弹窗",
    "外部 Activity",
    "返回流程",
    "POST_SCROLL_REBIND_REQUIRED",
    "APP_READY ≠ 业务首页到达",
    "external_activity_kind",
    "app_external_return_failed",
    "app_external_restore_loop",
    "长图与视口证据",
    "广告外跳取证",
    "收银台长链",
    "播放器连续拖动",
    "viewport_primary",
    "long_page_scroll_series",
    "ad_outbound_triplet",
    "checkout_chain_step",
    "player_scrub_series",
)


class AppWorkflowSkillConfigurationError(RuntimeError):
    """App Explorer 所需 Skill 缺失、损坏或版本不兼容。"""


class H5ExplorerWorkflowSkillConfigurationError(RuntimeError):
    """H5 Explorer 所需 Skill 缺失、损坏或版本不兼容。"""


class WebExplorerWorkflowSkillConfigurationError(RuntimeError):
    """Web Explorer 所需 Skill 缺失、损坏或版本不兼容。"""


class AliothSkillsProvider(SkillsProvider):
    """Alioth 运行时 Skill Provider：只读 Skill 加载不进入审批流。"""

    def _create_tools(self, skills: Sequence[Skill]) -> list[FunctionTool]:
        tools = super()._create_tools(skills)
        for skill_tool in tools:
            if skill_tool.name in _READ_ONLY_SKILL_TOOL_NAMES:
                skill_tool.approval_mode = "never_require"
        return tools

    async def load_skill(self, skill_name: str) -> str:
        """按框架 ``load_skill`` 的同一来源确定性加载完整 Skill 正文。"""
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise ValueError("skill_name 必须为非空字符串")
        skills, _instructions, _tools = await self._get_or_create_context()
        skill = self._find_skill(skills, skill_name)
        if skill is None:
            raise ValueError(f"Skill '{skill_name}' 不存在")
        content = await self._load_skill(skills, skill_name)
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Skill '{skill_name}' 正文为空")
        return content


def build_skills_context_provider(
    *,
    sub_dirs: tuple[str, ...] = _DEFAULT_SUB_DIRS,
    source_id: str = "alioth-skills",
) -> ContextProvider:
    """构建挂载阶段、规则和任务级 Workflow Skill 的 ``ContextProvider``。

    返回值可直接传给 ``Agent(context_providers=[...])``；Agent 会在系统提示中看到
    每个 Skill 的 name/description（Agent Skills 规范的"L1 摘要"），并可按需通过
    框架内置的 ``load_skill`` 工具读取某个 Skill 的完整正文。
    """
    paths = [str(_SKILLS_ROOT / sub_dir) for sub_dir in sub_dirs]
    return AliothSkillsProvider.from_paths(paths, source_id=source_id)


def validate_app_workflow_skill(skill_dir: Path | None = None) -> Path:
    """在构建 App Explorer 前校验强制 Skill，禁止框架静默跳过。"""
    directory = skill_dir or (_SKILLS_ROOT / "app-workflow")
    skill_path = directory / "SKILL.md"
    try:
        content = skill_path.read_text(encoding="utf-8")
    except OSError as error:
        raise AppWorkflowSkillConfigurationError(
            "App Explorer 缺少 app-workflow/SKILL.md"
        ) from error
    validate_app_workflow_skill_content(content)
    return skill_path


def validate_app_workflow_skill_content(content: str) -> str:
    """校验通过 Provider 实际加载的 App Workflow Skill 完整正文。"""
    if not isinstance(content, str) or not content.strip():
        raise AppWorkflowSkillConfigurationError("app-workflow Skill 正文为空")
    frontmatter = re.match(r"\A---\r?\n(?P<body>.*?)\r?\n---\r?\n", content, re.DOTALL)
    if frontmatter is None:
        raise AppWorkflowSkillConfigurationError("app-workflow Skill frontmatter 非法")
    header = frontmatter.group("body")
    name = re.search(r"(?m)^name:\s*([^\r\n]+)$", header)
    version = re.search(r"(?m)^\s{2}version:\s*['\"]?([^'\"\r\n]+)", header)
    if name is None or name.group(1).strip() != "app-workflow":
        raise AppWorkflowSkillConfigurationError("app-workflow Skill name 不匹配")
    if version is None or version.group(1).strip() != APP_WORKFLOW_SKILL_VERSION:
        raise AppWorkflowSkillConfigurationError(
            "app-workflow Skill version 不兼容，要求 " + APP_WORKFLOW_SKILL_VERSION
        )
    missing = [marker for marker in _APP_WORKFLOW_REQUIRED_MARKERS if marker not in content]
    if missing:
        raise AppWorkflowSkillConfigurationError(
            "app-workflow Skill 缺少强制边界内容：" + ", ".join(missing)
        )
    return content


def build_app_workflow_context_provider() -> ContextProvider:
    """校验 App Workflow，并挂载 APP 路由允许的绿色规则 Skill。"""
    validate_app_workflow_skill()
    return build_skills_context_provider(
        sub_dirs=skill_sub_dirs_for_route(WorkflowRoute.APP),
        source_id="app-workflow",
    )


def validate_h5_explorer_workflow_skill_content(content: str) -> str:
    """校验 H5 Explorer 的角色、输出和执行边界。"""
    if not isinstance(content, str) or not content.strip():
        raise H5ExplorerWorkflowSkillConfigurationError("h5-explorer-workflow Skill 正文为空")
    frontmatter = re.match(r"\A---\r?\n(?P<body>.*?)\r?\n---\r?\n", content, re.DOTALL)
    if frontmatter is None:
        raise H5ExplorerWorkflowSkillConfigurationError(
            "h5-explorer-workflow Skill frontmatter 非法"
        )
    header = frontmatter.group("body")
    name = re.search(r"(?m)^name:\s*([^\r\n]+)$", header)
    version = re.search(r"(?m)^\s{2}version:\s*['\"]?([^'\"\r\n]+)", header)
    if name is None or name.group(1).strip() != "h5-explorer-workflow":
        raise H5ExplorerWorkflowSkillConfigurationError("h5-explorer-workflow Skill name 不匹配")
    if version is None or version.group(1).strip() != H5_EXPLORER_WORKFLOW_SKILL_VERSION:
        raise H5ExplorerWorkflowSkillConfigurationError(
            "h5-explorer-workflow Skill version 不兼容，要求 " + H5_EXPLORER_WORKFLOW_SKILL_VERSION
        )
    required_markers = (
        "页面类型覆盖缺口",
        "动态 Frontier",
        "ActionIntent",
        "ExplorationTerminal",
        "不得直接调用 Playwright",
        "source_capture_id",
        "frontier_snapshot_id",
        "state_version",
        "平台签发",
        "长页面",
        "最上层弹窗",
        "外部页面",
    )
    missing = [marker for marker in required_markers if marker not in content]
    if missing:
        raise H5ExplorerWorkflowSkillConfigurationError(
            "h5-explorer-workflow Skill 缺少强制边界内容：" + ", ".join(missing)
        )
    forbidden_markers = (
        "ProbeWork",
        "EvidenceRequest",
        "Rule Judge",
        "规则 Judge",
        "ruleset",
        "规则覆盖",
        "质检",
        "补证",
        "manual_review",
        "evidence_insufficient",
        "报告",
    )
    forbidden = [marker for marker in forbidden_markers if marker in content]
    if forbidden:
        raise H5ExplorerWorkflowSkillConfigurationError(
            "h5-explorer-workflow Skill 包含非探索语义：" + ", ".join(forbidden)
        )

    return content


def build_h5_explorer_context_provider() -> ContextProvider:
    """校验并只挂载 H5 Explorer 所需的只读探索 Skill。"""
    skill_path = _SKILLS_ROOT / "h5-explorer-workflow" / "SKILL.md"
    try:
        validate_h5_explorer_workflow_skill_content(skill_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise H5ExplorerWorkflowSkillConfigurationError(
            "H5 Explorer 缺少 h5-explorer-workflow/SKILL.md"
        ) from error
    return build_skills_context_provider(
        sub_dirs=("h5-explorer-workflow",),
        source_id="h5-explorer-workflow",
    )


def h5_shortflow_skill_hash(name: str) -> str:
    """只读取注册的短流程，内容身份跟随计划冻结而不跟随最新文件漂移。"""
    if not isinstance(name, str) or name not in {"h5-player-workflow", "h5-payment-workflow"}:
        raise ValueError("未注册的 H5 短流程 Skill")
    content = (_SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    header = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", content, re.DOTALL)
    if (header is None or not re.search(rf"(?m)^name: {re.escape(name)}$", header[1])
            or not re.search(r"(?m)^  version: '1\.0\.0'$", header[1])):
        raise ValueError("H5 短流程 Skill 身份或版本不兼容")
    base = (_SKILLS_ROOT / "h5-explorer-workflow" / "SKILL.md").read_text(encoding="utf-8")
    validate_h5_explorer_workflow_skill_content(base)
    return hashlib.sha256(base.encode("utf-8") + b"\0" + content.encode("utf-8")).hexdigest()


def build_h5_shortflow_context_provider(name: str) -> ContextProvider:
    """每次只挂载通用边界和当前流程，避免所有专项正文挤占上下文。"""
    h5_shortflow_skill_hash(name)
    build_h5_explorer_context_provider()
    return build_skills_context_provider(
        sub_dirs=("h5-explorer-workflow", name), source_id=name,
    )


def validate_web_explorer_workflow_skill_content(content: str) -> str:
    """校验 Web Explorer 的页型、证据和动作权限边界。"""

    if not isinstance(content, str) or not content.strip():
        raise WebExplorerWorkflowSkillConfigurationError(
            "web-explorer-workflow Skill 正文为空"
        )
    frontmatter = re.match(r"\A---\r?\n(?P<body>.*?)\r?\n---\r?\n", content, re.DOTALL)
    if frontmatter is None:
        raise WebExplorerWorkflowSkillConfigurationError(
            "web-explorer-workflow Skill frontmatter 非法"
        )
    header = frontmatter.group("body")
    name = re.search(r"(?m)^name:\s*([^\r\n]+)$", header)
    version = re.search(r"(?m)^\s{2}version:\s*['\"]?([^'\"\r\n]+)", header)
    if name is None or name.group(1).strip() != "web-explorer-workflow":
        raise WebExplorerWorkflowSkillConfigurationError(
            "web-explorer-workflow Skill name 不匹配"
        )
    if (
        version is None
        or version.group(1).strip() != WEB_EXPLORER_WORKFLOW_SKILL_VERSION
    ):
        raise WebExplorerWorkflowSkillConfigurationError(
            "web-explorer-workflow Skill version 不兼容，要求 "
            + WEB_EXPLORER_WORKFLOW_SKILL_VERSION
        )
    required_markers = (
        "当前可见层",
        "成年入口",
        "到达验证",
        "候选不是到达证明",
        "质检通知",
        "不得直接调用 Playwright",
        "video_list",
        "video_detail",
        "content_list",
        "content_detail",
    )
    missing = [marker for marker in required_markers if marker not in content]
    if missing:
        raise WebExplorerWorkflowSkillConfigurationError(
            "web-explorer-workflow Skill 缺少强制边界内容：" + ", ".join(missing)
        )
    forbidden_markers = (
        "heiliao.com",
        "hl365.com",
        "91jav1.com",
        "91porna.com",
        "huangguoai.com",
        "avhub360.com",
        "javascript:",
    )
    forbidden = [marker for marker in forbidden_markers if marker in content.casefold()]
    if forbidden:
        raise WebExplorerWorkflowSkillConfigurationError(
            "web-explorer-workflow Skill 含站点硬编码或脚本动作："
            + ", ".join(forbidden)
        )
    return content


def load_web_explorer_workflow_skill_content(
    skill_dir: Path | None = None,
) -> str:
    """确定性读取并校验 Web Explorer Workflow Skill 正文。"""

    directory = skill_dir or (_SKILLS_ROOT / "web-explorer-workflow")
    skill_path = directory / "SKILL.md"
    try:
        content = skill_path.read_text(encoding="utf-8")
    except OSError as error:
        raise WebExplorerWorkflowSkillConfigurationError(
            "Web Explorer 缺少 web-explorer-workflow/SKILL.md"
        ) from error
    return validate_web_explorer_workflow_skill_content(content)


def build_web_explorer_context_provider() -> ContextProvider:
    """校验并只挂载 Web V2 受控探索 Skill。"""

    load_web_explorer_workflow_skill_content()
    return build_skills_context_provider(
        sub_dirs=("web-explorer-workflow",),
        source_id="web-explorer-workflow",
    )
