"""有序 Skill 冻结、原文字节摘要及历史无磁盘消费的安全边界。"""

import shutil
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from services.api.app.application import green_rule_skills as module
from services.api.app.application.green_rule_skills import (
    GREEN_RULE_SKILL_NAMES,
    GreenRuleSkillConfigurationError,
    build_green_skill_program,
    validate_frozen_green_skill_program,
)


def test_all_programs_freeze_order_name_version_full_body_and_original_bytes():
    before = {
        name: (module._SKILLS_ROOT / path).read_bytes()
        for name, path in module._SKILL_PATHS.items()
    }
    for key, names in GREEN_RULE_SKILL_NAMES.items():
        program = build_green_skill_program(key)
        assert tuple(skill["name"] for skill in program["skills"]) == names
        for skill in program["skills"]:
            assert skill["body"].encode("utf-8") == before[skill["name"]]
            assert skill["sha256"] == sha256(before[skill["name"]]).hexdigest()
            if skill["name"].startswith("green-"):
                assert skill["version"] == "1.0.0"
            else:
                assert skill["version"] == "sha256:" + skill["sha256"]
        assert sha256(program["combined_body"].encode()).hexdigest() == program["sha256"]
        assert validate_frozen_green_skill_program(program, rule_key=key) == program
    assert before == {
        name: (module._SKILLS_ROOT / path).read_bytes()
        for name, path in module._SKILL_PATHS.items()
    }


def test_history_validation_never_reads_current_disk_and_returns_independent_copy(monkeypatch):
    frozen = build_green_skill_program("s35")
    original = deepcopy(frozen)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("历史冻结程序不得读取运行中磁盘")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    checked = validate_frozen_green_skill_program(frozen, rule_key="s35")
    assert checked == original
    checked["skills"][0]["body"] += "改变返回对象"
    assert frozen == original


@pytest.mark.parametrize(
    "fault", ["body", "sha", "version", "order", "combined", "extra", "empty", "wrong_rule"]
)
def test_saved_program_tampering_or_wrong_order_is_rejected(fault):
    program = build_green_skill_program("s72")
    rule_key = "s72"
    if fault == "body":
        program["skills"][0]["body"] += "\n篡改"
    elif fault == "sha":
        program["skills"][0]["sha256"] = "0" * 64
    elif fault == "version":
        program["skills"][0]["version"] = "2.0.0"
    elif fault == "order":
        program["skills"].reverse()
    elif fault == "combined":
        program["combined_body"] += "\n未冻结正文"
    elif fault == "extra":
        program["skills"][0]["source_path"] = "不应混入公开冻结数据"
    elif fault == "empty":
        program["skills"] = []
    else:
        rule_key = "62"
    with pytest.raises(GreenRuleSkillConfigurationError):
        validate_frozen_green_skill_program(program, rule_key=rule_key)


def test_new_version_reads_new_bytes_without_upgrading_saved_program(tmp_path):
    for relative in module._SKILL_PATHS.values():
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(module._SKILLS_ROOT / relative, destination)
    old = build_green_skill_program("s35", skills_root=tmp_path)
    path = tmp_path / module._SKILL_PATHS["interaction-rule"]
    path.write_bytes(path.read_bytes() + "\n后续分类规程改动\n".encode())
    new = build_green_skill_program("s35", skills_root=tmp_path)
    assert new["sha256"] != old["sha256"]
    assert new["skills"][0]["version"] != old["skills"][0]["version"]
    assert validate_frozen_green_skill_program(old, rule_key="s35") == old


def test_invalid_source_skill_name_and_unknown_program_cannot_be_loaded(tmp_path):
    path = tmp_path / "green-content-safety" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: unrelated-skill\n---\n不相关规程", encoding="utf-8")
    with pytest.raises(GreenRuleSkillConfigurationError):
        build_green_skill_program("102", skills_root=tmp_path)
    with pytest.raises(GreenRuleSkillConfigurationError):
        build_green_skill_program("../../arbitrary")
