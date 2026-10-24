"""雷電多機型 profiles／list2 解析測試。"""

import json
from pathlib import Path

import pytest

from services.api.app.infrastructure.device.ldplayer import (
    LdPlayerError,
    parse_ldconsole_list2,
    resolve_ldplayer_profiles,
)


def test_resolve_profiles_fallback_index(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALIOTH_LDPLAYER_PROFILES", raising=False)
    monkeypatch.delenv("ALIOTH_LDPLAYER_PROFILES_FILE", raising=False)
    monkeypatch.setenv("ALIOTH_LDPLAYER_INDEX", "3")
    profiles = resolve_ldplayer_profiles()
    assert len(profiles) == 1
    assert profiles[0].id == "default"
    assert profiles[0].index == 3


def test_resolve_profiles_cwd_local_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ALIOTH_LDPLAYER_PROFILES", raising=False)
    monkeypatch.delenv("ALIOTH_LDPLAYER_PROFILES_FILE", raising=False)
    env_dir = tmp_path / "infra" / "env"
    env_dir.mkdir(parents=True)
    (env_dir / "ldplayer-profiles.local.json").write_text(
        json.dumps(
            [
                {"id": "a", "index": 0},
                {"id": "b", "index": 1},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    profiles = resolve_ldplayer_profiles()
    assert [p.id for p in profiles] == ["a", "b"]


def test_resolve_profiles_from_inline_json(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_LDPLAYER_PROFILES_FILE", raising=False)
    payload = [
        {"id": "phone_1080", "index": 0, "label": "手機", "width": 1080, "height": 1920},
        {"id": "tablet_1600", "index": 1, "label": "平板", "width": 1600, "height": 2560},
    ]
    monkeypatch.setenv("ALIOTH_LDPLAYER_PROFILES", json.dumps(payload, ensure_ascii=False))
    profiles = resolve_ldplayer_profiles()
    assert [p.id for p in profiles] == ["phone_1080", "tablet_1600"]
    assert profiles[0].width == 1080
    assert profiles[1].index == 1


def test_resolve_profiles_from_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ALIOTH_LDPLAYER_PROFILES", raising=False)
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            [{"id": "phone_1080", "index": 2, "label": "手機 1080x1920", "width": 1080, "height": 1920}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ALIOTH_LDPLAYER_PROFILES_FILE", str(path))
    profiles = resolve_ldplayer_profiles()
    assert len(profiles) == 1
    assert profiles[0].index == 2
    assert profiles[0].height == 1920


def test_resolve_profiles_reject_duplicate_id(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_LDPLAYER_PROFILES_FILE", raising=False)
    monkeypatch.setenv(
        "ALIOTH_LDPLAYER_PROFILES",
        json.dumps(
            [
                {"id": "same", "index": 0},
                {"id": "same", "index": 1},
            ]
        ),
    )
    with pytest.raises(LdPlayerError, match="重複"):
        resolve_ldplayer_profiles()


def test_resolve_profiles_reject_duplicate_index(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_LDPLAYER_PROFILES_FILE", raising=False)
    monkeypatch.setenv(
        "ALIOTH_LDPLAYER_PROFILES",
        json.dumps(
            [
                {"id": "a", "index": 0},
                {"id": "b", "index": 0},
            ]
        ),
    )
    with pytest.raises(LdPlayerError, match="重複 profile index"):
        resolve_ldplayer_profiles()


def test_resolve_profiles_reject_negative_index(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_LDPLAYER_PROFILES_FILE", raising=False)
    monkeypatch.setenv(
        "ALIOTH_LDPLAYER_PROFILES",
        json.dumps([{"id": "bad", "index": -1}]),
    )
    with pytest.raises(LdPlayerError, match="不可為負數"):
        resolve_ldplayer_profiles()


def test_resolve_profiles_reject_non_positive_size(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_LDPLAYER_PROFILES_FILE", raising=False)
    monkeypatch.setenv(
        "ALIOTH_LDPLAYER_PROFILES",
        json.dumps([{"id": "bad", "index": 0, "width": 0, "height": 1920}]),
    )
    with pytest.raises(LdPlayerError, match="width"):
        resolve_ldplayer_profiles()


def test_parse_ldconsole_list2() -> None:
    raw = """
0,leidian0,雷电模拟器,1,5554,-1,-1
1,leidian1,雷电模拟器-1,0,0,-1,-1
not-a-row
"""
    items = parse_ldconsole_list2(raw)
    assert [(i.index, i.name) for i in items] == [(0, "leidian0"), (1, "leidian1")]


def test_resolve_profiles_windows_path_falls_back_to_cwd(
    monkeypatch, tmp_path: Path
) -> None:
    """Windows 宿主路徑在當前環境不存在時，回退讀 cwd/infra/env 同名檔。"""
    monkeypatch.delenv("ALIOTH_LDPLAYER_PROFILES", raising=False)
    env_dir = tmp_path / "infra" / "env"
    env_dir.mkdir(parents=True)
    (env_dir / "matrix-test-profiles.json").write_text(
        json.dumps([{"id": "mapped", "index": 0}]),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "ALIOTH_LDPLAYER_PROFILES_FILE",
        "F:/no-such-drive/infra/env/matrix-test-profiles.json",
    )
    profiles = resolve_ldplayer_profiles()
    assert len(profiles) == 1
    assert profiles[0].id == "mapped"
