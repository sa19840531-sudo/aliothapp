"""App Workflow 取消必须干净释放设备，不得 quarantine。"""

from __future__ import annotations

import ast
import inspect
import textwrap

from services.api.app.application.app_workflow import AppQcWorkflow


def test_cancel_releases_device_claim_without_quarantine() -> None:
    source = textwrap.dedent(inspect.getsource(AppQcWorkflow._cancel))
    tree = ast.parse(source)
    release_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "_release_device_claim":
            release_calls.append(node)
    assert len(release_calls) == 1
    keywords = {kw.arg: kw.value for kw in release_calls[0].keywords if kw.arg}
    assert "quarantine" in keywords
    value = keywords["quarantine"]
    assert isinstance(value, ast.Constant)
    assert value.value is False
