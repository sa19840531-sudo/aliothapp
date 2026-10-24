"""绿色规则的有序冻结 Skill 程序；历史消费只验证保存正文，不重读磁盘。"""

import re
from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

GREEN_SKILL_PROGRAM_SCHEMA = "green-skill-program.v1"
GREEN_RULE_SKILL_NAMES = {
    "102": ("green-content-safety",),
    "s72": ("visual-rule", "green-process-evidence"),
    "41": ("interaction-rule",),
    "62": ("ad-rule", "green-process-evidence"),
    "s35": ("interaction-rule", "green-process-evidence"),
    "s49": ("interaction-rule", "green-process-evidence"),
    "s73": ("interaction-rule", "green-process-evidence"),
    "32": ("visual-rule",),
}
_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "agents" / "skills"
_SKILL_PATHS = {
    "visual-rule": "rule/visual-rule/SKILL.md",
    "interaction-rule": "rule/interaction-rule/SKILL.md",
    "ad-rule": "rule/ad-rule/SKILL.md",
    "green-content-safety": "green-content-safety/SKILL.md",
    "green-process-evidence": "green-process-evidence/SKILL.md",
}
_PROGRAM_FIELDS = {"schema_version", "skills", "combined_body", "sha256"}
_SKILL_FIELDS = {"name", "version", "body", "sha256"}


class GreenRuleSkillConfigurationError(ValueError):
    """新规则 Skill 来源或历史冻结程序不完整、不一致。"""


def build_green_skill_program(rule_key: str, *, skills_root: Path | None = None) -> dict:
    """仅在构造新版本时读取现行文件，逐字节冻结完整 UTF-8 正文。"""
    if not isinstance(rule_key, str):
        raise GreenRuleSkillConfigurationError("未声明的绿色规则 Skill 程序")
    names = GREEN_RULE_SKILL_NAMES.get(rule_key)
    if names is None:
        raise GreenRuleSkillConfigurationError("未声明的绿色规则 Skill 程序")
    root = skills_root if skills_root is not None else _SKILLS_ROOT
    skills = []
    for name in names:
        try:
            body = (root / _SKILL_PATHS[name]).read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise GreenRuleSkillConfigurationError(f"Skill {name} 缺失或不是 UTF-8") from exc
        digest = _sha256(body)
        declared_name, declared_version = _header(body)
        if declared_name != name:
            raise GreenRuleSkillConfigurationError(f"Skill {name} 的声明名称不一致")
        # 既有分类 Skill 没有版本号，以原始正文摘要作为版本，不改写旧文件。
        version = declared_version or "sha256:" + digest
        skills.append({"name": name, "version": version, "body": body, "sha256": digest})
    combined = _combine(skills)
    return validate_frozen_green_skill_program(
        {
            "schema_version": GREEN_SKILL_PROGRAM_SCHEMA,
            "skills": skills,
            "combined_body": combined,
            "sha256": _sha256(combined),
        },
        rule_key=rule_key,
    )


def validate_frozen_green_skill_program(program: object, *, rule_key: str | None = None) -> dict:
    """只校验保存的名称、版本、完整正文、顺序及摘要，绝不读取运行时磁盘。"""
    if rule_key is not None and (
        not isinstance(rule_key, str) or rule_key not in GREEN_RULE_SKILL_NAMES
    ):
        raise GreenRuleSkillConfigurationError("未声明的绿色规则 Skill 程序")
    if not isinstance(program, Mapping) or set(program) != _PROGRAM_FIELDS:
        raise GreenRuleSkillConfigurationError("冻结 Skill 程序字段不完整或含未声明字段")
    if program["schema_version"] != GREEN_SKILL_PROGRAM_SCHEMA:
        raise GreenRuleSkillConfigurationError("冻结 Skill 程序版本不受支持")
    skills = program["skills"]
    if not isinstance(skills, list) or not skills or len(skills) > 2:
        raise GreenRuleSkillConfigurationError("冻结 Skill 程序必须含有序完整 Skill")
    for skill in skills:
        if not isinstance(skill, Mapping) or set(skill) != _SKILL_FIELDS:
            raise GreenRuleSkillConfigurationError("冻结 Skill 字段不完整或含未声明字段")
        if not isinstance(skill["name"], str) or skill["name"] not in _SKILL_PATHS:
            raise GreenRuleSkillConfigurationError("冻结程序含未声明 Skill")
        body = skill["body"]
        if not isinstance(body, str) or not body.strip() or _sha256(body) != skill["sha256"]:
            raise GreenRuleSkillConfigurationError("冻结 Skill 正文为空或 SHA256 不一致")
        name, version = _header(body)
        if name != skill["name"] or skill["version"] != (version or "sha256:" + _sha256(body)):
            raise GreenRuleSkillConfigurationError("冻结 Skill 名称或版本与保存正文不一致")
    names = tuple(skill["name"] for skill in skills)
    allowed = (
        {GREEN_RULE_SKILL_NAMES[rule_key]}
        if rule_key in GREEN_RULE_SKILL_NAMES
        else set(GREEN_RULE_SKILL_NAMES.values())
    )
    if (rule_key is not None and rule_key not in GREEN_RULE_SKILL_NAMES) or names not in allowed:
        raise GreenRuleSkillConfigurationError("冻结 Skill 顺序与绿色规则声明不一致")
    combined = _combine(skills)
    if program["combined_body"] != combined or program["sha256"] != _sha256(combined):
        raise GreenRuleSkillConfigurationError("冻结 Skill 组合正文或 SHA256 不一致")
    return deepcopy(dict(program))


def _header(body: str) -> tuple[str, str | None]:
    header = re.match(r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|$)", body, re.DOTALL)
    if header is None:
        raise GreenRuleSkillConfigurationError("Skill 缺少 YAML 声明")
    # 只在解析声明时统一换行；冻结正文及其字节摘要始终保留原始 CRLF/LF。
    metadata = header.group(1).replace("\r\n", "\n")
    names = re.findall(r"(?m)^name:\s*([^\r\n]+)$", metadata)
    versions = re.findall(r"(?m)^  version:\s*([^\r\n]+)$", metadata)
    if len(names) != 1 or len(versions) > 1:
        raise GreenRuleSkillConfigurationError("Skill 名称或版本声明不唯一")
    name = names[0].strip().strip("\"'")
    version = versions[0].strip().strip("\"'") if versions else None
    if version is not None and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise GreenRuleSkillConfigurationError("Skill 显式版本必须是三段数字")
    return name, version


def _combine(skills) -> str:
    return "\n\n".join(
        f"# 冻结 Skill {index}：{skill['name']}\n版本：{skill['version']}\n"
        f"SHA256：{skill['sha256']}\n\n{skill['body']}"
        for index, skill in enumerate(skills, 1)
    )


def _sha256(body: str) -> str:
    return sha256(body.encode("utf-8")).hexdigest()
