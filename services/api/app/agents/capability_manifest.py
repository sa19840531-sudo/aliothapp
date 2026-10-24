"""Agent Capability Manifest：汇总运行时 Tool 与 Agent Skill 的只读能力快照。"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from services.api.app.agents.tools.registry import ToolMetadata, ToolRegistry

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parent / "skills"
_FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(?P<body>.*?)\n---(?:\s*\n|\Z)", re.DOTALL)


class CapabilityKind(StrEnum):
    """Agent 能力类型。"""

    SKILL = "skill"
    TOOL = "tool"


@dataclass(frozen=True)
class CapabilityManifestEntry:
    """Manifest 中的一条能力定义。"""

    name: str
    kind: CapabilityKind
    description: str
    implementation_ref: str
    version: str = "1.0.0"
    runtime_permission: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)


class CapabilityManifestError(ValueError):
    """Capability Manifest 配置不合法。"""


def build_capability_manifest(
    *,
    registry: ToolRegistry,
    skills_root: Path = _DEFAULT_SKILLS_ROOT,
    reference_root: Path = _REPOSITORY_ROOT,
) -> tuple[CapabilityManifestEntry, ...]:
    """从实际 Tool 注册表和 Skill 文件构建唯一能力快照。"""
    entries = [_tool_entry(metadata) for metadata in registry.list_all()]
    entries.extend(
        _skill_entry(path, reference_root=reference_root)
        for path in sorted(skills_root.rglob("SKILL.md"))
    )
    _validate_unique_names(entries)
    return tuple(sorted(entries, key=lambda item: (item.kind.value, item.name)))


def _tool_entry(metadata: ToolMetadata) -> CapabilityManifestEntry:
    handler = metadata.tool.func
    module = getattr(handler, "__module__", "")
    name = getattr(handler, "__name__", "")
    if not module or not name:
        raise CapabilityManifestError(f"Tool 缺少实现引用：{metadata.name}")
    return CapabilityManifestEntry(
        name=metadata.name,
        kind=CapabilityKind.TOOL,
        description=metadata.description,
        implementation_ref=f"{module}:{name}",
        runtime_permission=metadata.permission_level.value,
        input_schema=metadata.tool.parameters(),
    )


def _skill_entry(path: Path, *, reference_root: Path) -> CapabilityManifestEntry:
    text = path.read_text(encoding="utf-8")
    frontmatter = _FRONTMATTER_PATTERN.match(text)
    if frontmatter is None:
        raise CapabilityManifestError(f"Skill 缺少有效 frontmatter：{path}")
    name = _frontmatter_value(frontmatter.group("body"), "name")
    description = _frontmatter_value(frontmatter.group("body"), "description")
    if not name or not description:
        raise CapabilityManifestError(f"Skill 缺少 name 或 description：{path}")
    try:
        implementation_ref = path.resolve().relative_to(reference_root.resolve()).as_posix()
    except ValueError as exc:
        raise CapabilityManifestError(f"Skill 实现路径不在仓库内：{path}") from exc
    return CapabilityManifestEntry(
        name=name,
        kind=CapabilityKind.SKILL,
        description=description,
        implementation_ref=implementation_ref,
    )


def _frontmatter_value(frontmatter: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", frontmatter, re.MULTILINE)
    return match.group(1).strip() if match else None


def _validate_unique_names(entries: list[CapabilityManifestEntry]) -> None:
    seen: set[str] = set()
    for item in entries:
        if item.name in seen:
            raise CapabilityManifestError(f"能力名称重复：{item.name}")
        seen.add(item.name)
