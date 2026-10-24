"""物化時跳過 untitled6 桌面入口，避免與 worker am start 衝突。"""

from __future__ import annotations

from pathlib import Path

import pytest

md = pytest.importorskip(
    "services.api.app.api.v1.mobile_demo",
    reason="Mobile Demo API 已下线；旧 Airtest 私有 helper 不再作为正式质检链路门禁",
)


def test_rewrite_untitled6_entry_block(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    logs: list[str] = []
    monkeypatch.setattr(md, "_append_log", lambda _rid, msg: logs.append(msg))

    script = tmp_path / "untitled6.py"
    script.write_text(
        "# -*- encoding=utf8 -*-\n"
        "sleep(1)\n"
        "# 1) 桌面入口\n"
        "entry = local_tpl('tpl1785570146091.png')\n"
        "if entry_pos:\n"
        "    touch(entry_pos)\n"
        "else:\n"
        "    touch(record_pos_to_abs((0.004, -0.028), (1920, 1080)))\n"
        "sleep(2)\n"
        "# 2) 線路檢測：不點直连／重装\n"
        "wait_until_not_loading()\n",
        encoding="utf-8",
    )
    md._rewrite_airtest_skip_launcher_icon(script, package_name="sg.demo", run_id="r1")
    text = script.read_text(encoding="utf-8")
    assert "tpl1785570146091.png" not in text
    assert "App 已由 worker" in text
    assert "# 2) 線路檢測" in text
    assert any("entry block rewritten" in x for x in logs)
