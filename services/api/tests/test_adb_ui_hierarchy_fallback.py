"""非空闲页面 UI Hierarchy 回退采集的离线合同测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from services.api.app.infrastructure.device import adb_ui_hierarchy_fallback as fallback

_ANDROID_DIR = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "infrastructure"
    / "device"
    / "android"
)
_JAR_PATH = _ANDROID_DIR / "current_hierarchy.jar"


def _fixed_tokens(monkeypatch: pytest.MonkeyPatch, *tokens: str) -> None:
    values = iter(tokens)
    monkeypatch.setattr(
        fallback,
        "uuid4",
        lambda: SimpleNamespace(hex=next(values)),
    )


def _cleanup_calls(calls: list[tuple[list[str], float]]) -> list[tuple[list[str], float]]:
    return [item for item in calls if item[0][:2] == ["rm", "-f"]]


def test_success_reads_current_nodes_marks_mode_and_cleans_each_unique_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功标志必须读取当前节点，并为每轮使用独立临时文件。"""

    _fixed_tokens(monkeypatch, "a" * 32, "b" * 32)
    shell_calls: list[tuple[list[str], float]] = []
    run_commands: list[list[str]] = []
    cat_commands: list[list[str]] = []
    logs: list[str] = []

    def shell(command: list[str], timeout: float) -> None:
        shell_calls.append((command, timeout))

    def shell_output(command: list[str], timeout: float) -> str:
        del timeout
        if command[0] == "env":
            run_commands.append(command)
            return "INSTRUMENTATION_STATUS_CODE: 0\nOK (1 test)\n"
        assert command[0] == "cat"
        cat_commands.append(command)
        return f'<hierarchy><node text="{command[1]}" /></hierarchy>'

    first = fallback.dump_current_ui_hierarchy(shell_output, shell, logs.append)
    second = fallback.dump_current_ui_hierarchy(shell_output, shell, logs.append)

    first_xml = "/data/local/tmp/alioth_uia_" + "a" * 32 + ".xml"
    second_xml = "/data/local/tmp/alioth_uia_" + "b" * 32 + ".xml"
    assert f'<node text="{first_xml}" />' in first
    assert f'<node text="{second_xml}" />' in second
    assert 'alioth-observation-mode="current_root_without_idle"' in first
    assert 'alioth-observation-mode="current_root_without_idle"' in second
    assert len(run_commands) == 2
    assert len(cat_commands) == 2
    assert run_commands[0] != run_commands[1]
    assert cat_commands == [
        ["cat", "/data/local/tmp/alioth_uia_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.xml"],
        ["cat", "/data/local/tmp/alioth_uia_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.xml"],
    ]
    cleanup = _cleanup_calls(shell_calls)
    assert cleanup == [
        (
            [
                "rm",
                "-f",
                "/data/local/tmp/alioth_uia_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jar",
                "/data/local/tmp/alioth_uia_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.xml",
            ],
            3.0,
        ),
        (
            [
                "rm",
                "-f",
                "/data/local/tmp/alioth_uia_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.jar",
                "/data/local/tmp/alioth_uia_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.xml",
            ],
            3.0,
        ),
    ]
    assert all(
        "com.alioth.observation.CurrentHierarchy#testDump" in command
        for command in run_commands
    )
    assert any("mode=current_root_without_idle" in message for message in logs)


@pytest.mark.parametrize(
    "runner_output",
    (
        "OK (1 test)\nshortMsg=Process crashed",
        "OK (1 test)\nFAILURES!!!\n",
    ),
)
def test_runner_failure_markers_reject_even_when_output_contains_ok_and_skip_cat(
    runner_output: str,
) -> None:
    """OK 旁边出现失败标志时不能把结果当成成功，也不能读取 XML。"""

    shell_calls: list[tuple[list[str], float]] = []
    output_commands: list[list[str]] = []

    def shell(command: list[str], timeout: float) -> None:
        shell_calls.append((command, timeout))

    def shell_output(command: list[str], timeout: float) -> str:
        del timeout
        output_commands.append(command)
        return runner_output

    with pytest.raises(RuntimeError, match="UIA_CURRENT_ROOT_DUMP_FAILED"):
        fallback.dump_current_ui_hierarchy(shell_output, shell, lambda _message: None)

    assert [command[0] for command in output_commands] == ["env"]
    assert len(_cleanup_calls(shell_calls)) == 1


