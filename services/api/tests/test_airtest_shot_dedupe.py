"""結果截圖同步：欄目流水圖不因畫面近似被吞掉。"""

from pathlib import Path

import pytest
from PIL import Image

md = pytest.importorskip(
    "services.api.app.api.v1.mobile_demo",
    reason="Mobile Demo API 已下线；旧 Airtest 私有 helper 不再作为正式质检链路门禁",
)


def _write_solid_png(path: Path, color: tuple[int, int, int]) -> None:
    # 須 >512 bytes，否則 _file_size_stable 會拒絕同步
    Image.new("RGB", (320, 320), color).save(path, optimize=False)


def test_column_shots_not_visually_deduped(tmp_path: Path, monkeypatch) -> None:
    run_id = "dedupe-run"
    air = tmp_path / "untitled.air"
    air.mkdir()
    shots = tmp_path / "shots"
    shots.mkdir()

    # 兩張幾乎同色（視覺去重會判相似），但檔名是欄目流水
    _write_solid_png(air / "shequ_top.png", (40, 40, 40))
    _write_solid_png(air / "shequ_scroll_1.png", (42, 42, 42))
    _write_solid_png(air / "after_skip.png", (40, 40, 40))
    _write_solid_png(air / "inner_ad_1.png", (41, 41, 41))

    monkeypatch.setattr(md, "_run_shots_dir", lambda *_a, **_k: shots)
    monkeypatch.setattr(md, "_run_materialized_air_dirs", lambda *_a, **_k: [air])
    monkeypatch.setattr(md, "_airtest_dir_readable", lambda *_a, **_k: True)
    monkeypatch.setattr(md, "_append_log", lambda *_a, **_k: None)

    names = md._copy_result_screenshots_to_run(
        run_id,
        since_mtime=0.0,
        clear_first=False,
    )
    assert "shequ_top.png" in names
    assert "shequ_scroll_1.png" in names
    # 過渡雜訊圖仍可被視覺去重（只留一張）
    noise = [n for n in names if n in ("after_skip.png", "inner_ad_1.png")]
    assert len(noise) <= 1


def test_should_visual_dedupe_shot_allowlist() -> None:
    assert md._should_visual_dedupe_shot("after_skip.png")
    assert md._should_visual_dedupe_shot("inner_ad_2.png")
    assert not md._should_visual_dedupe_shot("shequ_top.png")
    assert not md._should_visual_dedupe_shot("chrome_after_ad_click3.png")