@pytest.mark.parametrize(
    "xml_text",
    (
        "",
        "<hierarchy>",
        "<not-hierarchy><node /></not-hierarchy>",
        "<hierarchy />",
        "<hierarchy><label /></hierarchy>",
    ),
)
def test_success_marker_with_invalid_or_empty_hierarchy_is_rejected_and_cleaned(
    xml_text: str,
) -> None:
    """成功标志不能替代可解析的 hierarchy 根和至少一个节点。"""

    shell_calls: list[tuple[list[str], float]] = []
    output_commands: list[list[str]] = []

    def shell(command: list[str], timeout: float) -> None:
        shell_calls.append((command, timeout))

    def shell_output(command: list[str], timeout: float) -> str:
        del timeout
        output_commands.append(command)
        if command[0] == "env":
            return "OK (1 test)"
        return xml_text

    with pytest.raises(RuntimeError, match="UIA_CURRENT_ROOT_INVALID_XML"):
        fallback.dump_current_ui_hierarchy(shell_output, shell, lambda _message: None)

    assert [command[0] for command in output_commands] == ["env", "cat"]
    assert len(_cleanup_calls(shell_calls)) == 1


@pytest.mark.parametrize("failure_stage", ("write", "run", "cat"))
def test_write_run_and_cat_failures_cleanup_and_preserve_original_exception(
    failure_stage: str,
) -> None:
    """写入、运行和读取阶段的原始异常都必须在清理后继续抛出。"""

    original = RuntimeError(f"{failure_stage} failure")
    shell_calls: list[tuple[list[str], float]] = []
    output_commands: list[list[str]] = []

    def shell(command: list[str], timeout: float) -> None:
        shell_calls.append((command, timeout))
        if command[0] != "rm" and failure_stage == "write":
            raise original

    def shell_output(command: list[str], timeout: float) -> str:
        del timeout
        output_commands.append(command)
        if command[0] == "env":
            if failure_stage == "run":
                raise original
            return "OK (1 test)"
        if failure_stage == "cat":
            raise original
        return "<hierarchy><node text=\"当前\" /></hierarchy>"

    with pytest.raises(RuntimeError) as exc_info:
        fallback.dump_current_ui_hierarchy(shell_output, shell, lambda _message: None)

    assert exc_info.value is original
    assert len(_cleanup_calls(shell_calls)) == 1
    if failure_stage == "write":
        assert output_commands == []
    elif failure_stage == "run":
        assert [command[0] for command in output_commands] == ["env"]
    else:
        assert [command[0] for command in output_commands] == ["env", "cat"]


def test_cleanup_failure_does_not_override_success() -> None:
    """清理失败只能记录日志，不能改变已成功取得的节点。"""

    logs: list[str] = []

    def shell(command: list[str], timeout: float) -> None:
        del timeout
        if command[0] == "rm":
            raise OSError("cleanup unavailable")

    def shell_output(command: list[str], timeout: float) -> str:
        del timeout
        if command[0] == "env":
            return "OK (1 test)"
        return "<hierarchy><node text=\"当前节点\" /></hierarchy>"

    result = fallback.dump_current_ui_hierarchy(shell_output, shell, logs.append)

    assert "current_root_without_idle" in result
    assert any("cleanup skipped: OSError" in message for message in logs)


def test_packaged_jar_contains_valid_dex_and_matching_current_hierarchy_class() -> None:
    """随包 JAR 必须包含可识别 DEX，且类名与 Runner 入口一致。"""

    java_source = (_ANDROID_DIR / "CurrentHierarchy.java").read_text(encoding="utf-8")
    with ZipFile(_JAR_PATH) as archive:
        assert archive.testzip() is None
        assert "classes.dex" in archive.namelist()
        dex = archive.read("classes.dex")

    assert dex.startswith(b"dex\n035\x00")
    assert len(dex) > 128
    assert b"Lcom/alioth/observation/CurrentHierarchy;" in dex
    assert b"testDump" in dex
    assert b"dumpWindowHierarchy" in dex
    assert "package com.alioth.observation;" in java_source
    assert "public final class CurrentHierarchy" in java_source
    assert "void testDump()" in java_source
